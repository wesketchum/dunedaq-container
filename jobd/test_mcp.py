#!/usr/bin/env python3
"""Smoke test for daq-jobd: drives `bin/daq-jobd mcp` over stdio, like a client would.

  python3 -I jobd/test_mcp.py AREA

AREA must be an existing work area; the test only runs read-only commands in it
(info, echo, sleep). Jobs go to /work/.daq-jobs-test, so the real queue and its
locks are untouched. Takes about two minutes, mostly area environment setup.
"""

import json
import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


class Client:
    def __init__(self):
        env = dict(os.environ, DAQ_JOBS_DIR="/work/.daq-jobs-test")
        self.p = subprocess.Popen([os.path.join(ROOT, "bin", "daq-jobd"), "mcp"], env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.n = 0

    def rpc(self, method, params=None):
        self.n += 1
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.n, "method": method,
                                       "params": params or {}}) + "\n")
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def notify(self, method):
        self.p.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.p.stdin.flush()

    def tool(self, name, **args):
        r = self.rpc("tools/call", {"name": name, "arguments": args})["result"]
        text = r["content"][0]["text"]
        try:
            return json.loads(text), r["isError"]
        except ValueError:
            return text, r["isError"]


def check(what, cond, info=""):
    print("%s  %s%s" % ("ok  " if cond else "FAIL", what, "" if cond else "  -> %s" % (info,)))
    if not cond:
        FAILS.append(what)


def main(area):
    c = Client()
    init = c.rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "test", "version": "0"}})
    check("initialize", init["result"]["serverInfo"]["name"] == "daq-jobd", init)
    c.notify("notifications/initialized")
    tools = [t["name"] for t in c.rpc("tools/list")["result"]["tools"]]
    check("tools/list has 8 tools", len(tools) == 8, tools)
    check("unknown method is an error", "error" in c.rpc("nope/nope"))
    check("malformed params don't kill the server", "error" in c.rpc("tools/call", [1]))

    st, _ = c.tool("daq_status")
    check("status: cvmfs readable", st.get("cvmfs") == "readable", st)
    areas, _ = c.tool("daq_areas")
    names = [a["area"] for a in areas]
    check("areas lists %s" % area, area in names, names)
    me = next(a for a in areas if a["area"] == area)
    check("area host_path is a host path", me["host_path"].startswith("/Users/"), me["host_path"])
    a_repo = me["repos"][0].split("/", 1)[1] if me["repos"] else None

    for label, args in [
        ("bad area name", {"area": "../x", "steps": ["info"]}),
        ("missing area", {"area": "no_such_area_xyz", "steps": ["info"]}),
        ("create over an existing area", {"area": area, "steps": [{"create": {"release": "last_fddaq"}}]}),
        ("unknown step", {"area": area, "steps": [{"push": "x"}]}),
        ("create not first", {"area": "no_such_area_xyz", "steps": ["info", {"create": {"release": "x"}}]}),
        ("lint path escaping the area", {"area": area, "steps": [{"lint": "../../etc/passwd"}]}),
        ("lint target that is an option", {"area": area, "steps": [{"lint": "--clean"}]}),
        ("ref that is an option", {"area": area, "steps": [{"checkout": [(a_repo or "x") + "@-f"]}]}),
    ] + ([("clone of an already cloned repo", {"area": area, "steps": [{"clone": [a_repo + "@develop"]}]})]
         if a_repo else []):
        r, err = c.tool("daq_submit", **args)
        check("reject: " + label, r.get("status") == "rejected" and err, r)

    r, _ = c.tool("daq_submit", area=area, steps=["info"], **{"from": "test_mcp"})
    check("info accepted", r.get("status") == "accepted", r)
    info, _ = c.tool("daq_result", id=r["id"], wait_s=300)
    check("info ok", info["status"] == "ok", info)
    check("info resolves the release", info.get("release", "").startswith(("N", "f", "c")), info.get("release"))
    tail = info["steps"][0].get("output_tail", "") if info.get("steps") else ""
    check("info output has the release name", "Release name" in tail, tail[:300])
    check("result log is a host path", info["steps"][0].get("log", "").startswith("/Users/"), info["steps"])

    r, _ = c.tool("daq_submit", area=area, steps=[{"exec": "echo hello-from-jobd; exit 3"}, "info"])
    res, _ = c.tool("daq_result", id=r["id"], wait_s=300)
    check("failing exec -> failed", res["status"] == "failed", res)
    check("exec output captured", "hello-from-jobd" in res["steps"][0].get("output_tail", ""), res["steps"][0])
    check("step after a failure is skipped", res["steps"][1]["status"] == "skipped", res["steps"])
    check("failure block present", "class" in res.get("failure", {}), res)

    long_r, _ = c.tool("daq_submit", area=area, steps=[{"exec": "sleep 300"}])
    queued_r, _ = c.tool("daq_submit", area=area, steps=[{"exec": "echo second"}])
    deadline = time.time() + 180
    while time.time() < deadline:
        cur, _ = c.tool("daq_result", id=long_r["id"])
        if cur["steps"] and cur["steps"][0].get("status") == "running":
            break
        time.sleep(3)
    check("sleep job running", cur["steps"] and cur["steps"][0].get("status") == "running", cur)
    second, _ = c.tool("daq_result", id=queued_r["id"])
    check("second request waits for the area lock",
          second["steps"] and second["steps"][0].get("status") == "queued", second)
    lst, _ = c.tool("daq_list", limit=5)
    check("list shows the running request", any(x["id"] == long_r["id"] and x["status"] == "running"
                                                for x in lst["requests"]), lst["requests"])
    can, _ = c.tool("daq_cancel", id=long_r["id"])
    check("cancel accepted", can.get("status") == "cancelling", can)
    cur, _ = c.tool("daq_result", id=long_r["id"], wait_s=30)
    check("cancelled", cur["status"] == "cancelled", cur)
    second, _ = c.tool("daq_result", id=queued_r["id"], wait_s=300)
    check("queued request then runs", second["status"] == "ok", second)

    # A runner killed -9 while this server (its parent) lives must read as lost, not
    # as a zombie that is "alive", and its locks must not block the next request.
    k_r, _ = c.tool("daq_submit", area=area, steps=[{"exec": "sleep 100"}])
    q_r, _ = c.tool("daq_submit", area=area, steps=[{"exec": "echo after-kill"}])
    time.sleep(5)
    runner = json.loads(subprocess.check_output(
        ["podman", "exec", "dunedaq-app", "cat", "/work/.daq-jobs-test/requests/%s.json" % k_r["id"]]))["runner"]
    subprocess.check_call(["podman", "exec", "dunedaq-app", "kill", "-9", runner.split()[0]])
    time.sleep(2)
    cur, _ = c.tool("daq_result", id=k_r["id"])
    check("runner killed -9 -> lost", cur["status"] == "lost", cur)
    cur, _ = c.tool("daq_cancel", id=q_r["id"])
    cur, _ = c.tool("daq_result", id=q_r["id"], wait_s=60)
    check("queued request cancelled before it ran", cur["status"] == "cancelled", cur)
    after, _ = c.tool("daq_submit", area=area, steps=[{"exec": "echo after-kill"}])
    cur, _ = c.tool("daq_result", id=after["id"], wait_s=200)
    check("dead runner's area lock reaped", cur["status"] == "ok", cur)
    subprocess.call(["podman", "exec", "dunedaq-app", "pkill", "-f", "sleep 100"])

    log, _ = c.tool("daq_log", request=queued_r["id"], lines=5)
    check("daq_log tail", "second" in log.get("tail", ""), log)

    c.p.stdin.close()
    c.p.wait(timeout=30)
    print("\n%d failure(s)%s" % (len(FAILS), ": " + ", ".join(FAILS) if FAILS else ""))
    return 1 if FAILS else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    sys.exit(main(sys.argv[1]))
