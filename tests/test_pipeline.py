"""
End-to-end: does an event actually travel PM -> Dev -> QA and back?

These run the real bus, the real handlers, the real ReAct loop and the real
tools — including genuinely executing the patched Python. Only the model is
scripted.
"""

import json

import pytest

from scrum.bus.local import LocalEventBus
from scrum.events import (
    PATCH_SUBMITTED, QA_FAILED, QA_PASSED, RETRY_REQUESTED,
    RUN_COMPLETED, RUN_ESCALATED, TICKET_CREATED, new_run_id,
)
from scrum.runtime import LocalRuntime


def _run(settings, qa_always_fails=False):
    bus = LocalEventBus(visibility_timeout=60, max_receives=3, log_path=None)
    runtime = LocalRuntime(bus=bus, settings=settings)
    runtime.start()
    try:
        outcome = runtime.run_to_completion(new_run_id(), timeout=60)
    finally:
        runtime.stop()
    return outcome, bus


@pytest.fixture
def settings():
    from scrum.config import Settings

    return Settings(backend="local", max_attempts=3, max_react_steps=8, tool_timeout=10)


def test_a_failed_review_costs_one_attempt_and_then_passes(workspace, scripted_llm, settings):
    scripted_llm()
    outcome, _ = _run(settings)

    assert outcome["verdict"] == "pass"
    assert outcome["attempts"] == 2, "QA failed once, so the fix should land on attempt 2"
    assert outcome["errors"] == []


def test_the_patch_is_written_and_actually_runs(workspace, scripted_llm, settings):
    scripted_llm()
    _run(settings)

    patched = workspace / "patched_script.py"
    assert patched.exists()

    import subprocess
    import sys

    result = subprocess.run([sys.executable, str(patched)], capture_output=True, text=True)
    assert result.returncode == 0
    assert "average: 85.0" in result.stdout, "the bug is genuinely fixed, not just claimed fixed"


def test_the_retry_budget_is_enforced(workspace, scripted_llm, settings):
    """QA never passes: the supervisor must give up at max_attempts, not loop."""
    scripted_llm(qa_always_fails=True)
    outcome, _ = _run(settings)

    assert outcome["verdict"] == "fail"
    assert outcome["attempts"] == settings.max_attempts
    assert "needs a human" in outcome["reason"]


def test_the_handoff_sequence_is_what_we_think_it_is(workspace, scripted_llm, settings, tmp_path):
    """Pin the event order, since that IS the architecture."""
    log = tmp_path / "events.jsonl"
    bus = LocalEventBus(visibility_timeout=60, max_receives=3, log_path=str(log))
    runtime = LocalRuntime(bus=bus, settings=settings)
    scripted_llm()
    runtime.start()
    try:
        runtime.run_to_completion(new_run_id(), timeout=60)
    finally:
        runtime.stop()

    published = [json.loads(line)["type"] for line in log.read_text().splitlines()]
    assert published == [
        "run.requested",
        TICKET_CREATED,
        PATCH_SUBMITTED,
        QA_FAILED,        # QA rejects attempt 1
        RETRY_REQUESTED,  # the supervisor — not QA — decides to retry
        PATCH_SUBMITTED,
        QA_PASSED,
        RUN_COMPLETED,
    ]


def test_escalation_is_terminal(workspace, scripted_llm, settings, tmp_path):
    log = tmp_path / "events.jsonl"
    bus = LocalEventBus(visibility_timeout=60, max_receives=3, log_path=str(log))
    runtime = LocalRuntime(bus=bus, settings=settings)
    scripted_llm(qa_always_fails=True)
    runtime.start()
    try:
        runtime.run_to_completion(new_run_id(), timeout=90)
    finally:
        runtime.stop()

    published = [json.loads(line)["type"] for line in log.read_text().splitlines()]
    assert published[-1] == RUN_ESCALATED
    assert published.count(RETRY_REQUESTED) == settings.max_attempts - 1
    assert published.count(PATCH_SUBMITTED) == settings.max_attempts


def test_the_ui_state_file_stays_readable_throughout(workspace, scripted_llm, settings):
    """The UI polls state.json ~1x/second; a torn read would break the office."""
    scripted_llm()
    _run(settings)

    state = json.loads((workspace / "state.json").read_text())
    assert state["agent"] == "done"
    assert state["verdict"] == "pass"
    assert set(state) >= {"agent", "status", "message", "attempt", "verdict", "ts"}
