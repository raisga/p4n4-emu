"""Docker Compose subprocess wrappers — multi-file variant for overlay support."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import yaml

from p4n4_emu.utils.project import find_compose_file

_COMPOSE_NETWORK_LABEL = "com.docker.compose.network"

# Override names in the order Docker Compose looks for them. Compose only loads
# one automatically when no -f is given, so passing the emu overlay with -f
# would silently drop it unless it is listed explicitly.
_OVERRIDE_FILES = (
    "compose.override.yml",
    "compose.override.yaml",
    "docker-compose.override.yml",
    "docker-compose.override.yaml",
)


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


def _compose_file_env(cwd: Path) -> str | None:
    """COMPOSE_FILE from the environment, else from the project's .env file."""
    if os.environ.get("COMPOSE_FILE"):
        return os.environ["COMPOSE_FILE"]
    try:
        lines = (cwd / ".env").read_text().splitlines()
    except OSError:
        return None
    for line in lines:
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == "COMPOSE_FILE":
            return value.strip().strip("\"'") or None
    return None


def compose_files(cwd: Path) -> list[Path]:
    """Compose files Docker Compose would load in *cwd* when run without -f."""
    env_files = _compose_file_env(cwd)
    if env_files:
        sep = os.environ.get("COMPOSE_PATH_SEPARATOR", os.pathsep)
        return [cwd / f for f in env_files.split(sep) if f]

    base = find_compose_file(cwd)
    if base is None:
        return []
    files = [base]
    for name in _OVERRIDE_FILES:
        if (cwd / name).exists():
            files.append(cwd / name)
            break
    return files


def compose_cmd(cwd: Path, overlay: Path | None = None) -> list[str]:
    """`docker compose` with the project's files plus the optional emu overlay."""
    cmd = ["docker", "compose"]
    for f in compose_files(cwd):
        cmd += ["-f", str(f)]
    if overlay is not None:
        cmd += ["-f", str(overlay)]
    return cmd


def list_services(cwd: Path) -> list[str] | None:
    """Service names in the project's compose config, or None if unreadable.

    Asks `docker compose config` first so profiles, includes and extends are
    resolved; falls back to reading the YAML files directly.
    """
    r = subprocess.run(
        [*compose_cmd(cwd), "config", "--services"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode == 0:
        return [line.strip() for line in r.stdout.splitlines() if line.strip()]

    files = compose_files(cwd)
    if not files:
        return None
    services: dict[str, None] = {}
    for f in files:
        try:
            doc = yaml.safe_load(f.read_text()) or {}
        except (OSError, yaml.YAMLError):
            return None
        services.update(dict.fromkeys(doc.get("services") or {}))
    return list(services)


def _base(
    args: list[str],
    cwd: Path,
    *,
    overlay: Path | None = None,
) -> int:
    result = subprocess.run([*compose_cmd(cwd, overlay), *args], cwd=cwd, check=False)
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
    cmd = [*compose_cmd(cwd, overlay), "ps", "--format", "json"]
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
