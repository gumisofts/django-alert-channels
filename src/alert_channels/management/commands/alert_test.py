from django.core.management.base import BaseCommand, CommandError

from alert_channels import get_dispatcher


class Command(BaseCommand):
    help = "Send a test alert to every configured channel and report which ones worked."

    def add_arguments(self, parser) -> None:
        parser.add_argument("message", nargs="?", default="Test alert: this channel works.")

    def handle(self, *args, **options) -> None:
        dispatcher = get_dispatcher()
        if not dispatcher.channels:
            raise CommandError("No alert channel is configured (ALERT_CHANNELS or ALERT_* variables).")
        results = dispatcher.send_test(options["message"])
        for name, result in results.items():
            style = self.style.SUCCESS if result == "ok" else self.style.ERROR
            self.stdout.write(f"{name}: {style(result)}")
        if any(result != "ok" for result in results.values()):
            raise CommandError("At least one channel failed.")
