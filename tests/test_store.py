"""Unit-Tests: Store-and-Forward-Puffer (SQLite) – FIFO, Grenze, Nachliefern (#124, #129)."""

from __future__ import annotations

import threading
import time

import pytest
from app.store import MessageStore


@pytest.fixture()
def store(tmp_path):
    s = MessageStore(str(tmp_path / "buffer.sqlite"), max_messages=5)
    yield s
    s.close()


class TestFifo:
    def test_nachliefern_aelteste_zuerst(self, store: MessageStore) -> None:
        for i in range(3):
            store.add(f"zigbee2mqtt/s{i}", b"%d" % i, ts=1000.0 + i)
        batch = store.next_batch(10)
        assert [t for _, t, _, _ in batch] == [
            "zigbee2mqtt/s0",
            "zigbee2mqtt/s1",
            "zigbee2mqtt/s2",
        ]

    def test_entfernen_nach_puback(self, store: MessageStore) -> None:
        row_id = store.add("zigbee2mqtt/s0", b"x", ts=1.0)
        assert store.count() == 1
        store.remove(row_id)
        assert store.count() == 0
        assert store.next_batch(10) == []


class TestPuffergrenze:
    def test_grenze_verwirft_aelteste(self, store: MessageStore) -> None:
        for i in range(8):  # Grenze: 5
            store.add(f"t/{i}", b"%d" % i, ts=float(i))
        assert store.count() == 5
        topics = [t for _, t, _, _ in store.next_batch(10)]
        assert topics == [f"t/{i}" for i in range(3, 8)]  # die neuesten bleiben

    def test_verworfene_werden_zaehlt(self, store: MessageStore) -> None:
        for i in range(8):
            store.add(f"t/{i}", b"x", ts=float(i))
        assert store.dropped() == 3

    def test_persistenz_über_restart(self, tmp_path) -> None:
        path = str(tmp_path / "buffer.sqlite")
        first = MessageStore(path, 100)
        first.add("zigbee2mqtt/s0", b"kept", ts=5.0)
        first.close()
        again = MessageStore(path, 100)
        try:
            batch = again.next_batch(10)
            assert batch[0][1:] == ("zigbee2mqtt/s0", b"kept", 5.0)
        finally:
            again.close()


class TestInternausfall:
    def test_ts_bleibt_erhalten(self, store: MessageStore) -> None:
        """Original-Zeitstempel (User-Property ts, #124) übersteht den Puffer."""
        original = time.time() - 3600  # Nachricht von vor einer Stunde
        store.add("zigbee2mqtt/s0", b"1", ts=original)
        assert store.next_batch(1)[0][3] == original


class TestThreadSicherheit:
    """#13: ``count()``/``oldest_ts()`` lieferten ohne Lock ``None`` → TypeError."""

    def test_paralleles_add_count_und_remove(self, tmp_path) -> None:
        store = MessageStore(str(tmp_path / "race.sqlite"), max_messages=10_000)
        errors: list[Exception] = []

        def adder(n: int) -> None:
            try:
                for i in range(n):
                    store.add(f"t/{threading.current_thread().name}/{i}", b"x")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def reader(n: int) -> None:
            try:
                for _ in range(n):
                    store.count()
                    store.oldest_ts()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=adder, args=(200,)) for _ in range(4)]
        threads += [threading.Thread(target=reader, args=(200,)) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Race recreatet: {errors}"
        assert store.count() == 800

        # Entfernen parallel zu Reads: FIFO-Zeilen einzeln weg, Puffer am Ende leer
        ids = [r[0] for r in store.next_batch(1000)]

        def remover(chunk: list[int]) -> None:
            try:
                for row_id in chunk:
                    store.remove(row_id)
                    store.count()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        rm_threads = [threading.Thread(target=remover, args=(ids[i::4],)) for i in range(4)]
        for t in rm_threads:
            t.start()
        for t in rm_threads:
            t.join()
        assert not errors, f"Race recreatet: {errors}"
        assert store.count() == 0
        assert store.oldest_ts() is None
        store.close()
