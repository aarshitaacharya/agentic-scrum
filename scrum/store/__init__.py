"""
store/ — artifacts and run state, AWS-backed or local, chosen once.

Same deal as bus/: import the factories, never the concrete classes.
"""

from __future__ import annotations

from scrum.config import SETTINGS, Settings, resolve_backends
from scrum.store.base import (
    BUGGY_SCRIPT, PATCHED, QA_REVIEW, TICKET, TRACE,
    ArtifactStore, ConcurrentUpdate, RunStateStore,
)

__all__ = [
    "ArtifactStore", "RunStateStore", "ConcurrentUpdate",
    "get_artifact_store", "get_run_state_store",
    "BUGGY_SCRIPT", "TICKET", "PATCHED", "QA_REVIEW", "TRACE",
]

_ARTIFACTS: ArtifactStore | None = None
_RUN_STATE: RunStateStore | None = None


def get_artifact_store(settings: Settings = SETTINGS) -> ArtifactStore:
    global _ARTIFACTS
    if _ARTIFACTS is not None:
        return _ARTIFACTS

    if resolve_backends(settings).is_aws:
        try:
            from scrum.store.aws import S3ArtifactStore

            _ARTIFACTS = S3ArtifactStore(settings)
            return _ARTIFACTS
        except Exception as exc:  # noqa: BLE001
            print(f"[store] S3 unavailable ({type(exc).__name__}: {exc}) — using workspace/")

    from scrum.store.local import LocalArtifactStore

    _ARTIFACTS = LocalArtifactStore()
    return _ARTIFACTS


def get_run_state_store(settings: Settings = SETTINGS) -> RunStateStore:
    global _RUN_STATE
    if _RUN_STATE is not None:
        return _RUN_STATE

    if resolve_backends(settings).is_aws:
        try:
            from scrum.store.aws import DynamoRunStateStore

            _RUN_STATE = DynamoRunStateStore(settings)
            return _RUN_STATE
        except Exception as exc:  # noqa: BLE001
            print(f"[store] DynamoDB unavailable ({type(exc).__name__}: {exc}) — using JSON file")

    from scrum.store.local import LocalRunStateStore

    _RUN_STATE = LocalRunStateStore()
    return _RUN_STATE
