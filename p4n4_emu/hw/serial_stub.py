"""Drop-in stub for pyserial: serial ports on the emulated board.

`p4n4-emu run` makes `import serial` load it. /dev/serial0 and /dev/ttyAMA0
exist, as on a Pi with the serial port enabled; other ports fail to open as a
missing device does. With nothing attached a read waits for its timeout and
returns what arrived: feed bytes in with buses.uart(path).feed(), or attach a
part (buses.attach_uart(path, device)). Every byte written is kept in
buses.uart(path).tx.
"""

from __future__ import annotations

import errno
import os
import time

from p4n4_emu.hw import buses

PARITY_NONE, PARITY_EVEN, PARITY_ODD, PARITY_MARK, PARITY_SPACE = "N", "E", "O", "M", "S"
STOPBITS_ONE, STOPBITS_ONE_POINT_FIVE, STOPBITS_TWO = 1, 1.5, 2
FIVEBITS, SIXBITS, SEVENBITS, EIGHTBITS = 5, 6, 7, 8
LF = b"\n"
CR = b"\r"

BAUDRATES = (
    50, 75, 110, 134, 150, 200, 300, 600, 1200, 1800, 2400, 4800, 9600, 19200, 38400, 57600,
    115200, 230400, 460800, 500000, 576000, 921600, 1000000, 1152000, 1500000, 2000000,
    2500000, 3000000, 3500000, 4000000,
)


class SerialException(OSError):
    """Base class for serial port related exceptions."""


class SerialTimeoutException(SerialException):
    """Write timeouts give an exception"""


class PortNotOpenError(SerialException):
    def __init__(self) -> None:
        super().__init__("Attempting to use a port that is not open")


class Serial:
    def __init__(
        self,
        port: str | None = None,
        baudrate: int = 9600,
        bytesize: int = EIGHTBITS,
        parity: str = PARITY_NONE,
        stopbits: float = STOPBITS_ONE,
        timeout: float | None = None,
        xonxoff: bool = False,  # noqa: FBT001, FBT002
        rtscts: bool = False,  # noqa: FBT001, FBT002
        write_timeout: float | None = None,
        dsrdtr: bool = False,  # noqa: FBT001, FBT002
        inter_byte_timeout: float | None = None,
        exclusive: bool | None = None,
        **kwargs: object,
    ) -> None:
        if kwargs:
            raise ValueError(f"unexpected keyword arguments: {kwargs!r}")
        self.is_open = False
        self._port_name = port
        self._port: buses.Port | None = None
        self.baudrate = baudrate
        self.bytesize = bytesize
        self.parity = parity
        self.stopbits = stopbits
        self.timeout = timeout
        self.write_timeout = write_timeout
        self.inter_byte_timeout = inter_byte_timeout
        self.xonxoff = xonxoff
        self.rtscts = rtscts
        self.dsrdtr = dsrdtr
        self.exclusive = exclusive
        if port is not None:
            self.open()

    # ── settings ──────────────────────────────────────────────────────────────

    @property
    def port(self) -> str | None:
        return self._port_name

    @port.setter
    def port(self, value: str | None) -> None:
        was_open = self.is_open
        if was_open:
            self.close()
        self._port_name = value
        if was_open:
            self.open()

    @property
    def name(self) -> str | None:
        return self._port_name

    @property
    def baudrate(self) -> int:
        return self._baudrate

    @baudrate.setter
    def baudrate(self, value: int) -> None:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"Not a valid baudrate: {value!r}")
        self._baudrate = value

    @property
    def timeout(self) -> float | None:
        return self._timeout

    @timeout.setter
    def timeout(self, value: float | None) -> None:
        if value is not None and value < 0:
            raise ValueError(f"Not a valid timeout: {value!r}")
        self._timeout = value

    # ── open / close ──────────────────────────────────────────────────────────

    def open(self) -> None:
        if self._port_name is None:
            raise SerialException("Port must be configured before it can be used.")
        if self.is_open:
            raise SerialException("Port is already open.")
        try:
            self._port = buses.uart(self._port_name)
        except FileNotFoundError as e:
            raise SerialException(
                e.errno, f"could not open port {self._port_name}: {e}"
            ) from None
        self.is_open = True

    def close(self) -> None:
        self.is_open = False
        self._port = None

    def __enter__(self) -> Serial:
        if self._port_name is not None and not self.is_open:
            self.open()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _open_port(self) -> buses.Port:
        if not self.is_open or self._port is None:
            raise PortNotOpenError()
        return self._port

    # ── I/O ───────────────────────────────────────────────────────────────────

    def read(self, size: int = 1) -> bytes:
        return self._open_port().read(size, self._timeout)

    def read_until(self, expected: bytes = LF, size: int | None = None) -> bytes:
        port = self._open_port()
        deadline = None if self._timeout is None else time.monotonic() + self._timeout
        line = bytearray()
        while size is None or len(line) < size:
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            byte = port.read(1, remaining)
            if not byte:
                break
            line += byte
            if line.endswith(expected):
                break
        return bytes(line)

    def readline(self, size: int = -1) -> bytes:
        return self.read_until(LF, None if size < 0 else size)

    def readlines(self, hint: int = -1) -> list[bytes]:
        lines = []
        while True:
            line = self.readline()
            if not line:
                return lines
            lines.append(line)

    def __iter__(self):
        return iter(self.readline, b"")

    def write(self, data: bytes | bytearray | memoryview) -> int:
        if isinstance(data, str):
            raise TypeError(f"unicode strings are not supported, please encode to bytes: {data!r}")
        data = bytes(data)
        self._open_port().write(data)
        return len(data)

    def flush(self) -> None:
        self._open_port()

    @property
    def in_waiting(self) -> int:
        return self._open_port().waiting()

    @property
    def out_waiting(self) -> int:
        self._open_port()
        return 0

    def inWaiting(self) -> int:  # noqa: N802 — pyserial 2 name
        return self.in_waiting

    def reset_input_buffer(self) -> None:
        self._open_port().flush_input()

    def reset_output_buffer(self) -> None:
        self._open_port()

    flushInput = reset_input_buffer  # noqa: N815
    flushOutput = reset_output_buffer  # noqa: N815

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return True

    def fileno(self) -> int:
        raise OSError(errno.EBADF, os.strerror(errno.EBADF))

    def __repr__(self) -> str:
        return (
            f"Serial<id=0x{id(self):x}, open={self.is_open}>(port={self._port_name!r}, "
            f"baudrate={self._baudrate!r}, bytesize={self.bytesize!r}, parity={self.parity!r}, "
            f"stopbits={self.stopbits!r}, timeout={self._timeout!r})"
        )


def serial_for_url(url: str, *args: object, do_not_open: bool = False, **kwargs: object) -> Serial:
    s = Serial(None, *args, **kwargs)
    s.port = url
    if not do_not_open:
        s.open()
    return s
