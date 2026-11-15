import hashlib
import hmac
import io
import json
import logging
import unittest
from unittest import mock

import alert_channels
from alert_channels import Alert, AlertHandler, Dispatcher, from_env, notify
from alert_channels.channels import Channel, ChannelError, register
from alert_channels.dispatcher import Deduper, RateLimiter
from alert_channels.scrub import Scrubber

SENT = []


@register("collect")
class CollectChannel(Channel):
    def send(self, alert, note=""):
        if self.options.get("fail"):
            raise ChannelError("boom")
        SENT.append((self.name, alert, note))


def collect(payload):
    SENT.append(("fn", payload, ""))


def config(**channels):
    return {"ASYNC": False, "SERVICE": "shop", "ENVIRONMENT": "prod", "HOST": "web-1",
            "CHANNELS": channels or {"all": {"type": "collect"}}}  # fmt: skip


def logger_with(handler, name="tests.app"):
    logger = logging.getLogger(name)
    logger.handlers, logger.propagate = [handler], False
    logger.setLevel(logging.DEBUG)
    return logger


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def capture_http():
    """Patch urlopen; returns the list of `(url, headers, body bytes)` it was called with."""
    calls = []

    def urlopen(request, timeout=None):
        calls.append((request.full_url, dict(request.header_items()), request.data))
        return FakeResponse(b'{"ok": true}')

    return calls, mock.patch("urllib.request.urlopen", urlopen)


class Base(unittest.TestCase):
    def setUp(self):
        SENT.clear()


class HandlerTests(Base):
    def test_an_error_reaches_the_channel_with_its_context(self):
        log = logger_with(AlertHandler(config=config()))
        try:
            raise ValueError("bad stake")
        except ValueError:
            log.exception("settlement failed for slip %s", 42, extra={"alert_extra": {"slip": 42}})
        (_, alert, _), = SENT
        self.assertEqual((alert.level, alert.exception, alert.message), ("ERROR", "ValueError", "settlement failed for slip 42"))
        text = alert.text()
        for part in ("ERROR [shop/prod @ web-1]", "logger: tests.app", "slip: 42", "ValueError: bad stake"):
            self.assertIn(part, text)

    def test_levels_route_to_channels(self):
        log = logger_with(AlertHandler(config=config(chat={"type": "collect"}, pager={"type": "collect", "level": "CRITICAL"})))
        log.warning("slow")
        log.error("failed")
        log.critical("down")
        self.assertEqual([(name, alert.message) for name, alert, _ in SENT],
                         [("chat", "failed"), ("chat", "down"), ("pager", "down")])  # fmt: skip

    def test_logger_filters_and_opt_out(self):
        log = logger_with(
            AlertHandler(config=config(pay={"type": "collect", "loggers": ["tests.pay"]},
                                       rest={"type": "collect", "exclude_loggers": ["tests.pay"]})),  # fmt: skip
            "tests.pay.hooks",
        )
        log.error("callback rejected")
        log.error("ignored", extra={"alert": False})
        self.assertEqual([name for name, _, _ in SENT], ["pay"])

    def test_the_same_error_is_sent_once_and_repeats_are_counted(self):
        handler = AlertHandler(config=config())
        log = logger_with(handler)
        def fail(player):
            log.error("payout failed for player %s", player)

        for player in range(5):
            fail(player)
        self.assertEqual(len(SENT), 1)
        entry = handler.dispatcher.deduper._memory[SENT[0][1].fingerprint]
        self.assertEqual(entry[1], 4)
        entry[0] = -(10**9)  # the window passes
        fail(9)
        self.assertEqual(SENT[-1][1].suppressed, 4)
        self.assertIn("repeated: 4 more", SENT[-1][1].text())

    def test_a_failing_channel_never_raises_and_does_not_stop_the_others(self):
        log = logger_with(AlertHandler(config=config(bad={"type": "collect", "fail": True}, good={"type": "collect"})))
        with mock.patch("sys.stderr", io.StringIO()) as err:
            log.error("still delivered")
        self.assertEqual([name for name, _, _ in SENT], ["good"])
        self.assertIn("channel 'bad' failed: boom", err.getvalue())

    def test_a_misconfigured_channel_is_skipped(self):
        with mock.patch("sys.stderr", io.StringIO()) as err:
            dispatcher = Dispatcher(config(tg={"type": "telegram"}, ok={"type": "collect"}))
        self.assertEqual([c.name for c in dispatcher.channels], ["ok"])
        self.assertIn("'tg' is not set up", err.getvalue())

    def test_records_logged_while_sending_are_not_alerted(self):
        handler = AlertHandler(config=config(loop={"type": "callable", "function": lambda payload: log.error("inside")},
                                             all={"type": "collect"}))  # fmt: skip
        log = logger_with(handler)
        log.error("outside")
        self.assertEqual([alert.message for _, alert, _ in SENT], ["outside"])

    def test_async_delivery_does_not_block_and_flushes(self):
        dispatcher = Dispatcher({**config(), "ASYNC": True})
        self.assertTrue(notify("queued", dispatcher=dispatcher))
        self.assertTrue(dispatcher.flush(5))
        self.assertEqual([alert.message for _, alert, _ in SENT], ["queued"])

    def test_django_request_is_summarised_without_query_or_body(self):
        from django.test import RequestFactory

        log = logger_with(AlertHandler(config=config()))
        request = RequestFactory().post("/api/withdraw/?token=abc", {"pin": "1234"})
        log.error("Internal Server Error: %s", request.path, extra={"request": request})
        alert = SENT[0][1]
        self.assertEqual(alert.request, {"method": "POST", "path": "/api/withdraw/"})
        self.assertNotIn("abc", alert.text())
        self.assertNotIn("1234", alert.text())


class LimitsTests(Base):
    def test_deduper_memory_and_cache(self):
        memory = Deduper(60)
        self.assertEqual([memory.check("a", now=0), memory.check("a", now=1), memory.check("a", now=61)], [0, None, 1])
        from django.core.cache import cache

        cache.clear()
        shared_a, shared_b = Deduper(60, "default"), Deduper(60, "default")  # two processes
        self.assertEqual([shared_a.check("x"), shared_b.check("x"), shared_a.check("x")], [0, None, None])
        cache.delete("alert_channels:g:x")  # the window passes
        self.assertEqual(shared_b.check("x"), 2)

    def test_rate_limiter_reports_what_it_dropped(self):
        limiter = RateLimiter(2, 60)
        self.assertEqual([limiter.allow(now=t)[0] for t in (0, 1, 2, 3)], [True, True, False, False])
        self.assertEqual(limiter.allow(now=61), (True, 2))

    def test_rate_limit_note_reaches_the_channel(self):
        dispatcher = Dispatcher({**config(), "RATE_LIMIT": 1, "RATE_PERIOD": 60, "DEDUP_SECONDS": 0})
        for n in range(3):
            notify(f"error {n}", dispatcher=dispatcher)
        self.assertEqual(len(SENT), 1)
        dispatcher.limiters["all"]._sent.clear()
        notify("later", dispatcher=dispatcher)
        self.assertEqual(SENT[-1][2], "rate limit: 2 alerts were not sent")


class ScrubTests(Base):
    def test_secrets_and_numbers_are_masked(self):
        scrub = Scrubber()
        cases = {
            "password=hunter2 next": "password=*** next",
            '{"api_key": "sk_live_abc123"}': '{"api_key": "***"}',
            "Authorization: Bearer abcdefgh12345": "Authorization: ***",
            "redis://user:s3cret@10.0.0.1:6380/0": "redis://user:***@10.0.0.1:6380/0",
            "https://api.telegram.org/bot123456789:AAHk3kd9s8f7g6h5j4k3l2m1n0b9v8c7x6z/sendMessage":
                "https://api.telegram.org/bot***/sendMessage",
            "player +251911223344 paid": "player +251***44 paid",
            "fixture 18263341 at 1.85": "fixture 18263341 at 1.85",
        }  # fmt: skip
        for raw, expected in cases.items():
            self.assertEqual(scrub(raw), expected)
        self.assertEqual(Scrubber(enabled=False)("password=x"), "password=x")
        self.assertEqual(Scrubber(patterns=[r"ACC-\d+"])("account ACC-991"), "account ***")

    def test_alert_text_is_scrubbed_and_cut(self):
        alert = Alert.build(alert_channels.conf.normalize(config()), level=logging.ERROR, logger="x",
                            message="login failed password=abc " + "y" * 6000)  # fmt: skip
        self.assertNotIn("abc", alert.text())
        self.assertLessEqual(len(alert.text(4000)), 4000)
        self.assertLessEqual(len(alert.short(160)), 160)


class ChannelTests(Base):
    def alert(self, message="feed is down", level=logging.CRITICAL):
        return Alert.build(alert_channels.conf.normalize(config()), level=level, logger="feed", message=message)

    def test_telegram_posts_to_every_chat(self):
        dispatcher = Dispatcher(config(tg={"type": "telegram", "token": "123456:TOKEN", "chat_id": [-1001, 555], "thread_id": 7}))
        calls, patch = capture_http()
        with patch:
            dispatcher.deliver(self.alert(), dispatcher.channels)
        self.assertEqual([url for url, _, _ in calls], ["https://api.telegram.org/bot123456:TOKEN/sendMessage"] * 2)
        body = json.loads(calls[0][2])
        self.assertEqual((body["chat_id"], body["message_thread_id"]), (-1001, 7))
        self.assertIn("feed is down", body["text"])

    def test_telegram_failure_does_not_leak_the_token(self):
        import urllib.error

        dispatcher = Dispatcher(config(tg={"type": "telegram", "token": "123456789:AAHk3kd9s8f7g6h5j4k3l2m1n0b9v8c7x6z", "chat_id": 1}))

        def urlopen(request, timeout=None):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"description":"bot was blocked"}'))

        with mock.patch("urllib.request.urlopen", urlopen), mock.patch("sys.stderr", io.StringIO()) as err:
            results = dispatcher.deliver(self.alert(), dispatcher.channels)
        self.assertIn("HTTP 403", results["tg"])
        self.assertNotIn("AAHk", results["tg"] + err.getvalue())

    def test_webhook_json_is_signed(self):
        dispatcher = Dispatcher(config(hook={"type": "webhook", "url": "https://ops.example.com/in", "secret": "s3", "headers": {"X-Key": "k"}}))
        calls, patch = capture_http()
        with patch:
            dispatcher.deliver(self.alert(), dispatcher.channels)
        url, headers, body = calls[0]
        headers = {k.lower(): v for k, v in headers.items()}
        self.assertEqual(headers["x-alert-signature"], "sha256=" + hmac.new(b"s3", body, hashlib.sha256).hexdigest())
        self.assertEqual(headers["x-key"], "k")
        payload = json.loads(body)
        self.assertEqual((payload["level"], payload["service"], payload["message"]), ("CRITICAL", "shop", "feed is down"))

    def test_webhook_slack_and_discord_formats(self):
        for fmt, key in (("slack", "text"), ("discord", "content")):
            dispatcher = Dispatcher(config(hook={"type": "webhook", "url": "https://h.example.com", "format": fmt}))
            calls, patch = capture_http()
            with patch:
                dispatcher.deliver(self.alert(), dispatcher.channels)
            self.assertEqual(list(json.loads(calls[0][2])), [key])

    def test_sms_fills_the_provider_payload_per_number_and_defaults_to_critical(self):
        options = {"type": "sms", "url": "https://sms.example.com/send", "to": ["0911000001", "0911000002"],
                   "payload": {"msisdn": "{to}", "body": {"text": "{text}"}, "sender": "OPS"},
                   "headers": {"KEY": "abc"}}  # fmt: skip
        dispatcher = Dispatcher(config(sms=options))
        self.assertEqual(dispatcher.targets(self.alert(level=logging.ERROR)), [])
        calls, patch = capture_http()
        with patch:
            dispatcher.deliver(self.alert(), dispatcher.channels)
        bodies = [json.loads(body) for _, _, body in calls]
        self.assertEqual([b["msisdn"] for b in bodies], ["0911000001", "0911000002"])
        self.assertEqual(bodies[0]["body"]["text"], "CRITICAL [shop/prod @ web-1] feed is down")
        self.assertEqual(bodies[0]["sender"], "OPS")

    def test_sms_form_encoding_and_custom_backend(self):
        dispatcher = Dispatcher(config(sms={"type": "sms", "url": "https://sms.example.com", "to": "0911000001", "encoding": "form"}))
        calls, patch = capture_http()
        with patch:
            dispatcher.deliver(self.alert(), dispatcher.channels)
        self.assertIn(b"to=0911000001&text=CRITICAL", calls[0][2])

        sent = []
        with mock.patch("alert_channels.channels.builtin.import_string", return_value=lambda to, text: sent.append((to, text))):
            dispatcher = Dispatcher(config(sms={"type": "sms", "backend": "app.sms.send", "to": ["0911000009"]}))
        dispatcher.deliver(self.alert(), dispatcher.channels)
        self.assertEqual(sent[0][0], "0911000009")

    def test_redis_publish_and_list(self):
        client = mock.MagicMock()
        for mode in ("publish", "list"):
            dispatcher = Dispatcher(config(bus={"type": "redis", "url": "redis://localhost/0", "channel": "ops", "mode": mode}))
            dispatcher.channels[0]._client = client
            dispatcher.deliver(self.alert(), dispatcher.channels)
        channel, body = client.publish.call_args[0]
        self.assertEqual((channel, json.loads(body)["message"]), ("ops", "feed is down"))
        client.pipeline.return_value.lpush.assert_called_once()
        client.pipeline.return_value.ltrim.assert_called_once_with("ops", 0, 999)

    def test_callable_and_email(self):
        from django.core import mail

        dispatcher = Dispatcher(config(fn={"type": "callable", "function": "tests.test_alerts.collect"},
                                       mail={"type": "email", "to": "ops@example.com"}))  # fmt: skip
        self.assertEqual(dispatcher.deliver(self.alert(), dispatcher.channels), {"fn": "ok", "mail": "ok"})
        self.assertEqual(SENT[0][1]["message"], "feed is down")
        self.assertEqual(mail.outbox[-1].to, ["ops@example.com"])
        self.assertIn("CRITICAL: feed is down", mail.outbox[-1].subject)

    def test_send_test_reports_each_channel(self):
        dispatcher = Dispatcher(config(good={"type": "collect"}, bad={"type": "collect", "fail": True}))
        with mock.patch("sys.stderr", io.StringIO()):
            self.assertEqual(dispatcher.send_test(), {"good": "ok", "bad": "failed: boom"})


class ConfigTests(Base):
    def test_from_env_builds_only_the_channels_that_are_set(self):
        env = {
            "ALERT_SERVICE": "backend", "ALERT_ENVIRONMENT": "prod", "ALERT_DEDUP_SECONDS": "120",
            "ALERT_TELEGRAM_BOT_TOKEN": "1:T", "ALERT_TELEGRAM_CHAT_ID": "-1001, 555",
            "ALERT_WEBHOOK_URL": "https://h.example.com", "ALERT_WEBHOOK_HEADERS": '{"X-Key": "k"}',
            "ALERT_SMS_URL": "https://sms.example.com", "ALERT_SMS_TO": "0911000001",
            "ALERT_REDIS_URL": "redis://localhost/1",
        }  # fmt: skip
        built = from_env(env)
        self.assertEqual((built["SERVICE"], built["DEDUP_SECONDS"]), ("backend", 120))
        self.assertEqual(sorted(built["CHANNELS"]), ["redis", "sms", "telegram", "webhook"])
        self.assertEqual(built["CHANNELS"]["telegram"]["chat_id"], ["-1001", "555"])
        self.assertEqual(built["CHANNELS"]["sms"]["level"], "CRITICAL")
        self.assertEqual(from_env({})["CHANNELS"], {})
        self.assertEqual(len(Dispatcher(built).channels), 4)

    def test_handler_without_config_uses_the_django_setting(self):
        from django.test import override_settings

        alert_channels.reset()
        with override_settings(ALERT_CHANNELS=config()):
            logger_with(AlertHandler()).error("from settings")
            alert_channels.reset()
        self.assertEqual([alert.message for _, alert, _ in SENT], ["from settings"])

    def test_no_channels_means_no_work(self):
        handler = AlertHandler(config={"CHANNELS": {}})
        with mock.patch.object(Alert, "from_record") as build:
            logger_with(handler).error("nothing configured")
        build.assert_not_called()


class CeleryTests(Base):
    def test_a_failed_task_alerts_once_without_its_arguments(self):
        from django.test import override_settings

        from alert_channels.contrib import celery as hook

        alert_channels.reset()
        task = mock.Mock(throws=(KeyError,))
        task.name = "wallet.tasks.confirm_deposit"
        with override_settings(ALERT_CHANNELS=config()):
            hook._on_failure(sender=task, task_id="t-1", exception=RuntimeError("gateway timeout"), args=("0911000001",))
            hook._on_failure(sender=task, task_id="t-2", exception=KeyError("expected"))
            alert_channels.reset()
        (_, alert, _), = SENT
        self.assertIn("wallet.tasks.confirm_deposit failed: RuntimeError: gateway timeout", alert.message)
        self.assertEqual(alert.extra, {"task": "wallet.tasks.confirm_deposit", "task_id": "t-1"})
        self.assertNotIn("0911000001", alert.text())
        self.assertTrue(hook.install())

    def test_workers_get_the_handler_and_celery_own_failure_line_is_skipped(self):
        from alert_channels.contrib import celery as hook

        hook.install()
        worker_root = logging.Logger("worker-root")
        hook._attach(logger=worker_root)
        hook._attach(logger=worker_root)
        self.assertEqual([type(h) for h in worker_root.handlers], [AlertHandler])

        handler = AlertHandler(config=config())
        logger_with(handler, "celery.app.trace").error("Task x raised unexpected: RuntimeError()")
        logger_with(handler, "apps.wallet.tasks").error("logged inside a task")
        self.assertEqual([alert.message for _, alert, _ in SENT], ["logged inside a task"])


class CommandTests(Base):
    def run_command(self, *args, channels=None):
        from django.core.management import call_command
        from django.test import override_settings

        out = io.StringIO()
        alert_channels.reset()
        chosen = channels or {"chat": {"type": "collect"}, "pager": {"type": "collect", "level": "CRITICAL"}}
        try:
            with override_settings(ALERT_CHANNELS=config(**chosen)):
                call_command("alert_test", *args, stdout=out)
        finally:
            alert_channels.reset()
        return out.getvalue()

    def test_ping_reaches_every_channel_whatever_its_level(self):
        out = self.run_command()
        self.assertIn("chat: ok", out)
        self.assertIn("pager: ok", out)
        self.assertEqual({alert.level for _, alert, _ in SENT}, {"TEST"})
        self.assertTrue(SENT[0][1].message.startswith("[TEST] "))

    def test_levels_are_routed_and_skips_are_explained(self):
        out = self.run_command("error", "critical")
        self.assertIn("pager: skipped: this channel takes CRITICAL and above", out)
        self.assertEqual([(name, alert.level) for name, alert, _ in SENT],
                         [("chat", "ERROR"), ("chat", "CRITICAL"), ("pager", "CRITICAL")])  # fmt: skip

    def test_exception_request_and_celery_kinds_look_like_the_real_thing(self):
        self.run_command("exception", "request", "celery", "--channel", "chat")
        by_logger = {alert.logger: alert for _, alert, _ in SENT}
        self.assertIn("KeyError: 'missing_key'", by_logger["alert_test"].traceback)
        request = by_logger["django.request"]
        self.assertEqual(request.request, {"method": "POST", "path": "/alert-test/checkout/"})
        self.assertNotIn("not-shown", request.text())
        task = by_logger["celery.task"]
        self.assertIn("Celery task alert_test.tasks.example failed: TimeoutError", task.message)
        self.assertEqual(task.extra["task"], "alert_test.tasks.example")

    def test_scrub_kind_shows_the_masked_text(self):
        out = self.run_command("scrub", "--channel", "chat")
        self.assertIn("password=*** api_key=***", out)
        for secret in ("hunter2", "sk_live_123456", "user:secret@", "251911223344"):
            self.assertNotIn(secret, SENT[0][1].message)

    def test_burst_shows_deduplication(self):
        out = self.run_command("burst", "--count", "6", "--channel", "chat")
        self.assertIn("6 raised, 1 sent, 5 held back", out)
        self.assertEqual(len([1 for name, _, _ in SENT if name == "chat"]), 1)

    def test_logging_kind_checks_the_handler_is_wired(self):
        from django.core.management import CommandError

        with self.assertRaises(CommandError):
            self.run_command("logging")
        root = logging.getLogger()
        handler = AlertHandler()
        root.addHandler(handler)
        try:
            out = self.run_command("logging", "--message", "wired")
        finally:
            root.removeHandler(handler)
        self.assertIn("handed to logging and sent", out)
        self.assertEqual([alert.message for _, alert, _ in SENT if alert.logger == "alert_test"], ["[TEST] wired"])

    def test_all_list_custom_message_logger_and_bad_input(self):
        from django.core.management import CommandError

        out = self.run_command("--list")
        self.assertIn("burst", out)
        self.assertIn("pager      type collect, CRITICAL and above", out)
        self.assertEqual(SENT, [])

        self.run_command("error", "--message", "disk is full", "--logger", "apps.wallet", "--channel", "chat")
        self.assertEqual((SENT[-1][1].message, SENT[-1][1].logger), ("[TEST] disk is full", "apps.wallet"))

        for bad in (["nonsense"], ["error", "--channel", "nobody"]):
            with self.assertRaises(CommandError):
                self.run_command(*bad)
        with self.assertRaises(CommandError):
            self.run_command(channels={"bad": {"type": "collect", "fail": True}})

    def test_no_channel_configured_is_an_error(self):
        from django.core.management import CommandError, call_command
        from django.test import override_settings

        alert_channels.reset()
        with override_settings(ALERT_CHANNELS={"CHANNELS": {}}), self.assertRaises(CommandError):
            call_command("alert_test", stdout=io.StringIO())
        alert_channels.reset()


if __name__ == "__main__":
    unittest.main()
