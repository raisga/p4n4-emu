"""GPIO over MQTT: drive the emulated board's inputs and watch its outputs from outside.

    emu/gpio/<pin>/set     1 / 0 (or high / low, on / off, true / false) drives
                           an input; "release" stops driving it, so it reads its pull
    emu/gpio/<pin>/state   the pin's level, published (retained) on every change
    emu/gpio/<pin>/pwm     {"frequency": Hz, "duty": %} while PWM runs, {} once it stops

Pins are BCM GPIO numbers whatever numbering the script uses. A test, Node-RED
or a dashboard can press a button or read an LED this way; `p4n4-emu run
--gpio-mqtt HOST[:PORT]` starts the bridge inside the script's process.
"""

from __future__ import annotations

import json
import logging

import paho.mqtt.client as mqtt

from p4n4_emu.hw import pins

_log = logging.getLogger(__name__)

DEFAULT_PREFIX = "emu/gpio"

_LEVELS = {
    "1": 1, "high": 1, "on": 1, "true": 1,
    "0": 0, "low": 0, "off": 0, "false": 0,
    "release": None, "none": None,
}


def parse_level(payload: bytes) -> int | None:
    text = payload.decode(errors="replace").strip().lower()
    if text not in _LEVELS:
        raise ValueError(f"not a level: {text!r} (expected 1 / 0, high / low or release)")
    return _LEVELS[text]


class GpioBridge:
    def __init__(self, host: str, port: int = 1883, prefix: str = DEFAULT_PREFIX,
                 client: mqtt.Client | None = None) -> None:
        self.host = host
        self.port = port
        self.prefix = prefix.rstrip("/")
        self.client = client or mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                            client_id="p4n4-emu-gpio")
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(1, 30)

    def start(self) -> None:
        pins.watch(self.on_change)
        self.client.connect_async(self.host, self.port)
        self.client.loop_start()

    def stop(self) -> None:
        pins.unwatch(self.on_change)
        self.client.loop_stop()
        self.client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code.is_failure:
            _log.warning("GPIO bridge: broker refused the connection (%s)", reason_code)
            return
        client.subscribe(f"{self.prefix}/+/set")
        # Publish what the pins are now, so a subscriber has them without waiting
        for gpio, pin in pins.claimed().items():
            self.publish_state(gpio, pin)
        _log.info("GPIO bridge: connected to %s:%d", self.host, self.port)

    def _on_message(self, client, userdata, msg: mqtt.MQTTMessage) -> None:
        parts = msg.topic[len(self.prefix) + 1:].split("/")
        try:
            gpio = int(parts[0])
            pins.drive(gpio, parse_level(msg.payload))
        except (ValueError, IndexError) as e:
            _log.warning("GPIO bridge: ignoring %s: %s", msg.topic, e)

    def publish_state(self, gpio: int, pin: pins.Pin) -> None:
        self.client.publish(f"{self.prefix}/{gpio}/state", str(pin.level), qos=1, retain=True)

    def on_change(self, change: pins.Change) -> None:
        if change.edge or change.pin.function != change.previous.function:
            self.publish_state(change.gpio, change.pin)
        if change.pin.pwm != change.previous.pwm:
            pwm = change.pin.pwm
            payload = {"frequency": pwm[0], "duty": pwm[1]} if pwm else {}
            self.client.publish(f"{self.prefix}/{change.gpio}/pwm", json.dumps(payload),
                                qos=1, retain=True)


def parse_address(address: str) -> tuple[str, int]:
    """HOST[:PORT] → (host, port), port 1883 by default."""
    host, _, port = address.rpartition(":") if ":" in address else (address, "", "")
    if not host:
        raise ValueError(f"expected HOST[:PORT], got {address!r}")
    try:
        return host, int(port) if port else 1883
    except ValueError:
        raise ValueError(f"expected HOST[:PORT], got {address!r}") from None
