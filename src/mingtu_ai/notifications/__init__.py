"""Freedesktop/KDE notification facade."""

from .service import (
    ActionCallback,
    DEFAULT_CLOSE_TIMEOUT_SECONDS,
    DEFAULT_MAX_ACTION_HELPERS,
    Notification,
    NotificationAction,
    NotificationBackend,
    NotificationReceipt,
    NotificationService,
    NotifySendBackend,
    has_desktop_session,
)

__all__ = [
    "ActionCallback",
    "DEFAULT_CLOSE_TIMEOUT_SECONDS",
    "DEFAULT_MAX_ACTION_HELPERS",
    "Notification",
    "NotificationAction",
    "NotificationBackend",
    "NotificationReceipt",
    "NotificationService",
    "NotifySendBackend",
    "has_desktop_session",
]
