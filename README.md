# Agentic Scrum Office

> A multi-agent bug-fixing system built with Gemini 2.5 Flash — free, local, and animated.

---

## Demo

https://github.com/user-attachments/assets/ad9e88f4-7e0a-479e-802c-19c6aaeb086f

---

## What it does

Three AI agents — PM, Dev, and QA — collaborate autonomously to analyse, fix, and verify bugs in any Python file you drop in. You watch them work in a pixel-art office in real time.

- **PM (Michael Scott)** reads your script, identifies logic bugs, and writes a structured ticket
- **Dev (Jim Halpert)** reads the ticket and patches the code
- **QA (Dwight Schrute)** reviews the patch — if it fails, he walks over to Jim's desk and sends the error back for another attempt (up to 3 tries)
- On pass, the ticket moves to **Done / Prod** on the Jira board

---

## Features

**The office**
- Top-down pixel-art office with three character cabins — Michael, Jim, and Dwight
- Characters bounce when active, screens light up in their colour, chat bubbles show live status
- On a QA fail, Dwight animates across to Jim's desk to re-ping him

**Jira board**
- Live kanban board built into the office floor: Analysis → In Development → QA Review → Failed QA → Done
- Ticket card moves columns automatically as the pipeline progresses

**Side panel — three tabs, auto-switching**
- *Ticket tab* — shows the PM's structured bug report parsed into cards, with QA verdict appended at the end
- *Diff tab* — line-by-line diff of original vs patched file, GitHub-style red/green highlighting
- *Stats tab* — attempt count, bugs found, verdict, total cycles run, and a timing bar chart showing seconds per agent

**File drop zone**
- Drag and drop any `.py` file — it uploads directly to the server and replaces the target file
- Hit **RUN ▸** to kick off the pipeline — button shows live state and re-enables when done

**Single-command server**
- One Flask server replaces the old two-terminal setup
- Serves the UI, handles file uploads, spawns the orchestrator, and exposes a `/status` endpoint

---

## Setup

**1. Install dependencies**
```
pip install -r requirements.txt
```

**2. Get a free Gemini API key**

Go to https://aistudio.google.com/app/apikey — no credit card needed.

**3. Add your key**

Open `.env` and replace the placeholder:
```
GEMINI_API_KEY=your-key-here
```

**4. Run**
```
python server.py
```

Open http://localhost:8000 — that's it.

---

## How to use

1. Open http://localhost:8000
2. Drag any `.py` file onto the drop zone at the bottom
3. Hit **RUN ▸**
4. Watch the agents work — check the side panel tabs for the ticket, diff, and stats
5. Drop another file and run again anytime

---

## Project structure

```
agentic-scrum/
├── server.py             # Flask server — the only file you run
├── orchestrator.py       # pipeline: PM → Dev → QA loop
├── .env                  # your Gemini API key goes here
├── requirements.txt
├── demo.mov              # demo video
├── agents/
│   ├── pm_prompt.py      # Michael's system prompt
│   ├── dev_prompt.py     # Jim's system prompt
│   └── qa_prompt.py      # Dwight's system prompt
├── characters/
│   ├── michael.png
│   ├── jim.png
│   └── dwight.png
├── workspace/
│   ├── buggy_script.py   # file being analysed (replaced on drop)
│   ├── ticket.txt        # PM output
│   ├── patched_script.py # Dev output
│   ├── qa_review.txt     # QA output
│   └── state.json        # live state polled by the UI
└── ui/
    └── index.html
```

---

## Stack

- **Gemini 2.5 Flash** — all three agents (free tier, ~1500 req/day)
- **Flask** — local server, file upload, pipeline trigger
- **Vanilla HTML/CSS/JS** — no frontend framework
- **Python** — orchestrator, agent calls, file I/O

---

## What's next (side quests)

- Docs agent that writes a `CHANGELOG.md` after every fix
- Security agent that reviews patches before QA signs off
- Real code execution — QA runs the patched file and compares output instead of reasoning about it
- Persistent memory across sessions so agents learn from past fixes
