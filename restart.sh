#!/usr/bin/env bash
# restart.sh
# Recreate the DUNE DAQ stack (CVMFS sidecar + DAQ container).
#
# `down` + `up` recreates the containers, which WIPES the CVMFS sidecar's
# writable layer -- i.e. its /var/lib/cvmfs cache. That clears stuck or
# corrupted cached chunks, the usual cause of "Input/output error" when reading
# from /cvmfs during a build. (A plain `podman restart` would NOT clear it.)

set -euo pipefail

COMPOSE="$(dirname "$0")/compose.yaml"
MACHINE_NAME="dunedaq"

# The stack MUST run on the rootful connection, or the CVMFS sidecar can't mount
# (cvmfs2's grab_mountpoint can't chown the bind dirs under a rootless userns).
if ! podman system connection list --format '{{.Name}} {{.Default}}' 2>/dev/null \
     | grep -q "^${MACHINE_NAME}-root true$"; then
  echo "Setting default connection to rootful (${MACHINE_NAME}-root)..."
  podman system connection default "${MACHINE_NAME}-root"
fi

echo "Bringing the stack down..."
podman-compose -f "$COMPOSE" down

echo "Bringing the stack up (fresh CVMFS cache)..."
podman-compose -f "$COMPOSE" up -d

echo "Waiting for the CVMFS sidecar to mount the repositories..."
# Poll until a real CVMFS read succeeds (not just that the directory exists --
# the compose healthcheck only does `test -d`, which is a false positive).
for i in $(seq 1 30); do
  if podman exec dunedaq-cvmfs sh -c \
       'ls /cvmfs/dunedaq.opensciencegrid.org/setup_dunedaq.sh' >/dev/null 2>&1; then
    echo "✅  CVMFS is up. Stack ready."
    echo "    Open a shell with: ./shell.sh"
    exit 0
  fi
  sleep 2
done

echo "⚠️  CVMFS did not come up within the timeout. Check: podman logs dunedaq-cvmfs"
exit 1
