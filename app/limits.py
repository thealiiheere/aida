"""Rate limits, kept in memory: they reset on restart and aren't shared between workers."""

import math
import threading
import time
from collections import deque
from collections.abc import Callable

MAX_KEYS = 10_000


class RateLimiter:
    """Allows `limit` calls per `seconds` for each key."""

    def __init__(
        self, limit: int = 5, seconds: float = 60, clock: Callable[[], float] = time.monotonic
    ):
        self._limit = limit
        self._seconds = seconds
        self._clock = clock
        self._calls: dict[tuple, deque[float]] = {}
        self._lock = threading.Lock()

    def wait(self, *key) -> int:
        """Counts a call. Returns 0 if it's allowed, else how many seconds to wait."""
        now = self._clock()
        with self._lock:
            if len(self._calls) >= MAX_KEYS:
                self._forget_old(now)
            calls = self._calls.setdefault(key, deque())
            while calls and calls[0] <= now - self._seconds:
                calls.popleft()
            if len(calls) >= self._limit:
                return math.ceil(calls[0] + self._seconds - now)
            calls.append(now)
            return 0

    def _forget_old(self, now: float) -> None:
        self._calls = {
            key: calls
            for key, calls in self._calls.items()
            if calls and calls[-1] > now - self._seconds
        }
