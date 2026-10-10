"""Tests for the register-level part models, read the way real drivers read them."""

import pytest

from p4n4_emu.hw import buses, shims
from p4n4_emu.hw.devices import ADS1115, BME280, DS18B20, MCP3008, MPU6050
from p4n4_emu.hw.devices.ds18b20 import crc8
from p4n4_emu.hw.readings import Readings
from p4n4_emu.hw.smbus_stub import SMBus
from p4n4_emu.hw.spidev_stub import SpiDev
from p4n4_emu.sim import generators
from p4n4_emu.sim.scenario import Wave


@pytest.fixture(autouse=True)
def _clean():
    shims.reset()
    yield
    shims.reset()


def steady(**values):
    """Readings that hold each measurement at a fixed value."""
    return Readings("emu-test", {name: Wave(v) for name, v in values.items()})


# ── BME280 ────────────────────────────────────────────────────────────────────

def _s16(v):
    return v - 0x10000 if v & 0x8000 else v


def _s8(v):
    return v - 0x100 if v & 0x80 else v


def bme280_read(bus, addr=0x76):
    """What a datasheet driver does: read the calibration, run a forced
    measurement, and compensate with the floating-point formulas (section 8.1)."""
    c = bus.read_i2c_block_data(addr, 0x88, 26)
    e = bus.read_i2c_block_data(addr, 0xE1, 7)
    t1 = c[0] | c[1] << 8
    t2, t3 = _s16(c[2] | c[3] << 8), _s16(c[4] | c[5] << 8)
    p1 = c[6] | c[7] << 8
    p = [_s16(c[i] | c[i + 1] << 8) for i in range(8, 24, 2)]
    p2, p3, p4, p5, p6, p7, p8, p9 = p
    h1, h2, h3 = c[25], _s16(e[0] | e[1] << 8), e[2]
    h4 = _s8(e[3]) << 4 | e[4] & 0xF
    h5 = _s8(e[5]) << 4 | e[4] >> 4
    h6 = _s8(e[6])

    bus.write_byte_data(addr, 0xF2, 0x01)        # osrs_h ×1
    bus.write_byte_data(addr, 0xF4, 0b00100101)  # osrs_t ×1, osrs_p ×1, forced
    d = bus.read_i2c_block_data(addr, 0xF7, 8)
    adc_p = d[0] << 12 | d[1] << 4 | d[2] >> 4
    adc_t = d[3] << 12 | d[4] << 4 | d[5] >> 4
    adc_h = d[6] << 8 | d[7]

    var1 = (adc_t / 16384.0 - t1 / 1024.0) * t2
    var2 = ((adc_t / 131072.0 - t1 / 8192.0) ** 2) * t3
    fine = var1 + var2
    temperature = fine / 5120.0

    var1 = fine / 2.0 - 64000.0
    var2 = var1 * var1 * p6 / 32768.0 + var1 * p5 * 2.0
    var2 = var2 / 4.0 + p4 * 65536.0
    var1 = (p3 * var1 * var1 / 524288.0 + p2 * var1) / 524288.0
    var1 = (1.0 + var1 / 32768.0) * p1
    pa = 1048576.0 - adc_p
    pa = (pa - var2 / 4096.0) * 6250.0 / var1
    pa = pa + (p9 * pa * pa / 2147483648.0 + pa * p8 / 32768.0 + p7) / 16.0

    h = fine - 76800.0
    h = (adc_h - (h4 * 64.0 + h5 / 16384.0 * h)) * (
        h2 / 65536.0 * (1.0 + h6 / 67108864.0 * h * (1.0 + h3 / 67108864.0 * h))
    )
    h = h * (1.0 - h1 * h / 524288.0)
    return temperature, pa / 100, min(max(h, 0.0), 100.0)


@pytest.mark.parametrize(
    ("t", "h", "p"), [(22.0, 55.0, 1013.25), (-12.5, 18.0, 940.0), (41.3, 93.0, 1060.0)]
)
def test_bme280_reads_back_through_the_datasheet_formulas(t, h, p):
    buses.attach_i2c(1, 0x76, BME280(steady(temperature=t, humidity=h, pressure=p)))
    temperature, pressure, humidity = bme280_read(SMBus(1))
    assert temperature == pytest.approx(t, abs=0.01)
    assert pressure == pytest.approx(p, abs=0.05)
    assert humidity == pytest.approx(h, abs=0.1)


def test_bme280_registers():
    bme = BME280(steady(temperature=20))
    buses.attach_i2c(1, 0x77, bme)
    bus = SMBus(1)
    assert bus.read_byte_data(0x77, 0xD0) == 0x60
    # Asleep since power-up: data registers hold the "skipped" value
    assert bus.read_i2c_block_data(0x77, 0xFA, 3) == [0x80, 0, 0]
    bus.write_byte_data(0x77, 0xF4, 0b00100001)  # forced: measures, then sleeps again
    assert bus.read_byte_data(0x77, 0xF4) & 0b11 == 0
    assert bus.read_i2c_block_data(0x77, 0xFA, 3) != [0x80, 0, 0]
    assert bus.read_i2c_block_data(0x77, 0xF7, 3) == [0x80, 0, 0]  # pressure skipped
    bus.write_byte_data(0x77, 0xE0, 0xB6)  # soft reset
    assert bus.read_byte_data(0x77, 0xF4) == 0


def test_bme280_humidity_setting_needs_a_ctrl_meas_write():
    buses.attach_i2c(1, 0x76, BME280(steady(humidity=40)))
    bus = SMBus(1)
    bus.write_byte_data(0x76, 0xF4, 0b00100111)  # normal mode, humidity still off
    bus.write_byte_data(0x76, 0xF2, 0x01)
    assert bus.read_i2c_block_data(0x76, 0xFD, 2) == [0x80, 0x00]
    bus.write_byte_data(0x76, 0xF4, 0b00100111)
    assert bus.read_i2c_block_data(0x76, 0xFD, 2) != [0x80, 0x00]


# ── MPU-6050 ──────────────────────────────────────────────────────────────────

def _words(raw):
    return [_s16(raw[i] << 8 | raw[i + 1]) for i in range(0, len(raw), 2)]


def test_mpu6050_powers_up_asleep():
    buses.attach_i2c(1, 0x68, MPU6050(steady(temperature=25)))
    bus = SMBus(1)
    assert bus.read_byte_data(0x68, 0x75) == 0x68
    assert bus.read_byte_data(0x68, 0x6B) == 0x40
    assert bus.read_i2c_block_data(0x68, 0x3B, 14) == [0] * 14


def test_mpu6050_reads_gravity_and_temperature():
    buses.attach_i2c(1, 0x68, MPU6050(steady(temperature=25)))
    bus = SMBus(1)
    bus.write_byte_data(0x68, 0x6B, 0)  # wake
    ax, ay, az, temp, gx, gy, gz = _words(bus.read_i2c_block_data(0x68, 0x3B, 14))
    assert az / 16384 == pytest.approx(1.0, abs=0.15)
    assert abs(ax) / 16384 < 0.15 and abs(ay) / 16384 < 0.15
    assert temp / 340 + 36.53 == pytest.approx(25, abs=0.01)
    assert all(abs(g) / 131 < 1 for g in (gx, gy, gz))
    bus.write_byte_data(0x68, 0x1C, 2 << 3)  # ±8 g: 4096 LSB/g
    az = _words(bus.read_i2c_block_data(0x68, 0x3F, 2))[0]
    assert az / 4096 == pytest.approx(1.0, abs=0.15)


# ── ADS1115 ───────────────────────────────────────────────────────────────────

def _ads_read(bus, config):
    bus.write_i2c_block_data(0x48, 0x01, [config >> 8, config & 0xFF])
    msb, lsb = bus.read_i2c_block_data(0x48, 0x00, 2)
    return _s16(msb << 8 | lsb)


def test_ads1115_single_shot_conversion():
    buses.attach_i2c(1, 0x48, ADS1115(steady(a0=1.2, a1=0.5, a2=3.0, a3=0.1)))
    bus = SMBus(1)
    # OS=1, MUX=AIN1/GND, PGA=±4.096 V, single-shot
    raw = _ads_read(bus, 0x8000 | 0b101 << 12 | 0b001 << 9 | 1 << 8 | 0x83)
    assert raw * 4.096 / 32768 == pytest.approx(0.5, abs=0.001)
    config = bus.read_i2c_block_data(0x48, 0x01, 2)
    assert config[0] & 0x80  # OS: no conversion in progress


def test_ads1115_differential_and_clipping():
    buses.attach_i2c(1, 0x48, ADS1115(steady(a0=1.2, a1=0.5, a2=3.0, a3=0.1)))
    bus = SMBus(1)
    raw = _ads_read(bus, 0x8000 | 0b000 << 12 | 0b010 << 9 | 1 << 8)  # AIN0 − AIN1, ±2.048 V
    assert raw * 2.048 / 32768 == pytest.approx(0.7, abs=0.001)
    raw = _ads_read(bus, 0x8000 | 0b110 << 12 | 0b010 << 9 | 1 << 8)  # AIN2 = 3 V > 2.048 V
    assert raw == 32767


def test_ads1115_defaults_without_scenario_channels():
    buses.attach_i2c(1, 0x48, ADS1115(Readings()))
    raw = _ads_read(SMBus(1), 0x8000 | 0b100 << 12 | 0b001 << 9 | 1 << 8)
    assert 0 <= raw * 4.096 / 32768 <= 3.3


# ── MCP3008 ───────────────────────────────────────────────────────────────────

def test_mcp3008_single_ended_read():
    buses.attach_spi(0, 0, MCP3008(steady(ch0=1.65, ch3=0.33)))
    spi = SpiDev(0, 0)
    r = spi.xfer2([1, (8 + 0) << 4, 0])
    assert ((r[1] & 3) << 8 | r[2]) == 512
    r = spi.xfer2([1, (8 + 3) << 4, 0])
    assert ((r[1] & 3) << 8 | r[2]) == 102


def test_mcp3008_other_framing():
    """Start bit late in the first byte, as some drivers send it."""
    buses.attach_spi(0, 0, MCP3008(steady(ch1=1.65)))
    r = SpiDev(0, 0).xfer2([0b00000110, 0b01000000, 0])
    assert ((r[1] & 0x0F) << 6 | r[2] >> 2) == 512


# ── DS18B20 ───────────────────────────────────────────────────────────────────

def test_ds18b20_w1_slave_file():
    sensor = DS18B20(steady(temperature=23.4))
    lines = sensor.w1_slave().splitlines()
    assert lines[0].endswith("YES")
    pad = bytes(int(b, 16) for b in lines[0].split(" : ")[0].split())
    assert crc8(pad) == 0  # the CRC byte makes the 9-byte CRC zero
    assert lines[1].endswith("t=23375")  # 12-bit: 1/16 °C steps
    assert sensor.temperature() == "23375\n"
    assert sensor.id.startswith("28-") and len(sensor.id) == 15


def test_ds18b20_negative_temperature():
    sensor = DS18B20(steady(temperature=-10.125))
    assert sensor.scratchpad()[:2] == bytes([0x5E, 0xFF])
    assert sensor.temperature() == "-10125\n"


def test_ds18b20_id_is_stable_per_device():
    assert DS18B20(Readings("a")).id == DS18B20(Readings("a")).id != DS18B20(Readings("b")).id


# ── readings ──────────────────────────────────────────────────────────────────

def test_readings_follow_the_simulators_curve_for_the_device():
    wave = Wave(22.0, 3.0, 0.0)
    readings = Readings("emu-sensor-7", {"temperature": wave})
    simulator = generators.wave(wave, phase=generators.device_phase("emu-sensor-7"))
    assert readings.value("temperature") == pytest.approx(next(simulator), abs=0.01)


def test_readings_json_round_trip():
    readings = Readings("dev-1", {"temperature": Wave(30.0, 2.0, unit="C"), "raw": None})
    copy = Readings.from_json(readings.to_json())
    assert copy.device_id == "dev-1"
    assert copy.waves == {"temperature": Wave(30.0, 2.0, unit="C")}


def test_readings_from_scenario(tmp_path):
    path = tmp_path / "s.yml"
    path.write_text(
        "devices:\n"
        "  - id: room-{n}\n    count: 2\n    measurements: [humidity]\n"
        "  - id: rig\n    measurements: {temperature: {base: 60}}\n"
    )
    assert Readings.from_scenario(path).device_id == "room-0"
    rig = Readings.from_scenario(path, "rig")
    assert rig.waves["temperature"].base == 60
    with pytest.raises(ValueError, match="no device 'nope'"):
        Readings.from_scenario(path, "nope")
