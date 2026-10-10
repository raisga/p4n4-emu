"""Tests for the emulated buses and the smbus2 / spidev / pyserial stubs."""

import errno
import threading

import pytest

from p4n4_emu.hw import buses, lgpio_stub, shims
from p4n4_emu.hw.buses import Loopback, RegisterDevice
from p4n4_emu.hw.serial_stub import PortNotOpenError, Serial, SerialException
from p4n4_emu.hw.smbus_stub import SMBus, i2c_msg
from p4n4_emu.hw.spidev_stub import SpiDev


@pytest.fixture(autouse=True)
def _clean():
    shims.reset()
    yield
    shims.reset()


class Memory(RegisterDevice):
    """A 256-byte register file, like an EEPROM page."""

    def __init__(self):
        super().__init__()
        self.mem = bytearray(range(256))

    def read_register(self, register):
        return self.mem[register]

    def write_register(self, register, value):
        self.mem[register] = value


class Echo(buses.SPIDevice):
    def transfer(self, data):
        return bytes(b ^ 0xFF for b in data)


# ── smbus2 ────────────────────────────────────────────────────────────────────

def test_smbus_reads_and_writes_registers():
    mem = Memory()
    buses.attach_i2c(1, 0x50, mem)
    with SMBus(1) as bus:
        assert bus.read_byte_data(0x50, 0x10) == 0x10
        bus.write_byte_data(0x50, 0x10, 0xAB)
        assert mem.mem[0x10] == 0xAB
        assert bus.read_i2c_block_data(0x50, 0x20, 4) == [0x20, 0x21, 0x22, 0x23]
        bus.write_i2c_block_data(0x50, 0x30, [1, 2, 3])
        assert mem.mem[0x30:0x33] == bytes([1, 2, 3])


def test_smbus_words_are_little_endian():
    mem = Memory()
    buses.attach_i2c(1, 0x50, mem)
    bus = SMBus(1)
    assert bus.read_word_data(0x50, 0x10) == 0x1110
    bus.write_word_data(0x50, 0x40, 0xBEEF)
    assert mem.mem[0x40:0x42] == bytes([0xEF, 0xBE])


def test_smbus_read_byte_continues_from_the_pointer():
    buses.attach_i2c(1, 0x50, Memory())
    bus = SMBus(1)
    bus.write_byte(0x50, 0x80)
    assert [bus.read_byte(0x50) for _ in range(3)] == [0x80, 0x81, 0x82]


def test_smbus_missing_address_is_a_remote_io_error():
    bus = SMBus(1)
    with pytest.raises(OSError) as e:
        bus.read_byte_data(0x40, 0)
    assert e.value.errno == errno.EREMOTEIO
    with pytest.raises(OSError):
        bus.write_quick(0x40)


def test_smbus_missing_bus_fails_to_open():
    with pytest.raises(FileNotFoundError):
        SMBus(3)
    with pytest.raises(FileNotFoundError):
        SMBus("/dev/i2c-3")
    assert SMBus("/dev/i2c-1").fd is not None


def test_smbus_closed_bus_raises():
    buses.attach_i2c(1, 0x50, Memory())
    bus = SMBus(1)
    bus.close()
    with pytest.raises(OSError):
        bus.read_byte(0x50)


def test_smbus_block_length_limit():
    buses.attach_i2c(1, 0x50, Memory())
    with pytest.raises(ValueError):
        SMBus(1).read_i2c_block_data(0x50, 0, 33)


def test_i2c_rdwr_combined_transfer():
    buses.attach_i2c(1, 0x50, Memory())
    write = i2c_msg.write(0x50, [0x05])
    read = i2c_msg.read(0x50, 3)
    SMBus(1).i2c_rdwr(write, read)
    assert list(read) == [5, 6, 7]
    assert bytes(read) == b"\x05\x06\x07"


def test_attach_rejects_reserved_addresses():
    with pytest.raises(ValueError):
        buses.attach_i2c(1, 0x00, Memory())


# ── spidev ────────────────────────────────────────────────────────────────────

def test_spidev_transfers_to_the_part():
    buses.attach_spi(0, 0, Echo())
    spi = SpiDev()
    spi.open(0, 0)
    assert spi.xfer2([0x00, 0x0F]) == [0xFF, 0xF0]
    assert spi.xfer([0xFF]) == [0x00]
    assert spi.readbytes(2) == [0xFF, 0xFF]


def test_spidev_with_nothing_attached_reads_zeros():
    spi = SpiDev(0, 1)
    assert spi.xfer2([1, 2, 3]) == [0, 0, 0]


def test_spidev_missing_device_and_closed():
    with pytest.raises(FileNotFoundError):
        SpiDev(1, 0)
    spi = SpiDev(0, 0)
    spi.close()
    with pytest.raises(OSError):
        spi.xfer2([0])


def test_spidev_mode_is_checked():
    spi = SpiDev()
    spi.mode = 3
    with pytest.raises(TypeError):
        spi.mode = 4


def test_spidev_lsbfirst_reverses_bits_on_the_wire():
    seen = []

    class Spy(buses.SPIDevice):
        def transfer(self, data):
            seen.append(data)
            return b"\x01"

    buses.attach_spi(0, 0, Spy())
    spi = SpiDev(0, 0)
    spi.lsbfirst = True
    assert spi.xfer2([0x01]) == [0x80]
    assert seen == [b"\x80"]


# ── pyserial ──────────────────────────────────────────────────────────────────

def test_serial_opens_the_pis_ports_only():
    assert Serial("/dev/serial0").is_open
    with pytest.raises(SerialException, match="could not open port /dev/ttyUSB0"):
        Serial("/dev/ttyUSB0")


def test_serial_read_times_out_with_what_arrived():
    port = buses.uart("/dev/serial0")
    port.feed(b"ab")
    s = Serial("/dev/serial0", timeout=0.05)
    assert s.read(5) == b"ab"
    assert s.read(1) == b""


def test_serial_readline_and_in_waiting():
    buses.uart("/dev/serial0").feed(b"$GPGGA,1\r\n$GPRMC,2\r\nrest")
    s = Serial("/dev/serial0", timeout=0.05)
    assert s.in_waiting == 24
    assert s.readline() == b"$GPGGA,1\r\n"
    assert s.read_until(b"\r\n") == b"$GPRMC,2\r\n"
    assert s.readline() == b"rest"  # no newline before the timeout


def test_serial_blocking_read_waits_for_data():
    s = Serial("/dev/serial0")  # timeout None: block until 3 bytes arrive
    threading.Timer(0.05, buses.uart("/dev/serial0").feed, (b"xyz",)).start()
    assert s.read(3) == b"xyz"


def test_serial_writes_are_recorded_and_a_loopback_echoes():
    buses.attach_uart("/dev/ttyAMA0", Loopback())
    with Serial("/dev/ttyAMA0", 115200, timeout=0.05) as s:
        assert s.write(b"AT\r\n") == 4
        assert s.readline() == b"AT\r\n"
    assert bytes(buses.uart("/dev/ttyAMA0").tx) == b"AT\r\n"
    assert not s.is_open


def test_serial_closed_port_and_text_writes():
    s = Serial()
    with pytest.raises(PortNotOpenError):
        s.read()
    s.port = "/dev/serial0"
    s.open()
    with pytest.raises(TypeError):
        s.write("text")


def test_attach_uart_adds_a_port():
    buses.attach_uart("/dev/ttyUSB0")
    assert Serial("/dev/ttyUSB0").is_open


# ── lgpio's I2C and SPI ───────────────────────────────────────────────────────

def test_lgpio_i2c_on_the_same_bus():
    buses.attach_i2c(1, 0x50, Memory())
    h = lgpio_stub.i2c_open(1, 0x50)
    assert lgpio_stub.i2c_read_byte_data(h, 0x07) == 7
    assert lgpio_stub.i2c_read_i2c_block_data(h, 0x10, 2) == (2, bytearray(b"\x10\x11"))
    lgpio_stub.i2c_write_byte_data(h, 0x07, 0x99)
    assert lgpio_stub.i2c_read_word_data(h, 0x07) == 0x0899
    lgpio_stub.i2c_close(h)


def test_lgpio_i2c_errors():
    with pytest.raises(lgpio_stub.error, match="can not open I2C device"):
        lgpio_stub.i2c_open(5, 0x50)
    h = lgpio_stub.i2c_open(1, 0x51)  # opening doesn't probe; the transfer fails
    with pytest.raises(lgpio_stub.error, match="I2C read failed"):
        lgpio_stub.i2c_read_byte(h)


def test_lgpio_spi():
    buses.attach_spi(0, 0, Echo())
    h = lgpio_stub.spi_open(0, 0, 500_000)
    assert lgpio_stub.spi_xfer(h, b"\x0f") == (1, bytearray(b"\xf0"))
    assert lgpio_stub.spi_write(h, [1, 2]) == 2
    with pytest.raises(lgpio_stub.error):
        lgpio_stub.spi_open(2, 0, 500_000)
