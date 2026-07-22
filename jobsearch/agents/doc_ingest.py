"""Extract plain text from input documents (résumé, JD, brag doc, …).

Supports .txt/.md/.csv/.json natively; .pdf via pypdf; .docx via python-docx.
Missing optional libs raise a clear DocIngestError the caller can surface.
"""

from __future__ import annotations

import io


class DocIngestError(Exception):
    pass


_TEXT_EXTS = {"txt", "md", "markdown", "csv", "json", "text", "rtf", "log"}


def _ext(filename: str) -> str:
    return filename.lower().rsplit(".", 1)[-1] if "." in filename else ""


def extract_text(filename: str, data: bytes) -> str:
    """Return extracted text for a document given its name and raw bytes."""
    ext = _ext(filename)

    if ext in _TEXT_EXTS or not ext:
        return data.decode("utf-8", errors="replace")

    if ext == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError as e:  # pragma: no cover
            raise DocIngestError("pip install pypdf to ingest PDF files") from e
        try:
            reader = PdfReader(io.BytesIO(data))
            return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
        except Exception as e:
            raise DocIngestError(f"could not read PDF: {e}") from e

    if ext in ("docx", "doc"):
        try:
            from docx import Document
        except ImportError as e:  # pragma: no cover
            raise DocIngestError("pip install python-docx to ingest .docx files") from e
        try:
            doc = Document(io.BytesIO(data))
            return "\n".join(p.text for p in doc.paragraphs).strip()
        except Exception as e:
            raise DocIngestError(f"could not read .docx: {e}") from e

    # Unknown extension — best-effort text decode.
    return data.decode("utf-8", errors="replace")
