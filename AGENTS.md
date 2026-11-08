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
- Agent setup (Claude Code): sessions build and test through the `daq-jobd` MCP tools
  (`jobd/README.md`), which the `dunedaq` plugin registers; `/dunedaq:dunedaq-env-client`
  says how, including the stack upkeep above that no broker does for them. The older broker
  (a long-lived session running `/dunedaq-env`, protocol `.claude/skills/dunedaq-env/PROTOCOL.md`)
  still works and shares the same queue. Start it on Sonnet (`claude --model sonnet`): a
  skill-frontmatter `model:` only lasts one turn.
- DUNE-DAQ org knowledge (repo list, dependencies, layering): the generated catalog from the
  `dunedaq` plugin, `cat "$(dunedaq-catalog)"`. For what a release actually built,
  `spack dependents --installed <pkg>` in an area's environment.
  Release manifests: `~/dunedaq/all_repos/daq-release/configs/`. Version tagging is
  `/dunedaq:tag-packages`, run from `~/dunedaq/all_repos`.
- `templates/bash_profile` is the source of truth for `work/.bash_profile`; `setup.sh` never
  overwrites an existing copy.
