"""Every profile's overlay, merged by `docker compose config` with the real stacks.

Renders each stack's overlay for every profile against the stack's own compose file,
then checks that Compose accepts the merge and that every service carries the limits.
Needs `docker compose` (no daemon calls beyond `config`) and the stack checkouts:
`../../stacks/*` and `../../dashboard` in the p4n4 repo, or P4N4_STACKS_DIR /
P4N4_DASHBOARD_DIR. Skipped without them.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from p4n4_emu.overlays.generator import docker_platform, render_overlay
from p4n4_emu.profiles.loader import list_profiles, load_profile
from p4n4_emu.utils.usage import expected_limits, parse_size
from tests.conftest import compose_available, stack_dirs

STACKS = stack_dirs()

pytestmark = [
    pytest.mark.compose,
    pytest.mark.skipif(not compose_available(), reason="docker compose not installed"),
    pytest.mark.skipif(not STACKS, reason="no stack checkouts (set P4N4_STACKS_DIR)"),
]

# Any path will do: `config` doesn't look the device up
_DEVICE = "/dev/sda"


def _compose(stack_dir: Path, *files: Path) -> list[str]:
    cmd = ["docker", "compose", "--project-directory", str(stack_dir)]
    for f in (stack_dir / "docker-compose.yml", *files):
        cmd += ["-f", str(f)]
    example = stack_dir / ".env.example"
    if example.exists():
        cmd += ["--env-file", str(example)]
    # Every service sits in a profile of its own; enable them all
    return [*cmd, "--profile", "*"]


def _services(stack_dir: Path) -> list[str]:
    r = subprocess.run(
        [*_compose(stack_dir), "config", "--services"],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.split()


@pytest.mark.parametrize("profile", list_profiles())
@pytest.mark.parametrize("stack", sorted(STACKS))
def test_overlay_merges_with_the_real_stack(stack, profile, tmp_path):
    stack_dir = STACKS[stack]
    prof = load_profile(profile)
    services = _services(stack_dir)
    assert services, f"{stack} defines no services"

    rendered = render_overlay(prof, stack, _DEVICE, services=services)
    overlay = tmp_path / f"{stack}.emu.yml"
    overlay.write_text(rendered)
    r = subprocess.run(
        [*_compose(stack_dir, overlay), "config", "--format", "json"],
        capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr
    merged = json.loads(r.stdout)["services"]

    expected = expected_limits(yaml.safe_load(rendered))
    platform = docker_platform(prof.arch) if prof.is_arm else None
    # The overlay names exactly the stack's services: none unconstrained, none invented
    assert set(expected) == set(merged)
    for name, svc in merged.items():
        want = expected[name]
        limits = svc["deploy"]["resources"]["limits"]
        assert float(limits["cpus"]) == pytest.approx(want.cpus), name
        assert int(limits["memory"]) == want.memory, name
        assert parse_size(str(svc["memswap_limit"])) == want.memswap, name
        assert svc.get("platform") == platform, name
        # Compose's JSON spells these keys Path / Rate
        (read,) = svc["blkio_config"]["device_read_bps"]
        assert read["Path"] == _DEVICE and int(read["Rate"]) == want.read_bps, name
        # The overlay must not change what the stack runs
        assert svc.get("image") or svc.get("build"), name


@pytest.mark.parametrize("stack", sorted(s for s in STACKS if s in ("ai", "edge")))
def test_gpu_reservations_merge_with_the_real_stack(stack, tmp_path):
    stack_dir = STACKS[stack]
    services = _services(stack_dir)
    rendered = render_overlay(
        load_profile("jetson-orin-nano"), stack, None, services=services, platform=None,
        gpu="nvidia",
    )
    overlay = tmp_path / f"{stack}.emu.yml"
    overlay.write_text(rendered)
    r = subprocess.run(
        [*_compose(stack_dir, overlay), "config", "--format", "json"],
        capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr
    merged = json.loads(r.stdout)["services"]
    gpu_users = {"ollama", "ei-runner"} & set(merged)
    assert gpu_users
    for name, svc in merged.items():
        devices = svc["deploy"]["resources"].get("reservations", {}).get("devices")
        if name in gpu_users:
            assert devices == [{"driver": "nvidia", "count": 1, "capabilities": ["gpu"]}], name
        else:
            assert not devices, name
