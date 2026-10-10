"""Tests for the simulator's media feeds: camera frames and audio windows."""

import json
import math
import random
import struct
import wave

import pytest

from p4n4_emu.sim import media, scenario, sensor_sim
from p4n4_emu.sim.scenario import Feed, ScenarioError, parse_scenario


def _feed(**settings):
    doc = {"devices": [{"id": "cam", "measurements": {"camera": settings}}]}
    return parse_scenario(doc).devices[0].measurements["camera"]


def _wav(path, samples, rate=8000, width=2, channels=1):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        fmt = {1: "B", 2: "<h", 4: "<i"}[width]
        w.writeframes(b"".join(struct.pack(fmt, s) for s in samples))
    return path


# ── parsing ───────────────────────────────────────────────────────────────────

def test_image_feed_defaults():
    f = _feed(kind="image")
    assert f == Feed("image", "packed", width=96, height=96, channels=3)
    assert f.features == 96 * 96


@pytest.mark.parametrize(
    ("settings", "features"),
    [
        ({"kind": "image", "width": 32, "height": 24, "encoding": "uint8"}, 32 * 24 * 3),
        ({"kind": "image", "width": 32, "height": 24, "channels": 1, "encoding": "float"},
         32 * 24),
        ({"kind": "audio"}, 16000),
        ({"kind": "audio", "sample_rate": 8000, "window": 0.5}, 4000),
    ],
)
def test_feature_counts(settings, features):
    assert _feed(**settings).features == features


@pytest.mark.parametrize(
    ("settings", "error"),
    [
        ({"kind": "video"}, "kind: expected one of image, audio"),
        ({"kind": "image", "sample_rate": 8000}, "unknown key(s) sample_rate"),
        ({"kind": "image", "channels": 2}, "channels: expected one of 1, 3"),
        ({"kind": "image", "encoding": "int16"}, "encoding: expected one of packed, uint8, float"),
        ({"kind": "audio", "window": 0}, "window: must be positive"),
        ({"kind": "audio", "noise": 2}, "noise: must be between 0 and 1"),
        ({"kind": "audio", "file": "missing.wav"}, "file: no such file 'missing.wav'"),
    ],
)
def test_feed_errors_name_the_key(settings, error):
    with pytest.raises(ScenarioError) as e:
        _feed(**settings)
    assert error in str(e.value)


def test_value_faults_refuse_a_feed():
    doc = {"devices": [{"id": "cam", "measurements": {"camera": {"kind": "image"}},
                        "faults": [{"type": "spike", "measurement": "camera", "magnitude": 1}]}]}
    with pytest.raises(ScenarioError, match="not raw or a feed"):
        parse_scenario(doc)


def test_a_wav_file_is_relative_to_the_scenario(tmp_path):
    (tmp_path / "clips").mkdir()
    _wav(tmp_path / "clips" / "door.wav", [0] * 800, rate=8000)
    path = tmp_path / "scenario.yml"
    path.write_text(
        "devices:\n  - id: mic\n    measurements:\n"
        "      mic: {kind: audio, file: clips/door.wav, window: 0.05}\n"
    )
    sc = scenario.load_scenario(path)
    f = sc.devices[0].measurements["mic"]
    assert f.sample_rate == 8000 and f.features == 400  # the file's rate wins
    assert sc.files == (("clips/door.wav", str(tmp_path / "clips" / "door.wav")),)


def test_a_wav_file_through_the_file_map(tmp_path):
    mounted = _wav(tmp_path / "0-door.wav", [0] * 100)
    path = tmp_path / "s.yml"
    path.write_text(
        "devices:\n  - id: mic\n    measurements:\n      mic: {kind: audio, file: /host/door.wav}\n"
    )
    sc = scenario.load_scenario(path, files={"/host/door.wav": str(mounted)})
    assert sc.devices[0].measurements["mic"].file == str(mounted)


def test_not_a_wav_file(tmp_path):
    (tmp_path / "x.wav").write_text("not audio")
    doc = {"devices": [{"id": "m", "measurements": {"m": {"kind": "audio", "file": "x.wav"}}}]}
    with pytest.raises(ScenarioError, match="not a PCM WAV file"):
        parse_scenario(doc, base=tmp_path)


# ── frames and windows ────────────────────────────────────────────────────────

def test_packed_rgb_frames_show_a_moving_blob():
    f = Feed("image", "packed", width=24, height=24, noise=0.0)
    frames = media.feed(f, random.Random(1))
    first, later = next(frames), [next(frames) for _ in range(20)][-1]
    assert len(first) == 24 * 24
    blob = (255 << 16) | (180 << 8) | 40
    background = (60 << 16) | (60 << 8) | 60
    assert set(first) == {blob, background}
    assert first != later  # it moved


def test_uint8_and_float_frames():
    gray = next(media.feed(Feed("image", "uint8", width=8, height=8, channels=1), random.Random()))
    assert len(gray) == 64 and all(0 <= v <= 255 and isinstance(v, int) for v in gray)
    rgb = next(media.feed(Feed("image", "float", width=8, height=8), random.Random()))
    assert len(rgb) == 8 * 8 * 3 and all(0.0 <= v <= 1.0 for v in rgb)


def test_tone_windows_continue_the_tone():
    f = Feed("audio", "float", sample_rate=8000, window=0.01, frequency=1000, amplitude=0.5,
             noise=0.0)
    windows = media.feed(f, random.Random())
    a, b = next(windows), next(windows)
    assert len(a) == 80
    expected = [0.5 * math.sin(2 * math.pi * 1000 * i / 8000) for i in range(160)]
    assert a + b == pytest.approx(expected, abs=1e-4)
    loud = next(media.feed(Feed("audio", "int16", amplitude=1.0, noise=0.0), random.Random()))
    assert max(loud) == 32767


def test_wav_windows_loop_over_the_file(tmp_path):
    path = _wav(tmp_path / "a.wav", [0, 16384, -16384, 32767, -32768], rate=10)
    f = Feed("audio", "int16", sample_rate=10, window=0.3, file=str(path))
    windows = media.feed(f, random.Random())
    assert next(windows) == [0, 16384, -16384]
    assert next(windows) == [32767, -32768, 0]  # wraps around, samples exact


@pytest.mark.parametrize(("width", "samples", "expected"), [
    (1, [128, 255, 0], [0.0, 127 / 128, -1.0]),
    (4, [0, 2**30, -(2**31)], [0.0, 0.5, -1.0]),
])
def test_read_wav_sample_widths(tmp_path, width, samples, expected):
    path = _wav(tmp_path / "w.wav", samples, width=width)
    assert media.read_wav(str(path)) == pytest.approx(expected)


def test_stereo_is_mixed_down(tmp_path):
    path = _wav(tmp_path / "s.wav", [16384, 0, -16384, -16384], channels=2)
    assert media.read_wav(str(path)) == pytest.approx([0.25, -0.5])


def test_the_simulator_publishes_feeds_as_values():
    sc = parse_scenario({"seed": 3, "devices": [{"id": "cam", "measurements": {
        "camera": {"kind": "image", "width": 4, "height": 4},
        "mic": {"kind": "audio", "sample_rate": 100, "window": 0.1},
    }}]})
    sim = sensor_sim.DeviceSim(sc.devices[0], sc, random.Random(), start=0.0)
    out = {topic: json.loads(payload) for _, topic, payload in sim.readings(0.0)}
    assert set(out) == {"sensors/cam/camera", "sensors/cam/mic"}
    assert len(out["sensors/cam/camera"]["values"]) == 16
    assert len(out["sensors/cam/mic"]["values"]) == 10
    assert set(out["sensors/cam/camera"]) == {"values"}  # Node-RED stores nothing of it
    # The same seed gives the same frames
    again = sensor_sim.DeviceSim(sc.devices[0], sc, random.Random(), start=0.0)
    assert json.loads(again.readings(0.0)[0][2]) == out["sensors/cam/camera"]
