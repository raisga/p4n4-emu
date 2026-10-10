"""Simulator scenarios: which devices publish what, how often, and which faults they inject.

A scenario is a YAML file (`sim start --scenario FILE`, or `SIM_SCENARIO` in the container):

    interval: 2.0        # seconds between readings, for devices without their own
    qos: 0               # MQTT QoS of every publish (0, 1 or 2)
    retain: false        # publish with the retain flag
    timestamp: false     # add "ts" (epoch ms, when the reading was taken) to each payload
    seed: 42             # makes fault injection repeat run to run
    devices:
      - id: emu-sensor-{n}       # {n} is replaced by 0 … count-1
        count: 3
        measurements: [temperature, humidity]
      - id: iot-device-001
        interval: 5
        measurements:
          temperature: {base: 30, amplitude: 5}
          vibration: {base: 0.5, amplitude: 0.2, noise: 0.05, unit: g, min: 0}
        faults:
          - {type: spike, measurement: temperature, probability: 0.01, magnitude: 15}
          - {type: dropout, probability: 0.05}

Measurements are the built-ins (temperature, humidity, pressure, raw), optionally with
some of their wave settings changed, or new waves under any other name. Without a
scenario the simulator runs the default one: `count` devices named emu-sensor-{n}
publishing every built-in.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml

# A sinusoid with Gaussian noise, clamped to [min, max]: see generators.wave()
WAVE_KEYS = ("base", "amplitude", "noise", "period", "min", "max", "unit")


@dataclass(frozen=True)
class Wave:
    base: float
    amplitude: float = 0.0
    noise: float = 0.0
    period: float = 300.0
    min: float | None = None
    max: float | None = None
    unit: str = ""


# `raw` is not a wave: it is the accelerometer plus the board's CPU load
RAW = "raw"
BUILTIN_WAVES = {
    "temperature": Wave(22.0, 3.0, 0.1, unit="C"),
    "humidity": Wave(55.0, 10.0, 0.5, min=10.0, max=95.0, unit="%"),
    "pressure": Wave(1013.25, 2.0, 0.05, unit="hPa"),
}
BUILTINS = (*BUILTIN_WAVES, RAW)


@dataclass(frozen=True)
class Fault:
    """One kind of misbehaviour, rolled for every reading it applies to.

    spike      the reading is off by ±magnitude
    stuck      the value freezes for *duration* seconds
    drift      the value moves by *rate* units per second since the simulator started
    dropout    the reading is not published; with *duration*, nothing is for that long
    delay      the reading is published *seconds* late, after newer ones
    malformed  the payload is broken: truncated JSON, not JSON, a string value, or no value
    """

    type: str
    probability: float = 1.0
    measurement: str | None = None  # None: every measurement of the device
    magnitude: float = 0.0
    duration: float = 0.0
    rate: float = 0.0
    seconds: float = 0.0


# Settings each fault type takes, besides type / probability / measurement
FAULT_SETTINGS = {
    "spike": ("magnitude",),
    "stuck": ("duration",),
    "drift": ("rate",),
    "dropout": ("duration",),
    "delay": ("seconds",),
    "malformed": (),
}
# Faults that change the number in a reading, which `raw` doesn't have
VALUE_FAULTS = ("spike", "stuck", "drift")


@dataclass(frozen=True)
class Device:
    id: str
    measurements: dict[str, Wave | None]  # None: the raw measurement
    interval: float | None = None  # None: the scenario's
    faults: tuple[Fault, ...] = ()


@dataclass(frozen=True)
class Scenario:
    devices: tuple[Device, ...]
    interval: float = 2.0
    qos: int = 0
    retain: bool = False
    timestamp: bool = False
    seed: int | None = None

    def interval_of(self, device: Device) -> float:
        return device.interval if device.interval is not None else self.interval


def default_scenario(devices: int = 1, interval: float = 2.0) -> Scenario:
    """What the simulator publishes without a scenario file."""
    measurements = {**BUILTIN_WAVES, RAW: None}
    return Scenario(
        devices=tuple(Device(f"emu-sensor-{i}", dict(measurements)) for i in range(devices)),
        interval=interval,
    )


class ScenarioError(ValueError):
    pass


def load_scenario(path: Path | str) -> Scenario:
    path = Path(path)
    try:
        doc = yaml.safe_load(path.read_text())
    except OSError as e:
        raise ScenarioError(f"Cannot read scenario {path}: {e.strerror}") from e
    except yaml.YAMLError as e:
        raise ScenarioError(f"Scenario {path} is not valid YAML: {e}") from e
    try:
        return parse_scenario(doc)
    except ScenarioError as e:
        raise ScenarioError(f"Scenario {path}: {e}") from e


def parse_scenario(doc: Any) -> Scenario:
    """Check a scenario document and build it; every error names where it is."""
    m = _mapping(doc, "the scenario")
    _known(m, ("interval", "qos", "retain", "timestamp", "seed", "devices"), "the scenario")
    scenario = Scenario(
        devices=(),
        interval=_positive(m.get("interval", 2.0), "interval"),
        qos=_choice(m.get("qos", 0), (0, 1, 2), "qos"),
        retain=_bool(m.get("retain", False), "retain"),
        timestamp=_bool(m.get("timestamp", False), "timestamp"),
        seed=_int(m["seed"], "seed") if m.get("seed") is not None else None,
    )
    entries = m.get("devices")
    if not isinstance(entries, list) or not entries:
        raise ScenarioError("devices: expected a non-empty list")
    devices = [d for i, e in enumerate(entries) for d in _devices(e, f"devices[{i}]")]
    seen: set[str] = set()
    for d in devices:
        if d.id in seen:
            raise ScenarioError(f"device id {d.id!r} is used twice")
        seen.add(d.id)
    return replace(scenario, devices=tuple(devices))


def _devices(entry: Any, where: str) -> list[Device]:
    m = _mapping(entry, where)
    _known(m, ("id", "count", "interval", "measurements", "faults"), where)
    id_ = m.get("id")
    if not isinstance(id_, str) or not id_:
        raise ScenarioError(f"{where}.id: expected a device id")
    count = _int(m.get("count", 1), f"{where}.count")
    if count < 1:
        raise ScenarioError(f"{where}.count: must be at least 1")
    if count > 1 and "{n}" not in id_:
        raise ScenarioError(f"{where}.id: needs {{n}} to give the {count} devices their own ids")
    ids = [id_.replace("{n}", str(n)) for n in range(count)]
    for i in ids:
        # The id is a topic level: sensors/<device-id>/<measurement>
        if any(c in i for c in "/+#") or i != i.strip():
            raise ScenarioError(f"{where}.id: {i!r} can't contain '/', '+', '#' or edge spaces")

    measurements = _measurements(m.get("measurements", list(BUILTINS)), f"{where}.measurements")
    faults = tuple(
        _fault(f, f"{where}.faults[{i}]", measurements) for i, f in enumerate(m.get("faults") or [])
    )
    interval = _positive(m["interval"], f"{where}.interval") if "interval" in m else None
    return [Device(i, dict(measurements), interval, faults) for i in ids]


def _measurements(value: Any, where: str) -> dict[str, Wave | None]:
    if isinstance(value, list):
        value = {name: None for name in value}
    m = _mapping(value, where)
    if not m:
        raise ScenarioError(f"{where}: expected at least one measurement")
    out: dict[str, Wave | None] = {}
    for name, settings in m.items():
        at = f"{where}.{name}"
        if not isinstance(name, str) or not name or any(c in name for c in "/+#"):
            raise ScenarioError(f"{at}: invalid measurement name")
        if name == RAW:
            if settings is not None:
                raise ScenarioError(f"{at}: raw takes no settings")
            out[name] = None
            continue
        out[name] = _wave(settings, at, BUILTIN_WAVES.get(name))
    return out


def _wave(settings: Any, where: str, builtin: Wave | None) -> Wave:
    if settings is None:
        if builtin is None:
            raise ScenarioError(
                f"{where}: not a built-in ({', '.join(BUILTINS)}); give it at least a base"
            )
        return builtin
    m = _mapping(settings, where)
    _known(m, WAVE_KEYS, where)
    if builtin is None and "base" not in m:
        raise ScenarioError(f"{where}.base: required for a measurement that isn't built in")
    wave = builtin or Wave(0.0)
    changes: dict[str, Any] = {}
    for key, v in m.items():
        if key == "unit":
            if not isinstance(v, str):
                raise ScenarioError(f"{where}.unit: expected a string")
            changes[key] = v
        elif key in ("min", "max") and v is None:
            changes[key] = None
        else:
            changes[key] = _number(v, f"{where}.{key}")
    wave = replace(wave, **changes)
    if wave.noise < 0 or wave.period <= 0:
        raise ScenarioError(f"{where}: noise can't be negative and period must be positive")
    if wave.min is not None and wave.max is not None and wave.min > wave.max:
        raise ScenarioError(f"{where}: min is above max")
    return wave


def _fault(entry: Any, where: str, measurements: dict[str, Wave | None]) -> Fault:
    m = _mapping(entry, where)
    kind = m.get("type")
    if kind not in FAULT_SETTINGS:
        raise ScenarioError(f"{where}.type: expected one of {', '.join(FAULT_SETTINGS)}")
    _known(m, ("type", "probability", "measurement", *FAULT_SETTINGS[kind]), where)
    fault = Fault(type=kind)
    probability = _number(m.get("probability", 1.0), f"{where}.probability")
    if not 0 <= probability <= 1:
        raise ScenarioError(f"{where}.probability: must be between 0 and 1")
    measurement = m.get("measurement")
    if measurement is not None:
        if measurement not in measurements:
            raise ScenarioError(f"{where}.measurement: the device has no {measurement!r}")
        if kind in VALUE_FAULTS and measurements[measurement] is None:
            raise ScenarioError(f"{where}: {kind} needs a measurement with a value, not raw")
    settings = {k: _number(m[k], f"{where}.{k}") for k in FAULT_SETTINGS[kind] if k in m}
    for key in ("magnitude", "duration", "seconds"):
        if settings.get(key, 0) < 0:
            raise ScenarioError(f"{where}.{key}: can't be negative")
    if kind == "delay" and settings.get("seconds", 0) <= 0:
        raise ScenarioError(f"{where}.seconds: required, and positive")
    return replace(fault, probability=probability, measurement=measurement, **settings)


# ── value checks ──────────────────────────────────────────────────────────────

def _mapping(value: Any, where: str) -> dict:
    if not isinstance(value, dict):
        raise ScenarioError(f"{where}: expected a mapping")
    return value


def _known(m: dict, keys: tuple[str, ...], where: str) -> None:
    unknown = [str(k) for k in m if k not in keys]
    if unknown:
        raise ScenarioError(
            f"{where}: unknown key(s) {', '.join(unknown)} (expected: {', '.join(keys)})"
        )


def _number(value: Any, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ScenarioError(f"{where}: expected a number")
    return float(value)


def _positive(value: Any, where: str) -> float:
    number = _number(value, where)
    if number <= 0:
        raise ScenarioError(f"{where}: must be positive")
    return number


def _int(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ScenarioError(f"{where}: expected a whole number")
    return value


def _bool(value: Any, where: str) -> bool:
    if not isinstance(value, bool):
        raise ScenarioError(f"{where}: expected true or false")
    return value


def _choice(value: Any, choices: tuple, where: str) -> Any:
    if isinstance(value, bool) or value not in choices:
        raise ScenarioError(f"{where}: expected one of {', '.join(map(str, choices))}")
    return value

