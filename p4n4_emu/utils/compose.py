"""Docker Compose subprocess wrappers — multi-file variant for overlay support."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

_COMPOSE_NETWORK_LABEL = "com.docker.compose.network"


def ensure_network(name: str) -> None:
    """Ensure a Docker network exists with the correct Compose label.

    Docker Compose warns when a network is found without the expected
    com.docker.compose.network label (e.g. created via `docker network create`
    or `docker run --network`).  Re-creating it with the correct label
    suppresses the warning.
    """
    inspect = subprocess.run(
        ["docker", "network", "inspect", name, "--format",
         f"{{{{index .Labels \"{_COMPOSE_NETWORK_LABEL}\"}}}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if inspect.returncode != 0:
        # Network doesn't exist — create it with the correct label.
        subprocess.run(
            ["docker", "network", "create",
             "--label", f"{_COMPOSE_NETWORK_LABEL}={name}", name],
            capture_output=True, check=False,
        )
        return

    if inspect.stdout.strip() == name:
        return  # Label already correct.

    # Label is wrong — force-disconnect any attached containers so rm succeeds,
    # recreate with the correct label, then reconnect them.
    containers_r = subprocess.run(
        ["docker", "network", "inspect", name, "--format",
         "{{range $id, $_ := .Containers}}{{$id}}\n{{end}}"],
        capture_output=True, text=True, check=False,
    )
    container_ids = [
        c.strip() for c in containers_r.stdout.splitlines() if c.strip()
    ]

    for cid in container_ids:
        subprocess.run(
            ["docker", "network", "disconnect", "-f", name, cid],
            capture_output=True, check=False,
        )

    rm_rc = subprocess.run(
        ["docker", "network", "rm", name],
        capture_output=True, check=False,
    ).returncode

    if rm_rc != 0:
        return  # Can't fix while network is still in use.

    subprocess.run(
        ["docker", "network", "create",
         "--label", f"{_COMPOSE_NETWORK_LABEL}={name}", name],
        capture_output=True, check=False,
    )

    for cid in container_ids:
        subprocess.run(
            ["docker", "network", "connect", name, cid],
            capture_output=True, check=False,
        )


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
    ensure_network("p4n4-net")
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
