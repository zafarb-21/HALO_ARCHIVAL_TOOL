# Five-drone ROS bag and PX4 ULog collection

Mission: `20260924_170120_UTC_five_drone_30s_rosbag_px4`.
All five workers observed arming and disarming and exited 0. All five bags finalized,
passed SQLite integrity checks, and decoded samples from each nonempty recorded topic.
Every collected mission-window ULog has a matching remote/local SHA-256 and valid
ULog header. Full ULog semantic decoding was not performed.

| Drone | Bag duration (s) | Detected armed interval (s) | Bag messages | Mission ULogs | Reported connection-loss events |
| --- | --- | --- | --- | --- | --- |
| D0012 | 134.9 | 135.3 | 28325 | 1 | 11 |
| D0013 | 95.1 | 94.4 | 56354 | 1 | 0 |
| D0014 | 89.2 | 88.4 | 53217 | 1 | 0 |
| D0015 | 52.9 | 52.1 | 33818 | 1 | 0 |
| D0016 | 4.9 | 4.2 | 2649 | 2 | 0 |

The requested target was 30 seconds; actual durations above are from recorded
message timestamps and observed status transitions. D0016 disarmed after a short
interval. D0012 had intermittent telemetry loss, so ground data may contain gaps
and transition detection may be delayed. The onboard logs were preserved separately.
Audio was not enabled. No flight-control commands were sent by the archive tools.

Per drone:
- `ros_bags/`: finalized ground bag.
- `px4_logs/all_mission_logs/<PX4 session>/`: every ULog modified in the mission window.
- `px4_logs/*.ulg`: the launcher's automatically selected latest log (also included
  in the complete session-preserving collection; do not count this duplicate twice).
- `metadata/final_verification.json`: message counts, decode checks, timings, hashes,
  and limitations.

Original files remain on the drones. No archive files were deleted or overwritten.
