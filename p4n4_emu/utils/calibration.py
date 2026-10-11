"""CPU calibration: how fast one host core is compared with one core of the board.

A CFS quota caps CPU *time*, not speed: `cpus: 4` on a desktop gives four cores
several times faster than a Pi's. Each profile carries the single-thread 7-Zip
rating of one of its cores (`cpu_score`: the `Tot:` MIPS of `7z b -mmt1`, the
"7-zip single" column of sbc-bench's results); `p4n4-emu calibrate` measures the
same benchmark on this host, once per Docker platform the stacks run (native,
or arm64 under QEMU), and stores it in CALIBRATION_FILE. `up` then scales every
CPU limit by board score / host score.

7-Zip, not `sysbench cpu`: sysbench's prime loop is integer division, which
arm64 does far faster for its class, so it rates a Pi 4 core above a desktop
Intel one. 7-Zip's mix of compression and decompression compares across
architectures. The benchmark runs in BENCH_IMAGE with Debian's p7zip-full, the
package sbc-bench's board numbers come from.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from p4n4_emu.profiles.loader import Profile

CALIBRATION_FILE = Path.home() / ".p4n4-emu" / "calibration.json"
# Debian bookworm packages p7zip-full for amd64, arm64 and armhf
BENCH_IMAGE = "debian:bookworm-slim"
# `Tot:  <usage %>  <R/U MIPS>  <rating MIPS>`: the rating is the score
_TOTAL = re.compile(r"^Tot:\s+\d+\s+\d+\s+(\d+)\s*$", re.MULTILINE)
_VERSION = re.compile(r"^7-Zip\b.*?(\d+\.\d+)", re.MULTILINE)


class CalibrationError(RuntimeError):
    pass


def bench_command(platform: str | None) -> list[str]:
    script = (
        "apt-get update -qq >/dev/null && "
        "apt-get install -y -qq --no-install-recommends p7zip-full >/dev/null 2>&1 && "
        "7z b -mmt1"
    )
    cmd = ["docker", "run", "--rm"]
    if platform:
        cmd += ["--platform", platform]
    return [*cmd, BENCH_IMAGE, "sh", "-c", script]


@dataclass(frozen=True)
class Measurement:
    score: float  # 7-Zip MIPS of one core
    version: str  # 7-Zip version that measured it


def parse_7zip(output: str) -> Measurement:
    m = _TOTAL.search(output)
    if not m:
        raise CalibrationError("7-Zip printed no 'Tot:' rating")
    v = _VERSION.search(output)
    return Measurement(float(m.group(1)), v.group(1) if v else "unknown")


def measure(platform: str | None) -> Measurement:
    """Run the single-thread 7-Zip benchmark in a container for *platform* (None: native)."""
    try:
        r = subprocess.run(bench_command(platform), capture_output=True, text=True, check=False)
    except FileNotFoundError as e:
        raise CalibrationError("Docker is not installed") from e
    if r.returncode != 0:
        detail = (r.stderr or r.stdout).strip().splitlines()
        raise CalibrationError(
            f"the benchmark container failed: {detail[-1] if detail else f'exit {r.returncode}'}"
        )
    return parse_7zip(r.stdout)


def load() -> dict[str, dict]:
    """Stored host scores by Docker platform; empty if never calibrated or unreadable."""
    try:
        doc = json.loads(CALIBRATION_FILE.read_text())
    except (OSError, ValueError):
        return {}
    scores = doc.get("scores") if isinstance(doc, dict) else None
    if not isinstance(scores, dict):
        return {}
    return {
        k: v for k, v in scores.items()
        if isinstance(v, dict) and isinstance(v.get("score"), int | float) and v["score"] > 0
    }


def save(platform: str, score: float, version: str, source: str) -> dict:
    """Record *platform*'s host score (*source*: "measured" or "manual")."""
    scores = load()
    entry = {
        "score": round(score, 2),
        "7zip": version,
        "source": source,
        "date": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    scores[platform] = entry
    CALIBRATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    CALIBRATION_FILE.write_text(json.dumps({"scores": scores}, indent=2) + "\n")
    return entry


def host_score(platform: str) -> float | None:
    entry = load().get(platform)
    return float(entry["score"]) if entry else None


@dataclass(frozen=True)
class CpuFactor:
    """What CPU limits are multiplied by, and why (shown by `up`)."""

    value: float = 1.0
    note: str = ""


def cpu_factor(profile: Profile, platform: str) -> CpuFactor:
    """Board core speed / host core speed for containers running *platform*.

    Never above 1: a quota can slow a fast core down, not speed a slow one up
    (the usual case under QEMU, where emulated cores are slower than a Pi's).
    """
    if profile.cpu_score is None:
        return CpuFactor(note=f"{profile.name} has no cpu_score")
    host = host_score(platform)
    if host is None:
        return CpuFactor(
            note=f"host not calibrated for {platform} (run p4n4-emu calibrate"
            + (f" --platform {platform})" if platform != _native() else ")")
        )
    ratio = profile.cpu_score / host
    if ratio > 1:
        return CpuFactor(
            note=f"a host core ({host:g}) is slower than a {profile.name} core "
            f"({profile.cpu_score:g}) for {platform}"
        )
    return CpuFactor(
        ratio, f"{profile.name} core {profile.cpu_score:g} / host core {host:g} ({platform})"
    )


def _native() -> str:
    from p4n4_emu.utils.preflight import host_platform

    return host_platform()
