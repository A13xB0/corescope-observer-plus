#!/usr/bin/env python3
"""Community polling bench: real openHop stations on MeshBench KISS modems, native
MeshCore repeaters and a local CoreScope. See README.md.

  bench.py up [--fresh]          start everything (firmware built on first use)
  bench.py down                  stop everything
  bench.py plugins               restart the plugins only
  bench.py status                CoreScope's view of each repeater
  bench.py count HH:MM:SS HH:MM:SS   polls and skips per station in a time range
  bench.py policy key=json ...   edit CoreScope's polling block, wait for the reload
"""
import collections, json, os, re, secrets, shutil, signal, socket, subprocess, sys, time
import urllib.request
from pathlib import Path

HOME = Path.home()
PROJ = HOME / "Documents/projects"
BENCH = Path(os.environ.get("NB_BENCH_DIR", HOME / ".cache/nb-bench"))
SCRATCH = Path(os.environ.get("NB_BENCH_TMP", "/tmp/claude-1000/nbbench-build"))  # build output: RAM, not disk
PLUGIN = Path(__file__).resolve().parents[2]
OPENHOP_SRC = Path(os.environ.get("OPENHOP_SRC", PROJ / "openhop_repeater-pole"))
MESHCORE = PROJ / "MeshCore"
NATIVE = PROJ / "meshcore-native"
CRYPTO = HOME / "msim/arduinolibs/libraries/Crypto"
MB_BIN = os.environ.get("MESHBENCH", str(HOME / ".local/bin/meshbench-nb"))
SOCK = "/tmp/claude-1000/nbbench.sock"
IMAGE = os.environ.get("NB_CORESCOPE_IMAGE", "corescope-local:cpbench-f66eb54e")
VENV = BENCH / "venv"
CS_URL = "http://127.0.0.1:13000"
TAG = "repeater-v1.18.0"

LAT = 56.30
# name: lat, lon. OA and OC hear West Lomond direct; OB only through Lucklaw.
REPEATERS = {"CP_WLOMOND": (LAT, -2.10), "CP_LUCKLAW": (LAT, -1.90)}
STATIONS = {"A": ("CP_OA", 18001, 15051, LAT, -2.30),
            "B": ("CP_OB", 18002, 15052, LAT, -1.62),
            "C": ("CP_OC", 18003, 15053, LAT + 0.15, -2.12)}
POLICY = {"directMinutes": 3, "oneHopMinutes": 6, "standbyGraceMinutes": 1,
          "maxRequestsPerWindow": 2, "takeoverAfterMissedWindows": 2,
          "paused": False, "pausedReason": "", "pausedUntil": "", "excludedNodes": [],
          "blockedObservers": [], "minPluginVersion": "", "minVersionReason": "", "nodeMinutes": {}}


def sh(cmd, **k):
    return subprocess.run(cmd, shell=isinstance(cmd, str), check=True, **k)


def wb():
    sys.path.insert(0, str(PROJ / "meshcoresim/pkg/client-python"))
    from meshbench import Workbench
    return Workbench.attach(SOCK)


def get(path):
    with urllib.request.urlopen(CS_URL + path, timeout=5) as r:
        return json.load(r)


def spawn(name, argv, log, env=None):
    with open(log, "ab") as f:
        p = subprocess.Popen(argv, stdout=f, stderr=f, start_new_session=True, env={**os.environ, **(env or {})})
    (BENCH / "pids").mkdir(exist_ok=True)
    (BENCH / "pids" / name).write_text(str(p.pid))
    return p


def stop(prefix=""):
    d = BENCH / "pids"
    for f in sorted(d.glob(prefix + "*")) if d.exists() else []:
        try:
            os.killpg(int(f.read_text()), signal.SIGTERM)
        except (ProcessLookupError, ValueError, PermissionError):
            pass
        f.unlink()


def firmware():
    kiss, rep = BENCH / "fw/kiss", BENCH / "fw/rep"
    if kiss.exists() and rep.exists():
        return kiss, rep
    src = SCRATCH / "mc118"
    shutil.rmtree(src, ignore_errors=True)
    src.mkdir(parents=True)
    sh(f"git -C {MESHCORE} archive {TAG} | tar -x -C {src}")
    # MeshBench raises TX done later than 1.5x the modem's airtime estimate, so
    # the stock timeout reports good sends as failed and openHop resends them.
    h = src / "examples/kiss_modem/KissModem.h"
    h.write_text(re.sub(r"#define KISS_TX_TIMEOUT_FACTOR.*", "#define KISS_TX_TIMEOUT_FACTOR 4", h.read_text()))
    (BENCH / "fw").mkdir(exist_ok=True, parents=True)
    for role, dest in (("kiss_modem", kiss), ("simple_repeater", rep)):
        out = SCRATCH / f"fw-{role}"
        sh(["./build.sh", role, str(out)], cwd=NATIVE, env={**os.environ, "MESHCORE": str(src), "CRYPTO": str(CRYPTO)},
           stdout=subprocess.DEVNULL)
        shutil.copy(out / f"meshcore-{role}-linux-amd64", dest)
    shutil.rmtree(src)
    return kiss, rep


def venv():
    if not (VENV / "bin/openhop-repeater").exists():
        sh([sys.executable, "-m", "venv", str(VENV)])
        sh([VENV / "bin/pip", "install", "-q", "openhop_core==1.1.3"])
        sh([VENV / "bin/pip", "install", "-q", "-e", str(OPENHOP_SRC)])
    wheel = max((PLUGIN / "dist").glob("*.whl"), key=lambda p: p.stat().st_mtime)
    sh([VENV / "bin/pip", "install", "-q", str(wheel)], stderr=subprocess.DEVNULL)  # its dependencies
    sh([VENV / "bin/pip", "install", "-q", "--force-reinstall", "--no-deps", str(wheel)], stderr=subprocess.DEVNULL)


def mesh():
    if not Path(SOCK).exists():
        Path(SOCK).parent.mkdir(parents=True, exist_ok=True)
        spawn("meshbench", [MB_BIN, "headless", "-fixture", "", "-control-socket", SOCK], BENCH / "meshbench.log")
        for _ in range(50):
            if Path(SOCK).exists():
                break
            time.sleep(0.2)
    from datetime import timedelta
    w = wb()
    sys.path.insert(0, str(PROJ / "meshcoresim/pkg/client-python"))
    from meshbench import Kind
    if len(w.nodes.list()) < 5:
        kiss, rep = firmware()
        w.project.new(place="Fife")
        nodes = [{"name": n, "kind": Kind.SIMPLE_REPEATER, "lat": a, "lon": o} for n, (a, o) in REPEATERS.items()]
        nodes += [{"name": s[0], "kind": Kind.COMPANION, "lat": s[3], "lon": s[4]} for s in STATIONS.values()]
        w.nodes.place_many(nodes)
        w.wait_idle(timedelta(minutes=5))
        w.call("firmware.import", {"path": str(kiss), "role": "kiss_modem", "version": "nb-kiss", "label": "nb-kiss"})
        w.call("firmware.import", {"path": str(rep), "role": "simple_repeater", "version": "nb-rep", "label": "nb-rep"})
        for n in nodes:
            modem = n["name"].startswith("CP_O")
            w.call("node.set_firmware", {"node": n["name"], "version": "nb-kiss" if modem else "nb-rep",
                                         "role": "kiss_modem" if modem else "simple_repeater", "board": ""})
        w.sim.start()
        time.sleep(5)
    return {st: w.call("bench.serve", {"node": s[0], "kind": "tcp"})["addr"] for st, s in STATIONS.items()}


def station_config(st):
    import yaml
    node, http, comp, lat, lon = STATIONS[st]
    d = BENCH / f"st{st}"
    (d / "data").mkdir(parents=True, exist_ok=True)
    def seed(name):
        f = d / name
        if not f.exists():
            f.write_bytes(secrets.token_bytes(32))
        return f.read_bytes()
    c = yaml.safe_load((OPENHOP_SRC / "config.yaml.example").read_text())
    r = c.setdefault("repeater", {})
    r.update(identity_key=seed("identity.seed"), node_name=f"CP-Station-{st}", latitude=lat, longitude=lon,
             send_advert_interval_hours=1,
             security={"admin_password": "nb-bench", "guest_password": "", "allow_read_only": False,
                       "jwt_expiry_minutes": 60, "jwt_secret": secrets.token_hex(32), "max_clients": 1})
    c.update(setup_completed=True, radio_type="kiss", kiss={"baud_rate": 115200, "port": str(d / "modem")},
             radio={"bandwidth": 250000, "coding_rate": 5, "frequency": 869525000, "implicit_header": False,
                    "preamble_length": 16, "spreading_factor": 10, "tx_power": 22},
             http={"enabled": True, "host": "127.0.0.1", "port": http},
             identities={"room_servers": [], "companions": [{
                 "identity_key": seed("companion.seed"), "name": "NeighbourBattery", "type": "companion",
                 "settings": {"bind_address": "127.0.0.1", "node_name": f"NB-{st}", "tcp_port": comp}}]},
             mqtt_brokers={"iata_code": "TST", "email": "", "owner": "", "brokers": [{
                 "name": "LocalScope", "enabled": True, "transport": "tcp", "host": "127.0.0.1", "port": 11883,
                 "format": "letsmesh", "base_topic": None, "disallowed_packet_types": [], "retain_status": False,
                 "neighbors": False, "tls": {"enabled": False, "insecure": False}, "use_jwt_auth": False}]},
             mesh={"default_region": None, "unscoped_flood_allow": True, "loop_detect": "minimal", "path_hash_mode": 2},
             storage={"storage_dir": str(d / "data"), "retention": {"sqlite_cleanup_days": 31}},
             policy_file_path=str(d / "policy.yaml"))
    for k, v in (("gps", {"enabled": False}), ("glass", {"enabled": False, "cert_store_dir": str(d / "glass")})):
        c[k] = {**(c.get(k) or {}), **v}
    (d / "config.yaml").write_text(yaml.safe_dump(c, allow_unicode=True))


def corescope():
    d = BENCH / "cs"
    d.mkdir(exist_ok=True)
    (d / "config.json").write_text(json.dumps({
        "port": 3000,
        "mqttSources": [{"name": "local", "broker": "mqtt://localhost:1883", "topics": ["meshcore/#"]}],
        "neighborBattery": {"enabled": True, "minIntervalMinutes": 1, "hideAfterHours": 24, "polling": POLICY}}, indent=1))
    sh("docker rm -f nbbench-corescope", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    sh(f"docker run --rm -v {d}:/cs alpine sh -c 'rm -rf /cs/data; mkdir -p /cs/data; chmod 777 /cs/data'")
    sh(f"docker run -d --name nbbench-corescope -p 127.0.0.1:13000:3000 -p 127.0.0.1:11883:1883 -e DISABLE_CADDY=true "
       f"-v {d}/config.json:/app/config.json -v {d}/data:/app/data {IMAGE}", stdout=subprocess.DEVNULL)
    for _ in range(60):
        try:
            return get("/api/guest-polling/policy")
        except OSError:
            time.sleep(1)
    sys.exit("CoreScope did not come up")


def token(http):
    b = f"http://127.0.0.1:{http}"
    py = (f"import requests,json\nb={b!r}\nj=requests.post(b+'/auth/login',json=dict(username='admin',password='nb-bench',"
          "client_id='bench'),timeout=10).json()['token']\n"
          "print(requests.post(b+'/api/auth/tokens',json=dict(name='nb'),headers=dict(Authorization='Bearer '+j),timeout=10).json()['token'])")
    return subprocess.check_output([VENV / "bin/python", "-c", py], text=True).strip()


def start_station(st, addr):
    d = BENCH / f"st{st}"
    (d / "modem").unlink(missing_ok=True)
    spawn(f"socat{st}", ["socat", f"pty,raw,echo=0,link={d}/modem", f"tcp:{addr}"], d / "socat.log")
    time.sleep(1)
    spawn(f"openhop{st}", [VENV / "bin/openhop-repeater", "--config", d / "config.yaml"], d / "openhop.log")


def adverts():
    w = wb()
    now = w.call("sim.state")["now_ms"]
    for i, n in enumerate(REPEATERS):
        w.call("schedule.add", {"node": n, "at_ms": now + 2000 + i * 10000, "command": "advert"})


def repeater_keys():
    f = BENCH / "repeaters.json"
    if f.exists():
        return json.loads(f.read_text())
    http = STATIONS["A"][1]
    tok = (BENCH / "stA/token").read_text().strip()
    py = (f"import requests,json\nr=requests.get('http://127.0.0.1:{http}/api/adverts_by_contact_type',"
          f"params=dict(contact_type='Repeater',hours=24,limit=50),headers={{'X-API-Key':{tok!r}}},timeout=10).json()\n"
          "print(json.dumps({x['node_name']:x['pubkey'] for x in r['data']}))")
    keys = {k: v for k, v in json.loads(subprocess.check_output([VENV / "bin/python", "-c", py], text=True)).items()
            if k in REPEATERS}
    if len(keys) == len(REPEATERS):
        f.write_text(json.dumps(keys))
    return keys


def start_plugin(st):
    node, http, *_ = STATIONS[st]
    d, reps = BENCH / f"st{st}", repeater_keys()
    pd = d / "plugin"
    pd.mkdir(exist_ok=True)
    cfg = {"openhop_api_url": f"http://127.0.0.1:{http}", "openhop_config_path": str(d / "config.yaml"),
           "companion_name": "NeighbourBattery", "enabled": True, "targets": list(reps.values()),
           "brokers": ["LocalScope"], "corescope_url": CS_URL, "max_hops": 1, "community": True, "spacing_seconds": 5}
    if not (pd / "secrets.json").exists():
        cfg["api_token_input"] = (d / "token").read_text().strip()
    (pd / "config.json").write_text(json.dumps(cfg, indent=1))
    spawn(f"plugin{st}", [VENV / "bin/python", "-m", "openhop_neighbour_battery.main"], d / "plugin.log",
          {"OPENHOP_NEIGHBOUR_BATTERY_TEST_FAST": "1", "OPENHOP_PLUGIN_DATA": str(pd), "OPENHOP_CONFIG_PATH": str(d / "config.yaml")})


def up():
    stop("plugin"); stop("openhop"); stop("socat")
    time.sleep(2)
    BENCH.mkdir(parents=True, exist_ok=True)
    venv()
    addrs = mesh()
    print("CoreScope policy", corescope()["version"])
    for st in STATIONS:
        d = BENCH / f"st{st}"
        shutil.rmtree(d / "data", ignore_errors=True); shutil.rmtree(d / "plugin", ignore_errors=True)
        for f in ("plugin.log", "openhop.log"):
            (d / f).unlink(missing_ok=True)
        station_config(st)
        start_station(st, addrs[st])
    time.sleep(12)
    for st, s in STATIONS.items():
        (BENCH / f"st{st}/token").write_text(token(s[1]))
    (BENCH / "repeaters.json").unlink(missing_ok=True)
    adverts()
    time.sleep(30)
    print("repeaters", repeater_keys())
    for st in STATIONS:
        start_plugin(st)


def status():
    for name, pk in repeater_keys().items():
        m = get(f"/api/nodes/{pk}/monitors")
        print(f"== {name} window {m.get('window_s')}s paused={m.get('paused')} excluded={m.get('excluded')}")
        for r in m.get("monitors", []):
            print(f"   {r.get('name') or r['observer_id'][:8]:>14} {r.get('role'):<17} hops={r.get('hops')} eff={r.get('effective_hops')} "
                  f"v={r.get('plugin_version')} last={str(r.get('last_reading'))[11:19]} n7d={r.get('readings_7d')} claim={str(r.get('last_claim'))[11:19]}")


def count(t0, t1):
    out = {}
    for st in STATIONS:
        polls, skips, other = collections.defaultdict(list), collections.Counter(), []
        for line in open(BENCH / f"st{st}/plugin.log", errors="replace"):
            t = line[11:19]
            if not t0 <= t < t1:
                continue
            m = re.search(r" poll (CP_\w+) .*?: (\w+)", line)
            if m:
                polls[m.group(1)].append(f"{t}:{m.group(2)}"); continue
            m = re.search(r" skip (CP_\w+) \(\w+\): (.*)$", line)
            if m:
                skips[f"{m.group(1)}: {re.sub(r'[0-9]+ min', 'N min', m.group(2))[:70]}"] += 1; continue
            if re.search(r"ERROR|Traceback|stepping in", line):
                other.append(t + " " + line.split(" ", 4)[-1].strip()[:120])
        out[st] = dict(polls)
        print(f"  {st}: " + ("; ".join(f"{k} x{len(v)} [{' '.join(v)}]" for k, v in sorted(polls.items())) or "no polls"))
        for k, n in skips.most_common(4):
            print(f"      skip x{n} {k}")
        for o in other[-3:]:
            print("      " + o)
    return out


def policy(kvs):
    p = BENCH / "cs/config.json"
    c = json.loads(p.read_text())
    for kv in kvs:
        k, v = kv.split("=", 1)
        c["neighborBattery"]["polling"][k] = json.loads(v)
    before = get("/api/guest-polling/policy")["version"]
    with open(p, "r+") as f:  # same inode: the container has this file bind-mounted
        f.write(json.dumps(c, indent=1)); f.truncate()
    t0 = time.time()
    while time.time() - t0 < 90:
        now = get("/api/guest-polling/policy")
        if now["version"] != before:
            print(f"policy {before} -> {now['version']} after {time.time() - t0:.0f} s")
            return time.time() - t0
        time.sleep(1)
    sys.exit("policy did not reload within 90 s")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "up":
        up()
    elif cmd == "down":
        stop("plugin"); stop("openhop"); stop("socat"); stop("meshbench")
        sh("docker rm -f nbbench-corescope", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        Path(SOCK).unlink(missing_ok=True)
    elif cmd == "plugins":
        stop("plugin"); time.sleep(2); venv()
        for st in STATIONS:
            start_plugin(st)
    elif cmd == "status":
        status()
    elif cmd == "count":
        count(sys.argv[2], sys.argv[3])
    elif cmd == "policy":
        policy(sys.argv[2:])
    else:
        print(__doc__)
