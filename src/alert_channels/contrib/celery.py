"""Alert when a Celery task fails for good.

Celery replaces the root logger's handlers in its workers by default, so a handler on the root logger
does not see task failures there. This hooks the `task_failure` signal instead, which fires once the
task has failed after its retries. Exceptions the task declares in `throws` are expected and skipped.

Workers also get the handler itself (`after_setup_logger`, `after_setup_task_logger`), so an error a
task logs is alerted like anywhere else. Celery's own "Task ... raised unexpected" line is then left
out: the hook has already reported that failure.

Installed automatically when `"alert_channels"` is in INSTALLED_APPS (switch off with
`"CELERY": False`); otherwise call `install()` next to your Celery app.
"""

import logging

from ..handler import notify

_installed = False


def failure_alert(name: str, task_id, exception) -> dict:
    """What a failed task is reported as (`Alert.build` / `notify` keyword arguments).
    The task's arguments are left out on purpose: they often hold personal data."""
    return {
        "message": f"Celery task {name} failed: {type(exception).__name__}: {exception}",
        "level": logging.ERROR,
        "logger": "celery.task",
        "exc_info": exception,
        "extra": {"task": name, "task_id": task_id},
        "fingerprint": f"celery:{name}:{type(exception).__name__}",
    }


def _on_failure(sender=None, task_id=None, exception=None, **_kwargs) -> None:
    try:
        name = getattr(sender, "name", None) or str(sender)
        throws = getattr(sender, "throws", ()) or ()
        if exception is not None and throws and isinstance(exception, tuple(throws)):
            return
        details = failure_alert(name, task_id, exception)
        notify(details.pop("message"), **details)
    except Exception:
        pass


def installed() -> bool:
    return _installed


def _attach(logger=None, **_kwargs) -> None:
    """Add the alert handler to a logger Celery has just set up, once."""
    try:
        from ..handler import AlertHandler

        if logger is not None and not any(isinstance(h, AlertHandler) for h in logger.handlers):
            logger.addHandler(AlertHandler())
    except Exception:
        pass


def install() -> bool:
    """Connect the hook. False when Celery is not installed."""
    global _installed
    if _installed:
        return True
    try:
        from celery.signals import after_setup_logger, after_setup_task_logger, task_failure
    except Exception:
        return False
    after_setup_logger.connect(_attach, weak=False, dispatch_uid="alert_channels.logger")
    after_setup_task_logger.connect(_attach, weak=False, dispatch_uid="alert_channels.task_logger")
    task_failure.connect(_on_failure, weak=False, dispatch_uid="alert_channels.task_failure")
    _installed = True
    return True
