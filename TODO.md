# TODO — p4n4-emu

Pending work to turn `p4n4-emu` from a resource-limit overlay into an emulator that
behaves like the edge hardware p4n4 runs on. Written 2026-10-01 against `87a896c`
plus the uncommitted topic-scheme change (`sensors/<device-id>/<measurement>`).

**What works today:** profile loading (`rpi4`, `rpi5`, `nuc`, `mcu-class`), per-stack
Compose overlays (CPU / memory / blkio), p4n4 project discovery (flat and multi-layer),
QEMU binfmt setup, the MQTT sensor simulator, and the `RPi.GPIO` / I2C / SPI / UART stubs.
The 89 unit tests pass.

Priorities: **P0** gives wrong results or breaks a run · **P1** needed for realistic
emulation · **P2** quality-of-life · **P3** stretch.

---

## 0. Housekeeping (do first)

- [x] Commit the working-tree change that moves the simulator to the spec topic scheme
      (`README.md`, `guide.md`, `sensor_sim.py`, `tests/test_sim.py`) and `.gitattributes`.
      The root `TODO.md` task 6 covers pushing.
- [x] Fix the license mismatch: `pyproject.toml` now says MIT, like `LICENSE` and the README.
- [x] Fix the install path in the README (`~/p4n4/demo/emu` → `~/p4n4/tools/emu`). The docs
      now install with `uv tool install --editable .`: `uv run p4n4-emu` only works inside
      `tools/emu`, not from a p4n4 project.
- [x] Replace the mirrored layout logic in `p4n4_emu/utils/project.py` with `p4n4-lib`
      (0.2.0, from PyPI): manifest lookup and layer order come from it. Directory
      resolution stays in emu, because `p4n4_lib.layout` only knows `docker-compose.yml`.
      `ensure_network` and the `p4n4-net` subnet move to it once a lib release includes
      `compose.ensure_network` with the recreate-when-unused fix (section 6).

## 1. Correctness bugs (P0) — fixed 2026-10-01

All ten are fixed, with regression tests in `tests/test_commands.py`, `test_compose.py`,
`test_docker_info.py`, `test_overlays.py`, `test_project.py` and `test_sim.py`.
Line numbers below refer to the code before the fix.

- [x] **ARM platform is set without QEMU being checked.** The overlay templates emit
      `platform: linux/arm64` whenever the profile is ARM (`profile.is_arm`), but
      `up` only checks binfmt when `--arch arm64` is passed (`commands/up.py:54`). On an x86 host
      without binfmt, `up --profile rpi5` pulls arm64 images that fail with `exec format error`.
      Decide one rule: either `--arch` controls `platform:` (pass it to `render_overlay`),
      or ARM profiles always run the QEMU preflight. Add a `--native` escape hatch that
      keeps limits but skips the platform override.
- [x] **`up --sim` races itself.** `_start_sim` (`commands/up.py:109`) starts `docker build`
      and `docker run` as two concurrent `Popen`s, so the first run fails with no image.
      It also ignores `--interval` / `--devices`, uses `--rm` (so logs vanish on crash),
      and never waits for `p4n4-mqtt` to be healthy. Delete it and call the shared logic
      in `commands/sim.py`.
- [x] **Overlays hardcode service names.** The templates declare `mqtt`, `influxdb`,
      `node-red`, `grafana`, `ollama`, `letta`, `n8n`, `ei-runner`. If a project removed one,
      Compose fails with "service has neither an image nor a build context". If it added one,
      that service runs unconstrained. Generate the overlay from
      `docker compose config --format json`. Keep the per-service share table as defaults and
      give unknown services a fallback share.
- [x] **Passing `-f` drops `docker-compose.override.yml`.** Compose only auto-loads the
      override file when no `-f` is given. `utils/compose.py:84` and `:110` also hardcode
      `docker-compose.yml`. Detect `compose.yaml` / `compose.yml` / `docker-compose.yaml`,
      honour `COMPOSE_FILE`, and include an existing override file before the emu overlay.
- [x] **Limits are per stack, not per device.** The service shares add up to 0.95 CPU / 0.65 RAM
      (iot), 1.0 / 0.90 (ai) and 0.25 / 0.10 (edge). With `--stack all`, an `rpi5` can use
      8.8 cores and about 11.5 GiB of its 7 GiB, more than the board has. Either normalise the shares across the
      enabled stacks, or put every container under one `cgroup_parent` slice with the profile's
      `cpu.max` / `memory.max` (best fidelity, needs a systemd driver).
- [x] **`memory_swap` is loaded but never rendered.** Emit `memswap_limit` in the overlays.
- [x] **`down` always removes the simulator** (`commands/down.py`), even for `--stack ai`.
      Only remove it when the iot stack, or all stacks, are stopped.
- [x] **The simulator ignores SIGTERM.** It runs as PID 1 with no handler, so `docker stop`
      waits 10 s and then SIGKILLs, skipping the `finally` block (`sim/sensor_sim.py:77`).
      Install a SIGTERM handler, or run it under `--init` / `tini`.
- [x] **`setup --arch arm64` continues after hard errors** (`commands/setup.py:63`). If Docker
      is missing, it still tries to install binfmt. The "Try running with sudo" hint at `:88`
      is also misleading, because the install runs through a privileged container.
- [x] **Mount matching uses a plain prefix check.** `utils/docker_info.py:39` matches
      `/home` against `/homework`. Compare path components instead.

## 2. Emulation fidelity (P1)

### CPU and memory
- [ ] Add a per-profile **CPU performance factor**. Four desktop cores are much faster than
      four Cortex-A72 (Pi 4) or A76 (Pi 5) cores, so scale `cpus` by a factor taken from a
      reference benchmark (for example `sysbench cpu`). Add a `p4n4-emu calibrate` command
      that measures the host.
- [ ] Replace budget scaling with a shared `cgroup_parent` slice. Scaling (section 1) keeps
      the summed per-service limits within the device, but it also stops one service from
      bursting into CPU the others leave idle, which a real board allows. A parent slice
      with the profile's `cpu.max` / `memory.max` gives both. It needs root or a systemd
      user slice.
- [ ] Optional `cpuset` pinning so a profile maps to real cores instead of a CFS quota.
- [ ] Add a `pids_limit` per profile, and an OOM policy (`oom_score_adj`) that matches what a
      Pi with no swap does.
- [ ] Support **per-service share overrides** in the profile YAML (and in
      `.p4n4.json` / `.p4n4-emu.yml` inside a project), instead of only in Jinja templates.

### Storage
- [ ] Add a storage **capacity** limit per profile (SD card / eMMC size). Use `storage_opt.size`
      where supported (overlay2 on xfs with pquota), or size-capped volumes.
- [ ] Add SD-card-style **write latency / IOPS** limits (`device_read_iops` / `device_write_iops`),
      not only bandwidth.
- [ ] Make block device detection work on btrfs, LVM / dm-crypt and Docker Desktop, or report
      clearly why disk limits were skipped.

### Network
- [ ] **Network shaping** (listed as a known limitation today): latency, jitter, bandwidth and
      packet loss per profile. Use a `NET_ADMIN` sidecar with `tc netem` that shares each
      service's network namespace, or wrap `pumba`.
- [ ] Network **profiles / scenarios**: `lan`, `wifi-weak`, `lte`, `offline`. Add a command to
      flip a running stack offline and back, to test the MQTT reconnect and buffering paths.

### Architecture
- [ ] Per-service architecture override, so `ollama` can run natively while the rest emulates
      arm64. LLM inference under QEMU is impractical (see the README's known limitations).
- [ ] Verify which stack images publish arm64 / armv7 variants and warn before `up` when an image
      has no matching platform (`docker manifest inspect`).
- [ ] Add an `armv7` (32-bit) path for older boards; `is_arm` already accepts it, but no profile
      or binfmt install uses it.

### Device behaviour
- [ ] Simulate **thermal throttling**: lower the CPU quota as simulated load stays high
      (`docker update --cpus`), following a simple thermal model per profile.
- [ ] Simulate **power loss and reboots**: kill and restart containers without a clean shutdown
      to test InfluxDB / Node-RED data durability and `restart:` policies.
- [ ] Simulate **clock skew / missing RTC** (Pi boots without network time) using libfaketime
      for chosen services.

## 3. New hardware profiles (P1)

- [ ] `rpi3` / `rpi-zero2w` (armv7 or arm64, 1 GB / 512 MB).
- [ ] `jetson-orin-nano`: GPU passthrough (`deploy.resources.reservations.devices`), and a
      degraded CPU-only mode on hosts without NVIDIA.
- [ ] Accelerator profiles for the edge runner (Coral, Hailo): stub device plus a latency model.
- [ ] Rename or document `mcu-class`. It is a 256 MB x86 Linux container, not an MCU.
      Real MCU nodes belong under section 5.
- [ ] User-defined profiles in `~/.p4n4-emu/profiles/*.yml` and inside the project, with a
      schema check (unknown keys, invalid sizes) and a `profile validate` command.

## 4. Hardware stubs (P1)

- [x] **`gpio_stub.setup` breaks real scripts.** It now takes the full `RPi.GPIO` signature
      (`pull_up_down=`, `initial=`, pin lists). `p4n4_button_handler.py` runs under the stub.
- [x] Edge detection: `add_event_detect` (with `bouncetime`), `add_event_callback`,
      `remove_event_detect`, `event_detected`, plus `getmode` and `gpio_function`. Inputs are
      driven with the stub-only `set_input(pin, level)`.
- [ ] Add the rest of the `RPi.GPIO` API: `PWM`, `wait_for_edge`, `BOARD` ↔ `BCM` mapping,
      `RPI_INFO`.
- [ ] **Pi 5 does not support `RPi.GPIO`.** Add stubs for `gpiozero` (mock pin factory) and
      `lgpio` / `gpiod`, which is what the `rpi5` profile would actually run.
- [ ] Add an external control channel so tests or a UI can drive input pins: MQTT topic
      `emu/gpio/<pin>/set`, with state published on `emu/gpio/<pin>/state`.
- [ ] Make `SimI2C` / `SimSPI` / `SimUART` look like the real libraries (`smbus2.SMBus`, `spidev.SpiDev`,
      `serial.Serial`) and register them as import shims.
- [ ] Replace random bytes with **register-level device models** for common parts
      (BME280, MPU-6050, ADS1115, DS18B20 over 1-Wire), fed by the same waveform generators as
      the MQTT simulator so both paths agree.
- [ ] Add a `p4n4-emu run <script.py>` command that injects every shim (via `sitecustomize`)
      so users stop hand-patching `sys.modules` as the README and guide show.

## 5. Sensor simulator (P1)

- [ ] Configurable devices and measurements from a YAML scenario file (count, ids,
      measurement set, ranges, rates per device), replacing the four hardcoded measurements.
- [x] Per-device phase. Each device's curves are shifted by a phase derived from its id.
- [ ] Fault injection: spikes, stuck values, drift, dropouts, out-of-order and late timestamps,
      malformed payloads. These exercise the Node-RED flows and anomaly detection.
- [ ] Replay mode from a CSV / InfluxDB export.
- [ ] Broker auth and TLS (`MQTT_USERNAME` / `MQTT_PASSWORD` / CA). The iot stack ships
      `allow_anonymous true` today, but the hardened config and `acl.example` only let the device
      account write `sensors/iot-device-001/+`, so `emu-sensor-*` ids would be denied.
- [x] Reconnect with backoff (1 s doubling to 30 s), including when the broker is not up yet.
- [ ] QoS / retain options for the simulator's publishes.
- [x] Read `SIM_DEVICE_COUNT` (and the other settings) when `run()` is called, not at import.
- [ ] Optional simulated **camera / audio feed** for the edge runner, so `ei-runner` can be tested
      without a physical sensor.
- [ ] MCU node emulation: a lightweight "virtual ESP32" container per device, or Renode / Wokwi
      integration, that speaks the same MQTT contract as real firmware.

## 6. CLI and UX (P2)

- [x] Persist the active profile per project, so `down` / `status` / `logs` need no
      `--profile`. Overlays live in one folder per stack directory
      (`overlays/<dir>-<hash>/<stack>.emu.yml`) and record their profile in an `x-p4n4-emu`
      block; `down` deletes them. No separate state file to drift from the overlays.
- [x] Move the duplicated `_OVERLAY_ROOT` (up, down, status) into one module
      (`overlays/paths.py`).
- [x] `status`: show live usage against limits (`docker stats --no-stream`) and confirm the
      limits were actually applied (`docker inspect` → `HostConfig.NanoCpus` / `Memory` /
      `MemorySwap` / `BlkioDevice*Bps`). Stale containers are flagged, and a missing cgroup v2
      gets a warning.
- [x] Add a `logs` command, with the main CLI's `--tail` / `--no-follow` / `--stack` behaviour.
      A service name selects the stack that defines it.
- [x] `profile switch <name>`: re-apply limits to running containers with `docker update`,
      without recreating them. CPU and memory only: `docker update` can't change disk rates
      (status shows them stale until the next `up`) or the image architecture (refused).
- [x] Roll back stacks already started when a later stack in `--stack all` fails. Only the
      stacks this run started (the failed one included) are stopped; ones already running
      stay up. Every stack directory is resolved before any starts.
- [x] Do not hardcode `p4n4-net` / `p4n4-mqtt`. Read them from the project's compose config
      (`utils/stack_config.py`): `up` creates the named networks a stack declares (labelled
      with their compose key; external ones only when missing), and the simulator joins the
      iot broker's network. `sim start --mqtt-host` / `--network` override them.
- [x] Preflight on Docker Desktop (macOS / Windows): read `docker info` →
      `CgroupVersion` / `CgroupDriver` instead of the host's `/sys/fs/cgroup`
      (`utils/docker_host.py`), and warn on the `none` driver (rootless without delegation).
      Docker Desktop's VM ships QEMU, so the host binfmt check and `setup`'s install are
      skipped there. Block device detection on Desktop is still open (section 2, Storage).
- [x] `--json` output for `status` and `profile` so CI and the p4n4 CLI / API can consume it.
      `status --json` gives per-service state, usage, applied and expected limits, a
      `limits_state` (`applied` / `stale` / `not-in-overlay` / `none` / `external`) and the
      differences; the exit code stays 0, and `stale` is a top-level field.
- [x] Integration point with the main `p4n4` CLI: `p4n4 up --emu rpi5` shells out to
      `p4n4-emu up` (with `--build` / `--pull`), and `p4n4 down` hands stacks whose
      containers were created with an `.emu.yml` overlay to `p4n4-emu down --yes`.

## 7. Testing and CI (P2)

- [x] CLI tests with `typer.testing.CliRunner` and mocked `subprocess`. `up`, `down`, `logs`,
      `status`, `setup`, `sim` and `profile` have them (`tests/test_commands.py`).
- [x] Overlay tests that render every template against every profile and check the result with
      `docker compose config` against the real stacks (`tests/test_real_stacks.py`, marker
      `compose`): the merge must succeed, name exactly the stack's services, and carry every
      limit. Stacks come from `../../stacks/*` and `../../dashboard`, or `P4N4_STACKS_DIR` /
      `P4N4_DASHBOARD_DIR`; skipped without them or without `docker compose`.
- [x] Integration test (marker `integration`, opt-in with `--run-integration`) that starts the
      iot stack (mqtt, influxdb, node-red) under a profile, runs the simulator, and checks that
      readings land in InfluxDB and every container runs with its limits
      (`tests/test_integration.py`). It skips while any `p4n4-*` container exists on the host.
      It defaults to `rpi4`, not `mcu-class`: 256 MB leaves Node-RED 38 MiB and InfluxDB
      89 MiB, below what they use (InfluxDB alone used 137 MiB on a workstation), so
      expect OOM kills (see section 3, `mcu-class`). Not yet run green: the dev
      host had a p4n4 project running.
- [x] Regression tests for every P0 bug in section 1.
- [x] GitHub Actions (`.github/workflows/ci.yml`): `ruff check` + pytest on 3.11–3.13 with the
      stack repos cloned for the compose tests. The integration job runs on demand
      (`workflow_dispatch`, with a profile input) and weekly, on x86, on native arm64
      (`ubuntu-24.04-arm`), and optionally on x86 with arm64 images under QEMU.
- [ ] `ruff format`: 19 files would be reformatted (hand-wrapped argument lists such as
      `"docker", "run", "-d",`). Decide whether to adopt it before adding it to CI.

## 8. Packaging and docs (P2)

- [x] The simulator image builds from the source checkout root. It now runs
      `ghcr.io/raisga/p4n4-sensor-sim:<version>`: a local image first, else the published one,
      else a build whose context is a copy of the package alone (so it works from a wheel).
      `.github/workflows/image.yml` publishes amd64 + arm64 (`:edge` from main, `:X.Y.Z` from
      tags) after a smoke test against Mosquitto. `P4N4_EMU_SIM_IMAGE` overrides the image.
- [ ] Publish `p4n4-emu` to PyPI next to `p4n4-lib`. Ready: `.github/workflows/publish.yml`
      (on a GitHub release, trusted publishing, checks the tag and the wheel's data files) and
      the package metadata. Left: create the `pypi` environment and the PyPI trusted
      publisher, then tag `v0.1.0` so the image and the package go out together.
- [x] Document how faithful each limit is (what is enforced and what is approximated) and how
      profile numbers were chosen (README, "How faithful the emulation is"). The cgroup v1
      warnings now say what v1 actually loses (buffered-write throttling, swap accounting).
- [x] Docs page: `docs/reference/emulator.md` (p4n4-docs, formerly `web/docs`) is brought up to
      date and linked from the main README's Quick Start; `cli-reference.md` documents
      `p4n4 up --emu`.
