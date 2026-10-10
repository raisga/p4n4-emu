"""Tests for synthetic sensor generators."""

import json

import pytest

from p4n4_emu.sim import generators


def _sample(gen, n: int = 100) -> list:
    return [next(gen) for _ in range(n)]


def test_temperature_in_range():
    samples = _sample(generators.temperature_c())
    assert all(0.0 <= v <= 50.0 for v in samples), "Temperature out of plausible range"


def test_humidity_in_range():
    samples = _sample(generators.humidity_pct())
    assert all(10.0 <= v <= 95.0 for v in samples), "Humidity out of plausible range"


def test_pressure_in_range():
    samples = _sample(generators.pressure_hpa())
    assert all(980.0 <= v <= 1050.0 for v in samples), "Pressure out of plausible range"


def test_accelerometer_shape():
    samples = _sample(generators.accelerometer_xyz())
    assert all(len(v) == 3 for v in samples)
    # Z axis should be close to 1g when at rest
    z_vals = [v[2] for v in samples]
    assert all(0.5 <= z <= 1.5 for z in z_vals), "Z-axis should be ~1g at rest"


def test_cpu_load_in_range():
    samples = _sample(generators.cpu_load_pct())
    assert all(0.0 <= v <= 100.0 for v in samples), "CPU load out of range"


def test_temperature_varied():
    samples = _sample(generators.temperature_c(), 50)
    assert len(set(samples)) > 1, "Generator should produce varied values"


def test_sensor_payload_json_serialisable():
    temp = next(generators.temperature_c())
    humi = next(generators.humidity_pct())
    pres = next(generators.pressure_hpa())
    accel = next(generators.accelerometer_xyz())

    payload = {
        "value": temp,
        "humidity": humi,
        "pressure": pres,
        "accel": list(accel),
        "device": "emu-sensor-0",
    }
    serialised = json.dumps(payload)
    parsed = json.loads(serialised)
    assert parsed["device"] == "emu-sensor-0"
    assert isinstance(parsed["accel"], list)
    assert len(parsed["accel"]) == 3


def test_sensor_sim_publishes_spec_topics():
    import random

    from p4n4_emu.sim import scenario, sensor_sim

    sc = scenario.default_scenario()
    sim = sensor_sim.DeviceSim(sc.devices[0], sc, random.Random(), start=0.0)
    readings = sim.readings(0.0)

    assert [topic for _, topic, _ in readings] == [
        "sensors/emu-sensor-0/temperature",
        "sensors/emu-sensor-0/humidity",
        "sensors/emu-sensor-0/pressure",
        "sensors/emu-sensor-0/raw",
    ]
    assert json.loads(readings[0][2])["unit"] == "C"
    raw = json.loads(readings[3][2])
    assert len(raw["values"]) == 3 and "cpu_pct" in raw
    assert all(delay == 0 for delay, _, _ in readings)


def test_sensor_sim_stops_cleanly_on_sigterm(monkeypatch):
    import os
    import signal
    from unittest.mock import MagicMock

    from p4n4_emu.sim import sensor_sim

    client = MagicMock()
    monkeypatch.setattr(sensor_sim.mqtt, "Client", lambda *a, **k: client)
    # `docker stop` delivers SIGTERM while the loop is publishing
    monkeypatch.setattr(
        sensor_sim.DeviceSim, "readings", lambda self, now: os.kill(os.getpid(), signal.SIGTERM)
        or []
    )
    before = signal.getsignal(signal.SIGTERM)

    sensor_sim.run(interval=60)  # would block for a minute if SIGTERM were ignored

    client.disconnect.assert_called_once()
    assert signal.getsignal(signal.SIGTERM) is before


def test_wave_phase_shifts_the_curve(monkeypatch):
    monkeypatch.setattr(generators.time, "time", lambda: 0.0)
    level = next(generators.temperature_c(amplitude=3.0, noise=0.0, phase=0.0))
    peak = next(generators.temperature_c(amplitude=3.0, noise=0.0, phase=0.25))
    assert level == 22.0
    assert peak == 25.0


def test_devices_get_distinct_stable_phases(monkeypatch):
    import random

    from p4n4_emu.sim import scenario, sensor_sim

    monkeypatch.setattr(generators.random, "gauss", lambda mu, sigma: 0.0)
    monkeypatch.setattr(generators.time, "time", lambda: 0.0)
    sc = scenario.default_scenario(3)

    def temperature(device):
        sim = sensor_sim.DeviceSim(device, sc, random.Random(), start=0.0)
        return json.loads(sim.readings(0.0)[0][2])["value"]

    temps = [temperature(d) for d in sc.devices]
    assert len(set(temps)) == 3, "every device should sit at its own point of the curve"
    assert temperature(sc.devices[0]) == temps[0]


class _FakeClock:
    """A clock that stop.wait() moves forward; stops the run after *until* seconds."""

    def __init__(self, stop, until: float):
        self.now = 1000.0
        self.end = self.now + until
        self.stop = stop

    def __call__(self) -> float:
        return self.now

    def wait(self, timeout: float) -> None:
        self.now += timeout
        if self.now > self.end:
            self.stop.set()


def _run(monkeypatch, until: float = 0.0, **kwargs):
    """Run the simulator for *until* simulated seconds; return the client and its publishes."""
    import threading
    from unittest.mock import MagicMock

    from p4n4_emu.sim import sensor_sim

    client = MagicMock()
    monkeypatch.setattr(sensor_sim.mqtt, "Client", lambda *a, **k: client)
    stop = threading.Event()
    clock = _FakeClock(stop, until)
    monkeypatch.setattr(stop, "wait", clock.wait)
    published = []
    client.publish.side_effect = lambda topic, payload, **kw: published.append(
        (clock.now - 1000.0, topic, payload, kw)
    )
    sensor_sim.run(stop=stop, clock=clock, **kwargs)
    return client, published


def _devices(published) -> list[str]:
    return sorted({topic.split("/")[1] for _, topic, _, _ in published})


def test_sensor_sim_reads_env_at_call_time(monkeypatch):
    monkeypatch.setenv("SIM_DEVICE_COUNT", "3")
    monkeypatch.setenv("MQTT_HOST", "broker.test")
    client, published = _run(monkeypatch)
    assert _devices(published) == ["emu-sensor-0", "emu-sensor-1", "emu-sensor-2"]
    assert client.connect_async.call_args.args[:2] == ("broker.test", 1883)


def test_sensor_sim_arguments_override_env(monkeypatch):
    monkeypatch.setenv("SIM_DEVICE_COUNT", "3")
    _, published = _run(monkeypatch, devices=1)
    assert _devices(published) == ["emu-sensor-0"]


def test_sensor_sim_retries_when_broker_is_down(monkeypatch):
    client, _ = _run(monkeypatch)
    # connect() raises if the broker isn't up; connect_async lets the loop retry
    client.connect.assert_not_called()
    client.connect_async.assert_called_once()
    client.reconnect_delay_set.assert_called_once()
    client.loop_start.assert_called_once()


# ── scenarios ─────────────────────────────────────────────────────────────────

def _scenario(**doc):
    from p4n4_emu.sim import scenario

    return scenario.parse_scenario(doc)


def test_scenario_devices_measurements_and_intervals(monkeypatch):
    sc = _scenario(
        interval=2,
        devices=[
            {"id": "node-{n}", "count": 2, "measurements": ["temperature"]},
            {"id": "pump", "interval": 5, "measurements": {"flow": {"base": 12, "unit": "l/min"}}},
        ],
    )
    _, published = _run(monkeypatch, until=10, scenario=sc)

    def times(topic):
        return [t for t, tp, _, _ in published if tp == topic]

    assert times("sensors/node-0/temperature") == [0, 2, 4, 6, 8, 10]
    assert times("sensors/pump/flow") == [0, 5, 10]
    assert {tp for _, tp, _, _ in published} == {
        "sensors/node-0/temperature", "sensors/node-1/temperature", "sensors/pump/flow",
    }
    flow = json.loads(next(p for _, tp, p, _ in published if tp == "sensors/pump/flow"))
    assert flow["unit"] == "l/min" and abs(flow["value"] - 12) < 1e-9


def test_scenario_qos_retain_and_timestamp(monkeypatch):
    sc = _scenario(qos=1, retain=True, timestamp=True,
                   devices=[{"id": "d", "measurements": ["pressure"]}])
    _, published = _run(monkeypatch, scenario=sc)
    _, _, payload, kw = published[0]
    assert kw == {"qos": 1, "retain": True}
    assert json.loads(payload)["ts"] == 1_000_000


def test_qos_and_retain_arguments_override_the_scenario(monkeypatch):
    sc = _scenario(qos=2, devices=[{"id": "d", "measurements": ["pressure"]}])
    _, published = _run(monkeypatch, scenario=sc, qos=0, retain=True)
    assert published[0][3] == {"qos": 0, "retain": True}


def test_scenario_file_from_env(monkeypatch, tmp_path):
    path = tmp_path / "scenario.yml"
    path.write_text("interval: 3\ndevices:\n  - id: from-file\n    measurements: [humidity]\n")
    monkeypatch.setenv("SIM_SCENARIO", str(path))
    _, published = _run(monkeypatch, until=6)
    assert [(t, tp) for t, tp, _, _ in published] == [
        (0, "sensors/from-file/humidity"), (3, "sensors/from-file/humidity"),
        (6, "sensors/from-file/humidity"),
    ]


def test_builtin_wave_settings_can_be_changed():
    from p4n4_emu.sim import scenario

    sc = _scenario(devices=[{"id": "d", "measurements": {"humidity": {"max": 60}}}])
    wave = sc.devices[0].measurements["humidity"]
    assert wave.max == 60 and wave.min == 10 and wave.base == 55
    assert wave.unit == scenario.BUILTIN_WAVES["humidity"].unit


def test_wave_is_clamped(monkeypatch):
    from p4n4_emu.sim.scenario import Wave

    monkeypatch.setattr(generators.time, "time", lambda: 75.0)  # a quarter period: the peak
    assert next(generators.wave(Wave(10, amplitude=5, max=12))) == 12
    assert next(generators.wave(Wave(10, amplitude=-5, min=8))) == 8


@pytest.mark.parametrize(
    "doc, message",
    [
        ({"devices": []}, "devices: expected a non-empty list"),
        ({"devices": [{"id": "d"}], "colour": 1}, "unknown key(s) colour"),
        ({"devices": [{"id": "d", "count": 2}]}, "devices[0].id: needs {n}"),
        ({"devices": [{"id": "a/b"}]}, "can't contain '/'"),
        ({"devices": [{"id": "d"}, {"id": "d"}]}, "device id 'd' is used twice"),
        ({"devices": [{"id": "d", "measurements": ["flow"]}]}, "flow: not a built-in"),
        ({"devices": [{"id": "d", "measurements": {"flow": {"unit": "l"}}}]}, "flow.base"),
        ({"devices": [{"id": "d", "measurements": {"humidity": {"min": 90, "max": 20}}}]},
         "min is above max"),
        ({"devices": [{"id": "d", "measurements": {"temperature": {"base": "hot"}}}]},
         "temperature.base: expected a number"),
        ({"qos": 3, "devices": [{"id": "d"}]}, "qos: expected one of 0, 1, 2"),
        ({"interval": 0, "devices": [{"id": "d"}]}, "interval: must be positive"),
        ({"devices": [{"id": "d", "faults": [{"type": "explode"}]}]}, "faults[0].type"),
        ({"devices": [{"id": "d", "faults": [{"type": "spike", "rate": 1}]}]},
         "unknown key(s) rate"),
        ({"devices": [{"id": "d", "faults": [{"type": "dropout", "probability": 2}]}]},
         "between 0 and 1"),
        ({"devices": [{"id": "d", "faults": [{"type": "delay"}]}]}, "seconds: required"),
        ({"devices": [{"id": "d", "faults": [{"type": "spike", "measurement": "raw"}]}]},
         "spike needs a measurement with a value"),
        ({"devices": [{"id": "d", "measurements": ["temperature"],
                       "faults": [{"type": "stuck", "measurement": "humidity"}]}]},
         "the device has no 'humidity'"),
    ],
)
def test_scenario_errors_say_where(doc, message):
    from p4n4_emu.sim import scenario

    with pytest.raises(scenario.ScenarioError) as e:
        scenario.parse_scenario(doc)
    assert message in str(e.value)


def test_load_scenario_reports_bad_yaml(tmp_path):
    from p4n4_emu.sim import scenario

    path = tmp_path / "bad.yml"
    path.write_text("devices: [\n")
    with pytest.raises(scenario.ScenarioError, match="not valid YAML"):
        scenario.load_scenario(path)
    with pytest.raises(scenario.ScenarioError, match="Cannot read"):
        scenario.load_scenario(tmp_path / "missing.yml")


# ── faults ────────────────────────────────────────────────────────────────────

def _flat(monkeypatch):
    """Generators that return the wave's base exactly."""
    monkeypatch.setattr(generators.random, "gauss", lambda mu, sigma: 0.0)
    monkeypatch.setattr(generators.time, "time", lambda: 0.0)


def _faulty(monkeypatch, faults, until=10.0, measurements=None, **doc):
    _flat(monkeypatch)
    sc = _scenario(
        seed=1,
        devices=[{
            "id": "d",
            "measurements": measurements or {"level": {"base": 50}},
            "faults": faults,
        }],
        **doc,
    )
    return _run(monkeypatch, until=until, scenario=sc)[1]


def _values(published, measurement="level"):
    return [
        (t, json.loads(p)["value"]) for t, tp, p, _ in published if tp.endswith("/" + measurement)
    ]


def test_spike_fault(monkeypatch):
    published = _faulty(monkeypatch, [{"type": "spike", "magnitude": 20}], until=4, interval=1)
    assert {v for _, v in _values(published)} <= {30, 70}


def test_spike_fault_only_hits_its_measurement(monkeypatch):
    published = _faulty(
        monkeypatch,
        [{"type": "spike", "measurement": "level", "magnitude": 20}],
        measurements={"level": {"base": 50}, "other": {"base": 5}},
    )
    assert all(v != 50 for _, v in _values(published))
    assert all(v == 5 for _, v in _values(published, "other"))


def test_drift_fault(monkeypatch):
    published = _faulty(monkeypatch, [{"type": "drift", "rate": 0.5}], until=4, interval=2)
    assert _values(published) == [(0, 50), (2, 51), (4, 52)]


def test_stuck_fault_freezes_the_value(monkeypatch):
    published = _faulty(
        monkeypatch,
        [{"type": "drift", "rate": 1}, {"type": "stuck", "duration": 3}],
        until=8, interval=1,
    )
    # Stuck at 0 s for 3 s, then sticks again on the next reading (probability 1)
    assert _values(published) == [
        (0, 50), (1, 50), (2, 50), (3, 53), (4, 53), (5, 53), (6, 56), (7, 56), (8, 56),
    ]


def test_dropout_without_measurement_silences_the_device(monkeypatch):
    published = _faulty(
        monkeypatch,
        [{"type": "dropout", "duration": 5}],
        measurements={"level": {"base": 50}, "other": {"base": 5}},
        until=10, interval=1,
    )
    assert published == []


def test_dropout_probability(monkeypatch):
    published = _faulty(
        monkeypatch, [{"type": "dropout", "probability": 0.5}], until=199, interval=1
    )
    assert 60 < len(published) < 140, "about half the readings should be dropped"


def test_dropout_of_one_measurement(monkeypatch):
    published = _faulty(
        monkeypatch,
        [{"type": "dropout", "measurement": "other"}],
        measurements={"level": {"base": 50}, "other": {"base": 5}},
        until=2, interval=1,
    )
    assert {tp for _, tp, _, _ in published} == {"sensors/d/level"}


def test_delay_fault_publishes_late_and_out_of_order(monkeypatch):
    published = _faulty(
        monkeypatch,
        [{"type": "delay", "measurement": "late", "seconds": 3}],
        measurements={"level": {"base": 50}, "late": {"base": 5}},
        until=6, interval=2, timestamp=True,
    )
    late = [(t, json.loads(p)["ts"]) for t, tp, p, _ in published if tp == "sensors/d/late"]
    # Taken at 0, 2 and 4 s; published at 3, 5 and 7 s (the last after the run's end)
    assert late == [(3, 1_000_000), (5, 1_002_000)]
    order = [tp.split("/")[2] for _, tp, _, _ in published]
    assert order[:3] == ["level", "level", "late"], "a newer reading goes out first"


def test_malformed_fault(monkeypatch):
    from p4n4_emu.sim import faults

    published = _faulty(monkeypatch, [{"type": "malformed"}], until=60, interval=1)
    kinds = set()
    for _, _, payload, _ in published:
        try:
            doc = json.loads(payload)
        except json.JSONDecodeError:
            kinds.add("truncated" if payload.startswith("{") else "not-json")
            continue
        kinds.add("string-value" if isinstance(doc.get("value"), str) else "no-value")
        assert "value" not in doc or isinstance(doc["value"], str)
    assert kinds == set(faults.MALFORMED)


def test_seed_repeats_the_faults(monkeypatch):
    faults = [{"type": "spike", "probability": 0.3, "magnitude": 1}]
    first = _faulty(monkeypatch, faults, until=50, interval=1)
    second = _faulty(monkeypatch, faults, until=50, interval=1)
    assert _values(first) == _values(second)
    assert len({v for _, v in _values(first)}) > 1


# ── broker auth and TLS ───────────────────────────────────────────────────────

def test_broker_auth_from_env(monkeypatch):
    monkeypatch.setenv("MQTT_USERNAME", "iot-device-001")
    monkeypatch.setenv("MQTT_PASSWORD", "s3cret")
    monkeypatch.setenv("MQTT_CA_FILE", "/certs/ca.crt")
    client, _ = _run(monkeypatch)
    client.username_pw_set.assert_called_once_with("iot-device-001", "s3cret")
    assert client.tls_set.call_args.kwargs["ca_certs"] == "/certs/ca.crt"
    # TLS without a port: MQTT's TLS port
    assert client.connect_async.call_args.args[1] == 8883


def test_anonymous_plain_connection_by_default(monkeypatch):
    for var in ("MQTT_USERNAME", "MQTT_PASSWORD", "MQTT_TLS", "MQTT_CA_FILE", "MQTT_PORT"):
        monkeypatch.delenv(var, raising=False)
    client, _ = _run(monkeypatch)
    client.username_pw_set.assert_not_called()
    client.tls_set.assert_not_called()
    assert client.connect_async.call_args.args[1] == 1883


def test_example_scenario_is_valid():
    from pathlib import Path

    from p4n4_emu.sim import scenario

    sc = scenario.load_scenario(Path(__file__).parent.parent / "examples" / "sim-scenario.yml")
    ids = [d.id for d in sc.devices]
    assert ids == ["emu-room-0", "emu-room-1", "emu-room-2", "iot-device-001"]
