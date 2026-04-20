"""Synthetic sensor waveform generators."""

from __future__ import annotations

import math
import random
import time
from collections.abc import Iterator


def _wave(base: float, amplitude: float, noise: float, period: float = 300.0) -> Iterator[float]:
    """Sinusoid + Gaussian noise iterator."""
    while True:
        t = time.time()
        value = base + amplitude * math.sin(2 * math.pi * t / period)
        value += random.gauss(0, noise)
        yield round(value, 3)


def temperature_c(
    base: float = 22.0, amplitude: float = 3.0, noise: float = 0.1
) -> Iterator[float]:
    return _wave(base, amplitude, noise)


def humidity_pct(
    base: float = 55.0, amplitude: float = 10.0, noise: float = 0.5
) -> Iterator[float]:
    gen = _wave(base, amplitude, noise)
    while True:
        yield max(10.0, min(95.0, next(gen)))


def pressure_hpa(
    base: float = 1013.25, amplitude: float = 2.0, noise: float = 0.05
) -> Iterator[float]:
    return _wave(base, amplitude, noise)


def accelerometer_xyz(
    g_bias: tuple[float, float, float] = (0.0, 0.0, 1.0),
    noise: float = 0.02,
) -> Iterator[tuple[float, float, float]]:
    while True:
        x = round(g_bias[0] + random.gauss(0, noise), 4)
        y = round(g_bias[1] + random.gauss(0, noise), 4)
        z = round(g_bias[2] + random.gauss(0, noise), 4)
        yield (x, y, z)


def cpu_load_pct(base: float = 45.0, amplitude: float = 20.0) -> Iterator[float]:
    gen = _wave(base, amplitude, noise=1.0, period=60.0)
    while True:
        yield max(0.0, min(100.0, next(gen)))
