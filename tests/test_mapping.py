"""Unit-Tests: Topic-Allowlist und Cloud-Mapping (iot-connector#129)."""

from __future__ import annotations

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


class TestAllowlist:
    def test_default_filters_erlauben_zigbee_und_shellies(self) -> None:
        from app.config import DEFAULT_TOPIC_FILTERS

        assert is_allowed("zigbee2mqtt/sensor1", DEFAULT_TOPIC_FILTERS)
        assert is_allowed("shellies/x/relay/0", DEFAULT_TOPIC_FILTERS)

    def test_homeassistant_standardmaessig_aus(self) -> None:
        from app.config import DEFAULT_TOPIC_FILTERS

        assert not is_allowed("homeassistant/sensor/x", DEFAULT_TOPIC_FILTERS)

    def test_nicht_gelistetes_topic_wird_nicht_gespiegelt(self) -> None:
        assert not is_allowed("tasmota/tele/x", ["zigbee2mqtt/#", "shellies/#"])


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
