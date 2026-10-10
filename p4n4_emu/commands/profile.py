"""p4n4-emu profile — list and inspect hardware profiles."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Annotated

import typer
import yaml
from rich.console import Console
from rich.table import Table

from p4n4_emu.commands.up import budget_stacks, stack_shares
from p4n4_emu.overlays.generator import budget_scale, docker_platform, render_overlay
from p4n4_emu.overlays.paths import META_KEY, existing_overlay, overlay_path, read_overlay
from p4n4_emu.profiles.loader import (
    PROFILE_KEYS,
    Profile,
    ProfileError,
    list_profiles,
    load_profile,
    load_profile_file,
    project_dir,
    search_dirs,
)
from p4n4_emu.utils import compose as dc
from p4n4_emu.utils.docker_info import detect_block_device
from p4n4_emu.utils.project import expand_stacks, resolve_stack_dir
from p4n4_emu.utils.usage import Limits, expected_limits

app = typer.Typer(help="Manage hardware profiles.", no_args_is_help=True)
console = Console()

JsonOption = Annotated[bool, typer.Option("--json", help="Print JSON instead of a table.")]


def _echo_json(data) -> None:
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False))


def _where(p: Profile) -> str:
    """Where a profile comes from: built-in, user (~/.p4n4-emu) or project."""
    if p.builtin:
        return "built-in"
    project = project_dir()
    if project is not None and p.source is not None and p.source.parent == project:
        return "project"
    return "user"


@app.command("list")
def list_cmd(as_json: JsonOption = False) -> None:
    """List all available hardware profiles (built-in, ~/.p4n4-emu/profiles, the project's)."""
    err = Console(stderr=True)
    profiles = []
    for name in list_profiles():
        try:
            profiles.append(load_profile(name))
        except ProfileError as e:
            err.print(f"[red]Skipping an invalid profile:[/red] {e}")
    if as_json:
        _echo_json([{**p.as_dict(), "from": _where(p)} for p in profiles])
        return
    table = Table(title="Available profiles", show_lines=False)
    table.add_column("Name", style="bold")
    table.add_column("Description")
    table.add_column("Arch")
    table.add_column("CPU")
    table.add_column("Memory")
    table.add_column("Disk R/W")
    table.add_column("From")

    for p in profiles:
        table.add_row(
            p.name,
            p.description,
            p.arch + (f" + {p.gpu} GPU" if p.gpu else ""),
            f"{p.cpus:g} core" + ("" if p.cpus == 1 else "s"),
            p.memory,
            f"{p.blkio_read_bps // 1_000_000} MB/s",
            _where(p),
        )

    console.print(table)


@app.command("validate")
def validate_cmd(
    targets: Annotated[
        list[str] | None,
        typer.Argument(
            help="Profile names or files to check. Default: every profile file p4n4-emu finds.",
            show_default=False,
        ),
    ] = None,
    as_json: JsonOption = False,
) -> None:
    """Check profile files against the schema: unknown or missing keys, invalid sizes."""
    files: list[Path] = []
    if targets:
        for t in targets:
            path = Path(t)
            if path.suffix in (".yml", ".yaml") or path.exists():
                files.append(path)
                continue
            found = [d / f"{t}{ext}" for d in search_dirs() for ext in (".yml", ".yaml")]
            found = [f for f in found if f.exists()]
            files += found or [path]
    else:
        files = [
            f for d in search_dirs() if d.is_dir()
            for f in sorted(d.glob("*.yml")) + sorted(d.glob("*.yaml"))
        ]

    results = []
    for f in files:
        try:
            load_profile_file(f)
            results.append({"file": str(f), "ok": True, "error": None})
        except ProfileError as e:
            results.append({"file": str(f), "ok": False, "error": str(e)})
    failed = [r for r in results if not r["ok"]]
    if as_json:
        _echo_json(results)
    else:
        for r in results:
            if r["ok"]:
                console.print(f"[green]OK[/green]    {r['file']}", soft_wrap=True)
            else:
                console.print(f"[red]FAIL[/red]  {r['error']}", soft_wrap=True)
        if failed:
            keys = ", ".join(PROFILE_KEYS)
            console.print(f"\n[dim]Profile keys: {keys}. See p4n4-emu profile show rpi5.[/dim]")
    if failed:
        raise typer.Exit(1)


@app.command("show")
def show_cmd(
    name: Annotated[str, typer.Argument(help="Profile name.")],
    as_json: JsonOption = False,
) -> None:
    """Show detailed information for a profile."""
    try:
        p = load_profile(name)
    except ValueError as e:
        Console(stderr=True).print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e
    if as_json:
        _echo_json(p.as_dict())
        return

    table = Table(title=f"Profile: {p.name}", show_lines=False)
    table.add_column("Field")
    table.add_column("Value")

    rows = [
        ("Name", p.name),
        ("Description", p.description),
        ("Architecture", p.arch),
        ("Board (GPIO)", p.board or "none"),
        ("GPU", p.gpu or "none"),
        ("CPU cores (max)", str(p.cpus)),
        ("Memory limit", p.memory),
        ("Swap limit", p.memory_swap),
        ("Memory (bytes)", str(p.memory_bytes)),
        ("Memory (MB)", str(p.memory_mb)),
        ("blkio weight", str(p.blkio_weight)),
        ("Read BPS", f"{p.blkio_read_bps:,} B/s ({p.blkio_read_bps // 1_000_000} MB/s)"),
        ("Write BPS", f"{p.blkio_write_bps:,} B/s ({p.blkio_write_bps // 1_000_000} MB/s)"),
        ("ARM", str(p.is_arm)),
        ("From", _where(p) if p.builtin else f"{_where(p)} ({p.source})"),
    ]
    for field, value in rows:
        table.add_row(field, value)

    console.print(table)


@app.command("switch")
def switch_cmd(
    name: Annotated[str, typer.Argument(help="Profile to switch the running stacks to.")],
    stack: Annotated[
        str | None,
        typer.Option(
            "--stack",
            "-s",
            help="Stack(s) to switch: iot, ai, edge, comma-separated, or all. "
            "Default: the current p4n4 project's enabled stacks (or iot).",
        ),
    ] = None,
    stack_dir: Annotated[
        Path | None,
        typer.Option("--stack-dir", help="Directory containing the stack's compose file."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Print the docker update commands only.")
    ] = False,
) -> None:
    """Re-apply CPU and memory limits to running containers, without recreating them.

    `docker update` can't change disk rates or the image architecture: those follow
    on the next `p4n4-emu up`, which recreates the containers.
    """
    err = Console(stderr=True)
    try:
        prof = load_profile(name)
    except ValueError as e:
        err.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    running = []  # (stack, compose dir, current overlay)
    for s in expand_stacks(stack):
        cwd = resolve_stack_dir(stack_dir, s)
        overlay_file = existing_overlay(cwd, s) if cwd is not None else None
        if overlay_file is not None:
            running.append((s, cwd, read_overlay(overlay_file)))
    if not running:
        err.print(
            "[yellow]No stack runs under p4n4-emu.[/yellow] "
            f"Start one with: p4n4-emu up --profile {name}"
        )
        raise typer.Exit(1)

    # The architecture is fixed when a container is created
    wanted = docker_platform(prof.arch) if prof.is_arm else None
    for s, _, overlay in running:
        platform = _meta(overlay).get("platform")
        if platform and platform != wanted:
            err.print(
                f"[red]The {s} stack runs {platform} images, and {prof.name} is "
                f"{prof.arch}.[/red] Recreate it with: p4n4-emu up --profile {prof.name}"
            )
            raise typer.Exit(1)
        if wanted and not platform:
            console.print(
                f"[dim]The {s} stack keeps running native images; "
                f"p4n4-emu up --profile {prof.name} emulates {prof.arch}.[/dim]"
            )
        if bool(_meta(overlay).get("gpu")) != bool(prof.gpu):
            console.print(
                f"[dim]The {s} stack keeps its GPU reservations as they are; they follow "
                f"{prof.name} on the next p4n4-emu up.[/dim]"
            )

    shares = {s: stack_shares(s, stack_dir) for s in budget_stacks([s for s, _, _ in running])}
    scale = budget_scale(shares.values())
    blkio_device = detect_block_device()

    failed = False
    for s, cwd, overlay in running:
        content = render_overlay(
            prof,
            s,
            blkio_device,
            services=shares[s].keys(),
            platform=_meta(overlay).get("platform"),
            scale=scale,
            gpu=_meta(overlay).get("gpu"),
        )
        expected = expected_limits(yaml.safe_load(content))
        containers = dc.ps(cwd, overlay=existing_overlay(cwd, s))
        console.print(f"[cyan]Switching the {s} stack to [bold]{prof.name}[/bold]…[/cyan]")
        for c in containers:
            service, container = c.get("Service"), c.get("Name")
            lim = expected.get(service)
            if not container or lim is None:
                continue
            cmd = update_command(container, lim)
            if dry_run:
                console.print(f"[dim]Would run: {' '.join(cmd)}[/dim]")
                continue
            r = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if r.returncode != 0:
                failed = True
                err.print(f"[red]{container}:[/red] {(r.stderr or '').strip()}")
        if not dry_run:
            # Record the new profile, so status compares against it and down finds it
            overlay_path(cwd, s).write_text(content)

    if dry_run:
        return
    if failed:
        err.print(
            "[yellow]Some containers kept their old limits[/yellow] (memory below what "
            "they use now?). Run p4n4-emu status to see which."
        )
        raise typer.Exit(1)
    console.print(
        f"[green]Switched to {prof.name}.[/green] Disk I/O limits change on the next "
        "[bold]p4n4-emu up[/bold]."
    )


def update_command(container: str, lim: Limits) -> list[str]:
    """`docker update` for a container's CPU and memory limits (disk rates can't change)."""
    cmd = ["docker", "update"]
    if lim.cpus is not None:
        cmd += ["--cpus", f"{lim.cpus:.2f}"]
    if lim.memory is not None:
        cmd += ["--memory", str(lim.memory)]
        # Set together, so lowering memory never leaves swap below it
        cmd += ["--memory-swap", str(lim.memswap if lim.memswap else lim.memory)]
    return [*cmd, container]


def _meta(overlay: dict) -> dict:
    meta = overlay.get(META_KEY)
    return meta if isinstance(meta, dict) else {}
