"""Drop-in stub for RPi.GPIO — runs on any platform without hardware.

Usage:
    import sys
    import p4n4_emu.hw.gpio_stub as GPIO
    sys.modules["RPi"] = type(sys)("RPi")
    sys.modules["RPi.GPIO"] = GPIO
"""

from __future__ import annotations

import logging

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

_mode: int | None = None
_pins: dict[int, int] = {}
_directions: dict[int, int] = {}


def setmode(mode: int) -> None:
    global _mode
    _mode = mode
    _log.debug("GPIO mode set to %s", "BCM" if mode == BCM else "BOARD")


def setwarnings(flag: bool) -> None:  # noqa: FBT001
    _log.debug("GPIO warnings %s", "enabled" if flag else "disabled")


def setup(pin: int, direction: int) -> None:
    _directions[pin] = direction
    _pins.setdefault(pin, LOW)
    _log.debug("GPIO pin %d configured as %s", pin, "OUT" if direction == OUT else "IN")


def output(pin: int, state: int) -> None:
    _pins[pin] = int(bool(state))
    _log.debug("GPIO pin %d → %s", pin, "HIGH" if state else "LOW")


def input(pin: int) -> int:  # noqa: A001
    return _pins.get(pin, LOW)


def cleanup() -> None:
    _pins.clear()
    _directions.clear()
    global _mode
    _mode = None
    _log.debug("GPIO cleanup — all pin state cleared")
