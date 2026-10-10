# p4n4-emu Setup & LED Toggle Guide

Step-by-step guide to install the emulator, start simulated sensor data, and drive a GPIO LED from a sensor threshold — entirely on a workstation with no physical hardware.

---

## Prerequisites

| Requirement | Minimum version | Check command |
|---|---|---|
| Docker Engine | 24.x | `docker --version` |
| Docker Compose (plugin) | 2.17 | `docker compose version` |
| Python | 3.11 | `python --version` |
| uv (package manager) | any | `uv --version` |
| cgroup v2 | — | `docker info -f '{{.CgroupVersion}}'` prints `2` (on v1, disk limits only throttle direct I/O) |

Install `uv` if missing:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## 1. Clone and Install

```bash
git clone https://github.com/raisga/p4n4-emu.git
cd p4n4-emu
uv tool install --editable .
```

`uv tool install` installs p4n4-emu and its dependencies in their own environment and puts
`p4n4-emu` on your `PATH`, so it works from any directory, including your p4n4 projects.

`--editable` keeps the install pointing at this checkout, so edits apply on the next
command. The simulator image is pulled from `ghcr.io/raisga/p4n4-sensor-sim`, or built
from the package when that fails; `p4n4-emu sim start --rebuild` builds it from your
edits. To work on p4n4-emu itself, `uv sync` and `uv run p4n4-emu` also work, but only
inside `tools/emu`: run from a p4n4 project, `uv run` doesn't find the command.

Verify the CLI is available:

```bash
p4n4-emu --help
```

Expected output:

```
 Usage: p4n4-emu [OPTIONS] COMMAND [ARGS]...
 ...
 Commands:
   down     Stop an emulated stack.
   profile  Inspect hardware profiles.
   setup    Preflight checks and QEMU install.
   sim      Manage the sensor simulator container.
   status   Show emulated stack status.
   up       Start a stack with emulated hardware constraints.
```

---

## 2. Run Preflight Checks

```bash
p4n4-emu setup
```

The command checks Docker version, Compose version, and cgroup v2. Expected output:

```
┌──────────────────────────────────┐
│       p4n4-emu preflight         │
├─────────────┬────────────────────┤
│ Check       │ Result             │
├─────────────┼────────────────────┤
│ Docker      │ ✓ 27.x             │
│ Compose     │ ✓ 2.29.x           │
│ cgroup v2   │ ✓ present          │
└─────────────┴────────────────────┘
All checks passed.
```

The Raspberry Pi profiles run ARM64 images, so on an x86 host install QEMU binfmt support first (or pass `--native` to `up` to keep host-architecture images and apply only the resource limits):

```bash
p4n4-emu setup --arch arm64
```

---

## 3. Inspect Available Profiles

```bash
p4n4-emu profile list
```

Expected output:

```
┌──────────────────────────────────────────────────────────────────┐
│                     Hardware profiles                            │
├───────────┬───────┬────────────┬───────────────┬────────────────┤
│ Name      │ CPUs  │ Memory     │ Disk R/W      │ Arch           │
├───────────┼───────┼────────────┼───────────────┼────────────────┤
│ rpi4      │ 4     │ 3584 MB    │ 50 MB/s       │ arm64          │
│ rpi5      │ 4     │ 7168 MB    │ 100 MB/s      │ arm64          │
│ nuc       │ 4     │ 14336 MB   │ 200 MB/s      │ x86_64         │
│ mcu-class │ 1     │ 256 MB     │ 10 MB/s       │ x86_64         │
└───────────┴───────┴────────────┴───────────────┴────────────────┘
```

Inspect a specific profile:

```bash
p4n4-emu profile show rpi5
```

---

## 4. Start the Emulated Stack

Point `--stack-dir` at the directory that contains your compose file (the p4n4 IoT stack). The emulator generates a Compose overlay and applies it on top.

```bash
p4n4-emu up \
  --profile rpi5 \
  --stack iot \
  --stack-dir ~/p4n4/stacks/iot \
  --sim
```

If you run the command inside a project scaffolded by `p4n4 init`, you can drop both
`--stack` and `--stack-dir`: the emulator finds the project's `.p4n4.json`, targets its
enabled stacks, and resolves each stack's directory for either layout — flat
(single-layer, `docker-compose.yml` at the project root) or multi-layer
(`<project>/iot/`, `<project>/ai/`):

```bash
cd ~/projects/my-p4n4-project
p4n4-emu up --profile rpi5 --sim
```

What each flag does:

| Flag | Purpose |
|---|---|
| `--profile rpi5` | Apply Raspberry Pi 5 CPU/memory/disk constraints |
| `--stack iot` | Target the IoT stack (MQTT, InfluxDB, Node-RED, Grafana); accepts comma-separated names or `all`; defaults to the project's enabled stacks |
| `--stack-dir` | Path to the directory with the stack's compose file (not needed inside a p4n4 project) |
| `--native` | Run host-architecture images; apply only the resource limits |
| `--sim` | Also start the sensor simulator container (`--sim-interval`, `--sim-devices` or `--sim-scenario` tune it; see the README's *Scenarios and faults*) |

Use `--dry-run` to preview the generated overlay without starting anything:

```bash
p4n4-emu up --dry-run --profile rpi5 --stack iot --stack-dir ~/p4n4/stacks/iot
```

Expected output (dry-run):

```
--- Generated overlay: iot.emu.yml ---
services:
  mqtt:
    platform: linux/arm64
    deploy:
      resources:
        limits:
          cpus: "0.40"
          memory: "358m"
    memswap_limit: "358m"

  influxdb:
    platform: linux/arm64
    deploy:
      resources:
        limits:
          cpus: "1.60"
          memory: "2508m"
    memswap_limit: "2508m"
  ...
Dry run — no containers started.
```

---

## 5. Verify the Stack is Running

```bash
p4n4-emu status --stack iot --stack-dir ~/p4n4/stacks/iot
```

`status` reads the profile from the overlay `up` wrote, so it needs no `--profile`.
After the profile summary, it lists each service with its CPU and memory use against
its limits:

```
                                 iot stack — rpi5
┏━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━━━━━━┓
┃ Service  ┃ Status  ┃ Health  ┃ CPU (cores) ┃            Memory ┃ Limits  ┃ Ports         ┃
┡━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━━━━━━┩
│ grafana  │ running │ healthy │ 0.01 / 0.60 │  98 MiB / 716 MiB │ applied │ 3000→3000/tcp │
│ influxdb │ running │ healthy │ 0.05 / 1.60 │ 131 MiB / 2.4 GiB │ applied │ 8086→8086/tcp │
│ mqtt     │ running │ healthy │ 0.00 / 0.40 │ 4.5 MiB / 358 MiB │ applied │ 1883→1883/tcp │
│ node-red │ running │ healthy │ 0.02 / 1.20 │  87 MiB / 1.0 GiB │ applied │ 1880→1880/tcp │
└──────────┴─────────┴─────────┴─────────────┴───────────────────┴─────────┴───────────────┘
```

Usage at 90% of a limit is highlighted. **stale** in the Limits column means the container
doesn't have the limits its overlay asks for (it was created before the overlay changed):
run `p4n4-emu up` again to recreate it.

Check that MQTT messages are flowing:

```bash
docker exec -it iot-mqtt-1 mosquitto_sub -t 'sensors/#' -v
```

Expected output (one line every 2 seconds):

```
sensors/emu-sensor-0/temperature {"value": 23.4, "unit": "C"}
sensors/emu-sensor-0/humidity    {"value": 58.2, "unit": "%"}
sensors/emu-sensor-0/pressure    {"value": 1012.7, "unit": "hPa"}
sensors/emu-sensor-0/raw         {"values": [0.01, -0.02, 1.00], "cpu_pct": 42.3}
```

---

## 6. Simulate LED Toggle from a Sensor Threshold

This script subscribes to the `sensors/emu-sensor-0/temperature` MQTT topic and uses the GPIO stub to toggle a virtual LED (pin 17) whenever the temperature crosses a threshold.

Create the file `led_threshold.py` anywhere on your workstation:

```python
"""
LED threshold demo — no physical hardware required.

Subscribes to sensors/emu-sensor-0/temperature on the emulated MQTT broker.
Toggles a simulated GPIO LED (pin 17) based on a temperature threshold.
"""

import json
import sys
import types

import paho.mqtt.client as mqtt

# ── Inject the GPIO stub before any RPi imports ──────────────────────────────
rpi_mod = types.ModuleType("RPi")
sys.modules["RPi"] = rpi_mod
from p4n4_emu.hw import gpio_stub as GPIO  # noqa: E402
sys.modules["RPi.GPIO"] = GPIO

# ── Configuration ─────────────────────────────────────────────────────────────
MQTT_HOST      = "localhost"
MQTT_PORT      = 1883
TOPIC          = "sensors/emu-sensor-0/temperature"
LED_PIN        = 17          # BCM numbering
THRESHOLD_C    = 25.0        # °C — LED turns ON above this value
# ─────────────────────────────────────────────────────────────────────────────

GPIO.setmode(GPIO.BCM)
GPIO.setup(LED_PIN, GPIO.OUT)
GPIO.output(LED_PIN, GPIO.LOW)

led_state = False


def on_connect(client, userdata, flags, reason_code, properties):
    print(f"[MQTT] Connected (rc={reason_code})")
    client.subscribe(TOPIC)
    print(f"[MQTT] Subscribed to {TOPIC!r}")
    print(f"[LED ] Pin {LED_PIN} | Threshold {THRESHOLD_C} °C | initial state: OFF\n")


def on_message(client, userdata, msg):
    global led_state

    try:
        payload = json.loads(msg.payload)
        temp = float(payload["value"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        print(f"[WARN] Bad payload: {exc}")
        return

    new_state = temp > THRESHOLD_C
    symbol    = "↑ ABOVE" if new_state else "↓ BELOW"
    indicator = "ON " if new_state else "OFF"

    if new_state != led_state:
        GPIO.output(LED_PIN, GPIO.HIGH if new_state else GPIO.LOW)
        led_state = new_state
        print(f"[TOGGLE] {temp:.1f} °C  {symbol} threshold → LED {indicator}")
    else:
        print(f"[HOLD  ] {temp:.1f} °C  {symbol} threshold   LED {indicator}")


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message

client.connect(MQTT_HOST, MQTT_PORT)
client.loop_forever()
```

Run the script (the emulator stack must already be up):

```bash
uv run python led_threshold.py
```

---

## 7. Expected Results

The simulated temperature generator produces a sine wave centred around **22 °C** with a ±3 °C amplitude and a 300-second period. It also adds Gaussian noise (~0.2 °C σ).

With `THRESHOLD_C = 25.0`:

- **Below threshold** (≤ 25 °C): LED pin 17 is held LOW → LED OFF
- **Above threshold** (> 25 °C): LED pin 17 is driven HIGH → LED ON
- A `[TOGGLE]` line is printed only when the state changes; `[HOLD]` lines confirm steady state.

Sample console output you should see:

```
[MQTT] Connected (rc=0)
[MQTT] Subscribed to 'sensors/emu-sensor-0/temperature'
[LED ] Pin 17 | Threshold 25.0 °C | initial state: OFF

[HOLD  ] 22.8 °C  ↓ BELOW threshold   LED OFF
[HOLD  ] 23.1 °C  ↓ BELOW threshold   LED OFF
[HOLD  ] 24.7 °C  ↓ BELOW threshold   LED OFF
[TOGGLE] 25.3 °C  ↑ ABOVE threshold → LED ON
[HOLD  ] 25.8 °C  ↑ ABOVE threshold   LED ON
[HOLD  ] 24.9 °C  ↓ BELOW threshold   LED OFF   ← crossed back down
[TOGGLE] 24.9 °C  ↓ BELOW threshold → LED OFF
```

The LED pin will cycle ON → OFF roughly once every **~150 seconds** (half the sine period), with small noise-driven jitter near the threshold.

### Adjusting the threshold

| `THRESHOLD_C` | Behaviour |
|---|---|
| `< 19.0` | LED is almost always ON (peak of sine wave rarely dips below) |
| `22.0` | LED toggles frequently — near the sine midpoint, noise causes rapid switching |
| `25.0` | **Default** — clean toggle near the sine peak, ~150 s cycle |
| `> 25.0` | LED spends most time OFF; only brief ON pulses near the sine maximum |

For a hysteresis band to prevent rapid flickering near the threshold, change the comparison:

```python
# Replace the single-line comparison with a hysteresis band (±0.5 °C)
if led_state:
    new_state = temp > (THRESHOLD_C - 0.5)   # stays ON until 0.5 °C below threshold
else:
    new_state = temp > (THRESHOLD_C + 0.5)   # turns ON only 0.5 °C above threshold
```

---

## 8. Tear Down

Stop the sensor simulator:

```bash
p4n4-emu sim stop
```

Stop the emulated stack:

```bash
p4n4-emu down --stack iot --stack-dir ~/p4n4/stacks/iot
```

To also remove persistent volumes (InfluxDB data, Grafana dashboards):

```bash
p4n4-emu down --stack iot --stack-dir ~/p4n4/stacks/iot --volumes
```

---

## Quick Reference

```
# First-time setup
uv tool install --editable .
p4n4-emu setup [--arch arm64]

# Inspect profiles
p4n4-emu profile list
p4n4-emu profile show <name>
p4n4-emu profile switch <name>   # new CPU / memory limits, no restart

# Start / stop
p4n4-emu up   --profile <profile> --stack <stack> --stack-dir <path> [--sim]
p4n4-emu down --stack <stack> --stack-dir <path> [--volumes]

# Status
p4n4-emu status --stack <stack> --stack-dir <path>

# Logs (one stack when following; --no-follow prints every stack)
p4n4-emu logs [SERVICE] --stack <stack> [--tail 100] [--no-follow]

# Simulator only
p4n4-emu sim start [--interval 2.0] [--devices 1 | --scenario FILE] [--mqtt-host HOST] [--network NAME]
                   [--username USER] [--tls] [--ca-file PATH] [--qos 0|1|2] [--retain]
p4n4-emu sim check FILE   # validate a scenario (devices, measurements, faults)
p4n4-emu sim stop
p4n4-emu sim status

# LED threshold demo
uv run python led_threshold.py
```
