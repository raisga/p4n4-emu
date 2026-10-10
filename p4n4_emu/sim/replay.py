"""Replay recorded readings: a CSV file, or an InfluxDB export of p4n4's own data.

Two formats, told apart by their header:

  CSV       time,device,measurement,value[,unit][,any other field]
            one reading per row. time is ISO 8601 or epoch seconds / ms / ns; every
            column but time, device and measurement goes into the payload, numbers
            as numbers, empty cells left out. (timestamp / ts and sensor are
            accepted for time and measurement.)
  InfluxDB  annotated CSV, as `influx query --raw` or the Data Explorer's CSV
            download gives it: _time, _field, _value, _measurement, and the device /
            sensor tags the iot stack's Node-RED flow writes (measurement
            sensor_data). Rows of one reading (same time, device, sensor) become one
            payload, {"value": 21.5, "unit": "C"}; other measurements are skipped.

Readings are published on sensors/<device>/<measurement> with the gaps they were
recorded with, divided by the replay's speed, starting when the simulator starts.
Arrays never reach InfluxDB, so an export has no accelerometer `values`.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from p4n4_emu.sim.scenario import Replay, ScenarioError

INFLUX_MEASUREMENT = "sensor_data"
_TIME = ("time", "timestamp", "ts")
_MEASUREMENT = ("measurement", "sensor")


class ReplayError(ScenarioError):
    pass


@dataclass(frozen=True)
class Record:
    time: float  # epoch seconds
    device: str
    measurement: str
    payload: dict


@dataclass(frozen=True)
class Recording:
    records: tuple[Record, ...]
    format: str  # "csv" or "influxdb"
    skipped: int = 0  # rows of other InfluxDB measurements

    @property
    def devices(self) -> list[str]:
        return sorted({r.device for r in self.records})

    @property
    def span(self) -> float:
        return self.records[-1].time - self.records[0].time if self.records else 0.0


def parse_time(text: str) -> float:
    """ISO 8601 / RFC 3339 (fractions to the nanosecond), or epoch s / ms / µs / ns."""
    text = text.strip()
    try:
        number = float(text)
    except ValueError:
        pass
    else:
        for scale in (1e9, 1e6, 1e3):  # the larger the number, the finer the unit
            if abs(number) >= scale * 1e9 * 0.1:
                return number / scale
        return number
    iso = text.replace("Z", "+00:00").replace("z", "+00:00")
    if "." in iso:  # fromisoformat stops at microseconds; InfluxDB gives nanoseconds
        head, _, rest = iso.partition(".")
        digits = len(rest) - len(rest.lstrip("0123456789"))
        iso = f"{head}.{rest[:min(digits, 6)].ljust(6, '0')}{rest[digits:]}"
    try:
        moment = datetime.fromisoformat(iso)
    except ValueError:
        raise ValueError(f"not a time: {text!r}") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


def _value(text: str):
    """A cell as JSON would carry it: a number, a boolean, or text."""
    lowered = text.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        number = float(text)
    except ValueError:
        return text
    return int(number) if number.is_integer() and "." not in text and "e" not in lowered else number


def load(path: Path | str) -> Recording:
    path = Path(path)
    try:
        with path.open(newline="") as f:
            rows = [(n, row) for n, row in enumerate(csv.reader(f), 1)]
    except OSError as e:
        raise ReplayError(f"Cannot read {path}: {e.strerror}") from e
    except (csv.Error, UnicodeDecodeError) as e:
        raise ReplayError(f"{path} is not a CSV file: {e}") from e
    # InfluxDB annotations (#datatype, #group, #default) and blank lines carry no data
    rows = [(n, r) for n, r in rows if r and any(r) and not r[0].startswith("#")]
    if not rows:
        raise ReplayError(f"{path}: no readings")
    header = [c.strip() for c in rows[0][1]]
    try:
        if "_time" in header and "_field" in header:
            recording = _influx(rows)
        else:
            recording = _plain(rows)
    except ReplayError as e:
        raise ReplayError(f"{path}: {e}") from None
    if not recording.records:
        raise ReplayError(f"{path}: no readings (of measurement {INFLUX_MEASUREMENT})")
    return recording


def _column(header: list[str], names: tuple[str, ...], what: str) -> int:
    for name in names:
        if name in header:
            return header.index(name)
    raise ReplayError(f"the header has no {what} column (expected: {' or '.join(names)})")


def _plain(rows: list[tuple[int, list[str]]]) -> Recording:
    header = [c.strip() for c in rows[0][1]]
    t = _column(header, _TIME, "time")
    d = _column(header, ("device",), "device")
    m = _column(header, _MEASUREMENT, "measurement")
    records = []
    for n, row in rows[1:]:
        cells = dict(zip(header, row, strict=False))
        try:
            when = parse_time(row[t])
        except (ValueError, IndexError) as e:
            raise ReplayError(f"line {n}: {e}") from None
        device, measurement = row[d].strip(), row[m].strip()
        _check_ids(device, measurement, n)
        payload = {
            k: _value(v) for k, v in cells.items()
            if k not in (header[t], header[d], header[m]) and v.strip() != ""
        }
        if not payload:
            raise ReplayError(f"line {n}: no value")
        records.append(Record(when, device, measurement, payload))
    records.sort(key=lambda r: r.time)
    return Recording(tuple(records), "csv")


def _influx(rows: list[tuple[int, list[str]]]) -> Recording:
    grouped: dict[tuple[float, str, str], dict] = {}
    skipped = 0
    header: list[str] = []
    for n, row in rows:
        cells = [c.strip() for c in row]
        if "_time" in cells and "_field" in cells:  # every table repeats its header
            header = cells
            continue
        doc = dict(zip(header, row, strict=False))
        if doc.get("_measurement") != INFLUX_MEASUREMENT:
            skipped += 1
            continue
        device, sensor = doc.get("device", "").strip(), doc.get("sensor", "").strip()
        _check_ids(device, sensor, n)
        try:
            when = parse_time(doc["_time"])
        except (ValueError, KeyError) as e:
            raise ReplayError(f"line {n}: {e}") from None
        grouped.setdefault((when, device, sensor), {})[doc["_field"]] = _value(doc["_value"])
    records = sorted(
        (Record(t, dev, sensor, payload) for (t, dev, sensor), payload in grouped.items()),
        key=lambda r: r.time,
    )
    return Recording(tuple(records), "influxdb", skipped)


def _check_ids(device: str, measurement: str, line: int) -> None:
    for what, value in (("device", device), ("measurement", measurement)):
        if not value or any(c in value for c in "/+#"):
            raise ReplayError(f"line {line}: {what} {value!r} can't be a topic level")


class Replayer:
    """Hands out a recording's readings when they fall due."""

    def __init__(self, replay: Replay, start: float, recording: Recording | None = None) -> None:
        self.replay = replay
        self.recording = recording or load(replay.file)
        self.records = self.recording.records
        self.start = start
        self.first = self.records[0].time
        gaps = [b.time - a.time for a, b in zip(self.records, self.records[1:], strict=False)]
        # A loop starts again one typical gap after the last reading
        self.period = self.recording.span + (sorted(gaps)[len(gaps) // 2] if gaps else 1.0)
        self.index = 0
        self.lap = 0

    def _due(self, i: int, lap: int) -> float:
        if self.replay.speed == 0:
            return self.start
        offset = self.records[i].time - self.first + lap * self.period
        return self.start + offset / self.replay.speed

    @property
    def next_due(self) -> float | None:
        if self.index >= len(self.records):
            return None
        return self._due(self.index, self.lap)

    def pop_due(self, now: float, timestamp: bool = False) -> list[tuple[str, str]]:  # noqa: FBT001, FBT002
        """(topic, payload) of every reading due by *now*."""
        out = []
        while self.index < len(self.records) and self._due(self.index, self.lap) <= now:
            r = self.records[self.index]
            device = self.replay.devices.get(r.device, r.device)
            payload = dict(r.payload)
            if timestamp:
                payload["ts"] = int(now * 1000)
            out.append((f"sensors/{device}/{r.measurement}", json.dumps(payload)))
            self.index += 1
            if self.index == len(self.records) and self.replay.loop:
                self.index = 0
                self.lap += 1
        return out
