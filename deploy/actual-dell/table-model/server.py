"""PaddleOCR-VL-1.5 with transformers behind a minimal OpenAI-compatible chat
endpoint: the serving design sketched in deploy/actual-dell/table-model.compose.yml, runnable
on CUDA, Apple MPS or CPU.

    python server.py --port 8142 --log requests.jsonl \\
        [--device cuda|mps|cpu] [--idle-minutes 5] [--vram-gb 4] [--min-free-gb 6]

- The model loads on the first request and is dropped after --idle-minutes
  without one (on CUDA its memory goes back to the driver; the process keeps
  only its CUDA context).
- One request runs at a time; a request arriving while one runs is answered
  503 at once, so the app keeps Docling's table instead of waiting.
- On CUDA, --vram-gb caps PyTorch's allocations
  (torch.cuda.set_per_process_memory_fraction): past it a request fails with
  out-of-memory instead of growing into other users' memory. Before loading,
  the server checks that the GPU has --min-free-gb free and answers 503 if
  not (another user holds the card).

POST /v1/chat/completions takes one image and the prompt; GET /health reports
the state. Each request is logged as a JSON line.
"""

from __future__ import annotations

import argparse
import base64
import gc
import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import torch
from PIL import Image
from transformers import AutoModelForImageTextToText, AutoProcessor

MODEL = "PaddlePaddle/PaddleOCR-VL-1.5"
STATE: dict = {"model": None, "processor": None, "last": 0.0, "loads": 0}
RUN = threading.Lock()


def log(record: dict) -> None:
    record["at"] = time.strftime("%H:%M:%S")
    with STATE["log"].open("a") as handle:
        handle.write(json.dumps(record) + "\n")


def gpu_free_gb() -> float | None:
    if STATE["device"] != "cuda":
        return None
    free, _ = torch.cuda.mem_get_info()
    return free / 1e9


class Busy(Exception):
    pass


def ensure_loaded() -> None:
    if STATE["model"] is not None:
        return
    free = gpu_free_gb()
    if free is not None and free < STATE["min_free_gb"]:
        raise Busy(f"GPU has {free:.1f} GB free")
    started = time.monotonic()
    dtype = torch.float32 if STATE["device"] == "cpu" else torch.bfloat16
    model = AutoModelForImageTextToText.from_pretrained(STATE["path"], dtype=dtype)
    STATE["model"] = model.to(STATE["device"]).eval()
    STATE["processor"] = AutoProcessor.from_pretrained(STATE["path"])
    STATE["loads"] += 1
    log({"event": "load", "seconds": round(time.monotonic() - started, 2), **memory()})


def unload() -> None:
    STATE["model"] = STATE["processor"] = None
    gc.collect()
    if STATE["device"] == "cuda":
        torch.cuda.empty_cache()
    elif STATE["device"] == "mps":
        torch.mps.empty_cache()


def memory() -> dict:
    if STATE["device"] == "cuda":
        return {"allocated_gb": round(torch.cuda.memory_allocated() / 1e9, 3),
                "peak_gb": round(torch.cuda.max_memory_allocated() / 1e9, 3)}
    if STATE["device"] == "mps":
        return {"allocated_gb": round(torch.mps.current_allocated_memory() / 1e9, 3)}
    return {}


def unload_when_idle() -> None:
    while True:
        time.sleep(15)
        with RUN:
            idle = time.monotonic() - STATE["last"]
            if STATE["model"] is not None and idle > STATE["idle_seconds"]:
                unload()
                log({"event": "unload", "idle_seconds": round(idle), **memory()})


def answer(body: dict) -> dict:
    image, prompt = None, ""
    for message in body["messages"]:
        content = message.get("content")
        parts = content if isinstance(content, list) else [{"type": "text", "text": content}]
        for part in parts:
            if part.get("type") == "image_url":
                data = base64.b64decode(part["image_url"]["url"].split(",", 1)[1])
                image = Image.open(io.BytesIO(data)).convert("RGB")
            elif part.get("type") == "text":
                prompt = part["text"]
    if image is None:
        raise ValueError("no image")
    ensure_loaded()
    model, processor = STATE["model"], STATE["processor"]
    messages = [{"role": "user", "content": [{"type": "image", "image": image},
                                             {"type": "text", "text": prompt}]}]
    # The processor's own pixel bounds (112,896 to 1,003,520) are the model
    # card's; transformers 5.20's native processor has no min_pixels.
    inputs = processor.apply_chat_template(
        messages, add_generation_prompt=True, tokenize=True, return_dict=True,
        return_tensors="pt",
    ).to(model.device)
    max_tokens = int(body.get("max_tokens") or 4096)
    if STATE["device"] == "cuda":
        torch.cuda.reset_peak_memory_stats()
    started = time.monotonic()
    with torch.inference_mode():
        # The model's config.json sets use_cache false; without the KV
        # cache every new token recomputes the whole sequence.
        output = model.generate(
            **inputs, max_new_tokens=max_tokens, do_sample=False, use_cache=True
        )
    seconds = time.monotonic() - started
    prompt_tokens = int(inputs["input_ids"].shape[-1])
    generated = output[0][prompt_tokens:]
    text = processor.decode(generated, skip_special_tokens=False)
    for end in ("</s>", "<|end_of_sentence|>"):
        text = text.removesuffix(end)
    finish = "length" if len(generated) >= max_tokens else "stop"
    return {
        "id": "local", "object": "chat.completion", "model": body.get("model"),
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                     "finish_reason": finish}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": int(len(generated)),
                  "total_tokens": prompt_tokens + int(len(generated))},
        "timing": {"seconds": round(seconds, 3), "pixels": image.width * image.height,
                   **memory()},
    }


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
                            "device": STATE["device"], "gpu_free_gb": gpu_free_gb(), **memory()})
        else:
            self.send(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self.send(404, {"error": {"message": "not found"}})
            return
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        if not RUN.acquire(blocking=False):
            log({"event": "busy"})
            self.send(503, {"error": {"message": "busy", "code": "busy"}})
            return
        try:
            payload = answer(body)
        except Busy as exc:
            log({"event": "gpu_busy", "reason": str(exc)})
            self.send(503, {"error": {"message": str(exc), "code": "busy"}})
            return
        except Exception as exc:  # noqa: BLE001 - reported to the client as a 500
            log({"event": "error", "error": repr(exc)})
            self.send(500, {"error": {"message": repr(exc)}})
            return
        finally:
            STATE["last"] = time.monotonic()
            RUN.release()
        log({"event": "request", **payload["usage"], **payload["timing"],
             "finish": payload["choices"][0]["finish_reason"]})
        self.send(200, payload)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--device", default="cuda" if torch.cuda.is_available() else "cpu")
    parser.add_argument("--idle-minutes", type=float, default=5.0)
    parser.add_argument("--vram-gb", type=float, default=4.0, help="CUDA: cap PyTorch's memory")
    parser.add_argument("--min-free-gb", type=float, default=6.0,
                        help="CUDA: load only when this much is free")
    parser.add_argument("--threads", type=int, help="CPU: torch threads")
    options = parser.parse_args()
    if options.threads:
        torch.set_num_threads(options.threads)
    if options.device == "cuda":
        total = torch.cuda.get_device_properties(0).total_memory / 1e9
        torch.cuda.set_per_process_memory_fraction(min(1.0, options.vram_gb / total))
    STATE.update(path=options.model, log=options.log, device=options.device,
                 idle_seconds=options.idle_minutes * 60, min_free_gb=options.min_free_gb,
                 last=time.monotonic())
    threading.Thread(target=unload_when_idle, daemon=True).start()
    print(f"serving {options.model} on {options.host}:{options.port} ({options.device})", flush=True)
    ThreadingHTTPServer((options.host, options.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
