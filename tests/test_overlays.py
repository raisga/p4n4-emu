"""Tests for Compose overlay rendering."""

import pytest
import yaml

from p4n4_emu.overlays.generator import (
    FALLBACK_SHARE,
    Scale,
    budget_scale,
    docker_platform,
    render_overlay,
    service_shares,
)
from p4n4_emu.profiles.loader import Profile, load_profile


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


# ── Services from the compose config ──────────────────────────────────────────

def test_overlay_only_renders_services_in_compose(rpi5):
    # A project that dropped grafana must not get an image-less grafana service
    doc = _parse(render_overlay(rpi5, "iot", None, services=["mqtt", "influxdb", "node-red"]))
    assert set(doc["services"]) == {"mqtt", "influxdb", "node-red"}


def test_overlay_limits_unknown_services_with_fallback_share(rpi5):
    doc = _parse(render_overlay(rpi5, "iot", None, services=["mqtt", "telegraf"]))
    limits = doc["services"]["telegraf"]["deploy"]["resources"]["limits"]
    assert float(limits["cpus"]) == pytest.approx(rpi5.cpus * FALLBACK_SHARE.cpu, abs=0.01)
    assert limits["memory"] == f"{int(rpi5.memory_mb * FALLBACK_SHARE.memory)}m"


def test_overlay_with_no_services_is_valid(rpi5):
    assert _parse(render_overlay(rpi5, "iot", None, services=[])) == {"services": {}}


# ── Platform ──────────────────────────────────────────────────────────────────

def test_overlay_native_platform_omits_platform(rpi5):
    doc = _parse(render_overlay(rpi5, "iot", None, platform=None))
    for svc in doc["services"].values():
        assert "platform" not in svc


def test_overlay_explicit_platform_on_x86_profile(mcu):
    doc = _parse(render_overlay(mcu, "iot", None, platform="linux/arm/v7"))
    for svc in doc["services"].values():
        assert svc["platform"] == "linux/arm/v7"


def test_docker_platform_rejects_unknown_arch():
    with pytest.raises(ValueError, match="Unknown architecture"):
        docker_platform("sparc")


# ── memswap_limit ─────────────────────────────────────────────────────────────

def test_overlay_memswap_equals_memory_without_swap(rpi5):
    doc = _parse(render_overlay(rpi5, "iot", None))
    for svc in doc["services"].values():
        assert svc["memswap_limit"] == svc["deploy"]["resources"]["limits"]["memory"]


def test_overlay_memswap_follows_profile_swap_ratio():
    prof = Profile(
        name="swappy", description="", cpus=4, memory="1000m", memory_swap="2000m",
        blkio_weight=100, blkio_read_bps=1, blkio_write_bps=1,
    )
    doc = _parse(render_overlay(prof, "iot", None, services=["influxdb"]))
    svc = doc["services"]["influxdb"]
    assert svc["deploy"]["resources"]["limits"]["memory"] == "350m"
    assert svc["memswap_limit"] == "700m"


# ── Per-device budget ─────────────────────────────────────────────────────────

def _total(docs: list[dict], key: str) -> float:
    total = 0.0
    for doc in docs:
        for svc in doc["services"].values():
            value = svc["deploy"]["resources"]["limits"][key]
            total += float(value[:-1]) if key == "memory" else float(value)
    return total


def test_budget_keeps_all_stacks_within_one_device(rpi5):
    scale = budget_scale(service_shares(s) for s in ("iot", "ai", "edge"))
    docs = [_parse(render_overlay(rpi5, s, None, scale=scale)) for s in ("iot", "ai", "edge")]
    assert _total(docs, "cpus") <= rpi5.cpus + 0.05  # rounding to 2 decimals
    assert _total(docs, "memory") <= rpi5.memory_mb


def test_budget_never_scales_up():
    assert budget_scale([service_shares("edge")]) == Scale()
