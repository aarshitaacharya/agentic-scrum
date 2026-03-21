# Agentic Scrum

A multi-agent system where PM, Dev, and QA agents collaborate to fix bugs.
Built with Gemini 2.5 Flash (free tier). No frameworks — plain Python + one HTML file.

## Project structure

```
agentic-scrum/
├── server.py             # Flask server — the only thing you need to run
├── orchestrator.py       # pipeline logic (called by server.py automatically)
├── requirements.txt
├── agents/
│   ├── pm_prompt.py
│   ├── dev_prompt.py
│   └── qa_prompt.py
├── characters/
│   ├── michael.png
│   ├── jim.png
│   └── dwight.png
├── workspace/
│   ├── buggy_script.py   # replaced when you drop a file in the UI
│   ├── ticket.txt        # written by PM
│   ├── patched_script.py # written by Dev
│   ├── qa_review.txt     # written by QA
│   └── state.json        # live state for the UI
└── ui/
    └── index.html
```

## Setup

1. Install dependencies:
   pip install -r requirements.txt

2. Get a free Gemini API key at https://aistudio.google.com/app/apikey

3. Set your key:
   export GEMINI_API_KEY="your-key-here"   # Mac/Linux
   set GEMINI_API_KEY=your-key-here        # Windows

## Running

Just one command now:

   python server.py

Then open http://localhost:8000 in your browser.

1. Drop any .py file onto the drop zone
2. Hit RUN — watch the agents go
3. Check the side panel for ticket, diff, and stats