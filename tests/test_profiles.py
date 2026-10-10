"""Tests for hardware profile loading and validation."""

import pytest

from p4n4_emu.hw import board
from p4n4_emu.profiles.loader import (
    ProfileError,
    list_profiles,
    load_profile,
    load_profile_file,
    parse_profile,
)

BUILTINS = ["jetson-orin-nano", "mcu-class", "nuc", "rpi-zero2w", "rpi3", "rpi4", "rpi5"]


def test_all_profiles_load():
    names = list_profiles()
    assert names == BUILTINS
    for name in names:
        p = load_profile(name)
        assert p.name == name
        assert p.builtin


@pytest.mark.parametrize("name", BUILTINS)
def test_required_fields(name):
    p = load_profile(name)
    assert p.description
    assert p.cpus > 0
    assert p.memory
    assert p.memory_swap
    assert p.blkio_weight > 0
    assert p.blkio_read_bps > 0
    assert p.blkio_write_bps > 0
    assert p.arch


def test_memory_bytes_rpi5():
    p = load_profile("rpi5")
    assert p.memory == "7168m"
    assert p.memory_bytes == 7168 * 1024 * 1024
    assert p.memory_mb == 7168


def test_memory_bytes_mcu():
    p = load_profile("mcu-class")
    assert p.memory_bytes == 256 * 1024 * 1024
    assert p.memory_mb == 256


def test_arm_profiles():
    assert load_profile("rpi4").is_arm is True
    assert load_profile("rpi5").is_arm is True
    assert load_profile("mcu-class").is_arm is False
    assert load_profile("nuc").is_arm is False


def test_unknown_profile_raises():
    with pytest.raises(ValueError, match="not found"):
        load_profile("does-not-exist")


def test_list_profiles_sorted():
    names = list_profiles()
    assert names == sorted(names)


def test_boards_of_the_pi_profiles():
    for name in ("rpi3", "rpi4", "rpi5", "rpi-zero2w"):
        assert load_profile(name).board == name
    assert load_profile("jetson-orin-nano").board is None
    assert load_profile("jetson-orin-nano").gpu == "nvidia"


@pytest.mark.parametrize(
    ("name", "info"),
    [
        ("rpi3", {"TYPE": "Pi 3 Model B", "PROCESSOR": "BCM2837", "RAM": "1G",
                  "REVISION": "a02082"}),
        ("rpi-zero2w", {"TYPE": "Zero 2 W", "PROCESSOR": "BCM2837", "RAM": "512M",
                        "REVISION": "902120"}),
    ],
)
def test_new_boards_report_what_the_real_ones_do(name, info):
    rpi_info = board.BOARDS[name].rpi_info()
    assert {k: rpi_info[k] for k in info} == info


# ── the schema ────────────────────────────────────────────────────────────────

MINIMAL = {
    "description": "A board",
    "cpus": 2,
    "memory": "1g",
    "blkio_read_bps": "20m",
    "blkio_write_bps": 10485760,
}


def test_minimal_profile_gets_defaults():
    p = parse_profile(MINIMAL, "mine")
    assert p.name == "mine"
    assert p.arch == "x86_64" and not p.is_arm
    assert p.memory_swap == "1g"  # no swap
    assert p.blkio_weight == 500
    assert p.blkio_read_bps == 20 * 1024**2
    assert p.board is None and p.gpu is None


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        ({"cpu": 2}, "unknown key(s) cpu"),
        ({"cpus": None}, "cpus: expected a number"),
        ({"cpus": 0}, "cpus: must be positive"),
        ({"memory": "lots"}, "memory: expected a size"),
        ({"memory": "4m"}, "memory: Docker needs at least 6m"),
        ({"memory_swap": "512m"}, "memory_swap: is memory plus swap"),
        ({"blkio_weight": 5}, "blkio_weight: expected a whole number from 10 to 1000"),
        ({"blkio_read_bps": 0}, "blkio_read_bps: must be positive"),
        ({"arch": "sparc"}, "arch: expected one of"),
        ({"board": "rpi2"}, "board: expected one of"),
        ({"gpu": "amd"}, "gpu: expected one of nvidia"),
        ({"name": "other"}, "doesn't match the file name"),
        ({"description": ""}, "description: expected a line of text"),
    ],
)
def test_schema_errors_name_the_key(changes, error):
    doc = {**MINIMAL, **changes}
    if changes.get("cpus", 1) is None:
        del doc["cpus"]
        error = "missing key(s) cpus"
    with pytest.raises(ProfileError) as e:
        parse_profile(doc, "mine")
    assert error in str(e.value)


def test_profile_file_errors_name_the_file(tmp_path):
    path = tmp_path / "bad.yml"
    path.write_text("description: [unclosed\n")
    with pytest.raises(ProfileError, match=r"bad.yml is not valid YAML"):
        load_profile_file(path)
    path.write_text("- a list\n")
    with pytest.raises(ProfileError, match=r"bad.yml: expected a mapping"):
        load_profile_file(path)


def test_user_profiles_and_project_profiles_win(tmp_path, monkeypatch):
    from p4n4_emu.profiles import loader

    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setattr(loader, "USER_DIR", user)
    (user / "rpi4.yml").write_text("description: tuned rpi4\ncpus: 2\nmemory: 2g\n"
                                   "blkio_read_bps: 1m\nblkio_write_bps: 1m\n")
    assert load_profile("rpi4").description == "tuned rpi4"
    assert not load_profile("rpi4").builtin

    project = tmp_path / "proj"
    (project / ".p4n4-emu" / "profiles").mkdir(parents=True)
    (project / ".p4n4.json").write_text("{}")
    (project / ".p4n4-emu" / "profiles" / "rpi4.yml").write_text(
        "description: the project's rpi4\ncpus: 1\nmemory: 1g\n"
        "blkio_read_bps: 1m\nblkio_write_bps: 1m\n"
    )
    monkeypatch.chdir(project)
    assert load_profile("rpi4").description == "the project's rpi4"
