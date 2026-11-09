#!/usr/bin/env python3
"""daq-jobd: the DUNE DAQ job service. Runs inside the dunedaq-app container, or
natively on a Linux host with CVMFS (a "native host", configured by DAQ_JOBD_CONF).

Takes DAQ requests (create, clone, build, unittest, lint, integtest, ...; the step
set of .claude/skills/dunedaq-env/PROTOCOL.md) as MCP tool calls, runs each request
in a detached runner process, and keeps all state in files under /work/.daq-jobs.
Job records and locks use the same layout as bin/daq-job, so jobs started here and
jobs started by the host-side broker share one queue: one job per area, MAX_SLOTS
jobs in total, integration tests alone.

  daq_jobd.py mcp                  MCP server on stdin/stdout (bin/daq-jobd mcp starts it)
  daq_jobd.py submit JSON          submit a request ({"area": ..., "steps": [...]})
  daq_jobd.py result ID [WAIT_S]   print a request's result, optionally waiting for it
  daq_jobd.py list | cancel ID     recent requests / cancel one
  daq_jobd.py wait ID [SECS]       block until request ID ends; prints REQUEST-ENDED <status> <id>
  daq_jobd.py snapshot [HOURS [LOG_LINES]]   JSON for the monitor (bin/daq-jobd top): active
                                   and queued requests with log tails, slots, health, history
  daq_jobd.py run-request ID       the detached runner (internal)

Host settings (work root, environment setup, slots, git mirror, ...) default to the
container's and can be replaced by a KEY=VALUE file named by $DAQ_JOBD_CONF; see
jobd/native.conf.example and jobd/README.md, "Native hosts".

Container-owned locks and jobs are marked with a `ctr_owner` / `ctr_runner` file
("<pid> <proc start time>"), because the host cannot see container pids and vice
versa. Their `owner` file holds only the job id, so a bin/daq-job that predates
this never reaps them.

Python 3.9 standard library only: the container has nothing else installed.
"""

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

POLL = 5
VERSION = "0.2.0"

# Host settings. The defaults are the dunedaq-app container's; $DAQ_JOBD_CONF names a
# KEY=VALUE file that overrides them (jobd/native.conf.example). {work} and {area} in
# the SETUP_* commands are filled in per job.
CONF = {
    "MODE": "container",          # container | native
    "HOST_LABEL": "",             # shown in results, e.g. daq.fnal.gov
    "WORK": "/work",              # where work areas live
    "JOBS": "",                   # job records and locks; default WORK/.daq-jobs
    "MAX_SLOTS": "2",
    "DEFAULT_JOBS": "",           # dbt-build -j when a request gives none (empty: dbt's choice)
    "SETUP_ROOT": "cd {work} && source /cvmfs/dunedaq.opensciencegrid.org/setup_dunedaq.sh"
                  " && setup_dbt latest",
    "SETUP_AREA": "cd {work}/{area} && spackroot && source ./env.sh && _dbt_export_spack_fns",
    "GIT_URL": "git@github.com:DUNE-DAQ/{repo}.git",
    "MIRROR": "",                 # bare mirror (jobd/update_mirror.sh); clone/checkout use it
    "RECIPE_SSH_REPOS": "1",      # dbtx-create-workarea-from-recipe.py --use-ssh-repos
    "SELF_SSH_HOST": "dunedaq-app",  # what drunc's ssh process manager connects to
    # all: an integtest takes every slot (the Mac VM is too small to share with it).
    # integtest: one integtest at a time, other jobs run alongside within MAX_SLOTS.
    "INTEGTEST_EXCLUSIVE": "all",
}


def load_conf(path):
    with open(path) as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, sep, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not sep or key not in CONF:
                raise SystemExit("daq-jobd: %s:%d: unknown setting %r" % (path, n, key))
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            CONF[key] = value


if os.environ.get("DAQ_JOBD_CONF"):
    load_conf(os.environ["DAQ_JOBD_CONF"])


def _path(v):
    return os.path.abspath(os.path.expandvars(os.path.expanduser(v))) if v else ""


NATIVE = CONF["MODE"] == "native"
WORK = _path(CONF["WORK"])
JOBS = _path(os.environ.get("DAQ_JOBS_DIR") or CONF["JOBS"]) or os.path.join(WORK, ".daq-jobs")
LOCKS = os.path.join(JOBS, "locks")
REQS = os.path.join(JOBS, "requests")
HOST_WORK = os.environ.get("DAQ_HOST_WORK", "")
MAX_SLOTS = int(CONF["MAX_SLOTS"])
MIRROR = _path(CONF["MIRROR"])
if CONF["INTEGTEST_EXCLUSIVE"] not in ("all", "integtest"):
    raise SystemExit("daq-jobd: INTEGTEST_EXCLUSIVE is all or integtest")
EXCLUSIVE_ALL = CONF["INTEGTEST_EXCLUSIVE"] == "all"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
REF_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/+-]*$")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x1b\[K")
BEGIN = "DAQJOBD-BEGIN"

SETUP_ROOT = CONF["SETUP_ROOT"].replace("{work}", shlex.quote(WORK))
SETUP_AREA = CONF["SETUP_AREA"].replace("{work}", shlex.quote(WORK))

# Pushes to GitHub are impossible from jobs: GitHub URLs map to an invalid scheme.
# An exec step can still undo this; it guards against mistakes, not against intent.
GIT_ENV = {"GIT_CONFIG_COUNT": "3"}
for _i, _url in enumerate(("git@github.com:", "https://github.com/", "ssh://git@github.com/")):
    GIT_ENV["GIT_CONFIG_KEY_%d" % _i] = "url.no-push-from-container:///.pushInsteadOf"
    GIT_ENV["GIT_CONFIG_VALUE_%d" % _i] = _url

# Changing public headers in these means release packages built against the
# release's copy may need cloning too.
LOW_LEVEL = {"daqdataformats", "detdataformats", "ers", "logging", "appfwk", "hdf5libs",
             "iomanager", "opmonlib", "utilities", "cmdlib", "serialization", "fddetdataformats",
             "trgdataformats", "conffwk", "appmodel", "confmodel"}

TERMINAL = ("ok", "failed", "cancelled", "lost", "rejected")


class Cancelled(Exception):
    pass


class Invalid(Exception):
    pass


# ── small helpers ─────────────────────────────────────────────────────────────

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return default


def write(path, text):
    tmp = "%s.tmp%d" % (path, os.getpid())
    with open(tmp, "w") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    os.replace(tmp, path)


def host_path(p):
    if HOST_WORK and (p == WORK or p.startswith(WORK + "/")):
        return HOST_WORK + p[len(WORK):]
    return p


def to_container_path(p):
    if HOST_WORK and (p == HOST_WORK or p.startswith(HOST_WORK + "/")):
        return WORK + p[len(HOST_WORK):]
    return p


def is_area(area):
    return os.path.isfile(os.path.join(WORK, area, "env.sh"))


def release_of(area):
    text = read(os.path.join(WORK, area, "dbt-workarea-constants.sh"))
    m = re.search(r'SPACK_RELEASE="([^"]+)"', text)
    return m.group(1) if m else ""


def repos_of(area):
    out = []
    for sub in ("sourcecode", "pythoncode"):
        base = os.path.join(WORK, area, sub)
        if os.path.isdir(base):
            out += ["%s/%s" % (sub, r) for r in sorted(os.listdir(base))
                    if os.path.isdir(os.path.join(base, r, ".git"))]
    return out


def strip_ansi(text):
    return ANSI_RE.sub("", text)


def fmt_secs(s):
    s = int(s)
    return "%dm%02ds" % (s // 60, s % 60) if s >= 60 else "%ds" % s


def q(s):
    return shlex.quote(str(s))


# ── process identity (pid + start time, so a reused pid is not mistaken) ──────

def proc_start(pid):
    """Start time of a live process, or None (gone, or a zombie nobody has reaped yet)."""
    try:
        with open("/proc/%d/stat" % pid) as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return None if fields[0] in ("Z", "X") else fields[19]
    except (OSError, IndexError, ValueError):
        return None


def ident(pid=None):
    pid = pid or os.getpid()
    return "%d %s" % (pid, proc_start(pid))


def ident_alive(s):
    try:
        pid, start = s.split()
        return proc_start(int(pid)) == start
    except ValueError:
        return False


# ── locks: mkdir is atomic; same directory layout as bin/daq-job ──────────────

HELD = []


def lock_alive(d):
    ctr = read(os.path.join(d, "ctr_owner"))
    if ctr:
        return ident_alive(ctr)
    if not os.path.exists(os.path.join(d, "owner")):
        # Owner died between mkdir and writing its owner file (or is writing it now).
        try:
            return time.time() - os.stat(d).st_mtime < 60
        except OSError:
            return False
    # Owned by a host-side bin/daq-job runner. Only the host can check host pids;
    # bin/daq-jobd reaps dead host locks at startup.
    return True


def reap_lock(d):
    """Remove lock d if its owner is dead. Reapers (here and bin/daq-job) take the
    locks/.reap mutex and re-check inside it, so two of them never both remove a
    lock that one of them has just re-taken."""
    mutex = os.path.join(LOCKS, ".reap")
    try:
        os.mkdir(mutex)
    except FileExistsError:
        try:
            if time.time() - os.stat(mutex).st_mtime > 60:  # its holder died
                os.rmdir(mutex)
        except OSError:
            pass
        return
    try:
        if os.path.isdir(d) and not lock_alive(d):
            shutil.rmtree(d, ignore_errors=True)
    finally:
        os.rmdir(mutex)


def try_lock(name, jid):
    d = os.path.join(LOCKS, name)
    for _ in range(2):
        try:
            os.mkdir(d)
        except FileExistsError:
            if lock_alive(d):
                return False
            reap_lock(d)
            continue
        write(os.path.join(d, "ctr_owner"), ident())
        write(os.path.join(d, "owner"), jid)
        HELD.append(name)
        return True
    return False


def release_all():
    me = ident()
    while HELD:
        d = os.path.join(LOCKS, HELD.pop())
        if read(os.path.join(d, "ctr_owner")) == me:
            shutil.rmtree(d, ignore_errors=True)


def acquire(jid, area, exclusive):
    if area and not try_lock("area-" + area, jid):
        return False
    if exclusive and not EXCLUSIVE_ALL:
        # Only one integtest at a time; it then needs a single slot like any job.
        if not try_lock("integtest", jid):
            release_all()
            return False
        exclusive = False
    got = 0
    for i in range(1, MAX_SLOTS + 1):
        if try_lock("slot-%d" % i, jid):
            got += 1
            if not exclusive:
                return True
        elif exclusive:
            release_all()
            return False
    if got:
        return True
    release_all()
    return False


# ── job records (one per step; bin/daq-job list shows them too) ───────────────

def job_ids():
    try:
        names = os.listdir(JOBS)
    except OSError:
        return []
    return sorted(n for n in names if os.path.isfile(os.path.join(JOBS, n, "state")))


def job_state(jid):
    d = os.path.join(JOBS, jid)
    s = read(os.path.join(d, "state"))
    if s in ("queued", "running"):
        ctr = read(os.path.join(d, "ctr_runner"))
        if ctr and not ident_alive(ctr):
            s = "lost"
    return s


def queue_position(jid):
    n = 0
    for j in job_ids():
        if j == jid:
            break
        if read(os.path.join(JOBS, j, "state")) == "queued":
            n += 1
    return n


def running_jobs():
    return [j for j in job_ids() if job_state(j) == "running"]


# ── requests ──────────────────────────────────────────────────────────────────

def req_path(rid, ext="json"):
    return os.path.join(REQS, "%s.%s" % (rid, ext))


def load_req(rid):
    with open(req_path(rid)) as f:
        return json.load(f)


def save_req(req):
    write(req_path(req["id"]), json.dumps(req, indent=1))
    write(req_path(req["id"], "state"), req["status"])


def req_status(req):
    s = req["status"]
    if s in ("queued", "running"):
        runner = req.get("runner", "")
        if runner:
            if not ident_alive(runner):
                s = "lost"
        elif time.time() - req.get("created_ts", 0) > 60:
            s = "lost"
    return s


def request_ids():
    try:
        names = os.listdir(REQS)
    except OSError:
        return []
    return sorted(n[:-5] for n in names if n.endswith(".json"))


# ── the runner ────────────────────────────────────────────────────────────────

CHILD = None


def on_term(signum, frame):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if CHILD is not None and CHILD.poll() is None:
        try:
            os.killpg(CHILD.pid, signal.SIGTERM)
        except OSError:
            pass
    raise Cancelled()


def new_job_dir(label):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    lab = re.sub(r"[^A-Za-z0-9_]", "-", label)[:24]
    for n in range(100):
        jid = "%s-%s-c%d%s" % (stamp, lab, os.getpid(), "" if n == 0 else chr(ord("a") + n - 1))
        try:
            os.mkdir(os.path.join(JOBS, jid))
            return jid
        except FileExistsError:
            continue
    raise RuntimeError("cannot create a job directory")


def run_job(req, label, cmd, env="area", exclusive=False):
    """Run one step's command under the locks; return (job id, exit code, log text).

    @JOBDIR@ in cmd becomes the job's directory, for files a step parses afterwards.
    """
    global CHILD
    area = req["area"]
    jid = new_job_dir("%s-%s" % (req["id"], label))
    d = os.path.join(JOBS, jid)
    cmd = cmd.replace("@JOBDIR@", q(d))
    for name, value in (("cmd", cmd), ("area", area), ("label", "%s-%s" % (req["id"], label)),
                        ("requester", req.get("from") or "-"), ("exclusive", str(int(exclusive))),
                        ("created", now()), ("request", req["id"]), ("ctr_runner", ident()),
                        ("state", "queued")):
        write(os.path.join(d, name), value)
    log = os.path.join(d, "log")
    open(log, "w").close()
    req["current_job"] = jid
    save_req(req)

    def cancelled():
        return (read(os.path.join(d, "state")) == "cancelled"
                or os.path.exists(req_path(req["id"], "cancel")))

    rc = None
    try:
        while not acquire(jid, area, exclusive):
            if cancelled():
                raise Cancelled()
            time.sleep(POLL)
        if cancelled():
            raise Cancelled()
        write(os.path.join(d, "state"), "running")
        write(os.path.join(d, "started"), now())

        if env == "area":
            setup = SETUP_AREA.replace("{area}", area)
        elif env == "root":
            setup = SETUP_ROOT
        else:
            setup = "cd %s" % q(os.path.join(WORK, area))
        script = ("{ %s ; } || { echo 'daq-jobd: environment setup failed' >&2; exit 4; }\n"
                  "echo %s\n%s\n" % (setup, BEGIN, cmd))
        with open(log, "ab") as out:
            CHILD = subprocess.Popen(["bash", "-lc", script], stdin=subprocess.DEVNULL,
                                     stdout=out, stderr=subprocess.STDOUT, cwd=WORK,
                                     env=dict(os.environ, DAQJOBD_MIRROR=MIRROR, **GIT_ENV),
                                     start_new_session=True)
            write(os.path.join(d, "container_pgid"), str(CHILD.pid))
            rc = CHILD.wait()
        # bin/daq-job cancel (on the host) kills the process group and marks the job.
        if read(os.path.join(d, "state")) == "cancelled":
            raise Cancelled()
        write(os.path.join(d, "state"), "done" if rc == 0 else "failed")
    except Cancelled:
        write(os.path.join(d, "state"), "cancelled")
        raise
    finally:
        if CHILD is not None and CHILD.poll() is None:
            try:
                os.killpg(CHILD.pid, signal.SIGKILL)
            except OSError:
                pass
        CHILD = None
        write(os.path.join(d, "finished"), now())
        write(os.path.join(d, "exit_code"), str(rc if rc is not None else -1))
        release_all()
    with open(log, errors="replace") as f:
        text = strip_ansi(f.read())
    return jid, rc, text


def job_duration(jid):
    d = os.path.join(JOBS, jid)
    try:
        t0 = time.mktime(time.strptime(read(os.path.join(d, "started")), "%Y-%m-%dT%H:%M:%S"))
        t1 = time.mktime(time.strptime(read(os.path.join(d, "finished")), "%Y-%m-%dT%H:%M:%S"))
        return fmt_secs(t1 - t0)
    except ValueError:
        return ""


def after_begin(text):
    i = text.rfind(BEGIN + "\n")
    return text[i + len(BEGIN) + 1:] if i >= 0 else text


def tail(text, n=40, limit=4000):
    out = "\n".join(text.rstrip("\n").splitlines()[-n:])
    return out[-limit:]


def markers(text, key):
    prefix = "DAQJOBD %s " % key
    return [ln[len(prefix):].strip() for ln in text.splitlines() if ln.startswith(prefix)]


def classify(step, text, rc):
    def first(pattern):
        for ln in text.splitlines():
            if re.search(pattern, ln):
                return ln.strip()[:300]
        return ""

    if "daq-jobd: environment setup failed" in text:
        cls, err = "env-setup", first(r"rror|not found|No such file")
    elif re.search(r"cvmfs\S*: Input/output error|Input/output error.*cvmfs", text):
        cls, err = "cvmfs-io", first(r"Input/output error")
    elif step in ("clone", "checkout", "branch", "commit"):
        cls, err = "clone", first(r"^fatal:|^error:|DAQJOBD error")
    elif step == "build":
        if "CMake Error" in text:
            cls, err = "cmake-config", first(r"CMake Error")
        elif re.search(r"undefined reference|ld returned", text):
            cls, err = "link", first(r"undefined reference|ld returned")
        else:
            cls, err = "compile", first(r"(fatal )?error:") or first(r"\bERROR\b")
    elif step == "unittest":
        cls, err = step, first(r'error: in "') or first(r"failures? (is|are) detected|FAILURE")
    elif step in ("lint", "integtest"):
        cls, err = step, first(r"FAILED|Total errors found: [1-9]|[Ee]rror(?!s found: 0)")
    else:
        cls, err = step, first(r"[Ee]rror|ERROR|fatal:|DAQJOBD error")
    suggestion = "Give the log to the daq-log-triage agent for a diagnosis."
    if cls == "cvmfs-io":
        suggestion = "CVMFS read error: on the host run bin/daq-stack restart-if-idle, then retry the step."
    elif cls == "env-setup":
        suggestion = ("Area environment did not load. Check that work/.bash_profile matches "
                      "templates/bash_profile and that CVMFS is readable (bin/daq-stack status).")
    return {"class": cls, "first_error": err or "exit code %s" % rc, "suggestion": suggestion}


def step_result(step, jid, rc, text, status=None, detail="", **extra):
    status = status or ("ok" if rc == 0 else "failed")
    res = {"step": step, "status": status, "duration": job_duration(jid),
           "log": host_path(os.path.join(JOBS, jid, "log"))}
    if detail:
        res["detail"] = detail
    res.update(extra)
    failure = classify(step, text, rc) if status == "failed" else None
    return res, failure


def repo_dir_sh(repo):
    return ('d=sourcecode/%s; [ -d "$d" ] || d=pythoncode/%s; '
            '[ -d "$d/.git" ] || { echo "DAQJOBD error no clone of %s in this area"; exit 9; }; '
            % (repo, repo, repo))


# ── step implementations ──────────────────────────────────────────────────────

def step_create(req, a):
    typ = a.get("type", "nightly")
    cmd = "dbt-create %s%s%s %s" % ("" if typ == "stable" else "-b %s " % q(typ),
                                    "-q " if a.get("quick") else "", q(a["release"]), q(req["area"]))
    jid, rc, text = run_job(req, "create", cmd, env="root")
    if rc == 0:
        req["release"] = release_of(req["area"])
    return step_result("create", jid, rc, text, detail="%s (%s)" % (req.get("release") or a["release"], typ))


def step_recipe_create(req, a):
    cmd = "dbtx-create-workarea-from-recipe.py --use-ref %s%s--workarea-name %s %s" % (
        "--use-ssh-repos " if CONF["RECIPE_SSH_REPOS"] == "1" else "",
        "--build " if a.get("build") else "", q(req["area"]), q(a["recipe"]))
    jid, rc, text = run_job(req, "recipe-create", cmd, env="root")
    if rc == 0:
        req["release"] = release_of(req["area"])
    return step_result("recipe-create", jid, rc, text, detail=req.get("release", ""))


# Shell helpers for clone/checkout from MIRROR. In a work clone, `daqfnal/*` are the
# branches pushed to the mirror from your own machine and `origin/*` are GitHub's
# (as of the mirror's last hourly update). A ref resolves to a pushed branch first,
# then a GitHub branch, a tag, a commit. Branches get a local branch tracking their
# source; that refuses to drop commits made locally on the host.
MIRROR_SH = r'''
daqjobd_fetch() {  # DIR REPO
  git -C "$1" remote get-url daqfnal >/dev/null 2>&1 || git -C "$1" remote add daqfnal "$DAQJOBD_MIRROR/$2.git"
  git -C "$1" fetch -q --prune daqfnal &&
    git -C "$1" fetch -q --prune "$DAQJOBD_MIRROR/$2.git" '+refs/remotes/origin/*:refs/remotes/origin/*' '+refs/tags/*:refs/tags/*'
}
daqjobd_resolve() {  # DIR REF -> checks it out, prints the source
  local d=$1 ref=$2 src=
  if git -C "$d" show-ref -q --verify "refs/remotes/daqfnal/$ref"; then src=daqfnal/$ref
  elif git -C "$d" show-ref -q --verify "refs/remotes/origin/$ref"; then src=origin/$ref
  elif git -C "$d" show-ref -q --verify "refs/tags/$ref"; then
    git -C "$d" checkout -q --detach "refs/tags/$ref" && echo "tag $ref"; return
  elif git -C "$d" rev-parse -q --verify "$ref^{commit}" >/dev/null; then
    git -C "$d" checkout -q --detach "$ref" && echo "commit"; return
  else echo "DAQJOBD error $ref is not a pushed branch, GitHub branch, tag or commit in the mirror"; return 1
  fi
  if git -C "$d" show-ref -q --verify "refs/heads/$ref" && [ -n "$(git -C "$d" rev-list "$src..refs/heads/$ref")" ]; then
    echo "DAQJOBD error local branch $ref has commits that $src does not"; return 1
  fi
  git -C "$d" checkout -q -B "$ref" --track "$src" && echo "$src"
}
'''


def mirror_clone_sh(repo, ref):
    return (
        "t=$(mktemp -d .daqjobd-clone.XXXXXX) && r=\"$t/%(repo)s\" && git init -q \"$r\" && "
        "git -C \"$r\" remote add origin %(url)s && daqjobd_fetch \"$r\" %(repo)s && "
        "src=$(daqjobd_resolve \"$r\" %(ref)s) && "
        "if [ -f \"$r/pyproject.toml\" ] && [ ! -f \"$r/CMakeLists.txt\" ]; "
        "then d=pythoncode; else d=sourcecode; fi && mkdir -p $d && [ ! -e $d/%(repo)s ] && "
        "mv \"$r\" $d/ && rmdir \"$t\" && "
        "echo \"DAQJOBD clone %(repo)s@%(ref_txt)s from $src ($(git -C $d/%(repo)s rev-parse --short HEAD)) -> $d\" "
        "|| { echo \"$src\" | grep '^DAQJOBD'; rm -rf \"$t\"; echo 'DAQJOBD error clone of %(repo)s@%(ref_txt)s failed'; exit 9; }"
        % {"repo": repo, "ref": q(ref), "ref_txt": ref, "url": q(CONF["GIT_URL"].replace("{repo}", repo))})


def step_clone(req, a):
    if MIRROR:
        cmd = MIRROR_SH + "\n".join(mirror_clone_sh(repo, ref) for repo, ref in a["repos"])
        jid, rc, text = run_job(req, "clone", cmd, env="none")
        return clone_result(req, a, jid, rc, text)
    parts = []
    for repo, ref in a["repos"]:
        url = CONF["GIT_URL"].replace("{repo}", repo)
        parts.append(
            "t=$(mktemp -d .daqjobd-clone.XXXXXX) && "
            "{ git clone -q -b %(ref)s %(url)s \"$t/%(repo)s\" 2>/dev/null || "
            "{ rm -rf \"$t/%(repo)s\"; git clone -q %(url)s \"$t/%(repo)s\" && "
            "git -C \"$t/%(repo)s\" checkout -q %(ref)s; }; } && "
            "if [ -f \"$t/%(repo)s/pyproject.toml\" ] && [ ! -f \"$t/%(repo)s/CMakeLists.txt\" ]; "
            "then d=pythoncode; else d=sourcecode; fi && mkdir -p $d && [ ! -e $d/%(repo)s ] && "
            "mv \"$t/%(repo)s\" $d/ && rmdir \"$t\" && "
            "echo \"DAQJOBD clone %(repo)s@%(ref)s ($(git -C $d/%(repo)s rev-parse --short HEAD)) -> $d\" "
            "|| { rm -rf \"$t\"; echo 'DAQJOBD error clone of %(repo)s@%(ref)s failed'; exit 9; }"
            % {"repo": repo, "ref": q(ref), "url": url})
    jid, rc, text = run_job(req, "clone", "\n".join(parts), env="none")
    return clone_result(req, a, jid, rc, text)


def clone_result(req, a, jid, rc, text):
    warnings = []
    for repo, _ in a["repos"]:
        if repo in LOW_LEVEL:
            warnings.append("%s is low level: release packages that depend on it link against the "
                            "release's copy. If you change its public headers, find them with "
                            "an exec step running `spack dependents --installed --transitive %s`."
                            % (repo, repo))
    stuck = quick_area_python(req["area"])
    if stuck:
        warnings.append("this area has no .venv (created with quick), so build will refuse while "
                        "it has Python repos: %s" % ", ".join(stuck))
    req.setdefault("warnings", []).extend(warnings)
    return step_result("clone", jid, rc, text, detail=", ".join(markers(text, "clone")))


def step_checkout(req, a):
    parts = []
    for repo, ref in a["repos"]:
        dirty = (repo_dir_sh(repo) +
                 "if [ -n \"$(git -C $d status --porcelain --untracked-files=no)\" ]; then "
                 "echo \"DAQJOBD error %s has uncommitted changes:\"; "
                 "git -C $d status --short --untracked-files=no; exit 9; fi; " % repo)
        if MIRROR:
            parts.append(
                dirty + "daqjobd_fetch $d %(repo)s && src=$(daqjobd_resolve $d %(ref)s) && "
                "echo \"DAQJOBD checkout %(repo)s@%(ref_txt)s from $src ($(git -C $d rev-parse --short HEAD))\" "
                "|| { echo \"$src\" | grep '^DAQJOBD'; exit 9; }"
                % {"repo": repo, "ref": q(ref), "ref_txt": ref})
        else:
            parts.append(
                dirty + "git -C $d fetch -q --tags origin && git -C $d checkout -q %(ref)s && "
                "echo \"DAQJOBD checkout %(repo)s@%(ref)s ($(git -C $d rev-parse --short HEAD))\" || exit 9"
                % {"repo": repo, "ref": q(ref)})
    jid, rc, text = run_job(req, "checkout", (MIRROR_SH if MIRROR else "") + "\n".join(parts), env="none")
    return step_result("checkout", jid, rc, text, detail=", ".join(markers(text, "checkout")))


def step_branch(req, a):
    cmd = (repo_dir_sh(a["repo"]) + "git -C $d checkout -b %s %s && "
           "echo \"DAQJOBD branch %s on %s ($(git -C $d rev-parse --short HEAD))\""
           % (q(a["name"]), q(a["from"]) if a.get("from") else "", a["name"], a["repo"]))
    jid, rc, text = run_job(req, "branch", cmd, env="none")
    return step_result("branch", jid, rc, text, detail=", ".join(markers(text, "branch")))


def step_commit(req, a):
    paths = " ".join(q(p) for p in a.get("paths") or [])
    cmd = (repo_dir_sh(a["repo"]) + "git -C $d add %s && git -C $d commit -q -m %s && "
           "echo \"DAQJOBD commit $(git -C $d rev-parse --abbrev-ref HEAD) "
           "($(git -C $d rev-parse --short HEAD))\""
           % ("-- " + paths if paths else "-A", q(a["message"])))
    jid, rc, text = run_job(req, "commit", cmd, env="none")
    return step_result("commit", jid, rc, text, detail=", ".join(markers(text, "commit")))


def quick_area_python(area):
    """Python repos in an area made with create quick (-q, no .venv), which dbt-build can't install."""
    if os.path.isdir(os.path.join(WORK, area, ".venv")):
        return []
    return [r for r in repos_of(area) if r.startswith("pythoncode/")]


def step_build(req, a):
    stuck = quick_area_python(req["area"])
    if stuck:
        return {"step": "build", "status": "failed", "detail": "not run"}, {
            "class": "env-setup",
            "first_error": "%s has no .venv (created with quick) but has Python repos: %s"
                           % (req["area"], ", ".join(stuck)),
            "suggestion": "dbt-build would fail installing them into the read-only release venv. "
                          "Use an area created without quick, or remove those clones."}
    flags = []
    if a.get("clean"):
        flags.append("--clean")
    if a.get("debug"):
        flags.append("--debug")
    jobs = a.get("jobs") or CONF["DEFAULT_JOBS"] or (6 if running_jobs() else None)
    if jobs:
        flags.append("-j %d" % int(jobs))
    if a.get("optimize"):
        flags.append("--optimize-flag %s" % q(a["optimize"]))
    if a.get("sanitize"):
        flags.append("--sanitize=%s" % q(a["sanitize"]))
    jid, rc, text = run_job(req, "build", "dbt-build " + " ".join(flags))
    return step_result("build", jid, rc, text,
                       detail=("-j %s" % jobs) if jobs else "", build_logs=host_path(
                           os.path.join(WORK, req["area"], "log")))


def parse_unittest(text):
    modules = re.findall(r'Leaving test module "([^"]+)"', text)
    clean = len(re.findall(r"\*\*\* No errors detected", text))
    failed = re.findall(r'\*\*\* (\d+) failures? (?:is|are) detected in the test module "([^"]+)"', text)
    return modules, clean, failed


def step_unittest(req, a):
    pkg = a.get("package", "all")
    jid, rc, text = run_job(req, "unittest", "dbt-build --unittest" + ("" if pkg == "all" else " " + q(pkg)))
    modules, clean, failed = parse_unittest(text)
    bad = ["%s (%s failure%s)" % (m, n, "" if n == "1" else "s") for n, m in failed]
    crashed = len(modules) - clean - len(failed)
    if crashed > 0:
        bad.append("%d module(s) ended without a result line (crash?)" % crashed)
    if rc != 0 or bad:
        detail = "%d/%d modules passed" % (clean, len(modules))
        if bad:
            detail += ": " + "; ".join(bad)
        return step_result("unittest", jid, rc, text, status="failed", detail=detail,
                           test_logs=host_path(os.path.join(WORK, req["area"], "log")))
    detail = "%d/%d modules passed" % (clean, len(modules))
    res = step_result("unittest", jid, rc, text, detail=detail)
    if not modules:
        res[0]["detail"] = "no unit test modules ran"
    return res


def step_lint(req, a):
    target = a.get("target", "all")
    if target == "all":
        arg = ""
    elif "/" in target and not target.startswith("sourcecode/"):
        arg = " " + q("sourcecode/" + target)
    else:
        arg = " " + q(target)
    jid, rc, text = run_job(req, "lint", "dbt-build --lint" + arg)
    counts = [int(n) for n in re.findall(r"Total errors found: (\d+)", text)]
    if counts and sum(counts):
        return step_result("lint", jid, rc, text, status="failed",
                           detail="%d style errors" % sum(counts))
    return step_result("lint", jid, rc, text, detail="%d style errors" % sum(counts) if counts else "")


def step_format(req, a):
    path = q(a["path"])
    sums = ("find " + path + " -type f \\( -name '*.cpp' -o -name '*.hpp' -o -name '*.cxx' "
            "-o -name '*.hxx' -o -name '*.cc' -o -name '*.h' \\) -exec md5sum {} + | sort > @JOBDIR@/fmt_")
    cmd = ("cd sourcecode && %sbefore && { echo yes | timeout --foreground 300 dbt-clang-format.sh %s; } && %safter"
           % (sums, path, sums))
    jid, rc, text = run_job(req, "format", cmd)

    def sums_of(which):
        lines = read(os.path.join(JOBS, jid, "fmt_" + which)).splitlines()
        return {f: h for h, f in (ln.split(None, 1) for ln in lines if ln.strip())}
    before, after = sums_of("before"), sums_of("after")
    changed = sorted(f for f in after if before.get(f) != after[f])
    detail = ("%d file(s) reformatted: %s" % (len(changed), ", ".join(changed[:20]))) if changed else "no changes"
    return step_result("format", jid, rc, text, detail=detail)


def parse_integtest(text):
    i = text.rfind("SUMMARY")
    summary = text[i:] if i >= 0 else ""
    totals, failing, current = {}, [], None
    for ln in summary.splitlines():
        m = re.search(r"Running (\S+)", ln)
        if m:
            current = m.group(1)
            continue
        m = re.match(r"=+ (.+) in [\d.]+s", ln.strip())
        if m:
            bad = False
            for n, kind in re.findall(r"(\d+) (passed|failed|skipped|errors?|xfailed|xpassed|deselected)", m.group(1)):
                kind = "error" if kind.startswith("error") else kind
                totals[kind] = totals.get(kind, 0) + int(n)
                bad = bad or (kind in ("failed", "error") and int(n) > 0)
            if bad and current:
                failing.append(current)
    return totals, failing


def step_integtest(req, a):
    label = "integtest-%s" % time.strftime("%H%M%S")
    # Some tests size themselves from the free space under the tmpdir (insufficient_disk_space_test),
    # so tmpdir lets them run on a local disk like CI's /tmp instead of the work area's filesystem.
    if a.get("tmpdir"):
        tmpdir = os.path.join(a["tmpdir"], "daq-jobd-%s-%s-%s" % (req["area"], req["id"], label))
    else:
        tmpdir = os.path.join(WORK, req["area"], "integtest_tmp", "%s-%s" % (req["id"], label))
    opts = ["-r %s" % q(a.get("repos", "daqsystemtest"))]
    if a.get("include"):
        opts.append("-k %s" % q(a["include"]))
    if a.get("exclude"):
        opts.append("-x %s" % q(a["exclude"]))
    if a.get("suite"):
        opts.append("-s %s" % q(a["suite"]))
    if a.get("pytest_options"):
        opts.append("--pytest-options %s" % q(a["pytest_options"]))
    if NATIVE:
        fix_ssh = "passwordless ssh from this host to itself is needed for drunc's process manager"
        fix_tools = "harmless; ask the host's admins"
    else:
        fix_ssh = "on the host run bin/daq-stack ensure (loopback sshd, port 2222)"
        fix_tools = ("harmless; install on the host: podman exec -u root dunedaq-app dnf install -y -q "
                     "--disablerepo=epel procps-ng jq")
    cmd = ("ssh -o BatchMode=yes -o ConnectTimeout=5 %s true || { echo %s; exit 7; }\n"
           "for t in ps jq; do command -v $t >/dev/null || echo \"DAQJOBD warning $t is missing\" %s; done\n"
           % (CONF["SELF_SSH_HOST"], q("DAQJOBD error ssh to %s failed; %s" % (CONF["SELF_SSH_HOST"], fix_ssh)),
              q("(%s)" % fix_tools)) +
           "mkdir -p %s && daqsystemtest_integtest_bundle.sh %s --concise-output --tmpdir %s"
           % (q(tmpdir), " ".join(opts), q(tmpdir)))
    jid, rc, text = run_job(req, "integtest", cmd, exclusive=True)
    req.setdefault("warnings", []).extend(markers(text, "warning"))
    totals, failing = parse_integtest(text)
    detail = ", ".join("%d %s" % (n, k) for k, n in sorted(totals.items()))
    status = "ok"
    if rc != 0 and not totals:
        status = "failed"
    elif not totals:
        status, detail = "failed", "no pytest summary in the log"
    elif failing or totals.get("failed") or totals.get("error"):
        status = "failed"
        detail += "; failing: " + ", ".join(failing)
    elif totals.get("skipped") and not totals.get("passed"):
        req.setdefault("warnings", []).append("every integtest was skipped (resource checks?)")
    return step_result("integtest", jid, rc, text, status=status, detail=detail,
                       output_dir=host_path(tmpdir))


def step_recipe_save(req, a):
    cmd = "dbtx-save-workarea-recipe.py %s%s" % ("--require-valid-refs " if a.get("require_valid_refs") else "",
                                                 q(a["name"]))
    jid, rc, text = run_job(req, "recipe-save", cmd)
    return step_result("recipe-save", jid, rc, text, output_tail=tail(after_begin(text), 10))


def step_exec(req, a):
    jid, rc, text = run_job(req, "exec", a["cmd"])
    return step_result("exec", jid, rc, text, detail="exit %s" % rc, output_tail=tail(after_begin(text)))


def step_info(req, a):
    cmd = ("dbt-info release; dbt-info sourcecode; for r in sourcecode/*/ pythoncode/*/; do "
           "[ -d \"$r.git\" ] && echo \"== $r\" && git -C \"$r\" status -sb; done; true")
    jid, rc, text = run_job(req, "info", cmd)
    return step_result("info", jid, rc, text, output_tail=tail(after_begin(text), 80, 6000))


STEPS = {
    "create": step_create, "recipe-create": step_recipe_create, "clone": step_clone,
    "checkout": step_checkout, "branch": step_branch, "commit": step_commit, "build": step_build,
    "unittest": step_unittest, "lint": step_lint, "format": step_format, "integtest": step_integtest,
    "recipe-save": step_recipe_save, "exec": step_exec, "info": step_info,
}


def run_request(rid):
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    req = load_req(rid)
    req.update(runner=ident(), status="running", started=now())
    req.setdefault("results", [])
    save_req(req)
    try:
        for name, args in req["plan"]:
            if os.path.exists(req_path(rid, "cancel")):
                raise Cancelled()
            try:
                res, failure = STEPS[name](req, args)
            except Cancelled:
                raise
            except Exception as e:  # report, don't die silently
                res, failure = {"step": name, "status": "failed"}, {
                    "class": "internal", "first_error": "daq-jobd: %r" % e,
                    "suggestion": "This is a daq-jobd bug; tell the user."}
            req["results"].append(res)
            save_req(req)
            if res["status"] != "ok":
                req["status"], req["failure"] = "failed", failure
                break
        else:
            req["status"] = "ok"
    except Cancelled:
        req["status"] = "cancelled"
    finally:
        release_all()
        done = len(req["results"])
        for name, _ in req["plan"][done:]:
            req["results"].append({"step": name, "status": "cancelled" if req["status"] == "cancelled"
                                   and len(req["results"]) == done else "skipped"})
        if req["status"] == "running":
            req["status"] = "failed"
        if not req.get("release") and is_area(req["area"]):
            req["release"] = release_of(req["area"])
        req.pop("current_job", None)
        req["finished"] = now()
        save_req(req)


# ── validation and submission ─────────────────────────────────────────────────

def parse_repo_refs(v, what):
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not v:
        raise Invalid("%s takes a list of repo@ref" % what)
    out = []
    for item in v:
        repo, _, ref = str(item).partition("@")
        ref = ref or "develop"
        if not NAME_RE.match(repo) or not REF_RE.match(ref):
            raise Invalid("bad repo@ref '%s'" % item)
        out.append((repo, ref))
    return out


def safe_rel(p, what):
    if not isinstance(p, str) or not p or p[0] in "/-" or ".." in p.split("/"):
        raise Invalid("%s must be a relative path, not starting with - and without '..': %r" % (what, p))
    return p


def normalize_steps(steps):
    """Accept [{"build": {...}}, "info", {"unittest": "pkg"}] and return [(name, args)]."""
    if not isinstance(steps, list) or not steps:
        raise Invalid("steps must be a non-empty list")
    out = []
    for s in steps:
        if isinstance(s, str):
            name, val = s, None
        elif isinstance(s, dict) and len(s) == 1:
            name, val = next(iter(s.items()))
        elif isinstance(s, dict) and "step" in s:
            name, val = s["step"], {k: v for k, v in s.items() if k != "step"}
        else:
            raise Invalid("each step is a one-key object like {\"build\": {...}}: %r" % (s,))
        if name not in STEPS:
            raise Invalid("unknown step '%s' (known: %s)" % (name, ", ".join(STEPS)))
        out.append((name, val))
    return out


def check_args(name, val, area, repos):
    a = val if isinstance(val, dict) else {}
    if name == "create":
        if not isinstance(val, dict) or not NAME_RE.match(str(a.get("release", ""))):
            raise Invalid("create needs {release: <name or alias like last_fddaq>}")
        if a.get("type", "nightly") not in ("nightly", "candidate", "stable"):
            raise Invalid("create type is nightly, candidate or stable")
    elif name == "recipe-create":
        if isinstance(val, str):
            a = {"recipe": val}
        recipe = os.path.normpath(to_container_path(str(a.get("recipe", ""))))
        if not recipe.startswith(WORK + "/") or not os.path.isfile(recipe):
            raise Invalid("recipe must be an existing file under work/ (%s)" % a.get("recipe"))
        a = dict(a, recipe=recipe)
    elif name in ("clone", "checkout"):
        refs = parse_repo_refs(val, name)
        for repo, _ in refs:
            if MIRROR and not os.path.isdir(os.path.join(MIRROR, repo + ".git")):
                raise Invalid("%s is not in the mirror %s (firmware repos are skipped; a new GitHub "
                              "repo appears within the hour)" % (repo, MIRROR))
            if name == "clone" and repo in repos:
                raise Invalid("%s is already cloned in %s" % (repo, area))
            if name == "checkout" and repo not in repos:
                raise Invalid("%s is not cloned in %s" % (repo, area))
            repos.add(repo)
        a = {"repos": refs}
    elif name in ("branch", "commit"):
        if a.get("repo") not in repos:
            raise Invalid("%s: repo %r is not cloned in %s" % (name, a.get("repo"), area))
        if name == "branch" and (not REF_RE.match(str(a.get("name", ""))) or
                                 (a.get("from") and not REF_RE.match(str(a["from"])))):
            raise Invalid("branch needs {repo, name, from?} with plain ref names")
        if name == "commit":
            if not str(a.get("message", "")).strip():
                raise Invalid("commit needs a message")
            for p in a.get("paths") or []:
                safe_rel(p, "commit path")
    elif name == "build":
        if a.get("jobs") is not None and not (isinstance(a["jobs"], int) and 1 <= a["jobs"] <= 64):
            raise Invalid("build jobs is an integer 1-64")
        if a.get("optimize") and a["optimize"] not in ("O0", "O1", "O2", "O3", "Os", "Og"):
            raise Invalid("build optimize is one of O0 O1 O2 O3 Os Og")
        if a.get("sanitize") and not NAME_RE.match(str(a["sanitize"])):
            raise Invalid("bad sanitize value")
    elif name == "unittest":
        pkg = val if isinstance(val, str) else a.get("package", "all")
        if not NAME_RE.match(str(pkg)):
            raise Invalid("unittest takes a package name or 'all'")
        a = {"package": pkg}
    elif name == "lint":
        target = val if isinstance(val, str) else a.get("target", "all")
        a = {"target": safe_rel(target, "lint target")}
    elif name == "format":
        path = val if isinstance(val, str) else a.get("path", "")
        if path.split("/")[0] not in repos:
            raise Invalid("format path must start with a repo cloned in %s: %r" % (area, path))
        a = {"path": safe_rel(path, "format path")}
    elif name == "integtest":
        for k in ("repos", "include", "exclude", "suite", "pytest_options", "tmpdir"):
            if k in a and not isinstance(a[k], str):
                raise Invalid("integtest %s is a string" % k)
        t = a.get("tmpdir")
        if t and (not t.startswith("/") or ".." in t.split("/") or not re.match(r"^[A-Za-z0-9._/-]+$", t)):
            raise Invalid("integtest tmpdir must be an absolute path without '..': %r" % t)
    elif name == "recipe-save":
        if isinstance(val, str):
            a = {"name": val}
        if not NAME_RE.match(str(a.get("name", ""))):
            raise Invalid("recipe-save needs {name}")
    elif name == "exec":
        cmd = val if isinstance(val, str) else a.get("cmd", "")
        if not str(cmd).strip():
            raise Invalid("exec needs {cmd}")
        a = {"cmd": cmd}
    return a


def validate(area, steps):
    if not isinstance(area, str) or not NAME_RE.match(area):
        raise Invalid("area must match [A-Za-z0-9._-]+")
    plan = normalize_steps(steps)
    exists = is_area(area)
    repos = {r.split("/", 1)[1] for r in repos_of(area)} if exists else set()
    out = []
    for i, (name, val) in enumerate(plan):
        if name in ("create", "recipe-create"):
            if i != 0:
                raise Invalid("%s must be the first step" % name)
            if os.path.exists(os.path.join(WORK, area)):
                raise Invalid("work/%s already exists (deleting it needs the user)" % area)
            exists = True
        elif not exists:
            raise Invalid("work/%s is not a work area; start with a create step" % area)
        out.append((name, check_args(name, val, area, repos)))
    return out


def ack_queue(area, exclusive):
    if exclusive and EXCLUSIVE_ALL:
        busy = any(os.path.isdir(os.path.join(LOCKS, "slot-%d" % i)) for i in range(1, MAX_SLOTS + 1))
        return "waiting for the whole machine (integtest)" if busy else "running"
    if exclusive and os.path.isdir(os.path.join(LOCKS, "integtest")):
        return "waiting for another integtest"
    if os.path.isdir(os.path.join(LOCKS, "area-" + area)):
        return "waiting for area lock"
    if all(os.path.isdir(os.path.join(LOCKS, "slot-%d" % i)) for i in range(1, MAX_SLOTS + 1)):
        return "waiting for a build slot"
    return "running"


def submit(area, steps, requester="", notes="", rid=""):
    for d in (WORK, JOBS, LOCKS, REQS):
        os.makedirs(d, exist_ok=True)
    try:
        if rid and not NAME_RE.match(rid):
            raise Invalid("id must match [A-Za-z0-9._-]+")
        plan = validate(area, steps)
    except Invalid as e:
        return {"id": rid or None, "status": "rejected", "reason": str(e)}
    base = rid or "r%s" % time.strftime("%Y%m%d-%H%M%S")
    rid, n = base, 1
    while True:
        try:
            fd = os.open(req_path(rid), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            break
        except FileExistsError:
            n += 1
            rid = "%s-%d" % (base, n)
    req = {"id": rid, "from": requester, "area": area, "notes": notes, "steps": steps,
           "plan": plan, "status": "queued", "created": now(), "created_ts": time.time(),
           "host_path": host_path(os.path.join(WORK, area)), "results": [], "warnings": []}
    save_req(req)
    with open(req_path(rid, "runner.log"), "a") as err:
        subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "run-request", rid],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=err, cwd=WORK,
                         start_new_session=True, close_fds=True)
    ack = {"id": rid, "status": "accepted", **host_field(),
           "queue": ack_queue(area, any(n == "integtest" for n, _ in plan)),
           "host_path": req["host_path"]}
    if rid != base:
        ack["note"] = "id %s was taken; using %s" % (base, rid)
    return ack


def host_field():
    return {"host": CONF["HOST_LABEL"]} if CONF["HOST_LABEL"] else {}


def result(rid, wait_s=0):
    deadline = time.time() + min(max(float(wait_s or 0), 0), 600)
    while True:
        try:
            req = load_req(rid)
        except (OSError, ValueError):
            return {"id": rid, "status": "unknown", "reason": "no such request"}
        st = req_status(req)
        if st in TERMINAL or time.time() >= deadline:
            break
        time.sleep(2)
    out = {"id": rid, "status": st, **host_field(), "area": req["area"], "release": req.get("release", ""),
           "host_path": req["host_path"], "steps": list(req["results"])}
    pending = req["plan"][len(req["results"]):]
    if st in ("queued", "running"):
        jid = req.get("current_job")
        if jid:
            js = job_state(jid)
            cur = {"step": pending[0][0] if pending else "?", "status": js,
                   "log": host_path(os.path.join(JOBS, jid, "log"))}
            if js == "queued":
                cur["queue_position"] = queue_position(jid)
            out["steps"].append(cur)
            pending = pending[1:]
        out["steps"] += [{"step": n, "status": "pending"} for n, _ in pending]
    if req.get("failure"):
        out["failure"] = req["failure"]
    if req.get("warnings"):
        out["warnings"] = req["warnings"]
    for k in ("from", "notes", "created", "finished"):
        if req.get(k):
            out[k] = req[k]
    if st == "lost":
        out["reason"] = "the runner died (container restart?); resubmit the remaining steps"
    return out


def cancel(rid):
    try:
        req = load_req(rid)
    except (OSError, ValueError):
        return {"id": rid, "status": "unknown", "reason": "no such request"}
    st = req_status(req)
    if st in TERMINAL:
        return {"id": rid, "status": st, "reason": "already finished"}
    write(req_path(rid, "cancel"), now())
    runner = load_req(rid).get("runner", "")  # the runner may have started meanwhile
    if runner and ident_alive(runner):
        os.kill(int(runner.split()[0]), signal.SIGTERM)
        return {"id": rid, "status": "cancelling"}
    return {"id": rid, "status": "cancelling", "note": "runner not started yet; it will stop at once"}


def list_requests(limit=15):
    out = []
    for rid in request_ids():
        try:
            req = load_req(rid)
        except (OSError, ValueError):
            continue
        st = req_status(req)
        done = len(req.get("results", []))
        plan = req.get("plan", [])
        out.append({"id": rid, "status": st, "area": req["area"], "from": req.get("from", ""),
                    "created": req.get("created", ""), "_ts": req.get("created_ts", 0),
                    "steps": "%d/%d %s" % (done, len(plan), plan[done][0] if st in ("queued", "running")
                                           and done < len(plan) else "")})
    out.sort(key=lambda r: r.pop("_ts"))
    return out[-limit:]


def list_jobs(limit=15):
    out = []
    for j in job_ids()[-limit:]:
        d = os.path.join(JOBS, j)
        out.append({"id": j, "state": job_state(j) if read(os.path.join(d, "ctr_runner")) else
                    read(os.path.join(d, "state")) + " (host job)",
                    "area": read(os.path.join(d, "area")), "requester": read(os.path.join(d, "requester")),
                    "label": read(os.path.join(d, "label"))})
    return out


# ── read-only tools ───────────────────────────────────────────────────────────

def sh(cmd, timeout):
    try:
        p = subprocess.run(["bash", "-lc", cmd], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=timeout, cwd=WORK)
        return p.returncode, strip_ansi(p.stdout.decode(errors="replace"))
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ds" % timeout


def list_areas():
    out = []
    for name in sorted(os.listdir(WORK)):
        if is_area(name):
            out.append({"area": name, "release": release_of(name), "repos": repos_of(name),
                        "host_path": host_path(os.path.join(WORK, name))})
    return out


def list_releases(typ="nightly"):
    if typ not in ("nightly", "candidate", "stable"):
        return {"error": "type is nightly, candidate or stable"}
    rc, text = sh("{ %s ; } >/dev/null 2>&1 && echo %s && dbt-create -l %s"
                  % (SETUP_ROOT, BEGIN, "" if typ == "stable" else "-b " + typ), 300)
    return {"type": typ, "exit": rc, "output": tail(after_begin(text), 60)}


def env_checks():
    rc_c, _ = sh("timeout 15 ls /cvmfs/dunedaq.opensciencegrid.org/setup_dunedaq.sh "
                 "/cvmfs/dunedaq-development.opensciencegrid.org/nightly >/dev/null", 20)
    rc_s, _ = sh("ssh -o BatchMode=yes -o ConnectTimeout=5 %s true" % CONF["SELF_SSH_HOST"], 15)
    return {"cvmfs": "readable" if rc_c == 0 else "NOT readable",
            "sshd": "ok" if rc_s == 0 else "NOT reachable (integtests will fail)",
            "integtest_tools": "ok" if shutil.which("ps") and shutil.which("jq") else "ps/jq missing (harmless)"}


def stack_status():
    active = [r for r in list_requests(200) if r["status"] in ("queued", "running")]
    return {**env_checks(),
            "active_requests": active,
            "running_jobs": running_jobs(),
            **host_field(),
            "note": ("Native host: nothing to manage; report CVMFS or ssh problems to the user." if NATIVE else
                     "Machine and container lifecycle is host-side: bin/daq-stack status|ensure|restart-if-idle.")}


# ── monitor snapshot (bin/daq-jobd top) ───────────────────────────────────────

def ts_of(stamp):
    try:
        return time.mktime(time.strptime(stamp, "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, TypeError):
        return 0


def lock_owner(name):
    d = os.path.join(LOCKS, name)
    return read(os.path.join(d, "owner"), "?") if os.path.isdir(d) else None


def file_tail(path, n, size=16384):
    """Last n non-blank lines, ANSI stripped; a \\r-redrawn progress line keeps its last state."""
    try:
        with open(path, "rb") as f:
            f.seek(max(0, os.fstat(f.fileno()).st_size - size))
            text = f.read().decode(errors="replace")
    except OSError:
        return []
    lines = [strip_ansi(ln).rstrip("\r").split("\r")[-1].rstrip() for ln in text.split("\n")]
    return [ln for ln in lines if ln.strip()][-n:]


def cached_env_checks(max_age=60):
    """env_checks() takes seconds (ssh, CVMFS), so a monitor polling every few seconds
    shares one result per minute through a cache file."""
    path = os.path.join(JOBS, ".monitor-health.json")
    try:
        if time.time() - os.stat(path).st_mtime < max_age:
            with open(path) as f:
                return json.load(f)
    except (OSError, ValueError):
        pass
    out = env_checks()
    try:
        write(path, json.dumps(out))
    except OSError:
        pass
    return out


def mirror_state():
    log = os.path.join(MIRROR, "update.log")
    try:
        age = time.time() - os.stat(log).st_mtime
    except OSError:
        return {"summary": "no update.log yet"}
    last = next((ln for ln in reversed(file_tail(log, 20)) if ln.startswith(("updated ", "warning"))), "")
    return {"age_s": int(age), "summary": last}


def snapshot(hours=24, log_lines=4, recent_max=10):
    """Everything bin/daq-jobd top shows for this host, in one read-only call."""
    t = time.time()
    active, recent, current = [], [], set()
    for rid in request_ids():
        try:
            req = load_req(rid)
        except (OSError, ValueError):
            continue
        st = req_status(req)
        plan, res = req.get("plan", []), req.get("results", [])
        created = req.get("created_ts", 0)
        if st in ("queued", "running"):
            step = plan[len(res)][0] if len(res) < len(plan) else ""
            item = {"id": rid, "status": st, "area": req["area"], "from": req.get("from", ""),
                    "elapsed_s": int(t - created), "step": step, "step_no": len(res) + 1,
                    "steps": len(plan), "done": [[r["step"], r["status"]] for r in res]}
            jid = req.get("current_job")
            if jid:
                current.add(jid)
                d = os.path.join(JOBS, jid)
                item["job"] = jid
                item["job_state"] = js = job_state(jid)
                if js == "running":
                    item["step_elapsed_s"] = int(t - ts_of(read(os.path.join(d, "started"))))
                    item["log_tail"] = file_tail(os.path.join(d, "log"), log_lines)
                elif js == "queued":
                    why = ack_queue(req["area"], step == "integtest")
                    item["waiting"] = "starting" if why == "running" else why
            else:
                item["waiting"] = "runner starting"
            active.append((created, item))
        elif t - created < hours * 3600:
            fin = ts_of(req.get("finished"))
            item = {"id": rid, "status": st, "area": req["area"], "from": req.get("from", ""),
                    "finished": req.get("finished", ""), "duration_s": int(fin - created) if fin else None,
                    "steps": [[r["step"], r["status"], r.get("duration", "")] for r in res]}
            if req.get("failure"):
                item["first_error"] = req["failure"].get("first_error", "")
            recent.append((created, item))
    host_jobs = []
    for j in job_ids()[-100:]:
        d = os.path.join(JOBS, j)
        if j in current or read(os.path.join(d, "ctr_runner")):
            continue
        s = read(os.path.join(d, "state"))
        if s in ("queued", "running"):
            host_jobs.append({"id": j, "state": s, "area": read(os.path.join(d, "area")),
                              "label": read(os.path.join(d, "label")),
                              "requester": read(os.path.join(d, "requester"))})
    out = {"host": CONF["HOST_LABEL"] or "container", "mode": CONF["MODE"], "version": VERSION,
           "time": now(), "max_slots": MAX_SLOTS,
           "slots": [lock_owner("slot-%d" % i) for i in range(1, MAX_SLOTS + 1)],
           "integtest_exclusive": CONF["INTEGTEST_EXCLUSIVE"],
           "active": [i for _, i in sorted(active, key=lambda p: p[0])],
           "host_jobs": host_jobs,
           "recent": [i for _, i in sorted(recent, key=lambda p: p[0])][-recent_max:][::-1],
           "health": cached_env_checks()}
    if not EXCLUSIVE_ALL:
        out["integtest_lock"] = lock_owner("integtest")
    if MIRROR:
        out["health"]["mirror"] = mirror_state()
    return out


def job_log(job="", request="", step=None, lines=60):
    lines = max(1, min(int(lines or 60), 400))
    if not job and request:
        try:
            req = load_req(request)
        except (OSError, ValueError):
            return {"error": "no such request"}
        logs = [r.get("log") for r in req.get("results", []) if r.get("log")]
        if req.get("current_job"):
            logs.append(host_path(os.path.join(JOBS, req["current_job"], "log")))
        if not logs:
            return {"error": "request has no job logs yet"}
        idx = -1 if step is None else int(step)
        try:
            job = os.path.basename(os.path.dirname(to_container_path(logs[idx])))
        except IndexError:
            return {"error": "no step %s" % step}
    if not job or not os.path.isdir(os.path.join(JOBS, job)):
        return {"error": "no such job"}
    path = os.path.join(JOBS, job, "log")
    with open(path, errors="replace") as f:
        text = strip_ansi(f.read())
    return {"job": job, "log": host_path(path), "tail": "\n".join(text.splitlines()[-lines:])}


# ── MCP (JSON-RPC 2.0 over newline-delimited stdio) ───────────────────────────

STEP_DOC = """Steps run in order; the first failure stops the rest. Each is a one-key object:
  {"create": {"release": "last_fddaq"|"NFD_DEV_...", "type": "nightly|candidate|stable", "quick": false}}
  {"recipe-create": {"recipe": "<path under work/>", "build": false}}
  {"clone": ["repo@ref", ...]}      ref = branch, tag or commit; default develop
  {"checkout": ["repo@ref", ...]}   refuses a dirty tree
  {"branch": {"repo": R, "name": N, "from": REF}}   local only
  {"commit": {"repo": R, "message": M, "paths": [...]}}   local only, never pushed
  {"build": {"clean": false, "jobs": 6, "optimize": "O2", "sanitize": "address", "debug": false}}
  {"unittest": "<package>|all"}   {"lint": "<package>|<repo/path/file>|all"}
  {"format": "<repo>[/path]"}     rewrites files in place with dbt-clang-format.sh
  {"integtest": {"repos": "daqsystemtest", "include": "pattern", "exclude": "pattern", "suite": "core", "pytest_options": "...", "tmpdir": "/tmp"}}
     waits until the whole machine is free; tmpdir (default: the area's integtest_tmp/) is the root for
     pytest output, e.g. /tmp for tests that size themselves from its free space as on CI
  {"recipe-save": {"name": N, "require_valid_refs": false}}
  {"exec": "<shell command run in the area's environment>"}
  "info"   release, local repos and git status
create/recipe-create must come first and need a new area; other steps need an existing one."""

TOOLS = [
    {"name": "daq_submit",
     "description": "Submit a DUNE DAQ request (create a work area, clone, build, test, ...) to the "
                    "job service. Returns an ack at once; the steps run in the background. Get the "
                    "outcome with daq_result.\n" + STEP_DOC,
     "inputSchema": {"type": "object", "required": ["area", "steps"], "properties": {
         "area": {"type": "string", "description": "work area name under work/, [A-Za-z0-9._-]+"},
         "steps": {"type": "array", "items": {"type": ["object", "string"]}},
         "from": {"type": "string", "description": "who is asking (your session name), for listings"},
         "id": {"type": "string", "description": "optional request id; one is generated if omitted"},
         "notes": {"type": "string"}}}},
    {"name": "daq_result",
     "description": "Status or result of a request: per-step status, durations, log paths (host paths), "
                    "and on failure a class, first error and suggestion. wait_s (max 600) blocks until "
                    "the request finishes or the time runs out. For long jobs, prefer running "
                    "`bin/daq-jobd wait <id>` in the background and calling this once it returns.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {
         "id": {"type": "string"}, "wait_s": {"type": "number"}}}},
    {"name": "daq_cancel", "description": "Cancel a queued or running request.",
     "inputSchema": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}},
    {"name": "daq_list", "description": "Recent requests and jobs (including jobs started by the host-side broker).",
     "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}}},
    {"name": "daq_log",
     "description": "Tail of a job log, ANSI codes stripped. Give a job id, or a request id plus an "
                    "optional step index (default: the latest step). For big logs, read the host path "
                    "or use the daq-log-triage agent instead.",
     "inputSchema": {"type": "object", "properties": {
         "job": {"type": "string"}, "request": {"type": "string"},
         "step": {"type": "integer"}, "lines": {"type": "integer", "description": "default 60, max 400"}}}},
    {"name": "daq_areas", "description": "Work areas with their release and local repos.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "daq_releases", "description": "Releases dbt-create can use (takes a few seconds).",
     "inputSchema": {"type": "object", "properties": {
         "type": {"type": "string", "enum": ["nightly", "candidate", "stable"]}}}},
    {"name": "daq_status", "description": "Health seen from inside the container (CVMFS, loopback sshd) and active work.",
     "inputSchema": {"type": "object", "properties": {}}},
]

INSTRUCTIONS = """DUNE DAQ job service (daq-jobd), running inside the dunedaq-app container.
- Edit code on the host at the area's host_path (same files as /work/<area> in the container).
- Nothing is ever pushed; git pushes are blocked in the container. Publishing is the user's call.
- One job per area, two in total, integtests alone: requests queue rather than fail.
- For long requests run `{C}/bin/daq-jobd wait <id>` in the background instead of polling.
- On a failed step, hand the log path to the daq-log-triage agent rather than reading big logs.
- No broker session looks after the stack; you do, on the host:
  container down or tools failing: `{C}/bin/daq-stack status`, then `{C}/bin/daq-stack ensure`;
  failure class cvmfs-io: `{C}/bin/daq-stack restart-if-idle` (refuses while jobs run: tell the
  user and wait; never cancel others' work to force it), then resubmit.
  Never run restart.sh or podman-compose down. Deleting or recreating a work area: ask the user.
  The /dunedaq:dunedaq-env-client skill has the full list."""


NATIVE_INSTRUCTIONS = """DUNE DAQ job service (daq-jobd), running natively on {H} (no container).
- Paths in results are on {H}: read logs with daq_log, not from your own disk.
- Code reaches {H} through git only. Commit in your own work area, push the branch to the
  `daqfnal` remote (add it with `bin/daq-jobd remote-add <area or repo dir>` in dunedaq-container),
  then request clone/checkout of that branch or commit. Pushing to daqfnal needs no approval;
  pushing anywhere else does. Branches you pushed win over GitHub branches of the same name.
- GitHub branches and tags come from a mirror updated hourly. A repo missing from it is
  either a *-firmware repo (skipped) or new on GitHub.
- One job per area, {S} in total, one integtest at a time (other jobs run alongside it):
  requests queue rather than fail. It is a
  shared machine: don't raise -j above the default without a reason.
- Nothing is pushed from {H}. Deleting or recreating a work area: ask the user.
- Wait for long requests in the background with
  `ssh {H} daq-jobd/dunedaq-container/bin/daq-jobd --conf daq-jobd/daq-jobd.conf wait <id>`.
- On a failed step, hand the log (via daq_log) to the daq-log-triage agent rather than reading big logs."""


def instructions():
    if NATIVE:
        return NATIVE_INSTRUCTIONS.replace("{H}", CONF["HOST_LABEL"] or "this host").replace("{S}", str(MAX_SLOTS))
    # The container repo is the parent of the host directory mounted at /work.
    return INSTRUCTIONS.replace("{C}", os.path.dirname(HOST_WORK) if HOST_WORK else "<dunedaq-container>")


def call_tool(name, a):
    if name == "daq_submit":
        return submit(a.get("area"), a.get("steps"), a.get("from", ""), a.get("notes", ""), a.get("id", ""))
    if name == "daq_result":
        return result(a["id"], a.get("wait_s", 0))
    if name == "daq_cancel":
        return cancel(a["id"])
    if name == "daq_list":
        n = int(a.get("limit") or 15)
        return {"requests": list_requests(n), "jobs": list_jobs(n)}
    if name == "daq_log":
        return job_log(a.get("job", ""), a.get("request", ""), a.get("step"), a.get("lines", 60))
    if name == "daq_areas":
        return list_areas()
    if name == "daq_releases":
        return list_releases(a.get("type", "nightly"))
    if name == "daq_status":
        return stack_status()
    raise KeyError(name)


PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")


def handle(msg):
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:
        return None  # notification
    if method == "initialize":
        want = params.get("protocolVersion")
        res = {"protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
               "capabilities": {"tools": {}},
               "serverInfo": {"name": "daq-jobd", "version": VERSION},
               "instructions": instructions()}
    elif method == "ping":
        res = {}
    elif method == "tools/list":
        res = {"tools": TOOLS}
    elif method == "tools/call":
        name = params.get("name")
        if name not in {t["name"] for t in TOOLS}:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": "unknown tool %s" % name}}
        try:
            out = call_tool(name, params.get("arguments") or {})
            err = isinstance(out, dict) and (out.get("status") == "rejected" or "error" in out)
            res = {"content": [{"type": "text", "text": json.dumps(out, indent=1)}], "isError": bool(err)}
        except Exception as e:
            res = {"content": [{"type": "text", "text": "daq-jobd error: %r" % e}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found: %s" % method}}
    return {"jsonrpc": "2.0", "id": mid, "result": res}


def reap_children():
    """Collect runners that have exited, so they don't linger as zombies of this server."""
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def serve():
    for d in (WORK, JOBS, LOCKS, REQS):
        os.makedirs(d, exist_ok=True)
    for raw in sys.stdin.buffer:
        reap_children()
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        else:
            try:
                reply = handle(msg) if isinstance(msg, dict) else None
            except Exception as e:
                reply = {"jsonrpc": "2.0", "id": msg.get("id"),
                         "error": {"code": -32603, "message": "internal error: %r" % e}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "mcp":
        serve()
    elif cmd == "run-request" and len(argv) == 3:
        run_request(argv[2])
    elif cmd == "submit" and len(argv) == 3:
        a = json.loads(argv[2])
        print(json.dumps(submit(a.get("area"), a.get("steps"), a.get("from", "cli"), a.get("notes", ""),
                                a.get("id", "")), indent=1))
    elif cmd == "result" and len(argv) in (3, 4):
        print(json.dumps(result(argv[2], float(argv[3]) if len(argv) == 4 else 0), indent=1))
    elif cmd == "cancel" and len(argv) == 3:
        print(json.dumps(cancel(argv[2]), indent=1))
    elif cmd == "wait" and len(argv) in (3, 4):
        every = float(argv[3]) if len(argv) == 4 else 15
        while True:
            try:
                st = req_status(load_req(argv[2]))
            except (OSError, ValueError):
                print("no request %s" % argv[2], file=sys.stderr)
                return 2
            if st in TERMINAL:
                print("REQUEST-ENDED %s %s" % (st, argv[2]))
                return 0
            time.sleep(every)
    elif cmd == "list":
        print(json.dumps({"requests": list_requests(), "jobs": list_jobs()}, indent=1))
    elif cmd == "snapshot" and len(argv) in (2, 3, 4):
        print(json.dumps(snapshot(float(argv[2]) if len(argv) > 2 else 24,
                                  int(argv[3]) if len(argv) > 3 else 4)))
    else:
        sys.stderr.write(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
