# daq-jobd: the DUNE DAQ job service

`daq-jobd` runs DUNE DAQ requests (create a work area, clone, build, unit tests, lint,
integration tests, ...) inside the `dunedaq-app` container, and serves them to agents as
**MCP tools**. It does the mechanical part of the `dunedaq-env` broker skill
(queueing, locks, step recipes, result parsing) in code, so a client session can use the
container directly, without a long-lived broker session in between.

It is a single Python 3.9 standard-library file, `jobd/daq_jobd.py`, so nothing has to be
installed in the image.

## How it fits together

```
client session (Claude Code, Codex, ...)
   │  MCP over stdio
   ▼
bin/daq-jobd mcp            host: copies daq_jobd.py to work/.daq-jobd/, reaps dead
   │  podman exec -i          host-side locks, then execs into the container
   ▼
daq_jobd.py mcp             container: one per client; validates, writes the request,
   │  spawns (setsid)         starts a runner, answers status/result/log queries
   ▼
daq_jobd.py run-request ID  container: detached; runs the steps in order, one job each,
                              under the shared locks; outlives the client
```

State is plain files in `work/.daq-jobs/`:

- `requests/<id>.json` (the full request and per-step results) and `requests/<id>.state`
  (only the status, so the host can poll it cheaply).
- One job directory per step, in the same layout `bin/daq-job` uses. `bin/daq-job list`,
  `bin/daq-watch` and `bin/daq-stack restart-if-idle` see these jobs.
- `locks/`: the same locks as `bin/daq-job`. That gives one job per area, two in total,
  and integtests alone, across jobs from the broker and from daq-jobd.

Neither side can see the other's pids. So container-owned locks and jobs carry a
`ctr_owner` / `ctr_runner` file holding `<pid> <process start time>`. `bin/daq-job`
checks those with `podman exec`. The container cannot check host pids, so it treats
host-owned locks as alive. `bin/daq-jobd` runs `bin/daq-job reap` at every start to clear
host locks whose runner has died. A container-owned lock's `owner` file holds only the
job id, so an older `bin/daq-job` without this support never reaps it by mistake.

## Tools

| Tool | What it does |
|---|---|
| `daq_submit` | `{area, steps, from?, id?, notes?}`. Validates and acks at once (`accepted` with a queue state, or `rejected` with a reason). The steps and their arguments are those of `PROTOCOL.md`. |
| `daq_result` | `{id, wait_s?}`. Per-step status, duration and host log path. On failure, also `class`, `first_error` and `suggestion`. `wait_s` (≤600) blocks until the request finishes. |
| `daq_cancel` | Cancels a queued or running request (kills the step's process group). |
| `daq_list` | Recent requests and jobs, including the broker's. |
| `daq_log` | Tail of a step's log, ANSI codes stripped (≤400 lines). |
| `daq_areas` | Work areas with release and local repos. |
| `daq_releases` | `dbt-create -l` for nightly/candidate/stable. |
| `daq_status` | CVMFS, loopback sshd and integtest tools as seen from inside, plus active work. |

What it does mechanically that the broker did by judgment:

- Release aliases are resolved from `dbt-workarea-constants.sh`.
- A clone goes to `pythoncode/` when the repo has `pyproject.toml` and no `CMakeLists.txt`.
- Clone, checkout and commit report short SHAs.
- `-j 6` is used when another job is running.
- `unittest` results come from the Boost lines (`dbt-build` exits 0 even when tests fail).
- `lint` paths get the `sourcecode/` prefix.
- `format` answers the `yes or no` prompt under a timeout and reports changed files by md5.
- `integtest` checks the loopback sshd first, creates the `--tmpdir`, and reads pytest's
  summary lines instead of the exit code.
- Cloning a low-level package adds the dependency-layering warning.

Failure triage stays with the client: hand `steps[].log` to the `daq-log-triage` agent.

## Using it from a session

It comes with the `dunedaq` plugin (ai_tools `repos/dunedaq-ai-tools`), which registers
`${user_config.container_dir}/bin/daq-jobd mcp` as the `daq-jobd` MCP server. Any settings
profile with the plugin installed gets it, and `/mcp` shows it as `plugin:dunedaq:daq-jobd`.
Without the plugin, register it by hand:
`claude mcp add --scope user daq-jobd -- <this repo>/bin/daq-jobd mcp`.

The usual pattern:

1. `daq_submit` → ack with `id`.
2. Run `bin/daq-jobd wait <id>` with Bash `run_in_background: true`. It polls the state
   file on the host, uses no model tokens, and prints `REQUEST-ENDED <status> <id>`. For
   short requests, `daq_result` with `wait_s` works too.
3. `daq_result` → act on it. Code is edited on the host at `host_path`, as before.

Nothing is pushed: git in the container has `pushInsteadOf` set to an invalid URL.
Deleting or recreating an area is not offered at all.

## Running alongside the broker

Both can run at once because they share the jobs and locks. The broker keeps what still
needs a host and judgment: stack lifecycle (`bin/daq-stack`), installing `ps`/`jq` as root
for integtests, deleting areas with your confirmation, and the dependency analysis that
`spack dependents` needs. A client can do that analysis itself with an `exec` step.

## Testing

```bash
python3 -I jobd/test_mcp.py <existing area>   # ~2 min; read-only in that area
```

This drives `bin/daq-jobd mcp` the way a client does, against `/work/.daq-jobs-test`, so
the real queue is untouched. It covers the handshake, rejections, info/exec, failure
reporting, area-lock queueing, cancel and logs. To exercise the full step set (create →
clone → branch → commit → build → unittest → lint → format → info), submit a request into
a throwaway area with `DAQ_JOBS_DIR=/work/.daq-jobs-test bin/daq-jobd submit '{...}'`.

## Known limits

- **Merge before mixing with the broker.** A `bin/daq-job` from before this branch reports
  daq-jobd's jobs as `lost`, so `bin/daq-stack restart-if-idle` would restart the container
  under them. It never reaps their locks or kills a wrong pid, though.
- The push block is the same `pushInsteadOf` guard `bin/daq-exec` uses, extended to
  `ssh://` URLs. It stops mistakes. An `exec` step can undo it, as it always could.
- If a runner is killed with `-9`, its request and job are reported `lost` and its locks
  are reaped. But the step's own process group keeps running until it ends. Normal cancel
  (`daq_cancel`, `bin/daq-job cancel`) does kill it.
- A container restart kills all runners. Their requests show as `lost`, so resubmit.
- MCP calls block the client while waiting, so use `bin/daq-jobd wait` for long jobs.
- The integtest step is implemented but has only been checked for argument handling, not
  run end to end.

## Next: remote hosts

The container side does not depend on macOS. On a Linux build host with the same
`compose.yaml`, a client registers
`ssh <host> /path/to/dunedaq-container/bin/daq-jobd mcp` as its MCP command. Source
exchange then has to move to git: push to a bare repo under the remote `work/`, then send
a `checkout` step. `host_path` editing only works on the machine that holds `work/`.
