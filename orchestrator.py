# orchestrator.py
# Phase 1 — PM agent only.
# Reads buggy_script.py, calls Gemini, writes ticket.txt
#
# Run:  python orchestrator.py
# Needs: GEMINI_API_KEY set as an environment variable
#        OR paste your key directly into the variable below (only for local dev).

import os
import google.generativeai as genai
from agents.pm_prompt import PM_SYSTEM_PROMPT, PM_USER_TEMPLATE

# ── Config ────────────────────────────────────────────────────────────────────

API_KEY = os.environ.get("GOOGLE_API_KEY")

SCRIPT_TO_REVIEW = "workspace/buggy_script.py"   # file the PM will read
TICKET_OUTPUT    = "workspace/ticket.txt"          # where the PM writes its ticket
MODEL_NAME       = "gemini-2.5-flash"              # free tier model

# ── Setup ─────────────────────────────────────────────────────────────────────

genai.configure(api_key=API_KEY)

model = genai.GenerativeModel(
    model_name=MODEL_NAME,
    system_instruction=PM_SYSTEM_PROMPT,   # PM personality lives here
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def read_file(path):
    """Read a file and return its contents as a string."""
    with open(path, "r") as f:
        return f.read()

def write_file(path, content):
    """Write a string to a file, overwriting if it exists."""
    with open(path, "w") as f:
        f.write(content)
    print(f"  -> Written to {path}")

def call_pm_agent(code):
    """
    Send the code to the PM agent and return its ticket as a string.
    The user message uses the template from pm_prompt.py so the
    code is neatly wrapped with clear delimiters.
    """
    user_message = PM_USER_TEMPLATE.format(code=code)

    print("  -> Sending to Gemini... (this may take a few seconds)")
    response = model.generate_content(user_message)

    return response.text

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n=== AGENTIC SCRUM — Phase 1 ===")
    print("Agent: PM\n")

    # Step 1: read the buggy script
    print(f"[PM] Reading {SCRIPT_TO_REVIEW} ...")
    code = read_file(SCRIPT_TO_REVIEW)

    # Step 2: ask Gemini to analyse it
    print("[PM] Analysing bugs...")
    ticket = call_pm_agent(code)

    # Step 3: write the ticket to disk
    print("[PM] Writing ticket...")
    write_file(TICKET_OUTPUT, ticket)

    # Step 4: show the result in the console too
    print("\n--- TICKET CONTENTS ---")
    print(ticket)
    print("-----------------------")
    print("\n[PM] Done. Ticket written to workspace/ticket.txt")
    print("Phase 1 complete — checkpoint passed if the ticket lists the two bugs.\n")

if __name__ == "__main__":
    main()