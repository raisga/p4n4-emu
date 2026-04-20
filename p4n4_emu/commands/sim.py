"""p4n4-emu sim — manage the synthetic sensor simulator container."""

from __future__ import annotations

import subprocess
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Manage the sensor data simulator.", no_args_is_help=True)
console = Console()

_SIM_IMAGE = "p4n4-sensor-sim"
_SIM_CONTAINER = "p4n4-sensor-sim"
_SIM_NETWORK = "p4n4-net"
_ROOT = Path(__file__).parent.parent.parent


@app.command("start")
def start_cmd(
    interval: float = typer.Option(2.0, "--interval", help="Publish interval in seconds."),
    devices: int = typer.Option(1, "--devices", help="Number of simulated sensor devices."),
    mqtt_host: str = typer.Option("p4n4-mqtt", "--mqtt-host", help="Mosquitto hostname."),
    rebuild: bool = typer.Option(False, "--rebuild", help="Force rebuild of the image."),
) -> None:
    """Start the sensor simulator container."""
    sim_dir = Path(__file__).parent.parent / "sim"

    if rebuild or not _image_exists():
        console.print(f"[cyan]Building {_SIM_IMAGE} image...[/cyan]")
        rc = subprocess.run(
            ["docker", "build", "-t", _SIM_IMAGE, "-f", str(sim_dir / "Dockerfile"), "."],
            cwd=str(_ROOT),
            check=False,
        ).returncode
        if rc != 0:
            console.print("[red]Failed to build sensor-sim image.[/red]")
            raise typer.Exit(rc)

    subprocess.run(["docker", "rm", "-f", _SIM_CONTAINER], capture_output=True, check=False)

    rc = subprocess.run(
        [
            "docker", "run", "-d",
            "--name", _SIM_CONTAINER,
            "--network", _SIM_NETWORK,
            "-e", f"MQTT_HOST={mqtt_host}",
            "-e", f"SIM_INTERVAL_SEC={interval}",
            "-e", f"SIM_DEVICE_COUNT={devices}",
            _SIM_IMAGE,
        ],
        check=False,
    ).returncode

    if rc == 0:
        console.print(
            f"[green]Sensor simulator started:[/green] "
            f"{devices} device(s) → {mqtt_host} every {interval}s"
        )
    else:
        console.print("[red]Failed to start sensor simulator.[/red]")
        raise typer.Exit(rc)


@app.command("stop")
def stop_cmd() -> None:
    """Stop the sensor simulator container."""
    rc = subprocess.run(
        ["docker", "rm", "-f", _SIM_CONTAINER],
        capture_output=True,
        check=False,
    ).returncode
    if rc == 0:
        console.print("[green]Sensor simulator stopped.[/green]")
    else:
        console.print("[yellow]Sensor simulator was not running.[/yellow]")


@app.command("status")
def status_cmd() -> None:
    """Show sensor simulator container status."""
    r = subprocess.run(
        ["docker", "inspect", _SIM_CONTAINER, "--format", "{{.State.Status}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        console.print("[yellow]Sensor simulator is not running.[/yellow]")
        return

    state = r.stdout.strip()
    table = Table(title="Sensor Simulator", show_lines=False)
    table.add_column("Container")
    table.add_column("Status")
    table.add_row(
        _SIM_CONTAINER,
        f"[green]{state}[/green]" if state == "running" else f"[yellow]{state}[/yellow]",
    )
    console.print(table)


def _image_exists() -> bool:
    r = subprocess.run(
        ["docker", "image", "inspect", _SIM_IMAGE],
        capture_output=True,
        check=False,
    )
    return r.returncode == 0
