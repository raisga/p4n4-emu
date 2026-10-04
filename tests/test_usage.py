"""Tests for limit and usage parsing, and for comparing overlay limits with applied ones."""

import json
import subprocess

import pytest
import yaml

from p4n4_emu.overlays.generator import render_overlay
from p4n4_emu.profiles.loader import load_profile
from p4n4_emu.utils import usage
from p4n4_emu.utils.usage import Limits


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("358m", 358 * 1024**2),      # Compose: binary
        ("2g", 2 * 1024**3),
        ("512KiB", 512 * 1024),       # docker stats
        ("41.2MiB", int(41.2 * 1024**2)),
        ("2.449GiB", int(2.449 * 1024**3)),
        ("5.71kB", 5710),             # decimal, as in NetIO
        ("0B", 0),
        ("1024", 1024),
        ("lots", None),
        ("12XB", None),
    ],
)
def test_parse_size(text, expected):
    assert usage.parse_size(text) == expected


@pytest.mark.parametrize(
    ("n", "text"),
    [
        (512, "512 B"),
        (520 * 1024, "520 KiB"),
        (32 * 1024**2, "32.0 MiB"),
        (358 * 1024**2, "358 MiB"),
        (int(2.4 * 1024**3), "2.4 GiB"),
        (3 * 1024**4, "3.0 TiB"),
    ],
)
def test_format_size(n, text):
    assert usage.format_size(n) == text


def _overlay(blkio="/dev/sda"):
    rendered = render_overlay(load_profile("rpi5"), "iot", blkio, services=["mqtt"])
    return yaml.safe_load(rendered)


# What `docker inspect` returned for a container created with the rpi5 mqtt overlay
_HOST_CONFIG = {
    "NanoCpus": 400_000_000,
    "CpuQuota": 0,
    "Memory": 375_390_208,
    "MemorySwap": 375_390_208,
    "BlkioWeight": 300,
    "BlkioDeviceReadBps": [{"Path": "/dev/sda", "Rate": 104_857_600}],
    "BlkioDeviceWriteBps": [{"Path": "/dev/sda", "Rate": 104_857_600}],
}


def test_expected_limits_from_overlay():
    assert usage.expected_limits(_overlay())["mqtt"] == Limits(
        cpus=0.40,
        memory=358 * 1024**2,
        memswap=358 * 1024**2,
        read_bps=104_857_600,
        write_bps=104_857_600,
    )


def test_expected_limits_without_blkio():
    limits = usage.expected_limits(_overlay(blkio=None))["mqtt"]
    assert limits.read_bps is None and limits.write_bps is None


def test_applied_limits_match_expected(monkeypatch):
    docs = [{"Name": "/p4n4-mqtt", "HostConfig": _HOST_CONFIG}]
    monkeypatch.setattr(
        usage.subprocess, "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(docs)),
    )
    applied = usage.applied_limits(["p4n4-mqtt"])
    assert usage.differences(usage.expected_limits(_overlay())["mqtt"], applied["p4n4-mqtt"]) == []


def test_unlimited_container_has_no_limits():
    host = {"NanoCpus": 0, "Memory": 0, "MemorySwap": 0, "BlkioDeviceReadBps": None}
    assert usage._limits_from_host_config(host) == Limits()


def test_differences_name_each_stale_limit():
    expected = Limits(cpus=0.4, memory=716 * 1024**2, read_bps=200 * 1024**2)
    applied = Limits(cpus=1.0, memory=358 * 1024**2, read_bps=100 * 1024**2)
    assert usage.differences(expected, applied) == [
        "cpus 1.00 ≠ 0.40",
        "memory 358 MiB ≠ 716 MiB",
        "disk read 100 MiB/s ≠ 200 MiB/s",
    ]


def test_differences_ignore_limits_the_overlay_does_not_set():
    assert usage.differences(Limits(cpus=0.4), Limits(cpus=0.4, memory=1024)) == []


def test_differences_report_missing_limits():
    assert usage.differences(Limits(cpus=0.4), Limits()) == ["cpus none ≠ 0.40"]


def test_live_usage_parses_docker_stats(monkeypatch):
    rows = [
        {"Name": "p4n4-mqtt", "CPUPerc": "39.99%", "MemUsage": "512KiB / 358MiB"},
        {"Name": "p4n4-influxdb", "CPUPerc": "--", "MemUsage": "-- / --"},
    ]
    stdout = "\n".join(json.dumps(r) for r in rows)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout)

    monkeypatch.setattr(usage.subprocess, "run", fake_run)
    result = usage.live_usage(["p4n4-mqtt", "p4n4-influxdb"])
    assert list(result) == ["p4n4-mqtt"]
    assert result["p4n4-mqtt"].cpus == pytest.approx(0.3999)
    assert result["p4n4-mqtt"].memory == 512 * 1024
    # One sample for every container
    assert len(calls) == 1 and calls[0][-2:] == ["p4n4-mqtt", "p4n4-influxdb"]


def test_no_containers_runs_no_docker(monkeypatch):
    monkeypatch.setattr(usage.subprocess, "run", lambda *a, **k: pytest.fail("ran docker"))
    assert usage.live_usage([]) == {}
    assert usage.applied_limits([]) == {}
