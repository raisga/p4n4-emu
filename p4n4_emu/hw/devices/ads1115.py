"""TI ADS1115: 4-channel 16-bit ADC on I2C (0x48–0x4B).

Registers are 16 bits, sent MSB first, behind a pointer register: config
(MUX, PGA, MODE, OS) selects the input and the full-scale range. A single-shot
conversion completes as soon as OS is written; continuous mode converts on
every read. Each input's voltage is a measurement of the device (a0 … a3 by
default), so a scenario can give a channel its own wave.
"""

from __future__ import annotations

from p4n4_emu.hw.buses import I2CDevice
from p4n4_emu.hw.readings import Readings
from p4n4_emu.sim.scenario import Wave

REG_CONVERSION = 0
REG_CONFIG = 1
REG_LO_THRESH = 2
REG_HI_THRESH = 3

CONFIG_RESET = 0x8583
OS = 1 << 15
MODE_SINGLE = 1 << 8

# PGA setting → full-scale range in volts
FSR = (6.144, 4.096, 2.048, 1.024, 0.512, 0.256, 0.256, 0.256)
# MUX setting → (positive input, negative input or None for GND)
MUX = ((0, 1), (0, 3), (1, 3), (2, 3), (0, None), (1, None), (2, None), (3, None))

# What the inputs see when the device's scenario doesn't name them: 0–3.3 V signals
DEFAULT_INPUTS = (
    Wave(1.65, 1.2, 0.002, period=120, min=0.0, max=3.3, unit="V"),
    Wave(0.9, 0.3, 0.002, period=300, min=0.0, max=3.3, unit="V"),
    Wave(2.5, 0.4, 0.002, period=600, min=0.0, max=3.3, unit="V"),
    Wave(0.3, 0.1, 0.001, period=60, min=0.0, max=3.3, unit="V"),
)


class ADS1115(I2CDevice):
    def __init__(
        self, readings: Readings | None = None, channels: tuple[str, ...] = ("a0", "a1", "a2", "a3")
    ) -> None:
        self.readings = readings or Readings()
        self.channels = channels
        self.pointer = REG_CONVERSION
        self.regs = {
            REG_CONVERSION: 0, REG_CONFIG: CONFIG_RESET, REG_LO_THRESH: 0x8000,
            REG_HI_THRESH: 0x7FFF,
        }

    def voltage(self, channel: int) -> float:
        return self.readings.value(self.channels[channel], DEFAULT_INPUTS[channel])

    def convert(self) -> None:
        config = self.regs[REG_CONFIG]
        positive, negative = MUX[config >> 12 & 0b111]
        volts = self.voltage(positive) - (self.voltage(negative) if negative is not None else 0.0)
        counts = round(volts / FSR[config >> 9 & 0b111] * 32768)
        self.regs[REG_CONVERSION] = max(-32768, min(32767, counts)) & 0xFFFF

    def write(self, data: bytes) -> None:
        if not data:
            return
        self.pointer = data[0] & 0b11
        if len(data) < 3:
            return
        value = data[1] << 8 | data[2]
        if self.pointer == REG_CONFIG:
            self.regs[REG_CONFIG] = value & ~OS
            if value & OS and value & MODE_SINGLE:
                self.convert()
        elif self.pointer != REG_CONVERSION:
            self.regs[self.pointer] = value

    def read(self, count: int) -> bytes:
        config = self.regs[REG_CONFIG]
        continuous = not config & MODE_SINGLE
        if self.pointer == REG_CONVERSION and continuous:
            self.convert()
        value = self.regs[self.pointer]
        if self.pointer == REG_CONFIG and not continuous:
            value |= OS  # the single-shot conversion is already done
        word = bytes([value >> 8, value & 0xFF])
        return (word * (count // 2 + 1))[:count]
