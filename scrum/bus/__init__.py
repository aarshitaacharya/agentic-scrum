"""
bus/ — one publish/subscribe interface, two transports.

`get_bus()` is the only place that chooses. Import this, never the concrete
classes, and the rest of the codebase stays unaware of which one it got.
"""

from __future__ import annotations

from scrum.bus.base import EventBus, ReceivedEvent
from scrum.config import SETTINGS, Settings, resolve_backends

__all__ = ["EventBus", "ReceivedEvent", "get_bus"]

_BUS: EventBus | None = None


def get_bus(settings: Settings = SETTINGS, force_new: bool = False) -> EventBus:
    """
    Return the process-wide bus, building it on first use.

    If the AWS probe passed we try to construct the SNS/SQS bus. If *that*
    throws anyway — a malformed ARN, a region mismatch, an IAM policy that
    forbids sns:Publish — we do not crash the run: we log why and drop to the
    local bus. The probe proves credentials work; only a real publish proves
    the permissions do, and the fallback has to cover both.
    """
    global _BUS
    if _BUS is not None and not force_new:
        return _BUS

    decision = resolve_backends(settings)

    if decision.is_aws:
        try:
            from scrum.bus.aws import SnsSqsEventBus

            _BUS = SnsSqsEventBus(settings)
            return _BUS
        except Exception as exc:  # noqa: BLE001
            print(f"[bus] AWS bus construction failed ({type(exc).__name__}: {exc}) "
                  f"— falling back to the local bus")

    from scrum.bus.local import LocalEventBus

    _BUS = LocalEventBus(
        visibility_timeout=settings.visibility_timeout,
        max_receives=settings.max_receives,
    )
    return _BUS
