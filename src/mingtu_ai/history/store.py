"""Durable, content-free history and non-overwriting file moves.

Only paths, operation outcomes, and small structured metadata are persisted.  File
contents never enter the history database.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any, Mapping

from mingtu_ai.naming import numbered_destination, rename_noreplace


_PRIVATE_DIRECTORY_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600


class EventType(StrEnum):
    SUGGESTION = "suggestion"
    IGNORED = "ignored"
    FAILURE = "failure"
    RENAME = "rename"
    UNDO = "undo"


class EventStatus(StrEnum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class RenamePostCommitError(RuntimeError):
    """The file move committed after its durable PENDING ledger was written.

    Callers must treat ``target_path`` as the file's current location.  The
    pending event is intentionally retained for recovery instead of attempting
    an unsafe compensating move after the source name has been released.
    """

    def __init__(self, event_id: int, target_path: Path, cause: BaseException) -> None:
        super().__init__(
            f"rename completed but history event {event_id} could not be finalized: {cause}"
        )
        self.event_id = event_id
        self.target_path = target_path


class RenameRecoveryRequiredError(RuntimeError):
    """A concurrent path change made automatic cleanup unsafe.

    Neither path is claimed to be the current file location. Keep both paths
    for inspection; deleting the target could delete the last original link.
    ``event_id`` is absent when the initial ledger write itself failed.
    """

    def __init__(
        self, source_path: Path, target_path: Path, cause: BaseException,
        *, event_id: int | None = None,
    ) -> None:
        ledger = f"history event {event_id}" if event_id is not None else "no confirmed history event"
        super().__init__(
            f"rename interrupted; paths preserved for manual inspection ({ledger}): "
            f"source={source_path}; target={target_path}; cause={cause}"
        )
        self.source_path = source_path
        self.target_path = target_path
        self.event_id = event_id


@dataclass(frozen=True, slots=True)
class HistoryEvent:
    id: int
    event_type: EventType
    status: EventStatus
    source_path: Path
    target_path: Path | None
    suggested_name: str | None
    message: str | None
    parent_event_id: int | None
    metadata: dict[str, Any]
    created_at: str
    updated_at: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _validate_filename(filename: str) -> str:
    """Return a safe basename or raise before touching the filesystem."""

    if not filename or filename in {".", ".."}:
        raise ValueError("target filename must not be empty")
    if (
        Path(filename).name != filename
        or "/" in filename
        or "\\" in filename
        or "\x00" in filename
    ):
        raise ValueError("target must be a filename, not a path")
    return filename


def _candidate_path(source: Path, filename: str, index: int) -> Path:
    return numbered_destination(source, filename, index=index)


def _source_identity(path: Path) -> tuple[int, int, int, int]:
    info = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("source must be an existing regular, non-symlink file")
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


def _restrict_permissions(
    path: Path,
    maximum_mode: int,
    *,
    created: bool,
    expect_directory: bool = False,
    tighten_existing: bool = True,
) -> None:
    """Tighten a non-symlink inode without widening existing permissions."""

    path_info = path.lstat()
    expected_type = stat.S_ISDIR if expect_directory else stat.S_ISREG
    if stat.S_ISLNK(path_info.st_mode) or not expected_type(path_info.st_mode):
        expected = "directory" if expect_directory else "regular file"
        raise ValueError(f"history path must be a non-symlink {expected}: {path}")

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    if expect_directory and hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not expected_type(info.st_mode):
            expected = "directory" if expect_directory else "regular file"
            raise ValueError(f"history path must be a non-symlink {expected}: {path}")
        current_mode = stat.S_IMODE(info.st_mode)
        desired_mode = (
            maximum_mode
            if created
            else current_mode & maximum_mode
            if tighten_existing
            else current_mode
        )
        if current_mode != desired_mode:
            os.fchmod(descriptor, desired_mode)
    finally:
        os.close(descriptor)


def _ensure_private_directory(directory: Path) -> None:
    try:
        directory.mkdir(parents=True, mode=_PRIVATE_DIRECTORY_MODE)
    except FileExistsError:
        created = False
    else:
        created = True
    _restrict_permissions(
        directory,
        _PRIVATE_DIRECTORY_MODE,
        created=created,
        expect_directory=True,
        # The caller may intentionally place the database in an existing shared
        # directory. Database files are private, but this library must not
        # silently change permissions on a directory it did not create.
        tighten_existing=False,
    )


def _ensure_private_file(path: Path) -> None:
    """Create an empty owner-only file, or tighten an existing file first."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, _PRIVATE_FILE_MODE)
    except FileExistsError:
        created = False
    else:
        created = True
        os.close(descriptor)
    _restrict_permissions(path, _PRIVATE_FILE_MODE, created=created)


def safe_move_no_replace(source: Path | str, target: Path | str) -> Path:
    """Move one regular file without ever replacing an existing directory entry.

    Linux ``renameat2(RENAME_NOREPLACE)`` changes both directory entries in one
    syscall. Hosts without that atomic primitive fail closed; this function
    never falls back to a check followed by ``unlink``.
    """

    source_path = Path(source)
    target_path = Path(target)
    if source_path.parent != target_path.parent:
        raise ValueError("safe moves must remain in the source directory")
    if source_path == target_path:
        raise ValueError("source and target are identical")
    identity = _source_identity(source_path)
    rename_noreplace(source_path, target_path)
    if HistoryStore._regular_file_identity_or_none(target_path) != list(identity[:2]):
        raise RenameRecoveryRequiredError(
            source_path,
            target_path,
            ValueError("atomic move used an unexpected source identity"),
        )
    return target_path


class HistoryStore:
    """SQLite-backed event ledger plus safe accept/undo operations."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        _ensure_private_directory(self.database_path.parent)
        _ensure_private_file(self.database_path)
        self._secure_sqlite_files()
        self._initialize()

    @property
    def _sqlite_paths(self) -> tuple[Path, Path, Path]:
        return (
            self.database_path,
            Path(f"{self.database_path}-wal"),
            Path(f"{self.database_path}-shm"),
        )

    def _secure_sqlite_files(self) -> None:
        """Tighten the database and any live WAL sidecars to owner-only."""

        for path in self._sqlite_paths:
            try:
                _restrict_permissions(path, _PRIVATE_FILE_MODE, created=False)
            except FileNotFoundError:
                continue

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            self._secure_sqlite_files()
        except BaseException:
            connection.close()
            raise
        return connection

    def _initialize(self) -> None:
        try:
            with closing(self._connect()) as connection, connection:
                connection.execute("PRAGMA journal_mode = WAL")
                self._secure_sqlite_files()
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS history_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type TEXT NOT NULL CHECK (event_type IN
                            ('suggestion', 'ignored', 'failure', 'rename', 'undo')),
                        status TEXT NOT NULL CHECK (status IN
                            ('pending', 'success', 'failed')),
                        source_path TEXT NOT NULL,
                        target_path TEXT,
                        suggested_name TEXT,
                        message TEXT,
                        parent_event_id INTEGER REFERENCES history_events(id),
                        metadata_json TEXT NOT NULL DEFAULT '{}',
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    );
                    CREATE INDEX IF NOT EXISTS history_events_created_at_idx
                        ON history_events(created_at DESC);
                    CREATE INDEX IF NOT EXISTS history_events_parent_idx
                        ON history_events(parent_event_id);
                    """
                )
                self._secure_sqlite_files()
        finally:
            self._secure_sqlite_files()

    def _insert(
        self,
        event_type: EventType,
        status: EventStatus,
        source_path: Path | str,
        *,
        target_path: Path | str | None = None,
        suggested_name: str | None = None,
        message: str | None = None,
        parent_event_id: int | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        timestamp = _utc_now()
        metadata_json = json.dumps(
            dict(metadata or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO history_events (
                    event_type, status, source_path, target_path, suggested_name,
                    message, parent_event_id, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_type.value,
                    status.value,
                    str(Path(source_path)),
                    str(Path(target_path)) if target_path is not None else None,
                    suggested_name,
                    message,
                    parent_event_id,
                    metadata_json,
                    timestamp,
                    timestamp,
                ),
            )
            return int(cursor.lastrowid)

    def _set_status(
        self, event_id: int, status: EventStatus, message: str | None = None,
        *, metadata_updates: Mapping[str, Any] | None = None,
    ) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE history_events SET status = ?, message = ?, updated_at = ? WHERE id = ?",
                (status.value, message, _utc_now(), event_id),
            )
            if metadata_updates:
                row = connection.execute(
                    "SELECT metadata_json FROM history_events WHERE id = ?", (event_id,),
                ).fetchone()
                if row is None:
                    raise ValueError(f"history event {event_id} does not exist")
                metadata = json.loads(row["metadata_json"])
                metadata.update(metadata_updates)
                connection.execute(
                    "UPDATE history_events SET metadata_json = ? WHERE id = ?",
                    (json.dumps(metadata, ensure_ascii=False, sort_keys=True), event_id),
                )

    def record_suggestion(
        self,
        source_path: Path | str,
        suggested_name: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        return self._insert(
            EventType.SUGGESTION,
            EventStatus.PENDING,
            source_path,
            suggested_name=suggested_name,
            metadata=metadata,
        )

    def record_ignored(
        self,
        source_path: Path | str,
        reason: str | None = None,
        *,
        suggestion_event_id: int | None = None,
    ) -> int:
        return self._insert(
            EventType.IGNORED,
            EventStatus.SUCCESS,
            source_path,
            message=reason,
            parent_event_id=suggestion_event_id,
        )

    def record_failure(
        self,
        source_path: Path | str,
        message: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> int:
        return self._insert(
            EventType.FAILURE,
            EventStatus.FAILED,
            source_path,
            message=message,
            metadata=metadata,
        )

    def rename_file(
        self,
        source_path: Path | str,
        requested_filename: str,
        *,
        suggestion_event_id: int | None = None,
        expected_source_identity: tuple[int, int, int, int] | None = None,
    ) -> HistoryEvent:
        """Accept a suggestion through a crash-aware, no-clobber ledger.

        A durable ``PENDING`` event precedes one Linux
        ``renameat2(RENAME_NOREPLACE)`` call. The atomic syscall cannot expose a
        check-to-unlink window or overwrite a destination race. Unsupported
        hosts fail closed without moving the file. A later status-finalization
        failure after a completed move raises :class:`RenamePostCommitError`.
        """

        source = Path(source_path)
        filename = _validate_filename(requested_filename)
        identity = _source_identity(source)
        if expected_source_identity is not None and identity != expected_source_identity:
            raise ValueError("source changed after the rename was proposed")
        if source.name == filename:
            raise ValueError("suggested filename is already the current filename")

        for index in range(1, 1_000_000):
            target = _candidate_path(source, filename, index)
            event_id = self._insert(
                EventType.RENAME,
                EventStatus.PENDING,
                source,
                target_path=target,
                suggested_name=filename,
                parent_event_id=suggestion_event_id,
                metadata={
                    "collision_index": index,
                    "target_identity": list(identity[:2]),
                    "move_strategy": "renameat2_noreplace",
                },
            )
            try:
                # Recheck immediately before the syscall. renameat2 itself is
                # atomic with respect to both directory entries; a later source
                # replacement can be moved, but it cannot be silently unlinked.
                if _source_identity(source) != identity:
                    raise ValueError("source changed before the atomic rename")
                rename_noreplace(source, target)
            except FileExistsError as exc:
                try:
                    self._set_status(event_id, EventStatus.FAILED, str(exc))
                except Exception as status_error:
                    raise status_error from exc
                continue
            except BaseException as exc:
                try:
                    self._set_status(event_id, EventStatus.FAILED, str(exc))
                except Exception as status_error:
                    raise status_error from exc
                raise
            break
        else:
            raise RuntimeError("could not find an available filename")

        if self._regular_file_identity_or_none(target) != list(identity[:2]):
            recovery_error = RenameRecoveryRequiredError(
                source,
                target,
                ValueError("atomic rename moved an unexpected source identity"),
                event_id=event_id,
            )
            try:
                self._set_status(
                    event_id,
                    EventStatus.PENDING,
                    str(recovery_error),
                    metadata_updates={"requires_manual_recovery": True},
                )
            except Exception:
                pass
            raise recovery_error

        try:
            self._set_status(event_id, EventStatus.SUCCESS)
            event = self.get(event_id)
            if event is None:
                raise RuntimeError(f"history event {event_id} disappeared after rename")
        except BaseException as exc:
            raise RenamePostCommitError(event_id, target, exc) from exc
        return event

    def undo_rename(self, rename_event_id: int) -> HistoryEvent:
        """Undo one successful rename, failing safely if the old name is occupied."""

        rename_event = self.get(rename_event_id)
        if rename_event is None or rename_event.event_type is not EventType.RENAME:
            raise ValueError(f"history event {rename_event_id} is not a rename")
        if rename_event.status is EventStatus.PENDING:
            rename_event = self.reconcile_pending_rename(rename_event_id)
        if rename_event.status is not EventStatus.SUCCESS or rename_event.target_path is None:
            raise ValueError(f"rename event {rename_event_id} is not undoable")
        if self._successful_undo_exists(rename_event_id):
            raise ValueError(f"rename event {rename_event_id} was already undone")

        current = rename_event.target_path
        original = rename_event.source_path
        recorded_identity = rename_event.metadata.get("target_identity")
        if not (
            isinstance(recorded_identity, list)
            and len(recorded_identity) == 2
            and all(isinstance(value, int) for value in recorded_identity)
        ):
            raise ValueError("rename event has no valid target identity")
        event_id = self._insert(
            EventType.UNDO,
            EventStatus.PENDING,
            current,
            target_path=original,
            parent_event_id=rename_event_id,
            metadata={
                "move_strategy": "renameat2_noreplace",
                "target_identity": rename_event.metadata.get("target_identity"),
            },
        )
        try:
            if self._regular_file_identity_or_none(current) != recorded_identity:
                raise ValueError("renamed path now refers to a different file")
            rename_noreplace(current, original)
        except BaseException as exc:
            self._set_status(event_id, EventStatus.FAILED, str(exc))
            raise

        if self._regular_file_identity_or_none(original) != recorded_identity:
            recovery_error = RenameRecoveryRequiredError(
                current,
                original,
                ValueError("atomic undo moved an unexpected source identity"),
                event_id=event_id,
            )
            try:
                self._set_status(
                    event_id,
                    EventStatus.PENDING,
                    str(recovery_error),
                    metadata_updates={"requires_manual_recovery": True},
                )
            except Exception:
                pass
            raise recovery_error

        try:
            self._set_status(event_id, EventStatus.SUCCESS)
            event = self.get(event_id)
            if event is None:
                raise RuntimeError(f"history event {event_id} disappeared after undo")
        except BaseException as exc:
            raise RenamePostCommitError(event_id, original, exc) from exc
        return event

    def can_undo_rename(self, rename_event_id: int) -> bool:
        """Check undo eligibility without changing files or reconciling events.

        The full ledger is queried for previous successful undo operations,
        independently of the UI's recent-history window. Filesystem checks are
        advisory; :meth:`undo_rename` repeats its safety checks on execution.
        """

        event = self.get(rename_event_id)
        if (
            event is None
            or event.event_type is not EventType.RENAME
            or event.status not in {EventStatus.SUCCESS, EventStatus.PENDING}
            or event.target_path is None
            or event.metadata.get("requires_manual_recovery")
            or self._successful_undo_exists(rename_event_id)
        ):
            return False
        try:
            # lstat also counts dangling symlinks and directories as conflicts.
            try:
                event.source_path.lstat()
            except FileNotFoundError:
                pass
            else:
                return False
            current_identity = self._regular_file_identity_or_none(event.target_path)
        except OSError:
            return False
        recorded_identity = event.metadata.get("target_identity")
        return (
            isinstance(recorded_identity, list)
            and len(recorded_identity) == 2
            and current_identity == recorded_identity
        )

    def reconcile_pending_rename(self, rename_event_id: int) -> HistoryEvent:
        """Resolve one interrupted rename from its paths and recorded inode.

        A missing source plus the expected target means the move committed, so
        the ledger can become ``SUCCESS``.  If both hard links still refer to
        the recorded inode, the crash happened before the source unlink; the
        target claim is removed and the ledger becomes ``FAILED``.  Ambiguous
        or replaced paths are never modified.
        """

        event = self.get(rename_event_id)
        if event is None or event.event_type is not EventType.RENAME:
            raise ValueError(f"history event {rename_event_id} is not a rename")
        if event.status is not EventStatus.PENDING or event.target_path is None:
            return event
        if event.metadata.get("requires_manual_recovery"):
            raise ValueError("pending rename requires manual inspection of preserved paths")

        recorded_identity = event.metadata.get("target_identity")
        if not (
            isinstance(recorded_identity, list)
            and len(recorded_identity) == 2
            and all(isinstance(value, int) for value in recorded_identity)
        ):
            raise ValueError("pending rename has no valid target identity")

        source = event.source_path
        target = event.target_path
        source_identity = self._regular_file_identity_or_none(source)
        target_identity = self._regular_file_identity_or_none(target)

        if event.metadata.get("move_strategy") == "renameat2_noreplace":
            if target_identity == recorded_identity:
                # The expected inode reached the destination. A concurrently
                # recreated source name is an independent occupant and remains
                # untouched; it can merely make a later undo unavailable.
                self._set_status(event.id, EventStatus.SUCCESS)
            elif source_identity == recorded_identity:
                # The atomic move did not commit. The target may be absent or a
                # collision occupant, neither of which this event created.
                self._set_status(event.id, EventStatus.FAILED, "atomic rename did not commit")
            else:
                message = "pending atomic rename paths are ambiguous or were replaced"
                self._set_status(
                    event.id,
                    EventStatus.PENDING,
                    message,
                    metadata_updates={"requires_manual_recovery": True},
                )
                raise ValueError(message)
        elif source_identity is None and target_identity == recorded_identity:
            self._set_status(event.id, EventStatus.SUCCESS)
        elif source_identity == recorded_identity and target_identity == recorded_identity:
            # Legacy versions could leave two hard links before source unlink.
            # POSIX has no inode-conditional unlink, so even a final identity
            # check cannot make automatic cleanup race-free. Preserve both
            # names and require explicit inspection instead of deleting either.
            message = "legacy pending rename has two links and requires manual inspection"
            self._set_status(
                event.id,
                EventStatus.PENDING,
                message,
                metadata_updates={"requires_manual_recovery": True},
            )
            raise ValueError(message)
        elif source_identity == recorded_identity and target_identity is None:
            self._set_status(event.id, EventStatus.FAILED, "target claim was rolled back")
        else:
            raise ValueError("pending rename paths are ambiguous or were replaced")

        resolved = self.get(event.id)
        if resolved is None:
            raise RuntimeError(f"history event {event.id} disappeared during reconciliation")
        return resolved

    def reconcile_pending_undo(self, undo_event_id: int) -> HistoryEvent:
        """Resolve one interrupted atomic undo without changing either path.

        The undo ledger records the inode expected at its destination before the
        atomic move. If that inode reached the original name, the move committed.
        If it remains at the renamed name, the move did not commit. Any other
        layout is preserved and marked for manual recovery.
        """

        event = self.get(undo_event_id)
        if event is None or event.event_type is not EventType.UNDO:
            raise ValueError(f"history event {undo_event_id} is not an undo")
        if event.status is not EventStatus.PENDING or event.target_path is None:
            return event
        if event.metadata.get("requires_manual_recovery"):
            raise ValueError("pending undo requires manual inspection of preserved paths")
        if event.metadata.get("move_strategy") != "renameat2_noreplace":
            raise ValueError("pending undo has no supported atomic move strategy")

        recorded_identity = event.metadata.get("target_identity")
        if not (
            isinstance(recorded_identity, list)
            and len(recorded_identity) == 2
            and all(isinstance(value, int) for value in recorded_identity)
        ):
            raise ValueError("pending undo has no valid target identity")

        current_identity = self._regular_file_identity_or_none(event.source_path)
        original_identity = self._regular_file_identity_or_none(event.target_path)
        if original_identity == recorded_identity:
            self._set_status(event.id, EventStatus.SUCCESS)
        elif current_identity == recorded_identity:
            self._set_status(event.id, EventStatus.FAILED, "atomic undo did not commit")
        else:
            message = "pending atomic undo paths are ambiguous or were replaced"
            self._set_status(
                event.id,
                EventStatus.PENDING,
                message,
                metadata_updates={"requires_manual_recovery": True},
            )
            raise ValueError(message)

        resolved = self.get(event.id)
        if resolved is None:
            raise RuntimeError(f"history event {event.id} disappeared during reconciliation")
        return resolved

    @staticmethod
    def _regular_file_identity_or_none(path: Path) -> list[int] | None:
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None
        return [info.st_dev, info.st_ino]

    def _successful_undo_exists(self, rename_event_id: int) -> bool:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT 1 FROM history_events
                WHERE event_type = 'undo' AND status = 'success' AND parent_event_id = ?
                LIMIT 1
                """,
                (rename_event_id,),
            ).fetchone()
        return row is not None

    def get(self, event_id: int) -> HistoryEvent | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM history_events WHERE id = ?", (event_id,)
            ).fetchone()
        return self._row_to_event(row) if row is not None else None

    def list_recent(self, limit: int = 100) -> list[HistoryEvent]:
        if limit < 1:
            return []
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM history_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_event(row) for row in rows]

    @staticmethod
    def _row_to_event(row: sqlite3.Row) -> HistoryEvent:
        return HistoryEvent(
            id=int(row["id"]),
            event_type=EventType(row["event_type"]),
            status=EventStatus(row["status"]),
            source_path=Path(row["source_path"]),
            target_path=Path(row["target_path"]) if row["target_path"] else None,
            suggested_name=row["suggested_name"],
            message=row["message"],
            parent_event_id=row["parent_event_id"],
            metadata=json.loads(row["metadata_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
