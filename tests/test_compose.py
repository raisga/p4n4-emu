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
