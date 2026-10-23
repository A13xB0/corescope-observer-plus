# Neighbour Battery

An openHop plugin that reads the battery of the repeaters your node hears
directly and publishes it to MQTT as your repeater's observer, for CoreScope to
show.

- Requests go direct, with an empty path, only to repeaters heard at zero hops. Never flooded.
- Logs in with a blank password (guest or ACL) unless you set one per repeater.
- Publishes `meshcore/{IATA}/{KEY}/battery` to the brokers you pick from openHop's list.
- No changes to openHop.

## Install

```sh
./build_wheel.sh
```

Install the wheel from openHop's Plugins page, open **Neighbour Battery**, press
**Connect**, tick the repeaters and brokers, enable polling and save.

## Payload

```json
{"v": 1, "origin": "My Repeater", "origin_id": "<KEY>", "timestamp": "…",
 "readings": [{"node_id": "<64-hex>", "battery_mv": 3940, "snr": -5.0, "rssi": -119,
               "source": "poll", "reply_hops": 0, "method": "status", "sampled_at": "…"}]}
```

`battery_mv` 0 means no battery fitted.
