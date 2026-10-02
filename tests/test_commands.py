"""CLI command tests with Docker calls stubbed out."""

import subprocess

import pytest
import yaml
from typer.testing import CliRunner

from p4n4_emu.cli import app
from p4n4_emu.commands import down, setup, sim, up
from p4n4_emu.utils import preflight

runner = CliRunner()


@pytest.fixture
def stack_dir(tmp_path, monkeypatch):
    """A bare iot stack in an isolated cwd, with Docker calls recorded."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    (tmp_path / "docker-compose.yml").write_text("services:\n  mqtt: {}\n  influxdb: {}\n")
    monkeypatch.setattr(up, "_OVERLAY_ROOT", tmp_path / "overlays")
    monkeypatch.setattr(up, "detect_block_device", lambda: None)
    monkeypatch.setattr(up.dc, "list_services", lambda cwd: ["mqtt", "influxdb"])
    monkeypatch.setattr(up.dc, "up", lambda cwd, overlay=None: 0)
    monkeypatch.setattr(preflight._platform, "machine", lambda: "x86_64")
    return tmp_path


@pytest.fixture
def preflight_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(up, "check_or_exit", lambda **kw: calls.append(kw))
    return calls


def _overlay(stack_dir, profile="rpi5"):
    return yaml.safe_load((stack_dir / "overlays" / profile / "iot.emu.yml").read_text())


# ── up: platform and QEMU preflight ───────────────────────────────────────────

def test_up_arm_profile_requires_qemu_without_arch_flag(stack_dir, preflight_calls):
    result = runner.invoke(app, ["up", "--profile", "rpi5", "--stack-dir", str(stack_dir)])
    assert result.exit_code == 0, result.output
    assert preflight_calls == [{"require_qemu": True, "platform": "linux/arm64"}]
    assert _overlay(stack_dir)["services"]["mqtt"]["platform"] == "linux/arm64"


def test_up_native_skips_platform_and_qemu(stack_dir, preflight_calls):
    result = runner.invoke(
        app, ["up", "--profile", "rpi5", "--native", "--stack-dir", str(stack_dir)]
    )
    assert result.exit_code == 0, result.output
    assert preflight_calls[0]["require_qemu"] is False
    assert "platform" not in _overlay(stack_dir)["services"]["mqtt"]


def test_up_arm_profile_on_arm_host_needs_no_qemu(stack_dir, preflight_calls, monkeypatch):
    monkeypatch.setattr(preflight._platform, "machine", lambda: "aarch64")
    result = runner.invoke(app, ["up", "--profile", "rpi5", "--stack-dir", str(stack_dir)])
    assert result.exit_code == 0, result.output
    assert preflight_calls[0]["require_qemu"] is False


def test_up_arch_flag_overrides_x86_profile(stack_dir, preflight_calls):
    result = runner.invoke(
        app, ["up", "--profile", "nuc", "--arch", "arm64", "--stack-dir", str(stack_dir)]
    )
    assert result.exit_code == 0, result.output
    assert preflight_calls[0]["require_qemu"] is True
    assert _overlay(stack_dir, "nuc")["services"]["mqtt"]["platform"] == "linux/arm64"


def test_up_rejects_unknown_arch(stack_dir, preflight_calls):
    result = runner.invoke(app, ["up", "--arch", "sparc", "--stack-dir", str(stack_dir)])
    assert result.exit_code == 1
    assert "Unknown architecture" in result.output


def test_up_overlay_matches_compose_services(stack_dir, preflight_calls):
    result = runner.invoke(app, ["up", "--profile", "rpi5", "--stack-dir", str(stack_dir)])
    assert result.exit_code == 0, result.output
    assert set(_overlay(stack_dir)["services"]) == {"mqtt", "influxdb"}


# ── up --sim ──────────────────────────────────────────────────────────────────

def test_up_sim_uses_shared_start_with_options(stack_dir, preflight_calls, monkeypatch):
    calls = []
    monkeypatch.setattr(up, "start_simulator", lambda **kw: calls.append(kw) or 0)
    result = runner.invoke(
        app,
        ["up", "--native", "--stack-dir", str(stack_dir), "--sim",
         "--sim-interval", "0.5", "--sim-devices", "3"],
    )
    assert result.exit_code == 0, result.output
    assert calls == [{"interval": 0.5, "devices": 3}]


def test_start_simulator_builds_before_running(monkeypatch):
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd[:2])
        return subprocess.CompletedProcess(cmd, 0, stdout="healthy")

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    monkeypatch.setattr(sim, "_image_exists", lambda: False)
    assert sim.start_simulator() == 0
    # build completes (run is synchronous) before the broker wait and the container start
    assert commands == [
        ["docker", "build"], ["docker", "inspect"], ["docker", "rm"], ["docker", "run"],
    ]


def test_start_simulator_stops_when_build_fails(monkeypatch):
    commands = []

    def fake_run(cmd, **kwargs):
        commands.append(cmd[:2])
        return subprocess.CompletedProcess(cmd, 1)

    monkeypatch.setattr(sim.subprocess, "run", fake_run)
    monkeypatch.setattr(sim, "_image_exists", lambda: False)
    assert sim.start_simulator() == 1
    assert commands == [["docker", "build"]]


def test_wait_for_broker_times_out(monkeypatch):
    monkeypatch.setattr(sim, "_broker_state", lambda c: "starting")
    monkeypatch.setattr(sim.time, "sleep", lambda s: None)
    assert sim._wait_for_broker("p4n4-mqtt", timeout=0) is False


# ── down ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def down_calls(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for stack in ("iot", "ai"):
        (tmp_path / stack).mkdir()
        (tmp_path / stack / "docker-compose.yml").touch()
    monkeypatch.setattr(down.dc, "down", lambda cwd, overlay=None, volumes=False: 0)
    calls = []
    monkeypatch.setattr(
        down.subprocess, "run", lambda cmd, **kw: calls.append(cmd)
    )
    return calls


def test_down_ai_keeps_simulator(down_calls):
    result = runner.invoke(app, ["down", "--stack", "ai"])
    assert result.exit_code == 0, result.output
    assert down_calls == []


def test_down_iot_removes_simulator(down_calls):
    result = runner.invoke(app, ["down", "--stack", "iot"])
    assert result.exit_code == 0, result.output
    assert down_calls == [["docker", "rm", "-f", "p4n4-sensor-sim"]]


# ── setup ─────────────────────────────────────────────────────────────────────

def test_setup_does_not_install_qemu_without_docker(monkeypatch):
    monkeypatch.setattr(preflight._platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(
        setup, "run_preflight",
        lambda **kw: ["Docker is not running or not installed.", "QEMU binfmt ... missing"],
    )
    installs = []
    monkeypatch.setattr(setup, "_install_binfmt", lambda p: installs.append(p))
    result = runner.invoke(app, ["setup", "--arch", "arm64"])
    assert result.exit_code == 1
    assert installs == []


def test_setup_installs_qemu_when_only_qemu_missing(monkeypatch):
    monkeypatch.setattr(preflight._platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(setup, "run_preflight", lambda **kw: ["QEMU binfmt ... missing"])
    installs = []
    monkeypatch.setattr(setup, "_install_binfmt", lambda p: installs.append(p))
    result = runner.invoke(app, ["setup", "--arch", "arm64"])
    assert result.exit_code == 0, result.output
    assert installs == ["linux/arm64"]


def test_setup_check_only_fails_when_qemu_missing(monkeypatch):
    monkeypatch.setattr(preflight._platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(setup, "run_preflight", lambda **kw: ["QEMU binfmt ... missing"])
    result = runner.invoke(app, ["setup", "--arch", "arm64", "--check-only"])
    assert result.exit_code == 1
