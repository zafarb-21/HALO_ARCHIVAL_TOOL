#!/usr/bin/env python3

"""Create, run, monitor, and collect one synchronized HALO swarm mission."""

from __future__ import annotations

import argparse
import ast
import ipaddress
import json
import os
import re
import shlex
import signal
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

try:
    from create_mission_archive import (
        capture_base_station_code_state,
        sanitize_id_component,
    )
except ImportError:
    from scripts.create_mission_archive import (
        capture_base_station_code_state,
        sanitize_id_component,
    )


IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_unique(items: list[str], message: str) -> None:
    if message not in items:
        items.append(message)


def save_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def run_command(
    command: list[str], timeout: float | None = None
) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return {
            "command": shlex.join(command),
            "return_code": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "ok": result.returncode == 0,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode()
            if isinstance(exc.stdout, bytes)
            else (exc.stdout or "")
        )
        stderr = (
            exc.stderr.decode()
            if isinstance(exc.stderr, bytes)
            else (exc.stderr or "")
        )
        return {
            "command": shlex.join(command),
            "return_code": 124,
            "stdout": stdout.strip(),
            "stderr": stderr.strip() or f"Timed out after {timeout:g} seconds",
            "ok": False,
        }
    except OSError as exc:
        return {
            "command": shlex.join(command),
            "return_code": 127,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "ok": False,
        }


def failure_detail(result: dict[str, Any]) -> str:
    detail = result.get("stderr") or result.get("stdout")
    if detail:
        return str(detail).splitlines()[0]
    return f"exit code {result.get('return_code')}"


def remote_command(command: str) -> str:
    return f"LC_ALL=C LANG=C sh -c {shlex.quote(command)}"


def ssh_command(host: str, command: str) -> list[str]:
    return ["ssh"] + SSH_OPTIONS + [host, remote_command(command)]


def rsync_command(source: str, destination: str, archive: bool = True) -> list[str]:
    flags = "-av" if archive else "-az"
    return [
        "rsync",
        flags,
        "-e",
        "ssh -o BatchMode=yes -o ConnectTimeout=10",
        "--rsync-path=LC_ALL=C LANG=C rsync",
        source,
        destination,
    ]


def log_event(
    log: dict[str, Any],
    event: str,
    drone_id: str | None = None,
    **details: Any,
) -> None:
    record: dict[str, Any] = {
        "timestamp_utc": utc_now(),
        "event": event,
    }
    if drone_id is not None:
        record["drone_id"] = drone_id
    record.update(details)
    log.setdefault("events", []).append(record)


def sync_log(path: Path, log: dict[str, Any]) -> None:
    log["updated_utc"] = utc_now()
    save_json_atomic(path, log)


def strip_yaml_comment(line: str) -> str:
    quote: str | None = None
    escaped = False
    for index, character in enumerate(line):
        if escaped:
            escaped = False
            continue
        if character == "\\" and quote == '"':
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if character == "#" and quote is None:
            if index == 0 or line[index - 1].isspace():
                return line[:index]
    return line


def parse_yaml_scalar(value: str) -> Any:
    value = value.strip()
    if not value:
        return ""
    lowered = value.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    if lowered in {"null", "none", "~"}:
        return None
    if value.startswith(("'", '"')) and value.endswith(value[0]):
        try:
            return ast.literal_eval(value)
        except (SyntaxError, ValueError):
            return value[1:-1]
    if re.fullmatch(r"[-+]?\d+", value):
        return int(value)
    if re.fullmatch(r"[-+]?(?:\d+\.\d*|\d*\.\d+)", value):
        return float(value)
    return value


def split_yaml_mapping(text: str, path: Path, line_number: int) -> tuple[str, str]:
    key, separator, value = text.partition(":")
    if not separator or not key.strip():
        raise ValueError(
            f"{path}:{line_number}: expected a YAML key/value mapping"
        )
    return key.strip(), value.strip()


def load_yaml_subset(path: Path) -> dict[str, Any]:
    """Load the mapping/list subset used by HALO configs without PyYAML."""
    records: list[tuple[int, str, int]] = []
    for line_number, original in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if "\t" in original[: len(original) - len(original.lstrip())]:
            raise ValueError(f"{path}:{line_number}: tabs are not allowed")
        without_comment = strip_yaml_comment(original).rstrip()
        if not without_comment.strip():
            continue
        indent = len(without_comment) - len(without_comment.lstrip(" "))
        records.append((indent, without_comment.lstrip(" "), line_number))

    if not records:
        return {}

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        is_list = records[index][1].startswith("- ")
        container: Any = [] if is_list else {}

        while index < len(records):
            current_indent, text, line_number = records[index]
            if current_indent < indent:
                break
            if current_indent > indent:
                raise ValueError(
                    f"{path}:{line_number}: unexpected indentation"
                )

            if is_list:
                if not text.startswith("- "):
                    raise ValueError(
                        f"{path}:{line_number}: mixed list and mapping block"
                    )
                item_text = text[2:].strip()
                index += 1
                if not item_text:
                    if index < len(records) and records[index][0] > indent:
                        item, index = parse_block(index, records[index][0])
                    else:
                        item = None
                elif ":" in item_text:
                    key, value = split_yaml_mapping(
                        item_text, path, line_number
                    )
                    item = {}
                    if value:
                        item[key] = parse_yaml_scalar(value)
                    elif index < len(records) and records[index][0] > indent:
                        item[key], index = parse_block(
                            index, records[index][0]
                        )
                    else:
                        item[key] = {}
                    if index < len(records) and records[index][0] > indent:
                        extra, index = parse_block(index, records[index][0])
                        if not isinstance(extra, dict):
                            raise ValueError(
                                f"{path}:{records[index - 1][2]}: "
                                "list mapping continuation must be a mapping"
                            )
                        item.update(extra)
                else:
                    item = parse_yaml_scalar(item_text)
                    if index < len(records) and records[index][0] > indent:
                        raise ValueError(
                            f"{path}:{records[index][2]}: "
                            "scalar list item cannot have children"
                        )
                container.append(item)
            else:
                if text.startswith("- "):
                    raise ValueError(
                        f"{path}:{line_number}: mixed mapping and list block"
                    )
                key, value = split_yaml_mapping(text, path, line_number)
                if key in container:
                    raise ValueError(
                        f"{path}:{line_number}: duplicate key {key!r}"
                    )
                index += 1
                if value:
                    container[key] = parse_yaml_scalar(value)
                elif index < len(records) and records[index][0] > indent:
                    container[key], index = parse_block(
                        index, records[index][0]
                    )
                else:
                    container[key] = {}
        return container, index

    parsed, final_index = parse_block(0, records[0][0])
    if final_index != len(records) or not isinstance(parsed, dict):
        raise ValueError(f"{path}: top-level YAML value must be a mapping")
    return parsed


def validate_identifier(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or value in {"", ".", ".."}
        or IDENTIFIER_PATTERN.fullmatch(value) is None
    ):
        raise ValueError(
            f"{label} must contain only letters, numbers, periods, "
            "underscores, and hyphens"
        )
    return value


def validate_remote_root(value: Any, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(
        r"/[A-Za-z0-9._/-]*", value
    ) is None:
        raise ValueError(f"{label} must be a safe absolute POSIX path")
    return value.rstrip("/") or "/"


def resolve_input_path(
    reference: str,
    code_root: Path,
    inventory_path: Path,
) -> Path:
    candidate = Path(reference).expanduser()
    candidates = (
        [candidate]
        if candidate.is_absolute()
        else [
            code_root / candidate,
            Path.cwd() / candidate,
            inventory_path.parent / candidate,
        ]
    )
    for item in candidates:
        resolved = item.resolve()
        if resolved.is_file():
            return resolved
    raise FileNotFoundError(
        f"Could not resolve config reference {reference!r}; checked: "
        + ", ".join(str(item.resolve()) for item in candidates)
    )


def normalize_swarm(
    swarm_path: Path, code_root: Path
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    swarm_data = load_yaml_subset(swarm_path)
    raw_drones = swarm_data.get("drones")
    if not isinstance(raw_drones, list) or not raw_drones:
        raise ValueError(f"{swarm_path}: drones must be a non-empty list")

    normalized: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_hosts: set[str] = set()
    seen_ips: set[str] = set()
    for index, raw in enumerate(raw_drones, start=1):
        if not isinstance(raw, dict):
            raise ValueError(
                f"{swarm_path}: drones item {index} must be a mapping"
            )
        drone_id = validate_identifier(
            raw.get("drone_id"), f"drones item {index} drone_id"
        )
        ssh_host = validate_identifier(
            raw.get("ssh_host"), f"{drone_id} ssh_host"
        )
        ip_value = raw.get("ip_address")
        try:
            ip_address = str(ipaddress.ip_address(str(ip_value)))
        except ValueError as exc:
            raise ValueError(
                f"{swarm_path}: invalid IP for {drone_id}: {ip_value!r}"
            ) from exc
        config_ref = raw.get("config")
        if not isinstance(config_ref, str) or not config_ref:
            raise ValueError(f"{swarm_path}: missing config for {drone_id}")
        config_path = resolve_input_path(config_ref, code_root, swarm_path)
        config = load_yaml_subset(config_path)

        if config.get("drone_id") != drone_id:
            raise ValueError(
                f"{config_path}: drone_id does not match swarm entry {drone_id}"
            )
        if str(config.get("ip_address")) != ip_address:
            raise ValueError(
                f"{config_path}: ip_address does not match swarm entry "
                f"{ip_address}"
            )
        if config.get("ssh_user") != "root":
            raise ValueError(f"{config_path}: ssh_user must be root")
        if drone_id in seen_ids or ssh_host in seen_hosts or ip_address in seen_ips:
            raise ValueError(
                f"{swarm_path}: duplicate drone ID, SSH host, or IP near "
                f"{drone_id}"
            )
        seen_ids.add(drone_id)
        seen_hosts.add(ssh_host)
        seen_ips.add(ip_address)

        paths = config.get("paths", {})
        if not isinstance(paths, dict):
            raise ValueError(f"{config_path}: paths must be a mapping")
        remote_sync_root = validate_remote_root(
            paths.get("drone_sync_folder"), f"{drone_id} drone_sync_folder"
        )

        audio = config.get("audio", {})
        if not isinstance(audio, dict):
            audio = {}
        legacy_audio = (
            config.get("sensors", {}).get("respeaker", {})
            if isinstance(config.get("sensors"), dict)
            else {}
        )
        if not isinstance(legacy_audio, dict):
            legacy_audio = {}
        audio_device = str(
            audio.get(
                "audio_device", legacy_audio.get("alsa_device", "hw:0,0")
            )
        )
        sample_rate = int(
            audio.get(
                "sample_rate_hz",
                legacy_audio.get("sample_rate_hz", 16000),
            )
        )
        channels = int(
            audio.get("channels", legacy_audio.get("channels", 6))
        )
        sample_format = str(
            audio.get(
                "sample_format",
                legacy_audio.get("sample_format", "S16_LE"),
            )
        )
        if sample_rate <= 0 or channels <= 0:
            raise ValueError(
                f"{config_path}: audio sample rate and channels must be positive"
            )

        normalized.append(
            {
                "drone_id": drone_id,
                "ssh_host": ssh_host,
                "ip_address": ip_address,
                "config_path": str(config_path),
                "config_reference": config_ref,
                "config": config,
                "remote_sync_root": remote_sync_root,
                "audio_device": audio_device,
                "sample_rate": sample_rate,
                "channels": channels,
                "sample_format": sample_format,
            }
        )
    return normalized, swarm_data


def remote_session_path(remote_root: str, session_id: str) -> str:
    if remote_root == "/":
        return "/" + session_id
    return remote_root + "/" + session_id


def run_parallel(
    items: list[dict[str, Any]],
    worker: Callable[[dict[str, Any]], dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not items:
        return {}
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=len(items)) as executor:
        futures = {executor.submit(worker, item): item for item in items}
        for future in as_completed(futures):
            item = futures[future]
            drone_id = item["drone_id"]
            try:
                results[drone_id] = future.result()
            except Exception as exc:
                results[drone_id] = {
                    "ok": False,
                    "errors": [
                        f"{type(exc).__name__}: {exc}"
                    ],
                    "warnings": [],
                    "actions": [],
                }
    return results


def upload_file(
    host: str, source: Path, remote_directory: str
) -> dict[str, Any]:
    return run_command(
        rsync_command(
            str(source),
            f"{host}:{remote_directory.rstrip('/')}/",
        ),
        timeout=90.0,
    )


def initialize_drone(
    drone: dict[str, Any],
    readme_path: Path,
    metadata_path: Path,
    profile_path: Path,
    archived_configs: dict[str, Path],
    agent_path: Path,
) -> dict[str, Any]:
    drone_id = drone["drone_id"]
    host = drone["ssh_host"]
    remote_dir = drone["remote_mission_dir"]
    remote_metadata = remote_dir + "/metadata"
    remote_config = remote_dir + "/config"
    directories = [
        remote_dir,
        remote_dir + "/audio",
        remote_metadata,
        remote_dir + "/status_logs",
        remote_config,
        remote_dir + "/bags",
        remote_dir + "/px4_logs",
    ]
    result: dict[str, Any] = {
        "ok": False,
        "drone_id": drone_id,
        "ssh_host": host,
        "remote_mission_dir": remote_dir,
        "remote_agent": remote_metadata + "/halo_drone_mission_agent.py",
        "actions": [],
        "copied_files": [],
        "warnings": [],
        "errors": [],
    }

    preflight = run_command(
        ssh_command(host, "hostname && date -u"), timeout=20.0
    )
    result["actions"].append(preflight)
    result["ssh_preflight"] = preflight
    if not preflight["ok"]:
        result["errors"].append(
            f"SSH preflight failed: {failure_detail(preflight)}"
        )
        return result

    quoted = " ".join(shlex.quote(value) for value in directories)
    mkdir_result = run_command(
        ssh_command(host, f"mkdir -p -- {quoted}"), timeout=30.0
    )
    result["actions"].append(mkdir_result)
    if not mkdir_result["ok"]:
        result["errors"].append(
            f"Remote folder creation failed: {failure_detail(mkdir_result)}"
        )
        return result

    copies = [
        ("README", readme_path, remote_dir),
        ("mission metadata", metadata_path, remote_metadata),
        ("profile", profile_path, remote_config),
        (
            "drone configuration",
            archived_configs[drone_id],
            remote_config,
        ),
        ("drone agent", agent_path, remote_metadata),
    ]
    for label, source, remote_destination in copies:
        copy_result = upload_file(host, source, remote_destination)
        result["actions"].append(copy_result)
        if copy_result["ok"]:
            result["copied_files"].append(
                remote_destination + "/" + source.name
            )
        else:
            result["errors"].append(
                f"{label} copy failed: {failure_detail(copy_result)}"
            )

    if result["errors"]:
        return result

    compatibility = run_command(
        ssh_command(
            host,
            "python3 {0} --help".format(
                shlex.quote(result["remote_agent"])
            ),
        ),
        timeout=25.0,
    )
    result["actions"].append(compatibility)
    result["agent_compatibility_check"] = compatibility
    if not compatibility["ok"]:
        result["errors"].append(
            "Drone agent Python compatibility check failed: "
            + failure_detail(compatibility)
        )
        return result

    result["ok"] = True
    return result


def build_agent_command(
    args: argparse.Namespace,
    drone: dict[str, Any],
    start_at_utc: str,
) -> list[str]:
    command = [
        "python3",
        drone["remote_agent"],
        "--mission-id",
        args.mission_id,
        "--drone-id",
        drone["drone_id"],
        "--drone-session-id",
        drone["drone_session_id"],
        "--mission-dir",
        drone["remote_mission_dir"],
        "--start-at-utc",
        start_at_utc,
        "--duration",
        str(args.duration),
        "--audio-device",
        drone["audio_device"],
        "--sample-rate",
        str(drone["sample_rate"]),
        "--channels",
        str(drone["channels"]),
        "--format",
        drone["sample_format"],
        "--status-interval-s",
        str(args.status_interval_s),
        "--post-disarm-wait-s",
        str(args.post_disarm_wait_s),
    ]
    if args.enable_audio:
        command.append("--enable-audio")
    if args.enable_rosbag:
        command.append("--enable-rosbag")
        if args.rosbag_topics:
            command.append("--rosbag-topics")
            command.extend(args.rosbag_topics)
    return command


def launch_drone(
    args: argparse.Namespace,
    drone: dict[str, Any],
    start_at_utc: str,
    metadata_path: Path,
) -> dict[str, Any]:
    host = drone["ssh_host"]
    result: dict[str, Any] = {
        "ok": False,
        "actions": [],
        "pid": None,
        "warnings": [],
        "errors": [],
    }
    metadata_copy = upload_file(
        host, metadata_path, drone["remote_mission_dir"] + "/metadata"
    )
    result["actions"].append(metadata_copy)
    if not metadata_copy["ok"]:
        result["errors"].append(
            "Updated mission metadata copy failed: "
            + failure_detail(metadata_copy)
        )
        return result

    command = build_agent_command(args, drone, start_at_utc)
    console_log = (
        drone["remote_mission_dir"]
        + "/status_logs/drone_agent_console.log"
    )
    launch_shell = (
        f"nohup {shlex.join(command)} > {shlex.quote(console_log)} 2>&1 "
        "< /dev/null & printf '%s\\n' $!"
    )
    launch_result = run_command(
        ssh_command(host, launch_shell), timeout=25.0
    )
    result["actions"].append(launch_result)
    result["agent_command"] = command
    result["agent_command_shell"] = shlex.join(command)
    if not launch_result["ok"]:
        result["errors"].append(
            "Drone agent launch failed: " + failure_detail(launch_result)
        )
        return result

    pid: int | None = None
    for line in reversed(launch_result["stdout"].splitlines()):
        if line.strip().isdigit():
            pid = int(line.strip())
            break
    if pid is None:
        result["errors"].append("Remote launch did not return an agent PID")
        return result
    result["pid"] = pid
    result["ok"] = True
    return result


def pull_agent_snapshot(drone: dict[str, Any]) -> dict[str, Any]:
    remote_dir = drone["remote_mission_dir"]
    pid = drone.get("pid")
    if not isinstance(pid, int):
        return {
            "ok": False,
            "agent_alive": False,
            "kind": "missing",
            "data": None,
            "error": "No remote agent PID is available",
        }
    remote_start = remote_dir + "/metadata/drone_agent_start.json"
    remote_status = remote_dir + "/metadata/drone_agent_status.json"
    remote_final = remote_dir + "/metadata/drone_agent_final.json"
    command = (
        f"if kill -0 {pid} 2>/dev/null; then echo __HALO_AGENT_ALIVE__; "
        "else echo __HALO_AGENT_EXITED__; fi; "
        f"if test -f {shlex.quote(remote_final)}; then "
        "echo __HALO_FINAL__; "
        f"cat {shlex.quote(remote_final)}; "
        f"elif test -f {shlex.quote(remote_status)}; then "
        "echo __HALO_STATUS__; "
        f"cat {shlex.quote(remote_status)}; "
        f"elif test -f {shlex.quote(remote_start)}; then "
        "echo __HALO_START__; "
        f"cat {shlex.quote(remote_start)}; "
        "else echo __HALO_NO_STATUS__; fi"
    )
    command_result = run_command(
        ssh_command(drone["ssh_host"], command), timeout=20.0
    )
    snapshot: dict[str, Any] = {
        "ok": command_result["ok"],
        "command_result": command_result,
        "agent_alive": None,
        "kind": None,
        "data": None,
    }
    if not command_result["ok"]:
        snapshot["error"] = failure_detail(command_result)
        return snapshot

    lines = command_result["stdout"].splitlines()
    snapshot["agent_alive"] = "__HALO_AGENT_ALIVE__" in lines
    marker = None
    for possible_marker, kind in (
        ("__HALO_FINAL__", "final"),
        ("__HALO_STATUS__", "status"),
        ("__HALO_START__", "start"),
        ("__HALO_NO_STATUS__", "missing"),
    ):
        if possible_marker in lines:
            marker = possible_marker
            snapshot["kind"] = kind
            break
    if marker is not None and marker != "__HALO_NO_STATUS__":
        payload = "\n".join(lines[lines.index(marker) + 1 :]).strip()
        try:
            snapshot["data"] = json.loads(payload)
        except json.JSONDecodeError as exc:
            snapshot["ok"] = False
            snapshot["error"] = (
                f"Could not parse remote {snapshot['kind']} JSON: {exc}"
            )
    return snapshot


def persist_snapshot(
    drone: dict[str, Any], snapshot: dict[str, Any]
) -> None:
    data = snapshot.get("data")
    if not isinstance(data, dict):
        return
    filenames = {
        "start": "drone_agent_start.json",
        "status": "drone_agent_status.json",
        "final": "drone_agent_final.json",
    }
    filename = filenames.get(snapshot.get("kind"))
    if filename:
        save_json_atomic(
            Path(drone["local_drone_dir"]) / "metadata" / filename,
            data,
        )


def mirror_drone(drone: dict[str, Any]) -> dict[str, Any]:
    destination = (
        Path(drone["local_drone_dir"])
        / "drone_data"
        / "audio"
        / drone["drone_session_id"]
    )
    destination.mkdir(parents=True, exist_ok=True)
    return run_command(
        rsync_command(
            f"{drone['ssh_host']}:{drone['remote_mission_dir']}/",
            str(destination) + "/",
            archive=False,
        ),
        timeout=180.0,
    )


def stop_drone_agent(drone: dict[str, Any]) -> dict[str, Any]:
    pid = drone.get("pid")
    if not isinstance(pid, int):
        return {
            "ok": False,
            "return_code": None,
            "stdout": "",
            "stderr": "No agent PID is available",
            "command": None,
        }
    return run_command(
        ssh_command(drone["ssh_host"], f"kill -INT {pid}"),
        timeout=15.0,
    )


def stop_active_agents(
    drones: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    active = [
        drone
        for drone in drones
        if isinstance(drone.get("pid"), int)
        and not isinstance(drone.get("final_data"), dict)
        and not drone.get("confirmed_agent_exit")
    ]
    return run_parallel(active, stop_drone_agent)


def wait_for_final_snapshots(
    drones: list[dict[str, Any]], wait_s: float
) -> None:
    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        active = [
            drone
            for drone in drones
            if isinstance(drone.get("pid"), int)
            and not isinstance(drone.get("final_data"), dict)
            and not drone.get("confirmed_agent_exit")
        ]
        if not active:
            return
        snapshots = run_parallel(active, pull_agent_snapshot)
        for drone in active:
            snapshot = snapshots.get(drone["drone_id"], {})
            if snapshot.get("ok"):
                persist_snapshot(drone, snapshot)
                if snapshot.get("kind") == "final":
                    drone["finalized"] = True
                    drone["final_data"] = snapshot.get("data")
                elif snapshot.get("agent_alive") is False:
                    drone["finalized"] = True
                    drone["confirmed_agent_exit"] = True
        if all(
            isinstance(drone.get("final_data"), dict)
            or drone.get("confirmed_agent_exit")
            for drone in active
        ):
            return
        time.sleep(1.0)


def create_archive(
    args: argparse.Namespace,
    drones: list[dict[str, Any]],
    swarm_path: Path,
    profile_path: Path,
    script_path: Path,
) -> tuple[Path, Path, Path, Path, dict[str, Path]]:
    archive_root = Path(args.archive_root).expanduser().resolve()
    archive_root.mkdir(parents=True, exist_ok=True)
    mission_dir = archive_root / args.mission_id
    if mission_dir.exists():
        raise FileExistsError(
            f"Mission archive already exists and was not changed: {mission_dir}"
        )

    code_root = Path(args.code_root).expanduser().resolve()
    code_state = capture_base_station_code_state(script_path, code_root)
    top_folders = [
        mission_dir / "metadata",
        mission_dir / "config" / "profiles",
        mission_dir / "config" / "drones",
    ]
    for folder in top_folders:
        folder.mkdir(parents=True, exist_ok=True)
    for drone in drones:
        drone_dir = mission_dir / "drones" / drone["drone_id"]
        for folder in (
            drone_dir / "drone_data" / "audio",
            drone_dir / "drone_data" / "px4_logs",
            drone_dir / "drone_data" / "ros_bags",
            drone_dir / "drone_data" / "status_logs",
            drone_dir / "drone_data" / "raw_sensor_data",
            drone_dir / "metadata",
            drone_dir / "processed",
            drone_dir / "plots",
            drone_dir / "reports",
        ):
            folder.mkdir(parents=True, exist_ok=True)
        drone["local_drone_dir"] = str(drone_dir)

    archived_swarm = mission_dir / "config" / "swarm_drones.yaml"
    archived_profile = mission_dir / "config" / "profiles" / profile_path.name
    shutil.copy2(swarm_path, archived_swarm)
    shutil.copy2(profile_path, archived_profile)
    archived_configs: dict[str, Path] = {}
    for drone in drones:
        source = Path(drone["config_path"])
        destination = (
            mission_dir / "config" / "drones" / source.name
        )
        shutil.copy2(source, destination)
        archived_configs[drone["drone_id"]] = destination

    code_state_path = mission_dir / "metadata" / "code_state.json"
    save_json_atomic(code_state_path, code_state)
    metadata_path = mission_dir / "metadata" / "mission_metadata.json"
    log_path = mission_dir / "metadata" / "ground_orchestrator_log.json"
    collection_path = (
        mission_dir / "metadata" / "swarm_collection_manifest.json"
    )

    metadata: dict[str, Any] = {
        "mission_id": args.mission_id,
        "mission_name": args.mission_name,
        "operator": args.operator,
        "created_utc": args.created_utc,
        "mission_archive_path": str(mission_dir),
        "swarm_config_file": str(archived_swarm),
        "flight_profile_file": str(archived_profile),
        "code_root": str(code_root),
        "code_state": {
            "details_path": str(code_state_path),
            "captured_utc": code_state["timestamp_utc"],
            "hostname": code_state["hostname"],
            "username": code_state["username"],
            "git_repo_root": code_state["git_repo_root"],
            "git_branch": code_state["git_branch"],
            "git_commit_hash": code_state["git_commit_hash"],
            "has_uncommitted_changes": code_state[
                "has_uncommitted_changes"
            ],
            "script_path": code_state["script_path"],
            "script_sha256": code_state["script_sha256"],
        },
        "ground_orchestrator": {
            "started_utc": args.created_utc,
            "start_at_utc": None,
            "start_delay_s": args.start_delay_s,
            "duration_s": args.duration,
            "audio_enabled": args.enable_audio,
            "rosbag_enabled": args.enable_rosbag,
            "auto_ulog": not args.no_auto_ulog,
            "mirror_interval_s": args.mirror_interval_s,
            "safety": (
                "Recording and collection only. This orchestrator never arms "
                "or disarms a drone."
            ),
        },
        "drones": {
            drone["drone_id"]: {
                "drone_id": drone["drone_id"],
                "drone_session_id": drone["drone_session_id"],
                "ssh_host": drone["ssh_host"],
                "ip_address": drone["ip_address"],
                "remote_mission_folder": drone["remote_mission_dir"],
                "local_drone_dir": drone["local_drone_dir"],
                "drone_config_file": str(
                    archived_configs[drone["drone_id"]]
                ),
                "run_status": "pending_initialization",
                "warnings": [],
                "errors": [],
            }
            for drone in drones
        },
        "run_status": {
            "current_status": "initialized",
            "started": False,
            "completed": False,
            "warnings": list(code_state["capture_warnings"]),
            "errors": [],
        },
        "expected_outputs": {
            "per_drone_respeaker_audio": "pending",
            "per_drone_px4_ulog": "pending",
            "per_drone_ros2_bag": "optional",
        },
    }
    save_json_atomic(metadata_path, metadata)

    readme = f"""# HALO Swarm Mission Archive: {args.mission_id}

This archive has one common mission ID for the complete swarm mission:

    {args.mission_id}

Every drone has an isolated session ID and remote folder:

    <mission_id>__<drone_id>
    /home/root/halo_sync_test/<mission_id>__<drone_id>/

The ground orchestrator prepares and launches all reachable drones concurrently. It
passes the same absolute UTC start barrier to every drone agent. It records and
collects data only; it never arms or disarms a drone. Use the approved manual flight
procedure for arming and disarming.

Mission name: {args.mission_name}
Operator: {args.operator}
Created UTC: {args.created_utc}
Swarm inventory: config/swarm_drones.yaml
Profile: config/profiles/{profile_path.name}

Archive layout:

    metadata/
      mission_metadata.json
      ground_orchestrator_log.json
      code_state.json
      swarm_collection_manifest.json
    config/
      swarm_drones.yaml
      profiles/
      drones/
    drones/
      <drone_id>/
        drone_data/
          audio/<mission_id>__<drone_id>/
          px4_logs/
          ros_bags/
          status_logs/
          raw_sensor_data/
        metadata/
        processed/
        plots/
        reports/

A failure on one drone is recorded independently and does not stop other drones.
Collection may be rerun later for one drone with scripts/collect_drone_data.py and
that drone's exact drone ID, session ID, SSH alias, and local drone folder.
"""
    readme_path = mission_dir / "README.md"
    readme_path.write_text(readme, encoding="utf-8")

    collection_manifest = {
        "schema_version": 1,
        "mission_id": args.mission_id,
        "created_utc": args.created_utc,
        "updated_utc": args.created_utc,
        "collection_status": "pending",
        "drones": {
            drone["drone_id"]: {
                "drone_id": drone["drone_id"],
                "drone_session_id": drone["drone_session_id"],
                "ssh_host": drone["ssh_host"],
                "remote_drone_mission_folder": drone[
                    "remote_mission_dir"
                ],
                "local_drone_dir": drone["local_drone_dir"],
                "collection_status": "pending",
                "warnings": [],
                "errors": [],
            }
            for drone in drones
        },
        "collection_summary": {
            "drone_count": len(drones),
            "complete": 0,
            "complete_with_warnings": 0,
            "failed": 0,
            "pending": len(drones),
        },
    }
    save_json_atomic(collection_path, collection_manifest)
    return (
        mission_dir,
        metadata_path,
        log_path,
        collection_path,
        archived_configs,
    )


def update_metadata_from_states(
    metadata_path: Path,
    drones: list[dict[str, Any]],
    status: str,
    finished_utc: str | None = None,
) -> None:
    metadata = load_json(metadata_path)
    for drone in drones:
        record = metadata.setdefault("drones", {}).setdefault(
            drone["drone_id"], {}
        )
        record["run_status"] = drone.get("run_status")
        record["remote_agent_pid"] = drone.get("pid")
        record["agent_startup_confirmed"] = drone.get(
            "startup_confirmed", False
        )
        record["agent_finalized"] = drone.get("finalized", False)
        record["termination_reason"] = drone.get("termination_reason")
        record["warnings"] = list(drone.get("warnings", []))
        record["errors"] = list(drone.get("errors", []))
    run_status = metadata.setdefault("run_status", {})
    run_status["current_status"] = status
    run_status["started"] = any(
        drone.get("startup_confirmed") for drone in drones
    )
    run_status["completed"] = finished_utc is not None
    if finished_utc is not None:
        run_status["finished_utc"] = finished_utc
        metadata.setdefault("ground_orchestrator", {})[
            "finished_utc"
        ] = finished_utc
    save_json_atomic(metadata_path, metadata)


def collect_one_drone(
    args: argparse.Namespace,
    drone: dict[str, Any],
    collector_path: Path,
    mission_dir: Path,
) -> dict[str, Any]:
    termination_reason = (
        drone.get("termination_reason")
        or (
            drone.get("final_data", {}).get("termination_reason")
            if isinstance(drone.get("final_data"), dict)
            else None
        )
        or "ground_orchestrator_collection_after_unknown_agent_state"
    )
    command = [
        sys.executable,
        str(collector_path),
        "--mission-dir",
        str(mission_dir),
        "--drone-host",
        drone["ssh_host"],
        "--drone-id",
        drone["drone_id"],
        "--drone-session-id",
        drone["drone_session_id"],
        "--local-drone-dir",
        drone["local_drone_dir"],
        "--remote-sync-root",
        drone["remote_sync_root"],
        "--termination-reason",
        str(termination_reason),
        "--notes",
        "Automatic collection by run_halo_swarm_mission.py",
    ]
    if args.no_auto_ulog or not isinstance(drone.get("pid"), int):
        command.append("--no-auto-ulog")
    else:
        command.append("--auto-ulog")
    result = run_command(command, timeout=360.0)
    return {
        "ok": result["ok"],
        "collector_result": result,
        "manifest_path": str(
            Path(drone["local_drone_dir"])
            / "metadata"
            / f"{drone['drone_session_id']}_collection_manifest.json"
        ),
        "warnings": [],
        "errors": (
            []
            if result["ok"]
            else [
                "Collector failed: " + failure_detail(result)
            ]
        ),
    }


def finalize_swarm_manifest(
    path: Path,
    drones: list[dict[str, Any]],
    collection_results: dict[str, dict[str, Any]],
    interrupted: bool,
) -> dict[str, Any]:
    manifest = load_json(path)
    records = manifest.setdefault("drones", {})
    for drone in drones:
        drone_id = drone["drone_id"]
        result = collection_results.get(drone_id, {})
        record = records.setdefault(drone_id, {})
        record["orchestrator_collection_result"] = result.get(
            "collector_result"
        )
        manifest_path = Path(
            result.get(
                "manifest_path",
                Path(drone["local_drone_dir"])
                / "metadata"
                / f"{drone['drone_session_id']}_collection_manifest.json",
            )
        )
        if manifest_path.is_file():
            per_drone = load_json(manifest_path)
            record.update(
                {
                    "collection_status": per_drone.get(
                        "collection_status", "failed"
                    ),
                    "collection_manifest_path": str(manifest_path),
                    "respeaker_audio": per_drone.get(
                        "collected_files", {}
                    ).get("respeaker_audio"),
                    "ros2_bags": per_drone.get(
                        "collected_files", {}
                    ).get("ros2_bags"),
                    "px4_ulogs": per_drone.get(
                        "collected_files", {}
                    ).get("px4_ulogs", []),
                    "selected_auto_ulog": per_drone.get(
                        "ulog_discovery", {}
                    ).get("selected_auto_ulog"),
                    "warnings": per_drone.get("warnings", []),
                    "errors": per_drone.get("errors", []),
                }
            )
        elif not result.get("ok"):
            record["collection_status"] = "failed"
            record.setdefault("errors", []).extend(
                result.get("errors", ["Collector did not create a manifest"])
            )

    statuses = [
        record.get("collection_status")
        for record in records.values()
        if isinstance(record, dict)
    ]
    summary = {
        "drone_count": len(records),
        "complete": statuses.count("complete"),
        "complete_with_warnings": statuses.count(
            "complete_with_warnings"
        ),
        "failed": statuses.count("failed"),
        "pending": sum(
            value
            not in {"complete", "complete_with_warnings", "failed"}
            for value in statuses
        ),
    }
    manifest["collection_summary"] = summary
    if interrupted:
        manifest["collection_status"] = "interrupted_collection_finished"
    elif summary["failed"] or summary["pending"]:
        manifest["collection_status"] = "complete_with_drone_failures"
    elif summary["complete_with_warnings"]:
        manifest["collection_status"] = "complete_with_warnings"
    else:
        manifest["collection_status"] = "complete"
    manifest["updated_utc"] = utc_now()
    save_json_atomic(path, manifest)
    return manifest


def main() -> int:
    return _swarm_v2_main()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mission-name", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--swarm", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--code-root", required=True)
    parser.add_argument("--duration", required=True, type=float)
    parser.add_argument("--enable-audio", action="store_true")
    parser.add_argument("--enable-rosbag", action="store_true")
    ulog_group = parser.add_mutually_exclusive_group()
    ulog_group.add_argument("--auto-ulog", action="store_true")
    ulog_group.add_argument("--no-auto-ulog", action="store_true")
    parser.add_argument("--start-delay-s", default=15.0, type=float)
    parser.add_argument("--mirror-interval-s", default=0.0, type=float)
    parser.add_argument(
        "--archive-root",
        default=str(Path.home() / "MIC_ARRAY_ROS" / "HALO_ARCHIVE"),
    )
    parser.add_argument("--status-interval-s", default=2.0, type=float)
    parser.add_argument("--post-disarm-wait-s", default=10.0, type=float)
    args = parser.parse_args()

    if args.duration <= 0:
        parser.error("--duration must be greater than zero")
    if args.start_delay_s < 0:
        parser.error("--start-delay-s cannot be negative")
    if args.mirror_interval_s < 0:
        parser.error("--mirror-interval-s cannot be negative")
    if args.status_interval_s <= 0:
        parser.error("--status-interval-s must be greater than zero")
    if args.post_disarm_wait_s < 0:
        parser.error("--post-disarm-wait-s cannot be negative")

    code_root = Path(args.code_root).expanduser().resolve()
    swarm_path = Path(args.swarm).expanduser().resolve()
    profile_path = Path(args.profile).expanduser().resolve()
    if not code_root.is_dir():
        parser.error(f"--code-root is not a directory: {code_root}")
    if not swarm_path.is_file():
        parser.error(f"--swarm does not exist: {swarm_path}")
    if not profile_path.is_file():
        parser.error(f"--profile does not exist: {profile_path}")

    try:
        drones, _swarm_data = normalize_swarm(swarm_path, code_root)
        profile = load_yaml_subset(profile_path)
    except (FileNotFoundError, OSError, ValueError) as exc:
        parser.error(str(exc))

    rosbag_section = profile.get("rosbag", {})
    if not isinstance(rosbag_section, dict):
        parser.error(f"{profile_path}: rosbag must be a mapping")
    raw_topics = rosbag_section.get("topics", [])
    if not isinstance(raw_topics, list) or not all(
        isinstance(topic, str) and topic.startswith("/")
        for topic in raw_topics
    ):
        parser.error(f"{profile_path}: rosbag topics must be a list")
    args.rosbag_topics = list(raw_topics)

    created = datetime.now(timezone.utc)
    args.created_utc = created.isoformat()
    args.mission_id = (
        created.strftime("%Y%m%d_%H%M%S_UTC")
        + "_"
        + sanitize_id_component(args.mission_name)
    )
    for drone in drones:
        session_id = args.mission_id + "__" + drone["drone_id"]
        drone["drone_session_id"] = session_id
        drone["remote_mission_dir"] = remote_session_path(
            drone["remote_sync_root"], session_id
        )
        drone["warnings"] = []
        drone["errors"] = []
        drone["run_status"] = "pending_initialization"
        drone["startup_confirmed"] = False
        drone["finalized"] = False
        drone["termination_reason"] = None
        drone["consecutive_poll_failures"] = 0

    script_path = Path(__file__).resolve()
    collector_path = script_path.with_name("collect_drone_data.py")
    agent_path = script_path.with_name("halo_drone_mission_agent.py")
    try:
        (
            mission_dir,
            metadata_path,
            log_path,
            collection_path,
            archived_configs,
        ) = create_archive(
            args,
            drones,
            swarm_path,
            profile_path,
            script_path,
        )
    except (FileExistsError, OSError, ValueError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")

    log: dict[str, Any] = {
        "mission_id": args.mission_id,
        "mission_name": args.mission_name,
        "operator": args.operator,
        "mission_dir": str(mission_dir),
        "started_utc": args.created_utc,
        "start_at_utc": None,
        "start_delay_s": args.start_delay_s,
        "duration_s": args.duration,
        "audio_enabled": args.enable_audio,
        "rosbag_enabled": args.enable_rosbag,
        "rosbag_topics": list(args.rosbag_topics),
        "auto_ulog": not args.no_auto_ulog,
        "mirror_interval_s": args.mirror_interval_s,
        "safety": (
            "This script never arms or disarms a drone. Manual flight "
            "procedures remain mandatory."
        ),
        "events": [],
        "warnings": [],
        "errors": [],
        "drones": {},
    }
    log_event(log, "swarm_archive_created")
    sync_log(log_path, log)

    print(f"Common mission ID: {args.mission_id}")
    print(f"Ground swarm archive: {mission_dir}")
    print(
        "Preparing all drones concurrently. This tool never arms or "
        "disarms a drone."
    )

    interrupted = False
    orchestration_exception: str | None = None
    try:
        initialization_results = run_parallel(
            drones,
            lambda drone: initialize_drone(
                drone,
                mission_dir / "README.md",
                metadata_path,
                profile_path,
                archived_configs,
                agent_path,
            ),
        )
        ready: list[dict[str, Any]] = []
        for drone in drones:
            drone_id = drone["drone_id"]
            result = initialization_results.get(drone_id, {})
            drone["initialization"] = result
            log["drones"].setdefault(drone_id, {})[
                "initialization"
            ] = result
            if result.get("ok"):
                drone["remote_agent"] = result["remote_agent"]
                drone["run_status"] = "ready_at_start_barrier"
                ready.append(drone)
                print(f"{drone_id}: prepared")
            else:
                drone["run_status"] = "initialization_failed"
                drone["termination_reason"] = "initialization_failed"
                drone["errors"].extend(result.get("errors", []))
                print(
                    f"{drone_id}: preparation failed; other drones continue",
                    file=sys.stderr,
                )
            log_event(
                log,
                "drone_initialization_finished",
                drone_id,
                ok=bool(result.get("ok")),
            )
        update_metadata_from_states(
            metadata_path, drones, "swarm_preparation_complete"
        )
        sync_log(log_path, log)

        start_at = datetime.now(timezone.utc) + timedelta(
            seconds=args.start_delay_s
        )
        start_at_utc = start_at.isoformat()
        args.start_at_utc = start_at_utc
        log["start_at_utc"] = start_at_utc
        metadata = load_json(metadata_path)
        metadata["start_at_utc"] = start_at_utc
        metadata.setdefault("ground_orchestrator", {})[
            "start_at_utc"
        ] = start_at_utc
        save_json_atomic(metadata_path, metadata)
        log_event(log, "start_barrier_set", start_at_utc=start_at_utc)
        sync_log(log_path, log)

        print(f"Common capture start barrier (UTC): {start_at_utc}")
        print("Launching every prepared drone agent concurrently ...")
        launch_results = run_parallel(
            ready,
            lambda drone: launch_drone(
                args, drone, start_at_utc, metadata_path
            ),
        )
        for drone in ready:
            drone_id = drone["drone_id"]
            result = launch_results.get(drone_id, {})
            drone["launch"] = result
            log["drones"].setdefault(drone_id, {})["launch"] = result
            if result.get("ok"):
                drone["pid"] = result["pid"]
                drone["launched_monotonic"] = time.monotonic()
                drone["run_status"] = "agent_starting"
                print(
                    f"{drone_id}: agent PID {drone['pid']} launched; "
                    "waiting at common barrier"
                )
            else:
                drone["run_status"] = "launch_failed"
                drone["termination_reason"] = "drone_agent_launch_failed"
                drone["errors"].extend(result.get("errors", []))
                print(
                    f"{drone_id}: launch failed; other drones continue",
                    file=sys.stderr,
                )
            log_event(
                log,
                "drone_launch_finished",
                drone_id,
                ok=bool(result.get("ok")),
                pid=result.get("pid"),
            )

        update_metadata_from_states(
            metadata_path, drones, "swarm_agents_launched"
        )
        sync_log(log_path, log)

        active = [
            drone for drone in drones if isinstance(drone.get("pid"), int)
        ]
        if active:
            print(
                "Agents launched. Arm/disarm manually only under the "
                "approved flight procedure."
            )
        deadline = (
            time.monotonic()
            + max(0.0, start_at.timestamp() - time.time())
            + args.duration
            + args.post_disarm_wait_s
            + 90.0
        )
        next_mirror = (
            time.monotonic() + args.mirror_interval_s
            if args.mirror_interval_s > 0
            else None
        )

        while any(not drone.get("finalized") for drone in active):
            polling = [
                drone for drone in active if not drone.get("finalized")
            ]
            snapshots = run_parallel(polling, pull_agent_snapshot)
            for drone in polling:
                drone_id = drone["drone_id"]
                snapshot = snapshots.get(drone_id, {})
                if not snapshot.get("ok"):
                    drone["consecutive_poll_failures"] += 1
                    add_unique(
                        drone["warnings"],
                        "Status poll failed: "
                        + str(snapshot.get("error", "unknown error")),
                    )
                    if drone["consecutive_poll_failures"] >= 3:
                        drone["finalized"] = True
                        drone["run_status"] = "status_connection_lost"
                        drone["termination_reason"] = (
                            "ssh_status_connection_lost"
                        )
                        add_unique(
                            drone["warnings"],
                            "Three consecutive status polls failed; "
                            "collection will still be attempted.",
                        )
                    continue

                drone["consecutive_poll_failures"] = 0
                persist_snapshot(drone, snapshot)
                data = snapshot.get("data")
                if isinstance(data, dict):
                    if snapshot.get("kind") in {
                        "start",
                        "status",
                        "final",
                    }:
                        drone["startup_confirmed"] = True
                    for warning in data.get("warnings", []):
                        add_unique(drone["warnings"], str(warning))
                    for error in data.get("errors", []):
                        add_unique(drone["errors"], str(error))
                    state_signature = (
                        data.get("phase"),
                        data.get("audio_process_running"),
                        data.get("rosbag_process_running"),
                        data.get("current_detected_flight_state"),
                    )
                    if state_signature != drone.get(
                        "last_reported_state"
                    ):
                        print(
                            f"{drone_id}: phase={state_signature[0]}, "
                            f"audio={state_signature[1]}, "
                            f"rosbag={state_signature[2]}, "
                            f"flight={state_signature[3]}"
                        )
                        drone["last_reported_state"] = state_signature
                if snapshot.get("kind") == "final":
                    drone["finalized"] = True
                    drone["final_data"] = data
                    drone["termination_reason"] = (
                        data.get("termination_reason")
                        if isinstance(data, dict)
                        else "agent_finalized"
                    )
                    drone["run_status"] = "agent_finalized"
                    log_event(
                        log,
                        "drone_agent_finalized",
                        drone_id,
                        termination_reason=drone[
                            "termination_reason"
                        ],
                    )
                elif snapshot.get("agent_alive") is False:
                    drone["finalized"] = True
                    drone["confirmed_agent_exit"] = True
                    drone["termination_reason"] = (
                        "agent_exited_without_final_status"
                    )
                    drone["run_status"] = (
                        "agent_exited_without_final_status"
                    )
                    add_unique(
                        drone["warnings"],
                        "Agent exited without a readable final status.",
                    )

            if (
                next_mirror is not None
                and time.monotonic() >= next_mirror
            ):
                mirror_targets = [
                    drone
                    for drone in active
                    if drone.get("startup_confirmed")
                ]
                mirror_results = run_parallel(
                    mirror_targets, mirror_drone
                )
                for drone in mirror_targets:
                    result = mirror_results.get(drone["drone_id"], {})
                    log_event(
                        log,
                        "periodic_mirror",
                        drone["drone_id"],
                        result=result,
                    )
                    if not result.get("ok"):
                        add_unique(
                            drone["warnings"],
                            "Periodic mirror failed: "
                            + failure_detail(result),
                        )
                next_mirror = (
                    time.monotonic() + args.mirror_interval_s
                )

            update_metadata_from_states(
                metadata_path, drones, "swarm_mission_running"
            )
            sync_log(log_path, log)
            if time.monotonic() >= deadline:
                add_unique(
                    log["warnings"],
                    "Ground wait deadline expired; active agents were "
                    "asked to finalize before collection.",
                )
                stop_results = stop_active_agents(active)
                for drone_id, result in stop_results.items():
                    log_event(
                        log,
                        "ground_timeout_agent_stop",
                        drone_id,
                        result=result,
                    )
                wait_for_final_snapshots(active, 15.0)
                for drone in active:
                    if not drone.get("finalized"):
                        drone["termination_reason"] = (
                            "ground_orchestrator_timeout"
                        )
                        drone["run_status"] = "ground_timeout"
                        drone["finalized"] = True
                break
            time.sleep(args.status_interval_s)

    except KeyboardInterrupt:
        interrupted = True
        add_unique(
            log["warnings"],
            "Ground orchestrator interrupted; best-effort agent stop "
            "and collection were requested for every drone.",
        )
        print(
            "\nInterrupt received. Asking reachable agents to finalize, "
            "then collecting every drone ..."
        )
    except Exception as exc:
        orchestration_exception = f"{type(exc).__name__}: {exc}"
        add_unique(
            log["errors"],
            "Unhandled ground orchestration error: "
            + orchestration_exception,
        )
        print(
            "Ground orchestration error; best-effort collection will "
            "still run: " + orchestration_exception,
            file=sys.stderr,
        )

    if interrupted or orchestration_exception is not None:
        stop_results = stop_active_agents(drones)
        for drone_id, result in stop_results.items():
            log_event(
                log,
                "best_effort_agent_stop",
                drone_id,
                result=result,
            )
        wait_for_final_snapshots(
            drones, args.post_disarm_wait_s + 20.0
        )
        for drone in drones:
            if (
                isinstance(drone.get("pid"), int)
                and not drone.get("termination_reason")
            ):
                drone["termination_reason"] = (
                    "ground_orchestrator_interrupted"
                    if interrupted
                    else "ground_orchestrator_error"
                )

    print("Starting best-effort parallel collection from all drones ...")
    update_metadata_from_states(
        metadata_path, drones, "swarm_collection_running"
    )
    sync_log(log_path, log)
    collection_results = run_parallel(
        drones,
        lambda drone: collect_one_drone(
            args, drone, collector_path, mission_dir
        ),
    )
    for drone in drones:
        drone_id = drone["drone_id"]
        result = collection_results.get(drone_id, {})
        log["drones"].setdefault(drone_id, {})[
            "collection"
        ] = result
        if result.get("ok"):
            print(f"{drone_id}: collection finished")
        else:
            add_unique(
                drone["errors"],
                "Automatic collection failed: "
                + (
                    failure_detail(result["collector_result"])
                    if isinstance(
                        result.get("collector_result"), dict
                    )
                    else "unknown collector error"
                ),
            )
            print(
                f"{drone_id}: collection failed; it can be rerun later",
                file=sys.stderr,
            )
        log_event(
            log,
            "drone_collection_finished",
            drone_id,
            ok=bool(result.get("ok")),
        )

    final_manifest = finalize_swarm_manifest(
        collection_path, drones, collection_results, interrupted
    )
    summary = final_manifest["collection_summary"]
    failed_drones = [
        drone
        for drone in drones
        if final_manifest.get("drones", {})
        .get(drone["drone_id"], {})
        .get("collection_status")
        == "failed"
        or drone.get("errors")
    ]
    finished_utc = utc_now()
    final_status = (
        "swarm_collection_complete_after_interrupt"
        if interrupted
        else (
            "swarm_collection_complete"
            if not failed_drones and orchestration_exception is None
            else "swarm_collection_complete_with_drone_failures"
        )
    )
    update_metadata_from_states(
        metadata_path, drones, final_status, finished_utc
    )
    log["finished_utc"] = finished_utc
    log["final_status"] = final_status
    log["collection_summary"] = summary
    sync_log(log_path, log)

    print(f"Ground mission folder: {mission_dir}")
    print(f"Ground orchestrator log: {log_path}")
    print(f"Swarm collection manifest: {collection_path}")
    print(
        "Collection summary: "
        f"{summary['complete']} complete, "
        f"{summary['complete_with_warnings']} with warnings, "
        f"{summary['failed']} failed, "
        f"{summary['pending']} pending"
    )
    if failed_drones:
        print(
            "One or more drone sessions need review or later collection; "
            "successful drone archives were retained."
        )
    if interrupted:
        return 130
    return 1 if failed_drones or orchestration_exception else 0



# Passive ground-side five-worker implementation.
_SW_JAZZY="/opt/ros/jazzy/setup.bash"
_SW_FOXY="/opt/ros/foxy/setup.bash"
_SW_PX4=str(Path.home()/"MIC_ARRAY_ROS"/"px4_ros2_jazzy_ws"/"install"/"setup.bash")
_SW_STATUS="/fmu/out/vehicle_status"
_SW_LAND="/fmu/out/vehicle_land_detected"
_SW_SIGNAL=None


def _sw_epoch(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _sw_run(cmd, timeout=20, env=None):
    try:
        r=subprocess.run(cmd,check=False,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout,env=env)
        return {"command":shlex.join(cmd),"return_code":r.returncode,"stdout":r.stdout.strip(),"stderr":r.stderr.strip(),"ok":r.returncode==0}
    except subprocess.TimeoutExpired as exc:
        return {"command":shlex.join(cmd),"return_code":124,"stdout":str(exc.stdout or "").strip(),"stderr":str(exc.stderr or "").strip(),"ok":False}
    except OSError as exc:
        return {"command":shlex.join(cmd),"return_code":127,"stdout":"","stderr":f"{type(exc).__name__}: {exc}","ok":False}


def _sw_ssh(host,command):
    return ["ssh","-o","BatchMode=yes","-o","ConnectTimeout=10",host,"LC_ALL=C LANG=C bash -lc "+shlex.quote(command)]


def _sw_stop(process,warnings):
    if process is None:
        return None
    if process.poll() is None:
        try:
            os.killpg(os.getpgid(process.pid),signal.SIGINT)
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            warnings.append("Process did not exit after SIGINT; sent SIGTERM")
            try:
                os.killpg(os.getpgid(process.pid),signal.SIGTERM)
                process.wait(timeout=10)
            except (OSError,subprocess.TimeoutExpired) as exc:
                warnings.append(f"Process force-stop failed: {type(exc).__name__}: {exc}")
        except (OSError,ProcessLookupError) as exc:
            warnings.append(f"Process SIGINT failed: {type(exc).__name__}: {exc}")
    return process.returncode


def _sw_topics(output):
    return sorted({line.strip().split(None,1)[0] for line in output.splitlines() if line.strip().startswith("/")})


def _sw_types(output):
    result={}
    for line in output.splitlines():
        match=re.match(r"^\s*(/\S+)\s+\[(.+)\]\s*$",line)
        if match: result[match.group(1)]=match.group(2)
    return result


def _sw_fields(output):
    result={}
    for line in output.splitlines():
        match=re.match(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$",line)
        if match:
            value=match.group(2).strip()
            if value.lower() in {"true","false"}: value=value.lower()=="true"
            elif re.fullmatch(r"[-+]?\d+",value): value=int(value)
            result[match.group(1)]=value
    return result


def _sw_normalize(path,root):
    data=load_yaml_subset(path); raw=data.get("drones")
    if not isinstance(raw,list) or not raw: raise ValueError(f"{path}: drones must be a non-empty list")
    result=[]; seen=set()
    for n,item in enumerate(raw,1):
        if not isinstance(item,dict): raise ValueError(f"{path}: drone item {n} must be a mapping")
        did,host=item.get("drone_id"),item.get("ssh_host")
        if not isinstance(did,str) or re.fullmatch(r"[A-Za-z0-9._-]+",did) is None: raise ValueError(f"{path}: invalid drone_id")
        if not isinstance(host,str) or re.fullmatch(r"[A-Za-z0-9._-]+",host) is None: raise ValueError(f"{did}: invalid ssh_host")
        try: ip=str(ipaddress.ip_address(str(item.get("ip_address"))))
        except ValueError as exc: raise ValueError(f"{path}: invalid IP for {did}") from exc
        if any(x in seen for x in (did,host,ip)): raise ValueError(f"{path}: duplicate drone identity")
        seen.update((did,host,ip))
        ref=item.get("config"); candidates=[Path(ref).expanduser(),root/ref,path.parent/ref]
        cfg=next((x.resolve() for x in candidates if isinstance(ref,str) and x.is_file()),None)
        if cfg is None: raise FileNotFoundError(f"Could not resolve config for {did}: {ref}")
        config=load_yaml_subset(cfg); errors=[]
        if config.get("drone_id")!=did: errors.append(f"{cfg}: drone_id does not match {did}")
        if str(config.get("ip_address"))!=ip: errors.append(f"{cfg}: ip_address does not match {ip}")
        if config.get("ssh_user")!="root": errors.append(f"{cfg}: ssh_user must be root")
        enabled=item.get("enabled"); required=item.get("required")
        if not isinstance(enabled,bool): errors.append(f"{did}: enabled must be true or false"); enabled=True
        if not isinstance(required,bool): errors.append(f"{did}: required must be true or false"); required=True
        domain=item.get("ros_domain_id"); cfg_domain=config.get("ros_domain_id")
        for value,label in ((domain,f"{did} swarm entry"),(cfg_domain,f"{did} YAML")):
            if value is None: errors.append(f"{label}: ros_domain_id is missing; refusing to guess it")
            elif isinstance(value,bool) or not isinstance(value,int) or not 1<=value<=7: errors.append(f"{label}: ros_domain_id must be in 1-7")
        if domain is not None and cfg_domain is not None and domain!=cfg_domain: errors.append(f"{cfg}: ros_domain_id does not match swarm_lab.yaml")
        post=item.get("post_landing_record_s",3)
        if isinstance(post,bool) or not isinstance(post,(int,float)) or not 2<=float(post)<=5: errors.append(f"{did}: post_landing_record_s must be between 2 and 5 seconds"); post=3
        paths=config.get("paths",{}) if isinstance(config.get("paths"),dict) else {}
        remote=paths.get("drone_sync_folder","/home/root/halo_sync_test"); logs=paths.get("px4_log_folder","/data/px4/log")
        audio=config.get("audio",{}) if isinstance(config.get("audio"),dict) else {}
        legacy_audio=config.get("sensors",{}).get("respeaker",{}) if isinstance(config.get("sensors"),dict) else {}
        legacy_audio=legacy_audio if isinstance(legacy_audio,dict) else {}
        audio_device=str(audio.get("audio_device",legacy_audio.get("alsa_device","hw:0,0")))
        try: sample_rate=int(audio.get("sample_rate_hz",legacy_audio.get("sample_rate_hz",16000)))
        except (TypeError,ValueError): sample_rate=16000; errors.append(f"{did}: invalid audio sample rate; using 16000")
        try: channels=int(audio.get("channels",legacy_audio.get("channels",6)))
        except (TypeError,ValueError): channels=6; errors.append(f"{did}: invalid audio channel count; using 6")
        sample_format=str(audio.get("sample_format",legacy_audio.get("sample_format","S16_LE")))
        if sample_rate <= 0 or channels <= 0:
            errors.append(f"{did}: audio sample rate and channels must be positive")
            sample_rate,channels=16000,6
        result.append({"drone_id":did,"ssh_host":host,"ip_address":ip,"ros_domain_id":cfg_domain if isinstance(cfg_domain,int) else domain,"enabled":enabled,"required":required,"post_landing_record_s":float(post),"config_path":str(cfg),"config_errors":errors,"remote_root":remote.rstrip("/"),"px4_root":logs.rstrip("/"),"audio_device":audio_device,"sample_rate":sample_rate,"channels":channels,"sample_format":sample_format})
    domains={}
    for drone in result:
        domain=drone["ros_domain_id"]
        if isinstance(domain,int):
            if domain in domains:
                message=f"ros_domain_id {domain} is duplicated by {domains[domain]} and {drone['drone_id']}; domain isolation requires unique values"
                drone["config_errors"].append(message)
                next(item for item in result if item["drone_id"]==domains[domain])["config_errors"].append(message)
            else:
                domains[domain]=drone["drone_id"]
    return result


def _sw_preflight(drone):
    result={"drone_id":drone["drone_id"],"ip_address":drone["ip_address"],"ros_domain_id":drone["ros_domain_id"],"static_peer":drone["ip_address"],"state":"CHECKING_DRONE_ROS","ok":False,"errors":list(drone["config_errors"]),"warnings":[]}
    if result["errors"] or not isinstance(drone["ros_domain_id"],int): result["state"]="FAILED_PREFLIGHT"; return result
    shell=(f"set -u\nsource {_SW_FOXY}\nexport ROS_DOMAIN_ID={drone['ros_domain_id']}\nexport ROS_LOCALHOST_ONLY=0\nexport ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET\nexport ROS_STATIC_PEERS={drone['ip_address']}\nexport RMW_IMPLEMENTATION=rmw_fastrtps_cpp\nros2 daemon stop >/dev/null 2>&1 || true\nros2 daemon start >/dev/null 2>&1 || true\nsleep 1\necho __T__\nros2 topic list -t\necho __E__\necho __Y__\nros2 topic type /fmu/out/vehicle_status\necho __Z__\necho __S__\nros2 topic type /fmu/out/sensor_combined\necho __Q__")
    run=_sw_run(_sw_ssh(drone["ssh_host"],shell),35); result["command_result"]=run
    if not run["ok"]: result["errors"].append("Drone ROS preflight failed: "+failure_detail(run)); result["state"]="FAILED_PREFLIGHT"; return result
    out=run["stdout"]; block=out.split("__T__",1); block=block[1].split("__E__",1)[0] if len(block)==2 else ""; typ=out.split("__Y__",1); typ=typ[1].split("__Z__",1)[0].strip() if len(typ)==2 else ""; sensor_typ=out.split("__S__",1); sensor_typ=sensor_typ[1].split("__Q__",1)[0].strip() if len(sensor_typ)==2 else ""
    result["visible_topics"]=_sw_topics(block); result["vehicle_status_type"]=typ; result["sensor_combined_type"]=sensor_typ; result["px4_msgs_decode_ok"]="px4_msgs" in typ and "px4_msgs" in sensor_typ
    if _SW_STATUS not in result["visible_topics"]: result["errors"].append(f"Drone ROS preflight did not find {_SW_STATUS}")
    if "/fmu/out/sensor_combined" not in result["visible_topics"]: result["errors"].append("Drone ROS preflight did not find /fmu/out/sensor_combined")
    if not any(x.startswith("/fmu") for x in result["visible_topics"]): result["errors"].append("Drone ROS preflight found no /fmu topics")
    if "px4_msgs" not in typ or "px4_msgs" not in sensor_typ: result["errors"].append("Drone ROS preflight could not confirm px4_msgs decoding for vehicle_status and sensor_combined")
    result["ok"]=not result["errors"]; result["state"]="DRONE_ROS_OK" if result["ok"] else "FAILED_PREFLIGHT"; return result


def _sw_echo(topic,timeout):
    run=_sw_run(["ros2","topic","echo",topic,"--once","--qos-reliability","best_effort"],timeout,os.environ.copy())
    if not run["ok"]: return {"available":False,"armed":None,"landed":None,"error":failure_detail(run)}
    fields=_sw_fields(run["stdout"])
    if topic==_SW_STATUS:
        armed=fields.get("armed") if isinstance(fields.get("armed"),bool) else fields.get("arming_state")==2 if isinstance(fields.get("arming_state"),int) else None
        return {"available":True,"armed":armed,"state":"armed" if armed is True else "disarmed" if armed is False else "unknown","timestamp":fields.get("timestamp")}
    return {"available":True,"landed":fields.get("landed") if isinstance(fields.get("landed"),bool) else None,"timestamp":fields.get("timestamp")}


def _sw_audio_start(args,state,warnings):
    if not args.enable_audio:
        return
    remote_audio=f"{args.remote_mission_dir}/audio"
    remote_wav=f"{remote_audio}/respeaker_6ch.wav"
    remote_log=f"{args.remote_mission_dir}/status_logs/arecord.log"
    command=(
        f"mkdir -p {shlex.quote(remote_audio)} {shlex.quote(args.remote_mission_dir + '/status_logs')} || exit 1; "
        f"command -v arecord >/dev/null 2>&1 || {{ echo arecord unavailable >&2; exit 127; }}; "
        f"test ! -e {shlex.quote(remote_wav)} || {{ echo audio output already exists >&2; exit 73; }}; "
        f"nohup arecord -D {shlex.quote(args.audio_device)} -f {shlex.quote(args.sample_format)} "
        f"-r {int(args.sample_rate)} -c {int(args.channels)} -t wav {shlex.quote(remote_wav)} "
        f"> {shlex.quote(remote_log)} 2>&1 < /dev/null & printf '%s\n' $!"
    )
    run=_sw_run(_sw_ssh(args.ssh_host,command),25)
    state["audio_start_result"]=run
    if run["ok"]:
        match=re.search(r"(\d+)\s*$",run.get("stdout", ""))
        state["audio_pid"]=int(match.group(1)) if match else None
        state["audio_started_utc"]=utc_now()
        if state["audio_pid"] is None:
            warnings.append("arecord started but its remote PID was not returned")
    else:
        warnings.append("ReSpeaker audio start failed: "+failure_detail(run))


def _sw_audio_stop(args,state,warnings):
    if not args.enable_audio:
        return
    pid=state.get("audio_pid")
    if not pid:
        return
    command=f"if kill -0 {int(pid)} >/dev/null 2>&1; then kill -INT {int(pid)}; sleep 2; fi"
    run=_sw_run(_sw_ssh(args.ssh_host,command),20)
    state["audio_stop_result"]=run
    state["audio_stopped_utc"]=utc_now()
    if not run["ok"]:
        warnings.append("ReSpeaker audio stop failed: "+failure_detail(run))


def _sw_sync_audio(args,root):
    destination=root/"audio"
    destination.mkdir(parents=True,exist_ok=True)
    return _sw_run(["rsync","-av","-e","ssh -o BatchMode=yes -o ConnectTimeout=10",f"{args.ssh_host}:{args.remote_mission_dir}/audio/",str(destination)+"/"],90)


def _sw_worker(args):
    global _SW_SIGNAL
    signal.signal(signal.SIGINT,lambda s,f: globals().__setitem__("_SW_SIGNAL",s)); signal.signal(signal.SIGTERM,lambda s,f: globals().__setitem__("_SW_SIGNAL",s))
    root=Path(args.drone_dir); meta=root/"metadata"; status_dir=root/"status_logs"; meta.mkdir(parents=True,exist_ok=True); status_dir.mkdir(parents=True,exist_ok=True)
    status_path=Path(getattr(args,"status_path",status_dir/"worker_status.json")); final_path=meta/"worker_final.json"; bag=root/"ros_bags"/f"{args.mission_id}__{args.drone_id}_ground_rosbag"
    state={"bag_path":str(bag),"bag_process":None,"bag_prepared":False,"bag_started":False,"audio_pid":None,"audio_started_utc":None,"audio_stopped_utc":None,"connection_loss_events":[]}; warnings=[]; errors=[]
    save_json_atomic(meta/"worker_start.json",{"mission_id":args.mission_id,"drone_id":args.drone_id,"ip_address":args.ip_address,"ros_domain_id":args.ros_domain_id,"static_peer":args.ip_address,"worker_pid":os.getpid(),"safety":"Passive observer only; no flight-control commands.","environment":{k:os.environ.get(k) for k in ("ROS_DOMAIN_ID","ROS_LOCALHOST_ONLY","ROS_AUTOMATIC_DISCOVERY_RANGE","ROS_STATIC_PEERS","RMW_IMPLEMENTATION")}})
    save_json_atomic(status_path,{"mission_id":args.mission_id,"drone_id":args.drone_id,"ip_address":args.ip_address,"ros_domain_id":args.ros_domain_id,"static_peer":args.ip_address,"state":"CHECKING_GROUND_ROS","phase":"CHECKING_GROUND_ROS"})
    _sw_run(["ros2","daemon","stop"],env=os.environ.copy()); _sw_run(["ros2","daemon","start"],env=os.environ.copy())
    topics_run=_sw_run(["ros2","topic","list","-t"],20,os.environ.copy()); topics=_sw_topics(topics_run.get("stdout","")); types=_sw_types(topics_run.get("stdout","")); status_type=types.get(_SW_STATUS,"") or _sw_run(["ros2","topic","type",_SW_STATUS],env=os.environ.copy()).get("stdout",""); sensor_type=types.get("/fmu/out/sensor_combined","") or _sw_run(["ros2","topic","type","/fmu/out/sensor_combined"],env=os.environ.copy()).get("stdout",""); vehicle=_sw_echo(_SW_STATUS,8); land_available=_SW_LAND in topics and "px4_msgs" in types.get(_SW_LAND,"")
    pre_errors=[]; pre_warnings=[]
    if not topics_run["ok"]: pre_errors.append("Ground ROS discovery failed: "+failure_detail(topics_run))
    if _SW_STATUS not in topics: pre_errors.append(f"Ground ROS preflight did not find {_SW_STATUS}")
    if "/fmu/out/sensor_combined" not in topics: pre_errors.append("Ground ROS preflight did not find /fmu/out/sensor_combined")
    if "px4_msgs" not in status_type: pre_errors.append("Ground ROS preflight could not confirm px4_msgs vehicle_status decoding")
    if "px4_msgs" not in sensor_type: pre_errors.append("Ground ROS preflight could not confirm px4_msgs sensor_combined decoding")
    if not vehicle.get("available"): pre_errors.append("Ground ROS vehicle_status message check failed: "+str(vehicle.get("error")))
    if _SW_LAND in topics and not land_available: pre_warnings.append("vehicle_land_detected unavailable; using ARMED-to-DISARMED fallback")
    state["bag_prepared"]=not pre_errors; state["land_available"]=land_available
    ground={"visible_topics":topics,"topic_types":types,"vehicle_status_type":status_type,"sensor_combined_type":sensor_type,"vehicle_status_echo":vehicle,"vehicle_land_detected_available":land_available,"drone_ros_ok":getattr(args,"drone_ros_ok",None),"ground_ros_ok":not pre_errors,"px4_msgs_decode_ok":"px4_msgs" in status_type and "px4_msgs" in sensor_type,"warnings":pre_warnings,"errors":pre_errors}
    if pre_errors:
        final={"mission_id":args.mission_id,"drone_id":args.drone_id,"ip_address":args.ip_address,"ros_domain_id":args.ros_domain_id,"static_peer":args.ip_address,"state":"FAILED_PREFLIGHT","phase":"FAILED_PREFLIGHT","ground_preflight":ground,"warnings":pre_warnings,"errors":pre_errors,"termination_reason":"ground_ros_preflight_failed"}
        save_json_atomic(final_path,final); save_json_atomic(status_path,final); return 2
    ready_utc=utc_now(); save_json_atomic(status_path,{"mission_id":args.mission_id,"drone_id":args.drone_id,"ip_address":args.ip_address,"ros_domain_id":args.ros_domain_id,"static_peer":args.ip_address,"state":"READY","phase":"READY","drone_ros_ok":getattr(args,"drone_ros_ok",None),"ground_ros_ok":True,"px4_msgs_decode_ok":"px4_msgs" in status_type and "px4_msgs" in sensor_type,"visible_topics":topics,"ready_utc":ready_utc,"bag":{"prepared":True,"path":str(bag)},"audio":{"enabled":args.enable_audio,"started":False}})
    _sw_audio_start(args,state,warnings)
    previous=None; armed_seen=False; landed_seen=False; landed={"landed":None}; phase="WAITING_FOR_ARM"; stop_at=None; started=time.monotonic(); status_failures=0; land_failures=0; termination=None; arm_utc=None; arm_px4_ts=None; land_utc=None; land_px4_ts=None; disarm_utc=None; disarm_px4_ts=None; bag_start=None; bag_stop=None; bag_trigger=None; bag_code=None; bag_handle=None; bag_proc=None
    try:
        while termination is None:
            now=time.monotonic()
            if _SW_SIGNAL is not None: termination="worker_interrupted"; break
            if now-started>=args.duration: termination="duration_expired_after_recording" if armed_seen else "duration_expired_no_arm_detected"; break
            vehicle=_sw_echo(_SW_STATUS,8); current=vehicle.get("armed") if vehicle.get("available") else None
            if current is None:
                status_failures+=1
                if status_failures==1 or status_failures%3==0: warnings.append("Vehicle status communication unavailable"); state["connection_loss_events"].append({"detected_utc":utc_now(),"phase":phase})
            else: status_failures=0
            if state["land_available"] and (armed_seen or current is True):
                landed=_sw_echo(_SW_LAND,6)
                if landed.get("available"): land_failures=0
                else:
                    land_failures+=1
                    if land_failures>=3: state["land_available"]=False; warnings.append("vehicle_land_detected became unavailable; using disarmed fallback")
            if current is True and previous is not True:
                armed_seen=True; arm_utc=utc_now(); arm_px4_ts=vehicle.get("timestamp"); phase="RECORDING"
                if args.enable_ground_rosbag:
                    bag_handle=(status_dir/"ground_rosbag_console.log").open("a",encoding="utf-8"); bag_proc=subprocess.Popen(["ros2","bag","record","-a","-o",str(bag)],stdout=bag_handle,stderr=subprocess.STDOUT,env=os.environ.copy(),start_new_session=True,text=True); bag_start=utc_now()
            if current is False and previous is True and not landed_seen:
                disarm_utc=utc_now(); disarm_px4_ts=vehicle.get("timestamp"); bag_trigger="disarmed_fallback"; termination="armed_to_disarmed_fallback"; phase="STOPPING_BAG"
            if state["land_available"] and landed.get("landed") is True and armed_seen and not landed_seen:
                landed_seen=True; land_utc=utc_now(); land_px4_ts=landed.get("timestamp"); stop_at=time.monotonic()+args.post_landing_record_s; phase="LANDED_POSTROLL"
            if stop_at is not None and time.monotonic()>=stop_at:
                bag_trigger="landed"; termination="landed_postroll_complete"; phase="STOPPING_BAG"
            vehicle_state="connection_lost" if current is None else vehicle.get("state","unknown")
            status={"mission_id":args.mission_id,"drone_id":args.drone_id,"ip_address":args.ip_address,"ros_domain_id":args.ros_domain_id,"static_peer":args.ip_address,"state":phase,"phase":phase,"vehicle_state":vehicle_state,"connection_lost":current is None,"armed":vehicle.get("armed"),"landed":landed.get("landed"),"arm_detected_utc":arm_utc,"arm_detected_px4_timestamp":arm_px4_ts,"land_detected_utc":land_utc,"land_detected_px4_timestamp":land_px4_ts,"disarm_detected_utc":disarm_utc,"disarm_detected_px4_timestamp":disarm_px4_ts,"post_landing_record_s":args.post_landing_record_s,"post_landing_remaining_s":max(0,stop_at-time.monotonic()) if stop_at else None,"bag":{"enabled":args.enable_ground_rosbag,"prepared":state["bag_prepared"],"started":bag_proc is not None,"active":bag_proc is not None and bag_proc.poll() is None,"path":str(bag),"start_utc":bag_start,"size_bytes":sum(x.stat().st_size for x in bag.rglob("*") if x.is_file()) if bag.exists() else 0},"warnings":warnings,"errors":errors,"connection_loss_events":state["connection_loss_events"]}
            save_json_atomic(status_path,status); previous=current if current is not None else previous; time.sleep(max(.1,min(args.status_interval_s,1)))
    except Exception as exc:
        errors.append(f"Unhandled worker exception: {type(exc).__name__}: {exc}"); termination="worker_exception"
    finally:
        if bag_proc is not None:
            bag_code=_sw_stop(bag_proc,warnings); bag_stop=utc_now()
            if bag_trigger is None: bag_trigger="landed" if landed_seen else "disarmed_fallback"
            if bag_handle: bag_handle.close()
        _sw_audio_stop(args,state,warnings)
        if args.enable_audio:
            audio_sync=_sw_sync_audio(args,root)
            state["audio_sync_result"]=audio_sync
            if not audio_sync["ok"]:
                errors.append("ReSpeaker audio collection failed: "+failure_detail(audio_sync))
    if termination=="duration_expired_after_recording" and status_failures:
        termination="connection_lost_then_duration_expired"
    bag_size=sum(x.stat().st_size for x in bag.rglob("*") if x.is_file()) if bag.exists() else 0
    if bag_proc is not None and bag_trigger is None:
        bag_trigger="landed" if landed_seen else "disarmed_fallback" if disarm_utc else "worker_termination"
    final={"mission_id":args.mission_id,"drone_id":args.drone_id,"ip_address":args.ip_address,"ros_domain_id":args.ros_domain_id,"static_peer":args.ip_address,"state":"DONE","phase":"DONE","arm_detected_utc":arm_utc,"arm_detected_px4_timestamp":arm_px4_ts,"land_detected_utc":land_utc,"land_detected_px4_timestamp":land_px4_ts,"disarm_detected_utc":disarm_utc,"disarm_detected_px4_timestamp":disarm_px4_ts,"bag_path":str(bag),"bag_stop_utc":bag_stop,"bag_stop_trigger":bag_trigger,"bag_return_code":bag_code,"bag_size_bytes":bag_size,"connection_lost":bool(state["connection_loss_events"]),"bag":{"enabled":args.enable_ground_rosbag,"path":str(bag),"start_utc":bag_start,"stop_utc":bag_stop,"stop_trigger":bag_trigger,"return_code":bag_code,"size_bytes":bag_size},"drone_ros_ok":getattr(args,"drone_ros_ok",None),"ground_ros_ok":True,"px4_msgs_decode_ok":"px4_msgs" in ground.get("vehicle_status_type","") and "px4_msgs" in ground.get("sensor_combined_type","") ,"visible_topics":ground.get("visible_topics",[]),"ready_utc":ready_utc,"audio":{"enabled":args.enable_audio,"disabled_intentionally":not args.enable_audio,"started_utc":state.get("audio_started_utc"),"stopped_utc":state.get("audio_stopped_utc"),"path":str(root/"audio"/"respeaker_6ch.wav"),"exists":(root/"audio"/"respeaker_6ch.wav").is_file(),"sync_result":state.get("audio_sync_result")},"ground_preflight":ground,"warnings":warnings,"errors":errors,"connection_loss_events":state["connection_loss_events"],"termination_reason":termination or "worker_finalized","post_landing_record_s":args.post_landing_record_s,"finalized_utc":utc_now()}
    save_json_atomic(final_path,final); save_json_atomic(status_path,final); return 1 if errors else 0


def _sw_archive(args,drones,swarm,profile):
    root=Path(args.archive_root).expanduser().resolve(); root.mkdir(parents=True,exist_ok=True); mission=root/args.mission_id
    if mission.exists(): raise FileExistsError(f"Mission archive already exists and was not changed: {mission}")
    code=Path(args.code_root).expanduser().resolve(); state=capture_base_station_code_state(Path(__file__).resolve(),code); (mission/"metadata").mkdir(parents=True); (mission/"config"/"profiles").mkdir(parents=True); (mission/"config"/"drones").mkdir(parents=True); shutil.copy2(swarm,mission/"config"/swarm.name); shutil.copy2(profile,mission/"config"/"profiles"/profile.name)
    records={}
    for drone in drones:
        local=mission/"drones"/drone["drone_id"]
        for name in ("ros_bags","px4_logs","audio","metadata","status_logs","processed"): (local/name).mkdir(parents=True,exist_ok=True)
        drone["local_dir"]=str(local); drone["remote_mission_dir"]=drone["remote_root"]+"/"+args.mission_id+"__"+drone["drone_id"]; shutil.copy2(drone["config_path"],mission/"config"/"drones"/Path(drone["config_path"]).name)
        records[drone["drone_id"]]={"drone_id":drone["drone_id"],"ip_address":drone["ip_address"],"ssh_host":drone["ssh_host"],"ros_domain_id":drone["ros_domain_id"],"static_peer":drone["ip_address"],"enabled":drone["enabled"],"required":drone["required"],"post_landing_record_s":drone["post_landing_record_s"],"status":"PENDING","warnings":[],"errors":list(drone["config_errors"])}
    code_path=mission/"metadata"/"code_state.json"; save_json_atomic(code_path,state); meta=mission/"metadata"/"mission_metadata.json"; manifest=mission/"metadata"/"swarm_manifest.json"
    base={"schema_version":2,"mission_id":args.mission_id,"mission_name":args.mission_name,"operator":args.operator,"mission_start_utc":args.created_utc,"mission_end_utc":None,"configured_drones":records,"drones":records,"archive_computer":{"hostname":os.uname().nodename},"code_state":{"path":str(code_path)},"configuration_snapshots":{"swarm":str(mission/"config"/swarm.name),"profile":str(mission/"config"/"profiles"/profile.name)},"swarm_readiness":{"status":"PENDING","required_drone_ids":[d["drone_id"] for d in drones if d["enabled"] and d["required"]],"ready_drone_ids":[],"failed_drone_ids":[],"allow_partial_swarm":args.allow_partial_swarm},"overall_termination_reason":None,"safety":"Ground Station B is passive and never sends arm, flight, land, or disarm commands."}
    save_json_atomic(meta,base); save_json_atomic(manifest,{"schema_version":2,"mission_id":args.mission_id,"collection_status":"pending","drones":records})
    save_json_atomic(mission/"metadata"/"ground_orchestrator_log.json",{"schema_version":2,"mission_id":args.mission_id,"mission_start_utc":args.created_utc,"ground_rosbag_enabled":args.enable_ground_rosbag,"audio_enabled":args.enable_audio,"auto_ulog":args.auto_ulog,"allow_partial_swarm":args.allow_partial_swarm,"events":[],"safety":"Passive archive computer; no arm, flight, land, or disarm commands."})
    for drone in drones: save_json_atomic(Path(drone["local_dir"])/"metadata"/"collection_manifest.json",{"schema_version":2,"mission_id":args.mission_id,"drone_id":drone["drone_id"],"ip_address":drone["ip_address"],"ros_domain_id":drone["ros_domain_id"],"static_peer":drone["ip_address"],"collection_status":"pending","warnings":[],"errors":list(drone["config_errors"])})
    (mission/"README.md").write_text(f"# HALO Swarm Mission Archive: {args.mission_id}\n\nGround Station B is passive. Ground Station A controls ARM, flight, LAND, and DISARM.\n\nPreflight order: CHECK DRONE ROS TOPICS FIRST, then CHECK GROUND ROS TOPICS.\n",encoding="utf-8")
    return mission,meta,manifest


def _sw_launch(args,drone):
    env=os.environ.copy(); env.update({"ROS_DOMAIN_ID":str(drone["ros_domain_id"]),"ROS_LOCALHOST_ONLY":"0","ROS_AUTOMATIC_DISCOVERY_RANGE":"SUBNET","ROS_STATIC_PEERS":drone["ip_address"],"RMW_IMPLEMENTATION":"rmw_fastrtps_cpp"})
    command=[sys.executable,str(Path(__file__).resolve()),"--worker","--mission-id",args.mission_id,"--drone-id",drone["drone_id"],"--drone-dir",drone["local_dir"],"--ip-address",drone["ip_address"],"--ros-domain-id",str(drone["ros_domain_id"]),"--post-landing-record-s",str(drone["post_landing_record_s"]),"--duration",str(args.duration),"--status-interval-s",str(args.status_interval_s),"--ssh-host",drone["ssh_host"],"--remote-mission-dir",drone["remote_mission_dir"],"--px4-msgs-workspace",args.px4_msgs_workspace,"--audio-device",drone["audio_device"],"--sample-rate",str(drone["sample_rate"]),"--channels",str(drone["channels"]),"--sample-format",drone["sample_format"]]
    if args.enable_ground_rosbag: command.append("--enable-ground-rosbag")
    if args.enable_audio: command.append("--enable-audio")
    shell=f"set -e\nsource {shlex.quote(_SW_JAZZY)}\nsource {shlex.quote(args.px4_msgs_workspace)}\nexport ROS_DOMAIN_ID={drone['ros_domain_id']}\nexport ROS_LOCALHOST_ONLY=0\nexport ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET\nexport ROS_STATIC_PEERS={shlex.quote(drone['ip_address'])}\nexport RMW_IMPLEMENTATION=rmw_fastrtps_cpp\nexec "+shlex.join(command)
    console=(Path(drone["local_dir"])/"status_logs"/"worker_console.log").open("a",encoding="utf-8"); drone["console"]=console; drone["process"]=subprocess.Popen(["bash","-lc",shell],stdout=console,stderr=subprocess.STDOUT,env=env,start_new_session=True,text=True)


def _sw_collect(args,drone,epoch):
    local=Path(drone["local_dir"]); meta=local/"metadata"; px4=local/"px4_logs"; final=load_json(meta/"worker_final.json"); final=final or {"state":drone.get("status","FAILED"),"errors":drone.get("errors",[]),"warnings":drone.get("warnings",[]),"bag":{}}
    search_records=[]; candidates=[]; cmd=None
    for attempt in range(1,4):
        cmd=_sw_run(_sw_ssh(drone["ssh_host"],f"find {shlex.quote(drone['px4_root'])} -type f -name '*.ulg' -printf '%T@ %p\\n' | sort -n | tail -20"),30)
        search_records.append({"attempt":attempt,"result":cmd})
        candidates=[]
        [candidates.append((float(m.group(1)),m.group(2))) for line in cmd.get("stdout","").splitlines() if (m:=re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s+(.+)$",line))]
        if cmd["ok"] and any(modified>=epoch for modified,_ in candidates):
            break
        if attempt<3:
            time.sleep(5)
    (meta/"ulog_candidates_remote.txt").write_text("\n\n".join(f"attempt {item['attempt']}\n{item['result'].get('stdout') or item['result'].get('stderr','')}" for item in search_records)+"\n",encoding="utf-8")
    selected=next((path for modified,path in reversed(candidates) if modified>=epoch),None); warnings=list(final.get("warnings",[])); errors=list(final.get("errors",[]))
    if not any(item["result"]["ok"] for item in search_records): errors.append("ULog candidate search failed after retries: "+failure_detail(cmd or {}))
    elif selected is None: warnings.append("No PX4 ULog candidate was modified after mission start")
    copy=None; local_path=None
    if selected and args.auto_ulog:
        px4.mkdir(parents=True,exist_ok=True); copy=_sw_run(["rsync","-av","-e","ssh -o BatchMode=yes -o ConnectTimeout=10",f"{drone['ssh_host']}:{selected}",str(px4)+"/"],120); local_path=str(px4/Path(selected).name) if copy["ok"] else None
        if not copy["ok"]: errors.append("Selected ULog copy failed: "+failure_detail(copy))
    audio=dict(final.get("audio",{})) if isinstance(final.get("audio"),dict) else {"enabled":args.enable_audio,"disabled_intentionally":not args.enable_audio}
    if args.enable_audio:
        audio_sync=_sw_sync_audio(args,local)
        audio["collection_sync_result"]=audio_sync
        audio["local_path"]=str(local/"audio"/"respeaker_6ch.wav")
        audio["exists"]=(local/"audio"/"respeaker_6ch.wav").is_file()
        if not audio_sync["ok"]: errors.append("ReSpeaker audio retry failed: "+failure_detail(audio_sync))
    manifest={"schema_version":2,"mission_id":args.mission_id,"drone_id":drone["drone_id"],"ip_address":drone["ip_address"],"ros_domain_id":drone["ros_domain_id"],"static_peer":drone["ip_address"],"worker_status":final,"bag":final.get("bag",{}),"audio":audio,"ulog":{"candidate_file":str(meta/"ulog_candidates_remote.txt"),"candidates":[{"modified_epoch":t,"remote_path":p} for t,p in candidates],"selected_remote_path":selected,"selected_local_path":local_path,"copy_result":copy},"warnings":warnings,"errors":errors,"termination_reason":final.get("termination_reason"),"collection_status":"failed" if final.get("state") in {"FAILED_PREFLIGHT","FAILED_ENVIRONMENT"} or errors else "complete_with_warnings" if warnings else "complete","collected_utc":utc_now()}
    save_json_atomic(meta/"collection_manifest.json",manifest); return manifest


def _sw_update_meta(path,drones,readiness=None,termination=None):
    metadata=load_json(path)
    if readiness is not None: metadata["swarm_readiness"]=readiness
    for drone in drones: metadata.setdefault("drones",{}).setdefault(drone["drone_id"],{}).update({"status":drone.get("status"),"preflight":drone.get("preflight"),"warnings":drone.get("warnings",[]),"errors":drone.get("errors",[])})
    if termination: metadata["mission_end_utc"],metadata["overall_termination_reason"]=utc_now(),termination
    save_json_atomic(path,metadata)


def _sw_parser():
    parser=argparse.ArgumentParser(description="Passive five-drone ground archive orchestrator")
    parser.add_argument("--worker",action="store_true",help=argparse.SUPPRESS)
    for name in ("mission-name","mission-id","operator","swarm","profile"): parser.add_argument("--"+name)
    parser.add_argument("--code-root",default=str(Path.cwd())); parser.add_argument("--archive-root",default=str(Path.home()/"MIC_ARRAY_ROS"/"HALO_ARCHIVE")); parser.add_argument("--duration",type=float); parser.add_argument("--max-duration-s",dest="duration_alias",type=float)
    parser.add_argument("--enable-audio",action="store_true"); parser.add_argument("--enable-ground-rosbag",action="store_true"); parser.add_argument("--enable-rosbag",action="store_true"); parser.add_argument("--auto-ulog",action="store_true"); parser.add_argument("--no-auto-ulog",action="store_true"); parser.add_argument("--allow-partial-swarm",action="store_true"); parser.add_argument("--dry-run",action="store_true"); parser.add_argument("--preflight-only",action="store_true")
    parser.add_argument("--start-delay-s",type=float,default=0.0); parser.add_argument("--mirror-interval-s",type=float,default=0.0); parser.add_argument("--post-disarm-wait-s",type=float,default=10.0); parser.add_argument("--status-interval-s",type=float,default=1.0); parser.add_argument("--preflight-timeout-s",type=float,default=90.0); parser.add_argument("--px4-msgs-workspace",default=_SW_PX4)
    parser.add_argument("--audio-device",default="hw:0,0"); parser.add_argument("--sample-rate",type=int,default=16000); parser.add_argument("--channels",type=int,default=6); parser.add_argument("--sample-format",default="S16_LE")
    parser.add_argument("--drone-id"); parser.add_argument("--drone-dir"); parser.add_argument("--ip-address"); parser.add_argument("--ros-domain-id",type=int); parser.add_argument("--post-landing-record-s",type=float); parser.add_argument("--ssh-host"); parser.add_argument("--remote-mission-dir")
    return parser


def _swarm_v2_main():
    parser=_sw_parser(); args=parser.parse_args()
    if args.worker:
        args.duration=args.duration if args.duration is not None else args.duration_alias
        needed=("mission_id","drone_id","drone_dir","ip_address","ros_domain_id","post_landing_record_s","ssh_host","remote_mission_dir")
        if any(getattr(args,x) in (None,"") for x in needed) or args.duration is None: parser.error("worker mode received incomplete arguments")
        return _sw_worker(args)
    args.enable_ground_rosbag=bool(args.enable_ground_rosbag or args.enable_rosbag); args.auto_ulog=not args.no_auto_ulog; args.duration=args.duration if args.duration is not None else args.duration_alias
    for name in ("mission_name","operator","swarm","profile"):
        if not getattr(args,name): parser.error(f"--{name.replace('_','-')} is required")
    if args.duration is None or args.duration<=0: parser.error("--duration/--max-duration-s must be greater than zero")
    args.px4_msgs_workspace=str(Path(args.px4_msgs_workspace).expanduser()); args.mission_id=args.mission_id or datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_UTC")+"_"+sanitize_id_component(args.mission_name)
    code_root,swarm_path,profile_path=Path(args.code_root).expanduser().resolve(),Path(args.swarm).expanduser().resolve(),Path(args.profile).expanduser().resolve()
    if not code_root.is_dir() or not swarm_path.is_file() or not profile_path.is_file(): parser.error("code root, swarm, and profile paths must exist")
    try: drones=_sw_normalize(swarm_path,code_root); load_yaml_subset(profile_path)
    except (OSError,ValueError,FileNotFoundError) as exc: parser.error(str(exc))
    print(f"Common mission ID: {args.mission_id}")
    for drone in drones: print(f"{drone['drone_id']} | ip={drone['ip_address']} | domain={drone['ros_domain_id'] if drone['ros_domain_id'] is not None else 'MISSING'} | required={str(drone['required']).lower()}")
    if args.dry_run:
        print("DRY RUN: no archive, SSH, ROS, bag, audio, or flight command was started.")
        for drone in drones: print(f"{drone['drone_id']}: NOT READY - "+"; ".join(drone["config_errors"]) if drone["config_errors"] else f"{drone['drone_id']}: worker env will use ROS_STATIC_PEERS={drone['ip_address']}")
        return 0
    args.created_utc=utc_now(); mission,meta,manifest_path=_sw_archive(args,drones,swarm_path,profile_path); enabled=[d for d in drones if d["enabled"]]; pf=_sw_parallel(enabled,_sw_preflight); failed=[]
    for drone in drones:
        result=pf.get(drone["drone_id"],{"ok":False,"state":"FAILED_PREFLIGHT","errors":["drone preflight did not run"],"warnings":[]}); drone["preflight"]=result; drone["warnings"]=result.get("warnings",[]); drone["errors"]=result.get("errors",[]); drone["status"]=result.get("state","FAILED_PREFLIGHT"); save_json_atomic(Path(drone["local_dir"])/"metadata"/"drone_preflight.json",result)
        if drone["enabled"] and not result.get("ok"): failed.append(drone["drone_id"])
    _sw_update_meta(meta,drones,{"status":"DRONE_PREFLIGHT_COMPLETE","required_drone_ids":[d["drone_id"] for d in enabled if d["required"]],"ready_drone_ids":[],"failed_drone_ids":failed,"allow_partial_swarm":args.allow_partial_swarm})
    workers=[]
    for drone in [d for d in enabled if d["drone_id"] not in failed]:
        _sw_launch(args,drone); workers.append(drone); drone["status"]="GROUND_WORKER_STARTING"
    ready=[]; deadline=time.monotonic()+args.preflight_timeout_s
    while time.monotonic()<deadline:
        ready=[d["drone_id"] for d in workers if _sw_status_file(d,"status_logs/worker_status.json").get("state")=="READY"]
        required=[d["drone_id"] for d in enabled if d["required"]]
        if all(x in ready for x in required) or (args.allow_partial_swarm and ready) or (workers and all(d["process"].poll() is not None for d in workers)): break
        time.sleep(min(args.status_interval_s,1))
    required=[d["drone_id"] for d in enabled if d["required"]]; all_ready=all(x in ready for x in required); readiness="READY" if all_ready else "PARTIAL_READY" if args.allow_partial_swarm and ready else "NOT_READY"
    print("SWARM ARCHIVE READY" if readiness=="READY" else "SWARM ARCHIVE PARTIAL READY (--allow-partial-swarm)" if readiness=="PARTIAL_READY" else "SWARM ARCHIVE NOT READY: required drones did not pass preflight.",file=sys.stderr if readiness=="NOT_READY" else sys.stdout)
    for drone in enabled:
        ready_text="READY | ROS topics OK | bag prepared" if drone["drone_id"] in ready else "NOT READY"
        print(f"{drone['drone_id']} {ready_text} | domain={drone['ros_domain_id'] if drone['ros_domain_id'] is not None else 'MISSING'}")
    _sw_update_meta(meta,drones,{"status":readiness,"required_drone_ids":required,"ready_drone_ids":sorted(ready),"failed_drone_ids":sorted(set(failed)),"allow_partial_swarm":args.allow_partial_swarm})
    if readiness=="NOT_READY" or args.preflight_only:
        for drone in workers:
            if drone["process"].poll() is None: _sw_stop(drone["process"],drone["warnings"])
    else:
        print("Waiting for ARMED state on each drone. Flight control remains with Ground Station A."); deadline=time.monotonic()+args.duration+45; last_live={}
        while time.monotonic()<deadline and any(d["process"].poll() is None for d in workers):
            for drone in workers:
                status=_sw_status_file(drone,"status_logs/worker_status.json")
                if not status: continue
                bag_status=status.get("bag",{}) if isinstance(status.get("bag"),dict) else {}
                size_mb=float(bag_status.get("size_bytes",0) or 0)/1048576.0
                remaining=status.get("post_landing_remaining_s")
                signature=(status.get("vehicle_state"),status.get("state"),round(size_mb,1),round(float(remaining),1) if remaining is not None else None)
                if last_live.get(drone["drone_id"])==signature: continue
                last_live[drone["drone_id"]]=signature
                detail=f"{float(remaining):.1f} s remaining" if remaining is not None else f"bag {size_mb:.1f} MB"
                print(f"{drone['drone_id']} | {str(status.get('vehicle_state','UNKNOWN')).upper()} | {status.get('state','UNKNOWN')} | {detail}")
            time.sleep(min(args.status_interval_s,1))
        for drone in workers:
            if drone["process"].poll() is None: _sw_stop(drone["process"],drone["warnings"])
    for drone in workers:
        if drone.get("console"): drone["console"].close()
        final=load_json(Path(drone["local_dir"])/"metadata"/"worker_final.json")
        if final: drone["status"]=final.get("state","DONE")
    epoch=_sw_epoch(args.created_utc) or time.time(); results=_sw_parallel(drones,lambda d:_sw_collect(args,d,epoch)); swarm=load_json(manifest_path); statuses=[]
    for drone in drones:
        m=results.get(drone["drone_id"],{}); statuses.append(m.get("collection_status","failed")); swarm.setdefault("drones",{})[drone["drone_id"]]=m; print(f"{drone['drone_id']}: collection {'complete' if m.get('collection_status')!='failed' else 'failed; archive retained'}")
    swarm["updated_utc"]=utc_now(); swarm["collection_status"]="complete" if all(x=="complete" for x in statuses) else "complete_with_drone_failures"; save_json_atomic(manifest_path,swarm)
    collection_issue=any(status!="complete" for status in statuses)
    reason="swarm_not_ready_required_preflight_failed" if readiness=="NOT_READY" else "preflight_only" if args.preflight_only else "completed_with_drone_failures" if failed or collection_issue else "swarm_workers_completed"; _sw_update_meta(meta,drones,termination=reason)
    log_path=mission/"metadata"/"ground_orchestrator_log.json"; log=load_json(log_path); log.update({"mission_end_utc":utc_now(),"overall_termination_reason":reason,"collection_status":swarm["collection_status"],"drone_collection_statuses":{d["drone_id"]:results.get(d["drone_id"],{}).get("collection_status","failed") for d in drones}}); save_json_atomic(log_path,log)
    print(f"Ground swarm archive: {mission}"); print(f"Swarm manifest: {manifest_path}")
    return 1 if readiness=="NOT_READY" or failed else 0


def _sw_status_file(drone,relative):
    return load_json(Path(drone["local_dir"])/relative)

if __name__ == "__main__":
    raise SystemExit(main())
