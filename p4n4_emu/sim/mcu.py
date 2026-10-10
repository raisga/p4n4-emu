"""Firmware-like devices: a scenario device with `mcu:` (scenario.Mcu).

A real ESP32 node isn't a perfect publisher. It boots, joins Wi-Fi and the broker
before its first reading, registers itself, may deep-sleep between readings, and
loses Wi-Fi now and then. McuNode does that on its own MQTT connection (client id =
the device id), driven by the simulator's clock through tick():

    devices/<id>/register      {"type", "firmware", "location"} on each connection,
                               until devices/<id>/status says "registered" (the ai
                               stack's onboarding flow answers there)
    devices/<id>/availability  "online" (retained) once connected, "sleeping" before a
                               deep sleep, "offline" as the will: the broker sends it
                               when the connection drops. p4n4-emu's own topic: no
                               stack reads it yet.

A Wi-Fi drop disconnects with the will (MQTT 5's "disconnect with will message"),
so subscribers see "offline" as they would on a lost connection, then reconnects
after the drop. Readings taken while offline are lost, or held in a buffer of
`buffer` readings (oldest dropped first) and published on reconnect.
"""

from __future__ import annotations

import json
import logging
import random
import threading
from collections import deque
from collections.abc import Callable

import paho.mqtt.client as mqtt
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from p4n4_emu.sim.scenario import Mcu

_log = logging.getLogger(__name__)

ONLINE, SLEEPING, OFFLINE = "online", "sleeping", "offline"
# How long a deep-sleeping device stays awake for the answer to its registration
REPLY_WAIT = 2.0
# How long disconnecting waits, at most, for the broker's acknowledgements and for the
# DISCONNECT to go out. paho closes the socket as soon as DISCONNECT is written: with
# an acknowledgement still unread, the kernel resets the connection, and the broker
# drops what it hadn't read yet (the last readings) and sends the will instead
FLUSH_WAIT = 2.0


class McuNode:
    """One firmware-like device. *readings(now)* gives (delay, topic, payload) for a
    round; *new_client(client_id)* makes its MQTT client (MQTT 5, with the broker's
    login) and *connect(client)* starts it (connect_async, loop_start)."""

    def __init__(
        self,
        device_id: str,
        mcu: Mcu,
        interval: float,
        readings: Callable[[float], list[tuple[float, str, str]]],
        new_client: Callable[[str], mqtt.Client],
        connect: Callable[[mqtt.Client], None],
        rng: random.Random,
        start: float,
        qos: int = 0,
        retain: bool = False,  # noqa: FBT001, FBT002
    ) -> None:
        self.id = device_id
        self.mcu = mcu
        self.interval = interval
        self.readings = readings
        self.rng = rng
        self.qos = qos
        self.retain = retain
        self.registered = False
        self.buffer: deque[tuple[str, str]] = deque(maxlen=mcu.buffer or None)
        self._connect = connect
        self.base = f"devices/{device_id}"

        self.client = new_client(device_id)
        self._log_disconnect = getattr(self.client, "on_disconnect", None)
        self._disconnected = threading.Event()
        self._subscribed = threading.Event()
        self._unacked: list = []  # QoS 1 publishes of the announcement
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.on_disconnect = self._on_disconnect
        self.client.on_subscribe = lambda *a: self._subscribed.set()
        if mcu.availability:
            self.client.will_set(f"{self.base}/availability", OFFLINE, qos=1, retain=True)
        self.awake = False  # the radio is on and the client running
        self.announced = False  # registered / online announced on this connection
        self.read = False  # a deep-sleeping device read its sensors this wake-up
        self.offline_until = 0.0  # a Wi-Fi drop lasts until then
        self.due = start  # power-on: the first tick joins Wi-Fi, as every wake-up does

    # ── the connection ───────────────────────────────────────────────────────

    def _wake(self, now: float) -> None:
        self._connect(self.client)
        self.awake = True
        self.announced = False

    def _sleep(self, status: str | None) -> None:
        """Disconnect: cleanly before a deep sleep, with the will for a Wi-Fi drop."""
        if status == SLEEPING and self.mcu.availability and self.connected():
            self._unacked.append(
                self.client.publish(f"{self.base}/availability", SLEEPING, qos=1, retain=True)
            )
        if self.connected():
            self._settle()
        self._disconnected.clear()
        was_connected = self.connected()  # disconnect() makes is_connected() false at once
        if status == OFFLINE:
            self.client.disconnect(reasoncode=ReasonCode(PacketTypes.DISCONNECT, identifier=4))
        else:
            self.client.disconnect()
        if was_connected:
            # What was published before goes out first: paho sends packets in order
            self._disconnected.wait(FLUSH_WAIT)
        self.client.loop_stop()
        self.awake = False

    def _settle(self) -> None:
        """Wait for the broker to acknowledge what it will answer, so no answer is
        unread when paho closes the socket."""
        for info in self._unacked:
            try:
                info.wait_for_publish(FLUSH_WAIT)
            except (RuntimeError, ValueError):
                pass  # the connection went meanwhile
        self._unacked.clear()
        if self.announced:
            self._subscribed.wait(FLUSH_WAIT)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        self._disconnected.set()
        if self._log_disconnect is not None and self.awake:
            self._log_disconnect(client, userdata, flags, reason_code, properties)

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            _log.warning("%s: broker refused the connection: %s", self.id, reason_code)
            return
        # Announce again on the next round: the broker may have sent the will meanwhile
        self.announced = False

    def _on_message(self, client, userdata, msg) -> None:
        try:
            status = json.loads(msg.payload).get("status")
        except (ValueError, AttributeError):
            return
        if status == "registered" and not self.registered:
            self.registered = True
            _log.info("%s: registered", self.id)

    def _announce(self) -> None:
        if self.announced:
            return
        self._subscribed.clear()
        self.client.subscribe(f"{self.base}/status", qos=1)
        if self.mcu.availability:
            self._unacked.append(
                self.client.publish(f"{self.base}/availability", ONLINE, qos=1, retain=True)
            )
        if not self.registered:
            info = {"type": self.mcu.type, "firmware": self.mcu.firmware}
            if self.mcu.location is not None:
                info["location"] = self.mcu.location
            self._unacked.append(
                self.client.publish(f"{self.base}/register", json.dumps(info), qos=1)
            )
        self.announced = True

    def connected(self) -> bool:
        return self.awake and self.client.is_connected()

    # ── publishing ───────────────────────────────────────────────────────────

    def publish(self, topic: str, payload: str) -> None:
        """Publish now, or hold the reading while offline (a late `delay` fault too)."""
        if self.connected():
            self.client.publish(topic, payload, qos=self.qos, retain=self.retain)
        elif self.mcu.buffer:
            self.buffer.append((topic, payload))

    def _flush(self) -> None:
        while self.buffer:
            topic, payload = self.buffer.popleft()
            self.client.publish(topic, payload, qos=self.qos, retain=self.retain)

    # ── the schedule ─────────────────────────────────────────────────────────

    def _drops(self) -> bool:
        p = self.mcu.drop_probability
        return p > 0 and (p >= 1 or self.rng.random() < p)

    def tick(self, now: float) -> list[tuple[float, str, str]]:
        """Do what is due at *now*; returns readings to publish later (`delay` faults),
        as (delay, topic, payload). Call again at self.due."""
        if now < self.due:
            return []
        if self.mcu.sleep is None:
            return self._always_on(now)
        return self._deep_sleep(now)

    def _always_on(self, now: float) -> list[tuple[float, str, str]]:
        if not self.awake and now >= self.offline_until:
            self._wake(now)  # Wi-Fi is back: reconnect, read once the link is up
            self.due = now + self.mcu.boot
            return []
        late = self._round(now)
        if self.awake and self._drops():
            _log.info("%s: Wi-Fi lost for %gs", self.id, self.mcu.drop_duration)
            self._sleep(OFFLINE)
            self.offline_until = now + self.mcu.drop_duration
        self.due = now + self.interval
        if not self.awake:
            # Keep reading on schedule while offline, and reconnect when the drop ends
            self.due = min(self.due, max(self.offline_until, now))
        return late

    def _deep_sleep(self, now: float) -> list[tuple[float, str, str]]:
        if not self.awake:
            # Waking up: sensors are read at once, Wi-Fi may fail to join this time
            if self._drops():
                _log.info("%s: no Wi-Fi this wake-up", self.id)
                late = self._round(now)
                self.due = now + self.mcu.sleep
                return late
            self._wake(now)
            self.read = False
            self.due = now + self.mcu.boot
            return []
        late = []
        if not self.read:
            late = self._round(now)
            self.read = True
            if not self.registered and self.connected():
                self.due = now + REPLY_WAIT  # stay up for the onboarding flow's answer
                return late
        self._sleep(SLEEPING)
        self.due = now + self.mcu.sleep
        return late

    def _round(self, now: float) -> list[tuple[float, str, str]]:
        """Take one round of readings: publish them (after anything held) or hold them."""
        if self.connected():
            self._announce()
            self._flush()
        late = []
        for delay, topic, payload in self.readings(now):
            if delay > 0:
                late.append((delay, topic, payload))
            else:
                self.publish(topic, payload)
        return late

    def stop(self) -> None:
        if self.awake:
            self._sleep(None)
