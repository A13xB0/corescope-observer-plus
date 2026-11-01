# CoreScope Observer Plus

An openHop plugin that reads the battery, temperature and status of nearby repeaters for CoreScope.

Observers share the work. CoreScope picks the closest observer to poll each repeater, and the others are backups. Direct beats 1 hop.

## Install

1. Download the `.whl` from the [latest release](https://github.com/A13xB0/corescope-observer-plus/releases/latest).
2. In openHop, open **Plugins**, choose **Install wheel** and enable it.
3. Open **CoreScope Observer Plus**, connect, pick your repeaters and save.

## Good to know

- Nothing it sends is flooded.
- It polls with a separate guest key by default, so other observers can feed CoreScope the replies they overhear. Your repeater's own key is never shared, and the guest key never logs in as admin.
- CoreScope sets how often repeaters are polled, and can pause polling or turn off a plugin version.

## Develop

```
pip install -e ".[dev]" && pytest
```

`tools/meshbench` runs real openHop stations in MeshBench. See its [README](tools/meshbench/README.md).

To release, set the version in `openhop-plugin.json`, `pyproject.toml` and `__init__.py`, then push a matching tag such as `v0.10.2`. The wheel is built and published for you.

## Licence

MIT
