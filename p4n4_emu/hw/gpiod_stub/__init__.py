"""Drop-in stub for gpiod, the libgpiod v2 Python bindings.

`p4n4-emu run` makes `import gpiod` (and gpiod.line …) load it. Covers chips,
line info, line requests (values, reconfiguration, active-low, bias) and edge
events, which a request's fd signals, so select() / poll() on it work. The v1
API (chip.get_line(), line.request()) is not stubbed.
"""

from __future__ import annotations

import errno
import os
import select
import threading
import time
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from enum import Enum

from p4n4_emu.hw import board, pins
from p4n4_emu.hw.gpiod_stub import line
from p4n4_emu.hw.gpiod_stub.line import Bias, Clock, Direction, Drive, Edge, Value

__version__ = "2.2.0"
api_version = "2.2"

__all__ = [
    "Chip", "ChipClosedError", "ChipInfo", "EdgeEvent", "LineInfo", "LineRequest",
    "LineSettings", "RequestReleasedError", "api_version", "is_gpiochip_device", "line",
    "request_lines",
]


class ChipClosedError(Exception):
    def __init__(self) -> None:
        super().__init__("I/O operation on closed chip")


class RequestReleasedError(Exception):
    def __init__(self) -> None:
        super().__init__("GPIO lines have been released")


@dataclass
class LineSettings:
    direction: Direction = Direction.AS_IS
    edge_detection: Edge = Edge.NONE
    bias: Bias = Bias.AS_IS
    drive: Drive = Drive.PUSH_PULL
    active_low: bool = False
    debounce_period: timedelta = field(default_factory=timedelta)
    event_clock: Clock = Clock.MONOTONIC
    output_value: Value = Value.INACTIVE


@dataclass(frozen=True)
class ChipInfo:
    name: str
    label: str
    num_lines: int


@dataclass(frozen=True)
class LineInfo:
    offset: int
    name: str
    used: bool
    consumer: str
    direction: Direction
    active_low: bool
    bias: Bias
    drive: Drive
    edge_detection: Edge
    event_clock: Clock
    debounced: bool
    debounce_period: timedelta


@dataclass(frozen=True, init=False)
class EdgeEvent:
    class Type(Enum):
        RISING_EDGE = 1
        FALLING_EDGE = 2

    event_type: Type
    timestamp_ns: int
    line_offset: int
    global_seqno: int
    line_seqno: int

    def __init__(self, event_type: int | Type, timestamp_ns: int, line_offset: int,
                 global_seqno: int, line_seqno: int) -> None:
        object.__setattr__(self, "event_type", EdgeEvent.Type(event_type))
        object.__setattr__(self, "timestamp_ns", timestamp_ns)
        object.__setattr__(self, "line_offset", line_offset)
        object.__setattr__(self, "global_seqno", global_seqno)
        object.__setattr__(self, "line_seqno", line_seqno)


_lock = threading.RLock()
_requested: dict[int, LineRequest] = {}  # line offset → the request holding it
_seqno = 0

_BIAS = {Bias.PULL_UP: pins.PULL_UP, Bias.PULL_DOWN: pins.PULL_DOWN, Bias.DISABLED: pins.PULL_OFF}


def _chip_number(path: str) -> int:
    path = os.fspath(path)
    prefix = "/dev/gpiochip"
    if not path.startswith(prefix) or not path[len(prefix):].isdigit():
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
    number = int(path[len(prefix):])
    if number not in board.current().chips:
        raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)
    return number


def is_gpiochip_device(path: str) -> bool:
    try:
        _chip_number(path)
    except FileNotFoundError:
        return False
    return True


def _line_name(offset: int) -> str:
    return f"GPIO{offset}" if offset < 28 else ""


def _config_items(config: dict) -> list[tuple[int | str, LineSettings | None]]:
    items = []
    for key, settings in config.items():
        keys = key if isinstance(key, Iterable) and not isinstance(key, str) else (key,)
        items += [(k, settings) for k in keys]
    return items


class _Line:
    """One requested line: its settings and its edge event state."""

    def __init__(self, offset: int, settings: LineSettings) -> None:
        self.offset = offset
        self.settings = settings
        self.seqno = 0
        self.last_event: float | None = None


class Chip:
    def __init__(self, path: str) -> None:
        self._number: int | None = _chip_number(path)
        self._path = os.fspath(path)

    def __bool__(self) -> bool:
        return self._number is not None

    def __enter__(self) -> Chip:
        self._check_closed()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _check_closed(self) -> None:
        if self._number is None:
            raise ChipClosedError()

    def close(self) -> None:
        self._number = None

    @property
    def path(self) -> str:
        self._check_closed()
        return self._path

    def get_info(self) -> ChipInfo:
        self._check_closed()
        b = board.current()
        return ChipInfo(f"gpiochip{self._number}", b.chip_label, b.lines)

    def line_offset_from_id(self, id: str | int) -> int:  # noqa: A002 — gpiod's name
        self._check_closed()
        if isinstance(id, int):
            pins.check(id)
            return id
        for offset in range(board.current().lines):
            if _line_name(offset) == id:
                return offset
        if id.isdigit():
            return self.line_offset_from_id(int(id))
        raise FileNotFoundError(errno.ENOENT, f"line '{id}' not found")

    def get_line_info(self, line: int | str) -> LineInfo:
        offset = self.line_offset_from_id(line)
        pin = pins.get(offset)
        with _lock:
            request = _requested.get(offset)
        settings = request._lines[offset].settings if request else LineSettings()
        direction = Direction.OUTPUT if pin.function == pins.OUTPUT else Direction.INPUT
        bias = {pins.PULL_UP: Bias.PULL_UP, pins.PULL_DOWN: Bias.PULL_DOWN}.get(
            pin.pull, Bias.DISABLED if pin.function else Bias.UNKNOWN
        )
        return LineInfo(
            offset=offset,
            name=_line_name(offset),
            used=request is not None or pin.function is not None,
            consumer=(request._consumer if request else "") or "",
            direction=direction,
            active_low=settings.active_low,
            bias=bias,
            drive=settings.drive,
            edge_detection=settings.edge_detection,
            event_clock=settings.event_clock,
            debounced=settings.debounce_period > timedelta(0),
            debounce_period=settings.debounce_period,
        )

    def request_lines(
        self,
        config: dict,
        consumer: str | None = None,
        event_buffer_size: int | None = None,
        output_values: dict[int | str, Value] | None = None,
    ) -> LineRequest:
        self._check_closed()
        lines: dict[int, _Line] = {}
        names: dict[str, int] = {}
        requested: list[int | str] = []
        for key, settings in _config_items(config):
            offset = self.line_offset_from_id(key)
            if offset in lines:
                raise ValueError(f"line must be configured exactly once - offset {offset} repeats")
            settings = settings or LineSettings()
            if output_values:
                values = {self.line_offset_from_id(k): v for k, v in output_values.items()}
                settings = LineSettings(**{**settings.__dict__,
                                           "output_value": values.get(offset, Value.INACTIVE)})
            lines[offset] = _Line(offset, settings)
            requested.append(key)
            if isinstance(key, str):
                names[key] = offset
            elif _line_name(offset):
                names.setdefault(_line_name(offset), offset)
        with _lock:
            busy = [o for o in lines if o in _requested]
            if busy:
                raise OSError(errno.EBUSY, os.strerror(errno.EBUSY))
            request = LineRequest(f"gpiochip{self._number}", lines, names, requested,
                                  consumer, event_buffer_size or 16)
            for offset in lines:
                _requested[offset] = request
        for offset, ln in lines.items():
            request._apply(offset, ln.settings)
        return request

    def __repr__(self) -> str:
        return f'gpiod.Chip("{self._path}")' if self else "<Chip CLOSED>"


class LineRequest:
    def __init__(self, chip_name: str, lines: dict[int, _Line], names: dict[str, int],
                 requested: list[int | str], consumer: str | None, buffer_size: int) -> None:
        self._chip_name = chip_name
        self._lines = lines
        self._name_map = names
        self._requested_lines = requested
        self._consumer = consumer
        self._events: deque[EdgeEvent] = deque()
        self._buffer_size = buffer_size
        self._read_fd, self._write_fd = os.pipe()
        self._released = False
        self._configuring = False

    def __bool__(self) -> bool:
        return not self._released

    def __enter__(self) -> LineRequest:
        self._check_released()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()

    def __del__(self) -> None:
        try:
            self.release()
        except Exception:  # noqa: BLE001
            pass

    def _check_released(self) -> None:
        if self._released:
            raise RequestReleasedError()

    def _apply(self, offset: int, settings: LineSettings) -> None:
        current = pins.get(offset)
        # As the kernel does, edge detection starts once the line is configured:
        # a level change from the new bias is not an event
        self._configuring = True
        try:
            if settings.direction == Direction.OUTPUT:
                level = int(bool(settings.output_value)) ^ settings.active_low
                pins.setup_output(offset, level)
            elif settings.direction == Direction.INPUT or current.function is None:
                pull = _BIAS.get(settings.bias, current.pull)
                pins.setup_input(offset, pull)
        finally:
            self._configuring = False
        self._lines[offset].settings = settings

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        with _lock:
            for offset in self._lines:
                if _requested.get(offset) is self:
                    del _requested[offset]
        for offset in self._lines:
            pins.release(offset)
        os.close(self._read_fd)
        os.close(self._write_fd)

    def _offset(self, line: int | str) -> int:
        if isinstance(line, int):
            return line
        if line not in self._name_map:
            raise ValueError(f"unknown line name: {line}")
        return self._name_map[line]

    def _line(self, line: int | str) -> _Line:
        offset = self._offset(line)
        if offset not in self._lines:
            raise ValueError(f"line {line!r} is not part of this request")
        return self._lines[offset]

    def get_value(self, line: int | str) -> Value:
        return self.get_values([line])[0]

    def get_values(self, lines: Iterable[int | str] | None = None) -> list[Value]:
        self._check_released()
        out = []
        for ln in map(self._line, lines or self._requested_lines):
            level = pins.read(ln.offset) ^ ln.settings.active_low
            out.append(Value.ACTIVE if level else Value.INACTIVE)
        return out

    def set_value(self, line: int | str, value: Value) -> None:
        self.set_values({line: value})

    def set_values(self, values: dict[int | str, Value]) -> None:
        self._check_released()
        for key, value in values.items():
            ln = self._line(key)
            if pins.get(ln.offset).function != pins.OUTPUT:
                raise PermissionError(errno.EPERM, os.strerror(errno.EPERM))
            pins.write(ln.offset, int(bool(value)) ^ ln.settings.active_low)

    def reconfigure_lines(self, config: dict) -> None:
        self._check_released()
        given = {self._offset(k): s for k, s in _config_items(config)}
        for offset in self._lines:
            self._apply(offset, given.get(offset) or LineSettings())

    def _edge(self, offset: int, level: int) -> None:
        """A level change on one of the lines: queue an event if its settings ask."""
        global _seqno
        ln = self._lines[offset]
        settings = ln.settings
        if settings.edge_detection == Edge.NONE or self._released or self._configuring:
            return
        logical = level ^ settings.active_low
        wanted = {Edge.RISING: logical == 1, Edge.FALLING: logical == 0}
        if not wanted.get(settings.edge_detection, True):
            return
        now = time.monotonic()
        debounce = settings.debounce_period.total_seconds()
        if debounce and ln.last_event is not None and now - ln.last_event < debounce:
            return
        ln.last_event = now
        with _lock:
            if len(self._events) >= self._buffer_size:
                return  # the kernel's event buffer is full: the event is lost
            _seqno += 1
            ln.seqno += 1
            event = EdgeEvent(
                EdgeEvent.Type.RISING_EDGE if logical else EdgeEvent.Type.FALLING_EDGE,
                time.time_ns() if settings.event_clock == Clock.REALTIME else time.monotonic_ns(),
                offset, _seqno, ln.seqno,
            )
            self._events.append(event)
            os.write(self._write_fd, b"\0")

    def wait_edge_events(self, timeout: timedelta | float | None = None) -> bool:
        self._check_released()
        if isinstance(timeout, timedelta):
            timeout = timeout.total_seconds()
        with _lock:
            if self._events:
                return True
        ready, _, _ = select.select([self._read_fd], [], [], timeout)
        return bool(ready)

    def read_edge_events(self, max_events: int | None = None) -> list[EdgeEvent]:
        """Pending events (up to *max_events*); blocks until there is one."""
        self._check_released()
        while True:
            with _lock:
                if self._events:
                    count = len(self._events)
                    if max_events is not None:
                        count = min(max_events, count)
                    events = [self._events.popleft() for _ in range(count)]
                    os.read(self._read_fd, count)
                    return events
            select.select([self._read_fd], [], [])

    def fileno(self) -> int:
        self._check_released()
        return self._read_fd

    @property
    def fd(self) -> int:
        return self.fileno()

    @property
    def chip_name(self) -> str:
        self._check_released()
        return self._chip_name

    @property
    def num_lines(self) -> int:
        self._check_released()
        return len(self._lines)

    @property
    def offsets(self) -> list[int]:
        self._check_released()
        return list(self._lines)

    @property
    def lines(self) -> list[int | str]:
        self._check_released()
        return list(self._requested_lines)


def _on_change(change: pins.Change) -> None:
    if not change.edge or change.pin.function != pins.INPUT:
        return
    with _lock:
        request = _requested.get(change.gpio)
    if request is not None:
        request._edge(change.gpio, change.pin.level)


pins.watch(_on_change)


def request_lines(
    path: str,
    config: dict,
    consumer: str | None = None,
    event_buffer_size: int | None = None,
    output_values: dict[int | str, Value] | None = None,
) -> LineRequest:
    with Chip(path) as chip:
        return chip.request_lines(config, consumer, event_buffer_size, output_values)


def _reset() -> None:
    """Release every request (between tests)."""
    with _lock:
        requests = set(_requested.values())
    for request in requests:
        request.release()
