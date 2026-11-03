"""Optionen des Add-ons aus /data/options.json (HAOS) mit Env-Übersteuerung (Tests)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

DEFAULT_TOPIC_FILTERS = ["zigbee2mqtt/#", "shellies/#"]
DEFAULT_BACKEND_URL = "https://iot-connector.gimtiese-tech.de"
DEFAULT_CLOUD_BROKER_URL = "wss://iot-connector-mqtt.gimtiese-tech.de"


@dataclass(frozen=True)
class Options:
    topic_filters: list[str] = field(default_factory=lambda: list(DEFAULT_TOPIC_FILTERS))
    buffer_max_messages: int = 50000
    backend_url: str = DEFAULT_BACKEND_URL
    cloud_broker_url: str = DEFAULT_CLOUD_BROKER_URL
    data_dir: str = "/data"

    @property
    def store_path(self) -> str:
        return os.path.join(self.data_dir, "buffer.sqlite")

    @property
    def credentials_path(self) -> str:
        return os.path.join(self.data_dir, "gateway.json")

    @property
    def state_path(self) -> str:
        return os.path.join(self.data_dir, "state.json")


def load_options() -> Options:
    """Liest /data/options.json (HAOS) und lässt Env-Variablen (Tests) übersteuern."""
    topic_filters = list(DEFAULT_TOPIC_FILTERS)
    buffer_max = 50000
    backend = DEFAULT_BACKEND_URL
    cloud = DEFAULT_CLOUD_BROKER_URL
    options_file = os.environ.get("ADDON_OPTIONS_FILE", "/data/options.json")
    if os.path.exists(options_file):
        raw = json.load(open(options_file, encoding="utf-8"))
        topic_filters = list(raw.get("topic_filters") or topic_filters)
        buffer_max = int(raw.get("buffer_max_messages") or buffer_max)
        backend = raw.get("backend_url") or backend
        cloud = raw.get("cloud_broker_url") or cloud
    return Options(
        topic_filters=[t for t in topic_filters if t],
        buffer_max_messages=max(100, int(os.environ.get("BUFFER_MAX_MESSAGES", buffer_max))),
        backend_url=os.environ.get("BACKEND_URL", backend),
        cloud_broker_url=os.environ.get("CLOUD_BROKER_URL", cloud),
        data_dir=os.environ.get("ADDON_DATA_DIR", "/data"),
    )
