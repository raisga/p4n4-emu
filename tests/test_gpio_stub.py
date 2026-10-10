"""Tests for the RPi.GPIO drop-in stub."""

import threading

import pytest

import p4n4_emu.hw.gpio_stub as GPIO
from p4n4_emu.hw import board, pins, shims


def setup_function():
    shims.reset()
    GPIO.setmode(GPIO.BCM)


def teardown_function():
    shims.reset()


def test_constants():
    assert GPIO.BCM == 11
    assert GPIO.BOARD == 10
    assert GPIO.OUT == 0
    assert GPIO.IN == 1
    assert GPIO.HIGH == 1
    assert GPIO.LOW == 0


def test_setmode_again_with_the_same_mode():
    GPIO.setmode(GPIO.BCM)
    assert GPIO.getmode() == GPIO.BCM


def test_setmode_to_another_mode_raises_until_cleanup():
    with pytest.raises(ValueError, match="different mode"):
        GPIO.setmode(GPIO.BOARD)
    GPIO.cleanup()
    GPIO.setmode(GPIO.BOARD)
    assert GPIO.getmode() == GPIO.BOARD


def test_pins_need_a_numbering_mode():
    GPIO.cleanup()
    with pytest.raises(RuntimeError, match="setmode"):
        GPIO.setup(17, GPIO.OUT)


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
    assert GPIO.getmode() is None
    assert pins.get(17) == pins.Pin()
    GPIO.setmode(GPIO.BCM)
    with pytest.raises(RuntimeError, match="setup"):
        GPIO.input(17)


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
    GPIO.setup(17, GPIO.OUT)
    assert GPIO.getmode() == GPIO.BCM
    assert GPIO.gpio_function(17) == GPIO.OUT
    assert GPIO.gpio_function(4) == GPIO.IN


def test_setup_argument_checks():
    with pytest.raises(ValueError, match="pull_up_down"):
        GPIO.setup(17, GPIO.OUT, pull_up_down=GPIO.PUD_UP)
    with pytest.raises(ValueError, match="initial"):
        GPIO.setup(27, GPIO.IN, initial=GPIO.HIGH)
    with pytest.raises(ValueError, match="invalid on a Raspberry Pi"):
        GPIO.setup(99, GPIO.OUT)


def test_input_and_output_need_setup():
    with pytest.raises(RuntimeError, match="setup"):
        GPIO.input(17)
    with pytest.raises(RuntimeError, match="OUTPUT"):
        GPIO.output(17, GPIO.HIGH)


# ── BOARD numbering ───────────────────────────────────────────────────────────

def test_board_numbering_maps_header_pins_to_bcm():
    GPIO.cleanup()
    GPIO.setmode(GPIO.BOARD)
    GPIO.setup(11, GPIO.OUT, initial=GPIO.HIGH)  # header pin 11 is GPIO 17
    assert pins.read(17) == 1


def test_board_numbering_rejects_power_and_ground_pins():
    GPIO.cleanup()
    GPIO.setmode(GPIO.BOARD)
    with pytest.raises(ValueError, match="invalid on a Raspberry Pi"):
        GPIO.setup(1, GPIO.OUT)  # 3V3


def test_callbacks_get_the_channel_in_board_numbering():
    GPIO.cleanup()
    GPIO.setmode(GPIO.BOARD)
    calls = []
    GPIO.setup(13, GPIO.IN, pull_up_down=GPIO.PUD_UP)  # GPIO 27
    GPIO.add_event_detect(13, GPIO.FALLING, callback=calls.append)
    GPIO.set_input(13, GPIO.LOW)
    assert calls == [13]


# ── RPI_INFO ──────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    ("name", "kind", "revision", "processor", "ram"),
    [("rpi4", "Pi 4 Model B", "c03114", "BCM2711", "4G"),
     ("rpi5", "Pi 5 Model B", "d04170", "BCM2712", "8G")],
)
def test_rpi_info_follows_the_board(name, kind, revision, processor, ram):
    board.use(name)
    info = GPIO.RPI_INFO
    assert (info["TYPE"], info["REVISION"], info["PROCESSOR"], info["RAM"]) == (
        kind, revision, processor, ram,
    )
    assert GPIO.RPI_REVISION == 3


# ── PWM ───────────────────────────────────────────────────────────────────────

def test_pwm_records_frequency_and_duty_cycle():
    GPIO.setup(18, GPIO.OUT)
    pwm = GPIO.PWM(18, 100)
    assert pins.get(18).pwm is None  # created, not started
    pwm.start(25)
    assert pins.get(18).pwm == (100.0, 25.0)
    pwm.ChangeDutyCycle(75)
    pwm.ChangeFrequency(50)
    assert pins.get(18).pwm == (50.0, 75.0)
    pwm.stop()
    assert pins.get(18).pwm is None
    assert pins.read(18) == GPIO.LOW


def test_pwm_checks():
    with pytest.raises(RuntimeError, match="output"):
        GPIO.PWM(18, 100)
    GPIO.setup(18, GPIO.OUT)
    with pytest.raises(ValueError, match="frequency"):
        GPIO.PWM(18, 0)
    pwm = GPIO.PWM(18, 100)
    with pytest.raises(RuntimeError, match="already exists"):
        GPIO.PWM(18, 100)
    with pytest.raises(ValueError, match="dutycycle"):
        pwm.start(101)


def test_cleanup_stops_pwm():
    GPIO.setup(18, GPIO.OUT)
    pwm = GPIO.PWM(18, 100)
    pwm.start(50)
    GPIO.cleanup(18)
    assert pins.get(18).pwm is None
    assert pwm  # still referenced: cleanup stopped it, not garbage collection


# ── wait_for_edge ─────────────────────────────────────────────────────────────

def test_wait_for_edge_times_out():
    GPIO.setup(27, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    assert GPIO.wait_for_edge(27, GPIO.FALLING, timeout=20) is None


def test_wait_for_edge_returns_the_channel():
    GPIO.setup(27, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    press = threading.Timer(0.05, GPIO.set_input, (27, GPIO.LOW))
    press.start()
    assert GPIO.wait_for_edge(27, GPIO.FALLING, timeout=2000) == 27
    press.join()
    # The temporary edge detection is gone again
    GPIO.add_event_detect(27, GPIO.RISING)


def test_wait_for_edge_conflicts_with_a_callback():
    calls = _button()
    with pytest.raises(RuntimeError, match="Conflicting"):
        GPIO.wait_for_edge(27, GPIO.FALLING, timeout=10)
    assert calls == []


def test_a_failing_callback_does_not_stop_the_next(capsys):
    calls = _button()

    def broken(channel):
        raise ValueError("boom")

    GPIO.add_event_callback(27, broken)
    GPIO.add_event_callback(27, calls.append)
    GPIO.set_input(27, GPIO.LOW)
    assert calls == [27, 27]
    assert "boom" in capsys.readouterr().err


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
