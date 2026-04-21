from __future__ import annotations

from dataclasses import replace

import pandas as pd

from app.services.pv_forecast import PvForecastResult
from app.services.storage import list_saved_imports, load_import_dataframe


def build_forecast_snapshot_frame(result: PvForecastResult, *, run_at: pd.Timestamp) -> pd.DataFrame:
    frame = result.forecast_frame.copy()
    if frame.empty or "Timestamp" not in frame.columns:
        return pd.DataFrame(columns=["Timestamp", "predicted_pv_power_kw", "forecast_run_at", "lead_hours"])
    frame["Timestamp"] = pd.to_datetime(frame["Timestamp"], errors="coerce")
    frame = frame.dropna(subset=["Timestamp"]).copy()
    frame["predicted_pv_power_kw"] = pd.to_numeric(frame.get("predicted_pv_power_kw"), errors="coerce").fillna(0.0)
    run_at_ts = pd.Timestamp(run_at).tz_localize(None) if pd.Timestamp(run_at).tzinfo is not None else pd.Timestamp(run_at)
    frame["forecast_run_at"] = run_at_ts.strftime("%Y-%m-%d %H:%M:%S")
    lead_hours = ((frame["Timestamp"] - run_at_ts).dt.total_seconds() / 3600.0).fillna(0.0)
    frame["lead_hours"] = lead_hours.round(2).clip(lower=0.0)
    cols = [
        "Timestamp",
        "predicted_pv_power_kw",
        "forecast_run_at",
        "lead_hours",
        "shortwave_radiation",
        "cloud_cover",
        "temperature_2m",
        "wind_speed_10m",
        "precipitation",
    ]
    for column in cols:
        if column not in frame.columns:
            frame[column] = pd.NA
    return frame[cols].copy()


def latest_profile_forecast_snapshot(
    *,
    pn: str,
    devcode: str,
    devaddr: str,
) -> pd.DataFrame:
    target = (str(pn).strip(), str(devcode).strip(), str(devaddr).strip())
    for record in list_saved_imports(limit=120):
        metadata = record.metadata or {}
        if str(metadata.get("provider", "")).strip().lower() != "pv_forecast":
            continue
        if str(metadata.get("series_type", "")).strip().lower() != "hourly_72h":
            continue
        key = (
            str(metadata.get("pn", "")).strip(),
            str(metadata.get("devcode", "")).strip(),
            str(metadata.get("devaddr", "")).strip(),
        )
        if key != target:
            continue
        return load_import_dataframe(record.import_id)
    return pd.DataFrame()


def build_hourly_correction_from_snapshots(
    *,
    pn: str,
    devcode: str,
    devaddr: str,
    dessmonitor_frame: pd.DataFrame,
    timestamp_column: str,
    pv_column: str,
    max_records: int = 36,
) -> tuple[dict[int, float], int]:
    target = (str(pn).strip(), str(devcode).strip(), str(devaddr).strip())
    snapshot_frames: list[pd.DataFrame] = []
    for record in list_saved_imports(limit=300):
        metadata = record.metadata or {}
        if str(metadata.get("provider", "")).strip().lower() != "pv_forecast":
            continue
        if str(metadata.get("series_type", "")).strip().lower() != "hourly_72h":
            continue
        key = (
            str(metadata.get("pn", "")).strip(),
            str(metadata.get("devcode", "")).strip(),
            str(metadata.get("devaddr", "")).strip(),
        )
        if key != target:
            continue
        frame = load_import_dataframe(record.import_id)
        if not frame.empty:
            snapshot_frames.append(frame)
        if len(snapshot_frames) >= max_records:
            break

    if not snapshot_frames:
        return {}, 0
    snapshots = pd.concat(snapshot_frames, ignore_index=True)
    snapshots["Timestamp"] = pd.to_datetime(snapshots.get("Timestamp"), errors="coerce")
    snapshots["forecast_run_at"] = pd.to_datetime(snapshots.get("forecast_run_at"), errors="coerce")
    snapshots["predicted_pv_power_kw"] = pd.to_numeric(
        snapshots.get("predicted_pv_power_kw"), errors="coerce"
    ).fillna(0.0)
    snapshots["lead_hours"] = pd.to_numeric(snapshots.get("lead_hours"), errors="coerce")
    snapshots = snapshots.dropna(subset=["Timestamp", "forecast_run_at"]).copy()
    if snapshots.empty:
        return {}, 0

    snapshots = snapshots[snapshots["Timestamp"] >= snapshots["forecast_run_at"]].copy()
    if snapshots.empty:
        return {}, 0
    snapshots.sort_values(["Timestamp", "lead_hours", "forecast_run_at"], inplace=True)
    dedup = snapshots.drop_duplicates(subset=["Timestamp"], keep="first").copy()

    actual = dessmonitor_frame[[timestamp_column, pv_column]].copy()
    actual.columns = ["Timestamp", "actual_pv_power_kw"]
    actual["Timestamp"] = pd.to_datetime(actual["Timestamp"], errors="coerce")
    actual["actual_pv_power_kw"] = pd.to_numeric(actual["actual_pv_power_kw"], errors="coerce").fillna(0.0).clip(lower=0.0)
    actual.dropna(subset=["Timestamp"], inplace=True)
    if actual.empty:
        return {}, 0
    actual["Timestamp"] = actual["Timestamp"].dt.floor("h")
    actual_hourly = actual.groupby("Timestamp", dropna=True)["actual_pv_power_kw"].mean().reset_index()

    merged = dedup.merge(actual_hourly, on="Timestamp", how="inner")
    merged = merged[merged["predicted_pv_power_kw"] > 0.02].copy()
    if merged.empty:
        return {}, 0

    merged["ratio"] = (merged["actual_pv_power_kw"] / merged["predicted_pv_power_kw"]).replace([pd.NA, pd.NaT], 1.0)
    merged["ratio"] = pd.to_numeric(merged["ratio"], errors="coerce").fillna(1.0).clip(lower=0.4, upper=1.8)
    merged["hour"] = merged["Timestamp"].dt.hour
    grouped = merged.groupby("hour")["ratio"].agg(["median", "count"]).reset_index()
    if grouped.empty:
        return {}, len(merged.index)

    correction: dict[int, float] = {}
    for row in grouped.itertuples(index=False):
        hour = int(row.hour)
        sample_count = int(row.count)
        median_ratio = float(row.median)
        trust = min(sample_count / 10.0, 1.0)
        blended = 1.0 + (median_ratio - 1.0) * trust
        correction[hour] = max(0.65, min(blended, 1.35))
    return correction, int(len(merged.index))


def apply_hourly_correction(
    result: PvForecastResult,
    hourly_correction: dict[int, float],
    *,
    note_prefix: str = "Snapshot self-calibration",
) -> tuple[PvForecastResult, int]:
    if not hourly_correction:
        return result, 0
    frame = result.forecast_frame.copy()
    if frame.empty or "Timestamp" not in frame.columns or "predicted_pv_power_kw" not in frame.columns:
        return result, 0

    frame["Timestamp"] = pd.to_datetime(frame["Timestamp"], errors="coerce")
    frame["predicted_pv_power_kw"] = pd.to_numeric(frame["predicted_pv_power_kw"], errors="coerce").fillna(0.0)
    valid_mask = frame["Timestamp"].notna()
    if not bool(valid_mask.any()):
        return result, 0

    hours = frame.loc[valid_mask, "Timestamp"].dt.hour
    factors = hours.map(lambda hour: float(hourly_correction.get(int(hour), 1.0)))
    frame.loc[valid_mask, "predicted_pv_power_kw"] = frame.loc[valid_mask, "predicted_pv_power_kw"] * factors.to_numpy()

    compare = result.compare_frame.copy()
    if not compare.empty and "Timestamp" in compare.columns:
        compare["Timestamp"] = pd.to_datetime(compare["Timestamp"], errors="coerce")
        predicted_lookup = frame[["Timestamp", "predicted_pv_power_kw"]].dropna(subset=["Timestamp"]).drop_duplicates(
            subset=["Timestamp"], keep="last"
        )
        compare = compare.drop(columns=["predicted_pv_power_kw"], errors="ignore").merge(
            predicted_lookup,
            on="Timestamp",
            how="left",
        )

    today = pd.Timestamp.now().normalize()
    tomorrow = today + pd.Timedelta(days=1)
    today_frame = frame[frame["Timestamp"].dt.normalize() == today].copy()
    tomorrow_frame = frame[frame["Timestamp"].dt.normalize() == tomorrow].copy()
    today_energy_kwh = float(today_frame["predicted_pv_power_kw"].sum()) if not today_frame.empty else 0.0
    tomorrow_energy_kwh = float(tomorrow_frame["predicted_pv_power_kw"].sum()) if not tomorrow_frame.empty else 0.0
    peak_row = frame.loc[frame["predicted_pv_power_kw"].idxmax()] if not frame.empty else None
    peak_power_kw = float(peak_row["predicted_pv_power_kw"]) if peak_row is not None else 0.0
    peak_power_at = (
        pd.Timestamp(peak_row["Timestamp"]).strftime("%d.%m %H:%M")
        if peak_row is not None and pd.notna(peak_row.get("Timestamp"))
        else "--"
    )

    forecast_to_now_kwh = result.forecast_today_to_now_kwh
    delta_today_kwh = result.delta_today_kwh
    if not compare.empty and {"Timestamp", "actual_pv_power_kw", "predicted_pv_power_kw"}.issubset(compare.columns):
        compare["actual_pv_power_kw"] = pd.to_numeric(compare["actual_pv_power_kw"], errors="coerce")
        compare["predicted_pv_power_kw"] = pd.to_numeric(compare["predicted_pv_power_kw"], errors="coerce")
        valid_actual = compare[compare["actual_pv_power_kw"].notna()].copy()
        if not valid_actual.empty:
            latest_actual_time = pd.to_datetime(valid_actual["Timestamp"], errors="coerce").max()
            upto = compare[pd.to_datetime(compare["Timestamp"], errors="coerce") <= latest_actual_time].copy()
            forecast_to_now_kwh = (
                float(pd.to_numeric(upto["predicted_pv_power_kw"], errors="coerce").fillna(0.0).sum()) if not upto.empty else 0.0
            )
            if result.actual_today_energy_kwh is not None:
                delta_today_kwh = float(result.actual_today_energy_kwh) - float(forecast_to_now_kwh)

    notes = list(result.notes)
    notes.append(f"{note_prefix}: {len(hourly_correction)} hourly bin(s)")
    updated = replace(
        result,
        forecast_frame=frame.reset_index(drop=True),
        compare_frame=compare.reset_index(drop=True) if not compare.empty else compare,
        today_energy_kwh=today_energy_kwh,
        tomorrow_energy_kwh=tomorrow_energy_kwh,
        forecast_today_to_now_kwh=forecast_to_now_kwh,
        delta_today_kwh=delta_today_kwh,
        peak_power_kw=peak_power_kw,
        peak_power_at=peak_power_at,
        notes=notes,
    )
    return updated, int(valid_mask.sum())
