"""Tests for preflight checks against what `docker info` reports."""

import subprocess

from p4n4_emu.utils import docker_host, preflight
from p4n4_emu.utils.docker_host import DockerHost

LINUX = DockerHost("29.4.1", "2", "systemd", "Manjaro Linux")
DESKTOP = DockerHost("29.4.1", "2", "cgroupfs", "Docker Desktop")


def _preflight(monkeypatch, host, *, binfmt=False, host_cgroup_v2=True):
    monkeypatch.setattr(preflight, "docker_host", lambda: host)
    monkeypatch.setattr(preflight, "_run", lambda args: (0, "2.29.0"))
    monkeypatch.setattr(
        docker_host.Path, "exists",
        lambda self: binfmt if "binfmt" in str(self) else host_cgroup_v2,
    )
    return preflight.run_preflight(require_qemu=True, platform="linux/arm64")


def test_docker_desktop_needs_no_host_binfmt(monkeypatch):
    # The VM ships QEMU; this host's /proc/sys/fs/binfmt_misc doesn't show it
    assert _preflight(monkeypatch, DESKTOP) == []


def test_linux_without_binfmt_needs_qemu(monkeypatch):
    issues = _preflight(monkeypatch, LINUX)
    assert len(issues) == 1 and "QEMU binfmt" in issues[0]


def test_cgroup_version_comes_from_the_engine(monkeypatch):
    # On Docker Desktop the host has no /sys/fs/cgroup; the engine's answer wins
    issues = _preflight(monkeypatch, DESKTOP, host_cgroup_v2=False)
    assert issues == []
    v1 = DockerHost("29.4.1", "1", "cgroupfs", "Docker Desktop")
    assert any("cgroup v2 not detected" in i for i in _preflight(monkeypatch, v1))


def test_no_cgroup_driver_warns(monkeypatch):
    rootless = DockerHost("29.4.1", "2", "none", "Ubuntu 24.04")
    issues = _preflight(monkeypatch, rootless, binfmt=True)
    assert len(issues) == 1 and issues[0].startswith("WARNING: Docker reports no cgroup driver")


def test_old_engine(monkeypatch):
    issues = _preflight(monkeypatch, DockerHost("23.0.1", "2", "systemd", "Debian"), binfmt=True)
    assert issues == ["Docker Engine >= 24 required; found '23.0.1'."]


def test_docker_host_parses_info(monkeypatch):
    out = "29.4.1|2|systemd|Docker Desktop|runc,io.containerd.runc.v2,\n"
    monkeypatch.setattr(
        docker_host.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=out),
    )
    host = docker_host.docker_host()
    assert host == DockerHost(
        "29.4.1", "2", "systemd", "Docker Desktop", ("io.containerd.runc.v2", "runc")
    )
    assert host.desktop
    assert not host.nvidia


def test_docker_host_sees_the_nvidia_runtime(monkeypatch):
    out = "29.4.1|2|systemd|Ubuntu 24.04|nvidia,runc,\n"
    monkeypatch.setattr(
        docker_host.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=out),
    )
    assert docker_host.docker_host().nvidia


def test_docker_host_none_when_daemon_down(monkeypatch):
    monkeypatch.setattr(
        docker_host.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, stdout="||||\n"),
    )
    assert docker_host.docker_host() is None
