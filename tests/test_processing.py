from __future__ import annotations

import sqlite3
import pandas as pd
import pytest
from pathlib import Path
from PySide6.QtWidgets import QApplication

from app.components.filters import DateFilter, apply_date_filter
from app.services.analytics import calculate_stats
from app.services.app_settings import DeviceProfile
from app.services.data_processor import DataProcessorError, process_dataframe
from app.services.dessmonitor_api import _date_range, build_timeseries_dataframe
from app.services.pv_forecast import build_pv_forecast
from app.services import storage
from app.services.weather_api import _align_hourly_weather_to_timestamps, _normalize_hourly_forecast_frame, _select_nearest_weather_row
from app.ui.device_profiles_dialog import DeviceProfileEditDialog


def test_process_dataframe_detects_timestamp_and_numeric_columns() -> None:
    dataframe = pd.DataFrame(
        {
            "Timestamp": ["2026-03-22 00:00:00", "2026-03-22 01:00:00", "2026-03-22 02:00:00"],
            "PV Power": ["100", "200", "250"],
            "Battery Voltage": [51.2, 51.0, 50.8],
            "Status": ["ok", "ok", "ok"],
        }
    )

    processed = process_dataframe(dataframe)

    assert processed.timestamp_column == "Timestamp"
    assert processed.numeric_columns == ["PV Power", "Battery Voltage"]
    assert list(processed.dataframe.columns) == ["Timestamp", "PV Power", "Battery Voltage"]


def test_process_dataframe_raises_when_timestamp_missing() -> None:
    dataframe = pd.DataFrame({"PV Power": [1, 2, 3]})

    try:
        process_dataframe(dataframe)
    except DataProcessorError as exc:
        assert "timestamp" in str(exc).lower()
    else:  # pragma: no cover - explicit failure path
        raise AssertionError("Expected DataProcessorError when timestamp is missing")


def test_apply_date_filter_for_last_24_hours() -> None:
    dataframe = pd.DataFrame(
        {
            "Timestamp": pd.to_datetime(
                [
                    "2026-03-20 00:00:00",
                    "2026-03-21 10:00:00",
                    "2026-03-22 10:00:00",
                ]
            ),
            "PV Power": [10, 20, 30],
        }
    )

    filtered = apply_date_filter(dataframe, "Timestamp", DateFilter(preset="24h"))

    assert filtered["PV Power"].tolist() == [20, 30]


def test_calculate_stats_returns_min_max_avg() -> None:
    dataframe = pd.DataFrame({"SOC": [20, 50, 80]})

    stats = calculate_stats(dataframe, ["SOC"])

    assert len(stats) == 1
    assert stats[0].minimum == 20.0
    assert stats[0].maximum == 80.0
    assert stats[0].average == 50.0


def test_process_dataframe_handles_string_backed_excel_values() -> None:
    dataframe = pd.DataFrame(
        {
            "Timestamp": ["2026-03-23 14:38:57", "2026-03-23 14:34:03"],
            "Grid Power(W)": ["0", "3096"],
            "Battery Voltage(V)": ["53.1", "52.6"],
            "PV Power(W)": ["1502", "1523"],
            "Operating mode": ["Off-Grid Mode", "Off-Grid Mode"],
        }
    )

    processed = process_dataframe(dataframe)

    assert processed.timestamp_column == "Timestamp"
    assert processed.numeric_columns == ["Grid Power(W)", "Battery Voltage(V)", "PV Power(W)"]
    assert processed.dataframe["Grid Power(W)"].tolist() == [3096, 0]


def test_build_timeseries_dataframe_merges_multiple_parameters() -> None:
    pv_frame = pd.DataFrame(
        {
            "Timestamp": ["2026-03-23 10:00:00", "2026-03-23 10:05:00"],
            "PV_OUTPUT_POWER": ["1000", "1200"],
        }
    )
    grid_frame = pd.DataFrame(
        {
            "Timestamp": ["2026-03-23 10:00:00", "2026-03-23 10:10:00"],
            "GRID_ACTIVE_POWER": ["200", "180"],
        }
    )

    merged = build_timeseries_dataframe([pv_frame, grid_frame])

    assert list(merged.columns) == ["Timestamp", "PV_OUTPUT_POWER", "GRID_ACTIVE_POWER"]
    assert merged["Timestamp"].tolist() == [
        "2026-03-23 10:00:00",
        "2026-03-23 10:05:00",
        "2026-03-23 10:10:00",
    ]


def test_build_timeseries_dataframe_deduplicates_timestamp_rows() -> None:
    pv_frame = pd.DataFrame(
        {
            "Timestamp": ["2026-03-23 10:00:00", "2026-03-23 10:00:00", "2026-03-23 10:05:00"],
            "PV_OUTPUT_POWER": ["900", "1000", "1200"],
        }
    )
    grid_frame = pd.DataFrame(
        {
            "Timestamp": ["2026-03-23 10:00:00", "2026-03-23 10:10:00", "2026-03-23 10:10:00"],
            "GRID_ACTIVE_POWER": ["210", "190", "180"],
        }
    )

    merged = build_timeseries_dataframe([pv_frame, grid_frame])

    assert merged["Timestamp"].tolist() == [
        "2026-03-23 10:00:00",
        "2026-03-23 10:05:00",
        "2026-03-23 10:10:00",
    ]
    row_1000 = merged.loc[merged["Timestamp"] == "2026-03-23 10:00:00"].iloc[0]
    row_1010 = merged.loc[merged["Timestamp"] == "2026-03-23 10:10:00"].iloc[0]
    assert str(row_1000["PV_OUTPUT_POWER"]) == "1000"
    assert str(row_1010["GRID_ACTIVE_POWER"]) == "180"


def test_date_range_includes_both_boundaries() -> None:
    assert _date_range("2026-03-21", "2026-03-23") == [
        "2026-03-21",
        "2026-03-22",
        "2026-03-23",
    ]


def test_save_import_dataframe_keeps_existing_rows_for_same_device(tmp_path, monkeypatch) -> None:
    db_file = tmp_path / "imports.sqlite3"
    monkeypatch.setattr(storage, "database_path", lambda: Path(db_file))

    day_one = pd.DataFrame(
        {
            "Timestamp": ["2026-03-22 00:00:00", "2026-03-22 01:00:00"],
            "PV_OUTPUT_POWER": [10, 20],
        }
    )
    day_two = pd.DataFrame(
        {
            "Timestamp": ["2026-03-23 00:00:00", "2026-03-23 01:00:00"],
            "PV_OUTPUT_POWER": [30, 40],
        }
    )
    metadata = {
        "provider": "dessmonitor",
        "pn": "PN1",
        "devcode": "CODE1",
        "devaddr": "1",
        "sn": "SN1",
        "device_label": "Device 1",
    }

    import_id_one, _ = storage.save_import_dataframe(day_one, source_label="DessMonitor Device 1", metadata=metadata)
    import_id_two, _ = storage.save_import_dataframe(day_two, source_label="DessMonitor Device 1", metadata=metadata)

    assert import_id_one == import_id_two

    merged = storage.load_import_dataframe(import_id_two)
    assert merged["Timestamp"].tolist() == [
        "2026-03-22 00:00:00",
        "2026-03-22 01:00:00",
        "2026-03-23 00:00:00",
        "2026-03-23 01:00:00",
    ]


def test_save_import_dataframe_merges_legacy_datasets_with_different_serials(tmp_path, monkeypatch) -> None:
    db_file = tmp_path / "imports.sqlite3"
    monkeypatch.setattr(storage, "database_path", lambda: Path(db_file))

    day_one = pd.DataFrame(
        {
            "Timestamp": ["2026-03-22 00:00:00", "2026-03-22 01:00:00"],
            "PV_OUTPUT_POWER": [10, 20],
        }
    )
    day_two = pd.DataFrame(
        {
            "Timestamp": ["2026-03-23 00:00:00", "2026-03-23 01:00:00"],
            "PV_OUTPUT_POWER": [30, 40],
        }
    )
    metadata_one = {
        "provider": "dessmonitor",
        "pn": "PN1",
        "devcode": "CODE1",
        "devaddr": "1",
        "sn": "SN-OLD",
        "device_label": "Device 1",
    }
    metadata_two = {
        "provider": "dessmonitor",
        "pn": "PN1",
        "devcode": "CODE1",
        "devaddr": "1",
        "sn": "SN-NEW",
        "device_label": "Device 1",
    }

    import_id_one, _ = storage.save_import_dataframe(day_one, source_label="DessMonitor Device 1", metadata=metadata_one)
    import_id_two, _ = storage.save_import_dataframe(day_two, source_label="DessMonitor Device 1", metadata=metadata_two)

    assert import_id_one == import_id_two
    assert len(storage.list_saved_imports()) == 1

    merged = storage.load_import_dataframe(import_id_two)
    assert merged["Timestamp"].tolist() == [
        "2026-03-22 00:00:00",
        "2026-03-22 01:00:00",
        "2026-03-23 00:00:00",
        "2026-03-23 01:00:00",
    ]


def test_save_import_dataframe_preserves_existing_columns_on_overlapping_timestamps(tmp_path, monkeypatch) -> None:
    db_file = tmp_path / "imports.sqlite3"
    monkeypatch.setattr(storage, "database_path", lambda: Path(db_file))

    initial = pd.DataFrame(
        {
            "Timestamp": ["2026-03-24 00:00:00", "2026-03-24 00:10:00"],
            "PV_OUTPUT_POWER": [1.2, 1.4],
            "LOAD_ACTIVE_POWER": [0.5, 0.7],
            "GRID_ACTIVE_POWER": [0.0, 0.0],
            "BATTERY_ACTIVE_POWER": [0.2, 0.3],
        }
    )
    overlapping_partial = pd.DataFrame(
        {
            "Timestamp": ["2026-03-24 00:00:00", "2026-03-24 00:10:00"],
            "LOAD_ACTIVE_POWER": [0.55, 0.75],
        }
    )
    metadata = {
        "provider": "dessmonitor",
        "pn": "PN1",
        "devcode": "CODE1",
        "devaddr": "1",
        "sn": "SN1",
        "device_label": "Device 1",
    }

    import_id, _ = storage.save_import_dataframe(initial, source_label="DessMonitor Device 1", metadata=metadata)
    storage.save_import_dataframe(overlapping_partial, source_label="DessMonitor Device 1", metadata=metadata)

    merged = storage.load_import_dataframe(import_id)
    assert merged["Timestamp"].tolist() == [
        "2026-03-24 00:00:00",
        "2026-03-24 00:10:00",
    ]
    assert merged["PV_OUTPUT_POWER"].tolist() == [1.2, 1.4]
    assert merged["LOAD_ACTIVE_POWER"].tolist() == [0.55, 0.75]
    assert merged["GRID_ACTIVE_POWER"].tolist() == [0.0, 0.0]
    assert merged["BATTERY_ACTIVE_POWER"].tolist() == [0.2, 0.3]


@pytest.mark.gui
def test_device_profile_dialog_uses_coordinates_from_field_for_manual_location() -> None:
    app = QApplication.instance() or QApplication([])
    profile = DeviceProfile("Profile", "user", "pass", "", 1, "pn", "code", "addr", "sn", "Device")
    dialog = DeviceProfileEditDialog(None, profile=profile)

    manual_index = dialog.location_mode.findData("manual")
    dialog.location_mode.setCurrentIndex(manual_index)
    dialog.location_field.setText("49.872840, 24.185361.")
    dialog._manual_location_latitude = ""
    dialog._manual_location_longitude = ""

    saved = dialog.profile()

    assert saved.inverter_location_mode == "manual"
    assert saved.inverter_location_label == "49.872840, 24.185361"
    assert saved.inverter_latitude == "49.872840"
    assert saved.inverter_longitude == "24.185361"


@pytest.mark.gui
def test_device_profile_dialog_parses_coordinates_with_hidden_symbols() -> None:
    app = QApplication.instance() or QApplication([])
    profile = DeviceProfile("Profile", "user", "pass", "", 1, "pn", "code", "addr", "sn", "Device")
    dialog = DeviceProfileEditDialog(None, profile=profile)

    manual_index = dialog.location_mode.findData("manual")
    dialog.location_mode.setCurrentIndex(manual_index)
    dialog.location_field.setText("49.872840\u200b,\u00a024.185361")
    dialog._manual_location_latitude = ""
    dialog._manual_location_longitude = ""

    saved = dialog.profile()

    assert saved.inverter_location_mode == "manual"
    assert saved.inverter_location_label == "49.872840, 24.185361"
    assert saved.inverter_latitude == "49.872840"
    assert saved.inverter_longitude == "24.185361"


@pytest.mark.gui
def test_device_profile_dialog_restores_map_address_separately_from_coordinates() -> None:
    app = QApplication.instance() or QApplication([])
    profile = DeviceProfile(
        "Profile",
        "user",
        "pass",
        "",
        1,
        "pn",
        "code",
        "addr",
        "sn",
        "Device",
        inverter_location_mode="manual",
        inverter_location_label="49.872840, 24.185361",
        inverter_location_description="Верхня вулиця, Ямпіль, Україна",
        inverter_latitude="49.872840",
        inverter_longitude="24.185361",
    )
    dialog = DeviceProfileEditDialog(None, profile=profile)

    assert dialog.location_field.text() == "49.872840, 24.185361"
    assert "Верхня вулиця" in dialog.location_hint.text()

    saved = dialog.profile()

    assert saved.inverter_location_description == "Верхня вулиця, Ямпіль, Україна"


def test_weather_storage_merges_rows_for_same_device_history_dataset(monkeypatch, tmp_path) -> None:
    db_file = tmp_path / "weather.sqlite3"
    monkeypatch.setattr(storage, "database_path", lambda: Path(db_file))

    first = pd.DataFrame(
        {
            "Timestamp": ["2026-03-30 10:00:00"],
            "temperature_2m": [12.0],
            "cloud_cover": [40.0],
        }
    )
    second = pd.DataFrame(
        {
            "Timestamp": ["2026-03-30 11:00:00"],
            "temperature_2m": [13.0],
            "cloud_cover": [55.0],
        }
    )
    metadata = {
        "provider": "weather",
        "weather_provider": "open-meteo",
        "series_type": "history",
        "pn": "PN1",
        "devcode": "CODE1",
        "devaddr": "1",
        "sn": "SN1",
    }

    import_id_one, _ = storage.save_import_dataframe(first, source_label="Weather History Device 1", metadata=metadata)
    import_id_two, _ = storage.save_import_dataframe(second, source_label="Weather History Device 1", metadata=metadata)

    assert import_id_one == import_id_two
    merged = storage.load_import_dataframe(import_id_two)
    assert merged["Timestamp"].tolist() == ["2026-03-30 10:00:00", "2026-03-30 11:00:00"]


def test_pv_forecast_storage_appends_runs_into_single_dataset(monkeypatch, tmp_path) -> None:
    db_file = tmp_path / "pv_forecast.sqlite3"
    monkeypatch.setattr(storage, "database_path", lambda: Path(db_file))

    run_one = pd.DataFrame(
        {
            "Timestamp": ["2026-04-05 10:00:00", "2026-04-05 11:00:00"],
            "predicted_pv_power_kw": [1.1, 1.3],
            "forecast_run_at": ["2026-04-05 09:00:00", "2026-04-05 09:00:00"],
            "lead_hours": [1.0, 2.0],
        }
    )
    run_two = pd.DataFrame(
        {
            "Timestamp": ["2026-04-05 10:00:00", "2026-04-05 11:00:00"],
            "predicted_pv_power_kw": [1.0, 1.25],
            "forecast_run_at": ["2026-04-05 09:30:00", "2026-04-05 09:30:00"],
            "lead_hours": [0.5, 1.5],
        }
    )
    metadata = {
        "provider": "pv_forecast",
        "series_type": "hourly_72h",
        "pn": "PN1",
        "devcode": "CODE1",
        "devaddr": "1",
        "sn": "SN1",
        "device_label": "Device 1",
        "forecast_run_at": "2026-04-05T09:00:00+03:00",
    }

    import_id_one, _ = storage.save_import_dataframe(run_one, source_label="PV Forecast", metadata=metadata)
    metadata["forecast_run_at"] = "2026-04-05T09:30:00+03:00"
    import_id_two, _ = storage.save_import_dataframe(run_two, source_label="PV Forecast", metadata=metadata)

    assert import_id_one == import_id_two
    merged = storage.load_import_dataframe(import_id_two)
    assert len(merged.index) == 4
    assert sorted(merged["forecast_run_at"].astype(str).unique().tolist()) == [
        "2026-04-05 09:00:00",
        "2026-04-05 09:30:00",
    ]
    assert len(storage.list_saved_imports()) == 1


def test_initialize_database_consolidates_legacy_pv_forecast_datasets(monkeypatch, tmp_path) -> None:
    db_file = tmp_path / "pv_forecast_migration.sqlite3"
    monkeypatch.setattr(storage, "database_path", lambda: Path(db_file))

    with sqlite3.connect(db_file) as connection:
        connection.execute(
            """
            CREATE TABLE datasets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dataset_key TEXT NOT NULL UNIQUE,
                source_label TEXT NOT NULL,
                imported_at TEXT NOT NULL,
                row_count INTEGER NOT NULL,
                column_count INTEGER NOT NULL,
                metadata_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE dataset_rows (
                dataset_id INTEGER NOT NULL,
                timestamp_value TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (dataset_id, timestamp_value)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE device_history_bounds (
                dataset_key TEXT PRIMARY KEY,
                remote_first_data_at TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        metadata = {
            "provider": "pv_forecast",
            "series_type": "hourly_72h",
            "pn": "PN1",
            "devcode": "CODE1",
            "devaddr": "1",
            "sn": "SN1",
            "device_label": "Device 1",
        }
        connection.execute(
            """
            INSERT INTO datasets (dataset_key, source_label, imported_at, row_count, column_count, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "pv_forecast:PV Forecast 2026-04-05 09:00",
                "PV Forecast 2026-04-05 09:00",
                "2026-04-05T09:00:00+03:00",
                1,
                4,
                '{"provider":"pv_forecast","series_type":"hourly_72h","pn":"PN1","devcode":"CODE1","devaddr":"1","sn":"SN1","device_label":"Device 1","forecast_run_at":"2026-04-05 09:00:00"}',
            ),
        )
        connection.execute(
            """
            INSERT INTO datasets (dataset_key, source_label, imported_at, row_count, column_count, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                "pv_forecast:PV Forecast 2026-04-05 09:30",
                "PV Forecast 2026-04-05 09:30",
                "2026-04-05T09:30:00+03:00",
                1,
                4,
                '{"provider":"pv_forecast","series_type":"hourly_72h","pn":"PN1","devcode":"CODE1","devaddr":"1","sn":"SN1","device_label":"Device 1","forecast_run_at":"2026-04-05 09:30:00"}',
            ),
        )
        first_id = int(connection.execute("SELECT id FROM datasets ORDER BY id ASC LIMIT 1").fetchone()[0])
        second_id = int(connection.execute("SELECT id FROM datasets ORDER BY id DESC LIMIT 1").fetchone()[0])
        connection.execute(
            """
            INSERT INTO dataset_rows (dataset_id, timestamp_value, payload_json)
            VALUES (?, ?, ?)
            """,
            (
                first_id,
                "2026-04-05 10:00:00",
                '{"Timestamp":"2026-04-05 10:00:00","predicted_pv_power_kw":1.1,"forecast_run_at":"2026-04-05 09:00:00","lead_hours":1.0}',
            ),
        )
        connection.execute(
            """
            INSERT INTO dataset_rows (dataset_id, timestamp_value, payload_json)
            VALUES (?, ?, ?)
            """,
            (
                second_id,
                "2026-04-05 10:00:00",
                '{"Timestamp":"2026-04-05 10:00:00","predicted_pv_power_kw":1.0,"forecast_run_at":"2026-04-05 09:30:00","lead_hours":0.5}',
            ),
        )

    storage.initialize_database()
    records = [record for record in storage.list_saved_imports() if (record.metadata or {}).get("provider") == "pv_forecast"]
    assert len(records) == 1
    frame = storage.load_import_dataframe(records[0].import_id)
    assert len(frame.index) == 2
    assert sorted(frame["forecast_run_at"].astype(str).unique().tolist()) == [
        "2026-04-05 09:00:00",
        "2026-04-05 09:30:00",
    ]


def test_weather_alignment_reuses_dessmonitor_timestamp_grid() -> None:
    hourly = pd.DataFrame(
        {
            "time": ["2026-03-30 10:00:00", "2026-03-30 11:00:00"],
            "temperature_2m": [10.0, 12.0],
            "cloud_cover": [20.0, 60.0],
            "weather_code": [1, 2],
        }
    )

    aligned = _align_hourly_weather_to_timestamps(
        hourly,
        ["2026-03-30 10:00:00", "2026-03-30 10:30:00", "2026-03-30 11:00:00"],
    )

    assert aligned["Timestamp"].tolist() == [
        "2026-03-30 10:00:00",
        "2026-03-30 10:30:00",
        "2026-03-30 11:00:00",
    ]
    assert aligned["temperature_2m"].tolist() == [10.0, 11.0, 12.0]
    assert aligned["weather_code"].tolist() == [1, 1, 2]


def test_weather_alignment_handles_duplicate_dessmonitor_timestamps() -> None:
    hourly = pd.DataFrame(
        {
            "time": ["2026-03-30 10:00:00", "2026-03-30 11:00:00"],
            "temperature_2m": [10.0, 12.0],
            "weather_code": [1, 2],
        }
    )

    aligned = _align_hourly_weather_to_timestamps(
        hourly,
        [
            "2026-03-30 10:00:00",
            "2026-03-30 10:00:00",
            "2026-03-30 10:30:00",
            "2026-03-30 11:00:00",
        ],
    )

    assert aligned["Timestamp"].tolist() == [
        "2026-03-30 10:00:00",
        "2026-03-30 10:00:00",
        "2026-03-30 10:30:00",
        "2026-03-30 11:00:00",
    ]
    assert aligned["temperature_2m"].tolist() == [10.0, 10.0, 11.0, 12.0]
    assert aligned["weather_code"].tolist() == [1, 1, 1, 2]


def test_weather_alignment_handles_duplicate_weather_timestamps() -> None:
    hourly = pd.DataFrame(
        {
            "time": ["2026-03-30 10:00:00", "2026-03-30 10:00:00", "2026-03-30 11:00:00"],
            "temperature_2m": [10.0, 10.5, 12.0],
            "weather_code": [1, 2, 3],
        }
    )

    aligned = _align_hourly_weather_to_timestamps(
        hourly,
        ["2026-03-30 10:00:00", "2026-03-30 10:30:00", "2026-03-30 11:00:00"],
    )

    assert aligned["Timestamp"].tolist() == [
        "2026-03-30 10:00:00",
        "2026-03-30 10:30:00",
        "2026-03-30 11:00:00",
    ]
    assert aligned["temperature_2m"].tolist() == [10.5, 11.25, 12.0]
    assert aligned["weather_code"].tolist() == [2, 2, 3]


def test_weather_live_snapshot_picks_nearest_hour_row() -> None:
    hourly = pd.DataFrame(
        {
            "time": ["2026-03-30 10:00:00", "2026-03-30 11:00:00", "2026-03-30 12:00:00"],
            "temperature_2m": [10.0, 12.0, 13.0],
        }
    )

    row, observed_at = _select_nearest_weather_row(
        hourly,
        reference_time=pd.Timestamp("2026-03-30 11:22:00"),
    )

    assert float(row["temperature_2m"]) == 12.0
    assert observed_at.strftime("%Y-%m-%d %H:%M:%S") == "2026-03-30 11:00:00"


def test_weather_forecast_frame_is_normalized_to_timestamp_column() -> None:
    hourly = pd.DataFrame(
        {
            "time": ["2026-04-01 00:00:00", "2026-04-01 01:00:00"],
            "shortwave_radiation": [0.0, 15.0],
        }
    )

    normalized = _normalize_hourly_forecast_frame(hourly)

    assert list(normalized.columns) == ["Timestamp", "shortwave_radiation"]
    assert normalized["Timestamp"].tolist() == ["2026-04-01 00:00:00", "2026-04-01 01:00:00"]


def test_build_pv_forecast_returns_forecast_and_confidence() -> None:
    history = pd.DataFrame(
        {
            "Timestamp": pd.date_range("2026-03-28 00:00:00", periods=72, freq="1h").strftime("%Y-%m-%d %H:%M:%S"),
            "PV_OUTPUT_POWER": [
                0.0, 0.0, 0.0, 0.0, 0.05, 0.2, 0.45, 0.9, 1.25, 1.55, 1.8, 1.9,
                1.85, 1.65, 1.3, 0.88, 0.46, 0.18, 0.04, 0.0, 0.0, 0.0, 0.0, 0.0,
            ] * 3,
        }
    )
    weather_history = pd.DataFrame(
        {
            "Timestamp": history["Timestamp"],
            "shortwave_radiation": [
                0, 0, 0, 0, 20, 70, 160, 310, 460, 620, 760, 820,
                790, 700, 540, 340, 170, 60, 18, 0, 0, 0, 0, 0,
            ] * 3,
            "cloud_cover": [18] * 72,
            "temperature_2m": [16] * 72,
            "wind_speed_10m": [4] * 72,
            "precipitation": [0] * 72,
        }
    )
    weather_forecast = pd.DataFrame(
        {
            "Timestamp": pd.date_range("2026-03-31 00:00:00", periods=24, freq="1h").strftime("%Y-%m-%d %H:%M:%S"),
            "shortwave_radiation": [0, 0, 0, 0, 15, 65, 155, 300, 455, 615, 745, 805, 780, 690, 530, 325, 155, 52, 10, 0, 0, 0, 0, 0],
            "cloud_cover": [22] * 24,
            "temperature_2m": [17] * 24,
            "wind_speed_10m": [5] * 24,
            "precipitation": [0] * 24,
        }
    )

    result = build_pv_forecast(history, weather_forecast, weather_history_frame=weather_history)

    assert result.source_pv_column == "PV_OUTPUT_POWER"
    assert not result.forecast_frame.empty
    assert float(result.forecast_frame["predicted_pv_power_kw"].sum()) > 0
    assert result.peak_power_kw > 0
    assert 0.15 <= result.confidence_score <= 0.95
    assert result.model_name


def test_build_pv_forecast_self_corrects_with_actual_weather_pairs() -> None:
    history = pd.DataFrame(
        {
            "Timestamp": pd.date_range("2026-03-26 06:00:00", periods=60, freq="1h").strftime("%Y-%m-%d %H:%M:%S"),
            "PV_OUTPUT_POWER": [
                0.0, 0.02, 0.18, 0.34, 0.42, 0.36, 0.22, 0.1, 0.02, 0.0, 0.0, 0.0,
            ] * 5,
        }
    )
    weather_history = pd.DataFrame(
        {
            "Timestamp": history["Timestamp"],
            "shortwave_radiation": [
                0, 20, 80, 160, 220, 180, 110, 50, 10, 0, 0, 0,
            ] * 5,
            "cloud_cover": [25] * 60,
            "temperature_2m": [15] * 60,
            "wind_speed_10m": [3] * 60,
            "precipitation": [0] * 60,
        }
    )
    weather_forecast = pd.DataFrame(
        {
            "Timestamp": pd.date_range("2026-03-31 06:00:00", periods=12, freq="1h").strftime("%Y-%m-%d %H:%M:%S"),
            "shortwave_radiation": [0, 18, 75, 150, 210, 175, 108, 48, 8, 0, 0, 0],
            "cloud_cover": [25] * 12,
            "temperature_2m": [15] * 12,
            "wind_speed_10m": [3] * 12,
            "precipitation": [0] * 12,
        }
    )

    result = build_pv_forecast(history, weather_forecast, weather_history_frame=weather_history)

    assert result.model_name == "Adaptive self-correcting hybrid"
    assert any("Hourly correction bins" in note for note in result.notes)
    assert result.forecast_frame["predicted_pv_power_kw"].max() > 0.25
    assert len(result.compare_frame.index) >= len(history[history["Timestamp"].str.startswith("2026-03-31")].index)
