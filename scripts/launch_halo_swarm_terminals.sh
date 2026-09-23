#!/usr/bin/env bash
# Compatibility wrapper for the background-worker swarm launcher.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/launch_halo_swarm.sh" "$@"
