"""Add-on-Einstiegspunkt: verdrahtet Supervisor, Puffer, Bridge, Cloud und Ingress-UI.

Ablauf (ADR 0001, iot-connector#129):
1. Lokale Broker-Zugangsdaten von der Supervisor-API holen (``mqtt:need``).
2. Falls noch nicht verknüpft: auf Claim-Code über die Ingress-UI warten.
3. Bridge starten: lokal nur abonnieren (Allowlist), in die Cloud publizieren
   (``d/{gateway_id}/<Topic>``, QoS 1, Store-and-Forward per SQLite).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import threading

from .bridge import Bridge, make_local_client
from .cloud import CloudClient
from .config import load_options
from .store import MessageStore
from .supervisor import local_mqtt_credentials
from .webui import WebUI, write_credentials

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("iot-connector-bridge")


class Runtime:
    """Zustand des Add-ons: Optionen, Zugangsdaten, Status für die UI."""

    def __init__(self) -> None:
        self.options = load_options()
        self._creds: dict | None = None
        self.store = MessageStore(self.options.store_path, self.options.buffer_max_messages)
        self.bridge: Bridge | None = None
        self.cloud: CloudClient | None = None
        self.local_connected = False
        if os.path.exists(self.options.credentials_path):
            try:
                with open(self.options.credentials_path, encoding="utf-8") as fh:
                    self._creds = json.load(fh)
            except Exception:  # noqa: BLE001 - beschädigte Datei: neu verknüpfen
                logger.exception("Gateway-Zugangsdaten unlesbar, neu verknüpfen")
                self._creds = None

    # ── Zugangsdaten ───────────────────────────────────────────────────────── #
    def has_credentials(self) -> bool:
        return self._creds is not None

    @property
    def gateway_name(self) -> str:
        gateway = (self._creds or {}).get("gateway") or {}
        return str(gateway.get("name") or "")

    @property
    def gateway_id(self) -> str:
        gateway = (self._creds or {}).get("gateway") or {}
        return str(gateway.get("id") or "")

    def save_credentials(self, creds: dict) -> None:
        write_credentials(self.options.credentials_path, creds)
        self._creds = creds
        logger.info("Gateway verknüpft, starte Cloud-Verbindung")
        self._start_cloud()

    def unlink(self) -> None:
        """Verknüpfung aufheben (#11): Cloud stoppen, Zugangsdaten löschen – Puffer bleibt.

        Der Gegenfall zum Claim: Ein Gateway, das im IoT-Connector gelöscht wurde,
        kommt mit den alten Zugangsdaten nie wieder rein (CONNACK *Not authorized*).
        Ohne Entkoppeln war das Add-on tot („Bereits verknüpft“ ohne Ausweg).
        """
        if self.cloud:
            self.cloud.stop()
        if self.bridge:
            self.bridge.cloud = None
        self.cloud = None
        self._creds = None
        try:
            os.remove(self.options.credentials_path)
        except FileNotFoundError:
            pass
        logger.info("Verknüpfung aufgehoben – neuer Claim möglich, Puffer bleibt")
        self._write_state()

    # ── Aufbau ─────────────────────────────────────────────────────────────── #
    def start(self) -> None:
        """Verbindet den lokalen Broker (Retry-Schleife) und liefert die Bridge.

        Reihenfolge: Die Cloud startet VOR dem Supervisor-Zugriff – direkt nach
        einem Add-on-Update ist der Supervisor oft noch beschäftigt, und die
        Cloud-Verbindung darf davon nicht abhängen (#11: „nach dem Update weg“).
        """
        if self._creds:
            self._start_cloud()
        local = local_mqtt_credentials()
        client = make_local_client(
            local["host"], local["port"], local["username"], local["password"]
        )
        # on_connect VOR connect: Subscribe gehört in den CONNACK-Callback,
        # sonst verpasst der Client die erste Verbindung
        client.on_connect = self._on_local_connect
        client.on_disconnect = self._on_local_disconnect
        self.bridge = Bridge(
            client,
            cloud=None,
            topic_filters=self.options.topic_filters,
            store=self.store,
            on_message=self._write_state,
        )
        # Reihenfolge Cloud-vor-Supervisor (#11) hat die Cloud schon gestartet,
        # bevor es eine Bridge gab – ``_start_cloud`` konnte sie nicht attachen.
        # Ohne attach blieb ``bridge.cloud`` None: Nach einem Neustart wurden
        # neue Nachrichten nur gepuffert und nie gesendet (iot-connector-ha-addon#13).
        if self.cloud:
            self.bridge.attach_cloud(self.cloud)
        self.bridge.start()

        def _connect_loop() -> None:
            import time

            while True:
                try:
                    client.connect(local["host"], local["port"], 30)
                    # loop_forever: paho verbindet nach Abbrüchen selbst neu
                    client.loop_forever(retry_first_connection=True)
                    return
                except Exception as exc:  # noqa: BLE001 - Add-on bleibt ansprechbar
                    logger.error("Lokaler Broker nicht verbindbar: %s – Retry in 30 s", exc)
                    time.sleep(30)

        threading.Thread(target=_connect_loop, daemon=True).start()
        logger.info(
            "Add-on läuft (Filter: %s, Puffergrenze: %s)",
            self.options.topic_filters,
            self.options.buffer_max_messages,
        )

    def _on_local_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        self.local_connected = True
        for f in self.options.topic_filters:
            client.subscribe(f, qos=1)
        logger.info("Lokaler Broker verbunden, abonniere %s", self.options.topic_filters)
        self._write_state()

    def _on_local_disconnect(
        self, client, userdata, disconnect_flags, reason_code, properties=None
    ) -> None:
        # Ohne diesen Callback bliebe die UI nach einem Broker-Abbruch auf "verbunden"
        self.local_connected = False
        logger.warning("Lokaler Broker getrennt (%s) – paho verbindet neu", reason_code)
        self._write_state()

    def _start_cloud(self) -> None:
        """Startet die Cloud-Verbindung einmalig – idempotent (#11).

        Die Supervisor-Retry-Schleife ruft ``start()`` wiederholt auf, solange
        der Supervisor nach einem Add-on-Update noch nicht antwortet. Ohne
        Wächter entstünde dabei pro Retry ein weiterer Cloud-Client mit
        derselben Client-ID – die Verbindungen treten sich gegenseitig vom
        Broker, und die Cloud-Verbindung flackert (wahrscheinliche Ursache
        des „nach dem Update weg“).
        """
        if self.cloud is not None:
            return
        assert self._creds
        broker_url = self._creds.get("broker_url") or self.options.cloud_broker_url
        try:
            self.cloud = CloudClient(
                broker_url=broker_url,
                username=self._creds["username"],
                password=self._creds["password"],
                client_id=self._creds["clientid"],
                topic_prefix=self._creds["topic_prefix"],
                store=self.store,
                on_state_change=self._write_state,
            )
            if self.bridge:
                self.bridge.attach_cloud(self.cloud)
            self.cloud.start()
        except Exception:  # noqa: BLE001 - Add-on bleibt lauffähig, Puffer aktiv
            logger.exception("Cloud-Verbindung nicht startbar (broker_url %s)", broker_url)
        self._write_state()

    # ── Status für die UI ──────────────────────────────────────────────────── #
    def status(self) -> dict:
        # cloud_error: Letzter CONNACK-Fehler des Cloud-Brokers (None, wenn nie
        # verbunden wurde bzw. nach Erfolg). Die UI leitet daraus den Hinweis
        # „Gateway im IoT-Connector gelöscht oder getrennt“ ab (#11).
        return {
            "claimed": self.has_credentials(),
            "gateway_name": self.gateway_name,
            "gateway_id": self.gateway_id,
            "cloud_connected": bool(self.cloud and self.cloud.connected),
            "cloud_error": (self.cloud.last_error if self.cloud else None),
            "local_connected": self.local_connected,
            "buffered": self.store.count(),
            "dropped": self.store.dropped(),
            "last_publish": (self.cloud.last_publish if self.cloud else None),
            "messages_forwarded": (self.bridge.messages_forwarded if self.bridge else 0),
        }

    def _write_state(self, *_args) -> None:
        try:
            with open(self.options.state_path, "w", encoding="utf-8") as fh:
                json.dump(self.status(), fh)
        except Exception:  # noqa: BLE001 - Status-Datei darf nie abbrechen
            pass


async def amain() -> None:
    runtime = Runtime()
    webui = WebUI(runtime)
    await webui.start(int(os.environ.get("INGRESS_PORT", "8099")))

    # Lokalen Broker im Hintergrund verbinden – die Ingress-UI läuft trotzdem,
    # damit der Claim-Code eingegeben werden kann, solange der Broker fehlt.
    def _start() -> None:
        import time

        while not runtime.local_connected:
            try:
                runtime.start()
                return
            except Exception as exc:  # noqa: BLE001 - Add-on bleibt ansprechbar
                logger.error("Lokaler Broker nicht verbindbar: %s – Retry in 30 s", exc)
                time.sleep(30)

    threading.Thread(target=_start, daemon=True).start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    await stop.wait()
    logger.info("Stoppe Add-on")
    if runtime.bridge:
        runtime.bridge.stop()
    if runtime.cloud:
        runtime.cloud.stop()
    await webui.stop()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
