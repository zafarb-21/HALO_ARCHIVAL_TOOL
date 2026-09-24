#!/usr/bin/env python3
"""Dedicated fixed-domain passive ground worker for one HALO drone."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

try:
    from halo_swarm_common import LAB_MAPPING, load_json, save_json_atomic, utc_now
    from run_halo_swarm_mission import _sw_collect, _sw_epoch, _sw_preflight, _sw_worker
except ImportError:
    from scripts.halo_swarm_common import LAB_MAPPING, load_json, save_json_atomic, utc_now
    from scripts.run_halo_swarm_mission import _sw_collect, _sw_epoch, _sw_preflight, _sw_worker


HUMBLE_SETUP = "/opt/ros/humble/setup.bash"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--mission-dir", required=True, type=Path)
    parser.add_argument("--drone-id", required=True)
    parser.add_argument("--drone-host", required=True)
    parser.add_argument("--drone-ip", required=True)
    parser.add_argument("--ros-domain-id", required=True, type=int)
    parser.add_argument("--px4-msgs-workspace", required=True)
    parser.add_argument("--post-landing-record-s", type=float, default=3.0)
    parser.add_argument("--duration", type=float, default=180.0)
    parser.add_argument("--status-interval-s", type=float, default=1.0)
    parser.add_argument("--remote-sync-root", default="/home/root/halo_sync_test")
    parser.add_argument("--auto-ulog", dest="auto_ulog", action="store_true", default=True)
    parser.add_argument("--no-auto-ulog", dest="auto_ulog", action="store_false")
    parser.add_argument("--enable-ground-rosbag", dest="enable_ground_rosbag", action="store_true", default=True)
    parser.add_argument("--no-ground-rosbag", dest="enable_ground_rosbag", action="store_false")
    parser.add_argument("--enable-audio", action="store_true")
    parser.add_argument("--preflight-only", action="store_true")
    parser.add_argument("--audio-device", default="hw:0,0")
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--channels", type=int, default=6)
    parser.add_argument("--sample-format", default="S16_LE")
    parser.add_argument("--_env-ready", action="store_true", help=argparse.SUPPRESS)
    return parser


def _fixed_environment(args: argparse.Namespace) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "ROS_DOMAIN_ID": str(args.ros_domain_id),
            "ROS_LOCALHOST_ONLY": "0",
            "FASTRTPS_DEFAULT_PROFILES_FILE": str(Path(__file__).resolve().parents[1] / "config" / "fastdds" / f"humble_{args.drone_id}.xml"),
            "RMW_IMPLEMENTATION": "rmw_fastrtps_cpp",
            "HALO_FIXED_WORKER_ENV": "1",
        }
    )
    return env


def _ensure_fixed_environment(args: argparse.Namespace) -> None:
    expected = _fixed_environment(args)
    if not args._env_ready:
        workspace = str(Path(args.px4_msgs_workspace).expanduser())
        command = [sys.executable, str(Path(__file__).resolve()), *sys.argv[1:], "--_env-ready"]
        shell = (
            "set -e; "
            f"source {shlex.quote(HUMBLE_SETUP)}; "
            f"source {shlex.quote(workspace)}; "
            f"export ROS_DOMAIN_ID={shlex.quote(expected['ROS_DOMAIN_ID'])}; "
            "export ROS_LOCALHOST_ONLY=0; "
            f"export FASTRTPS_DEFAULT_PROFILES_FILE={shlex.quote(expected['FASTRTPS_DEFAULT_PROFILES_FILE'])}; "
            "export RMW_IMPLEMENTATION=rmw_fastrtps_cpp; "
            "export HALO_FIXED_WORKER_ENV=1; "
            "exec " + shlex.join(command)
        )
        os.execvpe("bash", ["bash", "-lc", shell], expected)
    mismatches = [
        f"{key}={os.environ.get(key)!r}, expected {value!r}"
        for key, value in expected.items()
        if key.startswith("ROS_") and os.environ.get(key) != value
    ]
    if mismatches:
        raise RuntimeError("Worker ROS environment is not fixed as requested: " + "; ".join(mismatches))


def _drone_descriptor(args: argparse.Namespace, local_dir: Path) -> dict:
    remote_root = args.remote_sync_root.rstrip("/")
    remote_dir = f"/{args.mission_id}__{args.drone_id}" if not remote_root else f"{remote_root}/{args.mission_id}__{args.drone_id}"
    return {
        "drone_id": args.drone_id,
        "ssh_host": args.drone_host,
        "ip_address": args.drone_ip,
        "ros_domain_id": args.ros_domain_id,
        "static_peer": args.drone_ip,
        "config_errors": [],
        "local_dir": str(local_dir),
        "remote_mission_dir": remote_dir,
        "px4_root": "/data/px4/log",
        "audio_device": args.audio_device,
        "sample_rate": args.sample_rate,
        "channels": args.channels,
        "sample_format": args.sample_format,
    }


def _worker_namespace(args: argparse.Namespace, local_dir: Path, remote_dir: str, drone_ok: bool) -> argparse.Namespace:
    values = vars(args).copy()
    values.update(
        {
            "drone_dir": str(local_dir),
            "ip_address": args.drone_ip,
            "ssh_host": args.drone_host,
            "remote_mission_dir": remote_dir,
            "status_path": str(local_dir / "metadata" / "worker_status.json"),
            "drone_ros_ok": drone_ok,
        }
    )
    return SimpleNamespace(**values)


def _write_phase(path: Path, args: argparse.Namespace, phase: str, **extra: object) -> None:
    value = {
        "mission_id": args.mission_id,
        "drone_id": args.drone_id,
        "drone_host": args.drone_host,
        "drone_ip": args.drone_ip,
        "ros_domain_id": args.ros_domain_id,
        "static_peer": args.drone_ip,
        "phase": phase,
        "state": phase,
        "safety": "Passive observer only; no flight-control commands.",
    }
    value.update(extra)
    save_json_atomic(path, value)


def _augment_collection(local_dir: Path, args: argparse.Namespace, drone_check: dict) -> dict:
    final_path = local_dir / "metadata" / "worker_final.json"
    final = load_json(final_path)
    ground = final.get("ground_preflight", {}) if isinstance(final, dict) else {}
    final.update(
        {
            "drone_host": args.drone_host,
            "drone_ip": args.drone_ip,
            "drone_ros_check": drone_check,
            "ground_ros_check": ground,
            "visible_topics": ground.get("visible_topics", []),
            "px4_msgs_decode_ok": bool(ground.get("px4_msgs_decode_ok")),
        }
    )
    save_json_atomic(final_path, final)
    collection_path = local_dir / "metadata" / "collection_manifest.json"
    collection = load_json(collection_path)
    status = load_json(local_dir / "metadata" / "worker_status.json")
    collection.update(
        {
            "drone_host": args.drone_host,
            "drone_ip": args.drone_ip,
            "drone_ros_check": drone_check,
            "ground_ros_check": ground,
            "visible_topics": ground.get("visible_topics", []),
            "px4_msgs_decode_ok": bool(ground.get("px4_msgs_decode_ok")),
            "ready_utc": status.get("ready_utc"),
            "connection_loss": final.get("connection_loss_events", []),
            "termination_reason": final.get("termination_reason"),
        }
    )
    save_json_atomic(collection_path, collection)
    return collection


def _failed_worker(args: argparse.Namespace, local_dir: Path, drone_check: dict, reason: str, errors: list[str]) -> int:
    final = {
        "mission_id": args.mission_id,
        "drone_id": args.drone_id,
        "drone_host": args.drone_host,
        "drone_ip": args.drone_ip,
        "ros_domain_id": args.ros_domain_id,
        "static_peer": args.drone_ip,
        "state": "FAILED_PREFLIGHT",
        "phase": "FAILED_PREFLIGHT",
        "drone_ros_check": drone_check,
        "ground_ros_check": None,
        "drone_ros_ok": False,
        "ground_ros_ok": False,
        "px4_msgs_decode_ok": bool(drone_check.get("px4_msgs_decode_ok")),
        "warnings": [],
        "errors": errors,
        "termination_reason": reason,
        "finalized_utc": utc_now(),
    }
    save_json_atomic(local_dir / "metadata" / "worker_final.json", final)
    save_json_atomic(local_dir / "metadata" / "worker_status.json", final)
    return final


def run_worker(args: argparse.Namespace) -> int:
    expected = LAB_MAPPING.get(args.drone_id)
    if expected is None:
        raise ValueError(f"Unknown lab drone_id: {args.drone_id}")
    for field, actual in (("drone-host", args.drone_host), ("drone-ip", args.drone_ip), ("ros-domain-id", args.ros_domain_id)):
        key = {"drone-host": "ssh_host", "drone-ip": "ip_address", "ros-domain-id": "ros_domain_id"}[field]
        if actual != expected[key]:
            raise ValueError(f"{args.drone_id}: --{field} must be {expected[key]!r}; found {actual!r}")
    if args.ros_domain_id not in range(1, 8):
        raise ValueError("--ros-domain-id must be in the range 1-7")
    if not 2 <= args.post_landing_record_s <= 5:
        raise ValueError("--post-landing-record-s must be between 2 and 5")
    if args.duration <= 0:
        raise ValueError("--duration must be greater than zero")
    _ensure_fixed_environment(args)
    mission_dir = args.mission_dir.expanduser().resolve()
    local_dir = mission_dir / "drones" / args.drone_id
    for name in ("ros_bags", "px4_logs", "audio", "metadata", "status_logs", "processed"):
        (local_dir / name).mkdir(parents=True, exist_ok=True)
    status_path = local_dir / "metadata" / "worker_status.json"
    _write_phase(status_path, args, "CHECKING_DRONE_ROS")
    drone = _drone_descriptor(args, local_dir)
    drone_check = _sw_preflight(drone)
    save_json_atomic(local_dir / "metadata" / "drone_preflight.json", drone_check)
    if not drone_check.get("ok"):
        _failed_worker(args, local_dir, drone_check, "drone_ros_preflight_failed", list(drone_check.get("errors", [])))
        worker_args = _worker_namespace(args, local_dir, drone["remote_mission_dir"], False)
        mission_start = load_json(args.mission_dir.expanduser().resolve() / "metadata" / "mission_metadata.json").get("mission_start_utc")
        _sw_collect(worker_args, drone, _sw_epoch(mission_start) or 0)
        _augment_collection(local_dir, args, drone_check)
        return 2
    _write_phase(
        status_path,
        args,
        "CHECKING_GROUND_ROS",
        drone_ros_ok=True,
        drone_ros_check=drone_check,
    )
    worker_args = _worker_namespace(args, local_dir, drone["remote_mission_dir"], True)
    if args.preflight_only:
        worker_args.enable_audio = False
        worker_args.enable_ground_rosbag = False
        worker_args.duration = min(worker_args.duration, 0.25)
    result = _sw_worker(worker_args)
    _augment_collection(local_dir, args, drone_check)
    # The worker has already stopped its bag and audio before this per-drone collection step.
    mission_start = load_json(args.mission_dir.expanduser().resolve() / "metadata" / "mission_metadata.json").get("mission_start_utc")
    collection = _sw_collect(worker_args, drone, _sw_epoch(mission_start) or 0)
    _augment_collection(local_dir, args, drone_check)
    return 1 if result or collection.get("collection_status") == "failed" else 0


def main() -> int:
    args = _parser().parse_args()
    try:
        return run_worker(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        mission_dir = args.mission_dir.expanduser().resolve()
        local_dir = mission_dir / "drones" / args.drone_id
        local_dir.joinpath("metadata").mkdir(parents=True, exist_ok=True)
        failure = {
            "mission_id": args.mission_id,
            "drone_id": args.drone_id,
            "drone_host": args.drone_host,
            "drone_ip": args.drone_ip,
            "ros_domain_id": args.ros_domain_id,
            "static_peer": args.drone_ip,
            "phase": "FAILED_ENVIRONMENT",
            "state": "FAILED_ENVIRONMENT",
            "errors": [f"{type(exc).__name__}: {exc}"],
            "termination_reason": "worker_environment_failure",
            "finalized_utc": utc_now(),
        }
        save_json_atomic(local_dir / "metadata" / "worker_final.json", failure)
        save_json_atomic(local_dir / "metadata" / "worker_status.json", failure)
        print(f"{args.drone_id}: {failure['errors'][0]}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
