#!/usr/bin/env python3

"""Create, run, monitor, and automatically collect one HALO mission."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
        "audio_enabled": log.get("audio_enabled"),
        "rosbag_enabled": log.get("rosbag_enabled"),
        "no_auto_ulog": log.get("no_auto_ulog"),
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mission-name", required=True)
    parser.add_argument("--operator", required=True)
    parser.add_argument("--drone", required=True)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--drone-host", default="root@192.168.0.20")
    parser.add_argument("--code-root")
    parser.add_argument("--duration", required=True, type=float)
    parser.add_argument("--enable-audio", action="store_true")
    parser.add_argument("--enable-rosbag", action="store_true")
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
    if args.status_interval_s <= 0:
        parser.error("--status-interval-s must be greater than zero")
    if args.post_disarm_wait_s < 0 or args.mirror_interval_s < 0:
        parser.error("wait and mirror intervals cannot be negative")

    audio_only_mode = args.enable_audio and not args.enable_rosbag
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
        str(Path(args.profile).expanduser().resolve()),
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

    log: dict[str, Any] = {
        "mission_id": mission_id,
        "mission_dir": str(mission_dir),
        "drone_host": args.drone_host,
        "remote_mission_dir": remote_mission_dir,
        "started_utc": utc_now(),
        "duration_s": args.duration,
        "audio_enabled": args.enable_audio,
        "rosbag_enabled": args.enable_rosbag,
        "audio_only_mode": audio_only_mode,
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

        if final_data is not None:
            print("Drone agent finalized during startup; collection will begin.")
        elif startup_confirmed:
            print(
                "Mission agent startup confirmed. "
                "Waiting for recording/finalization status."
            )
            print("This tool does not arm the drone; arm and disarm manually only when safe.")
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

    if final_data:
        termination_reason = str(final_data.get("termination_reason") or "drone_agent_complete")
    elif interrupted:
        termination_reason = "ground_orchestrator_interrupted"
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


if __name__ == "__main__":
    raise SystemExit(main())
