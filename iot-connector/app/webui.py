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

_ADDON_VERSION = "1.1.3"

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
 .warn{border-color:#b00}
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
<div class="card" id="gwbox">
 <h2>Verknüpftes Gateway</h2>
 <p><b id="gwname"></b> <span class="muted" id="gwid"></span></p>
 <p id="cloudmsg" class="muted">Cloud: …</p>
 <button id="unlink" onclick="return unlink(event)">Verknüpfung aufheben</button>
 <p class="muted">Stoppt die Cloud-Verbindung und entfernt die Zugangsdaten.
 Der Puffer bleibt erhalten – danach ist ein neuer Claim möglich.</p>
</div>
<div class="card">
 <h2>Status</h2>
 <table id="status"><tbody></tbody></table>
 <p class="muted">Aktualisiert alle 5 s.</p>
</div>
<script>
const AUTH_HINT = 'Gateway im IoT-Connector gelöscht oder getrennt – ' +
 'hier „Verknüpfung aufheben“ und danach neu verknüpfen.';
async function refresh(){
 const s = await (await fetch('api/status')).json();
 // Claim-Formular nur, solange nicht verknüpft (#11) – sonst Gateway-Karte
 document.getElementById('claimbox').style.display = s.claimed ? 'none' : '';
 document.getElementById('gwbox').style.display = s.claimed ? '' : 'none';
 document.getElementById('gwname').textContent = s.gateway_name || '(unbenannt)';
 document.getElementById('gwid').textContent = s.gateway_id ? 'ID '+s.gateway_id : '';
 const cm = document.getElementById('cloudmsg');
 if(s.cloud_connected){cm.className='ok'; cm.textContent='Cloud verbunden.';}
 else if(s.claimed && s.cloud_error){cm.className='err';
  cm.textContent='Cloud getrennt ('+s.cloud_error+'). '+AUTH_HINT;}
 else if(s.claimed){cm.className='muted';
  cm.textContent='Cloud getrennt – neue Verbindung wird aufgebaut (Puffer aktiv).';}
 else{cm.className='muted'; cm.textContent='Nicht verknüpft.';}
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
async function unlink(e){
 e.preventDefault();
 if(!confirm('Verknüpfung aufheben? Die Cloud-Verbindung wird gestoppt und ' +
  'die Zugangsdaten gelöscht – der Puffer bleibt. Danach neu verknüpfen.')) return false;
 const r = await fetch('api/unlink',{method:'POST'});
 const msg = document.querySelector('#claimmsg');
 if(r.ok){msg.className='ok';
  msg.textContent='Verknüpfung aufgehoben – jetzt neuen Claim-Code eingeben.';}
 else{msg.className='err';
  const d = await r.json().catch(()=>({})); msg.textContent=d.error||'Fehlgeschlagen';}
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
        app.router.add_post("/api/unlink", self._unlink)
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
                {
                    "error": (
                        "Bereits verknüpft – zuerst „Verknüpfung aufheben“, "
                        "dann den neuen Code eingeben"
                    )
                },
                status=409,
            )
        try:
            creds = claim_gateway(self._runtime.options.backend_url, code)
        except ClaimError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        self._runtime.save_credentials(creds)
        return web.json_response({"gateway_name": creds.get("gateway", {}).get("name", "")})

    async def _unlink(self, request: web.Request) -> web.Response:
        """Verknüpfung aufheben (#11): Cloud stoppen, gateway.json löschen, Puffer bleibt."""
        if not self._runtime.has_credentials():
            return web.json_response({"error": "Nicht verknüpft"}, status=409)
        self._runtime.unlink()
        return web.json_response({"ok": True})


class ClaimError(RuntimeError):
    """Claim fehlgeschlagen – Code ungültig, abgelaufen, Backend-Limit oder Backend-Fehler."""


def normalize_claim_code(raw: str) -> str:
    """Bringt die Eingabe in die kanonische Form (groß, ohne Leerzeichen/Bindestriche).

    Tolerant wie das Backend (``normalize_code``): ``abcd 1234`` und ``abcd-1234``
    werden zu ``ABCD1234``. Alles andere (zu kurz, falsches Alphabet) bleibt
    unverändert – das Backend lehnt es dann mit 400 ab.
    """
    return "".join(raw.split()).replace("-", "").upper()


def _error_detail(exc: urllib.error.HTTPError) -> str:
    """Extrahiert ``detail`` aus dem Fehler-Body, falls das Backend ihn schickt."""
    try:
        body = json.loads(exc.read().decode("utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return ""
    detail = body.get("detail") if isinstance(body, dict) else None
    return str(detail) if isinstance(detail, str) else ""


def _claim_http_error(exc: urllib.error.HTTPError) -> ClaimError:
    """Übersetzt den HTTP-Status des Backends in eine unterscheidbare Meldung (#9).

    Vorher stand hier für jeden HTTPError pauschal „Code ungültig oder
    abgelaufen." – 429 (Rate-Limit), 503 (MQTT-Zugänge nicht konfiguriert) und
    echte 5xx waren davon nicht zu unterscheiden.
    """
    status = exc.code
    detail = _error_detail(exc)
    logger.warning("Claim fehlgeschlagen: HTTP %s %s", status, detail or exc.reason)
    if status == 400:
        msg = (
            "Code ungültig oder abgelaufen. Codes gelten 15 Minuten und nur einmal – "
            "ggf. im IoT-Connector neu erzeugen."
        )
    elif status == 403:
        msg = "Vom Cloudflare-Schutz geblockt (403) – bitte dem Support melden."
    elif status == 429:
        msg = (
            "Zu viele Versuche – der Server erlaubt 5 Versuche pro Minute. "
            "Bitte ~1 Minute warten und erneut versuchen."
        )
    elif status == 503:
        msg = (
            "Server-/Konfigurationsfehler (503): MQTT-Zugänge sind im Connector "
            "nicht konfiguriert. Support informieren."
        )
    elif status == 502:
        msg = (
            "Server-/Konfigurationsfehler (502): der Cloud-Broker hat abgelehnt. "
            "Support informieren."
        )
    elif status == 422:
        msg = f"Ungültige Anfrage (422) an den Server{': ' + detail if detail else ''}."
    elif status >= 500:
        msg = f"Serverfehler ({status}){': ' + detail if detail else ''} – bitte dem Support melden."  # noqa: E501
    else:
        msg = f"Fehlgeschlagen (HTTP {status}){': ' + detail if detail else ''}."
    return ClaimError(msg)


def claim_gateway(backend_url: str, code: str) -> dict:
    """Löst den Claim-Code beim Backend ein (iot-connector#122: ohne Login, einmalig)."""
    code = normalize_claim_code(code)
    url = backend_url.rstrip("/") + "/api/v1/gateways/claim"
    req = urllib.request.Request(
        url,
        data=json.dumps({"code": code}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            # Cloudflare blockt den Default-UA "Python-urllib/x" mit 403 (error 1010) –
            # das Add-on kommt sonst nie bis zum Backend (#9, echte Ursache des Reports)
            "User-Agent": f"IoTConnectorBridge/{_ADDON_VERSION}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise _claim_http_error(exc) from exc
    except Exception as exc:  # noqa: BLE001
        raise ClaimError(f"Backend nicht erreichbar: {exc}") from exc


def write_credentials(path: str, creds: dict) -> None:
    """Zugangsdaten mit 0600 ins Add-on-Datenverzeichnis."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(creds, fh, indent=2)
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)
