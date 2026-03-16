#!/usr/bin/env bash
# scripts/shell.sh
# Open an interactive bash shell inside the running DAQ container.
# If the stack isn't up yet, start it first.

set -euo pipefail

CONTAINER="dunedaq-app"

if ! podman ps --format '{{.Names}}' | grep -q "^${CONTAINER}$"; then
  echo "Container '$CONTAINER' is not running. Starting the stack..."
  podman-compose -f "$(dirname "$0")/compose.yaml" up -d
  echo "Waiting for CVMFS healthcheck..."
  sleep 5
fi

exec podman exec -it "$CONTAINER" /bin/bash -l
