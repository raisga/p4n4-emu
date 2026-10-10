"""Which parts sit on which bus: the board layout `p4n4-emu run --hardware FILE` reads.

    parts:
      - bme280                                   # bus 1, address 0x76
      - {part: bme280, address: 0x77}            # a second one, SDO strapped high
      - {part: ads1115, address: 0x49, measurements: {a0: soil_moisture, a1: light}}
      - {part: mcp3008, bus: 0, cs: 1, vref: 5.0}
      - {part: ds18b20, serial: 0000075a1c2f, measurements: {temperature: water_temp}}
      - {part: loopback, port: /dev/ttyUSB0}     # a serial port wired TX to RX

A part is the name alone, or a mapping with its bus settings. Addresses are the
ones the real part can be strapped to. `measurements` says which measurement of
the simulator device feeds each of the part's inputs (each input defaults to the
measurement of the same name). Attaching a part to a bus that doesn't exist yet
adds the bus (/dev/i2c-3, /dev/spidev1.0, the port).

Without a layout the board carries DEFAULT_LAYOUT. Parsing needs no PyYAML:
load() reads the file in p4n4-emu's process, and the script's process gets the
parsed parts as JSON.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from p4n4_emu.hw import buses
from p4n4_emu.hw.devices import ADS1115, BME280, DS18B20, MCP3008, MPU6050
from p4n4_emu.hw.readings import Readings
from p4n4_emu.sim.scenario import BUILTIN_WAVES

# I2C part → (its model, the addresses it can be strapped to; the first is the default)
I2C_PARTS = {
    "bme280": (BME280, (0x76, 0x77)),
    "mpu6050": (MPU6050, (0x68, 0x69)),
    "ads1115": (ADS1115, (0x48, 0x49, 0x4A, 0x4B)),
}
SPI_PARTS = {"mcp3008": MCP3008}
PARTS = (*I2C_PARTS, *SPI_PARTS, "ds18b20", "loopback")
_INPUTS = {
    "bme280": BME280.INPUTS, "mpu6050": MPU6050.INPUTS, "ads1115": ADS1115.INPUTS,
    "mcp3008": MCP3008.INPUTS, "ds18b20": DS18B20.INPUTS,
}
_KEYS = {
    **{name: ("bus", "address", "measurements") for name in I2C_PARTS},
    "mcp3008": ("bus", "cs", "vref", "measurements"),
    "ds18b20": ("serial", "measurements"),
    "loopback": ("port",),
}


class LayoutError(ValueError):
    pass


@dataclass(frozen=True)
class Part:
    part: str
    bus: int | None = None  # I2C bus, or SPI bus
    address: int | None = None  # I2C address
    cs: int | None = None  # SPI chip select
    vref: float | None = None
    serial: str | None = None  # DS18B20 serial number (12 hex digits)
    port: str | None = None  # serial port path
    measurements: dict[str, str] = field(default_factory=dict)

    @property
    def where(self) -> str | None:
        """Where the part sits, or None for a 1-Wire sensor without a serial number."""
        if self.address is not None:
            return f"i2c-{self.bus} 0x{self.address:02x}"
        if self.cs is not None:
            return f"spidev{self.bus}.{self.cs}"
        if self.port is not None:
            return self.port
        return f"w1 28-{self.serial}" if self.serial else None


def parse(doc: Any) -> tuple[Part, ...]:
    """Check a layout document and build its parts; every error names where it is."""
    if not isinstance(doc, dict):
        raise LayoutError("the layout: expected a mapping with a parts list")
    unknown = [str(k) for k in doc if k != "parts"]
    if unknown:
        raise LayoutError(f"the layout: unknown key(s) {', '.join(unknown)} (expected: parts)")
    entries = doc.get("parts")
    if not isinstance(entries, list):
        raise LayoutError("parts: expected a list")
    parts = [_part(e, f"parts[{i}]") for i, e in enumerate(entries)]
    seen: dict[str, int] = {}
    for i, p in enumerate(parts):
        if p.where is None:
            continue
        if p.where in seen:
            raise LayoutError(f"parts[{i}]: {p.where} is taken by parts[{seen[p.where]}]")
        seen[p.where] = i
    return tuple(parts)


def _part(entry: Any, where: str) -> Part:
    if isinstance(entry, str):
        entry = {"part": entry}
    if not isinstance(entry, dict):
        raise LayoutError(f"{where}: expected a part name or a mapping")
    name = entry.get("part")
    if name not in PARTS:
        raise LayoutError(f"{where}.part: expected one of {', '.join(PARTS)}")
    keys = ("part", *_KEYS[name])
    unknown = [str(k) for k in entry if k not in keys]
    if unknown:
        raise LayoutError(
            f"{where}: unknown key(s) {', '.join(unknown)} for a {name} "
            f"(expected: {', '.join(keys)})"
        )
    measurements = _measurements(entry.get("measurements"), name, f"{where}.measurements")

    if name in I2C_PARTS:
        addresses = I2C_PARTS[name][1]
        address = _int(entry.get("address", addresses[0]), f"{where}.address")
        if address not in addresses:
            raise LayoutError(
                f"{where}.address: a {name} answers on {', '.join(f'0x{a:02x}' for a in addresses)}"
            )
        bus = _int(entry.get("bus", 1), f"{where}.bus")
        return Part(name, bus=bus, address=address, measurements=measurements)
    if name in SPI_PARTS:
        vref = entry.get("vref", 3.3)
        if isinstance(vref, bool) or not isinstance(vref, int | float) or vref <= 0:
            raise LayoutError(f"{where}.vref: expected a positive number of volts")
        return Part(name, bus=_int(entry.get("bus", 0), f"{where}.bus"),
                    cs=_int(entry.get("cs", 0), f"{where}.cs"), vref=float(vref),
                    measurements=measurements)
    if name == "ds18b20":
        serial = entry.get("serial")
        if serial is not None:
            serial = str(serial).lower()
            if len(serial) != 12 or any(c not in "0123456789abcdef" for c in serial):
                raise LayoutError(f"{where}.serial: expected 12 hex digits")
        return Part(name, serial=serial, measurements=measurements)
    port = entry.get("port")
    if not isinstance(port, str) or not port.startswith("/dev/"):
        raise LayoutError(f"{where}.port: expected a device path, such as /dev/ttyUSB0")
    return Part(name, port=port)


def _measurements(value: Any, part: str, where: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and v for k, v in value.items()
    ):
        raise LayoutError(f"{where}: expected input: measurement pairs")
    unknown = [k for k in value if k not in _INPUTS[part]]
    if unknown:
        raise LayoutError(
            f"{where}: a {part} has no input(s) {', '.join(unknown)} "
            f"(it has: {', '.join(_INPUTS[part])})"
        )
    return dict(value)


def _int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LayoutError(f"{where}: expected a whole number")
    return value


DEFAULT_LAYOUT = parse({"parts": ["bme280", "mpu6050", "ads1115", "mcp3008", "ds18b20"]})


def load(path: Path | str) -> tuple[Part, ...]:
    import yaml  # only in p4n4-emu's own process; see the module docstring

    path = Path(path)
    try:
        doc = yaml.safe_load(path.read_text())
    except OSError as e:
        raise LayoutError(f"Cannot read layout {path}: {e.strerror}") from e
    except yaml.YAMLError as e:
        raise LayoutError(f"Layout {path} is not valid YAML: {e}") from e
    try:
        return parse(doc)
    except LayoutError as e:
        raise LayoutError(f"Layout {path}: {e}") from e


def check_measurements(parts: tuple[Part, ...], readings: Readings) -> None:
    """Every measurement a layout names must be one the device has."""
    known = {*readings.waves, *BUILTIN_WAVES}
    for i, p in enumerate(parts):
        missing = sorted(m for m in p.measurements.values() if m not in known)
        if missing:
            raise LayoutError(
                f"parts[{i}].measurements: device {readings.device_id!r} has no "
                f"{', '.join(missing)} (it has: {', '.join(sorted(known))})"
            )


def to_json(parts: tuple[Part, ...]) -> str:
    return json.dumps([asdict(p) for p in parts])


def from_json(text: str) -> tuple[Part, ...]:
    return tuple(Part(**p) for p in json.loads(text))


def attach(parts: tuple[Part, ...], readings: Readings, attach_onewire) -> None:
    """Put *parts* on the buses, sensing *readings*; 1-Wire sensors go through
    *attach_onewire* (shims.attach_onewire, which serves their sysfs files).

    DS18B20s without a serial number get stable ones of their own; the first keeps
    the device's default serial, the one the board has without a layout.
    """
    unnumbered = 0
    for p in parts:
        m = p.measurements or None
        if p.part in I2C_PARTS:
            buses.attach_i2c(p.bus, p.address, I2C_PARTS[p.part][0](readings, measurements=m))
        elif p.part in SPI_PARTS:
            buses.attach_spi(p.bus, p.cs, SPI_PARTS[p.part](readings, vref=p.vref, measurements=m))
        elif p.part == "ds18b20":
            serial = p.serial
            if serial is None:
                if unnumbered:
                    key = f"{readings.device_id}/{unnumbered}"
                    serial = hashlib.sha1(key.encode()).hexdigest()[:12]
                unnumbered += 1
            attach_onewire(DS18B20(readings, serial=serial, measurements=m))
        elif p.part == "loopback":
            buses.attach_uart(p.port, buses.Loopback())
