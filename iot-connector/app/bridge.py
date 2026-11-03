"""Bridge: abonniert lokal (nur lesend) und spiegelt Allowlist-Topics in die Cloud.

Pufferung gehört zur Bridge: Jede erlaubte Nachricht landet immer zuerst im
Store-and-Forward-Puffer (SQLite); die Cloud sendet ausschließlich gepufferte
Zeilen und entfernt sie erst nach dem PUBACK (QoS 1, at-least-once).
"""

from __future__ import annotations

import logging

import paho.mqtt.client as mqtt
from paho.mqtt.enums import CallbackAPIVersion

from .mapping import is_allowed

logger = logging.getLogger(__name__)


class Bridge:
    """Lokaler Broker → Allowlist → Puffer → Cloud.

    Das Add-on abonniert lokal und publiziert ausschließlich in die Cloud.
    In den lokalen Broker wird nie geschrieben – ein Fehler des Add-ons
    stört Home Assistant nicht (ADR 0001).
    """

    def __init__(
        self, local_client, cloud, topic_filters: list[str], store, on_message=None
    ) -> None:
        self._local = local_client
        self.cloud = cloud
        self._filters = list(topic_filters)
        self._store = store
        self._on_message_hook = on_message
        self.messages_forwarded = 0

    def attach_cloud(self, cloud) -> None:
        """Verknüpft die Cloud nach dem Claim – gepufferte Nachrichten laufen nach."""
        self.cloud = cloud

    def start(self) -> None:
        self._local.on_message = self._on_local_message

    def stop(self) -> None:
        if self.cloud:
            self.cloud.stop()
        try:
            self._local.disconnect()
        except Exception:  # noqa: BLE001
            pass

    # ── Lokal (nur lesend) ────────────────────────────────────────────────── #
    def _on_local_message(self, client, userdata, msg) -> None:
        import time

        try:
            if not is_allowed(msg.topic, self._filters):
                return  # Allowlist: alles andere bleibt lokal
            self.messages_forwarded += 1
            ts = time.time()  # Empfangszeit; Geräte-Zeitstempel folgen mit #124/#36
            row_id = self._store.add(msg.topic, msg.payload, ts=ts)
            if self.cloud:
                self.cloud.publish_row(row_id, msg.topic, msg.payload, ts)
            if self._on_message_hook:
                self._on_message_hook(msg)
        except Exception:  # noqa: BLE001 - Broker-Sessions dürfen nie sterben
            logger.exception("Weiterleiten von %s fehlgeschlagen", msg.topic)


def make_local_client(host: str, port: int, username: str | None, password: str | None):
    """MQTT-5-Client für den lokalen Broker (nur Subscribe, niemals Publish).

    Noch nicht verbunden: Der Aufrufer setzt erst ``on_connect`` (Subscribe)
    und ruft dann ``connect`` auf – sonst geht der CONNACK-Callback verloren.
    """
    client = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id="iot-connector-bridge",
        protocol=mqtt.MQTTv5,
    )
    if username:
        client.username_pw_set(username, password)
    return client
