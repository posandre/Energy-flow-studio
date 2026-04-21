from __future__ import annotations

from dataclasses import dataclass
import math

import pandas as pd


class PvForecastError(Exception):
    """Raised when the forecast cannot be built from the available inputs."""


@dataclass(slots=True)
class PvForecastResult:
    source_pv_column: str
    forecast_frame: pd.DataFrame
    compare_frame: pd.DataFrame
    today_energy_kwh: float
    tomorrow_energy_kwh: float
    actual_today_energy_kwh: float | None
    forecast_today_to_now_kwh: float | None
    delta_today_kwh: float | None
    peak_power_kw: float
    peak_power_at: str
    confidence_score: float
    confidence_label: str
    training_days: int
    matched_history_rows: int
    model_name: str
    driver_snapshot: dict[str, float | None]
    notes: list[str]


def _normalize_timestamped_frame(dataframe: pd.DataFrame) -> pd.DataFrame:
    if dataframe is None or dataframe.empty:
        return pd.DataFrame()
    frame = dataframe.copy()
    frame.columns = [str(column).strip() for column in frame.columns]
    timestamp_column = next(
        (column for column in frame.columns if str(column).strip().lower() == "timestamp"),
        None,
    )
    if timestamp_column is None:
        raise PvForecastError("The dataset does not contain a Timestamp column.")
    frame[timestamp_column] = pd.to_datetime(frame[timestamp_column], errors="coerce")
    # Normalize timezone-aware sources (UTC/+offset) to the app-local wall-clock.
    tzinfo = getattr(frame[timestamp_column].dt, "tz", None)
    if tzinfo is not None:
        frame[timestamp_column] = frame[timestamp_column].dt.tz_convert("Europe/Kiev").dt.tz_localize(None)
    frame = frame.dropna(subset=[timestamp_column]).sort_values(timestamp_column).reset_index(drop=True)
    if timestamp_column != "Timestamp":
        frame = frame.rename(columns={timestamp_column: "Timestamp"})
    return frame


def _select_pv_power_column(dataframe: pd.DataFrame) -> str:
    candidates: list[tuple[int, str]] = []
    for column in dataframe.columns:
        if column == "Timestamp":
            continue
        normalized = column.lower()
        score = 0
        if "pv" in normalized or "solar" in normalized:
            score += 6
        if "power" in normalized:
            score += 3
        if "output" in normalized:
            score += 2
        if "active" in normalized:
            score += 1
        if any(bad in normalized for bad in ("grid", "load", "battery", "home")):
            score -= 6
        if score > 0:
            candidates.append((score, column))
    if not candidates:
        raise PvForecastError("Could not identify a PV power column in the DessMonitor dataset.")
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return candidates[0][1]


def _confidence_label(score: float) -> str:
    if score >= 0.8:
        return "High confidence"
    if score >= 0.55:
        return "Medium confidence"
    return "Low confidence"


def _clip_numeric(series: pd.Series, *, lower: float | None = None) -> pd.Series:
    numeric = pd.to_numeric(series, errors="coerce")
    if lower is not None:
        numeric = numeric.clip(lower=lower)
    return numeric


def _temperature_factor(temp: float | None) -> float:
    if temp is None or pd.isna(temp):
        return 1.0
    return max(1.0 - max(float(temp) - 25.0, 0.0) * 0.004, 0.7)


def _wind_factor(wind: float | None) -> float:
    if wind is None or pd.isna(wind):
        return 1.0
    return min(1.0 + min(float(wind), 14.0) * 0.005, 1.06)


def _rain_factor(precipitation: float | None) -> float:
    if precipitation is None or pd.isna(precipitation):
        return 1.0
    return 0.92 if float(precipitation) > 0.2 else 1.0


def _predict_row(
    *,
    radiation: float,
    hour: int,
    month: int,
    temp: float | None,
    wind: float | None,
    precipitation: float | None,
    weather_ratio_base: float,
    hour_profile: dict[int, float],
    month_profile: dict[int, float],
    hour_correction: dict[int, float] | None = None,
    month_correction: dict[int, float] | None = None,
    peak_power_kw: float,
) -> float:
    predicted = radiation * weather_ratio_base * max(float(hour_profile.get(hour, 0.22)), 0.08) * max(
        float(month_profile.get(month, 1.0)),
        0.55,
    )
    predicted *= _temperature_factor(temp)
    predicted *= _wind_factor(wind)
    predicted *= _rain_factor(precipitation)
    if hour_correction:
        predicted *= float(hour_correction.get(hour, 1.0))
    if month_correction:
        predicted *= float(month_correction.get(month, 1.0))
    if radiation < 5.0:
        return 0.0
    return min(max(predicted, 0.0), peak_power_kw * 1.05)


def _derive_adaptive_corrections(
    valid_history: pd.DataFrame,
    *,
    weather_ratio_base: float,
    hour_profile: dict[int, float],
    month_profile: dict[int, float],
    peak_power_kw: float,
) -> tuple[dict[int, float], dict[int, float]]:
    if valid_history.empty:
        return {}, {}

    working = valid_history.copy()
    working["baseline_predicted_kw"] = working.apply(
        lambda row: _predict_row(
            radiation=float(row["shortwave_radiation"]),
            hour=int(row["Timestamp"].hour),
            month=int(row["Timestamp"].month),
            temp=row.get("temperature_2m"),
            wind=row.get("wind_speed_10m"),
            precipitation=row.get("precipitation"),
            weather_ratio_base=weather_ratio_base,
            hour_profile=hour_profile,
            month_profile=month_profile,
            peak_power_kw=peak_power_kw,
        ),
        axis=1,
    )
    working = working[working["baseline_predicted_kw"] > 0.01].copy()
    if working.empty:
        return {}, {}

    working["adaptive_ratio"] = (working["pv_power_kw"] / working["baseline_predicted_kw"]).replace(
        [math.inf, -math.inf], pd.NA
    )
    working = working.dropna(subset=["adaptive_ratio"])
    if working.empty:
        return {}, {}

    hour_correction = (
        working.groupby(working["Timestamp"].dt.hour)["adaptive_ratio"]
        .median()
        .clip(lower=0.55, upper=1.75)
        .to_dict()
    )
    month_correction = (
        working.groupby(working["Timestamp"].dt.month)["adaptive_ratio"]
        .median()
        .clip(lower=0.75, upper=1.35)
        .to_dict()
    )
    return (
        {int(key): float(value) for key, value in hour_correction.items() if pd.notna(value)},
        {int(key): float(value) for key, value in month_correction.items() if pd.notna(value)},
    )


def _build_compare_frame(
    today_forecast_frame: pd.DataFrame,
    actual_today_frame: pd.DataFrame,
) -> pd.DataFrame:
    if today_forecast_frame.empty:
        return pd.DataFrame(columns=["Timestamp", "predicted_pv_power_kw", "actual_pv_power_kw"])

    forecast_series = (
        today_forecast_frame[["Timestamp", "predicted_pv_power_kw"]]
        .dropna(subset=["Timestamp"])
        .drop_duplicates(subset=["Timestamp"])
        .sort_values("Timestamp")
        .set_index("Timestamp")["predicted_pv_power_kw"]
    )
    if actual_today_frame.empty:
        return forecast_series.reset_index().assign(actual_pv_power_kw=pd.NA)

    actual_series = (
        actual_today_frame[["Timestamp", "pv_power_kw"]]
        .dropna(subset=["Timestamp"])
        .drop_duplicates(subset=["Timestamp"])
        .sort_values("Timestamp")
        .set_index("Timestamp")["pv_power_kw"]
    )
    union_index = forecast_series.index.union(actual_series.index).sort_values()
    predicted = (
        forecast_series.reindex(union_index)
        .interpolate(method="time", limit_direction="both")
        .reindex(union_index)
    )
    actual = actual_series.reindex(union_index)
    compare_frame = pd.DataFrame(
        {
            "Timestamp": union_index,
            "predicted_pv_power_kw": predicted.to_numpy(),
            "actual_pv_power_kw": actual.to_numpy(),
        }
    )
    return compare_frame.reset_index(drop=True)


def _select_next_hour_forecast_row(forecast_frame: pd.DataFrame) -> pd.Series:
    if forecast_frame.empty:
        return pd.Series(dtype=object)
    now_local = pd.Timestamp.now(tz="Europe/Kiev").tz_localize(None)
    next_hour = now_local.ceil("h")
    candidate = forecast_frame[forecast_frame["Timestamp"] >= next_hour]
    if not candidate.empty:
        return candidate.iloc[0]
    return forecast_frame.iloc[-1]


def build_pv_forecast(
    dessmonitor_frame: pd.DataFrame,
    weather_forecast_frame: pd.DataFrame,
    *,
    weather_history_frame: pd.DataFrame | None = None,
) -> PvForecastResult:
    history = _normalize_timestamped_frame(dessmonitor_frame)
    forecast = _normalize_timestamped_frame(weather_forecast_frame)
    weather_history = _normalize_timestamped_frame(weather_history_frame) if weather_history_frame is not None else pd.DataFrame()

    if history.empty:
        raise PvForecastError("DessMonitor history is empty.")
    if forecast.empty:
        raise PvForecastError("Weather forecast data is empty.")

    pv_column = _select_pv_power_column(history)
    history["pv_power_kw"] = _clip_numeric(history[pv_column], lower=0.0)
    history = history.dropna(subset=["pv_power_kw"]).copy()
    if history.empty:
        raise PvForecastError("The PV power history does not contain usable numeric values.")

    history["hour"] = history["Timestamp"].dt.hour
    history["month"] = history["Timestamp"].dt.month

    peak_power_kw = float(history["pv_power_kw"].quantile(0.98) or history["pv_power_kw"].max() or 0.0)
    if peak_power_kw <= 0:
        raise PvForecastError("The PV history does not contain positive generation values yet.")

    daylight_history = history[history["pv_power_kw"] > 0].copy()
    if daylight_history.empty:
        raise PvForecastError("There is not enough daylight PV history to build a forecast.")

    training_days = max(1, int(history["Timestamp"].dt.normalize().nunique()))
    hour_profile = (
        daylight_history.groupby("hour")["pv_power_kw"].median() / peak_power_kw
    ).to_dict()
    month_profile = (
        daylight_history.groupby("month")["pv_power_kw"].median() / max(daylight_history["pv_power_kw"].median(), 1e-6)
    ).to_dict()

    weather_ratio_base = peak_power_kw / 850.0
    matched_history_rows = 0
    model_name = "Baseline profile"
    notes = [
        f"PV column: {pv_column}",
        f"Training window: {training_days} day(s)",
    ]
    hour_correction: dict[int, float] = {}
    month_correction: dict[int, float] = {}

    if not weather_history.empty:
        merged = history.merge(weather_history, on="Timestamp", how="inner")
        if "shortwave_radiation" in merged.columns:
            merged["shortwave_radiation"] = _clip_numeric(merged["shortwave_radiation"], lower=0.0)
            merged["temperature_2m"] = _clip_numeric(merged.get("temperature_2m", pd.Series(dtype=float)))
            merged["wind_speed_10m"] = _clip_numeric(merged.get("wind_speed_10m", pd.Series(dtype=float)), lower=0.0)
            merged["precipitation"] = _clip_numeric(merged.get("precipitation", pd.Series(dtype=float)), lower=0.0)
            valid = merged[(merged["shortwave_radiation"] > 20.0) & (merged["pv_power_kw"] >= 0.0)].copy()
            matched_history_rows = int(len(valid.index))
            if matched_history_rows >= 24:
                ratio = (valid["pv_power_kw"] / valid["shortwave_radiation"]).replace([math.inf, -math.inf], pd.NA).dropna()
                if not ratio.empty:
                    weather_ratio_base = float(ratio.median())
                    hour_ratio = (valid.groupby(valid["Timestamp"].dt.hour).apply(
                        lambda frame: (frame["pv_power_kw"] / frame["shortwave_radiation"]).median()
                    ) / max(weather_ratio_base, 1e-6)).to_dict()
                    month_ratio = (valid.groupby(valid["Timestamp"].dt.month).apply(
                        lambda frame: (frame["pv_power_kw"] / frame["shortwave_radiation"]).median()
                    ) / max(weather_ratio_base, 1e-6)).to_dict()
                    for key, value in hour_ratio.items():
                        if pd.notna(value):
                            hour_profile[int(key)] = float(value)
                    for key, value in month_ratio.items():
                        if pd.notna(value):
                            month_profile[int(key)] = float(value)
                    hour_correction, month_correction = _derive_adaptive_corrections(
                        valid,
                        weather_ratio_base=weather_ratio_base,
                        hour_profile=hour_profile,
                        month_profile=month_profile,
                        peak_power_kw=peak_power_kw,
                    )
                    model_name = "Adaptive self-correcting hybrid"
                    notes.append(f"Weather-matched rows: {matched_history_rows}")
                    if hour_correction:
                        notes.append(f"Hourly correction bins: {len(hour_correction)}")
                    if month_correction:
                        notes.append(f"Monthly correction bins: {len(month_correction)}")

    forecast["shortwave_radiation"] = _clip_numeric(forecast.get("shortwave_radiation", pd.Series(dtype=float)), lower=0.0).fillna(0.0)
    forecast["cloud_cover"] = _clip_numeric(forecast.get("cloud_cover", pd.Series(dtype=float)), lower=0.0)
    forecast["temperature_2m"] = _clip_numeric(forecast.get("temperature_2m", pd.Series(dtype=float)))
    forecast["wind_speed_10m"] = _clip_numeric(forecast.get("wind_speed_10m", pd.Series(dtype=float)), lower=0.0)
    forecast["precipitation"] = _clip_numeric(forecast.get("precipitation", pd.Series(dtype=float)), lower=0.0)
    forecast["hour"] = forecast["Timestamp"].dt.hour
    forecast["month"] = forecast["Timestamp"].dt.month

    predictions: list[float] = []
    for row in forecast.itertuples(index=False):
        radiation = max(float(getattr(row, "shortwave_radiation", 0.0) or 0.0), 0.0)
        predictions.append(
            _predict_row(
                radiation=radiation,
                hour=int(row.hour),
                month=int(row.month),
                temp=getattr(row, "temperature_2m", None),
                wind=getattr(row, "wind_speed_10m", None),
                precipitation=getattr(row, "precipitation", None),
                weather_ratio_base=weather_ratio_base,
                hour_profile=hour_profile,
                month_profile=month_profile,
                hour_correction=hour_correction,
                month_correction=month_correction,
                peak_power_kw=peak_power_kw,
            )
        )

    forecast["predicted_pv_power_kw"] = predictions
    forecast = forecast[forecast["Timestamp"] >= forecast["Timestamp"].min().normalize()].copy()

    today = pd.Timestamp.now().normalize()
    tomorrow = today + pd.Timedelta(days=1)
    today_frame = forecast[forecast["Timestamp"].dt.normalize() == today].copy()
    tomorrow_frame = forecast[forecast["Timestamp"].dt.normalize() == tomorrow].copy()

    today_energy_kwh = float(today_frame["predicted_pv_power_kw"].sum()) if not today_frame.empty else 0.0
    tomorrow_energy_kwh = float(tomorrow_frame["predicted_pv_power_kw"].sum()) if not tomorrow_frame.empty else 0.0

    history_today = history[history["Timestamp"].dt.normalize() == today][["Timestamp", "pv_power_kw"]].copy()
    compare_frame = _build_compare_frame(today_frame, history_today)

    actual_today_energy_kwh: float | None = 0.0
    forecast_today_to_now_kwh: float | None = 0.0
    delta_today_kwh: float | None = 0.0
    intraday_correction = 1.0
    if not history_today.empty:
        history_hourly = (
            history_today.set_index("Timestamp")["pv_power_kw"]
            .resample("1h")
            .mean()
            .dropna()
            .reset_index()
        )
        actual_today_energy_kwh = float(history_hourly["pv_power_kw"].sum())
        latest_actual_time = pd.Timestamp(history_today["Timestamp"].max())
        forecast_to_now = compare_frame[compare_frame["Timestamp"] <= latest_actual_time].copy()
        valid_live = forecast_to_now[
            forecast_to_now["actual_pv_power_kw"].notna()
            & (forecast_to_now["predicted_pv_power_kw"] > 0.01)
        ].copy()
        if not valid_live.empty:
            intraday_correction = float(
                (valid_live["actual_pv_power_kw"] / valid_live["predicted_pv_power_kw"])
                .replace([math.inf, -math.inf], pd.NA)
                .dropna()
                .median()
            )
            if math.isnan(intraday_correction):
                intraday_correction = 1.0
            intraday_correction = max(0.65, min(intraday_correction, 1.45))
            all_today_mask = forecast["Timestamp"].dt.normalize() == today
            forecast.loc[all_today_mask, "predicted_pv_power_kw"] = (
                forecast.loc[all_today_mask, "predicted_pv_power_kw"] * intraday_correction
            ).clip(lower=0.0, upper=peak_power_kw * 1.05)
            today_frame = forecast[forecast["Timestamp"].dt.normalize() == today].copy()
            compare_frame = _build_compare_frame(today_frame, history_today)
            forecast_to_now = compare_frame[compare_frame["Timestamp"] <= latest_actual_time].copy()
        forecast_today_to_now_kwh = float(forecast_to_now["predicted_pv_power_kw"].sum()) if not forecast_to_now.empty else 0.0
        delta_today_kwh = actual_today_energy_kwh - forecast_today_to_now_kwh
        if intraday_correction != 1.0:
            notes.append(f"Intraday self-correction: x{intraday_correction:.2f}")
    else:
        notes.append("No actual PV points for today yet; showing zero progress until telemetry arrives.")

    today_energy_kwh = float(today_frame["predicted_pv_power_kw"].sum()) if not today_frame.empty else 0.0
    tomorrow_energy_kwh = float(tomorrow_frame["predicted_pv_power_kw"].sum()) if not tomorrow_frame.empty else 0.0
    peak_row = forecast.loc[forecast["predicted_pv_power_kw"].idxmax()] if not forecast.empty else None
    peak_at = ""
    peak_power_display = 0.0
    if peak_row is not None:
        peak_power_display = float(peak_row["predicted_pv_power_kw"])
        peak_at = pd.Timestamp(peak_row["Timestamp"]).strftime("%d.%m %H:%M")

    confidence_score = 0.35
    confidence_score += min(training_days / 240.0, 1.0) * 0.35
    confidence_score += min(matched_history_rows / 1000.0, 1.0) * 0.2
    confidence_score += 0.1 if not weather_history.empty else 0.0
    confidence_score = max(0.15, min(confidence_score, 0.95))

    driver_source = _select_next_hour_forecast_row(forecast)
    driver_snapshot = {
        "shortwave_radiation": float(driver_source["shortwave_radiation"]) if pd.notna(driver_source.get("shortwave_radiation")) else None,
        "cloud_cover": float(driver_source["cloud_cover"]) if pd.notna(driver_source.get("cloud_cover")) else None,
        "temperature_2m": float(driver_source["temperature_2m"]) if pd.notna(driver_source.get("temperature_2m")) else None,
        "wind_speed_10m": float(driver_source["wind_speed_10m"]) if pd.notna(driver_source.get("wind_speed_10m")) else None,
    }
    if pd.notna(driver_source.get("Timestamp")):
        notes.append(f"Driver hour: {pd.Timestamp(driver_source['Timestamp']).strftime('%d.%m %H:%M')}")

    if weather_history.empty:
        notes.append("Weather History dataset not found; using DessMonitor-only fallback calibration.")

    return PvForecastResult(
        source_pv_column=pv_column,
        forecast_frame=forecast.reset_index(drop=True),
        compare_frame=compare_frame.reset_index(drop=True),
        today_energy_kwh=today_energy_kwh,
        tomorrow_energy_kwh=tomorrow_energy_kwh,
        actual_today_energy_kwh=actual_today_energy_kwh,
        forecast_today_to_now_kwh=forecast_today_to_now_kwh,
        delta_today_kwh=delta_today_kwh,
        peak_power_kw=peak_power_display,
        peak_power_at=peak_at,
        confidence_score=confidence_score,
        confidence_label=_confidence_label(confidence_score),
        training_days=training_days,
        matched_history_rows=matched_history_rows,
        model_name=model_name,
        driver_snapshot=driver_snapshot,
        notes=notes,
    )
