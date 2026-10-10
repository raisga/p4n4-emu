"""Install the hardware stubs in place of the real libraries.

    from p4n4_emu.hw import shims
    shims.install(board="rpi5")
    import RPi.GPIO as GPIO      # the stub, as are lgpio, gpiod, smbus2, spidev, serial

`p4n4-emu run script.py` does this before the script starts (through a
sitecustomize), so scripts run unmodified. install() also attaches the parts
(hw.layout: the default board, or a layout file), serves the board's files
(vfs), points gpiozero at lgpio, and can start the GPIO MQTT bridge.
"""

from __future__ import annotations

import atexit
import os
import struct
import sys
import types

from p4n4_emu.hw import board as boards
from p4n4_emu.hw import buses, layout, vfs
from p4n4_emu.hw.devices import DS18B20
from p4n4_emu.hw.readings import Readings

W1_DEVICES = "/sys/bus/w1/devices"
W1_MASTER = f"{W1_DEVICES}/w1_bus_master1"

_onewire: list[DS18B20] = []
_bridge = None


GPIOD_APIS = ("v2", "v1")


def modules(gpiod_api: str = "v2") -> dict[str, types.ModuleType]:
    """Import name → stub module, for every library the stubs replace.

    *gpiod_api* picks what `import gpiod` loads: the libgpiod v2 bindings, or
    the v1 ones (python3-libgpiod 1.6, Raspberry Pi OS bookworm's package).
    """
    from p4n4_emu.hw import (
        gpio_stub,
        gpiod_stub,
        gpiod_v1_stub,
        lgpio_stub,
        serial_stub,
        smbus_stub,
        spidev_stub,
    )

    if gpiod_api not in GPIOD_APIS:
        raise ValueError(f"Unknown gpiod API {gpiod_api!r}. Choose from: {', '.join(GPIOD_APIS)}")
    rpi = types.ModuleType("RPi", "p4n4-emu stand-in for the RPi package")
    rpi.__path__ = []  # a package, so `import RPi.GPIO` looks in sys.modules
    rpi.GPIO = gpio_stub
    if gpiod_api == "v1":
        gpiod = {"gpiod": gpiod_v1_stub}
    else:
        gpiod_names = ("chip", "chip_info", "edge_event", "exception", "info_event",
                       "line_info", "line_request", "line_settings")
        gpiod = {
            "gpiod": gpiod_stub,
            "gpiod.line": gpiod_stub.line,
            **{f"gpiod.{name}": gpiod_stub for name in gpiod_names},
        }
    return {
        "RPi": rpi,
        "RPi.GPIO": gpio_stub,
        "lgpio": lgpio_stub,
        **gpiod,
        "smbus2": smbus_stub,
        "smbus": smbus_stub,
        "spidev": spidev_stub,
        "serial": serial_stub,
        "serial.serialutil": serial_stub,
    }


def default_parts(readings: Readings) -> None:
    """A BME280, an MPU-6050 and an ADS1115 on I2C bus 1, an MCP3008 on SPI 0.0,
    and a DS18B20 on 1-Wire, all sensing *readings*."""
    attach_parts(layout.DEFAULT_LAYOUT, readings)


def attach_parts(parts: tuple[layout.Part, ...], readings: Readings) -> None:
    """Put a layout's *parts* on the buses, sensing *readings*."""
    layout.attach(parts, readings, attach_onewire)


def attach_onewire(sensor: DS18B20) -> None:
    """Put a 1-Wire sensor on the bus: it appears under /sys/bus/w1/devices."""
    _onewire.append(sensor)
    vfs.add_root(W1_DEVICES)
    base = f"{W1_DEVICES}/{sensor.id}"
    vfs.add_file(f"{base}/name", f"{sensor.id}\n")
    vfs.add_file(f"{base}/w1_slave", lambda: sensor.w1_slave().encode())
    vfs.add_file(f"{base}/temperature", lambda: sensor.temperature().encode())
    vfs.add_file(f"{W1_MASTER}/w1_master_slaves",
                 lambda: "".join(f"{s.id}\n" for s in _onewire).encode())
    vfs.add_file(f"{W1_MASTER}/w1_master_slave_count", lambda: f"{len(_onewire)}\n".encode())


def _board_files(board: boards.Board) -> None:
    """/proc/device-tree and the /dev nodes a script may check for."""
    vfs.add_root("/proc/device-tree")
    vfs.add_file("/proc/device-tree/model", board.model + "\0")
    vfs.add_file("/proc/device-tree/compatible", "".join(c + "\0" for c in board.compatible))
    vfs.add_file("/proc/device-tree/system/linux,revision", struct.pack(">I", board.revision))
    for chip in board.chips:
        vfs.add_file(f"/dev/gpiochip{chip}", b"")
    for bus in buses.i2c_buses():
        vfs.add_file(f"/dev/i2c-{bus}", b"")
    for bus, cs in buses.spi_devices():
        vfs.add_file(f"/dev/spidev{bus}.{cs}", b"")
    for path in buses.uart_ports():
        vfs.add_file(path, b"")


def install(
    board: str = boards.DEFAULT_BOARD,
    readings: Readings | None = None,
    parts: bool | tuple[layout.Part, ...] = True,  # noqa: FBT001, FBT002
    files: bool = True,  # noqa: FBT001, FBT002
    gpio_mqtt: str | None = None,
    gpio_prefix: str | None = None,
    gpiod_api: str = "v2",
) -> None:
    """Replace the hardware libraries with the stubs, for the *board* emulated.

    *parts* attaches the default parts (True), a layout's parts, or none (False),
    sensing *readings* (the built-in waves of emu-sensor-0 by default). *files*
    serves the board's files (vfs). *gpio_mqtt* (HOST[:PORT]) starts the GPIO MQTT
    bridge. *gpiod_api* picks the gpiod stub: "v2" (libgpiod 2) or "v1" (1.6).
    """
    global _bridge
    selected = boards.use(board)
    sys.modules.update(modules(gpiod_api))
    if parts:
        attach_parts(layout.DEFAULT_LAYOUT if parts is True else parts, readings or Readings())
    if files:
        _board_files(selected)
        root = vfs.install()
        atexit.register(_cleanup, root)
    # gpiozero would pick lgpio on its own; say so, unless the user chose a factory
    os.environ.setdefault("GPIOZERO_PIN_FACTORY", "lgpio")
    if gpio_mqtt and _bridge is None:
        from p4n4_emu.hw.mqtt_bridge import DEFAULT_PREFIX, GpioBridge, parse_address

        host, port = parse_address(gpio_mqtt)
        _bridge = GpioBridge(host, port, gpio_prefix or DEFAULT_PREFIX)
        _bridge.start()


def _cleanup(root: str) -> None:
    import shutil

    shutil.rmtree(root, ignore_errors=True)


def install_from_env() -> None:
    """install() as `p4n4-emu run` asks for it, through P4N4_EMU_* variables."""
    waves = os.environ.get("P4N4_EMU_READINGS")
    hardware = os.environ.get("P4N4_EMU_HARDWARE")
    install(
        board=os.environ.get("P4N4_EMU_BOARD", boards.DEFAULT_BOARD),
        readings=Readings.from_json(waves) if waves else None,
        parts=layout.from_json(hardware) if hardware
        else os.environ.get("P4N4_EMU_PARTS", "1") != "0",
        gpiod_api=os.environ.get("P4N4_EMU_GPIOD", "v2"),
        gpio_mqtt=os.environ.get("P4N4_EMU_GPIO_MQTT") or None,
        gpio_prefix=os.environ.get("P4N4_EMU_GPIO_PREFIX") or None,
    )


def reset() -> None:
    """Undo install() and forget all hardware state (tests)."""
    global _bridge
    from p4n4_emu.hw import gpio_stub, gpiod_stub, gpiod_v1_stub, lgpio_stub, pins

    if _bridge is not None:
        _bridge.stop()
        _bridge = None
    for api in GPIOD_APIS:
        for name, module in modules(api).items():
            if sys.modules.get(name) is module or name in ("RPi",):
                sys.modules.pop(name, None)
    gpiod_stub._reset()
    gpiod_v1_stub._reset()
    lgpio_stub._reset()
    gpio_stub.cleanup()
    pins.reset()
    buses.reset()
    vfs.uninstall()
    _onewire.clear()
    boards.use(boards.DEFAULT_BOARD)
