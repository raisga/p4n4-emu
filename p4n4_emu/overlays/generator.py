"""Compose overlay renderer — generates per-stack resource-limit overlays."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from p4n4_emu.profiles.loader import Profile
from p4n4_emu.profiles.shares import FALLBACK_KEY, Overrides

_TMPL_DIR = Path(__file__).parent / "templates"

# Docker refuses memory limits below 6 MiB
_MIN_MEMORY_MB = 6
_MIN_CPUS = 0.01
# A service's share of the device's pids, at least this many (a shell and a few threads)
_MIN_PIDS = 64


@dataclass(frozen=True)
class Share:
    """Fraction of the profile's CPU and memory given to one service. A fixed share is the
    service's own size on this board: budget_scale() never shrinks it, the others shrink instead."""

    cpu: float
    memory: float
    fixed: bool = False


@dataclass(frozen=True)
class Fixed:
    """A service's size whatever the board, capped at the board."""

    cpus: float
    memory_mb: int

    def share(self, profile: Profile) -> Share:
        return Share(
            min(1.0, self.cpus / profile.cpus), min(1.0, self.memory_mb / profile.memory_mb), True
        )


# Default shares for the services the p4n4 stacks ship. Services a project
# adds get FALLBACK_SHARE; services it removed are simply not rendered.
SERVICE_SHARES: dict[str, dict[str, Share]] = {
    "iot": {
        "mqtt": Share(0.10, 0.05),
        "influxdb": Share(0.40, 0.35),
        "node-red": Share(0.30, 0.15),
        "grafana": Share(0.15, 0.10),
    },
    "ai": {
        "ollama": Share(0.60, 0.55),
        "letta": Share(0.20, 0.20),
        "n8n": Share(0.20, 0.15),
    },
    "edge": {
        "ei-runner": Share(0.25, 0.10),
    },
    # nginx serving static files and proxying: a few tens of MB on a Pi
    "dashboard": {
        "dashboard": Share(0.05, 0.01),
    },
}
FALLBACK_SHARE = Share(0.10, 0.10)

# Services that need the same size on every board. System 1 (laya, opt-in, so not a default
# above) needs 2 GiB and up to 2 CPUs; the other services' shares shrink to make room
# (p4n4-docs, decisions/system1.md §8).
FIXED_LIMITS: dict[str, dict[str, Fixed]] = {
    "ai": {"system1": Fixed(cpus=2.0, memory_mb=2048)},
}
# Services that keep the host's platform when the board is emulated: PyTorch segfaults under
# QEMU's arm64 emulation (system1.md §4.7), so System 1 runs native
NATIVE_SERVICES = frozenset({"system1"})
# A fixed service taking more than this share of the board's memory is not recommended there
FIXED_WARN_MEMORY = 0.5

# Services that use a GPU when the profile has one: LLM inference and the edge runner
GPU_SERVICES = frozenset({"ollama", "ei-runner"})

_DOCKER_PLATFORMS = {
    "arm64": "linux/arm64",
    "aarch64": "linux/arm64",
    "armv7": "linux/arm/v7",
    "arm": "linux/arm/v7",
    "x86_64": "linux/amd64",
    "amd64": "linux/amd64",
}


def docker_platform(arch: str) -> str:
    """Map an architecture name (arm64, armv7, x86_64, …) to a Docker platform."""
    try:
        return _DOCKER_PLATFORMS[arch.lower()]
    except KeyError:
        raise ValueError(
            f"Unknown architecture {arch!r}. Choose from: {', '.join(_DOCKER_PLATFORMS)}."
        ) from None


def _apply(share: Share, fields: dict[str, float] | None) -> Share:
    if not fields:
        return share
    return Share(fields.get("cpu", share.cpu), fields.get("memory", share.memory), share.fixed)


def service_shares(
    stack: str,
    services: Iterable[str] | None = None,
    overrides: Overrides | None = None,
    profile: Profile | None = None,
) -> dict[str, Share]:
    """Shares for *services* of *stack* (default: the stack's known services).

    *overrides* (profiles.shares) change a service's share field by field; its
    "*" entry changes the fallback that services with no default share get.
    With *profile*, services in FIXED_LIMITS get their fixed size on that board.
    """
    overrides = overrides or {}
    known = SERVICE_SHARES.get(stack, {})
    fixed = FIXED_LIMITS.get(stack, {}) if profile is not None else {}
    fallback = _apply(FALLBACK_SHARE, overrides.get(FALLBACK_KEY))
    names = known if services is None else services
    return {
        name: _apply(
            fixed[name].share(profile) if name in fixed else known.get(name, fallback),
            overrides.get(name),
        )
        for name in names
    }


def fixed_warnings(profile: Profile, stacks: Iterable[dict[str, Share]]) -> list[str]:
    """Why a fixed service doesn't suit *profile*: it needs most of the board, or more."""
    out = []
    for shares in stacks:
        for name, share in shares.items():
            if not share.fixed:
                continue
            for stack in FIXED_LIMITS.values():
                need = stack.get(name)
                if need is None:
                    continue
                if need.memory_mb > profile.memory_mb:
                    out.append(
                        f"{name} needs {need.memory_mb} MiB and {profile.name} has "
                        f"{profile.memory_mb}: it can't run on this board."
                    )
                elif share.memory > FIXED_WARN_MEMORY:
                    out.append(
                        f"{name} takes {share.memory:.0%} of {profile.name}'s memory, leaving "
                        "little for the other services: not recommended on this board."
                    )
    return out


_CPUSET = re.compile(r"^\d+(-\d+)?(,\d+(-\d+)?)*$")


def resolve_cpuset(spec: str, profile: Profile, host_cpus: int | None) -> str:
    """The cores to pin every service to: *spec* "auto" means the first ones, as
    many as the profile has; otherwise a list like "0-3" or "0,2,4,6".

    Raises ValueError when the list is malformed or names cores the host lacks.
    """
    if spec == "auto":
        wanted = math.ceil(profile.cpus)
        if host_cpus is not None and wanted > host_cpus:
            raise ValueError(
                f"{profile.name} has {profile.cpus:g} cores and the host {host_cpus}: "
                "it can't be pinned to its own cores."
            )
        return "0" if wanted == 1 else f"0-{wanted - 1}"
    spec = spec.replace(" ", "")
    if not _CPUSET.match(spec):
        raise ValueError(f"--cpuset: expected auto, or cores such as 0-3 or 0,2,4, got {spec!r}")
    cores = set()
    for part in spec.split(","):
        low, _, high = part.partition("-")
        first, last = int(low), int(high or low)
        if last < first:
            raise ValueError(f"--cpuset: {part} runs backwards")
        cores.update(range(first, last + 1))
    if host_cpus is not None and max(cores) >= host_cpus:
        raise ValueError(f"--cpuset: the host has cores 0-{host_cpus - 1}, not {max(cores)}")
    return spec


@dataclass(frozen=True)
class Scale:
    cpu: float = 1.0
    memory: float = 1.0


def budget_scale(stacks: Iterable[dict[str, Share]]) -> Scale:
    """Scale factors that keep the combined shares of *stacks* within one device.

    Shares are only ever scaled down: a stack using less than the whole device
    keeps its limits. Fixed shares are never scaled: the others share what they
    leave of the device.
    """
    shares = [s for stack in stacks for s in stack.values()]
    cpu_left = max(0.0, 1.0 - sum(s.cpu for s in shares if s.fixed))
    mem_left = max(0.0, 1.0 - sum(s.memory for s in shares if s.fixed))
    cpu_total = sum(s.cpu for s in shares if not s.fixed)
    mem_total = sum(s.memory for s in shares if not s.fixed)
    return Scale(
        cpu=cpu_left / cpu_total if cpu_total > cpu_left else 1.0,
        memory=mem_left / mem_total if mem_total > mem_left else 1.0,
    )


def render_overlay(
    profile: Profile,
    stack: str,
    blkio_device: str | None,
    *,
    services: Iterable[str] | None = None,
    platform: str | None = "auto",
    scale: Scale | None = None,
    gpu: str | None = None,
    overrides: Overrides | None = None,
    cpu_factor: float = 1.0,
    cpuset: str | None = None,
    cpuset_auto: bool = False,
) -> str:
    """Render a Compose override YAML string for *stack* using *profile*.

    Args:
        profile: loaded hardware profile
        stack: one of "iot", "ai", "edge", "dashboard"
        blkio_device: block device path (e.g. "/dev/sda") or None to skip blkio limits
        services: service names in the stack's compose config; None uses the defaults
        platform: Docker platform to force, None for native, "auto" for the profile's
            own architecture when it is ARM
        scale: per-device budget scale from budget_scale()
        gpu: GPU driver (e.g. "nvidia") whose device GPU_SERVICES reserve, or None
        overrides: share overrides from the profile and the project (profiles.shares)
        cpu_factor: board core speed / host core speed (utils.calibration), at most 1
        cpuset: host cores every service is pinned to (resolve_cpuset), or None
        cpuset_auto: *cpuset* came from "auto", so `profile switch` recomputes it
    """
    if platform == "auto":
        platform = docker_platform(profile.arch) if profile.is_arm else None
    scale = scale or Scale()
    swap_ratio = profile.memory_swap_bytes / profile.memory_bytes

    rendered_services = []
    for name, share in service_shares(stack, services, overrides, profile).items():
        own = Scale() if share.fixed else scale
        memory_mb = max(_MIN_MEMORY_MB, int(profile.memory_mb * share.memory * own.memory))
        cpus = profile.cpus * cpu_factor * share.cpu * own.cpu
        rendered_services.append(
            {
                "name": name,
                "platform": None if name in NATIVE_SERVICES else platform,
                "cpus": f"{max(_MIN_CPUS, cpus):.2f}",
                "memory_mb": memory_mb,
                "memswap_mb": max(memory_mb, int(memory_mb * swap_ratio)),
                # Threads cost kernel memory, so pids follow the memory share
                "pids": max(_MIN_PIDS, int(profile.device_pids * share.memory * own.memory)),
                "gpu": gpu if name in GPU_SERVICES else None,
            }
        )

    env = Environment(
        loader=FileSystemLoader(str(_TMPL_DIR)),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    tmpl = env.get_template("stack.emu.yml.j2")
    return tmpl.render(
        profile=profile,
        stack=stack,
        services=rendered_services,
        platform=platform,
        blkio_device=blkio_device,
        gpu=gpu,
        cpu_factor=cpu_factor,
        cpuset=cpuset,
        cpuset_auto=cpuset_auto,
    )
