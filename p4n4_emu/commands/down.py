"""p4n4-emu down — stop emulator-constrained stack(s)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from p4n4_emu.utils import compose as dc
from p4n4_emu.utils.project import expand_stacks, resolve_stack_dir

console = Console()

_OVERLAY_ROOT = Path.home() / ".p4n4-emu" / "overlays"


def cmd(
    profile: Annotated[
        str, typer.Option("--profile", "-p", help="Profile name (used to locate overlay).")
    ] = "rpi5",
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
        typer.Option("--stack-dir", help="Directory containing docker-compose.yml."),
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
                f"[yellow]Cannot find docker-compose.yml for stack {s!r} — skipping.[/yellow]"
            )
            continue

        overlay_file = _OVERLAY_ROOT / profile / f"{s}.emu.yml"
        overlay = overlay_file if overlay_file.exists() else None

        console.print(f"[cyan]Stopping {s} stack...[/cyan]")
        rc = dc.down(cwd, overlay=overlay, volumes=volumes)
        if rc != 0:
            console.print(f"[red]Failed to stop {s} stack (exit {rc}).[/red]")

    subprocess.run(["docker", "rm", "-f", "p4n4-sensor-sim"], capture_output=True, check=False)
