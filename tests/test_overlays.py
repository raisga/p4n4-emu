"""Tests for Compose overlay rendering."""

import pytest
import yaml

from p4n4_emu.overlays.generator import render_overlay
from p4n4_emu.profiles.loader import load_profile


@pytest.fixture
def rpi5():
    return load_profile("rpi5")


@pytest.fixture
def mcu():
    return load_profile("mcu-class")


def _parse(rendered: str) -> dict:
    return yaml.safe_load(rendered)


# ── IoT stack ─────────────────────────────────────────────────────────────────

def test_iot_overlay_valid_yaml(rpi5):
    rendered = render_overlay(rpi5, "iot", None)
    doc = _parse(rendered)
    assert "services" in doc


def test_iot_overlay_all_services_present(rpi5):
    doc = _parse(render_overlay(rpi5, "iot", None))
    assert set(doc["services"].keys()) == {"mqtt", "influxdb", "node-red", "grafana"}


def test_iot_overlay_cpu_limits(rpi5):
    doc = _parse(render_overlay(rpi5, "iot", None))
    for svc in doc["services"].values():
        cpus = float(svc["deploy"]["resources"]["limits"]["cpus"])
        assert cpus > 0


def test_iot_overlay_memory_limits(rpi5):
    doc = _parse(render_overlay(rpi5, "iot", None))
    for svc in doc["services"].values():
        mem = svc["deploy"]["resources"]["limits"]["memory"]
        assert mem.endswith("m")
        assert int(mem[:-1]) > 0


def test_iot_overlay_no_blkio_when_device_none(rpi5):
    doc = _parse(render_overlay(rpi5, "iot", None))
    for svc in doc["services"].values():
        assert "blkio_config" not in svc


def test_iot_overlay_blkio_present_with_device(rpi5):
    doc = _parse(render_overlay(rpi5, "iot", "/dev/sda"))
    for svc in doc["services"].values():
        assert "blkio_config" in svc
        assert svc["blkio_config"]["device_read_bps"][0]["path"] == "/dev/sda"


def test_iot_overlay_arm64_platform(rpi5):
    doc = _parse(render_overlay(rpi5, "iot", None))
    for svc in doc["services"].values():
        assert svc.get("platform") == "linux/arm64"


def test_iot_overlay_no_platform_for_x86(mcu):
    doc = _parse(render_overlay(mcu, "iot", None))
    for svc in doc["services"].values():
        assert "platform" not in svc


# ── AI stack ──────────────────────────────────────────────────────────────────

def test_ai_overlay_services(rpi5):
    doc = _parse(render_overlay(rpi5, "ai", None))
    assert set(doc["services"].keys()) == {"ollama", "letta", "n8n"}


def test_ai_overlay_ollama_gets_most_cpu(rpi5):
    doc = _parse(render_overlay(rpi5, "ai", None))
    ollama_cpu = float(doc["services"]["ollama"]["deploy"]["resources"]["limits"]["cpus"])
    n8n_cpu = float(doc["services"]["n8n"]["deploy"]["resources"]["limits"]["cpus"])
    assert ollama_cpu > n8n_cpu


# ── Edge stack ────────────────────────────────────────────────────────────────

def test_edge_overlay_services(rpi5):
    doc = _parse(render_overlay(rpi5, "edge", None))
    assert "ei-runner" in doc["services"]


# ── Cross-profile ─────────────────────────────────────────────────────────────

def test_mcu_memory_lower_than_rpi5():
    rpi5 = load_profile("rpi5")
    mcu = load_profile("mcu-class")
    doc_rpi5 = _parse(render_overlay(rpi5, "iot", None))
    doc_mcu = _parse(render_overlay(mcu, "iot", None))

    def mem_mb(doc: dict, svc: str) -> int:
        return int(doc["services"][svc]["deploy"]["resources"]["limits"]["memory"][:-1])

    assert mem_mb(doc_mcu, "influxdb") < mem_mb(doc_rpi5, "influxdb")
