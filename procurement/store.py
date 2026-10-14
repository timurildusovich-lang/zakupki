from __future__ import annotations

import copy
import json
import os
import socket
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from .common import WorkflowError, atomic_json, iso, read_json


def empty_state(environment="test"):
    return {"schema_version": 1, "environment": environment, "requests": [], "prices": [],
            "inventory_checks": [], "inbox": [], "outbox": [], "jobs": [], "notifications": [], "events": []}


class Store:
    def __init__(self, root: Path, environment="test"):
        if environment not in {"test", "mail_test", "live"}:
            raise WorkflowError("Неизвестная среда.")
        self.root = root.resolve()
        self.environment = environment
        self.directory = self.root / "data" / environment
        self.path = self.directory / "state.json"

    def read(self):
        state = read_json(self.path) if self.path.exists() else empty_state(self.environment)
        if state.get("schema_version") != 1 or state.get("environment") != self.environment:
            raise WorkflowError("Файл истории имеет другую схему или среду.")
        return state

    @contextmanager
    def transaction(self):
        self.directory.mkdir(parents=True, exist_ok=True)
        lock = self.directory / ".state.lock"
        try:
            descriptor = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            raise WorkflowError("Историю уже изменяет другой процесс. При аварии проверьте владельца data/.../.state.lock.") from None
        token = uuid4().hex
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"pid": os.getpid(), "host": socket.gethostname(), "started_at": iso(), "token": token}, stream)
            state = self.read()
            before = copy.deepcopy(state)
            yield state
            if state != before or not self.path.exists():
                if self.path.exists():
                    atomic_json(self.path.with_suffix(".json.bak"), before)
                atomic_json(self.path, state)
        finally:
            lock.unlink(missing_ok=True)


def event(state, kind, now, **fields):
    entry = {"id": "evt_" + uuid4().hex[:12], "kind": kind, "at": iso(now), **fields}
    state["events"].append(entry)
    return entry


def notify(state, key, text, now, request_id=None, delivery_id=None):
    if any(n["key"] == key for n in state["notifications"]):
        return
    state["notifications"].append({"id": "note_" + uuid4().hex[:12], "key": key,
                                   "text": text, "at": iso(now), "channel": "codex",
                                   "request_id": request_id, "delivery_id": delivery_id, "acknowledged": False})
