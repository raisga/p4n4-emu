"""Maxim DS18B20: 1-Wire temperature sensor, read through the w1-therm sysfs files.

On a Pi with the w1-gpio overlay the kernel lists each sensor under
/sys/bus/w1/devices/28-xxxxxxxxxxxx/, and reading w1_slave or temperature runs
a conversion. Under `p4n4-emu run` those paths are served by vfs, from this
model: a 12-bit reading of the device's temperature, with a valid CRC. While the
sensor drops out (a scenario's dropout fault), w1_slave shows what a sensor
that doesn't answer gives, all ones and a failed CRC, and temperature fails with EIO.
"""

from __future__ import annotations

import errno
import hashlib
import os

from p4n4_emu.hw.readings import Readings, SensorDropout, feeds

FAMILY = 0x28


def crc8(data: bytes) -> int:
    """Dallas/Maxim 1-Wire CRC (x^8 + x^5 + x^4 + 1)."""
    crc = 0
    for byte in data:
        for _ in range(8):
            mix = (crc ^ byte) & 1
            crc >>= 1
            if mix:
                crc ^= 0x8C
            byte >>= 1
    return crc


# What the bus reads when no sensor answers: the line idles high
NO_ANSWER = b"\xff" * 9


class DS18B20:
    INPUTS = ("temperature",)

    def __init__(
        self,
        readings: Readings | None = None,
        serial: str | None = None,
        measurements: dict[str, str] | None = None,
    ) -> None:
        self.readings = readings or Readings()
        self.inputs = feeds(self.INPUTS, measurements)
        # 48-bit serial number: stable for a device id, as a real part's is
        self.serial = serial or hashlib.sha1(self.readings.device_id.encode()).hexdigest()[:12]

    @property
    def id(self) -> str:
        """The sysfs name: family code, then the serial number."""
        return f"{FAMILY:02x}-{self.serial}"

    def scratchpad(self) -> bytes:
        """A conversion's 9 bytes: temperature (1/16 °C), TH, TL, config, reserved, CRC."""
        raw = round(self.readings.value(self.inputs["temperature"]) * 16)
        raw = max(-55 * 16, min(125 * 16, raw)) & 0xFFFF
        data = bytes([raw & 0xFF, raw >> 8, 0x4B, 0x46, 0x7F, 0xFF, 0x0C, 0x10])
        return data + bytes([crc8(data)])

    @staticmethod
    def millidegrees(scratchpad: bytes) -> int:
        raw = int.from_bytes(scratchpad[:2], "little", signed=True)
        return int(raw * 1000 / 16)  # truncated toward zero, as the kernel does

    def w1_slave(self) -> str:
        """The w1_slave file: the CRC check line, then the reading in millidegrees."""
        try:
            pad = self.scratchpad()
        except SensorDropout:
            pad = NO_ANSWER
        hexes = " ".join(f"{b:02x}" for b in pad)
        ok = "YES" if crc8(pad[:8]) == pad[8] else "NO"
        return f"{hexes} : crc={crc8(pad[:8]):02x} {ok}\n{hexes} t={self.millidegrees(pad)}\n"

    def temperature(self) -> str:
        try:
            return f"{self.millidegrees(self.scratchpad())}\n"
        except SensorDropout:
            raise OSError(errno.EIO, os.strerror(errno.EIO)) from None
