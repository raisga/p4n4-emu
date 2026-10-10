"""Install the hardware stubs in place of the real libraries.

    from p4n4_emu.hw import shims
    shims.install(board="rpi5")
    import RPi.GPIO as GPIO      # the stub, as are lgpio, gpiod, smbus2, spidev, serial

`p4n4-emu run script.py` does this before the script starts (through a
sitecustomize), so scripts run unmodified. install() also attaches the default
parts (default_parts()), serves the board's files (vfs), points gpiozero at
lgpio, and can start the GPIO MQTT bridge.
"""

from __future__ import annotations

import atexit
import os
import struct
import sys
import types

from p4n4_emu.hw import board as boards
from p4n4_emu.hw import buses, vfs
from p4n4_emu.hw.devices import ADS1115, BME280, DS18B20, MCP3008, MPU6050
from p4n4_emu.hw.readings import Readings

W1_DEVICES = "/sys/bus/w1/devices"
W1_MASTER = f"{W1_DEVICES}/w1_bus_master1"

_onewire: list[DS18B20] = []
_bridge = None


def modules() -> dict[str, types.ModuleType]:
    """Import name → stub module, for every library the stubs replace."""
    from p4n4_emu.hw import gpio_stub, gpiod_stub, lgpio_stub, serial_stub, smbus_stub, spidev_stub

    rpi = types.ModuleType("RPi", "p4n4-emu stand-in for the RPi package")
    rpi.__path__ = []  # a package, so `import RPi.GPIO` looks in sys.modules
    rpi.GPIO = gpio_stub
    gpiod_names = ("chip", "chip_info", "edge_event", "exception", "info_event", "line_info",
                   "line_request", "line_settings")
    return {
        "RPi": rpi,
        "RPi.GPIO": gpio_stub,
        "lgpio": lgpio_stub,
        "gpiod": gpiod_stub,
        "gpiod.line": gpiod_stub.line,
        **{f"gpiod.{name}": gpiod_stub for name in gpiod_names},
        "smbus2": smbus_stub,
        "smbus": smbus_stub,
        "spidev": spidev_stub,
        "serial": serial_stub,
        "serial.serialutil": serial_stub,
    }


def default_parts(readings: Readings) -> None:
    """A BME280, an MPU-6050 and an ADS1115 on I2C bus 1, an MCP3008 on SPI 0.0,
    and a DS18B20 on 1-Wire, all sensing *readings*."""
    buses.attach_i2c(1, 0x76, BME280(readings))
    buses.attach_i2c(1, 0x68, MPU6050(readings))
    buses.attach_i2c(1, 0x48, ADS1115(readings))
    buses.attach_spi(0, 0, MCP3008(readings))
    attach_onewire(DS18B20(readings))


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
    compatible = {"rpi4": "raspberrypi,4-model-b\0brcm,bcm2711\0",
                  "rpi5": "raspberrypi,5-model-b\0brcm,bcm2712\0"}[board.name]
    vfs.add_file("/proc/device-tree/model", board.model + "\0")
    vfs.add_file("/proc/device-tree/compatible", compatible)
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
    parts: bool = True,  # noqa: FBT001, FBT002
    files: bool = True,  # noqa: FBT001, FBT002
    gpio_mqtt: str | None = None,
    gpio_prefix: str | None = None,
) -> None:
    """Replace the hardware libraries with the stubs, for the *board* emulated.

    *parts* attaches default_parts(), sensing *readings* (the built-in waves of
    emu-sensor-0 by default). *files* serves the board's files (vfs).
    *gpio_mqtt* (HOST[:PORT]) starts the GPIO MQTT bridge.
    """
    global _bridge
    selected = boards.use(board)
    sys.modules.update(modules())
    if parts:
        default_parts(readings or Readings())
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
    install(
        board=os.environ.get("P4N4_EMU_BOARD", boards.DEFAULT_BOARD),
        readings=Readings.from_json(waves) if waves else None,
        parts=os.environ.get("P4N4_EMU_PARTS", "1") != "0",
        gpio_mqtt=os.environ.get("P4N4_EMU_GPIO_MQTT") or None,
        gpio_prefix=os.environ.get("P4N4_EMU_GPIO_PREFIX") or None,
    )


def reset() -> None:
    """Undo install() and forget all hardware state (tests)."""
    global _bridge
    from p4n4_emu.hw import gpio_stub, gpiod_stub, lgpio_stub, pins

    if _bridge is not None:
        _bridge.stop()
        _bridge = None
    for name, module in modules().items():
        if sys.modules.get(name) is module or name in ("RPi",):
            sys.modules.pop(name, None)
    gpiod_stub._reset()
    lgpio_stub._reset()
    gpio_stub.cleanup()
    pins.reset()
    buses.reset()
    vfs.uninstall()
    _onewire.clear()
    boards.use(boards.DEFAULT_BOARD)
