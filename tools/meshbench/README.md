# Community polling bench

Real openHop stations and this plugin against native MeshCore 1.18 repeaters in MeshBench,
with a local CoreScope. Nothing here touches a real mesh or a real broker.

```
CP_OA --direct-- CP_WLOMOND --direct-- CP_LUCKLAW --direct-- CP_OB
         CP_OC --direct-- CP_WLOMOND
```

Each station is an openHop process whose radio is a MeshBench node running MeshCore's
`kiss_modem` firmware, served over TCP and bridged to a PTY with socat. CP_OB reaches West
Lomond only through Lucklaw. The plugins run in fast test mode, and CoreScope's policy is
direct 3 min, one hop 6 min, grace 1 min.

```
python3 bench.py up             # start it all (builds firmware on first use)
python3 scenarios.py            # every scenario, about 50 min
python3 scenarios.py kill_switch takeover
NB_GUEST=1 python3 bench.py up  # the same, polling under the guest key
python3 bench.py manager A      # run A through the plugin manager, for the real page
python3 bench.py status         # who polls what
python3 bench.py policy paused=true
python3 bench.py down
```

Needs: a MeshBench binary (`MESHBENCH`, default `~/.local/bin/meshbench-nb`), the MeshCore,
meshcore-native and arduinolibs checkouts, an openHop source tree (`OPENHOP_SRC`), a CoreScope
image with community polling (`NB_CORESCOPE_IMAGE`), `socat`, Docker and a built wheel in `dist/`.

Stations, keys and logs live in `NB_BENCH_DIR` (default `~/.cache/nb-bench`, under 300 MB).
Firmware objects build in `NB_BENCH_TMP`, in RAM. Station configs come from openHop's
`config.yaml.example` with fresh keys, never from a real station's config.

The modem firmware is built with a longer transmit timeout: MeshBench raises TX done later than
the 1.5x airtime estimate the stock modem allows, so a good send is reported as failed and openHop
sends it again. Node names must not clash with another running MeshBench instance.
