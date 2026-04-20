"""Environment preflight checks."""

from __future__ import annotations

import subprocess
from pathlib import Path


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


def run_preflight(require_qemu: bool = False) -> list[str]:
    """Return a list of error strings; empty list means all checks passed."""
    errors: list[str] = []

    # Docker Engine >= 24
    rc, out = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
    if rc != 0:
        errors.append("Docker is not running or not installed.")
    else:
        ver = _version_tuple(out)
        if ver < (24,):
            errors.append(f"Docker Engine >= 24 required; found {out!r}.")

    # Docker Compose >= 2.17
    rc, out = _run(["docker", "compose", "version", "--short"])
    if rc != 0:
        errors.append("Docker Compose v2 not found. Install the Compose plugin.")
    else:
        ver = _version_tuple(out)
        if ver < (2, 17):
            errors.append(f"Docker Compose >= 2.17 required; found {out!r}.")

    # cgroup v2 — warn only (non-fatal)
    cgroupv2 = Path("/sys/fs/cgroup/cgroup.controllers")
    if not cgroupv2.exists():
        errors.append(
            "WARNING: cgroup v2 not detected. CPU/memory limits may not be enforced. "
            "Check your kernel boot parameters (systemd.unified_cgroup_hierarchy=1)."
        )

    # QEMU binfmt for ARM64
    if require_qemu:
        binfmt = Path("/proc/sys/fs/binfmt_misc/qemu-aarch64")
        if not binfmt.exists():
            errors.append(
                "QEMU ARM64 binfmt not registered. "
                "Run: p4n4-emu setup --arch arm64"
            )

    return errors


def check_or_exit(require_qemu: bool = False) -> None:
    """Run preflight and raise SystemExit if any hard errors are found."""
    from rich.console import Console

    console = Console()
    issues = run_preflight(require_qemu=require_qemu)
    warnings = [e for e in issues if e.startswith("WARNING")]
    errors = [e for e in issues if not e.startswith("WARNING")]

    for w in warnings:
        console.print(f"[yellow]Warning:[/yellow] {w}")
    for e in errors:
        console.print(f"[red]Error:[/red] {e}")

    if errors:
        raise SystemExit(1)
