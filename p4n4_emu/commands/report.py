"""p4n4-emu report — bundle a run's status, usage over time, restarts and logs into one archive.

`status` shows one moment. A report samples usage several times, records what
`docker inspect` knows about each container's life (restarts, OOM kills, exit
codes), saves every container's recent logs and writes it all to a `.tar.gz`
(or a directory): `report.json` for scripts, `summary.md` for people, and
`logs/<container>.log`. Run it on the machine the stacks run on, then copy the
archive off before tearing the machine down.
"""

from __future__ import annotations

import io
import json
import platform
import socket
import subprocess
import tarfile
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from p4n4_emu import __version__
from p4n4_emu.commands import status
from p4n4_emu.commands.sim import _SIM_CONTAINER
from p4n4_emu.utils import calibration
from p4n4_emu.utils.docker_host import cgroup_v2, docker_host
from p4n4_emu.utils.usage import Usage, applied_limits, format_size, live_usage

console = Console()

# Peak usage at or above this share of its limit is a warning
_NEAR_LIMIT = 0.9


def cmd(
    stack: Annotated[
        str | None,
        typer.Option(
            "--stack",
            "-s",
            help="Stack(s) to report on: iot, ai, extras, comma-separated, or all. "
            "Default: the current p4n4 project's enabled stacks (or iot).",
        ),
    ] = None,
    stack_dir: Annotated[
        Path | None,
        typer.Option("--stack-dir", help="Directory containing the stack's compose file."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            "-o",
            help="Where to write the report: a .tar.gz / .tgz archive, or else a directory. "
            "Default: ./p4n4-emu-report-<UTC time>.tar.gz",
        ),
    ] = None,
    samples: Annotated[
        int,
        typer.Option("--samples", min=1, help="How many times to sample CPU and memory."),
    ] = 5,
    interval: Annotated[
        float,
        typer.Option("--interval", min=0.0, help="Seconds between samples."),
    ] = 2.0,
    tail: Annotated[
        int, typer.Option("--tail", min=0, help="Log lines to keep per container.")
    ] = 500,
    labels: Annotated[
        list[str] | None,
        typer.Option(
            "--label",
            "-l",
            help="KEY=VALUE recorded in the report, e.g. instance-type=t4g.large. Repeatable.",
        ),
    ] = None,
    fail_on_issues: Annotated[
        bool,
        typer.Option(
            "--fail-on-issues",
            help="Exit 1 when the report finds an error (warnings don't count), for CI.",
        ),
    ] = False,
) -> None:
    """Sample usage, record restarts and OOM kills, and save logs into one report."""
    try:
        meta = _parse_labels(labels or [])
    except ValueError as e:
        Console(stderr=True).print(f"[red]{e}[/red]")
        raise typer.Exit(1) from e

    started = datetime.now(UTC)
    stacks, active, containers = status.collect(stack, stack_dir)
    names, running = status.container_names(containers)
    if not names:
        console.print(
            "[red]No containers found for the selected stack(s).[/red] "
            "Start them with: p4n4-emu up --profile <name>"
        )
        raise typer.Exit(1)

    with console.status(f"Sampling {len(running)} containers {samples}×…"):
        taken = _sample(running, samples, interval)
    applied = applied_limits(names)
    shown = sorted(set(filter(None, active.values())))
    doc = status.status_document(stacks, active, shown, containers, applied, taken[-1])

    sim = _SIM_CONTAINER if _exists(_SIM_CONTAINER) else None
    inspected = [*names, sim] if sim else names
    states = _container_states(inspected)
    stats = _usage_stats(taken)
    for st in doc["stacks"]:
        for svc in st["services"]:
            svc["lifecycle"] = states.get(svc["container"])
            svc["usage_over_time"] = stats.get(svc["container"])

    doc["created"] = started.isoformat(timespec="seconds")
    doc["sampling"] = {"samples": samples, "interval": interval}
    doc["labels"] = meta
    doc["host"] = _host()
    doc["simulator"] = states.get(sim) if sim else None
    doc["issues"] = _issues(doc)

    logs = {name: _logs(name, tail) for name in inspected}
    stamp = started.strftime("%Y%m%d-%H%M%S")
    target = output or Path(f"p4n4-emu-report-{stamp}.tar.gz")
    files = {
        "report.json": json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
        "summary.md": summary_markdown(doc),
        **{f"logs/{name}.log": text for name, text in logs.items()},
    }
    _write(target, f"p4n4-emu-report-{stamp}", files)

    _print_summary(doc)
    console.print(f"Report written to [bold]{target}[/bold]")
    if fail_on_issues and any(i["level"] == "error" for i in doc["issues"]):
        raise typer.Exit(1)


def _parse_labels(labels: list[str]) -> dict[str, str]:
    out = {}
    for label in labels:
        key, sep, value = label.partition("=")
        if not sep or not key.strip():
            raise ValueError(f"--label must be KEY=VALUE, not {label!r}")
        out[key.strip()] = value.strip()
    return out


def _sample(running: list[str], samples: int, interval: float) -> list[dict[str, Usage]]:
    """*samples* readings of `docker stats`, *interval* seconds apart (each takes ~1 s)."""
    taken = []
    for i in range(samples):
        if i:
            time.sleep(interval)
        taken.append(live_usage(running))
    return taken


def _usage_stats(taken: list[dict[str, Usage]]) -> dict[str, dict]:
    """Mean and peak CPU (cores) and memory (bytes) per container, over the samples."""
    series: dict[str, list[Usage]] = {}
    for reading in taken:
        for name, use in reading.items():
            series.setdefault(name, []).append(use)
    return {
        name: {
            "samples": len(uses),
            "cpus": {
                "mean": round(sum(u.cpus for u in uses) / len(uses), 4),
                "peak": max(u.cpus for u in uses),
            },
            "memory": {
                "mean": sum(u.memory for u in uses) // len(uses),
                "peak": max(u.memory for u in uses),
            },
        }
        for name, uses in series.items()
    }


def _exists(container: str) -> bool:
    r = subprocess.run(["docker", "inspect", container], capture_output=True, check=False)
    return r.returncode == 0


def _container_states(containers: list[str]) -> dict[str, dict]:
    """What `docker inspect` says about each container's life, by container name."""
    if not containers:
        return {}
    r = subprocess.run(
        ["docker", "inspect", *containers], capture_output=True, text=True, check=False
    )
    try:
        docs = json.loads(r.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    states = {}
    for d in docs:
        state = d.get("State") or {}
        states[d.get("Name", "").lstrip("/")] = {
            "status": state.get("Status"),
            "restarts": d.get("RestartCount", 0),
            "oom_killed": bool(state.get("OOMKilled")),
            "exit_code": state.get("ExitCode"),
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
            "health": (state.get("Health") or {}).get("Status"),
            "image": (d.get("Config") or {}).get("Image"),
            "platform": d.get("Platform"),
        }
    return states


def _logs(container: str, tail: int) -> str:
    """The container's last *tail* log lines, stdout and stderr interleaved, with timestamps."""
    r = subprocess.run(
        ["docker", "logs", "--timestamps", "--tail", str(tail), container],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        check=False,
    )
    return r.stdout


def _host() -> dict:
    engine = docker_host()
    return {
        "hostname": socket.gethostname(),
        "machine": platform.machine(),
        "platform": platform.platform(),
        "p4n4_emu": __version__,
        "docker": asdict(engine) if engine else None,
        "cgroup_v2": cgroup_v2(engine),
        "calibration": calibration.load(),
    }


def _issues(doc: dict) -> list[dict]:
    """Errors (the run went wrong) and warnings (worth a look) found in the report."""
    issues = []

    def add(level: str, where: str, text: str) -> None:
        issues.append({"level": level, "where": where, "issue": text})

    if not any(st["profile"] for st in doc["stacks"]):
        add("warning", "stacks", "no stack runs under p4n4-emu, so no limits apply")
    if not doc["host"]["cgroup_v2"]:
        add("warning", "host", "cgroup v1: disk limits don't throttle buffered writes")

    for st in doc["stacks"]:
        for svc in st["services"]:
            where = svc["container"] or svc["service"]
            life = svc.get("lifecycle") or {}
            if svc["state"] != "running":
                code = life.get("exit_code")
                exit_note = f" (exit {code})" if code not in (None, 0) else ""
                add("error", where, f"{svc['state']}{exit_note}")
            if svc["health"] == "unhealthy":
                add("error", where, "unhealthy")
            if life.get("oom_killed"):
                add("error", where, "OOM-killed: over its memory limit")
            if life.get("restarts"):
                add("error", where, f"restarted {life['restarts']}×")
            if svc["limits_state"] == "stale":
                add("error", where, "limits differ from the overlay: " +
                    "; ".join(svc["differences"]))
            elif svc["limits_state"] == "not-in-overlay":
                add("error", where, "not in the overlay, so it runs without limits")
            over = svc.get("usage_over_time") or {}
            limits = svc["limits"]
            if over and limits["cpus"] and over["cpus"]["peak"] >= _NEAR_LIMIT * limits["cpus"]:
                add("warning", where,
                    f"CPU peaked at {over['cpus']['peak']:.2f} of {limits['cpus']:.2f} cores")
            if (over and limits["memory"]
                    and over["memory"]["peak"] >= _NEAR_LIMIT * limits["memory"]):
                add("warning", where,
                    f"memory peaked at {format_size(over['memory']['peak'])} "
                    f"of {format_size(limits['memory'])}")

    sim = doc.get("simulator")
    if sim and sim["status"] != "running":
        add("error", _SIM_CONTAINER, f"{sim['status']} (exit {sim['exit_code']})")
    return issues


def _cpu(over: dict | None, limit: float | None) -> str:
    if not over:
        return "–"
    cap = f"{limit:.2f}" if limit else "–"
    return f"{over['cpus']['mean']:.2f} / {over['cpus']['peak']:.2f} / {cap}"


def _memory(over: dict | None, limit: int | None) -> str:
    if not over:
        return "–"
    cap = format_size(limit) if limit else "–"
    mem = over["memory"]
    return f"{format_size(mem['mean'])} / {format_size(mem['peak'])} / {cap}"


def _rows(st: dict) -> list[list[str]]:
    rows = []
    for svc in st["services"]:
        life = svc.get("lifecycle") or {}
        over = svc.get("usage_over_time")
        rows.append([
            svc["service"],
            svc["state"] or "?",
            svc["health"] or "",
            str(life.get("restarts", 0)) + (" (OOM)" if life.get("oom_killed") else ""),
            _cpu(over, svc["limits"]["cpus"]),
            _memory(over, svc["limits"]["memory"]),
            svc["limits_state"],
        ])
    return rows


_HEADERS = ["Service", "State", "Health", "Restarts", "CPU mean / peak / limit",
            "Memory mean / peak / limit", "Limits"]


def summary_markdown(doc: dict) -> str:
    """The report for people: header, issues, and a table per stack."""
    host = doc["host"]
    engine = host["docker"] or {}
    sampling = doc["sampling"]
    lines = [
        "# p4n4-emu report",
        "",
        f"- Created: {doc['created']} "
        f"({sampling['samples']} samples, {sampling['interval']:g} s apart)",
        f"- Host: {host['hostname']} · {host['machine']} · "
        f"Docker {engine.get('version', '?')} · "
        f"cgroup {'v2' if host['cgroup_v2'] else 'v1'}",
        f"- p4n4-emu {host['p4n4_emu']}",
    ]
    if doc["labels"]:
        lines.append("- Labels: " + ", ".join(f"{k}={v}" for k, v in doc["labels"].items()))
    lines += ["", "## Issues", ""]
    if doc["issues"]:
        lines += [f"- **{i['level']}** `{i['where']}`: {i['issue']}" for i in doc["issues"]]
    else:
        lines.append("None.")
    for st in doc["stacks"]:
        lines += ["", f"## {st['stack']} stack — {st['profile'] or 'not under p4n4-emu'}", ""]
        if not st["services"]:
            lines.append("No containers.")
            continue
        lines.append("| " + " | ".join(_HEADERS) + " |")
        lines.append("|" + "---|" * len(_HEADERS))
        lines += ["| " + " | ".join(row) + " |" for row in _rows(st)]
    sim = doc.get("simulator")
    if sim:
        lines += ["", f"Sensor simulator: {sim['status']}, {sim['restarts']} restarts."]
    return "\n".join(lines) + "\n"


def _print_summary(doc: dict) -> None:
    for st in doc["stacks"]:
        if not st["services"]:
            continue
        table = Table(title=f"{st['stack']} stack — {st['profile'] or 'not under p4n4-emu'}")
        for header in _HEADERS:
            table.add_column(header, justify="right" if "/" in header else "left")
        for row in _rows(st):
            table.add_row(*row)
        console.print(table)
    if not doc["issues"]:
        console.print("[green]No issues found.[/green]")
    for i in doc["issues"]:
        color = "red" if i["level"] == "error" else "yellow"
        console.print(f"[{color}]{i['level']}[/{color}] {i['where']}: {i['issue']}")


def _write(target: Path, folder: str, files: dict[str, str]) -> None:
    """*files* into a .tar.gz under *folder*/, or straight into the directory *target*."""
    if target.name.endswith((".tar.gz", ".tgz")):
        target.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(target, "w:gz") as tar:
            for name, text in files.items():
                data = text.encode()
                info = tarfile.TarInfo(f"{folder}/{name}")
                info.size = len(data)
                info.mtime = int(time.time())
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
        return
    for name, text in files.items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
