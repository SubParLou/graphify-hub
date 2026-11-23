"""Tracked repos and their build status, persisted to one JSON file."""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone

from . import config

FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")

_lock = threading.RLock()
_repos: dict[str, dict] = {}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def valid_name(full_name: str) -> bool:
    return bool(FULL_NAME_RE.match(full_name)) and ".." not in full_name


def valid_branch(branch: str) -> bool:
    return bool(BRANCH_RE.match(branch)) and ".." not in branch and not branch.startswith(("-", "/"))


def load() -> None:
    with _lock:
        _repos.clear()
        if config.STATE_FILE.exists():
            data = json.loads(config.STATE_FILE.read_text(encoding="utf-8"))
            _repos.update(data.get("repos", {}))
        # A build interrupted by a restart is not still running.
        for r in _repos.values():
            if r.get("status") in ("queued", "syncing", "building"):
                r["status"] = "pending"


def _save() -> None:
    tmp = config.STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"repos": _repos}, indent=2), encoding="utf-8")
    os.replace(tmp, config.STATE_FILE)


def all_repos() -> dict[str, dict]:
    with _lock:
        return {k: dict(v) for k, v in sorted(_repos.items())}


def get(full_name: str) -> dict | None:
    with _lock:
        r = _repos.get(full_name)
        return dict(r) if r else None


def add(full_name: str, branch: str) -> None:
    with _lock:
        _repos.setdefault(full_name, {"added": now(), "status": "pending"})["branch"] = branch
        _save()


def update(full_name: str, **fields) -> None:
    with _lock:
        if full_name in _repos:
            _repos[full_name].update(fields)
            _save()


def remove(full_name: str) -> None:
    with _lock:
        _repos.pop(full_name, None)
        _save()
