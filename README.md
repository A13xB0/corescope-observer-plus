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
