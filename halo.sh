#!/usr/bin/env bash
# One-command HALO archive pipeline. Works from any current directory.
set -e
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$REPO_ROOT/scripts/halo_pipeline.py" "$@"
