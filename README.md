# dunedaq-env

Containerized DUNE DAQ development environment for macOS, using Podman.

- **CVMFS** is served by a privileged sidecar container (`registry.cern.ch/cvmfs/service`)
- **DUNE DAQ** runs in `ghcr.io/dune-daq/alma9-spack:latest`, with `/cvmfs` mounted read-only from the sidecar
- Orchestrated with `podman-compose`
- Your work directory, shell history, and user identity persist across container restarts

## Prerequisites

```bash
brew install podman podman-compose
```

## First-time setup

### 1. Initialize the Podman machine

Edit CPU/RAM/disk defaults at the top of the script if needed, then:

```bash
./init-podman-machine.sh
```

This creates a **rootful** Podman VM and installs a systemd service that configures the shared CVMFS mountpoint on every boot. It must be rootful — rootless Podman cannot do FUSE mounts inside containers.

### 2. Run the user setup script

```bash
./setup.sh
```

This creates:
- `.env` — your host UID/GID, read by `podman-compose` so the container runs as you
- `work/container_passwd` — a minimal passwd file so the container knows your username
- `work/` — your persistent work directory (code, history, dotfiles)

### 3. Start the stack

```bash
podman-compose up -d
```

This starts the CVMFS sidecar first, waits for it to pass its healthcheck, then starts the DAQ container.

### 4. Open a shell

```bash
./shell.sh
```

Inside the container you will be running as yourself (not root), with:
- `/work` as your home directory and working directory
- `/work/.bash_history` persisting your shell history across sessions
- `/cvmfs/dunedaq.opensciencegrid.org` and `/cvmfs/dunedaq-development.opensciencegrid.org` available read-only

## After a reboot

The Podman machine does not start automatically on macOS boot. Run:

```bash
podman machine start dunedaq
cd ~/dunedaq-container && podman-compose up -d
```

Then `./shell.sh` as usual. The containers have `restart: unless-stopped`, so if the machine was running when you last shut down, `podman machine start` may restore them automatically — but `podman-compose up -d` is always safe to run.

## Daily workflow

| Task | Command |
|------|---------|
| Start stack | `podman-compose up -d` |
| Open DAQ shell | `./shell.sh` |
| Stop stack | `podman-compose down` |
| View logs | `podman-compose logs -f` |
| Check CVMFS health | `podman ps --format "{{.Names}}\t{{.Status}}"` |

## Customizing the Podman machine

Edit the variables at the top of `init-podman-machine.sh`:

```bash
CPUS=12
MEMORY_MB=24576   # 24 GB
DISK_GB=200
```

To rebuild an existing machine:

```bash
podman machine stop dunedaq && podman machine rm dunedaq
./init-podman-machine.sh
./setup.sh
podman-compose up -d
```

## CVMFS repositories

The sidecar mounts the repositories listed in `CVMFS_REPOSITORIES` in `compose.yaml`. To add or remove repos, edit that environment variable and also update the `healthcheck` test and the `mkdir` lines in `init-podman-machine.sh`, then rebuild the machine.

## Troubleshooting

**CVMFS sidecar crash-loops (`podman inspect dunedaq-cvmfs --format "{{.RestartCount}}"` is high)**
- Check logs: `podman logs dunedaq-cvmfs --tail 30`
- Likely cause: the repo mount point directories don't exist on the VM. Verify the systemd service ran: `podman machine ssh dunedaq -- systemctl status cvmfs-mountpoint.service`
- If the service is missing the `mkdir` lines, SSH into the VM and update `/etc/systemd/system/cvmfs-mountpoint.service` to include an `ExecStart=/bin/mkdir -p /run/cvmfs/<repo>` line for each repository, then `sudo systemctl daemon-reload`.

**CVMFS sidecar requires rootful Podman**
- Check: `podman machine ssh dunedaq -- podman info 2>/dev/null | grep -i rootless`
- If `rootless: true`, run: `podman machine stop dunedaq && podman machine set --rootful dunedaq && podman machine start dunedaq`

**`/cvmfs` is empty inside the DAQ container**
- The sidecar must be healthy before the DAQ container starts. Check: `podman ps --format "{{.Names}}\t{{.Status}}"`
- If the sidecar is healthy but `/cvmfs` is still empty, check mount propagation on the VM: `podman machine ssh dunedaq -- cat /proc/self/mountinfo | grep cvmfs` — you should see `shared:` in the options.

**Running as root inside the container instead of yourself**
- Make sure `./setup.sh` has been run and `.env` exists in the repo directory.
- Check that `work/container_passwd` exists and contains your username/UID.

**Permission errors on `./work`**
- The `:Z` mount option applies SELinux relabeling, required for rootful Podman. Do not remove it.
