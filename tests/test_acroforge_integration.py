from __future__ import annotations

import importlib.util
from pathlib import Path

import fitz
import pytest

ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PDF = ROOT / "samples" / "sample_application_form.pdf"


@pytest.mark.skipif(importlib.util.find_spec("acroforge") is None, reason="AcroForge is installed on the Windows CI runner")
def test_acroforge_inject_fill_and_flatten_round_trip() -> None:
    import acroforge as af
    from acroforge import FieldSpec, FieldType
    from formbuilder.pdf_engine import generate_filled_pdf_bytes

    original = SAMPLE_PDF.read_bytes()
    specs = [
        FieldSpec(
            type=FieldType.TEXT,
            page=0,
            rect=(158.0, 684.0, 430.0, 707.0),
            name="ci_customer_name",
        ),
        FieldSpec(
            type=FieldType.CHECKBOX,
            page=0,
            rect=(160.0, 397.0, 172.0, 409.0),
            name="ci_savings",
            export_value="Yes",
        ),
    ]
    fillable = af.build(original, specs)
    filled = af.fill(fillable, {"ci_customer_name": "CI Sample", "ci_savings": True})
    flattened = af.flatten(filled)
    with fitz.open(stream=flattened, filetype="pdf") as pdf:
        assert pdf.page_count == 2
        assert all(not list(page.widgets() or []) for page in pdf)

    # Exercise the app's exact AcroForge validation pass and PyMuPDF final overlay.
    final_output = generate_filled_pdf_bytes(
        SAMPLE_PDF,
        [
            {
                "id": "ci-name", "label": "Customer name", "data_key": "customer_name",
                "type": "text", "page": 0, "rect": [158, 135, 430, 158],
                "font_size": 10, "min_font_size": 5,
            },
            {
                "id": "ci-check", "label": "Savings account", "data_key": "savings_account",
                "type": "checkbox", "page": 0, "rect": [160, 433, 172, 445],
            },
        ],
        {"customer_name": "CI Sample", "savings_account": "yes"},
    )
    with fitz.open(stream=final_output, filetype="pdf") as pdf:
        assert pdf.page_count == 2
        assert "CI Sample" in pdf[0].get_text()
        assert all(not list(page.widgets() or []) for page in pdf)
    assert SAMPLE_PDF.read_bytes() == original
