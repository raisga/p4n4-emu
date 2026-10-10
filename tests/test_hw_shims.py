"""Tests for installing the stubs: import shims, board files, the GPIO MQTT bridge, `run`."""

import builtins
import glob
import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from p4n4_emu.cli import app
from p4n4_emu.commands import run
from p4n4_emu.hw import gpio_stub, pins, shims, vfs
from p4n4_emu.hw.mqtt_bridge import GpioBridge, parse_address, parse_level
from p4n4_emu.hw.readings import Readings
from p4n4_emu.sim.scenario import Wave

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("GPIOZERO_PIN_FACTORY", raising=False)
    shims.reset()
    yield
    shims.reset()


# ── shims.install ─────────────────────────────────────────────────────────────

def test_install_replaces_the_hardware_libraries():
    shims.install()
    import RPi.GPIO
    import serial
    import smbus
    import smbus2
    import spidev
    from gpiod.line import Direction

    assert RPi.GPIO is gpio_stub
    assert smbus2.SMBus is smbus.SMBus
    assert spidev.SpiDev.__module__ == "p4n4_emu.hw.spidev_stub"
    assert serial.Serial.__module__ == "p4n4_emu.hw.serial_stub"
    assert Direction.OUTPUT.name == "OUTPUT"
    assert os.environ["GPIOZERO_PIN_FACTORY"] == "lgpio"


def test_install_attaches_the_default_parts():
    shims.install()
    import smbus2
    import spidev

    bus = smbus2.SMBus(1)
    assert bus.read_byte_data(0x76, 0xD0) == 0x60  # BME280
    assert bus.read_byte_data(0x68, 0x75) == 0x68  # MPU-6050
    assert bus.read_i2c_block_data(0x48, 0x01, 2) == [0x85, 0x83]  # ADS1115 config reset
    r = spidev.SpiDev(0, 0).xfer2([1, 0x80, 0])  # MCP3008
    assert 0 < ((r[1] & 3) << 8 | r[2]) < 1024


def test_install_without_parts_leaves_the_buses_empty():
    shims.install(parts=False)
    import smbus2

    with pytest.raises(OSError):
        smbus2.SMBus(1).read_byte_data(0x76, 0xD0)


def test_install_from_env(monkeypatch):
    readings = Readings("rig", {"temperature": Wave(31.0)})
    monkeypatch.setenv("P4N4_EMU_BOARD", "rpi4")
    monkeypatch.setenv("P4N4_EMU_READINGS", readings.to_json())
    shims.install_from_env()
    assert gpio_stub.RPI_INFO["TYPE"] == "Pi 4 Model B"
    (path,) = glob.glob("/sys/bus/w1/devices/28-*/temperature")
    assert open(path).read() == "31000\n"


# ── board files ───────────────────────────────────────────────────────────────

def test_device_tree_files():
    shims.install(board="rpi5")
    assert open("/proc/device-tree/model").read() == "Raspberry Pi 5 Model B Rev 1.0\0"
    with open("/proc/device-tree/system/linux,revision", "rb") as f:
        assert struct.unpack(">I", f.read(4))[0] == 0xD04170
    assert Path("/proc/device-tree/compatible").read_bytes().startswith(b"raspberrypi,5")
    assert os.path.exists("/dev/gpiochip4")
    assert os.path.exists("/dev/i2c-1") and os.path.exists("/dev/spidev0.0")
    with pytest.raises(FileNotFoundError):
        open("/proc/device-tree/nothing-here")


def test_one_wire_sensor_files_are_listed_and_fresh_on_every_read():
    values = iter([20.0, 20.5])

    class Changing(Readings):
        def value(self, name, default=None):
            return next(values)

    shims.install(parts=False)
    sensor = shims.DS18B20(Changing("w1-test"))
    shims.attach_onewire(sensor)
    assert sorted(os.listdir("/sys/bus/w1/devices")) == [sensor.id, "w1_bus_master1"]
    (folder,) = [p for p in Path("/sys/bus/w1/devices").iterdir() if p.name.startswith("28-")]
    assert str(folder) == f"/sys/bus/w1/devices/{sensor.id}"
    assert (folder / "w1_slave").read_text().endswith("t=20000\n")
    assert (folder / "w1_slave").read_text().endswith("t=20500\n")
    master = Path("/sys/bus/w1/devices/w1_bus_master1/w1_master_slaves")
    assert master.read_text() == f"{sensor.id}\n"


def test_other_paths_are_untouched(tmp_path):
    shims.install()
    target = tmp_path / "plain.txt"
    target.write_text("host file")
    assert open(target).read() == "host file"
    assert os.listdir(tmp_path) == ["plain.txt"]


def test_reset_restores_the_filesystem():
    original = builtins.open
    shims.install()
    assert builtins.open is not original
    shims.reset()
    assert builtins.open is original
    assert vfs.root() is None


# ── GPIO MQTT bridge ──────────────────────────────────────────────────────────

class FakeClient:
    def __init__(self):
        self.published = []
        self.subscribed = []

    def reconnect_delay_set(self, *a):
        pass

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, retain))

    def subscribe(self, topic):
        self.subscribed.append(topic)


class Message:
    def __init__(self, topic, payload):
        self.topic = topic
        self.payload = payload


class ReasonCode:
    is_failure = False


@pytest.fixture
def bridge():
    client = FakeClient()
    b = GpioBridge("broker", client=client)
    pins.watch(b.on_change)
    yield b, client
    pins.unwatch(b.on_change)


def test_bridge_drives_inputs(bridge):
    b, _ = bridge
    gpio_stub.setmode(gpio_stub.BCM)
    gpio_stub.setup(27, gpio_stub.IN, pull_up_down=gpio_stub.PUD_UP)
    calls = []
    gpio_stub.add_event_detect(27, gpio_stub.FALLING, callback=calls.append)
    b._on_message(None, None, Message("emu/gpio/27/set", b"low"))
    assert calls == [27] and pins.read(27) == 0
    b._on_message(None, None, Message("emu/gpio/27/set", b"release"))
    assert pins.read(27) == 1  # back to its pull-up


def test_bridge_ignores_bad_messages(bridge):
    b, _ = bridge
    gpio_stub.setmode(gpio_stub.BCM)
    gpio_stub.setup(17, gpio_stub.OUT)
    for topic, payload in [("emu/gpio/x/set", b"1"), ("emu/gpio/27/set", b"maybe"),
                           ("emu/gpio/17/set", b"1")]:  # 17 is an output: can't be driven
        b._on_message(None, None, Message(topic, payload))
    assert pins.read(17) == 0


def test_bridge_publishes_outputs_and_pwm(bridge):
    _, client = bridge
    gpio_stub.setmode(gpio_stub.BCM)
    gpio_stub.setup(17, gpio_stub.OUT)
    gpio_stub.output(17, gpio_stub.HIGH)
    gpio_stub.setup(18, gpio_stub.OUT)
    pwm = gpio_stub.PWM(18, 200)
    pwm.start(30)
    pwm.stop()
    assert ("emu/gpio/17/state", "1", True) in client.published
    pwm_msgs = [json.loads(p) for t, p, _ in client.published if t == "emu/gpio/18/pwm"]
    assert pwm_msgs == [{"frequency": 200.0, "duty": 30.0}, {}]


def test_bridge_publishes_current_pins_on_connect(bridge):
    b, client = bridge
    gpio_stub.setmode(gpio_stub.BCM)
    gpio_stub.setup(17, gpio_stub.OUT, initial=gpio_stub.HIGH)
    client.published.clear()
    b._on_connect(client, None, None, ReasonCode(), None)
    assert client.subscribed == ["emu/gpio/+/set"]
    assert client.published == [("emu/gpio/17/state", "1", True)]


def test_parse_level_and_address():
    assert [parse_level(p) for p in (b"1", b"HIGH", b" off ", b"release")] == [1, 1, 0, None]
    with pytest.raises(ValueError):
        parse_level(b"2")
    assert parse_address("localhost") == ("localhost", 1883)
    assert parse_address("10.0.0.5:1884") == ("10.0.0.5", 1884)
    with pytest.raises(ValueError):
        parse_address("host:port")


# ── p4n4-emu run ──────────────────────────────────────────────────────────────

@pytest.fixture
def execs(monkeypatch):
    calls = []

    def fake_exec(file, args, env):
        calls.append((file, args, env))
        raise SystemExit(0)

    monkeypatch.setattr(run.os, "execvpe", fake_exec)
    return calls


def test_run_execs_the_script_with_the_stubs_on_its_path(execs):
    result = runner.invoke(app, ["run", "-p", "rpi4", "button.py", "--pin", "27", "-v"])
    assert result.exit_code == 0, result.output
    ((file, args, env),) = execs
    assert file == sys.executable
    assert args == [sys.executable, "button.py", "--pin", "27", "-v"]
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(run.SITE_DIR)
    assert (run.SITE_DIR / "sitecustomize.py").is_file()
    assert env["P4N4_EMU_BOARD"] == "rpi4"
    assert json.loads(env["P4N4_EMU_READINGS"])["device"] == "emu-sensor-0"
    assert "P4N4_EMU_GPIO_MQTT" not in env


def test_run_options(execs, tmp_path):
    scenario = tmp_path / "s.yml"
    scenario.write_text("devices:\n  - id: rig\n    measurements: {temperature: {base: 60}}\n")
    result = runner.invoke(app, [
        "run", "--scenario", str(scenario), "--gpio-mqtt", "localhost:1884", "--no-parts",
        "--python", sys.executable, "-c", "print()",
    ])
    assert result.exit_code == 0, result.output
    ((_, args, env),) = execs
    assert args[1:] == ["-c", "print()"]
    readings = Readings.from_json(env["P4N4_EMU_READINGS"])
    assert readings.device_id == "rig" and readings.waves["temperature"].base == 60
    assert env["P4N4_EMU_GPIO_MQTT"] == "localhost:1884"
    assert env["P4N4_EMU_PARTS"] == "0"


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["-p", "nuc"], "no GPIO header"),
        (["-p", "nope"], "Unknown profile"),
        (["--gpio-mqtt", "host:x"], "HOST[:PORT]"),
        (["--python", "/no/such/python"], "Interpreter not found"),
    ],
)
def test_run_refuses_bad_options(execs, argv, message):
    result = runner.invoke(app, ["run", *argv, "script.py"])
    assert result.exit_code == 2
    assert message in result.output
    assert execs == []


def test_run_end_to_end(tmp_path):
    """A real interpreter, started the way `run` starts it, sees only the stubs."""
    other_site = tmp_path / "other"
    other_site.mkdir()
    (other_site / "sitecustomize.py").write_text("import os\nos.environ['OTHER_SITE'] = '1'\n")
    script = tmp_path / "probe.py"
    script.write_text(
        "import glob, os, sys\n"
        "import RPi.GPIO as GPIO, smbus2\n"
        "GPIO.setmode(GPIO.BCM)\n"
        "GPIO.setup(17, GPIO.OUT, initial=GPIO.HIGH)\n"
        "print(GPIO.RPI_INFO['TYPE'])\n"
        "print(hex(smbus2.SMBus(1).read_byte_data(0x76, 0xD0)))\n"
        "print(open(glob.glob('/sys/bus/w1/devices/28-*/temperature')[0]).read().strip())\n"
        "print(os.environ.get('OTHER_SITE'), sys.argv[1:])\n"
    )
    readings = Readings("e2e", {"temperature": Wave(19.0)})
    env = run.run_env(board="rpi5", readings=readings, gpio_mqtt=None,
                      gpio_prefix="emu/gpio", parts=True)
    env["PYTHONPATH"] += os.pathsep + str(other_site)
    out = subprocess.run([sys.executable, str(script), "a", "--b"], env=env,
                         capture_output=True, text=True, timeout=60, check=True).stdout
    assert out.splitlines() == ["Pi 5 Model B", "0x60", "19000", "1 ['a', '--b']"]
