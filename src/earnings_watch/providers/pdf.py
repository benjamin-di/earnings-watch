"""Bounded PDF text extraction for official issuer and exchange documents."""

from __future__ import annotations

import io
import re

from pypdf import PdfReader


def _normalize_text(raw: str) -> str:
    lines = [" ".join(line.split()) for line in raw.splitlines()]
    return "\n".join(line for line in lines if line)


def extract_pdf_text(data: bytes, *, max_pages: int = 5, max_chars: int = 150000) -> str:
    """Extract clean, whitespace-normalized text from a PDF, bounded by pages and chars."""
    if not data:
        raise ValueError("PDF payload is empty")
    if max_pages < 1 or max_chars < 1:
        raise ValueError("PDF extraction limits must be positive")
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            result = reader.decrypt("")
            if not result:
                raise ValueError("PDF is encrypted and unreadable")
        chunks: list[str] = []
        total = 0
        for page in reader.pages[:max_pages]:
            extracted = _normalize_text(page.extract_text() or "")
            if not extracted:
                continue
            chunks.append(extracted)
            total += len(extracted) + 1
            if total >= max_chars:
                break
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("PDF is unreadable") from exc

    text = re.sub(r"\n{3,}", "\n\n", "\n".join(chunks))[:max_chars].strip()
    if not text:
        raise ValueError("PDF contains no extractable text")
    return text
