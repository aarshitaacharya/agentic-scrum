# server.py
# Replaces "python -m http.server" — serves the UI AND handles file uploads.
#
# Run:  python server.py
# Then open http://localhost:8000 in your browser.
#
# Endpoints:
#   GET  /                          -> serves ui/index.html
#   GET  /static/<path>             -> serves ui/ files (sprites, characters)
#   GET  /workspace/<file>          -> serves workspace/ files (state.json, ticket.txt etc)
#   POST /upload                    -> writes uploaded .py file to workspace/buggy_script.py
#   POST /run                       -> runs the orchestrator pipeline in a background thread
#   GET  /status                    -> returns {"running": true/false}

import os
import logging
import threading
import subprocess
from flask import Flask, send_from_directory, request, jsonify

app = Flask(__name__)

# Silence the per-request log spam (state.json polls every second)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# Prevent browser from caching files — avoids stale JS/HTML after updates
@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response

# ── Path config ───────────────────────────────────────────────

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
UI_DIR        = os.path.join(BASE_DIR, "ui")
WORKSPACE_DIR = os.path.join(BASE_DIR, "workspace")
TARGET_FILE   = os.path.join(WORKSPACE_DIR, "buggy_script.py")

# Track whether a pipeline run is in progress
pipeline_running = False

# ── Page + static files ───────────────────────────────────────

@app.route("/")
def index():
    """Serve the main UI page."""
    return send_from_directory(UI_DIR, "index.html")

@app.route("/ui/<path:filename>")
def ui_files(filename):
    """Serve anything inside ui/ — sprites, js files, etc."""
    return send_from_directory(UI_DIR, filename)

@app.route("/characters/<path:filename>")
def character_files(filename):
    """Serve character PNG sprites."""
    chars_dir = os.path.join(BASE_DIR, "characters")
    return send_from_directory(chars_dir, filename)

@app.route("/workspace/<path:filename>")
def workspace_files(filename):
    """Serve workspace files — state.json, ticket.txt, patched_script.py, etc."""
    return send_from_directory(WORKSPACE_DIR, filename)

# ── File upload ───────────────────────────────────────────────

@app.route("/upload", methods=["POST"])
def upload():
    """
    Receives a .py file from the drop zone and writes it to
    workspace/buggy_script.py, replacing whatever was there before.
    """
    if "file" not in request.files:
        return jsonify({"ok": False, "error": "No file in request"}), 400

    file = request.files["file"]

    if not file.filename.endswith(".py"):
        return jsonify({"ok": False, "error": "Only .py files accepted"}), 400

    content = file.read().decode("utf-8")
    os.makedirs(WORKSPACE_DIR, exist_ok=True)

    with open(TARGET_FILE, "w") as f:
        f.write(content)

    lines = content.count("\n") + 1
    print(f"[server] Uploaded: {file.filename} ({lines} lines) -> workspace/buggy_script.py")
    return jsonify({"ok": True, "filename": file.filename, "lines": lines})

# ── Pipeline trigger ──────────────────────────────────────────

@app.route("/run", methods=["POST"])
def run_pipeline():
    """
    Kicks off orchestrator.py in a background thread.
    Only one run at a time — returns busy if already running.
    """
    global pipeline_running

    if pipeline_running:
        return jsonify({"ok": False, "error": "Pipeline already running"}), 409

    def run():
        global pipeline_running
        pipeline_running = True
        print("[server] Starting pipeline...")
        try:
            subprocess.run(
                ["python", "orchestrator.py"],
                cwd=BASE_DIR,
                env={**os.environ},  # inherit GEMINI_API_KEY etc
            )
        finally:
            pipeline_running = False
            print("[server] Pipeline finished.")

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return jsonify({"ok": True})

# ── Status check ──────────────────────────────────────────────

@app.route("/status")
def status():
    """Let the UI poll whether the pipeline is currently running."""
    return jsonify({"running": pipeline_running})

# ── Start ─────────────────────────────────────────────────────

if __name__ == "__main__":
    print("\n=== Agentic Scrum Server ===")
    print("Open http://localhost:8000 in your browser")
    print("Drop a .py file in the UI, then hit Run\n")
    app.run(port=8000, debug=False)