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
| cgroup v2 | — | `cat /sys/fs/cgroup/cgroup.controllers` (must exist) |

Install `uv` if missing:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## 1. Clone and Install

```bash
git clone https://github.com/raisga/p4n4-emu.git
cd p4n4-emu
uv sync
```

`uv sync` creates a virtual environment and installs all Python dependencies from `uv.lock`.

Verify the CLI is available:

```bash
uv run p4n4-emu --help
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
uv run p4n4-emu setup
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

If you want to test ARM64 images (Raspberry Pi profiles) on an x86 host, add the `--arch arm64` flag to install QEMU binfmt support:

```bash
uv run p4n4-emu setup --arch arm64
```

---

## 3. Inspect Available Profiles

```bash
uv run p4n4-emu profile list
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
uv run p4n4-emu profile show rpi5
```

---

## 4. Start the Emulated Stack

Point `--stack-dir` at the directory that contains your `docker-compose.yml` (the p4n4 IoT stack). The emulator generates a Compose overlay and applies it on top.

```bash
uv run p4n4-emu up \
  --profile rpi5 \
  --stack iot \
  --stack-dir ~/p4n4/docker/iot \
  --sim
```

What each flag does:

| Flag | Purpose |
|---|---|
| `--profile rpi5` | Apply Raspberry Pi 5 CPU/memory/disk constraints |
| `--stack iot` | Target the IoT stack (MQTT, InfluxDB, Node-RED, Grafana) |
| `--stack-dir` | Path to the directory with the base `docker-compose.yml` |
| `--sim` | Also start the sensor simulator container |

Use `--dry-run` to preview the generated overlay without starting anything:

```bash
uv run p4n4-emu up --dry-run --profile rpi5 --stack iot --stack-dir ~/p4n4/docker/iot
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
  influxdb:
    platform: linux/arm64
    deploy:
      resources:
        limits:
          cpus: "1.60"
          memory: "2867m"
  ...
Dry run — no containers started.
```

---

## 5. Verify the Stack is Running

```bash
uv run p4n4-emu status --profile rpi5 --stack iot --stack-dir ~/p4n4/docker/iot
```

Expected output:

```
Profile: rpi5  CPUs: 4  Memory: 7168 MB  Disk R/W: 100 MB/s
┌─────────────────────────────────────────────────────────────┐
│ Container              │ Status    │ Health                  │
├─────────────────────────┼───────────┼─────────────────────────┤
│ iot-mqtt-1             │ running   │ healthy                 │
│ iot-influxdb-1         │ running   │ healthy                 │
│ iot-node-red-1         │ running   │ healthy                 │
│ iot-grafana-1          │ running   │ healthy                 │
│ p4n4-sensor-sim        │ running   │ —                       │
└─────────────────────────┴───────────┴─────────────────────────┘
```

Check that MQTT messages are flowing:

```bash
docker exec -it iot-mqtt-1 mosquitto_sub -t 'sensors/#' -v
```

Expected output (one line every 2 seconds):

```
sensors/temperature {"value": 23.4, "unit": "C", "device": "emu-sensor-0"}
sensors/humidity    {"value": 58.2, "unit": "%", "device": "emu-sensor-0"}
sensors/pressure    {"value": 1012.7, "unit": "hPa", "device": "emu-sensor-0"}
sensors/raw         {"values": [0.01, -0.02, 1.00], "cpu_pct": 42.3, "device": "emu-sensor-0"}
```

---

## 6. Simulate LED Toggle from a Sensor Threshold

This script subscribes to the `sensors/temperature` MQTT topic and uses the GPIO stub to toggle a virtual LED (pin 17) whenever the temperature crosses a threshold.

Create the file `led_threshold.py` anywhere on your workstation:

```python
"""
LED threshold demo — no physical hardware required.

Subscribes to sensors/temperature on the emulated MQTT broker.
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
TOPIC          = "sensors/temperature"
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
[MQTT] Subscribed to 'sensors/temperature'
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
uv run p4n4-emu sim stop
```

Stop the emulated stack:

```bash
uv run p4n4-emu down --profile rpi5 --stack iot --stack-dir ~/p4n4/docker/iot
```

To also remove persistent volumes (InfluxDB data, Grafana dashboards):

```bash
uv run p4n4-emu down --profile rpi5 --stack iot --stack-dir ~/p4n4/docker/iot --volumes
```

---

## Quick Reference

```
# First-time setup
uv sync
uv run p4n4-emu setup [--arch arm64]

# Inspect profiles
uv run p4n4-emu profile list
uv run p4n4-emu profile show <name>

# Start / stop
uv run p4n4-emu up   --profile <profile> --stack <stack> --stack-dir <path> [--sim]
uv run p4n4-emu down --profile <profile> --stack <stack> --stack-dir <path> [--volumes]

# Status
uv run p4n4-emu status --profile <profile> --stack <stack> --stack-dir <path>

# Simulator only
uv run p4n4-emu sim start [--interval 2.0] [--devices 1] [--mqtt-host p4n4-mqtt]
uv run p4n4-emu sim stop
uv run p4n4-emu sim status

# LED threshold demo
uv run python led_threshold.py
```
