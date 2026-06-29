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

# ── Virtualization backend (auto-detected) ────────────────────────────────────
# Apple Silicon (Podman 6.x): default to applehv (Apple Virtualization.framework).
#   - This is the ONLY backend that supports Rosetta, which is required to run the
#     x86_64-only DUNE DAQ stack fast. Without it, x86_64 binaries fall back to
#     QEMU user emulation: much slower, and it breaks spack's setup-env.sh
#     (SPACK_ROOT auto-detection sees "qemu-x86_64-static" and fails).
#   - virtiofs mounts are automatic (the old --mount-type flag is gone).
#   - Rosetta is used automatically when installed (the old --rosetta flag is gone).
#   - Its helpers (vfkit, gvproxy) ship bundled with the Homebrew podman formula.
#
# libkrun is NOT suitable here: krunkit has no Rosetta support, so x86_64 always
# runs under QEMU. Only choose it (PODMAN_PROVIDER=libkrun) for arm64-native GPU
# workloads, and install it first: brew tap slp/krunkit && brew install krunkit
#
# Intel: use QEMU (no emulation needed, containers run natively).
if [[ "$(uname -m)" == "arm64" ]]; then
  VM_TYPE="${PODMAN_PROVIDER:-applehv}"
  # Detect Rosetta by actually running an x86_64 binary. The pkgutil package id
  # (com.apple.pkg.RosettaUpdateAuto) is unreliable: it can report "installed"
  # while the runtime share is missing, so test real execution instead.
  if ! arch -x86_64 /usr/bin/true &>/dev/null; then
    echo "WARNING: Rosetta is not usable. x86_64 containers will use slow QEMU emulation"
    echo "         and DUNE DAQ spack setup may fail. Install it with:"
    echo "         softwareupdate --install-rosetta --agree-to-license"
  fi
else
  VM_TYPE="${PODMAN_PROVIDER:-qemu}"
fi

# ── Sanity checks ────────────────────────────────────────────────────────────
if ! command -v podman &>/dev/null; then
  echo "ERROR: podman not found. Install with: brew install podman"
  exit 1
fi

# The libkrun backend needs the external 'krunkit' binary; fail early if missing.
if [[ "$VM_TYPE" == "libkrun" ]] && ! command -v krunkit &>/dev/null; then
  echo "ERROR: provider 'libkrun' requires the 'krunkit' binary, which is not in \$PATH."
  echo "       Install it with: brew tap slp/krunkit && brew install krunkit"
  echo "       Or use the bundled backend instead: PODMAN_PROVIDER=applehv $0"
  exit 1
fi

if podman machine inspect "$MACHINE_NAME" &>/dev/null; then
  echo "Machine '$MACHINE_NAME' already exists. To recreate it, run:"
  echo "  podman machine stop $MACHINE_NAME && podman machine rm $MACHINE_NAME"
  exit 0
fi

# ── Create machine ───────────────────────────────────────────────────────────
echo "Creating Podman machine '$MACHINE_NAME' (${CPUS} CPUs, ${MEMORY_MB} MB RAM, ${DISK_GB} GB disk, provider=${VM_TYPE})..."
podman machine init "$MACHINE_NAME" \
  --cpus "$CPUS" \
  --memory "$MEMORY_MB" \
  --disk-size "$DISK_GB" \
  --rootful \
  --provider "$VM_TYPE" \
  --now   # start immediately after init

# ── Make this machine the default (ROOTFUL connection) ──────────────────────
# A --rootful machine exposes two connections: "$MACHINE_NAME" (rootless) and
# "$MACHINE_NAME-root" (rootful). The stack MUST use the rootful one: the CVMFS
# sidecar mounts cvmfs with cvmfs2's grab_mountpoint option, which chowns the
# mountpoint. Under the rootless connection the bind-mounted /run/cvmfs dirs are
# owned by nobody (user namespace), grab_mountpoint fails, and CVMFS never mounts.
podman system connection default "${MACHINE_NAME}-root"

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

# ── Verify Rosetta is active on Apple Silicon ───────────────────────────────
# x86_64 DUNE DAQ needs the Rosetta binfmt handler; QEMU fallback breaks spack.
if [[ "$(uname -m)" == "arm64" ]]; then
  if podman machine ssh "$MACHINE_NAME" -- test -e /proc/sys/fs/binfmt_misc/rosetta 2>/dev/null; then
    echo "Rosetta is active for x86_64 containers. 👍"
  else
    echo "WARNING: no Rosetta binfmt handler in the VM — x86_64 will use slow QEMU"
    echo "         emulation and DUNE DAQ spack setup may fail. Check that Rosetta is"
    echo "         installed and that provider is 'applehv' (not libkrun)."
  fi
fi

echo ""
echo "✅  Podman machine '$MACHINE_NAME' is up."
echo "    To verify: podman machine list"
echo "    To start later: podman machine start $MACHINE_NAME"
