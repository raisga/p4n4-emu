"""p4n4-emu up — apply a hardware profile and start stack(s)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from p4n4_emu.commands.sim import start_simulator
from p4n4_emu.overlays.generator import (
    Scale,
    Share,
    budget_scale,
    docker_platform,
    render_overlay,
    service_shares,
)
from p4n4_emu.overlays.paths import overlay_path
from p4n4_emu.profiles.loader import Profile, load_profile
from p4n4_emu.utils import compose as dc
from p4n4_emu.utils.docker_info import detect_block_device
from p4n4_emu.utils.preflight import check_or_exit, needs_qemu
from p4n4_emu.utils.project import (
    STACKS,
    expand_stacks,
    find_manifest,
    manifest_layers,
    resolve_stack_dir,
)

console = Console()

def resolve_platform(prof: Profile, arch: str, native: bool) -> str | None:
    """Docker platform to force on every service, or None to run natively.

    --native wins; otherwise --arch overrides the profile, and ARM profiles
    emulate their own architecture.
    """
    if native:
        return None
    if arch:
        return docker_platform(arch)
    return docker_platform(prof.arch) if prof.is_arm else None


def _stack_shares(stack: str, stack_dir: Path | None) -> dict[str, Share]:
    """Shares for the services actually defined in *stack*'s compose config."""
    cwd = resolve_stack_dir(stack_dir, stack)
    services = dc.list_services(cwd) if cwd is not None else None
    return service_shares(stack, services)


def _budget_stacks(stacks_to_run: list[str]) -> list[str]:
    """Stacks that share the device: the ones starting now plus the project's enabled ones."""
    manifest = find_manifest()
    enabled = manifest_layers(manifest) if manifest else []
    return [s for s in STACKS if s in stacks_to_run or s in enabled]


def cmd(
    profile: Annotated[
        str, typer.Option("--profile", "-p", help="Hardware profile name.")
    ] = "rpi5",
    stack: Annotated[
        str | None,
        typer.Option(
            "--stack",
            "-s",
            help="Stack(s): iot, ai, edge, comma-separated, or all. "
            "Default: the current p4n4 project's enabled stacks (or iot).",
        ),
    ] = None,
    stack_dir: Annotated[
        Path | None,
        typer.Option("--stack-dir", help="Dir containing the stack's compose file."),
    ] = None,
    arch: Annotated[
        str,
        typer.Option(
            "--arch",
            help="Architecture to emulate, e.g. arm64 or armv7. "
            "Default: the profile's architecture when it is ARM.",
        ),
    ] = "",
    native: Annotated[
        bool,
        typer.Option(
            "--native",
            help="Run images for the host architecture; only apply resource limits.",
        ),
    ] = False,
    sim: Annotated[bool, typer.Option("--sim", help="Also start the sensor simulator.")] = False,
    sim_interval: Annotated[
        float, typer.Option("--sim-interval", help="Simulator publish interval in seconds.")
    ] = 2.0,
    sim_devices: Annotated[
        int, typer.Option("--sim-devices", help="Number of simulated sensor devices.")
    ] = 1,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print commands without executing.")
    ] = False,
) -> None:
    """Start p4n4 stack(s) constrained to a hardware profile."""
    if native and arch:
        console.print("[red]--native and --arch are mutually exclusive.[/red]")
        raise typer.Exit(1)

    try:
        prof = load_profile(profile)
        platform = resolve_platform(prof, arch, native)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    check_or_exit(require_qemu=needs_qemu(platform), platform=platform or "linux/arm64")

    blkio_device = detect_block_device()
    if blkio_device:
        console.print(f"[dim]Block device detected:[/dim] {blkio_device}")
    else:
        console.print(
            "[yellow]Warning:[/yellow] No block device detected — disk I/O limits will be skipped."
        )

    stacks_to_run = expand_stacks(stack)
    for s in stacks_to_run:
        if s not in STACKS:
            console.print(
                f"[red]Unknown stack:[/red] {s!r}. Choose from: {', '.join(STACKS)} or all."
            )
            raise typer.Exit(1)

    # All stacks of the project run on one device, so their combined limits must
    # fit within the profile even when they are started one at a time.
    shares = {s: _stack_shares(s, stack_dir) for s in _budget_stacks(stacks_to_run)}
    scale = budget_scale(shares.values())

    for s in stacks_to_run:
        cwd = resolve_stack_dir(stack_dir, s)
        if cwd is None:
            console.print(
                f"[red]Cannot find a compose file for stack {s!r}.[/red] "
                "Run inside a p4n4 project, or use --stack-dir."
            )
            raise typer.Exit(1)

        overlay_content = render_overlay(
            prof,
            s,
            blkio_device,
            services=shares[s].keys(),
            platform=platform,
            scale=scale,
        )
        overlay_file = overlay_path(cwd, s)

        if dry_run:
            console.print(f"\n[bold]--- Overlay for {s} ---[/bold]")
            console.print(overlay_content)
            command = " ".join(dc.compose_cmd(cwd, overlay_file))
            console.print(f"\n[dim]Would run in {cwd}: {command} up -d[/dim]")
            continue

        overlay_file.parent.mkdir(parents=True, exist_ok=True)
        overlay_file.write_text(overlay_content)
        console.print(f"[cyan]Starting {s} stack[/cyan] with profile [bold]{prof.name}[/bold]…")
        rc = dc.up(cwd, overlay=overlay_file)
        if rc != 0:
            raise typer.Exit(rc)

    if sim and not dry_run:
        rc = start_simulator(interval=sim_interval, devices=sim_devices)
        if rc != 0:
            raise typer.Exit(rc)

    if not dry_run:
        _print_summary(prof, blkio_device, stacks_to_run, platform, scale)


def _print_summary(
    prof: Profile,
    blkio_device: str | None,
    stacks: list[str],
    platform: str | None,
    scale: Scale,
) -> None:
    table = Table(title=f"p4n4-emu active — profile: {prof.name}", show_lines=False)
    table.add_column("Setting")
    table.add_column("Value")
    table.add_row("Profile", f"{prof.name} — {prof.description}")
    table.add_row("CPU limit", f"{prof.cpus} cores")
    table.add_row("Memory limit", prof.memory)
    if scale != Scale():
        table.add_row(
            "Budget scaling",
            f"CPU ×{scale.cpu:.2f}, memory ×{scale.memory:.2f} (stacks share one device)",
        )
    disk_io = (
        f"{prof.blkio_read_bps // 1_000_000} MB/s" if blkio_device else "no limit (no block device)"
    )
    table.add_row("Disk I/O", disk_io)
    table.add_row("Platform", platform or "native")
    table.add_row("Stacks", ", ".join(stacks))
    console.print(table)
