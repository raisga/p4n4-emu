"""Fault injection: what a scenario's faults do to one device's readings.

Each fault is rolled for every reading it applies to, with its own probability. A
`dropout` without a measurement is the exception: it is rolled once per round, and
takes the whole device off the air, as a real device losing power or Wi-Fi would.
"""

from __future__ import annotations

import json
import random

from p4n4_emu.sim.scenario import Fault

# How a malformed fault breaks a payload; each one is picked with equal odds
MALFORMED = ("truncated", "not-json", "string-value", "no-value")


class FaultInjector:
    def __init__(self, faults: tuple[Fault, ...], rng: random.Random, start: float) -> None:
        self.faults = faults
        self.rng = rng
        self.start = start
        self._stuck: dict[str, tuple[float, float]] = {}  # measurement → (value, until)
        self._silent: dict[str | None, float] = {}  # measurement (None: device) → until

    def _matching(self, kind: str, measurement: str | None) -> list[Fault]:
        return [
            f for f in self.faults
            if f.type == kind and f.measurement in (None, measurement)
        ]

    def _roll(self, fault: Fault) -> bool:
        return fault.probability >= 1 or self.rng.random() < fault.probability

    def device_silent(self, now: float) -> bool:
        """Whether the device publishes nothing this round."""
        if self._silent.get(None, 0) > now:
            return True
        for f in self.faults:
            if f.type == "dropout" and f.measurement is None and self._roll(f):
                self._silent[None] = now + f.duration
                return True
        return False

    def dropped(self, measurement: str, now: float) -> bool:
        if self._silent.get(measurement, 0) > now:
            return True
        for f in self.faults:
            if f.type == "dropout" and f.measurement == measurement and self._roll(f):
                self._silent[measurement] = now + f.duration
                return True
        return False

    def value(self, measurement: str, value: float, now: float) -> float:
        """The reading after drift, a stuck sensor and spikes."""
        for f in self._matching("drift", measurement):
            value += f.rate * (now - self.start)

        stuck = self._stuck.get(measurement)
        if stuck and stuck[1] > now:
            return stuck[0]
        for f in self._matching("stuck", measurement):
            if self._roll(f):
                self._stuck[measurement] = (value, now + f.duration)
                return value

        for f in self._matching("spike", measurement):
            if self._roll(f):
                value += self.rng.choice((-1, 1)) * f.magnitude
        return round(value, 3)

    def delay(self, measurement: str) -> float:
        """Seconds to hold the reading back (0: publish it now)."""
        return max(
            (f.seconds for f in self._matching("delay", measurement) if self._roll(f)),
            default=0.0,
        )

    def payload(self, measurement: str, payload: dict) -> str:
        """The payload as JSON, broken if a malformed fault fires."""
        text = json.dumps(payload)
        if not any(self._roll(f) for f in self._matching("malformed", measurement)):
            return text
        kind = self.rng.choice(MALFORMED)
        if kind == "truncated":
            return text[: len(text) // 2]
        if kind == "not-json":
            return f"value={payload.get('value', 'nan')}"
        if kind == "string-value" and "value" in payload:
            return json.dumps({**payload, "value": str(payload["value"])})
        return json.dumps({k: v for k, v in payload.items() if k not in ("value", "values")})
