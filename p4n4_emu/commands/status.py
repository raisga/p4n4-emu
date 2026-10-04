"""p4n4-emu status — show the active profile, container health and live usage against limits."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from p4n4_emu.overlays.paths import META_KEY, existing_overlay, read_overlay
from p4n4_emu.profiles.loader import Profile, load_profile
from p4n4_emu.utils import compose as dc
from p4n4_emu.utils.docker_info import detect_block_device
from p4n4_emu.utils.project import expand_stacks, resolve_stack_dir
from p4n4_emu.utils.usage import (
    Limits,
    Usage,
    applied_limits,
    cgroup_v2,
    differences,
    expected_limits,
    format_size,
    live_usage,
)

console = Console()

# Usage at or above this share of its limit is highlighted
_NEAR_LIMIT = 0.9


def cmd(
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            "-p",
            help="Profile to display. Default: the profile the stacks were started with.",
        ),
    ] = None,
    stack: Annotated[
        str | None,
        typer.Option(
            "--stack",
            "-s",
            help="Stack(s) to query: iot, ai, edge, comma-separated, or all. "
            "Default: the current p4n4 project's enabled stacks (or iot).",
        ),
    ] = None,
    stack_dir: Annotated[
        Path | None,
        typer.Option("--stack-dir", help="Directory containing the stack's compose file."),
    ] = None,
) -> None:
    """Show the active profile, and each service's usage against its limits."""
    stacks = []  # (stack, compose dir, overlay dict or None)
    for s in expand_stacks(stack):
        cwd = resolve_stack_dir(stack_dir, s)
        if cwd is None:
            continue
        overlay_file = existing_overlay(cwd, s)
        stacks.append((s, cwd, read_overlay(overlay_file) if overlay_file else None))

    active = {s: _profile_of(overlay) for s, _, overlay in stacks if overlay}
    shown = [profile] if profile else sorted(set(filter(None, active.values())))
    for name in shown:
        try:
            _print_profile(load_profile(name))
        except ValueError as e:
            console.print(f"[red]{e}[/red]")
            raise typer.Exit(1) from e
    if not active:
        console.print(
            "[dim]No stack is running under p4n4-emu. "
            "Start one with: p4n4-emu up --profile <name>[/dim]"
        )
    elif profile:
        for s, name in active.items():
            if name != profile:
                console.print(f"[yellow]The {s} stack runs under {name}, not {profile}.[/yellow]")

    containers = {s: dc.ps(cwd, overlay=existing_overlay(cwd, s)) for s, cwd, _ in stacks}
    names = [c["Name"] for svcs in containers.values() for c in svcs if c.get("Name")]
    running = [
        c["Name"] for svcs in containers.values() for c in svcs
        if c.get("Name") and c.get("State") == "running"
    ]
    applied = applied_limits(names)
    usage = live_usage(running)

    stale = False
    for s, _, overlay in stacks:
        if not containers[s]:
            continue
        expected = expected_limits(overlay) if overlay else None
        table, stack_stale = _stack_table(
            s, active.get(s), containers[s], expected, applied, usage
        )
        stale = stale or stack_stale
        console.print(table)

    if stale:
        console.print(
            "[yellow]Some containers don't have the limits of their overlay.[/yellow] "
            "Run [bold]p4n4-emu up[/bold] again to recreate them."
        )
    if active and not cgroup_v2():
        console.print(
            "[yellow]Warning:[/yellow] cgroup v2 not detected: the limits are set on the "
            "containers, but the kernel doesn't enforce them."
        )


def _profile_of(overlay: dict) -> str | None:
    meta = overlay.get(META_KEY)
    return meta.get("profile") if isinstance(meta, dict) else None


def _print_profile(prof: Profile) -> None:
    blkio = detect_block_device()
    table = Table(title=f"Profile: {prof.name}", show_lines=False)
    table.add_column("Setting")
    table.add_column("Value")
    table.add_row("Description", prof.description)
    table.add_row("Architecture", prof.arch)
    table.add_row("CPU (max)", f"{prof.cpus} cores")
    table.add_row("Memory (max)", prof.memory)
    table.add_row("Swap", prof.memory_swap)
    table.add_row("Disk R/W", f"{prof.blkio_read_bps // 1_000_000} MB/s")
    table.add_row("Block device", blkio or "[dim]not detected[/dim]")
    console.print(table)


def _near(used: float, limit: float | None) -> bool:
    return bool(limit) and used >= _NEAR_LIMIT * limit


def _usage_cells(use: Usage | None, lim: Limits) -> tuple[str, str]:
    """CPU and memory cells: "used / limit", highlighted near the limit."""
    if use is None:
        return "", ""
    cpu_limit = f"{lim.cpus:.2f}" if lim.cpus else "–"
    cpu = f"{use.cpus:.2f} / {cpu_limit}"
    mem_limit = format_size(lim.memory) if lim.memory else "–"
    mem = f"{format_size(use.memory)} / {mem_limit}"
    if _near(use.cpus, lim.cpus):
        cpu = f"[yellow]{cpu}[/yellow]"
    if _near(use.memory, lim.memory):
        mem = f"[yellow]{mem}[/yellow]"
    return cpu, mem


def _limits_cell(
    service: str, expected: dict[str, Limits] | None, lim: Limits
) -> tuple[str, bool]:
    """Whether the container has the limits its overlay asks for, and if they're stale."""
    if expected is None:
        return ("[dim]none[/dim]" if lim == Limits() else "[dim]not from p4n4-emu[/dim]"), False
    if service not in expected:
        # Added to the compose file after `up` wrote the overlay
        return "[yellow]none: not in overlay[/yellow]", True
    diff = differences(expected[service], lim)
    if diff:
        return "[red]stale:[/red] " + "; ".join(diff), True
    return "[green]applied[/green]", False


def _stack_table(
    stack: str,
    profile: str | None,
    services: list[dict],
    expected: dict[str, Limits] | None,
    applied: dict[str, Limits],
    usage: dict[str, Usage],
) -> tuple[Table, bool]:
    """One stack's table, and whether any container's limits are stale."""
    title = f"{stack} stack — {profile}" if profile else f"{stack} stack — not under p4n4-emu"
    table = Table(title=title, show_lines=False)
    table.add_column("Service", style="bold")
    table.add_column("Status")
    table.add_column("Health")
    table.add_column("CPU (cores)", justify="right")
    table.add_column("Memory", justify="right")
    table.add_column("Limits")
    table.add_column("Ports")

    stale = False
    for svc in services:
        name = svc.get("Service") or svc.get("Name", "?")
        container = svc.get("Name", "")
        state = svc.get("State", "?")
        health = svc.get("Health", "")
        lim = applied.get(container, Limits())

        cpu, mem = _usage_cells(usage.get(container), lim)
        limits, svc_stale = _limits_cell(name, expected, lim)
        stale = stale or svc_stale

        state_fmt = (
            f"[green]{state}[/green]"
            if state == "running"
            else f"[red]{state}[/red]"
            if state == "exited"
            else state
        )
        health_fmt = (
            f"[green]{health}[/green]"
            if health == "healthy"
            else f"[yellow]{health}[/yellow]"
            if health in ("starting", "unhealthy")
            else health
        )
        table.add_row(name, state_fmt, health_fmt, cpu, mem, limits, _ports(svc))
    return table, stale


def _ports(svc: dict) -> str:
    ports_raw = svc.get("Publishers") or []
    ports: list[str] = []
    for p in ports_raw if isinstance(ports_raw, list) else []:
        pub = p.get("PublishedPort", 0)
        tgt = p.get("TargetPort", 0)
        proto = p.get("Protocol", "tcp")
        port = f"{pub}→{tgt}/{proto}"
        # Docker lists a port once per address family (0.0.0.0 and ::)
        if pub and port not in ports:
            ports.append(port)
    return ", ".join(ports)
