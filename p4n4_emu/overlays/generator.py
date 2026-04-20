"""Compose overlay renderer — generates per-stack resource-limit overlays."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from p4n4_emu.profiles.loader import Profile

_TMPL_DIR = Path(__file__).parent / "templates"


def _mb(bytes_val: int) -> str:
    return f"{bytes_val // (1024 * 1024)}m"


def render_overlay(profile: Profile, stack: str, blkio_device: str | None) -> str:
    """Render a Compose override YAML string for *stack* using *profile*.

    Args:
        profile: loaded hardware profile
        stack: one of "iot", "ai", "edge"
        blkio_device: block device path (e.g. "/dev/sda") or None to skip blkio limits
    """
    env = Environment(
        loader=FileSystemLoader(str(_TMPL_DIR)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["mb"] = _mb

    tmpl = env.get_template(f"{stack}.emu.yml.j2")
    return tmpl.render(
        profile=profile,
        blkio_device=blkio_device,
    )
