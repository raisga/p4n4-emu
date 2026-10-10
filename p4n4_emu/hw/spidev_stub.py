"""Drop-in stub for spidev: SPI on the emulated buses.

`p4n4-emu run` makes `import spidev` load it. /dev/spidev0.0 and 0.1 exist,
as on a Pi with SPI enabled; transfers go to the part attached with
buses.attach_spi(), and a chip select with nothing on it reads back zeros.
"""

from __future__ import annotations

import errno
import logging
import os
from collections.abc import Iterable

from p4n4_emu.hw import buses

_log = logging.getLogger(__name__)


class SpiDev:
    def __init__(self, bus: int | None = None, client: int | None = None) -> None:
        self._target: tuple[int, int] | None = None
        self._mode = 0
        self.bits_per_word = 8
        self.max_speed_hz = 125_000_000
        self.cshigh = False
        self.lsbfirst = False
        self.threewire = False
        self.loop = False
        self.no_cs = False
        if bus is not None and client is not None:
            self.open(bus, client)

    def __enter__(self) -> SpiDev:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def mode(self) -> int:
        return self._mode

    @mode.setter
    def mode(self, value: int) -> None:
        if not isinstance(value, int) or not 0 <= value <= 3:
            raise TypeError("The mode attribute must be an integer between 0 and 3.")
        self._mode = value

    def open(self, bus: int, device: int) -> None:
        buses.check_spi(bus, device)
        self._target = (bus, device)
        _log.debug("spidev: opened /dev/spidev%d.%d", bus, device)

    def close(self) -> None:
        self._target = None

    def fileno(self) -> int:
        return -1 if self._target is None else 2000 + self._target[0] * 10 + self._target[1]

    def _transfer(self, data: Iterable[int]) -> list[int]:
        if self._target is None:
            raise OSError(errno.EBADF, os.strerror(errno.EBADF))
        out = bytes(data)
        if self.lsbfirst:
            out = bytes(int(f"{b:08b}"[::-1], 2) for b in out)
        back = buses.spi_transfer(*self._target, out)
        if self.lsbfirst:
            back = bytes(int(f"{b:08b}"[::-1], 2) for b in back)
        return list(back)

    def xfer(self, data: list[int], speed_hz: int = 0, delay_usecs: int = 0,
             bits_per_word: int = 0) -> list[int]:
        return self._transfer(data)

    def xfer2(self, data: list[int], speed_hz: int = 0, delay_usecs: int = 0,
              bits_per_word: int = 0) -> list[int]:
        return self._transfer(data)

    def xfer3(self, data: list[int], speed_hz: int = 0, delay_usecs: int = 0,
              bits_per_word: int = 0) -> tuple[int, ...]:
        return tuple(self._transfer(data))

    def readbytes(self, length: int) -> list[int]:
        return self._transfer(bytes(length))

    def writebytes(self, data: list[int]) -> None:
        self._transfer(data)

    def writebytes2(self, data: Iterable[int]) -> None:
        self._transfer(data)
