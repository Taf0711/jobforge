"""Cheap ATS/layout checks against the generated PDF."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pymupdf

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
PHONE = re.compile(r"(\+?\d[\d\s().-]{7,}\d)")
REQUIRED_SECTIONS = ("experience", "education", "skills")


def ats_check(pdf_path: str | Path) -> dict[str, Any]:
    path = Path(pdf_path)
    errors: list[str] = []
    warnings: list[str] = []
    if not path.exists():
        return {"ok": False, "errors": [f"Missing PDF: {path}"], "warnings": [], "pages": 0, "chars": 0, "images": 0}

    doc = pymupdf.open(path)
    pages = len(doc)
    text = "\n".join(page.get_text("text") for page in doc)
    images = sum(len(page.get_images(full=True)) for page in doc)
    chars = len(text.strip())

    if pages > 1:
        warnings.append(f"Resume is {pages} pages; one page is safer for early-career roles")
    if chars < 200:
        errors.append(f"Text layer is too short ({chars} chars); ATS may not parse it")
    if not EMAIL.search(text):
        warnings.append("No email found in the text layer")
    if not PHONE.search(text):
        warnings.append("No phone number found in the text layer")
    lowered = text.lower()
    for section in REQUIRED_SECTIONS:
        if section not in lowered:
            warnings.append(f"Section header not detected: {section}")
    if images:
        warnings.append(f"{images} embedded image(s) found; prefer a pure-text PDF")
    if "\ufffd" in text:
        errors.append("Unicode replacement character found; a glyph failed to render")

    doc.close()
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "pages": pages,
        "chars": chars,
        "images": images,
    }
