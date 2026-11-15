"""Alert when a Celery task fails for good.

Celery replaces the root logger's handlers in its workers by default, so a handler on the root logger
does not see task failures there. This hooks the `task_failure` signal instead, which fires once the
task has failed after its retries. Exceptions the task declares in `throws` are expected and skipped.

Installed automatically when `"alert_channels"` is in INSTALLED_APPS (switch off with
`"CELERY": False`); otherwise call `install()` next to your Celery app.
"""

import logging

from ..handler import notify

_installed = False


def _on_failure(sender=None, task_id=None, exception=None, **_kwargs) -> None:
    try:
        name = getattr(sender, "name", None) or str(sender)
        throws = getattr(sender, "throws", ()) or ()
        if exception is not None and throws and isinstance(exception, tuple(throws)):
            return
        # The task's arguments are left out on purpose: they often hold personal data.
        notify(
            f"Celery task {name} failed: {type(exception).__name__}: {exception}",
            level=logging.ERROR,
            logger="celery.task",
            exc_info=exception,
            extra={"task": name, "task_id": task_id},
            fingerprint=f"celery:{name}:{type(exception).__name__}",
        )
    except Exception:
        pass


def install() -> bool:
    """Connect the hook. False when Celery is not installed."""
    global _installed
    if _installed:
        return True
    try:
        from celery.signals import task_failure
    except Exception:
        return False
    task_failure.connect(_on_failure, weak=False, dispatch_uid="alert_channels.task_failure")
    _installed = True
    return True
