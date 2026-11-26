from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from typing import Any

import fitz

TEMPLATE_VERSION = 1


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_as_pdf_bytes(source_path: str | Path) -> bytes:
    """Return PDF bytes without writing to or modifying the source file."""
    path = Path(source_path)
    if not path.is_file():
        raise FileNotFoundError(f"Source form does not exist: {path}")
    payload = path.read_bytes()
    if path.suffix.lower() == ".pdf":
        # Validate the PDF before accepting it as a template source.
        with fitz.open(stream=payload, filetype="pdf") as document:
            if not document.page_count:
                raise ValueError("The source PDF contains no pages.")
        return payload
    image_types = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
    if path.suffix.lower() not in image_types:
        raise ValueError("Select a PDF or an image (PNG, JPG, TIFF, BMP, or WEBP).")
    with fitz.open(stream=payload, filetype=path.suffix.lower().lstrip(".")) as image:
        return image.convert_to_pdf()


def page_dimensions(pdf_bytes: bytes) -> list[list[float]]:
    with fitz.open(stream=pdf_bytes, filetype="pdf") as document:
        return [[round(page.rect.width, 3), round(page.rect.height, 3)] for page in document]


def make_field(
    *,
    page: int,
    rect: list[float] | tuple[float, float, float, float],
    label: str = "New field",
    data_key: str = "",
    field_type: str = "text",
    confidence: float = 1.0,
    source: str = "manual",
) -> dict[str, Any]:
    x0, y0, x1, y1 = (float(value) for value in rect)
    if x1 <= x0 or y1 <= y0:
        raise ValueError("Field rectangle must have positive width and height.")
    if field_type not in {"text", "checkbox", "signature"}:
        raise ValueError(f"Unsupported field type: {field_type}")
    return {
        "id": uuid.uuid4().hex[:10],
        "label": str(label),
        "data_key": str(data_key),
        "type": field_type,
        "page": int(page),
        "rect": [x0, y0, x1, y1],  # PyMuPDF coordinates: points, origin top-left.
        "font_size": 10.0,
        "min_font_size": 5.0,
        "align": "left",
        "format": "none",
        "date_format": "%d-%m-%Y",
        "currency_prefix": "Rs. ",
        "currency_decimals": 2,
        "wrap": False,
        "signature_path": "",
        "confidence": max(0.0, min(1.0, float(confidence))),
        "detection_source": source,
    }


def normalize_field(raw: dict[str, Any], index: int = 0) -> dict[str, Any]:
    field = make_field(
        page=raw.get("page", 0),
        rect=raw.get("rect", [40, 40, 220, 62]),
        label=raw.get("label", f"Field {index + 1}"),
        data_key=raw.get("data_key", ""),
        field_type=raw.get("type", "text"),
        confidence=raw.get("confidence", 1.0),
        source=raw.get("detection_source", "manual"),
    )
    field["id"] = str(raw.get("id") or field["id"])
    for key in (
        "font_size", "min_font_size", "align", "format", "date_format",
        "currency_prefix", "currency_decimals", "wrap", "signature_path",
    ):
        if key in raw:
            field[key] = raw[key]
    if field["align"] not in {"left", "center", "right"}:
        field["align"] = "left"
    if field["format"] not in {"none", "date", "currency", "upper", "lower"}:
        field["format"] = "none"
    field["font_size"] = max(4.0, min(48.0, float(field["font_size"])))
    field["min_font_size"] = max(3.0, min(field["font_size"], float(field["min_font_size"])))
    field["wrap"] = bool(field["wrap"])
    field["currency_decimals"] = max(0, min(4, int(field["currency_decimals"])))
    return field


def build_template(source_path: str | Path, fields: list[dict[str, Any]]) -> dict[str, Any]:
    path = Path(source_path).expanduser().resolve()
    pdf_bytes = source_as_pdf_bytes(path)
    return {
        "template_version": TEMPLATE_VERSION,
        "name": path.stem,
        "source_path": str(path),
        "source_sha256": sha256_bytes(path.read_bytes()),
        "page_sizes_pt": page_dimensions(pdf_bytes),
        "coordinate_system": "PyMuPDF points; origin at top-left of unrotated page",
        "fields": [normalize_field(field, i) for i, field in enumerate(fields)],
    }


def save_template(path: str | Path, source_path: str | Path, fields: list[dict[str, Any]]) -> None:
    template = build_template(source_path, fields)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(template, indent=2, ensure_ascii=False), encoding="utf-8")


def load_template(path: str | Path) -> dict[str, Any]:
    template_path = Path(path)
    template = json.loads(template_path.read_text(encoding="utf-8"))
    if template.get("template_version") != TEMPLATE_VERSION:
        raise ValueError("This template version is not supported by this app version.")
    source = Path(template.get("source_path", ""))
    if not source.is_absolute():
        source = template_path.resolve().parent / source
    if not source.is_file():
        raise FileNotFoundError(
            "The PDF/image saved with this template could not be found. "
            "Re-link the original source form in the app."
        )
    actual_hash = sha256_bytes(source.read_bytes())
    if template.get("source_sha256") != actual_hash:
        raise ValueError(
            "The selected source form does not match the SHA-256 fingerprint saved in this template."
        )
    template["source_path"] = str(source.resolve())
    template["fields"] = [normalize_field(f, i) for i, f in enumerate(template.get("fields", []))]
    return template
