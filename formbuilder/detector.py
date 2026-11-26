from __future__ import annotations

import re
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any

import fitz

from formbuilder.template import make_field, source_as_pdf_bytes

LABEL_RULES: list[tuple[re.Pattern[str], str, str, tuple[float, float]]] = [
    (re.compile(r"\b(account\s+title|account\s+holder|customer\s+name|full\s+name|name)\b", re.I), "customer_name", "text", (190, 20)),
    (re.compile(r"\b(account\s+(number|no\.?|#)|a/c\s*(number|no\.?|#))\b", re.I), "account_number", "text", (150, 20)),
    (re.compile(r"\b(cnic|national\s+id|identity\s+card|id\s+number)\b", re.I), "cnic", "text", (145, 20)),
    (re.compile(r"\b(mobile\s+(number|no\.?|#)|phone\s+(number|no\.?|#)|contact\s+(number|no\.?|#)|mobile|telephone)\b", re.I), "mobile_number", "text", (145, 20)),
    (re.compile(r"\b(email\s+address|email)\b", re.I), "email", "text", (180, 20)),
    (re.compile(r"\b(date\s+of\s+birth|date\s+of\s+application|application\s+date|date|dob)\b", re.I), "date_of_application", "text", (105, 20)),
    (re.compile(r"\b(initial\s+deposit|deposit\s+amount|amount|deposit)\b", re.I), "initial_deposit", "text", (140, 20)),
    (re.compile(r"\b(signature|sign\s+here)\b", re.I), "signature", "signature", (180, 42)),
]


def _pdf_rect_to_top_left(rect: tuple[float, float, float, float], page_height: float) -> list[float]:
    x0, y0, x1, y1 = rect
    return [float(x0), float(page_height - y1), float(x1), float(page_height - y0)]


def _guess_key(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    for pattern, key, _, _ in LABEL_RULES:
        if pattern.search(cleaned):
            return key
    return re.sub(r"\s+", "_", cleaned) or ""


def _overlaps(a: list[float], b: list[float]) -> bool:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    iw = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    ih = max(0.0, min(ay1, by1) - max(ay0, by0))
    intersection = iw * ih
    smaller = min((ax1 - ax0) * (ay1 - ay0), (bx1 - bx0) * (by1 - by0))
    return smaller > 0 and intersection / smaller > 0.45


def _line_words(words: list[tuple[Any, ...]]) -> dict[tuple[int, int], list[tuple[Any, ...]]]:
    grouped: dict[tuple[int, int], list[tuple[Any, ...]]] = defaultdict(list)
    for word in words:
        if len(word) >= 7:
            grouped[(int(word[5]), int(word[6]))].append(word)
    return grouped


def _label_proposals(page: fitz.Page, page_index: int, *, ocr_used: bool) -> list[dict[str, Any]]:
    words = page.get_text("words")
    if not words and shutil.which("tesseract"):
        try:
            text_page = page.get_textpage_ocr(language="eng", dpi=220, full=True)
            words = page.get_text("words", textpage=text_page)
            ocr_used = bool(words)
        except Exception:
            words = []
    candidates: list[dict[str, Any]] = []
    for line_words in _line_words(words).values():
        line_words.sort(key=lambda item: float(item[0]))
        # Avoid matching terms buried in paragraphs, declarations or footers.
        if len(line_words) > 8:
            continue
        line_text = " ".join(str(item[4]) for item in line_words)
        for pattern, data_key, field_type, size in LABEL_RULES:
            match = pattern.search(line_text)
            if not match:
                continue
            left = min(float(item[0]) for item in line_words)
            top = min(float(item[1]) for item in line_words)
            right = max(float(item[2]) for item in line_words)
            bottom = max(float(item[3]) for item in line_words)
            field_width, field_height = size
            if right + field_width + 6 < page.rect.width:
                x0, y0 = right + 6, max(0.0, top - 1)
                x1, y1 = min(page.rect.width - 6, x0 + field_width), min(page.rect.height, bottom + 4)
                if x1 - x0 < 50:
                    x0, y0 = left, min(page.rect.height - field_height, bottom + 3)
                    x1, y1 = min(page.rect.width - 6, x0 + field_width), min(page.rect.height, y0 + field_height)
            else:
                x0, y0 = left, min(page.rect.height - field_height, bottom + 3)
                x1, y1 = min(page.rect.width - 6, x0 + field_width), min(page.rect.height, y0 + field_height)
            if x1 <= x0 or y1 <= y0:
                continue
            candidates.append(
                make_field(
                    page=page_index,
                    rect=[x0, y0, x1, y1],
                    label=data_key.replace("_", " ").title(),
                    data_key=data_key,
                    field_type=field_type,
                    confidence=0.58 if ocr_used else 0.72,
                    source="local OCR" if ocr_used else "PDF text layer",
                )
            )
            break
    return candidates


def detect_fields(source_path: str | Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Propose boxes from AcroForms, acroforge geometry and local text/OCR.

    Every proposal is only a suggestion. Low-confidence boxes are always editable.
    """
    pdf_bytes = source_as_pdf_bytes(source_path)
    messages: list[str] = []
    candidates: list[dict[str, Any]] = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        try:
            import acroforge as af
        except ImportError:
            messages.append(
                "AcroForge is not installed; install the app requirements to enable AcroForm detection."
            )
        else:
            try:
                existing_specs = af.read_fields(pdf_bytes)
            except Exception as exc:
                messages.append(f"Existing AcroForm reading skipped: {exc}")
                existing_specs = []
            for spec in existing_specs:
                page_no = int(spec.page)
                if not 0 <= page_no < document.page_count:
                    continue
                rect = _pdf_rect_to_top_left(spec.rect, document[page_no].rect.height)
                field_type = "checkbox" if str(spec.type.value) == "checkbox" else "text"
                candidates.append(
                    make_field(
                        page=page_no,
                        rect=rect,
                        label=str(spec.name),
                        data_key=_guess_key(str(spec.name)),
                        field_type=field_type,
                        confidence=1.0,
                        source="Existing AcroForm",
                    )
                )
            try:
                manifest = af.detect(pdf_bytes)
            except Exception as exc:
                messages.append(f"AcroForge geometry detection skipped: {exc}")
            else:
                for spec in manifest.fields:
                    page_no = int(spec.page)
                    if not 0 <= page_no < document.page_count:
                        continue
                    rect = _pdf_rect_to_top_left(spec.rect, document[page_no].rect.height)
                    field_type = "checkbox" if str(spec.type.value) == "checkbox" else "text"
                    candidate = make_field(
                        page=page_no,
                        rect=rect,
                        label=str(spec.name),
                        data_key=_guess_key(str(spec.name)),
                        field_type=field_type,
                        confidence=float(spec.confidence),
                        source="AcroForge vector geometry",
                    )
                    if not any(
                        old["page"] == page_no and _overlaps(old["rect"], rect)
                        for old in candidates
                    ):
                        candidates.append(candidate)

        for page_no, page in enumerate(document):
            proposals = _label_proposals(page, page_no, ocr_used=False)
            for proposal in proposals:
                if not any(old["page"] == page_no and _overlaps(old["rect"], proposal["rect"]) for old in candidates):
                    candidates.append(proposal)
            if not page.get_text("words") and not shutil.which("tesseract"):
                messages.append(
                    f"Page {page_no + 1} appears image-only; Tesseract was not found. "
                    "Manual field placement is available."
                )
    if not candidates:
        messages.append("No fields were detected. Add boxes manually or install Tesseract for scanned forms.")
    else:
        messages.append("Review every proposed field and confidence score. Auto-detection is not final placement.")
    return candidates, messages
