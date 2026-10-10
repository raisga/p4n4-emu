"""Tests for replaying recorded readings (CSV and InfluxDB exports)."""

import json
import threading
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from p4n4_emu.cli import app
from p4n4_emu.commands import sim
from p4n4_emu.sim import replay, scenario, sensor_sim
from p4n4_emu.sim.scenario import Replay, ScenarioError, parse_scenario
from p4n4_emu.utils import stack_config

runner = CliRunner()

PLAIN = """\
time,device,measurement,value,unit
2026-10-01T12:00:02Z,greenhouse-01,humidity,61.5,%
2026-10-01T12:00:00Z,greenhouse-01,temperature,21.5,C
2026-10-01T12:00:04Z,greenhouse-02,temperature,,C
2026-10-01T12:00:04Z,greenhouse-02,temperature,19,C
"""

# What `influx query --raw` gives for the iot stack's bucket: annotations, one table
# per series (each with its header), nanosecond times, and other measurements
INFLUX = """\
#group,false,false,true,true,false,false,true,true,true,true
#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,dateTime:RFC3339,double,string,string,string,string
#default,_result,,,,,,,,,
,result,table,_start,_stop,_time,_value,_field,_measurement,device,sensor
,,0,2026-10-01T11:00:00Z,2026-10-01T13:00:00Z,2026-10-01T12:00:00.123456789Z,21.5,value,sensor_data,gh-01,temperature
,,0,2026-10-01T11:00:00Z,2026-10-01T13:00:00Z,2026-10-01T12:00:10.123456789Z,21.7,value,sensor_data,gh-01,temperature

#group,false,false,true,true,false,false,true,true,true,true
#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,string
#default,_result,,,,,,,,,
,result,table,_start,_stop,_time,_value,_field,_measurement,device,sensor
,,1,2026-10-01T11:00:00Z,2026-10-01T13:00:00Z,2026-10-01T12:00:00.123456789Z,C,unit,sensor_data,gh-01,temperature
,,1,2026-10-01T11:00:00Z,2026-10-01T13:00:00Z,2026-10-01T12:00:10.123456789Z,C,unit,sensor_data,gh-01,temperature
,,2,2026-10-01T11:00:00Z,2026-10-01T13:00:00Z,2026-10-01T12:00:05Z,0.9,confidence,inference,gh-01,
"""


@pytest.fixture
def plain(tmp_path):
    path = tmp_path / "recorded.csv"
    path.write_text(PLAIN)
    return path


@pytest.fixture
def influx(tmp_path):
    path = tmp_path / "export.csv"
    path.write_text(INFLUX)
    return path


# ── parsing ───────────────────────────────────────────────────────────────────

def test_plain_csv(plain):
    rec = replay.load(plain)
    assert rec.format == "csv"
    assert [(r.device, r.measurement, r.payload) for r in rec.records] == [
        ("greenhouse-01", "temperature", {"value": 21.5, "unit": "C"}),
        ("greenhouse-01", "humidity", {"value": 61.5, "unit": "%"}),
        ("greenhouse-02", "temperature", {"unit": "C"}),  # an empty value is left out
        ("greenhouse-02", "temperature", {"value": 19, "unit": "C"}),
    ]
    assert rec.devices == ["greenhouse-01", "greenhouse-02"]
    assert rec.span == 4.0


def test_influxdb_export(influx):
    rec = replay.load(influx)
    assert rec.format == "influxdb"
    assert rec.skipped == 1  # the inference row
    assert [(r.device, r.measurement, r.payload) for r in rec.records] == [
        ("gh-01", "temperature", {"value": 21.5, "unit": "C"}),
        ("gh-01", "temperature", {"value": 21.7, "unit": "C"}),
    ]
    assert rec.records[0].time == pytest.approx(1790856000.123456)
    assert rec.span == pytest.approx(10.0)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("1790856000", 1790856000.0),
        ("1790856000500", 1790856000.5),
        ("1790856000500000", 1790856000.5),
        ("1790856000500000000", 1790856000.5),
        ("2026-10-01T12:00:00.5Z", 1790856000.5),
        ("2026-10-01T12:00:00", 1790856000.0),  # no zone: UTC
        ("2026-10-01T14:00:00+02:00", 1790856000.0),
    ],
)
def test_times(text, seconds):
    assert replay.parse_time(text) == pytest.approx(seconds)


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("", "no readings"),
        ("a,b\n1,2\n", "no time column"),
        ("time,device,value\n1,d,2\n", "no measurement column"),
        ("time,device,measurement,value\nyesterday,d,t,1\n", "line 2: not a time"),
        ("time,device,measurement,value\n1,d/x,t,1\n", "line 2: device 'd/x'"),
        ("time,device,measurement,value\n1,d,t,\n", "line 2: no value"),
    ],
)
def test_errors_name_the_line(tmp_path, text, error):
    path = tmp_path / "bad.csv"
    path.write_text(text)
    with pytest.raises(replay.ReplayError) as e:
        replay.load(path)
    assert error in str(e.value) and "bad.csv" in str(e.value)


def test_scenario_replays(plain):
    sc = parse_scenario({"replay": [{"file": plain.name, "speed": 10, "loop": True,
                                     "devices": {"greenhouse-01": "gh-a"}}]}, base=plain.parent)
    assert sc.devices == ()
    assert sc.replays == (Replay(str(plain), 10.0, True, {"greenhouse-01": "gh-a"}),)
    assert sc.files == ((plain.name, str(plain)),)


@pytest.mark.parametrize(
    ("entry", "error"),
    [
        ({"file": "missing.csv"}, "replay[0].file: no such file"),
        ({"file": "recorded.csv", "speed": -1}, "speed: can't be negative"),
        ({"file": "recorded.csv", "speed": 0, "loop": True}, "loop needs a speed above 0"),
        ({"file": "recorded.csv", "devices": {"a": "b/c"}}, "devices: expected recorded id"),
        ({"file": "recorded.csv", "every": 1}, "unknown key(s) every"),
    ],
)
def test_replay_errors(plain, entry, error):
    with pytest.raises(ScenarioError) as e:
        parse_scenario({"replay": [entry]}, base=plain.parent)
    assert error in str(e.value)


def test_a_scenario_without_devices_needs_a_replay():
    with pytest.raises(ScenarioError, match="devices: expected a non-empty list"):
        parse_scenario({"interval": 1})


# ── the schedule ──────────────────────────────────────────────────────────────

def test_replayer_keeps_the_gaps_divided_by_the_speed(plain):
    r = replay.Replayer(Replay(str(plain), speed=2.0, devices={"greenhouse-01": "gh-a"}), 100.0)
    assert r.next_due == 100.0
    assert [t for t, _ in r.pop_due(100.0)] == ["sensors/gh-a/temperature"]
    assert r.next_due == 101.0  # 2 s recorded, at twice the speed
    assert r.pop_due(100.9) == []
    (topic, payload), = r.pop_due(101.0)
    assert topic == "sensors/gh-a/humidity" and json.loads(payload)["value"] == 61.5
    assert len(r.pop_due(102.0)) == 2
    assert r.next_due is None  # done


def test_replayer_loops_and_stamps(plain):
    r = replay.Replayer(Replay(str(plain), speed=1.0, loop=True), 0.0)
    assert len(r.pop_due(4.0)) == 4
    # The next lap starts one typical gap (2 s) after the last reading
    assert r.next_due == pytest.approx(6.0)
    (_, payload), = r.pop_due(6.0, timestamp=True)
    assert json.loads(payload)["ts"] == 6000


def test_replayer_as_fast_as_it_can(plain):
    r = replay.Replayer(Replay(str(plain), speed=0), 50.0)
    assert len(r.pop_due(50.0)) == 4


def test_the_simulator_replays_and_ends(monkeypatch, plain):
    client = MagicMock()
    monkeypatch.setattr(sensor_sim.mqtt, "Client", lambda *a, **k: client)
    stop = threading.Event()
    now = [1000.0]
    monkeypatch.setattr(stop, "wait", lambda t: now.__setitem__(0, now[0] + t))
    published = []
    client.publish.side_effect = lambda topic, payload, **kw: published.append(
        (now[0] - 1000.0, topic)
    )
    sensor_sim.run(stop=stop, clock=lambda: now[0], replay=Replay(str(plain)))
    assert published == [
        (0.0, "sensors/greenhouse-01/temperature"),
        (2.0, "sensors/greenhouse-01/humidity"),
        (4.0, "sensors/greenhouse-02/temperature"),
        (4.0, "sensors/greenhouse-02/temperature"),
    ]
    client.disconnect.assert_called_once()


def test_replay_from_env_runs_beside_the_devices(monkeypatch, plain):
    monkeypatch.setenv("SIM_REPLAY", str(plain))
    monkeypatch.setenv("SIM_REPLAY_SPEED", "5")
    monkeypatch.setenv("SIM_REPLAY_LOOP", "1")
    sc = sensor_sim.resolve_scenario(devices=2)
    assert len(sc.devices) == 2
    assert sc.replays == (Replay(str(plain), 5.0, True),)
    monkeypatch.delenv("SIM_DEVICE_COUNT", raising=False)
    assert sensor_sim.resolve_scenario().devices == ()  # the replay alone


# ── sim start / sim check ─────────────────────────────────────────────────────

def test_sim_start_replay_options(monkeypatch, plain):
    calls = []
    monkeypatch.setattr(sim, "project_broker", lambda: stack_config.DEFAULT_BROKER_INFO)
    monkeypatch.setattr(sim, "start_simulator", lambda **kw: calls.append(kw) or 0)
    result = runner.invoke(app, ["sim", "start", "--replay", str(plain), "--speed", "10",
                                 "--loop"])
    assert result.exit_code == 0, result.output
    assert calls[0]["replay"] == Replay(str(plain), 10.0, True)


@pytest.mark.parametrize(
    ("argv", "error"),
    [
        (["--speed", "2"], "--speed and --loop go with --replay"),
        (["--replay", "r.csv", "--speed", "0", "--loop"], "--loop needs a --speed above 0"),
    ],
)
def test_sim_start_replay_errors(monkeypatch, argv, error):
    monkeypatch.setattr(sim, "project_broker", lambda: stack_config.DEFAULT_BROKER_INFO)
    result = runner.invoke(app, ["sim", "start", *argv])
    assert result.exit_code == 1
    assert error in result.output


def test_run_options_mount_the_replay_and_the_scenarios_files(plain, tmp_path):
    wav = tmp_path / "door.wav"
    wav.write_bytes(b"")
    opts, _ = sim._run_options(
        None, None, None, stack_config.DEFAULT_BROKER_INFO, sim.Connection(), None, None,
        files=(("clips/door.wav", str(wav)),), replay=Replay(str(plain), 4.0, True),
    )
    joined = " ".join(opts)
    assert f"{wav}:/etc/p4n4-sim/files/0-door.wav:ro" in joined
    i = opts.index(next(o for o in opts if o.startswith("SIM_FILES=")))
    mapping = json.loads(opts[i].split("=", 1)[1])
    assert mapping == {"clips/door.wav": "/etc/p4n4-sim/files/0-door.wav"}
    assert f"{plain}:/etc/p4n4-sim/replay.csv:ro" in joined
    assert "SIM_REPLAY=/etc/p4n4-sim/replay.csv" in opts
    assert "SIM_REPLAY_SPEED=4" in opts and "SIM_REPLAY_LOOP=1" in opts


def test_start_simulator_checks_the_recording_first(monkeypatch, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n")
    monkeypatch.setattr(sim, "ensure_image", lambda **kw: pytest.fail("got the image"))
    assert sim.start_simulator(replay=Replay(str(bad))) == 1


def test_sim_check_a_recording(influx):
    result = runner.invoke(app, ["sim", "check", str(influx)], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "influxdb" in result.output and "gh-01" in result.output
    assert "1 other rows skipped" in result.output


def test_sim_check_a_scenario_with_everything(tmp_path, plain):
    import wave

    with wave.open(str(tmp_path / "door.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 100)
    path = tmp_path / "s.yml"
    path.write_text(
        "devices:\n"
        "  - id: esp32-a\n"
        "    mcu: {firmware: 1.2.0, sleep: 60, wifi_drop: {probability: 0.1}, buffer: 5}\n"
        "    measurements:\n"
        "      camera: {kind: image, width: 320, height: 240, encoding: uint8}\n"
        "      mic: {kind: audio, file: door.wav}\n"
        "replay:\n"
        f"  - {{file: {plain.name}, speed: 10, devices: {{greenhouse-01: gh-a}}}}\n"
    )
    result = runner.invoke(app, ["sim", "check", str(path)], env={"COLUMNS": "250"})
    assert result.exit_code == 0, result.output
    out = result.output
    assert "wakes every 60s" in out and "esp32 1.2.0" in out and "buffers 5" in out
    assert "camera (image 320×240×3, 230400 values)" in out
    assert "over the runner's MAX_FEATURES" in out
    assert "door.wav" in out
    assert "greenhouse-01→gh-a" in out and "×10" in out


def test_example_scenario_loads():
    from pathlib import Path

    examples = Path(__file__).parent.parent / "examples"
    assert scenario.load_scenario(examples / "sim-scenario.yml").devices
    edge = scenario.load_scenario(examples / "sim-edge.yml")
    assert any(d.mcu for d in edge.devices) and edge.replays
    assert replay.load(edge.replays[0].file).devices == ["greenhouse-01", "greenhouse-02"]
