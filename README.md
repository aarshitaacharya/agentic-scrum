# Agentic Scrum

A tiny multi-agent system where PM, Dev, and QA agents collaborate to fix bugs.
Built with Gemini 2.0 Flash (free tier). No frameworks, just plain Python.

## Project structure

```
agentic-scrum/
├── orchestrator.py          # main script — run this
├── requirements.txt
├── agents/
│   └── pm_prompt.py         # PM agent's system prompt (more agents added in Phase 2)
└── workspace/
    ├── buggy_script.py      # the file being reviewed/fixed
    ├── ticket.txt           # written by PM agent
    ├── patch.py             # written by Dev agent (Phase 2)
    └── test_result.txt      # written by QA agent (Phase 2)
```

## Setup

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Get a free Gemini API key at https://aistudio.google.com/app/apikey

3. Set your key as an environment variable:
   ```
   # Mac/Linux
   export GEMINI_API_KEY="your-key-here"

   # Windows
   set GEMINI_API_KEY=your-key-here
   ```
   Or just paste it directly into `orchestrator.py` (line with PASTE_YOUR_KEY_HERE) for quick local testing.

4. Run Phase 1:
   ```
   python orchestrator.py
   ```

## Phase 1 checkpoint

The PM agent should find both bugs in `workspace/buggy_script.py`:
- `get_average` divides by 10 instead of `len(numbers)`
- `find_max` starts with 99999 so it never updates

If `workspace/ticket.txt` mentions both — Phase 1 is done.