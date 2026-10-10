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
| `rpi-zero2w` | 4 cores | 384 MB | 22 MB/s | arm64 |
| `rpi3` | 4 cores | 768 MB | 22 MB/s | arm64 |
| `rpi4` | 4 cores | 3.5 GB | 50 MB/s | arm64 |
| `rpi5` | 4 cores | 7 GB | 100 MB/s | arm64 |
| `jetson-orin-nano` | 6 cores | 7 GB | 500 MB/s | arm64 + NVIDIA GPU |
| `mcu-class` | 1 core | 256 MB | 10 MB/s | x86_64 |
| `nuc` | 4 cores | 14 GB | 200 MB/s | x86_64 |

See [How faithful the emulation is](#how-faithful-the-emulation-is) for what each limit
does and doesn't reproduce, and where the numbers come from. `mcu-class` is not a
microcontroller: it's a Linux container squeezed to 256 MB, the floor of the range.

**GPU.** With `jetson-orin-nano`, `ollama` and `ei-runner` reserve one NVIDIA GPU
(`deploy.resources.reservations.devices`). That needs the NVIDIA runtime on the host
(nvidia-container-toolkit) and host-architecture images (`--native`): an arm64 image under
QEMU can't use an x86 host's GPU. Without both, or with `--no-gpu`, `up` warns and those
services run on the CPU only.

**Your own profiles.** Put a profile file in `~/.p4n4-emu/profiles/<name>.yml`, or in a
p4n4 project's `.p4n4-emu/profiles/`, and use it by name. A file with a built-in's name
replaces it, and a project's replaces yours. `p4n4-emu profile validate` checks every file
(or the names and paths you give it), and `profile list` shows where each one comes from:

```yaml
# ~/.p4n4-emu/profiles/rock5b.yml
description: "Radxa ROCK 5B — 16 GB"
arch: arm64                 # x86_64 (default), arm64, armv7
cpus: 8
memory: 15g
memory_swap: 15g            # memory plus swap (default: memory, so no swap)
blkio_weight: 500           # 10–1000 (default 500)
blkio_read_bps: 400m        # bytes/s, or a size per second
blkio_write_bps: 300m
# board: rpi5               # a GPIO header for `p4n4-emu run`: rpi3, rpi4, rpi5, rpi-zero2w
# gpu: nvidia               # the AI and edge services reserve a GPU
```

Unknown keys, missing ones (`description`, `cpus`, `memory`, the two `blkio_*_bps`) and
sizes that don't parse are errors that name the file and the key.

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

Once it's on PyPI: `uv tool install p4n4-emu` (or `pipx install p4n4-emu`).

The sensor simulator runs from `ghcr.io/raisga/p4n4-sensor-sim:<version>` (amd64 and
arm64), pulled the first time it starts. Without network access, or for a version that
isn't published, it's built from the installed package instead; `sim start --rebuild`
builds it on purpose, e.g. after editing the simulator. `P4N4_EMU_SIM_IMAGE` names
another image.

To work on p4n4-emu itself, `uv sync` and `uv run p4n4-emu` also work, but only inside
`tools/emu`: run from a p4n4 project, `uv run` doesn't find the command.

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

All checks should show **OK**. A `cgroup v2` warning means disk limits only throttle direct I/O (CPU and memory limits still apply); see [How faithful the emulation is](#how-faithful-the-emulation-is).

### 4. (Optional) Enable ARM64 emulation

Required to run the ARM profiles (`rpi3`, `rpi4`, `rpi5`, `rpi-zero2w`, `jetson-orin-nano`) on an x86 host. `up` runs their arm64 images by default and refuses to start until QEMU is registered; pass `--native` to apply only the resource limits with host-architecture images:

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
               [--stack-dir PATH] [--arch arm64|armv7|x86_64 | --native] [--no-gpu]
               [--sim] [--sim-interval 2.0] [--sim-devices 1] [--sim-scenario FILE]
               [--sim-replay FILE]
               [--build] [--pull] [--dry-run]
p4n4-emu down  [--stack iot|ai|edge|dashboard|iot,ai|all]
               [--stack-dir PATH] [--volumes] [--yes]
p4n4-emu status [--profile rpi5] [--stack iot|ai|edge|dashboard|iot,ai|all] [--json]
p4n4-emu logs  [SERVICE] [--stack iot|ai|edge|dashboard|iot,ai|all]
               [--stack-dir PATH] [--tail 100] [--no-follow]
p4n4-emu profile list [--json]
p4n4-emu profile show <name> [--json]
p4n4-emu profile validate [NAME|FILE ...] [--json]
p4n4-emu profile switch <name> [--stack ...] [--dry-run]
p4n4-emu sim start [--interval 2.0] [--devices 1 | --scenario FILE]
                   [--mqtt-host HOST] [--network NAME] [--port PORT]
                   [--username USER] [--tls] [--ca-file PATH] [--cert-file PATH --key-file PATH]
                   [--qos 0|1|2] [--retain] [--replay FILE [--speed 1] [--loop]] [--rebuild]
p4n4-emu sim check FILE   # a scenario, or a recording (.csv)
p4n4-emu sim stop
p4n4-emu sim status
p4n4-emu run   [--profile rpi5] [--device ID] [--scenario FILE] [--gpio-mqtt HOST[:PORT]]
               [--gpio-prefix emu/gpio] [--hardware FILE | --no-parts] [--gpiod v2|v1]
               [--python PATH] SCRIPT [ARGS...]
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

## Hardware stubs

`p4n4-emu run` runs a Python script against an emulated Raspberry Pi: the hardware
libraries it imports are replaced by stubs, so scripts like `p4n4_button_handler.py` run
unmodified on any workstation:

```bash
p4n4-emu run p4n4_button_handler.py                  # Pi 5 by default
p4n4-emu run -p rpi4 --gpio-mqtt localhost read_sensors.py --verbose
p4n4-emu run --python .venv/bin/python -- -m my_app  # the script's own virtualenv
```

| Library | What the stub covers |
|---------|----------------------|
| `RPi.GPIO` | As rpi-lgpio (the Pi 5's RPi.GPIO) behaves: `setmode` (BCM / BOARD), `setup`, `input` / `output`, edge detection with `bouncetime`, `wait_for_edge`, `PWM`, `RPI_INFO`, `cleanup`, with the same errors for a missing `setmode()` or `setup()` |
| `lgpio` | Chips, claims, groups, alerts and callbacks (debounce and watchdog included), notification pipes (`notify_*`, a FIFO `.lgd-nfy<handle>` in `$LG_WD` or the working directory), `tx_pwm` / `tx_servo`, and the `i2c_*` / `spi_*` / `serial_*` calls |
| `gpiozero` | The real library, unmodified: on the emulated board it picks its lgpio pin factory, which runs on the `lgpio` stub |
| `gpiod` | The libgpiod v2 API: chips, line info, `request_lines`, active-low, bias, edge events (the request's fd works with `select`). With `--gpiod v1`, the v1 API instead (python3-libgpiod 1.6, Raspberry Pi OS bookworm's package): `Chip` by path, name, label or number, `get_line` / `get_lines` / `find_line`, `request` with flags and default values, `LineBulk`, and edge events (`event_wait`, `event_read`, `event_get_fd`) |
| `smbus2`, `smbus` | Every SMBus call and `i2c_rdwr`, on the emulated I2C buses |
| `spidev` | `SpiDev` transfers on `/dev/spidev0.0` and `0.1` |
| `serial` (pyserial) | `Serial` on `/dev/serial0` and `/dev/ttyAMA0` |

All of them share one set of pins and buses: a pin set by `RPi.GPIO` reads the same through
`gpiod`. Like on a Pi, I2C bus 1 exists and a missing address raises `OSError` 121
(Remote I/O error), SPI reads zeros with nothing attached, and a serial read waits for its
timeout. The profile's board (`rpi3`, `rpi4`, `rpi5`, `rpi-zero2w`) gives the revision in
`/proc/device-tree` (what `RPI_INFO` and gpiozero read), the gpiochips (the Pi 5 has
`gpiochip0`, and `gpiochip4` linked to it) and the `/dev` nodes; `run` serves those files
inside the script's process only.

**Sensors.** The I2C bus has a BME280 (0x76), an MPU-6050 (0x68) and an ADS1115 (0x48), SPI
0.0 an MCP3008, and 1-Wire a DS18B20 (`/sys/bus/w1/devices/28-*/w1_slave`). They're
register-level models: a driver reads the chip id, the calibration data and the raw ADC
counts, and gets the values back through the part's own formulas. Their readings follow the
sensor simulator's curves for one device (`--device`, default `emu-sensor-0`; `--scenario`
for a scenario's waves), so a script on the bus reads what the simulator publishes on
`sensors/<device>/…`. The ADS1115 and MCP3008 inputs are the device's `a0`–`a3` and
`ch0`–`ch7` measurements when the scenario defines them. `--no-parts` leaves the buses empty.

**Your own board.** `--hardware FILE` replaces the default parts with a layout:

```yaml
parts:
  - bme280                                   # I2C bus 1, address 0x76
  - {part: bme280, address: 0x77}            # a second one, SDO strapped high
  - {part: ads1115, address: 0x49, measurements: {a0: soil_moisture, a1: light}}
  - {part: mcp3008, bus: 0, cs: 1, vref: 5.0}
  - {part: mpu6050, bus: 3}                  # adds /dev/i2c-3
  - {part: ds18b20, serial: 0000075a1c2f, measurements: {temperature: water_temp}}
  - {part: loopback, port: /dev/ttyUSB0}     # a serial port wired TX to RX
```

Addresses are the ones the real part can be strapped to (BME280 0x76/0x77, MPU-6050
0x68/0x69, ADS1115 0x48–0x4B). `measurements` says which measurement of the device feeds
each input of the part; an input you don't map reads the measurement of its own name. A
mapped measurement must be one the device has (in the scenario, or a built-in), and every
error names the entry and key at fault.

**Faults.** A scenario's faults reach the parts too. `spike`, `stuck` and `drift` change
what they read. A `dropout` takes the part off the bus for its duration: an I2C transfer
fails with `OSError` 121, SPI reads zeros, and a DS18B20's `w1_slave` fails its CRC (`NO`)
while `temperature` fails with `EIO`. A device-wide dropout takes every part off. `delay`
and `malformed` concern MQTT payloads only, so the hardware path ignores them.

**PWM.** A PWM output toggles: a read returns high for the duty cycle's part of each
period. Up to 10 Hz each toggle is also a change on `emu/gpio/<pin>/state` and to edge
detection (a blinking LED). Faster PWM only shows in what a read returns, so a dimmed LED
or a buzzer doesn't flood the broker.

**Driving pins.** Nothing outside the script drives an input, so press a button over MQTT
with `--gpio-mqtt HOST[:PORT]`:

```bash
mosquitto_pub -t emu/gpio/27/set -m 0         # press the button on GPIO 27 (pulled up)
mosquitto_pub -t emu/gpio/27/set -m 1         # release it ("release" stops driving it)
mosquitto_sub -t 'emu/gpio/+/state' -v        # outputs and inputs, on every change
```

Pins are BCM numbers in topics; `emu/gpio/<pin>/pwm` carries a PWM output's frequency and
duty cycle. In tests, drive pins directly:

```python
from p4n4_emu.hw import buses, pins, shims
shims.install(board="rpi5")       # what `run` does: stubs, parts, board files
import RPi.GPIO as GPIO           # the stub
pins.drive(27, 0)                 # press; RPi.GPIO's GPIO.set_input(27, GPIO.LOW) does the same
buses.uart("/dev/serial0").feed(b"$GPGGA,...\r\n")   # bytes arriving on the serial port
```

`buses.attach_i2c(bus, address, part)` (and `attach_spi`, `attach_uart`) add parts
`--hardware` doesn't know; `p4n4_emu.hw.devices` has the models.

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

### Scenarios and faults

A scenario file replaces the default devices: which devices exist, what each one measures,
how often, and how it misbehaves. [`examples/sim-scenario.yml`](examples/sim-scenario.yml)
uses every option:

```yaml
interval: 2          # seconds between readings, for devices without their own
qos: 1               # QoS of every publish
retain: false
timestamp: true      # add "ts" (epoch ms, when the reading was taken)
seed: 42             # the same faults fire at the same readings every run
devices:
  - id: emu-room-{n}             # emu-room-0 … emu-room-2
    count: 3
    measurements: [temperature, humidity]
  - id: iot-device-001
    interval: 5
    measurements:
      temperature: {base: 40, amplitude: 8}             # a built-in, changed
      vibration: {base: 0.5, amplitude: 0.2, unit: g, min: 0}
    faults:
      - {type: spike, measurement: vibration, probability: 0.02, magnitude: 2}
      - {type: dropout, probability: 0.005, duration: 30}
```

```bash
p4n4-emu sim check plant.yml               # validate it and list the devices
p4n4-emu sim start --scenario plant.yml    # or: p4n4-emu up --sim --sim-scenario plant.yml
```

Measurements are the built-ins (`temperature`, `humidity`, `pressure`, `raw`), or any other
name with at least a `base`. A wave takes `base`, `amplitude`, `noise` (σ), `period` (s,
default 300), `min`, `max` and `unit`. A fault applies to every measurement of its device,
or to the one it names, and fires with its `probability` (default 1) on each reading:

| Fault | Setting | Effect |
|---|---|---|
| `spike` | `magnitude` | The reading is off by ±magnitude |
| `stuck` | `duration` | The value freezes for that many seconds |
| `drift` | `rate` | The value moves by rate units per second since the simulator started |
| `dropout` | `duration` | The reading isn't published (for `duration` s, if given). Without a `measurement`, the whole device goes quiet |
| `delay` | `seconds` | The reading is published that late, after newer ones (add `timestamp: true` to see it) |
| `malformed` | — | The payload is broken: truncated JSON, not JSON, a string value, or no value |

`--interval` overrides the scenario's `interval` (not a device's own), and `--qos` /
`--retain` override its settings. `--devices` and a scenario don't mix. Files a scenario
names (a WAV file, a recording) are relative to the scenario's folder; `sim start` mounts
each one into the container. [`examples/sim-edge.yml`](examples/sim-edge.yml) uses the
three features below.

### Camera and audio feeds

A measurement with a `kind` is a media feed for the edge runner. Each reading is one frame
or one audio window, published as `{"values": [...]}`, the feature vector `ei-runner`
classifies. Point the edge stack's `MQTT_TOPIC_INPUT` at it (`sensors/+/camera`). Node-RED
stores nothing of these payloads, since InfluxDB gets no arrays.

```yaml
measurements:
  camera: {kind: image, width: 96, height: 96}           # 9216 values, one per pixel
  thermal: {kind: image, width: 32, height: 24, channels: 1, encoding: float}
  mic: {kind: audio, sample_rate: 16000, window: 1.0, frequency: 880, noise: 0.1}
  door: {kind: audio, file: clips/door.wav, window: 0.5}  # windows of a WAV file, looped
```

| Kind | Settings | Values |
|---|---|---|
| `image` | `width`, `height` (96), `channels` (1 or 3), `noise` (0–1), `encoding` | A bright blob drifting over a noisy background. `packed` (default): one `0xRRGGBB` number per pixel, what Edge Impulse image models take; `uint8`: 0–255 per channel; `float`: 0–1 per channel |
| `audio` | `sample_rate` (16000), `window` (s, 1.0), `frequency` (440), `amplitude`, `noise` (0–1 of full scale), `file`, `encoding` | A tone plus noise, continuous across windows, or consecutive windows of a PCM WAV file (8–32-bit, channels mixed down, the file's own rate). `int16` (default), what Edge Impulse audio models take, or `float` (−1 to 1) |

`sim check` shows each feed's size and warns above 65 536 values, the runner's default
`MAX_FEATURES`. A feed takes `dropout`, `delay` and `malformed` faults; `spike`, `stuck`
and `drift` need a number, so they don't apply.

### Firmware-like devices

`mcu:` makes a device behave like ESP32 firmware rather than a perfect publisher:

```yaml
- id: esp32-greenhouse
  measurements: [temperature, humidity]
  mcu:
    type: esp32            # registered as (default esp32)
    firmware: 1.4.2        # (default p4n4-emu)
    location: greenhouse
    boot: 3                # seconds from power-on or wake-up to the first reading (2)
    sleep: 300             # deep sleep: wake, connect, publish one round, sleep again
    wifi_drop: {probability: 0.02, duration: 45}   # rolled each round (each wake-up)
    buffer: 20             # readings kept while offline, sent on reconnect (0: lost)
    availability: true     # devices/<id>/availability (default true)
```

Each such device has its own MQTT connection (client id = the device id, MQTT 5) and:

- **registers** on `devices/<id>/register` with `{"type", "firmware", "location"}` on every
  connection until `devices/<id>/status` answers `{"status": "registered"}`. That's the ai
  stack's n8n onboarding flow (`device-onboarding.json`). A sleeping device stays awake up
  to 2 s for the answer.
- **announces availability** on `devices/<id>/availability`, retained: `online` once
  connected, `sleeping` before a deep sleep, and `offline` as its will, which the broker
  sends when the connection is lost. This topic is p4n4-emu's own; no stack reads it yet.
- **loses Wi-Fi**: a drop disconnects with the will (MQTT 5's "disconnect with will
  message"), so subscribers see `offline` as for a real loss. It reconnects after
  `duration`, then waits `boot` again. A sleeping device that fails to join stays offline
  for that wake-up.

With the hardened broker config, the ACL must let the device's account write
`devices/<id>/#` too, not only `sensors/<id>/+`.

### Replaying recordings

`replay:` (or `sim start --replay FILE [--speed N] [--loop]`, `up --sim --sim-replay FILE`)
publishes recorded readings again, with the gaps they were recorded with:

```yaml
replay:
  - {file: morning.csv, speed: 10, loop: true, devices: {greenhouse-01: replayed-01}}
```

| Format | Header | Notes |
|---|---|---|
| CSV | `time,device,measurement,value[,unit][,…]` | One reading per row; every other column goes into the payload, numbers as numbers, empty cells left out. `time` is ISO 8601 or epoch s / ms / µs / ns; `timestamp` / `ts` and `sensor` are accepted too |
| InfluxDB export | annotated CSV from `influx query --raw` or the Data Explorer | p4n4's own data: rows of `sensor_data` with the `device` / `sensor` tags become `{"value", "unit", …}` again; other measurements are skipped. Arrays never reach InfluxDB, so there's no accelerometer `values` to replay |

`speed: 0` publishes everything at once. Without `loop`, a replay ends; a simulator
running only replays then stops. `sim check FILE.csv` reads a recording on its own and
lists its devices, readings and time span. With `timestamp: true`, `ts` is the time the
reading is published again, not when it was recorded.

### Broker login and TLS

The iot stack's broker allows anonymous clients. Against a hardened one, log in as a device
account and name the device after it, since the ACL only lets `iot-device-001` write
`sensors/iot-device-001/+`:

```bash
export MQTT_PASSWORD=...                    # read by --password; stays out of shell history
p4n4-emu sim start --scenario plant.yml --username iot-device-001
p4n4-emu sim start --username iot-device-001 --ca-file certs/ca.crt   # TLS, port 8883
```

`--tls` verifies the broker against the system CAs, `--ca-file` against your own CA, and
`--cert-file` / `--key-file` add a client certificate. The files are mounted read-only
into the container. The password reaches it through `docker run`'s environment, so it
isn't on a command line, but `docker inspect p4n4-sensor-sim` shows it. Run standalone,
the simulator reads `MQTT_USERNAME`, `MQTT_PASSWORD`, `MQTT_TLS`, `MQTT_CA_FILE`,
`MQTT_CERT_FILE`, `MQTT_KEY_FILE`, `SIM_SCENARIO`, `SIM_QOS` and `SIM_RETAIN` (see
`p4n4_emu/sim/sensor_sim.py`).

All devices share the simulator's one connection, so they all log in as the same user. To
simulate several devices against per-device ACLs, run one simulator per account or use a
`pattern write sensors/%u/+` ACL rule.

## How faithful the emulation is

p4n4-emu reproduces a board's *resource ceilings* and *instruction set*, not its speed. Use
it to find what doesn't fit (a service killed for memory, a stack that can't start, a
dashboard that stalls on disk), not to predict latency or throughput on the real device.

| What | How it's applied | Enforced | Approximated, or not at all |
|------|------------------|----------|-----------------------------|
| CPU | `deploy.resources.limits.cpus` → CFS quota (`cpu.max`) per container | Yes, cgroup v1 or v2 | It caps CPU *time*, not core *speed*: four host cores are much faster than four Cortex-A72 / A76 cores (TODO: performance factor). Each service gets a share and can't borrow what the others leave idle, as it could on a real board (TODO: shared slice). |
| Memory | `limits.memory` → `memory.max`; over it, the kernel OOM-kills the container | Yes, cgroup v1 or v2 | Page cache counts towards the limit, as on the board. The OS's own share is left out of the profile (see below), not simulated. |
| Swap | `memswap_limit` = memory: no swap | Yes on cgroup v2; on v1 only with swap accounting (`swapaccount=1`) | A board configured with swap has some; the profiles assume none. |
| Disk bandwidth | `blkio_config` → `io.max` read/write bytes per second, on the whole disk that holds Docker's data root | Yes for direct and buffered I/O on cgroup v2; direct I/O only on v1 | Bandwidth only: no IOPS or latency limit, so small random writes are far faster than on an SD card (TODO). Reads served from page cache aren't throttled. Volumes or bind mounts on another disk aren't limited. Skipped when no block device is found (tmpfs, NFS, some btrfs / LVM setups, Docker Desktop). `profile switch` can't change it on running containers. |
| Disk priority | `blkio_config.weight` → `io.bfq.weight` | Only with the BFQ I/O scheduler | Has no effect on hosts using `mq-deadline` or `none`, the usual default for NVMe. |
| Architecture | `platform: linux/arm64` with QEMU user-mode emulation | Yes: the arm64 images and binaries run | Speed: QEMU is roughly 3–10x slower, so timings mean nothing and LLM inference (Ollama) is impractical. Kernel features come from the host kernel, not the board's. |
| GPIO, I2C, SPI, UART, 1-Wire | `p4n4-emu run`: stubs for the libraries a script imports, and register-level sensor models | In the script's process | Logic levels and register contents, not electrical behaviour or timing: transfers are instant, PWM toggles in software time (not to the microsecond), and a sensor reads the simulator's curve. Containers don't see these devices. |
| GPU (`jetson-orin-nano`) | `deploy.resources.reservations.devices`: one NVIDIA GPU for `ollama` and `ei-runner` | Only with the host's NVIDIA runtime and `--native` images | It's the host's GPU, with its own memory and speed: the Jetson's GPU shares its 8 GB with the CPU, which the profile can't reproduce. |
| Storage size, network, Coral / Hailo accelerators, temperature | — | No | No capacity limit, no network shaping (TODO: `tc netem`), no accelerator stub, no thermal throttling. |

On Docker Desktop the limits apply inside its Linux VM, and the VM's own CPU and memory
settings cap everything on top of them.

### How the profile numbers were chosen

| Profile | Board | CPU | Memory | Disk (read = write) |
|---------|-------|-----|--------|------|
| `rpi-zero2w` | Raspberry Pi Zero 2 W, 512 MB | 4 cores (Cortex-A53) | 384 MB: 512 MB minus the GPU's and the OS's share | 22 MB/s, the SD interface's ceiling |
| `rpi3` | Raspberry Pi 3 Model B, 1 GB | 4 cores (Cortex-A53) | 768 MB: 1 GB minus the GPU's and the OS's share | 22 MB/s, the SD interface's ceiling |
| `rpi4` | Raspberry Pi 4, 4 GB | 4 cores (Cortex-A72) | 3.5 GB: 4 GB minus 512 MB for the OS | 50 MB/s, an SD card |
| `rpi5` | Raspberry Pi 5, 8 GB | 4 cores (Cortex-A76) | 7 GB: 8 GB minus 1 GB for the OS | 100 MB/s, an SD card on the Pi 5's faster slot |
| `jetson-orin-nano` | NVIDIA Jetson Orin Nano, 8 GB | 6 cores (Cortex-A78AE) | 7 GB: 8 GB minus 1 GB for the OS | 500 MB/s, an NVMe SSD |
| `nuc` | Intel NUC, 16 GB | 4 cores | 14 GB: 16 GB minus 2 GB for the OS | 200 MB/s, a conservative SSD |
| `mcu-class` | none: a floor for very small Linux devices | 1 core | 256 MB | 10 MB/s |

These are round estimates for each class of board, not measurements; the repo holds no
benchmark behind them. Two known biases: real SD cards write much slower than they read,
while the profiles use one rate for both; and the CPU count says nothing about per-core
speed. `mcu-class` is a small x86 Linux container, not a microcontroller: with 256 MB, the
iot stack's Node-RED gets 38 MiB and InfluxDB 89 MiB (InfluxDB alone used 137 MiB on a
workstation), so expect them to be OOM-killed; `rpi-zero2w` is close to that. The Pi 3
and Zero 2 W profiles assume 64-bit Raspberry Pi OS: for the 32-bit OS pass `--arch armv7`,
though few of the stacks' images (InfluxDB 2 among the missing) publish armv7. To match
your own hardware, measure it (`sysbench cpu`, `fio`, `free -m`) and write a profile of
your own (see [Hardware profiles](#hardware-profiles)).

## License

MIT — see [LICENSE](LICENSE).