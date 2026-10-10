"""Tests for compose file resolution and service discovery."""

import subprocess

import pytest

from p4n4_emu.utils import compose as dc


@pytest.fixture(autouse=True)
def _no_compose_env(monkeypatch):
    monkeypatch.delenv("COMPOSE_FILE", raising=False)
    monkeypatch.delenv("COMPOSE_PATH_SEPARATOR", raising=False)


def test_compose_files_includes_override(tmp_path):
    # Passing -f disables Compose's own override lookup, so we must add it
    (tmp_path / "docker-compose.yml").touch()
    (tmp_path / "docker-compose.override.yml").touch()
    assert dc.compose_files(tmp_path) == [
        tmp_path / "docker-compose.yml",
        tmp_path / "docker-compose.override.yml",
    ]


def test_compose_files_prefers_compose_yaml(tmp_path):
    (tmp_path / "compose.yaml").touch()
    (tmp_path / "docker-compose.yml").touch()
    assert dc.compose_files(tmp_path) == [tmp_path / "compose.yaml"]


def test_compose_files_honours_env(tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").touch()
    monkeypatch.setenv("COMPOSE_FILE", "base.yml:extra.yml")
    monkeypatch.setenv("COMPOSE_PATH_SEPARATOR", ":")
    assert dc.compose_files(tmp_path) == [tmp_path / "base.yml", tmp_path / "extra.yml"]


def test_compose_files_reads_dotenv(tmp_path):
    (tmp_path / ".env").write_text("FOO=1\nCOMPOSE_FILE='a.yml'\n")
    assert dc.compose_files(tmp_path) == [tmp_path / "a.yml"]


def test_compose_cmd_puts_overlay_last(tmp_path):
    (tmp_path / "docker-compose.yml").touch()
    (tmp_path / "docker-compose.override.yml").touch()
    overlay = tmp_path / "iot.emu.yml"
    assert dc.compose_cmd(tmp_path, overlay) == [
        "docker", "compose",
        "-f", str(tmp_path / "docker-compose.yml"),
        "-f", str(tmp_path / "docker-compose.override.yml"),
        "-f", str(overlay),
    ]


def test_list_services_uses_compose_config(tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").touch()
    monkeypatch.setattr(
        dc.subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="mqtt\ninfluxdb\n"),
    )
    assert dc.list_services(tmp_path) == ["mqtt", "influxdb"]


def test_list_services_falls_back_to_yaml(tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text("services:\n  mqtt: {}\n  grafana: {}\n")
    (tmp_path / "docker-compose.override.yml").write_text("services:\n  telegraf: {}\n")
    monkeypatch.setattr(
        dc.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="")
    )
    assert dc.list_services(tmp_path) == ["mqtt", "grafana", "telegraf"]


def _network_run(calls, inspect_rc=0, inspect_out=""):
    def run(cmd, **kwargs):
        calls.append(cmd[:3])
        if cmd[:3] == ["docker", "network", "inspect"]:
            return subprocess.CompletedProcess(cmd, inspect_rc, stdout=inspect_out)
        return subprocess.CompletedProcess(cmd, 0)

    return run


def test_ensure_network_creates_a_missing_one_with_label_and_subnet(monkeypatch):
    created = []

    def run(cmd, **kwargs):
        if cmd[:3] == ["docker", "network", "inspect"]:
            return subprocess.CompletedProcess(cmd, 1, stdout="")
        created.append(cmd)
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(dc.subprocess, "run", run)
    dc.ensure_network("p4n4-net")
    (create,) = created
    assert create[:3] == ["docker", "network", "create"]
    assert "com.docker.compose.network=p4n4-net" in create
    assert create[create.index("--subnet") + 1] == "172.20.0.0/16"


def test_ensure_network_keeps_a_labelled_one(monkeypatch):
    calls = []
    monkeypatch.setattr(dc.subprocess, "run", _network_run(calls, inspect_out="p4n4-net 3\n"))
    dc.ensure_network("p4n4-net")
    assert calls == [["docker", "network", "inspect"]]


def test_ensure_network_never_disconnects_running_containers(monkeypatch, capsys):
    # Recreating the network under running stacks cut them off (lost aliases)
    calls = []
    monkeypatch.setattr(dc.subprocess, "run", _network_run(calls, inspect_out=" 2\n"))
    dc.ensure_network("p4n4-net")
    assert calls == [["docker", "network", "inspect"]]
    assert "docker network rm p4n4-net" in " ".join(capsys.readouterr().err.split())


def test_ensure_network_recreates_an_unused_unlabelled_one(monkeypatch):
    calls = []
    monkeypatch.setattr(dc.subprocess, "run", _network_run(calls, inspect_out=" 0\n"))
    dc.ensure_network("p4n4-net")
    assert calls == [
        ["docker", "network", "inspect"],
        ["docker", "network", "rm"],
        ["docker", "network", "create"],
    ]


def test_ensure_network_labels_a_renamed_network_with_its_key(monkeypatch):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 1 if cmd[2] == "inspect" else 0, stdout="")

    monkeypatch.setattr(dc.subprocess, "run", run)
    dc.ensure_network("plant-bus", "10.9.0.0/16", "bus")
    assert "com.docker.compose.network=bus" in calls[-1]
    assert calls[-1][-1] == "plant-bus"


def test_ensure_network_leaves_an_external_one_as_it_is(monkeypatch):
    # Another stack owns it; Compose doesn't check an external network's label
    calls = []
    monkeypatch.setattr(dc.subprocess, "run", _network_run(calls, inspect_out=" 0\n"))
    dc.ensure_network("p4n4-net", owned=False)
    assert calls == [["docker", "network", "inspect"]]
