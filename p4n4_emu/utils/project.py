"""Locate stack compose directories.

Understands three source layouts:

1. An explicit ``--stack-dir`` (checked first; ``<dir>/<stack>`` before ``<dir>``).
2. A p4n4 project (found by walking up to ``.p4n4.json``): single-layer projects
   keep ``docker-compose.yml`` at the project root, multi-layer projects give
   each layer its own subdirectory (``<project>/iot/``, ``<project>/ai/``).
3. Bare stack checkouts relative to the current directory (legacy fallback).

The p4n4 layout rules mirror ``p4n4_lib.layout`` so the emulator resolves
projects exactly like the p4n4 CLI does.
"""

from __future__ import annotations

import json
from pathlib import Path

MANIFEST_FILE = ".p4n4.json"
COMPOSE_FILE = "docker-compose.yml"
STACKS = ("iot", "ai", "edge")


def find_manifest(start: Path | None = None) -> Path | None:
    """Walk up from start (default cwd) to find .p4n4.json."""
    current = (start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        candidate = directory / MANIFEST_FILE
        if candidate.exists():
            return candidate
    return None


def manifest_layers(manifest_path: Path) -> list[str]:
    """Known stack layers enabled in a project manifest, in dependency order."""
    try:
        layers = json.loads(manifest_path.read_text()).get("layers", [])
    except (OSError, json.JSONDecodeError):
        return []
    return [name for name in STACKS if name in layers]


def expand_stacks(stack: str | None, start: Path | None = None) -> list[str]:
    """
    Turn the --stack option into a list of stack names.

    None or "all" resolve to the current p4n4 project's enabled layers when a
    manifest is found; otherwise None defaults to iot and "all" to every stack.
    Explicit values may be comma-separated.
    """
    if stack not in (None, "all"):
        return [s.strip() for s in stack.split(",") if s.strip()]
    manifest = find_manifest(start)
    if manifest:
        enabled = manifest_layers(manifest)
        if enabled:
            return enabled
    return list(STACKS) if stack == "all" else ["iot"]


def resolve_stack_dir(base: Path | None, stack: str, start: Path | None = None) -> Path | None:
    """Directory holding the given stack's docker-compose.yml, or None."""
    if base is not None:
        for candidate in (base / stack, base):
            if (candidate / COMPOSE_FILE).exists():
                return candidate
        return None

    manifest = find_manifest(start)
    if manifest:
        root = manifest.parent
        layers = manifest_layers(manifest)
        if stack not in layers:
            return None
        if (root / COMPOSE_FILE).exists():
            # Flat single-layer layout: the root compose belongs to the first
            # enabled layer only (mirrors p4n4_lib.layout.compose_dirs)
            return root if stack == layers[0] else None
        layer_dir = root / stack
        return layer_dir if (layer_dir / COMPOSE_FILE).exists() else None

    cwd = (start or Path.cwd()).resolve()
    for candidate in (cwd / stack, cwd, cwd.parent / "docker" / stack):
        if (candidate / COMPOSE_FILE).exists():
            return candidate
    return None
