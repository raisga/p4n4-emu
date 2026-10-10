"""Shared test fixtures, and the opt-in switch for the integration tests."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

# p4n4 checkout this repo usually sits in (tools/emu); P4N4_ROOT overrides it, and
# P4N4_STACKS_DIR / P4N4_DASHBOARD_DIR point at stack checkouts anywhere else (CI)
P4N4_ROOT = Path(os.environ.get("P4N4_ROOT", Path(__file__).resolve().parents[3]))


def stack_dirs() -> dict[str, Path]:
    """Checkouts of the real stacks, by stack name; missing ones are left out."""
    stacks = Path(os.environ.get("P4N4_STACKS_DIR", P4N4_ROOT / "stacks"))
    dirs = {name: stacks / name for name in ("iot", "ai", "edge")}
    dirs["dashboard"] = Path(os.environ.get("P4N4_DASHBOARD_DIR", P4N4_ROOT / "dashboard"))
    return {name: d for name, d in dirs.items() if (d / "docker-compose.yml").exists()}


def compose_available() -> bool:
    if shutil.which("docker") is None:
        return False
    r = subprocess.run(["docker", "compose", "version"], capture_output=True, check=False)
    return r.returncode == 0


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run the integration tests, which start real containers.",
    )


def pytest_collection_modifyitems(config, items):
    if config.getoption("--run-integration") or os.environ.get("P4N4_EMU_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="starts containers: pass --run-integration")
    for item in items:
        if "integration" in item.keywords:
            item.add_marker(skip)
