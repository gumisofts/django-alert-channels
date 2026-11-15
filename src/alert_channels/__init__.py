"""django-alert-channels: send errors and alerts from your apps to Telegram, SMS, webhooks, a Redis
bus, email or your own function, through a standard `logging` handler."""

from .alert import Alert
from .channels import Channel, ChannelError, register
from .conf import from_env
from .dispatcher import Dispatcher, get_dispatcher, reset
from .handler import AlertHandler, notify

__version__ = "0.2.0"

__all__ = [
    "Alert",
    "AlertHandler",
    "Channel",
    "ChannelError",
    "Dispatcher",
    "from_env",
    "get_dispatcher",
    "notify",
    "register",
    "reset",
]
