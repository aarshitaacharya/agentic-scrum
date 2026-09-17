"""
handlers.py — what each agent does when an event wakes it up.

These four functions are the only place the business logic lives, and they know
nothing about how they were invoked. Each one takes an event and returns the
events it wants published; it never publishes directly and never touches a
queue. That is what lets the same code run two very different ways:

    local   runtime.py polls a deque, calls the handler, publishes the result
    AWS     lambda_handlers.py unwraps an SQS record, calls the same handler,
            publishes the result

Swapping the transport swaps a ~20-line adapter. The agents do not notice.

Handlers must also be idempotent, because both SQS and SNS deliver at least
once. Re-running a PM turn on a duplicate rewrites the same ticket to the same
key — wasteful, but not wrong. The one place duplication would actually hurt is
the retry counter, which is why that lives behind a conditional write in the
supervisor rather than in here.
"""

from __future__ import annotations

import json

from scrum.agents.tools import Sandbox
from scrum.agents.workers import AgentContext, qa_passed, run_dev, run_pm, run_qa
from scrum.config import SETTINGS
from scrum.events import (
    AgentEvent, PATCH_SUBMITTED, QA_FAILED, QA_PASSED, RETRY_REQUESTED,
    RUN_COMPLETED, RUN_ESCALATED, RUN_REQUESTED, TERMINAL_EVENTS, TICKET_CREATED,
)

# Once a run reaches one of these, further events for it are noise.
TERMINAL_STATUSES = {"passed", "escalated", "aborted"}
from scrum.llm import build_llm
from scrum.store import ConcurrentUpdate, get_artifact_store, get_run_state_store
from scrum.store.base import BUGGY_SCRIPT, PATCHED, QA_REVIEW, TICKET, TRACE
from scrum import ui_state


# ── shared setup ──────────────────────────────────────────────────────────────

def build_context(run_id: str, attempt: int = 1, settings=SETTINGS) -> AgentContext:
    """
    Assemble an agent's working environment for one turn.

    On Lambda this is genuinely per-invocation: the sandbox is hydrated from S3
    into /tmp, used, and flushed back. Locally the sandbox and the store are
    the same directory, so the sync calls are no-ops.
    """
    store = get_artifact_store(settings)
    sandbox = Sandbox(store, run_id)
    sandbox.pull([BUGGY_SCRIPT, PATCHED, TICKET])

    return AgentContext(
        run_id=run_id,
        llm=build_llm(settings),
        sandbox=sandbox,
        settings=settings,
        on_step=ui_state.step_reporter(lambda: attempt),
    )


def _trace(store, run_id: str, outcome) -> None:
    """Append the turn's ReAct steps to the run trace, for the transcript view."""
    for step in outcome.result.steps:
        store.append(run_id, TRACE, json.dumps({
            "agent": outcome.agent,
            **step.as_dict(),
        }))


# ── PM ────────────────────────────────────────────────────────────────────────

def handle_pm(event: AgentEvent, settings=SETTINGS) -> list[AgentEvent]:
    """run.requested -> read the file, run it, write a ticket."""
    store = get_artifact_store(settings)
    ctx = build_context(event.run_id, attempt=1, settings=settings)

    ui_state.set_state("pm", "reading the ticket queue", "Michael is triaging the file", 1)
    outcome = run_pm(ctx)

    store.write(event.run_id, TICKET, outcome.output)
    _trace(store, event.run_id, outcome)

    return [AgentEvent(
        type=TICKET_CREATED,
        run_id=event.run_id,
        attempt=1,
        source="pm",
        payload={
            # Claim check: the pointer travels, the payload stays in the store.
            # SNS caps a message at 256 KB and a ticket plus a source file will
            # exceed that sooner than you would like.
            "artifact": TICKET,
            "summary": _first_line(outcome.output, "Summary:"),
            "tool_calls": outcome.tool_calls,
            "elapsed": round(outcome.elapsed, 1),
        },
    )]


# ── Dev ───────────────────────────────────────────────────────────────────────

def handle_dev(event: AgentEvent, settings=SETTINGS) -> list[AgentEvent]:
    """ticket.created | retry.requested -> patch the code."""
    store = get_artifact_store(settings)
    attempt = event.attempt
    ctx = build_context(event.run_id, attempt=attempt, settings=settings)

    ticket = store.read_or(event.run_id, TICKET, "")

    # A retry is the same ticket with QA's objection stapled to it. Dev's
    # handler does not branch on the event type beyond this.
    if event.type == RETRY_REQUESTED:
        feedback = event.payload.get("feedback") or store.read_or(event.run_id, QA_REVIEW, "")
        ticket = (
            f"{ticket}\n\n--- QA REJECTED ATTEMPT {attempt - 1} ---\n{feedback}\n"
            "--- END QA FEEDBACK ---\n"
            "Address the QA objection above. It is the reason this came back."
        )

    ui_state.set_state("dev", "reading ticket", f"Attempt {attempt} — Jim picked up the ticket", attempt)
    outcome = run_dev(ctx, ticket, attempt)

    ctx.sandbox.push([PATCHED])
    _trace(store, event.run_id, outcome)

    if not store.exists(event.run_id, PATCHED):
        # Dev finished its loop without ever saving a file. Fail the attempt
        # honestly instead of sending QA a phantom patch to review.
        return [AgentEvent(
            type=QA_FAILED, run_id=event.run_id, attempt=attempt, source="dev",
            payload={"reason": "Dev produced no patch file", "self_inflicted": True},
        )]

    return [AgentEvent(
        type=PATCH_SUBMITTED,
        run_id=event.run_id,
        attempt=attempt,
        source="dev",
        payload={
            "artifact": PATCHED,
            "dev_summary": outcome.output,
            "self_revised": outcome.revised,
            "tool_calls": outcome.tool_calls,
            "elapsed": round(outcome.elapsed, 1),
        },
    )]


# ── QA ────────────────────────────────────────────────────────────────────────

def handle_qa(event: AgentEvent, settings=SETTINGS) -> list[AgentEvent]:
    """patch.submitted -> run it, review it, pass or fail it."""
    store = get_artifact_store(settings)
    attempt = event.attempt
    ctx = build_context(event.run_id, attempt=attempt, settings=settings)

    ticket = store.read_or(event.run_id, TICKET, "")
    dev_summary = event.payload.get("dev_summary", "(none given)")

    ui_state.set_state("qa", "reviewing patch", f"Attempt {attempt} — Dwight is checking the fix", attempt)
    outcome = run_qa(ctx, ticket, dev_summary, attempt)

    store.write(event.run_id, QA_REVIEW, outcome.output)
    _trace(store, event.run_id, outcome)

    passed = qa_passed(outcome.output)
    return [AgentEvent(
        type=QA_PASSED if passed else QA_FAILED,
        run_id=event.run_id,
        attempt=attempt,
        source="qa",
        payload={
            "artifact": QA_REVIEW,
            "reason": _first_line(outcome.output, "Reason:") if not passed else "",
            "review": outcome.output,
            "tool_calls": outcome.tool_calls,
            "elapsed": round(outcome.elapsed, 1),
        },
    )]


# ── Supervisor ────────────────────────────────────────────────────────────────

def handle_supervisor(event: AgentEvent, settings=SETTINGS) -> list[AgentEvent]:
    """
    The top tier. Owns the retry budget, the terminal states, and the UI.

    It is the only stateful consumer and the only one that decides *who runs
    next*. Workers below it just do the work they are handed — which is what
    makes this a hierarchy rather than three agents shouting at each other.
    """
    runs = get_run_state_store(settings)
    store = get_artifact_store(settings)

    # ── circuit breaker ──
    # Two cheap guards against a run that will not die. Neither should ever
    # fire; both exist because the failure mode they prevent is "AWS bills you
    # all night for a loop nobody is watching", and the DLQ does not catch it
    # (these messages are being handled *successfully*, just endlessly).
    state = runs.get(event.run_id)

    if state.get("status") in TERMINAL_STATUSES and event.type not in TERMINAL_EVENTS:
        # A late or duplicate event for a finished run. SNS delivers at least
        # once, so this is expected occasionally rather than alarming.
        print(f"[supervisor] ignoring {event.type} for finished run {event.run_id}")
        return []

    seen = state.get("events_seen", 0) + 1
    if seen > settings.max_events_per_run:
        if state.get("status") == "aborted":
            return []
        runs.put(event.run_id, {"status": "aborted", "verdict": "fail", "events_seen": seen})
        return [AgentEvent(
            type=RUN_ESCALATED, run_id=event.run_id, attempt=event.attempt, source="supervisor",
            payload={"reason": f"circuit breaker: more than {settings.max_events_per_run} "
                               f"events for one run — something is looping"},
        )]
    runs.put(event.run_id, {"events_seen": seen})

    if event.type == RUN_REQUESTED:
        runs.put(event.run_id, {"status": "running", "attempt": 1, "verdict": ""})
        ui_state.set_state("pm", "ticket received", "New file dropped — triage starting", 1)
        return []

    if event.type == TICKET_CREATED:
        ui_state.set_state("pm", "writing ticket", event.payload.get("summary", "Ticket filed"), 1)
        return []

    if event.type == PATCH_SUBMITTED:
        note = "patch written"
        if event.payload.get("self_revised"):
            note = "patch written (revised itself once)"
        ui_state.set_state("dev", note, f"Attempt {event.attempt} — handed to QA", event.attempt)
        return []

    if event.type == QA_PASSED:
        runs.put(event.run_id, {"status": "passed", "verdict": "pass", "attempt": event.attempt})
        ui_state.set_state("qa", "all tests passed!", f"Passed on attempt {event.attempt}",
                           event.attempt, verdict="pass")
        return [AgentEvent(type=RUN_COMPLETED, run_id=event.run_id, attempt=event.attempt,
                           source="supervisor", payload={"verdict": "pass"})]

    if event.type == QA_FAILED:
        return _spend_retry(event, runs, store, settings)

    if event.type == RETRY_REQUESTED:
        ui_state.set_state("qa", "FAILED — pinging Dev", "Dwight sent it back for rework",
                           event.attempt, verdict="fail")
        return []

    if event.type == RUN_COMPLETED:
        ui_state.set_state("done", "bug fixed!", f"Shipped in {event.attempt} attempt(s)",
                           event.attempt, verdict="pass")
        _archive(store, event.run_id)
        return []

    if event.type == RUN_ESCALATED:
        reason = event.payload.get("reason", "retry budget exhausted")
        ui_state.set_state("done", "gave up", reason, event.attempt, verdict="fail")
        _archive(store, event.run_id)
        return []

    return []


def _spend_retry(event: AgentEvent, runs, store, settings) -> list[AgentEvent]:
    """
    Decide whether a QA failure gets another attempt.

    The conditional write is the point. On AWS two events for one run can land
    in two concurrently-executing Lambdas; without it, both read attempt=1,
    both write attempt=2, and one retry silently vanishes. Losing the race is
    not an error — we re-read and re-decide.
    """
    for _ in range(3):
        state = runs.get(event.run_id)
        attempt = max(state.get("attempt", event.attempt), event.attempt)

        if attempt >= settings.max_attempts:
            try:
                runs.put(event.run_id, {"status": "escalated", "verdict": "fail", "attempt": attempt},
                         expected_version=state.get("version", 0))
            except ConcurrentUpdate:
                continue
            return [AgentEvent(
                type=RUN_ESCALATED, run_id=event.run_id, attempt=attempt, source="supervisor",
                payload={"reason": f"QA still failing after {attempt} attempts — needs a human",
                         "last_reason": event.payload.get("reason", "")},
            )]

        try:
            runs.put(event.run_id, {"attempt": attempt + 1, "verdict": "fail"},
                     expected_version=state.get("version", 0))
        except ConcurrentUpdate:
            continue   # someone else moved the counter; re-read and re-decide

        return [AgentEvent(
            type=RETRY_REQUESTED, run_id=event.run_id, attempt=attempt + 1, source="supervisor",
            payload={"feedback": event.payload.get("review", ""),
                     "rejected_attempt": attempt},
        )]

    # Three lost races in a row means something is badly wrong upstream.
    return [AgentEvent(
        type=RUN_ESCALATED, run_id=event.run_id, attempt=event.attempt, source="supervisor",
        payload={"reason": "could not update run state — concurrent writers"},
    )]


def _archive(store, run_id: str) -> None:
    if hasattr(store, "archive"):
        store.archive(run_id, [BUGGY_SCRIPT, TICKET, PATCHED, QA_REVIEW, TRACE])


def _first_line(text: str, marker: str) -> str:
    for line in text.splitlines():
        if line.strip().startswith(marker):
            return line.split(marker, 1)[1].strip()
    return ""


# The routing table the adapters use to find the right handler.
AGENT_HANDLERS = {
    "pm": handle_pm,
    "dev": handle_dev,
    "qa": handle_qa,
    "supervisor": handle_supervisor,
}
