"""CLI command tests with Docker calls stubbed out."""

import subprocess

import pytest
import yaml
from typer.testing import CliRunner

from p4n4_emu.cli import app
from p4n4_emu.commands import down, logs, setup, sim, status, up
from p4n4_emu.overlays import paths
from p4n4_emu.overlays.generator import render_overlay
from p4n4_emu.profiles.loader import load_profile
from p4n4_emu.utils import preflight, stack_config, usage

runner = CliRunner()


@pytest.fixture
def stack_dir(tmp_path, monkeypatch):
    """A bare iot stack in an isolated cwd, with Docker calls recorded."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    (tmp_path / "docker-compose.yml").write_text("services:\n  mqtt: {}\n  influxdb: {}\n")
    monkeypatch.setattr(paths, "OVERLAY_ROOT", tmp_path / "overlays")
    monkeypatch.setattr(up, "detect_block_device", lambda: None)
    monkeypatch.setattr(up.dc, "list_services", lambda cwd: ["mqtt", "influxdb"])
    monkeypatch.setattr(up.dc, "up", lambda cwd, overlay=None, **kw: 0)
    monkeypatch.setattr(up.dc, "ps", lambda cwd, overlay=None: [])
    monkeypatch.setattr(up.dc, "ensure_network", lambda *a, **kw: None)
    monkeypatch.setattr(up, "load_config", lambda cwd: None)
    monkeypatch.setattr(sim, "load_config", lambda cwd: None)
    monkeypatch.setattr(preflight._platform, "machine", lambda: "x86_64")
    return tmp_path


@pytest.fixture
def preflight_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(up, "check_or_exit", lambda **kw: calls.append(kw))
    return calls


def _overlay(stack_dir, profile="rpi5"):
    doc = yaml.safe_load(paths.overlay_path(stack_dir, "iot").read_text())
    assert doc["x-p4n4-emu"]["profile"] == profile
    return doc


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
    assert calls == [
        {"interval": 0.5, "devices": 3, "broker": stack_config.DEFAULT_BROKER_INFO}
    ]


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
    monkeypatch.setattr(paths, "OVERLAY_ROOT", tmp_path / "overlays")
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


def _write_overlay(stack_dir, stack="iot", profile="rpi5", services=("mqtt",)):
    path = paths.overlay_path(stack_dir, stack)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_overlay(load_profile(profile), stack, None, services=list(services)))
    return path


def test_down_removes_the_overlay_without_profile(down_calls, tmp_path, monkeypatch):
    overlay = _write_overlay(tmp_path / "iot", profile="nuc")
    used = []
    monkeypatch.setattr(
        down.dc, "down", lambda cwd, overlay=None, volumes=False: used.append(overlay) or 0
    )
    result = runner.invoke(app, ["down", "--stack", "iot"])
    assert result.exit_code == 0, result.output
    assert used == [overlay]
    assert not overlay.exists()


def test_down_keeps_the_overlay_when_compose_fails(down_calls, tmp_path, monkeypatch):
    overlay = _write_overlay(tmp_path / "iot")
    monkeypatch.setattr(down.dc, "down", lambda cwd, overlay=None, volumes=False: 1)
    runner.invoke(app, ["down", "--stack", "iot"])
    assert overlay.exists()


def test_down_still_accepts_profile(down_calls):
    result = runner.invoke(app, ["down", "--stack", "ai", "--profile", "rpi5"])
    assert result.exit_code == 0, result.output


def test_up_in_two_projects_writes_two_overlays(tmp_path, monkeypatch, preflight_calls):
    monkeypatch.setattr(paths, "OVERLAY_ROOT", tmp_path / "overlays")
    monkeypatch.setattr(up, "detect_block_device", lambda: None)
    monkeypatch.setattr(up.dc, "list_services", lambda cwd: ["mqtt"])
    monkeypatch.setattr(up.dc, "up", lambda cwd, overlay=None, **kw: 0)
    for project, profile in (("a", "rpi5"), ("b", "nuc")):
        (tmp_path / project).mkdir()
        (tmp_path / project / "docker-compose.yml").write_text("services:\n  mqtt: {}\n")
        result = runner.invoke(
            app, ["up", "--native", "--profile", profile, "--stack-dir", str(tmp_path / project)]
        )
        assert result.exit_code == 0, result.output
    assert paths.active_profile(tmp_path / "a", "iot") == "rpi5"
    assert paths.active_profile(tmp_path / "b", "iot") == "nuc"


# ── status ────────────────────────────────────────────────────────────────────

_MQTT = {"Service": "mqtt", "Name": "p4n4-mqtt", "State": "running", "Health": "healthy"}


@pytest.fixture
def status_env(tmp_path, monkeypatch):
    """An iot stack whose containers, limits and usage the test sets."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "OVERLAY_ROOT", tmp_path / "overlays")
    (tmp_path / "iot").mkdir()
    (tmp_path / "iot" / "docker-compose.yml").touch()
    monkeypatch.setattr(status, "detect_block_device", lambda: None)
    monkeypatch.setattr(status, "cgroup_v2", lambda: True)
    env = {"ps": [_MQTT], "applied": {}, "usage": {}}
    monkeypatch.setattr(status.dc, "ps", lambda cwd, overlay=None: env["ps"])
    monkeypatch.setattr(status, "applied_limits", lambda names: env["applied"])
    monkeypatch.setattr(status, "live_usage", lambda names: env["usage"])
    return env


def _status(*args):
    # Wide enough that rich doesn't wrap the cells the tests look for
    return runner.invoke(app, ["status", "--stack", "iot", *args], env={"COLUMNS": "200"})


def test_status_reads_the_profile_from_the_overlay(status_env, tmp_path):
    _write_overlay(tmp_path / "iot", profile="nuc")
    result = _status()
    assert result.exit_code == 0, result.output
    assert "Profile: nuc" in result.output
    assert "iot stack — nuc" in result.output


def test_status_shows_usage_against_applied_limits(status_env, tmp_path):
    overlay = _write_overlay(tmp_path / "iot")
    status_env["applied"] = {
        "p4n4-mqtt": usage.expected_limits(yaml.safe_load(overlay.read_text()))["mqtt"]
    }
    status_env["usage"] = {"p4n4-mqtt": usage.Usage(cpus=0.12, memory=40 * 1024**2)}
    result = _status()
    assert result.exit_code == 0, result.output
    assert "0.12 / 0.40" in result.output
    assert "40.0 MiB / 358 MiB" in result.output
    assert "applied" in result.output
    assert "stale" not in result.output


def test_status_flags_stale_limits(status_env, tmp_path):
    _write_overlay(tmp_path / "iot")
    status_env["applied"] = {"p4n4-mqtt": usage.Limits(cpus=1.0)}
    result = _status()
    assert "stale" in result.output
    assert "cpus 1.00 ≠ 0.40" in result.output
    assert "p4n4-emu up" in result.output


def test_status_flags_services_missing_from_the_overlay(status_env, tmp_path):
    _write_overlay(tmp_path / "iot", services=("influxdb",))
    result = _status()
    assert "not in overlay" in result.output


def test_status_without_overlay(status_env):
    result = _status()
    assert result.exit_code == 0, result.output
    assert "No stack is running under p4n4-emu" in result.output
    assert "not under p4n4-emu" in result.output
    assert "Profile:" not in result.output


def test_status_profile_option_warns_on_mismatch(status_env, tmp_path):
    _write_overlay(tmp_path / "iot", profile="nuc")
    result = _status("--profile", "rpi5")
    assert "Profile: rpi5" in result.output
    assert "runs under nuc, not rpi5" in result.output


def test_status_warns_when_cgroup_v2_is_missing(status_env, tmp_path, monkeypatch):
    _write_overlay(tmp_path / "iot")
    monkeypatch.setattr(status, "cgroup_v2", lambda: False)
    assert "doesn't enforce them" in _status().output


# ── logs ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def logs_calls(tmp_path, monkeypatch):
    """A multi-layer project (iot + ai), with `docker compose logs` recorded."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(paths, "OVERLAY_ROOT", tmp_path / "overlays")
    (tmp_path / ".p4n4.json").write_text('{"schema_version": 1, "layers": ["iot", "ai"]}')
    services = {"iot": ["mqtt", "grafana"], "ai": ["ollama"]}
    for stack in services:
        (tmp_path / stack).mkdir()
        (tmp_path / stack / "docker-compose.yml").touch()
    monkeypatch.setattr(logs.dc, "list_services", lambda cwd: services[cwd.name])
    calls = []
    monkeypatch.setattr(
        logs.dc, "logs", lambda cwd, **kw: calls.append((cwd.name, kw)) or 0
    )
    return calls


def test_logs_follows_the_stack_that_defines_the_service(logs_calls):
    result = runner.invoke(app, ["logs", "ollama"])
    assert result.exit_code == 0, result.output
    assert logs_calls == [
        ("ai", {"overlay": None, "service": "ollama", "tail": 100, "follow": True}),
    ]


def test_logs_refuses_to_follow_several_stacks(logs_calls):
    result = runner.invoke(app, ["logs"])
    assert result.exit_code == 1
    assert "--no-follow" in result.output
    assert logs_calls == []


def test_logs_no_follow_prints_every_stack(logs_calls):
    result = runner.invoke(app, ["logs", "--no-follow", "--tail", "5"])
    assert result.exit_code == 0, result.output
    assert [(s, kw["follow"], kw["tail"]) for s, kw in logs_calls] == [
        ("iot", False, 5), ("ai", False, 5),
    ]


def test_logs_passes_the_overlay_up_wrote(logs_calls, tmp_path):
    overlay = paths.overlay_path(tmp_path / "iot", "iot")
    overlay.parent.mkdir(parents=True)
    overlay.touch()
    result = runner.invoke(app, ["logs", "--stack", "iot"])
    assert result.exit_code == 0, result.output
    assert logs_calls[0][1]["overlay"] == overlay


def test_logs_unknown_service(logs_calls):
    result = runner.invoke(app, ["logs", "nope"])
    assert result.exit_code == 1
    assert "nope" in result.output


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


# ── shared networks and the broker come from the compose config ───────────────

def test_up_creates_the_networks_the_config_names(stack_dir, preflight_calls, monkeypatch):
    config = {
        "name": "plant",
        "networks": {"bus": {"name": "plant-bus", "ipam": {"config": [{"subnet": "10.9.0.0/16"}]}}},
        "services": {"mqtt": {"container_name": "plant-mqtt", "networks": {"bus": None}}},
    }
    networks, sims = [], []
    monkeypatch.setattr(up, "load_config", lambda cwd: config)
    monkeypatch.setattr(up.dc, "ensure_network", lambda *a, **kw: networks.append((a, kw)))
    monkeypatch.setattr(up, "start_simulator", lambda **kw: sims.append(kw) or 0)
    result = runner.invoke(app, ["up", "--native", "--stack-dir", str(stack_dir), "--sim"])
    assert result.exit_code == 0, result.output
    assert networks == [(("plant-bus", "10.9.0.0/16", "bus"), {"owned": True})]
    assert sims[0]["broker"] == stack_config.Broker("plant-mqtt", "plant-mqtt", "plant-bus")


def test_sim_start_options_override_the_project_broker(monkeypatch):
    calls = []
    monkeypatch.setattr(sim, "project_broker", lambda: stack_config.Broker("a", "a", "net-a"))
    monkeypatch.setattr(sim, "start_simulator", lambda **kw: calls.append(kw) or 0)
    result = runner.invoke(app, ["sim", "start", "--network", "net-b"])
    assert result.exit_code == 0, result.output
    assert calls[0]["broker"] == stack_config.Broker("a", "a", "net-b")


# ── up: rollback when a later stack fails ─────────────────────────────────────

@pytest.fixture
def multi_project(tmp_path, monkeypatch):
    """A multi-layer project (iot, ai, edge) whose ai stack fails to start."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    (tmp_path / ".p4n4.json").write_text('{"project": "p", "layers": ["iot", "ai", "edge"]}')
    for name in ("iot", "ai", "edge"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "docker-compose.yml").write_text("services:\n  svc: {}\n")
    monkeypatch.setattr(paths, "OVERLAY_ROOT", tmp_path / "overlays")
    monkeypatch.setattr(up, "detect_block_device", lambda: None)
    monkeypatch.setattr(up, "check_or_exit", lambda **kw: None)
    monkeypatch.setattr(up, "load_config", lambda cwd: None)
    monkeypatch.setattr(up.dc, "list_services", lambda cwd: ["svc"])
    monkeypatch.setattr(up.dc, "ensure_network", lambda *a, **kw: None)
    events = []
    monkeypatch.setattr(
        up.dc, "up", lambda cwd, overlay=None, **kw: events.append(("up", cwd.name)) or (
            1 if cwd.name == "ai" else 0
        )
    )
    monkeypatch.setattr(
        up.dc, "down", lambda cwd, overlay=None: events.append(("down", cwd.name)) or 0
    )
    return tmp_path, events


def test_up_rolls_back_stacks_it_started(multi_project, monkeypatch):
    root, events = multi_project
    monkeypatch.setattr(up.dc, "ps", lambda cwd, overlay=None: [])
    result = runner.invoke(app, ["up", "--native", "--stack", "all"])
    assert result.exit_code == 1
    # ai failed half-way: it and iot go down, dependents first; edge never starts
    assert events == [("up", "iot"), ("up", "ai"), ("down", "ai"), ("down", "iot")]
    assert paths.existing_overlay(root / "iot", "iot") is None


def test_up_rollback_keeps_stacks_that_were_running(multi_project, monkeypatch):
    root, events = multi_project
    monkeypatch.setattr(
        up.dc, "ps",
        lambda cwd, overlay=None: [{"State": "running"}] if cwd.name == "iot" else [],
    )
    result = runner.invoke(app, ["up", "--native", "--stack", "all"])
    assert result.exit_code == 1
    assert events == [("up", "iot"), ("up", "ai"), ("down", "ai")]
    assert paths.existing_overlay(root / "iot", "iot") is not None


def test_up_missing_stack_starts_nothing(multi_project, monkeypatch):
    root, events = multi_project
    (root / "edge" / "docker-compose.yml").unlink()
    monkeypatch.setattr(up.dc, "ps", lambda cwd, overlay=None: [])
    result = runner.invoke(app, ["up", "--native", "--stack", "iot,edge"])
    assert result.exit_code == 1
    assert events == []


def test_setup_skips_install_when_qemu_is_available(monkeypatch):
    # e.g. Docker Desktop, whose VM registers QEMU itself
    monkeypatch.setattr(preflight._platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(setup, "run_preflight", lambda **kw: [])
    installs = []
    monkeypatch.setattr(setup, "_install_binfmt", lambda p: installs.append(p))
    result = runner.invoke(app, ["setup", "--arch", "arm64"])
    assert result.exit_code == 0, result.output
    assert installs == []


# ── --json ────────────────────────────────────────────────────────────────────

def test_status_json(status_env, tmp_path):
    import json

    overlay = _write_overlay(tmp_path / "iot")
    status_env["ps"] = [
        {**_MQTT, "Publishers": [
            {"PublishedPort": 1883, "TargetPort": 1883, "Protocol": "tcp"},
            {"PublishedPort": 1883, "TargetPort": 1883, "Protocol": "tcp"},
        ]},
    ]
    status_env["applied"] = {"p4n4-mqtt": usage.Limits(cpus=1.0)}
    status_env["usage"] = {"p4n4-mqtt": usage.Usage(cpus=0.12, memory=1024)}
    result = runner.invoke(app, ["status", "--stack", "iot", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert list(doc["profiles"]) == ["rpi5"]
    assert doc["profiles"]["rpi5"]["cpus"] == 4.0
    assert doc["stale"] is True and doc["cgroup_v2"] is True
    (stack,) = doc["stacks"]
    assert stack["stack"] == "iot" and stack["profile"] == "rpi5"
    (svc,) = stack["services"]
    want = usage.expected_limits(yaml.safe_load(overlay.read_text()))["mqtt"]
    assert svc["expected"]["cpus"] == want.cpus
    assert svc["limits"]["cpus"] == 1.0
    assert svc["limits_state"] == "stale"
    assert svc["differences"][0] == f"cpus 1.00 ≠ {want.cpus:.2f}"
    assert svc["usage"] == {"cpus": 0.12, "memory": 1024}
    assert svc["ports"] == [{"published": 1883, "target": 1883, "protocol": "tcp"}]


def test_status_json_without_overlay(status_env):
    import json

    doc = json.loads(runner.invoke(app, ["status", "--stack", "iot", "--json"]).output)
    assert doc["profiles"] == {}
    assert doc["stacks"][0]["profile"] is None
    assert doc["stacks"][0]["services"][0]["limits_state"] == "none"


def test_status_unknown_profile_fails(status_env):
    assert _status("--profile", "nope").exit_code == 1


def test_profile_json():
    import json

    shown = json.loads(runner.invoke(app, ["profile", "show", "rpi5", "--json"]).output)
    assert shown["name"] == "rpi5" and shown["is_arm"] is True
    assert shown["memory_bytes"] == 7168 * 1024**2
    listed = json.loads(runner.invoke(app, ["profile", "list", "--json"]).output)
    assert "rpi5" in {p["name"] for p in listed}


def test_profile_show_unknown_fails():
    result = runner.invoke(app, ["profile", "show", "nope"])
    assert result.exit_code == 1


# ── profile switch ────────────────────────────────────────────────────────────

@pytest.fixture
def switch_env(status_env, tmp_path, monkeypatch):
    """The status_env iot stack, with `docker update` calls recorded."""
    from p4n4_emu.commands import profile

    monkeypatch.setattr(profile, "detect_block_device", lambda: None)
    monkeypatch.setattr(profile.dc, "ps", lambda cwd, overlay=None: status_env["ps"])
    monkeypatch.setattr(up.dc, "list_services", lambda cwd: ["mqtt"])
    updates = []

    def run(cmd, **kw):
        updates.append(cmd)
        return subprocess.CompletedProcess(cmd, status_env.get("update_rc", 0), stderr="nope")

    monkeypatch.setattr(profile.subprocess, "run", run)
    return updates


def test_profile_switch_updates_limits_in_place(switch_env, tmp_path):
    _write_overlay(tmp_path / "iot", profile="nuc")
    paths_doc = paths.read_overlay(paths.existing_overlay(tmp_path / "iot", "iot"))
    assert paths_doc["x-p4n4-emu"]["profile"] == "nuc"
    result = runner.invoke(app, ["profile", "switch", "mcu-class", "--stack", "iot"])
    assert result.exit_code == 0, result.output
    (cmd,) = switch_env
    assert cmd[:2] == ["docker", "update"] and cmd[-1] == "p4n4-mqtt"
    assert "--cpus" in cmd and "--memory" in cmd and "--memory-swap" in cmd
    doc = paths.read_overlay(paths.existing_overlay(tmp_path / "iot", "iot"))
    assert doc["x-p4n4-emu"]["profile"] == "mcu-class"
    want = usage.expected_limits(doc)["mqtt"]
    assert cmd[cmd.index("--memory") + 1] == str(want.memory)


def test_profile_switch_refuses_an_architecture_change(switch_env, tmp_path):
    _write_overlay(tmp_path / "iot", profile="rpi5")  # rendered with linux/arm64
    result = runner.invoke(app, ["profile", "switch", "nuc", "--stack", "iot"])
    assert result.exit_code == 1
    assert switch_env == []


def test_profile_switch_without_running_stacks(switch_env):
    result = runner.invoke(app, ["profile", "switch", "rpi5", "--stack", "iot"])
    assert result.exit_code == 1
    assert switch_env == []


def test_profile_switch_reports_failed_updates(switch_env, status_env, tmp_path):
    status_env["update_rc"] = 1
    _write_overlay(tmp_path / "iot", profile="nuc")
    result = runner.invoke(app, ["profile", "switch", "mcu-class", "--stack", "iot"])
    assert result.exit_code == 1


def test_up_passes_build_and_pull(stack_dir, preflight_calls, monkeypatch):
    calls = []
    monkeypatch.setattr(up.dc, "up", lambda cwd, overlay=None, **kw: calls.append(kw) or 0)
    result = runner.invoke(
        app, ["up", "--native", "--stack-dir", str(stack_dir), "--build", "--pull"]
    )
    assert result.exit_code == 0, result.output
    assert calls == [{"build": True, "pull": True}]


def test_down_yes_skips_the_volume_prompt(down_calls, tmp_path, monkeypatch):
    used = []
    monkeypatch.setattr(
        down.dc, "down", lambda cwd, overlay=None, volumes=False: used.append(volumes) or 0
    )
    result = runner.invoke(app, ["down", "--stack", "iot", "--volumes", "--yes"])
    assert result.exit_code == 0, result.output
    assert used == [True]
