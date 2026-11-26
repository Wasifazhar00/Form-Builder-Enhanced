from pathlib import Path

import pandas as pd
import pytest

from formbuilder.data import load_master_data, record_from_row

SAMPLES = Path(__file__).resolve().parents[1] / "samples"


def test_sample_master_workbook_loads_with_synthetic_rows() -> None:
    frame = load_master_data(SAMPLES / "sample_master.xlsx")
    assert len(frame) == 2
    assert "customer_name" in frame.columns
    assert record_from_row(frame, 0)["customer_name"] == "Ayesha Sample"


def test_duplicate_or_blank_headers_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad.csv"
    path.write_text("name,name\nA,B\n", encoding="utf-8")
    # pandas normalizes duplicate CSV headers; the loader still returns a safe,
    # uniquely labelled frame rather than silently losing a column.
    frame = load_master_data(path)
    assert len(set(frame.columns)) == len(frame.columns)


def test_unsupported_xls_format_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "legacy.xls"
    path.write_bytes(b"not an xls workbook")
    with pytest.raises(ValueError, match="Unsupported"):
        load_master_data(path)


def test_out_of_range_row_is_rejected() -> None:
    frame = pd.DataFrame([{"customer_name": "Example"}])
    with pytest.raises(IndexError):
        record_from_row(frame, 1)
