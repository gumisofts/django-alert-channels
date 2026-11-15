import django
from django.conf import settings

if not settings.configured:
    settings.configure(
        SECRET_KEY="tests",
        INSTALLED_APPS=["alert_channels"],
        CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="alerts@example.com",
        ALERT_CHANNELS={"ASYNC": False, "CHANNELS": {}},
    )
    django.setup()
