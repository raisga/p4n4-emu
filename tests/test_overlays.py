"""Tests for Compose overlay rendering."""

import pytest
import yaml

from p4n4_emu.overlays.generator import (
    FALLBACK_SHARE,
    Scale,
    budget_scale,
    docker_platform,
    fixed_warnings,
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
    assert set(doc["services"].keys()) == {"ollama", "letta", "n8n", "ei-runner", "agent"}


def test_ai_overlay_keeps_the_agent_small(rpi5):
    limits = _parse(render_overlay(rpi5, "ai", ["ollama", "agent"]))["services"]["agent"]["deploy"]
    memory = limits["resources"]["limits"]["memory"]
    assert memory.endswith("m") and 200 <= int(memory[:-1]) <= 400


def test_ai_overlay_ollama_gets_most_cpu(rpi5):
    doc = _parse(render_overlay(rpi5, "ai", None))
    ollama_cpu = float(doc["services"]["ollama"]["deploy"]["resources"]["limits"]["cpus"])
    n8n_cpu = float(doc["services"]["n8n"]["deploy"]["resources"]["limits"]["cpus"])
    assert ollama_cpu > n8n_cpu


def test_ai_overlay_limits_the_inference_runner_as_the_edge_stack_did(rpi5):
    ai = _parse(render_overlay(rpi5, "ai", None))["services"]["ei-runner"]
    edge = _parse(render_overlay(rpi5, "edge", None))["services"]["ei-runner"]
    assert ai["deploy"] == edge["deploy"]


# ── Legacy edge layer (projects from before it merged into ai) ────────────────

def test_edge_overlay_services(rpi5):
    doc = _parse(render_overlay(rpi5, "edge", None))
    assert "ei-runner" in doc["services"]


def test_extras_services_get_the_fallback_share(rpi5):
    doc = _parse(render_overlay(rpi5, "extras", None, services=["video"]))
    assert "video" in doc["services"]


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
    assert _parse(render_overlay(rpi5, "iot", None, services=[]))["services"] == {}


def test_overlay_records_profile_and_platform(rpi5):
    doc = _parse(render_overlay(rpi5, "iot", None, services=["mqtt"]))
    assert doc["x-p4n4-emu"] == {"profile": "rpi5", "stack": "iot", "platform": "linux/arm64"}
    native = _parse(render_overlay(rpi5, "iot", None, services=["mqtt"], platform=None))
    assert native["x-p4n4-emu"] == {"profile": "rpi5", "stack": "iot"}


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
    stacks = ("iot", "ai", "extras", "dashboard")
    scale = budget_scale(service_shares(s) for s in stacks)
    docs = [_parse(render_overlay(rpi5, s, None, scale=scale)) for s in stacks]
    assert _total(docs, "cpus") <= rpi5.cpus + 0.05  # rounding to 2 decimals
    assert _total(docs, "memory") <= rpi5.memory_mb


def test_dashboard_gets_a_small_limit(rpi5):
    doc = _parse(render_overlay(rpi5, "dashboard", None))
    limits = doc["services"]["dashboard"]["deploy"]["resources"]["limits"]
    assert float(limits["memory"][:-1]) <= 128
    assert float(limits["cpus"]) <= 0.25


def test_budget_never_scales_up():
    assert budget_scale([service_shares("dashboard")]) == Scale()


# ── Fixed-size services (System 1) ────────────────────────────────────────────

AI_WITH_SYSTEM1 = ["ollama", "letta", "n8n", "system1"]


def _limits(doc, name):
    return doc["services"][name]["deploy"]["resources"]["limits"]


def test_system1_is_not_a_default_service(rpi5):
    assert "system1" not in service_shares("ai")
    assert "system1" not in _parse(render_overlay(rpi5, "ai", None))["services"]


def test_system1_gets_its_fixed_size(rpi5):
    shares = service_shares("ai", AI_WITH_SYSTEM1, profile=rpi5)
    scale = budget_scale(service_shares(s, AI_WITH_SYSTEM1 if s == "ai" else None, profile=rpi5)
                         for s in ("iot", "ai", "extras", "dashboard"))
    doc = _parse(render_overlay(rpi5, "ai", None, services=AI_WITH_SYSTEM1, scale=scale))
    assert shares["system1"].fixed
    assert _limits(doc, "system1")["memory"] == "2048m"
    assert float(_limits(doc, "system1")["cpus"]) == pytest.approx(2.0)


def test_other_services_shrink_around_system1(rpi5):
    stacks = ("iot", "ai", "extras", "dashboard")
    with_s1 = [service_shares(s, AI_WITH_SYSTEM1 if s == "ai" else None, profile=rpi5)
               for s in stacks]
    without = [service_shares(s, profile=rpi5) for s in stacks]
    scale, plain = budget_scale(with_s1), budget_scale(without)
    assert scale.memory < plain.memory and scale.cpu < plain.cpu
    docs = [_parse(render_overlay(rpi5, s, None, services=with_s1[i].keys(), scale=scale))
            for i, s in enumerate(stacks)]
    assert _total(docs, "memory") <= rpi5.memory_mb
    assert _total(docs, "cpus") <= rpi5.cpus + 0.05


def test_system1_runs_native_on_an_emulated_board(rpi5):
    doc = _parse(render_overlay(rpi5, "ai", None, services=AI_WITH_SYSTEM1))
    assert doc["services"]["ollama"]["platform"] == "linux/arm64"
    assert "platform" not in doc["services"]["system1"]


def test_system1_warnings(rpi5):
    rpi4, mcu = load_profile("rpi4"), load_profile("mcu-class")
    for prof, expected in ((rpi5, None), (rpi4, "not recommended"), (mcu, "can't run")):
        warnings = fixed_warnings(prof, [service_shares("ai", AI_WITH_SYSTEM1, profile=prof)])
        if expected is None:
            assert warnings == []
        else:
            assert len(warnings) == 1 and expected in warnings[0]


def test_system1_share_override_keeps_it_fixed(rpi5):
    shares = service_shares("ai", ["system1"], {"system1": {"memory": 0.4}}, profile=rpi5)
    assert shares["system1"].fixed and shares["system1"].memory == 0.4

