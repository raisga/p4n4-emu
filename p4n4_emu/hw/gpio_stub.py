"""Drop-in stub for RPi.GPIO — runs on any platform without hardware.

`p4n4-emu run script.py` makes `import RPi.GPIO` load it. To do it by hand:

    from p4n4_emu.hw import shims
    shims.install()

It follows rpi-lgpio, the RPi.GPIO the Pi 5 runs (the original can't drive its
GPIO): the same errors for a missing setmode() or setup(), BOARD and BCM
numbering, PWM, wait_for_edge(), and RPI_INFO for the emulated board.

Nothing outside the script drives an input pin, so tests and simulations call
set_input() to change its level, as a button or sensor would (or publish to
`emu/gpio/<pin>/set`, see mqtt_bridge). Edges fire the callbacks registered
with add_event_detect() / add_event_callback(), in the thread that drove the pin.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from collections.abc import Callable, Iterable
from weakref import WeakValueDictionary

from p4n4_emu.hw import board, pins

_log = logging.getLogger(__name__)

VERSION = "0.7.2"  # the rpi-lgpio release whose behaviour this follows

UNKNOWN = -1

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

# gpio_function() results for pins in an alternate function
SERIAL = 40
SPI = 41
I2C = 42
HARD_PWM = 43

_PULLS = {PUD_OFF: pins.PULL_OFF, PUD_DOWN: pins.PULL_DOWN, PUD_UP: pins.PULL_UP}


def __getattr__(name: str):
    # The board is chosen when the shims are installed, which may be after import
    if name == "RPI_INFO":
        return board.current().rpi_info()
    if name == "RPI_REVISION":
        return board.current().rpi_info()["P1_REVISION"]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


class _Alert:
    """Edge detection on one GPIO: its edge, debounce time and callbacks."""

    def __init__(self, edge: int, bouncetime: int | None) -> None:
        self.edge = edge
        self.bouncetime = bouncetime or 0
        self.callbacks: list[Callable[[int], None]] = []
        self.detected = False
        self.last_edge: float | None = None


_mode: int | None = None
_warnings = True
_lock = threading.RLock()
_used: set[int] = set()  # GPIOs this module set up; cleanup() releases only those
_alerts: dict[int, _Alert] = {}
_pwms: WeakValueDictionary[int, PWM] = WeakValueDictionary()


def _to_gpio(channel: int) -> int:
    """*channel* in the current numbering mode → BCM GPIO number."""
    if _mode is None:
        raise RuntimeError(
            "Please set pin numbering mode using GPIO.setmode(GPIO.BOARD) or "
            "GPIO.setmode(GPIO.BCM)"
        )
    if _mode == BOARD:
        if channel not in board.BOARD_TO_BCM:
            raise ValueError("The channel sent is invalid on a Raspberry Pi")
        return board.BOARD_TO_BCM[channel]
    if isinstance(channel, bool) or not isinstance(channel, int):
        raise ValueError("Channel must be an integer or list/tuple of integers")
    if not 0 <= channel < board.current().lines:
        raise ValueError("The channel sent is invalid on a Raspberry Pi")
    return channel


def _from_gpio(gpio: int) -> int:
    return board.BCM_TO_BOARD[gpio] if _mode == BOARD else gpio


def _channels(channel: int | Iterable[int]) -> list[int]:
    """RPi.GPIO accepts one channel or a list / tuple of them."""
    return list(channel) if isinstance(channel, (list, tuple)) else [channel]


def setmode(mode: int) -> None:
    global _mode
    if mode not in (BCM, BOARD):
        raise ValueError("An invalid mode was passed to setmode()")
    if _mode is not None and mode != _mode:
        raise ValueError("A different mode has already been set!")
    _mode = mode
    _log.debug("GPIO mode set to %s", "BCM" if mode == BCM else "BOARD")


def getmode() -> int | None:
    return _mode


def setwarnings(flag: bool) -> None:  # noqa: FBT001
    global _warnings
    _warnings = bool(flag)
    _log.debug("GPIO warnings %s", "enabled" if flag else "disabled")


def setup(
    channel: int | Iterable[int],
    direction: int,
    pull_up_down: int = PUD_OFF,
    initial: int | None = None,
) -> None:
    if direction == OUT and pull_up_down != PUD_OFF:
        raise ValueError("pull_up_down parameter is not valid for outputs")
    if direction == IN and initial is not None:
        raise ValueError("initial parameter is not valid for inputs")
    if direction not in (IN, OUT):
        raise ValueError("An invalid direction was passed to setup()")
    if pull_up_down not in _PULLS:
        raise ValueError(
            "Invalid value for pull_up_down - should be either PUD_OFF, PUD_UP or PUD_DOWN"
        )
    for gpio in map(_to_gpio, _channels(channel)):
        _remove_alert(gpio)
        if direction == OUT:
            pins.setup_output(gpio, initial)
        else:
            pins.setup_input(gpio, _PULLS[pull_up_down])
        _used.add(gpio)
        _log.debug("GPIO %d configured as %s", gpio, "OUT" if direction == OUT else "IN")


def gpio_function(channel: int) -> int:
    """The pin's direction (IN or OUT); IN when nothing set it up."""
    return OUT if pins.get(_to_gpio(channel)).function == pins.OUTPUT else IN


def output(channel: int | Iterable[int], state: int | Iterable[int]) -> None:
    gpios = [_to_gpio(c) for c in _channels(channel)]
    states = _channels(state) if isinstance(state, (list, tuple)) else [state] * len(gpios)
    if len(states) != len(gpios):
        raise RuntimeError("Number of channels != number of values")
    for gpio, value in zip(gpios, states, strict=True):
        if pins.get(gpio).function != pins.OUTPUT:
            raise RuntimeError("The GPIO channel has not been set up as an OUTPUT")
        pins.write(gpio, value)
        _log.debug("GPIO %d → %s", gpio, "HIGH" if value else "LOW")


def input(channel: int) -> int:  # noqa: A001
    gpio = _to_gpio(channel)
    if pins.get(gpio).function is None:
        raise RuntimeError("You must setup() the GPIO channel first")
    return pins.read(gpio)


def set_input(channel: int, state: int | None) -> None:
    """Drive an input pin from outside the script (stub only, not in RPi.GPIO).

    *channel* is in the current numbering mode (BCM before setmode()). A
    level change is an edge: it fires the pin's callbacks when it matches the
    edge passed to add_event_detect(), unless it falls within bouncetime.
    None stops driving the pin, so it reads its pull again.
    """
    gpio = _to_gpio(channel) if _mode is not None else pins.check(channel)
    pins.drive(gpio, state)
    _log.debug("GPIO %d driven %s", gpio, {None: "by nothing", 0: "LOW"}.get(state, "HIGH"))


def _on_change(change: pins.Change) -> None:
    """Edges on inputs with edge detection set fire its callbacks."""
    if not change.edge or change.pin.function != pins.INPUT:
        return
    with _lock:
        alert = _alerts.get(change.gpio)
        if alert is None:
            return
        level = change.pin.level
        if (alert.edge == RISING and not level) or (alert.edge == FALLING and level):
            return
        now = time.monotonic()
        if alert.last_edge is not None and (now - alert.last_edge) * 1000 < alert.bouncetime:
            return
        alert.last_edge = now
        alert.detected = True
        callbacks = list(alert.callbacks)
        channel = _from_gpio(change.gpio)
    for callback in callbacks:
        try:
            callback(channel)
        except Exception as exc:  # noqa: BLE001
            # As RPi.GPIO does: report it and carry on with the next callback
            print(exc, file=sys.stderr)


pins.watch(_on_change)


def _check_input(gpio: int) -> None:
    if pins.get(gpio).function != pins.INPUT:
        raise RuntimeError("You must setup() the GPIO channel as an input first")


def _check_edge(edge: int) -> None:
    if edge not in (RISING, FALLING, BOTH):
        raise ValueError("The edge must be set to RISING, FALLING or BOTH")


def _check_bounce(bouncetime: int | None) -> None:
    if bouncetime is not None and bouncetime <= 0:
        raise ValueError("Bouncetime must be greater than 0")


def _remove_alert(gpio: int) -> None:
    with _lock:
        _alerts.pop(gpio, None)


def add_event_detect(
    channel: int,
    edge: int,
    callback: Callable[[int], None] | None = None,
    bouncetime: int | None = None,
) -> None:
    gpio = _to_gpio(channel)
    _check_input(gpio)
    _check_edge(edge)
    _check_bounce(bouncetime)
    if callback is not None and not callable(callback):
        raise TypeError("Parameter must be callable")
    with _lock:
        if gpio in _alerts:
            raise RuntimeError("Conflicting edge detection already enabled for this GPIO channel")
        alert = _Alert(edge, bouncetime)
        if callback is not None:
            alert.callbacks.append(callback)
        _alerts[gpio] = alert


def add_event_callback(channel: int, callback: Callable[[int], None]) -> None:
    if not callable(callback):
        raise TypeError("Parameter must be callable")
    gpio = _to_gpio(channel)
    _check_input(gpio)
    with _lock:
        if gpio not in _alerts:
            raise RuntimeError(
                "Add event detection using add_event_detect first before adding a callback"
            )
        _alerts[gpio].callbacks.append(callback)


def remove_event_detect(channel: int) -> None:
    _remove_alert(_to_gpio(channel))


def event_detected(channel: int) -> bool:
    """Whether an edge occurred since the last call; resets the flag."""
    with _lock:
        alert = _alerts.get(_to_gpio(channel))
        if alert is None or not alert.detected:
            return False
        alert.detected = False
        return True


def wait_for_edge(
    channel: int, edge: int, bouncetime: int | None = None, timeout: int | None = None
) -> int | None:
    """Block until *edge* occurs on *channel*: the channel, or None after *timeout* ms."""
    gpio = _to_gpio(channel)
    _check_input(gpio)
    _check_edge(edge)
    _check_bounce(bouncetime)
    if timeout is not None and timeout <= 0:
        raise ValueError("Timeout must be greater than 0")
    event = threading.Event()

    def _seen(_channel: int) -> None:
        event.set()

    with _lock:
        alert = _alerts.get(gpio)
        added = alert is None
        if added:
            alert = _alerts[gpio] = _Alert(edge, bouncetime)
        elif alert.callbacks:
            raise RuntimeError("Conflicting edge detection already enabled for this GPIO channel")
        alert.callbacks.append(_seen)
    try:
        seen = event.wait(None if timeout is None else timeout / 1000)
    finally:
        with _lock:
            if added and _alerts.get(gpio) is alert:
                del _alerts[gpio]
            elif _seen in alert.callbacks:
                alert.callbacks.remove(_seen)
    return channel if seen else None


class PWM:
    """Software PWM on an output. The emulator records frequency and duty cycle
    (pins.get(gpio).pwm, `emu/gpio/<pin>/pwm`); it doesn't toggle the level."""

    def __init__(self, channel: int, frequency: float) -> None:
        self._gpio = _to_gpio(channel)
        if self._gpio in _pwms:
            raise RuntimeError("A PWM object already exists for this GPIO channel")
        if pins.get(self._gpio).function != pins.OUTPUT:
            raise RuntimeError("You must setup() the GPIO channel as an output first")
        self._frequency = 0.0
        self._dc = 0.0
        self._running = False
        self.ChangeFrequency(frequency)
        _pwms[self._gpio] = self

    def __del__(self) -> None:
        try:
            self.stop()
        except Exception:  # noqa: BLE001 — the pin may have been cleaned up already
            pass

    def start(self, dc: float) -> None:
        self.ChangeDutyCycle(dc)
        self._running = True
        pins.set_pwm(self._gpio, self._frequency, self._dc)

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if pins.get(self._gpio).function == pins.OUTPUT:
            pins.set_pwm(self._gpio, 0, 0)
            pins.write(self._gpio, LOW)

    def ChangeDutyCycle(self, dc: float) -> None:  # noqa: N802
        dc = float(dc)
        if not 0 <= dc <= 100:
            raise ValueError("dutycycle must have a value from 0.0 to 100.0")
        self._dc = dc
        if self._running:
            pins.set_pwm(self._gpio, self._frequency, self._dc)

    def ChangeFrequency(self, frequency: float) -> None:  # noqa: N802
        frequency = float(frequency)
        if frequency <= 0.0:
            raise ValueError("frequency must be greater than 0.0")
        self._frequency = frequency
        if self._running:
            pins.set_pwm(self._gpio, self._frequency, self._dc)


def cleanup(channel: int | Iterable[int] | None = None) -> None:
    """Return the channels (default: every one this module set up) to inputs."""
    global _mode
    if channel is None:
        gpios = sorted(_used)
    else:
        gpios = [_to_gpio(c) for c in _channels(channel)]
    for gpio in gpios:
        pwm = _pwms.pop(gpio, None)
        if pwm is not None:
            pwm.stop()
        _remove_alert(gpio)
        pins.release(gpio)
        _used.discard(gpio)
    if channel is None:
        _mode = None
    _log.debug("GPIO cleanup — %s", "all pins" if channel is None else f"GPIO {gpios}")
