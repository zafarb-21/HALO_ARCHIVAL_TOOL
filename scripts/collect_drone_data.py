#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def run_cmd(cmd: list[str], timeout: float | None = None) -> dict:
    """Run a collection command and preserve failures in a JSON-safe result."""
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
        )
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

    return {
        "command": shlex.join(cmd),
        "return_code": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "ok": result.returncode == 0,
    }


def failure_detail(result: dict) -> str:
    detail = result["stderr"] or result["stdout"]
    if detail:
        return detail.splitlines()[0]
    return f"exit code {result['return_code']}"


def remote_command(command: str) -> str:
    return f"LC_ALL=C LANG=C sh -c {shlex.quote(command)}"


def ssh_command_args(drone_host: str, command: str, bounded: bool) -> list[str]:
    options = (
        ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10"] if bounded else []
    )
    return ["ssh"] + options + [drone_host, remote_command(command)]


def rsync_command_args(bounded: bool) -> list[str]:
    command = ["rsync", "-av"]
    if bounded:
        command.extend(
            ["-e", "ssh -o BatchMode=yes -o ConnectTimeout=10"]
        )
    command.append("--rsync-path=LC_ALL=C LANG=C rsync")
    return command


def check_ssh_connection(drone_host: str, bounded: bool = False) -> dict:
    print(f"Checking SSH connection to {drone_host} ...")
    result = run_cmd(
        ssh_command_args(drone_host, "hostname && date -u", bounded),
        timeout=20.0 if bounded else None,
    )
    if not result["ok"]:
        raise RuntimeError(
            f"SSH connection check failed for {drone_host}: "
            f"{failure_detail(result)}. Check network access, credentials, and "
            "SSH key/password login."
        )

    remote_name = result["stdout"].splitlines()[0] if result["stdout"] else drone_host
    print(f"SSH connection OK: {remote_name}")
    return result


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data: dict) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def update_swarm_collection_manifest(
    path: Path, mission_id: str, drone_id: str, record: dict
) -> None:
    """Merge one collector result without racing parallel drone collectors."""
    lock_path = path.with_name("." + path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            swarm_manifest = load_json(path)
            if swarm_manifest.get("mission_id") not in (None, mission_id):
                raise ValueError(
                    f"Swarm manifest mission_id does not match {mission_id}"
                )
            swarm_manifest.setdefault("schema_version", 1)
            swarm_manifest["mission_id"] = mission_id
            swarm_manifest["updated_utc"] = datetime.now(timezone.utc).isoformat()
            drone_records = swarm_manifest.setdefault("drones", {})
            previous = drone_records.get(drone_id, {})
            previous.update(record)
            drone_records[drone_id] = previous
            statuses = [
                item.get("collection_status")
                for item in drone_records.values()
                if isinstance(item, dict)
            ]
            swarm_manifest["collection_summary"] = {
                "drone_count": len(drone_records),
                "complete": statuses.count("complete"),
                "complete_with_warnings": statuses.count(
                    "complete_with_warnings"
                ),
                "failed": statuses.count("failed"),
                "pending": sum(
                    status not in {
                        "complete",
                        "complete_with_warnings",
                        "failed",
                    }
                    for status in statuses
                ),
            }
            save_json(path, swarm_manifest)
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def validate_identifier(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value in {".", ".."}
        or re.fullmatch(r"[A-Za-z0-9._-]+", value) is None
    ):
        raise ValueError(
            f"{label} must be a single folder name containing only letters, "
            "numbers, periods, underscores, and hyphens."
        )
    return value


def validate_remote_sync_root(value: str) -> str:
    if re.fullmatch(r"/[A-Za-z0-9._/-]*", value) is None:
        raise ValueError(
            "remote sync root must be an absolute path containing only letters, "
            "numbers, periods, underscores, hyphens, and slashes."
        )
    return value.rstrip("/") or "/"


def remote_mission_folder(remote_sync_root: str, folder_id: str) -> str:
    if remote_sync_root == "/":
        return f"/{folder_id}"
    return f"{remote_sync_root}/{folder_id}"


def parse_ulog_candidates(output: str) -> list[dict]:
    """Parse find -printf output while preserving paths that contain spaces."""
    candidates = []
    for line in output.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) != 2:
            continue
        try:
            modified_epoch = float(parts[0])
        except ValueError:
            continue
        candidates.append(
            {"modified_epoch": modified_epoch, "remote_path": parts[1]}
        )
    return candidates


def parse_utc_epoch(value: object) -> float | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        timestamp = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.timestamp()


def mission_start_record(metadata: dict) -> dict:
    ground_orchestrator = metadata.get("ground_orchestrator", {})
    for source_name, value in (
        (
            "ground_orchestrator.start_at_utc",
            ground_orchestrator.get("start_at_utc"),
        ),
        (
            "ground_orchestrator.started_utc",
            ground_orchestrator.get("started_utc"),
        ),
        ("start_at_utc", metadata.get("start_at_utc")),
        ("created_utc", metadata.get("created_utc")),
    ):
        epoch = parse_utc_epoch(value)
        if epoch is not None:
            return {
                "source": source_name,
                "timestamp_utc": value,
                "epoch": epoch,
            }
    return {"source": None, "timestamp_utc": None, "epoch": None}


def capture_drone_code_state(
    drone_host: str, bounded: bool = False
) -> dict:
    """Capture a best-effort snapshot of relevant software and services on the drone."""
    remote_commands = {
        "hostname": "hostname",
        "date": "date",
        "uname_a": "uname -a",
        "voxl_inspect_services_filtered": (
            "if command -v voxl-inspect-services >/dev/null 2>&1; then "
            "voxl-inspect-services | grep -Ei 'px4|mavlink|microdds'; "
            "else echo 'voxl-inspect-services is not available' >&2; exit 127; fi"
        ),
        "voxl_px4_service_status": (
            "if command -v systemctl >/dev/null 2>&1; then "
            "systemctl status voxl-px4 --no-pager; "
            "else echo 'systemctl is not available' >&2; exit 127; fi"
        ),
        "voxl_microdds_agent_service_status": (
            "if command -v systemctl >/dev/null 2>&1; then "
            "systemctl status voxl-microdds-agent --no-pager; "
            "else echo 'systemctl is not available' >&2; exit 127; fi"
        ),
    }

    snapshot = {
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "drone_host": drone_host,
        "commands": {},
    }
    for name, remote_cmd in remote_commands.items():
        snapshot["commands"][name] = run_cmd(
            ssh_command_args(drone_host, remote_cmd, bounded),
            timeout=20.0 if bounded else None,
        )

    return snapshot


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect drone-side logs, ReSpeaker session files, and PX4 ULogs into a HALO mission archive."
    )

    parser.add_argument("--mission-dir", required=True, help="Mission archive folder.")
    parser.add_argument(
        "--drone-id",
        help="Drone identifier for swarm collection, for example D0012.",
    )
    parser.add_argument(
        "--drone-session-id",
        help="Swarm session ID, normally <mission_id>__<drone_id>.",
    )
    parser.add_argument(
        "--local-drone-dir",
        help="Ground drone folder, normally <mission-dir>/drones/<drone_id>.",
    )
    parser.add_argument(
        "--drone-host",
        default="root@192.168.0.20",
        help="SSH target, e.g. root@192.168.0.20.",
    )
    parser.add_argument(
        "--session-id",
        help="Optional remote session folder override; defaults to metadata mission_id.",
    )
    parser.add_argument(
        "--ulog",
        action="append",
        default=[],
        help="Remote PX4 ULog path. Can be repeated for multiple ULogs.",
    )
    ulog_group = parser.add_mutually_exclusive_group()
    ulog_group.add_argument(
        "--no-auto-ulog",
        action="store_true",
        help=(
            "Do not automatically copy an eligible ULog when --ulog is omitted; "
            "the candidate list is still saved."
        ),
    )
    ulog_group.add_argument(
        "--auto-ulog",
        action="store_true",
        help="Explicitly enable automatic newest-ULog selection.",
    )
    parser.add_argument(
        "--remote-sync-root",
        default="/home/root/halo_sync_test",
        help="Remote folder containing ReSpeaker sync sessions.",
    )
    parser.add_argument(
        "--termination-reason",
        help="Why the mission ended, for example completed, aborted, or safety stop.",
    )
    parser.add_argument(
        "--notes",
        help="Free-form operator notes to store with this collection.",
    )
    parser.add_argument(
        "--skip-drone-code-state",
        action="store_true",
        help="Skip the optional SSH snapshot of drone software and service state.",
    )
    parser.add_argument(
        "--skip-ssh-check",
        action="store_true",
        help="Skip the SSH connection preflight (advanced/offline validation use).",
    )

    args = parser.parse_args()

    mission_dir = Path(args.mission_dir).expanduser().resolve()
    if not mission_dir.is_dir():
        raise FileNotFoundError(f"Mission directory does not exist: {mission_dir}")

    metadata_dir = mission_dir / "metadata"
    metadata_path = metadata_dir / "mission_metadata.json"
    metadata = load_json(metadata_path)

    try:
        mission_id = validate_identifier(metadata.get("mission_id"), "mission_id")
    except ValueError as exc:
        parser.error(f"{metadata_path}: {exc}")

    if mission_dir.name != mission_id:
        parser.error(
            "Ground mission folder name does not match metadata mission_id: "
            f"{mission_dir.name!r} != {mission_id!r}"
        )

    swarm_mode = any(
        value is not None
        for value in (
            args.drone_id,
            args.drone_session_id,
            args.local_drone_dir,
        )
    )
    if swarm_mode and not all(
        value is not None
        for value in (
            args.drone_id,
            args.drone_session_id,
            args.local_drone_dir,
        )
    ):
        parser.error(
            "--drone-id, --drone-session-id, and --local-drone-dir must be "
            "supplied together in swarm mode"
        )
    if (
        args.session_id is not None
        and args.drone_session_id is not None
        and args.session_id != args.drone_session_id
    ):
        parser.error("--session-id and --drone-session-id disagree")

    try:
        drone_id = (
            validate_identifier(args.drone_id, "drone_id")
            if args.drone_id is not None
            else None
        )
        session_id = validate_identifier(
            args.drone_session_id
            if args.drone_session_id is not None
            else (
                args.session_id if args.session_id is not None else mission_id
            ),
            "drone_session_id" if swarm_mode else "session_id",
        )
        remote_sync_root = validate_remote_sync_root(args.remote_sync_root)
    except ValueError as exc:
        parser.error(str(exc))

    if swarm_mode:
        expected_session_id = f"{mission_id}__{drone_id}"
        if session_id != expected_session_id:
            parser.error(
                "Swarm drone_session_id must equal "
                f"{expected_session_id!r}, got {session_id!r}"
            )
        local_drone_dir = Path(args.local_drone_dir).expanduser().resolve()
        expected_local_drone_dir = mission_dir / "drones" / str(drone_id)
        if local_drone_dir != expected_local_drone_dir:
            parser.error(
                "--local-drone-dir must be the canonical mission drone folder: "
                f"{expected_local_drone_dir}"
            )
    else:
        local_drone_dir = mission_dir

    canonical_remote_folder = remote_mission_folder(remote_sync_root, mission_id)
    remote_drone_folder = remote_mission_folder(remote_sync_root, session_id)
    session_override_warning = None
    if session_id != mission_id and not swarm_mode:
        session_override_warning = (
            f"session_id {session_id!r} does not match canonical mission_id "
            f"{mission_id!r}; the override is used only for the remote source folder."
        )
        print(f"WARNING: {session_override_warning}")

    preflight_warning = None
    if args.skip_ssh_check:
        ssh_connection_check = {
            "performed": False,
            "drone_host": args.drone_host,
            "reason": "Skipped because --skip-ssh-check was supplied.",
        }
    else:
        try:
            ssh_preflight = check_ssh_connection(
                args.drone_host, bounded=swarm_mode
            )
        except RuntimeError as exc:
            if not swarm_mode:
                parser.exit(1, f"ERROR: {exc}\n")
            preflight_warning = str(exc)
            print(f"WARNING: {preflight_warning}")
            ssh_connection_check = {
                "performed": True,
                "drone_host": args.drone_host,
                "ok": False,
                "error": preflight_warning,
            }
        else:
            ssh_connection_check = {"performed": True, **ssh_preflight}

    drone_data_dir = local_drone_dir / "drone_data"
    per_drone_metadata_dir = (
        local_drone_dir / "metadata" if swarm_mode else metadata_dir
    )

    px4_dir = drone_data_dir / "px4_logs"
    audio_dir = drone_data_dir / "audio"
    ros_bags_dir = drone_data_dir / "ros_bags"
    status_dir = drone_data_dir / "status_logs"

    for folder in [
        metadata_dir,
        per_drone_metadata_dir,
        px4_dir,
        audio_dir,
        ros_bags_dir,
        status_dir,
    ]:
        folder.mkdir(parents=True, exist_ok=True)

    collection_started = datetime.now(timezone.utc)

    remote_session = f"{args.drone_host}:{remote_drone_folder}/"
    local_session = audio_dir / (session_id if swarm_mode else mission_id)

    mission_start = mission_start_record(metadata)

    manifest = {
        "collection_started_utc": collection_started.isoformat(),
        "mission_id": mission_id,
        "drone_id": drone_id,
        "drone_session_id": session_id,
        "session_id": session_id,
        "session_id_matches_mission_id": session_id == mission_id,
        "swarm_mode": swarm_mode,
        "mission_dir": str(mission_dir),
        "local_ground_mission_folder": str(mission_dir),
        "local_drone_dir": str(local_drone_dir),
        "local_audio_mission_folder": str(local_session),
        "drone_host": args.drone_host,
        "remote_sync_root": remote_sync_root,
        "canonical_remote_drone_mission_folder": canonical_remote_folder,
        "remote_drone_mission_folder": remote_drone_folder,
        "remote_drone_mission_source": remote_session,
        "termination_reason": args.termination_reason,
        "notes": args.notes,
        "ssh_connection_check": ssh_connection_check,
        "actions": [],
        "collected_files": {
            "respeaker_session": None,
            "respeaker_audio": {
                "path": str(local_session / "audio" / "respeaker_6ch.wav"),
                "exists": False,
                "size_bytes": None,
            },
            "ros2_bags": {
                "source_path": str(local_session / "bags"),
                "path": str(
                    ros_bags_dir if swarm_mode else local_session / "bags"
                ),
                "exists": False,
                "entries": [],
            },
            "px4_ulogs": [],
            "status_logs": {
                "path": str(status_dir),
                "exists": False,
                "entries": [],
            },
            "remote_status_snapshot": None,
            "drone_code_state": None,
        },
        "ulog_discovery": {
            "explicit_paths": list(args.ulog),
            "automatic_search_performed": False,
            "auto_copy_disabled": args.no_auto_ulog,
            "candidate_file": None,
            "candidates": [],
            "mission_start": mission_start,
            "eligible_candidates": [],
            "selection_policy": (
                "prefer_newest_after_mission_start_else_newest_candidate"
                if swarm_mode
                else "newest_candidate_not_older_than_mission_start"
            ),
            "selected_auto_ulog": None,
            "selected_auto_ulog_reason": None,
        },
        "warnings": [
            warning
            for warning in (session_override_warning, preflight_warning)
            if warning
        ],
        "errors": [],
    }
    # 1. Copy the complete drone mission folder. The local folder remains canonical.
    local_session.mkdir(parents=True, exist_ok=True)

    result = run_cmd(
        rsync_command_args(swarm_mode)
        + [remote_session, str(local_session) + "/"],
        timeout=180.0 if swarm_mode else None,
    )
    manifest["actions"].append(result)

    if result["ok"]:
        manifest["collected_files"]["respeaker_session"] = str(local_session)
    else:
        manifest["errors"].append(
            "Failed to copy ReSpeaker/session folder "
            f"{remote_session}: {failure_detail(result)}"
        )

    expected_audio_path = local_session / "audio" / "respeaker_6ch.wav"
    audio_record = manifest["collected_files"]["respeaker_audio"]
    if expected_audio_path.is_file():
        audio_record["exists"] = True
        audio_record["size_bytes"] = expected_audio_path.stat().st_size
    else:
        manifest["warnings"].append(
            "Expected ReSpeaker audio was not found after mission-folder copy: "
            f"{expected_audio_path}"
        )

    bags_path = local_session / "bags"
    collected_bags_path = bags_path
    if swarm_mode and bags_path.is_dir():
        bag_copy_result = run_cmd(
            ["rsync", "-a", str(bags_path) + "/", str(ros_bags_dir) + "/"]
        )
        manifest["actions"].append(bag_copy_result)
        if bag_copy_result["ok"]:
            collected_bags_path = ros_bags_dir
        else:
            manifest["errors"].append(
                "Failed to place ROS bags in the drone-specific ros_bags folder: "
                f"{failure_detail(bag_copy_result)}"
            )
    bag_entries = (
        sorted(
            str(path)
            for path in collected_bags_path.rglob("*")
            if path.is_file()
        )
        if collected_bags_path.is_dir()
        else []
    )
    manifest["collected_files"]["ros2_bags"]["path"] = str(
        collected_bags_path
    )
    manifest["collected_files"]["ros2_bags"]["exists"] = bool(bag_entries)
    manifest["collected_files"]["ros2_bags"]["entries"] = bag_entries

    source_status_dir = local_session / "status_logs"
    if swarm_mode and source_status_dir.is_dir():
        status_copy_result = run_cmd(
            ["rsync", "-a", str(source_status_dir) + "/", str(status_dir) + "/"]
        )
        manifest["actions"].append(status_copy_result)
        if not status_copy_result["ok"]:
            manifest["errors"].append(
                "Failed to place agent logs in the drone-specific status_logs "
                f"folder: {failure_detail(status_copy_result)}"
            )
    status_entries = sorted(
        str(path) for path in status_dir.rglob("*") if path.is_file()
    )
    manifest["collected_files"]["status_logs"]["exists"] = bool(status_entries)
    manifest["collected_files"]["status_logs"]["entries"] = status_entries

    # 2. Always save recent remote ULog candidates. Explicit paths still take
    # precedence; automatic selection is limited to files from this mission window.
    ulog_paths = list(args.ulog)
    ulog_candidates_path = (
        per_drone_metadata_dir / f"{session_id}_ulog_candidates_remote.txt"
        if swarm_mode
        else metadata_dir / "ulog_candidates_remote.txt"
    )
    manifest["ulog_discovery"]["automatic_search_performed"] = True
    discovery_command = (
        "find /data/px4/log -name '*.ulg' -printf '%T@ %p\\n' "
        "| sort -n | tail -10"
    )
    discovery_result = run_cmd(
        ssh_command_args(args.drone_host, discovery_command, swarm_mode),
        timeout=30.0 if swarm_mode else None,
    )
    manifest["actions"].append(discovery_result)
    ulog_candidates_path.write_text(
        discovery_result["stdout"] + ("\n" if discovery_result["stdout"] else ""),
        encoding="utf-8",
    )
    candidates = parse_ulog_candidates(discovery_result["stdout"])
    manifest["ulog_discovery"]["candidate_file"] = str(ulog_candidates_path)
    manifest["ulog_discovery"]["candidates"] = candidates

    mission_start_epoch = mission_start["epoch"]
    if mission_start_epoch is None:
        eligible_candidates = list(candidates)
    else:
        eligible_candidates = [
            candidate
            for candidate in candidates
            if candidate["modified_epoch"] >= mission_start_epoch
        ]
    manifest["ulog_discovery"]["eligible_candidates"] = eligible_candidates

    if not discovery_result["ok"]:
        manifest["warnings"].append(
            "Automatic ULog search failed: "
            f"{failure_detail(discovery_result)}"
        )
    elif not candidates:
        manifest["warnings"].append(
            "Automatic ULog search found no PX4 .ulg files under /data/px4/log."
        )

    if not ulog_paths and candidates:
        if args.no_auto_ulog:
            manifest["warnings"].append(
                "No --ulog path was supplied and --no-auto-ulog disabled automatic "
                "copying; remote candidates were saved for review."
            )
        elif discovery_result["ok"]:
            selection_pool = list(eligible_candidates)
            selection_reason = "newest_candidate_after_mission_start"
            if not selection_pool and swarm_mode:
                selection_pool = list(candidates)
                selection_reason = "newest_candidate_time_match_uncertain"
                manifest["warnings"].append(
                    "No ULog candidate was modified after the recorded mission "
                    "start. The newest candidate was copied for review and is "
                    "marked selected_auto_ulog; verify it before analysis."
                )
            elif not selection_pool:
                manifest["warnings"].append(
                    "No remote ULog candidate was new enough for this mission; "
                    "no old ULog was copied. Mission start: "
                    f"{mission_start['timestamp_utc']}."
                )

            if selection_pool:
                if mission_start_epoch is None:
                    manifest["warnings"].append(
                        "Mission start time was unavailable; automatic ULog "
                        "selection could not apply the mission-time filter."
                    )
                    selection_reason = "newest_candidate_no_mission_start"
                selected_record = max(
                    selection_pool,
                    key=lambda candidate: candidate["modified_epoch"],
                )
                selected = selected_record["remote_path"]
                manifest["ulog_discovery"]["selected_auto_ulog"] = selected
                manifest["ulog_discovery"][
                    "selected_auto_ulog_reason"
                ] = selection_reason
                ulog_paths.append(selected)

    # 3. Copy each explicit or automatically selected ULog file.
    for remote_ulog in ulog_paths:
        remote = f"{args.drone_host}:{remote_ulog}"
        result = run_cmd(
            rsync_command_args(swarm_mode) + [remote, str(px4_dir) + "/"],
            timeout=120.0 if swarm_mode else None,
        )
        manifest["actions"].append(result)

        if result["ok"]:
            local_ulog = px4_dir / Path(remote_ulog).name
            manifest["collected_files"]["px4_ulogs"].append(str(local_ulog))
        elif remote_ulog == manifest["ulog_discovery"]["selected_auto_ulog"]:
            manifest["warnings"].append(
                "Automatic ULog copy failed but other mission collection continued: "
                f"{failure_detail(result)}"
            )
        else:
            manifest["errors"].append(
                f"Failed to copy ULog {remote_ulog}: {failure_detail(result)}"
            )

    # 4. Save the general remote status snapshot.
    status_commands = {
        "ulog_list": "find /data -name '*.ulg' -printf '%T@ %p\\n' | sort -n | tail -20",
        "services": "voxl-inspect-services | grep -Ei 'px4|mavlink|ros|dds|micro|xrce'",
    }
    status_snapshot = {
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "mission_id": mission_id,
        "session_id": session_id,
        "drone_host": args.drone_host,
        "remote_drone_mission_folder": remote_drone_folder,
        "commands": {},
    }

    for name, remote_cmd in status_commands.items():
        result = run_cmd(
            ssh_command_args(args.drone_host, remote_cmd, swarm_mode),
            timeout=30.0 if swarm_mode else None,
        )
        status_snapshot["commands"][name] = result
        manifest["actions"].append(result)
        if not result["ok"]:
            manifest["warnings"].append(
                f"Remote status command {name} failed: {failure_detail(result)}"
            )

    status_path = status_dir / f"{session_id}_remote_status_snapshot.json"
    save_json(status_path, status_snapshot)
    manifest["collected_files"]["remote_status_snapshot"] = str(status_path)

    # 5. Save the optional drone code and service state snapshot.
    drone_code_state_path: Path | None = None
    if not args.skip_drone_code_state:
        drone_code_state = capture_drone_code_state(
            args.drone_host, bounded=swarm_mode
        )
        for name, result in drone_code_state["commands"].items():
            manifest["actions"].append(result)
            if not result["ok"]:
                manifest["warnings"].append(
                    f"Drone code-state command {name} failed: "
                    f"{failure_detail(result)}"
                )

        drone_code_state["mission_id"] = mission_id
        drone_code_state["session_id"] = session_id
        drone_code_state["remote_drone_mission_folder"] = remote_drone_folder
        drone_code_state_path = (
            status_dir / f"{session_id}_drone_code_state.json"
        )
        save_json(drone_code_state_path, drone_code_state)
        manifest["collected_files"]["drone_code_state"] = str(
            drone_code_state_path
        )

    collection_finished = datetime.now(timezone.utc)
    manifest["collection_finished_utc"] = collection_finished.isoformat()

    # 6. Save the collection manifest.
    collection_status = (
        "failed"
        if manifest["errors"]
        else (
            "complete_with_warnings"
            if manifest["warnings"]
            else "complete"
        )
    )
    manifest["collection_status"] = collection_status
    manifest_path = (
        per_drone_metadata_dir / f"{session_id}_collection_manifest.json"
        if swarm_mode
        else metadata_dir / f"{mission_id}_collection_manifest.json"
    )
    save_json(manifest_path, manifest)

    if swarm_mode:
        swarm_manifest_path = metadata_dir / "swarm_collection_manifest.json"
        update_swarm_collection_manifest(
            swarm_manifest_path,
            mission_id,
            str(drone_id),
            {
                "drone_id": drone_id,
                "drone_session_id": session_id,
                "ssh_host": args.drone_host,
                "remote_drone_mission_folder": remote_drone_folder,
                "local_drone_dir": str(local_drone_dir),
                "collection_status": collection_status,
                "collection_started_utc": collection_started.isoformat(),
                "collection_finished_utc": collection_finished.isoformat(),
                "collection_manifest_path": str(manifest_path),
                "respeaker_audio": dict(
                    manifest["collected_files"]["respeaker_audio"]
                ),
                "ros2_bags": dict(manifest["collected_files"]["ros2_bags"]),
                "px4_ulogs": list(
                    manifest["collected_files"]["px4_ulogs"]
                ),
                "selected_auto_ulog": manifest["ulog_discovery"][
                    "selected_auto_ulog"
                ],
                "warnings": list(manifest["warnings"]),
                "errors": list(manifest["errors"]),
            },
        )

        print("HALO swarm drone data collection complete.")
        print(f"Mission folder: {mission_dir}")
        print(f"Drone folder: {local_drone_dir}")
        print(f"Collection manifest: {manifest_path}")
        print(f"Swarm manifest: {swarm_manifest_path}")
        if manifest["warnings"]:
            print("\nWarnings:")
            for warning in manifest["warnings"]:
                print(f"  - {warning}")
        if manifest["errors"]:
            print("\nErrors:")
            for error in manifest["errors"]:
                print(f"  - {error}")
        return 1 if manifest["errors"] else 0

    # 7. Update the mission-wide metadata loaded above.
    collection_record = {
        "mission_id": mission_id,
        "session_id": session_id,
        "session_id_matches_mission_id": session_id == mission_id,
        "local_ground_mission_folder": str(mission_dir),
        "local_audio_mission_folder": str(local_session),
        "canonical_remote_drone_mission_folder": canonical_remote_folder,
        "remote_drone_mission_folder": remote_drone_folder,
        "remote_drone_mission_source": remote_session,
        "collection_time_utc": collection_finished.isoformat(),
        "collection_started_utc": collection_started.isoformat(),
        "collection_finished_utc": collection_finished.isoformat(),
        "copied_ulog_paths": list(manifest["collected_files"]["px4_ulogs"]),
        "px4_ulogs_local_paths": list(manifest["collected_files"]["px4_ulogs"]),
        "ulog_discovery": dict(manifest["ulog_discovery"]),
        "ulog_candidates_remote_path": (
            str(ulog_candidates_path)
            if manifest["ulog_discovery"]["automatic_search_performed"]
            else None
        ),
        "copied_respeaker_session_path": manifest["collected_files"][
            "respeaker_session"
        ],
        "respeaker_session_local_path": manifest["collected_files"][
            "respeaker_session"
        ],
        "respeaker_audio": dict(manifest["collected_files"]["respeaker_audio"]),
        "ros2_bags": dict(manifest["collected_files"]["ros2_bags"]),
        "remote_status_snapshot_path": str(status_path),
        "status_snapshot": str(status_path),
        "drone_code_state_path": (
            str(drone_code_state_path) if drone_code_state_path else None
        ),
        "collection_manifest_path": str(manifest_path),
        "termination_reason": args.termination_reason,
        "notes": args.notes,
        "warnings": list(manifest["warnings"]),
        "errors": list(manifest["errors"]),
    }
    metadata.setdefault("collections", []).append(collection_record)

    run_status = metadata.setdefault("run_status", {})
    run_status["data_collection_attempted"] = True
    run_status["data_collection_successful"] = not bool(manifest["errors"])
    run_status["last_collection_utc"] = collection_finished.isoformat()
    run_status["current_status"] = (
        "data_collection_completed_with_errors"
        if manifest["errors"]
        else "data_collection_complete"
    )
    run_status.setdefault("warnings", []).extend(manifest["warnings"])
    run_status.setdefault("errors", []).extend(manifest["errors"])
    if args.termination_reason is not None:
        run_status["termination_reason"] = args.termination_reason
    run_status["last_collection_notes"] = args.notes

    expected_outputs = metadata.setdefault("expected_outputs", {})
    if manifest["collected_files"]["respeaker_audio"]["exists"]:
        expected_outputs["respeaker_audio"] = "collected"
    elif manifest["collected_files"]["respeaker_session"]:
        expected_outputs["respeaker_audio"] = "session_collected_audio_missing"
    if manifest["collected_files"]["ros2_bags"]["exists"]:
        expected_outputs["ros2_bag"] = "collected"
    if manifest["collected_files"]["px4_ulogs"]:
        expected_outputs["px4_ulog"] = "collected"

    save_json(metadata_path, metadata)

    print("HALO drone data collection complete.")
    print(f"Mission folder: {mission_dir}")
    print(f"Collection manifest: {manifest_path}")
    print(f"Mission metadata: {metadata_path}")
    print(f"Remote status snapshot: {status_path}")
    if drone_code_state_path:
        print(f"Drone code state: {drone_code_state_path}")

    if manifest["warnings"]:
        print("\nWarnings:")
        for warning in manifest["warnings"]:
            print(f"  - {warning}")

    if manifest["errors"]:
        print("\nErrors:")
        for error in manifest["errors"]:
            print(f"  - {error}")
    elif manifest["warnings"]:
        print("\nCollection completed without transfer errors; review the warnings above.")
    else:
        print("\nAll requested data copied successfully.")

    return 1 if manifest["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
