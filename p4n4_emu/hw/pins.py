"""The emulated GPIO lines, shared by every GPIO library stub.

RPi.GPIO (gpio_stub), lgpio (lgpio_stub) and gpiod (gpiod_stub) are front ends
over this one set of lines, numbered by BCM GPIO, so gpiozero on lgpio, a
script on RPi.GPIO and the MQTT control channel all see the same pins.

An input's level is what drives it from outside (drive(): a button, a sensor,
`emu/gpio/<pin>/set`), or its pull when nothing does. Watchers see every
change, from any thread; the front ends turn changes on inputs into edge events.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace

from p4n4_emu.hw import board

INPUT = "input"
OUTPUT = "output"

PULL_OFF = "off"
PULL_UP = "up"
PULL_DOWN = "down"


@dataclass(frozen=True)
class Pin:
    level: int = 0
    function: str | None = None  # INPUT, OUTPUT, or None while nothing has claimed it
    pull: str = PULL_OFF
    driven: int | None = None  # level forced from outside, None when nothing drives it
    pwm: tuple[float, float] | None = None  # (frequency in Hz, duty cycle in %)


@dataclass(frozen=True)
class Change:
    """A pin's level, function or PWM changed; *timestamp_ns* is on the monotonic clock."""

    gpio: int
    pin: Pin
    previous: Pin
    timestamp_ns: int

    @property
    def edge(self) -> bool:
        return self.pin.level != self.previous.level


Watcher = Callable[[Change], None]

_lock = threading.RLock()
_pins: dict[int, Pin] = {}
_watchers: list[Watcher] = []


class PinError(ValueError):
    pass


def check(gpio: int) -> int:
    lines = board.current().lines
    if isinstance(gpio, bool) or not isinstance(gpio, int) or not 0 <= gpio < lines:
        raise PinError(f"GPIO {gpio!r} does not exist on {board.current().model} (0-{lines - 1})")
    return gpio


def get(gpio: int) -> Pin:
    with _lock:
        return _pins.get(check(gpio), Pin())


def read(gpio: int) -> int:
    return get(gpio).level


def _input_level(pin: Pin) -> int:
    if pin.driven is not None:
        return pin.driven
    return 1 if pin.pull == PULL_UP else 0


def _update(gpio: int, **changes) -> None:
    with _lock:
        previous = _pins.get(check(gpio), Pin())
        pin = replace(previous, **changes)
        if pin.function != OUTPUT:
            pin = replace(pin, level=_input_level(pin), pwm=None)
        _pins[gpio] = pin
        watchers = list(_watchers)
    if (pin.level, pin.function, pin.pwm) != (previous.level, previous.function, previous.pwm):
        change = Change(gpio, pin, previous, time.monotonic_ns())
        for watcher in watchers:
            watcher(change)


def setup_input(gpio: int, pull: str = PULL_OFF) -> None:
    _update(gpio, function=INPUT, pull=pull)


def setup_output(gpio: int, level: int | None = None) -> None:
    """Make *gpio* an output at *level*, or at the level it reads now."""
    current = get(gpio)
    _update(
        gpio,
        function=OUTPUT,
        pull=PULL_OFF,
        level=current.level if level is None else int(bool(level)),
    )


def release(gpio: int) -> None:
    """Nothing claims the pin any more: it floats (reads LOW) unless driven."""
    _update(gpio, function=None, pull=PULL_OFF)


def write(gpio: int, level: int) -> None:
    if get(gpio).function != OUTPUT:
        raise PinError(f"GPIO {gpio} is not an output")
    _update(gpio, level=int(bool(level)))


def drive(gpio: int, level: int | None) -> None:
    """Drive *gpio* from outside the board, as a button or sensor would.

    None stops driving it, so it falls back to its pull. An output can't be
    driven: two outputs on one line is a short circuit.
    """
    if get(gpio).function == OUTPUT:
        raise PinError(f"GPIO {gpio} is an output; it can't be driven from outside")
    _update(gpio, driven=None if level is None else int(bool(level)))


def set_pwm(gpio: int, frequency: float, duty: float) -> None:
    """Start or change PWM on an output; a frequency of 0 stops it."""
    if get(gpio).function != OUTPUT:
        raise PinError(f"GPIO {gpio} is not an output")
    _update(gpio, pwm=(float(frequency), float(duty)) if frequency > 0 else None)


def claimed() -> dict[int, Pin]:
    """Every pin something has set up or driven, by GPIO number."""
    with _lock:
        return dict(_pins)


def watch(watcher: Watcher) -> None:
    with _lock:
        _watchers.append(watcher)


def unwatch(watcher: Watcher) -> None:
    with _lock:
        if watcher in _watchers:
            _watchers.remove(watcher)


def reset() -> None:
    """Forget every pin (between tests); watchers stay."""
    with _lock:
        _pins.clear()
