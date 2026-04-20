"""p4n4-emu CLI — entrypoint and command registration."""

from __future__ import annotations

import typer
from rich.console import Console

from p4n4_emu import __version__
from p4n4_emu.commands import down, profile, setup, sim, status, up

app = typer.Typer(
    name="p4n4-emu",
    help="Sandbox emulator for p4n4 IoT / Edge AI stacks.",
    add_completion=True,
    no_args_is_help=True,
)
console = Console()

# ── sub-apps ──────────────────────────────────────────────────────────────────
app.add_typer(profile.app, name="profile")
app.add_typer(sim.app, name="sim")

# ── top-level commands ────────────────────────────────────────────────────────
app.command("setup")(setup.cmd)
app.command("up")(up.cmd)
app.command("down")(down.cmd)
app.command("status")(status.cmd)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"p4n4-emu version [bold]{__version__}[/bold]")
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(  # noqa: FBT001
        False,
        "--version",
        "-V",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """p4n4-emu — Hardware sandbox for p4n4 IoT platform development."""
