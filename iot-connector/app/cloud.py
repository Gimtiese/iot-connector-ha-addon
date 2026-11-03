"""Cloud-Client: MQTT über WebSocket mit TLS zum IoT-Connector (ADR 0001, #129).

- MQTT 5, QoS 1: Original-Zeitstempel als User-Property ``ts`` (#124)
- Keepalive 30 s (≤ 60 s laut Spezifikation)
- Zugangsdaten aus dem Claim (iot-connector#122) – Client-ID und Username kommen
  vom Backend, niemals aus manuellen Einstellungen
- Store-and-Forward: Gesendet wird ausschließlich, was im Puffer liegt; eine
  Zeile verschwindet erst nach dem PUBACK (at-least-once)
"""

from __future__ import annotations

import logging
import ssl
import threading
import time
import urllib.parse

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from .mapping import cloud_topic, is_valid_topic

KEEPALIVE_S = 30
# Nachliefern gedrosselt: kleine Batches mit Pause, damit retained-Bursts beim
# Start (z. B. Discovery unter homeassistant/…) Tenant-Ratenlimits nicht reißen
# (iot-connector#117). Puffergröße begrenzt zusätzlich.
DRAIN_BATCH = 200
DRAIN_PAUSE_S = 0.1

logger = logging.getLogger(__name__)


def parse_ws_url(url: str) -> tuple[str, int, bool, str]:
    """``wss://host:port/pfad`` → (host, port, tls, pfad). Default-Port 443/80."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ("ws", "wss"):
        raise ValueError(f"Kein WebSocket-URL: {url}")
    tls = parsed.scheme == "wss"
    port = parsed.port or (443 if tls else 80)
    return parsed.hostname or "", port, tls, parsed.path or "/"


class CloudClient:
    """Verbindung zum Cloud-Broker mit Store-and-Forward über einen Puffer."""

    def __init__(
        self,
        broker_url: str,
        username: str,
        password: str,
        client_id: str,
        topic_prefix: str,
        store,  # MessageStore
        on_state_change=None,
    ) -> None:
        self._broker_url = broker_url
        self._username = username
        self._password = password
        self._client_id = client_id
        self._topic_prefix = topic_prefix
        self._store = store
        self._on_state_change = on_state_change
        # mid → row_id (PUBACK ausstehend) und row_ids in Zustellung
        self._pending: dict[int, int] = {}
        self._inflight: set[int] = set()
        self._pending_lock = threading.Lock()
        self.connected = False
        self.last_publish: float | None = None

        host, port, tls, path = parse_ws_url(broker_url)
        self._client = mqtt.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=mqtt.MQTTv5,
            transport="websockets",
        )
        if tls:
            self._client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)
            self._client.tls_insecure_set(False)
        self._client.ws_set_options(path=path)
        if username:
            self._client.username_pw_set(username, password)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_publish = self._on_publish
        self._host, self._port, self._path = host, port, path

    # ── Verbindung ────────────────────────────────────────────────────────── #
    def start(self) -> None:
        logger.info("Cloud-Verbindung zu %s (ws-path %s)", self._broker_url, self._path)
        # connect_async + loop_start: paho verbindet neu, solange die Cloud nicht
        # erreichbar ist – Nachrichten bleiben derweil im Puffer
        self._client.connect_async(self._host, self._port, KEEPALIVE_S)
        self._client.loop_start()

    def stop(self) -> None:
        self.connected = False
        self._client.loop_stop()
        try:
            self._client.disconnect()
        except Exception:  # noqa: BLE001 - Stoppen muss immer klappen
            pass

    def _notify(self) -> None:
        if self._on_state_change:
            try:
                self._on_state_change()
            except Exception:  # noqa: BLE001
                logger.exception("Status-Hook fehlgeschlagen")

    # ── paho-Callbacks ─────────────────────────────────────────────────────── #
    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        # MQTT5-ReasonCode: 0 = Success (paho 2.1 hat kein reason_code.is_success)
        self.connected = getattr(reason_code, "value", 0) == 0
        if self.connected:
            logger.info(
                "Cloud verbunden, liefere %s gepufferte Nachrichten nach", self._store.count()
            )
            self._notify()
            # Nachliefern im eigenen Thread: Der Netzwerk-Thread muss weiter
            # laufen, sonst werden PUBLISH/PUBACK nie geschrieben/gelesen
            threading.Thread(target=self._drain, daemon=True).start()
        else:
            logger.warning("Cloud verbindet nicht: %s", reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        was = self.connected
        self.connected = False
        with self._pending_lock:
            self._pending.clear()
            self._inflight.clear()  # unbestätigte bleiben im Puffer (at-least-once)
        if was:
            self._notify()

    def _on_publish(self, client, userdata, mid, reason_code=None, properties=None) -> None:
        with self._pending_lock:
            row_id = self._pending.pop(mid, None)
            if row_id is not None:
                self._inflight.discard(row_id)
        if row_id is not None:
            self._store.remove(row_id)  # erst nach PUBACK weg aus dem Puffer

    # ── Publizieren ────────────────────────────────────────────────────────── #
    @staticmethod
    def _ts_property(ts: float) -> Properties:
        props = Properties(PacketTypes.PUBLISH)
        props.UserProperty = [("ts", f"{ts:.3f}")]
        return props

    def _publish_row(self, row_id: int, topic: str, payload: bytes, ts: float) -> bool:
        """Sendet eine gepufferte Zeile (QoS 1, User-Property ``ts``).

        Returns:
            True, wenn das PUBLISH abgeschickt wurde. Die Zeile bleibt so lange
            im Puffer, bis der PUBACK sie entfernt – at-least-once.
        """
        full = cloud_topic(self._topic_prefix, topic)
        if not is_valid_topic(full):
            self._store.remove(row_id)
            return False
        info = self._client.publish(
            full,
            payload=payload,
            qos=1,
            retain=False,
            properties=self._ts_property(ts),
        )
        with self._pending_lock:
            self._pending[info.mid] = row_id
            self._inflight.add(row_id)
        self.last_publish = time.time()
        return True

    def publish_row(self, row_id: int, topic: str, payload: bytes, ts: float) -> bool:
        """Sendet eine bereits gepufferte Nachricht, wenn die Cloud verbunden ist."""
        if not self.connected:
            return False
        return self._publish_row(row_id, topic, payload, ts)

    def _drain(self) -> None:
        """Nachliefern: älteste Zeilen zuerst, bis der Puffer leer ist.

        Läuft im eigenen Thread. Bereits zustellende Zeilen (PUBACK ausstehend)
        werden übersprungen – keine endlose Wiederholung, solange der
        Netzwerk-Thread die Bestätigungen verarbeitet.
        """
        while self.connected:
            batch = self._store.next_batch(DRAIN_BATCH)
            if not batch:
                return  # Puffer leer
            with self._pending_lock:
                todo = [row for row in batch if row[0] not in self._inflight]
            if not todo:
                time.sleep(0.1)  # alles unterwegs – PUBACKs abwarten
                continue
            for row_id, topic, payload, ts in todo:
                if not self.connected:
                    return  # Rest bleibt gepuffert
                try:
                    self._publish_row(row_id, topic, payload, ts)
                except Exception:  # noqa: BLE001 - Rest bleibt gepuffert
                    logger.exception("Nachliefern von %s fehlgeschlagen", topic)
                    return
            time.sleep(DRAIN_PAUSE_S)
