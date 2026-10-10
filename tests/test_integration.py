"""End to end: the iot stack under a profile, the simulator, and readings in InfluxDB.

Starts real containers, so it only runs with --run-integration (or
P4N4_EMU_INTEGRATION=1). It copies the p4n4-iot checkout into a temporary p4n4
project, runs mqtt, influxdb and node-red under the profile with the sensor
simulator, waits for Node-RED to write the simulator's readings to InfluxDB, and
checks every container got its limits. Everything is removed afterwards, volumes too.

p4n4 stacks use fixed container names (p4n4-mqtt, ...), so it refuses to run while
any p4n4 container exists on the host rather than touch another project.

  P4N4_EMU_IT_PROFILE  profile to run under (default rpi4; mcu-class's 256 MB
                       leaves Node-RED 38 MiB and InfluxDB 89 MiB)
  P4N4_EMU_IT_ARCH     emulate an architecture, e.g. arm64 (default: native)
  P4N4_EMU_IT_TIMEOUT  seconds to wait for the first reading (default 240)
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

import pytest
from typer.testing import CliRunner

from p4n4_emu.cli import app
from tests.conftest import compose_available, stack_dirs

pytestmark = pytest.mark.integration

PROFILE = os.environ.get("P4N4_EMU_IT_PROFILE", "rpi4")
ARCH = os.environ.get("P4N4_EMU_IT_ARCH", "")
TIMEOUT = float(os.environ.get("P4N4_EMU_IT_TIMEOUT", "240"))
SERVICES = "mqtt,influxdb,node-red"

runner = CliRunner()


def _p4n4_containers() -> list[str]:
    r = subprocess.run(
        ["docker", "ps", "-a", "--filter", "name=^p4n4-", "--format", "{{.Names}}"],
        capture_output=True, text=True, check=True,
    )
    return r.stdout.split()


def _env(path) -> dict[str, str]:
    env = {}
    for line in path.read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep and not key.lstrip().startswith("#"):
            env[key.strip()] = value.split("#")[0].strip()
    return env


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A flat iot project copied from the p4n4-iot checkout, stopped afterwards."""
    if not compose_available():
        pytest.skip("docker compose not installed")
    source = stack_dirs().get("iot")
    if source is None:
        pytest.skip("no p4n4-iot checkout (set P4N4_STACKS_DIR)")
    taken = _p4n4_containers()
    if taken:
        pytest.skip(f"p4n4 containers exist on this host: {', '.join(taken)}")

    root = tmp_path / "emu-it"
    shutil.copytree(source, root, ignore=shutil.ignore_patterns(".git", ".env"))
    (root / ".p4n4.json").write_text(
        json.dumps({"schema_version": 1, "project": "emu-it", "layers": ["iot"]})
    )
    env = (root / ".env.example").read_text()
    env = env.replace(
        "COMPOSE_PROFILES=mqtt,influxdb,node-red,grafana", f"COMPOSE_PROFILES={SERVICES}"
    )
    (root / ".env").write_text(env + "\nCOMPOSE_PROJECT_NAME=p4n4-emu-it\n")
    monkeypatch.chdir(root)
    try:
        yield root
    finally:
        runner.invoke(app, ["down", "--stack", "iot", "--volumes", "--yes"])


def _readings(org: str, token: str, bucket: str) -> int:
    """Sensor readings in the bucket from the last 15 minutes."""
    flux = (
        f'from(bucket: "{bucket}") |> range(start: -15m) '
        '|> filter(fn: (r) => r._measurement == "sensor_data") |> count() '
        '|> group() |> sum()'
    )
    r = subprocess.run(
        ["docker", "exec", "p4n4-influxdb", "influx", "query", "--raw",
         "--org", org, "--token", token, flux],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        return 0
    # Annotated CSV: the data row ends with the count
    rows = [line for line in r.stdout.splitlines() if line and not line.startswith("#")]
    try:
        return int(rows[-1].rsplit(",", 1)[-1]) if len(rows) > 1 else 0
    except ValueError:
        return 0


def test_simulator_readings_reach_influxdb_under_a_profile(project):
    arch = ["--arch", ARCH] if ARCH else ["--native"]
    result = runner.invoke(
        app,
        ["up", "--profile", PROFILE, "--stack", "iot", *arch,
         "--sim", "--sim-interval", "1", "--sim-devices", "2"],
    )
    assert result.exit_code == 0, result.output

    env = _env(project / ".env")
    deadline = time.monotonic() + TIMEOUT
    count = 0
    while time.monotonic() < deadline:
        count = _readings(env["INFLUXDB_ORG"], env["INFLUXDB_TOKEN"], env["INFLUXDB_BUCKET"])
        if count:
            break
        time.sleep(5)
    assert count > 0, f"no sensor_data in InfluxDB after {TIMEOUT:.0f}s under {PROFILE}"

    status = runner.invoke(app, ["status", "--stack", "iot", "--json"])
    assert status.exit_code == 0, status.output
    (stack,) = json.loads(status.output)["stacks"]
    assert stack["profile"] == PROFILE
    states = {svc["service"]: (svc["state"], svc["limits_state"]) for svc in stack["services"]}
    for service in SERVICES.split(","):
        # Running (not OOM-killed) with the overlay's limits
        assert states.get(service) == ("running", "applied"), states
