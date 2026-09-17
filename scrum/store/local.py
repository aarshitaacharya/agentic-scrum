"""
store/local.py — the fallback store: the workspace/ directory you already have.

Deliberately flat rather than namespaced by run_id. Two reasons: the UI fetches
/workspace/ticket.txt by that exact path, and the local server only ever runs
one pipeline at a time (server.py guards on `pipeline_running`). Under those
conditions "latest run" and "the run" are the same thing.

Finished runs are copied into workspace/runs/<run_id>/ so history survives the
next run overwriting the live files.
"""

from __future__ import annotations

import json
import os
import shutil
import threading

from scrum.store.base import ArtifactStore, ConcurrentUpdate, RunStateStore


class LocalArtifactStore(ArtifactStore):
    mode = "local"

    def __init__(self, root: str = "workspace"):
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, name: str) -> str:
        # Names come from our own constants, but an agent-supplied name must
        # never escape the workspace.
        safe = os.path.normpath(name).lstrip(os.sep)
        full = os.path.abspath(os.path.join(self.root, safe))
        if not full.startswith(self.root + os.sep) and full != self.root:
            raise ValueError(f"path escapes workspace: {name}")
        return full

    def read(self, run_id: str, name: str) -> str:
        with open(self._path(name)) as fh:
            return fh.read()

    def write(self, run_id: str, name: str, content: str) -> str:
        path = self._path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._lock, open(path, "w") as fh:
            fh.write(content)
        return path

    def exists(self, run_id: str, name: str) -> bool:
        return os.path.exists(self._path(name))

    def append(self, run_id: str, name: str, line: str) -> None:
        path = self._path(name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._lock, open(path, "a") as fh:
            fh.write(line.rstrip("\n") + "\n")

    def archive(self, run_id: str, names: list[str]) -> str:
        """Snapshot a finished run so the next one can overwrite the live files."""
        dest = os.path.join(self.root, "runs", run_id)
        os.makedirs(dest, exist_ok=True)
        for name in names:
            src = self._path(name)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(dest, os.path.basename(name)))
        return dest


class LocalRunStateStore(RunStateStore):
    """
    A JSON file with the same optimistic-locking contract as the DynamoDB one.

    Enforcing the version check locally is not pedantry: it means the
    supervisor's concurrency handling is exercised on a laptop instead of being
    dead code until the day it deploys.
    """

    mode = "local"

    def __init__(self, path: str = "workspace/run_state.json"):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._lock = threading.Lock()

    def _load_all(self) -> dict:
        if not os.path.exists(self.path):
            return {}
        try:
            with open(self.path) as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError):
            return {}

    def get(self, run_id: str) -> dict:
        with self._lock:
            return self._load_all().get(run_id, {})

    def put(self, run_id: str, state: dict, expected_version: int | None = None) -> dict:
        with self._lock:
            everything = self._load_all()
            current = everything.get(run_id, {})
            current_version = current.get("version", 0)

            if expected_version is not None and current_version != expected_version:
                raise ConcurrentUpdate(
                    f"run {run_id} is at version {current_version}, expected {expected_version}"
                )

            state = {**current, **state, "version": current_version + 1}
            everything[run_id] = state
            tmp = self.path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(everything, fh, indent=2)
            os.replace(tmp, self.path)   # atomic, so a crash mid-write is survivable
            return state
