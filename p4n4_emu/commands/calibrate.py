"""p4n4-emu calibrate — measure how fast a host core is, so CPU limits follow core speed."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from p4n4_emu.overlays.generator import docker_platform
from p4n4_emu.profiles.loader import ProfileError, list_profiles, load_profile
from p4n4_emu.utils import calibration
from p4n4_emu.utils.preflight import check_or_exit, host_platform, needs_qemu

console = Console()


def cmd(
    platform: Annotated[
        str,
        typer.Option(
            "--platform",
            help="Platform the stacks' images run: arm64 or armv7 measures them under QEMU. "
            "Default: the host's own.",
        ),
    ] = "",
    score: Annotated[
        float | None,
        typer.Option(
            "--score",
            min=0.01,
            help="Record this score (7-Zip MIPS of `7z b -mmt1`) instead of measuring.",
        ),
    ] = None,
    show: Annotated[
        bool, typer.Option("--show", help="Show the stored scores and each profile's factor.")
    ] = False,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of a table.")] = False,
) -> None:
    """Measure one host core with the single-thread 7-Zip benchmark, in a container.

    `up` then scales CPU limits by board core / host core, using the profile's
    cpu_score, so four host cores run about as fast as the board's four. Measure
    once per platform the stacks run: natively, and with --platform arm64 for
    images under QEMU, which is far slower.
    """
    err = Console(stderr=True)
    try:
        target = docker_platform(platform) if platform else host_platform()
    except ValueError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    if not show:
        if score is not None:
            calibration.save(target, score, "unknown", "manual")
        else:
            check_or_exit(require_qemu=needs_qemu(target), platform=target)
            if not as_json:
                console.print(
                    f"[cyan]Running 7-Zip's benchmark on one core[/cyan] "
                    f"({target}{', under QEMU: several minutes' if needs_qemu(target) else ''})…"
                )
            try:
                m = calibration.measure(target if needs_qemu(target) else None)
            except calibration.CalibrationError as e:
                err.print(f"[red]Calibration failed:[/red] {e}")
                raise typer.Exit(1) from e
            calibration.save(target, m.score, m.version, "measured")

    stored = calibration.load()
    factors = _factors(stored)
    if as_json:
        typer.echo(json.dumps({"scores": stored, "factors": factors}, indent=2))
        return
    if not stored:
        console.print("[yellow]Not calibrated yet.[/yellow] Run: p4n4-emu calibrate")
        return

    table = Table(title=f"Host core scores ({calibration.CALIBRATION_FILE})")
    for col in ("Platform", "MIPS", "7-Zip", "How", "When"):
        table.add_column(col, justify="right" if col == "MIPS" else "left")
    for plat, entry in sorted(stored.items()):
        table.add_row(
            plat, f"{entry['score']:g}", entry.get("7zip", ""),
            entry.get("source", ""), entry.get("date", ""),
        )
    console.print(table)

    table = Table(title="CPU factor per profile (board core / host core)")
    table.add_column("Profile", style="bold")
    table.add_column("Board core", justify="right")
    for plat in sorted(stored):
        table.add_column(plat, justify="right")
    for name, row in factors.items():
        table.add_row(
            name, f"{row['cpu_score']:g}",
            *(_factor_cell(row["factors"].get(plat)) for plat in sorted(stored)),
        )
    console.print(table)
    console.print(
        "[dim]1.000: the host core is slower, so limits stay core counts. "
        "Profiles without cpu_score aren't scaled.[/dim]"
    )


def _factors(stored: dict[str, dict]) -> dict[str, dict]:
    """Each profile with a cpu_score: its factor for every calibrated platform."""
    out = {}
    for name in list_profiles():
        try:
            prof = load_profile(name)
        except ProfileError:
            continue
        if prof.cpu_score is None:
            continue
        out[name] = {
            "cpu_score": prof.cpu_score,
            "factors": {
                plat: round(min(1.0, prof.cpu_score / entry["score"]), 3)
                for plat, entry in stored.items()
            },
        }
    return out


def _factor_cell(value: float | None) -> str:
    return "—" if value is None else f"{value:.3f}"
