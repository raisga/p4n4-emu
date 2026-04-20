"""p4n4-emu profile — list and inspect hardware profiles."""

from __future__ import annotations

from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from p4n4_emu.profiles.loader import list_profiles, load_profile

app = typer.Typer(help="Manage hardware profiles.", no_args_is_help=True)
console = Console()


@app.command("list")
def list_cmd() -> None:
    """List all available hardware profiles."""
    table = Table(title="Available profiles", show_lines=False)
    table.add_column("Name", style="bold")
    table.add_column("Description")
    table.add_column("Arch")
    table.add_column("CPU")
    table.add_column("Memory")
    table.add_column("Disk R/W")

    for name in list_profiles():
        p = load_profile(name)
        table.add_row(
            p.name,
            p.description,
            p.arch,
            f"{p.cpus} cores",
            p.memory,
            f"{p.blkio_read_bps // 1_000_000} MB/s",
        )

    console.print(table)


@app.command("show")
def show_cmd(
    name: Annotated[str, typer.Argument(help="Profile name.")],
) -> None:
    """Show detailed information for a profile."""
    try:
        p = load_profile(name)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    table = Table(title=f"Profile: {p.name}", show_lines=False)
    table.add_column("Field")
    table.add_column("Value")

    rows = [
        ("Name", p.name),
        ("Description", p.description),
        ("Architecture", p.arch),
        ("CPU cores (max)", str(p.cpus)),
        ("Memory limit", p.memory),
        ("Swap limit", p.memory_swap),
        ("Memory (bytes)", str(p.memory_bytes)),
        ("Memory (MB)", str(p.memory_mb)),
        ("blkio weight", str(p.blkio_weight)),
        ("Read BPS", f"{p.blkio_read_bps:,} B/s ({p.blkio_read_bps // 1_000_000} MB/s)"),
        ("Write BPS", f"{p.blkio_write_bps:,} B/s ({p.blkio_write_bps // 1_000_000} MB/s)"),
        ("ARM", str(p.is_arm)),
    ]
    for field, value in rows:
        table.add_row(field, value)

    console.print(table)
