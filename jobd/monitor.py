#!/usr/bin/env python3
"""Read-only monitor for daq-jobd instances: bin/daq-jobd top.

Polls `daq-jobd snapshot` on each host (the local container through bin/daq-jobd,
the native host over ssh) and redraws one screen with active and queued requests,
the running step's log tail, slots and locks, health, and recent history.

  monitor.py [--interval S] [--hosts local,NAME,...] [--lines N] [--hours H] [--once] [--json]

--once prints one frame and exits; --json prints the raw snapshots instead (the data
a web page would use). Native hosts come from jobd/native_hosts (default: all of them);
ssh needs a Kerberos ticket and reuses one connection between polls. Runs on the Mac with
any Python 3.9+, standard library only.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SSH = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ControlMaster=auto",
       "-o", "ControlPath=~/.ssh/cm-daqjobd-%C", "-o", "ControlPersist=120"]

RESET, BOLD, DIM = "\033[0m", "\033[1m", "\033[2m"
COLOR = {"ok": "\033[32m", "running": "\033[36m", "queued": "\033[33m", "failed": "\033[31m",
         "lost": "\033[35m", "cancelled": "\033[2m", "skipped": "\033[2m", "error": "\033[31m"}
MARK = {"ok": "✓", "failed": "✗", "cancelled": "-", "skipped": "·", "lost": "?"}


def native_hosts():
    """NAME -> (SSH_HOST, CHECKOUT, CONF) from jobd/native_hosts."""
    out = {}
    with open(os.path.join(ROOT, "jobd", "native_hosts")) as f:
        for line in f:
            fields = line.split()
            if len(fields) == 6 and not fields[0].startswith("#"):
                out[fields[0]] = (fields[1], fields[3], fields[4])
    return out


HOSTS = native_hosts()


def command(host, hours, lines):
    args = ["snapshot", str(hours), str(lines)]
    if host == "local":
        return [os.path.join(ROOT, "bin", "daq-jobd")] + args
    ssh_host, checkout, conf = HOSTS[host]
    return SSH + [ssh_host, " ".join([checkout + "/bin/daq-jobd", "--conf", conf] + args)]


def label(host):
    return HOSTS[host][0] if host in HOSTS else "container"


def fetch(host, hours, lines):
    t0 = time.time()
    try:
        p = subprocess.run(command(host, hours, lines), stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    except subprocess.TimeoutExpired:
        return {"host": label(host), "error": "no answer in 60 s"}
    if p.returncode != 0:
        err = p.stderr.decode(errors="replace").strip().splitlines()
        if p.returncode == 2 and not err:
            return {"host": label(host), "error": "no snapshot command there; update its daq-jobd checkout"}
        return {"host": label(host), "error": (err[-1] if err else "exit code %d" % p.returncode)}
    try:
        snap = json.loads(p.stdout)
    except ValueError:
        return {"host": label(host), "error": "unreadable snapshot (old daq-jobd there?)"}
    snap["fetch_s"] = time.time() - t0
    return snap


def fetch_all(hosts, hours, lines):
    out = [None] * len(hosts)

    def one(i):
        out[i] = fetch(hosts[i], hours, lines)

    threads = [threading.Thread(target=one, args=(i,), daemon=True) for i in range(len(hosts))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


def secs(s):
    if s is None:
        return ""
    s = int(s)
    if s >= 3600:
        return "%dh%02dm" % (s // 3600, s % 3600 // 60)
    return "%dm%02ds" % (s // 60, s % 60) if s >= 60 else "%ds" % s


class Screen:
    def __init__(self, color, width):
        self.color, self.width, self.lines = color, width, []

    def paint(self, text, style):
        return style + text + RESET if self.color and style else text

    def add(self, *parts):
        """parts: (text, style) pairs; the line is cut to the terminal width."""
        room, out = self.width, []
        for text, style in parts:
            if room <= 0:
                break
            text = text[:room]
            room -= len(text)
            out.append(self.paint(text, style))
        self.lines.append("".join(out))


def short_job(jid):
    return jid.split("-r", 1)[-1] if jid else "free"


def health_line(snap):
    h = snap.get("health", {})
    parts = []
    for key in ("cvmfs", "sshd"):
        v = h.get(key, "?")
        parts.append(("%s %s" % (key, "ok" if v in ("ok", "readable") else v), "" if v in ("ok", "readable") else "error"))
    if "ps/jq" in h.get("integtest_tools", ""):
        parts.append(("ps/jq missing", "queued"))
    m = h.get("mirror")
    if m:
        age = m.get("age_s")
        stale = age is None or age > 2 * 3600 or "failed 0" not in m.get("summary", "failed 0")
        parts.append(("mirror %s ago (%s)" % (secs(age), m.get("summary", "")) if age is not None
                      else "mirror: " + m.get("summary", ""), "error" if stale else ""))
    return parts


def render_host(sc, snap, hours):
    if "error" in snap:
        sc.add(("━━ %s  " % snap["host"], BOLD), ("UNREACHABLE: %s" % snap["error"], COLOR["error"]))
        return
    used = [s for s in snap["slots"] if s]
    head = "━━ %s  slots %d/%d" % (snap["host"], len(used), snap["max_slots"])
    if "integtest_lock" in snap:
        head += "  integtest %s" % short_job(snap["integtest_lock"]) if snap["integtest_lock"] else "  integtest free"
    sc.add((head, BOLD), ("   (%s, %.1fs)" % (snap["time"][11:], snap.get("fetch_s", 0)), DIM))
    parts = [("   ", "")]
    for text, style in health_line(snap):
        parts += [(text, COLOR.get(style, "")), ("  ", "")]
    sc.add(*parts)

    if not snap["active"] and not snap["host_jobs"]:
        sc.add(("   idle", DIM))
    for a in snap["active"]:
        st = a.get("job_state") if a["status"] == "running" and a.get("job_state") else a["status"]
        done = " ".join("%s%s" % (s, MARK.get(r, "")) for s, r in a["done"])
        when = secs(a["elapsed_s"])
        if a.get("step_elapsed_s") is not None:
            when = "%s, step %s" % (when, secs(a["step_elapsed_s"]))
        sc.add((" %-8s " % st.upper(), COLOR.get(st, "")),
               ("%s  %s  " % (a["id"], a["area"]), BOLD),
               ("%s %d/%d" % (a["step"], a["step_no"], a["steps"]), COLOR.get(st, "")),
               ("  %s" % when, ""), ("  %s" % done if done else "", DIM),
               ("  from %s" % a["from"] if a.get("from") else "", DIM))
        if a.get("waiting"):
            sc.add(("          ", ""), ("⏳ " + a["waiting"], COLOR["queued"]))
        for ln in a.get("log_tail", []):
            sc.add(("          │ ", DIM), (ln, ""))
    for j in snap["host_jobs"]:
        sc.add((" %-8s " % j["state"].upper(), COLOR.get(j["state"], "")),
               ("%s  %s  (broker job, from %s)" % (j["label"], j["area"], j["requester"]), ""))

    if snap["recent"]:
        sc.add(("   finished, last %gh:" % hours, DIM))
    for r in snap["recent"]:
        steps = " ".join("%s%s" % (s, MARK.get(st, "")) for s, st, _ in r["steps"])
        sc.add(("   %-9s " % r["status"], COLOR.get(r["status"], "")),
               ("%s  %-18s  " % (r["finished"][5:16].replace("T", " ") or "-", r["id"]), ""),
               ("%-8s " % secs(r.get("duration_s")), DIM), ("%s  " % r["area"], ""), (steps, ""))
        if r.get("first_error"):
            err, room = r["first_error"], sc.width - 13
            sc.add(("             ", ""), (err if len(err) <= room else "…" + err[-(room - 1):], COLOR["failed"]))


def frame(snaps, hours, color, width):
    sc = Screen(color, width)
    sc.add(("daq-jobd monitor  %s" % time.strftime("%H:%M:%S"), BOLD),
           ("   read-only; Ctrl-C to quit", DIM))
    for snap in snaps:
        sc.add(("", ""))
        render_host(sc, snap, hours)
    return sc.lines


def main():
    ap = argparse.ArgumentParser(description="Read-only monitor for daq-jobd instances.")
    ap.add_argument("--interval", type=float, default=5, help="seconds between polls (default 5)")
    ap.add_argument("--hosts", default=",".join(["local"] + list(HOSTS)),
                    help="comma list of local and jobd/native_hosts names (default all)")
    ap.add_argument("--lines", type=int, default=4, help="log tail lines per running step (default 4)")
    ap.add_argument("--hours", type=float, default=24, help="history window (default 24)")
    ap.add_argument("--once", action="store_true", help="print one frame and exit")
    ap.add_argument("--json", action="store_true", help="print the raw snapshots and exit")
    opt = ap.parse_args()
    hosts = [h.strip() for h in opt.hosts.split(",") if h.strip()]
    if not hosts or any(h != "local" and h not in HOSTS for h in hosts):
        ap.error("--hosts takes local and names from jobd/native_hosts: %s" % ", ".join(HOSTS))

    if opt.json or opt.once:
        snaps = fetch_all(hosts, opt.hours, opt.lines)
        if opt.json:
            print(json.dumps(snaps, indent=1))
        else:
            print("\n".join(frame(snaps, opt.hours, sys.stdout.isatty(),
                                  shutil.get_terminal_size().columns)))
        return 0

    out = sys.stdout
    out.write("\033[?1049h\033[?25l")  # alternate screen, hide cursor
    try:
        while True:
            snaps = fetch_all(hosts, opt.hours, opt.lines)
            size = shutil.get_terminal_size()
            lines = frame(snaps, opt.hours, True, size.columns)[:size.lines - 1]
            out.write("\033[H" + "".join(ln + "\033[K\n" for ln in lines) + "\033[J")
            out.flush()
            time.sleep(opt.interval)
    except KeyboardInterrupt:
        return 0
    finally:
        out.write("\033[?25h\033[?1049l")
        out.flush()


if __name__ == "__main__":
    sys.exit(main())
