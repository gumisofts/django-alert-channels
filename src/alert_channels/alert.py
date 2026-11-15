"""`Alert`: one event on its way to the channels, built from a log record or by `notify()`."""

import hashlib
import logging
import time
import traceback as traceback_module
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional

from .scrub import Scrubber

ICONS = {
    logging.CRITICAL: "\U0001F6A8",  # rotating light
    logging.ERROR: "\U0001F534",  # red circle
    logging.WARNING: "\U0001F7E0",  # orange circle
    logging.INFO: "\U0001F535",  # blue circle
}


def _icon(levelno: int) -> str:
    for threshold in sorted(ICONS, reverse=True):
        if levelno >= threshold:
            return ICONS[threshold]
    return "⚪"


def _request_summary(request: Any) -> Dict[str, str]:
    """Method, path and user id of a Django request. Never the query string, headers or body."""
    summary: Dict[str, str] = {}
    try:
        method, path = getattr(request, "method", None), getattr(request, "path", None)
        if not (isinstance(method, str) and isinstance(path, str)):
            return summary
        summary["method"], summary["path"] = method, path
        user = getattr(request, "user", None)
        if user is not None and getattr(user, "is_authenticated", False):
            summary["user_id"] = str(getattr(user, "pk", ""))
    except Exception:
        pass
    return summary


@dataclass
class Alert:
    level: str
    levelno: int
    logger: str
    message: str
    service: str = ""
    environment: str = ""
    release: str = ""
    host: str = ""
    timestamp: float = field(default_factory=time.time)
    location: str = ""
    exception: str = ""
    traceback: str = ""
    request: Dict[str, str] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)
    fingerprint: str = ""
    #: Identical alerts swallowed by deduplication since the last one that was sent.
    suppressed: int = 0

    @classmethod
    def build(
        cls,
        config: Mapping[str, Any],
        *,
        level: int,
        logger: str,
        message: str,
        template: Optional[str] = None,
        location: str = "",
        exc_info: Any = None,
        request: Any = None,
        extra: Optional[Mapping[str, Any]] = None,
        fingerprint: Optional[str] = None,
    ) -> "Alert":
        scrub = Scrubber(
            enabled=config.get("SCRUB", True),
            numbers=config.get("SCRUB_NUMBERS", True),
            patterns=config.get("SCRUB_PATTERNS") or (),
        )
        exception = trace = ""
        if exc_info and exc_info[0] is not None:
            exception = exc_info[0].__name__
            lines = "".join(traceback_module.format_exception(*exc_info)).rstrip().splitlines()
            keep = int(config.get("TRACEBACK_LINES", 25))
            if len(lines) > keep:
                lines = ["..."] + lines[-keep:]
            trace = scrub("\n".join(lines))
        if not fingerprint:
            # The unformatted message groups "user 5 failed" with "user 9 failed".
            raw = "|".join([logger, str(level), template or message, exception, location])
            fingerprint = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:12]
        return cls(
            level=logging.getLevelName(level),
            levelno=level,
            logger=logger,
            message=scrub(message),
            service=config.get("SERVICE", ""),
            environment=config.get("ENVIRONMENT", ""),
            release=config.get("RELEASE", ""),
            host=config.get("HOST", ""),
            location=location,
            exception=exception,
            traceback=trace,
            request=_request_summary(request) if request is not None else {},
            extra={str(k): scrub(str(v)) for k, v in (extra or {}).items()},
            fingerprint=fingerprint,
        )

    @classmethod
    def from_record(cls, record: logging.LogRecord, config: Mapping[str, Any]) -> "Alert":
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        return cls.build(
            config,
            level=record.levelno,
            logger=record.name,
            message=message,
            template=str(record.msg),
            location=f"{record.pathname}:{record.lineno}",
            exc_info=record.exc_info,
            request=getattr(record, "request", None),
            extra=getattr(record, "alert_extra", None),
            fingerprint=getattr(record, "alert_fingerprint", None),
        )

    @property
    def source(self) -> str:
        """`service/environment @ host`, leaving out what is not set."""
        name = "/".join(part for part in (self.service, self.environment) if part)
        return " @ ".join(part for part in (name, self.host) if part)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "level": self.level,
            "levelno": self.levelno,
            "logger": self.logger,
            "message": self.message,
            "service": self.service,
            "environment": self.environment,
            "release": self.release,
            "host": self.host,
            "timestamp": self.timestamp,
            "location": self.location,
            "exception": self.exception,
            "traceback": self.traceback,
            "request": dict(self.request),
            "extra": dict(self.extra),
            "fingerprint": self.fingerprint,
            "suppressed": self.suppressed,
        }

    def title(self) -> str:
        return f"{_icon(self.levelno)} {self.level}" + (f" [{self.source}]" if self.source else "")

    def text(self, limit: int = 3500, note: str = "") -> str:
        """The full message for chat-like channels, cut to `limit` characters."""
        lines = [self.title(), self.message]
        if self.request:
            who = f" (user {self.request['user_id']})" if self.request.get("user_id") else ""
            lines.append(f"{self.request.get('method', '')} {self.request.get('path', '')}{who}")
        details = [f"logger: {self.logger}"]
        if self.location:
            details.append(f"at: {self.location}")
        if self.release:
            details.append(f"release: {self.release}")
        details += [f"{key}: {value}" for key, value in self.extra.items()]
        if self.suppressed:
            details.append(f"repeated: {self.suppressed} more since the last alert")
        if note:
            details.append(note)
        lines.append("\n".join(details))
        head = "\n\n".join(lines[:2]) + "\n" + "\n".join(lines[2:])
        if self.traceback:
            room = limit - len(head) - 2
            if room > 80:
                trace = self.traceback if len(self.traceback) <= room else "..." + self.traceback[-(room - 3):]
                head = f"{head}\n\n{trace}"
        return head if len(head) <= limit else head[: limit - 3] + "..."

    def short(self, limit: int = 300) -> str:
        """One line for SMS-like channels."""
        source = f"[{self.source}] " if self.source else ""
        text = " ".join(f"{self.level} {source}{self.message}".split())
        return text if len(text) <= limit else text[: limit - 3] + "..."
