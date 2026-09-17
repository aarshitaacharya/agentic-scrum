"""
bus/local.py — SNS + SQS, minus AWS.

This is the fallback, and it is written to be a *simulator* rather than a
shortcut. It reproduces the four behaviours that actually change how you have
to write a consumer:

  1. fan-out with filtering  — one publish, N queues, filtered by event type
  2. visibility timeout      — a received message is hidden, not removed
  3. redelivery              — un-acked work reappears after the timeout
  4. dead-letter queues      — after maxReceiveCount, it stops coming back

Because those hold, a handler written against this bus keeps working when the
real one is swapped in, and bugs that only show up under redelivery (the
classic: a non-idempotent handler that double-writes) show up here too, on a
laptop, instead of in CloudWatch at 2am.

Every publish is also appended to workspace/events.jsonl, which gives you the
same audit trail you would get from an SNS delivery log.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from collections import deque

from scrum.bus.base import EventBus, ReceivedEvent
from scrum.events import AgentEvent, subscribers_for


class _Message:
    """One queue's copy of an event. Each copy carries its own retry count."""

    __slots__ = ("id", "event", "queue", "visible_at", "receive_count")

    def __init__(self, event: AgentEvent, queue: str):
        self.id = str(uuid.uuid4())
        self.event = event
        self.queue = queue        # which subscriber owns this copy
        self.visible_at = 0.0     # monotonic deadline; 0 == visible now
        self.receive_count = 0


class LocalEventBus(EventBus):
    """In-process pub/sub with SQS-shaped delivery semantics."""

    mode = "local"

    def __init__(self, visibility_timeout: int = 120, max_receives: int = 3,
                 log_path: str | None = "workspace/events.jsonl"):
        self.visibility_timeout = visibility_timeout
        self.max_receives = max_receives
        self.log_path = log_path

        self._lock = threading.Lock()
        self._new_message = threading.Condition(self._lock)
        self._queues: dict[str, deque[_Message]] = {}
        self._in_flight: dict[str, _Message] = {}   # message id -> message
        self._dlq: list[AgentEvent] = []
        self._published = 0

    # ── publish ───────────────────────────────────────────────────────────────

    def publish(self, event: AgentEvent) -> str:
        targets = subscribers_for(event.type)
        with self._new_message:
            for name in targets:
                # A separate _Message per queue: SNS fan-out gives each
                # subscriber its own copy with its own receipt and its own
                # retry count. Sharing one object here would quietly make
                # redelivery behave differently from the real thing.
                self._queues.setdefault(name, deque()).append(_Message(event, name))
            self._published += 1
            self._new_message.notify_all()

        self._append_log(event, targets)
        return event.event_id

    def _append_log(self, event: AgentEvent, targets: list[str]) -> None:
        if not self.log_path:
            return
        try:
            os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
            with open(self.log_path, "a") as fh:
                fh.write(json.dumps({
                    "ts": event.ts, "type": event.type, "run_id": event.run_id,
                    "attempt": event.attempt, "source": event.source,
                    "delivered_to": targets, "event_id": event.event_id,
                }) + "\n")
        except OSError:
            pass  # the log is a convenience, never a reason to fail a publish

    # ── receive ───────────────────────────────────────────────────────────────

    def receive(self, subscriber: str, wait_seconds: int = 5,
                max_messages: int = 1) -> list[ReceivedEvent]:
        deadline = time.monotonic() + wait_seconds
        out: list[ReceivedEvent] = []

        with self._new_message:
            while True:
                self._reclaim_expired_locked()
                queue = self._queues.setdefault(subscriber, deque())

                while queue and len(out) < max_messages:
                    msg = queue.popleft()
                    msg.receive_count += 1

                    # maxReceiveCount: stop redelivering poison messages.
                    if msg.receive_count > self.max_receives:
                        self._dlq.append(msg.event)
                        continue

                    msg.visible_at = time.monotonic() + self.visibility_timeout
                    self._in_flight[msg.id] = msg
                    out.append(ReceivedEvent(
                        event=msg.event, receipt=msg.id,
                        subscriber=subscriber, receive_count=msg.receive_count,
                    ))

                if out:
                    return out

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []          # empty receive — same as an SQS long poll
                # Wake early if someone publishes; this is what makes handoffs
                # feel instant locally instead of waiting out the poll.
                self._new_message.wait(timeout=min(remaining, 0.25))

    def _reclaim_expired_locked(self) -> None:
        """
        Un-acked messages past their visibility timeout become visible again.

        This is the behaviour that catches non-idempotent handlers: if an agent
        takes longer than the timeout and does not ack, a second copy goes out
        while the first is still working. Tune SCRUM_VISIBILITY_TIMEOUT above
        your slowest agent turn, exactly as you would on SQS.
        """
        now = time.monotonic()
        expired = [m for m in self._in_flight.values() if m.visible_at <= now]
        for msg in expired:
            del self._in_flight[msg.id]
            self._queues.setdefault(msg.queue, deque()).appendleft(msg)

    # ── ack / nack ────────────────────────────────────────────────────────────

    def ack(self, received: ReceivedEvent) -> None:
        with self._lock:
            self._in_flight.pop(received.receipt, None)

    def nack(self, received: ReceivedEvent) -> None:
        with self._new_message:
            msg = self._in_flight.pop(received.receipt, None)
            if msg is None:
                return
            msg.visible_at = 0.0
            self._queues.setdefault(msg.queue, deque()).appendleft(msg)
            self._new_message.notify_all()

    # ── diagnostics ───────────────────────────────────────────────────────────

    def stats(self) -> dict:
        with self._lock:
            return {
                "mode": self.mode,
                "published": self._published,
                "depths": {k: len(v) for k, v in self._queues.items()},
                "in_flight": len(self._in_flight),
                "dlq": len(self._dlq),
            }

    @property
    def dead_letters(self) -> list[AgentEvent]:
        with self._lock:
            return list(self._dlq)
