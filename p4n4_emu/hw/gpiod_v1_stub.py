"""Drop-in stub for gpiod, the libgpiod v1 Python bindings (python3-libgpiod 1.6).

Raspberry Pi OS bookworm packages these, so older scripts use them:
`p4n4-emu run --gpiod v1` makes `import gpiod` load this module instead of the
v2 stub. Covers chips (opened by path, name, label or number), lines and line
bulks (requests, values, reconfiguration, flags) and edge events, which
event_get_fd() signals, so select() / poll() on it work.

Errors behave as in the bindings: OSError with the errno the kernel gives
(EBUSY for a line in use, EPERM for a value or event the request doesn't allow),
ValueError for a closed chip.
"""

from __future__ import annotations

import errno
import os
import select
import threading
import time
from collections import deque
from collections.abc import Iterable, Iterator

from p4n4_emu.hw import board, pins

__version__ = "1.6.3"

# Request types
LINE_REQ_DIR_AS_IS = 1
LINE_REQ_DIR_IN = 2
LINE_REQ_DIR_OUT = 3
LINE_REQ_EV_FALLING_EDGE = 4
LINE_REQ_EV_RISING_EDGE = 5
LINE_REQ_EV_BOTH_EDGES = 6
_EVENT_TYPES = (LINE_REQ_EV_FALLING_EDGE, LINE_REQ_EV_RISING_EDGE, LINE_REQ_EV_BOTH_EDGES)

# Request flags
LINE_REQ_FLAG_OPEN_DRAIN = 1
LINE_REQ_FLAG_OPEN_SOURCE = 2
LINE_REQ_FLAG_ACTIVE_LOW = 4
LINE_REQ_FLAG_BIAS_DISABLE = 8
LINE_REQ_FLAG_BIAS_PULL_DOWN = 16
LINE_REQ_FLAG_BIAS_PULL_UP = 32

# The kernel's v1 event queue holds 16 events per line
_EVENT_BUFFER = 16


def version_string() -> str:
    return __version__


def _error(code: int) -> OSError:
    return OSError(code, os.strerror(code))


class _State:
    """A line of one open chip: its request, if any, and its pending events."""

    def __init__(self, offset: int) -> None:
        self.offset = offset
        self.consumer: str | None = None
        self.type = 0  # LINE_REQ_*, 0 while not requested
        self.flags = 0
        self.events: deque[LineEvent] = deque()
        self.source: Line | None = None  # the line object it was requested through
        self.read_fd: int | None = None
        self.write_fd: int | None = None

    @property
    def requested(self) -> bool:
        return self.type != 0

    @property
    def active_low(self) -> bool:
        return bool(self.flags & LINE_REQ_FLAG_ACTIVE_LOW)


_lock = threading.RLock()
_held: dict[int, _State] = {}  # offset → the requested line holding it


def _line_name(offset: int) -> str | None:
    return f"GPIO{offset}" if offset < 28 else None


def _open_chip(descr: str, how: int) -> int:
    """The gpiochip number *descr* names, looked up as *how* says."""
    b = board.current()
    names = {f"gpiochip{n}": n for n in b.chips}
    candidates = {
        Chip.OPEN_BY_PATH: lambda: names.get(descr[len("/dev/"):]) if descr.startswith("/dev/")
        else None,
        Chip.OPEN_BY_NAME: lambda: names.get(descr),
        Chip.OPEN_BY_LABEL: lambda: b.chips[0] if descr == b.chip_label else None,
        Chip.OPEN_BY_NUMBER: lambda: int(descr) if descr.isdigit() and int(descr) in b.chips
        else None,
    }
    order = candidates if how == Chip.OPEN_LOOKUP else {how: candidates.get(how)}
    for lookup in order.values():
        number = lookup() if lookup else None
        if number is not None:
            return number
    if how not in (Chip.OPEN_LOOKUP, *candidates):
        raise ValueError("Invalid value for 'how' argument")
    raise FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), descr)


class Chip:
    OPEN_LOOKUP = 1
    OPEN_BY_PATH = 2
    OPEN_BY_NAME = 3
    OPEN_BY_LABEL = 4
    OPEN_BY_NUMBER = 5

    def __init__(self, descr: str, how: int = OPEN_LOOKUP) -> None:
        self._number: int | None = _open_chip(str(descr), how)
        self._lines: dict[int, _State] = {}

    def __enter__(self) -> Chip:
        self._check()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"'{self.name()}'" if self._number is not None else "<closed chip>"

    def _check(self) -> None:
        if self._number is None:
            raise ValueError("I/O operation on closed file")

    def close(self) -> None:
        if self._number is None:
            return
        for state in self._lines.values():
            _release(state)
        self._number = None

    def name(self) -> str:
        self._check()
        return f"gpiochip{self._number}"

    def label(self) -> str:
        self._check()
        return board.current().chip_label

    def num_lines(self) -> int:
        self._check()
        return board.current().lines

    def get_line(self, offset: int) -> Line:
        self._check()
        if isinstance(offset, bool) or not isinstance(offset, int):
            raise TypeError("an integer is required")
        if not 0 <= offset < board.current().lines:
            raise _error(errno.EINVAL)
        state = self._lines.setdefault(offset, _State(offset))
        return Line(self, state)

    def find_line(self, name: str) -> Line | None:
        self._check()
        for offset in range(board.current().lines):
            if _line_name(offset) == name:
                return self.get_line(offset)
        return None

    def get_lines(self, offsets: Iterable[int]) -> LineBulk:
        return LineBulk([self.get_line(o) for o in offsets])

    def get_all_lines(self) -> LineBulk:
        return self.get_lines(range(self.num_lines()))

    def find_lines(self, names: Iterable[str]) -> LineBulk | None:
        lines = [self.find_line(n) for n in names]
        if any(ln is None for ln in lines):
            return None
        return LineBulk(lines)


class LineEvent:
    RISING_EDGE = 1
    FALLING_EDGE = 2

    def __init__(self, source: Line, type: int, timestamp_ns: int) -> None:  # noqa: A002
        self.source = source
        self.type = type
        self.sec, self.nsec = divmod(timestamp_ns, 1_000_000_000)

    def __repr__(self) -> str:
        kind = "RISING EDGE" if self.type == self.RISING_EDGE else "FALLING EDGE"
        return f"{kind:<12} {self.sec:>7}.{self.nsec:09} line: {self.source.offset()}"


class Line:
    DIRECTION_INPUT = 1
    DIRECTION_OUTPUT = 2
    ACTIVE_HIGH = 1
    ACTIVE_LOW = 2
    BIAS_AS_IS = 1
    BIAS_DISABLE = 2
    BIAS_PULL_UP = 3
    BIAS_PULL_DOWN = 4

    def __init__(self, chip: Chip, state: _State) -> None:
        self._chip = chip
        self._state = state

    def __repr__(self) -> str:
        return f"'{self.name() or 'unnamed'}'"

    def _check(self) -> _State:
        self._chip._check()
        return self._state

    def owner(self) -> Chip:
        return self._chip

    def offset(self) -> int:
        return self._check().offset

    def name(self) -> str | None:
        return _line_name(self._check().offset)

    def consumer(self) -> str | None:
        state = self._check()
        with _lock:
            holder = _held.get(state.offset)
        return holder.consumer if holder is not None else None

    def direction(self) -> int:
        pin = pins.get(self._check().offset)
        return self.DIRECTION_OUTPUT if pin.function == pins.OUTPUT else self.DIRECTION_INPUT

    def active_state(self) -> int:
        return self.ACTIVE_LOW if self._check().active_low else self.ACTIVE_HIGH

    def bias(self) -> int:
        state = self._check()
        if state.flags & LINE_REQ_FLAG_BIAS_PULL_UP:
            return self.BIAS_PULL_UP
        if state.flags & LINE_REQ_FLAG_BIAS_PULL_DOWN:
            return self.BIAS_PULL_DOWN
        if state.flags & LINE_REQ_FLAG_BIAS_DISABLE:
            return self.BIAS_DISABLE
        return self.BIAS_AS_IS

    def is_used(self) -> bool:
        state = self._check()
        with _lock:
            return state.offset in _held or pins.get(state.offset).function is not None

    def is_open_drain(self) -> bool:
        return bool(self._check().flags & LINE_REQ_FLAG_OPEN_DRAIN)

    def is_open_source(self) -> bool:
        return bool(self._check().flags & LINE_REQ_FLAG_OPEN_SOURCE)

    def is_requested(self) -> bool:
        return self._check().requested

    def update(self) -> None:
        self._check()

    def request(
        self,
        consumer: str | None = None,
        type: int = LINE_REQ_DIR_AS_IS,  # noqa: A002 — the bindings' keyword
        flags: int = 0,
        default_val: int | None = None,
        default_vals: Iterable[int] | None = None,
    ) -> None:
        if default_val is not None and default_vals is not None:
            raise TypeError(
                "Cannot pass both default_val and default_vals arguments at the same time"
            )
        if default_val is not None:
            default_vals = [default_val]
        LineBulk([self]).request(consumer, type, flags, default_vals)

    def release(self) -> None:
        LineBulk([self]).release()

    def get_value(self) -> int:
        return LineBulk([self]).get_values()[0]

    def set_value(self, value: int) -> None:
        LineBulk([self]).set_values([value])

    def set_config(self, direction: int, flags: int, value: int = 0) -> None:
        LineBulk([self]).set_config(direction, flags, [value])

    def set_flags(self, flags: int) -> None:
        LineBulk([self]).set_flags(flags)

    def set_direction_input(self) -> None:
        LineBulk([self]).set_direction_input()

    def set_direction_output(self, value: int = 0) -> None:
        LineBulk([self]).set_direction_output([value])

    def event_wait(self, sec: int = 0, nsec: int = 0) -> bool:
        return LineBulk([self]).event_wait(sec, nsec) is not None

    def event_read(self) -> LineEvent:
        """The oldest pending event; blocks until there is one."""
        state = self._events()
        while True:
            with _lock:
                if state.events:
                    os.read(state.read_fd, 1)
                    return state.events.popleft()
            select.select([state.read_fd], [], [])

    def event_read_multiple(self) -> list[LineEvent]:
        state = self._events()
        while True:
            with _lock:
                if state.events:
                    events = list(state.events)
                    state.events.clear()
                    os.read(state.read_fd, len(events))
                    return events
            select.select([state.read_fd], [], [])

    def event_get_fd(self) -> int:
        return self._events().read_fd

    def _events(self) -> _State:
        state = self._check()
        if state.type not in _EVENT_TYPES:
            raise _error(errno.EPERM)
        return state


def _pull(flags: int) -> str:
    if flags & LINE_REQ_FLAG_BIAS_PULL_UP:
        return pins.PULL_UP
    if flags & LINE_REQ_FLAG_BIAS_PULL_DOWN:
        return pins.PULL_DOWN
    return pins.PULL_OFF


def _configure(state: _State, kind: int, flags: int, value: int) -> None:
    state.type, state.flags = kind, flags
    if kind == LINE_REQ_DIR_OUT:
        pins.setup_output(state.offset, int(bool(value)) ^ state.active_low)
    elif kind != LINE_REQ_DIR_AS_IS or pins.get(state.offset).function is None:
        pins.setup_input(state.offset, _pull(flags))


def _release(state: _State) -> None:
    with _lock:
        if not state.requested:
            return
        if _held.get(state.offset) is state:
            del _held[state.offset]
        state.type, state.flags, state.consumer, state.source = 0, 0, None, None
        state.events.clear()
        fds = (state.read_fd, state.write_fd)
        state.read_fd = state.write_fd = None
    pins.release(state.offset)
    for fd in fds:
        if fd is not None:
            os.close(fd)


class LineBulk:
    def __init__(self, lines: Iterable[Line]) -> None:
        self._lines = list(lines)
        if not all(isinstance(ln, Line) for ln in self._lines):
            raise TypeError("Argument must be a non-empty sequence of GPIO line objects")
        chips = {id(ln.owner()) for ln in self._lines}
        if len(chips) > 1:
            raise ValueError("LineBulk cannot contain lines from different chips")

    def __len__(self) -> int:
        return len(self._lines)

    def __iter__(self) -> Iterator[Line]:
        return iter(self._lines)

    def __repr__(self) -> str:
        return f"[{', '.join(map(repr, self._lines))}]"

    def to_list(self) -> list[Line]:
        return list(self._lines)

    def _states(self) -> list[_State]:
        return [ln._check() for ln in self._lines]

    def _requested(self) -> list[_State]:
        states = self._states()
        if not all(s.requested for s in states):
            raise _error(errno.EPERM)
        return states

    def request(
        self,
        consumer: str | None = None,
        type: int = LINE_REQ_DIR_AS_IS,  # noqa: A002
        flags: int = 0,
        default_vals: Iterable[int] | None = None,
    ) -> None:
        states = self._states()
        kind = type or LINE_REQ_DIR_AS_IS
        if kind not in (LINE_REQ_DIR_AS_IS, LINE_REQ_DIR_IN, LINE_REQ_DIR_OUT, *_EVENT_TYPES):
            raise ValueError("Invalid line request type")
        values = list(default_vals or [])
        values += [0] * (len(states) - len(values))
        with _lock:
            if any(s.requested or s.offset in _held for s in states):
                raise _error(errno.EBUSY)
            for ln, s in zip(self._lines, states, strict=True):
                _held[s.offset] = s
                s.consumer = consumer or "?"
                s.source = ln
                if kind in _EVENT_TYPES:
                    s.read_fd, s.write_fd = os.pipe()
        for s, value in zip(states, values, strict=False):
            # Edge detection starts once the line is configured
            _configure(s, LINE_REQ_DIR_IN if kind in _EVENT_TYPES else kind, flags, value)
            s.type = kind

    def release(self) -> None:
        for s in self._states():
            _release(s)

    def get_values(self) -> list[int]:
        return [pins.read(s.offset) ^ s.active_low for s in self._requested()]

    def set_values(self, values: Iterable[int]) -> None:
        states = self._requested()
        for s, value in zip(states, values, strict=False):
            if pins.get(s.offset).function != pins.OUTPUT:
                raise _error(errno.EPERM)
            pins.write(s.offset, int(bool(value)) ^ s.active_low)

    def set_config(self, direction: int, flags: int, values: Iterable[int] | None = None) -> None:
        states = self._requested()
        if any(s.type in _EVENT_TYPES for s in states):
            raise _error(errno.EPERM)
        if direction not in (LINE_REQ_DIR_AS_IS, LINE_REQ_DIR_IN, LINE_REQ_DIR_OUT):
            raise ValueError("Invalid direction")
        levels = list(values or [])
        levels += [0] * (len(states) - len(levels))
        for s, value in zip(states, levels, strict=False):
            kind = direction
            if kind == LINE_REQ_DIR_AS_IS:
                is_output = pins.get(s.offset).function == pins.OUTPUT
                kind = LINE_REQ_DIR_OUT if is_output else LINE_REQ_DIR_IN
                value = pins.read(s.offset) ^ bool(flags & LINE_REQ_FLAG_ACTIVE_LOW)
            _configure(s, kind, flags, value)

    def set_flags(self, flags: int) -> None:
        self.set_config(LINE_REQ_DIR_AS_IS, flags)

    def set_direction_input(self) -> None:
        for s in self._requested():
            if s.type in _EVENT_TYPES:
                raise _error(errno.EPERM)
            _configure(s, LINE_REQ_DIR_IN, s.flags, 0)

    def set_direction_output(self, values: Iterable[int] | None = None) -> None:
        states = self._requested()
        levels = list(values or [])
        levels += [0] * (len(states) - len(levels))
        for s, value in zip(states, levels, strict=False):
            if s.type in _EVENT_TYPES:
                raise _error(errno.EPERM)
            _configure(s, LINE_REQ_DIR_OUT, s.flags, value)

    def event_wait(self, sec: int = 0, nsec: int = 0) -> LineBulk | None:
        """The lines with pending events, waiting up to *sec* + *nsec*; None on a timeout."""
        states = self._requested()
        if any(s.type not in _EVENT_TYPES for s in states):
            raise _error(errno.EPERM)
        deadline = time.monotonic() + sec + nsec / 1e9
        while True:
            with _lock:
                ready = [ln for ln, s in zip(self._lines, states, strict=True) if s.events]
            if ready:
                return LineBulk(ready)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            select.select([s.read_fd for s in states], [], [], remaining)


class LineIter:
    def __init__(self, chip: Chip) -> None:
        self._lines = iter(chip.get_all_lines())

    def __iter__(self) -> LineIter:
        return self

    def __next__(self) -> Line:
        return next(self._lines)


class ChipIter:
    def __init__(self) -> None:
        self._chips = iter(board.current().chips)

    def __iter__(self) -> ChipIter:
        return self

    def __next__(self) -> Chip:
        return Chip(f"gpiochip{next(self._chips)}")


def find_line(name: str) -> Line | None:
    for chip in ChipIter():
        line = chip.find_line(name)
        if line is not None:
            return line
    return None


def _on_change(change: pins.Change) -> None:
    if not change.edge or change.pin.function != pins.INPUT:
        return
    with _lock:
        state = _held.get(change.gpio)
        if state is None or state.type not in _EVENT_TYPES or state.source is None:
            return
        rising = (change.pin.level ^ state.active_low) == 1
        wanted = {LINE_REQ_EV_RISING_EDGE: rising, LINE_REQ_EV_FALLING_EDGE: not rising}
        if not wanted.get(state.type, True) or len(state.events) >= _EVENT_BUFFER:
            return
        kind = LineEvent.RISING_EDGE if rising else LineEvent.FALLING_EDGE
        state.events.append(LineEvent(state.source, kind, change.timestamp_ns))
        os.write(state.write_fd, b"\0")


pins.watch(_on_change)


def _reset() -> None:
    """Release every requested line (between tests)."""
    with _lock:
        states = list(_held.values())
    for state in states:
        _release(state)
