# Neighbour Battery

An openHop plugin that reads the battery of the repeaters your node hears
directly, or through one relay it hears directly, and publishes it to MQTT as
your repeater's observer, for CoreScope to show.

- Requests and retries always go direct: with an empty path to a repeater heard
  at zero hops, or along a one-relay path. Never flooded.
- Status is a binary request (`CMD_SEND_BINARY_REQ`, type `0x01`) matched on its
  tag. openHop sends that once; it resends a slow `CMD_SEND_STATUS_REQ` itself
  as a flood, so that command is never used.
- Logs in until one login works. After that a login is sent only as a probe,
  at most one per poll, when a request gets no answer: it costs a repeater
  that still knows this node nothing, re-adds it after a reboot or eviction,
  and a silent login marks the route dead so no more tries are spent on it.
  Blank password: guest or ACL, unless you set one per repeater.
- Each request (status, telemetry) is tried up to 3 times per poll, all direct.
- Direct repeaters are asked for status (battery and stats). Repeaters one
  hop away are asked by telemetry only (battery and chip temperature): the
  status reply is about four times the airtime, paid twice through a relay.
- When a direct repeater's status replies don't get through but telemetry
  does, the battery comes from telemetry, that repeater is asked by telemetry
  only, and status is tried again once a day.
- A "login failed" push only counts as a password problem after 3 in a row:
  stock repeaters don't answer a wrong password at all.
- A repeater that didn't answer is tried again after 20 minutes (up to 3 tries
  an hour), dropping to the normal interval after 9 misses in a row.
- With "Try an alternative route" on, the tries go round the routes heard in
  adverts; the route that last answered goes first.
- Temperature, switched on per repeater in its Details: a telemetry request
  (type `0x03`) after status, reading the chip temperature guests get on ESP32
  and nRF52 boards. A repeater that reports none 3 times is asked once a day.
- Every reading carries the status reply's counters (uptime, noise floor,
  airtime, packet counts), which cost no extra airtime.
- A repeater whose adverts never arrive can be added by key, or found by name in
  CoreScope (`corescope_url`). It is treated as heard direct.
- Publishes `meshcore/{IATA}/{KEY}/battery` to the brokers you pick from openHop's list.
- No changes to openHop.

### Shared guest key (opt-in)

A repeater can be polled under a shared, public guest identity instead of this
observer's own key, so any observer that hears the reply can feed CoreScope.
On by default (Poll with the guest key, under Polling); a repeater set by hand in its Details keeps
its own choice and shows Overridden.

The plugin keeps no key of its own: it fetches the shared key from CoreScope
(`GET /api/shared-guest/key` on `corescope_url`). When CoreScope serves a key,
repeaters using it (all of them by default; see Poll with the guest key) are polled under it; when it serves none, they fall back to
this observer's own key. The key is public by design.

- Requests, logins and the route update are built and encrypted by the plugin
  with the shared key and sent as finished raw packets (`CMD_SEND_RAW_PACKET`);
  replies are read off the air from the raw RX log (`0x88`). Nothing is flooded.
- The shared key is only ever used with a blank or `hello` password, enforced
  where the packets are built. A repeater with a saved password can't use it.
- It is never let near admin. If a repeater answers the key as admin, the
  plugin stops using it there, removes the key's access (`setperm <key> 0`,
  then a verifying login) and tells you.
- Readings taken this way are published with `"key":"shared"`, which CoreScope
  credits as "shared key".
- `check_corescope` turns on the wider antenna: a guest-key reply missed here
  is looked for in the packets CoreScope's observers heard.

### Community polling (0.8)

CoreScope sets the limits for everyone polling a repeater and picks one
observer to poll each one; the rest stand by. The plugin:

- pulls `GET /api/guest-polling/policy?observer=…&plugin_version=…` and
  `/api/guest-polling/owners` every 10 min, and the policy again before a send
  if the saved copy is older than 60 s. The last good policy is kept in
  `state.json`; with none ever fetched, the built-in defaults apply (60 min
  direct, 120 min one hop, 30 min standby grace, 2 requests per window, take
  over after 2 missed windows). Values are clamped (direct 30–1440 min, one hop
  60–1440, grace 10–120, requests 1–3, takeover 1–6) and the page says "CoreScope
  asked for X, using Y".
- stops sending anything, Test and Check now included, while CoreScope has
  paused polling, blocked this observer, or set a minimum version above this
  one. These hold through a CoreScope outage (or a 404) until a fetched policy
  clears them or the pause's end time passes. Excluded repeaters are never sent to.
- never polls a repeater faster than its window (the repeater's own window
  from CoreScope, else direct or one-hop by this observer's best route), and
  sends at most `max_requests` requests per repeater per window, quick retries
  included.
- before each scheduled poll asks CoreScope for the newest reading from anyone
  (`/api/nodes/{pk}/battery`): the poller skips if it is inside the window, a
  standby if it is inside window + grace, and reschedules to then. If CoreScope
  can't be reached the poller polls anyway and a standby holds; once owners
  have been unanswered for 30 min the plugin polls on its own, and with a
  policy over 6 h old (CoreScope unreachable) every window is doubled.
- with `community` on (default), publishes a claim v2 for every polled
  repeater (`…/polling`: key `shared`/`own`/`password`, route, SNR, policy
  version) every 30 min and at once when a route changes, and stands by where
  CoreScope picked another observer. With it off: no claims (one last claim
  says so), owners ignored; the limits, the check, pause and exclusions still
  apply.
- Check now "Only stale" follows the check and standby; "All of them" ignores
  both but keeps the request cap. Test ignores the check, standby and cap
  (60 s cooldown) but not the pause, block, version minimum or exclusions.
  Every skip is listed with its reason.

Headless runs: `OPENHOP_CONFIG_PATH` overrides `openhop_config_path`.
`OPENHOP_NEIGHBOUR_BATTERY_TEST_FAST=1` is a bench-only fast mode (minute
floors, 30 s policy refresh, 60 s claims, 30 s quick retry, interval = window);
it logs a warning at startup and is never set from the page.

## Install

```sh
./build_wheel.sh
```

Install the wheel from openHop's Plugins page, open **Neighbour Battery**, press
**Connect**, tick the repeaters and brokers, enable polling and save.

## Routes

Routes come from openHop's recorded adverts (`/api/filtered_packets`, type 4)
over `max_age_hours`. A repeater whose advert arrived through exactly one relay
can be reached through that relay when the relay itself was heard direct. The
hash width is the one the relay forwarded the advert with. Reach is "Direct
only" unless you set "Up to 1 hop".

## Payload

```json
{"v": 1, "origin": "My Repeater", "origin_id": "<KEY>", "timestamp": "…",
 "readings": [{"node_id": "<64-hex>", "battery_mv": 3940, "snr": -5.0, "rssi": -119,
               "source": "poll", "reply_hops": 0, "method": "status", "sampled_at": "…",
               "temperature_c": 31.5,
               "stats": {"uptime_secs": 788089, "noise_floor": -116, "last_rssi": -114,
                         "last_snr": 1.25, "tx_queue": 0, "packets_recv": 81234,
                         "packets_sent": 20276, "recv_flood": 76198, "recv_direct": 5012,
                         "sent_flood": 19211, "sent_direct": 1065, "tx_air_secs": 18301,
                         "rx_air_secs": 73835, "flood_dups": 56298, "direct_dups": 36,
                         "recv_errors": 28074, "err_events": 2}}]}
```

- `battery_mv` 0 means no battery fitted.
- `reply_hops` 1 and `via` (the relay's 64-hex key) mark a reading taken through a relay; `snr` is then the advert SNR on that route and `rssi` is left out.
- `temperature_c` is present only when temperature is on for that repeater and it reported one.
- `stats` holds the fields the repeater's firmware sent; an older, shorter reply leaves later ones out.
