"""Docker host introspection — detect the block device backing Docker's data root."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

_SYS_BLOCK = Path("/sys/dev/block")  # <major>:<minor> → the device's sysfs directory
_DEV = Path("/dev")


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


def _is_under(path: str, mount_point: str) -> bool:
    """True if *path* is *mount_point* or inside it, compared by path component."""
    if mount_point == "/":
        return path.startswith("/")
    return path == mount_point or path.startswith(mount_point.rstrip("/") + "/")


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
        if not _is_under(path, mount_point):
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
    return _device_by_number(path)


def _device_by_number(path: str) -> str | None:
    """The disk holding *path*, found by its device number in /sys/dev/block.

    For a root filesystem that /proc/mounts lists under a name with no device
    node, such as `/dev/root` on Ubuntu's cloud images (EC2, GCP).
    """
    try:
        dev = os.stat(path).st_dev
        node = (_SYS_BLOCK / f"{os.major(dev)}:{os.minor(dev)}").resolve(strict=True)
    except OSError:
        return None
    disk = node.parent if (node / "partition").exists() else node
    candidate = _DEV / disk.name
    return str(candidate) if candidate.exists() else None


def detect_block_device() -> str | None:
    """Return the block device path (e.g. '/dev/sda') backing Docker's data root.

    Returns None if detection fails or the path is not on a real block device.
    """
    root = _docker_root()
    if not root:
        return None
    return _resolve_device(root)
