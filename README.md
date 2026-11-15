# django-alert-channels

Know when your app fails. A standard `logging` handler that sends errors and alerts to the
channels you configure:

| Channel | What it does |
|---|---|
| `telegram` | A bot message to a group, a channel or a private chat |
| `sms` | A short text through any SMS provider with an HTTP API |
| `webhook` | POST to an endpoint: your own, Slack, Discord, Mattermost, Google Chat |
| `redis` | Publish to a Redis bus (pub/sub or a list) |
| `email` | Through Django's mail settings |
| `callable` | Your own function: Kafka, RabbitMQ, a pager, a database |

It is built to be left on in production:

- **Never blocks or breaks the app.** Sending happens on a background thread with a timeout. A
  channel that is down is skipped.
- **No floods.** The same error is sent once per window (5 minutes by default) with a count of the
  repeats, and each channel has a rate limit.
- **Scrubbed.** Passwords, tokens, API keys, credentials in URLs and long digit runs (phone and
  card numbers) are masked before anything leaves the server.
- **Says where it came from.** Service, environment, host and release are on every alert, so one
  chat can serve many apps.
- **No dependencies.** Standard library only. Django is needed for the settings, the email channel
  and the management command; the handler itself works in any Python program.

## Install

```
pip install git+https://github.com/gumisofts/django-alert-channels.git@v0.1.0
```

## Set up with environment variables

Add the handler to `LOGGING`, and optionally the app (for the test command and the Celery hook):

```python
INSTALLED_APPS = [..., "alert_channels"]

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "console": {"class": "logging.StreamHandler"},
        "alerts": {"class": "alert_channels.AlertHandler", "level": "ERROR"},
    },
    "root": {"handlers": ["console", "alerts"], "level": "INFO"},
}
```

Then configure channels in the environment. A channel exists only when its variables are set, so
the same code runs everywhere and an app with no variables sends nothing.

```
ALERT_SERVICE=backend
ALERT_ENVIRONMENT=production

ALERT_TELEGRAM_BOT_TOKEN=123456:AA...
ALERT_TELEGRAM_CHAT_ID=-1001234567890          # comma separated for several chats

ALERT_WEBHOOK_URL=https://ops.example.com/alerts
ALERT_WEBHOOK_SECRET=...                        # adds an HMAC signature header

ALERT_SMS_URL=https://sms.example.com/send
ALERT_SMS_TO=0911000001,0911000002
ALERT_SMS_HEADERS={"Authorization": "Bearer ..."}
ALERT_SMS_PAYLOAD={"to": "{to}", "message": "{text}"}

ALERT_REDIS_URL=redis://localhost:6379/0
ALERT_REDIS_CHANNEL=alerts
```

Check it:

```
python manage.py alert_test
```

All variables are listed in [`conf.py`](src/alert_channels/conf.py) (`from_env`).

## Set up in settings

For full control, define `ALERT_CHANNELS`. When it exists the environment variables are not read
(call `alert_channels.from_env()` yourself to start from them).

```python
ALERT_CHANNELS = {
    "SERVICE": "backend",
    "ENVIRONMENT": "production",
    "RELEASE": os.getenv("GIT_SHA", ""),
    "DEDUP_SECONDS": 300,
    "DEDUP_CACHE": "default",        # share deduplication between processes through a Django cache
    "RATE_LIMIT": 20,                # per channel, per RATE_PERIOD seconds
    "RATE_PERIOD": 60,
    "CHANNELS": {
        "ops": {
            "type": "telegram",
            "token": os.environ["OPS_BOT_TOKEN"],
            "chat_id": [-1001234567890],
            "level": "ERROR",
        },
        "payments": {                # only payment errors, to the people who handle them
            "type": "telegram",
            "token": os.environ["OPS_BOT_TOKEN"],
            "chat_id": 555000111,
            "loggers": ["apps.wallet", "apps.payments"],
        },
        "oncall": {
            "type": "sms",
            "level": "CRITICAL",
            "url": "https://sms.example.com/send",
            "headers": {"Authorization": "Bearer ..."},
            "payload": {"to": "{to}", "message": "{text}"},
            "to": ["0911000001"],
        },
        "bus": {"type": "redis", "url": "redis://localhost:6379/0", "channel": "alerts"},
        "pager": {"type": "callable", "function": "myapp.ops.page"},
    },
}
```

### Options every channel takes

| Option | Meaning |
|---|---|
| `type` | The channel type, or the dotted path of your own `Channel` class |
| `level` | Minimum level (default `ERROR`; `sms` defaults to `CRITICAL`) |
| `loggers` | Only records from these logger name prefixes |
| `exclude_loggers` | Never records from these |
| `rate_limit`, `rate_period` | Override the global rate limit |
| `timeout` | Seconds allowed for one send |

### Channel options

- **telegram**: `token`, `chat_id` (one or a list), `thread_id` (forum topic), `silent`.
  For a private chat, the person must press Start in the bot once. To find a chat id, send a
  message in the chat and open `https://api.telegram.org/bot<token>/getUpdates`.
- **webhook**: `url`, `format` (`json`, `slack` or `discord`), `headers`, `secret`
  (adds `X-Alert-Signature: sha256=<HMAC-SHA256 of the body>`), `method`.
- **sms**: `to`, and either `url` + `payload` + `headers` + `encoding` (`json` or `form`), or
  `backend` (dotted path of your `send(to, text)` function). `max_length` (default 300).
- **redis**: `url`, `channel`, `mode` (`publish`, or `list` for `LPUSH` trimmed to `max_length`).
  Needs `pip install redis`.
- **email**: `to`, `from_email`.
- **callable**: `function` (a callable or its dotted path), called with the alert as a dict.

The JSON a webhook, the Redis bus and a callable receive:

```json
{
  "level": "ERROR", "levelno": 40, "logger": "apps.wallet", "message": "...",
  "service": "backend", "environment": "production", "release": "", "host": "web-1",
  "timestamp": 1790000000.0, "location": "/app/wallet/services.py:120",
  "exception": "TimeoutError", "traceback": "...",
  "request": {"method": "POST", "path": "/api/withdraw/", "user_id": "42"},
  "extra": {}, "fingerprint": "3f2a9c1b7d10", "suppressed": 0, "text": "..."
}
```

## What gets alerted

- **Unhandled request errors.** Django logs every 500 on `django.request`; with the handler on the
  root logger they are alerted, with the method, path and user id (never the query string, headers
  or body).
- **Anything you log at the channel's level or above.**

  ```python
  logger.error("Payout failed for withdrawal %s", withdrawal.pk, extra={"alert_extra": {"provider": "bank"}})
  logger.exception("Feed sync crashed")
  logger.error("Known and noisy", extra={"alert": False})   # logged, not alerted
  ```

- **Failed Celery tasks.** Celery replaces the root logger's handlers in its workers, so the package
  hooks the `task_failure` signal instead. This is automatic with `"alert_channels"` in
  `INSTALLED_APPS`; otherwise call `alert_channels.contrib.celery.install()`. Task arguments are
  not included.
- **Direct alerts**, without a logger:

  ```python
  from alert_channels import notify

  notify("Odds feed has been silent for 10 minutes", level="CRITICAL")
  ```

## Your own channel

```python
from alert_channels import Channel, register

@register("pagerduty")
class PagerDuty(Channel):
    def send(self, alert, note=""):
        ...  # raise on failure; alert.text(), alert.short(), alert.to_dict()
```

Import the module before logging starts (your app's `apps.py`), or give its dotted path as `type`.

## What it does not do

- It alerts; it does not store or search logs. Use it next to a log store or an error tracker.
- It cannot report a process that is dead: a dead process logs nothing. Watch for that from
  outside (a heartbeat or uptime check).
- Alerts still queued when a process is killed are lost. On a normal exit the queue is flushed.

## Development

```
pip install -e ".[dev]"
python -m unittest discover -s tests -t .
```

MIT licence.
