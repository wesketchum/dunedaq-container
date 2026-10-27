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

# Loopback SSH for integration tests. drunc's default (ssh-standalone) process
# manager starts every DAQ app with `ssh <user>@<host>`, even on one machine. The
# image ships sshd; we run it as you (non-root, port 2222) from the compose
# entrypoint, with its keys and config under work/.ssh/. Paths inside these
# files are container paths (/work == work/). Existing files are never overwritten.
mkdir -p work/.ssh/sshd
chmod 700 work/.ssh
[ -f work/.ssh/sshd/host_ed25519 ] || ssh-keygen -q -t ed25519 -N "" -C dunedaq-app-host -f work/.ssh/sshd/host_ed25519
[ -f work/.ssh/id_ed25519_loopback ] || ssh-keygen -q -t ed25519 -N "" -C dunedaq-app-loopback -f work/.ssh/id_ed25519_loopback
touch work/.ssh/authorized_keys
chmod 600 work/.ssh/authorized_keys
grep -qF "$(cut -d' ' -f2 work/.ssh/id_ed25519_loopback.pub)" work/.ssh/authorized_keys \
  || cat work/.ssh/id_ed25519_loopback.pub >> work/.ssh/authorized_keys
if [ ! -f work/.ssh/sshd/sshd_config ]; then
  cat > work/.ssh/sshd/sshd_config <<'EOF'
# Non-root loopback sshd for drunc's SSH process manager (see setup.sh).
Port 2222
HostKey /work/.ssh/sshd/host_ed25519
PidFile /work/.ssh/sshd/sshd.pid
AuthorizedKeysFile /work/.ssh/authorized_keys
PasswordAuthentication no
KbdInteractiveAuthentication no
# Non-root sshd can't use PAM; RHEL warns about this at startup, harmlessly.
UsePAM no
StrictModes no
EOF
fi
# Client side: route only the container's own names to port 2222, so GitHub
# (or any other host) is unaffected.
touch work/.ssh/config
chmod 600 work/.ssh/config
if ! grep -q "dunedaq-app loopback sshd" work/.ssh/config; then
  cat >> work/.ssh/config <<'EOF'

# dunedaq-app loopback sshd (added by setup.sh) -- used by drunc integtests
Host localhost 127.0.0.1 dunedaq-app
    Port 2222
    IdentityFile ~/.ssh/id_ed25519_loopback
    IdentitiesOnly yes
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel error
EOF
fi

echo "Setup complete."
echo "  UID=$(id -u)  GID=$(id -g)  user=$(id -un)"
echo "  .env and work/container_passwd created."
echo ""
echo "  work/.ssh/ has the loopback sshd keys/config for integration tests."
echo ""
echo "Note: work/.gitconfig (git identity) and GitHub SSH keys in work/.ssh are not"
echo "managed by this script -- create/copy them into work/ yourself if needed."
echo ""
echo "Next: podman-compose up -d"
