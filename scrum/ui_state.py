"""
ui_state.py — projects the run onto workspace/state.json for the office UI.

This is a read model, in the CQRS sense. The bus carries events; this collapses
them into "what should the screen show right now". Keeping it separate means
the agents never think about the UI and the UI never learns about the bus.

The field names are a contract with ui/app.js — `agent`, `status`, `message`,
`attempt`, `verdict`, `ts` — so they are frozen. Anything new goes in as an
extra key, which the existing UI ignores harmlessly.
"""

from __future__ import annotations

import json
import os
import threading
import time

# Relative by default, which is correct locally and wrong in Lambda — the
# working directory there is /var/task and it is read-only. SCRUM_UI_STATE_FILE
# overrides it; the template leaves it unset so _writable() disables writing
# entirely and the state goes to the log instead.
STATE_FILE = os.environ.get("SCRUM_UI_STATE_FILE", os.path.join("workspace", "state.json"))
_LOCK = threading.Lock()

# None = not yet determined. Set to False the first time a write fails, so a
# read-only filesystem costs one failed attempt per process, not one per event.
_CAN_WRITE: bool | None = None

# Seeded by the runtime at startup so the UI can show which backend won the
# probe. Purely informational.
_BACKEND = {"mode": "local", "reason": ""}


def set_backend(mode: str, reason: str) -> None:
    _BACKEND["mode"] = mode
    _BACKEND["reason"] = reason


def set_state(agent: str, status: str, message: str = "", attempt: int = 1,
              verdict: str = "", **extra) -> dict:
    """
    Write the current pipeline state.

    Written whole and atomically: the UI polls this file roughly once a second
    and a torn read would show it a half-written JSON document. os.replace is
    atomic on POSIX, so a reader sees either the old file or the new one.

    Args:
        agent:   "idle" | "pm" | "dev" | "qa" | "done" — drives which character
                 is lit up in the office.
        status:  short label for the chat bubble above that character.
        message: longer line for the log panel.
        attempt: current Dev/QA cycle, shown as the attempt pill.
        verdict: "pass" | "fail" | "" — controls the green/red flash on QA.
    """
    state = {
        "agent": agent,
        "status": status,
        "message": message,
        "attempt": attempt,
        "verdict": verdict,
        "ts": time.time(),
        "backend": _BACKEND["mode"],
        "backend_reason": _BACKEND["reason"],
        **extra,
    }
    _write(state)
    return state


def _write(state: dict) -> None:
    """
    Persist the read model, if we can.

    This is a projection for a UI that may not exist — in Lambda nobody is
    polling a local file, and the filesystem is read-only anyway. So a failure
    here must never propagate: it would take down an agent handler that had
    already done its real work, the message would go back on the queue, and the
    whole expensive turn would run again to update a cosmetic file.

    When writing is impossible we emit one JSON line instead, which is what you
    actually want in CloudWatch.
    """
    global _CAN_WRITE

    if _CAN_WRITE is False:
        print(json.dumps({"level": "info", "ui_state": state}))
        return

    try:
        with _LOCK:
            directory = os.path.dirname(STATE_FILE)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(state, fh, indent=2)
            os.replace(tmp, STATE_FILE)
        _CAN_WRITE = True
    except OSError as exc:
        if _CAN_WRITE is None:
            print(f"[ui_state] {STATE_FILE} is not writable ({exc.strerror}) — "
                  f"logging state instead. This is expected in Lambda.")
        _CAN_WRITE = False
        print(json.dumps({"level": "info", "ui_state": state}))


def reset() -> None:
    set_state("idle", "waiting...", "Press RUN to start")


# ── live ReAct narration ──────────────────────────────────────────────────────

# Short, readable labels for the chat bubbles. Raw tool names are accurate but
# "run_python" reads worse above a cartoon head than "running the code".
_TOOL_LABELS = {
    "read_source": "reading the code",
    "list_workspace": "checking the workspace",
    "static_check": "checking it parses",
    "run_python": "running the code",
    "write_patch": "writing the fix",
    "diff_patch": "reviewing the diff",
}


def step_reporter(attempt_getter):
    """
    Build an `on_step` callback for a ReActAgent.

    Every tool call becomes a UI update, which is what makes the office show
    genuine activity instead of a spinner: you watch Jim call write_patch, then
    run_python, then write_patch again when it fails.
    """

    def on_step(agent_name: str, step) -> None:
        label = _TOOL_LABELS.get(step.action, step.action or "thinking")
        set_state(
            agent_name,
            label,
            message=(step.thought or "")[:240],
            attempt=attempt_getter(),
            verdict="",
            step=step.index,
            tool=step.action,
        )

    return on_step
