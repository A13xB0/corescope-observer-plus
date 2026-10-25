import json

from openhop_neighbour_battery.openhop import Neighbour, attach_routes

WL = "02f1fe" + "aa" * 29
GARVALD = "4128a6" + "bb" * 29
DALGETY = "d00d00" + "cc" * 29
FAR = "fa0000" + "dd" * 29


def adv(key, path, snr):
    return {"type": 4, "payload": key + "00" * 40, "original_path": json.dumps(path), "snr": snr}


def nodes():
    return [
        Neighbour(WL, "West Lomond", -1.0, -110, 0, True),
        Neighbour(GARVALD, "sco-edi-garvald", -4.0, -112, 0, False),  # latest advert came via a relay
        Neighbour(DALGETY, "Dalgety Bay", None, None, 0, False),
        Neighbour(FAR, "Far Away", None, None, 0, False),
    ]


def test_routes_from_adverts():
    ns = nodes()
    attach_routes(ns, [
        adv(GARVALD, [], -4.0),                     # garvald heard direct once
        adv(DALGETY, ["4128A6"], -5.0),
        adv(DALGETY, ["4128A6"], -3.0),
        adv(DALGETY, ["02F1FE"], -8.0),
        adv(FAR, ["4128A6", "02F1FE"], 1.0),       # two hops: no route
        adv(WL, [], -1.0),
    ])
    by = {n.node_id: n for n in ns}
    d = by[DALGETY]
    assert d.hops == 1
    assert [r.relay_id for r in d.routes] == [GARVALD, WL]  # most adverts first
    assert d.routes[0].hash_size == 3 and d.routes[0].path == bytes.fromhex("4128a6")
    assert d.routes[0].snr == -4.0 and d.routes[0].label == "via sco-edi-garvald"
    assert by[GARVALD].hops == 0  # a direct advert counts even though the latest wasn't
    assert by[FAR].hops is None and by[FAR].routes == []
    assert by[WL].routes[0].hops == 0


def test_ambiguous_or_unknown_relay_is_skipped():
    ns = nodes()
    twin = Neighbour("41" + "ee" * 31, "Another 41", -2.0, -100, 0, True)
    ns.append(twin)
    attach_routes(ns, [
        adv(DALGETY, ["41"], -5.0),       # 1-byte hash: only the twin is heard direct here
        adv(DALGETY, ["999999"], -5.0),   # nobody heard direct
    ])
    d = next(n for n in ns if n.node_id == DALGETY)
    assert [r.relay_id for r in d.routes] == [twin.node_id]
    assert d.routes[0].hash_size == 1


def test_relay_must_not_be_the_target():
    ns = nodes()
    attach_routes(ns, [adv(WL, ["02F1FE"], -1.0)])
    wl = next(n for n in ns if n.node_id == WL)
    assert [r.hops for r in wl.routes] == [0]
