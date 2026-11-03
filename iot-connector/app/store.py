"""Store-and-Forward-Puffer (SQLite) für Nachrichten bei Cloud-Verbindungsverlust (#124, #129).

At-least-once: Eine Zeile bleibt so lange im Puffer, bis der Cloud-Broker das
PUBLISH (QoS 1) bestätigt hat. Verwirft der Puffer älteste Zeilen, passiert das
vor dem Publish – die neuesten Daten haben Vorrang.
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time

_SCHEMA = """
CREATE TABLE IF NOT EXISTS buffer (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic TEXT NOT NULL,
    payload BLOB NOT NULL,
    ts REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_buffer_created ON buffer (id);
"""


class MessageStore:
    """Thread-sicherer FIFO-Puffer (älteste zuerst nachliefern)."""

    def __init__(self, path: str, max_messages: int = 50000) -> None:
        self._lock = threading.Lock()
        self._max = max_messages
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._dropped = 0

    # ── Schreiben ─────────────────────────────────────────────────────────── #
    def add(self, topic: str, payload: bytes, ts: float | None = None) -> int:
        """Puffert eine Nachricht und gibt die Zeilen-ID zurück.

        Übersteigt der Puffer die Grenze, verwirft er die ältesten Zeilen
        (Puffergrenze konfigurierbar, kein Absturz, kein unbegrenztes Wachstum).
        """
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO buffer (topic, payload, ts, created_at) VALUES (?, ?, ?, ?)",
                (topic, sqlite3.Binary(payload), ts if ts is not None else now, now),
            )
            row_id = int(cur.lastrowid)
            excess = self.count() - self._max
            if excess > 0:
                self._db.execute(
                    "DELETE FROM buffer WHERE id IN "
                    "(SELECT id FROM buffer ORDER BY id ASC LIMIT ?)",
                    (excess,),
                )
                self._dropped += excess
            self._db.commit()
        return row_id

    def count(self) -> int:
        row = self._db.execute("SELECT COUNT(*) FROM buffer").fetchone()
        return int(row[0])

    def dropped(self) -> int:
        return self._dropped

    # ── Nachliefern ────────────────────────────────────────────────────────── #
    def next_batch(self, limit: int = 500) -> list[tuple[int, str, bytes, float]]:
        """Älteste `limit` Zeilen – FIFO, zum Nachliefern nach der Verbindung."""
        with self._lock:
            rows = self._db.execute(
                "SELECT id, topic, payload, ts FROM buffer ORDER BY id ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [(int(r[0]), r[1], bytes(r[2]), float(r[3])) for r in rows]

    def remove(self, row_id: int) -> None:
        """Entfernt eine Zeile erst nach der Publish-Bestätigung (QoS 1, PUBACK)."""
        with self._lock:
            self._db.execute("DELETE FROM buffer WHERE id = ?", (row_id,))
            self._db.commit()

    def oldest_ts(self) -> float | None:
        row = self._db.execute("SELECT MIN(created_at) FROM buffer").fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def close(self) -> None:
        with self._lock:
            self._db.close()
