#!/usr/bin/env bash
# init-podman-machine.sh
# One-time setup for the Podman machine on macOS.
# Re-run after `podman machine rm` or on a new computer.
#
# Customize the variables below to taste.

set -euo pipefail

MACHINE_NAME="dunedaq"
CPUS=12
MEMORY_MB=24576   # 24 GB
DISK_GB=200

# ── Sanity checks ────────────────────────────────────────────────────────────
if ! command -v podman &>/dev/null; then
  echo "ERROR: podman not found. Install with: brew install podman"
  exit 1
fi

if podman machine inspect "$MACHINE_NAME" &>/dev/null; then
  echo "Machine '$MACHINE_NAME' already exists. To recreate it, run:"
  echo "  podman machine stop $MACHINE_NAME && podman machine rm $MACHINE_NAME"
  exit 0
fi

# ── Create machine ───────────────────────────────────────────────────────────
echo "Creating Podman machine '$MACHINE_NAME' (${CPUS} CPUs, ${MEMORY_MB} MB RAM, ${DISK_GB} GB disk)..."
podman machine init "$MACHINE_NAME" \
  --cpus "$CPUS" \
  --memory "$MEMORY_MB" \
  --disk-size "$DISK_GB" \
  --rootful \
  --now   # start immediately after init

# ── Make this machine the default ───────────────────────────────────────────
podman system connection default "$MACHINE_NAME"

# ── Set up persistent shared CVMFS mountpoint on VM ─────────────────────────
echo "Configuring persistent shared CVMFS mountpoint on VM..."
podman machine ssh "$MACHINE_NAME" -- bash -c "$(cat <<'SSHEOF'
  sudo mkdir -p /run/cvmfs

  sudo tee /etc/systemd/system/cvmfs-mountpoint.service > /dev/null <<'EOF'
[Unit]
Description=Shared CVMFS mountpoint for container FUSE propagation
After=local-fs.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/mkdir -p /run/cvmfs
ExecStart=/bin/mkdir -p /run/cvmfs/cvmfs-config.cern.ch
ExecStart=/bin/mkdir -p /run/cvmfs/dunedaq.opensciencegrid.org
ExecStart=/bin/mkdir -p /run/cvmfs/dunedaq-development.opensciencegrid.org
ExecStart=/bin/mount --bind /run/cvmfs /run/cvmfs
ExecStart=/bin/mount --make-rshared /run/cvmfs

[Install]
WantedBy=multi-user.target
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable --now cvmfs-mountpoint.service
SSHEOF
)"

echo ""
echo "✅  Podman machine '$MACHINE_NAME' is up."
echo "    To verify: podman machine list"
echo "    To start later: podman machine start $MACHINE_NAME"
