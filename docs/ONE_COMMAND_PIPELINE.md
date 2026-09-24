# One-command HALO recording and collection

From any terminal:

```bash
cd ~/HALO_ARCHIVAL_TOOL
./halo.sh
```

No separate ROS sourcing, SSH, recorder, or collector commands are needed.
The five drones must be powered, connected, and reachable by their existing SSH
aliases. The configured DDS firewall rules and firmware-matched Humble workspace
must remain available.

The command starts a detached background job, prints the archive and log paths,
and waits for all five preflights to pass. Wait for **ALL FIVE READY** before
arming under your normal test procedure. Disarm after your planned recording
interval. The pipeline observes the states; it never arms or disarms the drones.
For a bench test, remove propellers.

Each run creates a new UTC timestamped folder:

```text
~/HALO_ARCHIVAL_TOOL/archives/<UTC_timestamp>_<name>/
  config/                          # drone/profile/DDS settings and message definitions
  metadata/pipeline_status.json
  metadata/pipeline.log
  metadata/pipeline_verification.json
  reports/pipeline_verification.md
  drones/D0012/                    # also D0013 through D0016
    ros_bags/                      # finalized ground bag
    px4_logs/all_mission_logs/      # all logs in this run, preserving PX4 session names
    metadata/final_verification.json
```

The job performs SSH/IP checks, sets each drone's UTC clock from a fresh ground
clock reading, starts/checks MicroDDS, and verifies topic discovery and message
reception with Humble and the matched PX4 definitions. Clock setting over SSH is
approximate synchronization, not precision hardware clock synchronization.

On arming it starts the ground bag. After the observed disarm (or the existing
landing trigger), it finalizes the bag and collects PX4 logs. Once workers finish,
it copies **all** ULogs modified in the mission window, checks remote/local SHA-256
hashes and ULog headers, checks SQLite bag integrity, and decodes saved message
samples. It writes per-drone results and an overall report. Full ULog semantic
parsing and loss-free reception are not implied by these checks.

The launcher also stores its selected latest log directly in `px4_logs/`; that
file may duplicate a file in `all_mission_logs/`. Original drone files are retained.
Each worker handles one arm/disarm cycle. Start another run for another cycle.

Status and clean stop:

```bash
./halo.sh status
./halo.sh stop
```

`stop` finalizes active recordings and collects available logs; **it does not
disarm the drones**. Follow your normal disarm procedure. A stopped or incomplete
run is labeled accordingly, rather than reported as a complete test. A run with
no arm or no required ULog fails final verification.

The background job survives closing the launching terminal. While the first command
is waiting for READY, Ctrl-C only stops that foreground wait; use `./halo.sh stop`
to stop the background pipeline. `status` also prints the live log path, which can
be followed with `tail -f <printed-path>`. When finished, a copy of that log is
stored inside the mission folder. Another pipeline launch is blocked until the
current job finishes.

Useful optional commands:

```bash
./halo.sh --name bench_30s
./halo.sh --name longer_test --duration 900
./halo.sh --enable-audio
./halo.sh --preflight-only
./halo.sh --dry-run
./halo.sh --detach
```

The default 600-second duration is the maximum monitoring window per worker,
including waiting for arming. It is **not** an automatic 30-second disarm timer.
The default collects ROS bags and PX4 logs; audio requires `--enable-audio`.
`--preflight-only` checks readiness without recording or requiring new ULogs.
`--dry-run` validates config without SSH or archive creation. `--detach` returns
immediately; check status and the log for readiness before arming.

The matched workspace defaults to
`runtime/px4_ros2_humble_ws/install/setup.bash`. To rebuild after moving the repository,
follow `runtime/px4_ros2_humble_ws/SOURCE.md`. A different setup can be selected with
`--px4-msgs-workspace PATH` or `HALO_PX4_MSGS_SETUP`; it must match the drone firmware.
The script does not install ROS, change firewall rules, or rebuild dependencies on
every recording run.

Validation performed: shell/Python checks, six offline regression tests, successful
live five-drone background preflight, duplicate-run prevention, and offline finalizer
verification of copies of the previous five real bags and six ULogs. No new armed
recording was requested or performed while developing this wrapper.
