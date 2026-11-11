"""Keep the complete encoded vision request within its transport budget."""

import base64
import copy
import json
import math
from io import BytesIO
from typing import Any

from PIL import Image


class VisionRequestTooLarge(ValueError):
    """The request cannot fit without dropping text or making images unusable."""


def request_body_size(body: dict[str, Any]) -> int:
    # Match httpx's JSON encoding; tests also measure the actual wire body.
    return len(
        json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    )


def _inline_images(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [image for child in value for image in _inline_images(child)]
    if not isinstance(value, dict):
        return []
    image = value.get("image_url")
    if value.get("type") == "image_url" and isinstance(image, dict):
        url = image.get("url")
        if isinstance(url, str) and url.startswith("data:image/"):
            return [image]
    return [image for child in value.values() for image in _inline_images(child)]


def _fit_image(url: str, budget: int) -> str:
    if len(url) <= budget:
        return url
    header, encoded = url.split(",", 1)
    if not header.endswith(";base64"):
        raise VisionRequestTooLarge("Oversized inline images must use base64 encoding.")
    with Image.open(BytesIO(base64.b64decode(encoded, validate=True))) as source:
        # Preserve transparency against the PDF preview's white background.
        if "A" in source.getbands() or "transparency" in source.info:
            rgba = source.convert("RGBA")
            image = Image.new("RGB", rgba.size, "white")
            image.paste(rgba, mask=rgba.getchannel("A"))
        else:
            image = source.convert("RGB")
    while True:
        for quality in (85, 75):
            buffer = BytesIO()
            image.save(buffer, format="JPEG", quality=quality, optimize=True)
            candidate = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode(
                "ascii"
            )
            if len(candidate) <= budget:
                return candidate
        # Keep all of the crop, maintaining its aspect ratio. Do not send an
        # illegible one-pixel substitute simply to make a request fit.
        if max(image.size) <= 64 or budget <= 0:
            raise VisionRequestTooLarge("Image budget is too small for a usable vision request.")
        scale = min(0.85, math.sqrt(budget / len(candidate)) * 0.9)
        side = max(64, int(max(image.size) * scale))
        image.thumbnail((side, side), Image.Resampling.LANCZOS)


def bound_vision_request(body: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    """Resize/recompress inline images only when the whole JSON body is too big.

    Text, schemas, history, image count and source objects remain unchanged.
    The budget includes base64 overhead. A request that cannot fit fails before
    transport instead of silently discarding evidence.
    """
    if request_body_size(body) < max_bytes:
        return body
    bounded = copy.deepcopy(body)
    images = _inline_images(bounded)
    urls = [image["url"] for image in images]
    for image in images:
        image["url"] = ""
    available = max_bytes - 1 - request_body_size(bounded)
    if not images or available <= 0:
        raise VisionRequestTooLarge("Request text and schema exceed the configured body limit.")

    # Give large images the space left by small ones, rather than reducing
    # a small but useful crop just because its page preview is much larger.
    pending = list(range(len(images)))
    while pending:
        share = available // len(pending)
        small = [index for index in pending if len(urls[index]) <= share]
        if not small:
            for index in pending:
                images[index]["url"] = _fit_image(urls[index], share)
            break
        for index in small:
            images[index]["url"] = urls[index]
            available -= len(urls[index])
        pending = [index for index in pending if index not in small]
    if request_body_size(bounded) >= max_bytes:
        raise VisionRequestTooLarge("Vision request still exceeds the configured body limit.")
    return bounded
