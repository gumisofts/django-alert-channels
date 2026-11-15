"""Configuration: defaults, the Django setting `ALERT_CHANNELS`, and `from_env()`.

A config is a plain dict. Keys are upper case; `CHANNELS` maps a name of your choice to one
channel's options (`type` picks the channel class, `level` its minimum level).
"""

import json
import logging
import os
import socket
from typing import Any, Dict, Mapping, Optional

DEFAULTS: Dict[str, Any] = {
    # Shown in every alert, so one chat can serve many apps.
    "SERVICE": "",
    "ENVIRONMENT": "",
    "RELEASE": "",
    "HOST": "",
    # The same error is sent once per window; repeats are counted and reported with the next one.
    "DEDUP_SECONDS": 300,
    # Name of a Django cache to share the dedup state between processes ("" = per process).
    "DEDUP_CACHE": "",
    # Per channel: at most N alerts per PERIOD seconds.
    "RATE_LIMIT": 20,
    "RATE_PERIOD": 60,
    # Seconds allowed for one send.
    "TIMEOUT": 5,
    # False sends in the calling thread (tests, scripts).
    "ASYNC": True,
    "QUEUE_SIZE": 1000,
    # Lines of traceback kept (from the end).
    "TRACEBACK_LINES": 25,
    # Mask secrets, and long digit runs (phone numbers, card numbers).
    "SCRUB": True,
    "SCRUB_NUMBERS": True,
    # Extra regular expressions whose matches are replaced by ***.
    "SCRUB_PATTERNS": [],
    # Logger name prefixes that are never alerted.
    "IGNORE_LOGGERS": ["django.security.DisallowedHost"],
    # Hook Celery's task_failure signal when the Django app is installed.
    "CELERY": True,
    "CHANNELS": {},
}

SETTING = "ALERT_CHANNELS"


def level_number(level: Any, default: int = logging.ERROR) -> int:
    if isinstance(level, int):
        return level
    if isinstance(level, str) and level.strip():
        value = logging.getLevelName(level.strip().upper())
        if isinstance(value, int):
            return value
    return default


def normalize(config: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    merged = dict(DEFAULTS)
    merged.update({str(k).upper(): v for k, v in (config or {}).items()})
    merged["HOST"] = merged["HOST"] or socket.gethostname()
    merged["CHANNELS"] = dict(merged.get("CHANNELS") or {})
    return merged


def from_django() -> Optional[Dict[str, Any]]:
    """`settings.ALERT_CHANNELS`, or None when Django is absent, unconfigured or has no such setting."""
    try:
        from django.conf import settings

        if not settings.configured:
            return None
        return getattr(settings, SETTING, None)
    except Exception:
        return None


def load(explicit: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    """The config in force: the one given, else Django's setting, else the environment."""
    if explicit is not None:
        return normalize(explicit)
    found = from_django()
    return normalize(found if found is not None else from_env())


def _list(value: str) -> list:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _json(value: str, default: Any) -> Any:
    if not (value or "").strip():
        return default
    return json.loads(value)


def from_env(environ: Optional[Mapping[str, str]] = None, prefix: str = "ALERT_") -> Dict[str, Any]:
    """Build a config from environment variables, so an app is set up without code.

    General: `ALERT_SERVICE`, `ALERT_ENVIRONMENT`, `ALERT_RELEASE`, `ALERT_DEDUP_SECONDS`,
    `ALERT_RATE_LIMIT`, `ALERT_DEDUP_CACHE`.

    A channel exists only when its first variable is set:

    - Telegram: `ALERT_TELEGRAM_BOT_TOKEN`, `ALERT_TELEGRAM_CHAT_ID` (comma separated: group ids,
      private chat ids), `ALERT_TELEGRAM_THREAD_ID`, `ALERT_TELEGRAM_LEVEL` (default ERROR).
    - Webhook: `ALERT_WEBHOOK_URL`, `ALERT_WEBHOOK_SECRET`, `ALERT_WEBHOOK_FORMAT`
      (json | slack | discord), `ALERT_WEBHOOK_HEADERS` (JSON object), `ALERT_WEBHOOK_LEVEL`.
    - SMS: `ALERT_SMS_URL`, `ALERT_SMS_TO` (comma separated), `ALERT_SMS_PAYLOAD` (JSON object with
      `{to}` and `{text}` placeholders), `ALERT_SMS_HEADERS` (JSON object), `ALERT_SMS_ENCODING`
      (json | form), `ALERT_SMS_LEVEL` (default CRITICAL).
    - Redis bus: `ALERT_REDIS_URL`, `ALERT_REDIS_CHANNEL`, `ALERT_REDIS_MODE` (publish | list),
      `ALERT_REDIS_LEVEL`.
    - Email: `ALERT_EMAIL_TO` (comma separated), `ALERT_EMAIL_FROM`, `ALERT_EMAIL_LEVEL`.
    """
    env = os.environ if environ is None else environ

    def get(name: str, default: str = "") -> str:
        return env.get(prefix + name, default) or default

    config: Dict[str, Any] = {
        "SERVICE": get("SERVICE"),
        "ENVIRONMENT": get("ENVIRONMENT"),
        "RELEASE": get("RELEASE"),
    }
    for key in ("DEDUP_SECONDS", "RATE_LIMIT", "RATE_PERIOD", "TIMEOUT"):
        if get(key):
            config[key] = int(get(key))
    if get("DEDUP_CACHE"):
        config["DEDUP_CACHE"] = get("DEDUP_CACHE")

    channels: Dict[str, Dict[str, Any]] = {}
    if get("TELEGRAM_BOT_TOKEN") and get("TELEGRAM_CHAT_ID"):
        channels["telegram"] = {
            "type": "telegram",
            "token": get("TELEGRAM_BOT_TOKEN"),
            "chat_id": _list(get("TELEGRAM_CHAT_ID")),
            "thread_id": get("TELEGRAM_THREAD_ID") or None,
            "level": get("TELEGRAM_LEVEL", "ERROR"),
        }
    if get("WEBHOOK_URL"):
        channels["webhook"] = {
            "type": "webhook",
            "url": get("WEBHOOK_URL"),
            "secret": get("WEBHOOK_SECRET"),
            "format": get("WEBHOOK_FORMAT", "json"),
            "headers": _json(get("WEBHOOK_HEADERS"), {}),
            "level": get("WEBHOOK_LEVEL", "ERROR"),
        }
    if get("SMS_URL") and get("SMS_TO"):
        channels["sms"] = {
            "type": "sms",
            "url": get("SMS_URL"),
            "to": _list(get("SMS_TO")),
            "payload": _json(get("SMS_PAYLOAD"), {"to": "{to}", "text": "{text}"}),
            "headers": _json(get("SMS_HEADERS"), {}),
            "encoding": get("SMS_ENCODING", "json"),
            "level": get("SMS_LEVEL", "CRITICAL"),
        }
    if get("REDIS_URL"):
        channels["redis"] = {
            "type": "redis",
            "url": get("REDIS_URL"),
            "channel": get("REDIS_CHANNEL", "alerts"),
            "mode": get("REDIS_MODE", "publish"),
            "level": get("REDIS_LEVEL", "ERROR"),
        }
    if get("EMAIL_TO"):
        channels["email"] = {
            "type": "email",
            "to": _list(get("EMAIL_TO")),
            "from_email": get("EMAIL_FROM") or None,
            "level": get("EMAIL_LEVEL", "ERROR"),
        }
    config["CHANNELS"] = channels
    return config
