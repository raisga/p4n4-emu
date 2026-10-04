"""p4n4-emu down — stop emulator-constrained stack(s)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from p4n4_emu.overlays.paths import existing_overlay, remove_overlay
from p4n4_emu.utils import compose as dc
from p4n4_emu.utils.project import expand_stacks, resolve_stack_dir

console = Console()


def cmd(
    profile: Annotated[
        str | None,
        typer.Option(
            "--profile",
            "-p",
            hidden=True,
            help="Ignored: the overlay `up` wrote records the profile.",
        ),
    ] = None,
    stack: Annotated[
        str | None,
        typer.Option(
            "--stack",
            "-s",
            help="Stack(s) to stop: iot, ai, edge, comma-separated, or all. "
            "Default: the current p4n4 project's enabled stacks (or iot).",
        ),
    ] = None,
    stack_dir: Annotated[
        Path | None,
        typer.Option("--stack-dir", help="Directory containing the stack's compose file."),
    ] = None,
    volumes: Annotated[
        bool,
        typer.Option("--volumes", "-v", help="Also remove persistent data volumes."),
    ] = False,
) -> None:
    """Stop p4n4 stack(s) and remove the resource-limit overlay."""
    if volumes:
        confirmed = typer.confirm(
            "This will delete all persistent volumes (data loss). Continue?",
            default=False,
        )
        if not confirmed:
            raise typer.Abort()

    # Reverse dependency order: dependents stop before iot removes p4n4-net
    stacks_to_stop = list(reversed(expand_stacks(stack)))

    for s in stacks_to_stop:
        cwd = resolve_stack_dir(stack_dir, s)
        if cwd is None:
            console.print(
                f"[yellow]Cannot find a compose file for stack {s!r} — skipping.[/yellow]"
            )
            continue

        overlay = existing_overlay(cwd, s)

        console.print(f"[cyan]Stopping {s} stack...[/cyan]")
        rc = dc.down(cwd, overlay=overlay, volumes=volumes)
        if rc != 0:
            console.print(f"[red]Failed to stop {s} stack (exit {rc}).[/red]")
            continue
        # The stack no longer runs under a profile
        remove_overlay(cwd, s)

    # The simulator publishes to the iot broker, so it goes down with iot only
    if "iot" in stacks_to_stop:
        subprocess.run(["docker", "rm", "-f", "p4n4-sensor-sim"], capture_output=True, check=False)
