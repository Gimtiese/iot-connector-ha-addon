"""Ingress-UI: Claim-Code eingeben (#122), Status, letzte Übertragung, Puffergröße."""

from __future__ import annotations

import json
import logging
import os
import stat
import urllib.error
import urllib.request

from aiohttp import web

logger = logging.getLogger(__name__)

_PAGE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>IoT Connector Bridge</title>
<style>
 body{font-family:system-ui,sans-serif;margin:2rem;color:#222}
 .card{border:1px solid #ddd;border-radius:8px;padding:1rem;margin-bottom:1rem;max-width:34rem}
 input{width:100%;box-sizing:border-box;padding:.5rem;margin-top:.5rem}
 button{padding:.5rem 1.5rem;margin-top:.75rem}
 .ok{color:#0a7d32}.err{color:#b00}.muted{color:#666}
</style></head><body>
<h1>IoT Connector Bridge</h1>
<div class="card" id="claimbox">
 <h2>Gateway verknüpfen</h2>
 <p>Claim-Code aus dem IoT-Connector eingeben. Die Zugangsdaten werden einmalig
 geholt und im Add-on-Datenverzeichnis gespeichert.</p>
 <form onsubmit="return claim(event)">
  <input id="code" placeholder="Claim-Code" autocomplete="off" required>
  <button>Verknüpfen</button>
 </form>
 <p id="claimmsg" class="muted"></p>
</div>
<div class="card">
 <h2>Status</h2>
 <table id="status"><tbody></tbody></table>
 <p class="muted">Aktualisiert alle 5 s.</p>
</div>
<script>
async function refresh(){
 const s = await (await fetch('api/status')).json();
 const rows = [['Cloud', s.cloud_connected?'verbunden':'getrennt (Puffer aktiv)'],
  ['Lokaler Broker', s.local_connected?'verbunden':'getrennt'],
  ['Puffer', s.buffered+' Nachrichten'],
  ['Verworfen (Puffergrenze)', s.dropped],
  ['Letzte Übertragung', s.last_publish?new Date(s.last_publish*1000).toLocaleString():'–']];
 document.querySelector('#status tbody').innerHTML =
  rows.map(r=>'<tr><td class="muted">'+r[0]+'</td><td>'+r[1]+'</td></tr>').join('');
}
async function claim(e){
 e.preventDefault();
 const code = document.querySelector('#code').value;
 const msg = document.querySelector('#claimmsg');
 msg.className='muted'; msg.textContent='Sende …';
 const r = await fetch('api/claim',{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({code})});
 const d = await r.json();
 if(r.ok){msg.className='ok';
  msg.textContent='Verknüpft mit Gateway '+(d.gateway_name||'')+
  '. Das Add-on verbindet sich jetzt.';}
 else{msg.className='err'; msg.textContent=d.error||'Fehlgeschlagen';}
 refresh(); return false;
}
refresh(); setInterval(refresh, 5000);
</script></body></html>"""


class WebUI:
    """Kleiner HTTP-Server (Ingress, Port 8099) für Claim und Status."""

    def __init__(self, runtime) -> None:
        self._runtime = runtime
        self._runner: web.AppRunner | None = None

    async def start(self, port: int = 8099) -> None:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/api/status", self._status)
        app.router.add_post("/api/claim", self._claim)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, "0.0.0.0", port).start()

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # ── Handler ────────────────────────────────────────────────────────────── #
    async def _index(self, request: web.Request) -> web.Response:
        return web.Response(text=_PAGE, content_type="text/html")

    async def _status(self, request: web.Request) -> web.Response:
        return web.json_response(self._runtime.status())

    async def _claim(self, request: web.Request) -> web.Response:
        try:
            body = await request.json()
            code = str(body.get("code") or "").strip()
        except Exception:
            raise web.HTTPBadRequest(text='{"error": "Kein Code"}') from None
        if not code:
            return web.json_response({"error": "Kein Code angegeben"}, status=400)
        if self._runtime.has_credentials():
            return web.json_response(
                {"error": "Bereits verknüpft – zuerst im IoT-Connector trennen"}, status=409
            )
        try:
            creds = claim_gateway(self._runtime.options.backend_url, code)
        except ClaimError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        self._runtime.save_credentials(creds)
        return web.json_response({"gateway_name": creds.get("gateway", {}).get("name", "")})


class ClaimError(RuntimeError):
    """Claim fehlgeschlagen – Code ungültig, abgelaufen oder Backend nicht erreichbar."""


def claim_gateway(backend_url: str, code: str) -> dict:
    """Löst den Claim-Code beim Backend ein (iot-connector#122: ohne Login, einmalig)."""
    url = backend_url.rstrip("/") + "/api/v1/gateways/claim"
    req = urllib.request.Request(
        url,
        data=json.dumps({"code": code}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise ClaimError("Code ungültig oder abgelaufen.") from exc
    except Exception as exc:  # noqa: BLE001
        raise ClaimError(f"Backend nicht erreichbar: {exc}") from exc


def write_credentials(path: str, creds: dict) -> None:
    """Zugangsdaten mit 0600 ins Add-on-Datenverzeichnis."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(creds, fh, indent=2)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)
