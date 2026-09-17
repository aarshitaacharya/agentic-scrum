"""
agents/reflection.py — the inner self-correction loop (Reflexion-style).

There are two feedback loops in this system and they do different jobs:

  outer  Dev -> QA -> Dev.  Independent review. Costs a full round trip through
         the bus and burns one of the three attempts.

  inner  Dev -> Dev.  The agent grades its own work against a checklist before
         submitting. Cheap, catches the obvious ("I never actually ran it"),
         and keeps the expensive outer loop for genuine disagreement.

Reflection is only worth it when the critic can be concrete, so `critique`
is handed the evidence — the tool observations from the agent's own run — and
is told to answer REVISE or ACCEPT with a reason. A vague "could be better"
is explicitly rejected, because acting on it just burns tokens.

Note who reflects: Dev does, QA does not. A reviewer that talks itself out of
its own verdict is worse than no reviewer.
"""

from __future__ import annotations

from dataclasses import dataclass

from langchain_core.messages import HumanMessage, SystemMessage

from scrum.agents.react import _as_text

CRITIC_SYSTEM = """You are reviewing your own work before you submit it.

Be specific and be honest. You are looking for concrete, checkable defects:
a bug in the ticket that you did not actually fix, a change you made that the
ticket never asked for, a claim you made without running anything to support it.

Answer in exactly this format:

Verdict: ACCEPT
(or)
Verdict: REVISE
Problems:
- <one concrete, checkable problem>
- <another>

Rules:
- ACCEPT unless you can name a specific defect. "It could be clearer" is not a
  defect and must not trigger a revision.
- Never list more than three problems. Pick the ones that would fail review."""


@dataclass
class Critique:
    needs_revision: bool
    notes: str
    raw: str


def critique(llm, task: str, draft: str, evidence: str = "",
             checklist: str = "") -> Critique:
    """Grade a draft against the task. Returns ACCEPT, or REVISE plus reasons."""
    sections = [
        f"--- THE TASK YOU WERE GIVEN ---\n{task}",
        f"--- WHAT YOU PRODUCED ---\n{draft}",
    ]
    if evidence:
        sections.append(
            "--- WHAT YOU ACTUALLY OBSERVED ---\n"
            "(these are the real tool outputs from your run; if your output claims "
            "something these do not support, that is a defect)\n" + evidence
        )
    if checklist:
        sections.append(f"--- CHECK SPECIFICALLY FOR ---\n{checklist}")

    try:
        reply = llm.invoke([
            SystemMessage(content=CRITIC_SYSTEM),
            HumanMessage(content="\n\n".join(sections)),
        ])
        raw = _as_text(reply.content)
    except Exception as exc:  # noqa: BLE001
        # A failed critique must not fail the run — accept and move on. The
        # outer QA loop is still there to catch whatever we missed.
        return Critique(False, "", f"critique unavailable: {exc}")

    needs_revision = "Verdict: REVISE" in raw or "Verdict:REVISE" in raw
    notes = ""
    if "Problems:" in raw:
        notes = raw.split("Problems:", 1)[1].strip()
    return Critique(needs_revision, notes, raw)


def evidence_from(result) -> str:
    """
    Condense a ReAct run into what the critic needs: which tools ran and what
    came back. The reasoning is left out on purpose — we want the critic
    checking against reality, not agreeing with the original chain of thought.
    """
    lines = []
    for step in result.steps:
        if not step.action:
            continue
        observation = step.observation
        if len(observation) > 600:
            observation = observation[:600] + " ...[truncated]"
        lines.append(f"[{step.action}({step.action_input.splitlines()[0][:60] if step.action_input else ''})]\n{observation}")
    return "\n\n".join(lines) or "(no tools were called — this is itself a defect if the task needed evidence)"
