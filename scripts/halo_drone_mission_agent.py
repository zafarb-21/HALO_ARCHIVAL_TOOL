#!/usr/bin/env python3

"""Drone-side recorder and flight-state monitor for one HALO mission."""

import argparse
import calendar
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


VEHICLE_STATUS_TOPIC = "/fmu/out/vehicle_status"
TIMESYNC_TOPIC = "/fmu/out/timesync_status"
ROS2_FOXY_SETUP = "/opt/ros/foxy/setup.bash"
_termination_signal = None


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def shell_join(command):
    """Return a shell-readable command without Python 3.8 shlex.join."""
    return " ".join(shlex.quote(str(part)) for part in command)


def add_unique(items, message):
    if message not in items:
        items.append(message)


def write_text_atomic(path, text):
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def save_json_atomic(path, data):
    write_text_atomic(path, json.dumps(data, indent=2) + "\n")


def command_result(cmd, timeout=10.0):
    try:
        result = subprocess.run(
            cmd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            timeout=timeout,
        )
        return {
            "command": shell_join(cmd),
            "return_code": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "ok": result.returncode == 0,
        }
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
        return {
            "command": shell_join(cmd),
            "return_code": 124,
            "stdout": stdout.strip(),
            "stderr": (stderr.strip() or f"Timed out after {timeout:g} seconds"),
            "ok": False,
        }
    except OSError as exc:
        return {
            "command": shell_join(cmd),
            "return_code": 127,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "ok": False,
        }


def ros2_command(arguments):
    """Run ROS2 after loading Foxy while preserving a JSON-readable command."""
    ros_command = shell_join(arguments)
    setup = shlex.quote(ROS2_FOXY_SETUP)
    shell_command = ". {0} && exec {1}".format(setup, ros_command)
    return ["bash", "-lc", shell_command]


def discover_ros2_topics():
    result = command_result(
        ros2_command(["ros2", "topic", "list"]), timeout=8.0
    )
    if not result["ok"]:
        detail = result["stderr"] or result["stdout"] or "unknown error"
        return [], f"ROS2 topics are unavailable: {detail.splitlines()[0]}"

    topics = sorted({line.strip() for line in result["stdout"].splitlines() if line.strip()})
    if not topics:
        return [], "ROS2 returned no topics; ROS bag recording and flight-state monitoring are unavailable."
    return topics, None


def build_rosbag_plan(args, available_topics, rosbag_path):
    requested = list(args.rosbag_topics or [])
    if requested:
        selected = [topic for topic in requested if topic in available_topics]
        missing = [topic for topic in requested if topic not in available_topics]
        if not selected:
            return None, selected, missing
        ros_arguments = [
            "ros2",
            "bag",
            "record",
            "-o",
            str(rosbag_path),
        ] + selected
    elif available_topics:
        selected = list(available_topics)
        missing = []
        ros_arguments = [
            "ros2",
            "bag",
            "record",
            "-a",
            "-o",
            str(rosbag_path),
        ]
    else:
        return None, [], []
    return ros2_command(ros_arguments), selected, missing


def parse_utc_epoch(value):
    """Parse an ISO-8601 UTC timestamp without datetime.fromisoformat (Python 3.6)."""
    if value is None:
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1]
    if normalized.endswith("+00:00"):
        normalized = normalized[:-6]
    parsed = None
    for timestamp_format in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(normalized, timestamp_format)
            break
        except ValueError:
            pass
    if parsed is None:
        raise ValueError("expected an ISO-8601 UTC timestamp ending in Z or +00:00")
    return calendar.timegm(parsed.utctimetuple()) + parsed.microsecond / 1000000.0


def parse_ros_scalar(value):
    value = value.strip()
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if re.fullmatch(r"[-+]?\d+", value):
        return int(value)
    return value.strip("'\"")


def parse_vehicle_status(output):
    fields = {}
    for line in output.splitlines():
        match = re.match(r"^\s*([A-Za-z][A-Za-z0-9_]*)\s*:\s*(.*?)\s*$", line)
        if match:
            fields[match.group(1)] = parse_ros_scalar(match.group(2))

    arming_state = fields.get("arming_state")
    explicit_armed = fields.get("armed")
    armed = None
    if isinstance(explicit_armed, bool):
        armed = explicit_armed
    elif isinstance(arming_state, int):
        if arming_state == 2:
            armed = True
        elif arming_state in {0, 1, 3, 4, 5}:
            armed = False
    elif isinstance(arming_state, str):
        upper_state = arming_state.upper()
        if "DISARMED" in upper_state:
            armed = False
        elif "ARMED" in upper_state:
            armed = True

    failsafe = fields.get("failsafe") is True
    emergency = False
    emergency_fields = {}
    for key in (
        "failure_detector_status",
        "failure_status",
        "emergency_state",
        "crash_detected",
    ):
        value = fields.get(key)
        if value is True or (isinstance(value, int) and value != 0):
            emergency = True
            emergency_fields[key] = value

    if failsafe:
        state = "failsafe"
    elif emergency:
        state = "emergency_or_failure"
    elif armed is True:
        state = "armed"
    elif armed is False:
        state = "disarmed"
    elif arming_state is not None:
        state = f"arming_state_{arming_state}"
    else:
        state = "unknown"

    return {
        "available": True,
        "state": state,
        "armed": armed,
        "arming_state": arming_state,
        "nav_state": fields.get("nav_state"),
        "failsafe": failsafe,
        "emergency": emergency,
        "emergency_fields": emergency_fields,
        "fields": fields,
        "raw": output[-12000:],
    }


def read_vehicle_status(status_interval_s):
    timeout = max(1.0, min(5.0, status_interval_s))
    result = command_result(
        ros2_command([
            "ros2",
            "topic",
            "echo",
            VEHICLE_STATUS_TOPIC,
            "--once",
            "--qos-reliability",
            "best_effort",
        ]),
        timeout=timeout,
    )
    if not result["ok"]:
        return {
            "available": False,
            "state": "unavailable",
            "armed": None,
            "failsafe": False,
            "emergency": False,
            "error": result["stderr"] or result["stdout"] or "vehicle_status read failed",
            "command": result["command"],
        }
    parsed = parse_vehicle_status(result["stdout"])
    parsed["command"] = result["command"]
    return parsed


def process_running(process):
    return process is not None and process.poll() is None


def stop_process(process, label, warnings):
    if process is None:
        return None
    if process.poll() is not None:
        return process.returncode

    try:
        os.killpg(process.pid, signal.SIGINT)
        return process.wait(timeout=12.0)
    except subprocess.TimeoutExpired:
        add_unique(warnings, f"{label} did not stop after SIGINT; SIGTERM was sent.")
    except ProcessLookupError:
        return process.poll()

    try:
        os.killpg(process.pid, signal.SIGTERM)
        return process.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        add_unique(warnings, f"{label} did not stop after SIGTERM; SIGKILL was sent.")
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return process.wait(timeout=5.0)
    except ProcessLookupError:
        return process.poll()


def newest_ulog_candidates():
    command = "find /data/px4/log -name '*.ulg' -printf '%T@ %p\\n' | sort -n | tail -10"
    result = command_result(["sh", "-c", command], timeout=15.0)
    output = result["stdout"].rstrip()
    candidates = []
    for line in output.splitlines():
        parts = line.strip().split(maxsplit=1)
        if len(parts) == 2:
            candidates.append(parts[1])
    warning = None
    if not result["ok"]:
        detail = result["stderr"] or result["stdout"] or "unknown error"
        warning = f"Could not list PX4 ULog candidates: {detail.splitlines()[0]}"
    return output, candidates, warning


def signal_handler(signum, _frame):
    global _termination_signal
    _termination_signal = signum


def validate_args(parser, args):
    identifier_pattern = r"[A-Za-z0-9._-]+"
    if re.fullmatch(identifier_pattern, args.mission_id or "") is None:
        parser.error("--mission-id must be a safe single folder name")
    if args.drone_id is not None:
        if re.fullmatch(identifier_pattern, args.drone_id) is None:
            parser.error("--drone-id must be a safe identifier")
    if args.drone_session_id is None:
        args.drone_session_id = args.mission_id
    elif re.fullmatch(identifier_pattern, args.drone_session_id) is None:
        parser.error("--drone-session-id must be a safe single folder name")
    if args.drone_id is not None and args.drone_session_id != args.mission_id:
        expected_session_id = args.mission_id + "__" + args.drone_id
        if args.drone_session_id != expected_session_id:
            parser.error(
                "--drone-session-id must equal <mission-id>__<drone-id> in swarm mode"
            )
    mission_dir = Path(args.mission_dir).expanduser().resolve()
    if mission_dir.name != args.drone_session_id:
        parser.error(
            "--mission-dir folder name must exactly match --drone-session-id "
            "(or --mission-id in single-drone mode)"
        )
    if args.duration <= 0:
        parser.error("--duration must be greater than zero")
    if args.status_interval_s <= 0:
        parser.error("--status-interval-s must be greater than zero")
    if args.post_disarm_wait_s < 0:
        parser.error("--post-disarm-wait-s cannot be negative")
    if args.sample_rate <= 0 or args.channels <= 0:
        parser.error("--sample-rate and --channels must be greater than zero")
    for topic in args.rosbag_topics:
        if not topic.startswith("/"):
            parser.error("--rosbag-topics values must be absolute ROS2 topic names")
    try:
        args.start_at_epoch = parse_utc_epoch(args.start_at_utc)
    except (AttributeError, ValueError) as exc:
        parser.error("--start-at-utc: {0}".format(exc))
    return mission_dir


def build_status(
    args,
    hostname,
    started_utc,
    phase,
    audio_process,
    rosbag_process,
    flight_state,
    flight_monitoring_available,
    timesync_available,
    armed_seen,
    disarm_detected_utc,
    warnings,
    errors,
    termination_reason=None,
):
    return {
        "mission_id": args.mission_id,
        "drone_id": args.drone_id,
        "drone_session_id": args.drone_session_id,
        "drone_hostname": hostname,
        "start_at_utc": args.start_at_utc,
        "agent_pid": os.getpid(),
        "agent_started_utc": started_utc,
        "current_utc": utc_now(),
        "phase": phase,
        "termination_reason": termination_reason,
        "audio_enabled": args.enable_audio,
        "audio_process_running": process_running(audio_process),
        "audio_process_return_code": (
            audio_process.poll() if audio_process is not None else None
        ),
        "rosbag_enabled": args.enable_rosbag,
        "rosbag_process_running": process_running(rosbag_process),
        "rosbag_process_return_code": (
            rosbag_process.poll() if rosbag_process is not None else None
        ),
        "flight_state_monitoring_available": flight_monitoring_available,
        "current_detected_flight_state": flight_state.get("state", "unavailable"),
        "flight_state": flight_state,
        "armed_seen": armed_seen,
        "disarm_detected_utc": disarm_detected_utc,
        "ros2_timesync_available": timesync_available,
        "warnings": list(warnings),
        "errors": list(errors),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mission-id", required=True)
    parser.add_argument("--drone-id")
    parser.add_argument("--drone-session-id")
    parser.add_argument("--mission-dir", required=True)
    parser.add_argument("--start-at-utc")
    parser.add_argument("--duration", required=True, type=float)
    parser.add_argument("--audio-device", default="hw:0,0")
    parser.add_argument("--sample-rate", default=16000, type=int)
    parser.add_argument("--channels", default=6, type=int)
    parser.add_argument("--format", default="S16_LE")
    parser.add_argument("--enable-audio", action="store_true")
    parser.add_argument("--enable-rosbag", action="store_true")
    parser.add_argument(
        "--rosbag-topics",
        nargs="+",
        default=[],
        help="Optional ROS2 topic list. When omitted, all visible topics are recorded.",
    )
    parser.add_argument("--status-interval-s", default=2.0, type=float)
    parser.add_argument("--post-disarm-wait-s", default=10.0, type=float)
    args = parser.parse_args()
    mission_dir = validate_args(parser, args)

    audio_dir = mission_dir / "audio"
    metadata_dir = mission_dir / "metadata"
    status_logs_dir = mission_dir / "status_logs"
    bags_dir = mission_dir / "bags"
    px4_logs_dir = mission_dir / "px4_logs"
    for folder in (mission_dir, audio_dir, metadata_dir, status_logs_dir, bags_dir, px4_logs_dir):
        folder.mkdir(parents=True, exist_ok=True)

    start_path = metadata_dir / "drone_agent_start.json"
    status_path = metadata_dir / "drone_agent_status.json"
    final_path = metadata_dir / "drone_agent_final.json"
    pid_path = metadata_dir / "drone_agent_pid.txt"
    if final_path.exists():
        parser.error(f"Refusing to overwrite an existing finalized mission: {final_path}")
    if start_path.exists():
        parser.error(f"Refusing to overwrite an existing agent start record: {start_path}")

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    hostname = socket.gethostname()
    started_utc = utc_now()
    started_monotonic = time.monotonic()
    warnings = []
    errors = []

    # Write this before ROS discovery so the ground side can verify that the
    # uploaded script parsed and started under the drone's Python interpreter.
    bootstrap_start_record = {
        "mission_id": args.mission_id,
        "drone_id": args.drone_id,
        "drone_session_id": args.drone_session_id,
        "mission_dir": str(mission_dir),
        "drone_hostname": hostname,
        "agent_pid": os.getpid(),
        "started_utc": started_utc,
        "start_at_utc": args.start_at_utc,
        "phase": "initializing",
        "python_version": sys.version,
        "python_version_info": list(sys.version_info[:3]),
        "python_executable": sys.executable,
        "duration_s": args.duration,
        "audio_enabled": args.enable_audio,
        "rosbag_enabled": args.enable_rosbag,
    }
    save_json_atomic(start_path, bootstrap_start_record)
    write_text_atomic(pid_path, f"{os.getpid()}\n")

    topics, topic_warning = discover_ros2_topics()
    if topic_warning:
        add_unique(warnings, topic_warning)
    vehicle_topic_available = VEHICLE_STATUS_TOPIC in topics
    timesync_available = TIMESYNC_TOPIC in topics
    if not vehicle_topic_available:
        add_unique(
            warnings,
            f"ROS2 flight-state topic {VEHICLE_STATUS_TOPIC} is unavailable; duration fallback is active.",
        )
    if not timesync_available:
        add_unique(
            warnings,
            f"ROS2 timesync topic {TIMESYNC_TOPIC} is unavailable; recording continues without ROS2 timesync telemetry.",
        )

    audio_path = audio_dir / "respeaker_6ch.wav"
    arecord_command = [
        "arecord",
        "-D",
        args.audio_device,
        "-f",
        args.format,
        "-r",
        str(args.sample_rate),
        "-c",
        str(args.channels),
        "-t",
        "wav",
        str(audio_path),
    ]
    bag_name = "rosbag2_{0}".format(args.drone_session_id)
    rosbag_path = bags_dir / bag_name
    rosbag_command, rosbag_selected_topics, rosbag_missing_topics = (
        build_rosbag_plan(args, topics, rosbag_path)
    )
    if args.enable_rosbag and rosbag_missing_topics:
        add_unique(
            warnings,
            "Requested ROS2 topics unavailable at startup: {0}".format(
                ", ".join(rosbag_missing_topics)
            ),
        )

    start_record = {
        "mission_id": args.mission_id,
        "drone_id": args.drone_id,
        "drone_session_id": args.drone_session_id,
        "mission_dir": str(mission_dir),
        "drone_hostname": hostname,
        "agent_pid": os.getpid(),
        "started_utc": started_utc,
        "start_at_utc": args.start_at_utc,
        "python_version": sys.version,
        "python_version_info": list(sys.version_info[:3]),
        "python_executable": sys.executable,
        "duration_s": args.duration,
        "status_interval_s": args.status_interval_s,
        "post_disarm_wait_s": args.post_disarm_wait_s,
        "audio": {
            "enabled": args.enable_audio,
            "device": args.audio_device,
            "sample_rate": args.sample_rate,
            "channels": args.channels,
            "format": args.format,
            "output_path": str(audio_path),
            "command": arecord_command,
            "command_shell": shell_join(arecord_command),
        },
        "rosbag": {
            "enabled": args.enable_rosbag,
            "requested_topics": list(args.rosbag_topics),
            "selected_topics": list(rosbag_selected_topics),
            "missing_topics": list(rosbag_missing_topics),
            "output_path": str(rosbag_path),
            "command": rosbag_command,
            "command_shell": (
                shell_join(rosbag_command) if rosbag_command is not None else None
            ),
        },
        "ros2_topics_at_start": topics,
        "flight_state_topic": VEHICLE_STATUS_TOPIC,
        "timesync_topic": TIMESYNC_TOPIC,
        "safety": "This agent records and monitors only; it never arms or disarms the drone.",
    }
    save_json_atomic(start_path, start_record)
    write_text_atomic(pid_path, f"{os.getpid()}\n")

    audio_process = None
    rosbag_process = None
    audio_log_handle = None
    rosbag_log_handle = None
    audio_started_utc = None
    audio_stopped_utc = None
    critical_error = False
    pre_capture_termination_reason = None
    barrier_reached_utc = None
    barrier_lateness_s = None

    if args.start_at_epoch is not None:
        while _termination_signal is None:
            remaining = args.start_at_epoch - time.time()
            if remaining <= 0:
                break
            waiting_status = dict(bootstrap_start_record)
            waiting_status.update(
                {
                    "current_utc": utc_now(),
                    "phase": "waiting_for_start_barrier",
                    "seconds_until_start": round(remaining, 3),
                    "warnings": list(warnings),
                    "errors": list(errors),
                }
            )
            save_json_atomic(status_path, waiting_status)
            time.sleep(min(args.status_interval_s, remaining, 0.5))
        if _termination_signal is not None:
            pre_capture_termination_reason = (
                "user_interrupt_before_capture"
                if _termination_signal == signal.SIGINT
                else "termination_signal_before_capture"
            )

    barrier_reached_utc = utc_now()
    if args.start_at_epoch is not None:
        barrier_lateness_s = max(0.0, time.time() - args.start_at_epoch)
        if barrier_lateness_s > 0.25:
            add_unique(
                warnings,
                "Start barrier was reached {0:.3f} seconds late.".format(
                    barrier_lateness_s
                ),
            )
    capture_started_monotonic = time.monotonic()
    start_record["barrier_reached_utc"] = barrier_reached_utc
    start_record["barrier_lateness_s"] = barrier_lateness_s
    start_record["phase"] = (
        "capture_starting"
        if pre_capture_termination_reason is None
        else "finalizing_before_capture"
    )
    start_record["warnings"] = list(warnings)
    save_json_atomic(start_path, start_record)

    if args.enable_audio and pre_capture_termination_reason is None:
        if audio_path.exists():
            add_unique(errors, f"Audio output already exists and was not overwritten: {audio_path}")
            critical_error = True
        elif shutil.which("arecord") is None:
            add_unique(errors, "arecord is unavailable; ReSpeaker audio could not start.")
            critical_error = True
        else:
            try:
                audio_log_handle = (status_logs_dir / "arecord.log").open("a", encoding="utf-8")
                audio_process = subprocess.Popen(
                    arecord_command,
                    stdout=audio_log_handle,
                    stderr=subprocess.STDOUT,
                    universal_newlines=True,
                    start_new_session=True,
                )
                audio_started_utc = utc_now()
                write_text_atomic(
                    metadata_dir / "audio_start_utc.txt", audio_started_utc + "\n"
                )
            except OSError as exc:
                add_unique(errors, f"Could not start arecord: {type(exc).__name__}: {exc}")
                critical_error = True

    if (
        args.enable_rosbag
        and rosbag_command is not None
        and pre_capture_termination_reason is None
    ):
        try:
            rosbag_log_handle = (status_logs_dir / "ros2_bag.log").open(
                "a", encoding="utf-8"
            )
            rosbag_process = subprocess.Popen(
                rosbag_command,
                stdout=rosbag_log_handle,
                stderr=subprocess.STDOUT,
                universal_newlines=True,
                start_new_session=True,
            )
        except OSError as exc:
            add_unique(
                warnings,
                "Could not start ROS2 bag recording: {0}: {1}".format(
                    type(exc).__name__, exc
                ),
            )
    elif args.enable_rosbag and pre_capture_termination_reason is None:
        add_unique(
            warnings,
            "ROS2 bag recording was requested but none of the requested ROS2 "
            "topics were available; audio/PX4 workflow continues.",
        )

    if audio_process is not None:
        time.sleep(0.25)
        if audio_process.poll() is not None:
            add_unique(
                errors,
                "arecord exited during startup with code {0}.".format(
                    audio_process.returncode
                ),
            )
            critical_error = True

    last_flight_state = {
        "available": False,
        "state": "unavailable",
        "armed": None,
        "failsafe": False,
        "emergency": False,
    }
    previous_armed = None
    armed_seen = False
    disarm_detected_utc = None
    disarm_deadline = None
    consecutive_state_failures = 0
    rosbag_exit_recorded = False
    next_topic_retry = time.monotonic() + 10.0
    termination_reason = pre_capture_termination_reason
    if termination_reason is None and critical_error:
        termination_reason = "audio_start_failed"

    try:
        while termination_reason is None:
            loop_started = time.monotonic()
            elapsed = loop_started - capture_started_monotonic

            if _termination_signal is not None:
                termination_reason = (
                    "user_interrupt" if _termination_signal == signal.SIGINT else "termination_signal"
                )
                break

            if args.enable_audio and audio_process is not None and audio_process.poll() is not None:
                add_unique(errors, f"arecord exited unexpectedly with code {audio_process.returncode}.")
                termination_reason = "audio_process_exited"
                critical_error = True
                break

            if (
                args.enable_rosbag
                and rosbag_process is not None
                and rosbag_process.poll() is not None
                and not rosbag_exit_recorded
            ):
                add_unique(
                    warnings,
                    "ROS2 bag recorder exited with code {0}; audio/PX4 workflow "
                    "continues.".format(rosbag_process.returncode),
                )
                rosbag_exit_recorded = True

            retry_ros_discovery = (
                not vehicle_topic_available
                or (args.enable_rosbag and rosbag_process is None)
            )
            if retry_ros_discovery and loop_started >= next_topic_retry:
                topics, topic_warning = discover_ros2_topics()
                next_topic_retry = loop_started + 10.0
                if topic_warning:
                    add_unique(warnings, topic_warning)
                vehicle_topic_available = VEHICLE_STATUS_TOPIC in topics
                timesync_available = TIMESYNC_TOPIC in topics

                if args.enable_rosbag and rosbag_process is None:
                    (
                        retry_command,
                        retry_selected_topics,
                        retry_missing_topics,
                    ) = build_rosbag_plan(args, topics, rosbag_path)
                    if retry_command is not None:
                        rosbag_command = retry_command
                        rosbag_selected_topics = retry_selected_topics
                        rosbag_missing_topics = retry_missing_topics
                        start_record["rosbag"]["command"] = rosbag_command
                        start_record["rosbag"]["command_shell"] = shell_join(
                            rosbag_command
                        )
                        start_record["rosbag"]["selected_topics"] = list(
                            rosbag_selected_topics
                        )
                        start_record["rosbag"]["missing_topics"] = list(
                            rosbag_missing_topics
                        )
                        save_json_atomic(start_path, start_record)
                        try:
                            if rosbag_log_handle is None:
                                rosbag_log_handle = (
                                    status_logs_dir / "ros2_bag.log"
                                ).open("a", encoding="utf-8")
                            rosbag_process = subprocess.Popen(
                                rosbag_command,
                                stdout=rosbag_log_handle,
                                stderr=subprocess.STDOUT,
                                universal_newlines=True,
                                start_new_session=True,
                            )
                        except OSError as exc:
                            add_unique(
                                warnings,
                                "Could not start ROS2 bag recording: {0}: {1}".format(
                                    type(exc).__name__, exc
                                ),
                            )

            if vehicle_topic_available:
                last_flight_state = read_vehicle_status(args.status_interval_s)
                if last_flight_state.get("available"):
                    consecutive_state_failures = 0
                    current_armed = last_flight_state.get("armed")
                    if current_armed is True:
                        armed_seen = True
                        if disarm_deadline is not None:
                            add_unique(warnings, "Re-arm detected during post-disarm wait; finalization was deferred.")
                            disarm_deadline = None
                            disarm_detected_utc = None
                    elif previous_armed is True and current_armed is False:
                        disarm_detected_utc = utc_now()
                        disarm_deadline = time.monotonic() + args.post_disarm_wait_s

                    if last_flight_state.get("failsafe"):
                        termination_reason = "failsafe_detected"
                    elif last_flight_state.get("emergency"):
                        termination_reason = "crash_or_emergency_detected"
                    previous_armed = current_armed
                else:
                    consecutive_state_failures += 1
                    if (
                        armed_seen
                        and disarm_deadline is None
                        and consecutive_state_failures >= 3
                    ):
                        termination_reason = "flight_state_lost_after_armed"

            if termination_reason is None and disarm_deadline is not None:
                if time.monotonic() >= disarm_deadline:
                    termination_reason = "armed_to_disarmed"

            if termination_reason is None and elapsed >= args.duration and disarm_deadline is None:
                if not vehicle_topic_available or not last_flight_state.get("available"):
                    termination_reason = "duration_complete_no_flight_state"
                elif last_flight_state.get("armed") is True:
                    termination_reason = "duration_timeout_while_armed"
                elif armed_seen:
                    termination_reason = "duration_complete_after_flight_state"
                else:
                    termination_reason = "duration_complete_no_arm_detected"

            phase = "post_disarm_wait" if disarm_deadline is not None else "running"
            status = build_status(
                args,
                hostname,
                started_utc,
                phase,
                audio_process,
                rosbag_process,
                last_flight_state,
                vehicle_topic_available,
                timesync_available,
                armed_seen,
                disarm_detected_utc,
                warnings,
                errors,
            )
            save_json_atomic(status_path, status)

            remaining = args.status_interval_s - (time.monotonic() - loop_started)
            if remaining > 0:
                time.sleep(remaining)
    except Exception as exc:
        add_unique(
            errors,
            f"Unhandled drone-agent exception: {type(exc).__name__}: {exc}",
        )
        termination_reason = "drone_agent_exception"
        critical_error = True
    finally:
        audio_return_code = stop_process(audio_process, "arecord", warnings)
        if audio_started_utc is not None:
            audio_stopped_utc = utc_now()
            write_text_atomic(
                metadata_dir / "audio_stop_utc.txt", audio_stopped_utc + "\n"
            )
        else:
            write_text_atomic(metadata_dir / "audio_start_utc.txt", "")
            write_text_atomic(metadata_dir / "audio_stop_utc.txt", "")
        rosbag_return_code = stop_process(rosbag_process, "ROS2 bag recorder", warnings)
        if audio_log_handle is not None:
            audio_log_handle.close()
        if rosbag_log_handle is not None:
            rosbag_log_handle.close()

    termination_reason = termination_reason or "agent_finalized_without_reason"
    ulog_output, ulog_candidates, ulog_warning = newest_ulog_candidates()
    write_text_atomic(
        metadata_dir / "px4_ulog_candidates.txt",
        (ulog_output + "\n") if ulog_output else "",
    )
    if ulog_warning:
        add_unique(warnings, ulog_warning)

    write_text_atomic(metadata_dir / "termination_reason.txt", termination_reason + "\n")
    final_status = build_status(
        args,
        hostname,
        started_utc,
        "finalized",
        audio_process,
        rosbag_process,
        last_flight_state,
        vehicle_topic_available,
        timesync_available,
        armed_seen,
        disarm_detected_utc,
        warnings,
        errors,
        termination_reason,
    )
    final_status["finalized_utc"] = utc_now()
    final_status["elapsed_s"] = round(time.monotonic() - started_monotonic, 3)
    final_status["capture_elapsed_s"] = round(
        time.monotonic() - capture_started_monotonic, 3
    )
    final_status["start_barrier"] = {
        "requested_start_at_utc": args.start_at_utc,
        "reached_utc": barrier_reached_utc,
        "lateness_s": barrier_lateness_s,
    }
    final_status["audio"] = {
        "path": str(audio_path),
        "exists": audio_path.is_file(),
        "size_bytes": audio_path.stat().st_size if audio_path.is_file() else None,
        "started_utc": audio_started_utc,
        "stopped_utc": audio_stopped_utc,
        "return_code": audio_return_code,
        "command": arecord_command,
        "command_shell": shell_join(arecord_command),
    }
    final_status["rosbag"] = {
        "path": str(rosbag_path),
        "exists": rosbag_path.exists(),
        "return_code": rosbag_return_code,
        "requested_topics": list(args.rosbag_topics),
        "selected_topics": list(rosbag_selected_topics),
        "missing_topics": list(rosbag_missing_topics),
        "command": rosbag_command,
        "command_shell": (
            shell_join(rosbag_command) if rosbag_command is not None else None
        ),
    }
    final_status["px4_ulog_candidates"] = ulog_candidates
    save_json_atomic(status_path, final_status)
    save_json_atomic(final_path, final_status)

    print(f"HALO drone mission agent finalized: {termination_reason}")
    return 1 if critical_error else 0


if __name__ == "__main__":
    raise SystemExit(main())
