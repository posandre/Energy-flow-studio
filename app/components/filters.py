from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import pandas as pd


@dataclass(slots=True)
class DateFilter:
    preset: str
    start: pd.Timestamp | None = None
    end: pd.Timestamp | None = None


def apply_date_filter(
    dataframe: pd.DataFrame,
    timestamp_column: str,
    date_filter: DateFilter,
) -> pd.DataFrame:
    if dataframe.empty:
        return dataframe

    if date_filter.preset == "all":
        return dataframe

    max_timestamp = dataframe[timestamp_column].max()
    if pd.isna(max_timestamp):
        return dataframe

    if date_filter.preset == "today":
        start = max_timestamp.normalize()
        end = start + timedelta(days=1)
    elif date_filter.preset == "24h":
        end = max_timestamp
        start = end - timedelta(hours=24)
    elif date_filter.preset == "custom":
        start = date_filter.start
        end = date_filter.end
        if start is None or end is None:
            return dataframe
        end = end + timedelta(days=1)
    else:
        return dataframe

    mask = (dataframe[timestamp_column] >= start) & (dataframe[timestamp_column] <= end)
    return dataframe.loc[mask].copy()
