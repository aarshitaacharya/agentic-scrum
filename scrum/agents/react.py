"""
agents/react.py — the ReAct loop, written out rather than imported.

ReAct (Yao et al., 2022) interleaves reasoning and acting: the model emits a
Thought, chooses an Action, and is handed back an Observation from the real
world before it thinks again. The loop below is that, literally:

    Thought      -> the model's reasoning about what to do next
    Action       -> a tool name
    Action Input -> the argument
    Observation  -> what the tool actually returned  (we write this, not the model)
    ... repeat until ...
    Final Answer -> the agent's output

Why hand-rolled instead of LangChain's prebuilt agent runner:

  1. Observability. Every step goes to `on_step`, which is how the office UI
     shows what each character is thinking and how trace.jsonl gets written.
     A prebuilt executor hands you the final answer and an opaque middle.
  2. Control. The retry budget, the self-reflection pass and the loop-breaking
     nudge below are policy that belongs to *this* system, not to a framework
     default I would then have to fight.
  3. It is ~120 lines. Understanding them beats configuring something I cannot
     debug at 2am.

It still builds on LangChain: tools are `StructuredTool`s with generated
schemas, the model is a LangChain chat model, and messages are LangChain
message types — so any chat model works and tools stay declarative.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

# We stop generation here so the model cannot hallucinate an Observation.
# Letting it invent tool output is the classic ReAct failure: it will happily
# imagine the tests passed.
STOP_SEQUENCES = ["\nObservation:", "\nObservation :"]

FORMAT_INSTRUCTIONS = """You work in a strict Thought / Action / Observation loop.

Available tools:
{tool_block}

Respond using EXACTLY this format:

Thought: <your reasoning about what to do next>
Action: <exactly one tool name from [{tool_names}]>
Action Input: <the input for that tool>

Then STOP and wait. An Observation with the real tool output will be given to
you. Never write the Observation yourself.

When you have finished the task, respond instead with:

Thought: <why you are done>
Final Answer: <your answer, in the format the task asked for>

Rules:
- One Action per message. Never more.
- Action Input may span multiple lines (for example, a whole source file).
  Everything after "Action Input:" is the input.
- Do not wrap Action Input in quotes, JSON or markdown fences.
- Base your conclusions on Observations, not on assumptions about what the
  code probably does. If you can run it, run it."""


@dataclass
class ReActStep:
    index: int
    thought: str
    action: str
    action_input: str
    observation: str
    elapsed: float = 0.0

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReActResult:
    agent: str
    output: str
    steps: list = field(default_factory=list)
    stop_reason: str = "final_answer"   # final_answer | max_steps | error
    elapsed: float = 0.0

    @property
    def tool_calls(self) -> int:
        return len([s for s in self.steps if s.action])


class ReActAgent:
    """One agent: a role, a tool belt, and a loop."""

    def __init__(self, name: str, role_prompt: str, tools: dict, llm,
                 max_steps: int = 8, on_step=None):
        self.name = name
        self.role_prompt = role_prompt
        self.tools = tools
        self.llm = llm
        self.max_steps = max_steps
        self.on_step = on_step          # callback(agent_name, ReActStep)

    # ── prompt ────────────────────────────────────────────────────────────────

    def _system_message(self) -> SystemMessage:
        tool_block = "\n".join(
            f"  {name}: {tool.description}" for name, tool in self.tools.items()
        )
        return SystemMessage(content=(
            self.role_prompt.strip()
            + "\n\n"
            + FORMAT_INSTRUCTIONS.format(
                tool_block=tool_block,
                tool_names=", ".join(self.tools),
            )
        ))

    # ── the loop ──────────────────────────────────────────────────────────────

    def run(self, task: str) -> ReActResult:
        started = time.time()
        messages = [self._system_message(), HumanMessage(content=task)]
        steps: list[ReActStep] = []
        seen_actions: list[tuple] = []

        for index in range(1, self.max_steps + 1):
            step_started = time.time()

            try:
                reply = self.llm.invoke(messages, stop=STOP_SEQUENCES)
                text = _as_text(reply.content)
            except Exception as exc:  # noqa: BLE001 — a dead model ends the turn
                return ReActResult(self.name, f"ERROR: model call failed: {exc}",
                                   steps, "error", time.time() - started)

            thought, action, action_input, final = _parse(text)

            # ── the agent says it is done ──
            if final is not None:
                if thought:
                    self._emit(ReActStep(index, thought, "", "", "(final answer)",
                                         time.time() - step_started), steps)
                return ReActResult(self.name, final.strip(), steps,
                                   "final_answer", time.time() - started)

            # ── unparseable: hand the error back and let it self-correct ──
            if action is None:
                observation = (
                    "PARSE ERROR: I could not find an Action in that reply. Respond with "
                    "either 'Action:' plus 'Action Input:', or 'Final Answer:'."
                )
                messages += [AIMessage(content=text), HumanMessage(content=f"Observation: {observation}")]
                self._emit(ReActStep(index, thought, "", "", observation,
                                     time.time() - step_started), steps)
                continue

            # ── loop breaker ──
            # A stuck agent repeats one action verbatim until it runs out of
            # steps. Naming the repetition is usually enough to unstick it, and
            # it costs one observation instead of five wasted LLM calls.
            signature = (action, action_input.strip())
            if seen_actions.count(signature) >= 2:
                observation = (
                    f"You have already called {action} with this exact input twice and the "
                    "result will not change. Either try a different tool or give your Final Answer."
                )
            else:
                observation = self._call_tool(action, action_input)
            seen_actions.append(signature)

            messages += [AIMessage(content=text), HumanMessage(content=f"Observation: {observation}")]
            self._emit(ReActStep(index, thought, action, action_input, observation,
                                 time.time() - step_started), steps)

        # ── budget spent ──
        # Ask for the best answer it has rather than returning nothing. A
        # partial ticket is worth more to the next agent than a timeout.
        messages.append(HumanMessage(content=(
            "You have run out of steps. Give your Final Answer now, based on what you "
            "have observed so far."
        )))
        try:
            salvage = _as_text(self.llm.invoke(messages).content)
            _, _, _, final = _parse(salvage)
            output = (final or salvage).strip()
        except Exception as exc:  # noqa: BLE001
            output = f"ERROR: ran out of steps and could not summarise: {exc}"

        return ReActResult(self.name, output, steps, "max_steps", time.time() - started)

    # ── tool dispatch ─────────────────────────────────────────────────────────

    def _call_tool(self, action: str, action_input: str) -> str:
        tool = self.tools.get(action)
        if tool is None:
            # Not an error worth failing on — tell it what it may call. Models
            # invent plausible tool names ("run_tests") constantly.
            return (f"ERROR: there is no tool called '{action}'. "
                    f"Available tools: {', '.join(self.tools)}.")

        # Every tool here takes exactly one argument; map the raw string onto
        # whatever that parameter happens to be called.
        try:
            parameter = next(iter(tool.args), None)
            payload = {parameter: action_input.strip()} if parameter else {}
            return str(tool.invoke(payload))
        except Exception as exc:  # noqa: BLE001 — tool errors are observations
            return f"ERROR: {action} raised {type(exc).__name__}: {exc}"

    def _emit(self, step: ReActStep, steps: list) -> None:
        steps.append(step)
        if self.on_step:
            try:
                self.on_step(self.name, step)
            except Exception:  # noqa: BLE001 — telemetry never breaks a run
                pass


# ── parsing ───────────────────────────────────────────────────────────────────

def _as_text(content) -> str:
    """Chat models may return a string or a list of content blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and "text" in block:
                parts.append(block["text"])
        return "".join(parts)
    return str(content)


def _parse(text: str) -> tuple[str, str | None, str, str | None]:
    """
    Pull (thought, action, action_input, final_answer) out of a reply.

    Deliberately lenient. The model will add markdown bold, extra blank lines
    and stray fences; none of that is worth a retry. Action Input runs to the
    end of the message so a whole source file can be passed as one argument.
    """
    cleaned = text.replace("**", "").strip()

    thought = ""
    if "Thought:" in cleaned:
        after = cleaned.split("Thought:", 1)[1]
        for marker in ("Action:", "Final Answer:"):
            if marker in after:
                after = after.split(marker, 1)[0]
        thought = after.strip()

    if "Final Answer:" in cleaned:
        return thought, None, "", cleaned.split("Final Answer:", 1)[1]

    if "Action:" not in cleaned:
        return thought, None, "", None

    after_action = cleaned.split("Action:", 1)[1]
    if "Action Input:" in after_action:
        action_part, input_part = after_action.split("Action Input:", 1)
    else:
        action_part, input_part = after_action, ""

    action = action_part.strip().splitlines()[0].strip().strip("`'\"") if action_part.strip() else ""
    # Truncate at a hallucinated Observation, just in case a stop sequence missed.
    for marker in ("\nObservation:", "\nThought:"):
        if marker in input_part:
            input_part = input_part.split(marker, 1)[0]

    return thought, (action or None), input_part.strip(), None
