"""Docker Compose subprocess wrappers — multi-file variant for overlay support."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _base(
    args: list[str],
    cwd: Path,
    *,
    overlay: Path | None = None,
    stream: bool = True,
) -> int:
    cmd = ["docker", "compose", "-f", "docker-compose.yml"]
    if overlay is not None:
        cmd += ["-f", str(overlay)]
    cmd += args
    result = subprocess.run(cmd, cwd=cwd, check=False)
    return result.returncode


def up(cwd: Path, overlay: Path | None = None, build: bool = False, pull: bool = False) -> int:
    args = ["up", "-d"]
    if build:
        args.append("--build")
    if pull:
        args.append("--pull=always")
    return _base(args, cwd, overlay=overlay)


def down(cwd: Path, overlay: Path | None = None, volumes: bool = False) -> int:
    args = ["down"]
    if volumes:
        args.append("-v")
    return _base(args, cwd, overlay=overlay)


def ps(cwd: Path, overlay: Path | None = None) -> list[dict]:
    cmd = ["docker", "compose", "-f", "docker-compose.yml"]
    if overlay is not None:
        cmd += ["-f", str(overlay)]
    cmd += ["ps", "--format", "json"]
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    services = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, list):
                services.extend(obj)
            else:
                services.append(obj)
        except json.JSONDecodeError:
            continue
    return services


def logs(
    cwd: Path,
    overlay: Path | None = None,
    service: str | None = None,
    tail: int | None = None,
    follow: bool = True,
) -> int:
    args = ["logs"]
    if follow:
        args.append("-f")
    if tail is not None:
        args.extend(["--tail", str(tail)])
    if service:
        args.append(service)
    return _base(args, cwd, overlay=overlay)
