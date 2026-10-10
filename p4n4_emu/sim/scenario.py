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

      - id: esp32-garden         # a firmware-like device: see Mcu
        mcu: {type: esp32, firmware: 1.4.2, sleep: 60}
        measurements:
          camera: {kind: image, width: 96, height: 96}      # frames for the edge runner
          mic: {kind: audio, file: clips/door.wav}          # windows of a WAV file
    replay:
      - {file: recorded.csv, speed: 10, loop: true}         # see replay.py

Measurements are the built-ins (temperature, humidity, pressure, raw), optionally with
some of their wave settings changed, new waves under any other name, or media feeds
(`kind: image` / `kind: audio`, see Feed). Without a scenario the simulator runs the
default one: `count` devices named emu-sensor-{n} publishing every built-in.

Files a scenario names (a WAV file, a replay) are relative to the scenario's folder.
`sim start` mounts them into the container and maps their names (load_scenario's
*files*), so the scenario works unchanged on either side.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

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


@dataclass(frozen=True)
class Feed:
    """A media feed: each reading is one camera frame or one audio window, published as
    {"values": [...]}, the feature vector the edge runner (ei-runner) classifies.

    image  width x height pixels with *channels* (1 or 3): a bright blob drifting over a
           noisy background. encoding: packed (one 0xRRGGBB number per pixel, what Edge
           Impulse image models take), uint8 (0-255 per channel) or float (0-1).
    audio  *window* seconds of samples at *sample_rate*: a *frequency* tone plus noise, or
           the next window of a WAV *file* (mono or mixed down; its own rate wins).
           encoding: int16 (what Edge Impulse audio models take) or float (-1 to 1).
    """

    kind: str
    encoding: str
    noise: float = 0.05
    width: int = 96
    height: int = 96
    channels: int = 3
    sample_rate: int = 16000
    window: float = 1.0
    frequency: float = 440.0
    amplitude: float = 0.5
    file: str | None = None

    @property
    def features(self) -> int:
        """Values in one reading (for a WAV file, at the scenario's sample_rate)."""
        if self.kind == "image":
            return self.width * self.height * (1 if self.encoding == "packed" else self.channels)
        return int(self.sample_rate * self.window)


FEED_KEYS = {
    "image": ("kind", "width", "height", "channels", "encoding", "noise"),
    "audio": ("kind", "sample_rate", "window", "frequency", "amplitude", "noise", "file",
              "encoding"),
}
FEED_ENCODINGS = {"image": ("packed", "uint8", "float"), "audio": ("int16", "float")}


@dataclass(frozen=True)
class Mcu:
    """A device that behaves like microcontroller firmware rather than a perfect publisher.

    It has its own MQTT connection (client id = the device id). On every connection it
    registers on devices/<id>/register ({type, firmware, location}, what the onboarding
    flow in the ai stack reads) until devices/<id>/status confirms it, and keeps
    devices/<id>/availability (online / offline as its will / sleeping) retained.
    *boot* is how long a power-on or wake takes before the first reading. With *sleep*
    it deep-sleeps: wake, connect, publish one round, disconnect, sleep. *wifi_drop*
    loses the connection (rolled each round) for its duration; readings taken meanwhile
    are lost, or held up to *buffer* of them and sent on reconnect.
    """

    type: str = "esp32"
    firmware: str = "p4n4-emu"
    location: str | None = None
    boot: float = 2.0
    sleep: float | None = None
    drop_probability: float = 0.0
    drop_duration: float = 30.0
    buffer: int = 0
    availability: bool = True


MCU_KEYS = ("type", "firmware", "location", "boot", "sleep", "wifi_drop", "buffer",
            "availability")


@dataclass(frozen=True)
class Replay:
    """Recorded readings published again on their own schedule (see replay.py)."""

    file: str
    speed: float = 1.0  # 10: ten times faster; 0: as fast as possible
    loop: bool = False
    devices: dict[str, str] = field(default_factory=dict)  # recorded id → id to publish as


REPLAY_KEYS = ("file", "speed", "loop", "devices")


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
    measurements: dict[str, Wave | Feed | None]  # None: the raw measurement
    interval: float | None = None  # None: the scenario's
    faults: tuple[Fault, ...] = ()
    mcu: Mcu | None = None


@dataclass(frozen=True)
class Scenario:
    devices: tuple[Device, ...]
    interval: float = 2.0
    qos: int = 0
    retain: bool = False
    timestamp: bool = False
    seed: int | None = None
    replays: tuple[Replay, ...] = ()
    # Files the scenario names: (as written, the path it resolved to)
    files: tuple[tuple[str, str], ...] = ()

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


def load_scenario(path: Path | str, files: dict[str, str] | None = None) -> Scenario:
    """Load a scenario file. The files it names are relative to its folder, unless
    *files* maps a name as written to where the file is (inside the container)."""
    # Imported here: the hardware stubs use this module's waves, and they run in
    # the script's interpreter (`p4n4-emu run --python`), which may not have PyYAML
    import yaml

    path = Path(path)
    try:
        doc = yaml.safe_load(path.read_text())
    except OSError as e:
        raise ScenarioError(f"Cannot read scenario {path}: {e.strerror}") from e
    except yaml.YAMLError as e:
        raise ScenarioError(f"Scenario {path} is not valid YAML: {e}") from e
    try:
        return parse_scenario(doc, base=path.parent, files=files)
    except ScenarioError as e:
        raise ScenarioError(f"Scenario {path}: {e}") from e


def parse_scenario(
    doc: Any, base: Path | None = None, files: dict[str, str] | None = None
) -> Scenario:
    """Check a scenario document and build it; every error names where it is.

    Files it names resolve against *base* (default: the current directory), or
    through *files* (name as written → path).
    """
    resolved: dict[str, str] = {}

    def resolve(value: Any, where: str) -> str:
        if not isinstance(value, str) or not value:
            raise ScenarioError(f"{where}: expected a file path")
        if files and value in files:
            path = files[value]
        else:
            path = str((base or Path()).joinpath(Path(value).expanduser()).resolve())
        if not Path(path).is_file():
            raise ScenarioError(f"{where}: no such file {value!r}")
        resolved[value] = path
        return path

    m = _mapping(doc, "the scenario")
    _known(m, ("interval", "qos", "retain", "timestamp", "seed", "devices", "replay"),
           "the scenario")
    scenario = Scenario(
        devices=(),
        interval=_positive(m.get("interval", 2.0), "interval"),
        qos=_choice(m.get("qos", 0), (0, 1, 2), "qos"),
        retain=_bool(m.get("retain", False), "retain"),
        timestamp=_bool(m.get("timestamp", False), "timestamp"),
        seed=_int(m["seed"], "seed") if m.get("seed") is not None else None,
    )
    replays = m.get("replay") or []
    if not isinstance(replays, list):
        raise ScenarioError("replay: expected a list")
    replays = [_replay(r, f"replay[{i}]", resolve) for i, r in enumerate(replays)]
    entries = m.get("devices")
    if entries is None and replays:
        entries = []
    elif not isinstance(entries, list) or not entries:
        raise ScenarioError("devices: expected a non-empty list (or a replay)")
    devices = [d for i, e in enumerate(entries) for d in _devices(e, f"devices[{i}]", resolve)]
    seen: set[str] = set()
    for d in devices:
        if d.id in seen:
            raise ScenarioError(f"device id {d.id!r} is used twice")
        seen.add(d.id)
    return replace(scenario, devices=tuple(devices), replays=tuple(replays),
                   files=tuple(resolved.items()))


def _replay(entry: Any, where: str, resolve: Callable[[Any, str], str]) -> Replay:
    if isinstance(entry, str):
        entry = {"file": entry}
    m = _mapping(entry, where)
    _known(m, REPLAY_KEYS, where)
    speed = _number(m.get("speed", 1.0), f"{where}.speed")
    if speed < 0:
        raise ScenarioError(f"{where}.speed: can't be negative (0 publishes as fast as it can)")
    devices = m.get("devices") or {}
    if not isinstance(devices, dict) or not all(
        isinstance(k, str) and isinstance(v, str) and _topic_level(v) for k, v in devices.items()
    ):
        raise ScenarioError(f"{where}.devices: expected recorded id: new id pairs")
    loop = _bool(m.get("loop", False), f"{where}.loop")
    if loop and speed == 0:
        raise ScenarioError(f"{where}: loop needs a speed above 0, or it never stops")
    return Replay(resolve(m.get("file"), f"{where}.file"), speed, loop, dict(devices))


def _topic_level(value: str) -> bool:
    return bool(value) and not any(c in value for c in "/+#") and value == value.strip()


def _mcu(value: Any, where: str) -> Mcu:
    if value is True or value is None:
        value = {}
    m = _mapping(value, where)
    _known(m, MCU_KEYS, where)
    mcu = Mcu()
    changes: dict[str, Any] = {}
    for key in ("type", "firmware", "location"):
        if key in m:
            if not isinstance(m[key], str | int | float) or isinstance(m[key], bool):
                raise ScenarioError(f"{where}.{key}: expected text")
            changes[key] = str(m[key])
    if "boot" in m:
        changes["boot"] = _number(m["boot"], f"{where}.boot")
        if changes["boot"] < 0:
            raise ScenarioError(f"{where}.boot: can't be negative")
    if m.get("sleep") is not None:
        changes["sleep"] = _positive(m["sleep"], f"{where}.sleep")
    if "wifi_drop" in m:
        drop = _mapping(m["wifi_drop"], f"{where}.wifi_drop")
        _known(drop, ("probability", "duration"), f"{where}.wifi_drop")
        probability = _number(drop.get("probability", 1.0), f"{where}.wifi_drop.probability")
        if not 0 <= probability <= 1:
            raise ScenarioError(f"{where}.wifi_drop.probability: must be between 0 and 1")
        changes["drop_probability"] = probability
        changes["drop_duration"] = _positive(
            drop.get("duration", 30.0), f"{where}.wifi_drop.duration"
        )
    if "buffer" in m:
        changes["buffer"] = _int(m["buffer"], f"{where}.buffer")
        if changes["buffer"] < 0:
            raise ScenarioError(f"{where}.buffer: can't be negative")
    if "availability" in m:
        changes["availability"] = _bool(m["availability"], f"{where}.availability")
    return replace(mcu, **changes)


def _devices(entry: Any, where: str, resolve: Callable[[Any, str], str]) -> list[Device]:
    m = _mapping(entry, where)
    _known(m, ("id", "count", "interval", "measurements", "faults", "mcu"), where)
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

    measurements = _measurements(
        m.get("measurements", list(BUILTINS)), f"{where}.measurements", resolve
    )
    faults = tuple(
        _fault(f, f"{where}.faults[{i}]", measurements) for i, f in enumerate(m.get("faults") or [])
    )
    interval = _positive(m["interval"], f"{where}.interval") if "interval" in m else None
    mcu = _mcu(m["mcu"], f"{where}.mcu") if "mcu" in m and m["mcu"] is not False else None
    return [Device(i, dict(measurements), interval, faults, mcu) for i in ids]


def _feed(m: dict, where: str, resolve: Callable[[Any, str], str]) -> Feed:
    kind = m.get("kind")
    if kind not in FEED_KEYS:
        raise ScenarioError(f"{where}.kind: expected one of {', '.join(FEED_KEYS)}")
    _known(m, FEED_KEYS[kind], where)
    encoding = m.get("encoding", FEED_ENCODINGS[kind][0])
    if encoding not in FEED_ENCODINGS[kind]:
        raise ScenarioError(
            f"{where}.encoding: expected one of {', '.join(FEED_ENCODINGS[kind])}"
        )
    changes: dict[str, Any] = {}
    for key in ("width", "height", "sample_rate"):
        if key in m:
            changes[key] = _int(m[key], f"{where}.{key}")
            if changes[key] < 1:
                raise ScenarioError(f"{where}.{key}: must be at least 1")
    if "channels" in m:
        changes["channels"] = _choice(m["channels"], (1, 3), f"{where}.channels")
    for key in ("window", "frequency"):
        if key in m:
            changes[key] = _positive(m[key], f"{where}.{key}")
    for key in ("noise", "amplitude"):
        if key in m:
            changes[key] = _number(m[key], f"{where}.{key}")
            if not 0 <= changes[key] <= 1:
                raise ScenarioError(f"{where}.{key}: must be between 0 and 1 (of full scale)")
    feed = Feed(kind, encoding, **changes)
    if "file" in m:
        path = resolve(m["file"], f"{where}.file")
        from p4n4_emu.sim import media  # the wave module's header check

        try:
            rate = media.wav_rate(path)
        except ValueError as e:
            raise ScenarioError(f"{where}.file: {e}") from None
        feed = replace(feed, file=path, sample_rate=rate)
    return feed


def _measurements(
    value: Any, where: str, resolve: Callable[[Any, str], str] | None = None
) -> dict[str, Wave | Feed | None]:
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
        if isinstance(settings, dict) and "kind" in settings:
            out[name] = _feed(settings, at, resolve or (lambda v, w: str(v)))
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
        if kind in VALUE_FAULTS and not isinstance(measurements[measurement], Wave):
            raise ScenarioError(
                f"{where}: {kind} needs a measurement with a value, not raw or a feed"
            )
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

