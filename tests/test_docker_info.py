"""Tests for block device detection."""

import os

import pytest

from p4n4_emu.utils import docker_info


def test_is_under_compares_path_components():
    assert docker_info._is_under("/var/lib/docker", "/")
    assert docker_info._is_under("/var/lib/docker", "/var")
    assert docker_info._is_under("/var/lib/docker", "/var/lib/docker")
    assert not docker_info._is_under("/homework/docker", "/home")


@pytest.fixture
def host(monkeypatch, tmp_path):
    """A fake host: /proc/mounts, /sys/dev/block, /dev and os.stat.

    `disk(name, major, partitions)` adds a disk and its partitions (numbered from
    minor + 1), `mount(line)` a /proc/mounts line, and `node(path, major, minor)`
    a device node; `on(path, major, minor)` puts a path on that device. Paths the
    fake doesn't know don't exist.
    """
    sys_block = tmp_path / "sys" / "dev" / "block"
    sys_block.mkdir(parents=True)
    dev = tmp_path / "dev"
    dev.mkdir()
    mounts = tmp_path / "mounts"
    mounts.write_text("")
    nodes: dict[str, int] = {}   # /dev path as /proc/mounts names it → st_rdev
    paths: dict[str, int] = {}   # path → st_dev

    class Host:
        def disk(self, name, major, partitions=(), minor=0):
            disk_dir = tmp_path / "sys" / "devices" / name
            disk_dir.mkdir(parents=True)
            (sys_block / f"{major}:{minor}").symlink_to(disk_dir)
            (dev / name).touch()
            for i, part in enumerate(partitions, start=1):
                part_dir = disk_dir / part
                part_dir.mkdir()
                (part_dir / "partition").write_text(f"{i}\n")
                (sys_block / f"{major}:{minor + i}").symlink_to(part_dir)
                (dev / part).touch()

        def mount(self, line):
            mounts.write_text(mounts.read_text() + line + "\n")

        def node(self, path, major, minor):
            nodes[path] = os.makedev(major, minor)

        def on(self, path, major, minor):
            paths[path] = os.makedev(major, minor)

    real_path = docker_info.Path

    def fake_path(p):
        return mounts if p == "/proc/mounts" else real_path(p)

    class Stat:
        def __init__(self, st_dev=0, st_rdev=0):
            self.st_dev, self.st_rdev = st_dev, st_rdev

    real_stat = os.stat

    def fake_stat(path, **kw):
        if path in nodes:
            return Stat(st_rdev=nodes[path])
        if path in paths:
            return Stat(st_dev=paths[path])
        if str(path).startswith(str(tmp_path)):
            return real_stat(path, **kw)  # pathlib's own checks in the fake sysfs and /dev
        raise FileNotFoundError(path)

    monkeypatch.setattr(docker_info, "Path", fake_path)
    monkeypatch.setattr(docker_info, "_SYS_BLOCK", sys_block)
    monkeypatch.setattr(docker_info, "_DEV", dev)
    monkeypatch.setattr(docker_info.os, "stat", fake_stat)
    h = Host()
    h.dev = dev
    return h


def test_resolve_device_ignores_prefix_only_mounts(host):
    host.disk("sda", 8, ["sda1"])
    host.disk("sdb", 8, ["sdb1"], minor=16)
    host.mount("/dev/sda1 / ext4 rw 0 0")
    host.mount("/dev/sdb1 /home ext4 rw 0 0")
    host.node("/dev/sda1", 8, 1)
    host.node("/dev/sdb1", 8, 17)
    # /homework lives on /, not on /home
    assert docker_info._resolve_device("/homework/docker") == str(host.dev / "sda")
    assert docker_info._resolve_device("/home/docker") == str(host.dev / "sdb")


def test_resolve_device_takes_the_disk_of_a_partition_link(host):
    """GitHub's runners list / as /dev/root, a link to the partition sda1. Its
    numbers (8:1) are a partition's, which io.max refuses with "no such device"."""
    host.disk("sda", 8, ["sda1"])
    host.mount("/dev/root / ext4 rw 0 0")
    host.node("/dev/root", 8, 1)
    assert docker_info._resolve_device("/var/lib/docker") == str(host.dev / "sda")


def test_resolve_device_names_nvme_and_mmc_disks(host):
    host.disk("nvme0n1", 259, ["nvme0n1p1", "nvme0n1p2"])
    host.disk("mmcblk0", 179, ["mmcblk0p1", "mmcblk0p2"], minor=0)
    host.mount("/dev/nvme0n1p2 / ext4 rw 0 0")
    host.mount("/dev/mmcblk0p2 /data ext4 rw 0 0")
    host.node("/dev/nvme0n1p2", 259, 2)
    host.node("/dev/mmcblk0p2", 179, 2)
    assert docker_info._resolve_device("/var/lib/docker") == str(host.dev / "nvme0n1")
    assert docker_info._resolve_device("/data/docker") == str(host.dev / "mmcblk0")


def test_resolve_device_keeps_a_whole_disk(host):
    host.disk("vda", 252)
    host.mount("/dev/vda / ext4 rw 0 0")
    host.node("/dev/vda", 252, 0)
    assert docker_info._resolve_device("/var/lib/docker") == str(host.dev / "vda")


def test_resolve_device_finds_dev_root_by_device_number(host):
    """Ubuntu cloud images list / as /dev/root, which has no device node."""
    host.disk("nvme0n1", 259, ["nvme0n1p1"])
    host.mount("/dev/root / ext4 rw 0 0")
    host.on("/var/lib/docker", 259, 1)
    assert docker_info._resolve_device("/var/lib/docker") == str(host.dev / "nvme0n1")


def test_resolve_device_gives_up_without_a_sysfs_entry(host):
    """No disk limits rather than a guess: a wrong device stops every container."""
    host.mount("/dev/mapper/vg-root / ext4 rw 0 0")
    host.node("/dev/mapper/vg-root", 253, 0)
    assert docker_info._resolve_device("/var/lib/docker") is None


def test_resolve_device_skips_filesystems_without_a_device(host):
    host.mount("overlay / overlay rw 0 0")
    assert docker_info._resolve_device("/var/lib/docker") is None
