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
import time

import paho.mqtt.client as mqtt

from p4n4_emu.sim import generators

_log = logging.getLogger(__name__)

MQTT_HOST = os.getenv("MQTT_HOST", "localhost")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
INTERVAL = float(os.getenv("SIM_INTERVAL_SEC", "2.0"))
DEVICE_COUNT = int(os.getenv("SIM_DEVICE_COUNT", "1"))


def _publish_device(client: mqtt.Client, device_id: str, gens: dict) -> None:
    temp = next(gens["temp"])
    humi = next(gens["humi"])
    pres = next(gens["pres"])
    accel = next(gens["accel"])
    cpu = next(gens["cpu"])

    def pub(topic: str, payload: dict) -> None:
        payload["device"] = device_id
        client.publish(topic, json.dumps(payload))

    pub("sensors/temperature", {"value": temp, "unit": "C"})
    pub("sensors/humidity", {"value": humi, "unit": "%"})
    pub("sensors/pressure", {"value": pres, "unit": "hPa"})
    pub("sensors/raw", {"values": list(accel), "cpu_pct": cpu})
    _log.debug("%s: temp=%.1f humi=%.1f pres=%.1f", device_id, temp, humi, pres)


def run(host: str = MQTT_HOST, port: int = MQTT_PORT, interval: float = INTERVAL) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.connect(host, port, keepalive=60)
    client.loop_start()

    device_gens = []
    for i in range(DEVICE_COUNT):
        device_gens.append({
            "id": f"emu-sensor-{i}",
            "temp": generators.temperature_c(),
            "humi": generators.humidity_pct(),
            "pres": generators.pressure_hpa(),
            "accel": generators.accelerometer_xyz(),
            "cpu": generators.cpu_load_pct(),
        })

    _log.info(
        "Sensor sim started: %d device(s) → %s:%d every %.1fs",
        DEVICE_COUNT, host, port, interval,
    )
    try:
        while True:
            for dg in device_gens:
                _publish_device(client, dg["id"], dg)
            time.sleep(interval)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()
        _log.info("Sensor sim stopped.")


if __name__ == "__main__":
    run()
