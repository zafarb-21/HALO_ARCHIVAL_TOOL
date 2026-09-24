# Humble ground discovery: 2026-09-24 lab diagnosis

Ground PC: `192.168.0.18` on `eno1`, with a separate Wi-Fi network active.
Ground ROS: `/opt/ros/humble/setup.bash`.
PX4 messages: `~/HALO_ARCHIVAL_TOOL/runtime/px4_ros2_humble_ws/install/setup.bash`.
Drones run Foxy. SSH and local PX4 topic discovery succeeded on all five drones:
D0012 through D0016 use domains 3 through 7 respectively.

The old launch commands hardcoded Jazzy and relied on discovery variables introduced
after Humble. Current ground commands use `FASTRTPS_DEFAULT_PROFILES_FILE` with
`config/fastdds/humble_D0012.xml` through `humble_D0016.xml`. These contain explicit
unicast discovery ports for participant IDs 0–31 in the corresponding domain.
The pre-existing `config/fastdds/D0012.xml` was preserved.

The active UFW rules supplied by the operator allowed only campus traffic and
had no lab DDS allowance. After the operator added the following rules, all five
drones' topics became visible on the ground PC:

```bash
for ip in 192.168.0.{20..24}; do
  sudo ufw allow in on eno1 proto udp from "$ip" to any port 8150:9399 comment 'HALO ROS2 DDS'
done
```

The firewall remains enabled. Its defaults are managed by Puppet; if rules are
reset, ask the administrator to persist the lab-specific allowance.

A second issue was confirmed by the first recording test: the old ground
`~/px4_ros2_humble_ws` package used newer PX4 messages. Recorded VehicleStatus
messages failed Fast CDR deserialization on every drone. The isolated workspace
`runtime/px4_ros2_humble_ws` contains the drones' matching definitions, built for
Humble. All five drones share the same message-file hashes; source provenance and
rebuild instructions are in that workspace's `SOURCE.md`. The launchers now use
this workspace by default. `--px4-msgs-workspace` or `HALO_PX4_MSGS_SETUP` can override it.

Manual D0012 verification from the repository:

```bash
source /opt/ros/humble/setup.bash
source ~/HALO_ARCHIVAL_TOOL/runtime/px4_ros2_humble_ws/install/setup.bash
export ROS_DOMAIN_ID=3 ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export FASTRTPS_DEFAULT_PROFILES_FILE="$PWD/config/fastdds/humble_D0012.xml"
ros2 topic list --no-daemon --spin-time 8 -t
ros2 topic echo /fmu/out/vehicle_status --once --qos-reliability best_effort
```

Listing type names alone does not verify message compatibility. Receiving and
decoding a vehicle_status sample and a sensor_combined sample must succeed before
claiming a successful recording test. Foxy/Humble PX4 message definitions must
match the firmware's definitions.

Run the complete no-flight preflight:

```bash
scripts/launch_halo_swarm.sh --mission-name humble_discovery_preflight \
  --operator "$USER" --archive-root "$PWD/diagnostics" \
  --preflight-only --no-auto-ulog
```

This synchronizes drone clocks, starts/checks MicroDDS, and saves per-drone
metadata. It issues no flight commands. Failed diagnostic archives are evidence,
not successful flight recordings. Preserve each run under its unique mission ID.

References:
- https://docs.ros.org/en/humble/Releases/Release-Iron-Irwini.html
- https://docs.vulcanexus.org/en/humble/rst/tutorials/core/qos/initial_peers/initial_peers.html

Final verification passed on all five drones. See [test results](HUMBLE_TEST_RESULTS.md)
for counts, archive paths, and tested commands.
