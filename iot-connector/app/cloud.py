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
from .rate_limit import TokenBucket

KEEPALIVE_S = 30
# Nachliefern: Batch-Größe begrenzt nur die SQLite-Lesemenge. Die tatsächliche
# Senderate regelt der Token-Bucket (iot-connector-ha-addon#15): 3000 msg/min
# im Tenant-Ingest sind 50/s, Standard 40/s inklusive Live-Verkehr.
DRAIN_BATCH = 200
DRAIN_IDLE_S = 0.2
# Live-Nachrichten bekommen Vorrang: der Drain lässt einen Token Reserve und
# blockiert beim Publish höchstens kurz auf einen freien Token.
LIVE_RESERVE = 1.0
LIVE_SEND_TIMEOUT_S = 1.0
# Reconnect-Sturm begrenzen (989 Verbindungen in 4 min, GIM-119): paho wartet
# mindestens 2 s, höchstens 120 s zwischen Verbindungsversuchen.
RECONNECT_MIN_S = 2
RECONNECT_MAX_S = 120

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
        max_send_rate: float | None = None,
        token_bucket: TokenBucket | None = None,
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
        # Ein Budget für Live + Nachliefern (Token pro Publish).
        from .config import DEFAULT_MAX_SEND_RATE

        self._bucket = token_bucket or TokenBucket(max_send_rate or DEFAULT_MAX_SEND_RATE)
        self._stats_lock = threading.Lock()
        self.live_deferred = 0
        self.puback_errors = 0
        self._stop = threading.Event()
        self._drain_thread: threading.Thread | None = None
        self._started = False
        self.connected = False
        self.last_publish: float | None = None
        # Letzter CONNACK-Fehler (z. B. nach Löschungen im IoT-Connector) –
        # die UI zeigt daraus den Hinweis „neu verknüpfen“ (#11)
        self.last_error: str | None = None
        # Grund einmalig loggen, nicht bei jedem Retry (paho wiederholt endlos)
        self._logged_error: str | None = None

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
        # Reconnect-Sturm begrenzen (GIM-119: 989 Verbindungen in 4 min):
        # paho wartet exponentiell zwischen 2 s und 120 s. Zusätzlich begrenzen
        # wir Inflight/Queue, damit ein großer Puffer nicht im RAM des
        # Netzwerk-Threads landet – der Rest bleibt in SQLite.
        self._client.reconnect_delay_set(min_delay=RECONNECT_MIN_S, max_delay=RECONNECT_MAX_S)
        self._client.max_inflight_messages_set(20)
        self._client.max_queued_messages_set(1000)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_publish = self._on_publish
        self._host, self._port, self._path = host, port, path

    # ── Verbindung ────────────────────────────────────────────────────────── #
    def start(self) -> None:
        if self._started:
            return  # idempotent: kein zweiter Netzwerk-Thread, keine zweite Client-ID
        self._started = True
        self._stop.clear()
        logger.info(
            "Cloud-Verbindung zu %s (ws-path %s, max %s msg/s)",
            self._broker_url,
            self._path,
            self._bucket.rate,
        )
        # connect_async + loop_start: paho verbindet neu, solange die Cloud nicht
        # erreichbar ist – Nachrichten bleiben derweil im Puffer
        self._client.connect_async(self._host, self._port, KEEPALIVE_S)
        self._client.loop_start()
        # Persistenter Drain: läuft auch über Reconnects hinweg und sendet nur,
        # solange connected ist. Verhindert doppelte Drain-Threads pro CONNACK.
        self._drain_thread = threading.Thread(
            target=self._drain, daemon=True, name="cloud-drain"
        )
        self._drain_thread.start()

    def stop(self) -> None:
        self.connected = False
        self._stop.set()
        self._client.loop_stop()
        try:
            self._client.disconnect()
        except Exception:  # noqa: BLE001 - Stoppen muss immer klappen
            pass
        thread = self._drain_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._started = False

    def _notify(self) -> None:
        if self._on_state_change:
            try:
                self._on_state_change()
            except Exception:  # noqa: BLE001
                logger.exception("Status-Hook fehlgeschlagen")

    # ── paho-Callbacks ─────────────────────────────────────────────────────── #
    # CONNACK-Codes, die bedeuten: Diese Zugangsdaten werden nie akzeptiert –
    # z. B. weil das Gateway im IoT-Connector gelöscht oder getrennt wurde (#11).
    # Temporäre Codes (z. B. Server nicht bereit) behandeln wir nicht als Auth-Fehler.
    _AUTH_REASON_CODES = {4, 5, 134, 135}

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        # MQTT5-ReasonCode: 0 = Success (paho 2.1 hat kein reason_code.is_success)
        value = getattr(reason_code, "value", 0)
        self.connected = value == 0
        if self.connected:
            self.last_error = None
            self._logged_error = None
            logger.info(
                "Cloud verbunden, liefere %s gepufferte Nachrichten nach", self._store.count()
            )
            self._notify()
            # Der Drain-Thread läuft seit ``start()`` dauerhaft und wird hier
            # nicht erneut gestartet (keine Doppel-Threads pro Reconnect).
        else:
            # paho verbindet nach jedem abgelehnten CONNACK erneut – der Grund
            # gehört ins Protokoll, aber nur einmal pro Störung, nicht pro Retry
            reason = f"{reason_code}" if value not in (0,) else "unbekannt"
            self.last_error = reason
            if reason != self._logged_error:
                self._logged_error = reason
                logger.error(
                    "Cloud verbindet nicht (CONNACK %s) – Zugangsdaten ungültig"
                    " oder Broker-Störung; paho wiederholt, der Puffer bleibt aktiv",
                    reason,
                )
            if value in self._AUTH_REASON_CODES:
                self._notify()

    def _on_disconnect(self, client, userdata, flags, reason_code, properties=None) -> None:
        was = self.connected
        self.connected = False
        with self._pending_lock:
            self._pending.clear()
            self._inflight.clear()  # unbestätigte bleiben im Puffer (at-least-once)
        # Grund ins Protokoll (GIM-119: Reconnect-Sturm warum?) – aber nicht
        # bei jedem regulären Stop, only wenn die Verbindung tatsächlich stand.
        if was:
            logger.warning(
                "Cloud getrennt (reason=%s) – Puffer bleibt, paho verbindet mit Backoff neu",
                reason_code,
            )
            self._notify()

    def _on_publish(self, client, userdata, mid, reason_code=None, properties=None) -> None:
        with self._pending_lock:
            row_id = self._pending.pop(mid, None)
            if row_id is not None:
                self._inflight.discard(row_id)
        value = getattr(reason_code, "value", reason_code)
        if value not in (None, 0):
            # PUBACK mit Fehlergrund: Der Broker hat die Nachricht nicht
            # angenommen. Sichtbar machen, statt still zu verwerfen.
            with self._stats_lock:
                self.puback_errors += 1
            logger.warning("PUBACK mit Fehlergrund %s für row_id=%s", value, row_id)
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
            logger.warning("Ungültiges Cloud-Topic für row_id=%s: %s – verworfen", row_id, full)
            self._store.remove(row_id)
            return False
        info = self._client.publish(
            full,
            payload=payload,
            qos=1,
            retain=False,
            properties=self._ts_property(ts),
        )
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            # Nicht angenommen (Queue voll/Verbindung weg): Zeile bleibt im
            # Puffer und wird später erneut versucht – kein stiller Verlust.
            logger.warning(
                "Publish für row_id=%s nicht angenommen (rc=%s) – bleibt gepuffert",
                row_id,
                info.rc,
            )
            return False
        with self._pending_lock:
            self._pending[info.mid] = row_id
            self._inflight.add(row_id)
        self.last_publish = time.time()
        return True

    def publish_row(self, row_id: int, topic: str, payload: bytes, ts: float) -> bool:
        """Sendet eine bereits gepufferte Live-Nachricht, wenn die Cloud verbunden ist.

        Live hat Vorrang: Der Token-Bucket wird mit kurzem Timeout blockiert,
        der Drain lässt dagegen einen Token Reserve. Reicht das Budget nicht,
        bleibt die Zeile im Puffer und wird nachgeliefert.
        """
        if not self.connected:
            return False
        if not self._bucket.acquire(timeout=LIVE_SEND_TIMEOUT_S):
            with self._stats_lock:
                self.live_deferred += 1
            logger.debug("Live-Budget erschöpft – row_id=%s bleibt gepuffert", row_id)
            return False
        return self._publish_row(row_id, topic, payload, ts)

    def _drain(self) -> None:
        """Nachliefern: dauerhaft, gedrosselt über den Token-Bucket.

        Läuft ab ``start()`` und über Reconnects hinweg. Bereits zustellende
        Zeilen (PUBACK ausstehend) werden übersprungen. Nachliefern nimmt nur
        freie Tokens und lässt ``LIVE_RESERVE`` für Live-Nachrichten.
        """
        while not self._stop.is_set():
            if not self.connected:
                self._stop.wait(DRAIN_IDLE_S)
                continue
            if not self._drain_once():
                self._stop.wait(DRAIN_IDLE_S)

    def _drain_once(self) -> bool:
        """Ein Durchlauf über den ältesten Batch. ``False`` heißt: Puffer leer."""
        batch = self._store.next_batch(DRAIN_BATCH)
        if not batch:
            return False
        with self._pending_lock:
            todo = [row for row in batch if row[0] not in self._inflight]
        if not todo:
            # Alles in Zustellung: kurz warten, damit PUBACKs ankommen können,
            # ohne den Thread in einer Busy-Loop zu drehen.
            self._bucket.sleep(0.05)
            return True
        for row_id, topic, payload, ts in todo:
            if self._stop.is_set() or not self.connected:
                return True  # Rest bleibt gepuffert
            if not self._bucket.try_acquire(reserve=LIVE_RESERVE):
                # Kein Budget: kurz warten, dann Verbindung/Queue neu prüfen.
                wait = self._bucket.wait_seconds(reserve=LIVE_RESERVE)
                self._bucket.sleep(min(max(wait, 0.001), DRAIN_IDLE_S))
                return True
            try:
                self._publish_row(row_id, topic, payload, ts)
            except Exception:  # noqa: BLE001 - Rest bleibt gepuffert
                logger.exception("Nachliefern von %s fehlgeschlagen", topic)
                return True
        return True
