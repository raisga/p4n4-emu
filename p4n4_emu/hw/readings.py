"""What the emulated parts sense: the sensor simulator's curves for one device.

A BME280 on the emulated I2C bus reads the temperature the simulator publishes
on sensors/<device-id>/temperature (each sample has its own noise), so the
hardware path and the MQTT path agree. The device and its waves come from a
scenario (`p4n4-emu run --scenario FILE --device ID`), or are the built-ins.

The device's faults apply too. Spikes, stuck values and drift change what a part
reads. A dropout makes the part stop answering: value() raises SensorDropout, which
the bus turns into what a disconnected part does (an I2C transfer fails with
EREMOTEIO, SPI reads zeros, a DS18B20 fails its CRC). `delay` and `malformed`
only exist on the MQTT path, so the hardware path ignores them.
"""

from __future__ import annotations

import json
import random
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

from p4n4_emu.sim import generators
from p4n4_emu.sim.faults import FaultInjector
from p4n4_emu.sim.scenario import BUILTIN_WAVES, RAW, Fault, Feed, Wave, load_scenario

DEFAULT_DEVICE = "emu-sensor-0"


class SensorDropout(Exception):
    """A dropout fault fired: the part is off the bus for now."""


def feeds(inputs: tuple[str, ...], measurements: dict[str, str] | None) -> dict[str, str]:
    """A part's input → the device measurement that feeds it; each input defaults to
    the measurement of the same name. Raises ValueError for an input the part lacks."""
    unknown = sorted(set(measurements or ()) - set(inputs))
    if unknown:
        raise ValueError(f"no input(s) {', '.join(unknown)} (it has: {', '.join(inputs)})")
    return {name: (measurements or {}).get(name, name) for name in inputs}


class Readings:
    def __init__(
        self,
        device_id: str = DEFAULT_DEVICE,
        measurements: dict[str, Wave | Feed | None] | None = None,
        faults: tuple[Fault, ...] = (),
        seed: int | None = None,
    ) -> None:
        self.device_id = device_id
        # Waves only: raw (None) and media feeds aren't what a part senses
        self.waves = {
            name: wave for name, wave in (measurements or BUILTIN_WAVES).items()
            if isinstance(wave, Wave)
        }
        self.faults = tuple(faults)
        self.seed = seed
        self.phase = generators.device_phase(device_id)
        self._gens: dict[str, Iterator[float]] = {}
        self._accel: Iterator[tuple[float, float, float]] | None = None
        self._injector = FaultInjector(self.faults, random.Random(seed), time.time())
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
        return cls(device.id, device.measurements, device.faults, scenario.seed)

    def to_json(self) -> str:
        """The device, its waves and faults, for the script's process (`p4n4-emu run`)."""
        return json.dumps({"device": self.device_id,
                           "waves": {name: asdict(w) for name, w in self.waves.items()},
                           "faults": [asdict(f) for f in self.faults],
                           "seed": self.seed})

    @classmethod
    def from_json(cls, text: str) -> Readings:
        doc = json.loads(text)
        return cls(doc["device"], {name: Wave(**w) for name, w in doc["waves"].items()},
                   tuple(Fault(**f) for f in doc.get("faults", ())), doc.get("seed"))

    def _check_dropout(self, name: str, now: float) -> None:
        if self._injector.device_silent(now) or self._injector.dropped(name, now):
            raise SensorDropout(f"{self.device_id}: {name} dropped out")

    def value(self, name: str, default: Wave | None = None) -> float:
        """The current value of measurement *name*: the device's wave, else
        the built-in, else *default*, after the device's faults."""
        with self._lock:
            gen = self._gens.get(name)
            if gen is None:
                wave = self.waves.get(name) or BUILTIN_WAVES.get(name) or default
                if wave is None:
                    raise KeyError(f"device {self.device_id!r} has no measurement {name!r}")
                gen = self._gens[name] = generators.wave(wave, phase=self.phase)
            value = next(gen)
            if not self.faults:
                return value
            now = time.time()
            self._check_dropout(name, now)
            return self._injector.value(name, value, now)

    def acceleration(self) -> tuple[float, float, float]:
        """Acceleration in g, as the simulator's raw measurement publishes it."""
        with self._lock:
            if self._accel is None:
                self._accel = generators.accelerometer_xyz()
            if self.faults:
                self._check_dropout(RAW, time.time())
            return next(self._accel)
