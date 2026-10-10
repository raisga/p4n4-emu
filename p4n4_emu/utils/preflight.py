"""Environment preflight checks."""

from __future__ import annotations

import platform as _platform
import subprocess
from pathlib import Path

from p4n4_emu.utils.docker_host import cgroup_v2, docker_host

_BINFMT_DIR = Path("/proc/sys/fs/binfmt_misc")

# Docker platform → (binfmt_misc handler, tonistiigi/binfmt --install name)
_QEMU = {
    "linux/arm64": ("qemu-aarch64", "arm64"),
    "linux/arm/v7": ("qemu-arm", "arm"),
    "linux/amd64": ("qemu-x86_64", "amd64"),
}


def host_platform() -> str:
    """Docker platform of the machine running Docker (assumed to be this host)."""
    machine = _platform.machine().lower()
    if machine in ("aarch64", "arm64"):
        return "linux/arm64"
    if machine.startswith("armv7"):
        return "linux/arm/v7"
    return "linux/amd64"


def needs_qemu(platform: str | None) -> bool:
    """True when containers for *platform* cannot run natively on this host."""
    return platform is not None and platform != host_platform()


def qemu_handler(platform: str) -> tuple[Path, str]:
    """binfmt_misc entry and binfmt installer name for *platform*."""
    handler, install_name = _QEMU.get(platform, _QEMU["linux/arm64"])
    return _BINFMT_DIR / handler, install_name


def _run(args: list[str]) -> tuple[int, str]:
    r = subprocess.run(args, capture_output=True, text=True, check=False)
    return r.returncode, (r.stdout + r.stderr).strip()


def _version_tuple(ver_str: str) -> tuple[int, ...]:
    parts = []
    for part in ver_str.strip().split(".")[:3]:
        digits = "".join(c for c in part if c.isdigit())
        if digits:
            parts.append(int(digits))
    return tuple(parts)


def run_preflight(require_qemu: bool = False, platform: str = "linux/arm64") -> list[str]:
    """Return a list of error strings; empty list means all checks passed.

    With *require_qemu*, also checks the QEMU binfmt handler for *platform*.
    """
    errors: list[str] = []

    # Docker Engine >= 24
    host = docker_host()
    if host is None:
        errors.append("Docker is not running or not installed.")
    elif _version_tuple(host.version) < (24,):
        errors.append(f"Docker Engine >= 24 required; found {host.version!r}.")

    # Docker Compose >= 2.17
    rc, out = _run(["docker", "compose", "version", "--short"])
    if rc != 0:
        errors.append("Docker Compose v2 not found. Install the Compose plugin.")
    else:
        ver = _version_tuple(out)
        if ver < (2, 17):
            errors.append(f"Docker Compose >= 2.17 required; found {out!r}.")

    # cgroup v2 — warn only (non-fatal). Ask the engine: on Docker Desktop it runs
    # in a VM, and this host's /sys/fs/cgroup says nothing about it
    if host is not None and host.cgroup_driver == "none":
        errors.append(
            "WARNING: Docker reports no cgroup driver (rootless Docker without cgroup "
            "delegation?), so CPU/memory limits are not applied."
        )
    elif not cgroup_v2(host):
        errors.append(
            "WARNING: cgroup v2 not detected. CPU/memory limits may not be enforced. "
            "Check your kernel boot parameters (systemd.unified_cgroup_hierarchy=1)."
        )

    # QEMU binfmt for the emulated architecture. Docker Desktop's VM registers
    # its own handlers, which this host's binfmt_misc doesn't show
    if require_qemu and not (host is not None and host.desktop):
        binfmt, install_name = qemu_handler(platform)
        if not binfmt.exists():
            errors.append(
                f"QEMU binfmt for {platform} not registered ({binfmt.name}). "
                f"Run: p4n4-emu setup --arch {install_name}"
            )

    return errors


def check_or_exit(require_qemu: bool = False, platform: str = "linux/arm64") -> None:
    """Run preflight and raise SystemExit if any hard errors are found."""
    from rich.console import Console

    console = Console()
    issues = run_preflight(require_qemu=require_qemu, platform=platform)
    warnings = [e for e in issues if e.startswith("WARNING")]
    errors = [e for e in issues if not e.startswith("WARNING")]

    for w in warnings:
        console.print(f"[yellow]Warning:[/yellow] {w}")
    for e in errors:
        console.print(f"[red]Error:[/red] {e}")

    if errors:
        raise SystemExit(1)
