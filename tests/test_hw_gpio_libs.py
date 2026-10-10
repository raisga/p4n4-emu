"""Tests for the lgpio and gpiod stubs, and for gpiozero running on lgpio."""

import errno
import os
import select
import struct
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


# ── lgpio: watchdog, notification pipes, serial ──────────────────────────────

def test_lgpio_watchdog_sends_one_timeout_after_an_edge(chip):
    seen = []
    lgpio.gpio_claim_alert(chip, 23, lgpio.BOTH_EDGES)
    lgpio.gpio_set_watchdog_micros(chip, 23, 30_000)
    lgpio.callback(chip, 23, lgpio.BOTH_EDGES, lambda c, g, level, t: seen.append(level))
    time.sleep(0.08)
    assert seen == []  # no watchdog before the first edge, as in lgpio
    pins.drive(23, 1)
    time.sleep(0.12)
    assert seen == [1, lgpio.TIMEOUT]  # one timeout per stream of edges
    pins.drive(23, 0)
    time.sleep(0.01)
    lgpio.gpio_set_watchdog_micros(chip, 23, 0)
    time.sleep(0.06)
    assert seen == [1, lgpio.TIMEOUT, 0]


def test_lgpio_watchdog_range(chip):
    lgpio.gpio_claim_input(chip, 23)
    with pytest.raises(lgpio.error, match="bad watchdog microseconds"):
        lgpio.gpio_set_watchdog_micros(chip, 23, -1)


def test_lgpio_notification_pipe(chip, tmp_path, monkeypatch):
    monkeypatch.setenv("LG_WD", str(tmp_path))
    seen = []
    lgpio.callback(chip, 24, lgpio.BOTH_EDGES, lambda *a: seen.append(a))
    nfy = lgpio.notify_open()
    fifo = tmp_path / f".lgd-nfy{nfy}"
    assert fifo.exists()
    lgpio.gpio_claim_alert(chip, 24, lgpio.RISING_EDGE, notify_handle=nfy)
    fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    try:
        pins.drive(24, 1)
        tick, chip_no, gpio, level, flags, _ = struct.unpack("QBBBBI", os.read(fd, 16))
        assert (chip_no, gpio, level, flags) == (0, 24, 1, 0)
        assert tick > 0
        assert seen == []  # alerts for a notification handle skip the callbacks
        lgpio.notify_pause(nfy)
        pins.drive(24, 0)
        pins.drive(24, 1)
        with pytest.raises(BlockingIOError):
            os.read(fd, 16)
        lgpio.notify_resume(nfy)
        pins.drive(24, 0)
        pins.drive(24, 1)
        assert len(os.read(fd, 64)) == 16  # rising edges only
    finally:
        os.close(fd)
    lgpio.notify_close(nfy)
    assert not fifo.exists()
    with pytest.raises(lgpio.error, match="unknown handle"):
        lgpio.notify_close(nfy)
    with pytest.raises(lgpio.error, match="unknown handle"):
        lgpio.gpio_claim_alert(chip, 25, lgpio.RISING_EDGE, notify_handle=nfy)


def test_lgpio_serial():
    from p4n4_emu.hw import buses

    port = buses.uart("/dev/serial0")
    port.feed(b"stale")
    h = lgpio.serial_open("/dev/serial0", 9600)
    assert lgpio.serial_data_available(h) == 0  # flushed on open
    assert lgpio.serial_read(h) == (0, bytearray())
    with pytest.raises(lgpio.error, match="ser read no data available"):
        lgpio.serial_read_byte(h)
    port.feed(b"$GPGGA")
    assert lgpio.serial_data_available(h) == 6
    assert lgpio.serial_read_byte(h) == ord("$")
    assert lgpio.serial_read(h, 3) == (3, bytearray(b"GPG"))
    lgpio.serial_write(h, "AT\r\n")
    lgpio.serial_write_byte(h, 0x55)
    assert bytes(port.tx) == b"AT\r\n\x55"
    lgpio.serial_close(h)
    with pytest.raises(lgpio.error, match="unknown handle"):
        lgpio.serial_read(h)


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (("/dev/serial0", 12345), "bad serial baud rate"),
        (("/dev/serial0", 9600, 1), "bad serial open flags"),
        (("/dev/ttyUSB7", 9600), "can not open serial device"),
    ],
)
def test_lgpio_serial_open_errors(args, error):
    with pytest.raises(lgpio.error, match=error):
        lgpio.serial_open(*args)


def test_lgpio_serial_loopback_part():
    from p4n4_emu.hw import buses

    buses.attach_uart("/dev/ttyUSB0", buses.Loopback())
    h = lgpio.serial_open("/dev/ttyUSB0", 115200)
    lgpio.serial_write(h, b"echo")
    assert lgpio.serial_read(h, 100) == (4, bytearray(b"echo"))


# ── PWM toggles the level ────────────────────────────────────────────────────

def test_pwm_level_follows_the_duty_cycle(chip):
    lgpio.gpio_claim_output(chip, 18)
    lgpio.tx_pwm(chip, 18, 1000, 25)
    samples = []
    end = time.monotonic() + 0.2
    while time.monotonic() < end:
        samples.append(lgpio.gpio_read(chip, 18))
    assert 0.1 < sum(samples) / len(samples) < 0.4
    lgpio.tx_pwm(chip, 18, 0, 0)
    assert pins.read(18) == 0 and pins.get(18).pwm is None


def test_pwm_at_0_or_100_percent_holds_the_level(chip):
    lgpio.gpio_claim_output(chip, 18)
    lgpio.tx_pwm(chip, 18, 1000, 100)
    assert all(pins.read(18) == 1 for _ in range(200))
    lgpio.tx_pwm(chip, 18, 1000, 0)
    assert all(pins.read(18) == 0 for _ in range(200))


def test_slow_pwm_toggles_as_changes_watchers_see():
    changes = []

    def watcher(change):
        if change.gpio == 12 and change.edge:
            changes.append((change.pin.level, change.timestamp_ns))

    pins.watch(watcher)
    try:
        pins.setup_output(12, 0)
        pins.set_pwm(12, 10, 50)  # 50 ms high, 50 ms low
        time.sleep(0.33)
        pins.set_pwm(12, 0, 0)
    finally:
        pins.unwatch(watcher)
    levels = [level for level, _ in changes]
    assert 5 <= len(levels) <= 9
    assert all(a != b for a, b in zip(levels, levels[1:], strict=False))
    gaps = [(b - a) / 1e6 for (_, a), (_, b) in zip(changes, changes[1:], strict=False)][:-1]
    assert all(30 < gap < 90 for gap in gaps), gaps


def test_fast_pwm_doesnt_flood_watchers():
    changes = []
    pins.watch(changes.append)
    try:
        pins.setup_output(13, 0)
        pins.set_pwm(13, 2000, 50)
        time.sleep(0.1)
    finally:
        pins.unwatch(changes.append)
    assert len(changes) <= 2  # the output set up, PWM started


def test_rpi_gpio_pwm_toggles():
    gpio_stub.setmode(gpio_stub.BCM)
    gpio_stub.setup(18, gpio_stub.OUT)
    pwm = gpio_stub.PWM(18, 500)
    pwm.start(80)
    samples = [gpio_stub.input(18) for _ in range(20000)]
    assert 0.6 < sum(samples) / len(samples) < 0.95
    pwm.stop()
    assert gpio_stub.input(18) == 0


# ── gpiod v1 ──────────────────────────────────────────────────────────────────

def test_gpiod_v1_chip_lookup():
    from p4n4_emu.hw import gpiod_v1_stub as g1

    for descr in ("gpiochip0", "/dev/gpiochip0", "0", "pinctrl-rp1"):
        with g1.Chip(descr) as chip:
            assert chip.name() == "gpiochip0"
            assert chip.label() == "pinctrl-rp1"
            assert chip.num_lines() == 54
    assert g1.Chip("4").name() == "gpiochip4"
    with pytest.raises(FileNotFoundError):
        g1.Chip("gpiochip9")
    with pytest.raises(FileNotFoundError):
        g1.Chip("0", g1.Chip.OPEN_BY_NAME)
    chip = g1.Chip("gpiochip0")
    chip.close()
    with pytest.raises(ValueError, match="closed"):
        chip.get_line(17)
    assert [c.name() for c in g1.ChipIter()] == ["gpiochip0", "gpiochip4"]


def test_gpiod_v1_output_and_input():
    from p4n4_emu.hw import gpiod_v1_stub as g1

    chip = g1.Chip("gpiochip0")
    led = chip.get_line(17)
    assert not led.is_requested() and led.consumer() is None
    led.request(consumer="led", type=g1.LINE_REQ_DIR_OUT, default_val=1)
    assert pins.read(17) == 1 and led.get_value() == 1
    assert led.direction() == g1.Line.DIRECTION_OUTPUT and led.consumer() == "led"
    led.set_value(0)
    assert pins.read(17) == 0
    with pytest.raises(OSError) as e:
        g1.Chip("gpiochip0").get_line(17).request(consumer="other", type=g1.LINE_REQ_DIR_IN)
    assert e.value.errno == errno.EBUSY

    button = chip.find_line("GPIO27")
    button.request(consumer="btn", type=g1.LINE_REQ_DIR_IN,
                   flags=g1.LINE_REQ_FLAG_BIAS_PULL_UP | g1.LINE_REQ_FLAG_ACTIVE_LOW)
    assert button.get_value() == 0  # pulled up, active low
    assert button.bias() == g1.Line.BIAS_PULL_UP
    assert button.active_state() == g1.Line.ACTIVE_LOW
    pins.drive(27, 0)
    assert button.get_value() == 1
    with pytest.raises(OSError) as e:
        button.set_value(1)
    assert e.value.errno == errno.EPERM
    led.release()
    assert not led.is_requested() and pins.get(17).function is None
    with pytest.raises(OSError):
        led.get_value()
    assert g1.find_line("GPIO5").offset() == 5
    assert g1.find_line("nope") is None


def test_gpiod_v1_bulk():
    from p4n4_emu.hw import gpiod_v1_stub as g1

    chip = g1.Chip("gpiochip0")
    bulk = chip.get_lines([5, 6, 13])
    bulk.request(consumer="bar", type=g1.LINE_REQ_DIR_OUT, default_vals=[1, 0, 1])
    assert [pins.read(g) for g in (5, 6, 13)] == [1, 0, 1]
    bulk.set_values([0, 1, 1])
    assert bulk.get_values() == [0, 1, 1]
    bulk.set_direction_input()
    assert all(pins.get(g).function == pins.INPUT for g in (5, 6, 13))
    bulk.set_direction_output([1, 1, 0])
    assert [pins.read(g) for g in (5, 6, 13)] == [1, 1, 0]
    assert len(bulk) == 3 and [ln.offset() for ln in bulk] == [5, 6, 13]
    bulk.release()


def test_gpiod_v1_edge_events():
    from p4n4_emu.hw import gpiod_v1_stub as g1

    line = g1.Chip("gpiochip0").get_line(22)
    line.request(consumer="ev", type=g1.LINE_REQ_EV_BOTH_EDGES,
                 flags=g1.LINE_REQ_FLAG_BIAS_PULL_DOWN)
    assert not line.event_wait(sec=0, nsec=1_000_000)  # the bias isn't an event
    pins.drive(22, 1)
    pins.drive(22, 0)
    ready, _, _ = select.select([line.event_get_fd()], [], [], 1)
    assert ready
    assert line.event_wait(sec=1)
    first = line.event_read()
    assert first.type == g1.LineEvent.RISING_EDGE and first.source.offset() == 22
    assert first.source is line
    (second,) = line.event_read_multiple()
    assert second.type == g1.LineEvent.FALLING_EDGE
    assert (second.sec, second.nsec) >= (first.sec, first.nsec)
    with pytest.raises(OSError) as e:
        line.set_value(1)
    assert e.value.errno == errno.EPERM


def test_gpiod_v1_event_read_blocks_until_an_edge():
    from p4n4_emu.hw import gpiod_v1_stub as g1

    line = g1.Chip("gpiochip0").get_line(26)
    line.request(consumer="ev", type=g1.LINE_REQ_EV_RISING_EDGE)
    threading.Timer(0.05, pins.drive, (26, 1)).start()
    assert line.event_read().type == g1.LineEvent.RISING_EDGE


def test_gpiod_v1_events_on_a_plain_request_are_refused():
    from p4n4_emu.hw import gpiod_v1_stub as g1

    line = g1.Chip("gpiochip0").get_line(26)
    line.request(consumer="in", type=g1.LINE_REQ_DIR_IN)
    with pytest.raises(OSError) as e:
        line.event_wait(sec=0)
    assert e.value.errno == errno.EPERM


def test_shims_choose_the_gpiod_api():
    import sys

    shims.install(board="rpi5", parts=False, files=False, gpiod_api="v1")
    import gpiod

    assert gpiod.version_string() == "1.6.3"
    assert "gpiod.line" not in sys.modules
    shims.reset()
    shims.install(board="rpi5", parts=False, files=False)
    import gpiod

    assert gpiod.__version__.startswith("2.")
    with pytest.raises(ValueError, match="Unknown gpiod API"):
        shims.modules("v3")
