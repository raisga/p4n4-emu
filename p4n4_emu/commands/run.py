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
from p4n4_emu.hw import layout
from p4n4_emu.hw.mqtt_bridge import parse_address
from p4n4_emu.hw.readings import DEFAULT_DEVICE, Readings
from p4n4_emu.hw.shims import GPIOD_APIS
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
        typer.Option(
            "--profile", "-p", help="Hardware profile: its board (rpi3, rpi4, rpi5, rpi-zero2w)."
        ),
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
    hardware: Annotated[
        Path | None,
        typer.Option(
            "--hardware",
            help="Board layout: which parts sit on which bus and address, and which "
            "measurements feed them (default: a BME280, MPU-6050 and ADS1115 on I2C 1, "
            "an MCP3008 on SPI 0.0, a DS18B20 on 1-Wire).",
        ),
    ] = None,
    no_parts: Annotated[
        bool,
        typer.Option(
            "--no-parts", help="Leave the buses empty (no BME280, MPU-6050, ADS1115, ...)."
        ),
    ] = False,
    gpiod: Annotated[
        str,
        typer.Option(
            "--gpiod",
            help="What `import gpiod` gives: v2 (libgpiod 2) or v1 (python3-libgpiod 1.6, "
            "Raspberry Pi OS bookworm's package).",
        ),
    ] = "v2",
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
    try:
        prof = load_profile(profile)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(2) from None
    if hardware is not None and no_parts:
        console.print("[red]--hardware and --no-parts are mutually exclusive.[/red]")
        raise typer.Exit(2)
    if gpiod not in GPIOD_APIS:
        console.print(f"[red]--gpiod: expected one of {', '.join(GPIOD_APIS)}.[/red]")
        raise typer.Exit(2)
    board = prof.board
    if board is None:
        console.print(
            f"[red]Profile {profile!r} has no GPIO header to emulate.[/red] "
            "Use a Raspberry Pi profile (rpi3, rpi4, rpi5, rpi-zero2w)."
        )
        raise typer.Exit(2)
    # Check what the script's interpreter would fail on, where the error is readable
    try:
        if scenario is not None:
            readings = Readings.from_scenario(scenario, device)
        else:
            readings = Readings(device or DEFAULT_DEVICE)
        parts = None
        if hardware is not None:
            parts = layout.load(hardware)
            layout.check_measurements(parts, readings)
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
        parts=parts if parts is not None else not no_parts,
        gpiod=gpiod,
    )
    # The script takes over this process: signals and the exit status are its own
    os.execvpe(interpreter, [interpreter, script, *ctx.args], env)


def run_env(
    *,
    board: str,
    readings: Readings,
    gpio_mqtt: str | None,
    gpio_prefix: str,
    parts: bool | tuple[layout.Part, ...],
    gpiod: str = "v2",
) -> dict[str, str]:
    """The script's environment: the sitecustomize first on PYTHONPATH, and its settings."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (str(SITE_DIR), env.get("PYTHONPATH")) if p)
    env["P4N4_EMU_PATH"] = str(Path(p4n4_emu.__file__).resolve().parent.parent)
    env["P4N4_EMU_BOARD"] = board
    env["P4N4_EMU_PARTS"] = "1" if parts else "0"
    if isinstance(parts, tuple):
        env["P4N4_EMU_HARDWARE"] = layout.to_json(parts)
    else:
        env.pop("P4N4_EMU_HARDWARE", None)
    env["P4N4_EMU_GPIOD"] = gpiod
    env["P4N4_EMU_GPIO_PREFIX"] = gpio_prefix
    # The device's waves, so the script's interpreter needn't read the scenario's YAML
    env["P4N4_EMU_READINGS"] = readings.to_json()
    if gpio_mqtt:
        env["P4N4_EMU_GPIO_MQTT"] = gpio_mqtt
    else:
        env.pop("P4N4_EMU_GPIO_MQTT", None)
    return env
