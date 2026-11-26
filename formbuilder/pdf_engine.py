from __future__ import annotations

import io
import math
from datetime import date, datetime
from pathlib import Path
from typing import Any

import fitz
import pandas as pd

from formbuilder.template import source_as_pdf_bytes


ALIGNMENTS = {"left": 0, "center": 1, "right": 2}


def acroforge_rect(rect_top_left: list[float], page_height: float) -> tuple[float, float, float, float]:
    """Convert PyMuPDF top-left coordinates into PDF bottom-left coordinates."""
    x0, y0, x1, y1 = (float(v) for v in rect_top_left)
    return x0, page_height - y1, x1, page_height - y0


def is_checked(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().casefold() in {"1", "true", "yes", "y", "x", "checked", "on"}


def format_field_value(value: Any, field: dict[str, Any]) -> str:
    if value is None:
        return ""
    mode = field.get("format", "none")
    if mode == "date":
        parsed: date | datetime | None = None
        if isinstance(value, (date, datetime)):
            parsed = value
        elif isinstance(value, pd.Timestamp):
            parsed = value.to_pydatetime()
        else:
            candidate = str(value).strip()
            if not candidate:
                return ""
            try:
                iso_date = len(candidate) >= 10 and candidate[4:5] == "-" and candidate[7:8] == "-"
                parsed = pd.to_datetime(candidate, dayfirst=not iso_date, errors="raise").to_pydatetime()
            except Exception:
                return candidate
        return parsed.strftime(str(field.get("date_format", "%d-%m-%Y")))
    if mode == "currency":
        try:
            amount = float(str(value).replace(",", "").replace("Rs.", "").strip())
            decimals = max(0, min(4, int(field.get("currency_decimals", 2))))
            return f"{field.get('currency_prefix', 'Rs. ')}{amount:,.{decimals}f}"
        except (TypeError, ValueError):
            return str(value)
    text = str(value)
    if mode == "upper":
        return text.upper()
    if mode == "lower":
        return text.lower()
    return text


def _row_value(record: dict[str, Any], key: str) -> Any:
    return record.get(key, "") if key else ""


def _validate_field_mappings(fields: list[dict[str, Any]], record: dict[str, Any]) -> None:
    """Fail closed when a template refers to a column missing from the loaded sheet."""
    missing = sorted({
        str(field.get("data_key", "")).strip()
        for field in fields
        if str(field.get("data_key", "")).strip()
        and str(field.get("data_key", "")).strip() not in record
        and not (
            field.get("type") == "signature"
            and str(field.get("signature_path", "")).strip()
        )
    })
    if missing:
        columns = ", ".join(missing)
        raise ValueError(
            f"Template mapping error: these master-data columns are missing: {columns}. "
            "Load the correct workbook or update the field mappings before generating PDFs."
        )


def _fit_font_size(text: str, rect: fitz.Rect, requested: float, minimum: float) -> float:
    """Find the largest font size that can fit the field's available width."""
    available_width = max(1.0, rect.width - 4.0)
    size = min(float(requested), max(1.0, rect.height - 2.0))
    while size >= minimum:
        if fitz.get_text_length(text, fontname="helv", fontsize=size) <= available_width:
            return size
        size -= 0.5
    raise ValueError(
        f"Text will not fit in the field ({len(text)} characters, {rect.width:.1f} pt wide). "
        "Widen the box, enable wrapping, or shorten the value."
    )


def _overlay_field(page: fitz.Page, field: dict[str, Any], record: dict[str, Any]) -> None:
    raw_value = _row_value(record, str(field.get("data_key", "")))
    field_type = field.get("type", "text")
    rect = fitz.Rect(*[float(value) for value in field["rect"]])
    rect &= page.rect
    if rect.is_empty or rect.width < 3 or rect.height < 3:
        raise ValueError(f"Field '{field.get('label', field.get('id'))}' has an invalid rectangle.")

    if field_type == "checkbox":
        if is_checked(raw_value):
            inset = min(3.0, rect.width * 0.2, rect.height * 0.2)
            page.draw_line(
                fitz.Point(rect.x0 + inset, rect.y0 + inset),
                fitz.Point(rect.x1 - inset, rect.y1 - inset),
                color=(0, 0, 0), width=1.15, overlay=True,
            )
            page.draw_line(
                fitz.Point(rect.x0 + inset, rect.y1 - inset),
                fitz.Point(rect.x1 - inset, rect.y0 + inset),
                color=(0, 0, 0), width=1.15, overlay=True,
            )
        return

    if field_type == "signature":
        signature_path = str(raw_value or field.get("signature_path", "")).strip()
        if not signature_path:
            return
        image_path = Path(signature_path).expanduser()
        if not image_path.is_file():
            raise FileNotFoundError(f"Signature image does not exist: {image_path}")
        page.insert_image(rect, filename=str(image_path), keep_proportion=True, overlay=True)
        return

    text = format_field_value(raw_value, field)
    if not text:
        return
    requested = float(field.get("font_size", 10.0))
    minimum = float(field.get("min_font_size", 5.0))
    align = ALIGNMENTS.get(str(field.get("align", "left")), 0)
    if field.get("wrap", False):
        size = requested
        while size >= minimum:
            remaining = page.insert_textbox(
                rect, text, fontname="helv", fontsize=size, align=align,
                color=(0, 0, 0), overlay=True, lineheight=1.05,
            )
            if remaining >= 0:
                return
            size -= 0.5
        raise ValueError(
            f"Text does not fit the wrapped field '{field.get('label', field.get('id'))}'. "
            "Increase its height or shorten the value."
        )
    size = _fit_font_size(text, rect, requested, minimum)
    while size >= minimum:
        remaining = page.insert_textbox(
            rect, text, fontname="helv", fontsize=size, align=align,
            color=(0, 0, 0), overlay=True, lineheight=1.0,
        )
        if remaining >= 0:
            return
        size -= 0.5
    raise ValueError(
        f"Text does not fit in field '{field.get('label', field.get('id'))}'. "
        "Increase the box height or shorten the value."
    )


def _acroforge_validation_pass(
    pdf_bytes: bytes,
    fields: list[dict[str, Any]],
    record: dict[str, Any],
    page_sizes: list[tuple[float, float]],
) -> None:
    """Exercise AcroForge build/fill/flatten before creating the clean overlay PDF.

    AcroForge creates bordered AcroForm widgets. The final banking form must not
    acquire new widget borders, so the app renders transparent content overlays
    onto a fresh copy of the untouched source after this deterministic round-trip.
    """
    import acroforge as af
    from acroforge import FieldSpec, FieldType

    specs = []
    values: dict[str, Any] = {}
    for index, field in enumerate(fields):
        kind = field.get("type", "text")
        if kind not in {"text", "checkbox"}:
            continue
        page_index = int(field.get("page", 0))
        if not 0 <= page_index < len(page_sizes):
            raise ValueError(f"Field '{field.get('label', index)}' references a missing PDF page.")
        page_height = page_sizes[page_index][1]
        name = f"overlay_{index}_{field.get('id', index)}"
        field_type = FieldType.CHECKBOX if kind == "checkbox" else FieldType.TEXT
        specs.append(
            FieldSpec(
                type=field_type,
                page=page_index,
                rect=acroforge_rect(field["rect"], page_height),
                name=name,
                export_value="Yes" if kind == "checkbox" else None,
            )
        )
        raw = _row_value(record, str(field.get("data_key", "")))
        values[name] = (True if is_checked(raw) else "") if kind == "checkbox" else format_field_value(raw, field)
    if not specs:
        return
    fillable = af.build(pdf_bytes, specs)
    filled = af.fill(fillable, values)
    flattened = af.flatten(filled)
    # Validate that flattening returned a readable PDF and removed active widgets.
    with fitz.open(stream=flattened, filetype="pdf") as checked_pdf:
        if not checked_pdf.page_count:
            raise ValueError("AcroForge returned a PDF without pages.")
        if any(list(page.widgets() or []) for page in checked_pdf):
            raise ValueError("AcroForge flattening left interactive widgets in the PDF.")


def render_overlay_pdf_bytes(
    source_path: str | Path,
    fields: list[dict[str, Any]],
    record: dict[str, Any],
) -> bytes:
    """Render transparent, flattened content over a fresh in-memory source copy.

    This renderer deliberately does not create AcroForm widget borders. The
    source file on disk is only read; the returned PDF is a separate byte string.
    """
    _validate_field_mappings(fields, record)
    source_bytes = source_as_pdf_bytes(source_path)
    with fitz.open(stream=source_bytes, filetype="pdf") as source_document:
        original_page_count = source_document.page_count
        sizes = [(float(page.rect.width), float(page.rect.height)) for page in source_document]
        has_widgets = any(list(page.widgets() or []) for page in source_document)

        if has_widgets:
            # Rasterize the already-visible source pages at print resolution only
            # when the source contains interactive widgets. This preserves their
            # existing on-screen appearance while removing the interactive layer.
            output = fitz.open()
            for source_page in source_document:
                pixmap = source_page.get_pixmap(dpi=300, alpha=False, annots=True)
                new_page = output.new_page(
                    width=float(source_page.rect.width), height=float(source_page.rect.height)
                )
                new_page.insert_image(new_page.rect, stream=pixmap.tobytes("png"), overlay=False)
        else:
            output = fitz.open(stream=source_bytes, filetype="pdf")

    try:
        for field in fields:
            page_index = int(field.get("page", 0))
            if not 0 <= page_index < original_page_count:
                raise ValueError(
                    f"Field '{field.get('label', field.get('id'))}' references page {page_index + 1}."
                )
            _overlay_field(output[page_index], field, record)
        result = output.tobytes(garbage=4, deflate=True, deflate_images=True, deflate_fonts=True)
    finally:
        output.close()

    # Catch accidental page-size changes before a PDF reaches the print queue.
    with fitz.open(stream=result, filetype="pdf") as generated:
        if generated.page_count != original_page_count:
            raise RuntimeError("Output page count changed unexpectedly.")
        if any(list(page.widgets() or []) for page in generated):
            raise RuntimeError("Output still contains interactive PDF widgets; it is not fully flattened.")
        for index, page in enumerate(generated):
            old_w, old_h = sizes[index]
            if not (
                math.isclose(page.rect.width, old_w, abs_tol=0.05)
                and math.isclose(page.rect.height, old_h, abs_tol=0.05)
            ):
                raise RuntimeError(f"Output page {index + 1} no longer matches the original page size.")
    return result


def generate_filled_pdf_bytes(
    source_path: str | Path,
    fields: list[dict[str, Any]],
    record: dict[str, Any],
) -> bytes:
    """Run AcroForge's build/fill/flatten pass, then create the clean final overlay."""
    _validate_field_mappings(fields, record)
    source_bytes = source_as_pdf_bytes(source_path)
    with fitz.open(stream=source_bytes, filetype="pdf") as source_document:
        sizes = [(float(page.rect.width), float(page.rect.height)) for page in source_document]

    # AcroForge fields are injected, filled and flattened in memory. Its current
    # text-widget style includes visible borders, so the printable final uses the
    # same mapped values as transparent content overlays on a new source copy.
    _acroforge_validation_pass(source_bytes, fields, record, sizes)
    return render_overlay_pdf_bytes(source_path, fields, record)


def generate_filled_pdf(
    source_path: str | Path,
    fields: list[dict[str, Any]],
    record: dict[str, Any],
    output_path: str | Path,
) -> Path:
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = generate_filled_pdf_bytes(source_path, fields, record)
    target.write_bytes(payload)
    return target
