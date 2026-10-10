# p4n4-emu

Sandbox emulator for the [p4n4](https://github.com/raisga/p4n4) IoT + Edge AI platform.

Applies Docker resource constraints (CPU, RAM, disk I/O) and optional QEMU ARM64 emulation as a Compose overlay on top of the existing p4n4 stacks — making a developer's workstation feel like target edge hardware without modifying any production files.

## How it works

`p4n4-emu` generates a `*.emu.yml` overlay file per stack and passes it after the stack's own compose files (`compose.yaml` / `docker-compose.yml`, any `docker-compose.override.yml`, or `COMPOSE_FILE`) to `docker compose ... up -d`. The overlay covers the services the stack's compose config actually defines and injects `deploy.resources.limits` (CPU, memory), `memswap_limit` and optional `blkio_config` (disk I/O) per service. Production files are never touched.

All enabled stacks of a project share one emulated device: when their combined CPU or memory shares exceed the profile, every service is scaled down so the total fits.

Overlays live in `~/.p4n4-emu/overlays/<stack-dir>-<hash>/<stack>.emu.yml`, one folder per
stack directory, so two projects never share one. Each overlay records the profile it was
rendered for in an `x-p4n4-emu` block (Compose ignores `x-` keys), so `down`, `status` and
`logs` find the profile on their own. `down` deletes the overlay. Overlays written by
earlier versions (`~/.p4n4-emu/overlays/<profile>/`) are no longer read and can be deleted.

## Hardware profiles

| Profile | CPU | Memory | Disk R/W | Arch |
|---------|-----|--------|----------|------|
| `rpi4` | 4 cores | 3.5 GB | 50 MB/s | arm64 |
| `rpi5` | 4 cores | 7 GB | 100 MB/s | arm64 |
| `mcu-class` | 1 core | 256 MB | 10 MB/s | x86_64 |
| `nuc` | 4 cores | 14 GB | 200 MB/s | x86_64 |

## Requirements

- Docker Engine >= 24
- Docker Compose >= 2.17
- Python >= 3.11
- cgroup v2 (for limits to be enforced — check with `cat /sys/fs/cgroup/cgroup.controllers`)
- QEMU binfmt_misc (only to emulate another architecture, e.g. ARM profiles on an x86 host)

## Installation

```bash
cd ~/p4n4/tools/emu
uv tool install --editable .     # puts p4n4-emu on your PATH; or: pip install -e .
```

`--editable` keeps the install pointing at this checkout, which `sim` builds its
image from. To work on p4n4-emu itself, `uv sync` and `uv run p4n4-emu` also work, but
only inside `tools/emu`: run from a p4n4 project, `uv run` doesn't find the command.

## Setup guide

Follow these steps once to get p4n4-emu running on your workstation.

### 1. Install dependencies

Ensure Docker Engine >= 24 and Docker Compose >= 2.17 are installed and the daemon is running. Python >= 3.11 is also required.

```bash
docker version        # Engine version
docker compose version
python3 --version
```

### 2. Install p4n4-emu

Install the CLI as a uv tool, so `p4n4-emu` works from any directory, including your
p4n4 projects:

```bash
cd ~/p4n4/tools/emu
uv tool install --editable .
```

Verify the CLI is available:

```bash
p4n4-emu --help
```

### 3. Run preflight checks

```bash
p4n4-emu setup --check-only
```

All checks should show **OK**. A `cgroup v2` warning means CPU/memory limits won't be enforced — check your kernel or Docker Desktop settings if that matters for your testing.

### 4. (Optional) Enable ARM64 emulation

Required to run the ARM profiles (`rpi4` / `rpi5`) on an x86 host. `up` runs their arm64 images by default and refuses to start until QEMU is registered; pass `--native` to apply only the resource limits with host-architecture images:

```bash
p4n4-emu setup --arch arm64
```

This runs `tonistiigi/binfmt --install arm64` (pinned to `qemu-v10.2.3` by digest, since it runs privileged) via Docker once per host.

### 5. Start a stack

Inside a project scaffolded by `p4n4 init` (found via its `.p4n4.json`), the emulator
detects the enabled stacks and their layout automatically — flat single-layer projects
as well as multi-layer projects where each stack lives in its own subdirectory
(`<project>/iot/`, `<project>/ai/`):

```bash
cd ~/projects/my-p4n4-project
p4n4-emu up --profile rpi5              # all enabled stacks
p4n4-emu up --profile rpi5 --stack ai   # one stack only
```

Outside a project, point `--stack-dir` at a directory containing a compose file
(or a parent with per-stack subdirectories):

```bash
# Raspberry Pi 5 constraints, IoT stack only
p4n4-emu up --stack-dir ~/p4n4/stacks/iot --profile rpi5

# All stacks + synthetic sensor data
p4n4-emu up --stack all --stack-dir ~/p4n4/docker --profile rpi5 --sim
```

Use `--dry-run` first if you want to inspect the generated overlay before containers start:

```bash
p4n4-emu up --stack-dir ~/p4n4/stacks/iot --profile rpi5 --dry-run
```

### 6. Check status and stop

```bash
p4n4-emu status                    # the profile up used; usage against limits

p4n4-emu down                      # stop containers
p4n4-emu down --volumes            # stop and remove data volumes
```

`status` reads the profile from the stack's overlay, so none of these needs `--profile`.
For each service it shows CPU and memory in use against the container's limits
(highlighted at 90%), and whether the container actually has the limits its overlay asks
for. **stale** means it doesn't: it was created before the overlay changed, or without it.
Run `p4n4-emu up` again to recreate it. Without cgroup v2, `status` warns that the
limits are set but not enforced.

---

## Development

```
uv sync --extra dev
uv run ruff check .
uv run pytest                          # unit tests, plus overlays checked against the real stacks
uv run pytest --run-integration        # also starts the iot stack and the simulator (needs Docker,
                                       # and no other p4n4 project running)
```

`tests/test_real_stacks.py` merges every profile's overlay with the real stack compose files
through `docker compose config`; it finds them in `../../stacks/*` and `../../dashboard` (the
p4n4 repo), or `P4N4_STACKS_DIR` / `P4N4_DASHBOARD_DIR`. The integration test reads
`P4N4_EMU_IT_PROFILE` (default `rpi4`), `P4N4_EMU_IT_ARCH` (e.g. `arm64`, default native)
and `P4N4_EMU_IT_TIMEOUT`.

---

## Command reference

From a p4n4 project, `p4n4 up --emu rpi5` runs `p4n4-emu up --profile rpi5` for the
project's stacks, and `p4n4 down` hands the stacks p4n4-emu started back to
`p4n4-emu down`, so their overlays and the simulator go too. `p4n4-emu` must be on `PATH`.

```
p4n4-emu setup [--arch arm64|armv7] [--check-only]
p4n4-emu up    [--profile rpi5] [--stack iot|ai|edge|dashboard|iot,ai|all]
               [--stack-dir PATH] [--arch arm64|armv7|x86_64 | --native]
               [--sim] [--sim-interval 2.0] [--sim-devices 1] [--build] [--pull]
               [--dry-run]
p4n4-emu down  [--stack iot|ai|edge|dashboard|iot,ai|all]
               [--stack-dir PATH] [--volumes] [--yes]
p4n4-emu status [--profile rpi5] [--stack iot|ai|edge|dashboard|iot,ai|all] [--json]
p4n4-emu logs  [SERVICE] [--stack iot|ai|edge|dashboard|iot,ai|all]
               [--stack-dir PATH] [--tail 100] [--no-follow]
p4n4-emu profile list [--json]
p4n4-emu profile show <name> [--json]
p4n4-emu profile switch <name> [--stack ...] [--dry-run]
p4n4-emu sim start [--interval 2.0] [--devices 1] [--mqtt-host HOST] [--network NAME]
p4n4-emu sim stop
p4n4-emu sim status
```

Without `--stack`, `up`/`down`/`status`/`logs` target the enabled stacks of the surrounding
p4n4 project (`.p4n4.json` is found by walking up from the current directory), falling
back to `iot`. Stack directories resolve in this order: `--stack-dir` (its `<stack>/`
subdirectory first), then the p4n4 project layout (flat root or `<project>/<stack>/`),
then a `<stack>/` or compose file next to the current directory.

The shared network (`p4n4-net`) and the broker (`p4n4-mqtt`) are read from the stack's
compose config, so a project that renames them still works: `up` creates the networks the
stack names before Compose starts it, and the simulator joins the iot broker's network.
`sim start --mqtt-host` / `--network` override them.

`--arch` overrides the profile's architecture; without it, ARM profiles emulate arm64
and x86 profiles run natively. `--native` never forces a platform.

## GPIO stub

The `p4n4_emu.hw.gpio_stub` module is a drop-in replacement for `RPi.GPIO`, enabling scripts like `p4n4_boot_sim.py` to run on any workstation:

```python
import sys
import p4n4_emu.hw.gpio_stub as GPIO
sys.modules["RPi"] = type(sys)("RPi")
sys.modules["RPi.GPIO"] = GPIO

# Now import your RPi script normally
```

It covers the `RPi.GPIO` calls the p4n4 scripts make: `setmode`, `setup` (with
`pull_up_down=` and `initial=`, one channel or a list), `output`, `input`, the edge
functions (`add_event_detect`, `add_event_callback`, `remove_event_detect`,
`event_detected`, with `bouncetime`) and `cleanup`. `PWM` and `wait_for_edge` aren't
stubbed yet.

Nothing outside the script drives an input pin, so press a button with `set_input`, a
stub-only call. A level change is an edge and fires the pin's callbacks:

```python
GPIO.set_input(27, GPIO.LOW)    # press the button on GPIO 27 (pulled up)
GPIO.set_input(27, GPIO.HIGH)   # release it
```

Set `logging.basicConfig(level=logging.DEBUG)` to see pin state transitions in your terminal.

## Sensor simulator

The simulator publishes synthetic MQTT payloads on the topics the p4n4 stack consumes, `sensors/<device-id>/<measurement>`:

```
sensors/emu-sensor-0/temperature   {"value": 23.4, "unit": "C"}
sensors/emu-sensor-0/humidity      {"value": 58.2, "unit": "%"}
sensors/emu-sensor-0/pressure      {"value": 1012.7, "unit": "hPa"}
sensors/emu-sensor-0/raw           {"values": [0.01, -0.02, 1.00], "cpu_pct": 42.3}
```

Each device follows the same waveforms, shifted by a phase derived from its id,
so `emu-sensor-0` and `emu-sensor-1` report different values at the same moment. If the
broker isn't up yet, or restarts, the simulator keeps retrying (1 s, doubling up to 30 s)
and drops the readings it takes while disconnected.

Run standalone (against a local Mosquitto):
```bash
MQTT_HOST=localhost uv run python -m p4n4_emu.sim.sensor_sim
```

## Known limitations

- **cgroup v2 required** — CPU/memory limits are not enforced on cgroup v1 hosts.
- **blkio_config** is skipped when Docker's data root is on a non-block device (tmpfs, NFS).
- **QEMU overhead** ~3–10x on ARM64; Ollama LLM inference under QEMU is impractical.
- **Network I/O throttling** is not supported (requires host-level `tc netem`).

## License

MIT — see [LICENSE](LICENSE).