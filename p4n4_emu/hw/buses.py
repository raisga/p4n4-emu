"""The emulated I2C, SPI and UART buses, and the parts attached to them.

smbus2 / smbus, spidev, pyserial and lgpio's i2c_* / spi_* are front ends over
these buses. A part is a device model (p4n4_emu.hw.devices) at an address; an
address with nothing attached answers like real hardware does: an I2C transfer
fails with EREMOTEIO, SPI reads back zeros, and a UART read times out. A part
whose readings drop out (a scenario's dropout fault) answers the same way.
"""

from __future__ import annotations

import errno
import os
import threading
import time
from dataclasses import dataclass, field

from p4n4_emu.hw.readings import SensorDropout


class I2CDevice:
    """A part on an I2C bus, seen as raw transfers: what the master writes, what it reads."""

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def read(self, count: int) -> bytes:
        raise NotImplementedError


class RegisterDevice(I2CDevice):
    """An I2C part with 8-bit registers behind a pointer that auto-increments.

    A write's first byte sets the pointer and the rest are written from there;
    a read starts at the pointer. begin_read() runs once per read transfer, so
    a part can latch a consistent set of samples for a burst read.
    """

    def __init__(self) -> None:
        self.pointer = 0

    def read_register(self, register: int) -> int:
        return 0

    def write_register(self, register: int, value: int) -> None:
        pass

    def begin_read(self) -> None:
        pass

    def write(self, data: bytes) -> None:
        if not data:
            return
        self.pointer = data[0]
        for value in data[1:]:
            self.write_register(self.pointer, value)
            self.pointer = (self.pointer + 1) & 0xFF

    def read(self, count: int) -> bytes:
        self.begin_read()
        out = bytearray()
        for _ in range(count):
            out.append(self.read_register(self.pointer) & 0xFF)
            self.pointer = (self.pointer + 1) & 0xFF
        return bytes(out)


class SPIDevice:
    """A part on an SPI chip select: full duplex, one byte back per byte sent."""

    def transfer(self, data: bytes) -> bytes:
        raise NotImplementedError


class UARTDevice:
    """A part on a serial port: it receives what the script writes, and may answer."""

    def receive(self, data: bytes) -> bytes:
        """Bytes written by the script; returns bytes the part sends back."""
        return b""

    def poll(self) -> bytes:
        """Bytes the part sends on its own (a GPS sentence, a reading), checked on every read."""
        return b""


class Loopback(UARTDevice):
    """TX wired to RX: the script reads back what it writes."""

    def receive(self, data: bytes) -> bytes:
        return data


@dataclass
class Port:
    """A serial port: bytes waiting to be read, and every byte the script wrote."""

    path: str
    device: UARTDevice | None = None
    rx: bytearray = field(default_factory=bytearray)
    tx: bytearray = field(default_factory=bytearray)
    lock: threading.Condition = field(default_factory=threading.Condition)

    def feed(self, data: bytes) -> None:
        """Bytes arrive on the port (stub only): the next reads return them."""
        with self.lock:
            self.rx += data
            self.lock.notify_all()

    def write(self, data: bytes) -> None:
        with self.lock:
            self.tx += data
            if self.device is not None:
                self.rx += self.device.receive(bytes(data))
            self.lock.notify_all()

    def read(self, count: int, timeout: float | None) -> bytes:
        """Up to *count* bytes; waits until there are *count* or *timeout* runs out."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self.lock:
            while True:
                if self.device is not None:
                    self.rx += self.device.poll()
                if len(self.rx) >= count:
                    break
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    break
                # Wake now and then for a part that sends on its own
                self.lock.wait(0.05 if remaining is None else min(remaining, 0.05))
            data = bytes(self.rx[:count])
            del self.rx[:count]
            return data

    def waiting(self) -> int:
        with self.lock:
            if self.device is not None:
                self.rx += self.device.poll()
            return len(self.rx)

    def flush_input(self) -> None:
        with self.lock:
            self.rx.clear()


_lock = threading.RLock()
# Buses and chip selects that exist, as on a Pi with I2C and SPI enabled
_i2c: dict[int, dict[int, I2CDevice]] = {}
_spi: dict[tuple[int, int], SPIDevice | None] = {}
_uart: dict[str, Port] = {}


def _missing(path: str) -> FileNotFoundError:
    return FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), path)


def reset() -> None:
    """The buses of a Pi with I2C, SPI and the serial port enabled, with no parts."""
    with _lock:
        _i2c.clear()
        _i2c[1] = {}
        _spi.clear()
        _spi.update({(0, 0): None, (0, 1): None})
        _uart.clear()
        for path in ("/dev/serial0", "/dev/ttyAMA0"):
            _uart[path] = Port(path)


reset()


# ── I2C ───────────────────────────────────────────────────────────────────────

def attach_i2c(bus: int, address: int, device: I2CDevice) -> None:
    if not 0x03 <= address <= 0x77:
        raise ValueError(f"I2C address 0x{address:02x} is reserved or out of range")
    with _lock:
        _i2c.setdefault(bus, {})[address] = device


def i2c_buses() -> list[int]:
    with _lock:
        return sorted(_i2c)


def i2c_devices(bus: int) -> dict[int, I2CDevice]:
    with _lock:
        return dict(_i2c.get(bus, {}))


def check_i2c_bus(bus: int) -> None:
    with _lock:
        if bus not in _i2c:
            raise _missing(f"/dev/i2c-{bus}")


def _nack() -> OSError:
    """What i2c-dev reports when no part acknowledges the address."""
    return OSError(errno.EREMOTEIO, os.strerror(errno.EREMOTEIO))


class I2CClient:
    """SMBus and plain I2C transfers to one address, as the kernel's i2c-dev does them."""

    def __init__(self, bus: int, address: int) -> None:
        check_i2c_bus(bus)
        self.bus = bus
        self.address = address

    def _device(self) -> I2CDevice:
        with _lock:
            device = _i2c.get(self.bus, {}).get(self.address)
        if device is None:
            raise _nack()
        return device

    def write(self, data: bytes) -> None:
        with _lock:
            try:
                self._device().write(bytes(data))
            except SensorDropout:
                raise _nack() from None

    def read(self, count: int) -> bytes:
        with _lock:
            try:
                return self._device().read(count)
            except SensorDropout:
                raise _nack() from None

    def quick(self) -> None:
        self._device()

    def read_byte(self) -> int:
        return self.read(1)[0]

    def write_byte(self, value: int) -> None:
        self.write(bytes([value & 0xFF]))

    def read_byte_data(self, register: int) -> int:
        with _lock:
            self.write(bytes([register]))
            return self.read(1)[0]

    def write_byte_data(self, register: int, value: int) -> None:
        self.write(bytes([register, value & 0xFF]))

    def read_word_data(self, register: int) -> int:
        """SMBus words are little-endian: the first byte is the low one."""
        with _lock:
            self.write(bytes([register]))
            low, high = self.read(2)
        return low | high << 8

    def write_word_data(self, register: int, value: int) -> None:
        self.write(bytes([register, value & 0xFF, value >> 8 & 0xFF]))

    def process_call(self, register: int, value: int) -> int:
        with _lock:
            self.write_word_data(register, value)
            low, high = self.read(2)
        return low | high << 8

    def read_block_data(self, register: int) -> bytes:
        """SMBus block read: the part sends a count byte, then that many bytes."""
        with _lock:
            self.write(bytes([register]))
            count = self.read(1)[0]
            return self.read(min(count, 32))

    def write_block_data(self, register: int, data: bytes) -> None:
        self.write(bytes([register, len(data), *data]))

    def read_i2c_block_data(self, register: int, count: int) -> bytes:
        with _lock:
            self.write(bytes([register]))
            return self.read(count)

    def write_i2c_block_data(self, register: int, data: bytes) -> None:
        self.write(bytes([register, *data]))


# ── SPI ───────────────────────────────────────────────────────────────────────

def attach_spi(bus: int, chip_select: int, device: SPIDevice) -> None:
    with _lock:
        _spi[(bus, chip_select)] = device


def check_spi(bus: int, chip_select: int) -> None:
    with _lock:
        if (bus, chip_select) not in _spi:
            raise _missing(f"/dev/spidev{bus}.{chip_select}")


def spi_transfer(bus: int, chip_select: int, data: bytes) -> bytes:
    """One transfer with CS held low; nothing attached reads back zeros."""
    with _lock:
        check_spi(bus, chip_select)
        device = _spi[(bus, chip_select)]
        if device is None:
            return bytes(len(data))
        try:
            return device.transfer(bytes(data))
        except SensorDropout:
            return bytes(len(data))


def spi_devices() -> dict[tuple[int, int], SPIDevice | None]:
    with _lock:
        return dict(_spi)


# ── UART ──────────────────────────────────────────────────────────────────────

def attach_uart(path: str, device: UARTDevice | None = None) -> Port:
    """Add a serial port (or put a part on one): `serial.Serial(path)` then opens it."""
    with _lock:
        port = _uart.setdefault(path, Port(path))
        port.device = device
        return port


def uart(path: str) -> Port:
    with _lock:
        port = _uart.get(path)
    if port is None:
        raise _missing(path)
    return port


def uart_ports() -> list[str]:
    with _lock:
        return sorted(_uart)
