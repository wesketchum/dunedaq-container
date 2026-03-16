#!/usr/bin/env bash
# setup.sh
# One-time setup: generates .env and work/container_passwd for the compose stack.
# Re-run if your UID/GID changes or you want to reset the work directory.

set -euo pipefail

mkdir -p work

# Write .env with host UID/GID so compose can pass them to the container.
cat > .env <<EOF
HOST_UID=$(id -u)
HOST_GID=$(id -g)
EOF

# Write a minimal passwd file so the container recognises your user identity.
# Includes root (needed by some tooling) and your own entry.
cat > work/container_passwd <<EOF
root:x:0:0:root:/root:/bin/bash
nobody:x:65534:65534:nobody:/:/sbin/nologin
$(id -un):x:$(id -u):$(id -g):$(id -un):/work:/bin/bash
EOF

echo "Setup complete."
echo "  UID=$(id -u)  GID=$(id -g)  user=$(id -un)"
echo "  .env and work/container_passwd created."
echo ""
echo "Next: podman-compose up -d"
