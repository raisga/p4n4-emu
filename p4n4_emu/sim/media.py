"""Camera frames and audio windows for a scenario's media feeds (scenario.Feed).

Each reading of a feed is a flat feature vector, published as {"values": [...]} for
the edge runner. Frames show a bright blob drifting over a noisy background, so a
model sees something change; audio is a tone plus noise, or the next window of a WAV
file. Standard library only: the simulator image has no NumPy or Pillow.
"""

from __future__ import annotations

import math
import random
import wave
from collections.abc import Iterator

from p4n4_emu.sim.scenario import Feed

# The blob's colour on RGB frames, and how far it moves per frame (in periods)
BLOB_RGB = (255, 180, 40)
_DRIFT = 0.013


def feed(f: Feed, rng: random.Random, phase: float = 0.0) -> Iterator[list[float]]:
    """Feature vectors for *f*, one per reading."""
    if f.kind == "image":
        return image_frames(f, rng, phase)
    if f.file is not None:
        return wav_windows(f)
    return tone_windows(f, rng, phase)


def image_frames(f: Feed, rng: random.Random, phase: float = 0.0) -> Iterator[list[float]]:
    radius = max(1.0, min(f.width, f.height) / 6)
    background = 60
    sigma = f.noise * 255
    n = 0
    while True:
        # A Lissajous path keeps the blob inside the frame and never quite repeats
        t = (n * _DRIFT + phase) * 2 * math.pi
        cx = (0.5 + 0.35 * math.sin(t)) * (f.width - 1)
        cy = (0.5 + 0.35 * math.sin(1.7 * t + 1.0)) * (f.height - 1)
        pixels: list[tuple[int, ...]] = []
        for y in range(f.height):
            for x in range(f.width):
                inside = (x - cx) ** 2 + (y - cy) ** 2 <= radius**2
                base = BLOB_RGB if inside else (background,) * 3
                if f.channels == 1:
                    base = (round(0.299 * base[0] + 0.587 * base[1] + 0.114 * base[2]),)
                pixels.append(tuple(_clamp8(c + rng.gauss(0, sigma)) for c in base))
        yield _encode_image(pixels, f)
        n += 1


def _clamp8(value: float) -> int:
    return max(0, min(255, round(value)))


def _encode_image(pixels: list[tuple[int, ...]], f: Feed) -> list[float]:
    if f.encoding == "packed":
        if f.channels == 1:
            return [g << 16 | g << 8 | g for (g,) in pixels]
        return [r << 16 | g << 8 | b for r, g, b in pixels]
    flat = [c for px in pixels for c in px]
    if f.encoding == "float":
        return [round(c / 255, 4) for c in flat]
    return flat


def tone_windows(f: Feed, rng: random.Random, phase: float = 0.0) -> Iterator[list[float]]:
    count = int(f.sample_rate * f.window)
    start = 0  # the tone continues from one window to the next
    offset = phase * f.sample_rate / f.frequency
    while True:
        samples = [
            f.amplitude * math.sin(2 * math.pi * f.frequency * (start + i + offset) / f.sample_rate)
            + rng.gauss(0, f.noise)
            for i in range(count)
        ]
        start += count
        yield _encode_audio(samples, f)


def wav_windows(f: Feed) -> Iterator[list[float]]:
    """Consecutive windows of the file, wrapping around to its start."""
    samples = read_wav(f.file)
    count = int(f.sample_rate * f.window)
    pos = 0
    while True:
        window = [samples[(pos + i) % len(samples)] for i in range(count)]
        pos = (pos + count) % len(samples)
        yield _encode_audio(window, f)


def _encode_audio(samples: list[float], f: Feed) -> list[float]:
    if f.encoding == "float":
        return [round(max(-1.0, min(1.0, s)), 5) for s in samples]
    return [max(-32768, min(32767, round(s * 32768))) for s in samples]


def wav_rate(path: str) -> int:
    """The sample rate of a PCM WAV file; ValueError when it isn't one."""
    try:
        with wave.open(path, "rb") as w:
            if w.getnframes() == 0:
                raise ValueError("the WAV file has no samples")
            return w.getframerate()
    except (wave.Error, EOFError) as e:
        raise ValueError(f"not a PCM WAV file ({e})") from None


def read_wav(path: str) -> list[float]:
    """A PCM WAV file's samples as floats from -1 to 1, channels mixed down to mono."""
    with wave.open(path, "rb") as w:
        width, channels = w.getsampwidth(), w.getnchannels()
        data = w.readframes(w.getnframes())
    step = width * channels
    full = float(1 << (8 * width - 1))
    out = []
    for i in range(0, len(data) - step + 1, step):
        total = 0.0
        for c in range(channels):
            chunk = data[i + c * width : i + (c + 1) * width]
            if width == 1:  # 8-bit WAV is unsigned
                value = chunk[0] - 128
            else:
                value = int.from_bytes(chunk, "little", signed=True)
            total += value / full
        out.append(total / channels)
    return out
