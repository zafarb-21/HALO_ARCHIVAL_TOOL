#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import subprocess
import sys
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


def add_unique(items: list[str], message: str) -> None:
    if message not in items:
        items.append(message)


_active_collection_state = None


def save_partial_collection_manifest(reason: str, error: str | None = None) -> None:
    """Persist an in-progress collection manifest after an interruption/error."""
    state = _active_collection_state
    if not isinstance(state, dict):
        return
    manifest = state.get("manifest")
    manifest_path = state.get("manifest_path")
    if not isinstance(manifest, dict) or not isinstance(manifest_path, Path):
        return
    if error:
        add_unique(manifest.setdefault("errors", []), error)
    manifest["partial_reason"] = reason
    manifest["collection_finished_utc"] = datetime.now(timezone.utc).isoformat()
    manifest["collection_status"] = (
        "partial_with_errors" if manifest.get("errors") else "partial"
    )
    try:
        save_json(manifest_path, manifest)
    except Exception:
        pass

    metadata = state.get("metadata")
    metadata_path = state.get("metadata_path")
    if isinstance(metadata, dict) and isinstance(metadata_path, Path):
        run_status = metadata.setdefault("run_status", {})
        run_status["data_collection_attempted"] = True
        run_status["data_collection_successful"] = False
        run_status["current_status"] = "data_collection_partial"
        run_status["last_collection_utc"] = manifest["collection_finished_utc"]
        for warning in manifest.get("warnings", []):
            add_unique(run_status.setdefault("warnings", []), warning)
        for item in manifest.get("errors", []):
            add_unique(run_status.setdefault("errors", []), item)
        audio_record = manifest.get("recording_requests", {}).get("audio", {})
        rosbag_record = manifest.get("recording_requests", {}).get("rosbag", {})
        if isinstance(audio_record, dict) and isinstance(audio_record.get("requested"), bool):
            metadata["audio_enabled"] = audio_record["requested"]
            metadata["audio_disabled_intentionally"] = not audio_record["requested"]
        if isinstance(rosbag_record, dict) and isinstance(rosbag_record.get("requested"), bool):
            metadata["rosbag_enabled"] = rosbag_record["requested"]
        try:
            save_json(metadata_path, metadata)
        except Exception:
            pass


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


def recording_request(
    metadata: dict, key: str, legacy_default: bool
) -> dict:
    """Resolve an orchestrator capture flag while preserving old archives."""
    ground_orchestrator = metadata.get("ground_orchestrator", {})
    if isinstance(ground_orchestrator, dict):
        value = ground_orchestrator.get(key)
        if isinstance(value, bool):
            return {
                "requested": value,
                "source": f"ground_orchestrator.{key}",
            }

    recording_requests = metadata.get("recording_requests", {})
    if isinstance(recording_requests, dict):
        value = recording_requests.get(key)
        if isinstance(value, bool):
            return {"requested": value, "source": f"recording_requests.{key}"}

    return {
        "requested": legacy_default,
        "source": "legacy_archive_default",
    }


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


def _collect_main() -> int:
    global _active_collection_state

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
                args.drone_host, bounded=True
            )
        except RuntimeError as exc:
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
    audio_request = recording_request(metadata, "audio_enabled", True)
    rosbag_request = recording_request(metadata, "rosbag_enabled", False)
    ground_orchestrator = metadata.get("ground_orchestrator", {})
    ground_rosbag = (
        ground_orchestrator.get("ground_rosbag", {})
        if isinstance(ground_orchestrator, dict)
        else {}
    )
    if not isinstance(ground_rosbag, dict) or not ground_rosbag:
        ground_log = load_json(metadata_dir / "ground_orchestrator_log.json")
        ground_rosbag = ground_log.get("ground_rosbag", {})
    if not isinstance(ground_rosbag, dict):
        ground_rosbag = {}
    ground_rosbag = dict(ground_rosbag)
    ground_rosbag_requested = bool(ground_rosbag.get("enabled", False))

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
        "ground_rosbag": ground_rosbag,
        "ground_rosbag_enabled": ground_rosbag_requested,
        "ground_rosbag_start_attempted": bool(
            ground_rosbag.get("start_attempted", False)
        ),
        "ground_rosbag_started": bool(ground_rosbag.get("started", False)),
        "ground_rosbag_command": ground_rosbag.get("command"),
        "ground_rosbag_output_path": ground_rosbag.get("output_path"),
        "ground_rosbag_ros_domain_id": ground_rosbag.get("ros_domain_id"),
        "ground_rosbag_px4_msgs_setup_path": ground_rosbag.get(
            "px4_msgs_setup_path"
        ),
        "ground_rosbag_px4_msgs_workspace": ground_rosbag.get(
            "px4_msgs_workspace", ground_rosbag.get("px4_msgs_setup_path")
        ),
        "ground_rosbag_start_utc": ground_rosbag.get("start_utc"),
        "ground_rosbag_stop_utc": ground_rosbag.get("stop_utc"),
        "ground_rosbag_return_code": ground_rosbag.get("return_code"),
        "ground_rosbag_preflight_command": ground_rosbag.get("preflight_command"),
        "ground_rosbag_preflight_topics": list(
            ground_rosbag.get("preflight_topics", []) or []
        ),
        "ground_rosbag_preflight_fmu_topics": list(
            ground_rosbag.get("preflight_fmu_topics", []) or []
        ),
        "ground_rosbag_warnings": list(ground_rosbag.get("warnings", []) or []),
        "ground_rosbag_errors": list(ground_rosbag.get("errors", []) or []),
        "ssh_connection_check": ssh_connection_check,
        "recording_requests": {
            "audio": dict(audio_request),
            "rosbag": dict(rosbag_request),
            "px4_ulog": {
                "requested": not args.no_auto_ulog or bool(args.ulog),
                "auto_ulog_enabled": not args.no_auto_ulog,
                "explicit_auto_ulog_requested": args.auto_ulog,
                "explicit_ulog_paths": list(args.ulog),
            },
        },
        "rosbag_capture": {
            "requested": rosbag_request["requested"],
            "start_attempted": False,
            "started": False,
            "started_inferred_from_collected_bag": False,
            "output_path_remote": None,
            "collected_path": None,
            "collected": False,
            "requested_topics": [],
            "selected_topics": [],
            "missing_topics": [],
            "command": None,
            "command_shell": None,
            "return_code": None,
            "warnings": [],
            "errors": [],
        },
        "agent_metadata": {
            "start_path": None,
            "final_path": None,
        },
        "actions": [],
        "collected_files": {
            "respeaker_session": None,
            "respeaker_audio": {
                "requested": audio_request["requested"],
                "disabled_intentionally": not audio_request["requested"],
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
            "ground_ros2_bag": {
                "path": ground_rosbag.get("output_path"),
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
            "selection_policy": "prefer_newest_after_mission_start_else_newest_candidate",
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
    # Keep a short top-level alias for consumers that do not know the richer
    # rosbag_capture field name; both entries are updated together.
    manifest["rosbag"] = manifest["rosbag_capture"]
    manifest_path = (
        per_drone_metadata_dir / f"{session_id}_collection_manifest.json"
        if swarm_mode
        else metadata_dir / f"{mission_id}_collection_manifest.json"
    )
    manifest["collection_status"] = "in_progress"
    _active_collection_state = {
        "manifest": manifest,
        "manifest_path": manifest_path,
        "metadata": metadata,
        "metadata_path": metadata_path,
    }
    save_json(manifest_path, manifest)

    # 1. Copy the complete drone mission folder. The local folder remains canonical.
    local_session.mkdir(parents=True, exist_ok=True)

    result = run_cmd(
        rsync_command_args(True)
        + [remote_session, str(local_session) + "/"],
        timeout=180.0,
    )
    manifest["actions"].append(result)

    if result["ok"]:
        manifest["collected_files"]["respeaker_session"] = str(local_session)
    else:
        manifest["errors"].append(
            "Failed to copy drone mission/session folder "
            f"{remote_session}: {failure_detail(result)}"
        )

    agent_start_path = local_session / "metadata" / "drone_agent_start.json"
    agent_final_path = local_session / "metadata" / "drone_agent_final.json"
    agent_start = load_json(agent_start_path)
    agent_final = load_json(agent_final_path)
    if agent_start:
        manifest["agent_metadata"]["start_path"] = str(agent_start_path)
    if agent_final:
        manifest["agent_metadata"]["final_path"] = str(agent_final_path)
    agent_record = agent_final if agent_final else agent_start

    if agent_record:
        agent_audio_requested = agent_record.get("audio_enabled")
        agent_audio = agent_record.get("audio", {})
        if (
            not isinstance(agent_audio_requested, bool)
            and isinstance(agent_audio, dict)
        ):
            agent_audio_requested = agent_audio.get(
                "requested", agent_audio.get("enabled")
            )
        if isinstance(agent_audio_requested, bool):
            audio_request = {
                "requested": agent_audio_requested,
                "source": (
                    "drone_agent_final.json"
                    if agent_final
                    else "drone_agent_start.json"
                ),
            }
            manifest["recording_requests"]["audio"] = dict(audio_request)

        agent_rosbag = agent_record.get("rosbag", {})
        agent_rosbag_requested = agent_record.get("rosbag_enabled")
        if (
            not isinstance(agent_rosbag_requested, bool)
            and isinstance(agent_rosbag, dict)
        ):
            agent_rosbag_requested = agent_rosbag.get(
                "requested", agent_rosbag.get("enabled")
            )
        if isinstance(agent_rosbag_requested, bool):
            rosbag_request = {
                "requested": agent_rosbag_requested,
                "source": (
                    "drone_agent_final.json"
                    if agent_final
                    else "drone_agent_start.json"
                ),
            }
            manifest["recording_requests"]["rosbag"] = dict(rosbag_request)
            manifest["rosbag_capture"]["requested"] = agent_rosbag_requested

        if isinstance(agent_rosbag, dict):
            for source_key, target_key in (
                ("start_attempted", "start_attempted"),
                ("started", "started"),
                ("started_utc", "started_utc"),
                ("requested_topics", "requested_topics"),
                ("selected_topics", "selected_topics"),
                ("missing_topics", "missing_topics"),
                ("command", "command"),
                ("command_shell", "command_shell"),
                ("return_code", "return_code"),
            ):
                if source_key in agent_rosbag:
                    manifest["rosbag_capture"][target_key] = agent_rosbag[
                        source_key
                    ]
            remote_bag_path = agent_rosbag.get(
                "output_path", agent_rosbag.get("path")
            )
            if remote_bag_path is not None:
                manifest["rosbag_capture"]["output_path_remote"] = (
                    remote_bag_path
                )

            for warning in (agent_rosbag.get("warnings") or []):
                add_unique(manifest["rosbag_capture"]["warnings"], str(warning))
            for error in (agent_rosbag.get("errors") or []):
                add_unique(manifest["rosbag_capture"]["errors"], str(error))

        for warning in (agent_record.get("warnings") or []):
            warning_text = str(warning)
            if (
                "ros" in warning_text.lower()
                or "topic" in warning_text.lower()
                or "bag" in warning_text.lower()
            ):
                add_unique(
                    manifest["rosbag_capture"]["warnings"], warning_text
                )
        for error in (agent_record.get("errors") or []):
            error_text = str(error)
            if (
                "ros" in error_text.lower()
                or "topic" in error_text.lower()
                or "bag" in error_text.lower()
            ):
                add_unique(manifest["rosbag_capture"]["errors"], error_text)

    for warning in manifest["rosbag_capture"]["warnings"]:
        add_unique(manifest["warnings"], warning)
    for error in manifest["rosbag_capture"]["errors"]:
        add_unique(
            manifest["warnings"],
            "Drone-agent ROS bag error (non-fatal): " + error,
        )

    expected_audio_path = local_session / "audio" / "respeaker_6ch.wav"
    audio_record = manifest["collected_files"]["respeaker_audio"]
    audio_record["requested"] = audio_request["requested"]
    audio_record["disabled_intentionally"] = not audio_request["requested"]
    if expected_audio_path.is_file():
        audio_record["exists"] = True
        audio_record["size_bytes"] = expected_audio_path.stat().st_size
    elif audio_request["requested"]:
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
    remote_bag_output = manifest["rosbag_capture"]["output_path_remote"]
    if remote_bag_output:
        manifest["rosbag_capture"]["collected_path"] = str(
            collected_bags_path / Path(str(remote_bag_output)).name
        )
    else:
        manifest["rosbag_capture"]["collected_path"] = str(collected_bags_path)
    manifest["rosbag_capture"]["collected"] = bool(bag_entries)
    if (
        bag_entries
        and manifest["rosbag_capture"]["requested"]
        and not manifest["rosbag_capture"]["started"]
    ):
        manifest["rosbag_capture"]["started"] = True
        manifest["rosbag_capture"]["start_attempted"] = True
        manifest["rosbag_capture"][
            "started_inferred_from_collected_bag"
        ] = True

    if (
        manifest["rosbag_capture"]["requested"]
        and not manifest["rosbag_capture"]["started"]
    ):
        add_unique(
            manifest["warnings"],
            "ROS bag recording was requested but did not start; review "
            "rosbag_capture warnings and missing_topics.",
        )
    elif manifest["rosbag_capture"]["started"] and not bag_entries:
        add_unique(
            manifest["warnings"],
            "ROS bag recording started, but no bag files were found after "
            "mission-folder collection.",
        )

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

    # Ground-side bags are already in the mission archive. Record their final
    # contents in the same manifest as drone-side bags without treating a
    # missing/failed ground recorder as a transfer failure.
    ground_output = ground_rosbag.get("output_path")
    ground_output_path = Path(str(ground_output)) if ground_output else None
    ground_entries = (
        sorted(
            str(path)
            for path in ground_output_path.rglob("*")
            if path.is_file()
        )
        if ground_output_path is not None and ground_output_path.is_dir()
        else []
    )
    manifest["collected_files"]["ground_ros2_bag"]["path"] = (
        str(ground_output_path) if ground_output_path is not None else None
    )
    manifest["collected_files"]["ground_ros2_bag"]["exists"] = bool(ground_entries)
    manifest["collected_files"]["ground_ros2_bag"]["entries"] = ground_entries
    ground_rosbag["collected"] = bool(ground_entries)
    ground_rosbag["output_exists"] = bool(ground_entries)
    ground_rosbag["output_entries"] = ground_entries
    for warning in ground_rosbag.get("warnings", []) or []:
        add_unique(manifest["warnings"], "Ground ROS bag: " + str(warning))
    for error in ground_rosbag.get("errors", []) or []:
        add_unique(
            manifest["warnings"],
            "Ground ROS bag error (non-fatal): " + str(error),
        )
    if ground_rosbag_requested and not ground_entries:
        add_unique(
            manifest["warnings"],
            "Ground ROS bag was requested but no finalized bag files were found "
            "at the recorded output path.",
        )
    manifest["ground_rosbag"] = ground_rosbag
    manifest["ground_rosbag_enabled"] = ground_rosbag_requested
    manifest["ground_rosbag_start_attempted"] = bool(
        ground_rosbag.get("start_attempted", False)
    )
    manifest["ground_rosbag_started"] = bool(ground_rosbag.get("started", False))
    manifest["ground_rosbag_start_utc"] = ground_rosbag.get("start_utc")
    manifest["ground_rosbag_stop_utc"] = ground_rosbag.get("stop_utc")
    manifest["ground_rosbag_return_code"] = ground_rosbag.get("return_code")

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
        ssh_command_args(args.drone_host, discovery_command, True),
        timeout=30.0,
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
            if not selection_pool and candidates:
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
                    "no ULog was copied. Mission start: "
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
            rsync_command_args(True) + [remote, str(px4_dir) + "/"],
            timeout=120.0,
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
            ssh_command_args(args.drone_host, remote_cmd, True),
            timeout=30.0,
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
            args.drone_host, bounded=True
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
                "recording_requests": dict(manifest["recording_requests"]),
                "rosbag_capture": dict(manifest["rosbag_capture"]),
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
        "ground_ros2_bag": dict(manifest["collected_files"]["ground_ros2_bag"]),
        "ground_rosbag": dict(manifest["ground_rosbag"]),
        "recording_requests": dict(manifest["recording_requests"]),
        "rosbag_capture": dict(manifest["rosbag_capture"]),
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
    elif not audio_request["requested"]:
        expected_outputs["respeaker_audio"] = "not_requested"
    elif manifest["collected_files"]["respeaker_session"]:
        expected_outputs["respeaker_audio"] = "session_collected_audio_missing"
    if manifest["collected_files"]["ros2_bags"]["exists"]:
        expected_outputs["ros2_bag"] = "collected"
    elif not rosbag_request["requested"]:
        expected_outputs["ros2_bag"] = "not_requested"
    else:
        expected_outputs["ros2_bag"] = "requested_not_collected"
    if manifest["collected_files"]["px4_ulogs"]:
        expected_outputs["px4_ulog"] = "collected"
    if ground_rosbag_requested:
        expected_outputs["ground_ros2_bag"] = (
            "collected"
            if manifest["collected_files"]["ground_ros2_bag"]["exists"]
            else "requested_not_collected"
        )
    else:
        expected_outputs["ground_ros2_bag"] = "not_requested"

    metadata["ground_rosbag"] = dict(ground_rosbag)
    metadata["audio_enabled"] = bool(audio_request["requested"])
    metadata["audio_disabled_intentionally"] = not bool(audio_request["requested"])
    metadata["rosbag_enabled"] = bool(rosbag_request["requested"])
    metadata.setdefault("recording_requests", {})["audio"] = dict(audio_request)
    metadata.setdefault("recording_requests", {})["rosbag"] = dict(rosbag_request)
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


def main() -> int:
    global _active_collection_state
    try:
        return _collect_main()
    except KeyboardInterrupt:
        save_partial_collection_manifest(
            "user_interrupt_collection_attempted",
            "Collection interrupted by the user before the normal manifest finalization step.",
        )
        print("Collection interrupted; partial manifest was saved.")
        return 130
    except Exception as exc:
        error = "Unhandled collection exception: {0}: {1}".format(
            type(exc).__name__, exc
        )
        save_partial_collection_manifest("collector_exception", error)
        print("Collection failed; partial manifest was saved.", file=sys.stderr)
        print(error, file=sys.stderr)
        return 1
    finally:
        _active_collection_state = None


if __name__ == "__main__":
    raise SystemExit(main())
