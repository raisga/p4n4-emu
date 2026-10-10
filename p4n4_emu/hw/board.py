"""The emulated boards: what a script can find out about the Pi it runs on.

A profile names its board (`board:` in the profile YAML). The board gives the
revision code that /proc/device-tree reports (RPi.GPIO's RPI_INFO, gpiozero's
pi_info()), the gpiochip devices and their line count, and the 40-pin header
map that BOARD numbering uses.
"""

from __future__ import annotations

from dataclasses import dataclass

# Physical header pin → BCM GPIO, for the 40-pin header (Model B+ onwards)
BOARD_TO_BCM = {
    3: 2, 5: 3, 7: 4, 8: 14, 10: 15, 11: 17, 12: 18, 13: 27, 15: 22, 16: 23,
    18: 24, 19: 10, 21: 9, 22: 25, 23: 11, 24: 8, 26: 7, 27: 0, 28: 1, 29: 5,
    31: 6, 32: 12, 33: 13, 35: 19, 36: 16, 37: 26, 38: 20, 40: 21,
}
BCM_TO_BOARD = {gpio: pin for pin, gpio in BOARD_TO_BCM.items()}


@dataclass(frozen=True)
class Board:
    name: str
    model: str  # /proc/device-tree/model
    revision: int  # new-style revision code (/proc/device-tree/system/linux,revision)
    chip_label: str  # the header's gpiochip label
    lines: int  # lines on that gpiochip
    chips: tuple[int, ...]  # /dev/gpiochipN numbers that reach the header
    compatible: tuple[str, ...]  # /proc/device-tree/compatible

    @property
    def type(self) -> str:
        types = {0x08: "Pi 3 Model B", 0x11: "Pi 4 Model B", 0x12: "Zero 2 W",
                 0x17: "Pi 5 Model B"}
        return types.get(self.revision >> 4 & 0xFF, "Unknown")

    @property
    def processor(self) -> str:
        return {2: "BCM2837", 3: "BCM2711", 4: "BCM2712"}.get(self.revision >> 12 & 0xF, "Unknown")

    @property
    def memory_mb(self) -> int:
        return 256 << (self.revision >> 20 & 0x7)

    def rpi_info(self) -> dict:
        """RPi.GPIO's RPI_INFO for this board."""
        return {
            "P1_REVISION": 3,
            "REVISION": f"{self.revision:x}",
            "TYPE": self.type,
            "MANUFACTURER": "Sony UK",
            "PROCESSOR": self.processor,
            "RAM": f"{self.memory_mb // 1024}G" if self.memory_mb >= 1024 else f"{self.memory_mb}M",
        }


BOARDS = {
    # Pi 3 Model B 1 GB, rev 1.2
    "rpi3": Board("rpi3", "Raspberry Pi 3 Model B Rev 1.2", 0xA02082, "pinctrl-bcm2835", 54, (0,),
                  ("raspberrypi,3-model-b", "brcm,bcm2837")),
    # Pi 4 Model B 4 GB, rev 1.4: the rpi4 profile's memory
    "rpi4": Board("rpi4", "Raspberry Pi 4 Model B Rev 1.4", 0xC03114, "pinctrl-bcm2711", 58, (0,),
                  ("raspberrypi,4-model-b", "brcm,bcm2711")),
    # Pi 5 Model B 8 GB, rev 1.0. Since kernel 6.6.45 the header is gpiochip0;
    # gpiochip4 remains as a link to it, and older code (gpiozero 2.0) opens that
    "rpi5": Board("rpi5", "Raspberry Pi 5 Model B Rev 1.0", 0xD04170, "pinctrl-rp1", 54, (0, 4),
                  ("raspberrypi,5-model-b", "brcm,bcm2712")),
    # Zero 2 W 512 MB, rev 1.0: its RP3A0 reports itself as a BCM2837
    "rpi-zero2w": Board("rpi-zero2w", "Raspberry Pi Zero 2 W Rev 1.0", 0x902120,
                        "pinctrl-bcm2835", 54, (0,),
                        ("raspberrypi,model-zero-2-w", "brcm,bcm2837")),
}
DEFAULT_BOARD = "rpi5"

_current: Board = BOARDS[DEFAULT_BOARD]


def current() -> Board:
    return _current


def use(name: str) -> Board:
    """Select the emulated board (`p4n4-emu run` does this from the profile)."""
    global _current
    if name not in BOARDS:
        raise ValueError(f"Unknown board {name!r}. Available: {sorted(BOARDS)}")
    _current = BOARDS[name]
    return _current
