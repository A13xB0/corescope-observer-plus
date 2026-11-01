"""Files in the data directory.

secrets.json (0600) stays out of config.json, so openHop's settings API never returns it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .models import JsonDict, NodeState


def _read(path: Path) -> JsonDict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(path: Path, value: JsonDict, mode: int = 0o644) -> None:
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(value, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.chmod(tmp, mode)
    tmp.replace(path)


class Secrets:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "secrets.json"
        self._value = _read(self.path)

    @property
    def api_token(self) -> str:
        return str(self._value.get("api_token") or "")

    def set_api_token(self, token: str) -> None:
        self._value["api_token"] = token.strip()
        self._save()

    def password(self, node_id: str) -> str:
        return str((self._value.get("passwords") or {}).get(node_id) or "")

    def has_password(self, node_id: str) -> bool:
        return bool(self.password(node_id))

    def set_password(self, node_id: str, password: str) -> None:
        passwords = dict(self._value.get("passwords") or {})
        if password:
            passwords[node_id] = password
        else:
            passwords.pop(node_id, None)
        self._value["passwords"] = passwords
        self._save()

    def _save(self) -> None:
        _write(self.path, self._value, mode=0o600)


class State:
    def __init__(self, data_dir: Path):
        self.path = data_dir / "state.json"
        self.value = _read(self.path)
        self.value.setdefault("nodes", {})
        self.value.setdefault("queue", [])
        self.value.setdefault("brokers", {})

    def node(self, node_id: str) -> NodeState:
        node: NodeState = self.value["nodes"].setdefault(node_id, {})
        return node

    def broker(self, name: str) -> JsonDict:
        broker: JsonDict = self.value["brokers"].setdefault(name, {})
        return broker

    def save(self) -> None:
        _write(self.path, self.value)
