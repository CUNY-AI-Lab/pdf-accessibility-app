"""Step 3: Extract document structure using IBM Docling."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class FigureInfo:
    index: int
    path: Path
    caption: str | None = None
    page: int | None = None


@dataclass
class StructureResult:
    document_json: dict
    figures: list[FigureInfo] = field(default_factory=list)
    page_count: int = 0
    headings_count: int = 0
    tables_count: int = 0
    figures_count: int = 0


async def extract_structure(
    pdf_path: Path,
    job_dir: Path,
) -> StructureResult:
    """Extract document structure using Docling.

    Falls back to a basic pikepdf-based extraction if Docling is not installed.
    """
    try:
        return await _extract_with_docling(pdf_path, job_dir)
    except ImportError:
        logger.warning("Docling not installed, using basic extraction")
        return await _extract_basic(pdf_path, job_dir)


async def _extract_with_docling(pdf_path: Path, job_dir: Path) -> StructureResult:
    """Full structure extraction via Docling."""

    def _convert():
        from docling.document_converter import DocumentConverter

        converter = DocumentConverter()
        conv_result = converter.convert(str(pdf_path))
        doc = conv_result.document

        # Export structure
        doc_dict = doc.export_to_dict()

        # Extract and save figure images
        figures = []
        figures_dir = job_dir / "figures"
        figures_dir.mkdir(exist_ok=True)

        for i, element in enumerate(doc.pictures if hasattr(doc, 'pictures') else []):
            try:
                img = element.get_image(conv_result)
                if img:
                    fig_path = figures_dir / f"figure_{i}.png"
                    img.save(str(fig_path), "PNG")
                    figures.append(FigureInfo(
                        index=i,
                        path=fig_path,
                        caption=getattr(element, 'caption', None),
                    ))
            except Exception as e:
                logger.warning(f"Failed to extract figure {i}: {e}")

        # Count elements
        headings = sum(
            1 for item in doc_dict.get("texts", [])
            if item.get("label", "").startswith("section_header")
        )
        tables = len(doc_dict.get("tables", []))

        return StructureResult(
            document_json=doc_dict,
            figures=figures,
            page_count=doc_dict.get("pages", {}).get("count", 0),
            headings_count=headings,
            tables_count=tables,
            figures_count=len(figures),
        )

    return await asyncio.to_thread(_convert)


async def _extract_basic(pdf_path: Path, job_dir: Path) -> StructureResult:
    """Basic fallback extraction using pikepdf when Docling is not available."""

    def _extract():
        import pikepdf

        with pikepdf.open(str(pdf_path)) as pdf:
            page_count = len(pdf.pages)

            # Build a simple structure from PDF metadata
            structure: dict = {
                "source": str(pdf_path.name),
                "page_count": page_count,
                "elements": [],
            }

            # Try to extract images from pages for alt text processing
            figures = []
            figures_dir = job_dir / "figures"
            figures_dir.mkdir(exist_ok=True)

            fig_idx = 0
            for page_num, page in enumerate(pdf.pages):
                try:
                    resources = page.get("/Resources", {})
                    xobjects = resources.get("/XObject", {})
                    for name, xobj_ref in xobjects.items():
                        xobj = xobj_ref
                        if hasattr(xobj, 'keys') and xobj.get("/Subtype") == pikepdf.Name("/Image"):
                            try:
                                raw = xobj.read_raw_bytes()
                                width = int(xobj.get("/Width", 0))
                                height = int(xobj.get("/Height", 0))
                                # Only extract meaningful images (not tiny icons)
                                if width > 50 and height > 50:
                                    from PIL import Image
                                    import io

                                    # Try to decode the image
                                    pil_img = None
                                    filter_type = xobj.get("/Filter")

                                    if filter_type == pikepdf.Name("/DCTDecode"):
                                        pil_img = Image.open(io.BytesIO(raw))
                                    elif filter_type == pikepdf.Name("/FlateDecode"):
                                        import zlib
                                        try:
                                            decompressed = zlib.decompress(raw)
                                            cs = str(xobj.get("/ColorSpace", ""))
                                            if "RGB" in cs:
                                                mode = "RGB"
                                            else:
                                                mode = "L"
                                            pil_img = Image.frombytes(
                                                mode, (width, height), decompressed
                                            )
                                        except Exception:
                                            pass

                                    if pil_img:
                                        fig_path = figures_dir / f"figure_{fig_idx}.png"
                                        pil_img.save(str(fig_path), "PNG")
                                        figures.append(FigureInfo(
                                            index=fig_idx,
                                            path=fig_path,
                                            page=page_num,
                                        ))
                                        fig_idx += 1
                            except Exception as e:
                                logger.debug(f"Could not extract image: {e}")
                except Exception as e:
                    logger.debug(f"Error processing page {page_num}: {e}")

            structure["figures_count"] = len(figures)

            return StructureResult(
                document_json=structure,
                figures=figures,
                page_count=page_count,
                headings_count=0,
                tables_count=0,
                figures_count=len(figures),
            )

    return await asyncio.to_thread(_extract)
