# orchestrator.py
# Phase 4 — same pipeline as Phase 2, but now writes state.json at every
# step so the desk UI can animate in real time.
#
# Run:  python orchestrator.py
# Then open ui/index.html in your browser (via a local server — see README).

import os
import json
import time
import google.generativeai as genai

from agents.pm_prompt  import PM_SYSTEM_PROMPT,  PM_USER_TEMPLATE
from agents.dev_prompt import DEV_SYSTEM_PROMPT, DEV_USER_TEMPLATE
from agents.qa_prompt  import QA_SYSTEM_PROMPT,  QA_USER_TEMPLATE

# ── Config ────────────────────────────────────────────────────────────────────

API_KEY    = os.environ.get("GEMINI_API_KEY", "PASTE_YOUR_KEY_HERE")
MODEL_NAME = "gemini-2.5-flash"

ORIGINAL_SCRIPT = "workspace/buggy_script.py"
TICKET_FILE     = "workspace/ticket.txt"
PATCHED_SCRIPT  = "workspace/patched_script.py"
QA_REVIEW_FILE  = "workspace/qa_review.txt"
STATE_FILE      = "workspace/state.json"   # the UI watches this file

MAX_RETRIES = 3

# ── Setup ─────────────────────────────────────────────────────────────────────

genai.configure(api_key=API_KEY)

def make_agent(system_prompt):
    return genai.GenerativeModel(
        model_name=MODEL_NAME,
        system_instruction=system_prompt,
    )

pm_agent  = make_agent(PM_SYSTEM_PROMPT)
dev_agent = make_agent(DEV_SYSTEM_PROMPT)
qa_agent  = make_agent(QA_SYSTEM_PROMPT)

# ── State writer ──────────────────────────────────────────────────────────────

def set_state(agent, status, message="", attempt=1, verdict=""):
    """
    Write the current pipeline state to state.json.
    The UI polls this file and updates the animation accordingly.

    Fields:
      agent   — which agent is active: "pm", "dev", "qa", "done"
      status  — short action label shown in the chat bubble
      message — longer text for the log panel
      attempt — current retry number (shown in UI)
      verdict — "pass", "fail", or "" (controls green/red flash on QA desk)
    """
    state = {
        "agent":   agent,
        "status":  status,
        "message": message,
        "attempt": attempt,
        "verdict": verdict,
        "ts":      time.time(),   # timestamp so UI knows it's a fresh update
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── File helpers ──────────────────────────────────────────────────────────────

def read_file(path):
    with open(path, "r") as f:
        return f.read()

def write_file(path, content):
    with open(path, "w") as f:
        f.write(content)
    print(f"     -> saved to {path}")

# ── Agent callers ─────────────────────────────────────────────────────────────

def run_pm(code):
    print("  [PM] Analysing bugs...")
    set_state("pm", "analysing code...", "Reading the script for logic bugs")
    response = pm_agent.generate_content(PM_USER_TEMPLATE.format(code=code))
    set_state("pm", "writing ticket", "Ticket written to workspace/ticket.txt")
    return response.text

def run_dev(ticket, code, attempt):
    print("  [Dev] Writing fix...")
    set_state("dev", "reading ticket", f"Attempt {attempt} — reading PM ticket", attempt)
    response = dev_agent.generate_content(
        DEV_USER_TEMPLATE.format(ticket=ticket, code=code)
    )
    patch = response.text.strip()
    if patch.startswith("```"):
        lines = patch.splitlines()
        patch = "\n".join(lines[1:-1])
    set_state("dev", "patch written", f"Attempt {attempt} — patch saved", attempt)
    return patch

def run_qa(ticket, patched_code, attempt):
    print("  [QA] Reviewing patch...")
    set_state("qa", "reviewing patch", f"Attempt {attempt} — checking Dev's fix", attempt)
    response = qa_agent.generate_content(
        QA_USER_TEMPLATE.format(ticket=ticket, patched_code=patched_code)
    )
    return response.text

def qa_passed(review_text):
    return "Verdict: PASS" in review_text

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n=== AGENTIC SCRUM — Phase 4 ===\n")

    # Reset state so UI shows idle on fresh run
    set_state("idle", "waiting...", "Press run to start")

    # ── PM ────────────────────────────────────────────────────────────────────
    print("--- PM Agent ---")
    code   = read_file(ORIGINAL_SCRIPT)
    ticket = run_pm(code)
    write_file(TICKET_FILE, ticket)
    print()

    # ── Dev + QA loop ─────────────────────────────────────────────────────────
    attempt = 0
    passed  = False

    while attempt < MAX_RETRIES:
        attempt += 1
        print(f"--- Dev Agent (attempt {attempt}/{MAX_RETRIES}) ---")
        patch = run_dev(ticket, code, attempt)
        write_file(PATCHED_SCRIPT, patch)
        print()

        print(f"--- QA Agent (attempt {attempt}/{MAX_RETRIES}) ---")
        review = run_qa(ticket, patch, attempt)
        write_file(QA_REVIEW_FILE, review)
        print()

        if qa_passed(review):
            passed = True
            set_state("qa", "all tests passed!", f"Passed on attempt {attempt}", attempt, verdict="pass")
            break
        else:
            set_state("qa", "FAILED — pinging Dev", "QA found issues, sending error log", attempt, verdict="fail")
            print("  [QA] FAIL — pinging Dev for another attempt...")
            time.sleep(1)   # small pause so UI can show the fail state before moving on
            ticket = ticket + "\n\n--- QA FEEDBACK (attempt " + str(attempt) + ") ---\n" + review
            # Update 'code' with the latest patch for the next Dev attempt
            code = patch

    # ── Final ─────────────────────────────────────────────────────────────────
    print("=" * 40)
    if passed:
        set_state("done", "bug fixed!", f"Fixed in {attempt} attempt(s)", attempt, verdict="pass")
        print(f"PASS — fixed in {attempt} attempt(s).")
    else:
        set_state("done", "gave up", f"Failed after {MAX_RETRIES} attempts", attempt, verdict="fail")
        print(f"GAVE UP after {MAX_RETRIES} attempts.")
    print("=" * 40 + "\n")

if __name__ == "__main__":
    main()