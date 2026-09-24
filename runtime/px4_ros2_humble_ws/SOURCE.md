# Firmware-matched PX4 messages

Copied 2026-09-24 from halo-d0012:
`/opt/ros/foxy/mpa_to_ros2/install/px4_msgs/share/px4_msgs/msg/`.
All five drones have the same aggregate message-file SHA256:
`6253425c66eab48bec9340efb2f84a12a0633288e1c2e6fcfc97db4278013286`.
Computed using `find msg -name '*.msg' -print | sort | xargs sha256sum | sha256sum`.

CMakeLists.txt and package.xml were copied from the pre-existing ground
`~/px4_ros2_humble_ws/src/px4_msgs` package as build scaffolding. Its version field
does not identify the drone firmware. The message hashes identify this snapshot.

Build from this directory after sourcing only /opt/ros/humble/setup.bash:

    CMAKE_BUILD_PARALLEL_LEVEL=4 colcon build --packages-select px4_msgs --executor sequential --cmake-args -DBUILD_TESTING=OFF

Do not source the newer ~/px4_ros2_humble_ws overlay after this workspace.
