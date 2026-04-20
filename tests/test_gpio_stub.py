"""Tests for the RPi.GPIO drop-in stub."""

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
