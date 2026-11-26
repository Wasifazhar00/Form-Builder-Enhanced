from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd


SUPPORTED_DATA = {".csv", ".xlsx", ".xlsm"}


def load_master_data(path: str | Path) -> pd.DataFrame:
    """Read a local CSV/Excel sheet. Values remain in memory; nothing is uploaded."""
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"Master-data file does not exist: {source}")
    suffix = source.suffix.lower()
    if suffix not in SUPPORTED_DATA:
        raise ValueError(f"Unsupported master-data format: {suffix}")
    if suffix == ".csv":
        frame = pd.read_csv(source, dtype=object, keep_default_na=False)
    else:
        frame = pd.read_excel(source, dtype=object, keep_default_na=False)
    if frame.empty:
        raise ValueError("The selected master-data sheet contains no customer rows.")
    if len(frame.columns) == 0:
        raise ValueError("The selected master-data sheet has no columns.")
    frame.columns = [str(column).strip() for column in frame.columns]
    if any(not column for column in frame.columns):
        raise ValueError("Every master-data column must have a non-empty heading.")
    if len(set(frame.columns)) != len(frame.columns):
        raise ValueError("Master-data column headings must be unique.")
    frame = frame.astype(object)
    return frame.where(pd.notna(frame), "")


def record_from_row(frame: pd.DataFrame, row_index: int) -> dict[str, Any]:
    if row_index < 0 or row_index >= len(frame):
        raise IndexError(f"Customer row {row_index} is outside the loaded sheet.")
    return frame.iloc[row_index].to_dict()
