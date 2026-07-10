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

# Install the .bash_profile template (Rosetta/spack workaround, git_clone_dunedaq
# helper) on first run only -- never overwrite, since the user may have customized it.
if [ -f work/.bash_profile ]; then
  echo "work/.bash_profile already exists -- leaving it alone."
  echo "  Diff against templates/bash_profile to check for updates:"
  echo "  diff templates/bash_profile work/.bash_profile"
else
  cp templates/bash_profile work/.bash_profile
  echo "Installed work/.bash_profile from templates/bash_profile."
fi

echo "Setup complete."
echo "  UID=$(id -u)  GID=$(id -g)  user=$(id -un)"
echo "  .env and work/container_passwd created."
echo ""
echo "Note: work/.gitconfig (git identity) and work/.ssh (GitHub keys) are not"
echo "managed by this script -- create/copy them into work/ yourself if needed."
echo ""
echo "Next: podman-compose up -d"
