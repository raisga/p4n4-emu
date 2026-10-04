"""Synthetic sensor data publisher — connects to Mosquitto and publishes fake readings.

Run standalone:
    python -m p4n4_emu.sim.sensor_sim

Environment variables:
    MQTT_HOST          Mosquitto hostname (default: localhost)
    MQTT_PORT          Mosquitto port (default: 1883)
    SIM_INTERVAL_SEC   Publish interval in seconds (default: 2.0)
    SIM_DEVICE_COUNT   Number of simulated sensor devices (default: 1)
"""

from __future__ import annotations

import json
import logging
import os
import random
import signal
import threading

import paho.mqtt.client as mqtt

from p4n4_emu.sim import generators

_log = logging.getLogger(__name__)

# Seconds between reconnect attempts while the broker is down: 1, 2, 4, … 30
RECONNECT_MIN_DELAY = 1
RECONNECT_MAX_DELAY = 30


def _device_gens(device_id: str) -> dict:
    """Generators for one device, phase-shifted by a value derived from its id.

    The same id always gets the same phase, so a restarted simulator keeps
    each device on its own curve.
    """
    phase = random.Random(device_id).random()
    return {
        "id": device_id,
        "temp": generators.temperature_c(phase=phase),
        "humi": generators.humidity_pct(phase=phase),
        "pres": generators.pressure_hpa(phase=phase),
        "accel": generators.accelerometer_xyz(),
        "cpu": generators.cpu_load_pct(phase=phase),
    }


def _on_connect(client, userdata, flags, reason_code, properties) -> None:
    if reason_code.is_failure:
        _log.warning("Broker refused the connection: %s", reason_code)
    else:
        _log.info("Connected to the broker.")


def _on_connect_fail(client, userdata) -> None:
    _log.warning("Cannot reach the broker; retrying.")


def _on_disconnect(client, userdata, flags, reason_code, properties) -> None:
    if reason_code.is_failure:
        _log.warning("Lost the broker connection (%s); reconnecting.", reason_code)


def _publish_device(client: mqtt.Client, device_id: str, gens: dict) -> None:
    temp = next(gens["temp"])
    humi = next(gens["humi"])
    pres = next(gens["pres"])
    accel = next(gens["accel"])
    cpu = next(gens["cpu"])

    def pub(measurement: str, payload: dict) -> None:
        # Topic convention: sensors/<device-id>/<measurement>
        client.publish(f"sensors/{device_id}/{measurement}", json.dumps(payload))

    pub("temperature", {"value": temp, "unit": "C"})
    pub("humidity", {"value": humi, "unit": "%"})
    pub("pressure", {"value": pres, "unit": "hPa"})
    pub("raw", {"values": list(accel), "cpu_pct": cpu})
    _log.debug("%s: temp=%.1f humi=%.1f pres=%.1f", device_id, temp, humi, pres)


def run(
    host: str | None = None,
    port: int | None = None,
    interval: float | None = None,
    devices: int | None = None,
    stop: threading.Event | None = None,
) -> None:
    """Publish readings every *interval* seconds until *stop* is set or SIGTERM/SIGINT.

    Settings left as None come from the environment variables in the module
    docstring, read when run() is called.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if host is None:
        host = os.getenv("MQTT_HOST", "localhost")
    if port is None:
        port = int(os.getenv("MQTT_PORT", "1883"))
    if interval is None:
        interval = float(os.getenv("SIM_INTERVAL_SEC", "2.0"))
    if devices is None:
        devices = int(os.getenv("SIM_DEVICE_COUNT", "1"))
    stop = stop or threading.Event()

    # `docker stop` sends SIGTERM to PID 1, which ignores it without a handler
    in_main_thread = threading.current_thread() is threading.main_thread()
    if in_main_thread:
        previous_handler = signal.signal(signal.SIGTERM, lambda *_: stop.set())

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = _on_connect
    client.on_connect_fail = _on_connect_fail
    client.on_disconnect = _on_disconnect
    client.reconnect_delay_set(RECONNECT_MIN_DELAY, RECONNECT_MAX_DELAY)
    # The network loop retries the first connection too, so a broker that is
    # still starting (or restarting) doesn't end the simulator. Readings taken
    # while disconnected are dropped, as a QoS 0 device would drop them.
    client.connect_async(host, port, keepalive=60)
    client.loop_start()

    device_gens = [_device_gens(f"emu-sensor-{i}") for i in range(devices)]

    _log.info(
        "Sensor sim started: %d device(s) → %s:%d every %.1fs",
        devices, host, port, interval,
    )
    try:
        while not stop.is_set():
            for dg in device_gens:
                _publish_device(client, dg["id"], dg)
            stop.wait(interval)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()
        if in_main_thread:
            signal.signal(signal.SIGTERM, previous_handler)
        _log.info("Sensor sim stopped.")


if __name__ == "__main__":
    run()
