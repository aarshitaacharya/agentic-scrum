"""
bus/base.py — the interface both transports implement.

The shape is SQS's, not something neutral. Receive hands you a message plus a
receipt; the message stays invisible but undeleted until you ack it; if you
crash before acking, it comes back. Modelling the local bus on those semantics
(rather than modelling the AWS bus on a simple queue) means the easy path is
the one that also survives on AWS — you cannot accidentally write code that
only works because delivery was in-process and perfectly reliable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from scrum.events import AgentEvent


@dataclass
class ReceivedEvent:
    """An event plus the handle you need to acknowledge it."""

    event: AgentEvent
    receipt: str          # SQS ReceiptHandle, or the local bus's message id
    subscriber: str       # which queue it came off
    receive_count: int    # 1 on first delivery; >1 means a retry


class EventBus(ABC):
    """Publish/subscribe with at-least-once delivery and explicit acks."""

    mode: str = "abstract"

    @abstractmethod
    def publish(self, event: AgentEvent) -> str:
        """Fan the event out to every subscriber whose filter matches."""

    @abstractmethod
    def receive(self, subscriber: str, wait_seconds: int = 5,
                max_messages: int = 1) -> list[ReceivedEvent]:
        """Long-poll one subscriber's queue. Empty list means nothing waiting."""

    @abstractmethod
    def ack(self, received: ReceivedEvent) -> None:
        """Delete the message. Only call this once the work is durable."""

    @abstractmethod
    def nack(self, received: ReceivedEvent) -> None:
        """Return it for immediate redelivery (SQS: visibility timeout -> 0)."""

    def close(self) -> None:
        """Release any resources. No-op for most implementations."""

    # ── diagnostics, used by /backends and the CLI banner ──

    def stats(self) -> dict:
        return {"mode": self.mode}
