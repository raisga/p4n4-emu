"""The emulated GPIO lines, shared by every GPIO library stub.

RPi.GPIO (gpio_stub), lgpio (lgpio_stub) and gpiod (gpiod_stub) are front ends
over this one set of lines, numbered by BCM GPIO, so gpiozero on lgpio, a
script on RPi.GPIO and the MQTT control channel all see the same pins.

An input's level is what drives it from outside (drive(): a button, a sensor,
`emu/gpio/<pin>/set`), or its pull when nothing does. Watchers see every
change, from any thread; the front ends turn changes on inputs into edge events.

An output running PWM toggles: it reads high for the duty cycle's part of each
period. Up to TOGGLE_MAX_HZ every toggle is a change watchers see (a blinking
LED on `emu/gpio/<pin>/state`); faster PWM only shows in what a read returns,
so a dimmed LED or a buzzer can't flood the watchers.
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

TOGGLE_MAX_HZ = 10.0


@dataclass(frozen=True)
class Pin:
    level: int = 0
    function: str | None = None  # INPUT, OUTPUT, or None while nothing has claimed it
    pull: str = PULL_OFF
    driven: int | None = None  # level forced from outside, None when nothing drives it
    pwm: tuple[float, float] | None = None  # (frequency in Hz, duty cycle in %)
    pwm_since_ns: int = 0  # when PWM started (monotonic): where in its cycle it is


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
_wake = threading.Condition(_lock)  # PWM changed: the toggler recomputes its next edge
_toggler: threading.Thread | None = None


class PinError(ValueError):
    pass


def check(gpio: int) -> int:
    lines = board.current().lines
    if isinstance(gpio, bool) or not isinstance(gpio, int) or not 0 <= gpio < lines:
        raise PinError(f"GPIO {gpio!r} does not exist on {board.current().model} (0-{lines - 1})")
    return gpio


def pwm_level(pin: Pin, now_ns: int) -> int:
    """The level of a PWM output at *now_ns*: high for the first duty % of each period."""
    frequency, duty = pin.pwm
    if duty <= 0:
        return 0
    if duty >= 100:
        return 1
    cycle = (now_ns - pin.pwm_since_ns) * frequency / 1e9 % 1.0
    return int(cycle < duty / 100)


def get(gpio: int) -> Pin:
    with _lock:
        pin = _pins.get(check(gpio), Pin())
    if pin.pwm is not None and pin.function == OUTPUT:
        return replace(pin, level=pwm_level(pin, time.monotonic_ns()))
    return pin


def read(gpio: int) -> int:
    return get(gpio).level


def _input_level(pin: Pin) -> int:
    if pin.driven is not None:
        return pin.driven
    return 1 if pin.pull == PULL_UP else 0


def _update(gpio: int, only_if: Callable[[Pin], bool] | None = None, **changes) -> None:
    with _lock:
        previous = _pins.get(check(gpio), Pin())
        if only_if is not None and not only_if(previous):
            return
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
    """Start or change PWM on an output; a frequency of 0 stops it, leaving it low.

    A change of frequency or duty cycle keeps the cycle's phase, as a running
    PWM generator does.
    """
    global _toggler
    with _lock:
        current = _pins.get(check(gpio), Pin())
    if current.function != OUTPUT:
        raise PinError(f"GPIO {gpio} is not an output")
    # Watchers run outside the lock (see _update), so the front ends' locks never nest in it
    if frequency <= 0:
        _update(gpio, pwm=None, level=0)
        return
    now = time.monotonic_ns()
    pin = replace(current, pwm=(float(frequency), float(duty)),
                  pwm_since_ns=current.pwm_since_ns if current.pwm else now)
    _update(gpio, pwm=pin.pwm, pwm_since_ns=pin.pwm_since_ns, level=pwm_level(pin, now))
    with _lock:
        if _toggler is None and _toggles(pin):
            _toggler = threading.Thread(target=_toggle_loop, name="p4n4-emu-pwm", daemon=True)
            _toggler.start()
        _wake.notify_all()


def _toggles(pin: Pin) -> bool:
    """Whether *pin* runs PWM whose toggles watchers see."""
    return (
        pin.function == OUTPUT and pin.pwm is not None
        and pin.pwm[0] <= TOGGLE_MAX_HZ and 0 < pin.pwm[1] < 100
    )


def _next_edge(pin: Pin, now_ns: int) -> int:
    frequency, duty = pin.pwm
    period = 1e9 / frequency
    into = (now_ns - pin.pwm_since_ns) % period
    high = period * duty / 100
    return now_ns + int((high - into) if into < high else (period - into)) + 1


def _toggle_loop() -> None:
    """Turn slow PWM into level changes, at each edge, until no pin toggles."""
    global _toggler
    while True:
        with _lock:
            now = time.monotonic_ns()
            toggling = {g: p for g, p in _pins.items() if _toggles(p)}
            if not toggling:
                _toggler = None
                return
            levels = {g: pwm_level(p, now) for g, p in toggling.items()}
            due = {g: level for g, level in levels.items() if level != toggling[g].level}
        for gpio, level in due.items():
            # Unless PWM stopped or changed meanwhile
            pwm = toggling[gpio].pwm
            _update(gpio, only_if=lambda p, pwm=pwm: p.pwm == pwm, level=level)
        with _lock:
            now = time.monotonic_ns()
            toggling = [p for p in _pins.values() if _toggles(p)]
            if toggling:
                _wake.wait(max(0.0, (min(_next_edge(p, now) for p in toggling) - now) / 1e9))


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
        _wake.notify_all()
