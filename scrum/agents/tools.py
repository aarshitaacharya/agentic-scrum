"""
agents/tools.py — the tool belt. This is what turns three prompt calls into
three agents.

An LLM that is handed source code and asked "what is wrong with it?" is
guessing. An agent that can *run* the code, read the traceback, patch it, and
run it again is checking. Every tool below exists to move some claim from the
model's opinion column to the observation column:

    run_python     what the code actually does, not what it looks like it does
    static_check   does it even parse — a syntax error should cost one cheap
                   local call, not a full QA round trip
    write_patch    refuses to save code that does not compile, so the agent
                   gets the error back as an Observation and fixes it inside
                   its own loop instead of failing downstream
    diff_patch     what changed, so QA can review the delta rather than
                   re-reading 200 unchanged lines

Tools are built per-run by `build_toolbelt`, closed over a Sandbox, so an agent
physically cannot reach outside the run's directory.
"""

from __future__ import annotations

import difflib
import os
import subprocess
import sys
import tempfile

from langchain_core.tools import StructuredTool

from scrum.store.base import BUGGY_SCRIPT, PATCHED


class Sandbox:
    """
    A real directory the tools can read, write and execute inside.

    Locally this *is* workspace/, so pull/push are no-ops. On Lambda it is
    /tmp/<run_id>, hydrated from S3 on the way in and flushed back on the way
    out — the pattern every Lambda that touches files ends up using, because
    the only writable disk is /tmp and it does not outlive the container.
    """

    def __init__(self, store, run_id: str, root: str | None = None):
        self.store = store
        self.run_id = run_id
        if root is None:
            local_root = getattr(store, "root", None)
            root = local_root or os.path.join(tempfile.gettempdir(), f"scrum-{run_id}")
        self.root = os.path.abspath(root)
        os.makedirs(self.root, exist_ok=True)
        # If the store already writes here, syncing would just copy a file
        # onto itself.
        self._shares_disk_with_store = os.path.abspath(getattr(store, "root", "")) == self.root

    # ── path safety ──

    def resolve(self, name: str) -> str:
        """Resolve a tool-supplied path, refusing anything outside the sandbox."""
        candidate = os.path.abspath(os.path.join(self.root, os.path.normpath(name).lstrip("/")))
        if candidate != self.root and not candidate.startswith(self.root + os.sep):
            raise ValueError(f"'{name}' is outside the sandbox")
        return candidate

    # ── sync ──

    def pull(self, names: list[str]) -> None:
        if self._shares_disk_with_store:
            return
        for name in names:
            content = self.store.read_or(self.run_id, name, None)
            if content is not None:
                with open(self.resolve(name), "w") as fh:
                    fh.write(content)

    def push(self, names: list[str]) -> None:
        if self._shares_disk_with_store:
            return
        for name in names:
            path = self.resolve(name)
            if os.path.exists(path):
                with open(path) as fh:
                    self.store.write(self.run_id, name, fh.read())


# ── tool implementations ──────────────────────────────────────────────────────

MAX_OBSERVATION = 4000   # keep observations from eating the context window


def _clip(text: str, limit: int = MAX_OBSERVATION) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n...[{len(text) - limit} chars elided]...\n{text[-half:]}"


def build_toolbelt(sandbox: Sandbox, timeout: int = 15, allow_exec: bool = True) -> dict:
    """
    Build the tools for one run and return them keyed by name.

    Each agent then picks the subset it is allowed to use — PM gets read-only
    tools, only Dev can write. That restriction is not cosmetic: it is what
    stops QA from "helpfully" fixing the bug it was asked to judge, which
    destroys the independent check the whole pipeline exists to provide.
    """

    def read_source(path: str = BUGGY_SCRIPT) -> str:
        """Read a file from the workspace, with line numbers."""
        try:
            with open(sandbox.resolve(path)) as fh:
                lines = fh.read().splitlines()
        except FileNotFoundError:
            return f"ERROR: no such file '{path}'. Use list_workspace to see what exists."
        except ValueError as exc:
            return f"ERROR: {exc}"
        width = len(str(len(lines)))
        return _clip("\n".join(f"{i:>{width}} | {line}" for i, line in enumerate(lines, 1)))

    def list_workspace(_: str = "") -> str:
        """List the files available in the workspace."""
        entries = []
        for name in sorted(os.listdir(sandbox.root)):
            full = os.path.join(sandbox.root, name)
            if os.path.isfile(full):
                entries.append(f"{name} ({os.path.getsize(full)} bytes)")
        return "\n".join(entries) or "(workspace is empty)"

    def static_check(path: str = BUGGY_SCRIPT) -> str:
        """Check that a Python file parses. Cheap; no code is executed."""
        try:
            with open(sandbox.resolve(path)) as fh:
                source = fh.read()
        except FileNotFoundError:
            return f"ERROR: no such file '{path}'"
        except ValueError as exc:
            return f"ERROR: {exc}"
        try:
            compile(source, path, "exec")
        except SyntaxError as exc:
            return f"SYNTAX ERROR in {path} at line {exc.lineno}: {exc.msg}"
        return f"OK: {path} parses cleanly."

    def run_python(path: str = BUGGY_SCRIPT) -> str:
        """Execute a Python file and report exit code, stdout and stderr."""
        if not allow_exec:
            return "ERROR: execution is disabled (SCRUM_ALLOW_EXEC=0). Reason about the code instead."
        try:
            target = sandbox.resolve(path)
        except ValueError as exc:
            return f"ERROR: {exc}"
        if not os.path.exists(target):
            return f"ERROR: no such file '{path}'"

        try:
            proc = subprocess.run(
                [sys.executable, target],
                cwd=sandbox.root,
                capture_output=True,
                text=True,
                timeout=timeout,
                # Minimal environment: the agent's code has no business reading
                # our API key out of os.environ.
                env={"PATH": os.environ.get("PATH", ""), "HOME": sandbox.root,
                     "PYTHONDONTWRITEBYTECODE": "1"},
            )
        except subprocess.TimeoutExpired:
            return (f"TIMEOUT: {path} did not finish within {timeout}s. "
                    "Suspect an infinite loop — check your loop bounds.")
        except OSError as exc:
            return f"ERROR: could not execute {path}: {exc}"

        return _clip(
            f"exit_code: {proc.returncode}\n"
            f"--- stdout ---\n{proc.stdout or '(empty)'}\n"
            f"--- stderr ---\n{proc.stderr or '(empty)'}"
        )

    def write_patch(content: str) -> str:
        """Save the corrected script. Rejects code that does not parse."""
        code = _strip_code_fence(content)
        try:
            compile(code, PATCHED, "exec")
        except SyntaxError as exc:
            # The refusal is the feature: this comes back as an Observation, so
            # the agent repairs its own output inside the same loop.
            return (f"REJECTED — the patch has a syntax error at line {exc.lineno}: {exc.msg}. "
                    "Fix it and call write_patch again.")
        with open(sandbox.resolve(PATCHED), "w") as fh:
            fh.write(code)
        return f"Saved {len(code.splitlines())} lines to {PATCHED}. Run it to verify."

    def diff_patch(_: str = "") -> str:
        """Unified diff of the original script against the patched one."""
        try:
            with open(sandbox.resolve(BUGGY_SCRIPT)) as fh:
                original = fh.read().splitlines()
            with open(sandbox.resolve(PATCHED)) as fh:
                patched = fh.read().splitlines()
        except FileNotFoundError as exc:
            return f"ERROR: {exc}"
        diff = list(difflib.unified_diff(original, patched, "original", "patched", lineterm="", n=2))
        return _clip("\n".join(diff)) if diff else "No differences — the patch changed nothing."

    specs = [
        (read_source, "read_source",
         "Read a file from the workspace with line numbers. Input: the filename, "
         f"e.g. '{BUGGY_SCRIPT}'."),
        (list_workspace, "list_workspace",
         "List the files in the workspace. Input: leave empty."),
        (static_check, "static_check",
         "Check a Python file parses, without running it. Input: the filename."),
        (run_python, "run_python",
         "Run a Python file and see its real exit code, stdout and stderr. "
         "Input: the filename. This is how you find out what the code actually does."),
        (write_patch, "write_patch",
         "Save the corrected script. Input: the COMPLETE corrected Python source, "
         "nothing else — no prose, no markdown fences."),
        (diff_patch, "diff_patch",
         f"Show a unified diff of {BUGGY_SCRIPT} against {PATCHED}. Input: leave empty."),
    ]

    return {
        name: StructuredTool.from_function(
            func=func, name=name, description=description,
            infer_schema=True, handle_tool_error=True,
        )
        for func, name, description in specs
    }


def _strip_code_fence(text: str) -> str:
    """Models wrap code in ``` no matter how firmly you ask them not to."""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()
    lines = lines[1:]                       # drop ```python
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines)
