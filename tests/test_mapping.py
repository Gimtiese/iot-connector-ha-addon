"""Unit-Tests: Topic-Filter, Cloud-Mapping, Default-Migration (#3)."""

from __future__ import annotations

import json

import pytest
from app.cloud import parse_ws_url
from app.mapping import cloud_topic, is_allowed, is_valid_topic, topic_matches


class TestTopicMatches:
    @pytest.mark.parametrize(
        ("pattern", "topic", "expected"),
        [
            ("zigbee2mqtt/#", "zigbee2mqtt/sensor1", True),
            ("zigbee2mqtt/#", "zigbee2mqtt/bridge/config", True),
            ("zigbee2mqtt/#", "zigbee2mqtt", True),  # '#' zählt die Elternebene mit
            ("shellies/+", "shellies/shelly-1", True),
            ("shellies/+", "shellies/a/b", False),
            ("shellies/+/relay/0", "shellies/x/relay/0", True),
            ("shellies/+/relay/0", "shellies/x/relay/1", False),
            ("#", "anything/at/all", True),
            ("zigbee2mqtt/#", "homeassistant/sensor/x", False),
        ],
    )
    def test_pattern(self, pattern: str, topic: str, expected: bool) -> None:
        assert topic_matches(pattern, topic) is expected

    @pytest.mark.parametrize(
        "pattern",
        ["#", "+/#", "+/+", "#"],
    )
    def test_sys_topics_werden_von_wildcards_nicht_gematcht(self, pattern: str) -> None:
        """MQTT-Spezifikation 4.7.2: '$'-Topics matchen keine Wildcard-Filter."""
        assert not topic_matches(pattern, "$SYS/broker/uptime")

    def test_sys_topic_expliziter_filter(self) -> None:
        """Explizite '$'-Filter matchen weiterhin (Grundprinzip bleibt)."""
        assert topic_matches("$SYS/#", "$SYS/broker/uptime")


class TestDefaultAlleTopics:
    def test_default_ist_alles(self) -> None:
        from app.config import DEFAULT_TOPIC_FILTERS

        assert DEFAULT_TOPIC_FILTERS == ["#"]

    def test_default_spiegelt_auch_homeassistant(self) -> None:
        from app.config import DEFAULT_TOPIC_FILTERS

        assert is_allowed("homeassistant/sensor/x/config", DEFAULT_TOPIC_FILTERS)
        assert is_allowed("zigbee2mqtt/sensor1", DEFAULT_TOPIC_FILTERS)

    def test_leere_option_behandelt_wie_nicht_gesetzt(self) -> None:
        from app.config import _normalize_filters

        assert _normalize_filters([]) == ["#"]
        assert _normalize_filters(None) == ["#"]
        assert _normalize_filters(["", "  "]) == ["#"]

    def test_alter_default_gilt_als_nicht_gesetzt(self) -> None:
        """Migration: Installationen mit altem Default wechseln auf „alles“."""
        from app.config import _normalize_filters

        assert _normalize_filters(["zigbee2mqtt/#", "shellies/#"]) == ["#"]

    def test_gesetzte_filter_bleiben_allowlist(self) -> None:
        from app.config import _normalize_filters

        assert _normalize_filters(["zigbee2mqtt/#"]) == ["zigbee2mqtt/#"]

    def test_load_options_ohne_option(self, tmp_path, monkeypatch) -> None:
        from app.config import load_options

        options_file = tmp_path / "options.json"
        options_file.write_text(json.dumps({"buffer_max_messages": 1000}), encoding="utf-8")
        monkeypatch.setenv("ADDON_OPTIONS_FILE", str(options_file))
        opts = load_options()
        assert opts.topic_filters == ["#"]

    def test_load_options_mit_altem_default(self, tmp_path, monkeypatch) -> None:
        from app.config import load_options

        options_file = tmp_path / "options.json"
        options_file.write_text(
            json.dumps({"topic_filters": ["zigbee2mqtt/#", "shellies/#"]}), encoding="utf-8"
        )
        monkeypatch.setenv("ADDON_OPTIONS_FILE", str(options_file))
        assert load_options().topic_filters == ["#"]


class TestKeineSchleife:
    def test_bridge_publiziert_nie_lokal(self) -> None:
        """Schleifenschutz: die Bridge schreibt nie in den lokalen Broker."""
        from app.bridge import Bridge

        class FakeLocal:
            def __init__(self) -> None:
                self.published: list[tuple] = []

            def publish(self, *a, **kw) -> None:
                self.published.append((a, kw))

            def subscribe(self, *a, **kw) -> None:  # pragma: no cover
                pass

        class FakeMsg:
            topic = "homeassistant/sensor/x/config"
            payload = b"{}"

        class FakeStore:
            def add(self, topic, payload, ts=0.0) -> int:
                return 1

        local = FakeLocal()
        bridge = Bridge(local, cloud=None, topic_filters=["#"], store=FakeStore())
        bridge.start()
        bridge._on_local_message(local, None, FakeMsg())
        assert local.published == [], "Bridge darf nie in den lokalen Broker publizieren"


class TestCloudTopic:
    def test_prefix_aus_claim_wird_verwendet(self) -> None:
        assert cloud_topic("d/gw-123/", "zigbee2mqtt/sensor1") == "d/gw-123/zigbee2mqtt/sensor1"

    def test_prefix_ohne_slash(self) -> None:
        assert cloud_topic("d/gw-123", "shellies/x/relay/0") == "d/gw-123/shellies/x/relay/0"

    def test_valid_topic_lehnt_wildcards_ab(self) -> None:
        assert not is_valid_topic("d/gw/#")
        assert not is_valid_topic("d/gw/+")
        assert is_valid_topic("d/gw-123/zigbee2mqtt/sensor1")


class TestWsUrl:
    def test_wss_default_port(self) -> None:
        host, port, tls, path = parse_ws_url("wss://iot-connector-mqtt.gimtiese-tech.de")
        assert (host, port, tls, path) == ("iot-connector-mqtt.gimtiese-tech.de", 443, True, "/")

    def test_explicit_port_and_path(self) -> None:
        host, port, tls, path = parse_ws_url("ws://localhost:9001/mqtt")
        assert (host, port, tls) == ("localhost", 9001, False)
        assert path == "/mqtt"

    def test_invalid_scheme(self) -> None:
        import pytest as _pytest

        with _pytest.raises(ValueError):
            parse_ws_url("mqtt://example.com")
