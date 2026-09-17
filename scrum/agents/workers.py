"""
agents/workers.py — the three agents, their tool belts, and their turns.

This is the bottom tier of the hierarchy. Each worker is a ReAct agent with a
role prompt and a *subset* of the tool belt; none of them knows the others
exist. They are handed a task, they loop, they return text. Deciding who runs
next, how many attempts are left and when to give up belongs to the supervisor.

Tool allocation is the part worth arguing about:

    PM   read_source, list_workspace, static_check, run_python
         Read and execute, no write. It diagnoses; it must not fix.

    Dev  read_source, static_check, run_python, write_patch, diff_patch
         The only writer in the system.

    QA   read_source, static_check, run_python, diff_patch
         Execute but not write. A reviewer able to edit the code under review
         will quietly patch small defects instead of failing them, and the
         independent check disappears.

Those restrictions are enforced by what is in the dict, not by asking the model
nicely in a prompt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from scrum.agents.dev_prompt import DEV_SELF_CHECK, DEV_SYSTEM_PROMPT, DEV_TASK_TEMPLATE
from scrum.agents.pm_prompt import PM_SYSTEM_PROMPT, PM_TASK_TEMPLATE
from scrum.agents.qa_prompt import QA_SYSTEM_PROMPT, QA_TASK_TEMPLATE
from scrum.agents.react import ReActAgent, ReActResult
from scrum.agents.reflection import critique, evidence_from
from scrum.agents.tools import Sandbox, build_toolbelt
from scrum.store.base import BUGGY_SCRIPT, PATCHED

TOOLS_FOR = {
    "pm":  ["read_source", "list_workspace", "static_check", "run_python"],
    "dev": ["read_source", "static_check", "run_python", "write_patch", "diff_patch"],
    "qa":  ["read_source", "static_check", "run_python", "diff_patch"],
}

ROLE_PROMPTS = {
    "pm":  PM_SYSTEM_PROMPT,
    "dev": DEV_SYSTEM_PROMPT,
    "qa":  QA_SYSTEM_PROMPT,
}


@dataclass
class AgentContext:
    """Everything an agent turn needs, assembled once per run."""

    run_id: str
    llm: object
    sandbox: Sandbox
    settings: object
    on_step: object = None          # callback(agent_name, ReActStep)
    toolbelt: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.toolbelt:
            self.toolbelt = build_toolbelt(
                self.sandbox,
                timeout=self.settings.tool_timeout,
                allow_exec=True,
            )

    def agent(self, name: str) -> ReActAgent:
        return ReActAgent(
            name=name,
            role_prompt=ROLE_PROMPTS[name],
            tools={t: self.toolbelt[t] for t in TOOLS_FOR[name]},
            llm=self.llm,
            max_steps=self.settings.max_react_steps,
            on_step=self.on_step,
        )


@dataclass
class AgentOutcome:
    """What a turn produced, plus enough detail to explain how it got there."""

    agent: str
    output: str
    result: ReActResult
    elapsed: float
    revised: bool = False           # did self-reflection trigger a second pass?
    critique_notes: str = ""

    @property
    def tool_calls(self) -> int:
        return self.result.tool_calls


# ── PM ────────────────────────────────────────────────────────────────────────

def run_pm(ctx: AgentContext) -> AgentOutcome:
    """Triage: read the file, run it, write a ticket."""
    started = time.time()
    result = ctx.agent("pm").run(PM_TASK_TEMPLATE.format(filename=BUGGY_SCRIPT))
    return AgentOutcome("pm", result.output, result, time.time() - started)


# ── Dev ───────────────────────────────────────────────────────────────────────

def run_dev(ctx: AgentContext, ticket: str, attempt: int) -> AgentOutcome:
    """
    Patch the code, then grade the patch against the ticket before handing off.

    The reflection pass is the inner loop. If the critic names a concrete
    defect we re-enter the ReAct loop with that objection attached, which is
    far cheaper than discovering the same thing through a full QA round trip
    that also costs one of the three attempts.
    """
    started = time.time()
    agent = ctx.agent("dev")
    task = DEV_TASK_TEMPLATE.format(
        attempt=attempt,
        max_attempts=ctx.settings.max_attempts,
        ticket=ticket,
        original=BUGGY_SCRIPT,
    )
    result = agent.run(task)

    verdict = critique(
        ctx.llm,
        task=task,
        draft=result.output,
        evidence=evidence_from(result),
        checklist=DEV_SELF_CHECK,
    )

    revised = False
    if verdict.needs_revision and verdict.notes:
        revised = True
        retry_task = (
            f"{task}\n\n--- YOUR OWN REVIEW OF YOUR FIRST ATTEMPT ---\n"
            f"You looked back at your patch and found these problems:\n{verdict.notes}\n\n"
            "Fix them. Save the corrected file with write_patch and run it again."
        )
        result = agent.run(retry_task)

    return AgentOutcome("dev", result.output, result, time.time() - started,
                        revised=revised, critique_notes=verdict.notes)


# ── QA ────────────────────────────────────────────────────────────────────────

def run_qa(ctx: AgentContext, ticket: str, dev_summary: str, attempt: int) -> AgentOutcome:
    """Verify the patch. No self-reflection here — see the module docstring."""
    started = time.time()
    task = QA_TASK_TEMPLATE.format(
        attempt=attempt,
        max_attempts=ctx.settings.max_attempts,
        ticket=ticket,
        patched=PATCHED,
        dev_summary=dev_summary,
    )
    result = ctx.agent("qa").run(task)
    return AgentOutcome("qa", result.output, result, time.time() - started)


def qa_passed(review_text: str) -> bool:
    """
    Read QA's verdict.

    Defaults to FAIL. If the agent ran out of steps, crashed, or produced
    something unparseable, we did not get a pass — and treating an absent
    verdict as approval is how broken code ships.
    """
    normalised = review_text.replace("**", "").replace("Verdict:PASS", "Verdict: PASS")
    if "Verdict: PASS" in normalised:
        return True
    return False
