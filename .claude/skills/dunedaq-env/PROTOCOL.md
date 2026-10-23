# dunedaq-env message protocol (v1)

Peer Claude sessions talk to the **dunedaq-env broker** session with `SendMessage`.
The broker's current session name is written to
`/Users/wketchum/dunedaq-container/work/.daq-jobs/broker` when it starts, and
rewritten whenever the broker notices its name has changed (e.g. after a resume). Read
that file to get the `to:` address. If the file is missing, its name is not in
`ListAgents`, or a send fails, fall back to `ListAgents`. If more than one session
could be the broker, ask your user rather than guessing.

Every message is plain text whose first line is a header (`DAQ-REQUEST v1`,
`DAQ-ACK v1`, `DAQ-RESULT v1`), followed by YAML. Keep messages short. Logs are
never pasted; they are referenced by absolute host path.

## Request (peer → broker)

```yaml
DAQ-REQUEST v1
id: hdf5-003                    # requester-chosen, unique per requester
from: <your session name>       # where the reply goes
area: dev_hdf5libs_review_23Oct2026   # work area under work/; [A-Za-z0-9._-]+
steps:                          # run in order; the first failure stops the rest
  - create:  {release: last_fddaq, type: nightly}   # type: nightly|candidate|stable
  - clone:   [hdf5libs@develop, dfmodules@wketchum/my-feature, daqdataformats@v4.5.0]
  - build:   {clean: false, jobs: 6}
  - unittest: hdf5libs          # package name, or "all"
  - lint:    hdf5libs
notes: "Testing the new HDF5RawDataFile API before review"
```

### Steps

| Step | Args | What the broker runs (inside the container) |
|---|---|---|
| `create` | `release`, `type` (default `nightly`), `quick` (bool, `-q`) | `dbt-create [-b <type>] <release> <area>` |
| `recipe-create` | `recipe` (host or /work path), `build` (bool) | `dbtx-create-workarea-from-recipe.py --use-ref --use-ssh-repos [--build] --workarea-name <area> <recipe>` |
| `clone` | list of `repo@ref` (ref = branch, tag, or commit; default `develop`) | SSH clone into `sourcecode/` (C++) or `pythoncode/` (pure Python) |
| `checkout` | list of `repo@ref` | `git fetch` + `git checkout <ref>` in an existing clone; refuses if the tree is dirty |
| `branch` | `{repo, name, from?}` | `git checkout -b <name> [<from>]` (local only) |
| `commit` | `{repo, message, paths?}` | `git add <paths or -A>` + `git commit` (local only, never pushed) |
| `build` | `clean`, `jobs`, `optimize` (`O2`/`O3`/`Og`), `sanitize`, `debug` | `dbt-build [--clean] [-j N] ...` |
| `unittest` | package or `all` | `dbt-build --unittest <pkg>` |
| `lint` | package, file, or `all` | `dbt-build --lint <pkg>` |
| `format` | repo, or path within it | `dbt-clang-format.sh <path>` (rewrites files in place) |
| `integtest` | `{repos?, include?, exclude?, suite?, pytest_options?}` | `daqsystemtest_integtest_bundle.sh -r ... -k ... -x ... -s ...`, **exclusive**: waits for the whole machine |
| `recipe-save` | `name`, `require_valid_refs` (bool) | `dbtx-save-workarea-recipe.py [--require-valid-refs] <name>` |
| `exec` | `cmd` (string) | an arbitrary command in the area's environment, e.g. a script from rawdatautils or `hdf5_dump.py` |
| `info` | none | `dbt-info release` + `dbt-info sourcecode` + `git status -sb` per repo |

Standalone ops (with no `area`, or with `steps` omitted) go in a top-level `op:`:

```yaml
DAQ-REQUEST v1
id: q-17
from: <session>
op: status          # status | list-releases | list-areas | cancel
args: {type: nightly}             # list-releases
# args: {request: hdf5-003}       # cancel
```

## Ack (broker → peer), sent right away

```yaml
DAQ-ACK v1
id: hdf5-003
status: accepted | rejected
queue: "running" | "waiting for area lock" | "waiting for slot (position 1)"
host_path: /Users/wketchum/dunedaq-container/work/dev_hdf5libs_review_23Oct2026
reason: <only if rejected>
```

## Result (broker → peer), sent once all steps finish or one fails

```yaml
DAQ-RESULT v1
id: hdf5-003
status: ok | failed | cancelled
area: dev_hdf5libs_review_23Oct2026
release: NFD_DEV_261023_A9          # always the resolved name, never "last_fddaq"
host_path: /Users/wketchum/dunedaq-container/work/dev_hdf5libs_review_23Oct2026
steps:
  - {step: clone, status: ok, detail: "hdf5libs@develop (a1b2c3d), dfmodules@wketchum/my-feature (9f8e7d6)"}
  - {step: build, status: ok, duration: 6m41s, log: /Users/.../work/.daq-jobs/<job>/log}
  - {step: unittest, status: failed, duration: 48s,
     detail: "2/31 failed: HDF5RawDataFile_test (case write_read_roundtrip), HDF5FileLayout_test",
     log: /Users/.../work/dev_.../log/...}
  - {step: lint, status: skipped}
failure:                              # only when status is failed
  class: unittest | compile | link | cmake-config | clone | env-setup | cvmfs-io | lint | integtest | resource
  first_error: "src/HDF5RawDataFile.cpp:412: error: no member named 'get_fragment_ptr'"
  suggestion: "dfmodules is built from the release and links the release's hdf5libs; clone dfmodules too"
warnings: ["daqdataformats changed public headers; 4 release packages in this area depend on it"]
```

## Rules both sides rely on

- **Edits happen on the host.** Peers edit `host_path/sourcecode/<repo>/...` directly
  (it is the same directory as `/work/<area>/...` in the container). The broker never
  edits source unless a `format` step is requested.
- **Nothing is ever pushed.** Pushes are blocked inside the container. The broker
  reports branch + host path; publishing is the user's call, made from the host
  (see the `dune-github-publisher` agent in `~/dunedaq/all_repos`).
- **One job per area, two jobs total, integtests alone.** Requests queue
  instead of failing.
- The broker will **refuse**: deleting or overwriting a work area (the user must confirm
  that in the broker session), paths outside `work/`, `git push` / `gh`, rebases, and
  force operations.
- Results arrive asynchronously. A build can take many minutes, so do not poll; the
  broker messages you.
