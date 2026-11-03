"""Zugangsdaten zum lokalen Mosquitto über die Supervisor-API (`services: mqtt:need`)."""

from __future__ import annotations

import json
import os
import urllib.request

SUPERVISOR_URL = os.environ.get("SUPERVISOR_URL", "http://supervisor")
SUPERVISOR_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")


class SupervisorError(RuntimeError):
    """Supervisor nicht erreichbar oder MQTT nicht angebunden."""


def _get(path: str) -> dict:
    req = urllib.request.Request(
        SUPERVISOR_URL + path,
        headers={"Authorization": f"Bearer {SUPERVISOR_TOKEN}"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def local_mqtt_credentials() -> dict:
    """Host/Port/User/Passwort des lokalen Brokers aus /services/mqtt.

    Keine manuelle Eingabe: Mit `services: ["mqtt:need"]` garantiert der
    Supervisor die Verbindung, das Add-on fragt sie hier ab.

    Returns:
        dict mit ``host``, ``port``, ``username``, ``password``.
    """
    try:
        data = _get("/services/mqtt")
    except Exception as exc:  # noqa: BLE001 - dem Nutzer reicht die Meldung
        raise SupervisorError(f"Supervisor /services/mqtt nicht erreichbar: {exc}") from exc
    if data.get("result") != "ok":
        raise SupervisorError(f"Supervisor antwortete nicht mit ok: {data.get('message')}")
    svc = data.get("data") or {}
    if not svc.get("host"):
        raise SupervisorError("Kein MQTT-Dienst angebunden (mqtt:need)")
    return {
        "host": svc["host"],
        "port": int(svc.get("port", 1883)),
        "username": svc.get("username"),
        "password": svc.get("password"),
    }
