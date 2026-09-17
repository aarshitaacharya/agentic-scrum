# server.py
# One Flask process: serves the office UI, accepts a dropped file, and triggers
# a run.
#
#   python server.py    ->    http://localhost:8000
#
# Endpoints:
#   GET  /                  the UI
#   GET  /workspace/<file>  state.json, ticket.txt, patched_script.py, ...
#   POST /upload            replaces workspace/buggy_script.py
#   POST /run               starts a pipeline run in a background thread
#   GET  /status            is a run in progress?
#   GET  /backends          which backend the AWS probe chose, and why
#   GET  /trace             the ReAct transcript — every Thought and Observation

import json
import logging
import os
import threading

from flask import Flask, jsonify, request, send_from_directory

from scrum.config import SETTINGS, resolve_backends
from scrum import ui_state

app = Flask(__name__)

# state.json is polled once a second; without this the log is unreadable.
logging.getLogger("werkzeug").setLevel(logging.ERROR)


@app.after_request
def no_cache(response):
    """Stop the browser serving stale JS/HTML after an edit."""
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response


# ── Paths ─────────────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UI_DIR = os.path.join(BASE_DIR, "ui")
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")
TARGET_FILE = os.path.join(WORKSPACE_DIR, "buggy_script.py")

# One run at a time. The local artifact store is deliberately flat — it writes
# ticket.txt, not runs/<id>/ticket.txt — so two concurrent runs would overwrite
# each other's files. On AWS this guard is unnecessary: S3 keys are namespaced
# by run_id and the Lambdas scale out per run.
pipeline_running = False
_run_lock = threading.Lock()


# ── UI + static files ─────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_from_directory(UI_DIR, "index.html")


@app.route("/ui/<path:filename>")
def ui_files(filename):
    return send_from_directory(UI_DIR, filename)


@app.route("/characters/<path:filename>")
def character_files(filename):
    return send_from_directory(os.path.join(BASE_DIR, "characters"), filename)


@app.route("/workspace/<path:filename>")
def workspace_files(filename):
    return send_from_directory(WORKSPACE_DIR, filename)


# ── Upload ────────────────────────────────────────────────────────────────────

@app.route("/upload", methods=["POST"])
def upload():
    """Take a dropped .py file and make it the file under analysis."""
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file in request"}), 400

    uploaded = request.files["file"]
    if not uploaded.filename.endswith(".py"):
        return jsonify({"ok": False, "error": "Only .py files accepted"}), 400

    try:
        content = uploaded.read().decode("utf-8")
    except UnicodeDecodeError:
        return jsonify({"ok": False, "error": "That file is not UTF-8 text"}), 400

    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    with open(TARGET_FILE, "w") as fh:
        fh.write(content)

    # Clear the previous run's output so the UI does not show a stale diff
    # against a file that is no longer there.
    for stale in ("ticket.txt", "patched_script.py", "qa_review.txt", "trace.jsonl"):
        path = os.path.join(WORKSPACE_DIR, stale)
        if os.path.exists(path):
            os.remove(path)

    lines = content.count("\n") + 1
    print(f"[server] Uploaded {uploaded.filename} ({lines} lines)")
    return jsonify({"ok": True, "filename": uploaded.filename, "lines": lines})


# ── Trigger a run ─────────────────────────────────────────────────────────────

@app.route("/run", methods=["POST"])
def run_pipeline_endpoint():
    """
    Start a run in a background thread and return immediately.

    Returning 202 rather than holding the connection open is the same choice
    the API Gateway handler makes on AWS: an agent pipeline takes minutes, and
    no sensible HTTP timeout accommodates that. The UI finds out by polling
    state.json — which is exactly what it would do against a real deployment.
    """
    global pipeline_running

    with _run_lock:
        if pipeline_running:
            return jsonify({"ok": False, "error": "Pipeline already running"}), 409
        pipeline_running = True

    def work():
        global pipeline_running
        try:
            # Imported here, not at module scope: this pulls in LangChain and
            # the Gemini client, which would add a second or two to startup for
            # a server that might only ever serve the UI.
            from scrum.runtime import run_pipeline

            run_pipeline(SETTINGS)
        except Exception as exc:  # noqa: BLE001
            print(f"[server] Pipeline failed: {type(exc).__name__}: {exc}")
            ui_state.set_state("done", "crashed", str(exc)[:200], 1, verdict="fail")
        finally:
            with _run_lock:
                pipeline_running = False
            print("[server] Pipeline finished.")

    threading.Thread(target=work, daemon=True).start()
    return jsonify({"ok": True}), 202


@app.route("/status")
def status():
    return jsonify({"running": pipeline_running})


# ── Diagnostics ───────────────────────────────────────────────────────────────

@app.route("/backends")
def backends():
    """
    Which backend won the probe, and every check behind that decision.

    Worth exposing rather than burying in a log line: "is this actually talking
    to AWS right now?" is the first question anyone asks about a system with a
    fallback, and it should be answerable without reading the source.
    """
    return jsonify(resolve_backends(SETTINGS).as_dict())


@app.route("/trace")
def trace():
    """
    The ReAct transcript: every Thought, Action and Observation of the last run.

    This is the artifact that shows the agents are reasoning over real tool
    output rather than producing plausible text — it is the most convincing
    thing in the project to read.
    """
    path = os.path.join(WORKSPACE_DIR, "trace.jsonl")
    if not os.path.exists(path):
        return jsonify({"steps": []})

    steps = []
    with open(path) as fh:
        for line in fh:
            try:
                steps.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return jsonify({"steps": steps})


# ── Start ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        decision = resolve_backends(SETTINGS)
    except RuntimeError as exc:
        # AGENTIC_SCRUM_BACKEND=aws with a failed probe. Say so and stop, rather
        # than serving a UI whose Run button is guaranteed to fail.
        raise SystemExit(f"\n{exc}\n")
    ui_state.set_backend(decision.mode, decision.reason)

    print("\n=== Agentic Scrum Server ===")
    print(decision.banner())
    # Configurable because 8000 is a popular port and "Address already in use"
    # is a miserable first experience of someone else's project.
    port = int(os.environ.get("PORT", "8000"))
    print(f"\nOpen http://localhost:{port}")
    print("Drop a .py file in the UI, then hit RUN\n")

    app.run(port=port, debug=False)
