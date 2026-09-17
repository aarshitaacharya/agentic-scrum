"""
Shared fixtures. The important one is ScriptedLLM.

Testing an agent system against a real model is slow, costs money and is not
reproducible — the same prompt can fail on Tuesday. So the model is replaced by
a script: fixed replies in ReAct format, chosen by which role prompt it was
handed. That keeps the *system* under test (routing, retries, tool dispatch,
the state machine) while removing the only non-deterministic part.

What this deliberately does NOT test is whether Gemini is any good at fixing
bugs. That is a question for an eval set, not a unit test.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class Reply:
    def __init__(self, content):
        self.content = content


class ScriptedLLM:
    """Plays PM, Dev, QA and the self-critic, based on the system prompt."""

    def __init__(self, qa_always_fails=False):
        self.qa_always_fails = qa_always_fails
        self.turns = {"pm": 0, "dev": 0, "qa": 0}
        self.qa_reviews = 0
        self.calls = 0

    def invoke(self, messages, stop=None):
        self.calls += 1
        system = messages[0].content
        if "reviewing your own work" in system:
            return Reply("Verdict: ACCEPT")
        if "Product Manager" in system:
            return Reply(self._pm())
        if "Python developer" in system:
            return Reply(self._dev())
        return Reply(self._qa())

    def _pm(self):
        self.turns["pm"] += 1
        if self.turns["pm"] == 1:
            return "Thought: read it\nAction: read_source\nAction Input: buggy_script.py"
        if self.turns["pm"] == 2:
            return "Thought: run it\nAction: run_python\nAction Input: buggy_script.py"
        return ("Thought: I have evidence now\nFinal Answer: TICKET\n======\n"
                "Summary: average() is wrong\n\nEvidence: printed 60.0, expected 85.0\n\n"
                "Bugs found:\n1. Function: average\n   Line: 7\n   Problem: skips the last score\n"
                "   Expected: include every score\nEnd of ticket.")

    def _dev(self):
        self.turns["dev"] += 1
        if self.turns["dev"] == 1:
            # Submit code that does not parse, so write_patch's rejection — and
            # the agent's recovery from it — is exercised on every run.
            return "Thought: patching\nAction: write_patch\nAction Input: def broken(:\n    pass"
        if self.turns["dev"] == 2:
            return "Thought: fix my syntax error\nAction: write_patch\nAction Input: " + FIXED_SOURCE
        if self.turns["dev"] == 3:
            return "Thought: verify it\nAction: run_python\nAction Input: patched_script.py"
        self.turns["dev"] = 0     # reset so the next attempt replays the script
        return ("Thought: done\nFinal Answer: PATCH SUMMARY\n"
                "- Bug 1: fixed the range in average()\nVerified: average now prints 85.0")

    def _qa(self):
        self.turns["qa"] += 1
        if self.turns["qa"] == 1:
            return "Thought: run it myself\nAction: run_python\nAction Input: patched_script.py"
        self.turns["qa"] = 0
        self.qa_reviews += 1
        if self.qa_always_fails or self.qa_reviews == 1:
            return ("Thought: not convinced\nFinal Answer: QA REVIEW\n=========\n"
                    "Ran: patched_script.py\nBug 1 check: average — FIXED\n"
                    "Regressions: none found\nVerdict: FAIL\n"
                    "Reason: highest() still starts at 0, so negative scores break it.")
        return ("Thought: looks right\nFinal Answer: QA REVIEW\n=========\n"
                "Ran: patched_script.py\nBug 1 check: average — FIXED\n"
                "Regressions: none found\nVerdict: PASS")


FIXED_SOURCE = '''def average(scores):
    return sum(scores) / len(scores)


def highest(scores):
    best = scores[0]
    for score in scores:
        if score > best:
            best = score
    return best


def letter_grade(score):
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    return "F"


def main():
    scores = [90, 80, 70, 100]
    print("average:", average(scores))
    print("highest:", highest(scores))
    print("grade for 90:", letter_grade(90))


if __name__ == "__main__":
    main()
'''

BUGGY_SOURCE = '''def average(scores):
    total = 0
    for i in range(len(scores) - 1):
        total += scores[i]
    return total / len(scores)


def main():
    print("average:", average([90, 80, 70, 100]))


if __name__ == "__main__":
    main()
'''


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """An isolated workspace, so tests never touch the real one."""
    from scrum import store
    from scrum import ui_state
    from scrum.store.local import LocalArtifactStore, LocalRunStateStore

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "buggy_script.py").write_text(BUGGY_SOURCE)

    monkeypatch.setattr(store, "_ARTIFACTS", LocalArtifactStore(str(root)))
    monkeypatch.setattr(store, "_RUN_STATE", LocalRunStateStore(str(root / "run_state.json")))
    monkeypatch.setattr(ui_state, "STATE_FILE", str(root / "state.json"))
    return root


@pytest.fixture
def scripted_llm(monkeypatch):
    def _make(qa_always_fails=False):
        llm = ScriptedLLM(qa_always_fails=qa_always_fails)
        from scrum import handlers
        from scrum import llm as llm_module

        monkeypatch.setattr(llm_module, "build_llm", lambda *a, **k: llm)
        monkeypatch.setattr(handlers, "build_llm", lambda *a, **k: llm)
        return llm

    return _make
