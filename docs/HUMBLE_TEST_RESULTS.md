# Verified Humble ground test — 2026-09-24

All five drones passed the 15-second passive ground recording test. Each SQLite
bag finalized with metadata, `ros2 bag info` succeeded, and one saved sample from
each of the three topics decoded with the firmware-matched Humble package.
This confirms reception and basic decoding; it is not a loss-rate or flight test.

| Drone | Domain | VehicleStatus messages | SensorCombined messages | TimesyncStatus messages | Result |
| --- | --- | --- | --- | --- | --- |
| D0012 | 3 | 29 | 2585 | 144 | Pass |
| D0013 | 4 | 28 | 2602 | 144 | Pass |
| D0014 | 5 | 29 | 2595 | 145 | Pass |
| D0015 | 6 | 29 | 2845 | 145 | Pass |
| D0016 | 7 | 29 | 2814 | 145 | Pass |

Recording archive: `diagnostics/20260924_163824_UTC_humble_passive_bag_test/`.
Full launcher preflight: `diagnostics/20260924_163825_UTC_humble_discovery_preflight/`.
The launcher reported all five READY and exited 0. Collection manifests had no
errors. They warned that there was no new PX4 ULog, as expected for this no-flight
test. Audio, arming transitions, flight logging, and landing triggers were not tested.
No flight-control commands were issued. Preflight synchronized the drone clocks
and started/checked MicroDDS as implemented by the existing launcher.

The two confirmed failures were incoming DDS blocked by UFW and incompatible
newer ground PX4 message definitions. The operator added drone-specific UDP rules;
the archive scripts now use Humble and a separately built matching message package.

To reproduce the recording test from the repository:

```bash
source /opt/ros/humble/setup.bash
source runtime/px4_ros2_humble_ws/install/setup.bash
python3 scripts/verify_humble_ground_recording.py
```

To list one drone's topics in your terminal:

```bash
cd ~/HALO_ARCHIVAL_TOOL
source /opt/ros/humble/setup.bash
source runtime/px4_ros2_humble_ws/install/setup.bash
export ROS_DOMAIN_ID=3 ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export FASTRTPS_DEFAULT_PROFILES_FILE="$PWD/config/fastdds/humble_D0012.xml"
ros2 topic list --no-daemon --spin-time 5 -t
```

For the other drones, change both the domain and profile: D0013=4, D0014=5,
D0015=6, D0016=7. Do not source the incompatible older ground workspace afterward.

The flight archive launcher is ready for an operator-controlled flight test:

```bash
cd ~/HALO_ARCHIVAL_TOOL
scripts/launch_halo_swarm.sh --mission-name swarm_test_001 --operator "$USER" --duration 180
```

This waits for observed arming before recording and does not command flight.
Audio is off unless explicitly enabled. The five-drone arm/land lifecycle still
requires validation during an actual approved test.
