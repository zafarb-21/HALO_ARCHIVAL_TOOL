#!/usr/bin/env bash
# Launch five fixed-domain passive HALO archive workers as background jobs.
# Each worker is an independent Bash subshell with an immutable ROS environment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
COMMON="$SCRIPT_DIR/halo_swarm_common.py"
WORKER="$SCRIPT_DIR/run_halo_drone_worker.py"
SWARM_CONFIG="$REPO_ROOT/drones/swarm_lab.yaml"
PROFILE="$REPO_ROOT/profiles/audio_px4_sync_test.yaml"
ARCHIVE_ROOT="${HALO_ARCHIVE_ROOT:-$HOME/MIC_ARRAY_ROS/HALO_ARCHIVE}"
PX4_WORKSPACE="$HOME/MIC_ARRAY_ROS/px4_ros2_jazzy_ws/install/setup.bash"
MISSION_NAME="lab_swarm_test_001"
OPERATOR="${USER:-operator}"
MISSION_ID=""
DURATION_S=180
STATUS_INTERVAL_S=1
PREFLIGHT_TIMEOUT_S=120
REMOTE_SYNC_ROOT="/home/root/halo_sync_test"
ENABLE_AUDIO=0
NO_AUTO_ULOG=0
ALLOW_PARTIAL=0
PREFLIGHT_ONLY=0
DRY_RUN=0
SHUTDOWN_WAIT_S=120

DRONE_IDS=(D0012 D0013 D0014 D0015 D0016)
DRONE_IPS=(192.168.0.20 192.168.0.21 192.168.0.22 192.168.0.23 192.168.0.24)
DRONE_HOSTS=(halo-d0012 halo-d0013 halo-d0014 halo-d0015 halo-d0016)
DRONE_DOMAINS=(3 4 5 6 7)
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10)
TEMP_DIR=""
MISSION_DIR=""
SHUTDOWN_IN_PROGRESS=0
declare -A WORKER_PIDS=()
declare -A WORKER_REAPED=()
declare -A WORKER_EXIT_CODES=()
declare -A LAST_STATUS=()

usage() {
  cat <<'USAGE'
Usage: launch_halo_swarm.sh [options]

Starts five independent fixed-domain archive workers as background Bash jobs and
remains alive until all workers finish. No graphical terminal is required.

  --mission-name NAME       Mission name (default: lab_swarm_test_001)
  --operator NAME           Operator name
  --mission-id ID           Common mission ID; generated when omitted
  --duration SECONDS        Per-worker monitoring window (default: 180)
  --enable-audio            Enable the existing per-drone ReSpeaker path
  --no-auto-ulog            Save candidates without copying the selected ULog
  --allow-partial-swarm     Permit ready workers to continue if another fails
  --preflight-only          Run workers through preflight, then finish safely
  --headless                Compatibility option; no GUI is required
  --dry-run                 Validate config and print mapping; no SSH/archive/ROS
  --swarm PATH              Swarm YAML override
  --profile PATH            Flight/profile YAML override
  --archive-root PATH       Archive root override
  --px4-msgs-workspace PATH PX4 message workspace setup.bash override
  --preflight-timeout S     READY barrier timeout (default: 120)
  --status-interval S       Status polling interval (default: 1)
  -h, --help                Show this help

The launcher never sends ARM, takeoff, flight, LAND, DISARM, or trajectory
commands. Ground Station A remains responsible for flight control.
USAGE
}

while (($#)); do
  case "$1" in
    --mission-name) MISSION_NAME="$2"; shift 2 ;;
    --operator) OPERATOR="$2"; shift 2 ;;
    --mission-id) MISSION_ID="$2"; shift 2 ;;
    --duration) DURATION_S="$2"; shift 2 ;;
    --enable-audio) ENABLE_AUDIO=1; shift ;;
    --no-auto-ulog) NO_AUTO_ULOG=1; shift ;;
    --allow-partial-swarm) ALLOW_PARTIAL=1; shift ;;
    --preflight-only) PREFLIGHT_ONLY=1; shift ;;
    --headless) shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --swarm) SWARM_CONFIG="$2"; shift 2 ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --archive-root) ARCHIVE_ROOT="$2"; shift 2 ;;
    --px4-msgs-workspace) PX4_WORKSPACE="$2"; shift 2 ;;
    --preflight-timeout) PREFLIGHT_TIMEOUT_S="$2"; shift 2 ;;
    --status-interval) STATUS_INTERVAL_S="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [[ ! -f "$COMMON" || ! -f "$WORKER" ]]; then
  echo "Missing swarm support file under $SCRIPT_DIR" >&2
  exit 2
fi
if [[ ! -f "$SWARM_CONFIG" || ! -f "$PROFILE" ]]; then
  echo "Swarm config and profile must exist" >&2
  exit 2
fi
PX4_WORKSPACE="${PX4_WORKSPACE/#\~/$HOME}"
if [[ ! -f "$PX4_WORKSPACE" ]]; then
  echo "px4_msgs workspace setup file does not exist: $PX4_WORKSPACE" >&2
  exit 2
fi
if ! [[ "$DURATION_S" =~ ^[0-9]+$ && "$PREFLIGHT_TIMEOUT_S" =~ ^[0-9]+$ ]]; then
  echo "--duration and --preflight-timeout must be whole seconds" >&2
  exit 2
fi
if ! [[ "$STATUS_INTERVAL_S" =~ ^([0-9]+([.][0-9]+)?|[.][0-9]+)$ ]] || [[ "$STATUS_INTERVAL_S" =~ ^0+([.]0+)?$ ]]; then
  echo "--status-interval must be a positive number" >&2
  exit 2
fi

python3 "$COMMON" validate --swarm "$SWARM_CONFIG" --code-root "$REPO_ROOT"
slug="$(printf '%s' "$MISSION_NAME" | tr -cs 'A-Za-z0-9._-' '_' | sed 's/^_\+//; s/_\+$//')"
[[ -n "$slug" ]] || slug="lab_swarm_test"
[[ -n "$MISSION_ID" ]] || MISSION_ID="$(date -u +%Y%m%d_%H%M%S)_UTC_${slug}"

if (( DRY_RUN == 1 )); then
  echo "DRY RUN: no SSH, clock sync, microdds restart, archive, ROS, bag, audio, or flight command will run."
  for index in "${!DRONE_IDS[@]}"; do
    echo "${DRONE_IDS[$index]} | ${DRONE_IPS[$index]} | ROS_DOMAIN_ID=${DRONE_DOMAINS[$index]} | ${DRONE_HOSTS[$index]}"
    echo "  ROS_STATIC_PEERS=${DRONE_IPS[$index]}"
  done
  exit 0
fi

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/halo-swarm-launch.XXXXXX")"
PREFLIGHT_RESULTS="$TEMP_DIR/preflight_results.json"
printf '{}\n' > "$PREFLIGHT_RESULTS"

cleanup_temp() {
  [[ -z "$TEMP_DIR" || ! -d "$TEMP_DIR" ]] || rm -rf "$TEMP_DIR"
}
trap cleanup_temp EXIT

append_result() {
  local drone_id="$1" kind="$2" ok="$3" detail="$4"
  python3 "$COMMON" append-preflight --results-file "$PREFLIGHT_RESULTS" \
    --drone-id "$drone_id" --kind "$kind" --ok "$ok" --detail "$detail"
}

get_phase() {
  python3 - "$1" <<'PY'
import json
import sys
from pathlib import Path
try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    value = {}
print(value.get("phase") or value.get("state") or "NOT_STARTED")
PY
}

get_status_detail() {
  python3 - "$1" <<'PY'
import json
import sys
from pathlib import Path
try:
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    value = {}
bag = value.get("bag") if isinstance(value.get("bag"), dict) else {}
size_mb = float(bag.get("size_bytes", 0) or 0) / 1048576.0
remaining = value.get("post_landing_remaining_s")
remaining_text = "" if remaining is None else f"{float(remaining):.1f} s remaining"
print("\t".join((str(value.get("phase") or value.get("state") or "NOT_STARTED"),
                  str(value.get("vehicle_state") or "UNKNOWN"), f"{size_mb:.1f} MB",
                  remaining_text)))
PY
}

pid_alive() {
  local pid="$1" stat
  kill -0 "$pid" 2>/dev/null || return 1
  stat="$(ps -o stat= -p "$pid" 2>/dev/null | tr -d ' ')"
  [[ -n "$stat" && "${stat:0:1}" != Z ]]
}

reap_worker() {
  local drone_id="$1" code=0
  [[ "${WORKER_REAPED[$drone_id]:-0}" == 1 ]] && return 0
  set +e
  wait "${WORKER_PIDS[$drone_id]}"
  code=$?
  set -e
  WORKER_EXIT_CODES["$drone_id"]="$code"
  WORKER_REAPED["$drone_id"]=1
}

signal_workers() {
  local signal_name="$1" drone_id pid
  for drone_id in "${DRONE_IDS[@]}"; do
    pid="${WORKER_PIDS[$drone_id]:-}"
    [[ -n "$pid" ]] || continue
    pid_alive "$pid" && kill "-$signal_name" "$pid" 2>/dev/null || true
  done
}

wait_for_workers() {
  local deadline=$((SECONDS + SHUTDOWN_WAIT_S)) any_alive=1 drone_id
  while (( SECONDS < deadline )); do
    any_alive=0
    for drone_id in "${DRONE_IDS[@]}"; do
      pid_alive "${WORKER_PIDS[$drone_id]:-0}" && any_alive=1
    done
    (( any_alive == 0 )) && break
    sleep 1
  done
  if (( any_alive != 0 )); then
    echo "Workers did not exit after ${SHUTDOWN_WAIT_S}s; sending SIGTERM (never SIGKILL)." >&2
    signal_workers TERM
  fi
  for drone_id in "${DRONE_IDS[@]}"; do
    [[ -n "${WORKER_PIDS[$drone_id]:-}" ]] && reap_worker "$drone_id"
  done
}

finish_mission() {
  local reason="$1"
  [[ -n "$MISSION_DIR" && -d "$MISSION_DIR" ]] || return 0
  python3 "$COMMON" aggregate --mission-dir "$MISSION_DIR" --termination-reason "$reason" || true
}

handle_interrupt() {
  local signal_name="$1"
  (( SHUTDOWN_IN_PROGRESS == 1 )) && return 0
  SHUTDOWN_IN_PROGRESS=1
  trap - INT TERM
  echo "Received SIG${signal_name}; stopping archive workers without sending flight commands." >&2
  signal_workers INT
  wait_for_workers
  finish_mission "launcher_interrupted"
  exit 130
}

GROUND_EPOCH="$(date -u +%s)"
for index in "${!DRONE_IDS[@]}"; do
  drone_id="${DRONE_IDS[$index]}"; host="${DRONE_HOSTS[$index]}"; ip="${DRONE_IPS[$index]}"
  if detail="$(ssh "${SSH_OPTS[@]}" "$host" "hostname; hostname -I; hostname -I | tr ' ' '\\n' | grep -Fx '$ip'" 2>&1)"; then
    append_result "$drone_id" ssh true "$detail"; echo "$drone_id SSH/IP OK | $host | $ip"
  else
    append_result "$drone_id" ssh false "$detail"; echo "$drone_id SSH/IP FAILED | $detail" >&2
  fi
  if detail="$(ssh "${SSH_OPTS[@]}" "$host" "date -u -s '@${GROUND_EPOCH}' >/dev/null && date -u +%Y-%m-%dT%H:%M:%S.%NZ" 2>&1)"; then
    append_result "$drone_id" clock_sync true "ground_reference_epoch=$GROUND_EPOCH; $detail"; echo "$drone_id UTC SYNC OK | $detail"
  else
    append_result "$drone_id" clock_sync false "ground_reference_epoch=$GROUND_EPOCH; $detail"; echo "$drone_id UTC SYNC FAILED | $detail" >&2
  fi
  if detail="$(ssh "${SSH_OPTS[@]}" "$host" "systemctl start voxl-microdds-agent && systemctl is-active voxl-microdds-agent" 2>&1)"; then
    append_result "$drone_id" microdds true "$detail"; echo "$drone_id microdds OK | $detail"
  else
    append_result "$drone_id" microdds false "$detail"; echo "$drone_id microdds FAILED | $detail" >&2
  fi
done

MISSION_DIR="$(python3 "$COMMON" prepare --archive-root "$ARCHIVE_ROOT" --mission-id "$MISSION_ID" \
  --mission-name "$MISSION_NAME" --operator "$OPERATOR" --code-root "$REPO_ROOT" \
  --swarm "$SWARM_CONFIG" --profile "$PROFILE")"
python3 "$COMMON" merge-preflight --mission-dir "$MISSION_DIR" --results-file "$PREFLIGHT_RESULTS"
echo "Common mission ID: $MISSION_ID"
echo "Mission archive: $MISSION_DIR"
trap 'handle_interrupt INT' INT
trap 'handle_interrupt TERM' TERM

post_roll_for() {
  python3 - "$MISSION_DIR/metadata/launcher_context.json" "$1" <<'PY'
import json
import sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print(next(item["post_landing_record_s"] for item in data["drones"] if item["drone_id"] == sys.argv[2]))
PY
}

launch_worker() {
  local drone_id="$1" host="$2" ip="$3" domain="$4" post_roll="$5"
  local worker_log="$MISSION_DIR/drones/$drone_id/status_logs/worker_console.log"
  local worker_command=(python3 "$WORKER" --mission-id "$MISSION_ID" --mission-dir "$MISSION_DIR"
    --drone-id "$drone_id" --drone-host "$host" --drone-ip "$ip" --ros-domain-id "$domain"
    --px4-msgs-workspace "$PX4_WORKSPACE" --post-landing-record-s "$post_roll"
    --remote-sync-root "$REMOTE_SYNC_ROOT" --duration "$DURATION_S"
    --status-interval-s "$STATUS_INTERVAL_S" --enable-ground-rosbag)
  if (( NO_AUTO_ULOG == 0 )); then worker_command+=(--auto-ulog); else worker_command+=(--no-auto-ulog); fi
  (( ENABLE_AUDIO == 1 )) && worker_command+=(--enable-audio)
  (( PREFLIGHT_ONLY == 1 )) && worker_command+=(--preflight-only)

  printf 'Fixed worker: %s | ROS_DOMAIN_ID=%s | ROS_STATIC_PEERS=%s\n' "$drone_id" "$domain" "$ip" \
    > "$MISSION_DIR/drones/$drone_id/status_logs/worker_launch_command.sh"
  # The parentheses and the trailing & are the process-isolation boundary.
  (
    set -euo pipefail
    cd "$REPO_ROOT"
    source /opt/ros/jazzy/setup.bash
    source "$PX4_WORKSPACE"
    export ROS_DOMAIN_ID="$domain"
    export ROS_LOCALHOST_ONLY=0
    export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET
    export ROS_STATIC_PEERS="$ip"
    export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
    exec "${worker_command[@]}"
  ) > "$worker_log" 2>&1 &
  WORKER_PIDS["$drone_id"]="$!"
  WORKER_REAPED["$drone_id"]=0
  echo "$drone_id background worker started | PID=${WORKER_PIDS[$drone_id]} | domain=$domain | log=$worker_log"
}

# Five explicit calls keep the fixed lab mapping visible. Each call creates a
# separate background job and captures its own $! immediately.
launch_worker D0012 halo-d0012 192.168.0.20 3 "$(post_roll_for D0012)"
launch_worker D0013 halo-d0013 192.168.0.21 4 "$(post_roll_for D0013)"
launch_worker D0014 halo-d0014 192.168.0.22 5 "$(post_roll_for D0014)"
launch_worker D0015 halo-d0015 192.168.0.23 6 "$(post_roll_for D0015)"
launch_worker D0016 halo-d0016 192.168.0.24 7 "$(post_roll_for D0016)"

echo "Worker inspection: jobs -l"
for drone_id in "${DRONE_IDS[@]}"; do
  echo "Worker log: tail -f $MISSION_DIR/drones/$drone_id/status_logs/worker_console.log"
done

readiness_deadline=$((SECONDS + PREFLIGHT_TIMEOUT_S)); ready_count=0; readiness_reason="required_workers_not_ready"
while (( SECONDS < readiness_deadline )); do
  ready_count=0; terminal_count=0; dead_before_ready=()
  for index in "${!DRONE_IDS[@]}"; do
    drone_id="${DRONE_IDS[$index]}"; status_path="$MISSION_DIR/drones/$drone_id/metadata/worker_status.json"
    phase="$(get_phase "$status_path")"
    [[ "${LAST_STATUS[$drone_id]:-}" == "$phase" ]] || {
      detail="$(get_status_detail "$status_path")"; IFS=$'\t' read -r _phase vehicle bag_size remaining <<< "$detail"
      echo "$drone_id | PID ${WORKER_PIDS[$drone_id]} | $phase | domain=${DRONE_DOMAINS[$index]} | $vehicle | bag $bag_size${remaining:+ | $remaining}"
      LAST_STATUS["$drone_id"]="$phase"
    }
    [[ "$phase" == READY ]] && (( ready_count += 1 ))
    if [[ "$phase" == DONE || "$phase" == FAILED_PREFLIGHT || "$phase" == FAILED_ENVIRONMENT ]]; then
      (( terminal_count += 1 )); [[ "$phase" == READY ]] || dead_before_ready+=("$drone_id")
    elif ! pid_alive "${WORKER_PIDS[$drone_id]}"; then
      dead_before_ready+=("$drone_id")
    fi
  done
  (( ready_count == 5 )) && { readiness_reason="all_required_workers_ready"; break; }
  if (( ALLOW_PARTIAL == 1 && ready_count > 0 && ${#dead_before_ready[@]} > 0 )); then
    readiness_reason="partial_workers_ready"; break
  fi
  (( terminal_count == 5 || ${#dead_before_ready[@]} > 0 )) && break
  sleep "$STATUS_INTERVAL_S"
done

if (( ready_count == 5 )); then
  echo "================================================"; echo "              SWARM ARCHIVE READY"; echo "================================================"
  for index in "${!DRONE_IDS[@]}"; do
    drone_id="${DRONE_IDS[$index]}"
    echo "$drone_id READY | PID=${WORKER_PIDS[$drone_id]} | domain=${DRONE_DOMAINS[$index]} | ${DRONE_IPS[$index]}"
  done
elif (( ALLOW_PARTIAL == 1 && ready_count > 0 && readiness_reason == partial_workers_ready )); then
  echo "SWARM ARCHIVE PARTIAL READY (--allow-partial-swarm)"
else
  echo "SWARM ARCHIVE NOT READY: all five required workers did not reach READY" >&2
  for index in "${!DRONE_IDS[@]}"; do
    drone_id="${DRONE_IDS[$index]}"
    echo "$drone_id phase=$(get_phase "$MISSION_DIR/drones/$drone_id/metadata/worker_status.json") | PID=${WORKER_PIDS[$drone_id]} | log=$MISSION_DIR/drones/$drone_id/status_logs/worker_console.log" >&2
  done
  signal_workers INT; wait_for_workers; finish_mission "$readiness_reason"; exit 1
fi

if (( PREFLIGHT_ONLY == 1 )); then
  signal_workers INT; wait_for_workers; finish_mission "preflight_only"
  echo "Preflight-only run complete; no flight command was issued."; exit 0
fi

echo "Waiting for ARMED state on each drone. Flight control remains with Ground Station A."
echo "The launcher remains active; inspect jobs with: jobs -l"
while :; do
  running=0
  for index in "${!DRONE_IDS[@]}"; do
    drone_id="${DRONE_IDS[$index]}"; pid="${WORKER_PIDS[$drone_id]}"
    if pid_alive "$pid"; then running=1; elif [[ "${WORKER_REAPED[$drone_id]:-0}" == 0 ]]; then
      reap_worker "$drone_id"
      echo "$drone_id worker exited | PID=$pid | exit_code=${WORKER_EXIT_CODES[$drone_id]} | log=$MISSION_DIR/drones/$drone_id/status_logs/worker_console.log"
    fi
    detail="$(get_status_detail "$MISSION_DIR/drones/$drone_id/metadata/worker_status.json")"
    IFS=$'\t' read -r phase vehicle bag_size remaining <<< "$detail"
    signature="$phase|$vehicle|$bag_size|$remaining"
    if [[ "${LAST_STATUS[$drone_id]:-}" != "$signature" ]]; then
      echo "$drone_id | PID $pid | $vehicle | $phase | bag $bag_size${remaining:+ | $remaining}"
      LAST_STATUS["$drone_id"]="$signature"
    fi
  done
  (( running == 0 )) && break
  python3 "$COMMON" aggregate --mission-dir "$MISSION_DIR" >/dev/null || true
  sleep "$STATUS_INTERVAL_S"
done

for drone_id in "${DRONE_IDS[@]}"; do reap_worker "$drone_id"; done
failed_exit=0
for drone_id in "${DRONE_IDS[@]}"; do
  code="${WORKER_EXIT_CODES[$drone_id]:-1}"
  echo "$drone_id worker exit code: $code | PID=${WORKER_PIDS[$drone_id]}"
  [[ "$code" == 0 ]] || failed_exit=1
done
if (( failed_exit == 0 )); then finish_mission "workers_completed"; else finish_mission "completed_with_worker_failures"; fi
echo "Swarm archive complete: $MISSION_DIR"
