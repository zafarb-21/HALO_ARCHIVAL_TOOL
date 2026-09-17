# Automated HALO mission workflow

The recommended HALO workflow now uses `run_halo_mission.py`. Operators no longer need
to run `arecord` manually or manually copy mission files. The ground orchestrator
creates one canonical mission ID, starts the drone-side recorder/monitor, follows its
status, and invokes collection after finalization.

The standalone creator and collector remain available for diagnostics and recovery.

## Safety boundary

These scripts never arm or disarm the drone. The operator still controls arming and
disarming with the approved remote and procedure.

For a bench test that requires arming, remove the propellers unless the activity is an
approved flight test. An archive or recording command is never authorization to arm.

## One mission ID and two matching folders

The ground orchestrator obtains the canonical `mission_id` from
`create_mission_archive.py`. That exact value identifies:

- `~/MIC_ARRAY_ROS/HALO_ARCHIVE/<mission_id>/` on the ground computer.
- `/home/root/halo_sync_test/<mission_id>/` on the drone.
- The drone-agent metadata, ReSpeaker audio, ROS2 bag, and status records.
- The source selected automatically by `collect_drone_data.py`.

Do not generate a second recorder timestamp or create a differently named session.

## Drone Python compatibility and startup gates

The uploaded drone agent intentionally targets Python 3.6. It uses no postponed
annotations, built-in generic subscripts, union operators, `text=True`, dataclasses, or
`shlex.join`. Its first start record includes the exact drone Python version.

Before launch, the orchestrator runs:

```text
python3 <remote-agent-path> --help
```

If that command fails, the mission agent is not launched. The failure is stored in the
orchestrator log and mission metadata, collection is still attempted, and the
termination reason is `drone_agent_preflight_check_failed`.

After launch, the orchestrator waits up to five seconds for the agent to remain alive
and write `drone_agent_start.json` or `drone_agent_status.json`. It does not report
startup confirmation before this gate. A failure triggers log collection with
`drone_agent_startup_confirmation_failed`.

## Automated sequence

1. The ground orchestrator creates the archive and initializes the matching drone
   folder.
2. It uploads `halo_drone_mission_agent.py` into that mission's drone-side `metadata/`
   directory.
3. It runs the uploaded script's `--help` with the drone's own Python interpreter.
4. After compatibility passes, it launches the agent as a detached process so a short
   SSH interruption does not intentionally terminate recording.
5. The agent creates or confirms `audio/`, `metadata/`, `status_logs/`, `bags/`, and
   `px4_logs/`, then immediately records its Python version and startup identity.
6. The orchestrator confirms the process remained alive and produced startup/status
   metadata before calling startup successful.
7. With `--enable-audio`, the agent runs `arecord` and writes
   `audio/respeaker_6ch.wav` using the configured 6-channel, 16 kHz, S16_LE format.
8. With `--enable-rosbag`, it records the available requested ROS2 topics into
   `bags/`. Missing topics are recorded as warnings. If no requested topics are
   visible, it records a warning and continues without failing the PX4 workflow.
9. The agent refreshes `metadata/drone_agent_status.json` while the ground orchestrator
   pulls a lightweight copy into the ground archive.
10. The agent finalizes on an armed-to-disarmed transition, a reliable landed signal,
    detected failsafe/emergency, loss of flight state after arming, duration timeout,
    or an interrupt signal.
11. Finalization stops recording cleanly, writes final metadata, and lists recent PX4
    ULog candidates.
12. The ground orchestrator automatically runs `collect_drone_data.py`, which copies the
    complete drone mission folder and always preserves the remote ULog candidate list.

## Audio-only automated test

This test does not require arming:

```bash
cd ~/MIC_ARRAY_ROS/HALO_ARCHIVE_TOOL

python3 scripts/run_halo_mission.py \
  --mission-name "home_hotspot_audio_auto_test_002" \
  --operator "Victor Basvi" \
  --drone drones/D0012_home_temp.yaml \
  --profile profiles/audio_px4_sync_test.yaml \
  --drone-host halo-d0012-home \
  --code-root ~/MIC_ARRAY_ROS/HALO_ARCHIVE_TOOL \
  --duration 30 \
  --enable-audio \
  --no-auto-ulog
```

Audio-only mode disables automatic ULog copying by default, even if the explicit
`--no-auto-ulog` flag is omitted. Keeping the flag in test commands makes the intent
auditable. Use `--auto-ulog` only to explicitly allow an automatic ULog copy. The agent
normally reports `duration_complete_no_flight_state` when flight-state telemetry is
unavailable, and collection starts automatically afterward.

## Single-drone ROS bag + PX4 only test

Start this command before the partner arms or enters offboard mode. The archive tool
records and collects data only; it never arms or disarms the drone.

```bash
cd ~/MIC_ARRAY_ROS/HALO_ARCHIVE_TOOL

python3 scripts/run_halo_mission.py \
  --mission-name "lab_single_drone_rosbag_px4_test_001" \
  --operator "Victor Basvi" \
  --drone drones/D0012.yaml \
  --profile profiles/audio_px4_sync_test.yaml \
  --drone-host halo-d0012 \
  --code-root ~/MIC_ARRAY_ROS/HALO_ARCHIVE_TOOL \
  --duration 300 \
  --enable-rosbag \
  --auto-ulog
```

Audio is intentionally disabled because `--enable-audio` is absent. The profile's
requested ROS2 topics are filtered against the topics visible on D0012, so available
topics are recorded and missing topics become non-fatal metadata warnings. The maximum
recording/monitoring window is 300 seconds. A detected armed-to-disarmed transition
ends it earlier after `--post-disarm-wait-s`; without reliable vehicle status, the
duration timeout remains the fallback. When both ROS bag and flight-state
telemetry are unavailable, the agent reports `phase=monitoring_px4_ulog_window` and
keeps the PX4 ULog window alive until that maximum duration expires.

Verify the newest archive with:

```bash
MISSION_DIR=$(ls -td ~/MIC_ARRAY_ROS/HALO_ARCHIVE/* | head -1)
MISSION_ID=$(basename "$MISSION_DIR")

echo "$MISSION_DIR"
echo "$MISSION_ID"

find "$MISSION_DIR" -path "*/bags/*" -print | head -50
find "$MISSION_DIR" -path "*/ros_bags/*" -print | head -50
find "$MISSION_DIR" -path "*/px4_logs/*.ulg" -print -exec ls -lh {} \;
python3 -m json.tool "$MISSION_DIR/metadata/${MISSION_ID}_collection_manifest.json" | less
python3 -m json.tool "$MISSION_DIR/metadata/ground_orchestrator_log.json" | less
```

The collected drone session, including any bag, is under
`drone_data/audio/<mission_id>/`; selected ULogs are under
`drone_data/px4_logs/`. The legacy `audio` path name remains for archive
compatibility even when no WAV was requested.

## Ground-PC ROS bag capture with PX4 ULog collection

When PX4 topics are published with `ROS_DOMAIN_ID=3`, enable ground-side capture
before the partner arms/offboards. The orchestrator sources Jazzy and the optional
`px4_msgs` workspace, runs `ros2 topic list -t`, then starts `ros2 bag record -a` in
`drone_data/ros_bags/`. The ground bag is stopped with SIGINT before ULog collection
so MCAP metadata is finalized.

```bash
cd ~/MIC_ARRAY_ROS/HALO_ARCHIVE_TOOL

python3 scripts/run_halo_mission.py \
  --mission-name "home_ground_rosbag_px4_test_001" \
  --operator "Victor Basvi" \
  --drone drones/D0012_home_temp.yaml \
  --profile profiles/audio_px4_sync_test.yaml \
  --drone-host halo-d0012-home \
  --code-root ~/MIC_ARRAY_ROS/HALO_ARCHIVE_TOOL \
  --duration 300 \
  --enable-ground-rosbag \
  --ros-domain-id 3 \
  --px4-msgs-workspace ~/MIC_ARRAY_ROS/px4_ros2_jazzy_ws/install/setup.bash \
  --auto-ulog
```

If the preflight sees no `/fmu` topics, it records a warning and continues; the
archive and remote PX4 ULog collection remain non-fatal.

## ReSpeaker/PX4 mission with manual arming

```bash
cd ~/MIC_ARRAY_ROS/HALO_ARCHIVE_TOOL

python3 scripts/run_halo_mission.py \
  --mission-name "Automated ReSpeaker PX4 Test" \
  --operator "Operator Name" \
  --drone drones/D0012.yaml \
  --profile profiles/audio_px4_sync_test.yaml \
  --drone-host halo-d0012 \
  --code-root /absolute/path/to/mission-code-repo \
  --duration 300 \
  --enable-audio \
  --enable-rosbag \
  --mirror-interval-s 30
```

Watch the ground status output. Do not arm until it reports `audio=True` and the vehicle,
crew, and test area are ready. The operator then arms and disarms manually. After the
agent observes armed→disarmed, it keeps recording for the default 10-second post-disarm
period, finalizes, and triggers collection.

`--duration` is the maximum recording/monitoring window, not a required flight
duration. `--max-duration-s` is an equivalent alias, and argparse rejects a command
that supplies both. A detected armed-to-disarmed transition can finalize earlier after
the post-disarm wait. If flight-state monitoring is unavailable, the maximum duration
is the fallback. The software never commands the vehicle to arm or disarm.

## Drone-side mission contents

```text
/home/root/halo_sync_test/<mission_id>/
├── README.md
├── config/
├── audio/
│   └── respeaker_6ch.wav
├── bags/
│   └── rosbag2_<mission_id>/                    # when enabled and available
├── metadata/
│   ├── halo_drone_mission_agent.py
│   ├── drone_agent_start.json
│   ├── drone_agent_status.json
│   ├── drone_agent_final.json
│   ├── audio_start_utc.txt
│   ├── audio_stop_utc.txt
│   ├── arecord_command.txt
│   ├── rosbag_command.txt
│   ├── termination_reason.txt
│   └── px4_ulog_candidates.txt
├── px4_logs/
└── status_logs/
    ├── arecord.log
    ├── ros2_bag.log
    └── drone_agent_console.log
```

The full tree is copied to:

```text
<ground mission>/drone_data/audio/<mission_id>/
```

Selected PX4 `.ulg` files are also copied to
`<ground mission>/drone_data/px4_logs/`. Ground-side status snapshots remain under
`drone_data/status_logs/`, while orchestration and collection manifests remain under
`metadata/`.

## ROS2 and timesync fallback

The agent checks `/fmu/out/vehicle_status` for arming and safety state and checks
`/fmu/out/timesync_status` for timesync availability.

If ROS2 topics or the timesync topic are unavailable:

- ReSpeaker audio continues normally when audio is enabled.
- Missing topics are warnings in `drone_agent_status.json` and
  `drone_agent_final.json`, not fatal errors.
- ROS2 bag recording includes only available requested topics.
- If none of the requested topics are visible, no bag process starts and the reason is
  recorded clearly in agent and collection metadata.
- Without usable vehicle-status messages, the agent cannot detect disarm and uses the
  configured duration, recording `duration_complete_no_flight_state`.
- PX4 ULog discovery and collection are still attempted independently over SSH.

## Automatic collection and ULogs

After agent finalization, collection always rsyncs the complete matching drone folder.
It verifies `audio/respeaker_6ch.wav` and records its byte size only when audio was
requested. An intentionally audio-disabled mission records `not_requested` and does
not produce a missing-WAV warning. The collection manifest also records whether ROS bag
capture was requested and started, its remote and collected paths, requested/selected/
missing topics, command, return code, and ROS warnings or errors.

When no `--ulog` is supplied, the collector runs:

```bash
find /data/px4/log -name '*.ulg' -printf '%T@ %p\n' | sort -n | tail -10
```

It always saves the result to `metadata/ulog_candidates_remote.txt`, including when
explicit `--ulog` paths were supplied. Automatic selection filters candidates against
the recorded orchestrator/mission start time and selects the newest eligible file. If
all candidates predate the mission, none is copied and a warning is recorded. When a
start time cannot be parsed, candidates remain available and the manifest records that
the time filter could not be applied. Audio-only orchestrator runs default to no copy;
`--auto-ulog` is the explicit override. An automatic ULog-copy failure remains a warning
and does not discard the successfully copied mission folder.

## Connection loss, power loss, and recovery

The drone agent writes locally inside the mission folder. A temporary SSH loss does not
delete that data. After three consecutive polling failures, the orchestrator records a
warning, leaves both archives in place, and attempts collection.

Pressing Ctrl+C after an archive exists requests a clean agent stop and a best-effort
collection before exit. The ground log and mission metadata record
`user_interrupt_collection_attempted` for this path.

If the drone loses power, immediate copying and clean WAV/bag finalization may be
impossible. If its storage survives, reconnect and rerun the standalone collector with
the same ground mission folder:

```bash
MISSION_ID="PASTE_THE_EXISTING_MISSION_ID"
MISSION_DIR="$HOME/MIC_ARRAY_ROS/HALO_ARCHIVE/$MISSION_ID"

python3 scripts/collect_drone_data.py \
  --mission-dir "$MISSION_DIR" \
  --drone-host halo-d0012 \
  --termination-reason "recovery collection after connection or power loss" \
  --notes "Reused the existing canonical mission_id; no data was deleted"
```

This is still automatic copying: do not manually move individual audio, bag, metadata,
or status files.

## Optional periodic mirroring

`--mirror-interval-s N` periodically rsyncs the active drone mission folder into the
ground archive. Zero, the default, disables mirroring. A mirrored WAV or bag may be
incomplete while its writer is active; final collection runs again after the agent
closes the files cleanly.

## Audit files

- `metadata/ground_orchestrator_log.json` records creation, upload, agent PID, status
  changes, mirroring, warnings/errors, termination, and automatic collection result.
- `metadata/mission_metadata.json` receives orchestration and collection warnings,
  errors, notes, and termination reason.
- `metadata/<mission_id>_collection_manifest.json` records all remote/local paths,
  commands, audio size, ULog discovery, copied ULogs, warnings, and errors.

No script stages or commits files to Git.
