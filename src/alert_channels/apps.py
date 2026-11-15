from django.apps import AppConfig


class AlertChannelsConfig(AppConfig):
    """Optional: add `"alert_channels"` to INSTALLED_APPS for the `alert_test` command and the
    automatic Celery hook. The logging handler works without it."""

    name = "alert_channels"
    verbose_name = "Alert channels"

    def ready(self) -> None:
        from .conf import load

        if load().get("CELERY", True):
            from .contrib import celery

            celery.install()
