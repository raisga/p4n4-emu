"""Hardware profile loader."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_DEFS_DIR = Path(__file__).parent / "definitions"

_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmgKMG]?)$")
_MULTIPLIERS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}


def _parse_bytes(value: str | int | float) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    m = _SIZE_RE.match(str(value).strip())
    if not m:
        raise ValueError(f"Cannot parse size: {value!r}")
    num, suffix = float(m.group(1)), m.group(2).lower()
    return int(num * _MULTIPLIERS[suffix])


@dataclass
class Profile:
    name: str
    description: str
    cpus: float
    memory: str
    memory_swap: str
    blkio_weight: int
    blkio_read_bps: int
    blkio_write_bps: int
    arch: str = "x86_64"
    _memory_bytes: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._memory_bytes = _parse_bytes(self.memory)

    @property
    def memory_bytes(self) -> int:
        return self._memory_bytes

    @property
    def memory_mb(self) -> int:
        return self._memory_bytes // (1024 * 1024)

    @property
    def is_arm(self) -> bool:
        return self.arch in ("arm64", "aarch64", "armv7")


def load_profile(name: str) -> Profile:
    path = _DEFS_DIR / f"{name}.yml"
    if not path.exists():
        available = list_profiles()
        raise ValueError(f"Profile {name!r} not found. Available: {available}")
    data = yaml.safe_load(path.read_text())
    return Profile(
        name=data["name"],
        description=data["description"],
        cpus=float(data["cpus"]),
        memory=str(data["memory"]),
        memory_swap=str(data["memory_swap"]),
        blkio_weight=int(data["blkio_weight"]),
        blkio_read_bps=int(data["blkio_read_bps"]),
        blkio_write_bps=int(data["blkio_write_bps"]),
        arch=str(data.get("arch", "x86_64")),
    )


def list_profiles() -> list[str]:
    return sorted(p.stem for p in _DEFS_DIR.glob("*.yml"))
