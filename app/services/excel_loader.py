from __future__ import annotations

from pathlib import Path

import pandas as pd


class ExcelLoaderError(Exception):
    """Raised when an Excel file cannot be loaded."""


def load_excel(file_path: str | Path) -> pd.DataFrame:
    path = Path(file_path)
    if not path.exists():
        raise ExcelLoaderError(f"File not found: {path}")
    if path.suffix.lower() != ".xlsx":
        raise ExcelLoaderError("Only .xlsx files are supported.")

    try:
        dataframe = pd.read_excel(
            path,
            sheet_name=0,
            header=0,
            engine="openpyxl",
        )
    except Exception as exc:  # pragma: no cover - surface original load failures
        raise ExcelLoaderError(f"Failed to read Excel file: {exc}") from exc

    if dataframe.empty:
        raise ExcelLoaderError("Excel file is empty.")

    dataframe = dataframe.dropna(axis=1, how="all")
    dataframe.columns = [str(column).strip() for column in dataframe.columns]

    return dataframe
