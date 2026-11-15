"""`manage.py alert_test`: send test alerts of different kinds and report what each channel did."""

import logging
import time

from django.core.management.base import BaseCommand, CommandError

from alert_channels import Alert, AlertHandler, get_dispatcher

KINDS = {
    "ping": "A plain test message to every channel, whatever its level (the default)",
    "warning": "A WARNING, routed by each channel's level",
    "error": "An ERROR, routed by each channel's level",
    "critical": "A CRITICAL, routed by each channel's level (the one an SMS channel takes)",
    "exception": "An ERROR with a real traceback",
    "request": "An unhandled error in a web request, as Django reports a 500",
    "celery": "A failed Celery task, as the task_failure hook reports it",
    "scrub": "An ERROR full of fake secrets and a phone number, to see the masking",
    "burst": "The same ERROR several times through the real pipeline, to see deduplication",
    "logging": "An ERROR through Python logging, to check the handler is wired into LOGGING",
    "all": "Every kind above, one after the other",
}
PREFIX = "[TEST]"


class Command(BaseCommand):
    help = "Send test alerts of different kinds and report what each channel did. --list shows the kinds."

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "kinds", nargs="*", metavar="kind", help=f"One or more of: {', '.join(KINDS)} (default: ping)"
        )
        parser.add_argument("--channel", action="append", default=[], help="Only this channel (repeatable)")
        parser.add_argument("--message", default="", help="Your own text for the alert")
        parser.add_argument("--logger", default="alert_test", help="Logger name to send as (tests logger filters)")
        parser.add_argument("--count", type=int, default=5, help="How many alerts the burst sends (default 5)")
        parser.add_argument("--list", action="store_true", help="Show the kinds and the configured channels")

    # --- helpers --------------------------------------------------------------------------------

    def report(self, results: dict) -> bool:
        """Print one line per channel. False when a channel failed."""
        ok = True
        for name, result in results.items():
            if result == "ok":
                style = self.style.SUCCESS
            elif result.startswith("skipped"):
                style = self.style.WARNING
            else:
                style, ok = self.style.ERROR, False
            self.stdout.write(f"  {name}: {style(result)}")
        return ok

    def build(self, level: int, message: str, **kwargs) -> Alert:
        text = self.options["message"] or message
        return Alert.build(
            self.dispatcher.config,
            level=level,
            logger=kwargs.pop("logger", None) or self.options["logger"],
            message=f"{PREFIX} {text}",
            # Unique per run, so an earlier test never hides this one.
            fingerprint=kwargs.pop("fingerprint", None) or f"alert_test:{time.time_ns()}",
            **kwargs,
        )

    def routed(self, alert: Alert) -> bool:
        return self.report(self.dispatcher.send_routed(alert, self.options["channel"] or None))

    # --- the kinds ------------------------------------------------------------------------------

    def kind_ping(self) -> bool:
        message = self.options["message"] or "Test alert: this channel works."
        return self.report(self.dispatcher.send_test(f"{PREFIX} {message}", self.options["channel"] or None))

    def kind_warning(self) -> bool:
        return self.routed(self.build(logging.WARNING, "A warning: something looks wrong but still works."))

    def kind_error(self) -> bool:
        return self.routed(self.build(logging.ERROR, "An error: something failed."))

    def kind_critical(self) -> bool:
        return self.routed(self.build(logging.CRITICAL, "A critical alert: something is down."))

    def kind_exception(self) -> bool:
        try:
            {}["missing_key"]
        except KeyError as exc:
            return self.routed(self.build(logging.ERROR, "An error with a traceback.", exc_info=exc))
        return True

    def kind_request(self) -> bool:
        from django.test import RequestFactory

        request = RequestFactory().post("/alert-test/checkout/?token=not-shown", {"card": "not-shown"})
        try:
            raise RuntimeError("the view raised")
        except RuntimeError as exc:
            alert = self.build(
                logging.ERROR,
                "Internal Server Error: /alert-test/checkout/",
                logger="django.request",
                exc_info=exc,
                request=request,
            )
        return self.routed(alert)

    def kind_celery(self) -> bool:
        from alert_channels.contrib.celery import failure_alert

        try:
            raise TimeoutError("the provider did not answer")
        except TimeoutError as exc:
            details = failure_alert("alert_test.tasks.example", "00000000-test", exc)
        message = details.pop("message")
        details.pop("fingerprint")
        return self.routed(self.build(details.pop("level"), message, **details))

    def kind_scrub(self) -> bool:
        alert = self.build(
            logging.ERROR,
            "Login failed password=hunter2 api_key=sk_live_123456 "
            "redis://user:secret@10.0.0.1:6379/0 phone +251911223344",
        )
        self.stdout.write(f"  sent as: {alert.message}")
        return self.routed(alert)

    def kind_burst(self) -> bool:
        count = max(self.options["count"], 2)
        fingerprint = f"alert_test:burst:{time.time_ns()}"
        accepted = 0
        for _ in range(count):
            alert = self.build(
                logging.ERROR, f"The same error, {count} times in a row.", fingerprint=fingerprint
            )
            accepted += bool(self.dispatcher.submit(alert))
        flushed = self.dispatcher.flush(15)
        window = self.dispatcher.config["DEDUP_SECONDS"]
        self.stdout.write(
            f"  {count} raised, {accepted} sent, {count - accepted} held back "
            f"(one per {window} s; the next one reports the repeats)"
        )
        if not flushed:
            self.stdout.write(self.style.ERROR("  the queue did not empty in 15 s"))
        return flushed and accepted >= 1

    def kind_logging(self) -> bool:
        attached = [h for h in logging.getLogger().handlers if isinstance(h, AlertHandler)]
        if not attached:
            self.stdout.write(
                self.style.ERROR(
                    "  no AlertHandler on the root logger: add it to LOGGING "
                    '("handlers": {"alerts": {"class": "alert_channels.AlertHandler"}})'
                )
            )
            return False
        message = self.options["message"] or "An error sent through Python logging."
        logging.getLogger(self.options["logger"]).error(
            "%s %s", PREFIX, message, extra={"alert_fingerprint": f"alert_test:{time.time_ns()}"}
        )
        flushed = all(handler.dispatcher.flush(15) for handler in attached)
        self.stdout.write(
            "  handed to logging and sent; failures, if any, are on stderr above"
            if flushed
            else self.style.ERROR("  the queue did not empty in 15 s")
        )
        return flushed

    # --- entry ----------------------------------------------------------------------------------

    def handle(self, *args, **options) -> None:
        self.options = options
        self.dispatcher = get_dispatcher()
        channels = self.dispatcher.channels

        if options["list"]:
            self.stdout.write("Kinds:")
            for kind, description in KINDS.items():
                self.stdout.write(f"  {kind:<10} {description}")
            self.stdout.write("Channels:")
            for channel in channels:
                level = logging.getLevelName(channel.levelno)
                self.stdout.write(f"  {channel.name:<10} type {channel.options.get('type', channel.name)}, {level} and above")
            if not channels:
                self.stdout.write("  (none configured)")
            return

        if not channels:
            raise CommandError("No alert channel is configured (ALERT_CHANNELS or ALERT_* variables).")
        unknown = [name for name in options["channel"] if name not in {c.name for c in channels}]
        if unknown:
            raise CommandError(f"No such channel: {', '.join(unknown)}. Configured: {', '.join(c.name for c in channels)}")
        kinds = options["kinds"] or ["ping"]
        bad = [kind for kind in kinds if kind not in KINDS]
        if bad:
            raise CommandError(f"Unknown kind: {', '.join(bad)}. Choose from: {', '.join(KINDS)}")
        if "all" in kinds:
            kinds = [kind for kind in KINDS if kind != "all"]

        failed = []
        for kind in kinds:
            self.stdout.write(f"{kind}: {KINDS[kind]}")
            if not getattr(self, f"kind_{kind}")():
                failed.append(kind)
        if failed:
            raise CommandError(f"Failed: {', '.join(failed)}")
