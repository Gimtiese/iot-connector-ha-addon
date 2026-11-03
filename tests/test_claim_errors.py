"""Unit-Tests: Claim-Fehler-Mapping und Code-Normalisierung (iot-connector-ha-addon#9).

Vorher übersetzte das Add-on jeden HTTPError pauschal in „Code ungültig oder
abgelaufen." – 429 (Rate-Limit), 503 (MQTT-Zugänge nicht konfiguriert) und 5xx
waren nicht unterscheidbar. Diese Tests sichern das neue Mapping ab.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from app.webui import ClaimError, claim_gateway, normalize_claim_code


class _Backend(BaseHTTPRequestHandler):
    """Test-Backend: beantwortet POST /api/v1/gateways/claim aus `server.state`."""

    def log_message(self, *args):  # noqa: ARG002 - Tests still halten
        pass

    def do_POST(self):  # noqa: N802 - http.server-API
        state = self.server.state  # type: ignore[attr-defined]
        body = json.dumps(state["body"]).encode()
        self.send_response(state["status"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        state["seen_code"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))[
            "code"
        ]


@pytest.fixture()
def backend():
    server = HTTPServer(("127.0.0.1", 0), _Backend)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _url(server) -> str:
    return f"http://127.0.0.1:{server.server_port}"


def _fail(server, status: int, body: dict) -> str:
    server.state = {"status": status, "body": body}  # type: ignore[attr-defined]
    with pytest.raises(ClaimError) as exc:
        claim_gateway(_url(server), "ABCD2345")
    return str(exc.value)


class TestNormalisieren:
    def test_leerzeichen_bindestrich_gross(self) -> None:
        assert normalize_claim_code(" ab-cd 23 45 ") == "ABCD2345"

    def test_kleinbuchstaben(self) -> None:
        assert normalize_claim_code("abcd2345") == "ABCD2345"

    def test_unveraendert_wenn_kein_code(self) -> None:
        assert normalize_claim_code("kurz") == "KURZ"


class TestClaimMapping:
    def test_gesendet_wird_normalisiert(self, backend) -> None:
        backend.state = {"status": 200, "body": {"gateway": {}}}  # type: ignore[attr-defined]
        claim_gateway(_url(backend), " ab-cd 23 45 ")
        assert backend.state["seen_code"] == "ABCD2345"  # type: ignore[attr-defined]

    def test_400_ungueltig(self, backend) -> None:
        msg = _fail(backend, 400, {"detail": "Code ungültig oder abgelaufen."})
        assert "ungültig oder abgelaufen" in msg
        assert "15 Minuten" in msg

    def test_429_rate_limit(self, backend) -> None:
        msg = _fail(backend, 429, {"detail": "Too many requests"})
        assert "Zu viele Versuche" in msg

    def test_503_nicht_konfiguriert(self, backend) -> None:
        msg = _fail(backend, 503, {"detail": "MQTT-Zugänge nicht konfiguriert"})
        assert "Konfigurationsfehler" in msg and "503" in msg

    def test_502_broker(self, backend) -> None:
        msg = _fail(backend, 502, {"detail": "x"})
        assert "Cloud-Broker" in msg

    def test_500_serverfehler_mit_detail(self, backend) -> None:
        msg = _fail(backend, 500, {"detail": "Internal DB error"})
        assert "Serverfehler (500)" in msg and "Internal DB error" in msg

    def test_ohne_detail_bleibt_lesbar(self, backend) -> None:
        assert "500" in _fail(backend, 500, {})