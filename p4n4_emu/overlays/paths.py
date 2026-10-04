"""Where generated overlays live, and what they record.

Each stack directory gets its own folder, so two projects never share an overlay:

    ~/.p4n4-emu/overlays/<dir-name>-<hash>/<stack>.emu.yml

`up` writes the overlay and `down` removes it, so an existing overlay means the
stack is running under p4n4-emu. Its `x-p4n4-emu` block names the profile, which
lets `down`, `status` and `logs` work without `--profile`.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml

OVERLAY_ROOT = Path.home() / ".p4n4-emu" / "overlays"
META_KEY = "x-p4n4-emu"


def _dir_key(stack_dir: Path, stack: str) -> str:
    """Readable, unique folder name for *stack_dir*, e.g. greenhouse-1a2b3c4d.

    Multi-layer projects keep each stack in a directory named after it, so the
    project's name is added: greenhouse-iot-1a2b3c4d.
    """
    path = stack_dir.resolve()
    name = f"{path.parent.name}-{path.name}" if path.name == stack else path.name
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_") or "stack"
    digest = hashlib.sha256(str(path).encode()).hexdigest()[:8]
    return f"{slug}-{digest}"


def overlay_path(stack_dir: Path, stack: str) -> Path:
    """Path of *stack*'s overlay for the stack in *stack_dir*, whether or not it exists."""
    return OVERLAY_ROOT / _dir_key(stack_dir, stack) / f"{stack}.emu.yml"


def existing_overlay(stack_dir: Path, stack: str) -> Path | None:
    """*stack*'s overlay, or None if it isn't running under p4n4-emu."""
    path = overlay_path(stack_dir, stack)
    return path if path.exists() else None


def read_overlay(path: Path) -> dict:
    """The overlay as a dict; empty if it can't be read."""
    try:
        doc = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError):
        return {}
    return doc if isinstance(doc, dict) else {}


def active_profile(stack_dir: Path, stack: str) -> str | None:
    """Profile the stack was started with, from its overlay; None if not running under emu."""
    path = existing_overlay(stack_dir, stack)
    if path is None:
        return None
    meta = read_overlay(path).get(META_KEY)
    return meta.get("profile") if isinstance(meta, dict) else None


def remove_overlay(stack_dir: Path, stack: str) -> None:
    """Delete *stack*'s overlay, and its folder once empty."""
    path = overlay_path(stack_dir, stack)
    path.unlink(missing_ok=True)
    try:
        path.parent.rmdir()
    except OSError:
        pass  # another stack of the same directory still has its overlay
