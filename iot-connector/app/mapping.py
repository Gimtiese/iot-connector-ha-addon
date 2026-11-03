"""Topic-Allowlist und Cloud-Mapping (iot-connector#129, ADR 0001)."""

from __future__ import annotations

import fnmatch


def _levels(pattern: str) -> list[str]:
    return pattern.strip("/").split("/")


def topic_matches(pattern: str, topic: str) -> bool:
    """MQTT-Topic-Filter: '+' genau eine Ebene, '#' am Ende beliebig viele Ebenen.

    ``sport/#`` matcht auch ``sport`` selbst (MQTT-Spezifikation) – '#' zählt
    die Elternebene mit.

    Topics, die mit '$' beginnen (z. B. ``$SYS/…``), matcht kein Filter, der
    mit einem Wildcard beginnt (MQTT-Spezifikation 4.7.2): '``#``' spiegelt
    damit niemals Broker-Statistik.
    """
    p, t = _levels(pattern), _levels(topic)
    if t and t[0].startswith("$") and p and p[0] in ("#", "+"):
        return False
    if p and p[-1] == "#":
        p = p[:-1]
        return len(t) >= len(p) and all(
            pl == "+" or pl == tl for pl, tl in zip(p, t, strict=False)
        )
    if len(p) != len(t):
        return False
    return all(pl == "+" or pl == tl for pl, tl in zip(p, t, strict=False))


def is_allowed(topic: str, filters: list[str]) -> bool:
    """True, wenn das Topic von mindestens einem Filter erlaubt ist (Allowlist)."""
    return any(topic_matches(f, topic) for f in filters)


def cloud_topic(topic_prefix: str, local_topic: str) -> str:
    """Cloud-Topic: ``d/{gateway_id}/<lokales Topic>`` – das Prefix liefert der Claim."""
    return f"{topic_prefix.rstrip('/')}/{local_topic.strip('/')}"


def is_valid_topic(topic: str) -> bool:
    """Keine Wildcards, keine Leer-Ebenen – Publish-Validierung vor dem Senden."""
    return bool(topic) and topic.strip("/") == topic and not set("+#") & set(topic)


# Zwischenprodukt für künftige Filter-Feinheiten (Wildcard-Globs, exakte Namespaces)
def glob_matches(pattern: str, topic: str) -> bool:
    return fnmatch.fnmatchcase(topic, pattern)
