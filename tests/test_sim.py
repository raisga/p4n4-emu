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
