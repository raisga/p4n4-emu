"""p4n4-emu up — apply a hardware profile and start stack(s)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from p4n4_emu.overlays.generator import render_overlay
from p4n4_emu.profiles.loader import load_profile
from p4n4_emu.utils import compose as dc
from p4n4_emu.utils.docker_info import detect_block_device
from p4n4_emu.utils.preflight import check_or_exit

console = Console()

_STACKS = ("iot", "ai", "edge")
_OVERLAY_ROOT = Path.home() / ".p4n4-emu" / "overlays"


def _overlay_path(profile_name: str, stack: str) -> Path:
    p = _OVERLAY_ROOT / profile_name / f"{stack}.emu.yml"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def cmd(
    profile: Annotated[
        str, typer.Option("--profile", "-p", help="Hardware profile name.")
    ] = "rpi5",
    stack: Annotated[
        str, typer.Option("--stack", "-s", help="Stack(s): iot, ai, edge, all.")
    ] = "iot",
    stack_dir: Annotated[
        Path | None,
        typer.Option("--stack-dir", help="Dir containing docker-compose.yml."),
    ] = None,
    arch: Annotated[
        str, typer.Option("--arch", help="Architecture emulation, e.g. arm64.")
    ] = "",
    sim: Annotated[
        bool, typer.Option("--sim", help="Also start the sensor simulator.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print commands without executing.")
    ] = False,
) -> None:
    """Start p4n4 stack(s) constrained to a hardware profile."""
    require_qemu = arch.lower() in ("arm64", "aarch64")
    check_or_exit(require_qemu=require_qemu)

    prof = load_profile(profile)

    blkio_device = detect_block_device()
    if blkio_device:
        console.print(f"[dim]Block device detected:[/dim] {blkio_device}")
    else:
        console.print(
            "[yellow]Warning:[/yellow] No block device detected — "
            "disk I/O limits will be skipped."
        )

    stacks_to_run = list(_STACKS) if stack == "all" else [stack]

    for s in stacks_to_run:
        if s not in _STACKS:
            console.print(
                f"[red]Unknown stack:[/red] {s!r}. "
                f"Choose from: {', '.join(_STACKS)} or all."
            )
            raise typer.Exit(1)

        cwd = _resolve_stack_dir(stack_dir, s)
        if cwd is None:
            console.print(
                f"[red]Cannot find docker-compose.yml for stack {s!r}.[/red] "
                "Use --stack-dir."
            )
            raise typer.Exit(1)

        overlay_content = render_overlay(prof, s, blkio_device)
        overlay_file = _overlay_path(prof.name, s)

        if dry_run:
            console.print(f"\n[bold]--- Overlay for {s} ---[/bold]")
            console.print(overlay_content)
            console.print(
                f"\n[dim]Would run: docker compose -f docker-compose.yml "
                f"-f {overlay_file} up -d[/dim]"
            )
            continue

        overlay_file.write_text(overlay_content)
        console.print(
            f"[cyan]Starting {s} stack[/cyan] with profile [bold]{prof.name}[/bold]…"
        )
        rc = dc.up(cwd, overlay=overlay_file)
        if rc != 0:
            raise typer.Exit(rc)

    if sim and not dry_run:
        _start_sim()

    if not dry_run:
        _print_summary(prof, blkio_device, stacks_to_run)


def _resolve_stack_dir(base: Path | None, stack: str) -> Path | None:
    cwd = Path.cwd()
    candidates = []
    if base is not None:
        candidates += [base, base / stack]
    candidates += [cwd, cwd / stack, cwd.parent / "docker" / stack]
    for c in candidates:
        if (c / "docker-compose.yml").exists():
            return c
    return None


def _start_sim() -> None:
    import subprocess

    console.print("[cyan]Starting sensor simulator...[/cyan]")
    sim_dir = Path(__file__).parent.parent / "sim"
    pkg_root = str(Path(__file__).parent.parent.parent)
    subprocess.Popen(
        ["docker", "build", "-t", "p4n4-sensor-sim",
         "-f", str(sim_dir / "Dockerfile"), "."],
        cwd=pkg_root,
    )
    subprocess.Popen([
        "docker", "run", "-d", "--rm",
        "--name", "p4n4-sensor-sim",
        "--network", "p4n4-net",
        "-e", "MQTT_HOST=p4n4-mqtt",
        "p4n4-sensor-sim",
    ])


def _print_summary(
    prof, blkio_device: str | None, stacks: list[str]
) -> None:
    table = Table(title=f"p4n4-emu active — profile: {prof.name}", show_lines=False)
    table.add_column("Setting")
    table.add_column("Value")
    table.add_row("Profile", f"{prof.name} — {prof.description}")
    table.add_row("CPU limit", f"{prof.cpus} cores")
    table.add_row("Memory limit", prof.memory)
    disk_io = (
        f"{prof.blkio_read_bps // 1_000_000} MB/s"
        if blkio_device
        else "no limit (no block device)"
    )
    table.add_row("Disk I/O", disk_io)
    table.add_row("Architecture", prof.arch)
    table.add_row("Stacks", ", ".join(stacks))
    console.print(table)
