"""Live container usage and resource limits, compared with what an overlay asked for.

`docker inspect` shows the limits a container was created with: Compose turns an
overlay's `deploy.resources.limits.cpus` into `HostConfig.NanoCpus`, `memory`
into `Memory`, `memswap_limit` into `MemorySwap` and `blkio_config` rates into
`BlkioDevice{Read,Write}Bps`. A container created before the overlay changed,
or started without it, keeps its old values until `up` recreates it.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

_UNITS = {
    "": 1,
    "b": 1,
    "k": 1024, "kb": 1000, "kib": 1024,
    "m": 1024**2, "mb": 1000**2, "mib": 1024**2,
    "g": 1024**3, "gb": 1000**3, "gib": 1024**3,
    "t": 1024**4, "tb": 1000**4, "tib": 1024**4,
}
# Compose's single-letter units ("358m") are binary, like Docker's
_SIZE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([a-zA-Z]*)\s*$")


def parse_size(text: str) -> int | None:
    """Bytes in "358m" (Compose) or "41.2MiB" (docker stats); None if unparsable."""
    m = _SIZE.match(str(text))
    if not m or m.group(2).lower() not in _UNITS:
        return None
    return int(float(m.group(1)) * _UNITS[m.group(2).lower()])


def format_size(n: int) -> str:
    """Binary units, as `docker stats` prints them: 512 KiB, 358 MiB, 2.4 GiB."""
    value = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            # One decimal below 100 MiB / GiB; whole numbers otherwise
            whole = unit in ("B", "KiB") or value >= 100
            return f"{value:.0f} {unit}" if whole else f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable")


@dataclass(frozen=True)
class Limits:
    """Resource limits; None where none is set."""

    cpus: float | None = None
    memory: int | None = None
    memswap: int | None = None
    read_bps: int | None = None
    write_bps: int | None = None


@dataclass(frozen=True)
class Usage:
    cpus: float  # cores in use (docker stats CPU% / 100)
    memory: int  # bytes


def _first_rate(entries) -> int | None:
    if isinstance(entries, list) and entries:
        rate = entries[0].get("Rate") or entries[0].get("rate")
        return int(rate) if rate is not None else None
    return None


def expected_limits(overlay: dict) -> dict[str, Limits]:
    """Per-service limits an overlay (as a dict) asks for."""
    expected = {}
    for name, svc in (overlay.get("services") or {}).items():
        svc = svc or {}
        limits = ((svc.get("deploy") or {}).get("resources") or {}).get("limits") or {}
        blkio = svc.get("blkio_config") or {}
        cpus = limits.get("cpus")
        expected[name] = Limits(
            cpus=float(cpus) if cpus is not None else None,
            memory=parse_size(limits["memory"]) if "memory" in limits else None,
            memswap=parse_size(svc["memswap_limit"]) if "memswap_limit" in svc else None,
            read_bps=_first_rate(blkio.get("device_read_bps")),
            write_bps=_first_rate(blkio.get("device_write_bps")),
        )
    return expected


def _limits_from_host_config(host: dict) -> Limits:
    def positive(value):
        return value if isinstance(value, int) and value > 0 else None

    nano = positive(host.get("NanoCpus"))
    return Limits(
        cpus=nano / 1e9 if nano else None,
        memory=positive(host.get("Memory")),
        memswap=positive(host.get("MemorySwap")),  # -1 means unlimited swap
        read_bps=_first_rate(host.get("BlkioDeviceReadBps")),
        write_bps=_first_rate(host.get("BlkioDeviceWriteBps")),
    )


def applied_limits(containers: list[str]) -> dict[str, Limits]:
    """Limits each container was created with, by container name."""
    if not containers:
        return {}
    r = subprocess.run(
        ["docker", "inspect", *containers], capture_output=True, text=True, check=False
    )
    try:
        docs = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    return {
        d.get("Name", "").lstrip("/"): _limits_from_host_config(d.get("HostConfig") or {})
        for d in docs
    }


def live_usage(containers: list[str]) -> dict[str, Usage]:
    """CPU and memory each running container uses now, by container name.

    `docker stats --no-stream` samples for about a second, so this is one call
    for every container rather than one per container.
    """
    if not containers:
        return {}
    r = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{json .}}", *containers],
        capture_output=True,
        text=True,
        check=False,
    )
    usage = {}
    for line in r.stdout.splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            cpus = float(str(row.get("CPUPerc", "")).rstrip("%")) / 100
        except ValueError:
            continue  # "--" for a container that stopped meanwhile
        memory = parse_size(str(row.get("MemUsage", "")).split("/")[0])
        if memory is not None:
            usage[row.get("Name", "")] = Usage(cpus=cpus, memory=memory)
    return usage


def differences(expected: Limits, applied: Limits) -> list[str]:
    """What differs between the limits asked for and those applied, e.g. 'cpus 1.00 ≠ 0.40'."""
    out = []
    if expected.cpus is not None and (
        applied.cpus is None or abs(applied.cpus - expected.cpus) > 0.005
    ):
        have = f"{applied.cpus:.2f}" if applied.cpus is not None else "none"
        out.append(f"cpus {have} ≠ {expected.cpus:.2f}")
    for field, label, unit in (
        ("memory", "memory", ""),
        ("memswap", "swap", ""),
        ("read_bps", "disk read", "/s"),
        ("write_bps", "disk write", "/s"),
    ):
        want, have = getattr(expected, field), getattr(applied, field)
        if want is not None and want != have:
            have_text = format_size(have) + unit if have is not None else "none"
            out.append(f"{label} {have_text} ≠ {format_size(want)}{unit}")
    return out


def cgroup_v2() -> bool:
    """Whether this host uses cgroup v2, without which limits are set but not enforced."""
    return Path("/sys/fs/cgroup/cgroup.controllers").exists()
