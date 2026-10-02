"""Tests for block device detection."""

from p4n4_emu.utils import docker_info


def test_is_under_compares_path_components():
    assert docker_info._is_under("/var/lib/docker", "/")
    assert docker_info._is_under("/var/lib/docker", "/var")
    assert docker_info._is_under("/var/lib/docker", "/var/lib/docker")
    assert not docker_info._is_under("/homework/docker", "/home")


def test_resolve_device_ignores_prefix_only_mounts(monkeypatch, tmp_path):
    mounts = tmp_path / "mounts"
    mounts.write_text(
        "/dev/sda1 / ext4 rw 0 0\n"
        "/dev/sdb1 /home ext4 rw 0 0\n"
    )
    real_path = docker_info.Path

    def fake_path(p):
        return mounts if p == "/proc/mounts" else real_path(p)

    monkeypatch.setattr(docker_info, "Path", fake_path)
    monkeypatch.setattr(real_path, "exists", lambda self: True)
    # /homework lives on /, not on /home
    assert docker_info._resolve_device("/homework/docker") == "/dev/sda"
