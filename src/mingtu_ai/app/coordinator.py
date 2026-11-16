"""Application-shell coordination for one-shot and watched file analysis."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from typing import Any, Protocol
import uuid

from mingtu_ai import (
    AnalysisInput, AppConfig, BatchAnalysisResult, BatchItemProposal, BatchPreset, NamingSuggestion,
)
from mingtu_ai.adapters import AdaptedFile, FileChangedError, adapt_file
from mingtu_ai.adapters.secure_input import FileIdentity, identity_from_stat
from mingtu_ai.batch import apply_naming_preset, scan_batch_candidates
from mingtu_ai.desktop_paths import save_user_watch_directories
from mingtu_ai.history import (
    HistoryEvent, HistoryStore, RenamePostCommitError, RenameRecoveryRequiredError,
)
from mingtu_ai.naming import sanitize_stem, unique_destination
from mingtu_ai.notifications import NotificationService, has_desktop_session
from mingtu_ai.providers import LocalGemmaProvider
from mingtu_ai.queue import QueueTask, SingleWorkerQueue, TaskState
from mingtu_ai.runtime import LlamaServerRuntime
from mingtu_ai.watcher import (
    FileEventMonitor,
    InotifyWatcher,
    OwnedMoveSuppressor,
    WatchInvalidatedError,
    WatchOverflowError,
    should_ignore,
    wait_until_stable,
)


class ProcessingError(RuntimeError):
    """A one-shot file did not produce a validated suggestion."""


@dataclass(frozen=True, slots=True)
class SuggestionProposal:
    source_path: Path
    suggested_filename: str
    suggestion: NamingSuggestion
    history_event_id: int
    source_identity: tuple[int, int, int, int] | None = None


@dataclass(frozen=True, slots=True)
class PendingProposalSnapshot:
    """Immutable UI view of one undecided suggestion from this process."""

    history_event_id: int
    source_path: Path
    suggested_filename: str
    suggested_stem: str


MAX_PENDING_PROPOSALS = 256
MAX_WATCH_ANALYSIS_RETRIES = 2
WATCH_RETRY_DEDUP_SECONDS = 2.0


class Provider(Protocol):
    def analyze(self, item: AnalysisInput) -> NamingSuggestion: ...


RuntimeFactory = Callable[[], AbstractContextManager[Any]]
ProviderFactory = Callable[[Any], Provider]
AdapterFactory = Callable[[Path], AbstractContextManager[AdaptedFile]]
MonitorFactory = Callable[[Sequence[Path]], AbstractContextManager[Any]]
EditName = Callable[[SuggestionProposal], str | None]
FolderOpener = Callable[[Path], None]


@contextmanager
def _default_runtime_factory(config: AppConfig) -> Iterator[LlamaServerRuntime]:
    runtime = LlamaServerRuntime(config.llama)
    try:
        # Queue handlers call runtime.job(), which starts the model only when the
        # first provider request actually begins.
        yield runtime
    finally:
        runtime.shutdown()


@contextmanager
def _default_monitor_factory(
    directories: Sequence[Path],
    owned_moves: OwnedMoveSuppressor,
) -> Iterator[FileEventMonitor]:
    with InotifyWatcher(directories) as watcher:
        yield FileEventMonitor(watcher, owned_moves=owned_moves)


@contextmanager
def _provider_scope(provider: Provider) -> Iterator[Provider]:
    enter = getattr(provider, "__enter__", None)
    exit_method = getattr(provider, "__exit__", None)
    if callable(enter) and callable(exit_method):
        with provider:  # type: ignore[attr-defined]
            yield provider
        return
    try:
        yield provider
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            close()


class ApplicationCoordinator:
    """Wire adapters, model runtime, queue, history, and desktop actions."""

    def __init__(
        self,
        *,
        config: AppConfig,
        watch_directories: Sequence[Path | str],
        history: HistoryStore,
        notifications: NotificationService,
        runtime_factory: RuntimeFactory | None = None,
        provider_factory: ProviderFactory | None = None,
        adapter_factory: AdapterFactory = adapt_file,
        monitor_factory: MonitorFactory | None = None,
        owned_moves: OwnedMoveSuppressor | None = None,
        edit_name: EditName | None = None,
        open_folder: FolderOpener | None = None,
    ) -> None:
        self.config = config
        self._watch_directories: list[Path] = [
            Path(path).expanduser().absolute() for path in watch_directories
        ]
        self._active_monitor: Any = None
        self.history = history
        self.notifications = notifications
        self._runtime_factory = runtime_factory or (lambda: _default_runtime_factory(config))
        self._provider_factory = provider_factory or (
            lambda runtime: LocalGemmaProvider(runtime, config.provider)
        )
        self._adapter_factory = adapter_factory
        self._owned_moves = owned_moves or OwnedMoveSuppressor()
        self._monitor_factory = monitor_factory or (
            lambda directories: _default_monitor_factory(directories, self._owned_moves)
        )
        self._edit_name = edit_name or self._kdialog_edit_name
        self._open_folder = open_folder or self._xdg_open_folder
        self._failed_task_ids: set[str] = set()
        self._failure_lock = threading.Lock()
        self._decision_lock = threading.RLock()
        self._pending_proposals: dict[int, SuggestionProposal] = {}
        self._batch_options: dict[Path, tuple[str, str, str | None]] = {}
        self._paused = threading.Event()
        self._recent_watch_retries: OrderedDict[Path, tuple[FileIdentity, float]] = OrderedDict()
        self._queue: SingleWorkerQueue[Any, SuggestionProposal | None] = SingleWorkerQueue(
            runtime_factory=self._runtime_factory,
            handler=self._analyze,
            on_change=self._on_task_change,
            max_pending_tasks=config.max_pending_tasks,
        )

    @property
    def watch_directories(self) -> tuple[Path, ...]:
        with self._decision_lock:
            return tuple(self._watch_directories)

    def add_watch_directory(self, directory: Path | str) -> tuple[Path, ...]:
        candidate = Path(directory).expanduser().absolute()
        if candidate.is_symlink() or not candidate.is_dir():
            raise NotADirectoryError(f"無效的監控目錄：{candidate}")
        with self._decision_lock:
            if candidate not in self._watch_directories:
                self._watch_directories.append(candidate)
                if self._active_monitor is not None and hasattr(self._active_monitor, "watcher"):
                    self._active_monitor.watcher.add_directory(candidate)
                save_user_watch_directories(self._watch_directories)
            return tuple(self._watch_directories)

    def remove_watch_directory(self, directory: Path | str) -> tuple[Path, ...]:
        candidate = Path(directory).expanduser().absolute()
        with self._decision_lock:
            if candidate in self._watch_directories:
                self._watch_directories.remove(candidate)
                if self._active_monitor is not None and hasattr(self._active_monitor, "watcher"):
                    try:
                        self._active_monitor.watcher.remove_directory(candidate)
                    except (KeyError, OSError):
                        pass
                save_user_watch_directories(self._watch_directories)
            return tuple(self._watch_directories)

    @property
    def pending_count(self) -> int:
        return self._queue.pending_count

    @property
    def is_active(self) -> bool:
        return self._queue.is_active

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    def pause(self) -> None:
        """Pause intake of new filesystem events without touching other processes."""

        self._paused.set()

    def resume(self) -> None:
        self._paused.clear()

    def toggle_paused(self) -> bool:
        if self.is_paused:
            self.resume()
        else:
            self.pause()
        return self.is_paused

    def analyze_once(self, path: Path | str) -> SuggestionProposal:
        source = Path(path).expanduser().absolute()
        if should_ignore(source):
            raise ProcessingError(f"不處理目錄、符號連結、隱藏或未完成檔案：{source}")
        task = self._queue.submit(source)
        self._queue.run_until_idle()
        if task.state is not TaskState.SUCCEEDED or task.result is None:
            raise ProcessingError(task.error or "處理失敗")
        return task.result

    def analyze_batch(
        self,
        directory: Path | str,
        *,
        recursive: bool = False,
        preset: BatchPreset | str = BatchPreset.DEFAULT,
        hint: str | None = None,
        progress_callback: Callable[[int, int, Path], None] | None = None,
    ) -> BatchAnalysisResult:
        dir_path = Path(directory).expanduser().absolute()
        candidates = scan_batch_candidates(
            dir_path,
            recursive=recursive,
            history_path=self.history.database_path,
        )
        total = len(candidates)
        batch_id = f"batch_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        items: list[BatchItemProposal] = []
        failures: dict[str, str] = {}
        processed = 0

        for index, path in enumerate(candidates, start=1):
            if progress_callback is not None:
                try:
                    progress_callback(index - 1, total, path)
                except Exception:
                    pass

            with self._decision_lock:
                self._batch_options[path] = (batch_id, str(preset), hint)

            task = self._queue.submit(path)
            self._queue.run_until_idle()

            if task.state is TaskState.SUCCEEDED and task.result is not None:
                prop = task.result
                items.append(
                    BatchItemProposal(
                        source_path=prop.source_path,
                        suggested_filename=prop.suggested_filename,
                        suggested_stem=prop.suggestion.suggested_stem,
                        history_event_id=prop.history_event_id,
                        category=prop.suggestion.category,
                        keywords=prop.suggestion.keywords,
                        confidence=prop.suggestion.confidence,
                    )
                )
                processed += 1
            else:
                failures[str(path)] = task.error or "處理失敗"

            if progress_callback is not None:
                try:
                    progress_callback(index, total, path)
                except Exception:
                    pass

        res = BatchAnalysisResult(
            batch_id=batch_id,
            directory=dir_path,
            total_files=total,
            processed_files=processed,
            failed_files=len(failures),
            items=items,
            failures=failures,
        )
        if processed > 0 or len(failures) > 0:
            self.notifications.notify_batch_analysis_complete(
                processed, total, failed_files=len(failures)
            )
        return res

    def accept_batch(
        self,
        batch_id: str | None = None,
        proposals: Sequence[SuggestionProposal] | None = None,
    ) -> list[HistoryEvent]:
        accepted_events: list[HistoryEvent] = []
        with self._decision_lock:
            target_proposals: list[SuggestionProposal] = []
            if proposals is not None:
                target_proposals = list(proposals)
            elif batch_id is not None:
                for item in list(self._pending_proposals.values()):
                    history_event = self.history.get(item.history_event_id)
                    if history_event and history_event.metadata.get("batch_id") == batch_id:
                        target_proposals.append(item)
            else:
                target_proposals = list(self._pending_proposals.values())

            for proposal in target_proposals:
                event = self._accept_pending(proposal, edited_stem=None, notify_desktop=False)
                if event is not None:
                    accepted_events.append(event)

        if accepted_events:
            effective_batch_id = batch_id or (
                accepted_events[0].metadata.get("batch_id") if accepted_events[0].metadata else None
            )
            on_undo = (
                (lambda: self.undo_batch(effective_batch_id))
                if effective_batch_id
                else None
            )
            self.notifications.notify_batch_renamed(len(accepted_events), on_undo=on_undo)
        return accepted_events

    def undo_batch(self, batch_id: str) -> list[HistoryEvent]:
        with self._decision_lock:
            undone = self.history.undo_batch(batch_id)
        if undone:
            self.notifications.notify_batch_restored(len(undone))
        return undone

    def run_forever(self, stop_event: threading.Event | None = None) -> None:
        if not self.watch_directories:
            raise ValueError("常駐監控至少需要一個目錄")
        stopper = stop_event or threading.Event()
        self._queue.start(handler=self._analyze_watched)
        try:
            with self._monitor_factory(self.watch_directories) as monitor:
                self._active_monitor = monitor
                try:
                    while not stopper.is_set():
                        try:
                            paths = monitor.poll(timeout=0.5)
                        except (WatchOverflowError, WatchInvalidatedError) as exc:
                            self.notifications.notify_error(str(exc))
                            raise
                        if self.is_paused:
                            continue
                        for path in paths:
                            self._queue.submit(path)
                finally:
                    self._active_monitor = None
        finally:
            self._queue.stop(wait=True, drain=True)

    def close(self) -> None:
        self._queue.stop(wait=True, drain=True)
        self.notifications.close()

    def _analyze_watched(
        self, task: QueueTask[SuggestionProposal | None], runtime: Any,
    ) -> SuggestionProposal | None:
        path = task.path
        retries_left = MAX_WATCH_ANALYSIS_RETRIES
        while True:
            try:
                identity = self._analysis_identity(path)
                recent = self._recent_watch_retries.get(path)
                if recent is not None:
                    retried_identity, expires_at = recent
                    # Use admission time so jobs waiting behind another inference
                    # still coalesce with the retry that completed their version.
                    if identity == retried_identity and task.created_at <= expires_at:
                        return None
                proposal = self._analyze(path, runtime, expected_identity=identity)
            except FileChangedError:
                if retries_left and wait_until_stable(path):
                    retries_left -= 1
                    continue
                if should_ignore(path):
                    return None
                raise
            break
        if retries_left < MAX_WATCH_ANALYSIS_RETRIES:
            self._recent_watch_retries[path] = (identity, time.time() + WATCH_RETRY_DEDUP_SECONDS)
            self._recent_watch_retries.move_to_end(path)
            if len(self._recent_watch_retries) > self._queue.max_pending_tasks:
                self._recent_watch_retries.popitem(last=False)
        return proposal

    def _analyze(
        self, path: Path, runtime: Any, *, expected_identity: FileIdentity | None = None,
    ) -> SuggestionProposal:
        with self._decision_lock:
            self._check_proposal_capacity()
            batch_info = self._batch_options.pop(path, None)
        identity_before = expected_identity or self._analysis_identity(path)
        with self._adapter_factory(path) as adapted:
            authoritative_identity = adapted.source_identity or identity_before
            if authoritative_identity != identity_before:
                raise FileChangedError("檔案在安全開啟前已變更")
            request = AnalysisInput(
                source_path=adapted.source_path,
                mime_type=adapted.mime_type,
                kind=adapted.kind.value,
                text=adapted.text,
                image_paths=adapted.image_paths,
                image_file_descriptors=adapted.image_file_descriptors,
                metadata=adapted.metadata,
            )
            provider = self._provider_factory(runtime)
            with _provider_scope(provider) as scoped_provider:
                suggestion = scoped_provider.analyze(request)
        identity_after = self._analysis_identity(path)
        if identity_after != authoritative_identity:
            raise FileChangedError("檔案在分析期間已變更")

        safe_stem = sanitize_stem(suggestion.suggested_stem, fallback=path.stem)
        if batch_info and batch_info[1] != BatchPreset.DEFAULT:
            safe_stem = apply_naming_preset(
                safe_stem, path, preset=batch_info[1], category=suggestion.category
            )
            safe_stem = sanitize_stem(safe_stem, fallback=path.stem)

        filename = self._preview_available_filename(path, safe_stem)
        meta = {
            "detected_language": suggestion.detected_language,
            "category": suggestion.category,
            "keywords": suggestion.keywords,
            "confidence": suggestion.confidence,
            "adapter_kind": request.kind,
            "adapter_warnings": list(adapted.warnings),
        }
        if batch_info:
            meta["batch_id"] = batch_info[0]
            meta["preset"] = batch_info[1]
            if batch_info[2]:
                meta["hint"] = batch_info[2]

        with self._decision_lock:
            self._check_proposal_capacity()
            history_id = self.history.record_suggestion(
                path,
                filename,
                metadata=meta,
            )
            proposal = SuggestionProposal(
                path,
                filename,
                suggestion,
                history_id,
                source_identity=identity_after,
            )
            # Keep a private copy: --once callers still receive the historical
            # proposal type, whose Pydantic suggestion is mutable.
            self._pending_proposals[history_id] = SuggestionProposal(
                path, filename, suggestion.model_copy(deep=True), history_id,
                source_identity=identity_after,
            )
        if not batch_info:
            self.notifications.notify_suggestion(
                filename,
                on_accept=lambda: self.accept(proposal),
                on_edit=lambda: self.edit(proposal),
                on_ignore=lambda: self.ignore(proposal),
            )
        return proposal

    def _check_proposal_capacity(self) -> None:
        if len(self._pending_proposals) >= MAX_PENDING_PROPOSALS:
            raise ProcessingError(
                f"待決建議已達 {MAX_PENDING_PROPOSALS} 筆；請先接受或忽略建議，"
                "再手動重新分析此檔案。原檔保持不變。"
            )

    def list_pending_proposals(self) -> tuple[PendingProposalSnapshot, ...]:
        """Return a bounded immutable snapshot; proposals do not survive restart."""

        with self._decision_lock:
            return tuple(
                PendingProposalSnapshot(
                    item.history_event_id, item.source_path,
                    item.suggested_filename, item.suggestion.suggested_stem,
                )
                for item in self._pending_proposals.values()
            )

    @staticmethod
    def _preview_available_filename(path: Path, safe_stem: str) -> str:
        """Show the likely no-clobber name before the user accepts it.

        The actual rename still performs an atomic no-replace claim, so a race
        after this preview can safely advance the final name again.
        """

        for index in range(1, 1_000_000):
            candidate = unique_destination(path, safe_stem, index=index)
            if candidate == path or not (candidate.exists() or candidate.is_symlink()):
                return candidate.name
        raise ProcessingError("找不到可用的建議檔名")

    def accept(
        self,
        proposal: SuggestionProposal,
        *,
        edited_stem: str | None = None,
        notify_desktop: bool = True,
    ) -> HistoryEvent | None:
        """Accept through the same single-decision path used by the UI."""

        return self.accept_proposal(
            proposal.history_event_id, edited_stem=edited_stem, notify_desktop=notify_desktop
        )

    def accept_proposal(
        self,
        event_id: int,
        *,
        edited_stem: str | None = None,
        notify_desktop: bool = True,
    ) -> HistoryEvent | None:
        """Accept a still-pending ID once, retaining recoverable failures."""

        with self._decision_lock:
            proposal = self._pending_proposals.get(event_id)
            if proposal is None:
                return None
            return self._accept_pending(
                proposal, edited_stem=edited_stem, notify_desktop=notify_desktop
            )

    def _accept_pending(
        self,
        proposal: SuggestionProposal,
        *,
        edited_stem: str | None,
        notify_desktop: bool = True,
    ) -> HistoryEvent | None:
        stem = sanitize_stem(
            edited_stem if edited_stem is not None else proposal.suggestion.suggested_stem,
            fallback=proposal.source_path.stem,
        )
        try:
            if (
                proposal.source_identity is not None
                and self._source_identity(proposal.source_path) != proposal.source_identity
            ):
                raise ProcessingError("原檔案在建議後已移動或變更，已取消改名")
            requested_filename = unique_destination(proposal.source_path, stem).name
            with self._owned_moves.move_guard():
                if requested_filename == proposal.source_path.name:
                    rename_event = None
                else:
                    try:
                        rename_event = self.history.rename_file(
                            proposal.source_path,
                            requested_filename,
                            suggestion_event_id=proposal.history_event_id,
                            expected_source_identity=proposal.source_identity,
                        )
                    except RenamePostCommitError as exc:
                        # The filesystem move is already committed and has a
                        # durable PENDING ledger. Register it before releasing
                        # the watcher lock, then surface the history failure.
                        self._owned_moves.register(proposal.source_path, exc.target_path)
                        raise
                    assert rename_event.target_path is not None
                    self._owned_moves.register(
                        proposal.source_path,
                        rename_event.target_path,
                    )
            if rename_event is None:
                self.history.record_ignored(
                    proposal.source_path,
                    "suggested filename was unchanged",
                    suggestion_event_id=proposal.history_event_id,
                )
                self._pending_proposals.pop(proposal.history_event_id, None)
                return None
        except Exception as exc:
            if isinstance(exc, (RenamePostCommitError, RenameRecoveryRequiredError)) or not self._proposal_source_is_current(proposal):
                self._pending_proposals.pop(proposal.history_event_id, None)
            self._record_operation_failure(proposal.source_path, "rename", exc)
            return None

        self._pending_proposals.pop(proposal.history_event_id, None)
        target = rename_event.target_path
        assert target is not None
        if notify_desktop:
            self.notifications.notify_renamed(
                target.name,
                on_undo=lambda: self.undo(rename_event.id),
                on_open_folder=lambda: self.open_folder(target),
            )
        return rename_event

    @staticmethod
    def _source_identity(path: Path) -> FileIdentity:
        return identity_from_stat(path.lstat())

    def _analysis_identity(self, path: Path) -> FileIdentity:
        try:
            return self._source_identity(path)
        except FileNotFoundError as error:
            raise FileChangedError("檔案已移動或移除") from error

    def edit(self, proposal: SuggestionProposal) -> HistoryEvent | None:
        with self._decision_lock:
            current = self._pending_proposals.get(proposal.history_event_id)
            if current is None:
                return None
            editable = SuggestionProposal(
                current.source_path, current.suggested_filename,
                current.suggestion.model_copy(deep=True), current.history_event_id,
                source_identity=current.source_identity,
            )
        try:
            # Do not hold the decision lock while a user has the dialog open.
            # Its eventual accept rechecks that another action has not won.
            edited = self._edit_name(editable)
        except Exception as exc:
            self._record_operation_failure(proposal.source_path, "edit", exc)
            return None
        if edited is None or not edited.strip():
            return None
        return self.accept(proposal, edited_stem=edited.strip())

    def ignore(self, proposal: SuggestionProposal) -> int | None:
        return self.ignore_proposal(proposal.history_event_id)

    def ignore_proposal(self, event_id: int) -> int | None:
        """Ignore a pending ID once without touching the source file."""

        with self._decision_lock:
            proposal = self._pending_proposals.get(event_id)
            if proposal is None:
                return None
            try:
                ignored_id = self.history.record_ignored(
                    proposal.source_path, "user", suggestion_event_id=event_id,
                )
            except Exception as exc:
                self._record_operation_failure(proposal.source_path, "ignore", exc)
                return None
            self._pending_proposals.pop(event_id, None)
            return ignored_id

    def _proposal_source_is_current(self, proposal: SuggestionProposal) -> bool:
        try:
            return (
                proposal.source_identity is not None
                and self._source_identity(proposal.source_path) == proposal.source_identity
                and not should_ignore(proposal.source_path)
            )
        except OSError:
            return False

    def undo(self, rename_event_id: int) -> HistoryEvent | None:
        with self._decision_lock:
            return self._undo_locked(rename_event_id)

    def _undo_locked(self, rename_event_id: int) -> HistoryEvent | None:
        rename_event = self.history.get(rename_event_id)
        try:
            with self._owned_moves.move_guard():
                try:
                    undo_event = self.history.undo_rename(rename_event_id)
                except RenamePostCommitError as exc:
                    # The undo move committed before history finalization failed.
                    # Suppress only this owned move before releasing the watcher
                    # guard, matching the accept-side post-commit path.
                    if rename_event is not None and rename_event.target_path is not None:
                        self._owned_moves.register(
                            rename_event.target_path,
                            exc.target_path,
                        )
                    raise
                if rename_event is not None and undo_event.target_path is not None:
                    self._owned_moves.register(
                        rename_event.target_path,
                        undo_event.target_path,
                    )
        except Exception as exc:
            source = rename_event.target_path if rename_event and rename_event.target_path else Path("<undo>")
            self._record_operation_failure(source, "undo", exc)
            return None
        self.notifications.notify_restored(
            undo_event.target_path.name if undo_event.target_path else ""
        )
        return undo_event

    def open_folder(self, renamed_path: Path) -> None:
        try:
            self._open_folder(renamed_path.parent)
        except Exception as exc:
            self._record_operation_failure(renamed_path, "open-folder", exc)

    def _on_task_change(self, task: QueueTask[SuggestionProposal | None]) -> None:
        if task.state is not TaskState.FAILED:
            return
        with self._decision_lock:
            is_batch_task = task.path in self._batch_options
        with self._failure_lock:
            if task.id in self._failed_task_ids:
                return
            self._failed_task_ids.add(task.id)
            if len(self._failed_task_ids) > 4096:
                # Queue callbacks emit a terminal state once.  Bounding this
                # defensive dedupe set avoids daemon memory growth.
                self._failed_task_ids.clear()
                self._failed_task_ids.add(task.id)
        message = task.error or "unknown processing error"
        try:
            self.history.record_failure(
                task.path,
                message,
                metadata={"operation": "analysis"},
            )
        except Exception as history_error:
            # Queue admission and task failures must still reach the user if
            # SQLite logging is temporarily unavailable.  The queue observer
            # intentionally cannot propagate into the filesystem monitor.
            message = (
                f"{message}; history logging also failed: "
                f"{type(history_error).__name__}: {history_error}"
            )
        if not is_batch_task:
            self.notifications.notify_error(message)

    def _record_operation_failure(self, path: Path, operation: str, error: Exception) -> None:
        message = f"{type(error).__name__}: {error}"
        try:
            self.history.record_failure(path, message, metadata={"operation": operation})
        except Exception as history_error:
            # A post-commit rename failure may be caused by SQLite itself. Error
            # reporting must still reach the user instead of depending on a
            # second successful write to the same unavailable database.
            message = (
                f"{message}; history logging also failed: "
                f"{type(history_error).__name__}: {history_error}"
            )
        self.notifications.notify_error(message)

    def _kdialog_edit_name(self, proposal: SuggestionProposal) -> str | None:
        executable = shutil.which("kdialog")
        if executable is None or not has_desktop_session():
            raise RuntimeError("KDE input dialog is unavailable in this session")
        result = subprocess.run(
            [
                executable,
                "--title",
                "命圖 AI",
                "--inputbox",
                "輸入新檔名（副檔名會自動保留）",
                proposal.suggestion.suggested_stem,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.rstrip("\r\n")

    def _xdg_open_folder(self, directory: Path) -> None:
        executable = shutil.which("xdg-open")
        if executable is None or not has_desktop_session():
            raise RuntimeError("desktop folder opener is unavailable in this session")
        subprocess.Popen(
            [executable, os.fspath(directory)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )

    def __enter__(self) -> ApplicationCoordinator:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()
