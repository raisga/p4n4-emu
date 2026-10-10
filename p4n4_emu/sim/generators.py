"""Synthetic sensor waveform generators."""

from __future__ import annotations

import math
import random
import time
from collections.abc import Iterator

from p4n4_emu.sim.scenario import Wave


def device_phase(device_id: str) -> float:
    """The fraction of a period a device's curves are shifted by (0.0–1.0).

    The same id always gets the same phase, so a restarted simulator keeps each
    device on its own curve, and the emulated parts of `p4n4-emu run` follow
    the curve the simulator publishes for that device.
    """
    return random.Random(device_id).random()


def _wave(
    base: float, amplitude: float, noise: float, period: float = 300.0, phase: float = 0.0
) -> Iterator[float]:
    """Sinusoid + Gaussian noise iterator.

    *phase* shifts the curve by that fraction of a period (0.0–1.0), so
    devices sampled at the same moment don't all report the same value.
    """
    while True:
        t = time.time()
        value = base + amplitude * math.sin(2 * math.pi * (t / period + phase))
        value += random.gauss(0, noise)
        yield round(value, 3)


def wave(w: Wave, phase: float = 0.0) -> Iterator[float]:
    """A scenario measurement: _wave() clamped to the wave's min / max."""
    gen = _wave(w.base, w.amplitude, w.noise, w.period, phase)
    while True:
        value = next(gen)
        if w.min is not None:
            value = max(w.min, value)
        if w.max is not None:
            value = min(w.max, value)
        yield value


def temperature_c(
    base: float = 22.0, amplitude: float = 3.0, noise: float = 0.1, phase: float = 0.0
) -> Iterator[float]:
    return _wave(base, amplitude, noise, phase=phase)


def humidity_pct(
    base: float = 55.0, amplitude: float = 10.0, noise: float = 0.5, phase: float = 0.0
) -> Iterator[float]:
    gen = _wave(base, amplitude, noise, phase=phase)
    while True:
        yield max(10.0, min(95.0, next(gen)))


def pressure_hpa(
    base: float = 1013.25, amplitude: float = 2.0, noise: float = 0.05, phase: float = 0.0
) -> Iterator[float]:
    return _wave(base, amplitude, noise, phase=phase)


def accelerometer_xyz(
    g_bias: tuple[float, float, float] = (0.0, 0.0, 1.0),
    noise: float = 0.02,
) -> Iterator[tuple[float, float, float]]:
    while True:
        x = round(g_bias[0] + random.gauss(0, noise), 4)
        y = round(g_bias[1] + random.gauss(0, noise), 4)
        z = round(g_bias[2] + random.gauss(0, noise), 4)
        yield (x, y, z)


def cpu_load_pct(
    base: float = 45.0, amplitude: float = 20.0, phase: float = 0.0
) -> Iterator[float]:
    gen = _wave(base, amplitude, noise=1.0, period=60.0, phase=phase)
    while True:
        yield max(0.0, min(100.0, next(gen)))
