"""Tests for board layouts (`run --hardware`) and the scenario's faults on the hardware path."""

import errno
import glob
import json
import os
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from p4n4_emu.cli import app
from p4n4_emu.commands import run
from p4n4_emu.hw import buses, layout, shims
from p4n4_emu.hw.devices import BME280, DS18B20, MCP3008
from p4n4_emu.hw.readings import Readings, SensorDropout
from p4n4_emu.hw.smbus_stub import SMBus
from p4n4_emu.hw.spidev_stub import SpiDev
from p4n4_emu.sim.scenario import Fault, Wave

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean():
    shims.reset()
    yield
    shims.reset()


def steady(faults=(), seed=None, **values):
    return Readings("emu-test", {n: Wave(v) for n, v in values.items()}, tuple(faults), seed)


# ── parsing ───────────────────────────────────────────────────────────────────

def test_parts_by_name_get_their_default_bus_and_address():
    parts = layout.parse({"parts": ["bme280", "mpu6050", "ads1115", "mcp3008", "ds18b20"]})
    assert [p.where for p in parts] == [
        "i2c-1 0x76", "i2c-1 0x68", "i2c-1 0x48", "spidev0.0", None,
    ]
    assert parts == layout.DEFAULT_LAYOUT


def test_parts_with_settings():
    parts = layout.parse({"parts": [
        {"part": "bme280", "bus": 3, "address": 0x77, "measurements": {"temperature": "room"}},
        {"part": "mcp3008", "bus": 1, "cs": 2, "vref": 5},
        {"part": "ds18b20", "serial": "0000075A1C2F"},
        {"part": "loopback", "port": "/dev/ttyUSB0"},
    ]})
    assert [p.where for p in parts] == [
        "i2c-3 0x77", "spidev1.2", "w1 28-0000075a1c2f", "/dev/ttyUSB0",
    ]
    assert parts[0].measurements == {"temperature": "room"}
    assert parts[1].vref == 5.0


@pytest.mark.parametrize(
    ("doc", "error"),
    [
        ([], "expected a mapping with a parts list"),
        ({"part": []}, "unknown key(s) part"),
        ({"parts": "bme280"}, "parts: expected a list"),
        ({"parts": ["dht22"]}, "parts[0].part: expected one of"),
        ({"parts": [{"part": "bme280", "address": 0x40}]}, "a bme280 answers on 0x76, 0x77"),
        ({"parts": [{"part": "bme280", "cs": 1}]}, "unknown key(s) cs for a bme280"),
        ({"parts": [{"part": "ads1115", "measurements": {"a4": "x"}}]},
         "a ads1115 has no input(s) a4"),
        ({"parts": [{"part": "mcp3008", "measurements": ["ch0"]}]}, "input: measurement pairs"),
        ({"parts": [{"part": "mcp3008", "vref": 0}]}, "vref: expected a positive number"),
        ({"parts": [{"part": "ds18b20", "serial": "xyz"}]}, "serial: expected 12 hex digits"),
        ({"parts": [{"part": "loopback"}]}, "port: expected a device path"),
        ({"parts": ["bme280", {"part": "bme280", "address": 0x76}]},
         "parts[1]: i2c-1 0x76 is taken by parts[0]"),
    ],
)
def test_layout_errors_name_where_they_are(doc, error):
    with pytest.raises(layout.LayoutError) as e:
        layout.parse(doc)
    assert error in str(e.value)


def test_load_names_the_file(tmp_path):
    path = tmp_path / "board.yml"
    path.write_text("parts: [bme280, nope]\n")
    with pytest.raises(layout.LayoutError, match=r"board.yml: parts\[1\].part"):
        layout.load(path)


def test_measurements_must_be_the_devices():
    parts = layout.parse({"parts": [{"part": "ds18b20", "measurements": {"temperature": "x"}}]})
    with pytest.raises(layout.LayoutError, match="device 'emu-test' has no x"):
        layout.check_measurements(parts, steady(temperature=20))
    layout.check_measurements(parts, steady(x=20))


# ── attaching ─────────────────────────────────────────────────────────────────

def test_install_attaches_a_layout():
    parts = layout.parse({"parts": [
        {"part": "bme280", "bus": 3, "address": 0x77},
        {"part": "ds18b20"},
        {"part": "ds18b20"},
        {"part": "loopback", "port": "/dev/ttyUSB0"},
    ]})
    shims.install(board="rpi5", parts=parts, readings=steady(temperature=21.5))
    assert set(buses.i2c_devices(3)) == {0x77}
    assert buses.i2c_devices(1) == {}  # the default parts aren't there
    assert SMBus(3).read_byte_data(0x77, 0xD0) == 0x60
    assert os.path.exists("/dev/i2c-3")
    sensors = sorted(glob.glob("/sys/bus/w1/devices/28-*"))
    assert len(sensors) == 2
    # The first keeps the serial the default board's DS18B20 has
    assert DS18B20(steady()).id in map(os.path.basename, sensors)
    port = buses.uart("/dev/ttyUSB0")
    port.write(b"ping")
    assert port.read(4, timeout=0) == b"ping"


def test_measurements_feed_the_inputs_they_name():
    readings = steady(room=30.0, soil=1.2)
    parts = layout.parse({"parts": [
        {"part": "bme280", "measurements": {"temperature": "room"}},
        {"part": "mcp3008", "measurements": {"ch3": "soil"}},
    ]})
    shims.attach_parts(parts, readings)
    bme = buses.i2c_devices(1)[0x76]
    assert isinstance(bme, BME280) and bme.inputs["temperature"] == "room"
    spi = SpiDev()
    spi.open(0, 0)
    # MCP3008 single-ended read of channel 3
    reply = spi.xfer2([1, (8 | 3) << 4, 0])
    code = (reply[1] & 3) << 8 | reply[2]
    assert code == int(1.2 / 3.3 * 1024)


def test_part_models_refuse_inputs_they_lack():
    with pytest.raises(ValueError, match="no input"):
        MCP3008(steady(), measurements={"ch9": "x"})


# ── faults on the hardware path ───────────────────────────────────────────────

def test_value_faults_change_what_parts_read():
    readings = steady(
        temperature=20.0,
        faults=[Fault("spike", magnitude=5.0, measurement="temperature")],
        seed=1,
    )
    assert readings.value("temperature") in (15.0, 25.0)


def test_drift_and_stuck(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("p4n4_emu.hw.readings.time.time", lambda: now[0])
    readings = steady(temperature=20.0, faults=[Fault("drift", rate=0.5)])
    readings._injector.start = 1000.0
    now[0] = 1010.0
    assert readings.value("temperature") == 25.0

    stuck = steady(humidity=40.0, faults=[Fault("stuck", duration=60)])
    first = stuck.value("humidity")
    stuck.waves["humidity"] = Wave(80.0)
    stuck._gens.clear()
    assert stuck.value("humidity") == first


def test_a_dropout_takes_the_part_off_the_bus():
    readings = steady(temperature=20.0, humidity=50.0, pressure=1000.0,
                      faults=[Fault("dropout", duration=60)])
    shims.default_parts(readings)
    bus = SMBus(1)
    with pytest.raises(OSError) as e:
        bus.write_byte_data(0x76, 0xF4, 0b00100101)  # a forced measurement
    assert e.value.errno == errno.EREMOTEIO
    spi = SpiDev()
    spi.open(0, 0)
    assert spi.xfer2([1, 0x80, 0]) == [0, 0, 0]


def test_a_measurement_dropout_only_hits_its_parts():
    readings = steady(temperature=20.0, x=1.0,
                      faults=[Fault("dropout", measurement="x", duration=60)])
    with pytest.raises(SensorDropout):
        readings.value("x")
    assert readings.value("temperature") == 20.0


def test_a_dropped_ds18b20_fails_its_crc():
    sensor = DS18B20(steady(temperature=20.0, faults=[Fault("dropout", duration=60)]))
    first, second = sensor.w1_slave().splitlines()
    assert first == "ff ff ff ff ff ff ff ff ff : crc=c9 NO"
    assert second.endswith("t=-62")
    with pytest.raises(OSError) as e:
        sensor.temperature()
    assert e.value.errno == errno.EIO


def test_faults_reach_the_scripts_process():
    readings = steady(temperature=20.0, faults=[Fault("spike", 0.5, magnitude=3)], seed=7)
    again = Readings.from_json(readings.to_json())
    assert again.faults == readings.faults and again.seed == 7


def test_mqtt_only_faults_are_ignored():
    readings = steady(temperature=20.0, faults=[Fault("delay", seconds=5),
                                                Fault("malformed")])
    assert readings.value("temperature") == 20.0


# ── p4n4-emu run ──────────────────────────────────────────────────────────────

@pytest.fixture
def execs(monkeypatch):
    calls = []

    def fake_exec(file, args, env):
        calls.append(env)
        raise SystemExit(0)

    monkeypatch.setattr(run.os, "execvpe", fake_exec)
    return calls


def test_run_hardware_and_gpiod(execs, tmp_path):
    board = tmp_path / "board.yml"
    board.write_text("parts:\n  - {part: bme280, address: 0x77}\n")
    result = runner.invoke(app, ["run", "--hardware", str(board), "--gpiod", "v1", "x.py"])
    assert result.exit_code == 0, result.output
    (env,) = execs
    assert json.loads(env["P4N4_EMU_HARDWARE"])[0]["address"] == 0x77
    assert env["P4N4_EMU_GPIOD"] == "v1"


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--hardware", "board.yml", "--no-parts"], "mutually exclusive"),
        (["--hardware", "missing.yml"], "Cannot read layout"),
        (["--gpiod", "v3"], "--gpiod: expected one of v2, v1"),
        (["--hardware", "bad.yml"], "has no soil"),
    ],
)
def test_run_refuses_bad_hardware_options(execs, tmp_path, monkeypatch, argv, message):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "board.yml").write_text("parts: [bme280]\n")
    (tmp_path / "bad.yml").write_text(
        "parts:\n  - {part: ads1115, measurements: {a0: soil}}\n"
    )
    result = runner.invoke(app, ["run", *argv, "x.py"])
    assert result.exit_code == 2
    assert message in result.output
    assert execs == []


def test_run_end_to_end_with_a_layout_and_gpiod_v1(tmp_path):
    script = tmp_path / "probe.py"
    script.write_text(
        "import gpiod, smbus2\n"
        "print(gpiod.version_string())\n"
        "line = gpiod.Chip('gpiochip0').get_line(17)\n"
        "line.request(consumer='probe', type=gpiod.LINE_REQ_DIR_OUT, default_val=1)\n"
        "print(line.get_value(), line.consumer())\n"
        "bus = smbus2.SMBus(1)\n"
        "print(hex(bus.read_byte_data(0x77, 0xD0)))\n"
        "try:\n"
        "    bus.read_byte_data(0x76, 0xD0)\n"
        "except OSError as e:\n"
        "    print(e.errno)\n"
    )
    parts = layout.parse({"parts": [{"part": "bme280", "address": 0x77}]})
    env = run.run_env(board="rpi4", readings=Readings(), gpio_mqtt=None,
                      gpio_prefix="emu/gpio", parts=parts, gpiod="v1")
    out = subprocess.run([sys.executable, str(script)], env=env,
                         capture_output=True, text=True, timeout=60, check=True).stdout
    assert out.splitlines() == ["1.6.3", "1 probe", "0x60", str(errno.EREMOTEIO)]
