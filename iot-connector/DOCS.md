# IoT Connector Bridge (HAOS-Add-on)

Spiegelt Topics des lokalen Mosquitto **nur lesend** in den IoT-Connector
(Gimtiese-Tech, iot-connector#129 / ADR 0001). Home Assistant läuft unverändert
weiter – ein Absturz des Add-ons stört HAOS nicht.

## Installation

Repository in Home Assistant hinzufügen:

```text
https://github.com/Gimtiese/iot-connector-ha-addon
```

Dann „IoT Connector Bridge" installieren und starten. Voraussetzungen:

- Mosquitto-Broker in HAOS eingerichtet (Add-on `mosquitto-broker` o. ä.)
- Ein Gateway im IoT-Connector angelegt und dessen **Claim-Code** bereit

## Einrichtung

1. Add-on starten. Die Zugangsdaten zum lokalen Broker kommen automatisch über
   die Supervisor-API (`services: mqtt:need`) – nichts eintippen.
2. In der Seitenleiste **IoT Connector** (Ingress-UI) öffnen und den Claim-Code
   aus dem IoT-Connector eingeben. Die Gateway-Zugangsdaten werden einmalig beim
   Backend abgeholt (`POST /api/v1/gateways/claim`, iot-connector#122) und im
   Add-on-Datenverzeichnis gespeichert (`/data/gateway.json`, nur für das Add-on lesbar).
3. Das Add-on verbindet sich mit der Cloud und spiegelt fortan alle erlaubten
   Topics unter `d/{gateway_id}/<lokales Topic>` – der Cloud-Broker leitet sie an
   den Ingest des Tenants weiter, zu dem das Gateway gehört.

## Optionen

| Option | Default | Bedeutung |
| --- | --- | --- |
| `topic_filters` | `["zigbee2mqtt/#", "shellies/#"]` | MQTT-Filter, nur diese Topics werden abonniert und gespiegelt |
| `buffer_max_messages` | `50000` | Puffergrenze (Store-and-Forward). Mehr Nachrichten: die ältesten werden verworfen, das Add-on stürzt nicht ab |
| `backend_url` | `https://iot-connector.gimtiese-tech.de` | Backend für den Claim (Übersteuerung nur für Tests) |
| `cloud_broker_url` | `wss://iot-connector-mqtt.gimtiese-tech.de` | Cloud-Broker (Default, sofern der Claim keine URL liefert) |

`homeassistant/#` ist standardmäßig **aus** – Discovery-Metadaten folgen separat (iot-connector#36).

## Verhalten

- **Nur lesend:** Es wird nie in den lokalen Broker publiziert.
- **QoS 1 + Store-and-Forward:** Bei Verbindungsverlust zur Cloud puffert das
  Add-on in SQLite (`/data/buffer.sqlite`) und liefert nach – älteste zuerst,
  ohne Datenverlust bis zur Puffergrenze.
- **Original-Zeitstempel:** Jede Nachricht trägt die Empfangszeit als MQTT-5-User-Property `ts`
  (iot-connector#124); beim Nachliefern bleibt der ursprüngliche Zeitpunkt erhalten.
- **Keepalive 30 s** (≤ 60 s), MQTT über WebSocket mit TLS.

## Ingress-UI

- Claim-Code eingeben (nur solange noch nicht verknüpft; Neuverknüpfung über den IoT-Connector)
- Status: Cloud verbunden/getrennt, lokaler Broker, Puffergröße, verworfene Nachrichten
  (Puffergrenze), letzte Übertragung

## Tests

```bash
pip install -r iot-connector/app/requirements.txt pytest ruff
ruff check iot-connector/app tests
pytest tests/test_mapping.py tests/test_store.py --no-cov            # Unit
pytest tests/test_integration_mosquitto.py --no-cov                  # braucht Docker
```

## Ende-zu-Ende gegen die Cloud

Folgt, sobald iot-connector#399 (502 auf `iot-connector-mqtt.gimtiese-tech.de`) behoben ist.