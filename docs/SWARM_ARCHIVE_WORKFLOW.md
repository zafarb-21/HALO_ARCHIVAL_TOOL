# HALO swarm archive workflow

## Identity and storage model

A swarm run creates one common mission ID for the entire mission. Every drone then
gets a session ID formed as:

    <mission_id>__<drone_id>

For example:

    mission_id: 20260904_150000_UTC_swarm_audio_px4_test
    D0012 session: 20260904_150000_UTC_swarm_audio_px4_test__D0012
    D0013 session: 20260904_150000_UTC_swarm_audio_px4_test__D0013

Each drone records locally in its own directory:

    /home/root/halo_sync_test/<mission_id>__<drone_id>/

The ground archive keeps one mission directory and a separate subtree per drone:

    ~/MIC_ARRAY_ROS/HALO_ARCHIVE/<mission_id>/
      README.md
      metadata/
        mission_metadata.json
        ground_orchestrator_log.json
        code_state.json
        swarm_collection_manifest.json
      config/
        swarm_drones.yaml
        profiles/
        drones/
      drones/
        D0012/
          drone_data/
            audio/<mission_id>__D0012/
            px4_logs/
            ros_bags/
            status_logs/
            raw_sensor_data/
          metadata/
          processed/
          plots/
          reports/
        D0013/
        D0014/
        D0015/
        D0016/

## Parallel preparation and synchronized capture

scripts/run_halo_swarm_mission.py validates the inventory and every referenced drone
configuration before creating an archive. It prepares reachable drones concurrently,
uploads the README, common mission metadata, selected profile, matching drone
configuration, and Python 3.6-compatible drone agent, then checks the uploaded agent
with that drone's Python interpreter.

After preparation, the ground computer computes one absolute start_at_utc value.
Every ready agent is launched concurrently and receives that identical value. Each
agent initializes ROS discovery, publishes a waiting status, and waits against its
local UTC clock before starting capture. Check and synchronize all system clocks
before a mission; the barrier cannot correct clock skew.

This software records and collects data only. It never arms or disarms a drone.
Arming and disarming remain manual operations under the approved flight procedure.

## Audio, ROS bags, and PX4 ULogs

Audio is written on each drone as:

    audio/respeaker_6ch.wav

When --enable-rosbag is supplied, the agent sources /opt/ros/foxy/setup.bash and
checks the topics requested by the profile. It records the requested topics that are
actually visible into:

    bags/rosbag2_<drone_session_id>/

The exact arecord and ros2 bag commands are stored in the agent start and final JSON.
If none of the requested ROS2 topics are visible, the agent records a warning and
continues audio capture and PX4 ULog collection. It retries ROS discovery while the
mission runs. The profile deliberately does not fall back to recording every topic.

With automatic ULog selection enabled, collection prefers the newest .ulg modified
after the common mission start. If no candidate can be matched confidently in swarm
mode, it preserves the candidate list, copies the newest candidate for review, marks
it selected_auto_ulog, and emits a warning. --no-auto-ulog leaves all candidates
uncopied, which is useful for audio-only tests.

## Failure and interrupt behavior

Drone initialization, launch, status, and collection results are independent. One
unreachable or failed drone does not cancel recording or collection for the others.
The global log and swarm collection manifest identify the affected drone.

If a drone crashes, loses power, or loses its network link, immediate copying may be
impossible. Data already written to its local mission folder remains there if the
storage survives. Do not delete that folder. Collection can be rerun later with the
same common mission ID and exact per-drone session ID.

On Ctrl+C, the ground orchestrator sends a best-effort SIGINT to every known running
agent, waits briefly for final status, and then starts collection from every drone.
A second interruption or unavailable hardware can still prevent a transfer.

## Rerun collection for one drone

Set the existing mission directory and use the exact drone/session identity:

    MISSION_DIR=~/MIC_ARRAY_ROS/HALO_ARCHIVE/<mission_id>
    MISSION_ID=$(basename "$MISSION_DIR")

    python3 scripts/collect_drone_data.py \
      --mission-dir "$MISSION_DIR" \
      --drone-host halo-d0013 \
      --drone-id D0013 \
      --drone-session-id "$MISSION_ID"__D0013 \
      --local-drone-dir "$MISSION_DIR"/drones/D0013 \
      --remote-sync-root /home/root/halo_sync_test \
      --termination-reason "recovery_collection" \
      --auto-ulog \
      --notes "Rerun after D0013 returned to the network"

Use --no-auto-ulog instead when recovering an audio-only session. Rerunning uses
rsync and updates that drone's manifest plus metadata/swarm_collection_manifest.json;
it does not mix files between drone IDs.

## Lab commands

Syntax checks:

    python3 -m py_compile scripts/create_mission_archive.py
    python3 -m py_compile scripts/collect_drone_data.py
    python3 -m py_compile scripts/halo_drone_mission_agent.py
    python3 -m py_compile scripts/run_halo_mission.py
    python3 -m py_compile scripts/run_halo_swarm_mission.py

Audio-only swarm recording:

    python3 scripts/run_halo_swarm_mission.py \
      --mission-name "lab_swarm_audio_auto_test_001" \
      --operator "Victor Basvi" \
      --swarm drones/swarm_lab.yaml \
      --profile profiles/audio_px4_sync_test.yaml \
      --code-root ~/MIC_ARRAY_ROS/HALO_ARCHIVE_TOOL \
      --duration 30 \
      --enable-audio \
      --no-auto-ulog

Audio plus PX4 ULog recording:

    python3 scripts/run_halo_swarm_mission.py \
      --mission-name "lab_swarm_audio_px4_test_001" \
      --operator "Victor Basvi" \
      --swarm drones/swarm_lab.yaml \
      --profile profiles/audio_px4_sync_test.yaml \
      --code-root ~/MIC_ARRAY_ROS/HALO_ARCHIVE_TOOL \
      --duration 180 \
      --enable-audio \
      --auto-ulog

Add --enable-rosbag to either command only when ROS2 bag capture is desired.

SSH checks for all five drones are listed in docs/MULTI_DRONE_SSH_SETUP.md.

Verify the newest final archive:

    MISSION_DIR=$(ls -td ~/MIC_ARRAY_ROS/HALO_ARCHIVE/* | head -1)
    MISSION_ID=$(basename "$MISSION_DIR")

    find "$MISSION_DIR" -name "respeaker_6ch.wav" -print -exec ls -lh {} \;
    find "$MISSION_DIR" -path "*/px4_logs/*.ulg" -print -exec ls -lh {} \;
    find "$MISSION_DIR" -path "*/ros_bags/*" -print | head -50
    python3 -m json.tool "$MISSION_DIR/metadata/swarm_collection_manifest.json" | less
