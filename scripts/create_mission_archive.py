#!/usr/bin/env python3

from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import platform
import re
import shlex
import shutil
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def run_cmd(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a local command and return a result without raising on command failure."""
    try:
        return subprocess.run(
            cmd,
            cwd=cwd,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(
            cmd,
            returncode=127,
            stdout="",
            stderr=f"{type(exc).__name__}: {exc}",
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture_base_station_code_state(
    script_path: Path, code_root: Path | None = None
) -> dict:
    """Capture the local machine, Python, script, and Git state used for the archive."""
    script_path = script_path.expanduser().resolve()
    current_working_directory = Path.cwd().resolve()
    captured_utc = datetime.now(timezone.utc).isoformat()
    warnings: list[str] = []

    explicit_code_root = code_root.expanduser().resolve() if code_root else None

    # An explicit root is authoritative. Otherwise preserve the original discovery order.
    git_repo_root: Path | None = None
    search_paths: list[Path] = []
    candidates = (
        [explicit_code_root]
        if explicit_code_root
        else [current_working_directory, script_path.parent]
    )
    for candidate in candidates:
        if candidate not in search_paths:
            search_paths.append(candidate)

    for search_path in search_paths:
        result = run_cmd(
            ["git", "-C", str(search_path), "rev-parse", "--show-toplevel"]
        )
        if result.returncode == 0 and result.stdout.strip():
            git_repo_root = Path(result.stdout.strip()).resolve()
            break

    git_branch: str | None = None
    git_commit_hash: str | None = None
    git_status_short = ""

    if git_repo_root is None:
        if explicit_code_root:
            warnings.append(
                f"No Git repository was detected from code_root: {explicit_code_root}"
            )
        else:
            warnings.append(
                "No Git repository was detected from the current working directory "
                "or script path."
            )
    else:
        branch_result = run_cmd(
            ["git", "-C", str(git_repo_root), "branch", "--show-current"]
        )
        if branch_result.returncode == 0:
            git_branch = branch_result.stdout.strip() or "HEAD (detached)"
        else:
            warnings.append(f"Could not read Git branch: {branch_result.stderr.strip()}")

        commit_result = run_cmd(
            ["git", "-C", str(git_repo_root), "rev-parse", "HEAD"]
        )
        if commit_result.returncode == 0:
            git_commit_hash = commit_result.stdout.strip() or None
        else:
            warnings.append(f"Could not read Git commit: {commit_result.stderr.strip()}")

        status_result = run_cmd(
            ["git", "-C", str(git_repo_root), "status", "--short"]
        )
        if status_result.returncode == 0:
            git_status_short = status_result.stdout.rstrip()
        else:
            warnings.append(f"Could not read Git status: {status_result.stderr.strip()}")

    return {
        "hostname": socket.gethostname(),
        "username": getpass.getuser(),
        "timestamp_utc": captured_utc,
        "current_working_directory": str(current_working_directory),
        "code_root": str(explicit_code_root) if explicit_code_root else None,
        "git_repo_root": str(git_repo_root) if git_repo_root else None,
        "git_branch": git_branch,
        "git_commit": git_commit_hash,
        "git_commit_hash": git_commit_hash,
        "git_status_short": git_status_short,
        "has_uncommitted_changes": bool(git_status_short),
        "python_version": platform.python_version(),
        "script_path": str(script_path),
        "script_sha256": sha256_file(script_path),
        "capture_warnings": warnings,
    }


def save_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def sanitize_id_component(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return sanitized.strip("._-") or "mission"


def validate_mission_id(value: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or re.fullmatch(r"[A-Za-z0-9._-]+", value) is None
    ):
        raise ValueError(
            "mission_id must be a single folder name containing only letters, "
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


def read_drone_id(drone_file: Path) -> str | None:
    """Read the simple top-level drone_id value without requiring a YAML library."""
    for line in drone_file.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip() == "drone_id":
            drone_id = value.split("#", 1)[0].strip().strip("'\"")
            return drone_id or None
    return None


def remote_mission_folder(remote_sync_root: str, mission_id: str) -> str:
    if remote_sync_root == "/":
        return f"/{mission_id}"
    return f"{remote_sync_root}/{mission_id}"


def command_record(cmd: list[str]) -> dict:
    result = run_cmd(cmd)
    return {
        "command": shlex.join(cmd),
        "return_code": result.returncode,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
        "ok": result.returncode == 0,
    }


def command_failure(record: dict) -> str:
    detail = record["stderr"] or record["stdout"]
    if detail:
        return detail.splitlines()[0]
    return f"exit code {record['return_code']}"


def remote_command(command: str) -> str:
    return f"LC_ALL=C LANG=C sh -c {shlex.quote(command)}"


def check_ssh_connection(drone_host: str) -> dict:
    print(f"Checking SSH connection to {drone_host} ...")
    record = command_record(
        ["ssh", drone_host, remote_command("hostname && date")]
    )
    if not record["ok"]:
        raise RuntimeError(
            f"SSH connection check failed for {drone_host}: "
            f"{command_failure(record)}. Check network access, credentials, and "
            "SSH key/password login."
        )

    remote_name = record["stdout"].splitlines()[0] if record["stdout"] else drone_host
    print(f"SSH connection OK: {remote_name}")
    return record


def initialize_drone_folder(
    drone_host: str,
    remote_folder: str,
    readme_path: Path,
    drone_file: Path,
    profile_file: Path,
) -> dict:
    """Create the remote mission layout and copy non-metadata archive files."""
    initialized_utc = datetime.now(timezone.utc).isoformat()
    remote_metadata = f"{remote_folder}/metadata"
    remote_status_logs = f"{remote_folder}/status_logs"
    remote_config = f"{remote_folder}/config"

    initialization = {
        "requested": True,
        "initialized_utc": initialized_utc,
        "drone_host": drone_host,
        "remote_mission_folder": remote_folder,
        "remote_directory_created": False,
        "successful": False,
        "copied_files": [],
        "actions": [],
        "warnings": [],
        "errors": [],
    }

    quoted_directories = " ".join(
        shlex.quote(path)
        for path in [remote_folder, remote_metadata, remote_status_logs, remote_config]
    )
    mkdir_record = command_record(
        [
            "ssh",
            drone_host,
            remote_command(f"mkdir -p -- {quoted_directories}"),
        ]
    )
    initialization["actions"].append(mkdir_record)

    if not mkdir_record["ok"]:
        initialization["errors"].append(
            "Failed to create drone mission folders: "
            f"{command_failure(mkdir_record)}"
        )
        return initialization

    initialization["remote_directory_created"] = True
    files_to_copy = [
        ("README", readme_path, remote_folder),
        ("drone configuration", drone_file, remote_config),
        ("flight profile", profile_file, remote_config),
    ]

    for label, source, remote_destination in files_to_copy:
        record = command_record(
            [
                "rsync",
                "-av",
                "--rsync-path=LC_ALL=C LANG=C rsync",
                str(source),
                f"{drone_host}:{remote_destination}/",
            ]
        )
        initialization["actions"].append(record)
        if record["ok"]:
            initialization["copied_files"].append(
                f"{remote_destination}/{source.name}"
            )
        else:
            initialization["errors"].append(
                f"Failed to copy {label} to the drone: {command_failure(record)}"
            )

    initialization["successful"] = not bool(initialization["errors"])
    return initialization


def generate_next_commands(
    path: Path,
    mission_id: str,
    mission_dir: Path,
    drone_host: str,
    remote_sync_root: str,
    remote_folder: str,
) -> None:
    """Write a mission-specific, operator-run command guide."""
    collector_path = Path(__file__).resolve().with_name("collect_drone_data.py")
    content = f"""#!/usr/bin/env bash
# HALO commands generated for {mission_id}.
# Run one section with: metadata/next_commands.sh <command>
# This file never arms the drone and never runs automatically.
# The recommended no-manual-recording workflow uses scripts/run_halo_mission.py.
# These commands remain available for standalone diagnostics and recovery.

set -u

export MISSION_ID={shlex.quote(mission_id)}
export MISSION_DIR={shlex.quote(str(mission_dir))}
export DRONE_HOST={shlex.quote(drone_host)}
export REMOTE_SYNC_ROOT={shlex.quote(remote_sync_root)}
export REMOTE_MISSION_DIR={shlex.quote(remote_folder)}
export COLLECTOR={shlex.quote(str(collector_path))}

# Set this to the recorder launcher installed on the drone before start-respeaker.
# Example form: export RESPEAKER_RECORDER_COMMAND='python3 /path/to/recorder.py'
RESPEAKER_RECORDER_COMMAND="${{RESPEAKER_RECORDER_COMMAND:-}}"
TERMINATION_REASON="${{TERMINATION_REASON:-}}"
NOTES="${{NOTES:-}}"
NO_AUTO_ULOG="${{NO_AUTO_ULOG:-0}}"
# Pass zero or more verified remote ULog paths after the collect command. With no
# paths, collection searches for and normally copies the newest ULog. Set
# NO_AUTO_ULOG=1 for a dry/audio-only test that should only save candidates.

show_exports() {{
  printf 'export MISSION_DIR=%q\\n' "$MISSION_DIR"
  printf 'export MISSION_ID=%q\\n' "$MISSION_ID"
}}

check_drone_folder() {{
  ssh "$DRONE_HOST" \\
    "LC_ALL=C LANG=C test -d '$REMOTE_MISSION_DIR' && ls -la '$REMOTE_MISSION_DIR'"
}}

start_microdds() {{
  ssh "$DRONE_HOST" \\
    "LC_ALL=C LANG=C systemctl start voxl-microdds-agent && systemctl status voxl-microdds-agent --no-pager"
}}

start_respeaker() {{
  if [[ -z "$RESPEAKER_RECORDER_COMMAND" ]]; then
    echo "Set RESPEAKER_RECORDER_COMMAND to the installed drone-side recorder launcher." >&2
    echo "The generated session argument will be: --session-id '$MISSION_ID'" >&2
    return 2
  fi
  ssh -t "$DRONE_HOST" \\
    "LC_ALL=C LANG=C $RESPEAKER_RECORDER_COMMAND --session-id '$MISSION_ID'"
}}

find_ulogs() {{
  ssh "$DRONE_HOST" \\
    "LC_ALL=C LANG=C find /data/px4/log -name '*.ulg' -printf '%T@ %p\\n' | sort -nr | head -20"
}}

collect_data() {{
  local ulog_args=()
  local ulog_path
  if [[ -z "$TERMINATION_REASON" ]]; then
    echo "Set TERMINATION_REASON to the true reason the mission ended." >&2
    return 2
  fi
  for ulog_path in "$@"; do
    ulog_args+=(--ulog "$ulog_path")
  done
  if [[ "$NO_AUTO_ULOG" == "1" ]]; then
    ulog_args+=(--no-auto-ulog)
  fi

  python3 "$COLLECTOR" \\
    --mission-dir "$MISSION_DIR" \\
    --drone-host "$DRONE_HOST" \\
    --remote-sync-root "$REMOTE_SYNC_ROOT" \\
    "${{ulog_args[@]}}" \\
    --termination-reason "$TERMINATION_REASON" \\
    --notes "$NOTES"
}}

usage() {{
  cat <<'EOF'
Commands:
  exports          Print exact MISSION_DIR and MISSION_ID exports
  check-drone      Check the matching folder on the drone
  start-microdds   Start and inspect voxl-microdds-agent
  start-respeaker  Run the configured recorder with this exact MISSION_ID
  find-ulogs       List the newest PX4 ULogs after the test
  collect [ULOG ...] Run collect_drone_data.py; pass each verified remote ULog path

Before start-respeaker, export RESPEAKER_RECORDER_COMMAND with the installed launcher.
Before collect, set TERMINATION_REASON and optional NOTES. With no ULog argument, the
collector normally copies the newest candidate. Set NO_AUTO_ULOG=1 for a dry test.
EOF
}}

case "${{1:-help}}" in
  exports) show_exports ;;
  check-drone) check_drone_folder ;;
  start-microdds) start_microdds ;;
  start-respeaker) start_respeaker ;;
  find-ulogs) find_ulogs ;;
  collect) shift; collect_data "$@" ;;
  help|-h|--help) usage ;;
  *) echo "Unknown command: $1" >&2; usage >&2; exit 2 ;;
esac
"""
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a HALO mission archive folder with metadata and copied configuration files."
    )

    parser.add_argument("--mission-name", required=True, help="Short mission/test name.")
    parser.add_argument(
        "--mission-id",
        help="Fixed canonical mission/session ID. Generated automatically when omitted.",
    )
    parser.add_argument(
        "--operator", required=True, help="Name of the person running the test."
    )
    parser.add_argument("--drone", required=True, help="Path to drone YAML file.")
    parser.add_argument(
        "--profile", required=True, help="Path to flight profile YAML file."
    )
    parser.add_argument(
        "--archive-root",
        default=str(Path.home() / "MIC_ARRAY_ROS" / "HALO_ARCHIVE"),
        help="Local archive root folder.",
    )
    parser.add_argument(
        "--code-root",
        help=(
            "Optional base-station code directory used for Git discovery and status. "
            "When omitted, discover Git from the working directory and script path."
        ),
    )
    parser.add_argument(
        "--initialize-drone",
        action="store_true",
        help="Create the matching mission folder on the drone and copy setup files.",
    )
    parser.add_argument(
        "--drone-host",
        default="root@192.168.0.20",
        help="SSH target used when --initialize-drone is supplied.",
    )
    parser.add_argument(
        "--remote-sync-root",
        default="/home/root/halo_sync_test",
        help="Drone root under which the mission_id folder is created.",
    )
    parser.add_argument(
        "--skip-ssh-check",
        action="store_true",
        help="Skip the SSH preflight before --initialize-drone (advanced/offline use).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace the generated mission folder if it already exists.",
    )

    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%d_%H%M%S_UTC")

    drone_file = Path(args.drone).expanduser().resolve()
    profile_file = Path(args.profile).expanduser().resolve()
    if not drone_file.is_file():
        raise FileNotFoundError(
            f"Drone configuration file does not exist: {drone_file}"
        )
    if not profile_file.is_file():
        raise FileNotFoundError(f"Flight profile file does not exist: {profile_file}")

    code_root = Path(args.code_root).expanduser().resolve() if args.code_root else None
    if code_root is not None and not code_root.is_dir():
        parser.error(f"Code root is not a directory: {code_root}")

    drone_id = read_drone_id(drone_file)
    if args.mission_id is not None:
        try:
            mission_id = validate_mission_id(args.mission_id)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        mission_id_parts = [timestamp, sanitize_id_component(args.mission_name)]
        if drone_id:
            mission_id_parts.append(sanitize_id_component(drone_id))
        mission_id = "_".join(mission_id_parts)

    try:
        remote_sync_root = validate_remote_sync_root(args.remote_sync_root)
    except ValueError as exc:
        parser.error(str(exc))

    archive_root = Path(args.archive_root).expanduser().resolve()
    mission_dir = archive_root / mission_id
    drone_mission_folder = remote_mission_folder(remote_sync_root, mission_id)

    replaced_existing_archive = False
    if mission_dir.exists() and not args.force:
        parser.error(
            f"Mission archive already exists and was not changed: {mission_dir}. "
            "Use --force only if replacing it is intentional."
        )

    ssh_connection_check = {
        "performed": False,
        "drone_host": args.drone_host,
        "reason": "Drone initialization was not requested.",
    }
    if args.initialize_drone:
        if args.skip_ssh_check:
            ssh_connection_check["reason"] = (
                "Skipped because --skip-ssh-check was supplied."
            )
        else:
            try:
                preflight = check_ssh_connection(args.drone_host)
            except RuntimeError as exc:
                parser.exit(1, f"ERROR: {exc}\n")
            ssh_connection_check = {"performed": True, **preflight}

    # Capture code state before creating output, in case the archive root is in Git.
    code_state = capture_base_station_code_state(Path(__file__), code_root)

    if mission_dir.exists():
        if mission_dir.is_symlink() or mission_dir.is_file():
            mission_dir.unlink()
        else:
            shutil.rmtree(mission_dir)
        replaced_existing_archive = True

    folders = [
        mission_dir / "config",
        mission_dir / "metadata",
        mission_dir / "drone_data" / "px4_logs",
        mission_dir / "drone_data" / "audio",
        mission_dir / "drone_data" / "ros_bags",
        mission_dir / "drone_data" / "raw_sensor_data",
        mission_dir / "drone_data" / "status_logs",
        mission_dir / "processed",
        mission_dir / "plots",
        mission_dir / "reports",
    ]

    mission_dir.mkdir(parents=True, exist_ok=False)
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=False)

    archived_drone_file = mission_dir / "config" / drone_file.name
    archived_profile_file = mission_dir / "config" / profile_file.name
    shutil.copy2(drone_file, archived_drone_file)
    shutil.copy2(profile_file, archived_profile_file)

    code_state_path = mission_dir / "metadata" / "code_state.json"
    save_json(code_state_path, code_state)
    next_commands_path = mission_dir / "metadata" / "next_commands.sh"
    generate_next_commands(
        next_commands_path,
        mission_id,
        mission_dir,
        args.drone_host,
        remote_sync_root,
        drone_mission_folder,
    )

    warnings = list(code_state["capture_warnings"])
    if replaced_existing_archive:
        warnings.append(
            "An existing mission archive was replaced because --force was supplied."
        )

    metadata = {
        "mission_id": mission_id,
        "respeaker_session_id": mission_id,
        "mission_name": args.mission_name,
        "drone_id": drone_id,
        "operator": args.operator,
        "created_utc": now.isoformat(),
        "mission_archive_path": str(mission_dir),
        "ground_mission_folder": str(mission_dir),
        "remote_drone_mission_folder": drone_mission_folder,
        "drone_host": args.drone_host,
        "remote_sync_root": remote_sync_root,
        "code_root": code_state["code_root"],
        "drone_config_file": str(archived_drone_file),
        "flight_profile_file": str(archived_profile_file),
        "next_commands_path": str(next_commands_path),
        "ssh_connection_check": ssh_connection_check,
        "code_state": {
            "details_path": str(code_state_path),
            "captured_utc": code_state["timestamp_utc"],
            "hostname": code_state["hostname"],
            "username": code_state["username"],
            "code_root": code_state["code_root"],
            "git_repo_root": code_state["git_repo_root"],
            "git_branch": code_state["git_branch"],
            "git_commit": code_state["git_commit_hash"],
            "git_commit_hash": code_state["git_commit_hash"],
            "has_uncommitted_changes": code_state["has_uncommitted_changes"],
            "script_path": code_state["script_path"],
            "script_sha256": code_state["script_sha256"],
        },
        "run_status": {
            "current_status": "initialized",
            "started": False,
            "completed": False,
            "termination_reason": None,
            "warnings": warnings,
            "errors": [],
        },
        "expected_outputs": {
            "px4_ulog": "pending",
            "respeaker_audio": "pending",
            "ros2_bag": "pending",
            "timesync_metadata": "pending",
            "processed_csv": "pending",
            "plots": "pending",
        },
    }

    metadata_path = mission_dir / "metadata" / "mission_metadata.json"
    save_json(metadata_path, metadata)

    readme = f"""# HALO Mission Archive: {mission_id}

> **Preservation notice:** This mission folder is an archival record and should not be overwritten.

## Official mission/session ID

**{mission_id}**

The folder name is the official mission and ReSpeaker session ID. The automated ground
orchestrator passes this exact value to the drone mission agent. The ground and drone
folder names must remain identical.

- Expected ground path: {mission_dir}/
- Expected drone path: {args.drone_host}:{drone_mission_folder}/

## Mission summary

- Mission name: {args.mission_name}
- Mission ID: {mission_id}
- Drone ID: {drone_id or "not available"}
- Operator: {args.operator}
- Created UTC: {now.isoformat()}
- Drone configuration used: config/{drone_file.name} (copied from {drone_file})
- Flight profile used: config/{profile_file.name} (copied from {profile_file})

## Current mission status

**Initialized. Automated mission execution and post-mission collection are pending.**

When run through scripts/run_halo_mission.py, the drone agent records and finalizes data
without manual arecord or manual file copying. The machine-readable status, warnings,
errors, and termination reason are maintained in metadata/mission_metadata.json.
Before launch, the ground checks the uploaded agent with the drone's Python interpreter
and waits for a live startup/status record before reporting success. Audio-only runs
save ULog candidates but do not copy one automatically unless --auto-ulog is explicitly
requested.

## Expected data products

- PX4 ULog flight logs
- ReSpeaker session audio and synchronization metadata
- ROS bags and other raw sensor data, when produced by the mission
- Processed CSV or other derived data
- Plots and mission reports

## Archive layout

- Metadata, orchestrator log, and collection manifests: metadata/
- Base-station code state: metadata/code_state.json
- Mission-specific recovery command guide: metadata/next_commands.sh
- PX4 flight logs selected during collection: drone_data/px4_logs/
- Drone status and code-state logs: drone_data/status_logs/
- Complete copied drone mission tree: drone_data/audio/{mission_id}/
- ReSpeaker WAV after collection: drone_data/audio/{mission_id}/audio/respeaker_6ch.wav
- Drone-agent ROS2 bags after collection: drone_data/audio/{mission_id}/bags/
- Manually added legacy/external ROS bags, if any: drone_data/ros_bags/
- Other raw sensor data: drone_data/raw_sensor_data/
- Processed outputs: processed/
- Plots: plots/
- Reports: reports/

Keep this directory as one unit. Add mission outputs only to their designated locations;
do not reuse or overwrite this folder for another mission.
"""

    readme_path = mission_dir / "README.md"
    readme_path.write_text(readme, encoding="utf-8")

    if args.initialize_drone:
        initialization = initialize_drone_folder(
            args.drone_host,
            drone_mission_folder,
            readme_path,
            archived_drone_file,
            archived_profile_file,
        )
        metadata["drone_initialization"] = initialization
        metadata["run_status"]["warnings"].extend(initialization["warnings"])
        metadata["run_status"]["errors"].extend(initialization["errors"])
        save_json(metadata_path, metadata)

        if initialization["remote_directory_created"]:
            metadata_copy = command_record(
                [
                    "rsync",
                    "-av",
                    "--rsync-path=LC_ALL=C LANG=C rsync",
                    str(metadata_path),
                    f"{args.drone_host}:{drone_mission_folder}/metadata/",
                ]
            )
            initialization["actions"].append(metadata_copy)
            if metadata_copy["ok"]:
                initialization["copied_files"].append(
                    f"{drone_mission_folder}/metadata/{metadata_path.name}"
                )
            else:
                error = (
                    "Failed to copy mission metadata to the drone: "
                    f"{command_failure(metadata_copy)}"
                )
                initialization["errors"].append(error)
                metadata["run_status"]["errors"].append(error)

            initialization["successful"] = not bool(initialization["errors"])
            save_json(metadata_path, metadata)
    else:
        metadata["drone_initialization"] = {
            "requested": False,
            "drone_host": args.drone_host,
            "remote_mission_folder": drone_mission_folder,
            "successful": None,
            "warnings": [],
            "errors": [],
        }
        save_json(metadata_path, metadata)

    print("HALO mission archive created successfully.")
    print(f"Mission/session ID: {mission_id}")
    print(f"Ground folder     : {mission_dir}")
    print(f"Drone folder      : {args.drone_host}:{drone_mission_folder}/")
    print(f"ReSpeaker session : {mission_id}")
    print(f"Metadata file     : {metadata_path}")
    print(f"Code state        : {code_state_path}")
    print(f"Next commands     : {next_commands_path}")

    if args.initialize_drone:
        if metadata["drone_initialization"]["successful"]:
            print("Drone mission folder initialized successfully.")
        else:
            print("Drone mission folder initialization completed with errors.")
            for error in metadata["drone_initialization"]["errors"]:
                print(f"  - {error}")


if __name__ == "__main__":
    main()
