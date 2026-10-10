"""p4n4-emu sim — manage the synthetic sensor simulator container."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from p4n4_emu import __version__
from p4n4_emu.sim.scenario import Scenario, ScenarioError, load_scenario
from p4n4_emu.utils.project import resolve_stack_dir
from p4n4_emu.utils.stack_config import DEFAULT_BROKER_INFO, Broker, find_broker, load_config

app = typer.Typer(help="Manage the sensor data simulator.", no_args_is_help=True)
console = Console()

# Published for linux/amd64 and linux/arm64 by .github/workflows/image.yml, one tag per
# release; P4N4_EMU_SIM_IMAGE points at another (a mirror, or a local build)
SIM_IMAGE = os.environ.get(
    "P4N4_EMU_SIM_IMAGE", f"ghcr.io/raisga/p4n4-sensor-sim:{__version__}"
)
_SIM_CONTAINER = "p4n4-sensor-sim"
_PACKAGE = Path(__file__).parent.parent


# Where the files `sim start` mounts appear inside the container
_MOUNT_DIR = "/etc/p4n4-sim"


@dataclass(frozen=True)
class Connection:
    """How the simulator logs in to the broker; None / False leaves the simulator's default."""

    port: int | None = None
    username: str | None = None
    password: str | None = None
    tls: bool = False
    ca_file: Path | None = None
    cert_file: Path | None = None
    key_file: Path | None = None


def start_simulator(
    *,
    interval: float | None = None,
    devices: int | None = None,
    scenario: Path | None = None,
    broker: Broker = DEFAULT_BROKER_INFO,
    connection: Connection = Connection(),
    qos: int | None = None,
    retain: bool | None = None,
    rebuild: bool = False,
    broker_timeout: float = 60.0,
) -> int:
    """Get the image if needed, wait for the broker, and run the simulator.

    Settings left as None use the scenario's, or the simulator's defaults. Returns the
    exit code of the failing step, or 0 on success.
    """
    loaded = None
    if scenario is not None:
        try:
            loaded = load_scenario(scenario)
        except ScenarioError as e:
            console.print(f"[red]{e}[/red]")
            return 1
        if devices is not None:
            console.print("[red]--devices can't be used with a scenario, which lists them.[/red]")
            return 1

    try:
        options, env = _run_options(interval, devices, scenario, broker, connection, qos, retain)
    except FileNotFoundError as e:
        console.print(f"[red]File not found:[/red] {e}")
        return 1

    rc = ensure_image(rebuild=rebuild)
    if rc != 0:
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
            *options,
            SIM_IMAGE,
        ],
        check=False,
        env=env,
    ).returncode

    if rc == 0:
        summary = _describe(loaded, devices, interval, broker)
        console.print(f"[green]Sensor simulator started:[/green] {summary}")
    else:
        console.print("[red]Failed to start sensor simulator.[/red]")
    return rc


def _run_options(
    interval: float | None,
    devices: int | None,
    scenario: Path | None,
    broker: Broker,
    connection: Connection,
    qos: int | None,
    retain: bool | None,
) -> tuple[list[str], dict[str, str] | None]:
    """`docker run` options for the simulator's settings, and the environment to run it in.

    The password reaches the container through the environment of `docker run`, so it
    never shows on a command line.
    """
    opts = ["-e", f"MQTT_HOST={broker.host}"]
    env = None

    def mount(path: Path, name: str, var: str) -> None:
        path = path.expanduser().resolve(strict=True)
        opts.extend(["-v", f"{path}:{_MOUNT_DIR}/{name}:ro", "-e", f"{var}={_MOUNT_DIR}/{name}"])

    if scenario is not None:
        mount(scenario, "scenario.yml", "SIM_SCENARIO")
    if interval is not None:
        opts.extend(["-e", f"SIM_INTERVAL_SEC={interval}"])
    if devices is not None:
        opts.extend(["-e", f"SIM_DEVICE_COUNT={devices}"])
    if qos is not None:
        opts.extend(["-e", f"SIM_QOS={qos}"])
    if retain is not None:
        opts.extend(["-e", f"SIM_RETAIN={int(retain)}"])
    if connection.port is not None:
        opts.extend(["-e", f"MQTT_PORT={connection.port}"])
    if connection.username:
        opts.extend(["-e", f"MQTT_USERNAME={connection.username}"])
    if connection.password:
        opts.extend(["-e", "MQTT_PASSWORD"])
        env = {**os.environ, "MQTT_PASSWORD": connection.password}
    if connection.tls or connection.ca_file:
        opts.extend(["-e", "MQTT_TLS=1"])
    if connection.ca_file:
        mount(connection.ca_file, "ca.crt", "MQTT_CA_FILE")
    if connection.cert_file:
        mount(connection.cert_file, "client.crt", "MQTT_CERT_FILE")
    if connection.key_file:
        mount(connection.key_file, "client.key", "MQTT_KEY_FILE")
    return opts, env


def _describe(
    scenario: Scenario | None, devices: int | None, interval: float | None, broker: Broker
) -> str:
    if scenario is None:
        return f"{devices or 1} device(s) → {broker.host} every {interval or 2.0}s"
    faults = sum(len(d.faults) for d in scenario.devices)
    return (
        f"{len(scenario.devices)} device(s) from the scenario → {broker.host}"
        + (f", {faults} fault rule(s)" if faults else "")
    )


@app.command("start")
def start_cmd(
    interval: float | None = typer.Option(
        None, "--interval", help="Publish interval in seconds. Default: 2, or the scenario's."
    ),
    devices: int | None = typer.Option(
        None, "--devices", help="Number of simulated devices, without a scenario. Default: 1."
    ),
    scenario: Path | None = typer.Option(
        None,
        "--scenario",
        help="YAML file listing devices, measurements and faults (see `sim check`).",
    ),
    mqtt_host: str | None = typer.Option(
        None,
        "--mqtt-host",
        help="Broker hostname. Default: the broker in the current project's iot stack.",
    ),
    network: str | None = typer.Option(
        None, "--network", help="Docker network to join. Default: the broker's network."
    ),
    port: int | None = typer.Option(
        None, "--port", help="Broker port. Default: 1883, or 8883 with TLS."
    ),
    username: str | None = typer.Option(
        None, "--username", envvar="MQTT_USERNAME", help="Broker username."
    ),
    password: str | None = typer.Option(
        None,
        "--password",
        envvar="MQTT_PASSWORD",
        show_envvar=True,
        help="Broker password; prefer the environment variable, which stays out of shell history.",
    ),
    tls: bool = typer.Option(
        False, "--tls", help="Connect over TLS, verified against the system CAs."
    ),
    ca_file: Path | None = typer.Option(
        None, "--ca-file", help="CA certificate to verify the broker with (implies --tls)."
    ),
    cert_file: Path | None = typer.Option(None, "--cert-file", help="Client certificate."),
    key_file: Path | None = typer.Option(None, "--key-file", help="Client certificate's key."),
    qos: int | None = typer.Option(
        None, "--qos", min=0, max=2, help="QoS of every publish. Default: 0, or the scenario's."
    ),
    retain: bool | None = typer.Option(
        None, "--retain/--no-retain", help="Publish with the retain flag. Default: the scenario's."
    ),
    rebuild: bool = typer.Option(False, "--rebuild", help="Force rebuild of the image."),
) -> None:
    """Start the sensor simulator container."""
    if (cert_file is None) != (key_file is None):
        console.print("[red]--cert-file and --key-file go together.[/red]")
        raise typer.Exit(1)
    found = project_broker()
    target = Broker(
        host=mqtt_host or found.host,
        container=mqtt_host or found.container,
        network=network or found.network,
    )
    connection = Connection(port, username, password, tls, ca_file, cert_file, key_file)
    rc = start_simulator(
        interval=interval,
        devices=devices,
        scenario=scenario,
        broker=target,
        connection=connection,
        qos=qos,
        retain=retain,
        rebuild=rebuild,
    )
    if rc != 0:
        raise typer.Exit(rc)


@app.command("check")
def check_cmd(
    scenario: Path = typer.Argument(..., help="Scenario file to check."),
) -> None:
    """Check a scenario file and list the devices it simulates."""
    try:
        loaded = load_scenario(scenario)
    except ScenarioError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    table = Table(title=f"Scenario: {scenario}", show_lines=False)
    table.add_column("Device")
    table.add_column("Every")
    table.add_column("Measurements")
    table.add_column("Faults")
    for d in loaded.devices:
        faults = ", ".join(
            f"{f.type}" + (f" ({f.measurement})" if f.measurement else "") for f in d.faults
        )
        table.add_row(d.id, f"{loaded.interval_of(d):g}s", ", ".join(d.measurements), faults or "—")
    console.print(table)
    flags = [f"QoS {loaded.qos}"]
    if loaded.retain:
        flags.append("retained")
    if loaded.timestamp:
        flags.append("timestamped")
    if loaded.seed is not None:
        flags.append(f"seed {loaded.seed}")
    console.print(f"[dim]{', '.join(flags)}[/dim]")


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


def ensure_image(*, rebuild: bool = False) -> int:
    """Make the simulator image available: the local one, else the published one, else a build.

    The build is the fallback for a version or platform that isn't published, and what
    --rebuild does, e.g. to try changes to the simulator itself.
    """
    if not rebuild:
        if _image_exists():
            return 0
        console.print(f"[cyan]Pulling {SIM_IMAGE}...[/cyan]")
        pulled = subprocess.run(["docker", "pull", SIM_IMAGE], capture_output=True, check=False)
        if pulled.returncode == 0:
            return 0
        console.print("[dim]Not published for this version or platform; building it.[/dim]")
    return _build_image()


def _build_image() -> int:
    """Build the image from the installed package, wheel or source checkout alike.

    The context is a copy of the package alone: the package's parent is site-packages
    when p4n4-emu is installed from a wheel, far too much to send to the daemon.
    """
    console.print(f"[cyan]Building {SIM_IMAGE}...[/cyan]")
    with tempfile.TemporaryDirectory(prefix="p4n4-emu-sim-") as context:
        package = Path(context) / "p4n4_emu"
        shutil.copytree(_PACKAGE, package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        rc = subprocess.run(
            ["docker", "build", "-t", SIM_IMAGE, "-f", str(package / "sim" / "Dockerfile"),
             context],
            check=False,
        ).returncode
    if rc != 0:
        console.print("[red]Failed to build the sensor-sim image.[/red]")
    return rc


def _image_exists() -> bool:
    r = subprocess.run(
        ["docker", "image", "inspect", SIM_IMAGE],
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
