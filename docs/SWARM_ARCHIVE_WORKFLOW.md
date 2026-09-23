# HALO five-drone swarm archive runbook

This is the operational guide for the current passive five-drone archive
architecture. Ground Station A controls the flight. Ground Station B runs the
archive launcher only: it observes ROS 2 state, records ground bags, collects
ULogs/audio, and writes metadata. It never arms, takes off, flies, lands,
disarms, or sends trajectory commands.

The validated single-drone command remains separate:

    scripts/run_halo_mission.py

Do not replace it with the swarm launcher or change its flags for a single-drone
D0012 run.

## 1. Fixed lab mapping

Do not guess or change these values:

    D0012  192.168.0.20  halo-d0012  ROS_DOMAIN_ID=3
    D0013  192.168.0.21  halo-d0013  ROS_DOMAIN_ID=4
    D0014  192.168.0.22  halo-d0014  ROS_DOMAIN_ID=5
    D0015  192.168.0.23  halo-d0015  ROS_DOMAIN_ID=6
    D0016  192.168.0.24  halo-d0016  ROS_DOMAIN_ID=7

Every worker also uses its own drone IP as `ROS_STATIC_PEERS`.
`ros_domain_id` is required in both `drones/swarm_lab.yaml` and the matching
drone YAML. Missing or mismatched values fail validation; the launcher never
guesses a domain.

## 2. Required ground-side environment

The launcher establishes this environment independently inside each worker:

    source /opt/ros/jazzy/setup.bash
    source ~/MIC_ARRAY_ROS/px4_ros2_jazzy_ws/install/setup.bash
    export ROS_DOMAIN_ID=<that drone's domain>
    export ROS_LOCALHOST_ONLY=0
    export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET
    export ROS_STATIC_PEERS=<that drone's IP>
    export RMW_IMPLEMENTATION=rmw_fastrtps_cpp

The launcher does not continuously switch domains in one ROS process. It starts
five background jobs using separate Bash subshells:

    (
        source /opt/ros/jazzy/setup.bash
        source ~/MIC_ARRAY_ROS/px4_ros2_jazzy_ws/install/setup.bash
        export ROS_DOMAIN_ID=3
        export ROS_LOCALHOST_ONLY=0
        export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET
        export ROS_STATIC_PEERS=192.168.0.20
        export RMW_IMPLEMENTATION=rmw_fastrtps_cpp
        exec python3 scripts/run_halo_drone_worker.py ...
    ) > "$MISSION_DIR/drones/D0012/status_logs/worker_console.log" 2>&1 &
    PID_D0012=$!

The other four workers use their own fixed domain/IP pair. The parent launcher
stores all five PIDs, monitors them, and waits for them independently. GNOME
terminals and `nohup` are not required.

## 3. Pre-run safety checks

From the archive repository on Ground Station B:

    cd ~/MIC_ARRAY_ROS/HALO_ARCHIVE_TOOL

Confirm the workspace setup file exists:

    test -f ~/MIC_ARRAY_ROS/px4_ros2_jazzy_ws/install/setup.bash

Confirm the swarm configuration without contacting drones:

    python3 scripts/halo_swarm_common.py validate \
      --swarm drones/swarm_lab.yaml \
      --code-root "$PWD"

Confirm passwordless SSH and the expected drone addresses:

    for host in halo-d0012 halo-d0013 halo-d0014 halo-d0015 halo-d0016; do
      ssh -o BatchMode=yes -o ConnectTimeout=10 "$host" 'hostname; hostname -I'
    done

The live launcher repeats the SSH/IP validation, synchronizes each drone clock
to the archive computer UTC clock, and starts/checks `voxl-microdds-agent` before
workers begin ROS preflight.

## 4. Safe configuration-only dry run

This validates the mapping and performs no SSH, clock sync, ROS, archive, bag,
audio, ULog, or flight operation:

    scripts/launch_halo_swarm.sh \
      --mission-name "swarm_test_001" \
      --operator "Operator Name" \
      --dry-run

Expected output includes all five fixed domain/IP pairs and ends with:

    SWARM CONFIGURATION OK
    DRY RUN: no SSH, clock sync, microdds restart, archive, ROS, bag, audio, or flight command will run.

## 5. Live no-flight preflight

Run this before the first flight test:

    scripts/launch_halo_swarm.sh \
      --mission-name "swarm_preflight_001" \
      --operator "Operator Name" \
      --preflight-only

This is a live archive preflight, so it does perform SSH, clock synchronization,
MicroDDS checks, archive creation, and ROS topic checks. It does not issue any
flight command. For each drone, the worker performs this order:

1. SSH to that drone.
2. Source `/opt/ros/foxy/setup.bash` on the drone.
3. Set the drone's fixed domain and `ROS_LOCALHOST_ONLY=0`.
4. Restart/check the drone ROS 2 daemon.
5. Confirm `/fmu/out/sensor_combined` and `/fmu/out/vehicle_status`.
6. Start the corresponding fixed ground environment.
7. Restart/check ground ROS discovery for that domain.
8. Confirm ground `/fmu` topics.
9. Confirm `px4_msgs` decoding, including `sensor_combined`.
10. Write `phase=READY` only after all checks pass.

The launcher prints `SWARM ARCHIVE READY` only when all five required workers
report READY. If a required worker exits or fails before READY, the launcher
prints its status and log path, does not print READY, stops the remaining archive
workers cleanly, and preserves the partial manifests.

## 6. Start the real swarm archive

Use one ordinary terminal and leave it open for the whole mission:

    scripts/launch_halo_swarm.sh \
      --mission-name "lab_swarm_test_001" \
      --operator "Operator Name" \
      --duration 180

Add audio only when it is wanted and the existing ReSpeaker path is ready:

    scripts/launch_halo_swarm.sh \
      --mission-name "lab_swarm_test_001" \
      --operator "Operator Name" \
      --duration 180 \
      --enable-audio

Audio is optional. Without `--enable-audio`, the workers do not run `arecord` and
do not report missing-audio warnings. Automatic ULog selection is enabled by
default; use `--no-auto-ulog` only when candidates should be recorded without
copying a selected ULog.

The old filename remains usable as a compatibility wrapper:

    scripts/launch_halo_swarm_terminals.sh [the same options]

It now invokes the background-worker launcher and does not open terminals.

## 7. What happens during the mission

The launcher creates one common mission ID and mission directory, then starts
five workers concurrently. Each worker writes only to its own directory:

    HALO_ARCHIVE/<mission_id>/
      metadata/
      drones/D0012/ros_bags/
      drones/D0012/px4_logs/
      drones/D0012/audio/
      drones/D0012/metadata/
      drones/D0012/status_logs/
      drones/D0013/...
      drones/D0014/...
      drones/D0015/...
      drones/D0016/...

Each worker waits for actual ROS vehicle state:

    READY
      -> WAITING_FOR_ARM
      -> confirmed ARMED
      -> RECORDING
      -> LANDED_POSTROLL
      -> STOPPING_BAG
      -> COLLECTING_ULOG
      -> FINALIZING
      -> DONE

The bag starts only when `/fmu/out/vehicle_status` confirms the ARMED transition.
It never starts because Ground Station A sent an ARM command.

Preferred stop behavior:

    /fmu/out/vehicle_land_detected: landed=true
      -> configured post_landing_record_s interval, default 3 seconds
      -> SIGINT to ros2 bag record
      -> wait for MCAP/metadata finalization

Fallback behavior when the landed topic is unavailable:

    ARMED -> DISARMED
      -> SIGINT to ros2 bag record
      -> bag_stop_trigger=disarmed_fallback

One worker may be RECORDING while another is WAITING_FOR_ARM or CONNECTION_LOST.
A worker finishing or failing does not stop the other four.

## 8. Monitor the running archive

After the launcher prints the mission directory, set it in a second terminal:

    MISSION_ID="<mission_id>"
    MISSION_DIR="$HOME/MIC_ARRAY_ROS/HALO_ARCHIVE/$MISSION_ID"

Inspect background jobs and worker processes:

    jobs -l
    pgrep -af 'run_halo_drone_worker.py'
    pgrep -af 'ros2 bag record'

Inspect readiness/status files:

    for drone in D0012 D0013 D0014 D0015 D0016; do
      echo "=== $drone ==="
      python3 -m json.tool \
        "$MISSION_DIR/drones/$drone/metadata/worker_status.json" | \
        grep -E '"phase"|"state"|"ros_domain_id"|"armed"|"bag"'
    done

Follow an individual worker log:

    tail -f "$MISSION_DIR/drones/D0012/status_logs/worker_console.log"

The other logs are:

    $MISSION_DIR/drones/D0013/status_logs/worker_console.log
    $MISSION_DIR/drones/D0014/status_logs/worker_console.log
    $MISSION_DIR/drones/D0015/status_logs/worker_console.log
    $MISSION_DIR/drones/D0016/status_logs/worker_console.log

The parent launcher remains active after READY and waits until all workers have
finished. Do not close the launcher terminal during the mission.

## 9. Ctrl-C and abnormal worker failure

Ctrl-C on the parent launcher does not send any PX4 command. It:

1. Sends SIGINT to every still-running archive worker.
2. Lets workers stop active bags and finalize metadata.
3. Waits for worker processes to exit.
4. Records launcher termination in mission metadata.
5. Preserves each drone's existing bag, logs, and partial manifest.

If one worker crashes or loses its network connection, leave the other workers
running. The parent records the failed worker's exit/status and continues to wait
for the remaining workers. Do not delete the mission directory; it contains the
partial collection manifest, status log, bag data, and warnings/errors needed for
recovery.

For a permitted partial run, add:

    --allow-partial-swarm

The default remains all five required workers READY before the archive is declared
ready for flight.

## 10. Verify the completed archive

Verify every bag independently:

    for drone in D0012 D0013 D0014 D0015 D0016; do
      ros2 bag info \
        "$MISSION_DIR/drones/$drone/ros_bags/${MISSION_ID}__${drone}_ground_rosbag"
    done

Verify ULogs:

    for drone in D0012 D0013 D0014 D0015 D0016; do
      find "$MISSION_DIR/drones/$drone/px4_logs" \
        -type f -name '*.ulg' -print -exec ls -lh {} \;
    done

Verify per-drone manifests and mission metadata:

    for drone in D0012 D0013 D0014 D0015 D0016; do
      python3 -m json.tool \
        "$MISSION_DIR/drones/$drone/metadata/collection_manifest.json" >/dev/null
      python3 -m json.tool \
        "$MISSION_DIR/drones/$drone/metadata/worker_status.json" >/dev/null
    done
    python3 -m json.tool "$MISSION_DIR/metadata/swarm_manifest.json" >/dev/null
    python3 -m json.tool "$MISSION_DIR/metadata/mission_metadata.json" >/dev/null
    python3 -m json.tool "$MISSION_DIR/metadata/ground_orchestrator_log.json" >/dev/null

Review these fields for every drone:

- `arm_detected_utc` and `arm_detected_px4_timestamp`
- `bag_start_utc`, `bag_stop_utc`, and `bag_stop_trigger`
- `land_detected_utc` or `disarm_detected_utc`
- `bag_return_code` and bag size
- ULog candidates and selected ULog
- audio status
- connection-loss events
- warnings, errors, and termination reason

## 11. Development validation

These checks do not arm or fly any drone:

    bash -n scripts/launch_halo_swarm.sh
    bash -n scripts/launch_halo_swarm_terminals.sh
    python3 -m py_compile scripts/halo_swarm_common.py \
      scripts/run_halo_drone_worker.py \
      scripts/run_halo_swarm_mission.py \
      scripts/run_halo_mission.py

The final five-drone launcher has been validated with:

    scripts/launch_halo_swarm.sh --dry-run \
      --mission-name "validation" --operator "validation"

No automatic Git commit or flight command is part of the launcher.
