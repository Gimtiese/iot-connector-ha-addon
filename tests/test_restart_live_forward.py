"""Tests: Nach einem Add-on-Neustart werden Live-Nachrichten wieder gesendet (#13).

Report (Add-on 1.1.3): Neustart → HA „Cloud verbunden“, das Gateway steht im
IoT-Connector aber offline. Ursache: ``Runtime.start()`` startet die Cloud VOR
der Bridge (Reihenfolge-Fix aus #11), ``_start_cloud()`` attacht sie aber nur,
wenn es schon eine Bridge gibt – beim Neustart gibt es sie noch nicht. Die
Bridge läuft dann dauerhaft mit ``cloud=None``: Neue Nachrichten werden nur
gepuffert, nie gesendet. Der Drain liefert nur den Altbestand nach.
"""

from __future__ import annotations

import os
import sys
import threading

import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "iot-connector"))

from app import main as main_mod  # noqa: E402
from app.store import MessageStore  # noqa: E402
from app.webui import write_credentials  # noqa: E402

CREDS = {
    "gateway": {"id": "2er7cah9x1", "name": "Test"},
    "username": "gw-hof",
    "password": "geheim",
    "clientid": "gw-hof-client",
    "topic_prefix": "1/",
    "broker_url": "wss://example.test",
}


class FakeCloud:
    """Ersatz für CloudClient: zeichnet ``publish_row`` auf (#13)."""

    def __init__(self, *, broker_url, username, password, client_id,
                 topic_prefix, store, on_state_change=None, max_send_rate=None) -> None:
        self.published: list[tuple[int, str, bytes]] = []
        self.connected = False
        self.last_error = None
        self.last_publish = None
        self.live_deferred = 0
        self.puback_errors = 0
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        pass

    def publish_row(self, row_id, topic, payload, ts) -> bool:
        self.published.append((row_id, topic, bytes(payload)))
        self.connected = True
        self.last_publish = ts
        return True


class FakeLocal:
    """Ersatz für den lokalen paho-Client: Connect blockiert wie real."""

    def __init__(self) -> None:
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None

    def connect(self, *a, **k) -> None:  # noqa: ARG002
        pass

    def loop_forever(self, **k) -> None:  # noqa: ARG002
        threading.Event().wait(300)


def _start(monkeypatch, tmp_path):
    """Runtime mit vorhandener gateway.json starten – wie nach Neustart."""
    monkeypatch.setenv("ADDON_DATA_DIR", str(tmp_path))
    write_credentials(os.path.join(tmp_path, "gateway.json"), CREDS)
    monkeypatch.setattr(main_mod, "CloudClient", FakeCloud)
    monkeypatch.setattr(main_mod, "local_mqtt_credentials", lambda: {
        "host": "127.0.0.1", "port": 1883, "username": None, "password": None,
    })
    fake = FakeLocal()
    monkeypatch.setattr(main_mod, "make_local_client", lambda *a, **k: fake)
    rt = main_mod.Runtime()
    rt.store.close()
    rt.store = MessageStore(
        os.path.join(rt.options.data_dir, "buffer.sqlite"), rt.options.buffer_max_messages
    )
    rt.start()
    return rt, fake


def _msg(topic: str, payload: bytes) -> mqtt.MQTTMessage:
    m = mqtt.MQTTMessage(mid=1, topic=topic.encode())
    m.payload = payload
    return m


class TestNeustartLiveWeiterleitung:
    def test_bridge_hat_cloud_nach_neustart(self, monkeypatch, tmp_path) -> None:
        """Kern des Reports (#13): Nach dem Neustart ist die Cloud angehängt."""
        rt, _ = _start(monkeypatch, tmp_path)
        try:
            assert rt.cloud is not None, "Cloud muss ohne Claim gestartet werden (#11)"
            assert rt.bridge is not None
            assert rt.bridge.cloud is rt.cloud, (
                "Bridge muss die bereits laufende Cloud nutzen – sonst werden "
                "Live-Nachrichten nur gepuffert (Gateway offline)"
            )
        finally:
            rt.cloud.stop()
            rt.store.close()

    def test_live_nachricht_wird_gesendet_nicht_nur_gepuffert(
        self, monkeypatch, tmp_path
    ) -> None:
        """Neustart → Live-Nachricht → Publish an die Cloud (Akzeptanzkriterium)."""
        rt, fake = _start(monkeypatch, tmp_path)
        try:
            rt.bridge._on_local_message(fake, None, _msg("zigbee2mqtt/s0", b"on"))
            assert rt.cloud.published == [
                (rt.store.next_batch(1)[0][0], "zigbee2mqtt/s0", b"on")
            ], "Live-Nachricht muss nach dem Neustart an die Cloud gehen"
            assert rt.store.count() == 1, "at-least-once: Zeile bleibt bis PUBACK"
        finally:
            rt.cloud.stop()
            rt.store.close()

    def test_gepufferte_altlast_bleibt_bis_puback(self, monkeypatch, tmp_path) -> None:
        """Altbestand bleibt at-least-once im Puffer – nicht hinter Live-Nachrichten."""
        rt, fake = _start(monkeypatch, tmp_path)
        try:
            rt.store.add("homeassistant/x", b"old", ts=1000.0)
            rt.bridge._on_local_message(fake, None, _msg("zigbee2mqtt/s0", b"new"))
            # Live-Nachricht raus, Altlast bleibt bis der Drain sie bestätigt
            assert [r[1] for r in rt.cloud.published] == ["zigbee2mqtt/s0"]
            assert rt.store.count() == 2
        finally:
            rt.cloud.stop()
            rt.store.close()


class TestAttachCloudOhneNeustart:
    def test_claim_nach_ohne_bridge_startet_und_attacht(self, monkeypatch, tmp_path) -> None:
        """Claim vor der lokalen Verbindung: ``save_credentials`` attacht später."""
        monkeypatch.setenv("ADDON_DATA_DIR", str(tmp_path))
        rt = main_mod.Runtime()
        rt.store.close()
        rt.store = MessageStore(os.path.join(tmp_path, "b.sqlite"), 100)
        monkeypatch.setattr(main_mod, "CloudClient", FakeCloud)
        rt._creds = dict(CREDS)
        rt._start_cloud()  # Cloud vor der Bridge (Supervisor noch nicht da)
        monkeypatch.setattr(main_mod, "local_mqtt_credentials", lambda: {
            "host": "127.0.0.1", "port": 1883, "username": None, "password": None,
        })
        fake = FakeLocal()
        monkeypatch.setattr(main_mod, "make_local_client", lambda *a, **k: fake)
        rt.start()
        try:
            assert rt.bridge.cloud is rt.cloud
        finally:
            rt.cloud.stop()
            rt.store.close()
