#!/usr/bin/env bash
# Launch five fixed-domain passive HALO archive workers in separate terminals.
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
HEADLESS=0
DRY_RUN=0
PREFLIGHT_ONLY=0

DRONE_IDS=(D0012 D0013 D0014 D0015 D0016)
DRONE_IPS=(192.168.0.20 192.168.0.21 192.168.0.22 192.168.0.23 192.168.0.24)
DRONE_HOSTS=(halo-d0012 halo-d0013 halo-d0014 halo-d0015 halo-d0016)
DRONE_DOMAINS=(3 4 5 6 7)
SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10)
TEMP_DIR=""

usage() {
  cat <<'USAGE'
Usage: launch_halo_swarm_terminals.sh [options]

Required workflow options:
  --mission-name NAME       Mission name (default: lab_swarm_test_001)
  --operator NAME           Operator name
  --mission-id ID           Common mission ID; generated when omitted
  --duration SECONDS        Worker monitoring window (default: 180)
  --enable-audio            Enable the existing per-drone ReSpeaker path
  --no-auto-ulog            Save candidates without copying the selected ULog
  --allow-partial-swarm    Permit ready workers to continue if another fails
  --preflight-only          Run worker preflight and stop without waiting for flight
  --headless                Launch fixed-domain workers without GNOME terminals
  --dry-run                 Validate config and print mapping; no SSH/archive/ROS
  --swarm PATH              Swarm YAML override
  --profile PATH            Flight/profile YAML override
  --archive-root PATH       Archive root override
  --px4-msgs-workspace PATH PX4 message workspace setup.bash override
  --preflight-timeout S     READY barrier timeout (default: 120)
  --status-interval S       Status polling interval (default: 1)
  -h, --help                Show this help

The launcher never sends ARM, flight, LAND, DISARM, or trajectory commands.
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
    --headless) HEADLESS=1; shift ;;
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
if [[ "$DRY_RUN" == 0 && "$HEADLESS" == 0 ]] && ! command -v gnome-terminal >/dev/null 2>&1; then
  echo "gnome-terminal is unavailable; use --headless for a non-GUI launch" >&2
  exit 2
fi

python3 "$COMMON" validate --swarm "$SWARM_CONFIG" --code-root "$REPO_ROOT"

slug="$(printf '%s' "$MISSION_NAME" | tr -cs 'A-Za-z0-9._-' '_' | sed 's/^_\+//; s/_\+$//')"
[[ -n "$slug" ]] || slug="lab_swarm_test"
if [[ -z "$MISSION_ID" ]]; then
  MISSION_ID="$(date -u +%Y%m%d_%H%M%S)_UTC_${slug}"
fi

if [[ "$DRY_RUN" == 1 ]]; then
  echo "DRY RUN: no SSH, clock sync, microdds restart, archive, ROS, terminal, bag, audio, or flight command will run."
  for index in "${!DRONE_IDS[@]}"; do
    echo "${DRONE_IDS[$index]} | ${DRONE_IPS[$index]} | ROS_DOMAIN_ID=${DRONE_DOMAINS[$index]} | ${DRONE_HOSTS[$index]}"
    echo "  ROS_STATIC_PEERS=${DRONE_IPS[$index]}"
  done
  exit 0
fi

TEMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/halo-swarm-launch.XXXXXX")"
cleanup() {
  if [[ -n "$TEMP_DIR" && -d "$TEMP_DIR" ]]; then
    rm -rf "$TEMP_DIR"
  fi
}
trap cleanup EXIT
PREFLIGHT_RESULTS="$TEMP_DIR/preflight_results.json"
printf '{}\n' > "$PREFLIGHT_RESULTS"

append_result() {
  local drone_id="$1" kind="$2" ok="$3" detail="$4"
  python3 "$COMMON" append-preflight \
    --results-file "$PREFLIGHT_RESULTS" \
    --drone-id "$drone_id" \
    --kind "$kind" \
    --ok "$ok" \
    --detail "$detail"
}

GROUND_EPOCH="$(date -u +%s)"
for index in "${!DRONE_IDS[@]}"; do
  drone_id="${DRONE_IDS[$index]}"
  host="${DRONE_HOSTS[$index]}"
  ip="${DRONE_IPS[$index]}"
  if detail="$(ssh "${SSH_OPTS[@]}" "$host" "hostname; hostname -I; hostname -I | tr ' ' '\\n' | grep -Fx '$ip'" 2>&1)"; then
    append_result "$drone_id" ssh true "$detail"
    echo "$drone_id SSH/IP OK | $host | $ip"
  else
    append_result "$drone_id" ssh false "$detail"
    echo "$drone_id SSH/IP FAILED | $detail" >&2
  fi
  if detail="$(ssh "${SSH_OPTS[@]}" "$host" "date -u -s '@${GROUND_EPOCH}' >/dev/null && date -u +%Y-%m-%dT%H:%M:%S.%NZ" 2>&1)"; then
    append_result "$drone_id" clock_sync true "ground_reference_epoch=$GROUND_EPOCH; $detail"
    echo "$drone_id UTC SYNC OK | $detail"
  else
    append_result "$drone_id" clock_sync false "ground_reference_epoch=$GROUND_EPOCH; $detail"
    echo "$drone_id UTC SYNC FAILED | $detail" >&2
  fi
  if detail="$(ssh "${SSH_OPTS[@]}" "$host" "systemctl start voxl-microdds-agent && systemctl is-active voxl-microdds-agent" 2>&1)"; then
    append_result "$drone_id" microdds true "$detail"
    echo "$drone_id microdds OK | $detail"
  else
    append_result "$drone_id" microdds false "$detail"
    echo "$drone_id microdds FAILED | $detail" >&2
  fi
done

MISSION_DIR="$(python3 "$COMMON" prepare \
  --archive-root "$ARCHIVE_ROOT" \
  --mission-id "$MISSION_ID" \
  --mission-name "$MISSION_NAME" \
  --operator "$OPERATOR" \
  --code-root "$REPO_ROOT" \
  --swarm "$SWARM_CONFIG" \
  --profile "$PROFILE")"
python3 "$COMMON" merge-preflight --mission-dir "$MISSION_DIR" --results-file "$PREFLIGHT_RESULTS"

echo "Common mission ID: $MISSION_ID"
echo "Mission archive: $MISSION_DIR"

shell_quote() { printf '%q' "$1"; }
declare -A LAST_PHASE=()
WORKER_PIDS=()

for index in "${!DRONE_IDS[@]}"; do
  drone_id="${DRONE_IDS[$index]}"
  host="${DRONE_HOSTS[$index]}"
  ip="${DRONE_IPS[$index]}"
  domain="${DRONE_DOMAINS[$index]}"
  post_roll="$(python3 -c 'import json,sys; data=json.load(open(sys.argv[1], encoding="utf-8")); print(next(item["post_landing_record_s"] for item in data["drones"] if item["drone_id"] == sys.argv[2]))' "$MISSION_DIR/metadata/launcher_context.json" "$drone_id")"
  worker_args="$(printf '%q ' python3 "$WORKER" \
    --mission-id "$MISSION_ID" \
    --mission-dir "$MISSION_DIR" \
    --drone-id "$drone_id" \
    --drone-host "$host" \
    --drone-ip "$ip" \
    --ros-domain-id "$domain" \
    --px4-msgs-workspace "$PX4_WORKSPACE" \
    --post-landing-record-s "$post_roll" \
    --remote-sync-root "$REMOTE_SYNC_ROOT" \
    --duration "$DURATION_S" \
    --status-interval-s "$STATUS_INTERVAL_S" \
    --auto-ulog \
    --enable-ground-rosbag)"
  if [[ "$NO_AUTO_ULOG" == 1 ]]; then
    worker_args="${worker_args/--auto-ulog/--no-auto-ulog}"
  fi
  if [[ "$ENABLE_AUDIO" == 1 ]]; then
    worker_args+=" --enable-audio"
  fi
  if [[ "$PREFLIGHT_ONLY" == 1 ]]; then
    worker_args+=" --preflight-only"
  fi
  worker_shell="cd $(shell_quote "$REPO_ROOT") && source /opt/ros/jazzy/setup.bash && source $(shell_quote "$PX4_WORKSPACE") && export ROS_DOMAIN_ID=$domain && export ROS_LOCALHOST_ONLY=0 && export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET && export ROS_STATIC_PEERS=$ip && export RMW_IMPLEMENTATION=rmw_fastrtps_cpp && exec $worker_args"
  printf '%s\n' "$worker_shell" > "$MISSION_DIR/drones/$drone_id/status_logs/worker_launch_command.sh"
  if [[ "$HEADLESS" == 1 ]]; then
    nohup bash -lc "$worker_shell" > "$MISSION_DIR/drones/$drone_id/status_logs/worker_terminal.log" 2>&1 &
    WORKER_PIDS[$index]="$!"
  else
    if ! command -v gnome-terminal >/dev/null 2>&1; then
      echo "gnome-terminal is unavailable; use --headless for a non-GUI launch" >&2
      exit 2
    fi
    gnome-terminal --title="HALO Archive - $drone_id - Domain $domain" -- bash -lc "$worker_shell" >/dev/null 2>&1 &
    WORKER_PIDS[$index]="$!"
  fi
done

get_phase() {
  python3 - "$1" <<'PY'
import json
import sys
from pathlib import Path
path = Path(sys.argv[1])
try:
    value = json.loads(path.read_text(encoding="utf-8"))
except (OSError, json.JSONDecodeError):
    value = {}
print(value.get("phase") or value.get("state") or "NOT_STARTED")
PY
}

readiness_deadline=$((SECONDS + PREFLIGHT_TIMEOUT_S))
ready_count=0
while (( SECONDS < readiness_deadline )); do
  ready_count=0
  terminal_count=0
  for index in "${!DRONE_IDS[@]}"; do
    drone_id="${DRONE_IDS[$index]}"
    phase="$(get_phase "$MISSION_DIR/drones/$drone_id/metadata/worker_status.json")"
    if [[ "${LAST_PHASE[$drone_id]:-}" != "$phase" ]]; then
      echo "$drone_id | $phase | domain=${DRONE_DOMAINS[$index]} | ${DRONE_IPS[$index]}"
      LAST_PHASE[$drone_id]="$phase"
    fi
    [[ "$phase" == READY ]] && ((ready_count+=1))
    [[ "$phase" == DONE || "$phase" == FAILED_PREFLIGHT || "$phase" == FAILED_ENVIRONMENT ]] && ((terminal_count+=1))
  done
  if (( ready_count == 5 )); then
    echo "SWARM ARCHIVE READY"
    for index in "${!DRONE_IDS[@]}"; do
      echo "${DRONE_IDS[$index]} READY | domain=${DRONE_DOMAINS[$index]} | ${DRONE_IPS[$index]}"
    done
    break
  fi
  if (( ALLOW_PARTIAL == 1 && ready_count > 0 )); then
    echo "SWARM ARCHIVE PARTIAL READY (--allow-partial-swarm)"
    break
  fi
  if (( terminal_count == 5 )); then
    break
  fi
  sleep "$STATUS_INTERVAL_S"
done

if (( ready_count != 5 && ALLOW_PARTIAL == 0 )); then
  echo "SWARM ARCHIVE NOT READY: all five required workers did not reach READY" >&2
  python3 "$COMMON" aggregate --mission-dir "$MISSION_DIR" --termination-reason "required_workers_not_ready"
  exit 1
fi

if (( PREFLIGHT_ONLY == 1 )); then
  python3 "$COMMON" aggregate --mission-dir "$MISSION_DIR" --termination-reason "preflight_only"
  echo "Preflight-only run complete; no flight command was issued."
  exit 0
fi

mission_deadline=$((SECONDS + DURATION_S + PREFLIGHT_TIMEOUT_S + 180))
while (( SECONDS < mission_deadline )); do
  finished=0
  for index in "${!DRONE_IDS[@]}"; do
    drone_id="${DRONE_IDS[$index]}"
    phase="$(get_phase "$MISSION_DIR/drones/$drone_id/metadata/worker_status.json")"
    if [[ "${LAST_PHASE[$drone_id]:-}" != "$phase" ]]; then
      echo "$drone_id | $phase | domain=${DRONE_DOMAINS[$index]} | ${DRONE_IPS[$index]}"
      LAST_PHASE[$drone_id]="$phase"
    fi
    [[ "$phase" == DONE || "$phase" == FAILED_PREFLIGHT || "$phase" == FAILED_ENVIRONMENT ]] && ((finished+=1))
  done
  python3 "$COMMON" aggregate --mission-dir "$MISSION_DIR"
  (( finished == 5 )) && break
  sleep "$STATUS_INTERVAL_S"
done

reason="workers_completed"
if (( SECONDS >= mission_deadline )); then
  reason="launcher_timeout_workers_retained"
fi
python3 "$COMMON" aggregate --mission-dir "$MISSION_DIR" --termination-reason "$reason"
echo "Swarm archive complete: $MISSION_DIR"
