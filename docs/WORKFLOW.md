# HALO standalone recovery workflow

> The recommended operational workflow is now
> [Automated mission workflow](AUTOMATED_MISSION_WORKFLOW.md). It starts recording and
> collection automatically; no manual arecord or manual file copying is required.

This retained standalone sequence is for diagnostics and recovery. One canonical
`MISSION_ID` must remain unchanged across the ground archive, drone session folder,
ReSpeaker recorder, metadata, and collection destination.

## Before the test

From the HALO archive tool directory, create the mission and initialize its matching
drone folder:

```bash
python3 scripts/create_mission_archive.py \
  --mission-name "Motor Spinup Sync Test" \
  --operator "Operator Name" \
  --drone drones/D0012.yaml \
  --profile profiles/audio_px4_sync_test.yaml \
  --code-root /absolute/path/to/mission-code-repo \
  --initialize-drone \
  --drone-host root@192.168.0.20
```

Replace the example `--code-root` with the actual Git working tree, or omit it to use
automatic discovery. The creator prints the exact ID and writes
`<mission-dir>/metadata/next_commands.sh`. Use that generated guide so the mission
paths and ID do not need to be retyped.

## Full sequence

1. Create the ground mission archive and initialize the same-named drone folder.
2. Use the generated `MISSION_ID` unchanged as the ReSpeaker recorder's session ID.
3. Start `voxl-microdds-agent` on the drone and confirm its status.
4. Start the ReSpeaker synchronization recorder with that exact session ID.
5. Wait until the recorder prints `Recording WAVE...` before proceeding.
6. Arm the drone only when the vehicle, range, crew, and test authorization are ready.
7. After the test, disarm safely and allow the recorder to finish and close its files.
8. Identify the newest PX4 ULog, verify its timestamp belongs to this mission, and note
   every applicable remote ULog path.
9. Run `collect_drone_data.py` to copy the matching ReSpeaker session folder, selected
   ULogs, and drone status snapshots into the ground archive.

The generated guide provides these operator-run sections:

```bash
"$MISSION_DIR/metadata/next_commands.sh" check-drone
"$MISSION_DIR/metadata/next_commands.sh" start-microdds
export RESPEAKER_RECORDER_COMMAND='python3 /actual/path/to/the/recorder.py'
"$MISSION_DIR/metadata/next_commands.sh" start-respeaker
"$MISSION_DIR/metadata/next_commands.sh" find-ulogs
```

The recorder launcher is installation-specific, so set
`RESPEAKER_RECORDER_COMMAND` to the command already used on this drone. The guide adds
`--session-id "$MISSION_ID"` itself.

For collection, provide each verified ULog path with a repeated `--ulog` option. The
collector always saves the remote candidate list. If explicit paths are omitted, only
candidates at or after mission start are eligible for automatic selection. Use
`--no-auto-ulog` for dry/audio-only recovery runs that should copy no ULog.

## Safety

Arming is not needed for archive dry tests. For bench tests that require arming, remove
the propellers unless the activity is an approved flight test conducted under the
applicable flight-test procedure. Never let an archive command be interpreted as
authorization to arm or fly.

## Where results go

- ReSpeaker/session folder: `drone_data/audio/<mission_id>/`
- PX4 ULogs: `drone_data/px4_logs/`
- ROS bags added by the mission workflow: `drone_data/ros_bags/`
- Other raw sensors: `drone_data/raw_sensor_data/`
- Remote status and code snapshots: `drone_data/status_logs/`
- Code state, mission metadata, generated commands, and collection manifests:
  `metadata/`
- Derived data: `processed/`, `plots/`, and `reports/`

See the repository `README.md` for the complete archive schema and
`docs/SSH_KEY_SETUP.md` for passwordless login setup.
