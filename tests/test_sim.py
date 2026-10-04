"""Tests for synthetic sensor generators."""

import json

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
    from unittest.mock import MagicMock

    from p4n4_emu.sim import sensor_sim

    client = MagicMock()
    gens = {
        "temp": generators.temperature_c(),
        "humi": generators.humidity_pct(),
        "pres": generators.pressure_hpa(),
        "accel": generators.accelerometer_xyz(),
        "cpu": generators.cpu_load_pct(),
    }
    sensor_sim._publish_device(client, "emu-sensor-0", gens)

    topics = [c.args[0] for c in client.publish.call_args_list]
    assert topics == [
        "sensors/emu-sensor-0/temperature",
        "sensors/emu-sensor-0/humidity",
        "sensors/emu-sensor-0/pressure",
        "sensors/emu-sensor-0/raw",
    ]
    raw = json.loads(client.publish.call_args_list[3].args[1])
    assert len(raw["values"]) == 3


def test_sensor_sim_stops_cleanly_on_sigterm(monkeypatch):
    import os
    import signal
    from unittest.mock import MagicMock

    from p4n4_emu.sim import sensor_sim

    client = MagicMock()
    monkeypatch.setattr(sensor_sim.mqtt, "Client", lambda *a, **k: client)
    # `docker stop` delivers SIGTERM while the loop is publishing
    monkeypatch.setattr(
        sensor_sim, "_publish_device", lambda *a: os.kill(os.getpid(), signal.SIGTERM)
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
    from p4n4_emu.sim import sensor_sim

    monkeypatch.setattr(generators.random, "gauss", lambda mu, sigma: 0.0)
    monkeypatch.setattr(generators.time, "time", lambda: 0.0)
    temps = [next(sensor_sim._device_gens(f"emu-sensor-{i}")["temp"]) for i in range(3)]
    assert len(set(temps)) == 3, "every device should sit at its own point of the curve"
    assert next(sensor_sim._device_gens("emu-sensor-0")["temp"]) == temps[0]


def _run_once(monkeypatch, **kwargs):
    """Run the simulator for one publish round; return the client and device ids."""
    import threading
    from unittest.mock import MagicMock

    from p4n4_emu.sim import sensor_sim

    client = MagicMock()
    monkeypatch.setattr(sensor_sim.mqtt, "Client", lambda *a, **k: client)
    stop = threading.Event()
    devices = []

    def publish(c, device_id, gens):
        devices.append(device_id)

    monkeypatch.setattr(sensor_sim, "_publish_device", publish)
    monkeypatch.setattr(stop, "wait", lambda timeout: stop.set())
    sensor_sim.run(stop=stop, **kwargs)
    return client, devices


def test_sensor_sim_reads_env_at_call_time(monkeypatch):
    monkeypatch.setenv("SIM_DEVICE_COUNT", "3")
    monkeypatch.setenv("MQTT_HOST", "broker.test")
    client, devices = _run_once(monkeypatch)
    assert devices == ["emu-sensor-0", "emu-sensor-1", "emu-sensor-2"]
    assert client.connect_async.call_args.args[:2] == ("broker.test", 1883)


def test_sensor_sim_arguments_override_env(monkeypatch):
    monkeypatch.setenv("SIM_DEVICE_COUNT", "3")
    _, devices = _run_once(monkeypatch, devices=1)
    assert devices == ["emu-sensor-0"]


def test_sensor_sim_retries_when_broker_is_down(monkeypatch):
    client, _ = _run_once(monkeypatch)
    # connect() raises if the broker isn't up; connect_async lets the loop retry
    client.connect.assert_not_called()
    client.connect_async.assert_called_once()
    client.reconnect_delay_set.assert_called_once()
    client.loop_start.assert_called_once()
