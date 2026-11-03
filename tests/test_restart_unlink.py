"""Tests: Neustart mit vorhandener gateway.json, Entkoppeln, WebUI-Handler (#11).

Nach dem Update auf 1.1.2 war die Cloud-Verbindung weg und die UI zeigte
(scheinbar) wieder das Claim-Formular – ohne Ausweg, denn ein neuer Claim
wurde mit „Bereits verknüpft“ abgelehnt. Diese Tests sichern ab:

- Ein Neustart mit vorhandener ``gateway.json`` startet die Cloud ohne neuen
  Claim (und zwar VOR dem Supervisor-Zugriff).
- ``unlink()`` / ``POST /api/unlink`` entfernen die Zugangsdaten und lassen
  den Puffer unangetastet – danach ist ein neuer Claim möglich.
- Der Claim-409 nennt den Weg („Verknüpfung aufheben“), statt ihn zu verbauen.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "iot-connector"))

from app import main as main_mod  # noqa: E402
from app.store import MessageStore  # noqa: E402
from app.webui import WebUI, write_credentials  # noqa: E402

CREDS = {
    "gateway": {"id": "gw-1", "name": "Hof"}
    ,
    "username": "gw-hof",
    "password": "geheim",
    "clientid": "gw-hof-client",
    "topic_prefix": "1/",
    "broker_url": "wss://example.test",
}


class FakeCloud:
    """Ersatz für CloudClient: start/stop und Fehler-Zustand für die Tests."""

    lastconstructed: list[dict] = []

    def __init__(self, *, broker_url, username, password, client_id, topic_prefix,
                 store, on_state_change=None, max_send_rate=None) -> None:
        self.args = {
            "broker_url": broker_url, "username": username, "client_id": client_id,
            "topic_prefix": topic_prefix,
        }
        self.store = store
        self.started = False
        self.stopped = False
        self.connected = False
        self.last_error = None
        self.live_deferred = 0
        self.puback_errors = 0
        FakeCloud.lastconstructed.append(self.args)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    @property
    def last_publish(self):
        return None


class FakeLocal:
    """Ersatz für den lokalen paho-Client: Connect landet in einer Endlosschleife."""

    def __init__(self) -> None:
        self.on_connect = None
        self.on_disconnect = None
        self.on_message = None

    def connect(self, *args, **kwargs) -> None:  # noqa: ARG002
        pass

    def loop_forever(self, **kwargs) -> None:  # noqa: ARG002
        import time

        time.sleep(300)


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ADDON_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(main_mod, "CloudClient", FakeCloud)
    FakeCloud.lastconstructed = []
    yield tmp_path


def _runtime() -> main_mod.Runtime:
    rt = main_mod.Runtime()
    rt.store.close()
    rt.store = MessageStore(
        os.path.join(rt.options.data_dir, "buffer.sqlite"), rt.options.buffer_max_messages
    )
    return rt


def _start_with_local(rt: main_mod.Runtime, monkeypatch) -> FakeLocal:
    """Runtime.start() mit gefaktem Supervisor und lokalem Client."""
    monkeypatch.setattr(main_mod, "local_mqtt_credentials", lambda: {
        "host": "127.0.0.1", "port": 1883, "username": None, "password": None,
    })
    fake = FakeLocal()
    monkeypatch.setattr(main_mod, "make_local_client", lambda *a, **k: fake)
    rt.start()
    return fake


class TestNeustartMitCredentials:
    def test_gateway_json_ueberlebt_prozessneustart(self, data_dir) -> None:
        """Neustart = neuer Runtime-Prozess: Zugangsdaten werden gelesen."""
        write_credentials(os.path.join(data_dir, "gateway.json"), CREDS)
        rt = _runtime()
        assert rt.has_credentials()
        assert rt.gateway_name == "Hof"
        assert rt.gateway_id == "gw-1"
        rt.store.close()

    def test_start_ohne_neuen_claim_startet_cloud(self, data_dir, monkeypatch) -> None:
        """Kern des Reports (#11): Neustart mit vorhandener gateway.json verbindet."""
        write_credentials(os.path.join(data_dir, "gateway.json"), CREDS)
        rt = _runtime()
        _start_with_local(rt, monkeypatch)
        assert FakeCloud.lastconstructed, "Cloud muss ohne neuen Claim gestartet werden"
        assert FakeCloud.lastconstructed[0]["client_id"] == "gw-hof-client"
        assert FakeCloud.lastconstructed[0]["username"] == "gw-hof"
        assert rt.cloud.started
        rt.cloud.stop()
        rt.store.close()

    def test_beschadigte_gateway_json_gilt_als_unverknuepft(self, data_dir) -> None:
        (data_dir / "gateway.json").write_text("{kein json", encoding="utf-8")
        rt = _runtime()
        assert not rt.has_credentials()
        rt.store.close()


class TestUnlink:
    def test_unlink_loescht_credentials_puffer_bleibt(self, data_dir, monkeypatch) -> None:
        write_credentials(os.path.join(data_dir, "gateway.json"), CREDS)
        rt = _runtime()
        _start_with_local(rt, monkeypatch)
        rt.store.add("zigbee2mqtt/s0", b"x", ts=1.0)
        rt.unlink()
        assert not rt.has_credentials()
        assert not os.path.exists(os.path.join(data_dir, "gateway.json"))
        assert rt.cloud is None
        assert rt.bridge.cloud is None
        # Puffer (SQLite) bleibt erhalten – kein Datenverlust beim Entkoppeln
        assert rt.store.count() == 1
        assert rt.status()["claimed"] is False
        rt.store.close()

    def test_neuer_claim_nach_unlink_moeglich(self, data_dir, monkeypatch) -> None:
        write_credentials(os.path.join(data_dir, "gateway.json"), CREDS)
        rt = _runtime()
        _start_with_local(rt, monkeypatch)
        rt.unlink()
        rt.save_credentials(CREDS)  # neuer Claim
        assert rt.has_credentials()
        assert len(FakeCloud.lastconstructed) == 2
        rt.cloud.stop()
        rt.store.close()


class TestCloudFehlerZustand:
    def test_connack_auth_fehler_landet_im_status(self, data_dir) -> None:
        from app.cloud import CloudClient

        class Reason:
            value = 135  # Not authorized

            def __str__(self) -> str:
                return "Not authorized"

        store = MessageStore(str(data_dir / "b.sqlite"), 100)
        cloud = CloudClient(
            broker_url="wss://example.test", username="u", password="p",
            client_id="c", topic_prefix="p/", store=store,
        )
        cloud._on_connect(None, None, None, Reason(), None)
        assert cloud.connected is False
        assert cloud.last_error == "Not authorized"
        # Erfolg räumt den Fehlerzustand weg
        class Ok:
            value = 0

        cloud._on_connect(None, None, None, Ok(), None)
        assert cloud.connected is True
        assert cloud.last_error is None
        store.close()

    def test_fehler_wird_nur_einmal_gelogggt(self, data_dir, caplog) -> None:
        from app.cloud import CloudClient

        class Reason:
            value = 4  # Bad username or password

            def __str__(self) -> str:
                return "Bad username or password"

        store = MessageStore(str(data_dir / "b.sqlite"), 100)
        cloud = CloudClient(
            broker_url="wss://example.test", username="u", password="p",
            client_id="c", topic_prefix="p/", store=store,
        )
        with caplog.at_level("ERROR"):
            for _ in range(3):  # paho wiederholt: jeder Retry ruft on_connect
                cloud._on_connect(None, None, None, Reason(), None)
        assert len([r for r in caplog.records if "CONNACK" in r.message]) == 1
        store.close()


class TestWebUIHandler:
    def _call(self, handler, request) -> object:
        return asyncio.run(handler(request))

    def test_unlink_endpoint(self, data_dir) -> None:
        write_credentials(os.path.join(data_dir, "gateway.json"), CREDS)
        rt = _runtime()
        ui = WebUI(rt)
        resp = self._call(ui._unlink, types.SimpleNamespace())
        assert resp.status == 200
        assert not rt.has_credentials()
        rt.store.close()

    def test_unlink_ohne_verknuepfung_ist_409(self, data_dir) -> None:
        rt = _runtime()
        ui = WebUI(rt)
        resp = self._call(ui._unlink, types.SimpleNamespace())
        assert resp.status == 409
        rt.store.close()

    def test_claim_409_verweist_auf_entkoppeln(self, data_dir) -> None:
        write_credentials(os.path.join(data_dir, "gateway.json"), CREDS)
        rt = _runtime()
        ui = WebUI(rt)

        class Req:
            async def json(self):
                return {"code": "ABCD2345"}

        resp = self._call(ui._claim, Req())
        assert resp.status == 409
        assert "Verknüpfung aufheben" in json.loads(resp.text)["error"]
        rt.store.close()

    def test_status_enthaelt_gateway_und_fehler(self, data_dir) -> None:
        write_credentials(os.path.join(data_dir, "gateway.json"), CREDS)
        rt = _runtime()
        status = rt.status()
        assert status["claimed"] is True
        assert status["gateway_name"] == "Hof"
        assert status["gateway_id"] == "gw-1"
        assert "cloud_error" in status
        rt.store.close()
