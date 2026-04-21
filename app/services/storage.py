from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from PySide6.QtCore import QStandardPaths

from app.services.dessmonitor_api import InverterSetting


@dataclass(slots=True)
class SavedImportRecord:
    import_id: int
    source_label: str
    imported_at: str
    row_count: int
    column_count: int
    metadata: dict[str, object]


_PV_FORECAST_MIGRATED_DATABASES: set[str] = set()


def database_path() -> Path:
    base_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    root = Path(base_dir or Path.home() / ".energy-excel-analyzer")
    root.mkdir(parents=True, exist_ok=True)
    return root / "imports.sqlite3"


def initialize_database() -> Path:
    db_path = database_path()
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS datasets (
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
            CREATE TABLE IF NOT EXISTS dataset_rows (
                dataset_id INTEGER NOT NULL,
                timestamp_value TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                PRIMARY KEY (dataset_id, timestamp_value),
                FOREIGN KEY(dataset_id) REFERENCES datasets(id)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS device_history_bounds (
                dataset_key TEXT PRIMARY KEY,
                remote_first_data_at TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS inverter_settings_cache (
                profile_name TEXT NOT NULL,
                pn TEXT NOT NULL,
                devcode TEXT NOT NULL,
                devaddr TEXT NOT NULL,
                field_id TEXT NOT NULL,
                name TEXT NOT NULL,
                display_name TEXT NOT NULL,
                raw_value_json TEXT,
                display_value TEXT NOT NULL,
                unit TEXT NOT NULL,
                category TEXT NOT NULL,
                writable INTEGER NOT NULL,
                options_json TEXT NOT NULL,
                hint TEXT NOT NULL,
                raw_name TEXT NOT NULL,
                loaded_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (profile_name, pn, devcode, devaddr, field_id)
            )
            """
        )
        db_identity = str(db_path)
        if db_identity not in _PV_FORECAST_MIGRATED_DATABASES:
            _consolidate_pv_forecast_datasets(connection)
            _PV_FORECAST_MIGRATED_DATABASES.add(db_identity)
    return db_path


def _dataset_key(source_label: str, metadata: dict[str, object]) -> str:
    provider = str(metadata.get("provider", "other")).strip().lower()
    if provider == "dessmonitor":
        return "dessmonitor:" + ":".join(
            [
                str(metadata.get("pn", "")).strip(),
                str(metadata.get("devcode", "")).strip(),
                str(metadata.get("devaddr", "")).strip(),
            ]
        )
    if provider == "weather":
        series_type = str(metadata.get("series_type", "history")).strip().lower() or "history"
        weather_provider = str(metadata.get("weather_provider", "default")).strip().lower() or "default"
        pn = str(metadata.get("pn", "")).strip()
        devcode = str(metadata.get("devcode", "")).strip()
        devaddr = str(metadata.get("devaddr", "")).strip()
        if any((pn, devcode, devaddr)):
            return "weather:" + ":".join([weather_provider, series_type, pn, devcode, devaddr])
        latitude = str(metadata.get("latitude", "")).strip()
        longitude = str(metadata.get("longitude", "")).strip()
        return "weather:" + ":".join([weather_provider, series_type, latitude, longitude])
    if provider == "excel":
        return f"excel:{str(metadata.get('file_name', source_label)).strip()}"
    if provider == "pv_forecast":
        series_type = str(metadata.get("series_type", "hourly_72h")).strip().lower() or "hourly_72h"
        pn = str(metadata.get("pn", "")).strip()
        devcode = str(metadata.get("devcode", "")).strip()
        devaddr = str(metadata.get("devaddr", "")).strip()
        if any((pn, devcode, devaddr)):
            return "pv_forecast:" + ":".join([series_type, pn, devcode, devaddr])
        sn = str(metadata.get("sn", "")).strip()
        device_label = str(metadata.get("device_label", "")).strip()
        return "pv_forecast:" + ":".join([series_type, sn, device_label])
    return f"{provider}:{source_label}"


def _metadata_matches_existing(metadata: dict[str, object], existing_metadata: dict[str, object]) -> bool:
    provider = str(metadata.get("provider", "other")).strip().lower()
    existing_provider = str(existing_metadata.get("provider", "other")).strip().lower()
    if provider != existing_provider:
        return False
    if provider == "dessmonitor":
        return (
            str(metadata.get("pn", "")).strip(),
            str(metadata.get("devcode", "")).strip(),
            str(metadata.get("devaddr", "")).strip(),
        ) == (
            str(existing_metadata.get("pn", "")).strip(),
            str(existing_metadata.get("devcode", "")).strip(),
            str(existing_metadata.get("devaddr", "")).strip(),
        )
    if provider == "weather":
        left_core = (
            str(metadata.get("weather_provider", "default")).strip().lower(),
            str(metadata.get("series_type", "history")).strip().lower(),
            str(metadata.get("pn", "")).strip(),
            str(metadata.get("devcode", "")).strip(),
            str(metadata.get("devaddr", "")).strip(),
        )
        right_core = (
            str(existing_metadata.get("weather_provider", "default")).strip().lower(),
            str(existing_metadata.get("series_type", "history")).strip().lower(),
            str(existing_metadata.get("pn", "")).strip(),
            str(existing_metadata.get("devcode", "")).strip(),
            str(existing_metadata.get("devaddr", "")).strip(),
        )
        if any(left_core[2:]) or any(right_core[2:]):
            return left_core == right_core
        return (
            left_core[0],
            left_core[1],
            str(metadata.get("latitude", "")).strip(),
            str(metadata.get("longitude", "")).strip(),
        ) == (
            right_core[0],
            right_core[1],
            str(existing_metadata.get("latitude", "")).strip(),
            str(existing_metadata.get("longitude", "")).strip(),
        )
    if provider == "excel":
        return str(metadata.get("file_name", "")).strip() == str(existing_metadata.get("file_name", "")).strip()
    if provider == "pv_forecast":
        return (
            str(metadata.get("series_type", "hourly_72h")).strip().lower(),
            str(metadata.get("pn", "")).strip(),
            str(metadata.get("devcode", "")).strip(),
            str(metadata.get("devaddr", "")).strip(),
            str(metadata.get("sn", "")).strip(),
            str(metadata.get("device_label", "")).strip(),
        ) == (
            str(existing_metadata.get("series_type", "hourly_72h")).strip().lower(),
            str(existing_metadata.get("pn", "")).strip(),
            str(existing_metadata.get("devcode", "")).strip(),
            str(existing_metadata.get("devaddr", "")).strip(),
            str(existing_metadata.get("sn", "")).strip(),
            str(existing_metadata.get("device_label", "")).strip(),
        )
    return str(metadata.get("source", "")).strip() == str(existing_metadata.get("source", "")).strip()


def _pv_forecast_row_storage_key(
    row_payload: dict[str, object],
    *,
    fallback_timestamp: str,
    fallback_run_at: str,
) -> str | None:
    timestamp_value = str(row_payload.get("Timestamp", fallback_timestamp)).strip() or str(fallback_timestamp).strip()
    if not timestamp_value:
        return None
    run_at_value = str(row_payload.get("forecast_run_at", fallback_run_at)).strip() or str(fallback_run_at).strip()
    if not run_at_value:
        run_at_value = "unknown"
    return f"{timestamp_value}||{run_at_value}"


def _consolidate_pv_forecast_datasets(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT id, source_label, imported_at, metadata_json
        FROM datasets
        ORDER BY imported_at DESC, id DESC
        """
    ).fetchall()
    grouped: dict[tuple[str, ...], list[tuple[int, str, str, dict[str, object]]]] = {}
    for dataset_id, source_label, imported_at, metadata_json in rows:
        metadata = json.loads(metadata_json or "{}")
        if not isinstance(metadata, dict):
            continue
        if str(metadata.get("provider", "")).strip().lower() != "pv_forecast":
            continue
        key = (
            str(metadata.get("series_type", "hourly_72h")).strip().lower() or "hourly_72h",
            str(metadata.get("pn", "")).strip(),
            str(metadata.get("devcode", "")).strip(),
            str(metadata.get("devaddr", "")).strip(),
            str(metadata.get("sn", "")).strip(),
            str(metadata.get("device_label", "")).strip(),
        )
        grouped.setdefault(key, []).append((int(dataset_id), str(source_label), str(imported_at), metadata))

    for entries in grouped.values():
        canonical_dataset_id, _source_label, canonical_imported_at, canonical_metadata = entries[0]
        all_rows: list[tuple[str, dict[str, object]]] = []
        all_columns: set[str] = set()
        latest_imported_at = canonical_imported_at
        latest_metadata = dict(canonical_metadata)

        for dataset_id, _label, imported_at, metadata in entries:
            row_records = connection.execute(
                """
                SELECT timestamp_value, payload_json
                FROM dataset_rows
                WHERE dataset_id = ?
                ORDER BY timestamp_value ASC
                """,
                (dataset_id,),
            ).fetchall()
            metadata_run_at = str(metadata.get("forecast_run_at", "")).strip() or imported_at
            for timestamp_value, payload_json in row_records:
                payload = json.loads(payload_json or "{}")
                if not isinstance(payload, dict):
                    continue
                storage_key = _pv_forecast_row_storage_key(
                    payload,
                    fallback_timestamp=str(timestamp_value),
                    fallback_run_at=metadata_run_at,
                )
                if storage_key is None:
                    continue
                all_rows.append((storage_key, payload))
                all_columns.update(str(key) for key in payload.keys())
            if imported_at >= latest_imported_at:
                latest_imported_at = imported_at
                latest_metadata = dict(metadata)

        if not all_rows:
            continue

        latest_metadata["provider"] = "pv_forecast"
        latest_metadata["columns"] = sorted(all_columns)
        dataset_key = _dataset_key("PV Forecast", latest_metadata)

        for dataset_id, _label, _imported_at, _metadata in entries:
            connection.execute("DELETE FROM dataset_rows WHERE dataset_id = ?", (dataset_id,))

        for storage_key, payload in all_rows:
            connection.execute(
                """
                INSERT INTO dataset_rows (dataset_id, timestamp_value, payload_json)
                VALUES (?, ?, ?)
                ON CONFLICT(dataset_id, timestamp_value) DO UPDATE SET
                    payload_json = excluded.payload_json
                """,
                (
                    canonical_dataset_id,
                    storage_key,
                    json.dumps(payload, ensure_ascii=False),
                ),
            )

        for dataset_id, _label, _imported_at, _metadata in entries[1:]:
            connection.execute("DELETE FROM datasets WHERE id = ?", (dataset_id,))

        row_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM dataset_rows WHERE dataset_id = ?",
                (canonical_dataset_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            UPDATE datasets
            SET dataset_key = ?, imported_at = ?, row_count = ?, column_count = ?, metadata_json = ?, source_label = ?
            WHERE id = ?
            """,
            (
                dataset_key,
                latest_imported_at,
                row_count,
                int(len(all_columns)),
                json.dumps(latest_metadata, ensure_ascii=False),
                "PV Forecast",
                canonical_dataset_id,
            ),
        )


def _find_matching_dataset_ids(connection: sqlite3.Connection, metadata: dict[str, object]) -> list[int]:
    rows = connection.execute(
        """
        SELECT id, metadata_json
        FROM datasets
        ORDER BY imported_at DESC, id DESC
        """
    ).fetchall()
    matches: list[int] = []
    for row in rows:
        existing_metadata = json.loads(row[1] or "{}")
        if isinstance(existing_metadata, dict) and _metadata_matches_existing(metadata, existing_metadata):
            matches.append(int(row[0]))
    return matches


def _is_missing_cell(value: object) -> bool:
    if value is None:
        return True
    if pd.isna(value):
        return True
    text = str(value).strip()
    return text == "" or text.lower() in {"nan", "none", "null"}


def _merge_row_payload(existing_payload: dict[str, object], new_payload: dict[str, object]) -> dict[str, object]:
    merged = dict(existing_payload)
    for key, value in new_payload.items():
        if key not in merged:
            merged[key] = value
            continue
        if not _is_missing_cell(value):
            merged[key] = value
        elif _is_missing_cell(merged.get(key)):
            merged[key] = value
    return merged


def save_import_dataframe(
    dataframe: pd.DataFrame,
    *,
    source_label: str,
    metadata: dict[str, object] | None = None,
) -> tuple[int, Path]:
    db_path = initialize_database()
    metadata = {"source": source_label, **(metadata or {})}
    dataset_key = _dataset_key(source_label, metadata)
    serialized = dataframe.copy()
    for column in serialized.columns:
        if pd.api.types.is_datetime64_any_dtype(serialized[column]):
            serialized[column] = serialized[column].dt.strftime("%Y-%m-%d %H:%M:%S")

    timestamp_column = next(
        (column for column in serialized.columns if str(column).strip().lower() == "timestamp"),
        serialized.columns[0] if len(serialized.columns) else None,
    )
    if timestamp_column is None:
        raise ValueError("Cannot save a dataset without a timestamp column.")

    metadata["columns"] = [str(column) for column in serialized.columns]
    imported_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")

    with sqlite3.connect(db_path) as connection:
        matching_dataset_ids = _find_matching_dataset_ids(connection, metadata)
        if matching_dataset_ids:
            dataset_id = matching_dataset_ids[0]
            duplicate_ids = matching_dataset_ids[1:]
            for duplicate_id in duplicate_ids:
                connection.execute(
                    """
                    INSERT INTO dataset_rows (dataset_id, timestamp_value, payload_json)
                    SELECT ?, timestamp_value, payload_json
                    FROM dataset_rows
                    WHERE dataset_id = ?
                    ON CONFLICT(dataset_id, timestamp_value) DO UPDATE SET
                        payload_json = excluded.payload_json
                    """,
                    (dataset_id, duplicate_id),
                )
                connection.execute("DELETE FROM dataset_rows WHERE dataset_id = ?", (duplicate_id,))
                connection.execute("DELETE FROM datasets WHERE id = ?", (duplicate_id,))
        else:
            connection.execute(
                """
                INSERT INTO datasets (dataset_key, source_label, imported_at, row_count, column_count, metadata_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    dataset_key,
                    source_label,
                    imported_at,
                    int(len(serialized.index)),
                    int(len(serialized.columns)),
                    json.dumps(metadata, ensure_ascii=False),
                ),
            )
            dataset_id = int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])

        provider = str(metadata.get("provider", "")).strip().lower()
        for row in serialized.to_dict(orient="records"):
            timestamp_value = row.get(timestamp_column)
            if timestamp_value is None or str(timestamp_value).strip() == "":
                continue
            storage_timestamp_value = str(timestamp_value).strip()
            if provider == "pv_forecast":
                storage_key = _pv_forecast_row_storage_key(
                    row,
                    fallback_timestamp=storage_timestamp_value,
                    fallback_run_at=str(metadata.get("forecast_run_at", "")).strip() or imported_at,
                )
                if storage_key is None:
                    continue
                storage_timestamp_value = storage_key
            existing_row = connection.execute(
                """
                SELECT payload_json
                FROM dataset_rows
                WHERE dataset_id = ? AND timestamp_value = ?
                """,
                (dataset_id, storage_timestamp_value),
            ).fetchone()
            if existing_row and existing_row[0]:
                existing_payload = json.loads(existing_row[0])
                if isinstance(existing_payload, dict):
                    row = _merge_row_payload(existing_payload, row)
            connection.execute(
                """
                INSERT INTO dataset_rows (dataset_id, timestamp_value, payload_json)
                VALUES (?, ?, ?)
                ON CONFLICT(dataset_id, timestamp_value) DO UPDATE SET
                    payload_json = excluded.payload_json
                """,
                (
                    dataset_id,
                    storage_timestamp_value,
                    json.dumps(row, ensure_ascii=False),
                ),
            )

        row_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM dataset_rows WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            UPDATE datasets
            SET dataset_key = ?, imported_at = ?, row_count = ?, column_count = ?, metadata_json = ?, source_label = ?
            WHERE id = ?
            """,
            (
                dataset_key,
                imported_at,
                row_count,
                int(len(serialized.columns)),
                json.dumps(metadata, ensure_ascii=False),
                source_label,
                dataset_id,
            ),
        )

    return dataset_id, db_path


def list_saved_imports(limit: int | None = None) -> list[SavedImportRecord]:
    db_path = initialize_database()
    query = """
        SELECT id, source_label, imported_at, row_count, column_count, metadata_json
        FROM datasets
        ORDER BY imported_at DESC, id DESC
    """
    params: tuple[object, ...] = ()
    if limit is not None:
        query += " LIMIT ?"
        params = (limit,)

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(query, params).fetchall()

    records: list[SavedImportRecord] = []
    for row in rows:
        metadata = json.loads(row[5] or "{}")
        records.append(
            SavedImportRecord(
                import_id=int(row[0]),
                source_label=str(row[1]),
                imported_at=str(row[2]),
                row_count=int(row[3]),
                column_count=int(row[4]),
                metadata=metadata if isinstance(metadata, dict) else {},
            )
        )
    return records


def load_import_dataframe(import_id: int) -> pd.DataFrame:
    db_path = initialize_database()
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT payload_json
            FROM dataset_rows
            WHERE dataset_id = ?
            ORDER BY timestamp_value ASC
            """,
            (import_id,),
        ).fetchall()

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame.from_records([json.loads(row[0]) for row in rows])


def latest_dessmonitor_metadata() -> dict[str, object] | None:
    for record in list_saved_imports():
        if record.metadata.get("provider") == "dessmonitor":
            return record.metadata
    return None


def latest_timestamp_in_database() -> str | None:
    metadata = latest_dessmonitor_metadata()
    if metadata is None:
        return None
    return latest_timestamp_for_metadata(metadata)


def latest_timestamp_for_metadata(metadata: dict[str, object]) -> str | None:
    if metadata is None:
        return None
    dataset_key = _dataset_key(str(metadata.get("source", "DessMonitor")), metadata)
    db_path = initialize_database()
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT MAX(dr.timestamp_value)
            FROM dataset_rows dr
            JOIN datasets ds ON ds.id = dr.dataset_id
            WHERE ds.dataset_key = ?
            """,
            (dataset_key,),
        ).fetchone()

    if not row or not row[0]:
        return None
    return str(row[0])


def latest_import_id_for_metadata(metadata: dict[str, object]) -> int | None:
    if metadata is None:
        return None
    dataset_key = _dataset_key(str(metadata.get("source", "DessMonitor")), metadata)
    db_path = initialize_database()
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT id
            FROM datasets
            WHERE dataset_key = ?
            ORDER BY imported_at DESC, id DESC
            LIMIT 1
            """,
            (dataset_key,),
        ).fetchone()

    if not row or row[0] is None:
        return None
    return int(row[0])


def cached_remote_first_data_for_metadata(metadata: dict[str, object]) -> str | None:
    if metadata is None:
        return None
    dataset_key = _dataset_key(str(metadata.get("source", "DessMonitor")), metadata)
    db_path = initialize_database()
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT remote_first_data_at
            FROM device_history_bounds
            WHERE dataset_key = ?
            """,
            (dataset_key,),
        ).fetchone()

    if not row or not row[0]:
        return None
    return str(row[0])


def save_cached_remote_first_data_for_metadata(metadata: dict[str, object], remote_first_data_at: str) -> None:
    if metadata is None or not str(remote_first_data_at).strip():
        return
    dataset_key = _dataset_key(str(metadata.get("source", "DessMonitor")), metadata)
    db_path = initialize_database()
    updated_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO device_history_bounds (dataset_key, remote_first_data_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(dataset_key) DO UPDATE SET
                remote_first_data_at = excluded.remote_first_data_at,
                updated_at = excluded.updated_at
            """,
            (dataset_key, str(remote_first_data_at).strip(), updated_at),
        )


def save_inverter_settings_cache(
    *,
    profile_name: str,
    pn: str,
    devcode: str,
    devaddr: str,
    settings: list[InverterSetting],
    loaded_at: str,
) -> None:
    key = (profile_name.strip(), pn.strip(), devcode.strip(), devaddr.strip())
    if not all(key):
        return
    db_path = initialize_database()
    updated_at = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            DELETE FROM inverter_settings_cache
            WHERE profile_name = ? AND pn = ? AND devcode = ? AND devaddr = ?
            """,
            key,
        )
        for item in settings:
            connection.execute(
                """
                INSERT INTO inverter_settings_cache (
                    profile_name,
                    pn,
                    devcode,
                    devaddr,
                    field_id,
                    name,
                    display_name,
                    raw_value_json,
                    display_value,
                    unit,
                    category,
                    writable,
                    options_json,
                    hint,
                    raw_name,
                    loaded_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key[0],
                    key[1],
                    key[2],
                    key[3],
                    item.field_id,
                    item.name,
                    item.display_name,
                    json.dumps(item.raw_value, ensure_ascii=False),
                    item.display_value,
                    item.unit,
                    item.category,
                    1 if item.writable else 0,
                    json.dumps(list(item.options), ensure_ascii=False),
                    item.hint,
                    item.raw_name,
                    loaded_at.strip(),
                    updated_at,
                ),
            )


def load_inverter_settings_cache(
    *,
    profile_name: str,
    pn: str,
    devcode: str,
    devaddr: str,
) -> tuple[list[InverterSetting], str] | None:
    key = (profile_name.strip(), pn.strip(), devcode.strip(), devaddr.strip())
    if not all(key):
        return None
    db_path = initialize_database()
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT
                field_id,
                name,
                display_name,
                raw_value_json,
                display_value,
                unit,
                category,
                writable,
                options_json,
                hint,
                raw_name,
                loaded_at
            FROM inverter_settings_cache
            WHERE profile_name = ? AND pn = ? AND devcode = ? AND devaddr = ?
            ORDER BY category COLLATE NOCASE, display_name COLLATE NOCASE, field_id COLLATE NOCASE
            """,
            key,
        ).fetchall()
    if not rows:
        return None

    loaded_at = str(rows[0][11]).strip()
    settings: list[InverterSetting] = []
    for row in rows:
        raw_value: str | int | float | None
        try:
            raw_value = json.loads(row[3]) if row[3] is not None else None
        except Exception:
            raw_value = str(row[3]).strip() if row[3] is not None else None
        options: tuple[tuple[str, str], ...] = ()
        try:
            raw_options = json.loads(row[8] or "[]")
            if isinstance(raw_options, list):
                parsed_options: list[tuple[str, str]] = []
                for option in raw_options:
                    if isinstance(option, (list, tuple)) and len(option) >= 2:
                        parsed_options.append((str(option[0]).strip(), str(option[1]).strip()))
                options = tuple(parsed_options)
        except Exception:
            options = ()
        settings.append(
            InverterSetting(
                field_id=str(row[0]),
                name=str(row[1]),
                display_name=str(row[2]),
                raw_value=raw_value,
                display_value=str(row[4]),
                unit=str(row[5]),
                category=str(row[6]),
                writable=bool(row[7]),
                options=options,
                hint=str(row[9]),
                raw_name=str(row[10]),
            )
        )
    return settings, loaded_at


def clear_database() -> Path:
    db_path = initialize_database()
    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM dataset_rows")
        connection.execute("DELETE FROM datasets")
        connection.commit()
    return db_path
