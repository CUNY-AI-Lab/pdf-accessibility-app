"""RunPod serverless handler for Docling document structure extraction."""

import base64
import json
import tempfile
from pathlib import Path

import runpod

# ---------------------------------------------------------------------------
# Load the Docling converter ONCE at module level so it persists across jobs.
# Model loading happens here (during cold start), not per-request.
# ---------------------------------------------------------------------------
from docling.datamodel.base_models import InputFormat
from docling.datamodel.pipeline_options import PdfPipelineOptions
from docling.document_converter import DocumentConverter, PdfFormatOption

pipeline_options = PdfPipelineOptions(
    generate_picture_images=True,
    images_scale=2.0,
    do_picture_classification=True,
    do_table_structure=True,
)

CONVERTER = DocumentConverter(
    format_options={
        InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options),
    }
)

print("Docling converter loaded and ready.")


def handler(event):
    """Process a PDF and return Docling's structured JSON output.

    Expected input:
        {
            "pdf_base64": "<base64-encoded PDF bytes>",
            "filename": "optional-name.pdf"
        }

    Returns:
        Docling's export_to_dict() output — the same JSON your existing
        structure.py normalises into elements.
    """
    job_input = event["input"]
    pdf_b64 = job_input.get("pdf_base64")
    if not pdf_b64:
        return {"error": "pdf_base64 is required"}

    filename = job_input.get("filename", "input.pdf")

    # Write PDF to a temp file (Docling needs a file path)
    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = Path(tmp) / filename
        pdf_path.write_bytes(base64.b64decode(pdf_b64))

        # Run conversion
        conv_result = CONVERTER.convert(str(pdf_path))
        doc = conv_result.document
        doc_dict = doc.export_to_dict()

        # Extract figure images as base64
        figures = []
        for i, pic in enumerate(doc.pictures if hasattr(doc, "pictures") else []):
            try:
                img = pic.get_image(conv_result)
                if img:
                    import io

                    buf = io.BytesIO()
                    img.save(buf, format="PNG")
                    figures.append(
                        {
                            "index": i,
                            "image_base64": base64.b64encode(
                                buf.getvalue()
                            ).decode(),
                            "page": (pic.prov[0].page_no - 1)
                            if pic.prov
                            else None,
                        }
                    )
            except Exception as e:
                print(f"Failed to extract figure {i}: {e}")

        return {
            "document": doc_dict,
            "figures": figures,
            "page_count": len(doc_dict.get("pages", {})),
        }


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
