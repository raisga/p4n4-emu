"""Drop-in stub for smbus2 (and python-smbus): I2C on the emulated buses.

`p4n4-emu run` makes `import smbus2` and `import smbus` load it. Transfers go
to the parts attached with buses.attach_i2c(); like on a Pi, bus 1 exists, a
missing bus fails to open, and an address with no part raises
OSError(EREMOTEIO), errno 121.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from enum import IntFlag

from p4n4_emu.hw import buses

_log = logging.getLogger(__name__)

I2C_SMBUS_BLOCK_MAX = 32
I2C_M_RD = 0x0001


class I2cFunc(IntFlag):
    I2C = 0x00000001
    SMBUS_PEC = 0x00000008
    SMBUS_QUICK = 0x00010000
    SMBUS_READ_BYTE = 0x00020000
    SMBUS_WRITE_BYTE = 0x00040000
    SMBUS_READ_BYTE_DATA = 0x00080000
    SMBUS_WRITE_BYTE_DATA = 0x00100000
    SMBUS_READ_WORD_DATA = 0x00200000
    SMBUS_WRITE_WORD_DATA = 0x00400000
    SMBUS_PROC_CALL = 0x00800000
    SMBUS_READ_BLOCK_DATA = 0x01000000
    SMBUS_WRITE_BLOCK_DATA = 0x02000000
    SMBUS_READ_I2C_BLOCK = 0x04000000
    SMBUS_WRITE_I2C_BLOCK = 0x08000000


# What the Pi's i2c-bcm2835 driver reports (I2C plus the kernel's SMBus
# emulation): everything but SMBus block reads, whose length comes from the part
_FUNCS = I2cFunc(0)
for _f in I2cFunc:
    if _f != I2cFunc.SMBUS_READ_BLOCK_DATA:
        _FUNCS |= _f


class i2c_msg:  # noqa: N801 — smbus2's name
    """One message of an i2c_rdwr() transfer."""

    def __init__(self, addr: int, flags: int, buf: bytes | bytearray) -> None:
        self.addr = addr
        self.flags = flags
        self.buf = bytearray(buf)

    @property
    def len(self) -> int:
        return len(self.buf)

    def __iter__(self) -> Iterator[int]:
        return iter(self.buf)

    def __len__(self) -> int:
        return len(self.buf)

    def __bytes__(self) -> bytes:
        return bytes(self.buf)

    def __repr__(self) -> str:
        return f"i2c_msg({self.addr},{self.flags},{bytes(self.buf)!r})"

    @staticmethod
    def read(address: int, length: int) -> i2c_msg:
        return i2c_msg(address, I2C_M_RD, bytes(length))

    @staticmethod
    def write(address: int, buf: str | bytes | Iterable[int]) -> i2c_msg:
        if isinstance(buf, str):
            buf = buf.encode("latin-1")
        return i2c_msg(address, 0, bytes(buf))


class SMBus:
    def __init__(self, bus: int | str | None = None, force: bool = False) -> None:  # noqa: FBT001
        self.fd: int | None = None
        self.funcs = I2cFunc(0)
        self.address: int | None = None
        self.force = force
        self.pec = 0
        self._bus: int | None = None
        if bus is not None:
            self.open(bus)

    def __enter__(self) -> SMBus:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def open(self, bus: int | str) -> None:
        if isinstance(bus, str):
            if not bus.startswith("/dev/i2c-") or not bus[9:].isdigit():
                raise buses._missing(bus)
            bus = int(bus[9:])
        if not isinstance(bus, int):
            raise TypeError(f"Unexpected type(bus)={type(bus)}")
        buses.check_i2c_bus(bus)
        self._bus = bus
        self.fd = 1000 + bus  # not a real descriptor; scripts only check it is set
        self.funcs = _FUNCS
        _log.debug("smbus: opened /dev/i2c-%d", bus)

    def close(self) -> None:
        self.fd = None
        self._bus = None

    def enable_pec(self, enable: bool = True) -> None:  # noqa: FBT001
        if not self.funcs & I2cFunc.SMBUS_PEC:
            raise OSError("SMBUS_PEC is not a feature")
        self.pec = int(enable)

    def _client(self, i2c_addr: int) -> buses.I2CClient:
        if self._bus is None:
            raise OSError(9, "Bad file descriptor")  # used after close(), as smbus2 fails
        self.address = i2c_addr
        return buses.I2CClient(self._bus, i2c_addr)

    def write_quick(self, i2c_addr: int, force: bool | None = None) -> None:
        self._client(i2c_addr).quick()

    def read_byte(self, i2c_addr: int, force: bool | None = None) -> int:
        return self._client(i2c_addr).read_byte()

    def write_byte(self, i2c_addr: int, value: int, force: bool | None = None) -> None:
        self._client(i2c_addr).write_byte(value)

    def read_byte_data(self, i2c_addr: int, register: int, force: bool | None = None) -> int:
        return self._client(i2c_addr).read_byte_data(register)

    def write_byte_data(
        self, i2c_addr: int, register: int, value: int, force: bool | None = None
    ) -> None:
        self._client(i2c_addr).write_byte_data(register, value)

    def read_word_data(self, i2c_addr: int, register: int, force: bool | None = None) -> int:
        return self._client(i2c_addr).read_word_data(register)

    def write_word_data(
        self, i2c_addr: int, register: int, value: int, force: bool | None = None
    ) -> None:
        self._client(i2c_addr).write_word_data(register, value)

    def process_call(
        self, i2c_addr: int, register: int, value: int, force: bool | None = None
    ) -> int:
        return self._client(i2c_addr).process_call(register, value)

    def read_block_data(self, i2c_addr: int, register: int, force: bool | None = None) -> list[int]:
        return list(self._client(i2c_addr).read_block_data(register))

    def write_block_data(
        self, i2c_addr: int, register: int, data: list[int], force: bool | None = None
    ) -> None:
        _check_block(data)
        self._client(i2c_addr).write_block_data(register, bytes(data))

    def block_process_call(
        self, i2c_addr: int, register: int, data: list[int], force: bool | None = None
    ) -> list[int]:
        _check_block(data)
        client = self._client(i2c_addr)
        client.write_block_data(register, bytes(data))
        count = client.read(1)[0]
        return list(client.read(min(count, I2C_SMBUS_BLOCK_MAX)))

    def read_i2c_block_data(
        self, i2c_addr: int, register: int, length: int, force: bool | None = None
    ) -> list[int]:
        if length > I2C_SMBUS_BLOCK_MAX:
            raise ValueError(f"Desired block length over {I2C_SMBUS_BLOCK_MAX} bytes")
        return list(self._client(i2c_addr).read_i2c_block_data(register, length))

    def write_i2c_block_data(
        self, i2c_addr: int, register: int, data: list[int], force: bool | None = None
    ) -> None:
        _check_block(data)
        self._client(i2c_addr).write_i2c_block_data(register, bytes(data))

    def i2c_rdwr(self, *i2c_msgs: i2c_msg) -> None:
        """Combined transfer: each read message's buf is filled in place."""
        if self._bus is None:
            raise OSError(9, "Bad file descriptor")
        for msg in i2c_msgs:
            client = buses.I2CClient(self._bus, msg.addr)
            if msg.flags & I2C_M_RD:
                msg.buf[:] = client.read(len(msg.buf))
            else:
                client.write(bytes(msg.buf))


def _check_block(data: list[int]) -> None:
    if len(data) > I2C_SMBUS_BLOCK_MAX:
        raise ValueError(f"Data length cannot exceed {I2C_SMBUS_BLOCK_MAX} bytes")
