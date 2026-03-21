# Agentic Scrum

A tiny multi-agent system where PM, Dev, and QA agents collaborate to fix bugs.
Built with Gemini 2.5 Flash (free tier). No frameworks, just plain Python + one HTML file.

## Project structure

```
agentic-scrum/
├── orchestrator.py       # main script — run this
├── requirements.txt
├── agents/
│   ├── pm_prompt.py      # PM agent personality + prompt templates
│   ├── dev_prompt.py     # Dev agent personality + prompt templates
│   └── qa_prompt.py      # QA agent personality + prompt templates
├── workspace/
│   ├── buggy_script.py   # the file being reviewed/fixed (swap this out)
│   ├── ticket.txt        # written by PM
│   ├── patched_script.py # written by Dev
│   ├── qa_review.txt     # written by QA
│   └── state.json        # written by orchestrator, read by UI
└── ui/
    └── index.html        # the desk animation UI
```

## Setup

1. Install dependencies:
   pip install -r requirements.txt

2. Get a free Gemini API key at https://aistudio.google.com/app/apikey

3. Set your key:
   export GEMINI_API_KEY="your-key-here"   # Mac/Linux
   set GEMINI_API_KEY=your-key-here        # Windows

## Running

You need two terminals open at the same time.

Terminal 1 — serve the UI (must be run from the project root):
   python -m http.server 8000

Terminal 2 — run the pipeline:
   python orchestrator.py

Then open http://localhost:8000/ui/index.html in your browser.
Start watching, then run the orchestrator — agents will animate as they work.

## Swapping in your own script
Replace workspace/buggy_script.py with any single-file Python script.
The agents will figure out the rest.