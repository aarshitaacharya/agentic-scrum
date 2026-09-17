"""
store/base.py — where the work-in-progress lives.

Locally the agents can just share workspace/. On Lambda they cannot: three
functions on three machines have no common disk, and /tmp dies with the
container. So artifacts have to live somewhere both can reach, and the handoff
event carries a *pointer*, not the payload.

That is not only a Lambda constraint — SNS caps a message at 256 KB, and a
patched source file plus a QA transcript will blow past that sooner than you
would like. Passing keys instead of blobs is the standard claim-check pattern
and it keeps the messages small either way.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

# The fixed set of artifacts a run produces. The UI fetches these by name, so
# the names are part of the contract with ui/app.js — do not rename casually.
BUGGY_SCRIPT = "buggy_script.py"
TICKET       = "ticket.txt"
PATCHED      = "patched_script.py"
QA_REVIEW    = "qa_review.txt"
TRACE        = "trace.jsonl"      # every ReAct step, for the transcript view


class ConcurrentUpdate(Exception):
    """
    An optimistic-locked write lost the race — someone else wrote first.

    Never fatal. The caller re-reads the current state and re-applies its
    change, which is the same retry loop whether the loser was a DynamoDB
    ConditionalCheckFailed or a local version mismatch.
    """


class ArtifactStore(ABC):
    """Read/write the files a run passes between agents."""

    mode: str = "abstract"

    @abstractmethod
    def read(self, run_id: str, name: str) -> str: ...

    @abstractmethod
    def write(self, run_id: str, name: str, content: str) -> str:
        """Returns a locator (a path or an s3:// URI) to put in the event."""

    @abstractmethod
    def exists(self, run_id: str, name: str) -> bool: ...

    @abstractmethod
    def append(self, run_id: str, name: str, line: str) -> None: ...

    def read_or(self, run_id: str, name: str, default: str = "") -> str:
        try:
            return self.read(run_id, name)
        except (FileNotFoundError, KeyError):
            return default


class RunStateStore(ABC):
    """
    The supervisor's memory: attempt count, verdict, terminal state.

    Split from ArtifactStore because the access pattern is different — small,
    hot, read-modify-write under concurrency. That is a key-value store's job
    (DynamoDB), not an object store's.
    """

    mode: str = "abstract"

    @abstractmethod
    def get(self, run_id: str) -> dict: ...

    @abstractmethod
    def put(self, run_id: str, state: dict, expected_version: int | None = None) -> dict:
        """
        Write state. If `expected_version` is given the write must fail when
        someone else has written since — that is what stops two concurrent QA
        failures from both incrementing `attempt` to 2 and burning one retry.
        """
