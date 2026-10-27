# dunedaq-container

Podman-based DUNE DAQ development environment for macOS (see README.md). The
`alma9-spack` image is x86_64 and runs under Rosetta in an `applehv` VM; CVMFS comes
from a sidecar container.

- `work/` on the host == `/work` (and `$HOME`) in the `dunedaq-app` container.
  Work areas live at `work/<area>/` with `sourcecode/` (C++) and `pythoncode/` (pure Python).
- Run container commands with `bin/daq-exec` (quick) or `bin/daq-job` (anything long).
  Never raw `podman exec ... dbt-build`: non-interactive shells miss the `.bash_profile`
  prompt hook that sets `SPACK_ROOT`, so dbt/spack fail under Rosetta.
- Integtests need the loopback sshd (non-root, port 2222, hostname `dunedaq-app`) that the
  compose entrypoint starts from `work/.ssh/sshd/` (written by `setup.sh`). `bin/daq-stack
  status` checks it; `ensure` revives it. No custom image: the official one ships sshd.
- Stack health and lifecycle: `bin/daq-stack status|ensure|restart-if-idle`. Never run
  `restart.sh` or `podman-compose down` while jobs or interactive shells are active.
- Agent setup: one long-lived session runs the `/dunedaq-env` skill (broker). Other sessions
  use `/dunedaq-env-client` and talk to it via `SendMessage`; the protocol is
  `.claude/skills/dunedaq-env/PROTOCOL.md`. Start the broker session on Sonnet
  (`claude --model sonnet`). A skill-frontmatter `model:` only lasts one turn, so it
  can't pin a long-lived broker.
- DUNE-DAQ org knowledge (repo list, dependency layering): `~/dunedaq/all_repos/CLAUDE.md`.
  Release manifests: `~/dunedaq/all_repos/daq-release/configs/`. Version tagging is
  `/tag-packages` in `~/dunedaq/all_repos`.
- `templates/bash_profile` is the source of truth for `work/.bash_profile`; `setup.sh` never
  overwrites an existing copy.
