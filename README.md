# p4n4-emu

Sandbox emulator for the [p4n4](https://github.com/raisga/p4n4) IoT + Edge AI platform.

Applies Docker resource constraints (CPU, RAM, disk I/O) and optional QEMU ARM64 emulation as a Compose overlay on top of the existing p4n4 stacks — making a developer's workstation feel like target edge hardware without modifying any production files.

## How it works

`p4n4-emu` generates a `docker-compose.emu.yml` overlay file per stack and passes it to `docker compose -f docker-compose.yml -f *.emu.yml up -d`. The overlay injects `deploy.resources.limits` (CPU, memory) and optional `blkio_config` (disk I/O) per service. Production files are never touched.

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
- QEMU binfmt_misc (only for `--arch arm64`)

## Installation

```bash
cd ~/p4n4/demo/emu
uv sync          # or: pip install -e .
```

## Quick start

```bash
# Check your environment
p4n4-emu setup --check-only

# Enable ARM64 emulation (once, requires Docker)
p4n4-emu setup --arch arm64

# Start the IoT stack constrained to Raspberry Pi 5 specs
p4n4-emu up --stack-dir ~/p4n4/docker/iot --profile rpi5 

# Start all stacks with synthetic sensor data
p4n4-emu up --stack all --stack-dir ~/p4n4/docker --profile rpi5 --sim

# Preview overlay without starting (dry run)
p4n4-emu up --profile mcu-class --dry-run

# Check status
p4n4-emu status --profile rpi5

# Stop
p4n4-emu down --profile rpi5
p4n4-emu down --profile rpi5 --volumes   # also remove data volumes
```

## Command reference

```
p4n4-emu setup [--arch arm64] [--check-only]
p4n4-emu up    [--profile rpi5] [--stack iot|ai|edge|all]
               [--stack-dir PATH] [--arch arm64] [--sim] [--dry-run]
p4n4-emu down  [--profile rpi5] [--stack iot|ai|edge|all]
               [--stack-dir PATH] [--volumes]
p4n4-emu status [--profile rpi5] [--stack iot|ai|edge|all]
p4n4-emu profile list
p4n4-emu profile show <name>
p4n4-emu sim start [--interval 2.0] [--devices 1] [--mqtt-host p4n4-mqtt]
p4n4-emu sim stop
p4n4-emu sim status
```

## GPIO stub

The `p4n4_emu.hw.gpio_stub` module is a drop-in replacement for `RPi.GPIO`, enabling scripts like `p4n4_boot_sim.py` to run on any workstation:

```python
import sys
import p4n4_emu.hw.gpio_stub as GPIO
sys.modules["RPi"] = type(sys)("RPi")
sys.modules["RPi.GPIO"] = GPIO

# Now import your RPi script normally
```

Set `logging.basicConfig(level=logging.DEBUG)` to see pin state transitions in your terminal.

## Sensor simulator

The simulator publishes synthetic MQTT payloads on the same topics the p4n4 stack already consumes:

```
sensors/temperature   {"value": 23.4, "unit": "C", "device": "emu-sensor-0"}
sensors/humidity      {"value": 58.2, "unit": "%", "device": "emu-sensor-0"}
sensors/pressure      {"value": 1012.7, "unit": "hPa", "device": "emu-sensor-0"}
sensors/raw           {"values": [0.01, -0.02, 1.00], "cpu_pct": 42.3, "device": "emu-sensor-0"}
```

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