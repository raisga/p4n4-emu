"""Tests for firmware-like devices (scenario `mcu:`)."""

import json
import random
import threading
from types import SimpleNamespace

import pytest

from p4n4_emu.sim import mcu as mcu_mod
from p4n4_emu.sim import sensor_sim
from p4n4_emu.sim.mcu import McuNode
from p4n4_emu.sim.scenario import Mcu, ScenarioError, parse_scenario


class FakeClient:
    """Enough of paho's client to follow a node's connection and what it publishes."""

    def __init__(self, client_id=None, now=lambda: 0.0, reachable=True):
        self.client_id = client_id
        self.now = now
        self.reachable = reachable
        self.connected = False
        self.will = None
        self.published = []  # (time, topic, payload, qos, retain)
        self.events = []  # (time, what)
        self.subscriptions = []
        self.on_disconnect = None

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def connect_async(self, *a, **kw):
        self.events.append((self.now(), "connect"))

    def loop_start(self):
        self.connected = self.reachable

    def loop_stop(self):
        self.connected = False

    def is_connected(self):
        return self.connected

    def disconnect(self, reasoncode=None):
        will = reasoncode is not None and reasoncode.value == 4
        self.events.append((self.now(), "drop" if will else "disconnect"))
        self.connected = False
        if self.on_disconnect is not None:
            code = reasoncode or SimpleNamespace(is_failure=False)  # paho always passes one
            self.on_disconnect(self, None, None, code, None)

    def subscribe(self, topic, qos=0):
        self.subscriptions.append(topic)
        if getattr(self, "on_subscribe", None):
            self.on_subscribe(self, None, 1, [], None)

    def publish(self, topic, payload, qos=0, retain=False):
        assert self.connected, f"published {topic} while disconnected"
        self.published.append((self.now(), topic, payload, qos, retain))
        return SimpleNamespace(wait_for_publish=lambda timeout=None: True)

    def topics(self, prefix=""):
        return [(t, topic) for t, topic, *_ in self.published if topic.startswith(prefix)]


class Rig:
    """A node, its fake client and a clock; run(until) ticks it like the simulator does."""

    def __init__(self, mcu, interval=10.0, readings=None, reachable=True, seed=1):
        self.now = 0.0
        self.client = FakeClient("node", lambda: self.now, reachable)

        def default_readings(now):
            return [(0.0, "sensors/node/temperature", json.dumps({"value": now}))]

        self.node = McuNode(
            "node", mcu, interval, readings or default_readings,
            new_client=lambda cid: self.client,
            connect=lambda c: (c.connect_async(), c.loop_start()),
            rng=random.Random(seed), start=0.0, qos=1,
        )

    def run(self, until):
        while self.node.due <= until:
            self.now = self.node.due
            for delay, topic, payload in self.node.tick(self.now):
                self.node.publish(topic, payload)
        self.now = until

    def readings(self):
        return [(t, json.loads(p)["value"]) for t, topic, p, *_ in self.client.published
                if topic.startswith("sensors/")]


# ── the scenario ──────────────────────────────────────────────────────────────

def test_mcu_settings():
    sc = parse_scenario({"devices": [
        {"id": "a", "mcu": True},
        {"id": "b", "mcu": {"type": "esp8266", "firmware": 2.1, "location": "barn", "boot": 0,
                            "sleep": 300, "wifi_drop": {"probability": 0.05, "duration": 10},
                            "buffer": 20, "availability": False}},
        {"id": "c"},
    ]})
    a, b, c = sc.devices
    assert a.mcu == Mcu()
    assert b.mcu == Mcu("esp8266", "2.1", "barn", 0.0, 300.0, 0.05, 10.0, 20, False)
    assert c.mcu is None


@pytest.mark.parametrize(
    ("mcu", "error"),
    [
        ({"sleep": 0}, "mcu.sleep: must be positive"),
        ({"boot": -1}, "mcu.boot: can't be negative"),
        ({"wifi_drop": {"probability": 2}}, "wifi_drop.probability: must be between 0 and 1"),
        ({"wifi_drop": {"every": 2}}, "unknown key(s) every"),
        ({"buffer": -3}, "mcu.buffer: can't be negative"),
        ({"ota": True}, "unknown key(s) ota"),
        ({"type": ["x"]}, "mcu.type: expected text"),
    ],
)
def test_mcu_errors_name_the_key(mcu, error):
    with pytest.raises(ScenarioError) as e:
        parse_scenario({"devices": [{"id": "a", "mcu": mcu}]})
    assert error in str(e.value)


# ── always on ─────────────────────────────────────────────────────────────────

def test_boots_announces_and_registers_until_confirmed():
    rig = Rig(Mcu(boot=3.0, location="barn"))
    assert rig.client.will == ("devices/node/availability", "offline", 1, True)
    rig.run(25)
    assert rig.readings() == [(3.0, 3.0), (13.0, 13.0), (23.0, 23.0)]
    assert rig.client.topics("devices/") == [
        (3.0, "devices/node/availability"), (3.0, "devices/node/register"),
    ]
    reg = next(p for _, topic, p, *_ in rig.client.published if topic.endswith("register"))
    assert json.loads(reg) == {"type": "esp32", "firmware": "p4n4-emu", "location": "barn"}
    assert rig.client.subscriptions == ["devices/node/status"]

    # A reconnect announces again, and registers again while unconfirmed
    rig.node._on_connect(None, None, None, SimpleNamespace(is_failure=False), None)
    rig.run(35)
    assert [t for t, _ in rig.client.topics("devices/node/register")] == [3.0, 33.0]

    msg = SimpleNamespace(payload=json.dumps({"status": "registered", "device": "node"}).encode())
    rig.node._on_message(None, None, msg)
    assert rig.node.registered
    rig.node._on_connect(None, None, None, SimpleNamespace(is_failure=False), None)
    rig.run(45)
    assert [t for t, _ in rig.client.topics("devices/node/register")] == [3.0, 33.0]
    assert [t for t, _ in rig.client.topics("devices/node/availability")] == [3.0, 33.0, 43.0]


def test_wifi_drop_sends_the_will_and_loses_readings():
    rig = Rig(Mcu(boot=1.0, drop_probability=1.0, drop_duration=25.0), interval=10.0)
    rig.run(60)
    # Readings at 1 (then the drop), 11 and 21 are taken offline and lost; at 26 the
    # link is back, readings resume once it's up (27), and the next round drops again
    assert rig.readings() == [(1.0, 1.0), (27.0, 27.0), (53.0, 53.0)]
    assert [e for e in rig.client.events if e[1] != "connect"] == [
        (1.0, "drop"), (27.0, "drop"), (53.0, "drop"),
    ]
    assert [t for t, e in rig.client.events if e == "connect"] == [0.0, 26.0, 52.0]


def test_a_buffer_keeps_the_readings_taken_offline():
    rig = Rig(Mcu(boot=1.0, drop_probability=1.0, drop_duration=25.0, buffer=1), interval=10.0)
    rig.run(30)
    # Two readings were taken offline (11, 21); a one-reading buffer kept the newest
    assert rig.readings() == [(1.0, 1.0), (27.0, 21.0), (27.0, 27.0)]


def test_readings_while_the_broker_is_unreachable_are_buffered():
    rig = Rig(Mcu(boot=1.0, buffer=10), reachable=False)
    rig.run(25)
    assert rig.readings() == []
    assert len(rig.node.buffer) == 3
    rig.client.connected = True
    rig.run(35)
    assert [v for _, v in rig.readings()] == [1.0, 11.0, 21.0, 31.0]


def test_no_availability_topic():
    rig = Rig(Mcu(availability=False))
    rig.run(5)
    assert rig.client.will is None
    assert rig.client.topics("devices/node/availability") == []


# ── deep sleep ────────────────────────────────────────────────────────────────

def test_deep_sleep_cycle():
    rig = Rig(Mcu(boot=2.0, sleep=60.0))
    rig.run(150)
    # Wake at 0, read at 2, wait for the registration answer until 4, sleep 60 s
    assert rig.readings() == [(2.0, 2.0), (66.0, 66.0), (130.0, 130.0)]
    assert rig.client.events == [
        (0.0, "connect"), (4.0, "disconnect"),
        (64.0, "connect"), (68.0, "disconnect"),
        (128.0, "connect"), (132.0, "disconnect"),
    ]
    sleeping = [t for t, topic, p, *_ in rig.client.published
                if topic.endswith("availability") and p == "sleeping"]
    assert sleeping == [4.0, 68.0, 132.0]


def test_a_registered_sleeper_goes_back_to_sleep_at_once():
    rig = Rig(Mcu(boot=2.0, sleep=60.0))
    rig.node.registered = True
    rig.run(63)
    assert rig.client.events == [(0.0, "connect"), (2.0, "disconnect"), (62.0, "connect")]


def test_a_sleeper_without_wifi_keeps_its_reading():
    rig = Rig(Mcu(boot=2.0, sleep=60.0, drop_probability=1.0, buffer=5))
    rig.node.registered = True
    rig.run(130)
    assert rig.client.events == []  # never joined
    assert len(rig.node.buffer) == 3
    rig.node.mcu = Mcu(boot=2.0, sleep=60.0, buffer=5)
    rig.run(200)
    # Woken at 180 with Wi-Fi: the held readings go out before the new one
    assert [v for _, v in rig.readings()] == [0.0, 60.0, 120.0, 182.0]


# ── in the simulator ──────────────────────────────────────────────────────────

def test_the_simulator_gives_each_mcu_device_its_own_client(monkeypatch):
    import paho.mqtt.client as mqtt

    clients = {}

    def make(*a, **kw):
        c = FakeClient(kw.get("client_id"), lambda: now[0])
        c.protocol = kw.get("protocol")
        for name in ("on_connect", "on_connect_fail", "on_disconnect", "on_message"):
            setattr(c, name, None)
        c.reconnect_delay_set = lambda *a: None
        clients[kw.get("client_id")] = c
        return c

    monkeypatch.setattr(sensor_sim.mqtt, "Client", make)
    stop = threading.Event()
    now = [1000.0]

    def wait(t):
        now[0] += t
        if now[0] > 1030:
            stop.set()

    monkeypatch.setattr(stop, "wait", wait)
    sc = parse_scenario({"interval": 10, "devices": [
        {"id": "plain", "measurements": ["temperature"]},
        {"id": "esp-{n}", "count": 2, "measurements": ["temperature"],
         "mcu": {"boot": 5}, "faults": [{"type": "delay", "seconds": 3}]},
    ]})
    sensor_sim.run(stop=stop, clock=lambda: now[0], scenario=sc)
    assert set(clients) == {None, "esp-0", "esp-1"}
    assert clients["esp-0"].protocol == mqtt.MQTTv5
    assert [t - 1000 for t, *_ in clients[None].topics("sensors/")] == [0, 10, 20, 30]
    esp = [(t - 1000, topic) for t, topic in clients["esp-0"].topics()]
    assert esp[:2] == [(5, "devices/esp-0/availability"), (5, "devices/esp-0/register")]
    # Readings at 5, 15, 25, held 3 s by the delay fault, on the device's own connection
    assert [t for t, topic in esp if topic.startswith("sensors/")] == [8, 18, 28]
    assert not clients["esp-0"].connected  # stopped with the simulator


def test_reply_wait_is_short():
    assert 0 < mcu_mod.REPLY_WAIT <= 5
