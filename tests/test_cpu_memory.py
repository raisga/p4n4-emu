"""CPU and memory fidelity: core speed (calibrate), pinning, pids, OOM and share overrides."""

import json
import subprocess

import pytest
import yaml
from typer.testing import CliRunner

from p4n4_emu.cli import app
from p4n4_emu.commands import up
from p4n4_emu.overlays import paths
from p4n4_emu.overlays.generator import (
    FALLBACK_SHARE,
    Share,
    render_overlay,
    resolve_cpuset,
    service_shares,
)
from p4n4_emu.profiles import loader, shares
from p4n4_emu.profiles.loader import ProfileError, load_profile, parse_profile
from p4n4_emu.utils import calibration, usage

runner = CliRunner()

BASE = {
    "description": "A board",
    "cpus": 4,
    "memory": "1g",
    "blkio_read_bps": "20m",
    "blkio_write_bps": "20m",
}

# `7z b -mmt1` on the dev host (Debian bookworm's p7zip-full), trimmed
SEVEN_ZIP = """
7-Zip [64] 26.02 : Copyright (c) 1999-2026 Igor Pavlov : 2026-06-25
p7zip Version 16.02 (locale=C.UTF-8,Utf16=on,HugeFiles=on,64 bits,8 CPUs)

RAM size:   15712 MB,  # CPU hardware threads:   8
RAM usage:    437 MB,  # Benchmark threads:      1

                       Compressing  |                  Decompressing
Dict     Speed Usage    R/U Rating  |      Speed Usage    R/U Rating
         KiB/s     %   MIPS   MIPS  |      KiB/s     %   MIPS   MIPS

22:       4321   100   4204   4204  |      48190   100   4114   4114
----------------------------------  | ------------------------------
Avr:             100   4086   4086  |              100   4022   4022
Tot:             100   4061   4054
"""


def _services(rendered: str) -> dict:
    return yaml.safe_load(rendered)["services"]


def _cpus(svc: dict) -> float:
    return float(svc["deploy"]["resources"]["limits"]["cpus"])


# ── profile keys ──────────────────────────────────────────────────────────────

def test_profile_reads_the_new_keys():
    p = parse_profile(
        {
            **BASE, "cpu_score": 1769, "pids_limit": 2000, "oom_score_adj": 500,
            "shares": {"influxdb": {"cpu": 0.5}},
        },
        "board",
    )
    assert (p.cpu_score, p.pids_limit, p.oom_score_adj) == (1769, 2000, 500)
    assert p.shares == {"influxdb": {"cpu": 0.5}}
    assert p.device_pids == 2000


@pytest.mark.parametrize(
    ("memory", "pids"),
    [("384m", 3072), ("768m", 6144), ("3584m", 28672), ("7168m", 32768)],
)
def test_device_pids_follow_memory_like_threads_max(memory, pids):
    assert parse_profile({**BASE, "memory": memory}, "board").device_pids == pids


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"cpu_score": 0}, "cpu_score: must be positive"),
        ({"cpu_score": "fast"}, "cpu_score: expected a number"),
        ({"pids_limit": 10}, "pids_limit: expected a whole number, at least 64"),
        ({"pids_limit": 1.5e3}, "pids_limit"),
        ({"oom_score_adj": 1001}, "oom_score_adj: expected a whole number from -1000"),
        ({"oom_score_adj": True}, "oom_score_adj"),
        ({"shares": ["influxdb"]}, "shares: expected a mapping"),
        ({"shares": {"influxdb": {"cpu": 0}}}, "shares.influxdb.cpu: expected a fraction"),
        ({"shares": {"influxdb": {"cpu": 1.5}}}, "shares.influxdb.cpu"),
        ({"shares": {"influxdb": {"disk": 0.1}}}, "shares.influxdb: unknown key(s) disk"),
        ({"shares": {"influxdb": {}}}, "shares.influxdb: expected cpu and/or memory"),
    ],
)
def test_profile_rejects_bad_new_keys(extra, message):
    with pytest.raises(ProfileError, match=message.replace("(", r"\(").replace(")", r"\)")):
        parse_profile({**BASE, **extra}, "board")


def test_built_in_arm_profiles_have_a_cpu_score():
    for name in ("rpi3", "rpi4", "rpi5", "rpi-zero2w", "jetson-orin-nano"):
        assert load_profile(name).cpu_score > 0, name
    # Faster boards score higher
    scores = [load_profile(n).cpu_score for n in ("rpi-zero2w", "rpi3", "rpi4", "rpi5")]
    assert scores == sorted(scores)


# ── share overrides ───────────────────────────────────────────────────────────

def test_overrides_change_one_field_and_keep_the_other():
    got = service_shares("iot", ["influxdb", "mqtt"], {"influxdb": {"cpu": 0.6}})
    assert got["influxdb"] == Share(0.6, 0.35)
    assert got["mqtt"] == Share(0.10, 0.05)


def test_fallback_override_applies_to_unknown_services_only():
    got = service_shares("iot", ["mqtt", "telegraf"], {"*": {"memory": 0.02}})
    assert got["telegraf"] == Share(FALLBACK_SHARE.cpu, 0.02)
    assert got["mqtt"] == Share(0.10, 0.05)


def test_merge_later_sources_win_per_field():
    merged = shares.merge(
        {"influxdb": {"cpu": 0.5, "memory": 0.4}}, {"influxdb": {"memory": 0.2}}
    )
    assert merged == {"influxdb": {"cpu": 0.5, "memory": 0.2}}


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / ".p4n4.json").write_text("{}")
    (tmp_path / ".p4n4-emu").mkdir()
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_project_config_shares(project):
    (project / ".p4n4-emu" / "config.yml").write_text(
        "shares:\n  node-red: {memory: 0.25}\n"
    )
    assert shares.project_shares() == {"node-red": {"memory": 0.25}}


def test_project_config_errors_name_the_file_and_key(project):
    path = project / ".p4n4-emu" / "config.yml"
    path.write_text("shares:\n  node-red: {memory: 2}\n")
    with pytest.raises(shares.SharesError, match=r"config\.yml: shares\.node-red\.memory"):
        shares.project_shares()
    path.write_text("limits: {}\n")
    with pytest.raises(shares.SharesError, match="unknown key"):
        shares.project_shares()


def test_no_project_config_means_no_overrides(project, tmp_path):
    assert shares.project_shares() == {}


# ── rendering ─────────────────────────────────────────────────────────────────

def test_cpu_factor_scales_cpus_not_memory():
    rpi4 = load_profile("rpi4")
    plain = _services(render_overlay(rpi4, "iot", None, services=["influxdb"]))
    slow = _services(render_overlay(rpi4, "iot", None, services=["influxdb"], cpu_factor=0.5))
    assert _cpus(slow["influxdb"]) == pytest.approx(_cpus(plain["influxdb"]) / 2)
    limits = lambda s: s["influxdb"]["deploy"]["resources"]["limits"]  # noqa: E731
    assert limits(slow)["memory"] == limits(plain)["memory"]


def test_overlay_records_factor_and_cpuset():
    doc = yaml.safe_load(
        render_overlay(
            load_profile("rpi4"), "iot", None, services=["mqtt"],
            cpu_factor=0.44, cpuset="0-3", cpuset_auto=True,
        )
    )
    meta = doc["x-p4n4-emu"]
    assert (meta["cpu_factor"], meta["cpuset"], meta["cpuset_auto"]) == (0.44, "0-3", True)
    assert doc["services"]["mqtt"]["cpuset"] == "0-3"


def test_overlay_without_factor_or_cpuset_records_neither():
    doc = yaml.safe_load(render_overlay(load_profile("rpi4"), "iot", None, services=["mqtt"]))
    assert not {"cpu_factor", "cpuset", "cpuset_auto"} & set(doc["x-p4n4-emu"])
    assert "cpuset" not in doc["services"]["mqtt"]


def test_pids_follow_the_memory_share_with_a_floor():
    zero = load_profile("rpi-zero2w")  # 384m: 3072 pids for the device
    svc = _services(render_overlay(zero, "iot", None, services=["influxdb", "mqtt"]))
    pids = {n: s["deploy"]["resources"]["limits"]["pids"] for n, s in svc.items()}
    assert pids["influxdb"] == int(3072 * 0.35)
    assert pids["mqtt"] == 153
    tiny = parse_profile({**BASE, "memory": "16m"}, "tiny")
    svc = _services(render_overlay(tiny, "iot", None, services=["mqtt"]))
    assert svc["mqtt"]["deploy"]["resources"]["limits"]["pids"] == 64


def test_oom_score_adj_only_when_the_profile_sets_it():
    svc = _services(render_overlay(load_profile("rpi4"), "iot", None, services=["mqtt"]))
    assert "oom_score_adj" not in svc["mqtt"]
    prof = parse_profile({**BASE, "oom_score_adj": 500}, "board")
    svc = _services(render_overlay(prof, "iot", None, services=["mqtt"]))
    assert svc["mqtt"]["oom_score_adj"] == 500


def test_overrides_reach_the_overlay():
    prof = load_profile("rpi4")
    svc = _services(
        render_overlay(prof, "iot", None, services=["influxdb"], overrides={"influxdb": {"cpu": 1}})
    )
    assert _cpus(svc["influxdb"]) == 4.0


@pytest.mark.parametrize(
    ("spec", "host", "want"),
    [("auto", 8, "0-3"), ("auto", None, "0-3"), ("0-3", 8, "0-3"), ("0, 2,4", 8, "0,2,4")],
)
def test_resolve_cpuset(spec, host, want):
    assert resolve_cpuset(spec, load_profile("rpi4"), host) == want


def test_resolve_cpuset_one_core():
    assert resolve_cpuset("auto", load_profile("mcu-class"), 8) == "0"


@pytest.mark.parametrize(
    ("spec", "host", "message"),
    [
        ("auto", 2, "can't be pinned"),
        ("0-9", 8, "the host has cores 0-7"),
        ("3-1", 8, "runs backwards"),
        ("a-b", 8, "expected auto"),
        ("", 8, "expected auto"),
    ],
)
def test_resolve_cpuset_rejects(spec, host, message):
    with pytest.raises(ValueError, match=message):
        resolve_cpuset(spec, load_profile("rpi4"), host)


# ── calibration ───────────────────────────────────────────────────────────────

def test_parse_7zip_reads_the_total_rating():
    m = calibration.parse_7zip(SEVEN_ZIP)
    assert (m.score, m.version) == (4054, "26.02")


def test_parse_7zip_without_a_total_fails():
    with pytest.raises(calibration.CalibrationError):
        calibration.parse_7zip("7-Zip [64] 16.02\nno benchmark here\n")


def test_bench_command_sets_the_platform_only_when_asked():
    assert "--platform" not in calibration.bench_command(None)
    cmd = calibration.bench_command("linux/arm64")
    assert cmd[cmd.index("--platform") + 1] == "linux/arm64"
    assert cmd[-1].endswith("7z b -mmt1")


def test_save_and_load_by_platform(_no_calibration):
    calibration.save("linux/amd64", 4054, "26.02", "measured")
    calibration.save("linux/arm64", 410, "26.02", "measured")
    stored = calibration.load()
    assert stored["linux/amd64"]["score"] == 4054
    assert calibration.host_score("linux/arm64") == 410
    assert calibration.host_score("linux/arm/v7") is None


def test_load_ignores_a_corrupt_file(_no_calibration):
    _no_calibration.write_text("{not json")
    assert calibration.load() == {}
    _no_calibration.write_text(json.dumps({"scores": {"linux/amd64": {"score": -1}}}))
    assert calibration.load() == {}


def test_cpu_factor_is_board_over_host():
    calibration.save("linux/amd64", 4054, "26.02", "measured")
    f = calibration.cpu_factor(load_profile("rpi4"), "linux/amd64")
    assert f.value == pytest.approx(1769 / 4054)
    assert "rpi4" in f.note


def test_cpu_factor_never_speeds_up():
    calibration.save("linux/arm64", 410, "26.02", "measured")  # QEMU is slow
    f = calibration.cpu_factor(load_profile("rpi5"), "linux/arm64")
    assert f.value == 1.0 and "slower" in f.note


def test_cpu_factor_without_calibration_or_score():
    f = calibration.cpu_factor(load_profile("rpi4"), "linux/arm64")
    assert f.value == 1.0 and "calibrate --platform linux/arm64" in f.note
    assert calibration.cpu_factor(load_profile("nuc"), "linux/amd64").value == 1.0


def test_measure_reports_a_failed_container(monkeypatch):
    monkeypatch.setattr(
        calibration.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="", stderr="no network\n"),
    )
    with pytest.raises(calibration.CalibrationError, match="no network"):
        calibration.measure(None)


# ── calibrate command ─────────────────────────────────────────────────────────

@pytest.fixture
def calibrate_env(monkeypatch):
    from p4n4_emu.commands import calibrate

    runs = []
    monkeypatch.setattr(calibrate, "check_or_exit", lambda **kw: runs.append(("check", kw)))
    monkeypatch.setattr(
        calibrate.calibration, "measure",
        lambda platform: runs.append(("measure", platform)) or calibration.parse_7zip(SEVEN_ZIP),
    )
    monkeypatch.setattr(calibrate, "host_platform", lambda: "linux/amd64")
    return runs


def test_calibrate_measures_natively_and_stores(calibrate_env):
    result = runner.invoke(app, ["calibrate", "--json"])
    assert result.exit_code == 0, result.output
    assert ("measure", None) in calibrate_env
    out = json.loads(result.output)
    assert out["scores"]["linux/amd64"]["score"] == 4054
    assert out["factors"]["rpi4"]["factors"]["linux/amd64"] == round(1769 / 4054, 3)


def test_calibrate_arm64_runs_under_qemu(calibrate_env):
    result = runner.invoke(app, ["calibrate", "--platform", "arm64"])
    assert result.exit_code == 0, result.output
    assert ("check", {"require_qemu": True, "platform": "linux/arm64"}) in calibrate_env
    assert ("measure", "linux/arm64") in calibrate_env
    assert calibration.host_score("linux/arm64") == 4054


def test_calibrate_score_records_without_measuring(calibrate_env):
    result = runner.invoke(app, ["calibrate", "--score", "3000"])
    assert result.exit_code == 0, result.output
    assert calibrate_env == []
    assert calibration.load()["linux/amd64"]["source"] == "manual"


def test_calibrate_show_before_any_run(calibrate_env):
    result = runner.invoke(app, ["calibrate", "--show"])
    assert result.exit_code == 0
    assert "Not calibrated" in result.output
    assert calibrate_env == []


# ── up and profile switch ─────────────────────────────────────────────────────

@pytest.fixture
def stack_dir(tmp_path, monkeypatch, _no_calibration):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    (tmp_path / "docker-compose.yml").write_text("services:\n  influxdb: {}\n")
    monkeypatch.setattr(paths, "OVERLAY_ROOT", tmp_path / "overlays")
    monkeypatch.setattr(up, "detect_block_device", lambda: None)
    monkeypatch.setattr(up, "check_or_exit", lambda **kw: None)
    monkeypatch.setattr(up, "host_cpus", lambda: 8)
    monkeypatch.setattr(up, "host_platform", lambda: "linux/amd64")
    monkeypatch.setattr(up.dc, "list_services", lambda cwd: ["influxdb"])
    monkeypatch.setattr(up.dc, "up", lambda cwd, overlay=None, **kw: 0)
    monkeypatch.setattr(up.dc, "ps", lambda cwd, overlay=None: [])
    monkeypatch.setattr(up.dc, "ensure_network", lambda *a, **kw: None)
    monkeypatch.setattr(up, "load_config", lambda cwd: None)
    return tmp_path


def _overlay(stack_dir):
    return yaml.safe_load(paths.overlay_path(stack_dir, "iot").read_text())


def _up(stack_dir, *args):
    return runner.invoke(
        app, ["up", "-p", "rpi4", "--native", "--stack-dir", str(stack_dir), *args]
    )


def test_up_uses_the_calibrated_factor(stack_dir):
    calibration.save("linux/amd64", 3538, "26.02", "measured")  # twice a Pi 4 core
    result = _up(stack_dir)
    assert result.exit_code == 0, result.output
    doc = _overlay(stack_dir)
    assert doc["x-p4n4-emu"]["cpu_factor"] == 0.5
    assert _cpus(doc["services"]["influxdb"]) == pytest.approx(4 * 0.5 * 0.40)


def test_up_uncalibrated_keeps_core_counts_and_says_so(stack_dir):
    result = _up(stack_dir)
    assert result.exit_code == 0, result.output
    assert "calibrate" in result.output
    assert _cpus(_overlay(stack_dir)["services"]["influxdb"]) == pytest.approx(4 * 0.40)


def test_up_cpu_factor_option_wins(stack_dir):
    calibration.save("linux/amd64", 3538, "26.02", "measured")
    result = _up(stack_dir, "--cpu-factor", "1")
    assert result.exit_code == 0, result.output
    assert _cpus(_overlay(stack_dir)["services"]["influxdb"]) == pytest.approx(1.6)


def test_up_cpu_factor_range(stack_dir):
    assert _up(stack_dir, "--cpu-factor", "2").exit_code != 0


def test_up_cpuset_auto_and_bad_list(stack_dir):
    result = _up(stack_dir, "--cpuset", "auto")
    assert result.exit_code == 0, result.output
    assert _overlay(stack_dir)["services"]["influxdb"]["cpuset"] == "0-3"
    result = _up(stack_dir, "--cpuset", "0-15")
    assert result.exit_code == 1
    assert "the host has cores 0-7" in result.output


def test_up_applies_project_shares(stack_dir):
    (stack_dir / ".p4n4.json").write_text("{}")
    (stack_dir / ".p4n4-emu").mkdir()
    (stack_dir / ".p4n4-emu" / "config.yml").write_text("shares:\n  influxdb: {cpu: 0.75}\n")
    result = _up(stack_dir, "--stack", "iot")
    assert result.exit_code == 0, result.output
    assert _cpus(_overlay(stack_dir)["services"]["influxdb"]) == pytest.approx(3.0)


def test_up_bad_project_shares_start_nothing(stack_dir, monkeypatch):
    started = []
    monkeypatch.setattr(up.dc, "up", lambda cwd, overlay=None, **kw: started.append(cwd) or 0)
    (stack_dir / ".p4n4.json").write_text("{}")
    (stack_dir / ".p4n4-emu").mkdir()
    (stack_dir / ".p4n4-emu" / "config.yml").write_text("shares:\n  influxdb: {cpu: 3}\n")
    result = _up(stack_dir, "--stack", "iot")
    assert result.exit_code == 1
    assert "shares.influxdb.cpu" in result.output
    assert started == []


def test_update_command_sets_pids_and_cores():
    lim = usage.Limits(cpus=0.7, memory=1024, memswap=1024, pids=500, cpuset="0-3")
    from p4n4_emu.commands.profile import update_command

    cmd = update_command("c", lim)
    assert cmd[cmd.index("--pids-limit") + 1] == "500"
    assert cmd[cmd.index("--cpuset-cpus") + 1] == "0-3"


def test_profile_switch_follows_auto_cores_and_the_factor(stack_dir, monkeypatch):
    from p4n4_emu.commands import profile

    calibration.save("linux/amd64", 3538, "26.02", "measured")
    assert _up(stack_dir, "--cpuset", "auto").exit_code == 0
    monkeypatch.setattr(profile, "detect_block_device", lambda: None)
    monkeypatch.setattr(profile, "host_cpus", lambda: 8)
    monkeypatch.setattr(
        profile.dc, "ps",
        lambda cwd, overlay=None: [{"Service": "influxdb", "Name": "p4n4-influxdb"}],
    )
    updates = []
    monkeypatch.setattr(
        profile.subprocess, "run",
        lambda cmd, **kw: updates.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""),
    )
    result = runner.invoke(
        app, ["profile", "switch", "mcu-class", "--stack", "iot", "--stack-dir", str(stack_dir)]
    )
    assert result.exit_code == 0, result.output
    (cmd,) = updates
    assert cmd[cmd.index("--cpuset-cpus") + 1] == "0"  # mcu-class has one core
    doc = _overlay(stack_dir)
    # mcu-class has no cpu_score: core counts again
    assert "cpu_factor" not in doc["x-p4n4-emu"]
    assert doc["x-p4n4-emu"]["cpuset_auto"] is True


def test_profile_show_json_has_the_new_keys():
    out = json.loads(runner.invoke(app, ["profile", "show", "rpi4", "--json"]).output)
    assert out["cpu_score"] == 1769
    assert out["device_pids"] == 28672
    assert out["shares"] == {}


def test_loader_still_skips_user_profiles(tmp_path):
    # conftest keeps ~/.p4n4-emu/profiles out; make sure the new keys load from there too
    loader.USER_DIR.mkdir(parents=True, exist_ok=True)
    (loader.USER_DIR / "mine.yml").write_text(
        yaml.safe_dump({**BASE, "description": "Mine", "cpu_score": 1000})
    )
    assert load_profile("mine").cpu_score == 1000
