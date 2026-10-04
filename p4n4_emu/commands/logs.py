"""p4n4-emu logs — show logs from emulated stack(s)."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from p4n4_emu.overlays.paths import existing_overlay
from p4n4_emu.utils import compose as dc
from p4n4_emu.utils.project import expand_stacks, resolve_stack_dir

console = Console()


def cmd(
    service: Annotated[str | None, typer.Argument(help="Service to show.")] = None,
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
            help="Stack(s) to read: iot, ai, edge, comma-separated, or all. "
            "Default: the current p4n4 project's enabled stacks (or iot).",
        ),
    ] = None,
    stack_dir: Annotated[
        Path | None,
        typer.Option("--stack-dir", help="Directory containing the stack's compose file."),
    ] = None,
    tail: Annotated[int | None, typer.Option("--tail", help="Lines from end of log.")] = 100,
    no_follow: Annotated[bool, typer.Option("--no-follow", help="Print once and exit.")] = False,
) -> None:
    """Show logs from the services of emulated stack(s)."""
    dirs = []
    for s in expand_stacks(stack):
        cwd = resolve_stack_dir(stack_dir, s)
        if cwd is not None:
            dirs.append((s, cwd))
    if not dirs:
        console.print("[red]No compose file found for the selected stack(s).[/red]")
        raise typer.Exit(1)

    # A service lives in one stack: read only that one, so following it works
    if service is not None:
        dirs = [(s, cwd) for s, cwd in dirs if service in (dc.list_services(cwd) or [])]
        if not dirs:
            console.print(f"[red]No stack defines a service named {service!r}.[/red]")
            raise typer.Exit(1)

    # Following blocks, so it can only follow one stack
    if len(dirs) > 1 and not no_follow:
        console.print(
            "[red]Error:[/red] More than one stack is selected. "
            "Pass [bold]--stack <name>[/bold] to follow one stack's logs, "
            "or [bold]--no-follow[/bold] to print logs from all of them."
        )
        raise typer.Exit(1)

    for s, cwd in dirs:
        rc = dc.logs(
            cwd,
            overlay=existing_overlay(cwd, s),
            service=service,
            tail=tail,
            follow=not no_follow,
        )
        if rc != 0:
            raise typer.Exit(rc)
