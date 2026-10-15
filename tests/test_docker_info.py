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


def test_resolve_device_finds_dev_root_by_device_number(monkeypatch, tmp_path):
    """Ubuntu cloud images list / as /dev/root, which has no device node."""
    mounts = tmp_path / "mounts"
    mounts.write_text("/dev/root / ext4 rw 0 0\n")
    partition = tmp_path / "sys" / "devices" / "nvme0n1" / "nvme0n1p1"
    partition.mkdir(parents=True)
    (partition / "partition").write_text("1\n")
    block = tmp_path / "sys" / "dev" / "block"
    block.mkdir(parents=True)
    (block / "259:1").symlink_to(partition)
    (tmp_path / "dev").mkdir()
    (tmp_path / "dev" / "nvme0n1").touch()

    real_path = docker_info.Path
    real_exists = real_path.exists

    def fake_path(p):
        return mounts if p == "/proc/mounts" else real_path(p)

    real_stat = docker_info.os.stat

    class Stat:
        st_dev = docker_info.os.makedev(259, 1)

    def fake_stat(path, **kw):
        return Stat() if path == "/var/lib/docker" else real_stat(path, **kw)

    monkeypatch.setattr(docker_info, "Path", fake_path)
    monkeypatch.setattr(real_path, "exists", lambda self: str(self) != "/dev/root"
                        and real_exists(self))
    monkeypatch.setattr(docker_info, "_SYS_BLOCK", block)
    monkeypatch.setattr(docker_info, "_DEV", tmp_path / "dev")
    monkeypatch.setattr(docker_info.os, "stat", fake_stat)
    assert docker_info._resolve_device("/var/lib/docker") == str(tmp_path / "dev" / "nvme0n1")
