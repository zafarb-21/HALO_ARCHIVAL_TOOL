#!/usr/bin/env python3

"""Create, run, monitor, and automatically collect one HALO mission."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_ROSBAG_TOPICS = (
    "/fmu/out/vehicle_status",
    "/fmu/out/sensor_combined",
    "/fmu/out/vehicle_local_position",
    "/fmu/out/vehicle_attitude",
    "/fmu/out/vehicle_odometry",
    "/fmu/out/battery_status",
    "/fmu/out/timesync_status",
)

_interrupt_recovery_context: dict[str, Any] | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def add_unique(items: list[str], message: str) -> None:
    if message not in items:
        items.append(message)


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_json_atomic(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_profile_rosbag_topics(path: Path) -> list[str] | None:
    """Read the block-style rosbag.topics list without requiring PyYAML."""
    rosbag_indent: int | None = None
    topics_indent: int | None = None
    topics: list[str] = []

    for line_number, original in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        # ROS topic names cannot contain '#', so stripping YAML comments here is
        # intentionally narrow and sufficient for this profile field.
        line = original.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        text = line.strip()

        if rosbag_indent is None:
            if text == "rosbag:":
                rosbag_indent = indent
            continue

        if indent <= rosbag_indent:
            break

        if topics_indent is None:
            match = re.fullmatch(r"topics\s*:\s*(.*)", text)
            if match is None:
                continue
            topics_indent = indent
            inline_value = match.group(1).strip()
            if inline_value == "[]":
                return []
            if inline_value:
                raise ValueError(
                    f"{path}:{line_number}: rosbag.topics must use a YAML block list"
                )
            continue

        if indent <= topics_indent:
            break
        match = re.fullmatch(r"-\s+(.+)", text)
        if match is None:
            raise ValueError(
                f"{path}:{line_number}: invalid rosbag.topics list entry"
            )
        topic = match.group(1).strip().strip("'\"")
        topics.append(topic)

    return topics if topics_indent is not None else None


def normalize_rosbag_topics(
    topics: list[str], parser: argparse.ArgumentParser
) -> list[str]:
    normalized: list[str] = []
    for topic in topics:
        if not topic.startswith("/"):
            parser.error(
                f"ROS bag topic {topic!r} must be an absolute ROS2 topic name"
            )
        if topic not in normalized:
            normalized.append(topic)
    return normalized


def run_command(cmd: list[str], timeout: float | None = None) -> dict[str, Any]:
    try:
        result = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return {
            "command": shlex.join(cmd),
            "return_code": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "ok": result.returncode == 0,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return {
            "command": shlex.join(cmd),
            "return_code": 124,
            "stdout": stdout.strip(),
            "stderr": stderr.strip() or f"Timed out after {timeout:g} seconds",
            "ok": False,
        }
    except OSError as exc:
        return {
            "command": shlex.join(cmd),
            "return_code": 127,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "ok": False,
        }


def ground_rosbag_setup_lines(args: argparse.Namespace) -> list[str]:
    """Build the shell setup used by ground-side ROS2 commands."""
    lines = [
        "set -e",
        "source /opt/ros/jazzy/setup.bash",
    ]
    if args.px4_msgs_workspace:
        workspace = str(Path(args.px4_msgs_workspace).expanduser())
        lines.append("source " + shlex.quote(workspace))
    lines.extend(
        [
            "export ROS_DOMAIN_ID={0}".format(args.ros_domain_id),
            "export ROS_LOCALHOST_ONLY=0",
            "export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET",
        ]
    )
    return lines


def ground_rosbag_shell(args: argparse.Namespace, command: str) -> str:
    return "\n".join(ground_rosbag_setup_lines(args) + [command])


def parse_ros2_topic_list(output: str) -> list[str]:
    topics = []
    for line in output.splitlines():
        fields = line.strip().split(None, 1)
        if not fields or not fields[0].startswith("/"):
            continue
        if fields[0] not in topics:
            topics.append(fields[0])
    return sorted(topics)


def ground_rosbag_record_template(
    args: argparse.Namespace, mission_id: str, mission_dir: Path
) -> dict[str, Any]:
    output_name = args.ground_rosbag_output_name or (mission_id + "_ground_rosbag")
    output_path = mission_dir / "drone_data" / "ros_bags" / output_name
    output_path.parent.mkdir(parents=True, exist_ok=True)
    bag_command = "exec ros2 bag record -a -o {0}".format(
        shlex.quote(str(output_path))
    )
    return {
        "enabled": bool(args.enable_ground_rosbag),
        "start_attempted": False,
        "started": False,
        "active": False,
        "all_topics": True,
        "all_topics_flag_requested": bool(args.ground_rosbag_all),
        "command": ground_rosbag_shell(args, bag_command),
        "output_path": str(output_path),
        "ros_domain_id": args.ros_domain_id,
        "ros_localhost_only": "0",
        "ros_automatic_discovery_range": "SUBNET",
        "px4_msgs_setup_path": (
            str(Path(args.px4_msgs_workspace).expanduser())
            if args.px4_msgs_workspace
            else None
        ),
        "px4_msgs_workspace": (
            str(Path(args.px4_msgs_workspace).expanduser())
            if args.px4_msgs_workspace
            else None
        ),
        "preflight_command": ground_rosbag_shell(args, "ros2 topic list -t"),
        "preflight_topics": [],
        "preflight_fmu_topics": [],
        "preflight_return_code": None,
        "start_utc": None,
        "stop_utc": None,
        "return_code": None,
        "termination_reason": None,
        "console_log_path": str(
            mission_dir / "metadata" / "ground_rosbag_console.log"
        ),
        "warnings": [],
        "errors": [],
    }


def start_ground_rosbag(
    args: argparse.Namespace,
    mission_dir: Path,
    log: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    """Run a non-fatal ROS2 topic preflight and start the local bag recorder."""
    record = state["record"]
    if not record["enabled"]:
        return record

    preflight = run_command(
        ["bash", "-lc", record["preflight_command"]], timeout=20.0
    )
    record["preflight_return_code"] = preflight.get("return_code")
    record["preflight_topics"] = parse_ros2_topic_list(preflight.get("stdout", ""))
    record["preflight_fmu_topics"] = [
        topic for topic in record["preflight_topics"] if topic.startswith("/fmu")
    ]
    record["preflight_result"] = preflight
    if not preflight["ok"]:
        warning = (
            "Ground ROS2 topic preflight failed; ground rosbag will still be "
            "attempted: {0}".format(failure_detail(preflight))
        )
        add_unique(record["warnings"], warning)
        add_unique(log["warnings"], warning)
    elif not record["preflight_fmu_topics"]:
        warning = (
            "Ground ROS2 topic preflight found no /fmu topics; ground rosbag "
            "recording may contain no PX4 topics, but the mission continues."
        )
        add_unique(record["warnings"], warning)
        add_unique(log["warnings"], warning)

    console_path = Path(record["console_log_path"])
    console_path.parent.mkdir(parents=True, exist_ok=True)
    record["start_attempted"] = True
    try:
        handle = console_path.open("a", encoding="utf-8")
        process = subprocess.Popen(
            ["bash", "-lc", record["command"]],
            stdout=handle,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            start_new_session=True,
        )
    except OSError as exc:
        if "handle" in locals():
            handle.close()
        error = "Could not start ground ROS bag recording: {0}: {1}".format(
            type(exc).__name__, exc
        )
        add_unique(record["errors"], error)
        add_unique(log["warnings"], error)
        # Keep flat log aliases in sync even when Popen fails before the normal
        # startup bookkeeping block is reached.
        log["ground_rosbag"] = record
        log["ground_rosbag_enabled"] = record["enabled"]
        log["ground_rosbag_start_attempted"] = record["start_attempted"]
        log["ground_rosbag_started"] = record["started"]
        log["ground_rosbag_output_path"] = record["output_path"]
        log["ground_rosbag_command"] = record["command"]
        log["ground_rosbag_ros_domain_id"] = record["ros_domain_id"]
        log["ground_rosbag_px4_msgs_setup_path"] = record["px4_msgs_setup_path"]
        log["ground_rosbag_px4_msgs_workspace"] = record["px4_msgs_workspace"]
        log["ground_rosbag_start_utc"] = record["start_utc"]
        log["ground_rosbag_stop_utc"] = record["stop_utc"]
        log["ground_rosbag_return_code"] = record["return_code"]
        log["ground_rosbag_warnings"] = list(record["warnings"])
        log["ground_rosbag_errors"] = list(record["errors"])
        log["ground_rosbag_preflight_topics"] = list(record["preflight_topics"])
        log["ground_rosbag_preflight_fmu_topics"] = list(record["preflight_fmu_topics"])
        return record

    state["process"] = process
    state["log_handle"] = handle
    record["started"] = True
    record["active"] = True
    record["start_utc"] = utc_now()
    # Let an immediately failing shell (missing ROS setup/ros2) report itself
    # before the readiness message, while keeping startup non-fatal.
    time.sleep(0.2)
    return_code = process.poll()
    if return_code is not None:
        record["started"] = False
        record["active"] = False
        record["return_code"] = return_code
        warning = (
            "Ground ROS bag process exited during startup with return code "
            "{0}; PX4 ULog/audio collection continues.".format(return_code)
        )
        add_unique(record["warnings"], warning)
        add_unique(log["warnings"], warning)
    log["ground_rosbag"] = record
    log["ground_rosbag_enabled"] = record["enabled"]
    log["ground_rosbag_start_attempted"] = record["start_attempted"]
    log["ground_rosbag_started"] = record["started"]
    log["ground_rosbag_output_path"] = record["output_path"]
    log["ground_rosbag_command"] = record["command"]
    log["ground_rosbag_ros_domain_id"] = record["ros_domain_id"]
    log["ground_rosbag_px4_msgs_setup_path"] = record["px4_msgs_setup_path"]
    log["ground_rosbag_px4_msgs_workspace"] = record["px4_msgs_workspace"]
    log["ground_rosbag_start_utc"] = record["start_utc"]
    log["ground_rosbag_stop_utc"] = record["stop_utc"]
    log["ground_rosbag_return_code"] = record["return_code"]
    log["ground_rosbag_warnings"] = list(record["warnings"])
    log["ground_rosbag_errors"] = list(record["errors"])
    log["ground_rosbag_preflight_topics"] = list(record["preflight_topics"])
    log["ground_rosbag_preflight_fmu_topics"] = list(record["preflight_fmu_topics"])
    log_event(
        log,
        "ground_rosbag_started",
        started=record["started"],
        active=record["active"],
        output_path=record["output_path"],
        preflight_fmu_topics=record["preflight_fmu_topics"],
    )
    return record


def stop_ground_rosbag(
    state: dict[str, Any] | None,
    log: dict[str, Any],
    reason: str,
) -> dict[str, Any] | None:
    """Stop the local ros2 bag with SIGINT first so metadata is finalized."""
    if not isinstance(state, dict) or not isinstance(state.get("record"), dict):
        return None
    record = state["record"]
    if not record.get("enabled"):
        return record
    process = state.get("process")
    if process is not None and process.poll() is None:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGINT)
            process.wait(timeout=20.0)
        except subprocess.TimeoutExpired:
            warning = "Ground ROS bag did not stop after SIGINT; SIGTERM was sent."
            add_unique(record["warnings"], warning)
            add_unique(log["warnings"], warning)
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                process.wait(timeout=5.0)
            except Exception as exc:
                error = "Ground ROS bag force-stop failed: {0}: {1}".format(
                    type(exc).__name__, exc
                )
                add_unique(record["errors"], error)
                add_unique(log["warnings"], error)
        except ProcessLookupError:
            pass
        except OSError as exc:
            error = "Could not signal ground ROS bag: {0}: {1}".format(
                type(exc).__name__, exc
            )
            add_unique(record["errors"], error)
            add_unique(log["warnings"], error)
    if process is not None:
        record["return_code"] = process.poll()
    record["active"] = False
    record["stop_utc"] = utc_now()
    record["termination_reason"] = reason
    handle = state.get("log_handle")
    if handle is not None:
        try:
            handle.close()
        except Exception:
            pass
    state["process"] = None
    state["log_handle"] = None
    log["ground_rosbag"] = record
    log["ground_rosbag_enabled"] = record["enabled"]
    log["ground_rosbag_start_attempted"] = record["start_attempted"]
    log["ground_rosbag_started"] = record["started"]
    log["ground_rosbag_output_path"] = record["output_path"]
    log["ground_rosbag_command"] = record["command"]
    log["ground_rosbag_ros_domain_id"] = record["ros_domain_id"]
    log["ground_rosbag_px4_msgs_setup_path"] = record["px4_msgs_setup_path"]
    log["ground_rosbag_px4_msgs_workspace"] = record["px4_msgs_workspace"]
    log["ground_rosbag_start_utc"] = record["start_utc"]
    log["ground_rosbag_stop_utc"] = record["stop_utc"]
    log["ground_rosbag_return_code"] = record["return_code"]
    log["ground_rosbag_warnings"] = list(record["warnings"])
    log["ground_rosbag_errors"] = list(record["errors"])
    log["ground_rosbag_preflight_topics"] = list(record["preflight_topics"])
    log["ground_rosbag_preflight_fmu_topics"] = list(record["preflight_fmu_topics"])
    log_event(
        log,
        "ground_rosbag_stopped",
        reason=reason,
        return_code=record.get("return_code"),
        output_path=record.get("output_path"),
    )
    return record


def remote_command(command: str) -> str:
    return f"LC_ALL=C LANG=C sh -c {shlex.quote(command)}"


def failure_detail(result: dict[str, Any]) -> str:
    detail = result.get("stderr") or result.get("stdout")
    if detail:
        return str(detail).splitlines()[0]
    return f"exit code {result.get('return_code')}"


def log_event(log: dict[str, Any], event: str, **details: Any) -> None:
    log.setdefault("events", []).append(
        {"timestamp_utc": utc_now(), "event": event, **details}
    )


def sync_log(path: Path, log: dict[str, Any]) -> None:
    log["updated_utc"] = utc_now()
    save_json_atomic(path, log)


def merge_mission_metadata(
    metadata_path: Path,
    log_path: Path,
    log: dict[str, Any],
    current_status: str,
) -> None:
    metadata = load_json(metadata_path)
    run_status = metadata.setdefault("run_status", {})
    run_status["current_status"] = current_status
    run_status["started"] = bool(log.get("agent_startup_confirmed"))
    run_status["completed"] = current_status == "automatic_collection_complete"
    run_status["orchestrator_started"] = True
    run_status["orchestrator_finished_utc"] = log.get("finished_utc")
    if log.get("agent_termination_reason"):
        run_status["termination_reason"] = log["agent_termination_reason"]
    for warning in log.get("warnings", []):
        warnings = run_status.setdefault("warnings", [])
        if warning not in warnings:
            warnings.append(warning)
    for error in log.get("errors", []):
        errors = run_status.setdefault("errors", [])
        if error not in errors:
            errors.append(error)
    metadata["ground_orchestrator"] = {
        "log_path": str(log_path),
        "started_utc": log.get("started_utc"),
        "finished_utc": log.get("finished_utc"),
        "drone_host": log.get("drone_host"),
        "remote_agent_pid": log.get("remote_agent_pid"),
        "agent_preflight_check": log.get("agent_preflight_check"),
        "agent_startup_confirmed": log.get("agent_startup_confirmed", False),
        "agent_startup_confirmation_failed": log.get(
            "agent_startup_confirmation_failed", False
        ),
        "duration_s": log.get("duration_s"),
        "max_duration_s": log.get("duration_s"),
        "duration_semantics": "maximum_recording_and_monitoring_window",
        "audio_enabled": log.get("audio_enabled"),
        "audio_disabled_intentionally": log.get("audio_enabled") is False,
        "rosbag_enabled": log.get("rosbag_enabled"),
        "rosbag_topics": list(log.get("rosbag_topics", [])),
        "ground_rosbag": dict(log.get("ground_rosbag", {})),
        "ground_rosbag_enabled": log.get("ground_rosbag_enabled", False),
        "ground_rosbag_start_attempted": log.get("ground_rosbag_start_attempted", False),
        "ground_rosbag_started": log.get("ground_rosbag_started", False),
        "ground_rosbag_output_path": log.get("ground_rosbag_output_path"),
        "ground_rosbag_ros_domain_id": log.get("ground_rosbag_ros_domain_id"),
        "ground_rosbag_px4_msgs_setup_path": log.get("ground_rosbag_px4_msgs_setup_path"),
        "ground_rosbag_px4_msgs_workspace": log.get("ground_rosbag_px4_msgs_workspace"),
        "ground_rosbag_start_utc": log.get("ground_rosbag_start_utc"),
        "ground_rosbag_stop_utc": log.get("ground_rosbag_stop_utc"),
        "ground_rosbag_return_code": log.get("ground_rosbag_return_code"),
        "ground_rosbag_warnings": list(log.get("ground_rosbag_warnings", [])),
        "ground_rosbag_errors": list(log.get("ground_rosbag_errors", [])),
        "ground_rosbag_preflight_topics": list(log.get("ground_rosbag_preflight_topics", [])),
        "ground_rosbag_preflight_fmu_topics": list(log.get("ground_rosbag_preflight_fmu_topics", [])),
        "monitor_only_px4_ulog_mode": log.get("monitor_only_px4_ulog_mode", False),
        "no_auto_ulog": log.get("no_auto_ulog"),
        "auto_ulog_requested": log.get("auto_ulog_requested", False),
        "mirror_interval_s": log.get("mirror_interval_s"),
        "agent_termination_reason": log.get("agent_termination_reason"),
        "automatic_collection_attempted": log.get("automatic_collection_attempted", False),
        "automatic_collection_successful": log.get("automatic_collection_successful"),
        "warnings": list(log.get("warnings", [])),
        "errors": list(log.get("errors", [])),
    }
    save_json_atomic(metadata_path, metadata)


def parse_mission_id(creator_output: str) -> str | None:
    match = re.search(r"^Mission/session ID:\s*([A-Za-z0-9._-]+)\s*$", creator_output, re.MULTILINE)
    return match.group(1) if match else None


def upload_agent(
    drone_host: str,
    local_agent: Path,
    remote_agent: str,
) -> dict[str, Any]:
    return run_command(
        [
            "rsync",
            "-av",
            "--rsync-path=LC_ALL=C LANG=C rsync",
            str(local_agent),
            f"{drone_host}:{remote_agent}",
        ]
    )


def check_remote_agent_compatibility(
    drone_host: str,
    remote_agent: str,
) -> dict[str, Any]:
    command = f"python3 {shlex.quote(remote_agent)} --help"
    return run_command(
        ["ssh", drone_host, remote_command(command)], timeout=20.0
    )


def launch_agent(
    args: argparse.Namespace,
    mission_id: str,
    remote_mission_dir: str,
    remote_agent: str,
) -> tuple[dict[str, Any], int | None]:
    command = [
        "python3",
        remote_agent,
        "--mission-id",
        mission_id,
        "--mission-dir",
        remote_mission_dir,
        "--duration",
        str(args.duration),
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

    console_log = f"{remote_mission_dir}/status_logs/drone_agent_console.log"
    launch = (
        f"nohup {shlex.join(command)} > {shlex.quote(console_log)} 2>&1 "
        "< /dev/null & printf '%s\\n' $!"
    )
    result = run_command(
        ["ssh", args.drone_host, remote_command(launch)], timeout=20.0
    )
    pid = None
    if result["ok"]:
        for line in reversed(result["stdout"].splitlines()):
            if line.strip().isdigit():
                pid = int(line.strip())
                break
        if pid is None:
            result["ok"] = False
            result["stderr"] = "Remote launch did not return an agent PID."
    return result, pid


def pull_agent_snapshot(
    drone_host: str,
    remote_mission_dir: str,
    agent_pid: int,
) -> dict[str, Any]:
    remote_start = f"{remote_mission_dir}/metadata/drone_agent_start.json"
    remote_status = f"{remote_mission_dir}/metadata/drone_agent_status.json"
    remote_final = f"{remote_mission_dir}/metadata/drone_agent_final.json"
    command = (
        f"if kill -0 {agent_pid} 2>/dev/null; then echo __HALO_AGENT_ALIVE__; "
        "else echo __HALO_AGENT_EXITED__; fi; "
        f"if test -f {shlex.quote(remote_final)}; then echo __HALO_FINAL__; "
        f"cat {shlex.quote(remote_final)}; "
        f"elif test -f {shlex.quote(remote_status)}; then echo __HALO_STATUS__; "
        f"cat {shlex.quote(remote_status)}; "
        f"elif test -f {shlex.quote(remote_start)}; then echo __HALO_START__; "
        f"cat {shlex.quote(remote_start)}; else echo __HALO_NO_STATUS__; fi"
    )
    result = run_command(
        ["ssh", drone_host, remote_command(command)], timeout=15.0
    )
    snapshot: dict[str, Any] = {
        "ok": result["ok"],
        "command": result["command"],
        "return_code": result["return_code"],
        "stderr": result["stderr"],
        "agent_alive": None,
        "kind": None,
        "data": None,
    }
    if not result["ok"]:
        snapshot["error"] = failure_detail(result)
        return snapshot

    lines = result["stdout"].splitlines()
    snapshot["agent_alive"] = "__HALO_AGENT_ALIVE__" in lines
    marker = None
    if "__HALO_FINAL__" in lines:
        marker = "__HALO_FINAL__"
        snapshot["kind"] = "final"
    elif "__HALO_STATUS__" in lines:
        marker = "__HALO_STATUS__"
        snapshot["kind"] = "status"
    elif "__HALO_START__" in lines:
        marker = "__HALO_START__"
        snapshot["kind"] = "start"
    elif "__HALO_NO_STATUS__" in lines:
        snapshot["kind"] = "missing"

    if marker is not None:
        payload = "\n".join(lines[lines.index(marker) + 1 :]).strip()
        try:
            snapshot["data"] = json.loads(payload)
        except json.JSONDecodeError as exc:
            snapshot["ok"] = False
            snapshot["error"] = f"Could not parse remote {snapshot['kind']} JSON: {exc}"
    return snapshot


def persist_agent_snapshot(
    mission_dir: Path,
    snapshot: dict[str, Any],
    log: dict[str, Any],
):
    data = snapshot.get("data")
    if not isinstance(data, dict):
        return data

    filenames = {
        "start": "drone_agent_start.json",
        "status": "drone_agent_status.json",
        "final": "drone_agent_final.json",
    }
    filename = filenames.get(snapshot.get("kind"))
    if filename:
        save_json_atomic(mission_dir / "metadata" / filename, data)
    for warning in data.get("warnings", []):
        add_unique(log["warnings"], warning)
    for error in data.get("errors", []):
        add_unique(log["errors"], error)
    return data


def mirror_remote_mission(
    drone_host: str,
    remote_mission_dir: str,
    local_session_dir: Path,
) -> dict[str, Any]:
    local_session_dir.mkdir(parents=True, exist_ok=True)
    return run_command(
        [
            "rsync",
            "-az",
            "--partial",
            "--rsync-path=LC_ALL=C LANG=C rsync",
            f"{drone_host}:{remote_mission_dir}/",
            str(local_session_dir) + "/",
        ]
    )


def stop_remote_agent(
    drone_host: str,
    agent_pid: int,
) -> dict[str, Any]:
    return run_command(
        ["ssh", drone_host, remote_command(f"kill -INT {agent_pid}")],
        timeout=10.0,
    )


def recover_after_unhandled_interrupt() -> int:
    """Attempt agent shutdown and collection after an uncaught Ctrl+C."""
    context = _interrupt_recovery_context
    if context is None:
        print("\nInterrupted before a recoverable mission archive was created.")
        return 130

    args = context["args"]
    mission_dir = context["mission_dir"]
    metadata_path = context["metadata_path"]
    log_path = context["log_path"]
    log = context["log"]
    collector_path = context["collector_path"]
    remote_mission_dir = context["remote_mission_dir"]
    agent_pid = context.get("agent_pid")
    ground_rosbag_state = context.get("ground_rosbag_state")

    add_unique(
        log["warnings"],
        "Ground orchestrator interrupted by the user; clean shutdown and final "
        "collection were attempted.",
    )
    log_event(log, "unhandled_user_interrupt_recovery_started")
    stop_ground_rosbag(
        ground_rosbag_state, log, "user_interrupt_collection_attempted"
    )
    print("\nInterrupt received. Attempting clean agent shutdown and collection ...")

    if not isinstance(agent_pid, int):
        pid_result = run_command(
            [
                "ssh",
                args.drone_host,
                remote_command(
                    "if test -f {0}/metadata/drone_agent_pid.txt; then "
                    "cat {0}/metadata/drone_agent_pid.txt; fi".format(
                        shlex.quote(remote_mission_dir)
                    )
                ),
            ],
            timeout=10.0,
        )
        log_event(log, "user_interrupt_agent_pid_lookup", result=pid_result)
        if pid_result["ok"]:
            for line in reversed(pid_result["stdout"].splitlines()):
                if line.strip().isdigit():
                    agent_pid = int(line.strip())
                    break

    if isinstance(agent_pid, int):
        stop_result = stop_remote_agent(args.drone_host, agent_pid)
        log_event(log, "user_interrupt_agent_stop", result=stop_result)
        if not stop_result["ok"]:
            add_unique(
                log["warnings"],
                "Could not signal drone agent during interrupt recovery: "
                + failure_detail(stop_result),
            )
        else:
            final_deadline = time.monotonic() + args.post_disarm_wait_s + 20.0
            while time.monotonic() < final_deadline:
                snapshot = pull_agent_snapshot(
                    args.drone_host, remote_mission_dir, agent_pid
                )
                if snapshot["ok"]:
                    persist_agent_snapshot(mission_dir, snapshot, log)
                    if snapshot["kind"] == "final" or snapshot["agent_alive"] is False:
                        break
                time.sleep(min(args.status_interval_s, 2.0))

    termination_reason = "user_interrupt_collection_attempted"
    log["remote_agent_pid"] = agent_pid
    log["agent_termination_reason"] = termination_reason
    log["automatic_collection_attempted"] = True
    sync_log(log_path, log)
    merge_mission_metadata(
        metadata_path,
        log_path,
        log,
        "user_interrupt_collection_attempted",
    )

    collector_command = [
        sys.executable,
        str(collector_path),
        "--mission-dir",
        str(mission_dir),
        "--drone-host",
        args.drone_host,
        "--remote-sync-root",
        args.remote_sync_root,
        "--termination-reason",
        termination_reason,
        "--notes",
        "Best-effort collection after Ctrl+C in run_halo_mission.py",
    ]
    if context["effective_no_auto_ulog"] or not isinstance(agent_pid, int):
        collector_command.append("--no-auto-ulog")
    elif args.auto_ulog:
        collector_command.append("--auto-ulog")

    collector_result = run_command(collector_command)
    log["collection_result"] = collector_result
    log["automatic_collection_successful"] = collector_result["ok"]
    log_event(
        log,
        "user_interrupt_collection_finished",
        ok=collector_result["ok"],
    )
    log["finished_utc"] = utc_now()
    sync_log(log_path, log)
    merge_mission_metadata(
        metadata_path,
        log_path,
        log,
        (
            "automatic_collection_complete"
            if collector_result["ok"]
            else "automatic_collection_pending_retry"
        ),
    )

    if collector_result["stdout"]:
        print(collector_result["stdout"])
    if not collector_result["ok"]:
        if collector_result["stderr"]:
            print(collector_result["stderr"], file=sys.stderr)
        print(
            "Collection needs a retry; no remote or archived mission data was deleted.",
            file=sys.stderr,
        )
    print(f"Ground mission folder: {mission_dir}")
    print(f"Orchestrator log: {log_path}")
    return 130 if collector_result["ok"] else 1


def _run_mission() -> int:
    global _interrupt_recovery_context

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mission-name", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--drone", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--drone-host", default="root@192.168.0.20")
    parser.add_argument("--code-root")
    duration_group = parser.add_mutually_exclusive_group(required=True)
    duration_group.add_argument(
        "--duration",
        dest="duration",
        type=float,
        help="Maximum recording/monitoring window in seconds.",
    )
    duration_group.add_argument(
        "--max-duration-s",
        dest="duration",
        type=float,
        help="Alias for --duration; the two options cannot be supplied together.",
    )
    parser.add_argument("--enable-audio", action="store_true")
    parser.add_argument("--enable-rosbag", action="store_true")
    parser.add_argument(
        "--enable-ground-rosbag",
        action="store_true",
        help="Record all visible ROS2 topics from the ground computer.",
    )
    parser.add_argument(
        "--ros-domain-id",
        type=int,
        default=3,
        help="ROS_DOMAIN_ID for ground-side ROS2 discovery (default: 3).",
    )
    parser.add_argument(
        "--ground-rosbag-all",
        action="store_true",
        help="Document that ground rosbag records all topics (the default).",
    )
    parser.add_argument(
        "--ground-rosbag-output-name",
        help="Ground bag output folder name; defaults to <mission_id>_ground_rosbag.",
    )
    parser.add_argument(
        "--px4-msgs-workspace",
        help="Optional px4_msgs setup.bash to source for ground ROS2 commands.",
    )
    parser.add_argument(
        "--rosbag-topics",
        nargs="+",
        help=(
            "ROS2 topics to record. Defaults to profile rosbag.topics, then the "
            "HALO PX4 topic set."
        ),
    )
    ulog_group = parser.add_mutually_exclusive_group()
    ulog_group.add_argument(
        "--no-auto-ulog",
        action="store_true",
        help="Save remote ULog candidates but do not auto-copy one after the mission.",
    )
    ulog_group.add_argument(
        "--auto-ulog",
        action="store_true",
        help="Explicitly allow automatic ULog copying, including for audio-only tests.",
    )
    parser.add_argument("--archive-root")
    parser.add_argument("--remote-sync-root", default="/home/root/halo_sync_test")
    parser.add_argument("--status-interval-s", default=2.0, type=float)
    parser.add_argument("--post-disarm-wait-s", default=10.0, type=float)
    parser.add_argument(
        "--mirror-interval-s",
        default=0.0,
        type=float,
        help="Periodically rsync the active drone mission folder; zero disables mirroring.",
    )
    args = parser.parse_args()

    if args.duration <= 0:
        parser.error("--duration must be greater than zero")
    if args.ros_domain_id < 0 or args.ros_domain_id > 232:
        parser.error("--ros-domain-id must be between 0 and 232")
    if args.ground_rosbag_output_name is not None and re.fullmatch(
        r"[A-Za-z0-9._-]+", args.ground_rosbag_output_name
    ) is None:
        parser.error(
            "--ground-rosbag-output-name must be a simple folder name "
            "containing only letters, numbers, periods, underscores, and hyphens"
        )
    if args.status_interval_s <= 0:
        parser.error("--status-interval-s must be greater than zero")
    if args.post_disarm_wait_s < 0 or args.mirror_interval_s < 0:
        parser.error("wait and mirror intervals cannot be negative")

    profile_path = Path(args.profile).expanduser().resolve()
    try:
        profile_topics = load_profile_rosbag_topics(profile_path)
    except (OSError, ValueError) as exc:
        parser.error(f"Could not read ROS bag topics from {profile_path}: {exc}")
    selected_topics = (
        list(args.rosbag_topics)
        if args.rosbag_topics is not None
        else (
            list(profile_topics)
            if profile_topics
            else list(DEFAULT_ROSBAG_TOPICS)
        )
    )
    args.rosbag_topics = normalize_rosbag_topics(selected_topics, parser)

    audio_only_mode = args.enable_audio and not args.enable_rosbag
    monitor_only_px4_ulog_mode = (
        not args.enable_audio
        and args.enable_rosbag
        and args.auto_ulog
    )
    effective_no_auto_ulog = args.no_auto_ulog or (
        audio_only_mode and not args.auto_ulog
    )
    if audio_only_mode and effective_no_auto_ulog and not args.no_auto_ulog:
        print(
            "Audio-only mode: automatic ULog copying is disabled by default. "
            "Use --auto-ulog to explicitly enable it."
        )

    script_dir = Path(__file__).resolve().parent
    creator_path = script_dir / "create_mission_archive.py"
    collector_path = script_dir / "collect_drone_data.py"
    agent_path = script_dir / "halo_drone_mission_agent.py"
    archive_root = (
        Path(args.archive_root).expanduser().resolve()
        if args.archive_root
        else Path.home() / "MIC_ARRAY_ROS" / "HALO_ARCHIVE"
    )

    creator_command = [
        sys.executable,
        str(creator_path),
        "--mission-name",
        args.mission_name,
        "--operator",
        args.operator,
        "--drone",
        str(Path(args.drone).expanduser().resolve()),
        "--profile",
        str(profile_path),
        "--archive-root",
        str(archive_root),
        "--initialize-drone",
        "--drone-host",
        args.drone_host,
        "--remote-sync-root",
        args.remote_sync_root,
    ]
    if args.code_root:
        creator_command.extend(
            ["--code-root", str(Path(args.code_root).expanduser().resolve())]
        )

    print("Creating the canonical mission archive and matching drone folder ...")
    creator_result = run_command(creator_command)
    if creator_result["stdout"]:
        print(creator_result["stdout"])
    if not creator_result["ok"]:
        print(creator_result["stderr"], file=sys.stderr)
        return 1

    mission_id = parse_mission_id(creator_result["stdout"])
    if mission_id is None:
        print("ERROR: Could not read mission_id from creator output.", file=sys.stderr)
        return 1

    mission_dir = archive_root / mission_id
    metadata_path = mission_dir / "metadata" / "mission_metadata.json"
    log_path = mission_dir / "metadata" / "ground_orchestrator_log.json"
    remote_mission_dir = (
        f"/{mission_id}"
        if args.remote_sync_root.rstrip("/") == ""
        else f"{args.remote_sync_root.rstrip('/')}/{mission_id}"
    )
    remote_agent = f"{remote_mission_dir}/metadata/halo_drone_mission_agent.py"
    local_session_dir = mission_dir / "drone_data" / "audio" / mission_id
    ground_rosbag_state: dict[str, Any] = {
        "process": None,
        "log_handle": None,
        "record": ground_rosbag_record_template(args, mission_id, mission_dir),
    }
    ground_rosbag_record = ground_rosbag_state["record"]

    log: dict[str, Any] = {
        "mission_id": mission_id,
        "mission_dir": str(mission_dir),
        "drone_host": args.drone_host,
        "remote_mission_dir": remote_mission_dir,
        "started_utc": utc_now(),
        "duration_s": args.duration,
        "max_duration_s": args.duration,
        "duration_semantics": "maximum_recording_and_monitoring_window",
        "audio_enabled": args.enable_audio,
        "rosbag_enabled": args.enable_rosbag,
        "rosbag_topics": list(args.rosbag_topics),
        "ground_rosbag": ground_rosbag_record,
        "ground_rosbag_enabled": args.enable_ground_rosbag,
        "ground_rosbag_start_attempted": ground_rosbag_record["start_attempted"],
        "ground_rosbag_started": ground_rosbag_record["started"],
        "ground_rosbag_output_path": ground_rosbag_record["output_path"],
        "ground_rosbag_command": ground_rosbag_record["command"],
        "ground_rosbag_ros_domain_id": args.ros_domain_id,
        "ground_rosbag_px4_msgs_setup_path": ground_rosbag_record["px4_msgs_setup_path"],
        "ground_rosbag_px4_msgs_workspace": ground_rosbag_record["px4_msgs_workspace"],
        "ground_rosbag_start_utc": ground_rosbag_record["start_utc"],
        "ground_rosbag_stop_utc": ground_rosbag_record["stop_utc"],
        "ground_rosbag_return_code": ground_rosbag_record["return_code"],
        "ground_rosbag_warnings": list(ground_rosbag_record["warnings"]),
        "ground_rosbag_errors": list(ground_rosbag_record["errors"]),
        "ground_rosbag_preflight_topics": list(ground_rosbag_record["preflight_topics"]),
        "ground_rosbag_preflight_fmu_topics": list(ground_rosbag_record["preflight_fmu_topics"]),
        "audio_only_mode": audio_only_mode,
        "monitor_only_px4_ulog_mode": monitor_only_px4_ulog_mode,
        "no_auto_ulog": effective_no_auto_ulog,
        "no_auto_ulog_requested": args.no_auto_ulog,
        "auto_ulog_requested": args.auto_ulog,
        "mirror_interval_s": args.mirror_interval_s,
        "creator_command": creator_result,
        "events": [],
        "warnings": [],
        "errors": [],
        "agent_preflight_check": None,
        "agent_startup_confirmed": False,
        "agent_startup_confirmation_failed": False,
        "automatic_collection_attempted": False,
        "automatic_collection_successful": None,
    }
    log_event(log, "archive_created")
    sync_log(log_path, log)
    _interrupt_recovery_context = {
        "args": args,
        "mission_id": mission_id,
        "mission_dir": mission_dir,
        "metadata_path": metadata_path,
        "log_path": log_path,
        "log": log,
        "collector_path": collector_path,
        "remote_mission_dir": remote_mission_dir,
        "agent_pid": None,
        "effective_no_auto_ulog": effective_no_auto_ulog,
        "ground_rosbag_state": ground_rosbag_state,
    }

    metadata = load_json(metadata_path)
    if not metadata.get("drone_initialization", {}).get("successful"):
        error = "Drone folder initialization did not complete successfully; agent was not started."
        add_unique(log["errors"], error)
        log_event(log, "drone_initialization_failed")
        log["finished_utc"] = utc_now()
        sync_log(log_path, log)
        merge_mission_metadata(metadata_path, log_path, log, "orchestrator_start_failed")
        print(f"ERROR: {error}", file=sys.stderr)
        return 1

    print(f"Mission ID: {mission_id}")
    print("Uploading the drone mission agent ...")
    preflight_failed = False
    agent_pid = None
    upload_result = upload_agent(args.drone_host, agent_path, remote_agent)
    log_event(log, "agent_upload", result=upload_result)
    if not upload_result["ok"]:
        error = f"Drone agent upload failed: {failure_detail(upload_result)}"
        add_unique(log["errors"], error)
    else:
        print("Checking the uploaded agent with the drone's Python interpreter ...")
        preflight_result = check_remote_agent_compatibility(
            args.drone_host, remote_agent
        )
        log_event(log, "agent_preflight_check", result=preflight_result)
        log["agent_preflight_check"] = preflight_result
        if not preflight_result["ok"]:
            preflight_failed = True
            add_unique(
                log["errors"],
                "Drone agent remote syntax/help check failed: "
                f"{failure_detail(preflight_result)}",
            )
            print(
                "ERROR: Drone agent compatibility preflight failed; it was not launched.",
                file=sys.stderr,
            )
        else:
            print("Remote agent compatibility check passed. Launching the agent ...")
            launch_result, agent_pid = launch_agent(
                args, mission_id, remote_mission_dir, remote_agent
            )
            log_event(log, "agent_launch", result=launch_result, agent_pid=agent_pid)
            if not launch_result["ok"]:
                add_unique(
                    log["errors"],
                    f"Drone agent launch failed: {failure_detail(launch_result)}",
                )
                agent_pid = None

    log["remote_agent_pid"] = agent_pid
    _interrupt_recovery_context["agent_pid"] = agent_pid
    sync_log(log_path, log)
    merge_mission_metadata(metadata_path, log_path, log, "mission_agent_starting")

    final_data: dict[str, Any] | None = None
    interrupted = False
    connection_lost = False
    startup_confirmation_failed = False
    startup_confirmed = False
    consecutive_ssh_failures = 0
    last_mirror = 0.0
    last_reported_state: tuple[Any, ...] | None = None

    if agent_pid is not None:
        print("Waiting for the drone agent to prove it started successfully ...")
        startup_started = time.monotonic()
        startup_deadline = startup_started + 5.0
        startup_snapshot_seen = False
        last_startup_error = None
        last_startup_snapshot = None
        while time.monotonic() < startup_deadline:
            snapshot = pull_agent_snapshot(
                args.drone_host, remote_mission_dir, agent_pid
            )
            last_startup_snapshot = snapshot
            if not snapshot["ok"]:
                last_startup_error = snapshot.get("error")
            else:
                data = persist_agent_snapshot(mission_dir, snapshot, log)
                if (
                    snapshot.get("kind") in {"start", "status", "final"}
                    and isinstance(data, dict)
                ):
                    startup_snapshot_seen = True
                    log_event(log, "agent_startup_snapshot", kind=snapshot["kind"])
                    if snapshot["kind"] == "final":
                        final_data = data
                        break
                if snapshot.get("agent_alive") is False:
                    active_status = (
                        isinstance(data, dict)
                        and data.get("termination_reason") in (None, "")
                        and data.get("phase") not in ("finalized", "finalizing")
                    )
                    if monitor_only_px4_ulog_mode and active_status:
                        add_unique(
                            log["warnings"],
                            "Agent PID was not reported alive during startup, but its active monitor-only status remains; continuing the PX4 ULog window.",
                        )
                        startup_confirmed = True
                        log_event(
                            log,
                            "agent_liveness_unconfirmed_startup_monitor_window_continued",
                        )
                        break
                    last_startup_error = "agent process exited before startup confirmation"
                    break
                if (
                    startup_snapshot_seen
                    and snapshot.get("agent_alive") is True
                    and time.monotonic() - startup_started >= 1.0
                ):
                    startup_confirmed = True
                    break
            sync_log(log_path, log)
            time.sleep(min(args.status_interval_s, 0.5))

        startup_data = (
            last_startup_snapshot.get("data")
            if isinstance(last_startup_snapshot, dict)
            else None
        )
        if not isinstance(startup_data, dict):
            startup_data = {}
        startup_rosbag_active = startup_data.get("rosbag_process_running")
        startup_rosbag_record = startup_data.get("rosbag")
        if not isinstance(startup_rosbag_record, dict):
            startup_rosbag_record = {}
        if not isinstance(startup_rosbag_active, bool):
            startup_rosbag_active = bool(startup_rosbag_record.get("started"))
        startup_flight_available = bool(
            startup_data.get("flight_state_monitoring_available", False)
        )
        if final_data is not None:
            print("Drone agent finalized during startup; collection will begin.")
        elif startup_confirmed:
            if args.enable_ground_rosbag:
                start_ground_rosbag(
                    args, mission_dir, log, ground_rosbag_state
                )
                ground_rosbag_record = ground_rosbag_state["record"]
                sync_log(log_path, log)
                merge_mission_metadata(
                    metadata_path,
                    log_path,
                    log,
                    (
                        "ground_rosbag_started"
                        if ground_rosbag_record["started"]
                        else "ground_rosbag_start_attempted"
                    ),
                )
            print(
                "Mission agent startup confirmed. "
                "Waiting for recording/finalization status."
            )
            print("This tool does not arm the drone; arm and disarm manually only when safe.")
            print("Mission agent is running.")
            if args.enable_ground_rosbag and ground_rosbag_record["started"] and ground_rosbag_record["active"]:
                print("Ground ROS bag recording started.")
                print("Output folder: {0}".format(ground_rosbag_record["output_path"]))
            elif args.enable_ground_rosbag:
                print("Ground ROS bag recording was attempted but is not active.")
                print("Output folder: {0}".format(ground_rosbag_record["output_path"]))
            print("Audio enabled: {0}".format(str(bool(args.enable_audio)).lower()))
            print("ROS bag requested: {0}".format(str(bool(args.enable_rosbag)).lower()))
            print("ROS bag active: {0}".format(str(bool(startup_rosbag_active)).lower()))
            print("Flight state available: {0}".format(str(startup_flight_available).lower()))
            if monitor_only_px4_ulog_mode:
                if not startup_rosbag_active:
                    print("ROS bag unavailable; continuing monitor-only window for PX4 ULog collection.")
                print("Continuing monitor-only window for PX4 ULog collection.")
            print("ROS bag capture is active or attempted when --enable-rosbag is set.")
            print("--duration/--max-duration-s is the maximum recording window.")
            print(
                "If disarm is detected, collection may happen earlier after "
                "the post-disarm wait."
            )
            print(
                "If flight-state detection is unavailable, duration is the "
                "fallback stop condition."
            )
            print("Partner may arm/offboard the drone when safe.")
            log_event(log, "agent_startup_confirmed", minimum_survival_s=1.0)
        else:
            startup_confirmation_failed = True
            detail = (
                f" Last result: {last_startup_error}."
                if last_startup_error
                else ""
            )
            error = (
                "Drone agent did not remain alive and produce drone_agent_start.json "
                f"or drone_agent_status.json within 5 seconds.{detail}"
            )
            add_unique(log["errors"], error)
            log_event(log, "agent_startup_confirmation_failed", error=error)
            print(f"ERROR: {error}", file=sys.stderr)
            if (
                last_startup_snapshot
                and last_startup_snapshot.get("agent_alive") is True
            ):
                stop_result = stop_remote_agent(args.drone_host, agent_pid)
                log_event(log, "agent_stop_after_startup_failure", result=stop_result)
        log["agent_startup_confirmed"] = startup_confirmed
        log["agent_startup_confirmation_failed"] = startup_confirmation_failed
        sync_log(log_path, log)
        merge_mission_metadata(
            metadata_path,
            log_path,
            log,
            (
                "mission_agent_running"
                if startup_confirmed
                else "mission_agent_start_failed"
            ),
        )

    if agent_pid is not None and startup_confirmed:
        deadline = time.monotonic() + args.duration + args.post_disarm_wait_s + 90.0
        try:
            while True:
                snapshot = pull_agent_snapshot(
                    args.drone_host, remote_mission_dir, agent_pid
                )
                if not snapshot["ok"]:
                    consecutive_ssh_failures += 1
                    log_event(
                        log,
                        "status_pull_failed",
                        consecutive_failures=consecutive_ssh_failures,
                        error=snapshot.get("error"),
                    )
                    if consecutive_ssh_failures >= 3:
                        connection_lost = True
                        warning = (
                            "SSH status polling failed three consecutive times; local and drone data "
                            "were left in place and automatic collection will still be attempted."
                        )
                        add_unique(log["warnings"], warning)
                        if monitor_only_px4_ulog_mode:
                            log_event(
                                log,
                                "ssh_polling_unavailable_monitor_window_continued",
                            )
                            consecutive_ssh_failures = 0
                        else:
                            break
                else:
                    consecutive_ssh_failures = 0
                    data = persist_agent_snapshot(mission_dir, snapshot, log)
                    if isinstance(data, dict):
                        reported_state = (
                            data.get("phase"),
                            data.get("audio_process_running"),
                            data.get("rosbag_process_running"),
                            data.get("current_detected_flight_state"),
                        )
                        if reported_state != last_reported_state:
                            print(
                                "Status: "
                                f"phase={reported_state[0]}, audio={reported_state[1]}, "
                                f"rosbag={reported_state[2]}, flight={reported_state[3]}"
                            )
                            last_reported_state = reported_state
                        log_event(
                            log,
                            "agent_snapshot",
                            kind=snapshot["kind"],
                            phase=data.get("phase"),
                            flight_state=data.get("current_detected_flight_state"),
                        )
                    if snapshot["kind"] == "final" and isinstance(data, dict):
                        final_data = data
                        break
                    if snapshot["agent_alive"] is False:
                        active_status = (
                            isinstance(data, dict)
                            and data.get("termination_reason") in (None, "")
                            and data.get("phase") not in ("finalized", "finalizing")
                        )
                        if monitor_only_px4_ulog_mode and active_status:
                            add_unique(
                                log["warnings"],
                                "Agent PID was not reported alive, but its active monitor-only status remains; continuing until the PX4 ULog window ends.",
                            )
                            log_event(
                                log,
                                "agent_liveness_unconfirmed_monitor_window_continued",
                            )
                        else:
                            add_unique(
                                log["warnings"],
                                "Drone agent exited without a readable final status; collection was triggered.",
                            )
                            break

                if args.mirror_interval_s > 0:
                    now_monotonic = time.monotonic()
                    if now_monotonic - last_mirror >= args.mirror_interval_s:
                        mirror_result = mirror_remote_mission(
                            args.drone_host, remote_mission_dir, local_session_dir
                        )
                        log_event(log, "periodic_mirror", result=mirror_result)
                        if not mirror_result["ok"]:
                            add_unique(
                                log["warnings"],
                                f"Periodic mission mirror failed: {failure_detail(mirror_result)}",
                            )
                        last_mirror = now_monotonic

                sync_log(log_path, log)
                if time.monotonic() >= deadline:
                    add_unique(log["warnings"], "Ground orchestrator wait deadline expired; agent stop was requested.")
                    stop_result = stop_remote_agent(args.drone_host, agent_pid)
                    log_event(log, "agent_stop_after_ground_timeout", result=stop_result)
                    break
                time.sleep(args.status_interval_s)
        except KeyboardInterrupt:
            interrupted = True
            add_unique(log["warnings"], "Ground orchestrator interrupted by the user; final collection was requested.")
            stop_ground_rosbag(
                ground_rosbag_state, log, "user_interrupt_collection_attempted"
            )
            print("\nInterrupt received. Asking the drone agent to finalize before collection ...")
            stop_result = stop_remote_agent(args.drone_host, agent_pid)
            log_event(log, "user_interrupt_agent_stop", result=stop_result)
            if not stop_result["ok"]:
                add_unique(log["warnings"], f"Could not signal drone agent: {failure_detail(stop_result)}")

            interrupt_deadline = time.monotonic() + args.post_disarm_wait_s + 20.0
            while time.monotonic() < interrupt_deadline:
                snapshot = pull_agent_snapshot(args.drone_host, remote_mission_dir, agent_pid)
                if snapshot["ok"] and snapshot["kind"] == "final":
                    final_data = snapshot.get("data")
                    if isinstance(final_data, dict):
                        save_json_atomic(
                            mission_dir / "metadata" / "drone_agent_final.json",
                            final_data,
                        )
                    break
                if snapshot["ok"] and snapshot["agent_alive"] is False:
                    break
                time.sleep(min(args.status_interval_s, 2.0))

    if interrupted:
        if final_data:
            log["drone_agent_reported_termination_reason"] = final_data.get(
                "termination_reason"
            )
        termination_reason = "user_interrupt_collection_attempted"
    elif final_data:
        termination_reason = str(
            final_data.get("termination_reason") or "drone_agent_complete"
        )
    elif connection_lost:
        termination_reason = "ssh_connection_lost_collection_pending"
    elif preflight_failed:
        termination_reason = "drone_agent_preflight_check_failed"
    elif startup_confirmation_failed:
        termination_reason = "drone_agent_startup_confirmation_failed"
    elif agent_pid is None:
        termination_reason = "drone_agent_start_failed"
    else:
        termination_reason = "drone_agent_exit_without_final_status"
    log["agent_termination_reason"] = termination_reason
    mission_start_failed = (
        preflight_failed or startup_confirmation_failed or agent_pid is None
    )

    stop_ground_rosbag(ground_rosbag_state, log, termination_reason)
    sync_log(log_path, log)
    merge_mission_metadata(
        metadata_path, log_path, log, "mission_collection_starting"
    )
    print("Starting automatic mission collection ...")
    collector_command = [
        sys.executable,
        str(collector_path),
        "--mission-dir",
        str(mission_dir),
        "--drone-host",
        args.drone_host,
        "--remote-sync-root",
        args.remote_sync_root,
        "--termination-reason",
        termination_reason,
        "--notes",
        "Automatic collection by run_halo_mission.py",
    ]
    if effective_no_auto_ulog or mission_start_failed:
        collector_command.append("--no-auto-ulog")
    elif args.auto_ulog:
        collector_command.append("--auto-ulog")
    log["automatic_collection_attempted"] = True
    collector_result = run_command(collector_command)
    log["collection_result"] = collector_result
    log["automatic_collection_successful"] = collector_result["ok"]
    log_event(log, "automatic_collection_finished", ok=collector_result["ok"])
    if collector_result["stdout"]:
        print(collector_result["stdout"])
    if not collector_result["ok"]:
        warning = (
            f"Automatic collection did not complete: {failure_detail(collector_result)}. "
            f"Rerun collect_drone_data.py later with mission_id {mission_id}."
        )
        add_unique(log["warnings"], warning)
        if collector_result["stderr"]:
            print(collector_result["stderr"], file=sys.stderr)

    log["finished_utc"] = utc_now()
    sync_log(log_path, log)
    final_status_name = (
        "automatic_collection_complete_after_mission_start_failure"
        if collector_result["ok"] and mission_start_failed
        else (
            "automatic_collection_complete"
            if collector_result["ok"]
            else "automatic_collection_pending_retry"
        )
    )
    merge_mission_metadata(metadata_path, log_path, log, final_status_name)

    print(f"Ground mission folder: {mission_dir}")
    print(f"Orchestrator log: {log_path}")
    if not collector_result["ok"]:
        print("No data was deleted. The same mission_id can be collected again later.")
        return 1
    if mission_start_failed:
        return 1
    return 130 if interrupted else 0


def main() -> int:
    try:
        return _run_mission()
    except KeyboardInterrupt:
        return recover_after_unhandled_interrupt()


if __name__ == "__main__":
    raise SystemExit(main())
