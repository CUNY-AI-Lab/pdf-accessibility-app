import base64, io, json, sys, time, httpx, pypdfium2 as pdfium
sys.path.insert(0, "data/exp")
from ocr_local_plugin import OLMOCR_PROMPT, natural_text
pdf, prompt_kind = sys.argv[1], sys.argv[2]
page = pdfium.PdfDocument(pdf)[0]
w, h = page.get_size()
img = page.render(scale=1288 / max(w, h)).to_pil()
buf = io.BytesIO(); img.save(buf, format="PNG")
url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
if prompt_kind == "olmocr":
    messages = [{"role": "user", "content": [{"type": "text", "text": OLMOCR_PROMPT}, {"type": "image_url", "image_url": {"url": url}}]}]
else:
    sys.path.insert(0, ".")
    from app.pipeline.ocr_vlm_plugin import SYSTEM_PROMPT
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": [{"type": "text", "text": "Return the lines of this page."}, {"type": "image_url", "image_url": {"url": url}}]}]
t = time.time()
r = httpx.post("http://127.0.0.1:8131/v1/chat/completions", json={"model": "olmocr-2-7b-1025-mlx-8bit", "temperature": 0.1, "max_tokens": int(sys.argv[3]) if len(sys.argv) > 3 else 8000, "messages": messages}, timeout=900).json()
print(round(time.time() - t, 1), r["usage"], r.get("timing"), r["choices"][0]["finish_reason"])
print(r["choices"][0]["message"]["content"][:1500])
