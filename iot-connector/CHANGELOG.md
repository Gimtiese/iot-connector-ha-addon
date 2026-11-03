# Changelog

## 1.1.3 (2026-11-03)

Fix: Nach dem Update auf 1.1.2 „Cloud getrennt“ ohne Ausweg – Claim verweigert mit „Bereits verknüpft“ (iot-connector-ha-addon#11).

- **Ursache des festgefahrenen Zustands:** Die Claim-Box wurde immer angezeigt – auch bei bestehender Verknüpfung. Nach einem Verbindungsverlust (z. B. Update-Neustart oder Backend-Neustart) sah das wie „bitte neu verknüpfen“ aus. Wer dann das Gateway im IoT-Connector löschte, kam mit den alten Zugangsdaten nie wieder rein (CONNACK *Not authorized*) – und ein neuer Claim wurde abgelehnt („Bereits verknüpft“), weil `gateway.json` noch da war. Einziger Ausweg: Deinstallieren (verliert `/data` inkl. Puffer)
- **Cloud startet jetzt vor dem Supervisor-Zugriff:** `_start_cloud()` läuft in `Runtime.start()` vor `local_mqtt_credentials()` – direkt nach einem Add-on-Update ist der Supervisor oft noch beschäftigt, und die Cloud-Verbindung darf nicht hinter der lokalen Broker-Verbindung stehen bleiben. Zusätzlich fängt `_start_cloud()` alle Fehler ab: Ein Cloud-Start-fehler legt nie mehr die lokale Bridge lahm
- **UI zeigt den Klartext-Zustand:** Claim-Formular nur solange nicht verknüpft. Verknüpft: Gateway-Karte mit Name/ID, Cloud-Status und „Verknüpfung aufheben“ (mit Bestätigung). Das Formular verschwindet aus der Ansicht, wenn `claimed: true`
- **„Verknüpfung aufheben“ (neu, `POST /api/unlink`):** Stoppt den Cloud-Client, trennt ihn von der Bridge und löscht `gateway.json` – der SQLite-Puffer bleibt vollständig erhalten. Danach ist ein neuer Claim sofort möglich
- **CONNACK-Grund in UI und Protokoll:** Lehnt der Cloud-Broker die Zugangsdaten dauerhaft ab (4/5/134/135, z. B. gelöschtes Gateway), zeigt die UI „Gateway im IoT-Connector gelöscht oder getrennt – neu verknüpfen“ mit dem Weg dorthin. Der Grund wird einmalig pro Störung geloggt statt bei jedem Retry (paho wiederholt endlos mit Backoff)
- **`api/status`:** Neue Felder `gateway_name`, `gateway_id`, `cloud_error` (letzter CONNACK-Fehler)
- **Claim-409:** „Bereits verknüpft – zuerst ‚Verknüpfung aufheben‘, dann den neuen Code eingeben“ – der Weg ist jetzt in der Meldung
- **Tests:** `tests/test_restart_unlink.py` deckt den Neustart mit vorhandener `gateway.json` (Cloud startet ohne neuen Claim), das Entkoppeln (Credentials weg, Puffer bleibt) und die WebUI-Handler (Unlink, Claim-409, Status) ab

## 1.1.2 (2026-11-03)

Fix: Claim-Code wird nicht angenommen – Fehlermeldungen unterscheiden jetzt die Ursache (iot-connector-ha-addon#9).

- **Ursache gegen die Produktion gefunden:** Das Add-on 1.1.1 schickt `urllib` mit Default-User-Agent `Python-urllib/3.x` – **Cloudflare blockt genau den mit 403 (error 1010, „banned browser signature“)**. Das Backend wurde nie erreicht; die UI zeigte pauschal „Code ungültig oder abgelaufen.“ Das Backend selbst ist gesund (`/health` ok, 400 für ungültige Codes, DynSec konfiguriert – `redeem()` prüft DynSec vor dem Code und antwortet nicht mit 503). Fix: Das Add-on meldet sich als `IoTConnectorBridge/1.1.2` – damit kommt die Anfrage durch (verifiziert gegen die Produktion: `400` statt `403`)
- **Fehler-Mapping:** `claim_gateway()` übersetzt jetzt den HTTP-Status in eigene Meldungen – 400 (ungültig/abgelaufen, mit Hinweis TTL/einmalig), 403 (Cloudflare-Block), 429 (Rate-Limit), 503 (MQTT-Zugänge nicht konfiguriert), 502 (Broker abgelehnt), 422 (Schemafehler) und 5xx (Serverfehler). Vorher stand für jeden HTTP-Fehler pauschal „Code ungültig oder abgelaufen."
- **Backend-`detail` durchreichen:** Der Text aus der Backend-Antwort steht in der UI und im Add-on-Log (`Claim fehlgeschlagen: HTTP <status> <detail>`)
- **Code normalisieren:** Leerzeichen, Bindestriche und Groß-/Kleinschreibung werden vor dem Senden vereinheitlicht (wie das Backend)
- **Tests:** `tests/test_claim_errors.py` deckt das Status-Mapping (400/429/503/422/500) und die Normalisierung ab

## 1.1.1 (2026-11-03)

Fix: Add-on erscheint nach dem Hinzufügen des Repos nicht im HA-Store (iot-connector-ha-addon#5).

- **Ursache:** `config.yaml` war gegen das echte Supervisor-Schema invalide – der Supervisor verwirft das Add-on beim Laden des Repos still. `panel_ingress` (existiert nicht, korrekt ist `panel_title`/`panel_icon`) und `privileged: false` (muss eine Liste von Capabilities sein, nicht Bool) sind raus bzw. ersetzt
- **Belegt mit dem echten Supervisor-Validator** (home-assistant/supervisor, Tag 2025.06.0, `SCHEMA_ADDON_CONFIG`/`SCHEMA_REPOSITORY_CONFIG`): beide Dateien validieren jetzt OK
- **Architekturen geprüft:** aarch64 + amd64 im `config.yaml`, passend zu `build.yaml` (Raspberry Pi 4 = aarch64, Intel/AMD = amd64)
- **CI:** neuer Job `schema`, der `config.yaml`/`repository.yaml` gegen das Supervisor-Schema (gepinntes Tag) validiert – künftige Inkompatibilitäten fallen vor dem Merge auf

## 1.1.0 (2026-11-03)

Default: alle MQTT-Topics übertragen, `topic_filters` optional (iot-connector-ha-addon#3).

- **Default `#` statt Allowlist:** Ohne `topic_filters` (oder mit leerer Liste) werden alle Topics des lokalen Brokers gespiegelt, inkl. `homeassistant/#`
- **Migration:** Bestehende Installationen mit dem alten Default (`zigbee2mqtt/#`, `shellies/#`) wechseln automatisch auf „alles" – die Option wird als „nicht gesetzt" behandelt
- **`$SYS/#` bleibt außen vor:** `#` matcht per MQTT-Spezifikation keine `$`-Topics (4.7.2), per Test abgesichert
- **Keine Schleifen:** Das Add-on publiziert weiterhin nie in den lokalen Broker (per Test belegt)
- **Retained beim Start gedrosselt:** Nachliefern in kleinen Batches mit Pause (200/0,1 s), damit retained-Bursts (Discovery) Tenant-Ratenlimits (iot-connector#117) nicht reißen
- `config.yaml`: `topic_filters` optional (Default `[]`), Schema unverändert
- DOCS: Abschnitt „Filtern (optional)" ergänzt

## 1.0.0 (2026-11-03)

Erste Version (iot-connector-ha-addon#1, Spezifikation iot-connector#129).

- Add-on-Repository-Struktur (`repository.yaml`, `config.yaml`, `Dockerfile`, `build.yaml`, Multi-Arch aarch64/amd64)
- `services: mqtt:need` – Zugangsdaten zum lokalen Mosquitto über die Supervisor-API, keine manuelle Eingabe
- Nur lesend: abonniert lokal, publiziert in die Cloud unter `d/{gateway_id}/<lokales Topic>`. Nichts wird lokal geschrieben
- Topic-Allowlist in den Add-on-Optionen (Default `zigbee2mqtt/#`, `shellies/#`; `homeassistant/#` aus)
- Store-and-Forward (SQLite) bei Verbindungsverlust, Puffergrenze konfigurierbar, QoS 1, Original-Zeitstempel als MQTT-5-User-Property `ts`
- Cloud-Verbindung: MQTT über WebSocket mit TLS zu `iot-connector-mqtt.gimtiese-tech.de`, Keepalive 30 s
- Ingress-UI: Claim-Code eingeben (iot-connector#122), Status, letzte Übertragung, Puffergröße
- Unit-Tests für Allowlist, Mapping, Puffer; Integrationstest gegen lokale Mosquitto-Container
- CI: Lint, Tests, Image-Build für beide Architekturen