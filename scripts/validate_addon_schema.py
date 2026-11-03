#!/usr/bin/env python3
"""Validiert Add-on- und Repository-Config gegen das echte Supervisor-Schema.

Nimmt den Pfad zum Klon von home-assistant/supervisor als Argument, damit CI
das Tag pinnen kann. Basis ist dasselbe voluptuous-Schema, mit dem der
Supervisor Add-ons beim Laden eines Repos validiert – invalide Add-ons
erscheinen sonst still nicht im Store (iot-connector-ha-addon#5).
"""

import sys
from pathlib import Path

import yaml

SUPERVISOR_REF = "2025.06.0"


def main() -> int:
    supervisor = Path(sys.argv[1] if len(sys.argv) > 1 else f"/tmp/supervisor-{SUPERVISOR_REF}")
    sys.path.insert(0, str(supervisor))

    from supervisor.addons.validate import SCHEMA_ADDON_CONFIG, SCHEMA_BUILD_CONFIG
    from supervisor.store.validate import SCHEMA_REPOSITORY_CONFIG

    root = Path(__file__).resolve().parent.parent
    failed = False

    cases = [
        (root / "iot-connector" / "config.yaml", SCHEMA_ADDON_CONFIG),
        (root / "iot-connector" / "build.yaml", SCHEMA_BUILD_CONFIG),
        (root / "repository.yaml", SCHEMA_REPOSITORY_CONFIG),
    ]
    for path, schema in cases:
        data = yaml.safe_load(path.read_text())
        try:
            schema(data)
        except Exception as err:
            failed = True
            print(f"FAIL {path}")
            for sub in getattr(err, "errors", [err]):
                print(f"  - {list(getattr(sub, 'path', []))}: {getattr(sub, 'message', sub)}")
        else:
            print(f"OK   {path}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
