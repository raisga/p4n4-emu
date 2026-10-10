"""Microchip MCP3008: 8-channel 10-bit ADC on SPI.

Modelled bit by bit, so every common framing works: after a start bit the
master sends SGL/DIFF and three channel bits; after a sampling clock the part
answers with a null bit and the 10-bit result MSB first. Vref is 3.3 V; each
input is a measurement of the device (ch0 … ch7 by default).
"""

from __future__ import annotations

from p4n4_emu.hw.buses import SPIDevice
from p4n4_emu.hw.devices.ads1115 import DEFAULT_INPUTS
from p4n4_emu.hw.readings import Readings, feeds

VREF = 3.3


class MCP3008(SPIDevice):
    INPUTS = tuple(f"ch{n}" for n in range(8))

    def __init__(
        self,
        readings: Readings | None = None,
        vref: float = VREF,
        measurements: dict[str, str] | None = None,
    ) -> None:
        self.readings = readings or Readings()
        self.vref = vref
        self.inputs = feeds(self.INPUTS, measurements)

    def voltage(self, channel: int) -> float:
        return self.readings.value(
            self.inputs[self.INPUTS[channel]], DEFAULT_INPUTS[channel % len(DEFAULT_INPUTS)]
        )

    def code(self, single: bool, channel: int) -> int:
        if single:
            volts = self.voltage(channel)
        else:  # pseudo-differential: CH(2n) − CH(2n+1), or the reverse when D0 is set
            pair = channel & ~1
            plus, minus = (pair, pair + 1) if not channel & 1 else (pair + 1, pair)
            volts = self.voltage(plus) - self.voltage(minus)
        return max(0, min(1023, int(volts / self.vref * 1024)))

    def transfer(self, data: bytes) -> bytes:
        mosi = [byte >> (7 - i) & 1 for byte in data for i in range(8)]
        miso = [0] * len(mosi)
        try:
            start = mosi.index(1)
        except ValueError:
            return bytes(len(data))
        control = mosi[start + 1 : start + 5]
        if len(control) == 4:
            single, d2, d1, d0 = control
            value = self.code(bool(single), d2 << 2 | d1 << 1 | d0)
            # One clock after D0 to sample, a null bit, then B9 … B0
            out = [0, 0] + [value >> (9 - i) & 1 for i in range(10)]
            first = start + 5
            for i, bit in enumerate(out):
                if first + i < len(miso):
                    miso[first + i] = bit
        return bytes(
            sum(bit << (7 - i) for i, bit in enumerate(miso[n : n + 8]))
            for n in range(0, len(miso), 8)
        )
