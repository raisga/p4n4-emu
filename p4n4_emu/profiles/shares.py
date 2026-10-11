"""Per-service share overrides, from a profile and from a p4n4 project.

A service's share is the fraction of the device's CPU and memory it gets. The
defaults live in overlays.generator.SERVICE_SHARES; a profile's `shares:` key
changes them for that board, and a project's `.p4n4-emu/config.yml` changes
them for that project, on any board:

    shares:
      influxdb: {cpu: 0.5, memory: 0.4}
      node-red: {memory: 0.25}      # cpu keeps its default
      "*": {cpu: 0.05}              # services with no default share

Later sources win per field: the defaults, then the profile, then the project.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

FALLBACK_KEY = "*"
SHARE_FIELDS = ("cpu", "memory")
PROJECT_CONFIG = Path(".p4n4-emu") / "config.yml"
PROJECT_KEYS = {"shares": "per-service CPU and memory shares (see `profile show`)"}

# service → field → fraction of the device, e.g. {"influxdb": {"cpu": 0.5}}
Overrides = dict[str, dict[str, float]]


class SharesError(ValueError):
    pass


def parse_shares(doc: Any, key: str = "shares") -> Overrides:
    """Check a `shares:` mapping; errors name the key at fault (e.g. shares.influxdb.cpu)."""
    if doc is None:
        return {}
    if not isinstance(doc, dict):
        raise SharesError(f"{key}: expected a mapping of service names")
    out: Overrides = {}
    for service, fields in doc.items():
        where = f"{key}.{service}"
        if not isinstance(fields, dict) or not fields:
            raise SharesError(f"{where}: expected cpu and/or memory, e.g. {{cpu: 0.3}}")
        unknown = [str(f) for f in fields if f not in SHARE_FIELDS]
        if unknown:
            raise SharesError(
                f"{where}: unknown key(s) {', '.join(unknown)} (expected: cpu, memory)"
            )
        out[str(service)] = {}
        for field, value in fields.items():
            if isinstance(value, bool) or not isinstance(value, int | float) or not 0 < value <= 1:
                raise SharesError(
                    f"{where}.{field}: expected a fraction of the device above 0, up to 1"
                )
            out[str(service)][field] = float(value)
    return out


def merge(*sources: Overrides) -> Overrides:
    """Combine overrides field by field, later sources winning."""
    out: Overrides = {}
    for source in sources:
        for service, fields in source.items():
            out.setdefault(service, {}).update(fields)
    return out


def project_config_path(start: Path | None = None) -> Path | None:
    from p4n4_emu.utils.project import find_manifest

    manifest = find_manifest(start)
    return manifest.parent / PROJECT_CONFIG if manifest else None


def load_project_config(path: Path) -> Overrides:
    """A project's `.p4n4-emu/config.yml` shares; errors name the file and key."""
    try:
        doc = yaml.safe_load(path.read_text())
    except OSError as e:
        raise SharesError(f"Cannot read {path}: {e.strerror}") from e
    except yaml.YAMLError as e:
        raise SharesError(f"{path} is not valid YAML: {e}") from e
    if doc is None:
        return {}
    try:
        if not isinstance(doc, dict):
            raise SharesError("expected a mapping")
        unknown = [str(k) for k in doc if k not in PROJECT_KEYS]
        if unknown:
            raise SharesError(
                f"unknown key(s) {', '.join(unknown)} (expected: {', '.join(PROJECT_KEYS)})"
            )
        return parse_shares(doc.get("shares"))
    except SharesError as e:
        raise SharesError(f"{path}: {e}") from e


def project_shares(start: Path | None = None) -> Overrides:
    """The current p4n4 project's share overrides; empty outside a project or without the file."""
    path = project_config_path(start)
    if path is None or not path.exists():
        return {}
    return load_project_config(path)
