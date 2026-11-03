# Changelog

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