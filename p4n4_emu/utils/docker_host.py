"""What `docker info` says about the engine: version, cgroups, and whether it runs in a VM.

On Docker Desktop (macOS, Windows) the engine runs in a Linux VM, so this host's
`/sys/fs/cgroup` and `/proc/sys/fs/binfmt_misc` say nothing about it: ask the
engine instead, and only fall back to the host's files when it doesn't answer.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

_FIELDS = ("ServerVersion", "CgroupVersion", "CgroupDriver", "OperatingSystem")


@dataclass(frozen=True)
class DockerHost:
    version: str
    cgroup_version: str  # "1" or "2"
    cgroup_driver: str  # "systemd", "cgroupfs", or "none" (no limits, e.g. rootless)
    os: str

    @property
    def desktop(self) -> bool:
        """Docker Desktop: the engine runs in a VM that ships QEMU for arm64 and amd64."""
        return "docker desktop" in self.os.lower()


def docker_host() -> DockerHost | None:
    """The engine's `docker info`, or None when Docker is missing or not running."""
    fmt = "|".join(f"{{{{.{f}}}}}" for f in _FIELDS)
    try:
        r = subprocess.run(
            ["docker", "info", "--format", fmt], capture_output=True, text=True, check=False
        )
    except FileNotFoundError:
        return None
    parts = r.stdout.strip().split("|")
    if r.returncode != 0 or len(parts) != len(_FIELDS) or not parts[0]:
        return None
    return DockerHost(*parts)


def cgroup_v2(host: DockerHost | None = None) -> bool:
    """Whether the engine uses cgroup v2, without which limits are set but not enforced."""
    host = host or docker_host()
    if host is not None and host.cgroup_version:
        return host.cgroup_version == "2"
    return Path("/sys/fs/cgroup/cgroup.controllers").exists()
