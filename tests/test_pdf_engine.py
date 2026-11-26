from __future__ import annotations

from pathlib import Path

import fitz
import pytest

from formbuilder.pdf_engine import (
    acroforge_rect,
    format_field_value,
    is_checked,
    render_overlay_pdf_bytes,
)
from formbuilder.template import sha256_bytes, source_as_pdf_bytes

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PDF = ROOT / "samples" / "sample_application_form.pdf"


def test_acroforge_rect_flips_top_left_to_bottom_left_coordinates() -> None:
    assert acroforge_rect([10, 20, 110, 40], 800) == (10.0, 760.0, 110.0, 780.0)


@pytest.mark.parametrize("value", [True, 1, "yes", "Y", "checked", "x"])
def test_truthy_checkbox_values(value: object) -> None:
    assert is_checked(value)


@pytest.mark.parametrize("value", [None, False, 0, "", "no", "off"])
def test_false_checkbox_values(value: object) -> None:
    assert not is_checked(value)


def test_date_formatting_is_day_first_and_configurable() -> None:
    actual = format_field_value("26-11-2026", {"format": "date", "date_format": "%Y/%m/%d"})
    assert actual == "2026/11/26"


def test_currency_formatting_uses_grouping_and_prefix() -> None:
    actual = format_field_value("25000", {"format": "currency", "currency_prefix": "Rs. "})
    assert actual == "Rs. 25,000.00"


def test_text_case_formatting() -> None:
    assert format_field_value("Abc", {"format": "upper"}) == "ABC"
    assert format_field_value("Abc", {"format": "lower"}) == "abc"


def test_source_pdf_conversion_does_not_mutate_pdf_source() -> None:
    before = sha256_bytes(SAMPLE_PDF.read_bytes())
    pdf_bytes = source_as_pdf_bytes(SAMPLE_PDF)
    assert pdf_bytes.startswith(b"%PDF")
    assert sha256_bytes(SAMPLE_PDF.read_bytes()) == before


def test_overlay_generation_preserves_original_page_count_and_size() -> None:
    fields = [
        {
            "id": "name",
            "label": "Customer name",
            "data_key": "customer_name",
            "type": "text",
            "page": 0,
            "rect": [158, 135, 530, 158],
            "font_size": 10,
            "min_font_size": 5,
            "align": "left",
            "format": "none",
        },
        {
            "id": "savings",
            "label": "Savings account",
            "data_key": "savings_account",
            "type": "checkbox",
            "page": 0,
            "rect": [160, 445, 172, 457],
        },
        {
            "id": "address",
            "label": "Residential address",
            "data_key": "residential_address",
            "type": "text",
            "page": 1,
            "rect": [158, 135, 530, 158],
            "font_size": 9,
            "min_font_size": 5,
            "wrap": True,
        },
    ]
    record = {
        "customer_name": "Ayesha Sample",
        "savings_account": "yes",
        "residential_address": "12 Example Street, Sample Town",
    }
    original_hash = sha256_bytes(SAMPLE_PDF.read_bytes())
    output_bytes = render_overlay_pdf_bytes(SAMPLE_PDF, fields, record)
    with fitz.open(stream=source_as_pdf_bytes(SAMPLE_PDF), filetype="pdf") as original:
        with fitz.open(stream=output_bytes, filetype="pdf") as output:
            assert output.page_count == original.page_count == 2
            for source_page, output_page in zip(original, output):
                assert output_page.rect.width == pytest.approx(source_page.rect.width, abs=0.05)
                assert output_page.rect.height == pytest.approx(source_page.rect.height, abs=0.05)
                assert not list(output_page.widgets() or [])
            assert "Ayesha Sample" in output[0].get_text()
            assert "12 Example Street" in output[1].get_text()
    assert sha256_bytes(SAMPLE_PDF.read_bytes()) == original_hash


def test_overlay_rejects_missing_master_data_mapping() -> None:
    with pytest.raises(ValueError, match="master-data columns are missing"):
        render_overlay_pdf_bytes(
            SAMPLE_PDF,
            [{
                "id": "missing-column",
                "label": "Customer name",
                "data_key": "customer_name_typo",
                "type": "text",
                "page": 0,
                "rect": [100, 100, 240, 124],
            }],
            {"customer_name": "Ayesha"},
        )


def test_overlay_rejects_field_outside_document_pages() -> None:
    with pytest.raises(ValueError, match="references page"):
        render_overlay_pdf_bytes(
            SAMPLE_PDF,
            [{"id": "bad", "label": "Bad field", "type": "text", "page": 9, "rect": [1, 1, 10, 10]}],
            {},
        )


def test_overlay_rejects_field_that_is_too_small() -> None:
    with pytest.raises(ValueError, match="invalid rectangle"):
        render_overlay_pdf_bytes(
            SAMPLE_PDF,
            [{"id": "bad", "label": "Bad field", "type": "text", "page": 0, "rect": [1, 1, 2, 2]}],
            {},
        )


def test_existing_interactive_source_widgets_are_flattened_in_output(tmp_path: Path) -> None:
    source = tmp_path / "fillable_source.pdf"
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((50, 60), "Original page artwork")
    widget = fitz.Widget()
    widget.field_type = fitz.PDF_WIDGET_TYPE_TEXT
    widget.field_name = "existing_customer_name"
    widget.field_value = "Existing visible value"
    widget.rect = fitz.Rect(100, 100, 300, 126)
    widget.text_fontsize = 12
    widget.border_width = 1
    widget.border_color = (0, 0, 0)
    widget.fill_color = (1, 1, 1)
    page.add_widget(widget)
    document.save(source)
    document.close()

    original_hash = sha256_bytes(source.read_bytes())
    output = render_overlay_pdf_bytes(source, [], {})
    with fitz.open(stream=output, filetype="pdf") as generated:
        assert generated.page_count == 1
        assert list(generated[0].widgets() or []) == []
        # The existing widget appearance is retained by a 300-DPI page image.
        assert generated[0].get_images(full=True)
    assert sha256_bytes(source.read_bytes()) == original_hash


def test_signature_image_is_embedded_as_page_content(tmp_path: Path) -> None:
    import struct
    import zlib

    def png_chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)

    signature = tmp_path / "signature.png"
    width = height = 8
    scanline = b"\x00" + bytes([0, 0, 0, 255]) * width
    raw = scanline * height
    png = b"\x89PNG\r\n\x1a\n"
    png += png_chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    png += png_chunk(b"IDAT", zlib.compress(raw))
    png += png_chunk(b"IEND", b"")
    signature.write_bytes(png)
    field = {
        "id": "signature",
        "label": "Signature",
        "data_key": "signature_path",
        "type": "signature",
        "page": 0,
        "rect": [100, 100, 200, 140],
    }
    output = render_overlay_pdf_bytes(SAMPLE_PDF, [field], {"signature_path": str(signature)})
    with fitz.open(stream=output, filetype="pdf") as generated:
        assert generated[0].get_images(full=True)
        assert not list(generated[0].widgets() or [])
