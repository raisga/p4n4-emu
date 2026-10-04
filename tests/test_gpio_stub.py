"""Tests for the RPi.GPIO drop-in stub."""

import pytest

import p4n4_emu.hw.gpio_stub as GPIO


def setup_function():
    GPIO.cleanup()


def test_constants():
    assert GPIO.BCM == 11
    assert GPIO.BOARD == 10
    assert GPIO.OUT == 0
    assert GPIO.IN == 1
    assert GPIO.HIGH == 1
    assert GPIO.LOW == 0


def test_setmode_does_not_raise():
    GPIO.setmode(GPIO.BCM)
    GPIO.setmode(GPIO.BOARD)


def test_setwarnings_does_not_raise():
    GPIO.setwarnings(True)
    GPIO.setwarnings(False)


def test_setup_and_output():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(17, GPIO.OUT)
    GPIO.output(17, GPIO.HIGH)
    assert GPIO.input(17) == GPIO.HIGH


def test_output_low():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(17, GPIO.OUT)
    GPIO.output(17, GPIO.LOW)
    assert GPIO.input(17) == GPIO.LOW


def test_input_reflects_last_output():
    GPIO.setup(4, GPIO.OUT)
    GPIO.output(4, GPIO.HIGH)
    assert GPIO.input(4) == 1
    GPIO.output(4, GPIO.LOW)
    assert GPIO.input(4) == 0


def test_cleanup_resets_state():
    GPIO.setup(17, GPIO.OUT)
    GPIO.output(17, GPIO.HIGH)
    GPIO.cleanup()
    assert GPIO.input(17) == GPIO.LOW


def test_toggle_pattern():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(17, GPIO.OUT)
    GPIO.output(17, GPIO.LOW)

    for expected in [GPIO.HIGH, GPIO.LOW, GPIO.HIGH]:
        state = GPIO.input(17)
        GPIO.output(17, not state)
        assert GPIO.input(17) == expected


def test_multiple_pins_independent():
    GPIO.setup(17, GPIO.OUT)
    GPIO.setup(27, GPIO.OUT)
    GPIO.output(17, GPIO.HIGH)
    GPIO.output(27, GPIO.LOW)
    assert GPIO.input(17) == GPIO.HIGH
    assert GPIO.input(27) == GPIO.LOW


# ── setup() with RPi.GPIO's full signature ────────────────────────────────────

def test_setup_input_with_pull_up_idles_high():
    GPIO.setup(27, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    assert GPIO.input(27) == GPIO.HIGH


def test_setup_input_with_pull_down_idles_low():
    GPIO.setup(27, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    assert GPIO.input(27) == GPIO.LOW


def test_setup_output_initial():
    GPIO.setup(17, GPIO.OUT, initial=GPIO.HIGH)
    assert GPIO.input(17) == GPIO.HIGH


def test_setup_and_output_accept_channel_lists():
    GPIO.setup([5, 6], GPIO.OUT)
    GPIO.output([5, 6], (GPIO.HIGH, GPIO.LOW))
    assert (GPIO.input(5), GPIO.input(6)) == (GPIO.HIGH, GPIO.LOW)
    GPIO.output((5, 6), GPIO.HIGH)
    assert (GPIO.input(5), GPIO.input(6)) == (GPIO.HIGH, GPIO.HIGH)


def test_output_value_count_must_match():
    GPIO.setup([5, 6], GPIO.OUT)
    with pytest.raises(RuntimeError):
        GPIO.output([5, 6], [GPIO.HIGH])


def test_output_to_input_pin_raises():
    GPIO.setup(27, GPIO.IN)
    with pytest.raises(RuntimeError):
        GPIO.output(27, GPIO.HIGH)


def test_getmode_and_gpio_function():
    assert GPIO.getmode() is None
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(17, GPIO.OUT)
    assert GPIO.getmode() == GPIO.BCM
    assert GPIO.gpio_function(17) == GPIO.OUT


# ── edge detection ────────────────────────────────────────────────────────────

def _button(edge=GPIO.BOTH, bouncetime=None):
    """Pin 27 as a pulled-up button, with every callback channel recorded."""
    calls = []
    GPIO.setup(27, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.add_event_detect(27, edge, callback=calls.append, bouncetime=bouncetime)
    return calls


def test_set_input_fires_callback_on_both_edges():
    calls = _button()
    GPIO.set_input(27, GPIO.LOW)   # press
    GPIO.set_input(27, GPIO.HIGH)  # release
    assert calls == [27, 27]
    assert GPIO.input(27) == GPIO.HIGH


def test_falling_edge_only():
    calls = _button(GPIO.FALLING)
    GPIO.set_input(27, GPIO.LOW)
    GPIO.set_input(27, GPIO.HIGH)
    assert calls == [27]


def test_same_level_is_not_an_edge():
    calls = _button()
    GPIO.set_input(27, GPIO.HIGH)
    assert calls == []


def test_bouncetime_ignores_fast_edges(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(GPIO.time, "monotonic", lambda: now[0])
    calls = _button(bouncetime=200)
    GPIO.set_input(27, GPIO.LOW)
    now[0] += 0.05                 # 50 ms later: contact bounce
    GPIO.set_input(27, GPIO.HIGH)
    now[0] += 0.3                  # 350 ms after the first edge
    GPIO.set_input(27, GPIO.LOW)
    assert calls == [27, 27]


def test_event_detected_resets_after_reading():
    _button()
    assert GPIO.event_detected(27) is False
    GPIO.set_input(27, GPIO.LOW)
    assert GPIO.event_detected(27) is True
    assert GPIO.event_detected(27) is False


def test_add_event_callback_adds_a_second_callback():
    calls = _button()
    more = []
    GPIO.add_event_callback(27, more.append)
    GPIO.set_input(27, GPIO.LOW)
    assert calls == [27] and more == [27]


def test_add_event_detect_requires_input_pin():
    GPIO.setup(17, GPIO.OUT)
    with pytest.raises(RuntimeError):
        GPIO.add_event_detect(17, GPIO.BOTH)


def test_add_event_detect_twice_conflicts():
    _button()
    with pytest.raises(RuntimeError):
        GPIO.add_event_detect(27, GPIO.RISING)


def test_remove_event_detect_stops_callbacks():
    calls = _button()
    GPIO.remove_event_detect(27)
    GPIO.set_input(27, GPIO.LOW)
    assert calls == []


def test_cleanup_one_channel_keeps_others():
    GPIO.setup(17, GPIO.OUT, initial=GPIO.HIGH)
    calls = _button()
    GPIO.cleanup(27)
    GPIO.set_input(27, GPIO.LOW)
    assert calls == []
    assert GPIO.input(17) == GPIO.HIGH
