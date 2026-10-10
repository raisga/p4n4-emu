"""Drop-in stub for lgpio, the GPIO library of the Pi 5.

`p4n4-emu run` makes `import lgpio` load it, which also brings up gpiozero:
its default pin factory on a Pi 5 is lgpio. Covers the gpiochip / GPIO / group /
PWM / alert calls, and the I2C and SPI calls on the emulated buses; the serial
and notification-pipe calls are not stubbed.

Errors behave as in lgpio: a negative status, raised as lgpio.error with its
text while `exceptions` is True (the default).
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Iterable

from p4n4_emu.hw import board, buses, pins

LGPIO_PY_VERSION = 0x00020200

exceptions = True

OFF = LOW = CLEAR = 0
ON = HIGH = SET = 1
TIMEOUT = 2
GROUP_ALL = 0xFFFFFFFFFFFFFFFF

# Line flags
SET_ACTIVE_LOW = 4
SET_OPEN_DRAIN = 8
SET_OPEN_SOURCE = 16
SET_PULL_UP = 32
SET_PULL_DOWN = 64
SET_PULL_NONE = 128
# Edge flags
RISING_EDGE = 1
FALLING_EDGE = 2
BOTH_EDGES = 3
# tx kinds
TX_PWM = 0
TX_WAVE = 1
SPI_MODE_0, SPI_MODE_1, SPI_MODE_2, SPI_MODE_3 = 0, 1, 2, 3

# Error codes (the ones the stub returns)
OKAY = 0
BAD_HANDLE = -5
I2C_OPEN_FAILED = -26
SPI_OPEN_FAILED = -28
BAD_I2C_BUS = -29
BAD_I2C_ADDR = -30
BAD_SPI_CHANNEL = -31
BAD_I2C_PARAM = -39
I2C_WRITE_FAILED = -41
I2C_READ_FAILED = -42
BAD_SPI_COUNT = -43
BAD_EVENT_REQUEST = -72
BAD_GPIO_NUMBER = -73
CANNOT_OPEN_CHIP = -78
GPIO_BUSY = -79
GPIO_NOT_ALLOCATED = -80
NOT_A_GPIOCHIP = -81
NOT_GROUP_LEADER = -87
BAD_DEBOUNCE_MICS = -98
BAD_WATCHDOG_MICS = -99
BAD_SERVO_FREQ = -100
BAD_SERVO_WIDTH = -101
BAD_PWM_FREQ = -102
BAD_PWM_DUTY = -103
GPIO_NOT_AN_OUTPUT = -104

_ERRORS = {
    OKAY: "No error",
    BAD_HANDLE: "unknown handle",
    I2C_OPEN_FAILED: "can not open I2C device",
    SPI_OPEN_FAILED: "can not open SPI device",
    BAD_I2C_BUS: "bad I2C bus",
    BAD_I2C_ADDR: "bad I2C address",
    BAD_SPI_CHANNEL: "bad SPI channel",
    BAD_I2C_PARAM: "bad I2C parameter",
    I2C_WRITE_FAILED: "I2C write failed",
    I2C_READ_FAILED: "I2C read failed",
    BAD_SPI_COUNT: "bad SPI count",
    BAD_EVENT_REQUEST: "bad event request",
    BAD_GPIO_NUMBER: "bad GPIO number",
    CANNOT_OPEN_CHIP: "can not open gpiochip",
    GPIO_BUSY: "GPIO busy",
    GPIO_NOT_ALLOCATED: "GPIO not allocated",
    NOT_A_GPIOCHIP: "not a gpiochip",
    NOT_GROUP_LEADER: "GPIO is not the group leader",
    BAD_DEBOUNCE_MICS: "bad debounce microseconds",
    BAD_WATCHDOG_MICS: "bad watchdog microseconds",
    BAD_SERVO_FREQ: "bad servo frequency",
    BAD_SERVO_WIDTH: "bad servo pulsewidth",
    BAD_PWM_FREQ: "bad PWM frequency",
    BAD_PWM_DUTY: "bad PWM dutycycle",
    GPIO_NOT_AN_OUTPUT: "GPIO not set as an output",
}

# gpio_get_mode() bits
_KERNEL_IN_USE = 1 << 0
_KERNEL_OUTPUT = 1 << 1
_LG_INPUT = 1 << 8
_LG_OUTPUT = 1 << 9
_LG_ALERT = 1 << 10
_LG_GROUP = 1 << 11
_KERNEL_INPUT = 1 << 16
_KERNEL_RISING = 1 << 17
_KERNEL_FALLING = 1 << 18
_LINE_FLAGS = (
    SET_ACTIVE_LOW | SET_OPEN_DRAIN | SET_OPEN_SOURCE | SET_PULL_UP | SET_PULL_DOWN | SET_PULL_NONE
)


class error(Exception):  # noqa: N801 — lgpio's name
    def __init__(self, value: str) -> None:
        self.value = value

    def __str__(self) -> str:
        return repr(self.value)


class pulse:  # noqa: N801
    def __init__(self, group_bits: int, group_mask: int, pulse_delay: int) -> None:
        self.group_bits = group_bits
        self.group_mask = group_mask
        self.pulse_delay = pulse_delay


def error_text(errnum: int) -> str:
    return _ERRORS.get(errnum, "unknown error")


def get_module_version() -> str:
    v = LGPIO_PY_VERSION
    return f"lgpio.py_{v >> 16 & 0xFF}.{v >> 8 & 0xFF}.{v & 0xFF}"


def u2i(uint32: int) -> int:
    mask = (2**32) - 1
    return uint32 | ~mask if uint32 & (1 << 31) else uint32 & mask


def _status(code: int) -> int:
    if code < 0 and exceptions:
        raise error(error_text(code))
    return code


class _Claim:
    """A GPIO this process claimed through lgpio."""

    def __init__(self, handle: int, flags: int, output: bool) -> None:  # noqa: FBT001
        self.handle = handle
        self.flags = flags
        self.output = output
        self.alert = 0  # edge flags while claimed for alerts
        self.group: int | None = None  # the leader, while in a group
        self.debounce_us = 0
        self.watchdog_us = 0
        self.reported: int | None = None  # last level an alert reported
        self.timer: threading.Timer | None = None

    @property
    def active_low(self) -> bool:
        return bool(self.flags & SET_ACTIVE_LOW)


_lock = threading.RLock()
_chips: dict[int, int] = {}  # handle → gpiochip number
_next_handle = 0
_claims: dict[int, _Claim] = {}  # GPIO → its claim
_groups: dict[int, list[int]] = {}  # leader → its GPIOs
_callbacks: list[_callback] = []
_i2c: dict[int, buses.I2CClient] = {}
_spi: dict[int, tuple[int, int]] = {}


def _new_handle(registry: dict, value: object) -> int:
    global _next_handle
    handle = _next_handle
    _next_handle += 1
    registry[handle] = value
    return handle


def _chip(handle: int) -> int | None:
    """The gpiochip of a handle, or None when the handle isn't open."""
    with _lock:
        return _chips.get(handle & 0xFFFF)


def _gpio_ok(handle: int, gpio: int) -> int:
    """0 when *handle* is open and *gpio* exists on its chip, else the error code."""
    if _chip(handle) is None:
        return BAD_HANDLE
    if isinstance(gpio, bool) or not isinstance(gpio, int) or not 0 <= gpio < board.current().lines:
        return BAD_GPIO_NUMBER
    return OKAY


# ── gpiochip ──────────────────────────────────────────────────────────────────

def gpiochip_open(gpiochip: int) -> int:
    if gpiochip not in board.current().chips:
        return _status(CANNOT_OPEN_CHIP)
    with _lock:
        handle = _new_handle(_chips, gpiochip)
    return handle | gpiochip << 16


def gpiochip_close(handle: int) -> int:
    with _lock:
        if _chips.pop(handle & 0xFFFF, None) is None:
            return _status(BAD_HANDLE)
        for gpio in [g for g, c in _claims.items() if c.handle == handle & 0xFFFF]:
            _free(gpio)
    return OKAY


def gpio_get_chip_info(handle: int) -> list:
    chip = _chip(handle)
    if chip is None:
        return [_status(BAD_HANDLE), 0, "", ""]
    b = board.current()
    return [OKAY, b.lines, f"gpiochip{chip}", b.chip_label]


def gpio_get_line_info(handle: int, gpio: int) -> list:
    code = _gpio_ok(handle, gpio)
    if code:
        return [_status(code), gpio, 0, "", ""]
    mode = gpio_get_mode(handle, gpio)
    user = "lg" if gpio in _claims else ""
    return [OKAY, gpio, mode & 0xFF, f"GPIO{gpio}" if gpio < 28 else "", user]


def gpio_get_mode(handle: int, gpio: int) -> int:
    code = _gpio_ok(handle, gpio)
    if code:
        return _status(code)
    pin = pins.get(gpio)
    with _lock:
        claim = _claims.get(gpio)
    mode = 0
    if pin.function is not None:
        mode |= _KERNEL_IN_USE
    mode |= _KERNEL_OUTPUT if pin.function == pins.OUTPUT else _KERNEL_INPUT
    if claim is not None:
        mode |= claim.flags & _LINE_FLAGS
        mode |= _LG_OUTPUT if claim.output else _LG_INPUT
        if claim.alert:
            mode |= _LG_ALERT
            mode |= _KERNEL_RISING if claim.alert & RISING_EDGE else 0
            mode |= _KERNEL_FALLING if claim.alert & FALLING_EDGE else 0
        if claim.group is not None:
            mode |= _LG_GROUP
    else:
        mode |= {pins.PULL_UP: SET_PULL_UP, pins.PULL_DOWN: SET_PULL_DOWN}.get(pin.pull, 0)
    return mode


# ── claiming GPIO ─────────────────────────────────────────────────────────────

def _pull(flags: int) -> str:
    if flags & SET_PULL_UP:
        return pins.PULL_UP
    if flags & SET_PULL_DOWN:
        return pins.PULL_DOWN
    return pins.PULL_OFF


def _claim(handle: int, gpio: int, flags: int, output: bool, level: int = 0) -> int:  # noqa: FBT001
    code = _gpio_ok(handle, gpio)
    if code:
        return code
    with _lock:
        claim = _claims.get(gpio)
        if claim is not None and claim.handle != handle & 0xFFFF:
            return GPIO_BUSY
        if claim is not None and claim.timer is not None:
            claim.timer.cancel()
        new = _Claim(handle & 0xFFFF, flags, output)
        if claim is not None:  # reclaiming keeps the debounce and watchdog settings
            new.debounce_us, new.watchdog_us = claim.debounce_us, claim.watchdog_us
        _claims[gpio] = new
    if output:
        pins.setup_output(gpio, int(bool(level)) ^ new.active_low)
    else:
        pins.setup_input(gpio, _pull(flags))
    return OKAY


def gpio_claim_input(handle: int, gpio: int, lFlags: int = 0) -> int:  # noqa: N803
    return _status(_claim(handle, gpio, lFlags, output=False))


def gpio_claim_output(handle: int, gpio: int, level: int = 0, lFlags: int = 0) -> int:  # noqa: N803
    return _status(_claim(handle, gpio, lFlags, output=True, level=level))


def gpio_claim_alert(
    handle: int, gpio: int, eFlags: int, lFlags: int = 0, notify_handle: int | None = None  # noqa: N803
) -> int:
    if eFlags not in (RISING_EDGE, FALLING_EDGE, BOTH_EDGES):
        return _status(BAD_EVENT_REQUEST)
    code = _claim(handle, gpio, lFlags, output=False)
    if code:
        return _status(code)
    with _lock:
        claim = _claims[gpio]
        claim.alert = eFlags
        claim.reported = pins.read(gpio) ^ claim.active_low
    return OKAY


def _free(gpio: int) -> None:
    with _lock:
        claim = _claims.pop(gpio, None)
        if claim is not None and claim.timer is not None:
            claim.timer.cancel()
    pins.release(gpio)


def gpio_free(handle: int, gpio: int) -> int:
    code = _gpio_ok(handle, gpio)
    if code:
        return _status(code)
    with _lock:
        claim = _claims.get(gpio)
        if claim is None or claim.handle != handle & 0xFFFF:
            return _status(GPIO_NOT_ALLOCATED)
        _free(gpio)
    return OKAY


def _owned(handle: int, gpio: int) -> tuple[int, _Claim | None]:
    code = _gpio_ok(handle, gpio)
    if code:
        return code, None
    with _lock:
        claim = _claims.get(gpio)
    if claim is None or claim.handle != handle & 0xFFFF:
        return GPIO_NOT_ALLOCATED, None
    return OKAY, claim


def gpio_read(handle: int, gpio: int) -> int:
    code, claim = _owned(handle, gpio)
    if code:
        return _status(code)
    return pins.read(gpio) ^ claim.active_low


def gpio_write(handle: int, gpio: int, level: int) -> int:
    code, claim = _owned(handle, gpio)
    if code:
        return _status(code)
    if not claim.output:
        return _status(GPIO_NOT_AN_OUTPUT)
    pins.write(gpio, int(bool(level)) ^ claim.active_low)
    return OKAY


# ── groups ────────────────────────────────────────────────────────────────────

def group_claim_input(handle: int, gpio: Iterable[int], lFlags: int = 0) -> int:  # noqa: N803
    gpios = list(gpio)
    for g in gpios:
        code = _claim(handle, g, lFlags, output=False)
        if code:
            return _status(code)
    _make_group(gpios)
    return OKAY


def group_claim_output(
    handle: int, gpio: Iterable[int], levels: Iterable[int] = (0,), lFlags: int = 0  # noqa: N803
) -> int:
    gpios = list(gpio)
    initial = list(levels)
    initial += [0] * (len(gpios) - len(initial))
    for g, level in zip(gpios, initial, strict=False):
        code = _claim(handle, g, lFlags, output=True, level=level)
        if code:
            return _status(code)
    _make_group(gpios)
    return OKAY


def _make_group(gpios: list[int]) -> None:
    with _lock:
        _groups[gpios[0]] = gpios
        for g in gpios:
            _claims[g].group = gpios[0]


def _group(handle: int, leader: int) -> tuple[int, list[int]]:
    code, claim = _owned(handle, leader)
    if code:
        return code, []
    with _lock:
        if leader not in _groups:
            return NOT_GROUP_LEADER, []
        return OKAY, list(_groups[leader])


def group_free(handle: int, gpio: int) -> int:
    code, members = _group(handle, gpio)
    if code:
        return _status(code)
    with _lock:
        del _groups[gpio]
    for g in members:
        _free(g)
    return OKAY


def group_read(handle: int, gpio: int) -> int:
    code, members = _group(handle, gpio)
    if code:
        return _status(code)
    bits = 0
    for i, g in enumerate(members):
        bits |= (pins.read(g) ^ _claims[g].active_low) << i
    return bits


def group_write(handle: int, gpio: int, group_bits: int, group_mask: int = GROUP_ALL) -> int:
    code, members = _group(handle, gpio)
    if code:
        return _status(code)
    for i, g in enumerate(members):
        if group_mask >> i & 1:
            if not _claims[g].output:
                return _status(GPIO_NOT_AN_OUTPUT)
            pins.write(g, (group_bits >> i & 1) ^ _claims[g].active_low)
    return OKAY


# ── PWM and servo pulses (recorded, not toggled) ─────────────────────────────

def _output(handle: int, gpio: int) -> int:
    code, claim = _owned(handle, gpio)
    if code:
        return code
    return OKAY if claim.output else GPIO_NOT_AN_OUTPUT


def tx_pwm(
    handle: int, gpio: int, pwm_frequency: float, pwm_duty_cycle: float,
    pulse_offset: int = 0, pulse_cycles: int = 0,
) -> int:
    code = _output(handle, gpio)
    if code:
        return _status(code)
    if pwm_frequency and not 0.1 <= pwm_frequency <= 10000:
        return _status(BAD_PWM_FREQ)
    if not 0 <= pwm_duty_cycle <= 100:
        return _status(BAD_PWM_DUTY)
    pins.set_pwm(gpio, pwm_frequency, pwm_duty_cycle)
    return 1  # entries left in the PWM queue


def tx_servo(
    handle: int, gpio: int, pulse_width: int, servo_frequency: int = 50,
    pulse_offset: int = 0, pulse_cycles: int = 0,
) -> int:
    code = _output(handle, gpio)
    if code:
        return _status(code)
    if pulse_width and not 500 <= pulse_width <= 2500:
        return _status(BAD_SERVO_WIDTH)
    if not 40 <= servo_frequency <= 10000:
        return _status(BAD_SERVO_FREQ)
    duty = pulse_width * servo_frequency / 10_000  # µs × Hz → % of the period
    pins.set_pwm(gpio, servo_frequency if pulse_width else 0, duty)
    return 1


def tx_busy(handle: int, gpio: int, kind: int) -> int:
    code = _output(handle, gpio)
    if code:
        return _status(code)
    return int(pins.get(gpio).pwm is not None)


def tx_room(handle: int, gpio: int, kind: int) -> int:
    code = _output(handle, gpio)
    return _status(code) if code else 1


# ── alerts ────────────────────────────────────────────────────────────────────

def gpio_set_debounce_micros(handle: int, gpio: int, debounce_micros: int) -> int:
    code, claim = _owned(handle, gpio)
    if code:
        return _status(code)
    if not 0 <= debounce_micros <= 5_000_000:
        return _status(BAD_DEBOUNCE_MICS)
    claim.debounce_us = debounce_micros
    return OKAY


def gpio_set_watchdog_micros(handle: int, gpio: int, watchdog_micros: int) -> int:
    """Accepted and stored; the stub never sends watchdog (TIMEOUT) alerts."""
    code, claim = _owned(handle, gpio)
    if code:
        return _status(code)
    if not 0 <= watchdog_micros <= 300_000_000:
        return _status(BAD_WATCHDOG_MICS)
    claim.watchdog_us = watchdog_micros
    return OKAY


class _callback:  # noqa: N801
    """A level-change callback; cancel() removes it. Without a function it counts edges."""

    def __init__(self, chip: int, gpio: int, edge: int = RISING_EDGE,
                 func: Callable[[int, int, int, int], None] | None = None) -> None:
        self.count = 0
        self._reset = False
        self.chip = chip
        self.gpio = gpio
        self.edge = edge
        self.func = func or self._tally
        with _lock:
            _callbacks.append(self)

    def cancel(self) -> None:
        with _lock:
            if self in _callbacks:
                _callbacks.remove(self)

    def _tally(self, chip: int, gpio: int, level: int, tick: int) -> None:
        if self._reset:
            self._reset = False
            self.count = 0
        self.count += 1

    def tally(self) -> int:
        return self.count

    def reset_tally(self) -> None:
        self._reset = True
        self.count = 0


def callback(handle: int, gpio: int, edge: int = RISING_EDGE,
             func: Callable[[int, int, int, int], None] | None = None) -> _callback:
    return _callback(handle >> 16, gpio, edge, func)


def _alert(gpio: int, claim: _Claim) -> None:
    """Report the line's level if it changed since the last alert and matches the edge."""
    with _lock:
        if _claims.get(gpio) is not claim or not claim.alert:
            return
        level = pins.read(gpio) ^ claim.active_low
        if level == claim.reported:
            return
        claim.reported = level
        if not claim.alert & (RISING_EDGE if level else FALLING_EDGE):
            return
        chip = _chips.get(claim.handle)
        targets = [cb for cb in _callbacks if cb.chip == chip and cb.gpio == gpio]
    tick = time.monotonic_ns()
    for cb in targets:
        try:
            cb.func(chip, gpio, level, tick)
        except Exception as exc:  # noqa: BLE001
            print(f"lgpio callback: {exc!r}", file=sys.stderr)


def _on_change(change: pins.Change) -> None:
    if not change.edge or change.pin.function != pins.INPUT:
        return
    with _lock:
        claim = _claims.get(change.gpio)
        if claim is None or not claim.alert:
            return
        if claim.timer is not None:
            claim.timer.cancel()
            claim.timer = None
        if claim.debounce_us:
            # Debounce as lgpio does: alert once the level has been stable that long
            claim.timer = threading.Timer(claim.debounce_us / 1e6, _alert, (change.gpio, claim))
            claim.timer.daemon = True
            claim.timer.start()
            return
    _alert(change.gpio, claim)


pins.watch(_on_change)


# ── I2C ───────────────────────────────────────────────────────────────────────

def i2c_open(i2c_bus: int, i2c_address: int, i2c_flags: int = 0) -> int:
    if not 0 <= i2c_address <= 0x7F:
        return _status(BAD_I2C_ADDR)
    try:
        client = buses.I2CClient(i2c_bus, i2c_address)
    except FileNotFoundError:
        return _status(I2C_OPEN_FAILED)
    with _lock:
        return _new_handle(_i2c, client)


def i2c_close(handle: int) -> int:
    with _lock:
        return OKAY if _i2c.pop(handle, None) is not None else _status(BAD_HANDLE)


def _i2c_call(handle: int, fail: int, op: Callable[[buses.I2CClient], object]):
    with _lock:
        client = _i2c.get(handle)
    if client is None:
        return _status(BAD_HANDLE)
    try:
        return op(client)
    except OSError:
        return _status(fail)


def i2c_write_quick(handle: int, bit: int) -> int:
    return _i2c_call(handle, I2C_WRITE_FAILED, lambda c: c.quick() or OKAY)


def i2c_write_byte(handle: int, byte_val: int) -> int:
    return _i2c_call(handle, I2C_WRITE_FAILED, lambda c: c.write_byte(byte_val) or OKAY)


def i2c_read_byte(handle: int) -> int:
    return _i2c_call(handle, I2C_READ_FAILED, lambda c: c.read_byte())


def i2c_write_byte_data(handle: int, reg: int, byte_val: int) -> int:
    return _i2c_call(handle, I2C_WRITE_FAILED, lambda c: c.write_byte_data(reg, byte_val) or OKAY)


def i2c_write_word_data(handle: int, reg: int, word_val: int) -> int:
    return _i2c_call(handle, I2C_WRITE_FAILED, lambda c: c.write_word_data(reg, word_val) or OKAY)


def i2c_read_byte_data(handle: int, reg: int) -> int:
    return _i2c_call(handle, I2C_READ_FAILED, lambda c: c.read_byte_data(reg))


def i2c_read_word_data(handle: int, reg: int) -> int:
    return _i2c_call(handle, I2C_READ_FAILED, lambda c: c.read_word_data(reg))


def i2c_process_call(handle: int, reg: int, word_val: int) -> int:
    return _i2c_call(handle, I2C_READ_FAILED, lambda c: c.process_call(reg, word_val))


def _bytes(data: bytes | bytearray | str | Iterable[int]) -> bytes:
    if isinstance(data, str):
        return data.encode("latin-1")
    return bytes(data)


def i2c_write_block_data(handle: int, reg: int, data) -> int:
    return _i2c_call(handle, I2C_WRITE_FAILED,
                     lambda c: c.write_block_data(reg, _bytes(data)) or OKAY)


def _counted(data: bytes) -> tuple[int, bytearray]:
    return len(data), bytearray(data)


def i2c_read_block_data(handle: int, reg: int) -> tuple[int, bytearray]:
    result = _i2c_call(handle, I2C_READ_FAILED, lambda c: _counted(c.read_block_data(reg)))
    return result if isinstance(result, tuple) else (result, bytearray())


def i2c_write_i2c_block_data(handle: int, reg: int, data) -> int:
    return _i2c_call(handle, I2C_WRITE_FAILED,
                     lambda c: c.write_i2c_block_data(reg, _bytes(data)) or OKAY)


def i2c_read_i2c_block_data(handle: int, reg: int, count: int) -> tuple[int, bytearray]:
    if not 0 < count <= 32:
        return (_status(BAD_I2C_PARAM), bytearray())
    result = _i2c_call(handle, I2C_READ_FAILED,
                       lambda c: _counted(c.read_i2c_block_data(reg, count)))
    return result if isinstance(result, tuple) else (result, bytearray())


def i2c_read_device(handle: int, count: int) -> tuple[int, bytearray]:
    result = _i2c_call(handle, I2C_READ_FAILED, lambda c: _counted(c.read(count)))
    return result if isinstance(result, tuple) else (result, bytearray())


def i2c_write_device(handle: int, data) -> int:
    return _i2c_call(handle, I2C_WRITE_FAILED, lambda c: c.write(_bytes(data)) or OKAY)


# ── SPI ───────────────────────────────────────────────────────────────────────

def spi_open(spi_device: int, spi_channel: int, baud: int, spi_flags: int = 0) -> int:
    try:
        buses.check_spi(spi_device, spi_channel)
    except FileNotFoundError:
        return _status(SPI_OPEN_FAILED)
    with _lock:
        return _new_handle(_spi, (spi_device, spi_channel))


def spi_close(handle: int) -> int:
    with _lock:
        return OKAY if _spi.pop(handle, None) is not None else _status(BAD_HANDLE)


def _spi_xfer(handle: int, data: bytes) -> tuple[int, bytearray]:
    with _lock:
        target = _spi.get(handle)
    if target is None:
        return (_status(BAD_HANDLE), bytearray())
    back = buses.spi_transfer(*target, data)
    return len(back), bytearray(back)


def spi_read(handle: int, count: int) -> tuple[int, bytearray]:
    if count <= 0:
        return (_status(BAD_SPI_COUNT), bytearray())
    return _spi_xfer(handle, bytes(count))


def spi_write(handle: int, data) -> int:
    return _spi_xfer(handle, _bytes(data))[0]


def spi_xfer(handle: int, data) -> tuple[int, bytearray]:
    return _spi_xfer(handle, _bytes(data))


def _reset() -> None:
    """Close every handle (between tests)."""
    with _lock:
        for claim in _claims.values():
            if claim.timer is not None:
                claim.timer.cancel()
        _chips.clear()
        _claims.clear()
        _groups.clear()
        _callbacks.clear()
        _i2c.clear()
        _spi.clear()
