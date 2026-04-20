"""p4n4-emu status — show active profile and container health."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from p4n4_emu.profiles.loader import load_profile
from p4n4_emu.utils import compose as dc
from p4n4_emu.utils.docker_info import detect_block_device

console = Console()

_STACKS = ("iot", "ai", "edge")
_OVERLAY_ROOT = Path.home() / ".p4n4-emu" / "overlays"


def cmd(
    profile: Annotated[
        str, typer.Option("--profile", "-p", help="Profile to display.")
    ] = "rpi5",
    stack: Annotated[
        str, typer.Option("--stack", "-s", help="Stack to query: iot, ai, edge, all.")
    ] = "iot",
    stack_dir: Annotated[
        Path | None,
        typer.Option("--stack-dir", help="Directory containing docker-compose.yml."),
    ] = None,
) -> None:
    """Print active profile summary and container status."""
    try:
        prof = load_profile(profile)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    blkio = detect_block_device()

    ptable = Table(title=f"Profile: {prof.name}", show_lines=False)
    ptable.add_column("Setting")
    ptable.add_column("Value")
    ptable.add_row("Description", prof.description)
    ptable.add_row("Architecture", prof.arch)
    ptable.add_row("CPU (max)", f"{prof.cpus} cores")
    ptable.add_row("Memory (max)", prof.memory)
    ptable.add_row("Swap", prof.memory_swap)
    ptable.add_row("Disk R/W", f"{prof.blkio_read_bps // 1_000_000} MB/s")
    ptable.add_row("Block device", blkio or "[dim]not detected[/dim]")
    console.print(ptable)

    stacks_to_check = list(_STACKS) if stack == "all" else [stack]
    for s in stacks_to_check:
        cwd = _resolve_stack_dir(stack_dir, s)
        if cwd is None:
            continue
        overlay_file = _OVERLAY_ROOT / profile / f"{s}.emu.yml"
        overlay = overlay_file if overlay_file.exists() else None
        services = dc.ps(cwd, overlay=overlay)
        if not services:
            continue

        ctable = Table(title=f"{s} stack", show_lines=False)
        ctable.add_column("Service", style="bold")
        ctable.add_column("Status")
        ctable.add_column("Health")
        ctable.add_column("Ports")

        for svc in services:
            name = svc.get("Service") or svc.get("Name", "?")
            state = svc.get("State", "?")
            health = svc.get("Health", "")
            ports_raw = svc.get("Publishers") or []
            ports: list[str] = []
            for p in (ports_raw if isinstance(ports_raw, list) else []):
                pub = p.get("PublishedPort", 0)
                tgt = p.get("TargetPort", 0)
                proto = p.get("Protocol", "tcp")
                if pub:
                    ports.append(f"{pub}→{tgt}/{proto}")

            state_fmt = (
                f"[green]{state}[/green]" if state == "running"
                else f"[red]{state}[/red]" if state == "exited"
                else state
            )
            health_fmt = (
                f"[green]{health}[/green]" if health == "healthy"
                else f"[yellow]{health}[/yellow]" if health in ("starting", "unhealthy")
                else health
            )
            ctable.add_row(name, state_fmt, health_fmt, ", ".join(ports))

        console.print(ctable)


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
