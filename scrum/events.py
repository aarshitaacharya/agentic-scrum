"""
events.py — the contract between agents.

Agents never call each other. They publish facts ("a ticket exists", "a patch
was submitted") and the bus decides who cares. That indirection is the reason
this runs unchanged on SNS+SQS or on a deque: the *vocabulary* is the API, not
the transport.

`SUBSCRIPTIONS` below is the single source of truth for routing. The local bus
reads it directly; the SAM template encodes the same table as SNS subscription
filter policies. If they ever drift, `infra/check_routing.py` fails loudly.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field, asdict

# ── Event types ───────────────────────────────────────────────────────────────
# Past tense, always. An event is something that already happened; a command is
# something you want done. Commands couple the sender to the receiver, events
# do not — which is what lets us add a Docs or Security agent later by adding a
# subscription instead of editing the orchestrator.

RUN_REQUESTED   = "run.requested"     # a human dropped a file and hit Run
TICKET_CREATED  = "ticket.created"    # PM finished triage
PATCH_SUBMITTED = "patch.submitted"   # Dev finished a patch
QA_PASSED       = "qa.passed"         # QA verified the patch
QA_FAILED       = "qa.failed"         # QA rejected it — carries the reason
RETRY_REQUESTED = "retry.requested"   # supervisor spent a retry and re-tasked Dev
RUN_COMPLETED   = "run.completed"     # terminal: shipped
RUN_ESCALATED   = "run.escalated"     # terminal: retry budget spent, needs a human

ALL_EVENTS = [
    RUN_REQUESTED, TICKET_CREATED, PATCH_SUBMITTED,
    QA_PASSED, QA_FAILED, RETRY_REQUESTED, RUN_COMPLETED, RUN_ESCALATED,
]

TERMINAL_EVENTS = {RUN_COMPLETED, RUN_ESCALATED}

# ── Routing table ─────────────────────────────────────────────────────────────
# Who wakes up for what.
#
# Note what Dev does NOT subscribe to: qa.failed. A rejection goes to the
# supervisor, which checks the retry budget and only then emits
# retry.requested. If Dev listened to qa.failed directly it would retry
# forever, because the budget is policy and policy lives one tier up. Dev's
# code still does not branch on which event woke it — a retry is just a ticket
# with feedback attached — so the loop costs no extra logic in the worker.

SUBSCRIPTIONS: dict[str, list[str]] = {
    "pm":         [RUN_REQUESTED],
    "dev":        [TICKET_CREATED, RETRY_REQUESTED],
    "qa":         [PATCH_SUBMITTED],
    # The supervisor tails everything: it owns the retry budget, the terminal
    # states and the UI projection. It is the only subscriber that needs the
    # whole stream, which is exactly why it is the only one that is stateful.
    "supervisor": list(ALL_EVENTS),
}


def subscribers_for(event_type: str) -> list[str]:
    """Which queues an event fans out to. Mirrors the SNS filter policies."""
    return [name for name, types in SUBSCRIPTIONS.items() if event_type in types]


# ── The envelope ──────────────────────────────────────────────────────────────

@dataclass
class AgentEvent:
    """
    One message on the bus.

    `run_id` groups every event for a single bug-fix run — it is the partition
    key in DynamoDB, the S3 key prefix, and the MessageGroupId if you move to a
    FIFO topic. `attempt` is what the supervisor counts against the budget.
    """

    type: str
    run_id: str
    attempt: int = 1
    payload: dict = field(default_factory=dict)
    source: str = "supervisor"          # which agent emitted it
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts: float = field(default_factory=time.time)

    # ── serialisation ──

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, raw: str) -> "AgentEvent":
        return cls(**json.loads(raw))

    # ── SNS plumbing ──

    def message_attributes(self) -> dict:
        """
        Attributes SNS filter policies match on.

        This is the important bit for cost: filtering happens *at the topic*,
        so a queue only ever receives messages it asked for. The alternative —
        fan out everything and let each consumer discard what it does not want —
        pays SQS request charges and Lambda invocations for messages that are
        thrown away immediately.
        """
        return {
            "event_type": {"DataType": "String", "StringValue": self.type},
            "run_id":     {"DataType": "String", "StringValue": self.run_id},
            "source":     {"DataType": "String", "StringValue": self.source},
        }

    def describe(self) -> str:
        return f"{self.type}[run={self.run_id[:8]} attempt={self.attempt} from={self.source}]"


def new_run_id() -> str:
    """Short, sortable-ish, and safe in an S3 key or a queue attribute."""
    return f"{int(time.time())}-{uuid.uuid4().hex[:6]}"
