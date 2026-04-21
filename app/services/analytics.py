from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(slots=True)
class SeriesStats:
    name: str
    minimum: float
    maximum: float
    average: float


def calculate_stats(dataframe: pd.DataFrame, columns: list[str]) -> list[SeriesStats]:
    stats: list[SeriesStats] = []
    for column in columns:
        series = dataframe[column].dropna()
        if series.empty:
            continue
        stats.append(
            SeriesStats(
                name=column,
                minimum=float(series.min()),
                maximum=float(series.max()),
                average=float(series.mean()),
            )
        )
    return stats
