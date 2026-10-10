"""p4n4-emu setup — preflight checks and QEMU binfmt installation."""

from __future__ import annotations

import subprocess
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from p4n4_emu.overlays.generator import docker_platform
from p4n4_emu.utils.preflight import needs_qemu, qemu_handler, run_preflight

console = Console()


def cmd(
    arch: Annotated[
        str,
        typer.Option("--arch", help="Enable architecture emulation (e.g. arm64, armv7)."),
    ] = "",
    check_only: Annotated[
        bool,
        typer.Option("--check-only", help="Only run checks; do not install anything."),
    ] = False,
) -> None:
    """Verify the environment and optionally install QEMU binfmt support."""
    try:
        platform = docker_platform(arch) if arch else None
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    if platform and not needs_qemu(platform):
        console.print(f"[dim]{platform} is native on this host — no emulation needed.[/dim]")
    require_qemu = needs_qemu(platform)

    issues = run_preflight(require_qemu=require_qemu, platform=platform or "linux/arm64")
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
        checks.append((f"QEMU binfmt ({platform})", qemu_ok))

    for name, passed in checks:
        status = "[green]OK[/green]" if passed else "[red]FAIL[/red]"
        table.add_row(name, status)

    console.print(table)

    for w in warnings:
        console.print(f"[yellow]Warning:[/yellow] {w[len('WARNING: '):]}")

    for e in errors:
        console.print(f"[red]Error:[/red] {e}")

    # A missing QEMU handler is what the install below fixes; anything else
    # (no Docker, old Compose) would make the install fail too.
    hard_errors = [e for e in errors if "QEMU" not in e]
    if hard_errors:
        raise typer.Exit(1)

    if not require_qemu:
        return
    if check_only:
        if errors:
            raise typer.Exit(1)
        return
    if not errors:
        # Registered already, or provided by Docker Desktop's VM
        console.print(f"[green]QEMU emulation for {platform} is available.[/green]")
        return
    _install_binfmt(platform)


# Runs privileged, so pinned to a tag and its digest (tonistiigi/binfmt:latest
# on 2026-10-04)
BINFMT_IMAGE = (
    "tonistiigi/binfmt:qemu-v10.2.3"
    "@sha256:400a4873b838d1b89194d982c45e5fb3cda4593fbfd7e08a02e76b03b21166f0"
)


def _install_binfmt(platform: str) -> None:
    binfmt, install_name = qemu_handler(platform)
    if binfmt.exists():
        console.print(f"[green]QEMU binfmt for {platform} already registered.[/green]")
        return

    console.print(f"[cyan]Installing QEMU binfmt for {platform}...[/cyan]")
    rc = subprocess.run(
        [
            "docker", "run", "--privileged", "--rm",
            BINFMT_IMAGE, "--install", install_name,
        ],
        check=False,
    ).returncode

    if rc == 0:
        console.print(f"[green]QEMU binfmt for {platform} installed successfully.[/green]")
    else:
        console.print(
            "[red]Failed to install QEMU binfmt.[/red] The installer runs as a privileged "
            "container: check that your Docker daemon allows `docker run --privileged` "
            "(rootless Docker and some Docker Desktop setups do not)."
        )
        raise typer.Exit(1)
