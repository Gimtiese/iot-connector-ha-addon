# Changelog

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