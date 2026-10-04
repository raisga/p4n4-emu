"""Drop-in stub for RPi.GPIO — runs on any platform without hardware.

Usage:
    import sys
    import p4n4_emu.hw.gpio_stub as GPIO
    sys.modules["RPi"] = type(sys)("RPi")
    sys.modules["RPi.GPIO"] = GPIO

Nothing outside the script drives an input pin, so tests and simulations call
set_input() to change its level, as a button or sensor would. Edges fire the
callbacks registered with add_event_detect() / add_event_callback().
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterable

_log = logging.getLogger(__name__)

# Pin numbering modes
BCM = 11
BOARD = 10

# Pin directions
OUT = 0
IN = 1

# Pin states
HIGH = 1
LOW = 0

# Pull resistors (values as in RPi.GPIO)
PUD_OFF = 20
PUD_DOWN = 21
PUD_UP = 22

# Edges
RISING = 31
FALLING = 32
BOTH = 33

_mode: int | None = None
_pins: dict[int, int] = {}
_directions: dict[int, int] = {}
# Per channel: the edge to detect, its debounce time and callbacks, and
# whether an edge was seen since the last event_detected() call
_edges: dict[int, int] = {}
_bouncetimes: dict[int, float] = {}
_last_edge: dict[int, float] = {}
_callbacks: dict[int, list[Callable[[int], None]]] = {}
_detected: dict[int, bool] = {}


def _channels(channel: int | Iterable[int]) -> list[int]:
    """RPi.GPIO accepts one channel or a list / tuple of them."""
    return list(channel) if isinstance(channel, (list, tuple)) else [channel]


def setmode(mode: int) -> None:
    global _mode
    _mode = mode
    _log.debug("GPIO mode set to %s", "BCM" if mode == BCM else "BOARD")


def getmode() -> int | None:
    return _mode


def setwarnings(flag: bool) -> None:  # noqa: FBT001
    _log.debug("GPIO warnings %s", "enabled" if flag else "disabled")


def setup(
    channel: int | Iterable[int],
    direction: int,
    pull_up_down: int = PUD_OFF,
    initial: int | None = None,
) -> None:
    for pin in _channels(channel):
        _directions[pin] = direction
        if direction == OUT:
            if initial is not None:
                _pins[pin] = int(bool(initial))
            else:
                _pins.setdefault(pin, LOW)
        else:
            # An input with a pull-up idles HIGH; otherwise it reads LOW
            _pins[pin] = HIGH if pull_up_down == PUD_UP else LOW
        _log.debug("GPIO pin %d configured as %s", pin, "OUT" if direction == OUT else "IN")


def gpio_function(channel: int) -> int:
    """The pin's configured direction (IN or OUT); IN when not set up."""
    return _directions.get(channel, IN)


def output(channel: int | Iterable[int], state: int | Iterable[int]) -> None:
    pins = _channels(channel)
    states = _channels(state) if isinstance(state, (list, tuple)) else [state] * len(pins)
    if len(states) != len(pins):
        raise RuntimeError("Number of channels != number of values")
    for pin, value in zip(pins, states, strict=True):
        if _directions.get(pin) == IN:
            raise RuntimeError("The GPIO channel has not been set up as an OUTPUT")
        _pins[pin] = int(bool(value))
        _log.debug("GPIO pin %d → %s", pin, "HIGH" if value else "LOW")


def input(channel: int) -> int:  # noqa: A001
    return _pins.get(channel, LOW)


def set_input(channel: int, state: int) -> None:
    """Drive an input pin from outside the script (stub only, not in RPi.GPIO).

    A level change is an edge: it fires the pin's callbacks when it matches
    the edge passed to add_event_detect(), unless it falls within bouncetime.
    """
    level = int(bool(state))
    previous = _pins.get(channel, LOW)
    _pins[channel] = level
    _log.debug("GPIO pin %d driven %s", channel, "HIGH" if level else "LOW")
    if level == previous or channel not in _edges:
        return

    edge = _edges[channel]
    if (edge == RISING and not level) or (edge == FALLING and level):
        return

    now = time.monotonic()
    last = _last_edge.get(channel)
    if last is not None and (now - last) * 1000 < _bouncetimes.get(channel, 0):
        return
    _last_edge[channel] = now

    _detected[channel] = True
    for callback in list(_callbacks.get(channel, [])):
        callback(channel)


def add_event_detect(
    channel: int,
    edge: int,
    callback: Callable[[int], None] | None = None,
    bouncetime: int | None = None,
) -> None:
    if _directions.get(channel) != IN:
        raise RuntimeError("You must setup() the GPIO channel as an input first")
    if channel in _edges:
        raise RuntimeError("Conflicting edge detection already enabled for this GPIO channel")
    if edge not in (RISING, FALLING, BOTH):
        raise ValueError("The edge must be set to RISING, FALLING or BOTH")
    _edges[channel] = edge
    _bouncetimes[channel] = bouncetime or 0
    _callbacks[channel] = [callback] if callback else []
    _detected[channel] = False


def add_event_callback(channel: int, callback: Callable[[int], None]) -> None:
    if channel not in _edges:
        raise RuntimeError(
            "Add event detection using add_event_detect first before adding a callback"
        )
    _callbacks[channel].append(callback)


def remove_event_detect(channel: int) -> None:
    for state in (_edges, _bouncetimes, _last_edge, _callbacks, _detected):
        state.pop(channel, None)


def event_detected(channel: int) -> bool:
    """Whether an edge occurred since the last call; resets the flag."""
    seen = _detected.get(channel, False)
    if channel in _detected:
        _detected[channel] = False
    return seen


def cleanup(channel: int | Iterable[int] | None = None) -> None:
    global _mode
    if channel is not None:
        for pin in _channels(channel):
            remove_event_detect(pin)
            _pins.pop(pin, None)
            _directions.pop(pin, None)
        _log.debug("GPIO cleanup — pins %s cleared", _channels(channel))
        return
    for state in (_pins, _directions, _edges, _bouncetimes, _last_edge, _callbacks, _detected):
        state.clear()
    _mode = None
    _log.debug("GPIO cleanup — all pin state cleared")
