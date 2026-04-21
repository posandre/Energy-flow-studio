from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import pandas as pd


TIMESTAMP_HINTS = (
    "timestamp",
    "time",
    "date",
    "datetime",
    "logged at",
)

GROUP_KEYWORDS = {
    "Voltage": ("volt", "voltage"),
    "Power": ("power", "pv", "grid", "load", "watt", "kw"),
    "Current": ("current", " amp", "amps", "(a)", "[a]", "_a"),
    "SOC": ("soc", "state of charge", "battery level"),
    "Capacity": ("capacity",),
}


@dataclass(slots=True)
class ProcessedData:
    dataframe: pd.DataFrame
    timestamp_column: str
    numeric_columns: list[str]
    grouped_columns: dict[str, list[str]]


class DataProcessorError(Exception):
    """Raised when the input data cannot be normalized for charting."""


def detect_timestamp_column(columns: Iterable[str]) -> str | None:
    for column in columns:
        column_lower = column.strip().lower()
        compact = column_lower.replace("_", " ").replace("-", " ")
        if any(hint in compact for hint in TIMESTAMP_HINTS):
            return column
    return None


def infer_group_name(column_name: str) -> str:
    normalized = column_name.lower()
    for group, keywords in GROUP_KEYWORDS.items():
        if any(keyword in normalized for keyword in keywords):
            return group
    return "Other"


def process_dataframe(dataframe: pd.DataFrame) -> ProcessedData:
    if dataframe.empty:
        raise DataProcessorError("Input data is empty.")

    normalized = dataframe.copy()
    normalized.columns = [str(column).strip() for column in normalized.columns]
    normalized.dropna(how="all", inplace=True)

    timestamp_column = detect_timestamp_column(normalized.columns)
    if not timestamp_column:
        raise DataProcessorError("No timestamp column found. Expected a column like Timestamp or DateTime.")

    timestamp_series = normalized[timestamp_column].astype(str).str.strip()
    normalized[timestamp_column] = pd.to_datetime(
        timestamp_series,
        errors="coerce",
    )
    normalized.dropna(subset=[timestamp_column], inplace=True)
    if normalized.empty:
        raise DataProcessorError("Timestamp column could not be parsed into valid dates.")

    numeric_columns: list[str] = []
    for column in normalized.columns:
        if column == timestamp_column:
            continue
        cleaned_series = (
            normalized[column]
            .astype(str)
            .str.strip()
            .replace({"": None, "nan": None, "None": None})
        )
        converted = pd.to_numeric(cleaned_series, errors="coerce")
        if converted.notna().sum() == 0:
            continue
        normalized[column] = converted
        numeric_columns.append(column)

    if not numeric_columns:
        raise DataProcessorError("No numeric columns found in the Excel file.")

    normalized = normalized[[timestamp_column, *numeric_columns]]
    normalized.dropna(how="all", subset=numeric_columns, inplace=True)
    normalized.sort_values(by=timestamp_column, inplace=True)
    normalized.reset_index(drop=True, inplace=True)

    grouped_columns: dict[str, list[str]] = {}
    for column in numeric_columns:
        grouped_columns.setdefault(infer_group_name(column), []).append(column)

    return ProcessedData(
        dataframe=normalized,
        timestamp_column=timestamp_column,
        numeric_columns=numeric_columns,
        grouped_columns=grouped_columns,
    )
