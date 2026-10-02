"""Compose overlay renderer — generates per-stack resource-limit overlays."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from p4n4_emu.profiles.loader import Profile

_TMPL_DIR = Path(__file__).parent / "templates"

# Docker refuses memory limits below 6 MiB
_MIN_MEMORY_MB = 6
_MIN_CPUS = 0.01


@dataclass(frozen=True)
class Share:
    """Fraction of the profile's CPU and memory given to one service."""

    cpu: float
    memory: float


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
}
FALLBACK_SHARE = Share(0.10, 0.10)

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


def service_shares(stack: str, services: Iterable[str] | None = None) -> dict[str, Share]:
    """Shares for *services* of *stack* (default: the stack's known services)."""
    known = SERVICE_SHARES.get(stack, {})
    if services is None:
        return dict(known)
    return {name: known.get(name, FALLBACK_SHARE) for name in services}


@dataclass(frozen=True)
class Scale:
    cpu: float = 1.0
    memory: float = 1.0


def budget_scale(stacks: Iterable[dict[str, Share]]) -> Scale:
    """Scale factors that keep the combined shares of *stacks* within one device.

    Shares are only ever scaled down: a stack using less than the whole device
    keeps its limits.
    """
    shares = [s for stack in stacks for s in stack.values()]
    cpu_total = sum(s.cpu for s in shares)
    mem_total = sum(s.memory for s in shares)
    return Scale(
        cpu=1.0 / cpu_total if cpu_total > 1 else 1.0,
        memory=1.0 / mem_total if mem_total > 1 else 1.0,
    )


def render_overlay(
    profile: Profile,
    stack: str,
    blkio_device: str | None,
    *,
    services: Iterable[str] | None = None,
    platform: str | None = "auto",
    scale: Scale | None = None,
) -> str:
    """Render a Compose override YAML string for *stack* using *profile*.

    Args:
        profile: loaded hardware profile
        stack: one of "iot", "ai", "edge"
        blkio_device: block device path (e.g. "/dev/sda") or None to skip blkio limits
        services: service names in the stack's compose config; None uses the defaults
        platform: Docker platform to force, None for native, "auto" for the profile's
            own architecture when it is ARM
        scale: per-device budget scale from budget_scale()
    """
    if platform == "auto":
        platform = docker_platform(profile.arch) if profile.is_arm else None
    scale = scale or Scale()
    swap_ratio = profile.memory_swap_bytes / profile.memory_bytes

    rendered_services = []
    for name, share in service_shares(stack, services).items():
        memory_mb = max(_MIN_MEMORY_MB, int(profile.memory_mb * share.memory * scale.memory))
        rendered_services.append(
            {
                "name": name,
                "cpus": f"{max(_MIN_CPUS, profile.cpus * share.cpu * scale.cpu):.2f}",
                "memory_mb": memory_mb,
                "memswap_mb": max(memory_mb, int(memory_mb * swap_ratio)),
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
    )
