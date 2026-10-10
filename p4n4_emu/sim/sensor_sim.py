"""Synthetic sensor data publisher — connects to Mosquitto and publishes fake readings.

Run standalone:
    python -m p4n4_emu.sim.sensor_sim

Environment variables:
    MQTT_HOST          Mosquitto hostname (default: localhost)
    MQTT_PORT          Mosquitto port (default: 1883, or 8883 with TLS)
    MQTT_USERNAME      Username to log in with (default: none, anonymous)
    MQTT_PASSWORD      Its password
    MQTT_TLS           1 to connect over TLS, verified against the system CAs
    MQTT_CA_FILE       CA certificate to verify the broker with (implies TLS)
    MQTT_CERT_FILE     Client certificate, for brokers that require one
    MQTT_KEY_FILE      Its private key
    SIM_SCENARIO       Scenario file (see scenario.py); without one, every device
                       publishes every built-in measurement
    SIM_FILES          JSON map of the files the scenario names (as written) to where
                       they are mounted; `sim start` sets it
    SIM_REPLAY         A recording to replay (see replay.py), besides the scenario's
    SIM_REPLAY_SPEED   Its speed (default 1; 0 publishes as fast as it can)
    SIM_REPLAY_LOOP    1 to start it over at the end
    SIM_INTERVAL_SEC   Publish interval in seconds (default: 2.0, or the scenario's)
    SIM_DEVICE_COUNT   Number of simulated sensor devices without a scenario (default: 1)
    SIM_QOS            MQTT QoS of every publish (default: 0, or the scenario's)
    SIM_RETAIN         1 to publish with the retain flag (default: 0, or the scenario's)
"""

from __future__ import annotations

import heapq
import json
import logging
import os
import random
import signal
import ssl
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace

import paho.mqtt.client as mqtt

from p4n4_emu.sim import generators, media
from p4n4_emu.sim.faults import FaultInjector
from p4n4_emu.sim.mcu import McuNode
from p4n4_emu.sim.replay import Replayer
from p4n4_emu.sim.scenario import (
    Device,
    Feed,
    Replay,
    Scenario,
    Wave,
    default_scenario,
    load_scenario,
)

_log = logging.getLogger(__name__)

# Seconds between reconnect attempts while the broker is down: 1, 2, 4, … 30
RECONNECT_MIN_DELAY = 1
RECONNECT_MAX_DELAY = 30


@dataclass(frozen=True)
class BrokerAuth:
    username: str | None = None
    password: str | None = None
    tls: bool = False
    ca_file: str | None = None
    cert_file: str | None = None
    key_file: str | None = None

    @classmethod
    def from_env(cls) -> BrokerAuth:
        ca_file = os.getenv("MQTT_CA_FILE") or None
        return cls(
            username=os.getenv("MQTT_USERNAME") or None,
            password=os.getenv("MQTT_PASSWORD") or None,
            tls=_env_flag("MQTT_TLS") or ca_file is not None,
            ca_file=ca_file,
            cert_file=os.getenv("MQTT_CERT_FILE") or None,
            key_file=os.getenv("MQTT_KEY_FILE") or None,
        )

    def apply(self, client: mqtt.Client) -> None:
        if self.username:
            client.username_pw_set(self.username, self.password)
        if self.tls:
            client.tls_set(
                ca_certs=self.ca_file,
                certfile=self.cert_file,
                keyfile=self.key_file,
                cert_reqs=ssl.CERT_REQUIRED,
            )


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")


class DeviceSim:
    """One device's generators and faults; turns a moment into the messages it sends."""

    def __init__(self, device: Device, scenario: Scenario, rng: random.Random, start: float):
        self.device = device
        self.scenario = scenario
        self.interval = scenario.interval_of(device)
        # Devices don't all report the same value: each has its own phase
        phase = generators.device_phase(device.id)
        self.gens: dict[str, Iterator] = {}
        for name, wave in device.measurements.items():
            if wave is None:
                self.gens[name] = zip(
                    generators.accelerometer_xyz(), generators.cpu_load_pct(phase=phase)
                )
            elif isinstance(wave, Feed):
                # Its own generator, so a feed's noise doesn't shift when faults fire
                noise = random.Random(f"{scenario.seed}/{device.id}/{name}")
                self.gens[name] = media.feed(wave, noise, phase)
            else:
                self.gens[name] = generators.wave(wave, phase=phase)
        self.faults = FaultInjector(device.faults, rng, start)

    def readings(self, now: float) -> list[tuple[float, str, str]]:
        """(delay in seconds, topic, payload) for each reading taken at *now*."""
        if self.faults.device_silent(now):
            return []
        out = []
        for name, gen in self.gens.items():
            sample = next(gen)  # sampled even when dropped, so the curve stays on time
            if self.faults.dropped(name, now):
                continue
            kind = self.device.measurements[name]
            if kind is None:
                accel, cpu = sample
                payload: dict = {"values": list(accel), "cpu_pct": cpu}
            elif not isinstance(kind, Wave):
                payload = {"values": sample}
            else:
                payload = {
                    "value": self.faults.value(name, sample, now),
                    "unit": self.device.measurements[name].unit,
                }
            if self.scenario.timestamp:
                payload["ts"] = int(now * 1000)
            # Topic convention: sensors/<device-id>/<measurement>
            topic = f"sensors/{self.device.id}/{name}"
            out.append((self.faults.delay(name), topic, self.faults.payload(name, payload)))
        return out


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


def resolve_scenario(
    scenario: Scenario | str | None = None,
    interval: float | None = None,
    devices: int | None = None,
    qos: int | None = None,
    retain: bool | None = None,
    replay: Replay | None = None,
) -> Scenario:
    """The scenario to run, with the arguments, then the environment, overriding it.

    Settings left as None come from the environment variables in the module
    docstring, read when this is called. *replay* (or SIM_REPLAY) is replayed besides
    the scenario's own; with neither a scenario nor a device count, it is all that runs.
    """
    if replay is None and os.getenv("SIM_REPLAY"):
        replay = Replay(
            os.environ["SIM_REPLAY"],
            float(os.getenv("SIM_REPLAY_SPEED") or 1.0),
            _env_flag("SIM_REPLAY_LOOP"),
        )
    if scenario is None:
        scenario = os.getenv("SIM_SCENARIO") or None
    if isinstance(scenario, str):
        files = json.loads(os.environ["SIM_FILES"]) if os.getenv("SIM_FILES") else None
        scenario = load_scenario(scenario, files)
    if interval is None and os.getenv("SIM_INTERVAL_SEC"):
        interval = float(os.environ["SIM_INTERVAL_SEC"])
    if scenario is None:
        if devices is None and replay is not None and not os.getenv("SIM_DEVICE_COUNT"):
            scenario = Scenario(devices=())
        else:
            if devices is None:
                devices = int(os.getenv("SIM_DEVICE_COUNT", "1"))
            scenario = default_scenario(devices)
    if replay is not None:
        scenario = replace(scenario, replays=(*scenario.replays, replay))
    if qos is None and os.getenv("SIM_QOS"):
        qos = int(os.environ["SIM_QOS"])
    if retain is None and os.getenv("SIM_RETAIN"):
        retain = _env_flag("SIM_RETAIN")
    changes = {"interval": interval, "qos": qos, "retain": retain}
    return replace(scenario, **{k: v for k, v in changes.items() if v is not None})


def run(
    host: str | None = None,
    port: int | None = None,
    interval: float | None = None,
    devices: int | None = None,
    stop: threading.Event | None = None,
    scenario: Scenario | str | None = None,
    auth: BrokerAuth | None = None,
    qos: int | None = None,
    retain: bool | None = None,
    clock: Callable[[], float] = time.time,
    replay: Replay | None = None,
) -> None:
    """Publish readings on the scenario's schedule until *stop* is set or SIGTERM/SIGINT.

    *interval* sets the scenario's interval, which devices without their own use.
    *devices* only applies without a scenario. Settings left as None come from the
    environment variables in the module docstring, read when run() is called.
    """
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if host is None:
        host = os.getenv("MQTT_HOST", "localhost")
    if auth is None:
        auth = BrokerAuth.from_env()
    if port is None:
        port = int(os.getenv("MQTT_PORT") or (8883 if auth.tls else 1883))
    scenario = resolve_scenario(scenario, interval, devices, qos, retain, replay)
    stop = stop or threading.Event()

    # `docker stop` sends SIGTERM to PID 1, which ignores it without a handler
    in_main_thread = threading.current_thread() is threading.main_thread()
    if in_main_thread:
        previous_handler = signal.signal(signal.SIGTERM, lambda *_: stop.set())

    def new_client(client_id: str | None = None) -> mqtt.Client:
        if client_id is None:
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        else:  # a firmware-like device: its own id, and MQTT 5 to drop with its will
            c = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=client_id,
                            protocol=mqtt.MQTTv5)
        c.on_connect = _on_connect
        c.on_connect_fail = _on_connect_fail
        c.on_disconnect = _on_disconnect
        c.reconnect_delay_set(RECONNECT_MIN_DELAY, RECONNECT_MAX_DELAY)
        auth.apply(c)
        return c

    def connect(c: mqtt.Client) -> None:
        # The network loop retries the first connection too, so a broker that is
        # still starting (or restarting) doesn't end the simulator. QoS 0 readings
        # taken while disconnected are dropped, as a QoS 0 device would drop them.
        c.connect_async(host, port, keepalive=60)
        c.loop_start()

    client = new_client()
    connect(client)

    start = clock()
    rng = random.Random(scenario.seed)
    sims = [DeviceSim(d, scenario, rng, start) for d in scenario.devices]
    nodes = [
        McuNode(sim.device.id, sim.device.mcu, sim.interval, sim.readings, new_client, connect,
                rng, start, scenario.qos, scenario.retain)
        for sim in sims if sim.device.mcu is not None
    ]
    replayers = [Replayer(r, start) for r in scenario.replays]
    # (when, sequence, …): the sequence keeps equal times in order and never compares further
    due = [(start, i, sim) for i, sim in enumerate(sims) if sim.device.mcu is None]
    heapq.heapify(due)
    # Delayed readings: (when, seq, publish, topic, payload)
    held: list[tuple[float, int, Callable[[str, str], None], str, str]] = []
    seq = len(sims)

    def publish(topic: str, payload: str) -> None:
        client.publish(topic, payload, qos=scenario.qos, retain=scenario.retain)

    def hold(now: float, late, publisher) -> None:
        nonlocal seq
        for delay, topic, payload in late:
            seq += 1
            heapq.heappush(held, (now + delay, seq, publisher, topic, payload))

    _log.info(
        "Sensor sim started: %d device(s)%s%s → %s:%d%s every %.1fs (QoS %d%s)",
        len(sims), f", {len(nodes)} firmware-like" if nodes else "",
        f", {len(replayers)} replay(s)" if replayers else "",
        host, port, " over TLS" if auth.tls else "", scenario.interval,
        scenario.qos, ", retained" if scenario.retain else "",
    )
    try:
        while not stop.is_set():
            now = clock()
            while due and due[0][0] <= now:
                _, i, sim = heapq.heappop(due)
                late = []
                for delay, topic, payload in sim.readings(now):
                    if delay > 0:
                        late.append((delay, topic, payload))
                    else:
                        publish(topic, payload)
                hold(now, late, publish)
                heapq.heappush(due, (now + sim.interval, i, sim))
            for node in nodes:
                if node.due <= now:
                    hold(now, node.tick(now), node.publish)
            for replayer in replayers:
                for topic, payload in replayer.pop_due(now, scenario.timestamp):
                    publish(topic, payload)
            while held and held[0][0] <= now:
                _, _, publisher, topic, payload = heapq.heappop(held)
                publisher(topic, payload)
            times = [t for t in (
                due[0][0] if due else None,
                held[0][0] if held else None,
                *(n.due for n in nodes),
                *(r.next_due for r in replayers),
            ) if t is not None]
            if not times:
                _log.info("Every replay has ended.")
                break
            stop.wait(max(0.0, min(times) - clock()))
    except KeyboardInterrupt:
        pass
    finally:
        for node in nodes:
            node.stop()
        client.loop_stop()
        client.disconnect()
        if in_main_thread:
            signal.signal(signal.SIGTERM, previous_handler)
        _log.info("Sensor sim stopped.")


if __name__ == "__main__":
    run()
