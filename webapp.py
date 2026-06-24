#!/usr/bin/env python3
"""Local web UI for gemma4-mac — chat, vision/audio, Photos tagging, yearbook.

A small Flask app that wraps the same on-device Gemma 4 e4b capability the
CLI exposes, so you don't have to touch the terminal for everyday use.

  * Chat / vision / audio run **in-process** with the model kept resident in
    memory — the ~6 s cold load happens once, then every prompt is instant.
  * Photos tagging and yearbook curation shell out to the existing
    `photos_caption.py` / `yearbook.py` scripts and stream their stdout live
    to the browser, so there is a single source of truth for that logic.

Run it via the `gemma-web` alias (added by install.sh), the double-click
`Gemma.command` launcher, or directly:

    ./venv/bin/python webapp.py            # opens http://127.0.0.1:7860

Everything stays on-device; the server only ever binds to localhost.
"""
import argparse
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

# HEIF/HEIC support so uploaded iPhone photos can be read by PIL (mlx-vlm).
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
except ImportError:
    pass

REPO = "mlx-community/gemma-4-e4b-it-4bit"
HERE = Path(__file__).resolve().parent
WEB_DIR = HERE / "web"
HOST = "127.0.0.1"
PORT = int(os.environ.get("GEMMA_WEB_PORT", "7860"))

app = Flask(__name__, static_folder=None)


# --------------------------------------------------------------------------
# Model manager — lazy-load the text (mlx-lm) and vision (mlx-vlm) backends
# once and reuse them across requests. Inference is serialised with a lock
# because MLX models are not safe to drive from two requests concurrently.
# --------------------------------------------------------------------------
class Models:
    def __init__(self):
        self._lock = threading.Lock()
        self._text = None   # (model, tokenizer)
        self._vlm = None     # (model, processor, config)

    @property
    def text_loaded(self):
        return self._text is not None

    @property
    def vlm_loaded(self):
        return self._vlm is not None

    def text(self):
        if self._text is None:
            from mlx_lm.utils import load_model, load_tokenizer, snapshot_download
            path = Path(snapshot_download(REPO))
            model, config = load_model(path, strict=False)
            tok = load_tokenizer(path, eos_token_ids=config.get("eos_token_id"))
            self._text = (model, tok)
        return self._text

    def vlm(self):
        if self._vlm is None:
            from mlx_vlm import load
            from mlx_vlm.utils import load_config
            model, processor = load(REPO)
            config = load_config(REPO)
            self._vlm = (model, processor, config)
        return self._vlm

    @property
    def lock(self):
        return self._lock


MODELS = Models()


def sse(data: dict) -> str:
    """Format a dict as a Server-Sent Events data frame."""
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
@app.route("/")
def index():
    return send_from_directory(WEB_DIR, "index.html")


@app.route("/static/<path:name>")
def static_files(name):
    return send_from_directory(WEB_DIR, name)


@app.route("/api/status")
def status():
    return jsonify({
        "model": REPO,
        "text_loaded": MODELS.text_loaded,
        "vlm_loaded": MODELS.vlm_loaded,
    })


# --------------------------------------------------------------------------
# Chat (text) — token streaming via mlx-lm
# --------------------------------------------------------------------------
@app.route("/api/chat", methods=["POST"])
def chat():
    payload = request.get_json(force=True)
    history = payload.get("messages", [])
    if not history:
        return jsonify({"error": "no messages"}), 400

    def stream():
        from mlx_lm.generate import stream_generate
        try:
            with MODELS.lock:
                model, tok = MODELS.text()
                prompt = tok.apply_chat_template(history, add_generation_prompt=True)
                for resp in stream_generate(model, tok, prompt=prompt, max_tokens=-1):
                    yield sse({"token": resp.text})
            yield sse({"done": True})
        except Exception as e:  # surface model errors to the UI
            yield sse({"error": str(e)})

    return Response(stream(), mimetype="text/event-stream")


# --------------------------------------------------------------------------
# Vision / audio — single response (mlx-vlm)
# --------------------------------------------------------------------------
@app.route("/api/vision", methods=["POST"])
def vision():
    prompt = request.form.get("prompt", "").strip() or "Beskriv detta."
    images, audios, tmpdir = [], [], tempfile.mkdtemp(prefix="gemma-web-")
    try:
        for f in request.files.getlist("images"):
            if f and f.filename:
                p = Path(tmpdir) / f.filename
                f.save(p)
                images.append(str(p))
        for f in request.files.getlist("audio"):
            if f and f.filename:
                p = Path(tmpdir) / f.filename
                f.save(p)
                audios.append(str(p))
        if not images and not audios:
            return jsonify({"error": "ingen bild eller ljud bifogad"}), 400

        from mlx_vlm import generate
        from mlx_vlm.prompt_utils import apply_chat_template
        with MODELS.lock:
            model, processor, config = MODELS.vlm()
            formatted = apply_chat_template(
                processor, config, prompt,
                num_images=len(images), num_audios=len(audios),
            )
            result = generate(
                model, processor, formatted,
                image=images or None, audio=audios or None,
                max_tokens=-1, verbose=False,
            )
        text = result.text if hasattr(result, "text") else str(result)
        return jsonify({"text": text.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        for p in images + audios:
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Subprocess streaming — Photos tagging and yearbook reuse the CLI scripts
# verbatim so there is no logic duplication. We stream their stdout as SSE.
# --------------------------------------------------------------------------
def stream_subprocess(argv: list[str]):
    """Run argv and yield each output line as an SSE 'line' event."""
    def gen():
        try:
            proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, cwd=str(HERE),
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
        except Exception as e:
            yield sse({"error": str(e)})
            return
        # Pump stdout from a thread so Flask can flush each line promptly.
        q: queue.Queue = queue.Queue()

        def pump():
            for line in proc.stdout:
                q.put(line.rstrip("\n"))
            proc.wait()
            q.put(None)

        threading.Thread(target=pump, daemon=True).start()
        while True:
            line = q.get()
            if line is None:
                break
            yield sse({"line": line})
        yield sse({"done": True, "code": proc.returncode})

    return Response(gen(), mimetype="text/event-stream")


@app.route("/api/photos", methods=["POST"])
def photos():
    o = request.get_json(force=True)
    argv = [sys.executable, str(HERE / "photos_caption.py")]
    if o.get("dry_run"):
        argv.append("--dry-run")
    if o.get("no_caption"):
        argv.append("--no-caption")
    if o.get("no_keywords"):
        argv.append("--no-keywords")
    if o.get("replace_keywords"):
        argv.append("--replace-keywords")
    if o.get("explicit_context"):
        argv.append("--explicit-context")
    if o.get("no_context"):
        argv.append("--no-context")
    style = (o.get("style") or "").strip()
    if style:
        argv += ["--style", style]
    return stream_subprocess(argv)


@app.route("/api/yearbook", methods=["POST"])
def yearbook():
    o = request.get_json(force=True)
    argv = [sys.executable, str(HERE / "yearbook.py")]

    # Date range: either --year or --from/--to.
    if o.get("year"):
        argv += ["--year", str(int(o["year"]))]
    if o.get("from_"):
        argv += ["--from", str(o["from_"])]
    if o.get("to"):
        argv += ["--to", str(o["to"])]

    # Plain-valued flags: only forward when the user set them.
    for key, flag in (
        ("count", "--count"), ("album", "--album"), ("holidays", "--holidays"),
        ("keep_per_scene", "--keep-per-scene"),
        ("similarity_threshold", "--similarity-threshold"),
        ("max_per_cluster", "--max-per-cluster"),
        ("max_per_trip", "--max-per-trip"),
        ("min_trip_size", "--min-trip-size"),
        ("min_trip_persons", "--min-trip-persons"),
        ("person_balance", "--person-balance"),
    ):
        val = o.get(key)
        if val not in (None, ""):
            argv += [flag, str(val)]

    for key, flag in (
        ("include_videos", "--include-videos"),
        ("include_screenshots", "--include-screenshots"),
        ("include_no_camera", "--include-no-camera"),
        ("include_no_gps", "--include-no-gps"),
        ("dry_run", "--dry-run"),
    ):
        if o.get(key):
            argv.append(flag)

    return stream_subprocess(argv)


def main():
    ap = argparse.ArgumentParser(prog="gemma-web")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--no-browser", action="store_true",
                    help="Don't auto-open the browser on start.")
    args = ap.parse_args()

    url = f"http://{args.host}:{args.port}"
    print(f"\n  gemma4-mac web UI → {url}")
    print("  Allt körs lokalt på din Mac. Ctrl-C för att avsluta.\n")
    if not args.no_browser:
        # Open after a short delay so the server is accepting connections.
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    # threaded=True so the SSE stream and a parallel /api/status poll coexist.
    app.run(host=args.host, port=args.port, threaded=True)


if __name__ == "__main__":
    main()
