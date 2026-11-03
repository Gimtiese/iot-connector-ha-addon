"""Tests: Senderate begrenzt (iot-connector-ha-addon#15).

Der Tenant-Ingest verwirft oberhalb von 3000 Nachrichten/Minute (50/s) still.
Beim Nachliefern eines großen Puffers darf das Add-on diese Grenze nicht
reißen. Geprüft werden der Token-Bucket selbst und das Zusammenspiel mit dem
Drain (50.000 Nachrichten) mit einer simulierten Uhr – ohne echtes Warten.
"""

from __future__ import annotations

import threading

from app.cloud import CloudClient
from app.rate_limit import TokenBucket


class FakeInfo:
    rc = 0

    def __init__(self, mid: int) -> None:
        self.mid = mid


class FakeMqttClient:
    """Zeichnet PUBLISH-Aufrufe samt simuliertem Zeitstempel auf."""

    def __init__(self, clock) -> None:
        self._clock = clock
        self.mid = 0
        self.published: list[tuple[str, bytes]] = []
        self.publish_times: list[float] = []

    def publish(self, topic, payload, qos, retain, properties=None):  # noqa: ARG002
        self.mid += 1
        self.published.append((topic, bytes(payload)))
        self.publish_times.append(self._clock())
        return FakeInfo(self.mid)


class FakeStore:
    """FIFO ohne SQLite; Zeilen bleiben bis zum PUBACK (``remove``)."""

    def __init__(self, count: int) -> None:
        self.rows = [(i, "t/x", f"p{i}".encode(), 1.0) for i in range(count)]
        self.head = 0

    def next_batch(self, limit: int):
        return self.rows[self.head : self.head + limit]

    def remove(self, row_id: int) -> None:
        while self.head < len(self.rows) and self.rows[self.head][0] <= row_id:
            self.head += 1

    def count(self) -> int:
        return len(self.rows) - self.head

    def dropped(self) -> int:
        return 0


def _make_cloud(store, client, bucket) -> CloudClient:
    """CloudClient ohne echten paho-Client/Thread – nur für den Drain-Test."""
    cloud = CloudClient.__new__(CloudClient)
    cloud._broker_url = "wss://example.test"
    cloud._path = "/"
    cloud._topic_prefix = "1/"
    cloud._store = store
    cloud._on_state_change = None
    cloud._pending = {}
    cloud._inflight = set()
    cloud._pending_lock = threading.Lock()
    cloud._stats_lock = threading.Lock()
    cloud._bucket = bucket
    cloud.live_deferred = 0
    cloud.puback_errors = 0
    cloud._stop = threading.Event()
    cloud._drain_thread = None
    cloud._started = True
    cloud.connected = True
    cloud.last_publish = None
    cloud.last_error = None
    cloud._logged_error = None
    cloud._client = client
    return cloud


class TestTokenBucket:
    def test_burst_dann_nachfuellen(self) -> None:
        now = [0.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            now[0] += seconds

        bucket = TokenBucket(rate=40.0, clock=lambda: now[0], sleep=sleep)
        assert bucket.capacity == 40.0
        for _ in range(40):
            assert bucket.try_acquire()
        assert not bucket.try_acquire()

        now[0] += 0.025  # 1 Token bei 40/s
        assert bucket.try_acquire()
        assert not bucket.try_acquire()

    def test_reserve_bleibt_fuer_live(self) -> None:
        now = [0.0]
        bucket = TokenBucket(rate=10.0, capacity=2.0, clock=lambda: now[0],
                             sleep=lambda s: None)
        assert bucket.try_acquire(reserve=1.0)  # 2 -> 1 Token
        assert not bucket.try_acquire(reserve=1.0)  # Reserve von 1 bleibt
        assert bucket.try_acquire(reserve=0.0)  # Live darf den Rest nehmen

    def test_acquire_timeout(self) -> None:
        now = [0.0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        bucket = TokenBucket(rate=1.0, capacity=1.0, clock=lambda: now[0], sleep=sleep)
        assert bucket.try_acquire()
        # 0.5 s Timeout < 1 s bis zum nächsten Token -> kein Erfolg, kein Verbrauch
        assert not bucket.acquire(timeout=0.5)
        assert now[0] < 1.0


class TestDrainRate:
    def test_50000_nachrichten_ohne_limitbruch(self) -> None:
        """Simulierter Ausfall: 50.000 gepufferte Nachrichten nachliefern."""
        total = 50_000
        now = [0.0]
        slept: list[float] = []

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            now[0] += seconds

        bucket = TokenBucket(rate=40.0, clock=lambda: now[0], sleep=sleep)
        client = FakeMqttClient(lambda: now[0])
        store = FakeStore(total)
        cloud = _make_cloud(store, client, bucket)

        iterations = 0
        while True:
            worked = cloud._drain_once()
            # PUBACKs simulieren: erst dann verschwinden Zeilen aus dem Puffer.
            for mid in list(cloud._pending):
                cloud._on_publish(None, None, mid)
            iterations += 1
            assert iterations < 200_000
            if not worked:
                break

        assert len(client.published) == total
        assert store.count() == 0
        # 50.000 bei 40/s (minus Start-Burst) brauchen ~1250 s simulierte Zeit.
        assert now[0] >= (total - bucket.capacity) / bucket.rate
        # Kein 1-s-Fenster über Burst + Rate hinaus (Tenant-Limit 50/s).
        times = client.publish_times
        start = 0
        max_window = 0
        for i, ts in enumerate(times):
            while times[start] < ts - 1.0:
                start += 1
            max_window = max(max_window, i - start + 1)
        assert max_window <= bucket.capacity + bucket.rate + 1

    def test_live_nachricht_nach_drain_nicht_verhungert(self) -> None:
        """Nach dem Drain bleibt ein Token Reserve – Live geht sofort durch."""
        now = [0.0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        bucket = TokenBucket(rate=40.0, clock=lambda: now[0], sleep=sleep)
        client = FakeMqttClient(lambda: now[0])
        store = FakeStore(3)
        cloud = _make_cloud(store, client, bucket)

        # Drain bis der Puffer leer ist (lässt LIVE_RESERVE=1 Token stehen).
        while cloud._drain_once():
            for mid in list(cloud._pending):
                cloud._on_publish(None, None, mid)

        assert cloud.publish_row(9999, "t/live", b"live", 1.0) is True
        assert client.published[-1] == ("1/t/live", b"live")

    def test_live_bei_erschoepftem_budget_bleibt_gepuffert(self) -> None:
        now = [0.0]

        def sleep(seconds: float) -> None:
            now[0] += seconds

        # 0.1 Token/s: nächster Token erst in 10 s, Timeout ist 1 s
        bucket = TokenBucket(rate=0.1, capacity=1.0, clock=lambda: now[0], sleep=sleep)
        assert bucket.try_acquire()  # Budget leer
        client = FakeMqttClient(lambda: now[0])
        store = FakeStore(0)
        cloud = _make_cloud(store, client, bucket)

        assert cloud.publish_row(1, "t/live", b"live", 1.0) is False
        assert cloud.live_deferred == 1
        assert client.published == []
