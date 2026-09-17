"""
The ReAct loop and the tool belt.

The parser tests matter more than they look: every model output goes through
`_parse`, and a parser that trips over a stray markdown fence turns a working
agent into one that burns its whole step budget on PARSE ERROR observations.
"""

import pytest

from scrum.agents.react import ReActAgent, _parse
from scrum.agents.tools import Sandbox, build_toolbelt
from scrum.agents.workers import TOOLS_FOR, qa_passed
from scrum.store.local import LocalArtifactStore


# ── parsing ───────────────────────────────────────────────────────────────────

def test_parses_a_normal_step():
    thought, action, action_input, final = _parse(
        "Thought: I should look first\nAction: read_source\nAction Input: buggy_script.py"
    )
    assert thought == "I should look first"
    assert action == "read_source"
    assert action_input == "buggy_script.py"
    assert final is None


def test_action_input_may_be_a_whole_file():
    """write_patch takes an entire source file, so input must span lines."""
    _, action, action_input, _ = _parse(
        "Thought: patching\nAction: write_patch\nAction Input: def f():\n    return 1\n\nprint(f())"
    )
    assert action == "write_patch"
    assert action_input == "def f():\n    return 1\n\nprint(f())"


def test_final_answer_ends_the_loop():
    _, action, _, final = _parse("Thought: done\nFinal Answer: TICKET\n======\nSummary: x")
    assert action is None
    assert final.strip().startswith("TICKET")


def test_markdown_bold_does_not_break_it():
    """Models emit **Action:** constantly. Not worth a retry."""
    _, action, action_input, _ = _parse(
        "**Thought:** checking\n**Action:** run_python\n**Action Input:** patched_script.py"
    )
    assert action == "run_python"
    assert action_input == "patched_script.py"


def test_garbage_yields_no_action():
    assert _parse("I'm not sure what to do here") == ("", None, "", None)


def test_a_hallucinated_observation_is_truncated():
    """If a stop sequence is missed, the model's invented output must be dropped."""
    _, _, action_input, _ = _parse(
        "Thought: go\nAction: run_python\nAction Input: patched_script.py\n"
        "Observation: all tests passed!\nThought: great"
    )
    assert action_input == "patched_script.py"
    assert "all tests passed" not in action_input


# ── tools ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def tools(tmp_path):
    return build_toolbelt(Sandbox(LocalArtifactStore(str(tmp_path)), "run-1", str(tmp_path)),
                          timeout=5)


def test_run_python_reports_real_output(tools, tmp_path):
    (tmp_path / "demo.py").write_text("print(2 + 2)")
    observation = tools["run_python"].invoke({"path": "demo.py"})
    assert "exit_code: 0" in observation and "4" in observation


def test_run_python_surfaces_the_traceback(tools, tmp_path):
    """An agent can only fix what it can see."""
    (tmp_path / "boom.py").write_text("raise ValueError('nope')")
    observation = tools["run_python"].invoke({"path": "boom.py"})
    assert "exit_code: 1" in observation and "ValueError" in observation


def test_run_python_kills_an_infinite_loop(tools, tmp_path):
    (tmp_path / "spin.py").write_text("while True:\n    pass")
    assert "TIMEOUT" in tools["run_python"].invoke({"path": "spin.py"})


def test_write_patch_refuses_code_that_does_not_parse(tools, tmp_path):
    """The rejection becomes an Observation, so the agent self-corrects."""
    observation = tools["write_patch"].invoke({"content": "def broken(:\n    pass"})
    assert "REJECTED" in observation
    assert not (tmp_path / "patched_script.py").exists()


def test_write_patch_strips_markdown_fences(tools, tmp_path):
    tools["write_patch"].invoke({"content": "```python\nx = 1\n```"})
    assert (tmp_path / "patched_script.py").read_text().strip() == "x = 1"


def test_tools_cannot_escape_the_sandbox(tools):
    assert "outside the sandbox" in tools["read_source"].invoke({"path": "../../../etc/passwd"})


def test_the_subprocess_cannot_read_our_api_key(tools, tmp_path, monkeypatch):
    """Agent-authored code runs with a minimal environment, not ours."""
    monkeypatch.setenv("GEMINI_API_KEY", "super-secret-value")
    (tmp_path / "leak.py").write_text("import os; print(os.environ.get('GEMINI_API_KEY', 'ABSENT'))")
    assert "ABSENT" in tools["run_python"].invoke({"path": "leak.py"})


# ── role restrictions ─────────────────────────────────────────────────────────

def test_only_dev_can_write():
    """A reviewer that can edit the code under review is not a reviewer."""
    assert "write_patch" in TOOLS_FOR["dev"]
    assert "write_patch" not in TOOLS_FOR["qa"]
    assert "write_patch" not in TOOLS_FOR["pm"]


# ── verdict parsing ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("QA REVIEW\nVerdict: PASS", True),
    ("**Verdict: PASS**", True),
    ("Verdict: FAIL\nReason: still broken", False),
    ("ERROR: model call failed", False),
    ("I think it's probably fine", False),
])
def test_qa_verdict_defaults_to_fail(text, expected):
    """No parseable verdict means no pass. Silence must never ship code."""
    assert qa_passed(text) is expected


# ── the loop itself ───────────────────────────────────────────────────────────

class _Repeater:
    """A model stuck calling the same tool forever."""

    def __init__(self):
        self.calls = 0

    def invoke(self, messages, stop=None):
        self.calls += 1

        class R:
            content = "Thought: again\nAction: list_workspace\nAction Input: "

        return R()


def test_the_loop_is_bounded(tools):
    """A stuck agent must stop, not run until the bill notices."""
    model = _Repeater()
    agent = ReActAgent("dev", "You are a developer.", tools, model, max_steps=4)
    result = agent.run("do something")

    assert result.stop_reason == "max_steps"
    assert len(result.steps) == 4
    assert model.calls == 5   # four steps plus the salvage call


def test_an_unknown_tool_is_an_observation_not_a_crash(tools):
    """Models invent plausible tool names. Tell them what exists and move on."""
    agent = ReActAgent("dev", "You are a developer.", tools, _Repeater(), max_steps=1)
    assert "no tool called" in agent._call_tool("run_tests", "")
