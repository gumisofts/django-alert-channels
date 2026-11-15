"""The channel interface, the registry of channel types, and the HTTP helper they share."""

import importlib
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Mapping, Optional, Type

from ..alert import Alert
from ..conf import level_number


class ChannelError(Exception):
    """A send failed. The message must never contain a secret."""


class Channel:
    """One destination. Subclass it, implement `send`, and register it with `register()`.

    Options every channel takes: `level` (minimum level, default ERROR), `loggers` (only these logger
    name prefixes) and `exclude_loggers`.
    """

    def __init__(self, name: str, options: Mapping[str, Any], config: Mapping[str, Any]) -> None:
        self.name = name
        self.options = dict(options)
        self.config = config
        self.levelno = level_number(options.get("level"))
        self.timeout = float(options.get("timeout") or config.get("TIMEOUT", 5))
        self.loggers = tuple(options.get("loggers") or ())
        self.exclude_loggers = tuple(options.get("exclude_loggers") or ())

    def accepts(self, alert: Alert) -> bool:
        if alert.levelno < self.levelno:
            return False
        if self.loggers and not alert.logger.startswith(self.loggers):
            return False
        return not (self.exclude_loggers and alert.logger.startswith(self.exclude_loggers))

    def send(self, alert: Alert, note: str = "") -> None:  # pragma: no cover - interface
        """Deliver one alert or raise. `note` is a line to add (rate-limit notice)."""
        raise NotImplementedError

    def require(self, key: str) -> Any:
        value = self.options.get(key)
        if not value:
            raise ValueError(f"alert channel {self.name!r}: option {key!r} is required")
        return value


_REGISTRY: Dict[str, Type[Channel]] = {}


def register(type_name: str) -> Callable[[Type[Channel]], Type[Channel]]:
    def decorator(cls: Type[Channel]) -> Type[Channel]:
        _REGISTRY[type_name] = cls
        return cls

    return decorator


def import_string(path: str) -> Any:
    module_path, _, attribute = path.rpartition(".")
    if not module_path:
        raise ValueError(f"{path!r} is not a dotted path")
    return getattr(importlib.import_module(module_path), attribute)


def build(name: str, options: Mapping[str, Any], config: Mapping[str, Any]) -> Channel:
    """`type` is a registered name (telegram, webhook, sms, redis, email, callable, console) or the
    dotted path of a `Channel` subclass."""
    type_name = options.get("type") or name
    cls = _REGISTRY.get(type_name)
    if cls is None:
        if "." not in str(type_name):
            raise ValueError(f"alert channel {name!r}: unknown type {type_name!r}")
        cls = import_string(type_name)
    return cls(name, options, config)


def http_post(
    url: str,
    *,
    json_body: Any = None,
    form: Optional[Mapping[str, Any]] = None,
    raw: Optional[bytes] = None,
    headers: Optional[Mapping[str, str]] = None,
    method: str = "POST",
    timeout: float = 5,
) -> bytes:
    """POST with the standard library. Raises `ChannelError` (status only, never the URL: it can hold
    a token) on a network error or a non-2xx answer."""
    send_headers = {"User-Agent": "django-alert-channels"}
    if raw is not None:
        data = raw
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        send_headers["Content-Type"] = "application/x-www-form-urlencoded"
    else:
        data = json.dumps(json_body, default=str).encode()
        send_headers["Content-Type"] = "application/json"
    send_headers.update(headers or {})
    request = urllib.request.Request(url, data=data, headers=send_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(300).decode("utf-8", "replace")
        except Exception:
            pass
        raise ChannelError(f"HTTP {exc.code} {detail}".strip()) from None
    except Exception as exc:
        raise ChannelError(type(exc).__name__) from None
