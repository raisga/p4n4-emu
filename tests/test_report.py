import json
import tarfile

import pytest
import yaml
from typer.testing import CliRunner

from p4n4_emu.cli import app
from p4n4_emu.commands import report, status
from p4n4_emu.overlays import paths
from p4n4_emu.overlays.generator import render_overlay
from p4n4_emu.profiles.loader import load_profile
from p4n4_emu.utils import usage

runner = CliRunner()

_MQTT = {"Service": "mqtt", "Name": "p4n4-mqtt", "State": "running", "Health": "healthy"}
_LIFE = {"status": "running", "restarts": 0, "oom_killed": False, "exit_code": 0}


def _write_overlay(stack_dir, profile="rpi5"):
    path = paths.overlay_path(stack_dir, "iot")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_overlay(load_profile(profile), "iot", None, services=["mqtt"]))
    return yaml.safe_load(path.read_text())


@pytest.fixture
def report_env(tmp_path, monkeypatch):
    """An iot stack under rpi5 whose containers, limits, usage and logs the test sets."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "OVERLAY_ROOT", tmp_path / "overlays")
    (tmp_path / "iot").mkdir()
    (tmp_path / "iot" / "docker-compose.yml").touch()
    overlay = _write_overlay(tmp_path / "iot")
    mqtt_limits = usage.expected_limits(overlay)["mqtt"]
    env = {
        "ps": [_MQTT],
        "applied": {"p4n4-mqtt": mqtt_limits},
        "samples": [
            {"p4n4-mqtt": usage.Usage(cpus=0.10, memory=10 * 1024**2)},
            {"p4n4-mqtt": usage.Usage(cpus=0.30, memory=30 * 1024**2)},
        ],
        "states": {"p4n4-mqtt": dict(_LIFE)},
        "sim": False,
        "limits": mqtt_limits,
    }
    readings = iter(env["samples"])
    monkeypatch.setattr(status, "cgroup_v2", lambda: True)
    monkeypatch.setattr(status.dc, "ps", lambda cwd, overlay=None: env["ps"])
    monkeypatch.setattr(report, "applied_limits", lambda names: env["applied"])
    monkeypatch.setattr(report, "live_usage", lambda names: next(readings, {}))
    monkeypatch.setattr(report.time, "sleep", lambda s: None)
    monkeypatch.setattr(report, "_exists", lambda name: env["sim"])
    monkeypatch.setattr(
        report, "_container_states", lambda names: {n: env["states"][n] for n in names}
    )
    monkeypatch.setattr(report, "_logs", lambda name, tail: f"{name} log line\n")
    monkeypatch.setattr(report, "docker_host", lambda: None)
    monkeypatch.setattr(report, "cgroup_v2", lambda engine=None: True)
    monkeypatch.setattr(report.calibration, "load", lambda: {})
    return env


def _report(*args):
    return runner.invoke(
        app, ["report", "--stack", "iot", "--samples", "2", *args], env={"COLUMNS": "200"}
    )


def _read_archive(path):
    with tarfile.open(path) as tar:
        return {m.name.split("/", 1)[1]: tar.extractfile(m).read().decode()
                for m in tar.getmembers()}


def test_report_writes_an_archive(report_env, tmp_path):
    result = _report("--output", "run.tar.gz", "--label", "instance-type=t4g.large")
    assert result.exit_code == 0, result.output
    files = _read_archive(tmp_path / "run.tar.gz")
    assert set(files) == {"report.json", "summary.md", "logs/p4n4-mqtt.log"}
    assert files["logs/p4n4-mqtt.log"] == "p4n4-mqtt log line\n"

    doc = json.loads(files["report.json"])
    assert doc["labels"] == {"instance-type": "t4g.large"}
    assert doc["sampling"] == {"samples": 2, "interval": 2.0}
    (svc,) = doc["stacks"][0]["services"]
    over = svc["usage_over_time"]
    assert over["samples"] == 2
    assert over["cpus"] == {"mean": 0.2, "peak": 0.3}
    assert over["memory"] == {"mean": 20 * 1024**2, "peak": 30 * 1024**2}
    assert svc["lifecycle"]["restarts"] == 0
    assert doc["issues"] == []

    assert "instance-type=t4g.large" in files["summary.md"]
    assert "## iot stack — rpi5" in files["summary.md"]
    assert "No issues found." in result.output


def test_report_default_name_and_directory_output(report_env, tmp_path):
    result = _report("--output", "out")
    assert result.exit_code == 0, result.output
    assert json.loads((tmp_path / "out" / "report.json").read_text())["stacks"]
    assert (tmp_path / "out" / "logs" / "p4n4-mqtt.log").exists()


def test_report_flags_oom_restarts_and_exits_on_request(report_env):
    report_env["states"]["p4n4-mqtt"].update(restarts=3, oom_killed=True)
    assert _report("--output", "a.tgz").exit_code == 0
    result = _report("--output", "b.tgz", "--fail-on-issues")
    assert result.exit_code == 1
    assert "OOM-killed" in result.output
    assert "restarted 3×" in result.output


def test_report_flags_stale_limits_and_exited_containers(report_env, tmp_path):
    report_env["applied"] = {"p4n4-mqtt": usage.Limits(cpus=1.0)}
    report_env["ps"] = [{**_MQTT, "State": "exited", "Health": ""}]
    report_env["states"]["p4n4-mqtt"].update(status="exited", exit_code=137)
    result = _report("--output", "out")
    issues = json.loads((tmp_path / "out" / "report.json").read_text())["issues"]
    texts = [i["issue"] for i in issues]
    assert "exited (exit 137)" in texts
    assert any(t.startswith("limits differ from the overlay: cpus 1.00") for t in texts)
    assert result.exit_code == 0


def test_report_warns_near_the_memory_limit(report_env, tmp_path):
    report_env["samples"][1] = {
        "p4n4-mqtt": usage.Usage(cpus=0.1, memory=int(report_env["limits"].memory * 0.95))
    }
    result = _report("--output", "out", "--fail-on-issues")
    assert result.exit_code == 0, result.output  # a warning doesn't fail the run
    (issue,) = json.loads((tmp_path / "out" / "report.json").read_text())["issues"]
    assert issue["level"] == "warning" and issue["issue"].startswith("memory peaked")


def test_report_includes_the_simulator(report_env, tmp_path):
    report_env["sim"] = True
    report_env["states"]["p4n4-sensor-sim"] = dict(_LIFE)
    assert _report("--output", "out").exit_code == 0
    assert (tmp_path / "out" / "logs" / "p4n4-sensor-sim.log").exists()
    doc = json.loads((tmp_path / "out" / "report.json").read_text())
    assert doc["simulator"]["status"] == "running"


def test_report_without_containers(report_env):
    report_env["ps"] = []
    result = _report()
    assert result.exit_code == 1
    assert "No containers found" in result.output


def test_report_rejects_a_bad_label(report_env):
    result = _report("--label", "nokey")
    assert result.exit_code == 1
    assert "KEY=VALUE" in result.output
