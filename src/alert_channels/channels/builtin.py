"""The built-in channels: telegram, webhook, sms, redis, email, callable, console."""

import hashlib
import hmac
import json
import sys
from typing import Any, List, Mapping

from ..alert import Alert
from .base import Channel, ChannelError, http_post, import_string, register


def _as_list(value: Any) -> List[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [value]


@register("telegram")
class TelegramChannel(Channel):
    """A Telegram bot message.

    `token`: the bot's token. `chat_id`: one id or a list; a group (`-100...`), a channel
    (`@name`) or a private chat with the bot (the person must have pressed Start once).
    `thread_id`: a forum topic. `silent`: no sound. `api_base`: default https://api.telegram.org.
    """

    LIMIT = 4000  # Telegram's is 4096

    def __init__(self, name, options, config) -> None:
        super().__init__(name, options, config)
        self.token = self.require("token")
        self.chat_ids = _as_list(self.require("chat_id"))
        self.thread_id = options.get("thread_id")
        self.silent = bool(options.get("silent", False))
        self.api_base = str(options.get("api_base") or "https://api.telegram.org").rstrip("/")

    def send(self, alert: Alert, note: str = "") -> None:
        text = alert.text(self.LIMIT, note)
        errors = []
        for chat_id in self.chat_ids:
            payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
            if self.thread_id:
                payload["message_thread_id"] = int(self.thread_id)
            if self.silent:
                payload["disable_notification"] = True
            try:
                http_post(
                    f"{self.api_base}/bot{self.token}/sendMessage", json_body=payload, timeout=self.timeout
                )
            except ChannelError as exc:
                errors.append(f"chat {chat_id}: {exc}")
        if errors:
            raise ChannelError("; ".join(errors))


@register("webhook")
class WebhookChannel(Channel):
    """POST to an endpoint.

    `url`. `format`: `json` (the whole alert, default), `slack` (`{"text": ...}`, also Mattermost,
    Google Chat) or `discord` (`{"content": ...}`). `headers`: extra headers (an API key, say).
    `secret`: adds `X-Alert-Signature: sha256=<hex HMAC of the body>`. `method`: default POST.
    """

    def __init__(self, name, options, config) -> None:
        super().__init__(name, options, config)
        self.url = self.require("url")
        self.format = str(options.get("format") or "json").lower()
        self.headers = dict(options.get("headers") or {})
        self.secret = options.get("secret") or ""
        self.method = str(options.get("method") or "POST").upper()

    def body(self, alert: Alert, note: str) -> Mapping[str, Any]:
        if self.format == "slack":
            return {"text": alert.text(3500, note)}
        if self.format == "discord":
            return {"content": alert.text(1900, note)}
        payload = alert.to_dict()
        payload["text"] = alert.text(3500, note)
        return payload

    def send(self, alert: Alert, note: str = "") -> None:
        raw = json.dumps(self.body(alert, note), default=str).encode()
        headers = {"Content-Type": "application/json", **self.headers}
        if self.secret:
            digest = hmac.new(str(self.secret).encode(), raw, hashlib.sha256).hexdigest()
            headers["X-Alert-Signature"] = f"sha256={digest}"
        http_post(self.url, raw=raw, headers=headers, method=self.method, timeout=self.timeout)


@register("sms")
class SmsChannel(Channel):
    """A short text to phone numbers, through any SMS provider with an HTTP API.

    `to`: the numbers. Then either

    - `url` + `payload`: a dict sent once per number, where `{to}` and `{text}` are replaced in every
      string value. `encoding`: `json` (default) or `form`. `headers`: the provider's auth header.
    - or `backend`: the dotted path of your own `send(to: str, text: str)` function.

    `max_length`: default 300 characters. Defaults to level CRITICAL: an SMS costs money.
    """

    def __init__(self, name, options, config) -> None:
        options = {"level": "CRITICAL", **options}
        super().__init__(name, options, config)
        self.to = _as_list(self.require("to"))
        self.backend = import_string(options["backend"]) if options.get("backend") else None
        self.url = options.get("url") or ""
        if not (self.backend or self.url):
            raise ValueError(f"alert channel {name!r}: set 'url' or 'backend'")
        self.payload = dict(options.get("payload") or {"to": "{to}", "text": "{text}"})
        self.headers = dict(options.get("headers") or {})
        self.encoding = str(options.get("encoding") or "json").lower()
        self.max_length = int(options.get("max_length") or 300)

    @staticmethod
    def _fill(value: Any, to: str, text: str) -> Any:
        if isinstance(value, str):
            return value.replace("{to}", to).replace("{text}", text)
        if isinstance(value, dict):
            return {k: SmsChannel._fill(v, to, text) for k, v in value.items()}
        if isinstance(value, list):
            return [SmsChannel._fill(v, to, text) for v in value]
        return value

    def send(self, alert: Alert, note: str = "") -> None:
        text = alert.short(self.max_length)
        errors = []
        for to in self.to:
            try:
                if self.backend is not None:
                    self.backend(str(to), text)
                    continue
                body = self._fill(self.payload, str(to), text)
                if self.encoding == "form":
                    http_post(self.url, form=body, headers=self.headers, timeout=self.timeout)
                else:
                    http_post(self.url, json_body=body, headers=self.headers, timeout=self.timeout)
            except ChannelError as exc:
                errors.append(str(exc))
            except Exception as exc:
                errors.append(type(exc).__name__)
        if errors:
            # Recipients are personal data: report how many failed, not which.
            raise ChannelError(f"{len(errors)} of {len(self.to)} failed: {errors[0]}")


@register("redis")
class RedisChannel(Channel):
    """Put the alert (JSON) on a Redis bus. Needs the `redis` package.

    `url`. `channel`: the pub/sub channel or list key (default `alerts`). `mode`: `publish`
    (default) or `list` (`LPUSH`, trimmed to `max_length`, default 1000) for consumers that are
    not always connected.
    """

    def __init__(self, name, options, config) -> None:
        super().__init__(name, options, config)
        self.url = self.require("url")
        self.channel = str(options.get("channel") or "alerts")
        self.mode = str(options.get("mode") or "publish").lower()
        self.max_length = int(options.get("max_length") or 1000)
        self._client = None

    def client(self):
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(
                self.url, socket_timeout=self.timeout, socket_connect_timeout=self.timeout
            )
        return self._client

    def send(self, alert: Alert, note: str = "") -> None:
        body = json.dumps(alert.to_dict(), default=str)
        try:
            client = self.client()
            if self.mode == "list":
                pipe = client.pipeline()
                pipe.lpush(self.channel, body)
                pipe.ltrim(self.channel, 0, self.max_length - 1)
                pipe.execute()
            else:
                client.publish(self.channel, body)
        except Exception as exc:
            self._client = None
            raise ChannelError(type(exc).__name__) from None


@register("email")
class EmailChannel(Channel):
    """An email through Django's mail settings. `to`: addresses. `from_email`: optional."""

    def __init__(self, name, options, config) -> None:
        super().__init__(name, options, config)
        self.to = _as_list(self.require("to"))
        self.from_email = options.get("from_email") or None

    def send(self, alert: Alert, note: str = "") -> None:
        from django.core.mail import send_mail

        subject = " ".join(f"[{alert.source or alert.logger}] {alert.level}: {alert.message}".split())
        send_mail(subject[:200], alert.text(20000, note), self.from_email, self.to, fail_silently=False)


@register("callable")
class CallableChannel(Channel):
    """Hand the alert to your own function: any other bus (Kafka, RabbitMQ, SNS, ...), a pager, a
    database. `function`: a callable or its dotted path, called with the alert as a dict."""

    def __init__(self, name, options, config) -> None:
        super().__init__(name, options, config)
        function = self.require("function")
        self.function = import_string(function) if isinstance(function, str) else function

    def send(self, alert: Alert, note: str = "") -> None:
        payload = alert.to_dict()
        payload["text"] = alert.text(3500, note)
        self.function(payload)


@register("console")
class ConsoleChannel(Channel):
    """Print the alert to stderr. For development."""

    def send(self, alert: Alert, note: str = "") -> None:
        print(alert.text(4000, note), file=sys.stderr, flush=True)
