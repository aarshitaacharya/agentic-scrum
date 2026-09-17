"""
lambda_handlers.py — the AWS adapter.

One Lambda per agent, each triggered by its own SQS queue. Every function here
is a thin wrapper that unwraps the SQS/SNS envelope, calls the *same* handler
runtime.py calls locally, and publishes whatever comes back.

    SQS record -> AgentEvent -> handlers.handle_dev(event) -> [AgentEvent] -> SNS

The logic is in handlers.py. If you find yourself adding business rules to this
file, they are in the wrong place — that is exactly the coupling the split is
there to prevent.

Three things here are not obvious and matter in production:

1. Partial batch failures. An event source mapping hands you up to 10 messages.
   If the handler raises, Lambda retries the WHOLE batch by default — so nine
   successful agent turns get re-run because the tenth failed, at nine times
   the token cost. Returning `batchItemFailures` (with
   ReportBatchItemFailures enabled on the mapping, which template.yaml does)
   makes only the failed message redeliver.

2. Clients are built at module scope, not per invocation. Module scope runs
   once per cold start and is reused by every warm invocation after it;
   building a boto3 client per request adds tens of milliseconds and a pile of
   avoidable TLS handshakes.

3. Timeouts. An agent turn is several sequential LLM calls, so these functions
   need a multi-minute timeout — and the queue's visibility timeout must be
   longer than the function timeout, or SQS hands the same message to a second
   Lambda while the first is still thinking. template.yaml sets 300s / 360s.
"""

from __future__ import annotations

import json
import os

from scrum.config import SETTINGS
from scrum.events import AgentEvent
from scrum.handlers import AGENT_HANDLERS

# Cold-start scope: built once, reused by every warm invocation.
_BUS = None


def _bus():
    global _BUS
    if _BUS is None:
        from scrum.bus import get_bus

        _BUS = get_bus(SETTINGS)
    return _BUS


# ── envelope unwrapping ───────────────────────────────────────────────────────

def _parse_record(record: dict) -> AgentEvent | None:
    """
    Turn one SQS record into an AgentEvent.

    The body is our JSON if the SNS subscription has RawMessageDelivery on, or
    an SNS envelope wrapping it if not. Accept both — a subscription someone
    created by hand in the console defaults to off, and silently dropping every
    message is a miserable afternoon.
    """
    body = record.get("body", "")
    try:
        outer = json.loads(body)
    except json.JSONDecodeError:
        return None

    if isinstance(outer, dict) and "Message" in outer and "TopicArn" in outer:
        try:
            return AgentEvent.from_json(outer["Message"])
        except (json.JSONDecodeError, TypeError):
            return None
    try:
        return AgentEvent(**outer)
    except TypeError:
        return None


def _process(agent: str, event: dict, context) -> dict:
    """
    Shared body for all four agent Lambdas.

    Returns the partial-batch-failure response. A message whose handler raised
    is reported as failed and redelivers on its own; the rest are deleted.
    """
    handler = AGENT_HANDLERS[agent]
    failures: list[dict] = []

    for record in event.get("Records", []):
        message_id = record.get("messageId", "")
        parsed = _parse_record(record)

        if parsed is None:
            # Unparseable. Do NOT report it as a failure: it will never parse,
            # so retrying just burns invocations until the DLQ catches it. Let
            # it be deleted and log loudly instead.
            print(json.dumps({"level": "error", "agent": agent,
                              "msg": "undecodable record", "messageId": message_id}))
            continue

        receive_count = int(record.get("attributes", {}).get("ApproximateReceiveCount", 1))
        print(json.dumps({
            "level": "info", "agent": agent, "event": parsed.type,
            "run_id": parsed.run_id, "attempt": parsed.attempt,
            "receive_count": receive_count,
            "remaining_ms": context.get_remaining_time_in_millis() if context else None,
        }))

        try:
            for outgoing in handler(parsed, SETTINGS) or []:
                _bus().publish(outgoing)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"level": "error", "agent": agent, "run_id": parsed.run_id,
                              "error": f"{type(exc).__name__}: {exc}"}))
            failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": failures}


# ── the four functions ────────────────────────────────────────────────────────

def pm_handler(event, context):
    """Triggered by agentic-scrum-pm. Subscribed to: run.requested."""
    return _process("pm", event, context)


def dev_handler(event, context):
    """Triggered by agentic-scrum-dev. Subscribed to: ticket.created, retry.requested."""
    return _process("dev", event, context)


def qa_handler(event, context):
    """Triggered by agentic-scrum-qa. Subscribed to: patch.submitted."""
    return _process("qa", event, context)


def supervisor_handler(event, context):
    """
    Triggered by agentic-scrum-supervisor. Subscribed to: everything.

    Set this function's reserved concurrency to 1. It is the only stateful
    consumer, and serialising it turns the conditional-write contention in
    _spend_retry from a common case into a rare one.
    """
    return _process("supervisor", event, context)


# ── kicking off a run over HTTP ───────────────────────────────────────────────

def start_run_handler(event, context):
    """
    API Gateway -> publish run.requested -> return immediately.

    This is the async story in one function. The caller gets a run_id in
    milliseconds instead of holding an HTTP connection open for the two or
    three minutes the agents need; it then polls for the result. Doing it
    synchronously would mean an API Gateway integration timeout (29s hard cap)
    killing the request long before QA ever weighs in.
    """
    from scrum.events import RUN_REQUESTED, new_run_id
    from scrum.store.base import BUGGY_SCRIPT

    body = {}
    if event.get("body"):
        try:
            body = json.loads(event["body"])
        except json.JSONDecodeError:
            pass

    run_id = body.get("run_id") or new_run_id()
    _bus().publish(AgentEvent(
        type=RUN_REQUESTED, run_id=run_id, attempt=1, source="api",
        payload={"artifact": body.get("artifact", BUGGY_SCRIPT)},
    ))

    return {
        "statusCode": 202,          # Accepted: work started, not finished
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"run_id": run_id, "status": "accepted",
                            "poll": f"/runs/{run_id}"}),
    }


def get_run_handler(event, context):
    """API Gateway -> read run state from DynamoDB. This is what the UI polls."""
    from scrum.store import get_run_state_store

    run_id = (event.get("pathParameters") or {}).get("run_id", "")
    state = get_run_state_store(SETTINGS).get(run_id) if run_id else {}

    return {
        "statusCode": 200 if state else 404,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(state or {"error": "unknown run"}),
    }
