"""Integrationstest gegen lokale Mosquitto-Container in Docker (iot-connector-ha-addon#1).

Voraussetzung: Docker im Laufzeitumfeld (CI: ubuntu-latest). Ohne Docker wird der
Test übersprungen – Unit-Abdeckung übernimmt tests/test_mapping.py und test_store.py.

Ende-zu-Ende gegen die echte Cloud folgt, sobald iot-connector#399 behoben ist.
"""

from __future__ import annotations

import os
import socket
import subprocess
import time

import paho.mqtt.client as mqtt
import pytest
from app.bridge import Bridge, make_local_client
from app.cloud import CloudClient
from app.store import MessageStore
from paho.mqtt.enums import CallbackAPIVersion

IMAGE = "eclipse-mosquitto:2.0.22"
PREFIX = "d/gw-test/"
MOSQUITTO_CONF = "\n".join(
    [
        "listener 1883",
        "allow_anonymous true",
        "listener 9001",
        "protocol websockets",
        "allow_anonymous true",
    ]
) + "\n"


def _docker_available() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=30, check=True)
        return True
    except Exception:  # noqa: BLE001
        return False


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _start_mosquitto(name: str, conf_port: tuple[int, int]) -> tuple[int, int]:
    tcp_port, ws_port = conf_port
    conf_dir = f"/tmp/mosq-{name}"
    os.makedirs(conf_dir, exist_ok=True)
    with open(f"{conf_dir}/mosquitto.conf", "w", encoding="utf-8") as fh:
        fh.write(MOSQUITTO_CONF)
    subprocess.run(
        [
            "docker", "run", "-d", "--rm", "--name", name,
            "-p", f"{tcp_port}:1883",
            "-p", f"{ws_port}:9001",
            "-v", f"{conf_dir}/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro",
            IMAGE,
        ],
        check=True,
        timeout=120,
        capture_output=True,
    )
    return tcp_port, ws_port


def _wait_mosquitto(port: int, transport: str = "tcp", timeout_s: float = 60.0) -> None:
    """Wartet auf einen echten CONNACK – TCP-Connect genügt nicht.

    docker-proxy nimmt Verbindungen an, bevor Mosquitto lauscht; erst ein
    MQTT-CONNECT/CONNACK beweist, dass der Broker Datenverarbeitung kann.
    """
    import paho.mqtt.client as mqtt_lib

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        probe = mqtt_lib.Client(
            CallbackAPIVersion.VERSION2, protocol=mqtt_lib.MQTTv311, transport=transport
        )
        try:
            probe.connect("127.0.0.1", port, 30)
            probe.disconnect()
            return
        except Exception:  # noqa: BLE001 - Broker noch nicht bereit
            time.sleep(0.5)
    raise TimeoutError(f"Mosquitto auf Port {port} nicht bereit (kein CONNACK)")


def _wait_for(predicate, timeout_s: float = 20.0, interval: float = 0.2) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _cloud_subscriber(ws_port: int, received: list) -> mqtt.Client:
    """Abonniert die Cloud (als wäre es der Ingest) und sammelt Nachrichten."""
    client = mqtt.Client(
        CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5, transport="websockets"
    )

    def on_message(c, u, m):
        received.append((m.topic, bytes(m.payload)))

    client.on_message = on_message
    client.connect("127.0.0.1", ws_port, 30)
    client.subscribe("d/gw-test/#", qos=1)
    client.loop_start()
    return client


@pytest.fixture(scope="module")
def brokers():
    """Zwei Mosquittos: 'lokal' (HAOS-Broker, nur TCP) und 'Cloud' (WebSocket)."""
    if not _docker_available():
        pytest.skip("Docker nicht verfügbar – Integrationstest übersprungen")
    local_tcp = _free_port()
    cloud_tcp2 = _free_port()
    cloud_ws = _free_port()
    _start_mosquitto("addon-test-local", (local_tcp, _free_port()))
    _start_mosquitto("addon-test-cloud", (cloud_tcp2, cloud_ws))
    try:
        _wait_mosquitto(local_tcp)
        _wait_mosquitto(cloud_ws, transport="websockets")
        yield {"local_tcp": local_tcp, "cloud_ws": cloud_ws}
    finally:
        for name in ("addon-test-local", "addon-test-cloud"):
            subprocess.run(
                ["docker", "rm", "-f", name], capture_output=True, timeout=60, check=False
            )


_DBG: list = []


def _publisher(port: int) -> mqtt.Client:
    pub = mqtt.Client(CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5)
    pub.connect("127.0.0.1", port, 30)
    pub.loop_start()
    _DBG.append(("pub", pub.is_connected()))
    return pub


def _local_bridge(local_tcp: int, cloud, store, filters) -> mqtt.Client:
    local = make_local_client("127.0.0.1", local_tcp, None, None)
    bridge = Bridge(local, cloud, filters, store)
    bridge.start()  # setzt on_message
    _DBG.append(("bridge-made", local_tcp))
    # on_connect VOR connect: sonst verpasst der Client den CONNACK (kein Subscribe)
    local.on_connect = lambda c, u, f, r, p=None: [
        c.subscribe(f, qos=1) for f in filters
    ]
    local.connect("127.0.0.1", local_tcp, 30)
    local.loop_start()
    return local


class TestEndeZuEnde:
    def test_lokale_nachricht_spiegelt_in_die_cloud(self, brokers, tmp_path):
        """Lokal publiziert → Cloud-Ingest empfängt ``d/{gateway_id}/<lokales Topic>``."""
        received: list = []
        cloud_sub = _cloud_subscriber(brokers["cloud_ws"], received)
        store = MessageStore(str(tmp_path / "buffer.sqlite"), 10000)
        cloud = CloudClient(
            f"ws://127.0.0.1:{brokers['cloud_ws']}/", "gw-test", "pw", "gw-test", PREFIX, store
        )
        cloud.start()
        local = _local_bridge(brokers["local_tcp"], cloud, store, ["zigbee2mqtt/#"])

        pub = _publisher(brokers["local_tcp"])
        pub.publish("zigbee2mqtt/sensor1", b"24.5", qos=1)

        def dbg():
            print(
                f"DBG: local={local.is_connected()} pub={pub.is_connected()} "
                f"cloud={cloud.connected} store={store.count()} dbg={_DBG}",
                flush=True,
            )
        dbg()
        import time as _t
        _t.sleep(2)
        dbg()
        assert _wait_for(
            lambda: any(
                t == "d/gw-test/zigbee2mqtt/sensor1" and p == b"24.5" for t, p in received
            )
        ), f"Nichts in der Cloud angekommen: {received}"

        pub.loop_stop()
        pub.disconnect()
        local.loop_stop()
        local.disconnect()
        cloud.stop()
        cloud_sub.loop_stop()
        cloud_sub.disconnect()
        store.close()

    def test_nicht_allowlisted_topic_bleibt_lokal(self, brokers, tmp_path):
        """homeassistant/# steht nicht in der Allowlist → nichts in der Cloud."""
        received: list = []
        cloud_sub = _cloud_subscriber(brokers["cloud_ws"], received)
        store = MessageStore(str(tmp_path / "b.sqlite"), 10000)
        cloud = CloudClient(
            f"ws://127.0.0.1:{brokers['cloud_ws']}/", "gw-test", "pw", "gw-t2", PREFIX, store
        )
        cloud.start()
        local = _local_bridge(brokers["local_tcp"], cloud, store, ["zigbee2mqtt/#"])

        pub = _publisher(brokers["local_tcp"])
        pub.publish("homeassistant/sensor/x", b"secret", qos=1)

        time.sleep(3)
        assert received == [], f"Nicht erlaubtes Topic wurde gespiegelt: {received}"

        pub.loop_stop()
        pub.disconnect()
        local.loop_stop()
        local.disconnect()
        cloud.stop()
        cloud_sub.loop_stop()
        cloud_sub.disconnect()
        store.close()

    def test_store_and_forward_nach_ausfall(self, brokers, tmp_path):
        """Cloud-Verbindung erst nach dem Ausfall hergestellt → Nachlieferung aus Puffer."""
        store = MessageStore(str(tmp_path / "b.sqlite"), 10000)
        local = _local_bridge(
            brokers["local_tcp"], cloud=None, store=store, filters=["zigbee2mqtt/#"]
        )
        time.sleep(1)

        pub = _publisher(brokers["local_tcp"])
        pub.publish("zigbee2mqtt/sensor2", b"11.5", qos=1)
        import time as _t
        _t.sleep(2)
        print(f"DBG t3: local={local.is_connected()} pub={_DBG} store={store.count()}", flush=True)
        assert _wait_for(lambda: store.count() == 1, timeout_s=15), (
            "Nachricht muss ohne Cloud im Puffer liegen"
        )

        received: list = []
        cloud_sub = _cloud_subscriber(brokers["cloud_ws"], received)
        cloud = CloudClient(
            f"ws://127.0.0.1:{brokers['cloud_ws']}/", "gw-test", "pw", "gw-test", PREFIX, store
        )
        cloud.start()
        assert _wait_for(
            lambda: any(
                t == "d/gw-test/zigbee2mqtt/sensor2" and p == b"11.5" for t, p in received
            ),
            timeout_s=30,
        ), f"Puffer nicht nachgeliefert: {received}"
        assert _wait_for(lambda: store.count() == 0, timeout_s=10), "Puffer nicht geleert"

        pub.loop_stop()
        pub.disconnect()
        local.loop_stop()
        local.disconnect()
        cloud.stop()
        cloud_sub.loop_stop()
        cloud_sub.disconnect()
        store.close()
