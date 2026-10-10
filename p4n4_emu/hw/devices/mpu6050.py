"""InvenSense MPU-6050: 3-axis accelerometer and gyroscope on I2C (0x68, or 0x69).

It powers up asleep (PWR_MGMT_1 = 0x40) with its data registers at zero, as
the real part does, so a driver has to wake it first. Acceleration follows the
simulator's raw measurement; the board is at rest, so the gyro reads noise.
"""

from __future__ import annotations

import random

from p4n4_emu.hw.buses import RegisterDevice
from p4n4_emu.hw.readings import Readings, feeds

WHO_AM_I = 0x68

REG_SMPLRT_DIV = 0x19
REG_CONFIG = 0x1A
REG_GYRO_CONFIG = 0x1B
REG_ACCEL_CONFIG = 0x1C
REG_DATA = 0x3B  # ACCEL_XOUT_H … GYRO_ZOUT_L: 0x3B–0x48
REG_PWR_MGMT_1 = 0x6B
REG_PWR_MGMT_2 = 0x6C
REG_WHO_AM_I = 0x75

# LSB per g / per °/s for each full-scale setting (FS_SEL / AFS_SEL 0–3)
ACCEL_LSB = (16384, 8192, 4096, 2048)
GYRO_LSB = (131.0, 65.5, 32.8, 16.4)
GYRO_NOISE_DPS = 0.05


def _int16(value: float) -> int:
    return max(-32768, min(32767, round(value))) & 0xFFFF


class MPU6050(RegisterDevice):
    INPUTS = ("temperature",)  # the die temperature; acceleration is the raw measurement's

    def __init__(
        self, readings: Readings | None = None, measurements: dict[str, str] | None = None
    ) -> None:
        super().__init__()
        self.readings = readings or Readings()
        self.inputs = feeds(self.INPUTS, measurements)
        self._reset()

    def _reset(self) -> None:
        self.regs = {REG_PWR_MGMT_1: 0x40}
        self.data = [0] * 14

    @property
    def asleep(self) -> bool:
        return bool(self.regs[REG_PWR_MGMT_1] & 0x40)

    def sample(self) -> None:
        """Latch every data register at once, as a burst read expects."""
        if self.asleep:
            return
        accel_lsb = ACCEL_LSB[self.regs.get(REG_ACCEL_CONFIG, 0) >> 3 & 0b11]
        gyro_lsb = GYRO_LSB[self.regs.get(REG_GYRO_CONFIG, 0) >> 3 & 0b11]
        x, y, z = self.readings.acceleration()
        temperature = self.readings.value(self.inputs["temperature"])
        words = [
            x * accel_lsb, y * accel_lsb, z * accel_lsb,
            (temperature - 36.53) * 340,  # datasheet: °C = raw / 340 + 36.53
            *(random.gauss(0, GYRO_NOISE_DPS) * gyro_lsb for _ in range(3)),
        ]
        self.data = [b for w in map(_int16, words) for b in (w >> 8, w & 0xFF)]

    def begin_read(self) -> None:
        if REG_DATA <= self.pointer < REG_DATA + 14:
            self.sample()

    def read_register(self, register: int) -> int:
        if register == REG_WHO_AM_I:
            return WHO_AM_I
        if REG_DATA <= register < REG_DATA + 14:
            return self.data[register - REG_DATA]
        return self.regs.get(register, 0)

    def write_register(self, register: int, value: int) -> None:
        if register == REG_PWR_MGMT_1 and value & 0x80:  # DEVICE_RESET
            self._reset()
            return
        self.regs[register] = value
