"""Hardware profile loader.

Profiles are YAML files, found by name in three places, later ones winning:

    p4n4_emu/profiles/definitions/   the built-in profiles
    ~/.p4n4-emu/profiles/            your own
    <project>/.p4n4-emu/profiles/    a p4n4 project's, next to its .p4n4.json

so a project can carry the board it targets, or tune a built-in under the same
name. Every file is checked against the schema (PROFILE_KEYS); an error names
the file and the key at fault. `p4n4-emu profile validate` runs the check.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_DEFS_DIR = Path(__file__).parent / "definitions"
USER_DIR = Path.home() / ".p4n4-emu" / "profiles"

_SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([kmgKMG]?)$")
_MULTIPLIERS = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3}

# Architectures a profile may name (the ones overlays.generator maps to a platform)
ARCHES = ("x86_64", "amd64", "arm64", "aarch64", "armv7", "arm")
GPUS = ("nvidia",)
# key → (required, what it holds)
PROFILE_KEYS = {
    "name": (False, "the profile's name, which must match the file name"),
    "description": (True, "one line shown by `profile list`"),
    "arch": (False, f"CPU architecture: {', '.join(ARCHES)} (default x86_64)"),
    "board": (False, "GPIO board `p4n4-emu run` emulates (see hw.board)"),
    "gpu": (False, f"GPU the AI and edge services reserve: {', '.join(GPUS)}"),
    "cpus": (True, "CPU cores the device has"),
    "memory": (True, "memory for containers, e.g. 3584m or 7g"),
    "memory_swap": (False, "memory plus swap (default: memory, no swap)"),
    "blkio_weight": (False, "relative disk I/O weight, 10-1000 (default 500)"),
    "blkio_read_bps": (True, "disk read rate in bytes/s, e.g. 52428800 or 50m"),
    "blkio_write_bps": (True, "disk write rate in bytes/s"),
}


class ProfileError(ValueError):
    pass


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
    board: str | None = None  # the emulated board (hw.board), None without a GPIO header
    gpu: str | None = None  # "nvidia": the GPU services reserve one, when the host has it
    source: Path | None = None  # the file it was loaded from
    _memory_bytes: int = field(default=0, init=False, repr=False)
    _memory_swap_bytes: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        self._memory_bytes = _parse_bytes(self.memory)
        self._memory_swap_bytes = _parse_bytes(self.memory_swap)

    @property
    def memory_bytes(self) -> int:
        return self._memory_bytes

    @property
    def memory_swap_bytes(self) -> int:
        """Memory plus swap, as Docker's memswap_limit counts it."""
        return self._memory_swap_bytes

    @property
    def memory_mb(self) -> int:
        return self._memory_bytes // (1024 * 1024)

    @property
    def is_arm(self) -> bool:
        return self.arch in ("arm64", "aarch64", "armv7", "arm")

    @property
    def builtin(self) -> bool:
        return self.source is None or self.source.parent == _DEFS_DIR

    def as_dict(self) -> dict:
        """The profile for --json output, with sizes in bytes as well as as written."""
        return {
            "name": self.name,
            "description": self.description,
            "arch": self.arch,
            "is_arm": self.is_arm,
            "board": self.board,
            "gpu": self.gpu,
            "cpus": self.cpus,
            "memory": self.memory,
            "memory_bytes": self.memory_bytes,
            "memory_swap": self.memory_swap,
            "memory_swap_bytes": self.memory_swap_bytes,
            "blkio_weight": self.blkio_weight,
            "blkio_read_bps": self.blkio_read_bps,
            "blkio_write_bps": self.blkio_write_bps,
            "source": str(self.source) if self.source else None,
        }


# ── where profiles live ──────────────────────────────────────────────────────

def project_dir(start: Path | None = None) -> Path | None:
    from p4n4_emu.utils.project import find_manifest

    manifest = find_manifest(start)
    return manifest.parent / ".p4n4-emu" / "profiles" if manifest else None


def search_dirs(start: Path | None = None) -> list[Path]:
    """Profile directories, lowest precedence first."""
    dirs = [_DEFS_DIR, USER_DIR]
    project = project_dir(start)
    if project is not None:
        dirs.append(project)
    return dirs


def profile_files(start: Path | None = None) -> dict[str, Path]:
    """Profile name → the file that defines it (the one that wins)."""
    files: dict[str, Path] = {}
    for d in search_dirs(start):
        if d.is_dir():
            for path in sorted(d.glob("*.yml")) + sorted(d.glob("*.yaml")):
                files[path.stem] = path
    return files


def list_profiles(start: Path | None = None) -> list[str]:
    return sorted(profile_files(start))


def load_profile(name: str, start: Path | None = None) -> Profile:
    path = profile_files(start).get(name)
    if path is None:
        available = list_profiles(start)
        raise ProfileError(f"Profile {name!r} not found. Available: {available}")
    return load_profile_file(path)


def load_profile_file(path: Path | str) -> Profile:
    path = Path(path)
    try:
        doc = yaml.safe_load(path.read_text())
    except OSError as e:
        raise ProfileError(f"Cannot read profile {path}: {e.strerror}") from e
    except yaml.YAMLError as e:
        raise ProfileError(f"Profile {path} is not valid YAML: {e}") from e
    try:
        return parse_profile(doc, path.stem, path)
    except ProfileError as e:
        raise ProfileError(f"Profile {path}: {e}") from e


# ── the schema ───────────────────────────────────────────────────────────────

def parse_profile(doc: Any, name: str, source: Path | None = None) -> Profile:
    """Check a profile document named *name* and build it; errors name the key at fault."""
    from p4n4_emu.hw.board import BOARDS

    if not isinstance(doc, dict):
        raise ProfileError("expected a mapping of profile keys")
    unknown = [str(k) for k in doc if k not in PROFILE_KEYS]
    if unknown:
        raise ProfileError(
            f"unknown key(s) {', '.join(unknown)} (expected: {', '.join(PROFILE_KEYS)})"
        )
    missing = [k for k, (required, _) in PROFILE_KEYS.items() if required and k not in doc]
    if missing:
        raise ProfileError(f"missing key(s) {', '.join(missing)}")

    if doc.get("name", name) != name:
        raise ProfileError(f"name: {doc['name']!r} doesn't match the file name ({name!r})")
    description = doc["description"]
    if not isinstance(description, str) or not description.strip():
        raise ProfileError("description: expected a line of text")
    arch = str(doc.get("arch", "x86_64"))
    if arch not in ARCHES:
        raise ProfileError(f"arch: expected one of {', '.join(ARCHES)}")
    board = doc.get("board")
    if board is not None and board not in BOARDS:
        raise ProfileError(f"board: expected one of {', '.join(BOARDS)}")
    gpu = doc.get("gpu")
    if gpu is not None and gpu not in GPUS:
        raise ProfileError(f"gpu: expected one of {', '.join(GPUS)}")

    cpus = _number(doc["cpus"], "cpus")
    if cpus <= 0:
        raise ProfileError("cpus: must be positive")
    memory = _size(doc["memory"], "memory")
    memory_swap = _size(doc.get("memory_swap", doc["memory"]), "memory_swap")
    if _parse_bytes(memory) < 6 * 1024**2:
        raise ProfileError("memory: Docker needs at least 6m")
    if _parse_bytes(memory_swap) < _parse_bytes(memory):
        raise ProfileError("memory_swap: is memory plus swap, so can't be below memory")
    weight = doc.get("blkio_weight", 500)
    if isinstance(weight, bool) or not isinstance(weight, int) or not 10 <= weight <= 1000:
        raise ProfileError("blkio_weight: expected a whole number from 10 to 1000")
    rates = {}
    for key in ("blkio_read_bps", "blkio_write_bps"):
        rates[key] = _parse_bytes(_size(doc[key], key))
        if rates[key] <= 0:
            raise ProfileError(f"{key}: must be positive")

    return Profile(
        name=name,
        description=description.strip(),
        cpus=cpus,
        memory=memory,
        memory_swap=memory_swap,
        blkio_weight=weight,
        arch=arch,
        board=board,
        gpu=gpu,
        source=source,
        **rates,
    )


def _number(value: Any, key: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProfileError(f"{key}: expected a number")
    return float(value)


def _size(value: Any, key: str) -> str:
    """A size as written (512m, 7g, or a number of bytes), checked."""
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise ProfileError(f"{key}: expected a size such as 512m or 7g")
    try:
        _parse_bytes(value)
    except ValueError:
        raise ProfileError(f"{key}: expected a size such as 512m or 7g, got {value!r}") from None
    return str(value)
