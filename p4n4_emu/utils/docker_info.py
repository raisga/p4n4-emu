"""Docker host introspection — detect the block device backing Docker's data root."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


def _docker_root() -> str | None:
    r = subprocess.run(
        ["docker", "info", "--format", "{{json .DockerRootDir}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout.strip())
    except (json.JSONDecodeError, ValueError):
        return r.stdout.strip().strip('"')


def _resolve_device(path: str) -> str | None:
    """Walk /proc/mounts to find the block device that hosts *path*."""
    try:
        mounts_text = Path("/proc/mounts").read_text()
    except OSError:
        return None

    best_match: tuple[int, str] = (0, "")
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        device, mount_point = parts[0], parts[1]
        if not path.startswith(mount_point):
            continue
        if len(mount_point) > best_match[0]:
            best_match = (len(mount_point), device)

    device = best_match[1]
    if not device or not device.startswith("/dev/"):
        return None

    # Strip partition number to get the base block device (e.g. /dev/sda1 → /dev/sda)
    base = re.sub(r"p?\d+$", "", device)
    if Path(base).exists():
        return base
    if Path(device).exists():
        return device
    return None


def detect_block_device() -> str | None:
    """Return the block device path (e.g. '/dev/sda') backing Docker's data root.

    Returns None if detection fails or the path is not on a real block device.
    """
    root = _docker_root()
    if not root:
        return None
    return _resolve_device(root)
