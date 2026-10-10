"""What the emulated parts sense: the sensor simulator's curves for one device.

A BME280 on the emulated I2C bus reads the temperature the simulator publishes
on sensors/<device-id>/temperature (each sample has its own noise), so the
hardware path and the MQTT path agree. The device and its waves come from a
scenario (`p4n4-emu run --scenario FILE --device ID`), or are the built-ins.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

from p4n4_emu.sim import generators
from p4n4_emu.sim.scenario import BUILTIN_WAVES, Wave, load_scenario

DEFAULT_DEVICE = "emu-sensor-0"


class Readings:
    def __init__(
        self, device_id: str = DEFAULT_DEVICE, measurements: dict[str, Wave | None] | None = None
    ) -> None:
        self.device_id = device_id
        self.waves = {
            name: wave for name, wave in (measurements or BUILTIN_WAVES).items() if wave is not None
        }
        self.phase = generators.device_phase(device_id)
        self._gens: dict[str, Iterator[float]] = {}
        self._accel: Iterator[tuple[float, float, float]] | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_scenario(cls, path: Path | str, device_id: str | None = None) -> Readings:
        """The waves of *device_id* in the scenario (its first device by default)."""
        scenario = load_scenario(path)
        devices = {d.id: d for d in scenario.devices}
        if device_id is None:
            device = scenario.devices[0]
        elif device_id in devices:
            device = devices[device_id]
        else:
            raise ValueError(
                f"Scenario {path} has no device {device_id!r} (it has: {', '.join(devices)})"
            )
        return cls(device.id, device.measurements)

    def to_json(self) -> str:
        """The device and its waves, for the script's process (`p4n4-emu run`)."""
        return json.dumps({"device": self.device_id,
                           "waves": {name: asdict(w) for name, w in self.waves.items()}})

    @classmethod
    def from_json(cls, text: str) -> Readings:
        doc = json.loads(text)
        return cls(doc["device"], {name: Wave(**w) for name, w in doc["waves"].items()})

    def value(self, name: str, default: Wave | None = None) -> float:
        """The current value of measurement *name*: the device's wave, else
        the built-in, else *default*."""
        with self._lock:
            gen = self._gens.get(name)
            if gen is None:
                wave = self.waves.get(name) or BUILTIN_WAVES.get(name) or default
                if wave is None:
                    raise KeyError(f"device {self.device_id!r} has no measurement {name!r}")
                gen = self._gens[name] = generators.wave(wave, phase=self.phase)
            return next(gen)

    def acceleration(self) -> tuple[float, float, float]:
        """Acceleration in g, as the simulator's raw measurement publishes it."""
        with self._lock:
            if self._accel is None:
                self._accel = generators.accelerometer_xyz()
            return next(self._accel)
