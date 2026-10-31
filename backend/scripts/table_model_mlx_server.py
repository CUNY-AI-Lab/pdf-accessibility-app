"""PaddleOCR-VL-1.5 on Apple silicon (MLX), behind a minimal OpenAI-compatible
chat endpoint, for measuring the table-model step on a Mac.

    python table_model_mlx_server.py --port 8141 --log requests.jsonl [--idle-minutes 5] [--queue 1]

It behaves as the proposed production server should (see
deploy/actual-dell/table-model.compose.yml):

- the model loads on the first request and unloads after --idle-minutes
  without one;
- one request runs at a time; with --queue requests already waiting, the
  next is answered 503 at once, so the app keeps Docling's table instead of
  waiting;
- --memory-gb caps MLX's allocations (mx.set_memory_limit).

POST /v1/chat/completions takes one image and the prompt; GET /health reports
whether the model is loaded and the peak memory so far. Each request is
logged as a JSON line with its timing, tokens and peak memory.
"""

from __future__ import annotations

import argparse
import base64
import gc
import io
import json
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import mlx.core as mx
from mlx_vlm import generate, load
from mlx_vlm.prompt_utils import apply_chat_template
from PIL import Image

MODEL = "mlx-community/PaddleOCR-VL-1.5-bf16"
STATE: dict = {"model": None, "processor": None, "last": 0.0, "waiting": 0, "loads": 0}
LOCK = threading.Lock()
GUARD = threading.Lock()


def ensure_loaded() -> None:
    if STATE["model"] is None:
        started = time.monotonic()
        STATE["model"], STATE["processor"] = load(STATE["path"])
        STATE["loads"] += 1
        STATE["load_seconds"] = round(time.monotonic() - started, 2)
        log({"event": "load", "seconds": STATE["load_seconds"],
             "active_gb": round(mx.get_active_memory() / 1e9, 2)})


def unload_when_idle() -> None:
    while True:
        time.sleep(15)
        with LOCK:
            idle = time.monotonic() - STATE["last"]
            if STATE["model"] is not None and idle > STATE["idle_seconds"]:
                STATE["model"] = STATE["processor"] = None
                gc.collect()
                mx.clear_cache()
                log({"event": "unload", "idle_seconds": round(idle),
                     "active_gb": round(mx.get_active_memory() / 1e9, 2)})


def answer(body: dict) -> dict:
    image, prompt_text = None, ""
    for message in body["messages"]:
        content = message.get("content")
        parts = content if isinstance(content, list) else [{"type": "text", "text": content}]
        for part in parts:
            if part.get("type") == "image_url":
                data = base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
                image = Image.open(io.BytesIO(data)).convert("RGB")
            elif part.get("type") == "text":
                prompt_text = part["text"]
    if image is None:
        raise ValueError("no image")
    ensure_loaded()
    model, processor = STATE["model"], STATE["processor"]
    prompt = apply_chat_template(processor, model.config, prompt_text, num_images=1)
    max_tokens = int(body.get("max_tokens") or 4096)
    mx.reset_peak_memory()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "crop.png"
        image.save(path)
        started = time.monotonic()
        result = generate(model, processor, prompt, image=[str(path)], max_tokens=max_tokens,
                          temperature=0.0, verbose=False, skip_special_tokens=False)
        seconds = time.monotonic() - started
    finish = "length" if result.generation_tokens >= max_tokens else "stop"
    return {
        "id": "local",
        "object": "chat.completion",
        "model": body.get("model"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": result.text},
                     "finish_reason": finish}],
        "usage": {"prompt_tokens": result.prompt_tokens,
                  "completion_tokens": result.generation_tokens,
                  "total_tokens": result.prompt_tokens + result.generation_tokens},
        "timing": {"seconds": round(seconds, 3), "pixels": image.width * image.height,
                   "peak_memory_gb": round(mx.get_peak_memory() / 1e9, 3)},
    }


def log(record: dict) -> None:
    record["at"] = time.strftime("%H:%M:%S")
    with STATE["log"].open("a") as handle:
        handle.write(json.dumps(record) + "\n")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/health"):
            self.send(200, {"loaded": STATE["model"] is not None, "loads": STATE["loads"],
                            "active_gb": round(mx.get_active_memory() / 1e9, 3),
                            "peak_gb": round(mx.get_peak_memory() / 1e9, 3)})
        else:
            self.send(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self.send(404, {"error": {"message": "not found"}})
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        with GUARD:
            if STATE["waiting"] >= STATE["queue"]:
                self.send(503, {"error": {"message": "busy", "code": "busy"}})
                log({"event": "busy"})
                return
            STATE["waiting"] += 1
        queued = time.monotonic()
        try:
            with LOCK:
                waited = time.monotonic() - queued
                try:
                    payload = answer(body)
                except Exception as exc:  # noqa: BLE001 - reported to the client as a 500
                    log({"event": "error", "error": repr(exc)})
                    self.send(500, {"error": {"message": repr(exc)}})
                    return
                finally:
                    STATE["last"] = time.monotonic()
        finally:
            with GUARD:
                STATE["waiting"] -= 1
        log({"event": "request", "waited": round(waited, 3), **payload["usage"],
             **payload["timing"], "finish": payload["choices"][0]["finish_reason"]})
        self.send(200, payload)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--idle-minutes", type=float, default=5.0)
    parser.add_argument("--queue", type=int, default=4, help="requests allowed to wait or run")
    parser.add_argument("--memory-gb", type=float, help="cap MLX allocations")
    options = parser.parse_args()
    if options.memory_gb:
        mx.set_memory_limit(int(options.memory_gb * 1e9))
    STATE.update(path=options.model, log=options.log, idle_seconds=options.idle_minutes * 60,
                 queue=options.queue, last=time.monotonic())
    threading.Thread(target=unload_when_idle, daemon=True).start()
    print(f"serving {options.model} on 127.0.0.1:{options.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", options.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
