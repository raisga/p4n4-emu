"""Tests for hardware profile loading and validation."""

import pytest

from p4n4_emu.profiles.loader import list_profiles, load_profile


def test_all_profiles_load():
    names = list_profiles()
    assert len(names) == 4
    for name in names:
        p = load_profile(name)
        assert p.name == name


@pytest.mark.parametrize("name", ["rpi4", "rpi5", "mcu-class", "nuc"])
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
