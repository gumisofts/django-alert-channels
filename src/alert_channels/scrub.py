"""Mask secrets and personal numbers before an alert leaves the server."""

import re
from typing import Iterable, List, Pattern

MASK = "***"

# password=..., "api_key": "...", Authorization: ...
_KEY_VALUE = re.compile(
    r"(?i)([\w-]*(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|sessionid|signature)"
    r"[\w-]*)([\"']?\s*[:=]\s*[\"']?)(?:(?:bearer|basic)\s+)?([^\s\"',&})\]]+)"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[\w.~+/=-]{8,}")
# Telegram bot tokens, also inside URLs (`/bot123456:AA.../sendMessage`).
_BOT_TOKEN = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
# scheme://user:password@host
_URL_CREDENTIALS = re.compile(r"(://[^/\s:@]+):[^/\s@]+@")
# Runs of 9 to 19 digits: phone numbers, card numbers. Keeps the first 3 and last 2.
_LONG_NUMBER = re.compile(r"(?<![\w.])\+?\d{9,19}(?![\w.])")


def _mask_number(match: "re.Match") -> str:
    text = match.group(0)
    sign, digits = ("+", text[1:]) if text.startswith("+") else ("", text)
    return f"{sign}{digits[:3]}{MASK}{digits[-2:]}"


class Scrubber:
    def __init__(self, *, enabled: bool = True, numbers: bool = True, patterns: Iterable[str] = ()):
        self.enabled = enabled
        self.numbers = numbers
        self.extra: List[Pattern] = [re.compile(p) for p in patterns or ()]

    def __call__(self, text: str) -> str:
        if not self.enabled or not text:
            return text
        text = _URL_CREDENTIALS.sub(rf"\1:{MASK}@", text)
        text = _BOT_TOKEN.sub(MASK, text)
        text = _KEY_VALUE.sub(rf"\1\2{MASK}", text)
        text = _BEARER.sub(rf"\1 {MASK}", text)
        for pattern in self.extra:
            text = pattern.sub(MASK, text)
        if self.numbers:
            text = _LONG_NUMBER.sub(_mask_number, text)
        return text
