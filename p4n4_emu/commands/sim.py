"""p4n4-emu sim — manage the synthetic sensor simulator container."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from p4n4_emu.utils.project import resolve_stack_dir
from p4n4_emu.utils.stack_config import DEFAULT_BROKER_INFO, Broker, find_broker, load_config

app = typer.Typer(help="Manage the sensor data simulator.", no_args_is_help=True)
console = Console()

_SIM_IMAGE = "p4n4-sensor-sim"
_SIM_CONTAINER = "p4n4-sensor-sim"
_ROOT = Path(__file__).parent.parent.parent


def start_simulator(
    *,
    interval: float = 2.0,
    devices: int = 1,
    broker: Broker = DEFAULT_BROKER_INFO,
    rebuild: bool = False,
    broker_timeout: float = 60.0,
) -> int:
    """Build the image if needed, wait for the broker, and run the simulator.

    Returns the exit code of the failing step, or 0 on success.
    """
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
            return rc

    if not _wait_for_broker(broker.container, broker_timeout):
        console.print(
            f"[yellow]Warning:[/yellow] broker container {broker.container!r} is not ready after "
            f"{broker_timeout:.0f}s; the simulator will keep retrying until it is."
        )

    subprocess.run(["docker", "rm", "-f", _SIM_CONTAINER], capture_output=True, check=False)

    rc = subprocess.run(
        [
            "docker", "run", "-d",
            "--name", _SIM_CONTAINER,
            "--network", broker.network,
            # Restart if the broker drops or was not up yet; a clean stop exits 0
            "--restart", "on-failure",
            "-e", f"MQTT_HOST={broker.host}",
            "-e", f"SIM_INTERVAL_SEC={interval}",
            "-e", f"SIM_DEVICE_COUNT={devices}",
            _SIM_IMAGE,
        ],
        check=False,
    ).returncode

    if rc == 0:
        console.print(
            f"[green]Sensor simulator started:[/green] "
            f"{devices} device(s) → {broker.host} every {interval}s"
        )
    else:
        console.print("[red]Failed to start sensor simulator.[/red]")
    return rc


@app.command("start")
def start_cmd(
    interval: float = typer.Option(2.0, "--interval", help="Publish interval in seconds."),
    devices: int = typer.Option(1, "--devices", help="Number of simulated sensor devices."),
    mqtt_host: str | None = typer.Option(
        None,
        "--mqtt-host",
        help="Broker hostname. Default: the broker in the current project's iot stack.",
    ),
    network: str | None = typer.Option(
        None, "--network", help="Docker network to join. Default: the broker's network."
    ),
    rebuild: bool = typer.Option(False, "--rebuild", help="Force rebuild of the image."),
) -> None:
    """Start the sensor simulator container."""
    found = project_broker()
    target = Broker(
        host=mqtt_host or found.host,
        container=mqtt_host or found.container,
        network=network or found.network,
    )
    rc = start_simulator(interval=interval, devices=devices, broker=target, rebuild=rebuild)
    if rc != 0:
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


def project_broker(stack_dir: Path | None = None) -> Broker:
    """The broker of the current project's iot stack, or p4n4's defaults."""
    cwd = resolve_stack_dir(stack_dir, "iot")
    return find_broker(load_config(cwd)) if cwd is not None else DEFAULT_BROKER_INFO


def _image_exists() -> bool:
    r = subprocess.run(
        ["docker", "image", "inspect", _SIM_IMAGE],
        capture_output=True,
        check=False,
    )
    return r.returncode == 0


def _broker_state(container: str) -> str | None:
    """Health status if the container has a healthcheck, else its run state."""
    r = subprocess.run(
        [
            "docker", "inspect", container, "--format",
            "{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    return r.stdout.strip() if r.returncode == 0 else None


def _wait_for_broker(container: str, timeout: float, poll: float = 2.0) -> bool:
    """Wait until *container* is healthy (or running, without a healthcheck)."""
    deadline = time.monotonic() + timeout
    while True:
        if _broker_state(container) in ("healthy", "running"):
            return True
        if time.monotonic() >= deadline:
            return False
        console.print(f"[dim]Waiting for {container}...[/dim]")
        time.sleep(poll)
