#!/usr/bin/env python3
"""Community polling scenarios on a running bench (bench.py up). Windows are the bench's:
direct 3 min, 1 hop 6 min, grace 1 min. Prints PASS or FAIL per check; exit 1 on any FAIL."""
import contextlib, io, subprocess, sys, time

import bench as b

WINDOW, ONE_HOP, GRACE = 180, 360, 60
fails = []


def check(ok, text):
    print(f"  {'PASS' if ok else 'FAIL'} {text}", flush=True)
    if not ok:
        fails.append(text)


def clock():
    return time.strftime("%H:%M:%S")


def polls(t0, t1):
    """{repeater: [(time, station, result)]}, sorted, with the log lines printed."""
    out = {}
    print(f"  polls {t0} to {t1}:", flush=True)
    for st, by_node in b.count(t0, t1).items():
        for node, rows in by_node.items():
            out.setdefault(node, []).extend((r.split(":", 3)[0] + ":" + r.split(":")[1] + ":" + r.split(":")[2], st, r.rsplit(":", 1)[1]) for r in rows)
    return {k: sorted(v) for k, v in out.items()}


def secs(t):
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def monitors(node):
    return b.get(f"/api/nodes/{b.repeater_keys()[node]}/monitors")


def roles(node):
    return {r["name"][-1]: r["role"] for r in monitors(node)["monitors"]}


def poller(node):
    return next((s for s, r in roles(node).items() if r == "polling"), None)


def key_of(station):
    return next(r["observer_id"] for r in monitors("CP_WLOMOND")["monitors"] if r["name"].endswith(station))


def wait(secs_):
    print(f"  waiting {secs_} s", flush=True)
    time.sleep(secs_)


def baseline():
    print("== baseline: one poller per repeater, one window apart", flush=True)
    wait(120)  # routes settle after the start
    t0 = clock(); wait(WINDOW * 3 + 30); t1 = clock()
    p = polls(t0, t1)
    for node in b.REPEATERS:
        rows = [r for r in p.get(node, [])]
        check(len({r[1] for r in rows}) == 1, f"{node}: one station polled ({sorted({r[1] for r in rows})})")
        gaps = [secs(y[0]) - secs(x[0]) for x, y in zip(rows, rows[1:])]
        check(rows and all(g >= WINDOW - 15 for g in gaps), f"{node}: {len(rows)} polls, gaps {gaps} s, none under the window")
    check(roles("CP_WLOMOND").get("B") == "standby", "B, one hop from West Lomond, is standby")


def kill_switch():
    print("== kill switch", flush=True)
    b.policy(['paused=true', 'pausedReason="bench"'])
    wait(20); t0 = clock()
    wait(WINDOW + GRACE); t1 = clock()
    check(not polls(t0, t1), "no polls while paused")
    check(set(roles("CP_WLOMOND").values()) == {"paused"}, f"monitors show paused ({roles('CP_WLOMOND')})")
    claim1 = monitors("CP_WLOMOND")["monitors"][0]["last_claim"]
    wait(70)
    check(monitors("CP_WLOMOND")["monitors"][0]["last_claim"] > claim1, "claims still arrive while paused")
    print("  CoreScope down, pause must hold", flush=True)
    subprocess.run("docker stop nbbench-corescope", shell=True, stdout=subprocess.DEVNULL)
    t0 = clock(); wait(WINDOW); t1 = clock()
    check(not polls(t0, t1), "no polls while paused and CoreScope is unreachable")
    subprocess.run("docker start nbbench-corescope", shell=True, stdout=subprocess.DEVNULL)
    wait(25)
    t0 = clock()
    b.policy(['paused=false', 'pausedReason=""'])
    wait(WINDOW + 60); t1 = clock()
    check(bool(polls(t0, t1)), "polling resumes after the pause lifts")


def min_version():
    print("== minimum version above the running plugin", flush=True)
    b.policy(['minPluginVersion="9.9.9"', 'minVersionReason="bench"'])
    wait(20); t0 = clock(); wait(WINDOW + GRACE); t1 = clock()
    check(not polls(t0, t1), "no polls from a disabled version")
    check(set(roles("CP_WLOMOND").values()) == {"version_disabled"}, f"monitors show version disabled ({roles('CP_WLOMOND')})")
    t0 = clock()
    b.policy(['minPluginVersion=""', 'minVersionReason=""'])
    wait(WINDOW + 60)
    check(bool(polls(t0, clock())), "polling resumes when the minimum drops")


def excluded():
    print("== excluded repeater", flush=True)
    pk = b.repeater_keys()["CP_LUCKLAW"]
    b.policy([f'excludedNodes=["{pk}"]'])
    wait(20); t0 = clock(); wait(WINDOW + GRACE); t1 = clock()
    p = polls(t0, t1)
    check("CP_LUCKLAW" not in p, "nobody polled the excluded repeater")
    check("CP_WLOMOND" in p, "the other repeater is still polled")
    b.policy(['excludedNodes=[]'])
    wait(40)


def blocked():
    print("== blocked observer", flush=True)
    who = poller("CP_WLOMOND")
    b.policy([f'blockedObservers=["{key_of(who)}"]'])
    wait(20); t0 = clock(); wait(WINDOW + GRACE + 45); t1 = clock()
    p = polls(t0, t1)
    check(all(r[1] != who for rows in p.values() for r in rows), f"blocked station {who} sent nothing")
    check(any(r[1] != who for r in p.get("CP_WLOMOND", [])), "another station polled West Lomond")
    check(roles("CP_WLOMOND").get(who) == "blocked", f"monitors show blocked ({roles('CP_WLOMOND')})")
    b.policy(['blockedObservers=[]'])
    wait(40)


def takeover():
    print("== poller goes silent, then comes back", flush=True)
    who = poller("CP_WLOMOND")
    last = max(secs(r[0]) for r in polls("00:00:00", clock()).get("CP_WLOMOND", []) )
    b.stop("plugin" + who)
    t0 = clock(); wait(WINDOW * 3 + GRACE); t1 = clock()
    rows = polls(t0, t1).get("CP_WLOMOND", [])
    first = rows[0] if rows else None
    check(first is not None and first[1] != who, f"a standby stepped in ({first})")
    check(first is not None and secs(first[0]) - last >= WINDOW + GRACE - 15,
          f"not before window plus grace ({secs(first[0]) - last if first else '-'} s after the last reading)")
    r = roles("CP_WLOMOND")
    check(r.get(who) != "polling" and "polling" in r.values(), f"the poller changed ({r})")
    b.start_plugin(who)
    wait(WINDOW * 2 + 60)
    print(f"  roles after {who} returned: {roles('CP_WLOMOND')}", flush=True)


def one_hop():
    print("== only the one-hop station left", flush=True)
    b.stop("pluginA"); b.stop("pluginC")
    wait(WINDOW + GRACE + 60)  # B takes over
    t0 = clock(); wait(ONE_HOP * 2 + GRACE); t1 = clock()
    rows = polls(t0, t1).get("CP_WLOMOND", [])
    check(bool(rows) and all(r[1] == "B" and r[2] == "ok" for r in rows), f"B polls West Lomond through Lucklaw and is answered ({rows})")
    gaps = [secs(y[0]) - secs(x[0]) for x, y in zip(rows, rows[1:])]
    check(bool(rows) and all(g >= ONE_HOP - 15 for g in gaps), f"one-hop window kept: gaps {gaps} s")
    b.start_plugin("A"); b.start_plugin("C")


STEPS = {f.__name__: f for f in (baseline, kill_switch, min_version, excluded, blocked, takeover, one_hop)}
if __name__ == "__main__":
    for name in sys.argv[1:] or STEPS:
        STEPS[name]()
    print(f"\n{'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + '; '.join(fails)}", flush=True)
    sys.exit(1 if fails else 0)
