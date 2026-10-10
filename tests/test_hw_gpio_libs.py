"""Tests for the lgpio and gpiod stubs, and for gpiozero running on lgpio."""

import select
import threading
import time
from datetime import timedelta

import pytest

from p4n4_emu.hw import board, gpio_stub, pins, shims
from p4n4_emu.hw import gpiod_stub as gpiod
from p4n4_emu.hw import lgpio_stub as lgpio
from p4n4_emu.hw.gpiod_stub.line import Bias, Direction, Edge, Value


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("GPIOZERO_PIN_FACTORY", raising=False)
    shims.reset()
    yield
    shims.reset()


# ── lgpio ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def chip():
    h = lgpio.gpiochip_open(0)
    yield h
    lgpio.gpiochip_close(h)


def test_gpiochip_open_follows_the_board():
    assert lgpio.gpiochip_open(4) >> 16 == 4  # Pi 5: gpiochip4 links to the header
    board.use("rpi4")
    with pytest.raises(lgpio.error, match="can not open gpiochip"):
        lgpio.gpiochip_open(4)
    h = lgpio.gpiochip_open(0)
    assert lgpio.gpio_get_chip_info(h) == [0, 58, "gpiochip0", "pinctrl-bcm2711"]


def test_lgpio_output_and_input(chip):
    lgpio.gpio_claim_output(chip, 17, 1)
    assert pins.read(17) == 1 and lgpio.gpio_read(chip, 17) == 1
    lgpio.gpio_write(chip, 17, 0)
    assert pins.read(17) == 0
    lgpio.gpio_claim_input(chip, 27, lgpio.SET_PULL_UP)
    assert lgpio.gpio_read(chip, 27) == 1
    pins.drive(27, 0)
    assert lgpio.gpio_read(chip, 27) == 0


def test_lgpio_errors(chip):
    with pytest.raises(lgpio.error, match="GPIO not allocated"):
        lgpio.gpio_read(chip, 5)
    lgpio.gpio_claim_input(chip, 5)
    with pytest.raises(lgpio.error, match="not set as an output"):
        lgpio.gpio_write(chip, 5, 1)
    with pytest.raises(lgpio.error, match="bad GPIO number"):
        lgpio.gpio_claim_input(chip, 99)
    other = lgpio.gpiochip_open(0)
    with pytest.raises(lgpio.error, match="GPIO busy"):
        lgpio.gpio_claim_output(other, 5)


def test_lgpio_errors_as_status_codes(chip, monkeypatch):
    monkeypatch.setattr(lgpio, "exceptions", False)
    assert lgpio.gpio_read(chip, 5) == lgpio.GPIO_NOT_ALLOCATED


def test_lgpio_mode_bits(chip):
    lgpio.gpio_claim_output(chip, 17)
    lgpio.gpio_claim_alert(chip, 27, lgpio.FALLING_EDGE, lgpio.SET_PULL_UP)
    out, alert = lgpio.gpio_get_mode(chip, 17), lgpio.gpio_get_mode(chip, 27)
    assert out & 0x2 and out & 0x200 and out & 0x1
    assert alert & lgpio.SET_PULL_UP and alert & 0x400 and alert & 1 << 18
    assert not alert & 0x2


def test_lgpio_active_low(chip):
    lgpio.gpio_claim_output(chip, 17, 1, lgpio.SET_ACTIVE_LOW)
    assert pins.read(17) == 0
    assert lgpio.gpio_read(chip, 17) == 1


def test_lgpio_alerts_call_back(chip):
    events = []
    lgpio.gpio_claim_alert(chip, 27, lgpio.BOTH_EDGES, lgpio.SET_PULL_UP)
    cb = lgpio.callback(chip, 27, lgpio.BOTH_EDGES, lambda *a: events.append(a))
    pins.drive(27, 0)
    pins.drive(27, 1)
    assert [(c, g, level) for c, g, level, _ in events] == [(0, 27, 0), (0, 27, 1)]
    cb.cancel()
    pins.drive(27, 0)
    assert len(events) == 2


def test_lgpio_alert_edge_filter_and_tally(chip):
    lgpio.gpio_claim_alert(chip, 27, lgpio.RISING_EDGE)
    cb = lgpio.callback(chip, 27)
    for level in (1, 0, 1, 0):
        pins.drive(27, level)
    assert cb.tally() == 2


def test_lgpio_debounce_reports_the_settled_level(chip):
    events = []
    lgpio.gpio_claim_alert(chip, 27, lgpio.BOTH_EDGES, lgpio.SET_PULL_UP)
    lgpio.gpio_set_debounce_micros(chip, 27, 30_000)
    lgpio.callback(chip, 27, lgpio.BOTH_EDGES, lambda c, g, level, t: events.append(level))
    for level in (0, 1, 0):  # contact bounce, faster than 30 ms
        pins.drive(27, level)
    assert events == []
    time.sleep(0.1)
    assert events == [0]


def test_lgpio_pwm_and_servo(chip):
    lgpio.gpio_claim_output(chip, 18)
    lgpio.tx_pwm(chip, 18, 1000, 40)
    assert pins.get(18).pwm == (1000.0, 40.0)
    assert lgpio.tx_busy(chip, 18, lgpio.TX_PWM) == 1
    lgpio.tx_pwm(chip, 18, 0, 0)
    assert pins.get(18).pwm is None
    lgpio.tx_servo(chip, 18, 1500)
    assert pins.get(18).pwm == (50.0, 7.5)
    with pytest.raises(lgpio.error, match="bad PWM dutycycle"):
        lgpio.tx_pwm(chip, 18, 100, 101)


def test_lgpio_groups(chip):
    lgpio.group_claim_output(chip, [5, 6, 13], [1, 0, 1])
    assert lgpio.group_read(chip, 5) == 0b101
    lgpio.group_write(chip, 5, 0b010)
    assert [pins.read(g) for g in (5, 6, 13)] == [0, 1, 0]
    lgpio.group_write(chip, 5, 0b111, 0b001)
    assert [pins.read(g) for g in (5, 6, 13)] == [1, 1, 0]
    lgpio.group_free(chip, 5)
    assert pins.get(6).function is None


def test_gpiochip_close_frees_its_gpio(chip):
    h = lgpio.gpiochip_open(0)
    lgpio.gpio_claim_output(h, 22, 1)
    lgpio.gpiochip_close(h)
    assert pins.get(22).function is None


# ── the libraries share one set of pins ───────────────────────────────────────

def test_rpi_gpio_output_is_seen_through_gpiod():
    gpio_stub.setmode(gpio_stub.BCM)
    gpio_stub.setup(17, gpio_stub.OUT, initial=gpio_stub.HIGH)
    with gpiod.Chip("/dev/gpiochip0") as c:
        info = c.get_line_info(17)
    assert info.used and info.direction == Direction.OUTPUT and info.name == "GPIO17"


# ── gpiod ─────────────────────────────────────────────────────────────────────

def test_gpiod_output_request():
    with gpiod.request_lines(
        "/dev/gpiochip0", consumer="blink",
        config={17: gpiod.LineSettings(direction=Direction.OUTPUT, output_value=Value.ACTIVE)},
    ) as req:
        assert pins.read(17) == 1
        req.set_value(17, Value.INACTIVE)
        assert pins.read(17) == 0
        assert req.get_value(17) == Value.INACTIVE
        assert req.offsets == [17] and req.chip_name == "gpiochip0"
    assert pins.get(17).function is None
    with pytest.raises(gpiod.RequestReleasedError):
        req.get_value(17)


def test_gpiod_lines_by_name_and_tuple_keys():
    settings = gpiod.LineSettings(direction=Direction.INPUT, bias=Bias.PULL_UP)
    with gpiod.request_lines("/dev/gpiochip0", config={("GPIO5", "GPIO6"): settings}) as req:
        assert req.offsets == [5, 6]
        assert req.get_values() == [Value.ACTIVE, Value.ACTIVE]
        assert req.get_value("GPIO6") == Value.ACTIVE


def test_gpiod_busy_and_missing_chip():
    out = gpiod.LineSettings(direction=Direction.OUTPUT)
    with gpiod.request_lines("/dev/gpiochip0", config={17: out}):
        with pytest.raises(OSError, match="busy"):
            gpiod.request_lines("/dev/gpiochip0", config={17: out})
    with pytest.raises(FileNotFoundError):
        gpiod.Chip("/dev/gpiochip9")
    assert gpiod.is_gpiochip_device("/dev/gpiochip4") is True
    assert gpiod.is_gpiochip_device("/dev/gpiochip1") is False


def test_gpiod_set_value_on_an_input_is_refused():
    with gpiod.request_lines("/dev/gpiochip0", config={27: None}) as req:
        with pytest.raises(PermissionError):
            req.set_value(27, Value.ACTIVE)


def test_gpiod_active_low():
    settings = gpiod.LineSettings(direction=Direction.OUTPUT, active_low=True,
                                  output_value=Value.ACTIVE)
    with gpiod.request_lines("/dev/gpiochip0", config={17: settings}) as req:
        assert pins.read(17) == 0
        assert req.get_value(17) == Value.ACTIVE


def test_gpiod_edge_events():
    settings = gpiod.LineSettings(edge_detection=Edge.BOTH, bias=Bias.PULL_UP,
                                  direction=Direction.INPUT)
    with gpiod.request_lines("/dev/gpiochip0", config={27: settings}) as req:
        assert req.wait_edge_events(0) is False
        pins.drive(27, 0)
        pins.drive(27, 1)
        assert req.wait_edge_events(timedelta(seconds=1)) is True
        assert select.select([req.fd], [], [], 0)[0] == [req.fd]
        events = req.read_edge_events()
        assert [e.event_type for e in events] == [
            gpiod.EdgeEvent.Type.FALLING_EDGE, gpiod.EdgeEvent.Type.RISING_EDGE,
        ]
        assert [e.line_seqno for e in events] == [1, 2]
        assert events[0].timestamp_ns < events[1].timestamp_ns
        assert req.wait_edge_events(0) is False


def test_gpiod_read_edge_events_blocks_until_one_arrives():
    settings = gpiod.LineSettings(edge_detection=Edge.FALLING, direction=Direction.INPUT,
                                  bias=Bias.PULL_UP)
    with gpiod.request_lines("/dev/gpiochip0", config={27: settings}) as req:
        threading.Timer(0.05, pins.drive, (27, 0)).start()
        (event,) = req.read_edge_events()
        assert event.line_offset == 27


def test_gpiod_reconfigure_lines():
    with gpiod.request_lines("/dev/gpiochip0", config={17: None}) as req:
        req.reconfigure_lines({17: gpiod.LineSettings(direction=Direction.OUTPUT,
                                                      output_value=Value.ACTIVE)})
        assert pins.get(17).function == pins.OUTPUT and pins.read(17) == 1


# ── gpiozero, unmodified, on the lgpio stub ───────────────────────────────────

@pytest.fixture
def gpiozero(monkeypatch):
    gz = pytest.importorskip("gpiozero")
    shims.install(board="rpi5", parts=False)
    yield gz
    if gz.Device.pin_factory is not None:
        gz.Device.pin_factory.close()
        gz.Device.pin_factory = None


def test_gpiozero_picks_the_lgpio_stub_for_the_emulated_pi5(gpiozero):
    led = gpiozero.LED(17)
    factory = gpiozero.Device.pin_factory
    assert type(factory).__name__ == "LGPIOFactory"
    assert factory.board_info.model == "5B" and factory.chip == 4
    led.on()
    assert pins.read(17) == 1
    led.close()


def test_gpiozero_button_events(gpiozero):
    events = []
    button = gpiozero.Button(27)
    button.when_pressed = lambda: events.append("pressed")
    button.when_released = lambda: events.append("released")
    pins.drive(27, 0)
    assert button.is_pressed
    pins.drive(27, 1)
    assert events == ["pressed", "released"]
    button.close()


def test_gpiozero_pwm_led(gpiozero):
    led = gpiozero.PWMLED(18)
    led.value = 0.5
    assert pins.get(18).pwm == (100.0, 50.0)
    led.close()
