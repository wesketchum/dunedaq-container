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
- `work/.bash_profile` — copied from `templates/bash_profile` (only if it doesn't already
  exist — re-running `setup.sh` never overwrites your copy). Sets up the Rosetta/spack
  workaround (see [Apple Silicon](#apple-silicon-m1m2m3m4m5)) and a `git_clone_dunedaq` helper.
- `work/.ssh/` loopback-sshd files for integration tests (see
  [Integration tests and sshd](#integration-tests-and-sshd)). Existing files are never overwritten.
- `work/` — your persistent work directory (code, history, dotfiles)

Not created by this script, but needed for a fully working environment — set these up
yourself in `work/` if you need them:
- `work/.gitconfig` — git identity (`user.name`/`user.email`) for commits made inside the container
- `work/.ssh` — an SSH keypair with GitHub access, if you'll be cloning/pushing over SSH
  (e.g. via the `git_clone_dunedaq` helper)

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
| Recreate stack (fresh CVMFS cache) | `./restart.sh` |
| View logs | `podman-compose logs -f` |
| Check CVMFS health | `podman ps --format "{{.Names}}\t{{.Status}}"` |

## Agent-driven workflow (Claude Code)

One long-lived Claude Code session acts as the **dunedaq-env broker**. It owns the
container, and other Claude sessions send it requests: create a work area, clone
repos, build, run unit tests / lint / integration tests.

```bash
cd ~/dunedaq-container && claude --model sonnet      # then: /dunedaq-env
```

Run the broker on Sonnet. Its work is mostly routine message handling and job
dispatch, so Opus costs more without helping. Set the model at launch (or with
`/model sonnet` before running `/dunedaq-env`). A `model:` line in the skill's
frontmatter won't do it, because that override only lasts for the turn the skill
is invoked in. Every later request message reverts to the session model.

From any other session (e.g. one working in `~/dunedaq/all_repos`), use the
`/dunedaq-env-client` skill, or just ask it to "have dunedaq-env build and unit-test X".
Peers edit code directly in `work/<area>/sourcecode/` on the host; the broker runs
everything in the container. It never pushes. Pushes are blocked inside the
container, and publishing is left to you.

| Piece | Purpose |
|---|---|
| `bin/daq-exec` | Run a command in the container with the DAQ env set up correctly (handles the Rosetta `SPACK_ROOT` trap) |
| `bin/daq-job` | Queue/run long jobs: one per work area, ≤2 total, integtests exclusive; state + logs in `work/.daq-jobs/` |
| `bin/daq-stack` | `status` / `ensure` / `restart-if-idle` (refuses if jobs or interactive shells are active) |
| `.claude/skills/dunedaq-env/` | Broker instructions + `PROTOCOL.md` (message format) |
| `.claude/skills/dunedaq-env-client/` | How peer sessions make requests |
| `.claude/agents/daq-log-triage.md` | Read-only subagent that diagnoses failed build/test logs |

To make the client skill available in sessions outside this directory:

```bash
ln -s ~/dunedaq-container/.claude/skills/dunedaq-env-client ~/.claude/skills/dunedaq-env-client
```

## Integration tests and sshd

drunc's default process manager (`ssh-standalone`) starts every DAQ app with
`ssh <user>@<hostname>`, even when the whole system runs on one machine. Without an sshd,
integtests fail with `ssh: connect to host ... port 22: Connection refused`.

The official image already ships `openssh-server`, so this is all set up at run time and
needs no custom image:
- `compose.yaml` gives the container a stable hostname (`dunedaq-app`), and its entrypoint
  starts a **non-root sshd on port 2222** before the login shell.
- `setup.sh` writes the sshd config, host key, a separate loopback keypair
  (`id_ed25519_loopback`, added to `authorized_keys`) and a `Host localhost 127.0.0.1
  dunedaq-app` block in `work/.ssh/config` that points those names at port 2222. Other
  hosts (e.g. GitHub) are unaffected.
- `bin/daq-stack status` checks it end to end (an `ssh dunedaq-app true` inside the
  container), and `bin/daq-stack ensure` restarts sshd if it has died. Log:
  `work/.ssh/sshd/sshd.log`. Its warning that "'UsePAM no' is not supported in RHEL"
  is expected: a non-root sshd can't use PAM.

The bundle script's `ps` (numad check) and `jq` (copying failed-test dirs) aren't in the
image. Both are optional and only produce warnings without them. The container doesn't
install them at startup. The broker installs them (`dnf`, as root via `podman exec -u root`)
before its first integtest in a container, and they last until the container is recreated.

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

## Apple Silicon (M1/M2/M3/M4/M5)

`ghcr.io/dune-daq/alma9-spack` is an x86_64-only image — there is no ARM64 build. On Apple Silicon, `init-podman-machine.sh` automatically uses Apple's Virtualization Framework (`vz`) and Rosetta 2 for x86_64 emulation, which is significantly faster than QEMU software emulation.

Rosetta must be installed before running the init script:

```bash
softwareupdate --install-rosetta
```

If Rosetta is not present, the script will warn and fall back to QEMU emulation (everything works, but builds will be slower).

The full setup procedure is otherwise identical on Apple Silicon.

## CVMFS repositories

The sidecar mounts the repositories listed in `CVMFS_REPOSITORIES` in `compose.yaml`. To add or remove repos, edit that environment variable and also update the `healthcheck` test and the `mkdir` lines in `init-podman-machine.sh`, then rebuild the machine.

## Troubleshooting

**CVMFS sidecar crash-loops (`podman inspect dunedaq-cvmfs --format "{{.RestartCount}}"` is high)**
- Check logs: `podman logs dunedaq-cvmfs --tail 30`
- Likely cause: the repo mount point directories don't exist on the VM. Verify the systemd service ran: `podman machine ssh dunedaq -- systemctl status cvmfs-mountpoint.service`
- If the service is missing the `mkdir` lines, SSH into the VM and update `/etc/systemd/system/cvmfs-mountpoint.service` to include an `ExecStart=/bin/mkdir -p /run/cvmfs/<repo>` line for each repository, then `sudo systemctl daemon-reload`.

**CVMFS reads fail with "Input/output error" during a build**
- Usually a stuck or corrupted cached chunk. Run `./restart.sh` — it recreates the stack
  (wiping the sidecar's writable layer / CVMFS cache), and unlike a plain `podman restart`
  or `podman-compose down && up`, it also waits for a real CVMFS read to succeed before
  returning (the compose healthcheck's `test -d` alone is a false positive).

**CVMFS sidecar requires rootful Podman**
- Check: `podman machine ssh dunedaq -- podman info 2>/dev/null | grep -i rootless`
- If `rootless: true`, run: `podman machine stop dunedaq && podman machine set --rootful dunedaq && podman machine start dunedaq`

**`/cvmfs` is empty inside the DAQ container**
- The sidecar must be healthy before the DAQ container starts. Check: `podman ps --format "{{.Names}}\t{{.Status}}"`
- If the sidecar is healthy but `/cvmfs` is still empty, check mount propagation on the VM: `podman machine ssh dunedaq -- cat /proc/self/mountinfo | grep cvmfs` — you should see `shared:` in the options.

**Running as root inside the container instead of yourself**
- Make sure `./setup.sh` has been run and `.env` exists in the repo directory.
- Check that `work/container_passwd` exists and contains your username/UID.

**Integtests fail with `ssh: connect to host ... Connection refused`**
- Run `bin/daq-stack status`. If `sshd` is not reachable, run `bin/daq-stack ensure`.
- If `work/.ssh/sshd/sshd_config` is missing, run `./setup.sh`. A container created before
  the sshd change needs `bin/daq-stack restart-if-idle` so it picks up the hostname and entrypoint.

**Permission errors on `./work`**
- The `:Z` mount option applies SELinux relabeling, required for rootful Podman. Do not remove it.
