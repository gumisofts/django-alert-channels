"""`AlertHandler`: the `logging.Handler` to put in `LOGGING`, and `notify()` to alert without a logger."""

import logging
import sys
from typing import Any, Mapping, Optional

from .alert import Alert
from .conf import level_number
from .dispatcher import Dispatcher, get_dispatcher, sending


class AlertHandler(logging.Handler):
    """Sends log records to the configured channels.

        LOGGING = {
            "version": 1,
            "disable_existing_loggers": False,
            "handlers": {"alerts": {"class": "alert_channels.AlertHandler", "level": "ERROR"}},
            "root": {"handlers": ["alerts"], "level": "INFO"},
        }

    `config`: a config dict for this handler alone; without it the handler uses
    `settings.ALERT_CHANNELS`, or the `ALERT_*` environment variables when there is no such setting.
    The config is read at the first record, so the handler can be created before Django is set up.

    Per record: `extra={"alert_extra": {"order": 5}}` adds lines to the alert,
    `extra={"alert_fingerprint": "..."}` sets the deduplication key, `extra={"alert": False}` skips it.
    """

    def __init__(self, level: Any = logging.ERROR, config: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(level_number(level))
        self._config = config
        self._dispatcher: Optional[Dispatcher] = None

    @property
    def dispatcher(self) -> Dispatcher:
        if self._config is None:
            return get_dispatcher()
        if self._dispatcher is None:
            self._dispatcher = Dispatcher(self._config)
        return self._dispatcher

    def emit(self, record: logging.LogRecord) -> None:
        # Never alert about our own sending: that would loop.
        if sending() or record.name.startswith("alert_channels.") or record.name == "alert_channels":
            return
        if getattr(record, "alert", True) is False:
            return
        try:
            dispatcher = self.dispatcher
            if record.levelno < dispatcher.min_level:
                return
            dispatcher.submit(Alert.from_record(record, dispatcher.config))
        except Exception:
            self.handleError(record)

    def flush(self) -> None:
        try:
            if self._dispatcher is not None:
                self._dispatcher.flush(2.0)
        except Exception:
            pass


def notify(
    message: str,
    level: Any = logging.ERROR,
    *,
    logger: str = "alert",
    exc_info: Any = None,
    extra: Optional[Mapping[str, Any]] = None,
    fingerprint: Optional[str] = None,
    dispatcher: Optional[Dispatcher] = None,
) -> bool:
    """Send an alert directly, without going through `logging`. Never raises.

    `exc_info`: True for the exception being handled, an exception instance, or a
    `(type, value, traceback)` tuple. Returns whether the alert was accepted for sending.
    """
    try:
        if exc_info is True:
            exc_info = sys.exc_info()
        elif isinstance(exc_info, BaseException):
            exc_info = (type(exc_info), exc_info, exc_info.__traceback__)
        target = dispatcher or get_dispatcher()
        alert = Alert.build(
            target.config,
            level=level_number(level),
            logger=logger,
            message=str(message),
            exc_info=exc_info,
            extra=extra,
            fingerprint=fingerprint,
        )
        return target.submit(alert)
    except Exception:
        return False
