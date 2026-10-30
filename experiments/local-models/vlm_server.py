"""A minimal OpenAI-compatible chat server for one MLX vision model.

    vlm_server.py --model <hf repo or path> --name <served id> --port 8131 --cache <dir>

Serves POST /v1/chat/completions for the one model id ``--name`` and answers
404 for any other id, so the app's AI steps (which name another model) fail at
once. Requests run one at a time (one GPU). Each answer is kept on disk keyed
by the request (model, messages, temperature, max_tokens), so a re-run of the
same page image is answered without the model. Every request is logged as a
JSON line with its timing.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import mlx.core as mx
from mlx_vlm import generate, load
from PIL import Image

LOCK = threading.Lock()
STATE: dict = {}


def hf_messages(messages: list[dict]) -> tuple[list[dict], list[Image.Image]]:
    """OpenAI chat messages as the processor's chat template takes them, with
    each image's place kept, and the images in order."""
    out, images = [], []
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            out.append({"role": message["role"], "content": [{"type": "text", "text": content}]})
            continue
        parts = []
        for part in content or []:
            if part.get("type") == "text":
                parts.append({"type": "text", "text": part["text"]})
            elif part.get("type") == "image_url":
                url = part["image_url"]["url"]
                data = base64.b64decode(url.split(",", 1)[1])
                images.append(Image.open(io.BytesIO(data)).convert("RGB"))
                parts.append({"type": "image"})
        out.append({"role": message["role"], "content": parts})
    return out, images


def answer(body: dict) -> dict:
    model, processor = STATE["model"], STATE["processor"]
    messages, images = hf_messages(body["messages"])
    prompt = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    max_tokens = int(body.get("max_tokens") or 4096)
    temperature = float(body.get("temperature") or 0.0)
    with tempfile.TemporaryDirectory() as tmp:
        paths = []
        for index, image in enumerate(images):
            path = Path(tmp) / f"{index}.png"
            image.save(path)
            paths.append(str(path))
        mx.random.seed(0)
        started = time.monotonic()
        result = generate(
            model,
            processor,
            prompt,
            image=paths or None,
            max_tokens=max_tokens,
            temperature=temperature,
            verbose=False,
        )
        seconds = time.monotonic() - started
    finish = result.finish_reason or (
        "length" if result.generation_tokens >= max_tokens else "stop"
    )
    return {
        "id": "local",
        "object": "chat.completion",
        "model": body["model"],
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": result.text},
                "finish_reason": finish,
            }
        ],
        "usage": {
            "prompt_tokens": result.prompt_tokens,
            "completion_tokens": result.generation_tokens,
            "total_tokens": result.prompt_tokens + result.generation_tokens,
        },
        "timing": {
            "seconds": round(seconds, 2),
            "prompt_tps": round(result.prompt_tps, 1),
            "generation_tps": round(result.generation_tps, 1),
            "peak_memory_gb": round(result.peak_memory, 2),
        },
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # requests are logged as JSON lines instead
        pass

    def send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        if self.path.rstrip("/").endswith("/models"):
            self.send(200, {"object": "list", "data": [{"id": STATE["name"], "object": "model"}]})
        else:
            self.send(404, {"error": {"message": "not found", "code": "not_found"}})

    def do_POST(self) -> None:
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self.send(404, {"error": {"message": "not found", "code": "not_found"}})
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        if body.get("model") != STATE["name"]:
            self.send(404, {"error": {"message": f"model {body.get('model')!r} is not served here",
                                      "code": "model_not_found"}})
            return
        key = hashlib.sha256(
            json.dumps(
                {k: body.get(k) for k in ("model", "messages", "temperature", "max_tokens")},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        cached = STATE["cache"] / f"{key}.json"
        queued = time.monotonic()
        record = {"key": key[:16], "page": self.headers.get("X-Bench-Page")}
        if cached.exists():
            payload = json.loads(cached.read_text())
            record.update(cache="hit")
        else:
            with LOCK:
                waited = time.monotonic() - queued
                try:
                    payload = answer(body)
                except Exception as exc:  # noqa: BLE001 - reported to the client as a 500
                    record.update(error=repr(exc))
                    self.log(record)
                    self.send(500, {"error": {"message": repr(exc), "code": "server_error"}})
                    return
            tmp = cached.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(cached)
            record.update(cache="miss", waited=round(waited, 1), **payload["usage"],
                          **payload["timing"], finish=payload["choices"][0]["finish_reason"],
                          temperature=body.get("temperature"))
        self.log(record)
        self.send(200, payload)

    def log(self, record: dict) -> None:
        record["at"] = time.strftime("%H:%M:%S")
        with STATE["log"].open("a") as handle:
            handle.write(json.dumps(record) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    options = parser.parse_args()
    options.cache.mkdir(parents=True, exist_ok=True)
    model, processor = load(options.model)
    STATE.update(model=model, processor=processor, name=options.name, cache=options.cache,
                 log=options.log)
    print(f"serving {options.model} as {options.name} on {options.port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", options.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
