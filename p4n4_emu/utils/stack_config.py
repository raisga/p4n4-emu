"""Names a stack's compose config defines: its shared networks and its MQTT broker.

p4n4-iot names the network `p4n4-net` and the broker container `p4n4-mqtt`, but a
project can rename either, so they are read from `docker compose config` and the
p4n4 names are only the fallback when the config can't be read.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

from p4n4_emu.utils.compose import NETWORK_SUBNET, compose_cmd

DEFAULT_NETWORK = "p4n4-net"
DEFAULT_BROKER = "p4n4-mqtt"
# Service names or image names that mark the MQTT broker
_BROKER_SERVICES = ("mqtt", "mosquitto", "broker")
_BROKER_IMAGES = ("mosquitto", "emqx", "hivemq", "vernemq")


@dataclass(frozen=True)
class Network:
    name: str
    subnet: str | None = None
    external: bool = False
    # The key under `networks:`; Compose labels the networks it creates with it
    key: str | None = None

    @property
    def label(self) -> str:
        # An external network belongs to the stack that declares it, which in
        # p4n4 uses the name as the key (`p4n4-net: {name: p4n4-net}`)
        return self.name if self.external else self.key or self.name


@dataclass(frozen=True)
class Broker:
    host: str  # hostname clients on the network connect to
    container: str  # container to wait on
    network: str


DEFAULT_BROKER_INFO = Broker(DEFAULT_BROKER, DEFAULT_BROKER, DEFAULT_NETWORK)


def load_config(cwd: Path) -> dict | None:
    """The stack's resolved compose config (every profile enabled), or None."""
    r = subprocess.run(
        [*compose_cmd(cwd), "--profile", "*", "config", "--format", "json"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        return None
    try:
        config = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    return config if isinstance(config, dict) else None


def _network_name(config: dict, key: str) -> str:
    spec = (config.get("networks") or {}).get(key) or {}
    if spec.get("name"):
        return spec["name"]
    # Compose prefixes the networks it names itself with the project name
    return f"{config['name']}_{key}" if config.get("name") else key


def shared_networks(config: dict | None) -> list[Network]:
    """Networks the stack refers to by an explicit name, which other stacks can join.

    External ones must exist before `up`; declared ones are created ahead of Compose
    so the label they get lets every stack adopt them. Project-scoped networks (no
    `name:`) are left to Compose.
    """
    if config is None:
        return [Network(DEFAULT_NETWORK, NETWORK_SUBNET)]
    networks = []
    for key, spec in (config.get("networks") or {}).items():
        spec = spec or {}
        name = spec.get("name")
        # `config` spells out the names Compose gives project networks itself
        if not name or (config.get("name") and name == f"{config['name']}_{key}"):
            continue
        subnets = [c.get("subnet") for c in (spec.get("ipam") or {}).get("config") or []]
        networks.append(
            Network(name, next((s for s in subnets if s), None), bool(spec.get("external")), key)
        )
    return networks


def _is_broker(name: str, service: dict) -> bool:
    image = str(service.get("image", "")).lower()
    return name in _BROKER_SERVICES or any(b in image for b in _BROKER_IMAGES)


def find_broker(config: dict | None) -> Broker:
    """The stack's MQTT broker: its hostname, its container and a network it is on."""
    if config is None:
        return DEFAULT_BROKER_INFO
    for name, service in (config.get("services") or {}).items():
        service = service or {}
        if not _is_broker(name, service):
            continue
        container = service.get("container_name") or (
            f"{config['name']}-{name}-1" if config.get("name") else name
        )
        networks = list(service.get("networks") or {})
        network = _network_name(config, networks[0]) if networks else (
            f"{config['name']}_default" if config.get("name") else DEFAULT_NETWORK
        )
        # The service name resolves on every network it joins; container_name too
        host = service.get("container_name") or name
        return Broker(host, container, network)
    return DEFAULT_BROKER_INFO
