"""The dispatcher: deduplication, rate limits and the background thread that does the sending.

Nothing here may raise into the application or block it: `submit()` only puts the alert on a
bounded queue. A send that fails is reported on stderr (never through `logging`, which would loop).
"""

import atexit
import os
import queue
import sys
import threading
import time
from collections import deque
from typing import Any, Dict, List, Mapping, Optional, Tuple

from . import channels as channel_module
from .alert import Alert
from .conf import load
from .scrub import Scrubber

#: Set while a thread is sending, so anything it logs is not turned into another alert.
_state = threading.local()


def sending() -> bool:
    return getattr(_state, "busy", False)


def _complain(text: str) -> None:
    try:
        print(f"alert_channels: {Scrubber()(text)}", file=sys.stderr, flush=True)
    except Exception:
        pass


class Deduper:
    """One alert per fingerprint per window. `check()` returns None to drop the alert, else how many
    were dropped since the one sent before.

    With `cache_alias` the state lives in a Django cache, shared by every process that uses it; if
    the cache fails, the in-process memory takes over.
    """

    MAX_ENTRIES = 5000

    def __init__(self, seconds: float, cache_alias: str = "") -> None:
        self.seconds = float(seconds or 0)
        self.cache_alias = cache_alias
        self._lock = threading.Lock()
        self._memory: Dict[str, List[float]] = {}  # fingerprint -> [window start, dropped]

    def check(self, fingerprint: str, now: Optional[float] = None) -> Optional[int]:
        if self.seconds <= 0:
            return 0
        if self.cache_alias:
            try:
                return self._check_cache(fingerprint)
            except Exception:
                pass
        return self._check_memory(fingerprint, time.monotonic() if now is None else now)

    def _check_memory(self, fingerprint: str, now: float) -> Optional[int]:
        with self._lock:
            entry = self._memory.get(fingerprint)
            if entry is not None and now - entry[0] < self.seconds:
                entry[1] += 1
                return None
            dropped = int(entry[1]) if entry is not None else 0
            if len(self._memory) >= self.MAX_ENTRIES:
                cutoff = now - self.seconds
                self._memory = {k: v for k, v in self._memory.items() if v[0] >= cutoff}
            self._memory[fingerprint] = [now, 0]
            return dropped

    def _check_cache(self, fingerprint: str) -> Optional[int]:
        from django.core.cache import caches

        cache = caches[self.cache_alias]
        gate, count = f"alert_channels:g:{fingerprint}", f"alert_channels:c:{fingerprint}"
        if cache.add(gate, 1, self.seconds):
            dropped = int(cache.get(count) or 0)
            if dropped:
                cache.delete(count)
            return dropped
        try:
            cache.incr(count)
        except ValueError:
            cache.add(count, 1, max(self.seconds * 12, 3600))
        return None


class RateLimiter:
    """At most `limit` sends per `period` seconds. `allow()` returns `(ok, dropped)`, where `dropped`
    counts the alerts refused since the last one let through."""

    def __init__(self, limit: int, period: float) -> None:
        self.limit, self.period = int(limit or 0), float(period or 0)
        self._sent: deque = deque()
        self._dropped = 0

    def allow(self, now: Optional[float] = None) -> Tuple[bool, int]:
        if self.limit <= 0 or self.period <= 0:
            return True, 0
        now = time.monotonic() if now is None else now
        while self._sent and now - self._sent[0] >= self.period:
            self._sent.popleft()
        if len(self._sent) >= self.limit:
            self._dropped += 1
            return False, 0
        self._sent.append(now)
        dropped, self._dropped = self._dropped, 0
        return True, dropped


class Dispatcher:
    def __init__(self, config: Optional[Mapping[str, Any]] = None) -> None:
        self.config = load(config)
        self.channels: List[channel_module.Channel] = []
        for name, options in self.config["CHANNELS"].items():
            try:
                self.channels.append(channel_module.build(name, options or {}, self.config))
            except Exception as exc:
                # A broken channel must not take logging down with it.
                _complain(f"channel {name!r} is not set up: {exc}")
        self.deduper = Deduper(self.config["DEDUP_SECONDS"], self.config["DEDUP_CACHE"])
        self.limiters = {
            channel.name: RateLimiter(
                channel.options.get("rate_limit", self.config["RATE_LIMIT"]),
                channel.options.get("rate_period", self.config["RATE_PERIOD"]),
            )
            for channel in self.channels
        }
        self.ignore = tuple(self.config.get("IGNORE_LOGGERS") or ())
        self.min_level = min((c.levelno for c in self.channels), default=10**6)
        self.overflow = 0
        self._queue: Optional[queue.Queue] = None
        self._thread: Optional[threading.Thread] = None
        self._pid: Optional[int] = None
        self._lock = threading.Lock()
        self._last_complaint = float("-inf")

    # --- intake ---------------------------------------------------------------------------------

    def targets(self, alert: Alert) -> List[channel_module.Channel]:
        if self.ignore and alert.logger.startswith(self.ignore):
            return []
        return [channel for channel in self.channels if channel.accepts(alert)]

    def submit(self, alert: Alert) -> bool:
        """Queue the alert for its channels. False when no channel wants it or it was deduplicated."""
        targets = self.targets(alert)
        if not targets:
            return False
        dropped = self.deduper.check(alert.fingerprint)
        if dropped is None:
            return False
        alert.suppressed = dropped
        if not self.config["ASYNC"]:
            self.deliver(alert, targets)
            return True
        self._ensure_worker()
        try:
            self._queue.put_nowait((alert, targets))  # type: ignore[union-attr]
        except queue.Full:
            self.overflow += 1
            return False
        return True

    # --- sending --------------------------------------------------------------------------------

    def deliver(self, alert: Alert, targets: List[channel_module.Channel]) -> Dict[str, str]:
        """Send to each target now. Returns `{channel name: "ok" | "rate limited" | error}`."""
        results: Dict[str, str] = {}
        _state.busy = True
        try:
            for channel in targets:
                allowed, dropped = self.limiters[channel.name].allow()
                if not allowed:
                    results[channel.name] = "rate limited"
                    continue
                note = f"rate limit: {dropped} alerts were not sent" if dropped else ""
                results[channel.name] = self._send(channel, alert, note)
        finally:
            _state.busy = False
        return results

    def _send(self, channel: channel_module.Channel, alert: Alert, note: str = "") -> str:
        try:
            channel.send(alert, note)
            return "ok"
        except Exception as exc:
            reason = str(exc) or type(exc).__name__
            now = time.monotonic()
            if now - self._last_complaint > 60:
                self._last_complaint = now
                _complain(f"channel {channel.name!r} failed: {reason}")
            return f"failed: {Scrubber()(reason)}"

    def send_test(self, message: str = "Test alert: this channel works.") -> Dict[str, str]:
        """Send one alert to every channel now, ignoring levels, dedup and rate limits."""
        import logging

        alert = Alert.build(self.config, level=logging.CRITICAL, logger="alert_channels.test", message=message)
        alert.level = "TEST"
        _state.busy = True
        try:
            return {channel.name: self._send(channel, alert) for channel in self.channels}
        finally:
            _state.busy = False

    # --- the worker thread ----------------------------------------------------------------------

    def _ensure_worker(self) -> None:
        # A forked child (gunicorn, Celery prefork) inherits the object but not the thread.
        if self._thread is not None and self._pid == os.getpid() and self._thread.is_alive():
            return
        with self._lock:
            if self._thread is not None and self._pid == os.getpid() and self._thread.is_alive():
                return
            self._queue = queue.Queue(maxsize=int(self.config["QUEUE_SIZE"]))
            self._pid = os.getpid()
            self._thread = threading.Thread(target=self._run, name="alert-channels", daemon=True)
            self._thread.start()
            atexit.register(self.flush, 3.0)

    def _run(self) -> None:
        work = self._queue
        while True:
            alert, targets = work.get()  # type: ignore[union-attr]
            try:
                self.deliver(alert, targets)
            except Exception as exc:  # pragma: no cover - deliver() already catches
                _complain(f"delivery failed: {type(exc).__name__}")
            finally:
                work.task_done()  # type: ignore[union-attr]

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until queued alerts are sent (process exit, tests). False on timeout."""
        work = self._queue
        if work is None or self._pid != os.getpid():
            return True
        deadline = time.monotonic() + timeout
        while work.unfinished_tasks:
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)
        return True


_default: Optional[Dispatcher] = None
_default_lock = threading.Lock()


def get_dispatcher() -> Dispatcher:
    """The process-wide dispatcher, built from `settings.ALERT_CHANNELS` (or the environment)."""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = Dispatcher()
    return _default


def reset() -> None:
    """Forget the process-wide dispatcher (tests, after changing settings)."""
    global _default
    with _default_lock:
        if _default is not None:
            _default.flush(2.0)
        _default = None
