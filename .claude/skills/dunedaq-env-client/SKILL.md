---
name: dunedaq-env-client
description: Ask the dunedaq-env broker session to create DUNE DAQ work areas, clone repos, build, and run unit/lint/integration tests in the Podman container, then act on its results. Use when a session needs DUNE DAQ code built or tested, or needs a dbt work area, and is not itself the broker.
---

# Using the dunedaq-env broker

DUNE DAQ builds run only inside a Podman container managed by one long-lived Claude
session: the **dunedaq-env broker**. You don't run `podman`, `dbt-build`, or the
`bin/daq-*` scripts yourself. You send it requests.

Protocol spec: `/Users/wketchum/dunedaq-container/.claude/skills/dunedaq-env/PROTOCOL.md`.
Read it before your first request.

## How

1. Get the broker's address: `cat /Users/wketchum/dunedaq-container/work/.daq-jobs/broker`,
   and check that name appears in `ListAgents`. The broker can be renamed when its session
   restarts, and the file can lag behind. If the name is missing from `ListAgents`, or a
   send to it fails:
   - Look in `ListAgents` for another session that could be the broker (it runs in
     `~/dunedaq-container`; the name often starts with `dunedaq-container`, but not always).
     Don't guess between candidates. Ask the user which session is the broker, naming the
     candidates, and tell them the address file is stale.
   - If there is no plausible candidate, tell the user to start one
     (`cd ~/dunedaq-container && claude`, then `/dunedaq-env`) and stop.
   Once you're talking to the broker, use the name from its `DAQ-ACK` replies.
2. Send a `DAQ-REQUEST v1` with `SendMessage`. Include `from:` (your own session name, from
   `ListAgents`) and a unique `id`. Put all steps for one goal in a single request, e.g.
   `create` → `clone` → `build` → `unittest`.
3. Expect a `DAQ-ACK v1` quickly and a `DAQ-RESULT v1` when done. Builds take minutes,
   so carry on with other work or tell the user you're waiting. Don't poll.
4. **Edit code on the host**, in `<host_path>/sourcecode/<repo>/` (or `pythoncode/`). That
   is the same directory the container builds from. Then request `build` / `unittest` again.
5. When a result has `failure.log` / `evidence_log`, read only the relevant part of that
   file (around `first_error`), not the whole log.

## Good habits

- Reuse one area per topic. Name new ones like the user does: `dev_<topic>_<DDMonYYYY>`
  (e.g. `dev_hdf5libs_review_23Oct2026`).
- Prefer `release: last_fddaq` for develop work, unless the user names a release.
  Feature work goes on a branch `<user>/<topic>`.
- If you change public headers in a low-level package, expect the broker to warn about
  dependents. Ask it to clone those dependents if they need rebuilding.
- Integration tests are heavy (they take the whole machine). Ask for them only when the
  user wants them, and narrow them with `include:`.
- Nothing gets pushed by the broker. To publish, tell the user the host path and branch;
  they push from the host (or via `/tag-packages` for version bumps).
