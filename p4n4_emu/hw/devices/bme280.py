"""Bosch BME280: temperature, humidity and pressure on I2C (0x76, or 0x77).

The part reports raw ADC counts that a driver turns into °C / %RH / Pa with the
calibration constants it reads from the chip. The model holds a fixed set of
calibration constants and finds the ADC counts that Bosch's compensation
formulas (datasheet section 4.2.3, integer versions) turn back into the
simulated values, so any driver that follows the datasheet reads them.
"""

from __future__ import annotations

from p4n4_emu.hw.buses import RegisterDevice
from p4n4_emu.hw.readings import Readings

CHIP_ID = 0x60
RESET_WORD = 0xB6

# Calibration constants (the datasheet's example values; humidity from a real part)
T = (27504, 26435, -1000)
P = (36477, -10685, 3024, 2855, 140, -7, 15500, -14600, 6000)
H = (75, 362, 0, 313, 50, 30)

REG_CALIB_00 = 0x88
REG_CALIB_26 = 0xE1
REG_ID = 0xD0
REG_RESET = 0xE0
REG_CTRL_HUM = 0xF2
REG_STATUS = 0xF3
REG_CTRL_MEAS = 0xF4
REG_CONFIG = 0xF5
REG_DATA = 0xF7  # press_msb … hum_lsb: 0xF7–0xFE

# What a data register holds while its measurement is skipped (oversampling 0)
SKIPPED_20 = 0x80000
SKIPPED_16 = 0x8000


def _le16(value: int) -> list[int]:
    return [value & 0xFF, value >> 8 & 0xFF]


def _calibration() -> dict[int, int]:
    """The calibration registers: 0x88–0xA1 and 0xE1–0xE7."""
    block = [*_le16(T[0]), *_le16(T[1]), *_le16(T[2])]
    for p in P:
        block += _le16(p)
    block += [0, H[0]]  # 0xA0 is unused, 0xA1 is dig_H1
    regs = {REG_CALIB_00 + i: b for i, b in enumerate(block)}
    h2, h3, h4, h5, h6 = H[1:]
    regs.update({
        0xE1: h2 & 0xFF, 0xE2: h2 >> 8 & 0xFF, 0xE3: h3,
        0xE4: h4 >> 4 & 0xFF, 0xE5: (h4 & 0xF) | (h5 & 0xF) << 4, 0xE6: h5 >> 4 & 0xFF,
        0xE7: h6 & 0xFF,
    })
    return regs


CALIBRATION = _calibration()


# ── Bosch's integer compensation (datasheet 4.2.3) ────────────────────────────

def t_fine(adc_t: int) -> int:
    t1, t2, t3 = T
    var1 = (((adc_t >> 3) - (t1 << 1)) * t2) >> 11
    var2 = (((((adc_t >> 4) - t1) * ((adc_t >> 4) - t1)) >> 12) * t3) >> 14
    return var1 + var2


def compensate_t(adc_t: int) -> int:
    """Temperature in 0.01 °C."""
    return (t_fine(adc_t) * 5 + 128) >> 8


def compensate_p(adc_p: int, fine: int) -> int:
    """Pressure in Pa as Q24.8 (Pa × 256)."""
    p1, p2, p3, p4, p5, p6, p7, p8, p9 = P
    var1 = fine - 128000
    var2 = var1 * var1 * p6
    var2 = var2 + ((var1 * p5) << 17)
    var2 = var2 + (p4 << 35)
    var1 = ((var1 * var1 * p3) >> 8) + ((var1 * p2) << 12)
    var1 = (((1 << 47) + var1) * p1) >> 33
    if var1 == 0:
        return 0
    p = 1048576 - adc_p
    p = _cdiv(((p << 31) - var2) * 3125, var1)
    var1 = (p9 * (p >> 13) * (p >> 13)) >> 25
    var2 = (p8 * p) >> 19
    return ((p + var1 + var2) >> 8) + (p7 << 4)


def compensate_h(adc_h: int, fine: int) -> int:
    """Relative humidity in %RH as Q22.10 (%RH × 1024)."""
    h1, h2, h3, h4, h5, h6 = H
    v = fine - 76800
    v = (
        ((((adc_h << 14) - (h4 << 20) - (h5 * v)) + 16384) >> 15)
        * (((((((v * h6) >> 10) * (((v * h3) >> 11) + 32768)) >> 10) + 2097152) * h2 + 8192) >> 14)
    )
    v = v - (((((v >> 15) * (v >> 15)) >> 7) * h1) >> 4)
    v = min(max(v, 0), 419430400)
    return v >> 12


def _cdiv(a: int, b: int) -> int:
    """Integer division truncating toward zero, as C does."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def _search(target: float, f, lo: int, hi: int, increasing: bool) -> int:
    """The ADC count in [lo, hi] whose compensated value is closest to *target*."""
    while lo < hi:
        mid = (lo + hi) // 2
        below = f(mid) < target
        if below == increasing:
            lo = mid + 1
        else:
            hi = mid
    return lo


def adc_for(temperature_c: float, humidity_pct: float, pressure_hpa: float) -> tuple[int, int, int]:
    """Raw (adc_T, adc_P, adc_H) that compensate to these values."""
    adc_t = _search(temperature_c * 100, compensate_t, 0, (1 << 20) - 1, increasing=True)
    fine = t_fine(adc_t)
    adc_p = _search(
        pressure_hpa * 100 * 256, lambda a: compensate_p(a, fine), 0, (1 << 20) - 1,
        increasing=False,
    )
    adc_h = _search(
        humidity_pct * 1024, lambda a: compensate_h(a, fine), 0, (1 << 16) - 1, increasing=True
    )
    return adc_t, adc_p, adc_h


class BME280(RegisterDevice):
    """Sleep, forced and normal mode; a forced measurement goes back to sleep."""

    def __init__(self, readings: Readings | None = None) -> None:
        super().__init__()
        self.readings = readings or Readings()
        self._reset()

    def _reset(self) -> None:
        self.ctrl_hum = 0
        self.ctrl_meas = 0
        self.config = 0
        self.osrs_h = 0  # ctrl_hum only takes effect at the next ctrl_meas write
        self.data = [0x80, 0, 0, 0x80, 0, 0, 0x80, 0]

    @property
    def mode(self) -> int:
        return self.ctrl_meas & 0b11

    def measure(self) -> None:
        r = self.readings
        adc_t, adc_p, adc_h = adc_for(
            r.value("temperature"), r.value("humidity"), r.value("pressure")
        )
        if not self.ctrl_meas >> 5:
            adc_t = SKIPPED_20
        if not self.ctrl_meas >> 2 & 0b111:
            adc_p = SKIPPED_20
        if not self.osrs_h:
            adc_h = SKIPPED_16
        self.data = [
            adc_p >> 12, adc_p >> 4 & 0xFF, (adc_p & 0xF) << 4,
            adc_t >> 12, adc_t >> 4 & 0xFF, (adc_t & 0xF) << 4,
            adc_h >> 8, adc_h & 0xFF,
        ]

    def begin_read(self) -> None:
        # Normal mode measures continuously: a burst read sees the latest sample
        if self.mode == 0b11 and REG_DATA <= self.pointer <= REG_DATA + 7:
            self.measure()

    def read_register(self, register: int) -> int:
        if register in CALIBRATION:
            return CALIBRATION[register]
        if REG_DATA <= register <= REG_DATA + 7:
            return self.data[register - REG_DATA]
        return {
            REG_ID: CHIP_ID,
            REG_CTRL_HUM: self.ctrl_hum,
            REG_STATUS: 0,  # measurements complete at once: never "measuring"
            REG_CTRL_MEAS: self.ctrl_meas,
            REG_CONFIG: self.config,
        }.get(register, 0)

    def write_register(self, register: int, value: int) -> None:
        if register == REG_RESET and value == RESET_WORD:
            self._reset()
        elif register == REG_CTRL_HUM:
            self.ctrl_hum = value & 0b111
        elif register == REG_CONFIG:
            self.config = value & 0b11111101
        elif register == REG_CTRL_MEAS:
            self.ctrl_meas = value
            self.osrs_h = self.ctrl_hum
            if self.mode in (0b01, 0b10):  # forced: one measurement, then sleep
                self.measure()
                self.ctrl_meas &= ~0b11
            elif self.mode == 0b11:
                self.measure()
