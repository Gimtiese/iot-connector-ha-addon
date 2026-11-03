"""Token-Bucket für die Senderate zum Cloud-Broker (iot-connector-ha-addon#15).

Der Tenant-Ingest im IoT-Connector verwirft Nachrichten oberhalb von
3000 Nachrichten/Minute (50/s) still nach dem PUBACK. Beim Nachliefern eines
großen Puffers darf das Add-on diese Grenze deshalb nicht reißen. Live- und
Nachliefer-Verkehr teilen sich ein Budget; Nachliefern lässt einen Token
Reserve, damit Live-Nachrichten nicht dahinter verhungern.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class TokenBucket:
    """Thread-sicherer Token-Bucket mit injizierbarer Uhr (Tests)."""

    def __init__(
        self,
        rate: float,
        capacity: float | None = None,
        *,
        clock: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        if rate <= 0:
            raise ValueError("rate muss > 0 sein")
        self._rate = float(rate)
        self._capacity = float(capacity) if capacity is not None else max(1.0, self._rate)
        self._tokens = self._capacity
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._last = self._clock()
        self._lock = threading.Lock()

    @property
    def rate(self) -> float:
        return self._rate

    @property
    def capacity(self) -> float:
        return self._capacity

    def _refill_locked(self) -> None:
        now = self._clock()
        elapsed = now - self._last
        if elapsed > 0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
            self._last = now

    def try_acquire(self, reserve: float = 0.0) -> bool:
        """Nimmt einen Token, wenn ohne Unterschreiten von ``reserve`` möglich."""
        with self._lock:
            self._refill_locked()
            if self._tokens >= 1.0 + reserve:
                self._tokens -= 1.0
                return True
            return False

    def acquire(self, timeout: float | None = None, reserve: float = 0.0) -> bool:
        """Wartet bis zu ``timeout`` Sekunden auf einen Token.

        Ohne ``timeout`` blockiert der Aufruf, bis ein Token frei ist.
        """
        deadline = None if timeout is None else self._clock() + timeout
        while True:
            with self._lock:
                self._refill_locked()
                if self._tokens >= 1.0 + reserve:
                    self._tokens -= 1.0
                    return True
                wait = (1.0 + reserve - self._tokens) / self._rate
            if deadline is not None and self._clock() + wait > deadline:
                return False
            # Kurze Schritte, damit Abbruch/Verbindungswechsel zügig greifen.
            self._sleep(min(max(wait, 0.001), 0.5))

    def wait_seconds(self, reserve: float = 0.0) -> float:
        """Sekunden bis zum nächsten freien Token (für das Nachliefern)."""
        with self._lock:
            self._refill_locked()
            if self._tokens >= 1.0 + reserve:
                return 0.0
            return (1.0 + reserve - self._tokens) / self._rate

    def sleep(self, seconds: float) -> None:
        """Schlafen über die injizierte Sleep-Funktion (Tests)."""
        self._sleep(seconds)
