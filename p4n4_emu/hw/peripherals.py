"""Simulated hardware peripherals — I2C, SPI, UART."""

from __future__ import annotations

import logging
import random

_log = logging.getLogger(__name__)


class SimulationError(IOError):
    """Raised when a simulated peripheral is misconfigured."""


class SimI2C:
    def __init__(self, bus: int = 1) -> None:
        if bus < 0:
            raise SimulationError(f"Invalid I2C bus: {bus}")
        self.bus = bus
        _log.debug("SimI2C: opened bus %d", bus)

    def read_byte(self, addr: int) -> int:
        val = random.randint(0, 255)
        _log.debug("SimI2C bus=%d addr=0x%02x read_byte → 0x%02x", self.bus, addr, val)
        return val

    def write_byte(self, addr: int, value: int) -> None:
        _log.debug("SimI2C bus=%d addr=0x%02x write_byte 0x%02x", self.bus, addr, value)

    def read_i2c_block_data(self, addr: int, register: int, length: int) -> list[int]:
        data = [random.randint(0, 255) for _ in range(length)]
        _log.debug(
            "SimI2C bus=%d addr=0x%02x reg=0x%02x read %d bytes → %s",
            self.bus, addr, register, length, data,
        )
        return data


class SimSPI:
    def __init__(self, bus: int = 0, device: int = 0) -> None:
        if bus < 0 or device < 0:
            raise SimulationError(f"Invalid SPI bus/device: {bus}/{device}")
        self.bus = bus
        self.device = device
        _log.debug("SimSPI: opened bus=%d device=%d", bus, device)

    def xfer2(self, data: list[int]) -> list[int]:
        response = [random.randint(0, 255) for _ in data]
        _log.debug(
            "SimSPI bus=%d dev=%d xfer %d bytes → %s",
            self.bus, self.device, len(data), response,
        )
        return response


class SimUART:
    def __init__(self, port: str = "/dev/ttyS0", baudrate: int = 9600) -> None:
        self.port = port
        self.baudrate = baudrate
        self._open = False
        _log.debug("SimUART: configured %s @ %d baud", port, baudrate)

    def open(self) -> None:
        self._open = True
        _log.debug("SimUART: opened %s", self.port)

    def close(self) -> None:
        self._open = False
        _log.debug("SimUART: closed %s", self.port)

    def read(self, size: int = 1) -> bytes:
        if not self._open:
            raise SimulationError(f"SimUART {self.port!r} is not open")
        data = bytes(random.randint(0, 255) for _ in range(size))
        _log.debug("SimUART %s read %d bytes → %r", self.port, size, data)
        return data

    def write(self, data: bytes) -> int:
        if not self._open:
            raise SimulationError(f"SimUART {self.port!r} is not open")
        _log.debug("SimUART %s write %d bytes", self.port, len(data))
        return len(data)

    def __enter__(self) -> SimUART:
        self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
