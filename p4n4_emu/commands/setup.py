"""p4n4-emu setup — preflight checks and QEMU binfmt installation."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from p4n4_emu.utils.preflight import run_preflight

console = Console()


def cmd(
    arch: Annotated[
        str,
        typer.Option("--arch", help="Enable architecture emulation (e.g. arm64)."),
    ] = "",
    check_only: Annotated[
        bool,
        typer.Option("--check-only", help="Only run checks; do not install anything."),
    ] = False,
) -> None:
    """Verify the environment and optionally install QEMU binfmt support."""
    require_qemu = arch.lower() in ("arm64", "aarch64")

    issues = run_preflight(require_qemu=require_qemu)
    warnings = [e for e in issues if e.startswith("WARNING")]
    errors = [e for e in issues if not e.startswith("WARNING")]

    table = Table(title="p4n4-emu preflight", show_lines=False)
    table.add_column("Check")
    table.add_column("Result")

    docker_ok = not any("Docker is not" in e or "Docker Engine" in e for e in errors)
    compose_ok = not any("Docker Compose" in e for e in errors)
    cgroupv2_ok = not any("cgroup" in e for e in warnings)
    checks = [
        ("Docker Engine >= 24", docker_ok),
        ("Docker Compose >= 2.17", compose_ok),
        ("cgroup v2", cgroupv2_ok),
    ]
    if require_qemu:
        qemu_ok = not any("QEMU" in e for e in errors)
        checks.append(("QEMU ARM64 binfmt", qemu_ok))

    for name, passed in checks:
        status = "[green]OK[/green]" if passed else "[red]FAIL[/red]"
        table.add_row(name, status)

    console.print(table)

    for w in warnings:
        console.print(f"[yellow]Warning:[/yellow] {w[len('WARNING: '):]}")

    for e in errors:
        console.print(f"[red]Error:[/red] {e}")

    if errors and not require_qemu:
        raise typer.Exit(1)

    if require_qemu and not check_only:
        _install_binfmt()


def _install_binfmt() -> None:
    binfmt = Path("/proc/sys/fs/binfmt_misc/qemu-aarch64")
    if binfmt.exists():
        console.print("[green]QEMU ARM64 binfmt already registered.[/green]")
        return

    console.print("[cyan]Installing QEMU ARM64 binfmt...[/cyan]")
    rc = subprocess.run(
        [
            "docker", "run", "--privileged", "--rm",
            "tonistiigi/binfmt", "--install", "arm64",
        ],
        check=False,
    ).returncode

    if rc == 0:
        console.print("[green]QEMU ARM64 binfmt installed successfully.[/green]")
    else:
        console.print("[red]Failed to install QEMU binfmt. Try running with sudo.[/red]")
        raise typer.Exit(1)
