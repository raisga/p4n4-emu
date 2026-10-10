"""p4n4-emu run — run a Python script against the emulated board's hardware."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

import p4n4_emu
from p4n4_emu.hw.mqtt_bridge import parse_address
from p4n4_emu.hw.readings import DEFAULT_DEVICE, Readings
from p4n4_emu.profiles.loader import list_profiles, load_profile
from p4n4_emu.sim.scenario import ScenarioError

console = Console(stderr=True)

# Holds the sitecustomize the script's interpreter loads at startup (it's put first
# on PYTHONPATH): it installs the stubs before the script imports anything
SITE_DIR = Path(__file__).resolve().parent.parent / "hw" / "site"


def cmd(
    ctx: typer.Context,
    script: Annotated[
        str,
        typer.Argument(
            help="Script to run, then its arguments. Everything after it goes to the "
            "interpreter as given, so `-m module` works too.",
            show_default=False,
        ),
    ],
    profile: Annotated[
        str,
        typer.Option("--profile", "-p", help="Hardware profile: its board (rpi4, rpi5)."),
    ] = "rpi5",
    device: Annotated[
        str | None,
        typer.Option(
            "--device",
            help="Simulator device whose curves the sensors follow "
            "(default: emu-sensor-0, or the scenario's first device).",
        ),
    ] = None,
    scenario: Annotated[
        Path | None,
        typer.Option("--scenario", help="Simulator scenario that gives the device's waves."),
    ] = None,
    gpio_mqtt: Annotated[
        str | None,
        typer.Option(
            "--gpio-mqtt",
            metavar="HOST[:PORT]",
            help="Broker for GPIO control: drive inputs on emu/gpio/<pin>/set, "
            "watch emu/gpio/<pin>/state.",
        ),
    ] = None,
    gpio_prefix: Annotated[
        str, typer.Option("--gpio-prefix", help="Topic prefix of the GPIO control channel.")
    ] = "emu/gpio",
    no_parts: Annotated[
        bool,
        typer.Option(
            "--no-parts", help="Leave the buses empty (no BME280, MPU-6050, ADS1115, ...)."
        ),
    ] = False,
    python: Annotated[
        str | None,
        typer.Option(
            "--python",
            help="Interpreter to run the script with (default: the one p4n4-emu runs on). "
            "Use the script's own virtualenv when it needs packages p4n4-emu doesn't have.",
        ),
    ] = None,
) -> None:
    """Run a script with RPi.GPIO, lgpio, gpiod, gpiozero, smbus2, spidev and pyserial
    replaced by the emulated board's.

    Sensors on the buses follow the sensor simulator's curves for one device, so
    the script reads what the simulator publishes for it.
    """
    if profile not in list_profiles():
        console.print(f"[red]Unknown profile {profile!r}.[/red] Available: {list_profiles()}")
        raise typer.Exit(2)
    board = load_profile(profile).board
    if board is None:
        console.print(
            f"[red]Profile {profile!r} has no GPIO header to emulate.[/red] "
            "Use a Raspberry Pi profile (rpi4, rpi5)."
        )
        raise typer.Exit(2)
    # Check what the script's interpreter would fail on, where the error is readable
    try:
        if scenario is not None:
            readings = Readings.from_scenario(scenario, device)
        else:
            readings = Readings(device or DEFAULT_DEVICE)
        if gpio_mqtt:
            parse_address(gpio_mqtt)
    except (ScenarioError, ValueError) as e:
        console.print(f"[red]Error:[/red] {e}")
        raise typer.Exit(2) from None
    interpreter = python or sys.executable
    if shutil.which(interpreter) is None and not Path(interpreter).is_file():
        console.print(f"[red]Interpreter not found:[/red] {interpreter}")
        raise typer.Exit(2)

    env = run_env(
        board=board,
        readings=readings,
        gpio_mqtt=gpio_mqtt,
        gpio_prefix=gpio_prefix,
        parts=not no_parts,
    )
    # The script takes over this process: signals and the exit status are its own
    os.execvpe(interpreter, [interpreter, script, *ctx.args], env)


def run_env(
    *,
    board: str,
    readings: Readings,
    gpio_mqtt: str | None,
    gpio_prefix: str,
    parts: bool,
) -> dict[str, str]:
    """The script's environment: the sitecustomize first on PYTHONPATH, and its settings."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(SITE_DIR), env.get("PYTHONPATH")) if p)
    env["P4N4_EMU_PATH"] = str(Path(p4n4_emu.__file__).resolve().parent.parent)
    env["P4N4_EMU_BOARD"] = board
    env["P4N4_EMU_PARTS"] = "1" if parts else "0"
    env["P4N4_EMU_GPIO_PREFIX"] = gpio_prefix
    # The device's waves, so the script's interpreter needn't read the scenario's YAML
    env["P4N4_EMU_READINGS"] = readings.to_json()
    if gpio_mqtt:
        env["P4N4_EMU_GPIO_MQTT"] = gpio_mqtt
    else:
        env.pop("P4N4_EMU_GPIO_MQTT", None)
    return env
