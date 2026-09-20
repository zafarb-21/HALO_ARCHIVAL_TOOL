#!/usr/bin/env python3
"""Shared configuration and archive utilities for the passive HALO swarm launcher."""

from __future__ import annotations

import argparse
import copy
import json
import os
import platform
import re
import shlex
import shutil
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from create_mission_archive import capture_base_station_code_state
    from run_halo_swarm_mission import load_yaml_subset
except ImportError:
    from scripts.create_mission_archive import capture_base_station_code_state
    from scripts.run_halo_swarm_mission import load_yaml_subset


LAB_MAPPING = {
    "D0012": {"ip_address": "192.168.0.20", "ssh_host": "halo-d0012", "ros_domain_id": 3},
    "D0013": {"ip_address": "192.168.0.21", "ssh_host": "halo-d0013", "ros_domain_id": 4},
    "D0014": {"ip_address": "192.168.0.22", "ssh_host": "halo-d0014", "ros_domain_id": 5},
    "D0015": {"ip_address": "192.168.0.23", "ssh_host": "halo-d0015", "ros_domain_id": 6},
    "D0016": {"ip_address": "192.168.0.24", "ssh_host": "halo-d0016", "ros_domain_id": 7},
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_json_atomic(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def remote_mission_dir(remote_root: str, mission_id: str, drone_id: str) -> str:
    root = str(remote_root).rstrip("/")
    return f"/{mission_id}__{drone_id}" if not root else f"{root}/{mission_id}__{drone_id}"


def _config_path(reference: object, code_root: Path, swarm_path: Path) -> Path:
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError("Every swarm entry needs a config path")
    candidates = [Path(reference).expanduser(), code_root / reference, swarm_path.parent / reference]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(f"Could not resolve drone config: {reference}")


def validate_swarm(swarm_path: Path, code_root: Path) -> list[dict[str, Any]]:
    data = load_yaml_subset(swarm_path)
    raw = data.get("drones")
    if not isinstance(raw, list):
        raise ValueError(f"{swarm_path}: drones must be a list")
    by_id: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            errors.append("Each swarm drone entry must be a mapping")
            continue
        drone_id = item.get("drone_id")
        if drone_id not in LAB_MAPPING:
            errors.append(f"Unexpected drone_id: {drone_id!r}")
            continue
        if drone_id in by_id:
            errors.append(f"Duplicate drone_id: {drone_id}")
            continue
        expected = LAB_MAPPING[drone_id]
        for field in ("ip_address", "ssh_host", "ros_domain_id"):
            if item.get(field) != expected[field]:
                errors.append(
                    f"{drone_id}: {field} must be {expected[field]!r}; "
                    f"found {item.get(field)!r}"
                )
        if item.get("enabled") is not True:
            errors.append(f"{drone_id}: enabled must be true for the five-drone lab run")
        if item.get("required") is not True:
            errors.append(f"{drone_id}: required must be true for the five-drone lab run")
        post = item.get("post_landing_record_s", 3)
        if isinstance(post, bool) or not isinstance(post, (int, float)) or not 2 <= float(post) <= 5:
            errors.append(f"{drone_id}: post_landing_record_s must be between 2 and 5")
            post = 3
        try:
            config_path = _config_path(item.get("config"), code_root, swarm_path)
            config = load_yaml_subset(config_path)
        except (OSError, ValueError) as exc:
            errors.append(f"{drone_id}: {exc}")
            continue
        if config.get("drone_id") != drone_id:
            errors.append(f"{config_path}: drone_id does not match {drone_id}")
        if str(config.get("ip_address")) != expected["ip_address"]:
            errors.append(f"{config_path}: ip_address does not match {expected['ip_address']}")
        if config.get("ssh_user") != "root":
            errors.append(f"{config_path}: ssh_user must be root")
        if config.get("ros_domain_id") != expected["ros_domain_id"]:
            errors.append(
                f"{config_path}: ros_domain_id must be {expected['ros_domain_id']} "
                "and must not be guessed"
            )
        paths = config.get("paths") if isinstance(config.get("paths"), dict) else {}
        audio = config.get("audio") if isinstance(config.get("audio"), dict) else {}
        legacy_audio = config.get("sensors", {}).get("respeaker", {}) if isinstance(config.get("sensors"), dict) else {}
        legacy_audio = legacy_audio if isinstance(legacy_audio, dict) else {}
        remote_root = str(paths.get("drone_sync_folder", "/home/root/halo_sync_test"))
        px4_root = str(paths.get("px4_log_folder", "/data/px4/log"))
        try:
            sample_rate = int(audio.get("sample_rate_hz", legacy_audio.get("sample_rate_hz", 16000)))
            channels = int(audio.get("channels", legacy_audio.get("channels", 6)))
        except (TypeError, ValueError):
            sample_rate, channels = 16000, 6
            errors.append(f"{drone_id}: invalid audio sample configuration")
        by_id[drone_id] = {
            "drone_id": drone_id,
            "drone_host": expected["ssh_host"],
            "ssh_host": expected["ssh_host"],
            "drone_ip": expected["ip_address"],
            "ip_address": expected["ip_address"],
            "ros_domain_id": expected["ros_domain_id"],
            "static_peer": expected["ip_address"],
            "enabled": True,
            "required": True,
            "post_landing_record_s": float(post),
            "config_path": str(config_path),
            "remote_sync_root": remote_root,
            "remote_mission_dir": remote_mission_dir(remote_root, "__MISSION_ID__", drone_id),
            "px4_root": px4_root,
            "audio_device": str(audio.get("audio_device", legacy_audio.get("alsa_device", "hw:0,0"))),
            "sample_rate": sample_rate,
            "channels": channels,
            "sample_format": str(audio.get("sample_format", legacy_audio.get("sample_format", "S16_LE"))),
        }
    missing = sorted(set(LAB_MAPPING) - set(by_id))
    if missing:
        errors.append("Missing required lab drones: " + ", ".join(missing))
    domains = [item["ros_domain_id"] for item in by_id.values()]
    if len(domains) != len(set(domains)):
        errors.append("ROS_DOMAIN_ID values must be unique across the five workers")
    if errors:
        raise ValueError("\n".join(errors))
    return [by_id[drone_id] for drone_id in LAB_MAPPING]


def _record_template(drone: dict[str, Any]) -> dict[str, Any]:
    return {
        "drone_id": drone["drone_id"],
        "drone_host": drone["drone_host"],
        "drone_ip": drone["drone_ip"],
        "ros_domain_id": drone["ros_domain_id"],
        "static_peer": drone["static_peer"],
        "enabled": True,
        "required": True,
        "post_landing_record_s": drone["post_landing_record_s"],
        "status": "PENDING",
        "warnings": [],
        "errors": [],
        "clock_sync": None,
        "microdds": None,
    }


def prepare_archive(
    archive_root: Path,
    mission_id: str,
    mission_name: str,
    operator: str,
    code_root: Path,
    swarm_path: Path,
    profile_path: Path,
) -> tuple[Path, list[dict[str, Any]]]:
    if re.fullmatch(r"[A-Za-z0-9._-]+", mission_id) is None:
        raise ValueError("mission_id may contain only letters, numbers, dot, underscore, and hyphen")
    drones = validate_swarm(swarm_path, code_root)
    archive_root = archive_root.expanduser().resolve()
    archive_root.mkdir(parents=True, exist_ok=True)
    mission_dir = archive_root / mission_id
    if mission_dir.exists():
        raise FileExistsError(f"Mission archive already exists: {mission_dir}")
    mission_dir.mkdir(parents=True)
    (mission_dir / "metadata").mkdir()
    (mission_dir / "config" / "drones").mkdir(parents=True)
    (mission_dir / "config" / "profiles").mkdir(parents=True)
    shutil.copy2(swarm_path, mission_dir / "config" / "swarm_lab.yaml")
    shutil.copy2(profile_path, mission_dir / "config" / "profiles" / profile_path.name)
    code_state = capture_base_station_code_state(Path(__file__).resolve(), code_root)
    save_json_atomic(mission_dir / "metadata" / "code_state.json", code_state)
    records = {drone["drone_id"]: _record_template(drone) for drone in drones}
    configured: list[dict[str, Any]] = []
    for drone in drones:
        local = mission_dir / "drones" / drone["drone_id"]
        for name in ("ros_bags", "px4_logs", "audio", "metadata", "status_logs", "processed"):
            (local / name).mkdir(parents=True, exist_ok=True)
        shutil.copy2(drone["config_path"], mission_dir / "config" / "drones" / Path(drone["config_path"]).name)
        configured.append({**drone, "local_dir": str(local), "remote_mission_dir": remote_mission_dir(drone["remote_sync_root"], mission_id, drone["drone_id"])})
    created = utc_now()
    metadata = {
        "schema_version": 3,
        "mission_id": mission_id,
        "mission_name": mission_name,
        "operator": operator,
        "mission_start_utc": created,
        "mission_end_utc": None,
        "configured_drones": records,
        "archive_computer": {"hostname": socket.gethostname(), "platform": platform.platform()},
        "code_state": {"path": str(mission_dir / "metadata" / "code_state.json")},
        "configuration_snapshots": {
            "swarm": str(mission_dir / "config" / "swarm_lab.yaml"),
            "profile": str(mission_dir / "config" / "profiles" / profile_path.name),
        },
        "clock_sync": {},
        "microdds": {},
        "swarm_readiness": {
            "status": "PENDING",
            "required_drone_ids": list(LAB_MAPPING),
            "ready_drone_ids": [],
            "failed_drone_ids": [],
        },
        "overall_termination_reason": None,
        "safety": "Passive archive station only; no arm, flight, land, disarm, or trajectory commands.",
    }
    save_json_atomic(mission_dir / "metadata" / "mission_metadata.json", metadata)
    save_json_atomic(
        mission_dir / "metadata" / "swarm_manifest.json",
        {"schema_version": 3, "mission_id": mission_id, "collection_status": "pending", "drones": records},
    )
    save_json_atomic(
        mission_dir / "metadata" / "ground_orchestrator_log.json",
        {
            "schema_version": 3,
            "mission_id": mission_id,
            "mission_start_utc": created,
            "events": [{"timestamp_utc": created, "event": "archive_created"}],
            "safety": metadata["safety"],
        },
    )
    for drone in configured:
        local_meta = Path(drone["local_dir"]) / "metadata"
        save_json_atomic(
            local_meta / "collection_manifest.json",
            {
                "schema_version": 3,
                "mission_id": mission_id,
                "drone_id": drone["drone_id"],
                "drone_host": drone["drone_host"],
                "drone_ip": drone["drone_ip"],
                "ros_domain_id": drone["ros_domain_id"],
                "static_peer": drone["static_peer"],
                "drone_ros_check": None,
                "ground_ros_check": None,
                "visible_topics": [],
                "px4_msgs_decode_ok": False,
                "collection_status": "pending",
                "warnings": [],
                "errors": [],
            },
        )
    (mission_dir / "metadata" / "launcher_context.json").write_text(
        json.dumps({"mission_id": mission_id, "drones": configured, "created_utc": created}, indent=2) + "\n",
        encoding="utf-8",
    )
    (mission_dir / "README.md").write_text(
        f"# HALO five-drone passive archive: {mission_id}\n\n"
        "Ground Station A controls flight. Ground Station B only observes ROS state, records bags, "
        "collects ULogs/audio, and writes metadata. Each drone has a dedicated fixed-domain worker.\n\n"
        "Preflight order: drone ROS first, then ground ROS.\n",
        encoding="utf-8",
    )
    return mission_dir, configured


def append_preflight_result(results_file: Path, drone_id: str, kind: str, ok: bool, detail: str) -> None:
    values = load_json(results_file)
    values.setdefault(drone_id, {})[kind] = {"ok": ok, "detail": detail, "recorded_utc": utc_now()}
    save_json_atomic(results_file, values)


def merge_preflight(mission_dir: Path, results_file: Path) -> None:
    results = load_json(results_file)
    metadata_path = mission_dir / "metadata" / "mission_metadata.json"
    metadata = load_json(metadata_path)
    metadata.setdefault("preflight", {})
    log_path = mission_dir / "metadata" / "ground_orchestrator_log.json"
    log = load_json(log_path)
    for drone_id, values in results.items():
        metadata["preflight"][drone_id] = values
        if "clock_sync" in values:
            metadata.setdefault("clock_sync", {})[drone_id] = values["clock_sync"]
        if "microdds" in values:
            metadata.setdefault("microdds", {})[drone_id] = values["microdds"]
        record = metadata.setdefault("configured_drones", {}).setdefault(drone_id, {})
        record.update({key: value for key, value in values.items() if key in {"ssh", "clock_sync", "microdds"}})
        log.setdefault("events", []).append({"timestamp_utc": utc_now(), "event": "launcher_preflight", "drone_id": drone_id, "results": values})
        manifest_path = mission_dir / "drones" / drone_id / "metadata" / "collection_manifest.json"
        manifest = load_json(manifest_path)
        manifest.update({key: values[key] for key in ("ssh", "clock_sync", "microdds") if key in values})
        save_json_atomic(manifest_path, manifest)
    save_json_atomic(metadata_path, metadata)
    save_json_atomic(log_path, log)


def aggregate_mission(mission_dir: Path, termination_reason: str | None = None) -> dict[str, Any]:
    metadata_path = mission_dir / "metadata" / "mission_metadata.json"
    metadata = load_json(metadata_path)
    swarm_path = mission_dir / "metadata" / "swarm_manifest.json"
    swarm = load_json(swarm_path)
    aggregate: dict[str, Any] = {}
    phases: list[str] = []
    collection_states: list[str] = []
    for drone_id in LAB_MAPPING:
        status = load_json(mission_dir / "drones" / drone_id / "metadata" / "worker_status.json")
        collection = load_json(mission_dir / "drones" / drone_id / "metadata" / "collection_manifest.json")
        final = collection.get("worker_status", {}) if collection else {}
        phase = status.get("phase") or status.get("state") or collection.get("termination_reason") or "NOT_STARTED"
        phases.append(str(phase))
        collection_state = collection.get("collection_status", "pending")
        collection_states.append(collection_state)
        aggregate[drone_id] = {
            "status": phase,
            "phase": status.get("phase"),
            "worker_status": status,
            "worker_status_path": str(mission_dir / "drones" / drone_id / "metadata" / "worker_status.json"),
            "collection_manifest_path": str(mission_dir / "drones" / drone_id / "metadata" / "collection_manifest.json"),
            "collection_status": collection_state,
            "bag": collection.get("bag", status.get("bag", {})),
            "selected_ulog": collection.get("ulog", {}).get("selected_local_path"),
            "warnings": collection.get("warnings", status.get("warnings", [])),
            "errors": collection.get("errors", status.get("errors", [])),
        }
        metadata.setdefault("configured_drones", {}).setdefault(drone_id, {}).update(aggregate[drone_id])
    ready_ids = [drone_id for drone_id in LAB_MAPPING if load_json(mission_dir / "drones" / drone_id / "metadata" / "worker_status.json").get("ready_utc")]
    failed_ids = [drone_id for drone_id in LAB_MAPPING if str(load_json(mission_dir / "drones" / drone_id / "metadata" / "worker_status.json").get("phase", "")).startswith("FAILED")]
    metadata["swarm_readiness"] = {
        "status": "READY" if len(ready_ids) == len(LAB_MAPPING) else "PARTIAL_READY" if ready_ids else "NOT_READY",
        "required_drone_ids": list(LAB_MAPPING),
        "ready_drone_ids": ready_ids,
        "failed_drone_ids": failed_ids,
    }
    collection_failure = any(state == "failed" or aggregate[drone_id].get("errors") for drone_id, state in zip(LAB_MAPPING, collection_states))
    if termination_reason:
        swarm_status = "complete_with_drone_failures" if collection_failure else "complete"
        swarm["collection_status"] = swarm_status
        swarm["updated_utc"] = utc_now()
        metadata["mission_end_utc"] = utc_now()
        metadata["overall_termination_reason"] = termination_reason
    elif all(phase in {"DONE", "FAILED_PREFLIGHT", "FAILED_ENVIRONMENT"} for phase in phases):
        swarm["collection_status"] = "complete_with_drone_failures" if collection_failure else "complete"
        swarm["updated_utc"] = utc_now()
    else:
        swarm["collection_status"] = "running"
    swarm["drones"] = aggregate
    save_json_atomic(swarm_path, swarm)
    save_json_atomic(metadata_path, metadata)
    return aggregate


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--swarm", required=True, type=Path)
    validate.add_argument("--code-root", required=True, type=Path)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--archive-root", required=True, type=Path)
    prepare.add_argument("--mission-id", required=True)
    prepare.add_argument("--mission-name", required=True)
    prepare.add_argument("--operator", required=True)
    prepare.add_argument("--code-root", required=True, type=Path)
    prepare.add_argument("--swarm", required=True, type=Path)
    prepare.add_argument("--profile", required=True, type=Path)
    merge = sub.add_parser("merge-preflight")
    merge.add_argument("--mission-dir", required=True, type=Path)
    merge.add_argument("--results-file", required=True, type=Path)
    append = sub.add_parser("append-preflight")
    append.add_argument("--results-file", required=True, type=Path)
    append.add_argument("--drone-id", required=True)
    append.add_argument("--kind", required=True)
    append.add_argument("--ok", required=True, choices=("true", "false"))
    append.add_argument("--detail", default="")
    aggregate = sub.add_parser("aggregate")
    aggregate.add_argument("--mission-dir", required=True, type=Path)
    aggregate.add_argument("--termination-reason")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "validate":
        for drone in validate_swarm(args.swarm.resolve(), args.code_root.resolve()):
            print(f"{drone['drone_id']} | {drone['drone_ip']} | ROS_DOMAIN_ID={drone['ros_domain_id']} | {drone['drone_host']}")
        print("SWARM CONFIGURATION OK")
        return 0
    if args.command == "prepare":
        mission, _ = prepare_archive(args.archive_root, args.mission_id, args.mission_name, args.operator, args.code_root.resolve(), args.swarm.resolve(), args.profile.resolve())
        print(mission)
        return 0
    if args.command == "append-preflight":
        append_preflight_result(args.results_file, args.drone_id, args.kind, args.ok == "true", args.detail)
        return 0
    if args.command == "merge-preflight":
        merge_preflight(args.mission_dir.resolve(), args.results_file.resolve())
        return 0
    if args.command == "aggregate":
        aggregate_mission(args.mission_dir.resolve(), args.termination_reason)
        return 0
    raise RuntimeError(args.command)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
