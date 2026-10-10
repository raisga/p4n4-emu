"""Tests for reading networks and the MQTT broker from a stack's compose config."""

from p4n4_emu.utils.stack_config import (
    DEFAULT_BROKER_INFO,
    Broker,
    Network,
    find_broker,
    shared_networks,
)

IOT = {
    "name": "greenhouse-iot",
    "networks": {
        "p4n4-net": {
            "name": "p4n4-net",
            "driver": "bridge",
            "ipam": {"config": [{"subnet": "172.20.0.0/16"}]},
        },
        "default": {"name": "greenhouse-iot_default"},
    },
    "services": {
        "influxdb": {"container_name": "p4n4-influxdb", "networks": {"p4n4-net": None}},
        "mqtt": {
            "image": "eclipse-mosquitto:2",
            "container_name": "p4n4-mqtt",
            "networks": {"p4n4-net": None},
        },
    },
}


def test_shared_networks_reads_name_and_subnet():
    # greenhouse-iot_default is the name Compose gives the project network itself
    assert shared_networks(IOT) == [
        Network("p4n4-net", "172.20.0.0/16", False, "p4n4-net")
    ]


def test_shared_networks_label_is_the_key():
    config = {"networks": {"bus": {"name": "plant-bus"}}}
    assert shared_networks(config)[0].label == "bus"


def test_shared_networks_external_labelled_by_name():
    # The stack that declares it owns it; p4n4 stacks use the name as the key
    config = {"networks": {"net": {"name": "plant-net", "external": True}}}
    assert shared_networks(config) == [Network("plant-net", None, True, "net")]
    assert shared_networks(config)[0].label == "plant-net"


def test_shared_networks_skips_project_scoped():
    config = {"name": "proj", "networks": {"internal": {"driver": "bridge"}}}
    assert shared_networks(config) == []


def test_shared_networks_defaults_without_config():
    assert [n.name for n in shared_networks(None)] == ["p4n4-net"]


def test_broker_from_container_name():
    assert find_broker(IOT) == Broker("p4n4-mqtt", "p4n4-mqtt", "p4n4-net")


def test_broker_renamed_network_and_container():
    config = {
        "name": "plant",
        "networks": {"bus": {"name": "plant-bus"}},
        "services": {
            "broker": {
                "image": "eclipse-mosquitto:2",
                "container_name": "plant-mqtt",
                "networks": {"bus": None},
            }
        },
    }
    assert find_broker(config) == Broker("plant-mqtt", "plant-mqtt", "plant-bus")


def test_broker_found_by_image_without_container_name():
    config = {
        "name": "plant",
        "services": {"events": {"image": "emqx/emqx:5", "networks": {"default": None}}},
    }
    # Reachable by service name; Compose names the container <project>-<service>-1
    assert find_broker(config) == Broker("events", "plant-events-1", "plant_default")


def test_broker_defaults_without_config_or_broker():
    assert find_broker(None) == DEFAULT_BROKER_INFO
    assert find_broker({"services": {"influxdb": {}}}) == DEFAULT_BROKER_INFO
