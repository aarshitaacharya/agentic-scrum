"""
runtime.py — the local adapter. One thread per queue, where AWS has one Lambda.

This is the fallback's execution half. bus/local.py replaces SNS and SQS; this
replaces Lambda's event-source mapping — the bit of AWS that polls a queue,
hands each message to your function, and deletes it if the function returns
without raising.

The loop below does exactly that, in about forty lines:

    receive -> handler -> publish what it returned -> ack
    handler raised? -> nack, so it redelivers and eventually lands in the DLQ

Which means the failure modes are the same ones you get in production. A
handler that throws does not lose its message here either.

Threads, not processes, because the agents are I/O-bound — they spend their
lives waiting on the Gemini API — and threads share the LangChain client and
the in-process bus for free. If the work were CPU-bound this would be the wrong
choice, but it is not.
"""

from __future__ import annotations

import threading
import time

from scrum.bus import get_bus
from scrum.config import SETTINGS, resolve_backends
from scrum.events import (
    AgentEvent, RUN_REQUESTED, TERMINAL_EVENTS, SUBSCRIPTIONS, new_run_id,
)
from scrum.handlers import AGENT_HANDLERS
from scrum.store import get_artifact_store
from scrum.store.base import BUGGY_SCRIPT
from scrum import ui_state


class LocalRuntime:
    """Runs every agent in one process, driven by the bus."""

    def __init__(self, bus=None, settings=SETTINGS):
        self.settings = settings
        self.bus = bus or get_bus(settings)
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._terminal = threading.Event()
        self._outcome: dict = {}
        self._errors: list[str] = []

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        self._stop.clear()
        self._terminal.clear()
        for name in SUBSCRIPTIONS:
            thread = threading.Thread(target=self._worker, args=(name,),
                                      name=f"agent-{name}", daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=2)
        self._threads.clear()

    # ── the event-source mapping ──────────────────────────────────────────────

    def _worker(self, name: str) -> None:
        handler = AGENT_HANDLERS[name]
        while not self._stop.is_set():
            try:
                received = self.bus.receive(name, wait_seconds=1, max_messages=1)
            except Exception as exc:  # noqa: BLE001 — a dead bus should not spin hot
                self._errors.append(f"{name}: receive failed: {exc}")
                time.sleep(1)
                continue

            for message in received:
                self._dispatch(name, handler, message)

    def _dispatch(self, name: str, handler, message) -> None:
        event = message.event
        if message.receive_count > 1:
            print(f"[{name}] redelivery #{message.receive_count} of {event.describe()}")

        try:
            emitted = handler(event, self.settings) or []
        except Exception as exc:  # noqa: BLE001
            # Do NOT ack. The message becomes visible again after the
            # visibility timeout and, if it keeps failing, ends up in the DLQ —
            # same as Lambda.
            self._errors.append(f"{name}: {type(exc).__name__}: {exc}")
            print(f"[{name}] handler raised {type(exc).__name__}: {exc} — leaving it for redelivery")
            self.bus.nack(message)
            return

        for outgoing in emitted:
            self.bus.publish(outgoing)
        self.bus.ack(message)

        # The supervisor handling a terminal event is what ends the run.
        if name == "supervisor" and event.type in TERMINAL_EVENTS:
            self._outcome = {
                "verdict": "pass" if event.payload.get("verdict") == "pass" else "fail",
                "attempts": event.attempt,
                "reason": event.payload.get("reason", ""),
                "run_id": event.run_id,
            }
            self._terminal.set()

    # ── driving a run ─────────────────────────────────────────────────────────

    def run_to_completion(self, run_id: str, timeout: float = 600) -> dict:
        """Publish run.requested and block until the supervisor calls it done."""
        self.bus.publish(AgentEvent(
            type=RUN_REQUESTED, run_id=run_id, attempt=1, source="human",
            payload={"artifact": BUGGY_SCRIPT},
        ))

        if not self._terminal.wait(timeout=timeout):
            return {"verdict": "fail", "attempts": 0, "run_id": run_id,
                    "reason": f"timed out after {timeout:.0f}s",
                    "errors": self._errors}

        return {**self._outcome, "errors": self._errors}


# ── the entry point everything else calls ─────────────────────────────────────

def run_pipeline(settings=SETTINGS, timeout: float = 600, quiet: bool = False) -> dict:
    """
    Run one full PM -> Dev -> QA cycle over whatever is in the workspace.

    Called by orchestrator.py from the CLI and by server.py from the UI's Run
    button. On AWS you would not call this at all — you would publish
    run.requested to the topic and let the Lambdas pick it up — which is what
    `publish_run_request()` below does.
    """
    decision = resolve_backends(settings)
    ui_state.set_backend(decision.mode, decision.reason)

    if not quiet:
        print("\n=== AGENTIC SCRUM ===")
        print(decision.banner())
        print()

    store = get_artifact_store(settings)
    run_id = new_run_id()

    if not store.exists(run_id, BUGGY_SCRIPT):
        ui_state.set_state("idle", "no file", "Drop a .py file first", 1, verdict="fail")
        return {"verdict": "fail", "run_id": run_id,
                "reason": f"no {BUGGY_SCRIPT} in the workspace — drop a file first"}

    ui_state.reset()

    runtime = LocalRuntime(settings=settings)
    runtime.start()
    try:
        outcome = runtime.run_to_completion(run_id, timeout=timeout)
    finally:
        runtime.stop()

    if not quiet:
        print("=" * 46)
        verdict = outcome.get("verdict")
        if verdict == "pass":
            print(f"PASS — fixed in {outcome.get('attempts')} attempt(s).")
        else:
            print(f"FAIL — {outcome.get('reason') or 'QA never passed it'}")
        print(f"bus: {runtime.bus.stats()}")
        print("=" * 46 + "\n")

    return outcome


def publish_run_request(settings=SETTINGS) -> str:
    """
    Fire-and-forget: put run.requested on the topic and return.

    This is the AWS-shaped entry point. Nothing waits for a result — the
    Lambdas take it from here and the UI finds out by polling. Same call works
    locally; the local runtime just has to already be running to hear it.
    """
    run_id = new_run_id()
    get_bus(settings).publish(AgentEvent(
        type=RUN_REQUESTED, run_id=run_id, attempt=1, source="human",
        payload={"artifact": BUGGY_SCRIPT},
    ))
    return run_id
