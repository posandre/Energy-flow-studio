from __future__ import annotations

import json
import math
import re
import sys
import threading
import hashlib
import ssl
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import requests
import urllib3
from PySide6.QtCore import QDate, QObject, QEvent, QRectF, QSize, Qt, QThread, QTimer, Signal, QStandardPaths
from PySide6.QtGui import QColor, QCloseEvent, QIcon, QPainter, QPainterPath, QPen, QPixmap, QWindow
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QStatusBar,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
    QListView,
    QPushButton,
    QSizePolicy,
)
from shiboken6 import isValid

from app.components.chart import build_chart_figure, export_all_formats, export_figure
from app.components.filters import DateFilter, apply_date_filter
from app.services.data_processor import DataProcessorError, ProcessedData, process_dataframe
from app.services.dessmonitor_api import (
    DessMonitorApiError,
    DessMonitorConfig,
    DeviceControlField,
    fetch_device_control_fields,
    fetch_device_control_value,
    fetch_device_location,
    humanize_error_text,
    InverterSetting,
    detect_remote_first_data_date,
    fetch_device_range_data,
)
from app.services.energy_flow import EnergyFlowSnapshot, fetch_energy_flow_snapshot
from app.services.excel_loader import ExcelLoaderError, load_excel
from app.services.app_settings import (
    DEFAULT_ENERGYFLOW_REFRESH_SECONDS,
    DeviceControlFieldProfile,
    DeviceProfile,
    TuyaSettings,
    load_ui_language,
    load_device_profiles,
    load_last_loaded_profile_name,
    save_device_profiles,
    save_last_loaded_profile_name,
)
from app.services.i18n import set_language, tr, tr_fragment, translate_widget_tree
from app.services.logging_utils import get_logger
from app.services.storage import (
    cached_remote_first_data_for_metadata,
    clear_database,
    load_inverter_settings_cache,
    list_saved_imports,
    latest_dessmonitor_metadata,
    latest_import_id_for_metadata,
    latest_timestamp_in_database,
    latest_timestamp_for_metadata,
    load_import_dataframe,
    save_import_dataframe,
    save_cached_remote_first_data_for_metadata,
    save_inverter_settings_cache,
)
from app.services.pv_forecast import (
    PvForecastError,
    PvForecastResult,
    _select_pv_power_column,
    build_pv_forecast,
)
from app.services.forecast_calibration import (
    apply_hourly_correction,
    build_forecast_snapshot_frame,
    build_hourly_correction_from_snapshots,
    latest_profile_forecast_snapshot,
)
from app.services.weather_api import (
    DEFAULT_WEATHER_FORECAST_FIELDS,
    DEFAULT_WEATHER_HISTORY_FIELDS,
    DEFAULT_WEATHER_PROVIDER,
    WeatherApiError,
    WeatherDatasetRequest,
    WeatherForecastRequest,
    WeatherLiveRequest,
    WeatherLiveSnapshot,
    fetch_weather_forecast_dataset,
    fetch_weather_history_dataset,
    fetch_weather_live_snapshot,
)
from app.services.tuya_api import (
    TuyaDevice,
    fetch_devices,
    set_device_power,
)
from app.ui.device_profiles_dialog import DeviceProfileEditDialog, DeviceProfilesDialog
from app.ui.dessmonitor_dialog import CalendarDateField, DessMonitorDialog
from app.ui.dialogs import (
    NEON_CLOSE_BUTTON_STYLE,
    NEON_HEADER_BAR_STYLE,
    ask_compact_confirmation,
    create_neon_header_bar,
    exec_modal_dialog,
    open_modal_dialog,
    show_compact_message,
)
from app.ui.energy_flow_widget import EnergyFlowWidget
from app.ui.forecast_tab import ForecastTab
from app.ui.inverter_analysis_dialog import InverterAnalysisDialog
from app.ui.inverter_settings_tab import InverterSettingsTab
from app.ui.pv_history_dialog import PowerHistoryDialog, PvHistoryDialog, WeatherMetricHistoryDialog
from app.ui.saved_data_section import SavedDataSection
from app.ui.settings_dialog import SettingsDialog
from app.ui.sidebar import SidebarWidget
from app.ui.design_system import compose_styles

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
LOGGER = get_logger(__name__)

AUTO_SYNC_INTERVAL_SECONDS = 12 * 60
AUTO_SYNC_LOOKBACK_MINUTES = 15
PROFILE_ACTIVATION_ENERGYFLOW_RETRY_DELAYS_MS = (700,)
ENERGYFLOW_REQUEST_TIMEOUT_MS = 75000
TUYA_REFRESH_TIMEOUT_MS = 35000


def _is_profile_activation_retryable_energyflow_error(message: str) -> bool:
    normalized = message.strip().lower()
    timeout_markers = (
        "request timed out",
        "did not respond in time",
        "connection to dessmonitor api timed out",
        "the request timed out",
        "read timed out",
        "connect timed out",
        "timed out",
    )
    return any(marker in normalized for marker in timeout_markers)


class DessMonitorImportWorker(QObject):
    progress = Signal(int, str)
    log = Signal(str)
    finished = Signal()
    failed = Signal(str)

    def __init__(self, config: DessMonitorConfig) -> None:
        super().__init__()
        self._config = config
        self.result_dataframe: pd.DataFrame | None = None
        self.resolved_first_remote_date: str | None = None

    def run(self) -> None:
        try:
            self.progress.emit(10, "Authenticating with DessMonitor")
            self.log.emit(
                f"Connecting to DessMonitor for device {self._config.device_label or self._config.sn or self._config.pn}."
            )
            if self._config.period_mode == "all_data":
                self.progress.emit(18, "Detecting remote history start")
                self.log.emit("Looking up the earliest remote date with saved data for this device...")
                detected_start = detect_remote_first_data_date(
                    self._config,
                    logger=self.log.emit,
                )
                self._config.date_from = detected_start
                self.resolved_first_remote_date = detected_start
                self.log.emit(f"Earliest remote data detected on {detected_start}.")
            dataframe = fetch_device_range_data(
                self._config,
                logger=self.log.emit,
                progress_callback=self.progress.emit,
            )
            self.log.emit(
                f"DessMonitor returned {len(dataframe.columns) - 1} parameter series for "
                f"{self._config.date_from} to {self._config.date_to}."
            )
            self.result_dataframe = dataframe
            self.finished.emit()
        except (DessMonitorApiError, DataProcessorError) as exc:
            self.failed.emit(str(exc))


class InverterSettingsWorker(QObject):
    progress = Signal(int, str)
    log = Signal(str)
    setting_loading = Signal(str, str)
    setting_loaded = Signal(object)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        config: DessMonitorConfig,
        *,
        force_refresh: bool = False,
        profile_fields: list[DeviceControlFieldProfile] | None = None,
    ) -> None:
        super().__init__()
        self._config = config
        self._force_refresh = force_refresh
        self._profile_fields = list(profile_fields or [])

    def run(self) -> None:
        try:
            from app.services.dessmonitor_api import authenticate

            self.progress.emit(5, "Authenticating with DessMonitor for inverter settings")
            auth = authenticate(self._config, logger=self.log.emit)
            profile_fields = [
                DeviceControlField(
                    field_id=item.field_id,
                    name=item.name,
                    unit=item.unit,
                    hint="",
                    category=item.category,
                    writable=item.writable,
                    options=tuple(item.options or ()),
                    raw_name=item.name,
                )
                for item in self._profile_fields
                if item.field_id
            ]
            if profile_fields:
                self.log.emit(f"Loaded {len(profile_fields)} control field(s) from the saved profile.")
            self.progress.emit(15, "Loading inverter control field list")
            try:
                live_fields = fetch_device_control_fields(
                    self._config,
                    logger=self.log.emit,
                    auth=auth,
                    force_refresh=self._force_refresh,
                )
            except DessMonitorApiError as exc:
                if profile_fields:
                    self.log.emit(f"Falling back to saved field list: {exc}")
                    live_fields = []
                else:
                    raise

            fields = self._merge_profile_and_live_fields(profile_fields, live_fields)
            self.log.emit(f"Loaded {len(fields)} control field(s).")
            settings = self._load_values_parallel(auth, fields)
            settings.sort(key=lambda item: (item.category, item.display_name.lower(), item.field_id))
            self.progress.emit(100, f"Loaded {len(settings)} inverter setting(s)")
            self.finished.emit(settings)
        except DessMonitorApiError as exc:
            self.failed.emit(str(exc))

    def _merge_profile_and_live_fields(
        self,
        profile_fields: list[DeviceControlField],
        live_fields: list[DeviceControlField],
    ) -> list[DeviceControlField]:
        if not profile_fields:
            return list(live_fields)
        if not live_fields:
            return list(profile_fields)

        live_by_id = {field.field_id: field for field in live_fields}
        merged: list[DeviceControlField] = []
        seen_ids: set[str] = set()

        for field in profile_fields:
            live = live_by_id.get(field.field_id)
            if live is None:
                merged.append(field)
            else:
                # Keep profile order, but prefer fresh API metadata/options when available.
                merged.append(
                    DeviceControlField(
                        field_id=field.field_id,
                        name=live.name or field.name,
                        unit=live.unit or field.unit,
                        hint=live.hint or field.hint,
                        category=live.category or field.category,
                        writable=live.writable if live.writable is not None else field.writable,
                        access_status_text=live.access_status_text or field.access_status_text,
                        options=live.options or field.options,
                        raw_name=live.raw_name or field.raw_name or live.name or field.name,
                    )
                )
            seen_ids.add(field.field_id)

        for field in live_fields:
            if field.field_id in seen_ids:
                continue
            merged.append(field)
        return merged

    def _load_values_parallel(self, auth, fields: list[DeviceControlField]) -> list[InverterSetting]:
        if not fields:
            return []

        settings: list[InverterSetting] = []
        total = len(fields)
        alias_lookup = {
            item.field_id: item.alias.strip()
            for item in self._profile_fields
            if item.field_id and item.alias.strip()
        }

        def load_single(field: DeviceControlField) -> InverterSetting:
            try:
                raw_value = fetch_device_control_value(
                    self._config,
                    field.field_id,
                    auth=auth,
                    force_refresh=self._force_refresh,
                )
                display_value = next(
                    (label for value, label in field.options if str(raw_value).strip() == value),
                    str(raw_value).strip() if raw_value is not None else "Not available",
                ) or "Not available"
            except DessMonitorApiError as exc:
                raw_value = None
                display_value = "Not available"
                self.log.emit(f"Failed to read {field.name or field.field_id}: {humanize_error_text(str(exc))}")

            return InverterSetting(
                field_id=field.field_id,
                name=field.name,
                display_name=alias_lookup.get(field.field_id, field.name),
                raw_value=raw_value,
                display_value=display_value,
                unit=field.unit,
                category=field.category,
                writable=field.writable,
                options=field.options,
                hint=field.hint,
                raw_name=field.raw_name,
            )

        self.log.emit("Reading setting values one by one...")
        for index, field in enumerate(fields, start=1):
            display_name = alias_lookup.get(field.field_id, field.name)
            self.setting_loading.emit(field.field_id, display_name)
            self.progress.emit(
                min(95, 15 + int(((index - 1) / total) * 80)),
                f"Reading inverter settings {index}/{total}: {display_name}",
            )
            setting = load_single(field)
            settings.append(setting)
            self.setting_loaded.emit(setting)
            self.progress.emit(
                min(95, 15 + int((index / total) * 80)),
                f"Reading inverter settings {index}/{total}: {setting.display_name}",
            )
        return settings


class EnergyFlowBridge(QObject):
    finished = Signal(int, object)
    failed = Signal(int, str)


class ForecastBridge(QObject):
    finished = Signal(int, object)
    failed = Signal(int, str)


class _DisabledChartView(QWidget):
    def set_placeholder(self, _message: str) -> None:
        return

    def set_figure(self, _figure: object) -> None:
        return


class SolarLoaderWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._phase = 0
        self._sun_rotation = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(45)
        self._timer.timeout.connect(self._animate)

    def _animate(self) -> None:
        self._phase = (self._phase + 1) % 48
        self._sun_rotation = (self._sun_rotation + 0.035) % math.tau
        self.update()

    def stop(self) -> None:
        self._timer.stop()
        self.update()

    def start(self) -> None:
        self._phase = 0
        self._sun_rotation = 0.0
        self._timer.start()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(9, 17, 31, 0))

        center_x = self.width() / 2
        sun_x = center_x - 98
        sun_y = 58
        panel_x = center_x + 6
        panel_y = 78

        painter.setBrush(QColor(250, 204, 21))
        painter.drawEllipse(sun_x - 18.0, sun_y - 18.0, 36.0, 36.0)

        ray_pen = QPen(QColor(253, 224, 71), 2.5)
        painter.setPen(ray_pen)
        for index in range(8):
            angle = (index / 8.0) * math.tau + self._sun_rotation
            inner = 25.0
            outer = 37.0 + (2.0 if index % 2 == 0 else 0.0)
            x1 = sun_x + inner * math.cos(angle)
            y1 = sun_y + inner * math.sin(angle)
            x2 = sun_x + outer * math.cos(angle)
            y2 = sun_y + outer * math.sin(angle)
            painter.drawLine(x1, y1, x2, y2)

        beam_start_1 = (sun_x + 16.0, sun_y + 8.0)
        beam_end_1 = (panel_x + 12.0, panel_y + 10.0)
        beam_start_2 = (sun_x + 14.0, sun_y + 18.0)
        beam_end_2 = (panel_x + 10.0, panel_y + 22.0)

        base_beam_pen = QPen(QColor(125, 211, 252, 105), 4)
        painter.setPen(base_beam_pen)
        painter.drawLine(*beam_start_1, *beam_end_1)
        painter.drawLine(*beam_start_2, *beam_end_2)

        pulse_pen = QPen(QColor(125, 211, 252, 210), 4)
        pulse_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pulse_pen)

        def draw_beam_pulse(start_x: float, start_y: float, end_x: float, end_y: float, progress: float) -> None:
            dx = end_x - start_x
            dy = end_y - start_y
            head = (progress + 0.22) % 1.0

            if head < progress:
                painter.drawLine(
                    start_x + dx * progress,
                    start_y + dy * progress,
                    end_x,
                    end_y,
                )
                painter.drawLine(
                    start_x,
                    start_y,
                    start_x + dx * head,
                    start_y + dy * head,
                )
                return

            painter.drawLine(
                start_x + dx * progress,
                start_y + dy * progress,
                start_x + dx * head,
                start_y + dy * head,
            )

        beam_progress = (self._phase / 12.0) % 1.0
        draw_beam_pulse(*beam_start_1, *beam_end_1, beam_progress)
        draw_beam_pulse(*beam_start_2, *beam_end_2, (beam_progress + 0.18) % 1.0)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(14, 165, 233))
        painter.drawRoundedRect(panel_x, panel_y, 86, 42, 8, 8)
        painter.setBrush(QColor(8, 47, 73))
        cell_w = 14
        cell_h = 9
        glow_col = (self._phase // 4) % 4
        for row in range(3):
            for col in range(4):
                color = QColor(14, 116, 144) if col != glow_col else QColor(125, 211, 252)
                painter.setBrush(color)
                painter.drawRoundedRect(panel_x + 6 + (col * 19), panel_y + 5 + (row * 12), cell_w, cell_h, 3, 3)

        stand_pen = QPen(QColor(148, 163, 184), 4)
        painter.setPen(stand_pen)
        painter.drawLine(panel_x + 26.0, panel_y + 43.0, panel_x + 40.0, panel_y + 57.0)
        painter.drawLine(panel_x + 60.0, panel_y + 43.0, panel_x + 46.0, panel_y + 57.0)
        painter.drawLine(panel_x + 22.0, panel_y + 57.0, panel_x + 64.0, panel_y + 57.0)

        painter.setPen(QColor(248, 250, 252))


def _format_import_date(value: str) -> str:
    normalized = str(value).strip()
    if not normalized:
        return "-"
    try:
        return pd.Timestamp(normalized).strftime("%d.%m.%Y")
    except Exception:
        return normalized


def _primary_device_name(label: str, fallback: str) -> str:
    normalized_label = str(label).strip()
    if normalized_label:
        return normalized_label.split("|", 1)[0].strip() or fallback
    return fallback


def _dessmonitor_import_summary(config: DessMonitorConfig, profile_name: str) -> str:
    device_value = _primary_device_name(
        config.device_label,
        config.sn or config.pn or "Selected device",
    )
    details_text = (
        f"PN {config.pn or '-'}"
        f" • addr {config.devaddr or '-'}"
        f" • code {config.devcode or '-'}"
    )
    return (
        f"Profile: {profile_name}\n"
        f"Device: {device_value}\n"
        f"Details: {details_text}\n"
        f"Period: {_format_import_date(config.date_from)} - {_format_import_date(config.date_to)}"
    )


def _format_loaded_status_message(
    source_label: str,
    metadata: dict[str, object] | None,
    *,
    row_count: int,
    metric_count: int,
    min_date,
    max_date,
    active_profile_name: str = "",
) -> tuple[str, str]:
    metadata = metadata or {}
    provider = str(metadata.get("provider", "")).strip().lower()
    if provider == "dessmonitor":
        profile_label = active_profile_name.strip() or str(metadata.get("device_label", "")).strip() or tr("DessMonitor profile")
        if min_date == max_date:
            date_label = pd.Timestamp(min_date).strftime("%d %b %Y")
        else:
            date_label = f"{pd.Timestamp(min_date).strftime('%d %b %Y')} - {pd.Timestamp(max_date).strftime('%d %b %Y')}"
        message = tr('Loaded profile "{profile_label}" • {row_count:,} rows • {metric_count} metrics • {date_label}').format(
            profile_label=profile_label,
            row_count=row_count,
            metric_count=metric_count,
            date_label=date_label,
        )
        tooltip = tr("DessMonitor | PN {pn} | addr {addr} | code {code} | SN {sn}").format(
            pn=str(metadata.get("pn", "")).strip() or "-",
            addr=str(metadata.get("devaddr", "")).strip() or "-",
            code=str(metadata.get("devcode", "")).strip() or "-",
            sn=str(metadata.get("sn", "")).strip() or "-",
        )
        return message, tooltip

    if provider == "excel":
        file_name = str(metadata.get("file_name", "")).strip() or source_label
        return (
            tr("Loaded Excel file {file_name} • {row_count:,} rows • {metric_count} metrics").format(
                file_name=file_name,
                row_count=row_count,
                metric_count=metric_count,
            ),
            file_name,
        )

    if provider == "weather":
        weather_provider = str(metadata.get("weather_provider", DEFAULT_WEATHER_PROVIDER)).strip() or DEFAULT_WEATHER_PROVIDER
        series_type = str(metadata.get("series_type", "history")).strip() or "history"
        device_label = str(metadata.get("device_label", "")).strip() or str(metadata.get("location_label", "")).strip() or source_label
        return (
            tr("Loaded weather {series_type} for {device_label} • {row_count:,} rows • {metric_count} metrics").format(
                series_type=tr_fragment(series_type),
                device_label=device_label,
                row_count=row_count,
                metric_count=metric_count,
            ),
            f"{weather_provider} | {tr_fragment(series_type)}",
        )
    if provider == "pv_forecast":
        device_label = str(metadata.get("device_label", "")).strip() or source_label
        run_at = str(metadata.get("forecast_run_at", "")).strip()
        return (
            tr("Loaded PV forecast for {device_label} • {row_count:,} rows • {metric_count} metrics").format(
                device_label=device_label,
                row_count=row_count,
                metric_count=metric_count,
            ),
            run_at or tr("PV forecast snapshot"),
        )

    return tr("Loaded {source_label} • {row_count:,} rows • {metric_count} metrics").format(
        source_label=source_label,
        row_count=row_count,
        metric_count=metric_count,
    ), source_label


class ProfileIconWidget(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(64, 64)
        self._icon = QPixmap()
        self._sync_active = False
        self._spinner_angle = 0
        self._spinner_timer = QTimer(self)
        self._spinner_timer.setInterval(80)
        self._spinner_timer.timeout.connect(self._advance_spinner)

    def setIcon(self, icon: QIcon) -> None:  # noqa: N802
        self._icon = icon.pixmap(QSize(64, 64))
        self.update()

    def setSyncActive(self, active: bool) -> None:  # noqa: N802
        if self._sync_active == active:
            return
        self._sync_active = active
        if active:
            self._spinner_angle = 0
            self._spinner_timer.start()
        else:
            self._spinner_timer.stop()
        self.update()

    def _advance_spinner(self) -> None:
        self._spinner_angle = (self._spinner_angle + 28) % 360
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if not self._icon.isNull():
            painter.drawPixmap(0, 0, self._icon)
        if self._sync_active:
            badge_rect = QRectF(41, 3, 20, 20)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#081a2d"))
            painter.drawEllipse(badge_rect)

            track_pen = QPen(QColor(103, 232, 249, 55), 2.3)
            track_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(track_pen)
            painter.drawArc(badge_rect.adjusted(3, 3, -3, -3), 0, 360 * 16)

            spinner_pen = QPen(QColor("#67e8f9"), 2.5)
            spinner_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(spinner_pen)
            painter.drawArc(
                badge_rect.adjusted(3, 3, -3, -3),
                int(-self._spinner_angle * 16),
                int(-220 * 16),
            )

        painter.end()


class ProfileCardButton(QFrame):
    change_profile_requested = Signal()
    edit_profile_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("DeviceProfileButton")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 0, 24, 0)
        layout.setSpacing(14)
        layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.icon_widget = ProfileIconWidget()
        self.icon_widget.setObjectName("DeviceProfileIcon")

        text_wrap = QWidget()
        text_layout = QVBoxLayout(text_wrap)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(4)
        text_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

        self.eyebrow_label = QLabel("Active Profile")
        self.eyebrow_label.setObjectName("DeviceProfileEyebrow")
        self.eyebrow_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.title_label = QLabel("Profile")
        self.title_label.setObjectName("DeviceProfileTitle")
        self.title_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        actions_row = QHBoxLayout()
        actions_row.setContentsMargins(0, 2, 0, 0)
        actions_row.setSpacing(14)
        self.change_profile_link = QPushButton("Choose")
        self.change_profile_link.setObjectName("DeviceProfileLink")
        self.change_profile_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.change_profile_link.clicked.connect(self.change_profile_requested.emit)
        self.edit_profile_link = QPushButton("Edit")
        self.edit_profile_link.setObjectName("DeviceProfileLink")
        self.edit_profile_link.setCursor(Qt.CursorShape.PointingHandCursor)
        self.edit_profile_link.clicked.connect(self.edit_profile_requested.emit)
        actions_row.addWidget(self.change_profile_link)
        actions_row.addWidget(self.edit_profile_link)
        actions_row.addStretch()

        text_layout.addWidget(self.eyebrow_label)
        text_layout.addWidget(self.title_label)
        text_layout.addLayout(actions_row)

        layout.addWidget(self.icon_widget)
        layout.addWidget(text_wrap, 1)

    def setIcon(self, icon: QIcon) -> None:  # noqa: N802
        self.icon_widget.setIcon(icon)

    def setProfileLines(self, title: str, pn: str = "", sn: str = "") -> None:  # noqa: N802
        del pn, sn
        self.title_label.setText(title)

    def setSyncActive(self, active: bool) -> None:  # noqa: N802
        del active
        # The Active Profile card should stay static; loading is shown on tab badges/buttons.
        self.icon_widget.setSyncActive(False)


class TuyaTileCard(QFrame):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._loading = False
        self._dash_offset = 0.0
        self._dash_timer = QTimer(self)
        self._dash_timer.setInterval(45)
        self._dash_timer.timeout.connect(self._advance_dash)

    def set_loading(self, loading: bool) -> None:
        if self._loading == loading:
            return
        self._loading = loading
        if loading:
            self._dash_offset = 0.0
            self._dash_timer.start()
        else:
            self._dash_timer.stop()
        self.update()

    def _advance_dash(self) -> None:
        self._dash_offset = (self._dash_offset - 1.6) % 1000.0
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if not self._loading:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(3, 3, -3, -3)

        glow_pen = QPen(QColor(34, 211, 238, 70), 6.0)
        glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        glow_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(glow_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, 16, 16)

        dash_pen = QPen(QColor("#22d3ee"), 2.0)
        dash_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        dash_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        dash_pen.setDashPattern([8.0, 6.0])
        dash_pen.setDashOffset(self._dash_offset)
        painter.setPen(dash_pen)
        painter.drawRoundedRect(rect, 16, 16)
        painter.end()


class MainTabBadge(QFrame):
    def __init__(self, icon: QIcon, text: str, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(104, 88)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setStyleSheet("background: transparent; border: none;")
        self._active = False
        self._loading = False
        self._dash_offset = 0.0
        self._dash_timer = QTimer(self)
        self._dash_timer.setInterval(45)
        self._dash_timer.timeout.connect(self._advance_dash)

        badge_layout = QVBoxLayout(self)
        badge_layout.setContentsMargins(0, 0, 0, 0)
        badge_layout.setSpacing(0)
        badge_layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.icon_label = QLabel()
        self.icon_label.setObjectName("MainTabBadgeIcon")
        self.icon_label.setFixedSize(42, 42)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setPixmap(icon.pixmap(QSize(42, 42)))

        self.text_label = QLabel(text)
        self.text_label.setObjectName("MainTabBadgeText")
        self.text_label.setFixedHeight(16)
        self.text_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        badge_layout.addWidget(self.icon_label, alignment=Qt.AlignmentFlag.AlignCenter)
        badge_layout.addWidget(self.text_label, alignment=Qt.AlignmentFlag.AlignCenter)
        self.set_active(False)

    def set_active(self, active: bool) -> None:
        if self._active == active:
            return
        self._active = active
        self.text_label.setStyleSheet(
            "color: #eff6ff; font-size: 14px; font-weight: 700; background: transparent;"
            if active
            else "color: #9fb0c5; font-size: 14px; font-weight: 600; background: transparent;"
        )
        self.update()

    def set_loading(self, loading: bool) -> None:
        if self._loading == loading:
            return
        self._loading = loading
        if loading:
            self._dash_offset = 0.0
            self._dash_timer.start()
        else:
            self._dash_timer.stop()
        self.update()

    def _advance_dash(self) -> None:
        self._dash_offset = (self._dash_offset - 1.6) % 1000.0
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        if not self._loading:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(4, 4, -4, -4)

        glow_pen = QPen(QColor(34, 211, 238, 60), 5.0)
        glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        glow_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(glow_pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, 16, 16)

        dash_pen = QPen(QColor("#22d3ee"), 1.8)
        dash_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        dash_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        dash_pen.setDashPattern([8.0, 6.0])
        dash_pen.setDashOffset(self._dash_offset)
        painter.setPen(dash_pen)
        painter.drawRoundedRect(rect, 16, 16)
        painter.end()


class ImportProgressDialog(QDialog):
    def __init__(self, parent=None, *, header_title: str = "Import Progress", mode: str = "import") -> None:
        super().__init__(parent)
        self.setModal(True)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowTitle(tr("Loading"))
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setObjectName("ImportProgressDialog")
        self.resize(560, 560)
        self._default_size = QSize(560, 560)
        self._compact_size = QSize(self._default_size)
        self._expanded_log_min_height = 620
        self._mode = mode
        self._header_title = tr_fragment(header_title)
        self.setMinimumSize(self._default_size)

        self.loader = SolarLoaderWidget()
        self.loader.setMinimumHeight(125)
        self.step_label = QLabel(tr("Step 1 of 5"))
        self.step_label.setObjectName("ProgressStep")
        self.message_label = QLabel(tr("Preparing import"))
        self.message_label.setObjectName("ProgressMessage")
        self.detail_label = QLabel(tr("Connecting to the selected source."))
        self.detail_label.setObjectName("ProgressDetail")
        self.detail_label.setWordWrap(True)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("ProgressSummary")
        self.summary_label.setWordWrap(True)
        self.summary_label.setMinimumHeight(72)
        self.summary_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)
        self.summary_label.setVisible(False)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        self.toggle_log_button = QPushButton(tr("View log"))
        self.toggle_log_button.setObjectName("SecondaryActionButton")
        self.toggle_log_button.clicked.connect(self._toggle_log)
        self.log_view = QTextBrowser()
        self.log_view.setVisible(False)
        self.log_view.setMaximumHeight(180)
        self.log_view.setOpenExternalLinks(False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 24, 24, 24)
        layout.setSpacing(10)
        layout.addWidget(create_neon_header_bar(self, self.reject, title=self._header_title))
        layout.addSpacing(8)
        layout.addWidget(self.loader)
        layout.addSpacing(10)
        layout.addWidget(self.step_label)
        layout.addWidget(self.message_label)
        layout.addWidget(self.detail_label)
        layout.addWidget(self.summary_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.toggle_log_button, alignment=Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self.log_view)

        self.setStyleSheet(
            compose_styles(
                IMPORT_PROGRESS_DIALOG_STYLE,
                NEON_CLOSE_BUTTON_STYLE,
                NEON_HEADER_BAR_STYLE,
            )
        )

    def _phase_text(self, value: int, message: str = "") -> str:
        if self._mode == "settings":
            if value >= 100:
                return tr("Step 4 of 4")
            if value >= 80:
                return tr("Step 4 of 4")
            if value >= 55:
                return tr("Step 3 of 4")
            if value >= 25:
                return tr("Step 2 of 4")
            return tr("Step 1 of 4")
        message_text = str(message or "").strip().lower()
        if value >= 100:
            return tr("Step 5 of 5")
        if "forecast" in message_text:
            return tr("Step 5 of 5")
        if value >= 80:
            return tr("Step 4 of 5")
        if value >= 55:
            return tr("Step 3 of 5")
        if value >= 25:
            return tr("Step 2 of 5")
        return tr("Step 1 of 5")

    def _headline_text(self, value: int, message: str) -> str:
        if self._mode == "settings":
            if value >= 100:
                return tr("Settings loaded")
            if value >= 80:
                return tr("Finishing settings list")
            if value >= 35:
                return tr("Reading parameter values")
            if "auth" in message.lower() or "sign" in message.lower():
                return tr("Checking access")
            if "field" in message.lower():
                return tr("Loading parameter structure")
            return tr("Starting settings load")
        if value >= 100:
            return tr("Import complete")
        if "forecast" in message.lower():
            return tr("Preparing forecast")
        if value >= 80:
            return tr("Updating charts")
        if value >= 55:
            return tr("Preparing imported data")
        if value >= 25:
            return tr("Downloading telemetry")
        if "auth" in message.lower() or "sign" in message.lower():
            return tr("Checking access")
        return tr("Starting import")

    def start(self, message: str, *, summary: str = "") -> None:
        self._log_popup_event("start_called", message=message, has_summary=bool(summary), visible=self.isVisible())
        self.step_label.setText(tr("Step 1 of 5"))
        self.message_label.setText(tr_fragment(message))
        self.detail_label.setText(tr("Connecting to the selected source."))
        self._compact_size = QSize(self._default_size)
        self.resize(self._default_size)
        self.progress_bar.setValue(0)
        self.log_view.clear()
        self.log_view.setVisible(False)
        self.toggle_log_button.setText(tr("View log"))
        self._sync_dialog_size_with_log(False)
        self.summary_label.setVisible(bool(summary))
        self.summary_label.setText(tr_fragment(summary))
        self.loader.start()
        self._ensure_modal_open()
        self._schedule_ui_refresh()

    def update_status(self, value: int, message: str) -> None:
        self.progress_bar.setValue(value)
        self.step_label.setText(self._phase_text(value, message))
        self.message_label.setText(self._headline_text(value, message))
        self.detail_label.setText(tr_fragment(message))
        self._schedule_ui_refresh()

    def finish_error(self, message: str) -> None:
        self._log_popup_event("finish_error_called", message=message, visible=self.isVisible())
        self.loader.stop()
        self.step_label.setText(tr("Import stopped"))
        self.message_label.setText(tr("Import failed"))
        self.detail_label.setText(self._compact_error_text(message))
        self.progress_bar.setValue(0)
        self._compact_size = QSize(self.size())
        self.log_view.setVisible(True)
        self.toggle_log_button.setText(tr("Hide log"))
        self._sync_dialog_size_with_log(True)
        self._ensure_modal_open()
        self._schedule_ui_refresh()

    def stop(self) -> None:
        self._log_popup_event("stop_called", visible=self.isVisible())
        self.loader.stop()
        self.hide()

    def append_log(self, message: str) -> None:
        self.log_view.append(message)
        self._schedule_ui_refresh()

    def _toggle_log(self) -> None:
        visible = not self.log_view.isVisible()
        if visible:
            self._compact_size = self.size()
        self.log_view.setVisible(visible)
        self.toggle_log_button.setText(tr("Hide log") if visible else tr("View log"))
        self._sync_dialog_size_with_log(visible)
        self._schedule_ui_refresh()

    def _sync_dialog_size_with_log(self, log_visible: bool) -> None:
        target = QSize(self._compact_size if not log_visible else self.size())
        if log_visible:
            target.setWidth(max(target.width(), self._default_size.width()))
            target.setHeight(max(target.height(), self._expanded_log_min_height))
        else:
            target.setWidth(max(self._compact_size.width(), self._default_size.width()))
            target.setHeight(max(self._compact_size.height(), self._default_size.height()))
            # Force compact geometry restore when log is hidden.
            self.setMinimumSize(self._default_size)
        if self.size() != target:
            self.resize(target)

    @staticmethod
    def _compact_error_text(message: str) -> str:
        text = " ".join(str(message or "").split())
        lower = text.lower()
        marker = " for url:"
        marker_pos = lower.find(marker)
        if marker_pos >= 0:
            text = text[:marker_pos].rstrip()
        return text or tr("Import failed.")

    def _schedule_ui_refresh(self) -> None:
        # Avoid nested event-loop pumping from UI callbacks.
        QTimer.singleShot(0, self.update)

    def _ensure_modal_open(self) -> None:
        was_visible = self.isVisible()
        self._log_popup_event("ensure_modal_open_enter", visible=was_visible)
        owner = self.parentWidget()
        if owner is not None and hasattr(owner, "_open_registered_modal"):
            owner._open_registered_modal(self, source=f"progress:{self._header_title}")
            self._log_popup_event("ensure_modal_open_delegated", visible=self.isVisible())
            return
        if self.isVisible():
            self.raise_()
            self.activateWindow()
            self._log_popup_event("ensure_modal_open_reactivate", visible=self.isVisible())
            return
        open_modal_dialog(self, frameless=True, application_modal=False, raise_and_activate=False)
        self._log_popup_event("ensure_modal_open_opened", visible=self.isVisible())

    def _log_popup_event(self, event: str, **details: object) -> None:
        _ = (event, details)
        return

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        try:
            self._log_popup_event("show_event", visible=self.isVisible())
            owner = self.parentWidget()
            if owner is not None and hasattr(owner, "_sync_popup_backdrop_state"):
                owner._sync_popup_backdrop_state()
        except Exception:
            LOGGER.exception("ImportProgressDialog showEvent handling failed")

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        try:
            self._log_popup_event("hide_event", visible=self.isVisible())
            owner = self.parentWidget()
            if owner is not None and hasattr(owner, "_sync_popup_backdrop_state"):
                owner._sync_popup_backdrop_state()
        except Exception:
            LOGGER.exception("ImportProgressDialog hideEvent handling failed")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        super().closeEvent(event)
        try:
            self._log_popup_event("close_event", visible=self.isVisible())
            owner = self.parentWidget()
            if owner is not None and hasattr(owner, "_sync_popup_backdrop_state"):
                owner._sync_popup_backdrop_state()
        except Exception:
            LOGGER.exception("ImportProgressDialog closeEvent handling failed")


class _InputBlockerOverlay(QWidget):
    """Visual overlay that also blocks interaction with widgets behind it."""

    _BLOCKED_EVENT_TYPES = {
        QEvent.Type.MouseButtonPress,
        QEvent.Type.MouseButtonRelease,
        QEvent.Type.MouseButtonDblClick,
        QEvent.Type.MouseMove,
        QEvent.Type.Wheel,
        QEvent.Type.ContextMenu,
        QEvent.Type.KeyPress,
        QEvent.Type.KeyRelease,
        QEvent.Type.ShortcutOverride,
        QEvent.Type.TouchBegin,
        QEvent.Type.TouchUpdate,
        QEvent.Type.TouchEnd,
    }

    def event(self, event):  # noqa: N802
        if event is not None and event.type() in self._BLOCKED_EVENT_TYPES:
            event.accept()
            owner = self.parentWidget()
            if owner is not None and hasattr(owner, "_schedule_popup_focus_recovery"):
                owner._schedule_popup_focus_recovery(reason="backdrop_input_blocked")
            return True
        return super().event(event)


class MainWindow(QMainWindow):
    """Main EnergyFlow shell coordinating data import, dashboards and popups."""
    tuya_power_change_finished = Signal(str, object)
    tuya_devices_fetch_finished = Signal(int, object)
    tuya_icon_download_finished = Signal(str, str, object)
    inverter_edit_sync_finished = Signal(str, object, object)

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(tr("EnergyFlow Studio"))
        self.resize(1440, 900)
        self.setAcceptDrops(True)

        self.processed_data: ProcessedData | None = None
        self.filtered_data = pd.DataFrame()
        self.current_figure = None
        self.current_dataset_metadata: dict[str, object] = {}
        self.active_device_profile: DeviceProfile | None = None
        self.current_source_label = ""
        self._import_thread: QThread | None = None
        self._import_worker: DessMonitorImportWorker | None = None
        self._settings_thread: QThread | None = None
        self._settings_worker: InverterSettingsWorker | None = None
        self._settings_profile_key: tuple[str, str, str, str] | None = None
        self._latest_inverter_settings: list[InverterSetting] = []
        self._latest_inverter_settings_key: tuple[str, str, str, str] | None = None
        self._inverter_settings_cache: dict[tuple[str, str, str, str], tuple[list[InverterSetting], str]] = {}
        self._inverter_edit_sync_inflight = False
        self._inverter_edit_sync_field_id: str | None = None
        self._inverter_edit_sync_dialog: QDialog | None = None
        self._pending_import_is_background = False
        self._pending_import_source_label = ""
        self._pending_import_metadata: dict[str, object] = {}
        self._pending_import_include_weather = False
        self._energyflow_thread: threading.Thread | None = None
        self._energyflow_worker = None
        self._energyflow_bridge = EnergyFlowBridge(self)
        self._energyflow_bridge.finished.connect(self._apply_energyflow_snapshot, Qt.ConnectionType.QueuedConnection)
        self._energyflow_bridge.failed.connect(self._handle_energyflow_error, Qt.ConnectionType.QueuedConnection)
        self._energyflow_request_id = 0
        self._energyflow_refresh_inflight = False
        self._energyflow_refresh_pending = False
        self._energyflow_auto_refresh_enabled = True
        # Autorefresh timers are unlocked only after successful startup profile import/activation.
        self._energyflow_auto_refresh_ready = False
        self._last_energyflow_snapshot: EnergyFlowSnapshot | None = None
        self._last_energyflow_snapshot_key: tuple[str, str, str, str] | None = None
        self._last_energyflow_updated_at_text = ""
        self._forecast_thread: threading.Thread | None = None
        self._forecast_bridge = ForecastBridge(self)
        self._forecast_bridge.finished.connect(self._apply_forecast_result, Qt.ConnectionType.QueuedConnection)
        self._forecast_bridge.failed.connect(self._handle_forecast_error, Qt.ConnectionType.QueuedConnection)
        self._forecast_request_id = 0
        self._forecast_refresh_inflight = False
        self._forecast_loaded_profile_key: tuple[str, str, str, str] | None = None
        self._forecast_actual_autorefresh_marker: tuple[tuple[str, str, str, str], str] | None = None
        self._forecast_best_result: PvForecastResult | None = None
        self._forecast_best_result_key: tuple[str, str, str, str] | None = None
        self._energyflow_manual_refresh_active = False
        self._energyflow_start_timers_after_refresh = False
        self._energyflow_status_override: str | None = None
        self._energyflow_refresh_started_monotonic: float | None = None
        self._energyflow_refresh_remaining = DEFAULT_ENERGYFLOW_REFRESH_SECONDS
        self._energyflow_refresh_spinner_phase = 0
        self._energyflow_refresh_spinner_timer = QTimer(self)
        self._energyflow_refresh_spinner_timer.setInterval(120)
        self._energyflow_refresh_spinner_timer.timeout.connect(self._tick_energyflow_refresh_spinner)
        self._energyflow_request_timeout_timer = QTimer(self)
        self._energyflow_request_timeout_timer.setSingleShot(True)
        self._energyflow_request_timeout_timer.timeout.connect(self._handle_energyflow_request_timeout_tick)
        self._energyflow_timeout_request_id = 0
        self._close_requested = False
        self._deferred_close_attempts = 0
        self._profile_recovery_attempted = False
        self._profile_activation_in_progress = False
        self._profile_activation_restore_saved_import = False
        self._profile_activation_energyflow_retry_count = 0
        self._last_profile_activation_signature: tuple[object, ...] | None = None
        self._last_profile_activation_started_monotonic = 0.0
        self._profile_activation_retry_timer = QTimer(self)
        self._profile_activation_retry_timer.setSingleShot(True)
        self._profile_activation_retry_timer.timeout.connect(self._retry_profile_activation_energyflow_snapshot)
        self._startup_sync_offer_shown = False
        self._startup_profile_initialized = False
        self._open_popup_dialogs: list[QDialog] = []
        self._popup_backdrop: QWidget | None = None
        self._popup_backdrop_last_state: bool | None = None
        self._popup_visible_signature = ""
        self._popup_focus_recovery_token = 0
        self._embedded_history_overlay: QWidget | None = None
        self._embedded_history_dialog: QDialog | None = None
        self._embedded_history_key = ""
        self._popup_backdrop_sync_timer = QTimer(self)
        self._popup_backdrop_sync_timer.setInterval(120)
        self._popup_backdrop_sync_timer.timeout.connect(self._sync_popup_backdrop_state)
        self._popup_block_target: QWidget | None = None
        self._auto_sync_timer = QTimer(self)
        self._auto_sync_timer.setInterval(AUTO_SYNC_INTERVAL_SECONDS * 1000)
        self._auto_sync_timer.timeout.connect(self._handle_auto_sync_timer_timeout)
        self._auto_sync_inflight = False
        self._tuya_settings = TuyaSettings()
        self._tuya_tab_index = -1
        self._tuya_loading = False
        self._tuya_refresh_request_id = 0
        self._tuya_action_inflight_ids: set[str] = set()
        self._tuya_icon_cache: dict[str, QPixmap] = {}
        self._tuya_icon_download_inflight: set[str] = set()
        self._tuya_icon_device_cache: dict[str, QPixmap] = {}
        self._tuya_icon_failed_device_ids: set[str] = set()
        self._tuya_icon_failed_last_attempt_epoch: dict[str, float] = {}
        self._tuya_icon_cache_dir = self._resolve_tuya_icon_cache_dir()
        self._tuya_devices: list[TuyaDevice] = []
        self._energyflow_tuya_icons_signature: tuple[tuple[str, str], ...] = ()
        self._energyflow_tuya_icons_updated_at_label = ""
        self._tuya_tile_action_controls: dict[str, tuple[QPushButton, TuyaTileCard]] = {}
        self._tuya_filter_mode = "all"
        self._tuya_filter_buttons: dict[str, QPushButton] = {}
        self._tuya_refresh_spinner_timer = QTimer(self)
        self._tuya_refresh_spinner_timer.setInterval(120)
        self._tuya_refresh_spinner_timer.timeout.connect(self._tick_tuya_refresh_spinner)
        self._tuya_refresh_spinner_phase = 0
        self._tuya_refresh_timeout_timer = QTimer(self)
        self._tuya_refresh_timeout_timer.setSingleShot(True)
        self._tuya_refresh_timeout_timer.timeout.connect(self._handle_tuya_refresh_timeout)
        self._tuya_gate_state_cache: dict[str, str] = {}
        self._tuya_gate_last_change_cache: dict[str, str] = {}
        self._tuya_poll_timer = QTimer(self)
        self._tuya_poll_timer.timeout.connect(self._handle_tuya_poll_timeout)
        self._tuya_countdown_timer = QTimer(self)
        self._tuya_countdown_timer.setInterval(1000)
        self._tuya_countdown_timer.timeout.connect(self._tick_tuya_countdown)
        self.tuya_power_change_finished.connect(self._finish_tuya_power_change, Qt.ConnectionType.QueuedConnection)
        self.tuya_devices_fetch_finished.connect(self._finish_tuya_devices_refresh, Qt.ConnectionType.QueuedConnection)
        self.tuya_icon_download_finished.connect(self._finish_tuya_icon_download, Qt.ConnectionType.QueuedConnection)
        self.inverter_edit_sync_finished.connect(self._on_inverter_edit_sync_finished, Qt.ConnectionType.QueuedConnection)
        self._last_weather_snapshot: WeatherLiveSnapshot | None = None
        self._weather_live_state = "unknown"
        self._weather_live_error = ""

        self.sidebar = SidebarWidget()
        self.sidebar.selection_changed.connect(self.refresh_chart)

        self.graph_view = _DisabledChartView()
        self.progress_popup = ImportProgressDialog(self)
        self.progress_popup.setWindowModality(Qt.WindowModality.WindowModal)
        self.progress_popup.setModal(True)
        self.settings_progress_popup = ImportProgressDialog(self, header_title="Settings Load", mode="settings")
        self.settings_progress_popup.setWindowModality(Qt.WindowModality.WindowModal)
        self.settings_progress_popup.setModal(True)
        self.inverter_sync_popup = ImportProgressDialog(self, header_title="Parameter Sync", mode="settings")
        self.inverter_sync_popup.setWindowModality(Qt.WindowModality.WindowModal)
        self.inverter_sync_popup.setModal(True)
        self._register_popup_dialog(
            self.progress_popup,
            delete_on_close=False,
            origin="progress_popup:init",
        )
        self._register_popup_dialog(
            self.settings_progress_popup,
            delete_on_close=False,
            origin="settings_progress_popup:init",
        )
        self._register_popup_dialog(
            self.inverter_sync_popup,
            delete_on_close=False,
            origin="inverter_sync_popup:init",
        )

        self.filter_combo = QComboBox()
        self.filter_combo.setObjectName("DashboardFilterField")
        self.filter_combo.addItem("All data", "all")
        self.filter_combo.addItem("Today", "today")
        self.filter_combo.addItem("Last 24h", "24h")
        self.filter_combo.addItem("Custom range", "custom")
        self.filter_combo.setView(QListView())
        self.filter_combo.currentIndexChanged.connect(self.on_filter_changed)

        today = QDate.currentDate()
        self.start_date = CalendarDateField(today, display_format="dd.MM.yyyy")
        self.end_date = CalendarDateField(today, display_format="dd.MM.yyyy")
        self.start_date.dateChanged.connect(self._handle_manual_date_change)
        self.end_date.dateChanged.connect(self._handle_manual_date_change)
        self.export_chart_button = QPushButton(self)
        self.export_chart_button.hide()
        self.export_all_button = QPushButton(self)
        self.export_all_button.hide()
        self.reset_chart_button = QPushButton(self)
        self.reset_chart_button.hide()
        self.energyflow_refresh_timer = QTimer(self)
        self.energyflow_refresh_timer.setInterval(DEFAULT_ENERGYFLOW_REFRESH_SECONDS * 1000)
        self.energyflow_refresh_timer.timeout.connect(self._handle_energyflow_timer_timeout)
        self.energyflow_countdown_timer = QTimer(self)
        self.energyflow_countdown_timer.setInterval(1000)
        self.energyflow_countdown_timer.timeout.connect(self._tick_energyflow_countdown)
        self._build_top_bar()
        self._build_layout()
        self._apply_styles()

        status_bar = QStatusBar()
        status_bar.setObjectName("MainStatusBar")
        self.setStatusBar(status_bar)
        self.statusBar().showMessage(tr("Open an Excel file or connect to DessMonitor to begin."))
        self._apply_ui_language(load_ui_language(), persist=False)
        self._update_filter_controls()
        self._update_chart_action_states()
        self._popup_backdrop_sync_timer.start()

    def _popup_modal_diag(self, event: str, **details: object) -> None:
        _ = (event, details)
        return

    def initialize_startup_profile(self) -> None:
        if self._startup_profile_initialized:
            self._popup_modal_diag("startup_profile:init_skipped_already_initialized")
            return
        self._startup_profile_initialized = True
        self._popup_modal_diag("startup_profile:init_started")
        self._initialize_startup_profile()

    def event(self, event):  # noqa: N802
        if self._should_redirect_main_focus(event):
            return True
        return super().event(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._close_requested = True
        self._deferred_close_attempts = 0
        self.energyflow_refresh_timer.stop()
        self.energyflow_countdown_timer.stop()
        self._energyflow_refresh_spinner_timer.stop()
        self._auto_sync_timer.stop()
        self._tuya_poll_timer.stop()
        self._tuya_countdown_timer.stop()
        self._tuya_refresh_timeout_timer.stop()
        self._energyflow_request_timeout_timer.stop()
        self._popup_backdrop_sync_timer.stop()
        self._set_energyflow_refresh_active(False)
        self.progress_popup.hide()
        self.settings_progress_popup.hide()
        self.inverter_sync_popup.hide()
        if self._embedded_history_dialog is not None or (
            self._embedded_history_overlay is not None and self._embedded_history_overlay.isVisible()
        ):
            self._close_embedded_history_dialog(reason="main_window_close")
        self._sync_popup_backdrop_state()
        if not self._prepare_threads_for_close():
            QApplication.instance().setQuitOnLastWindowClosed(False)
            self.setEnabled(False)
            self.statusBar().showMessage(tr("Stopping background requests before closing..."))
            QTimer.singleShot(200, self._finalize_deferred_close)
            event.ignore()
            return
        QApplication.instance().setQuitOnLastWindowClosed(True)
        super().closeEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._resize_popup_backdrop()
        self._resize_embedded_history_overlay()

    def _request_thread_shutdown(self, attr_name: str) -> None:
        thread = getattr(self, attr_name)
        if thread is None:
            return
        thread.requestInterruption()
        thread.quit()

    def _has_running_worker_threads(self) -> bool:
        return any(
            thread is not None and thread.isRunning()
            for thread in (self._import_thread, self._settings_thread)
        )

    def _prepare_threads_for_close(self) -> bool:
        self._request_thread_shutdown("_import_thread")
        self._request_thread_shutdown("_settings_thread")
        if not self._has_running_worker_threads():
            return True
        thread = self._import_thread
        if thread is not None and thread.isRunning():
            thread.wait(600)
        settings_thread = self._settings_thread
        if settings_thread is not None and settings_thread.isRunning():
            settings_thread.wait(600)
        return not self._has_running_worker_threads()

    def _force_stop_worker_threads(self) -> None:
        for thread in (self._import_thread, self._settings_thread):
            if thread is not None and thread.isRunning():
                thread.terminate()
                thread.wait(300)

    def _finalize_deferred_close(self) -> None:
        if self._prepare_threads_for_close():
            QApplication.instance().setQuitOnLastWindowClosed(True)
            self.close()
            return
        self._deferred_close_attempts += 1
        if self._deferred_close_attempts >= 15:
            self._force_stop_worker_threads()
            QApplication.instance().setQuitOnLastWindowClosed(True)
            self.close()
            return
        QTimer.singleShot(200, self._finalize_deferred_close)

    def _show_message(self, icon, title: str, text: str) -> None:
        kind = "info"
        if icon == QMessageBox.Icon.Critical:
            kind = "error"
        elif icon == QMessageBox.Icon.Warning:
            kind = "warning"
        elif icon == QMessageBox.Icon.Information:
            kind = "info"
        show_compact_message(self, kind=kind, title=tr_fragment(title), text=tr_fragment(text))

    def _set_tab_message(self, label: QLabel, text: str, *, error: bool) -> None:
        label.setText(tr_fragment(text))
        if error:
            label.setContentsMargins(0, 0, 0, 0)
            label.setMinimumHeight(40)
            label.setStyleSheet(
                "color: #fecaca; background: rgba(127, 29, 29, 0.35); "
                "border: 1px solid #ef4444; border-radius: 10px; "
                "padding: 10px 16px;"
            )
            return
        label.setMinimumHeight(0)
        label.setStyleSheet("")

    def _confirm_action(self, title: str, text: str) -> bool:
        return ask_compact_confirmation(
            self,
            title=tr(title),
            text=tr(text),
            accept_text=tr("Delete"),
            reject_text=tr("Cancel"),
            destructive=True,
        )

    def _build_top_bar(self) -> None:
        self.tabs_corner = QWidget()
        self.tabs_corner.setObjectName("TabsCorner")
        self.tabs_corner.setMinimumHeight(94)
        tabs_corner_layout = QHBoxLayout(self.tabs_corner)
        tabs_corner_layout.setContentsMargins(0, 0, 0, 0)
        tabs_corner_layout.setSpacing(8)
        tabs_corner_layout.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        tabs_corner_layout.addStretch(1)

        self.device_profile_button = ProfileCardButton()
        self.device_profile_button.change_profile_requested.connect(self.open_device_profiles_dialog)
        self.device_profile_button.edit_profile_requested.connect(self.open_active_profile_editor)
        self.device_profile_button.setIcon(self._build_device_profile_icon())
        tabs_corner_layout.addWidget(self.device_profile_button)

        self._update_device_profile_button()

    def _create_chart_action_button(
        self,
        icon_name: str,
        handler,
        tooltip: str,
        *,
        primary: bool = False,
    ) -> QPushButton:
        button = QPushButton()
        button.setObjectName("ChartPrimaryActionButton" if primary else "ChartActionButton")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setIcon(self._build_action_icon(icon_name, primary=primary))
        button.setIconSize(QSize(18, 18))
        button.setToolTip(tooltip)
        button.clicked.connect(handler)
        return button

    def _build_action_icon(self, icon_name: str, *, primary: bool) -> QIcon:
        pixmap = QPixmap(24, 24)
        pixmap.fill(Qt.GlobalColor.transparent)

        stroke = QColor("#eff6ff" if primary else "#dbeafe")
        accent = QColor("#67e8f9" if primary else "#38bdf8")

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        pen = QPen(stroke, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)

        if icon_name == "export":
            painter.drawRoundedRect(5, 16, 14, 4, 1.6, 1.6)
            painter.drawLine(12, 5, 12, 14)
            painter.drawLine(8, 10, 12, 14)
            painter.drawLine(16, 10, 12, 14)
        elif icon_name == "export_all":
            painter.drawRoundedRect(6, 6, 9, 11, 1.8, 1.8)
            painter.drawRoundedRect(10, 9, 9, 11, 1.8, 1.8)
            painter.setPen(QPen(accent, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawLine(17, 4, 17, 10)
            painter.drawLine(14, 7, 17, 10)
            painter.drawLine(20, 7, 17, 10)
        else:
            painter.setPen(QPen(stroke, 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            path = QPainterPath()
            path.arcMoveTo(5, 5, 14, 14, 40)
            path.arcTo(5, 5, 14, 14, 40, 260)
            painter.drawPath(path)
            painter.drawLine(16, 5, 19, 5)
            painter.drawLine(19, 5, 19, 8)

        painter.end()
        return QIcon(pixmap)

    def _build_device_profile_icon(self) -> QIcon:
        pixmap = QPixmap(68, 68)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        painter.setPen(QPen(QColor("#05070b"), 4.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        painter.setBrush(QColor("#c7d1db"))
        painter.drawRoundedRect(9, 4, 50, 57, 7, 7)

        painter.setBrush(QColor("#97a6b3"))
        painter.drawRoundedRect(9, 50, 50, 11, 3, 3)

        painter.setBrush(QColor("#2f8ff0"))
        painter.drawRoundedRect(22, 15, 24, 23, 4, 4)

        painter.setBrush(QColor("#a3eb22"))
        painter.drawRoundedRect(26, 20, 16, 8, 1.5, 1.5)

        painter.setBrush(QColor("#05070b"))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(27, 31, 3, 3)
        painter.drawEllipse(33, 31, 3, 3)
        painter.drawEllipse(39, 31, 3, 3)

        painter.setPen(QPen(QColor("#05070b"), 2.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(29, 41, 39, 41)

        painter.setPen(QPen(QColor("#05070b"), 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        painter.drawLine(22, 45, 46, 45)

        painter.end()
        return QIcon(pixmap)

    def _build_nav_icon(self, icon_name: str) -> QIcon:
        pixmap = QPixmap(44, 44)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        if icon_name == "charts":
            painter.setPen(QPen(QColor("#bfeaff"), 2.4, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawLine(8, 31, 16, 23)
            painter.drawLine(16, 23, 23, 25)
            painter.drawLine(23, 25, 33, 12)
            painter.setBrush(QColor("#1fb6ff"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(13, 20, 7, 7)
            painter.drawEllipse(20, 22, 7, 7)
            painter.drawEllipse(30, 9, 7, 7)
        elif icon_name == "energyflow":
            painter.setPen(QPen(QColor("#bfeaff"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(QColor("#10243a"))
            painter.drawRoundedRect(6, 15, 11, 14, 3, 3)
            painter.drawRoundedRect(27, 15, 11, 14, 3, 3)
            painter.setPen(QPen(QColor("#1fb6ff"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.drawLine(17, 22, 22, 22)
            painter.drawLine(22, 22, 24, 17)
            painter.drawLine(22, 22, 24, 27)
            painter.drawLine(24, 17, 27, 17)
            painter.drawLine(24, 27, 27, 27)
        elif icon_name == "settings":
            painter.setPen(QPen(QColor("#bfeaff"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(QColor("#10243a"))
            painter.drawRoundedRect(11, 10, 22, 24, 6, 6)
            painter.drawLine(17, 16, 27, 16)
            painter.drawLine(17, 22, 27, 22)
            painter.drawLine(17, 28, 23, 28)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#1fb6ff"))
            painter.drawEllipse(25, 25, 6, 6)
        elif icon_name == "tuya":
            painter.setPen(QPen(QColor("#bfeaff"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(QColor("#10243a"))
            painter.drawRoundedRect(10, 10, 24, 24, 8, 8)
            painter.drawLine(22, 14, 22, 30)
            painter.drawLine(16, 22, 28, 22)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#1fb6ff"))
            painter.drawEllipse(19, 19, 6, 6)
        else:
            painter.setPen(QPen(QColor("#bfeaff"), 2.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            painter.setBrush(QColor("#10243a"))
            painter.drawRoundedRect(10, 9, 14, 18, 3, 3)
            painter.drawRoundedRect(18, 13, 14, 18, 3, 3)
            painter.setPen(QPen(QColor("#1fb6ff"), 2.0, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            painter.drawLine(19, 18, 27, 18)
            painter.drawLine(19, 23, 27, 23)

        painter.end()
        return QIcon(pixmap)

    def _build_tab_badge(self, icon_name: str, text: str) -> MainTabBadge:
        return MainTabBadge(self._build_nav_icon(icon_name), text)

    def _is_tab_refresh_active(self, index: int) -> bool:
        if index == 0:
            return bool(
                self._energyflow_refresh_inflight
                or self._energyflow_refresh_pending
                or self._energyflow_refresh_spinner_timer.isActive()
            )
        if index == 1:
            return bool(self._forecast_refresh_inflight)
        if index == 2:
            return bool(self._settings_worker is not None or self._inverter_edit_sync_inflight)
        if index == 3:
            return bool(
                self._tuya_loading
                or self._tuya_refresh_spinner_timer.isActive()
                or bool(self._tuya_action_inflight_ids)
            )
        if index == 4:
            return bool(self._import_thread is not None or self._auto_sync_inflight)
        return False

    def _refresh_tab_badges(self) -> None:
        if not hasattr(self, "tabs"):
            return
        for index, badge in enumerate(getattr(self, "_tab_badges", [])):
            if not isinstance(badge, MainTabBadge):
                continue
            badge.set_active(index == self.tabs.currentIndex())
            badge.set_loading(self._is_tab_refresh_active(index))

    def _apply_tuya_settings(self, *, refresh_now: bool = False, refresh_if_empty: bool = True) -> None:
        settings = self._tuya_settings
        interval_ms = max(5000, settings.normalized_poll_interval_sec() * 1000)
        self._tuya_poll_timer.setInterval(interval_ms)
        self._update_tuya_tab_visibility()

        if not settings.enabled:
            self._tuya_poll_timer.stop()
            self._tuya_countdown_timer.stop()
            self._tuya_refresh_timeout_timer.stop()
            self._tuya_action_inflight_ids.clear()
            self._tuya_devices = []
            self._render_tuya_devices([])
            self._set_tab_message(self.tuya_status_label, "Enable Tuya in Settings to load devices.", error=False)
            self._set_tuya_refresh_loading(False)
            self.tuya_refresh_button.setEnabled(False)
            return

        self._set_tuya_refresh_loading(False)
        if refresh_now or (refresh_if_empty and not self._tuya_devices):
            self._refresh_tuya_devices()
        if not self._tuya_poll_timer.isActive():
            self._tuya_poll_timer.start()
        if not self._tuya_countdown_timer.isActive():
            self._tuya_countdown_timer.start()
        self._update_tuya_countdown_text()

    def _update_tuya_tab_visibility(self) -> None:
        if self._tuya_tab_index < 0:
            return
        self.tabs.setTabVisible(self._tuya_tab_index, self._tuya_settings.enabled)
        if not self._tuya_settings.enabled and self.tabs.currentIndex() == self._tuya_tab_index:
            self.tabs.setCurrentIndex(0)

    def _handle_tuya_poll_timeout(self) -> None:
        if not self._tuya_settings.enabled:
            self._tuya_poll_timer.stop()
            return
        if self._tuya_loading:
            self._ensure_tuya_polling_active()
            return
        self._refresh_tuya_devices()

    def _refresh_tuya_devices(self) -> None:
        if not self._tuya_settings.enabled or self._tuya_loading:
            return
        if self._tuya_poll_timer.isActive():
            self._tuya_poll_timer.stop()
            self._tuya_poll_timer.start()
        # Allow controlled retries for icons that previously failed.
        self._tuya_icon_failed_device_ids.clear()
        self._tuya_loading = True
        self._tuya_refresh_request_id += 1
        request_id = self._tuya_refresh_request_id
        self._set_tuya_refresh_loading(True)
        self._tuya_refresh_timeout_timer.start(TUYA_REFRESH_TIMEOUT_MS)
        self._set_tab_message(self.tuya_status_label, tr("Loading Tuya devices..."), error=False)
        def worker() -> None:
            try:
                devices = fetch_devices(
                    client_id=self._tuya_settings.client_id,
                    client_secret=self._tuya_settings.client_secret,
                    endpoint=self._tuya_settings.normalized_endpoint(),
                )
                result = {"ok": True, "devices": list(devices)}
            except Exception as exc:
                result = {"ok": False, "error": str(exc)}
            self.tuya_devices_fetch_finished.emit(request_id, result)

        threading.Thread(target=worker, daemon=True, name=f"tuya-refresh-{request_id}").start()

    def _handle_tuya_refresh_timeout(self) -> None:
        if not self._tuya_loading:
            return
        # Invalidate the late worker response and unlock UI state.
        self._tuya_refresh_request_id += 1
        self._tuya_loading = False
        self._set_tuya_refresh_loading(False)
        self._set_tab_message(self.tuya_status_label, tr("Tuya refresh timed out. Try Refresh again."), error=True)
        self._ensure_tuya_polling_active()

    def _finish_tuya_devices_refresh(self, request_id: int, result: dict[str, object]) -> None:
        if request_id != self._tuya_refresh_request_id:
            return
        self._tuya_refresh_timeout_timer.stop()
        try:
            if bool(result.get("ok")):
                devices = list(result.get("devices") or [])
                self._tuya_devices = devices
                self._render_tuya_devices(self._tuya_devices)
                message = tr_fragment(f"Loaded {len(devices)} Tuya device(s).")
                if not devices:
                    message = tr("No Tuya devices found in the linked project/account.")
                self._set_tab_message(self.tuya_status_label, message, error=False)
            else:
                exc_text = str(result.get("error", "Unknown Tuya error"))
                if self._tuya_devices:
                    self._set_tab_message(
                        self.tuya_status_label,
                        tr_fragment(f"Tuya error: {exc_text} (showing last known devices)"),
                        error=True,
                    )
                else:
                    self._set_tab_message(self.tuya_status_label, tr_fragment(f"Tuya error: {exc_text}"), error=True)
                    self._render_tuya_devices([])
        except Exception as exc:
            self._append_log(f"Tuya refresh UI warning: {exc}")
        finally:
            self._tuya_loading = False
            self._set_tuya_refresh_loading(False)
            self._ensure_tuya_polling_active()
            for device_id in list(self._tuya_action_inflight_ids):
                self._set_tuya_action_loading(device_id=device_id, loading=True)

    def _render_tuya_devices(self, devices: list[TuyaDevice]) -> None:
        self._sync_tuya_gate_change_metadata(devices)
        self._refresh_tuya_filter_button_states()
        self._render_tuya_tiles(self._filtered_tuya_devices(devices))
        self._sync_energyflow_tuya_icons()

    def _sync_energyflow_tuya_icons(self) -> None:
        if not hasattr(self, "energyflow_view"):
            return
        if not self._tuya_settings.enabled:
            self._energyflow_tuya_icons_signature = ()
            self._energyflow_tuya_icons_updated_at_label = ""
            self.energyflow_view.set_active_tuya_icons([])
            self.energyflow_view.set_active_tuya_icons_updated_at("")
            return
        updated_at = pd.Timestamp.now().strftime("%H:%M")
        icons: list[tuple[str, QPixmap]] = []
        signature_parts: list[tuple[str, str]] = []
        for device in self._tuya_devices:
            active_power_watts = self._tuya_device_power_watts(device)
            if not (bool(device.power_on) and bool(device.online) and active_power_watts > 0.0):
                continue
            pixmap = self._load_tuya_icon_pixmap(device.device_id, device.icon_url)
            if pixmap is None or pixmap.isNull():
                pixmap = self._tuya_fallback_icon_pixmap()
            device_name = (device.name or device.device_name or "Device").strip()
            icons.append((device_name, pixmap))
            signature_parts.append((device.device_id, device_name))
        signature = tuple(signature_parts)
        if signature != self._energyflow_tuya_icons_signature:
            self._energyflow_tuya_icons_signature = signature
            self.energyflow_view.set_active_tuya_icons(icons)
        if updated_at != self._energyflow_tuya_icons_updated_at_label:
            self._energyflow_tuya_icons_updated_at_label = updated_at
            self.energyflow_view.set_active_tuya_icons_updated_at(updated_at)

    def _tuya_fallback_icon_pixmap(self) -> QPixmap:
        pixmap = QPixmap(34, 34)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#22d3ee"))
        painter.drawEllipse(4, 4, 26, 26)
        painter.setBrush(QColor("#0b1220"))
        painter.drawEllipse(10, 10, 14, 14)
        painter.end()
        return pixmap

    def _tuya_device_current_amps(self, device: TuyaDevice) -> float:
        parsed_items = self._parse_tuya_measurements(device.measurements_text or "")
        for label, value in parsed_items:
            if "current" not in label.strip().lower():
                continue
            return self._parse_current_amps(value)
        text_fallback = self._extract_current_from_text(device.measurements_text or "")
        if text_fallback > 0.0:
            return text_fallback
        status_items = self._parse_tuya_status(device.status_text or "")
        for code, value in status_items:
            normalized_code = code.strip().lower()
            if "cur_current" not in normalized_code and "current" not in normalized_code:
                continue
            parsed = self._parse_status_current_amps(value)
            if parsed > 0.0:
                return parsed
        status_fallback = self._extract_status_current_from_text(device.status_text or "")
        if status_fallback > 0.0:
            return status_fallback
        return 0.0

    def _tuya_device_power_watts(self, device: TuyaDevice) -> float:
        parsed_items = self._parse_tuya_measurements(device.measurements_text or "")
        for label, value in parsed_items:
            if "power" not in label.strip().lower():
                continue
            return self._parse_power_watts(value)
        text_fallback = self._extract_power_from_text(device.measurements_text or "")
        if text_fallback > 0.0:
            return text_fallback
        status_items = self._parse_tuya_status(device.status_text or "")
        for code, value in status_items:
            normalized_code = code.strip().lower()
            if "cur_power" not in normalized_code and "power" not in normalized_code:
                continue
            parsed = self._parse_status_power_watts(value)
            if parsed > 0.0:
                return parsed
        status_fallback = self._extract_status_power_from_text(device.status_text or "")
        if status_fallback > 0.0:
            return status_fallback
        return 0.0

    def _parse_tuya_status(self, raw_text: str) -> list[tuple[str, str]]:
        normalized = (raw_text or "").strip()
        if not normalized or normalized.lower() == "no status":
            return []
        parts = [item.strip() for item in normalized.split(",") if item.strip()]
        pairs: list[tuple[str, str]] = []
        for part in parts:
            code, sep, value = part.partition("=")
            if not sep:
                continue
            clean_code = code.strip()
            clean_value = value.strip()
            if not clean_code or not clean_value:
                continue
            pairs.append((clean_code, clean_value))
        return pairs

    def _parse_status_current_amps(self, raw_value: str) -> float:
        text = (raw_value or "").strip().replace(",", ".")
        try:
            value = float(text)
        except ValueError:
            return 0.0
        value = abs(value)
        if value > 100.0:
            # Tuya status for current is commonly returned in mA-like raw units (example: 6637 -> 6.637 A).
            value /= 1000.0
        return value

    def _parse_status_power_watts(self, raw_value: str) -> float:
        text = (raw_value or "").strip().replace(",", ".")
        try:
            value = float(text)
        except ValueError:
            return 0.0
        return abs(value)

    def _extract_current_from_text(self, raw_text: str) -> float:
        normalized = (raw_text or "").strip()
        if not normalized:
            return 0.0
        match = re.search(r"current[^0-9-]*(-?\d+(?:[.,]\d+)?)\s*(ma|a)\b", normalized, flags=re.IGNORECASE)
        if not match:
            return 0.0
        raw_value = (match.group(1) or "").replace(",", ".")
        unit = (match.group(2) or "a").lower()
        try:
            value = abs(float(raw_value))
        except ValueError:
            return 0.0
        if unit == "ma":
            value /= 1000.0
        return value

    def _extract_power_from_text(self, raw_text: str) -> float:
        normalized = (raw_text or "").strip()
        if not normalized:
            return 0.0
        match = re.search(r"power[^0-9-]*(-?\d+(?:[.,]\d+)?)\s*(kw|w)\b", normalized, flags=re.IGNORECASE)
        if not match:
            return 0.0
        raw_value = (match.group(1) or "").replace(",", ".")
        unit = (match.group(2) or "w").lower()
        try:
            value = abs(float(raw_value))
        except ValueError:
            return 0.0
        if unit == "kw":
            value *= 1000.0
        return value

    def _extract_status_current_from_text(self, raw_text: str) -> float:
        normalized = (raw_text or "").strip()
        if not normalized:
            return 0.0
        match = re.search(r"cur_current\s*=\s*(-?\d+(?:[.,]\d+)?)", normalized, flags=re.IGNORECASE)
        if not match:
            return 0.0
        return self._parse_status_current_amps(match.group(1))

    def _extract_status_power_from_text(self, raw_text: str) -> float:
        normalized = (raw_text or "").strip()
        if not normalized:
            return 0.0
        match = re.search(r"cur_power\s*=\s*(-?\d+(?:[.,]\d+)?)", normalized, flags=re.IGNORECASE)
        if not match:
            return 0.0
        return self._parse_status_power_watts(match.group(1))

    def _parse_current_amps(self, raw_value: str) -> float:
        text = (raw_value or "").strip()
        if not text:
            return 0.0
        lowered = text.lower()
        unit = "a"
        if "ma" in lowered:
            unit = "ma"
        numeric = text
        for token in ("mA", "ma", "A", "a"):
            numeric = numeric.replace(token, "")
        numeric = numeric.strip().replace(",", ".")
        try:
            value = float(numeric)
        except ValueError:
            return 0.0
        if unit == "ma":
            value /= 1000.0
        if value < 0:
            value = abs(value)
        return value

    def _parse_power_watts(self, raw_value: str) -> float:
        text = (raw_value or "").strip()
        if not text:
            return 0.0
        numeric = text
        for token in ("kW", "kw", "W", "w"):
            numeric = numeric.replace(token, "")
        numeric = numeric.strip().replace(",", ".")
        try:
            value = float(numeric)
        except ValueError:
            return 0.0
        if "kw" in text.lower():
            value *= 1000.0
        return abs(value)

    def _set_tuya_refresh_loading(self, loading: bool) -> None:
        if loading:
            self._tuya_refresh_spinner_phase = 0
            self.tuya_refresh_button.setEnabled(False)
            self.tuya_refresh_button.setText(f"{tr('Refreshing')} ⠋")
            self._tuya_refresh_spinner_timer.start()
            self._refresh_tab_badges()
            return
        self._tuya_refresh_spinner_timer.stop()
        self._tuya_refresh_spinner_phase = 0
        self._update_tuya_countdown_text()
        self.tuya_refresh_button.setEnabled(self._tuya_settings.enabled and not self._tuya_loading)
        self._refresh_tab_badges()

    def _tick_tuya_refresh_spinner(self) -> None:
        frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
        self._tuya_refresh_spinner_phase = (self._tuya_refresh_spinner_phase + 1) % len(frames)
        self.tuya_refresh_button.setText(f"{tr('Refreshing')} {frames[self._tuya_refresh_spinner_phase]}")

    def _tick_tuya_countdown(self) -> None:
        self._ensure_tuya_polling_active()
        self._update_tuya_countdown_text()

    def _ensure_tuya_polling_active(self) -> None:
        if self._close_requested or not self._tuya_settings.enabled:
            return
        if not self._tuya_poll_timer.isActive():
            self._tuya_poll_timer.start()
        if not self._tuya_countdown_timer.isActive():
            self._tuya_countdown_timer.start()

    def _update_tuya_countdown_text(self) -> None:
        if not hasattr(self, "tuya_refresh_button"):
            return
        if self._tuya_loading:
            return
        if not self._tuya_settings.enabled:
            self.tuya_refresh_button.setText(tr("Refresh"))
            return
        interval_seconds = max(1, self._tuya_poll_timer.interval() // 1000)
        remaining_ms = self._tuya_poll_timer.remainingTime()
        if remaining_ms < 0:
            remaining_seconds = interval_seconds
        else:
            remaining_seconds = max(0, math.ceil(remaining_ms / 1000))
        minutes, seconds = divmod(remaining_seconds, 60)
        self.tuya_refresh_button.setText(f"{tr('Refresh')} {minutes:02d}:{seconds:02d}")

    def _set_tuya_filter_mode(self, mode: str) -> None:
        normalized = mode if mode in {"all", "online", "offline", "gates"} else "all"
        self._tuya_filter_mode = normalized
        self._refresh_tuya_filter_button_states()
        self._render_tuya_tiles(self._filtered_tuya_devices(self._tuya_devices))

    def _refresh_tuya_filter_button_states(self) -> None:
        has_devices = bool(self._tuya_devices)
        for mode, button in self._tuya_filter_buttons.items():
            button.blockSignals(True)
            button.setChecked(mode == self._tuya_filter_mode)
            button.setEnabled(has_devices)
            button.blockSignals(False)

    def _filtered_tuya_devices(self, devices: list[TuyaDevice]) -> list[TuyaDevice]:
        if self._tuya_filter_mode == "online":
            return [device for device in devices if device.online]
        if self._tuya_filter_mode == "offline":
            return [device for device in devices if not device.online]
        if self._tuya_filter_mode == "gates":
            return [device for device in devices if self._is_tuya_gate_device(device)]
        return list(devices)

    def _format_tuya_update_time(self, epoch_seconds: float | None) -> str:
        if not epoch_seconds:
            return "—"
        try:
            return datetime.fromtimestamp(float(epoch_seconds)).strftime("%H:%M:%S")
        except (TypeError, ValueError, OSError):
            return "—"

    def _render_tuya_tiles(self, devices: list[TuyaDevice]) -> None:
        self._tuya_tile_action_controls = {}
        while self.tuya_tile_grid.count():
            item = self.tuya_tile_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        columns = 4
        for col in range(columns):
            self.tuya_tile_grid.setColumnStretch(col, 1)
        for idx, device in enumerate(devices):
            row = idx // columns
            col = idx % columns
            card = self._build_tuya_tile_card(device)
            self.tuya_tile_grid.addWidget(card, row, col)
        self.tuya_tile_grid.setRowStretch((len(devices) + 1) // columns, 1)

    def _build_tuya_tile_card(self, device: TuyaDevice) -> QWidget:
        card = TuyaTileCard()
        card.setObjectName("TuyaTileCard")
        card.setFixedHeight(360)
        card.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 12, 14, 6)
        layout.setSpacing(8)

        top_row = QHBoxLayout()
        top_row.setSpacing(8)

        icon_stack = QVBoxLayout()
        icon_stack.setContentsMargins(0, 0, 0, 0)
        icon_stack.setSpacing(6)
        online_state = tr("Online") if device.online else tr("Offline")
        online_color = "#22c55e" if device.online else "#ef4444"
        online_label = QLabel(f"● {online_state}")
        online_label.setObjectName("TuyaTileOnline")
        online_label.setStyleSheet(f"color: {online_color}; font-size: 13px; font-weight: 700; background: transparent;")
        icon_stack.addWidget(online_label, alignment=Qt.AlignmentFlag.AlignLeft)
        icon_cell = self._build_tuya_icon_cell(device.device_id, device.icon_url)
        icon_stack.addWidget(icon_cell, alignment=Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        top_row.addLayout(icon_stack)
        top_row.addStretch(1)

        toggle_btn = QPushButton("⏻")
        toggle_btn.setObjectName("TuyaTileToggleButton")
        toggle_btn.setMinimumSize(32, 32)
        toggle_btn.setMaximumSize(32, 32)
        toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle_btn.setStyleSheet(self._tuya_power_button_style(device.power_on, loading=False, tile=True))
        target_state = True if device.power_on is None else (not device.power_on)
        toggle_btn.clicked.connect(
            lambda _checked=False, did=device.device_id, state=target_state: self._request_tuya_power_change(did, state)
        )
        top_row.addWidget(toggle_btn, alignment=Qt.AlignmentFlag.AlignTop)
        layout.addLayout(top_row)

        name_label = QLabel(device.name or device.device_name or tr("Unnamed device"))
        name_label.setObjectName("TuyaTileName")
        name_label.setWordWrap(True)
        layout.addWidget(name_label)

        meta_label = QLabel(
            f"{tr('Device name')}: {device.device_name or '-'}\n{tr('ID')}: {device.device_id}"
        )
        meta_label.setObjectName("TuyaTileMeta")
        meta_label.setTextFormat(Qt.TextFormat.PlainText)
        meta_label.setWordWrap(True)
        layout.addWidget(meta_label)

        update_label = QLabel(f"{tr('Last update')}: {self._format_tuya_update_time(device.last_update_epoch)}")
        update_label.setObjectName("TuyaTileMeta")
        update_label.setTextFormat(Qt.TextFormat.PlainText)
        layout.addWidget(update_label)

        layout.addStretch(1)
        layout.addSpacing(2)
        if self._is_tuya_gate_device(device):
            layout.addWidget(
                self._build_tuya_gate_status_panel(
                    status_text=device.status_text,
                    last_change=self._tuya_gate_last_change_cache.get(device.device_id, "—"),
                )
            )
        else:
            measurements_panel = self._build_tuya_measurements_panel(device.measurements_text)
            layout.addWidget(measurements_panel)

        self._tuya_tile_action_controls[device.device_id] = (toggle_btn, card)
        if device.device_id in self._tuya_action_inflight_ids:
            toggle_btn.setEnabled(False)
            card.set_loading(True)
        return card


    def _friendly_tuya_error(self, text: str) -> str:
        clean = text.strip()
        if not clean:
            return tr("Unknown Tuya error")
        first_line = clean.splitlines()[0].strip()
        if first_line.lower().startswith("tuya api error"):
            return first_line
        return first_line

    def _format_tuya_measurements_column(self, raw_text: str) -> str:
        normalized = (raw_text or "").strip()
        if not normalized or normalized.lower() == "no measurements":
            return f"{tr('Measurements')}:\n{tr('No measurements')}"
        items = [item.strip() for item in normalized.split(",") if item.strip()]
        if not items:
            return f"{tr('Measurements')}:\n{tr('No measurements')}"
        return f"{tr('Measurements')}:\n" + "\n".join(items)

    def _build_tuya_measurements_panel(self, raw_text: str) -> QWidget:
        container = QWidget()
        container.setObjectName("TuyaMetricPanel")
        grid = QGridLayout(container)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)

        parsed_items = self._parse_tuya_measurements(raw_text)
        raw_map = {label.strip().lower(): value.strip() for label, value in parsed_items}
        ordered_items = [
            (tr("Energy"), raw_map.get("energy", "—")),
            (tr("Current"), raw_map.get("current", "—")),
            (tr("Power"), raw_map.get("power", "—")),
            (tr("Voltage"), raw_map.get("voltage", "—")),
        ]

        for idx, (label, value) in enumerate(ordered_items):
            card = QFrame()
            card.setObjectName("TuyaMetricCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(10, 8, 10, 8)
            card_layout.setSpacing(4)

            label_widget = QLabel(label)
            label_widget.setObjectName("TuyaMetricLabel")
            value_widget = QLabel(value)
            value_widget.setObjectName("TuyaMetricValue")
            value_widget.setTextFormat(Qt.TextFormat.PlainText)
            value_widget.setWordWrap(False)
            value_widget.setProperty("muted", "true" if self._is_tuya_metric_muted(value) else "false")
            value_widget.style().unpolish(value_widget)
            value_widget.style().polish(value_widget)

            card_layout.addWidget(label_widget)
            card_layout.addWidget(value_widget)

            row = idx // 2
            col = idx % 2
            grid.addWidget(card, row, col)

        return container

    def _is_tuya_metric_muted(self, value: str) -> bool:
        cleaned = (value or "").strip()
        if cleaned in {"", "—"}:
            return True
        numeric = cleaned
        for unit in ("kWh", "kwh", "кВт*год", "Wh", "Вт*год", "A", "mA", "W", "Вт", "V", "s"):
            numeric = numeric.replace(unit, "")
        numeric = numeric.strip()
        try:
            return float(numeric) == 0.0
        except ValueError:
            return False

    def _parse_tuya_measurements(self, raw_text: str) -> list[tuple[str, str]]:
        normalized = (raw_text or "").strip()
        if not normalized or normalized.lower() == "no measurements":
            return []

        chunks = [item.strip() for item in normalized.split(",") if item.strip()]
        pairs: list[tuple[str, str]] = []
        for chunk in chunks:
            label, sep, value = chunk.partition(":")
            if not sep:
                continue
            clean_label = label.strip()
            clean_value = tr_fragment(value.strip())
            if not clean_label or not clean_value:
                continue
            pairs.append((clean_label, clean_value))
        return pairs

    def _is_tuya_gate_device(self, device: TuyaDevice) -> bool:
        text = " ".join(
            [
                (device.name or "").lower(),
                (device.device_name or "").lower(),
                (device.status_text or "").lower(),
                (device.measurements_text or "").lower(),
            ]
        )
        markers = (
            "gate",
            "garage",
            "door",
            "doorcontact",
            "closed_opened",
            "door_time",
        )
        return any(marker in text for marker in markers)

    def _build_tuya_gate_status_panel(self, status_text: str, last_change: str) -> QWidget:
        container = QWidget()
        container.setObjectName("TuyaMetricPanel")
        grid = QGridLayout(container)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(8)

        state_text, is_open = self._parse_gate_state(status_text)
        card = QFrame()
        card.setObjectName("TuyaMetricCard")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(10, 8, 10, 8)
        card_layout.setSpacing(4)

        label_widget = QLabel(tr("Gate status"))
        label_widget.setObjectName("TuyaMetricLabel")
        value_widget = QLabel(state_text)
        value_widget.setObjectName("TuyaGateValue")
        if is_open is True:
            value_widget.setProperty("state", "open")
        elif is_open is False:
            value_widget.setProperty("state", "closed")
        else:
            value_widget.setProperty("state", "unknown")
        value_widget.style().unpolish(value_widget)
        value_widget.style().polish(value_widget)

        card_layout.addWidget(label_widget)
        card_layout.addWidget(value_widget)
        last_change_widget = QLabel(f"{tr('Last change')}: {last_change if last_change else '—'}")
        last_change_widget.setObjectName("TuyaMetricLabel")
        card_layout.addWidget(last_change_widget)
        grid.addWidget(card, 0, 0)
        return container

    def _parse_gate_state(self, status_text: str) -> tuple[str, bool | None]:
        lower = (status_text or "").lower()
        open_markers = (
            "opened",
            "open",
            "closed_opened=open",
            "doorcontact_state=true",
            "door_status=open",
        )
        closed_markers = (
            "closed",
            "close",
            "closed_opened=close",
            "doorcontact_state=false",
            "door_status=closed",
        )
        if any(marker in lower for marker in open_markers):
            return tr("Opened"), True
        if any(marker in lower for marker in closed_markers):
            return tr("Closed"), False
        return tr("Unknown"), None

    def _sync_tuya_gate_change_metadata(self, devices: list[TuyaDevice]) -> None:
        now_label = datetime.now().strftime("%H:%M:%S")
        current_ids = {device.device_id for device in devices}
        for stale_id in list(self._tuya_gate_state_cache.keys()):
            if stale_id not in current_ids:
                self._tuya_gate_state_cache.pop(stale_id, None)
                self._tuya_gate_last_change_cache.pop(stale_id, None)

        for device in devices:
            if not self._is_tuya_gate_device(device):
                continue
            gate_state, _ = self._parse_gate_state(device.status_text)
            previous = self._tuya_gate_state_cache.get(device.device_id)
            if previous is None:
                self._tuya_gate_state_cache[device.device_id] = gate_state
                self._tuya_gate_last_change_cache.setdefault(device.device_id, now_label)
                continue
            if previous != gate_state:
                self._tuya_gate_state_cache[device.device_id] = gate_state
                self._tuya_gate_last_change_cache[device.device_id] = now_label

    def _build_tuya_icon_cell(self, device_id: str, icon_url: str) -> QWidget:
        container = QWidget()
        layout = QHBoxLayout(container)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(0)
        label = QLabel()
        label.setFixedSize(70, 70)
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        pixmap = self._load_tuya_icon_pixmap(device_id, icon_url)
        if pixmap is not None:
            label.setPixmap(
                pixmap.scaled(
                    60,
                    60,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        else:
            label.setText("•")
            label.setStyleSheet("color: #64748b; font-size: 25px;")
        layout.addWidget(label, alignment=Qt.AlignmentFlag.AlignCenter)
        return container

    def _load_tuya_icon_pixmap(self, device_id: str, icon_url: str) -> QPixmap | None:
        normalized_device_id = device_id.strip()
        normalized = icon_url.strip()
        if not normalized_device_id:
            return None
        if normalized_device_id in self._tuya_icon_device_cache:
            return self._tuya_icon_device_cache[normalized_device_id]
        cached_for_device = self._tuya_icon_cache_file_for_device(normalized_device_id)
        if cached_for_device.exists():
            cached_pixmap = QPixmap(str(cached_for_device))
            if not cached_pixmap.isNull():
                self._tuya_icon_device_cache[normalized_device_id] = cached_pixmap
                return cached_pixmap
            try:
                cached_for_device.unlink()
            except OSError as exc:
                LOGGER.warning("Failed to remove invalid cached Tuya icon for device %s: %s", normalized_device_id, exc)
        if not normalized:
            return None
        if normalized in self._tuya_icon_cache:
            pixmap = self._tuya_icon_cache[normalized]
            self._tuya_icon_device_cache[normalized_device_id] = pixmap
            return pixmap
        cached_path = self._tuya_icon_cache_file(normalized)
        if cached_path is not None and cached_path.exists():
            cached_pixmap = QPixmap(str(cached_path))
            if not cached_pixmap.isNull():
                self._tuya_icon_cache[normalized] = cached_pixmap
                self._tuya_icon_device_cache[normalized_device_id] = cached_pixmap
                return cached_pixmap
            try:
                cached_path.unlink()
            except OSError as exc:
                LOGGER.warning("Failed to remove invalid cached Tuya icon for url %s: %s", normalized, exc)
        if normalized_device_id in self._tuya_icon_failed_device_ids:
            last_attempt = float(self._tuya_icon_failed_last_attempt_epoch.get(normalized_device_id, 0.0))
            if (datetime.now().timestamp() - last_attempt) < 30:
                return None
            self._tuya_icon_failed_device_ids.discard(normalized_device_id)
        self._start_tuya_icon_download(normalized_device_id, normalized)
        return None

    def _start_tuya_icon_download(self, device_id: str, normalized_icon_url: str) -> None:
        download_key = f"{device_id}:{normalized_icon_url}"
        if not normalized_icon_url or download_key in self._tuya_icon_download_inflight:
            return
        self._tuya_icon_download_inflight.add(download_key)

        def looks_like_image_bytes(blob: bytes) -> bool:
            if not blob:
                return False
            signatures = (
                b"\x89PNG\r\n\x1a\n",
                b"\xff\xd8\xff",
                b"RIFF",
                b"GIF87a",
                b"GIF89a",
            )
            return any(blob.startswith(sig) for sig in signatures)

        def worker() -> None:
            data: bytes | None = None
            attempts: list[str] = []
            for candidate_url in self._tuya_icon_candidates(normalized_icon_url):
                try:
                    response = requests.get(
                        candidate_url,
                        timeout=(3.0, 5.0),
                        allow_redirects=True,
                        verify=False,
                        headers={
                            "User-Agent": "EnergyFlow-Studio/1.0",
                            "Accept": "image/*,*/*;q=0.8",
                        },
                    )
                    attempts.append(
                        f"[requests] {candidate_url} -> {response.status_code} "
                        f"content-type={response.headers.get('Content-Type', '-')!s} bytes={len(response.content or b'')}"
                    )
                    if response.ok and response.content:
                        content_type = response.headers.get("Content-Type", "").lower()
                        if "image" in content_type or looks_like_image_bytes(response.content):
                            data = response.content
                            break
                except Exception as exc:
                    attempts.append(f"[requests] {candidate_url} -> error {exc.__class__.__name__}: {exc}")
                    continue
            if data is None:
                for candidate_url in self._tuya_icon_candidates(normalized_icon_url):
                    try:
                        # Fallback for environments where requests/cert chain can fail in packaged mode.
                        from urllib.request import Request, urlopen

                        request = Request(
                            candidate_url,
                            headers={
                                "User-Agent": "EnergyFlow-Studio/1.0",
                                "Accept": "image/*,*/*;q=0.8",
                            },
                        )
                        with urlopen(request, timeout=5, context=ssl._create_unverified_context()) as raw_response:
                            blob = raw_response.read()
                        attempts.append(
                            f"[urllib] {candidate_url} -> content-type={raw_response.headers.get('Content-Type', '-')!s} bytes={len(blob)}"
                        )
                        if blob and looks_like_image_bytes(blob):
                            data = blob
                            break
                    except Exception as exc:
                        attempts.append(f"[urllib] {candidate_url} -> error {exc.__class__.__name__}: {exc}")
                        continue
            payload = {"data": data, "attempts": attempts}
            self.tuya_icon_download_finished.emit(device_id, normalized_icon_url, payload)

        threading.Thread(target=worker, daemon=True, name=f"tuya-icon-{hash(download_key)}").start()

    def _finish_tuya_icon_download(self, device_id: str, normalized_icon_url: str, payload: object) -> None:
        download_key = f"{device_id}:{normalized_icon_url}"
        self._tuya_icon_download_inflight.discard(download_key)
        data: bytes | None = None
        attempts: list[str] = []
        if isinstance(payload, dict):
            raw_data = payload.get("data")
            if isinstance(raw_data, (bytes, bytearray)):
                data = bytes(raw_data)
            raw_attempts = payload.get("attempts")
            if isinstance(raw_attempts, list):
                attempts = [str(item) for item in raw_attempts]
        elif isinstance(payload, (bytes, bytearray)):
            data = bytes(payload)
        if not data:
            self._tuya_icon_failed_device_ids.add(device_id)
            self._tuya_icon_failed_last_attempt_epoch[device_id] = datetime.now().timestamp()
            return
        pixmap = QPixmap()
        loaded_ok = pixmap.loadFromData(data)
        if not loaded_ok:
            self._tuya_icon_failed_device_ids.add(device_id)
            self._tuya_icon_failed_last_attempt_epoch[device_id] = datetime.now().timestamp()
            return
        self._tuya_icon_cache[normalized_icon_url] = pixmap
        self._tuya_icon_device_cache[device_id] = pixmap
        self._tuya_icon_failed_device_ids.discard(device_id)
        self._tuya_icon_failed_last_attempt_epoch.pop(device_id, None)
        cached_path = self._tuya_icon_cache_file(normalized_icon_url)
        if cached_path is not None:
            try:
                cached_path.write_bytes(data)
            except OSError as exc:
                LOGGER.warning("Failed to write shared Tuya icon cache file %s: %s", cached_path, exc)
        device_path = self._tuya_icon_cache_file_for_device(device_id)
        try:
            device_path.write_bytes(data)
        except OSError as exc:
            LOGGER.warning("Failed to write device Tuya icon cache file %s: %s", device_path, exc)
        # Re-render only if Tuya tab currently has device cards.
        if self._tuya_devices:
            self._render_tuya_devices(self._tuya_devices)
            self._sync_energyflow_tuya_icons()

    def _resolve_tuya_icon_cache_dir(self) -> Path:
        base_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation).strip()
        if base_dir:
            cache_dir = Path(base_dir) / "tuya-icons"
        else:
            cache_dir = Path("/tmp/energyflow-studio-cache/tuya-icons")
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            LOGGER.warning("Failed to create primary Tuya cache directory %s: %s", cache_dir, exc)
            fallback_dir = Path("/tmp/energyflow-studio-cache/tuya-icons")
            try:
                fallback_dir.mkdir(parents=True, exist_ok=True)
            except OSError as fallback_exc:
                LOGGER.warning("Failed to create fallback Tuya cache directory %s: %s", fallback_dir, fallback_exc)
            return fallback_dir
        return cache_dir

    def _tuya_icon_cache_file(self, normalized_icon_url: str) -> Path | None:
        if not normalized_icon_url:
            return None
        digest = hashlib.sha256(normalized_icon_url.encode("utf-8")).hexdigest()
        return self._tuya_icon_cache_dir / f"{digest}.img"

    def _tuya_icon_cache_file_for_device(self, device_id: str) -> Path:
        normalized = "".join(ch for ch in device_id if ch.isalnum() or ch in ("-", "_")).strip() or "unknown"
        return self._tuya_icon_cache_dir / f"device-{normalized}.img"

    def _tuya_icon_candidates(self, raw_icon_url: str) -> list[str]:
        value = raw_icon_url.strip()
        if not value:
            return []
        if value.startswith("http://") or value.startswith("https://"):
            return [value]

        path = value.lstrip("/")
        endpoint = self._tuya_settings.normalized_endpoint().strip()
        candidates: list[str] = []
        # Prefer Tuya image CDNs first. OpenAPI often returns JSON wrappers, not raw image bytes.
        candidates.append(f"https://images.tuyaeu.com/{path}")
        candidates.append(f"https://images.tuyaus.com/{path}")
        candidates.append(f"https://images.tuyacn.com/{path}")
        candidates.append(f"https://images.tuyain.com/{path}")
        candidates.append(f"https://images.smartlife.com/{path}")
        candidates.append(f"https://images.tuya.com/{path}")
        if endpoint:
            parsed = urlparse(endpoint)
            host = (parsed.hostname or "").lower()
            if host:
                candidates.append(f"https://{host}/{path}")
                host_parts = host.split(".")
                if len(host_parts) >= 2:
                    suffix = ".".join(host_parts[-2:])
                    candidates.append(f"https://images.{suffix}/{path}")
                if "tuyaeu" in host:
                    candidates.append(f"https://images.tuyaeu.com/{path}")
                if "tuyaus" in host:
                    candidates.append(f"https://images.tuyaus.com/{path}")
                if "tuyacn" in host:
                    candidates.append(f"https://images.tuyacn.com/{path}")
                if "tuyain" in host:
                    candidates.append(f"https://images.tuyain.com/{path}")

        unique: list[str] = []
        seen: set[str] = set()
        for item in candidates:
            if item in seen:
                continue
            seen.add(item)
            unique.append(item)
        return unique

    def _request_tuya_power_change(self, device_id: str, turn_on: bool) -> None:
        normalized_device_id = device_id.strip()
        if not normalized_device_id or normalized_device_id in self._tuya_action_inflight_ids:
            return
        self._tuya_action_inflight_ids.add(normalized_device_id)
        self._set_tuya_action_loading(device_id=normalized_device_id, loading=True)
        self._set_tab_message(
            self.tuya_status_label,
            f"Sending {'ON' if turn_on else 'OFF'} command to {normalized_device_id}...",
            error=False,
        )

        def worker() -> None:
            try:
                updated_device = set_device_power(
                    client_id=self._tuya_settings.client_id,
                    client_secret=self._tuya_settings.client_secret,
                    endpoint=self._tuya_settings.normalized_endpoint(),
                    device_id=normalized_device_id,
                    turn_on=turn_on,
                )
                result = {"ok": True, "device": updated_device, "target": turn_on}
            except Exception as exc:
                result = {"ok": False, "error": str(exc), "target": turn_on}
            self.tuya_power_change_finished.emit(normalized_device_id, result)

        threading.Thread(target=worker, daemon=True).start()

    def _finish_tuya_power_change(self, device_id: str, result: dict[str, object]) -> None:
        self._tuya_action_inflight_ids.discard(device_id)
        if bool(result.get("ok")):
            target_on = bool(result.get("target"))
            updated_device = result.get("device")
            if updated_device is not None:
                self._apply_single_tuya_device_update(updated_device)
            self._set_tuya_action_loading(device_id=device_id, loading=False)
            self._set_tab_message(
                self.tuya_status_label,
                f"Command confirmed: {device_id} is now {'On' if target_on else 'Off'}.",
                error=False,
            )
            return

        error_text = str(result.get("error", "Unknown Tuya error"))
        self._set_tuya_action_loading(device_id=device_id, loading=False)
        self._set_tab_message(
            self.tuya_status_label,
            f"Tuya command error: {self._friendly_tuya_error(error_text)}",
            error=True,
        )

    def _apply_single_tuya_device_update(self, device) -> None:
        replaced = False
        for idx, current in enumerate(self._tuya_devices):
            if current.device_id != device.device_id:
                continue
            self._tuya_devices[idx] = device
            replaced = True
            break
        if not replaced:
            self._tuya_devices.append(device)
        self._render_tuya_devices(self._tuya_devices)

    def _set_tuya_action_loading(self, *, device_id: str, loading: bool) -> None:
        tile_controls = self._tuya_tile_action_controls.get(device_id)
        if tile_controls is not None:
            tile_button, tile_card = tile_controls
            for device in self._tuya_devices:
                if device.device_id == device_id:
                    tile_button.setStyleSheet(self._tuya_power_button_style(device.power_on, loading=False, tile=True))
                    break
            tile_button.setEnabled(not loading)
            tile_card.set_loading(loading)

    def _is_tuya_power_on_from_status(self, status_text: str) -> bool | None:
        lower = status_text.lower()
        if "switch_1=true" in lower or "switch=true" in lower:
            return True
        if "switch_1=false" in lower or "switch=false" in lower:
            return False
        return None

    def _tuya_power_button_style(self, power_on: bool | None, *, loading: bool, tile: bool = False) -> str:
        if power_on is True:
            base = "#16a34a"
            border = "#22c55e"
        else:
            base = "#334155"
            border = "#475569"
        radius = "16px" if tile else "16px"
        pad = "0" if tile else "6px 14px"
        font_size = "15px" if tile else "22px"
        return (
            "QPushButton {"
            f"background: {base};"
            f"border: 1px solid {border};"
            f"border-radius: {radius};"
            "color: #e2e8f0;"
            "font-weight: 700;"
            f"font-size: {font_size};"
            f"padding: {pad};"
            "}"
            "QPushButton:disabled {"
            "color: #e2e8f0;"
            "}"
        )

    def _tuya_settings_from_profile(self, profile: DeviceProfile | None) -> TuyaSettings:
        if profile is None:
            return TuyaSettings()
        return TuyaSettings(
            enabled=bool(profile.tuya_enabled),
            client_id=profile.tuya_client_id,
            client_secret=profile.tuya_client_secret,
            endpoint=profile.resolved_tuya_endpoint(),
            poll_interval_sec=profile.resolved_tuya_poll_interval_sec(),
        )

    def _build_layout(self) -> None:
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        self._popup_block_target = main_widget

        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("MainTabs")
        self.tabs.setDocumentMode(True)
        self.tabs.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.tabs.tabBar().setExpanding(False)
        self.tabs.tabBar().setObjectName("MainTabBar")
        self.tabs.tabBar().setFixedHeight(94)
        self.tabs.tabBar().setDrawBase(False)
        self.tabs.tabBar().setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.tabs.tabBar().setCursor(Qt.CursorShape.PointingHandCursor)
        self.tabs_corner.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.tabs.setCornerWidget(self.tabs_corner, Qt.Corner.TopRightCorner)
        self.energyflow_tab = QWidget()
        energyflow_tab = self.energyflow_tab
        energyflow_layout = QVBoxLayout(energyflow_tab)
        energyflow_layout.setContentsMargins(12, 12, 12, 12)
        energyflow_layout.setSpacing(12)

        energyflow_section = QWidget()
        energyflow_section.setObjectName("ChartSection")
        energyflow_section_layout = QVBoxLayout(energyflow_section)
        energyflow_section_layout.setContentsMargins(24, 24, 24, 24)
        energyflow_section_layout.setSpacing(12)

        energyflow_header = QHBoxLayout()
        energyflow_title = QLabel("EnergyFlow")
        energyflow_title.setObjectName("ChartSectionTitle")
        energyflow_header.addWidget(energyflow_title)
        energyflow_header.addStretch()

        energyflow_actions = QVBoxLayout()
        energyflow_actions.setContentsMargins(0, 0, 0, 0)
        energyflow_actions.setSpacing(0)

        self.energyflow_refresh_button = QPushButton("Refresh")
        self.energyflow_refresh_button.setObjectName("EnergyFlowRefreshButton")
        self.energyflow_refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.energyflow_refresh_button.clicked.connect(self._handle_energyflow_refresh_click)
        energyflow_actions.addWidget(self.energyflow_refresh_button, alignment=Qt.AlignmentFlag.AlignRight)
        energyflow_header.addLayout(energyflow_actions)

        energyflow_description = QLabel("Waiting for a live snapshot.")
        self.energyflow_summary_label = energyflow_description
        energyflow_description.setWordWrap(True)
        energyflow_description.setVisible(False)

        self.energyflow_meta_label = QLabel("Choose an active device profile to start.")
        self.energyflow_meta_label.setObjectName("SidebarMeta")
        self.energyflow_meta_label.setWordWrap(True)
        self.energyflow_meta_label.setVisible(False)

        self.energyflow_view = EnergyFlowWidget()
        self.energyflow_view.inverter_clicked.connect(self._open_inverter_analysis_dialog)
        self.energyflow_view.pv_clicked.connect(self._open_pv_history_dialog)
        self.energyflow_view.grid_clicked.connect(self._open_grid_history_dialog)
        self.energyflow_view.battery_clicked.connect(self._open_battery_history_dialog)
        self.energyflow_view.home_clicked.connect(self._open_home_history_dialog)
        self.energyflow_view.weather_metric_clicked.connect(self._open_weather_metric_history_dialog)

        energyflow_section_layout.addLayout(energyflow_header)
        energyflow_section_layout.addWidget(energyflow_description)
        energyflow_section_layout.addWidget(self.energyflow_meta_label)
        energyflow_section_layout.addWidget(self.energyflow_view, stretch=1)
        energyflow_layout.addWidget(energyflow_section, stretch=1)

        self.saved_data_section = SavedDataSection()
        self.saved_data_section.open_import_requested.connect(self.open_saved_import)
        self.saved_data_section.update_requested.connect(self._handle_saved_data_update_requested)
        self.saved_data_section.import_excel_requested.connect(self.open_file_dialog)
        self.saved_data_section.export_excel_requested.connect(self.export_saved_import_to_excel)
        self.saved_data_section.clear_database_requested.connect(self.clear_saved_database)

        self.inverter_settings_tab = InverterSettingsTab()
        self.inverter_settings_tab.refresh_requested.connect(self._handle_inverter_settings_refresh_click)
        self.inverter_settings_tab.edit_requested.connect(self._handle_inverter_setting_edit_requested)
        self.inverter_settings_tab.setting_cached.connect(self._handle_inverter_setting_cached)

        self.forecast_tab = ForecastTab()
        self.forecast_tab.refresh_requested.connect(self._handle_forecast_refresh_click)

        self.tuya_tab = QWidget()
        tuya_layout = QVBoxLayout(self.tuya_tab)
        tuya_layout.setContentsMargins(12, 12, 12, 12)
        tuya_layout.setSpacing(12)

        tuya_section = QWidget()
        tuya_section.setObjectName("ChartSection")
        tuya_section_layout = QVBoxLayout(tuya_section)
        tuya_section_layout.setContentsMargins(24, 24, 24, 24)
        tuya_section_layout.setSpacing(12)

        tuya_header = QHBoxLayout()
        tuya_title = QLabel("Tuya Devices")
        tuya_title.setObjectName("ChartSectionTitle")
        tuya_header.addWidget(tuya_title)
        tuya_header.addStretch(1)
        self.tuya_refresh_button = QPushButton("Refresh")
        self.tuya_refresh_button.setObjectName("EnergyFlowRefreshButton")
        self.tuya_refresh_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.tuya_refresh_button.clicked.connect(self._refresh_tuya_devices)
        tuya_header.addWidget(self.tuya_refresh_button, alignment=Qt.AlignmentFlag.AlignRight)

        self.tuya_status_label = QLabel(tr("Enable Tuya in Settings to load devices."))
        self.tuya_status_label.setObjectName("SidebarMeta")
        self.tuya_status_label.setWordWrap(True)
        self.tuya_status_wrap = QWidget()
        self.tuya_status_wrap_layout = QVBoxLayout(self.tuya_status_wrap)
        self.tuya_status_wrap_layout.setContentsMargins(0, 20, 0, 20)
        self.tuya_status_wrap_layout.setSpacing(0)
        self.tuya_status_wrap_layout.addWidget(self.tuya_status_label)

        tuya_filters_row = QHBoxLayout()
        tuya_filters_row.setContentsMargins(0, 0, 0, 0)
        tuya_filters_row.setSpacing(8)
        self._tuya_filter_buttons = {}
        for key, label in (
            ("all", tr("All")),
            ("online", tr("Online")),
            ("offline", tr("Offline")),
            ("gates", tr("Gates")),
        ):
            button = QPushButton(label)
            button.setObjectName("TuyaFilterButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda checked=False, m=key: self._set_tuya_filter_mode(m))
            self._tuya_filter_buttons[key] = button
            tuya_filters_row.addWidget(button)
        tuya_filters_row.addStretch(1)

        self.tuya_tile_scroll = QScrollArea()
        self.tuya_tile_scroll.setWidgetResizable(True)
        self.tuya_tile_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.tuya_tile_scroll.setObjectName("TuyaTilesScroll")
        self.tuya_tile_container = QWidget()
        self.tuya_tile_container.setObjectName("TuyaTileContainer")
        self.tuya_tile_grid = QGridLayout(self.tuya_tile_container)
        self.tuya_tile_grid.setContentsMargins(4, 4, 4, 4)
        self.tuya_tile_grid.setSpacing(14)
        self.tuya_tile_scroll.setWidget(self.tuya_tile_container)
        self.tuya_tile_scroll.viewport().setStyleSheet("background: transparent;")

        tuya_section_layout.addLayout(tuya_header)
        tuya_section_layout.addWidget(self.tuya_status_wrap)
        tuya_section_layout.addLayout(tuya_filters_row)
        tuya_section_layout.addWidget(self.tuya_tile_scroll, stretch=1)
        tuya_layout.addWidget(tuya_section, stretch=1)

        self._tab_badges: list[MainTabBadge] = []
        self.tabs.addTab(energyflow_tab, "")
        self.tabs.addTab(self.forecast_tab, "")
        self.tabs.addTab(self.inverter_settings_tab, "")
        self.tabs.addTab(self.tuya_tab, "")
        self.tabs.addTab(self.saved_data_section, "")
        energyflow_badge = self._build_tab_badge("energyflow", "EnergyFlow")
        forecast_badge = self._build_tab_badge("charts", "Forecast")
        settings_badge = self._build_tab_badge("settings", "Inverter")
        tuya_badge = self._build_tab_badge("tuya", "Tuya")
        saved_badge = self._build_tab_badge("saved", "Data")
        self._tab_badges.extend([energyflow_badge, forecast_badge, settings_badge, tuya_badge, saved_badge])
        self.tabs.tabBar().setTabButton(0, self.tabs.tabBar().ButtonPosition.LeftSide, energyflow_badge)
        self.tabs.tabBar().setTabButton(0, self.tabs.tabBar().ButtonPosition.RightSide, QWidget())
        self.tabs.tabBar().setTabButton(1, self.tabs.tabBar().ButtonPosition.LeftSide, forecast_badge)
        self.tabs.tabBar().setTabButton(1, self.tabs.tabBar().ButtonPosition.RightSide, QWidget())
        self.tabs.tabBar().setTabButton(2, self.tabs.tabBar().ButtonPosition.LeftSide, settings_badge)
        self.tabs.tabBar().setTabButton(2, self.tabs.tabBar().ButtonPosition.RightSide, QWidget())
        self.tabs.tabBar().setTabButton(3, self.tabs.tabBar().ButtonPosition.LeftSide, tuya_badge)
        self.tabs.tabBar().setTabButton(3, self.tabs.tabBar().ButtonPosition.RightSide, QWidget())
        self.tabs.tabBar().setTabButton(4, self.tabs.tabBar().ButtonPosition.LeftSide, saved_badge)
        self.tabs.tabBar().setTabButton(4, self.tabs.tabBar().ButtonPosition.RightSide, QWidget())
        self.tabs.setTabToolTip(0, "")
        self.tabs.setTabToolTip(1, "")
        self.tabs.setTabToolTip(2, "")
        self.tabs.setTabToolTip(3, "")
        self.tabs.setTabToolTip(4, "")
        self._tuya_tab_index = 3
        self._apply_tuya_settings()
        self.tabs.currentChanged.connect(self._handle_tab_changed)
        self._refresh_tab_badges()
        right_layout.addWidget(self.tabs)

        root_layout = QVBoxLayout(main_widget)
        root_layout.setContentsMargins(12, 12, 12, 12)
        root_layout.addWidget(right_panel)

        self._popup_backdrop = _InputBlockerOverlay(self)
        self._popup_backdrop.setObjectName("PopupBackdrop")
        self._popup_backdrop.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._popup_backdrop.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        self._popup_backdrop.hide()
        self._popup_backdrop.setGeometry(self.rect())
        self._embedded_history_overlay = _InputBlockerOverlay(self)
        self._embedded_history_overlay.setObjectName("EmbeddedHistoryOverlay")
        self._embedded_history_overlay.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._embedded_history_overlay.hide()
        self._embedded_history_overlay.setGeometry(self.rect())

    def _apply_styles(self) -> None:
        # Resolve icon paths both in development and inside PyInstaller bundles.
        source_assets_root = Path(__file__).resolve().parents[1] / "assets" / "icons"
        bundled_base = getattr(sys, "_MEIPASS", None)
        bundled_assets_root = (
            Path(bundled_base) / "app" / "assets" / "icons" if bundled_base else None
        )
        assets_root = (
            bundled_assets_root
            if bundled_assets_root is not None and bundled_assets_root.exists()
            else source_assets_root
        )
        branch_closed_icon = (assets_root / "tree_branch_closed.png").as_uri()
        branch_open_icon = (assets_root / "tree_branch_open.png").as_uri()
        style_qss = """
            QMainWindow {
                background: #0f172a;
            }
            #PopupBackdrop {
                background: rgba(130, 136, 145, 0.50);
                border-radius: 18px;
            }
            #EmbeddedHistoryOverlay {
                background: transparent;
                border-radius: 18px;
            }
            QWidget {
                color: #e2e8f0;
                font-size: 13px;
            }
            #ChartSection {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #101a30, stop:1 #0b1220);
                border: 1px solid #22304a;
                border-radius: 18px;
            }
            #ChartSectionTitle {
                font-size: 20px;
                font-weight: 700;
                color: #f8fafc;
            }
            #SavedDataFrame {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #101a30, stop:1 #0b1220);
                border: 1px solid #22304a;
                border-radius: 18px;
            }
            #ChartToolbar {
                background: rgba(8, 15, 30, 0.78);
                border: 1px solid #243244;
                border-radius: 14px;
            }
            #ChartToolbarSeparator {
                color: #243244;
                background: #243244;
                min-width: 1px;
                max-width: 1px;
                margin: 4px 2px;
            }
            #InverterSettingsSummary {
                background: rgba(8, 15, 30, 0.78);
                border: 1px solid #243244;
                border-radius: 14px;
            }
            #InverterSettingsSummaryText {
                color: #dbeafe;
                font-size: 15px;
                line-height: 1.35em;
            }
            #InverterSettingsSearch {
                min-height: 38px;
                padding: 0 12px;
                border-radius: 12px;
                border: 1px solid #314764;
                background: rgba(8, 15, 30, 0.78);
                color: #e2e8f0;
                selection-background-color: #1d4ed8;
            }
            #InverterSettingsSearch:focus {
                border-color: #38bdf8;
            }
            #InverterSettingsTree {
                background: rgba(8, 15, 30, 0.78);
                border: 1px solid #243244;
                border-radius: 14px;
                alternate-background-color: rgba(15, 23, 42, 0.84);
                color: #dbeafe;
                selection-background-color: rgba(56, 189, 248, 0.16);
                selection-color: #f0f9ff;
                padding: 8px;
            }
            #InverterSettingsTree::item {
                height: 28px;
            }
            #InverterSettingsTree::item:selected {
                background: rgba(14, 165, 233, 0.14);
                color: #f0f9ff;
            }
            #InverterSettingsTree::item:selected:active {
                background: rgba(14, 165, 233, 0.20);
                color: #f0f9ff;
            }
            #InverterSettingsTree::item:selected:!active {
                background: rgba(56, 189, 248, 0.12);
                color: #dbeafe;
            }
            #InverterSettingsTree::branch:selected,
            #InverterSettingsTree::branch:selected:active {
                background: rgba(56, 189, 248, 0.16);
            }
            #InverterSettingsTree::branch:selected:!active {
                background: rgba(56, 189, 248, 0.12);
            }
            #CategoryHeaderCell {
                background: transparent;
            }
            #CategoryHeaderLabel {
                color: #e2edff;
                font-size: 14px;
                font-weight: 700;
                background: transparent;
            }
            #CategoryHeaderBadge {
                color: #d9fbff;
                font-size: 11px;
                font-weight: 700;
                background: rgba(14, 165, 233, 0.16);
                border: 1px solid rgba(34, 211, 238, 0.65);
                border-radius: 9px;
                padding: 1px 7px;
            }
            #InverterSettingsTree::branch:has-children:closed:has-siblings,
            #InverterSettingsTree::branch:closed:has-children:!has-siblings {
                image: url("__TREE_BRANCH_CLOSED_ICON__");
            }
            #InverterSettingsTree::branch:has-children:open:has-siblings,
            #InverterSettingsTree::branch:open:has-children:!has-siblings {
                image: url("__TREE_BRANCH_OPEN_ICON__");
            }
            #InverterSettingsTree::branch:has-children:closed:has-siblings,
            #InverterSettingsTree::branch:closed:has-children:!has-siblings,
            #InverterSettingsTree::branch:has-children:open:has-siblings,
            #InverterSettingsTree::branch:open:has-children:!has-siblings {
                margin-left: 3px;
            }
            #InverterSettingsTree::branch:has-siblings:!adjoins-item {
                border-image: none;
                border-left: none;
            }
            #InverterSettingsTree::branch:has-siblings:adjoins-item {
                border-image: none;
                border-left: 1px solid rgba(56, 189, 248, 0.28);
                border-bottom: 1px solid rgba(56, 189, 248, 0.28);
            }
            #InverterSettingsTree::branch:!has-siblings:adjoins-item {
                border-image: none;
                border-left: 1px solid rgba(56, 189, 248, 0.28);
                border-bottom: 1px solid rgba(56, 189, 248, 0.28);
            }
            QScrollArea#TuyaTilesScroll {
                background: transparent;
                border: none;
            }
            QScrollArea#TuyaTilesScroll QWidget#qt_scrollarea_viewport,
            #TuyaTileContainer {
                background: transparent;
                border: none;
            }
            QFrame#TuyaTileCard {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 rgba(9, 18, 37, 0.96),
                    stop:1 rgba(5, 13, 29, 0.96));
                border: 1px solid #224163;
                border-radius: 18px;
            }
            #TuyaTileName {
                font-size: 18px;
                font-weight: 700;
                color: #ecfeff;
                background: transparent;
            }
            #TuyaTileMeta {
                color: #8bb5cc;
                font-size: 12px;
                line-height: 1.35em;
                background: transparent;
            }
            #TuyaMetricPanel {
                background: transparent;
                border: none;
            }
            #TuyaMetricCard {
                background: rgba(2, 18, 45, 0.92);
                border: 1px solid #224163;
                border-radius: 14px;
            }
            #TuyaMetricLabel {
                color: #8fd9ff;
                font-size: 11px;
                font-weight: 700;
                background: transparent;
            }
            #TuyaMetricValue {
                color: #d9fbff;
                font-size: 20px;
                font-weight: 700;
                background: transparent;
            }
            #TuyaMetricValue[muted="true"] {
                color: #7aa0b8;
                font-size: 17px;
                font-weight: 600;
            }
            #TuyaGateValue {
                font-size: 24px;
                font-weight: 800;
                background: transparent;
            }
            #TuyaGateValue[state="open"] {
                color: #22c55e;
            }
            #TuyaGateValue[state="closed"] {
                color: #ef4444;
            }
            #TuyaGateValue[state="unknown"] {
                color: #94a3b8;
            }
            #TuyaTileOnline {
                font-size: 13px;
                font-weight: 700;
                background: transparent;
            }
            #TuyaTileLoader {
                color: #67e8f9;
                font-size: 12px;
                background: transparent;
            }
            #EnergyFlowStatus {
                color: #94a3b8;
                background: rgba(8, 15, 30, 0.72);
                border: 1px solid #243244;
                border-radius: 14px;
                padding: 12px 14px;
                font-size: 13px;
            }
            #EnergyFlowCard,
            #EnergyFlowSummary {
                background: rgba(8, 15, 30, 0.78);
                border: 1px solid #243244;
                border-radius: 16px;
            }
            #EnergyFlowCard[accent="pv"] {
                border-color: #1d95be;
            }
            #EnergyFlowCard[accent="grid"] {
                border-color: #42556f;
            }
            #EnergyFlowCard[accent="battery"] {
                border-color: #a58614;
            }
            #EnergyFlowCard[accent="load"] {
                border-color: #2f84b8;
            }
            #EnergyFlowCard[accent="device"] {
                border-color: #2d8da8;
            }
            #EnergyFlowCardTitle,
            #EnergyFlowSummaryTitle {
                color: #f8fafc;
                font-size: 15px;
                font-weight: 700;
                background: transparent;
                border: none;
            }
            #EnergyFlowCardValue {
                color: #e2e8f0;
                font-size: 28px;
                font-weight: 700;
                background: transparent;
                border: none;
            }
            #EnergyFlowCardMeta,
            #EnergyFlowSummaryText {
                color: #8da2bc;
                font-size: 12px;
                background: transparent;
                border: none;
            }
            #ChartSidebar {
                background: rgba(12, 20, 36, 0.92);
                border: 1px solid #22304a;
                border-radius: 16px;
            }
            #SidebarHelper {
                color: #f8fafc;
                background: transparent;
                border: none;
                padding: 0;
                font-size: 13px;
                font-weight: 600;
            }
            #SidebarMeta {
                color: #7f93ad;
                font-size: 11px;
                padding: 0 0 4px 0;
            }
            #SidebarTree {
                background: transparent;
                border: none;
                border-radius: 14px;
                padding: 0;
                outline: 0;
            }
            #SidebarTree::item {
                min-height: 24px;
                padding: 1px 4px;
                border-radius: 8px;
            }
            #SidebarTree::item:selected {
                background: rgba(56, 189, 248, 0.10);
                color: #f8fafc;
            }
            QFrame#DeviceProfileButton {
                min-height: 88px;
                max-height: 88px;
                min-width: 300px;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #0f2740, stop:0.24 #143758, stop:1 #11253b);
                border: 1px solid #31597b;
                border-radius: 14px;
            }
            #DeviceProfileIcon,
            #DeviceProfileEyebrow,
            #DeviceProfileTitle,
            #DeviceProfileLink {
                background: transparent;
                border: none;
                color: #e2ecf8;
            }
            #DeviceProfileEyebrow {
                color: #8bdcff;
                font-size: 10px;
                font-weight: 700;
                letter-spacing: 0.12em;
                text-transform: uppercase;
                padding-bottom: 2px;
            }
            #DeviceProfileTitle {
                font-size: 18px;
                font-weight: 700;
                padding-left: 0px;
                padding-bottom: 4px;
                margin-left: 0px;
            }
            #TabsCorner {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 #0b1220,
                    stop:0.45 #102033,
                    stop:1 #0b1220);
                border-top-left-radius: 16px;
                border-top-right-radius: 16px;
            }
            QTabWidget#MainTabs {
                background: #0b1220;
                border: none;
                border-radius: 16px;
            }
            QTabWidget#MainTabs::pane {
                border: none;
                border-top: none;
                border-bottom-left-radius: 16px;
                border-bottom-right-radius: 16px;
                background: #0f172a;
                margin-top: 20px;
                top: 0px;
            }
            QTabWidget#MainTabs QWidget#qt_tabwidget_stackedwidget {
                background: #0f172a;
                border-bottom-left-radius: 16px;
                border-bottom-right-radius: 16px;
            }
            QTabBar#MainTabBar {
                background: #0b1220;
                border: none;
            }
            QTabBar#MainTabBar::tab {
                background: transparent;
                color: #9fb0c5;
                padding: 0px;
                margin: 1px 8px 1px 0px;
                border: none;
                border-bottom: 2px solid transparent;
                border-radius: 18px;
                min-width: 104px;
                max-width: 104px;
                min-height: 88px;
                max-height: 88px;
                font-weight: 600;
                font-size: 15px;
            }
            QTabBar#MainTabBar::tab:hover {
                color: #e2e8f0;
                background: rgba(56, 189, 248, 0.08);
            }
            QTabBar#MainTabBar::tab:selected {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(8, 72, 61, 0.95), stop:1 rgba(10, 45, 60, 0.95));
                border: 1px solid rgba(34, 197, 94, 0.28);
                border-bottom: 2px solid #22c55e;
                color: #eff6ff;
            }
            QGroupBox {
                border: 1px solid #334155;
                margin-top: 10px;
                border-radius: 8px;
                padding-top: 12px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 4px;
            }
            QScrollArea, QTextEdit, QComboBox, QDateEdit, QTreeWidget {
                background: #111827;
                border: 1px solid #334155;
                border-radius: 8px;
            }
            QComboBox { combobox-popup: 0; }
            QComboBox QAbstractItemView {
                background: #111827;
                color: #e2e8f0;
                border: 1px solid #334155;
                selection-background-color: #0ea5e9;
                selection-color: #eff6ff;
                outline: 0;
            }
            QComboBox#SavedDataPageSizeCombo,
            QComboBox#SavedDataDatasetCombo {
                min-height: 40px;
                padding: 0 36px 0 14px;
                font-size: 13px;
                color: #e2e8f0;
                background: rgba(8, 15, 30, 0.78);
                border: 1px solid #243244;
                border-radius: 14px;
            }
            QComboBox#SavedDataPageSizeCombo::drop-down,
            QComboBox#SavedDataDatasetCombo::drop-down {
                width: 34px;
                border: none;
                background: transparent;
            }
            QComboBox#SavedDataPageSizeCombo::down-arrow,
            QComboBox#SavedDataDatasetCombo::down-arrow {
                image: none;
                width: 10px;
                height: 10px;
                border-right: 2px solid #e2e8f0;
                border-bottom: 2px solid #e2e8f0;
                margin-right: 10px;
                transform: rotate(45deg);
            }
            QComboBox#SavedDataDatasetCombo QAbstractItemView {
                padding: 6px;
                border-radius: 14px;
            }
            QComboBox#DashboardFilterField,
            #DateFieldShell {
                min-height: 40px;
                padding: 0 36px 0 14px;
                font-size: 13px;
                color: #e2e8f0;
                background: rgba(8, 15, 30, 0.78);
                border: 1px solid #243244;
                border-radius: 14px;
            }
            #DateFieldShell {
                padding: 0;
            }
            QComboBox#DashboardFilterField:hover,
            #DateFieldShell:hover {
                border-color: #35506d;
                background: rgba(10, 19, 36, 0.86);
            }
            #DateFieldEditor {
                background: transparent;
                color: #f8fafc;
                border: none;
                padding: 0 0 0 14px;
                min-height: 38px;
                font-size: 13px;
            }
            #DateFieldEditor::drop-down {
                width: 0px;
                border: none;
                background: transparent;
            }
            #DateFieldEditor::down-arrow {
                image: none;
                width: 0px;
                height: 0px;
            }
            #DateFieldTrigger {
                min-width: 36px;
                max-width: 36px;
                border: none;
                background: transparent;
                color: #dbeafe;
                font-size: 14px;
                padding: 0 10px 0 0;
            }
            #DateFieldTrigger:hover {
                color: #f8fafc;
            }
            QComboBox#DashboardFilterField::drop-down,
            QDateEdit#DashboardFilterField::drop-down {
                width: 34px;
                border: none;
                background: transparent;
            }
            QComboBox#DashboardFilterField::down-arrow {
                image: none;
                width: 10px;
                height: 10px;
                border-right: 2px solid #e2e8f0;
                border-bottom: 2px solid #e2e8f0;
                margin-right: 10px;
                transform: rotate(45deg);
            }
            QProgressBar {
                background: #111827;
                border: 1px solid #334155;
                border-radius: 8px;
                text-align: center;
                min-height: 24px;
            }
            QProgressBar::chunk {
                background: #0ea5e9;
                border-radius: 7px;
            }
            QStatusBar#MainStatusBar {
                background: #0b1220;
                color: #a9bbd1;
                border-top: 1px solid #243244;
            }
            QStatusBar#MainStatusBar::item {
                border: none;
            }
            QTableWidget {
                background: #111827;
                border: 1px solid #334155;
                border-radius: 10px;
                gridline-color: #1f2937;
                alternate-background-color: #0f172a;
                selection-background-color: rgba(56, 189, 248, 0.16);
                selection-color: #f8fafc;
            }
            QTableWidget::item:selected {
                background: rgba(56, 189, 248, 0.16);
                color: #f8fafc;
                border-top: 1px solid rgba(34, 211, 238, 0.95);
                border-bottom: 1px solid rgba(34, 211, 238, 0.95);
            }
            QHeaderView::section {
                background: #0b1220;
                color: #cbd5e1;
                padding: 8px;
                border: none;
                border-bottom: 1px solid #334155;
            }
            QHeaderView {
                background: #0b1220;
            }
            QTableCornerButton::section {
                background: #0b1220;
                border: none;
                border-bottom: 1px solid #334155;
                border-right: 1px solid #334155;
            }
            #SavedDataTitle {
                font-size: 22px;
                font-weight: 700;
                color: #f8fafc;
            }
            """
        self.setStyleSheet(
            style_qss
            .replace("__TREE_BRANCH_CLOSED_ICON__", branch_closed_icon)
            .replace("__TREE_BRANCH_OPEN_ICON__", branch_open_icon)
        )

    def _matching_saved_dataset_id(self, profile: DeviceProfile) -> int | None:
        for record in list_saved_imports():
            metadata = record.metadata
            if metadata.get("provider") != "dessmonitor":
                continue
            if (
                str(metadata.get("pn", "")) == profile.pn
                and str(metadata.get("devcode", "")) == profile.devcode
                and str(metadata.get("devaddr", "")) == profile.devaddr
                and str(metadata.get("sn", "")) == profile.sn
            ):
                return record.import_id
        return None

    def _apply_device_profile(
        self,
        profile: DeviceProfile,
        *,
        start_energyflow: bool = True,
        tuya_refresh_now: bool = True,
        tuya_refresh_if_empty: bool = True,
    ) -> None:
        stack_preview = " > ".join(frame.name for frame in traceback.extract_stack(limit=7)[:-1])
        pass
        # Prevent leftover timers from triggering an extra refresh during profile activation.
        self.energyflow_refresh_timer.stop()
        self.energyflow_countdown_timer.stop()
        self._energyflow_start_timers_after_refresh = False
        self.active_device_profile = profile
        self._apply_ui_language(profile.resolved_ui_language(), persist=True)
        self._profile_recovery_attempted = False
        self._forecast_loaded_profile_key = None
        active_key = self._active_profile_key()
        if active_key is not None and active_key not in self._inverter_settings_cache:
            disk_cached = load_inverter_settings_cache(
                profile_name=profile.profile_name,
                pn=profile.pn,
                devcode=profile.devcode,
                devaddr=profile.devaddr,
            )
            if disk_cached is not None:
                self._inverter_settings_cache[active_key] = disk_cached
        cached = self._inverter_settings_cache.get(active_key) if active_key is not None else None
        if cached is not None and active_key is not None:
            self._latest_inverter_settings = list(cached[0])
            self._latest_inverter_settings_key = active_key
        elif self._latest_inverter_settings_key != active_key:
            self._latest_inverter_settings = []
            self._latest_inverter_settings_key = None
        self._weather_live_state = "unknown"
        self._weather_live_error = ""
        self._last_weather_snapshot = None
        self._apply_energyflow_refresh_interval(profile)
        self._apply_auto_sync_interval(profile)
        self._tuya_settings = self._tuya_settings_from_profile(profile)
        self._apply_tuya_settings(
            refresh_now=tuya_refresh_now,
            refresh_if_empty=tuya_refresh_if_empty,
        )
        if hasattr(self, "inverter_settings_tab"):
            if cached is not None:
                self.inverter_settings_tab.set_settings(profile, list(cached[0]), cached[1])
            else:
                self.inverter_settings_tab.set_profile(profile)
        self.energyflow_view.set_device_meta(
            profile.profile_name or profile.device_label or "Inverter",
            profile.pn,
            profile.sn,
        )
        profile_label = profile.device_label or profile.profile_name or profile.sn or profile.pn or "selected profile"
        if self._last_energyflow_snapshot is not None and self._last_energyflow_snapshot_key == active_key:
            self.energyflow_summary_label.setText(self._build_energyflow_summary(self._last_energyflow_snapshot))
            updated_at = self._last_energyflow_updated_at_text or pd.Timestamp.now().strftime("%H:%M:%S")
            self._set_tab_message(
                self.energyflow_meta_label,
                tr_fragment(f"Last updated {updated_at} • Source: DessMonitor + Weather • Weather: {self._localized_weather_state()}"),
                error=False,
            )
        else:
            self.energyflow_summary_label.setText(tr("Waiting for a live snapshot."))
            self._set_tab_message(self.energyflow_meta_label, tr_fragment(f"Loading snapshot for {profile_label}..."), error=False)
        self._update_energyflow_status_bar()
        self.energyflow_view.set_battery_capability_ah(profile.resolved_battery_capability_ah())
        self._sync_energyflow_tuya_icons()
        self._update_device_profile_button()
        if hasattr(self, "forecast_tab"):
            self.forecast_tab.set_unavailable(tr("Forecast will refresh when you open the Forecast tab."))
        self.statusBar().showMessage(tr_fragment(f"Active API device: {profile.device_label or profile.profile_name}"))
        if hasattr(self, "tabs") and self.tabs.currentWidget() is self.inverter_settings_tab:
            self._load_inverter_settings_if_needed(force=False)
        if start_energyflow:
            self._begin_energyflow_refresh_cycle()

    def _can_run_energyflow_auto_cycle(self) -> bool:
        return (
            not self._close_requested
            and self.active_device_profile is not None
            and hasattr(self, "tabs")
            and self.tabs.currentWidget() is self.energyflow_tab
            and not self._profile_activation_in_progress
            and self._energyflow_auto_refresh_ready
        )

    def _resume_energyflow_auto_cycle(self) -> None:
        if not self._energyflow_auto_refresh_enabled:
            self.energyflow_refresh_timer.stop()
            self.energyflow_countdown_timer.stop()
            self._set_energyflow_refresh_button_label()
            return
        if not self._can_run_energyflow_auto_cycle():
            return
        self.energyflow_refresh_timer.start()
        if not self.energyflow_countdown_timer.isActive():
            self.energyflow_countdown_timer.start()
        self._reset_energyflow_countdown()

    def _reconcile_energyflow_refresh_state(self) -> None:
        if self._energyflow_refresh_pending:
            self._set_energyflow_refresh_active(True)
            return
        if not self._energyflow_refresh_inflight:
            if self._energyflow_status_override:
                pass
                self._energyflow_status_override = None
            self._set_energyflow_refresh_active(False)
            return
        thread = self._energyflow_thread
        if thread is None:
            elapsed = (
                max(0.0, time.monotonic() - self._energyflow_refresh_started_monotonic)
                if self._energyflow_refresh_started_monotonic is not None
                else 0.0
            )
            pass
            if elapsed < 2.0:
                self._set_energyflow_refresh_active(True)
                return
            pass
            self._clear_energyflow_thread()
            return
        if not thread.is_alive():
            self._clear_energyflow_thread()
            return
        self._set_energyflow_refresh_active(True)

    def _update_device_profile_button(self) -> None:
        if not hasattr(self, "device_profile_button"):
            return
        if self.active_device_profile is None:
            self.device_profile_button.setProfileLines("Choose a profile")
            self.device_profile_button.edit_profile_link.setEnabled(False)
            self.device_profile_button.setSyncActive(False)
            self.device_profile_button.setToolTip("")
            return
        self.device_profile_button.edit_profile_link.setEnabled(True)
        active_label = self.active_device_profile.profile_name or self.active_device_profile.device_label or "Active profile"
        self.device_profile_button.setProfileLines(active_label)
        self.device_profile_button.setSyncActive(self._auto_sync_inflight)
        self.device_profile_button.setToolTip("")

    def _update_chart_action_states(self) -> None:
        has_processed_data = self.processed_data is not None
        has_chart = self.current_figure is not None
        self.export_chart_button.setEnabled(has_chart)
        self.export_all_button.setEnabled(has_chart)
        self.reset_chart_button.setEnabled(has_processed_data)

    def _profile_initial_state(self) -> dict[str, object]:
        profile = self.active_device_profile
        if profile is None:
            return {}
        return {
            "profile_name": profile.profile_name,
            "username": profile.username,
            "password": profile.password,
            "company_key": profile.company_key,
            "source": profile.source,
            "pn": profile.pn,
            "devcode": profile.devcode,
            "devaddr": profile.devaddr,
            "sn": profile.sn,
            "parameter_keys": profile.resolved_parameter_keys(),
            "weather_available": bool(
                profile.inverter_latitude.strip() and profile.inverter_longitude.strip()
            ),
            "include_dessmonitor": True,
            "include_weather": bool(
                profile.inverter_latitude.strip() and profile.inverter_longitude.strip()
            ),
        }

    def _build_energyflow_config(self) -> DessMonitorConfig:
        profile = self.active_device_profile
        if profile is None:
            raise DessMonitorApiError(tr("Choose an active device profile to load EnergyFlow."))
        return DessMonitorConfig(
            username=profile.username,
            password=profile.password,
            company_key=profile.company_key,
            source=profile.source,
            pn=profile.pn,
            devcode=profile.devcode,
            devaddr=profile.devaddr,
            sn=profile.sn,
            device_label=profile.device_label or profile.profile_name,
            parameter_keys=[],
            date_from=pd.Timestamp.today().date().isoformat(),
            date_to=pd.Timestamp.today().date().isoformat(),
        )

    def _apply_ui_language(self, language_code: str, *, persist: bool) -> None:
        applied = set_language(language_code, persist=persist)
        translate_widget_tree(self)
        LOGGER.info("UI language applied: %s", applied)

    def _active_profile_key(self) -> tuple[str, str, str, str] | None:
        profile = self.active_device_profile
        if profile is None:
            return None
        return (profile.profile_name, profile.pn, profile.devcode, profile.devaddr)

    def _build_inverter_settings_config(self) -> DessMonitorConfig:
        profile = self.active_device_profile
        if profile is None:
            raise DessMonitorApiError("Choose an active device profile to load inverter settings.")
        today = pd.Timestamp.today().date().isoformat()
        return DessMonitorConfig(
            username=profile.username,
            password=profile.password,
            company_key=profile.company_key,
            source=profile.source,
            pn=profile.pn,
            devcode=profile.devcode,
            devaddr=profile.devaddr,
            sn=profile.sn,
            device_label=profile.device_label or profile.profile_name,
            parameter_keys=[],
            date_from=today,
            date_to=today,
        )

    def _handle_tab_changed(self, index: int) -> None:
        self._refresh_tab_badges()
        if self.tabs.widget(index) is self.forecast_tab:
            # Ensure forecast cache/live sync logic runs when the user opens Forecast.
            self._prime_forecast_for_active_profile(build_if_missing=True)
        if self.tabs.widget(index) is not self.energyflow_tab:
            self.energyflow_refresh_timer.stop()
            self.energyflow_countdown_timer.stop()
            self._energyflow_start_timers_after_refresh = False
        else:
            if (
                self._energyflow_auto_refresh_enabled
                and
                self.active_device_profile is not None
                and not self._energyflow_refresh_inflight
                and not self._profile_activation_in_progress
                and not self.energyflow_countdown_timer.isActive()
            ):
                self.energyflow_countdown_timer.start()
            # Do not auto-refresh on tab enter; only keep current view state and countdown.
            self._update_energyflow_countdown_text()

    def _handle_forecast_refresh_click(self) -> None:
        self._refresh_forecast(force=True)

    def _refresh_forecast(self, *, force: bool) -> None:
        if self._forecast_refresh_inflight or self._close_requested:
            return
        profile = self.active_device_profile
        if profile is None:
            self.forecast_tab.set_tariff_cost_estimate(0.0, 0.0, enabled=False)
            self.forecast_tab.set_unavailable("Choose an active device profile to build a forecast.")
            return
        if not self._active_profile_has_weather_location():
            self._apply_forecast_tariff_cost_estimate(profile)
            self.forecast_tab.set_unavailable("Save inverter coordinates in the profile to use weather-based forecasting.")
            return
        self._apply_forecast_tariff_cost_estimate(profile)
        if not force and self.tabs.currentWidget() is not self.forecast_tab:
            return
        if not force and self._forecast_loaded_profile_key == self._active_profile_key():
            return
        self._forecast_refresh_inflight = True
        self._refresh_tab_badges()
        self._forecast_request_id += 1
        request_id = self._forecast_request_id
        self.forecast_tab.set_loading("Building the PV forecast from DessMonitor and weather data...")
        thread = threading.Thread(
            target=self._run_forecast_fetch,
            args=(request_id, profile),
            daemon=True,
            name=f"pv-forecast-{request_id}",
        )
        self._forecast_thread = thread
        thread.start()

    def _run_forecast_fetch(self, request_id: int, profile: DeviceProfile) -> None:
        try:
            forecast_run_at = pd.Timestamp.now(tz="UTC")
            dessmonitor_metadata = {
                "provider": "dessmonitor",
                "pn": profile.pn,
                "devcode": profile.devcode,
                "devaddr": profile.devaddr,
                "sn": profile.sn,
                "device_label": profile.device_label,
            }
            dessmonitor_import_id = latest_import_id_for_metadata(dessmonitor_metadata)
            if dessmonitor_import_id is None:
                raise PvForecastError("Import DessMonitor data first to build a forecast.")
            dessmonitor_frame = load_import_dataframe(dessmonitor_import_id)
            if dessmonitor_frame.empty:
                raise PvForecastError("The saved DessMonitor dataset does not contain any rows.")
            dessmonitor_frame = self._enrich_today_pv_actual_from_api(profile, dessmonitor_frame)

            weather_history_frame = pd.DataFrame()
            weather_metadata = {
                "provider": "weather",
                "weather_provider": DEFAULT_WEATHER_PROVIDER,
                "series_type": "history",
                "pn": profile.pn,
                "devcode": profile.devcode,
                "devaddr": profile.devaddr,
                "sn": profile.sn,
                "device_label": profile.device_label,
                "latitude": profile.inverter_latitude.strip(),
                "longitude": profile.inverter_longitude.strip(),
            }
            weather_history_import_id = latest_import_id_for_metadata(weather_metadata)
            if weather_history_import_id is not None:
                weather_history_frame = load_import_dataframe(weather_history_import_id)

            weather_forecast_frame = fetch_weather_forecast_dataset(
                WeatherForecastRequest(
                    latitude=profile.inverter_latitude.strip(),
                    longitude=profile.inverter_longitude.strip(),
                    timezone="Europe/Kiev",
                    location_label=(
                        profile.inverter_location_description.strip()
                        or profile.inverter_location_label.strip()
                    ),
                    hourly_fields=list(DEFAULT_WEATHER_FORECAST_FIELDS),
                    forecast_days=4,
                )
            )
            result = build_pv_forecast(
                dessmonitor_frame,
                weather_forecast_frame,
                weather_history_frame=weather_history_frame,
            )
            dess_ts_column = next(
                (column for column in dessmonitor_frame.columns if str(column).strip().lower() == "timestamp"),
                "Timestamp",
            )
            hourly_correction, matched_pairs = build_hourly_correction_from_snapshots(
                pn=profile.pn,
                devcode=profile.devcode,
                devaddr=profile.devaddr,
                dessmonitor_frame=dessmonitor_frame,
                timestamp_column=str(dess_ts_column),
                pv_column=result.source_pv_column,
            )
            if hourly_correction:
                result, _ = apply_hourly_correction(
                    result,
                    hourly_correction,
                    note_prefix=f"Snapshot self-calibration ({matched_pairs} matched pair(s))",
                )
            result = self._inject_live_actual_into_forecast_if_missing(profile, result)
            snapshot_frame = build_forecast_snapshot_frame(result, run_at=forecast_run_at)
            if not snapshot_frame.empty:
                source_label = "PV Forecast"
                try:
                    save_import_dataframe(
                        snapshot_frame,
                        source_label=source_label,
                        metadata={
                            "provider": "pv_forecast",
                            "series_type": "hourly_72h",
                            "pn": profile.pn,
                            "devcode": profile.devcode,
                            "devaddr": profile.devaddr,
                            "sn": profile.sn,
                            "device_label": profile.device_label,
                            "model_name": result.model_name,
                            "forecast_run_at": forecast_run_at.isoformat(),
                        },
                    )
                except Exception:
                    # A failed snapshot write should not block the forecast UI refresh.
                    LOGGER.exception("Failed to persist PV forecast snapshot to local storage")
        except (PvForecastError, WeatherApiError) as exc:
            if not self._close_requested:
                self._forecast_bridge.failed.emit(request_id, str(exc))
            return
        if not self._close_requested:
            self._forecast_bridge.finished.emit(request_id, result)

    def _inject_live_actual_into_forecast_if_missing(
        self,
        profile: DeviceProfile,
        result: PvForecastResult,
    ) -> PvForecastResult:
        compare = result.compare_frame.copy() if isinstance(result.compare_frame, pd.DataFrame) else pd.DataFrame()
        if (
            not compare.empty
            and "actual_pv_power_kw" in compare.columns
            and compare["actual_pv_power_kw"].notna().any()
        ):
            return result
        try:
            today = pd.Timestamp.today().date().isoformat()
            snapshot = fetch_energy_flow_snapshot(
                DessMonitorConfig(
                    username=profile.username,
                    password=profile.password,
                    company_key=profile.company_key,
                    source=profile.source,
                    pn=profile.pn,
                    devcode=profile.devcode,
                    devaddr=profile.devaddr,
                    sn=profile.sn,
                    device_label=profile.device_label or profile.profile_name,
                    parameter_keys=[],
                    date_from=today,
                    date_to=today,
                )
            )
        except Exception:
            LOGGER.exception("Forecast actual fallback: failed to fetch current EnergyFlow snapshot")
            return result
        if snapshot.pv_power is None:
            LOGGER.warning("Forecast actual fallback: snapshot has no PV power value")
            return result
        try:
            actual_power_kw = max(float(snapshot.pv_power) / 1000.0, 0.0)
        except (TypeError, ValueError):
            LOGGER.exception("Forecast actual fallback: failed to normalize PV power from snapshot")
            return result
        now_local = pd.Timestamp.now(tz="Europe/Kiev").tz_localize(None).floor("h")
        stamp = self._to_local_naive_timestamp(pd.Series([snapshot.timestamp])).iloc[0]
        if pd.isna(stamp):
            stamp = now_local
        stamp = pd.Timestamp(stamp).floor("h")

        if compare.empty or "predicted_pv_power_kw" not in compare.columns or "Timestamp" not in compare.columns:
            today_frame = result.forecast_frame.copy()
            today_frame["Timestamp"] = self._to_local_naive_timestamp(today_frame["Timestamp"])
            today_key = now_local.normalize()
            today_frame = today_frame[today_frame["Timestamp"].dt.normalize() == today_key].copy()
            compare = pd.DataFrame(
                {
                    "Timestamp": today_frame["Timestamp"],
                    "predicted_pv_power_kw": pd.to_numeric(
                        today_frame.get("predicted_pv_power_kw"),
                        errors="coerce",
                    ),
                    "actual_pv_power_kw": pd.NA,
                }
            )
        else:
            compare["Timestamp"] = self._to_local_naive_timestamp(compare["Timestamp"])
            compare["predicted_pv_power_kw"] = pd.to_numeric(compare["predicted_pv_power_kw"], errors="coerce")
            if "actual_pv_power_kw" not in compare.columns:
                compare["actual_pv_power_kw"] = pd.NA
            compare["actual_pv_power_kw"] = pd.to_numeric(compare["actual_pv_power_kw"], errors="coerce")
        compare.dropna(subset=["Timestamp"], inplace=True)
        if compare.empty:
            return result

        if (compare["Timestamp"] == stamp).any():
            compare.loc[compare["Timestamp"] == stamp, "actual_pv_power_kw"] = actual_power_kw
        else:
            predicted_series = (
                compare[["Timestamp", "predicted_pv_power_kw"]]
                .dropna(subset=["Timestamp"])
                .drop_duplicates(subset=["Timestamp"])
                .sort_values("Timestamp")
                .set_index("Timestamp")["predicted_pv_power_kw"]
            )
            predicted_at_stamp = float("nan")
            if not predicted_series.empty:
                union_index = predicted_series.index.union(pd.DatetimeIndex([stamp])).sort_values()
                predicted_interp = (
                    predicted_series.reindex(union_index)
                    .interpolate(method="time", limit_direction="both")
                    .reindex(union_index)
                )
                predicted_at_stamp = float(predicted_interp.loc[stamp]) if pd.notna(predicted_interp.loc[stamp]) else 0.0
            compare = pd.concat(
                [
                    compare,
                    pd.DataFrame(
                        {
                            "Timestamp": [stamp],
                            "predicted_pv_power_kw": [predicted_at_stamp if math.isfinite(predicted_at_stamp) else 0.0],
                            "actual_pv_power_kw": [actual_power_kw],
                        }
                    ),
                ],
                ignore_index=True,
            )

        compare.sort_values("Timestamp", inplace=True)
        compare.drop_duplicates(subset=["Timestamp"], keep="last", inplace=True)
        compare.reset_index(drop=True, inplace=True)

        today_key = now_local.normalize()
        compare_today = compare[compare["Timestamp"].dt.normalize() == today_key].copy()
        valid_actual = compare_today[compare_today["actual_pv_power_kw"].notna()].copy()
        if valid_actual.empty:
            return result
        latest_actual_time = pd.to_datetime(valid_actual["Timestamp"], errors="coerce").max()
        upto = compare_today[pd.to_datetime(compare_today["Timestamp"], errors="coerce") <= latest_actual_time].copy()
        result.compare_frame = compare_today.reset_index(drop=True)
        result.actual_today_energy_kwh = float(
            pd.to_numeric(valid_actual["actual_pv_power_kw"], errors="coerce").fillna(0.0).sum()
        )
        result.forecast_today_to_now_kwh = float(
            pd.to_numeric(upto["predicted_pv_power_kw"], errors="coerce").fillna(0.0).sum()
        )
        result.delta_today_kwh = float(result.actual_today_energy_kwh - result.forecast_today_to_now_kwh)
        notes = list(result.notes or [])
        note = tr("Actual PV was backfilled from current EnergyFlow snapshot.")
        if note not in notes:
            notes.append(note)
        result.notes = notes
        return result

    def _enrich_today_pv_actual_from_api(self, profile: DeviceProfile, base_frame: pd.DataFrame) -> pd.DataFrame:
        frame = base_frame.copy()
        today = pd.Timestamp.now(tz="Europe/Kiev").strftime("%Y-%m-%d")
        today_config = DessMonitorConfig(
            username=profile.username,
            password=profile.password,
            company_key=profile.company_key,
            source=profile.source,
            pn=profile.pn,
            devcode=profile.devcode,
            devaddr=profile.devaddr,
            sn=profile.sn,
            device_label=profile.device_label or profile.profile_name,
            parameter_keys=["PV_OUTPUT_POWER"],
            date_from=today,
            date_to=today,
            period_mode="custom",
        )
        try:
            today_frame = fetch_device_range_data(today_config)
        except Exception:
            LOGGER.exception("Failed to enrich forecast history with today's PV actual API data")
            return frame
        if today_frame is None or today_frame.empty:
            return frame

        merged = pd.concat([frame, today_frame], ignore_index=True, sort=False)
        ts_column = next(
            (column for column in merged.columns if str(column).strip().lower() == "timestamp"),
            None,
        )
        if ts_column is None:
            return merged
        timestamps = pd.to_datetime(merged[ts_column], errors="coerce")
        merged = merged.loc[timestamps.notna()].copy()
        if merged.empty:
            return merged
        merged["_timestamp_norm"] = pd.to_datetime(merged[ts_column], errors="coerce")
        merged.sort_values("_timestamp_norm", inplace=True)
        merged = merged.drop_duplicates(subset=["_timestamp_norm"], keep="last")
        merged.drop(columns=["_timestamp_norm"], inplace=True, errors="ignore")
        merged.reset_index(drop=True, inplace=True)
        return merged

    def _apply_forecast_result(self, request_id: int, payload: object) -> None:
        if request_id != self._forecast_request_id or self._close_requested:
            return
        if not isinstance(payload, PvForecastResult):
            self._handle_forecast_error(request_id, "Forecast payload is invalid.")
            return
        self._forecast_best_result = payload
        self._forecast_best_result_key = self._active_profile_key()
        self.forecast_tab.set_result(payload)
        if self.active_device_profile is not None:
            self._apply_forecast_tariff_cost_estimate(self.active_device_profile)
        self._forecast_loaded_profile_key = self._active_profile_key()
        self.statusBar().showMessage(tr("PV forecast updated."))
        self._clear_forecast_thread()

    def _handle_forecast_error(self, request_id: int, message: str) -> None:
        if request_id != self._forecast_request_id or self._close_requested:
            return
        cached_snapshot = self._load_latest_pv_forecast_snapshot_for_active_profile()
        cached_result = self._build_forecast_result_from_snapshot(cached_snapshot)
        if cached_result is not None:
            self._forecast_best_result = cached_result
            self._forecast_best_result_key = self._active_profile_key()
            self.forecast_tab.set_result(cached_result)
            if self.active_device_profile is not None:
                self._apply_forecast_tariff_cost_estimate(self.active_device_profile)
            self._forecast_loaded_profile_key = self._active_profile_key()
            self.statusBar().showMessage(tr("PV forecast loaded from cache (live API unavailable)."))
        else:
            self.forecast_tab.set_error(message)
            if self.active_device_profile is not None:
                self._apply_forecast_tariff_cost_estimate(self.active_device_profile)
            self.statusBar().showMessage(tr("PV forecast unavailable."))
        self._clear_forecast_thread()

    @staticmethod
    def _profile_tariffs_enabled(profile: DeviceProfile | None) -> bool:
        if profile is None:
            return False
        return (
            profile.resolved_day_zone_tariff_uah_per_kwh() > 0.0
            and profile.resolved_night_zone_tariff_uah_per_kwh() > 0.0
        )

    def _estimate_latest_day_grid_import_costs(self, profile: DeviceProfile) -> tuple[float, float]:
        processed = self._resolved_active_profile_processed_data()
        if processed is None:
            return 0.0, 0.0
        frame = processed.dataframe.copy()
        timestamp_column = processed.timestamp_column
        if timestamp_column not in frame.columns:
            return 0.0, 0.0
        grid_column = next(
            (
                column
                for column in processed.numeric_columns
                if column in {"GRID_ACTIVE_POWER", "grid_active_power"}
            ),
            None,
        )
        if grid_column is None:
            grid_column = next(
                (
                    column
                    for column in processed.numeric_columns
                    if "grid" in str(column).strip().lower() and "power" in str(column).strip().lower()
                ),
                None,
            )
        if grid_column is None or grid_column not in frame.columns:
            return 0.0, 0.0

        frame["timestamp"] = pd.to_datetime(frame[timestamp_column], errors="coerce")
        frame["grid_power"] = pd.to_numeric(frame[grid_column], errors="coerce")
        frame.dropna(subset=["timestamp", "grid_power"], inplace=True)
        frame.sort_values("timestamp", inplace=True)
        if frame.empty:
            return 0.0, 0.0

        frame["next_timestamp"] = frame["timestamp"].shift(-1)
        frame["next_grid_power"] = frame["grid_power"].shift(-1)
        frame.dropna(subset=["next_timestamp", "next_grid_power"], inplace=True)
        frame["duration_hours"] = (frame["next_timestamp"] - frame["timestamp"]).dt.total_seconds() / 3600.0
        frame = frame[frame["duration_hours"] > 0]
        if frame.empty:
            return 0.0, 0.0

        latest_day = frame["timestamp"].dt.normalize().max()
        frame = frame[frame["timestamp"].dt.normalize() == latest_day].copy()
        if frame.empty:
            return 0.0, 0.0

        midpoint = frame["timestamp"] + (frame["next_timestamp"] - frame["timestamp"]) / 2
        frame["period"] = midpoint.dt.hour.map(lambda hour: "day" if 7 <= int(hour) < 23 else "night")
        frame["grid_import_kwh"] = (
            (frame["grid_power"].clip(lower=0.0) + frame["next_grid_power"].clip(lower=0.0)) / 2.0
        ) * frame["duration_hours"]
        grouped = frame.groupby("period", dropna=True)["grid_import_kwh"].sum()
        day_import = float(grouped.get("day", 0.0))
        night_import = float(grouped.get("night", 0.0))
        return (
            day_import * profile.resolved_day_zone_tariff_uah_per_kwh(),
            night_import * profile.resolved_night_zone_tariff_uah_per_kwh(),
        )

    def _apply_forecast_tariff_cost_estimate(self, profile: DeviceProfile | None) -> None:
        if not self._profile_tariffs_enabled(profile):
            self.forecast_tab.set_tariff_cost_estimate(0.0, 0.0, enabled=False)
            return
        assert profile is not None
        day_cost, night_cost = self._estimate_latest_day_grid_import_costs(profile)
        self.forecast_tab.set_tariff_cost_estimate(day_cost, night_cost, enabled=True)

    def _build_forecast_result_from_snapshot(self, snapshot_frame: pd.DataFrame) -> PvForecastResult | None:
        if snapshot_frame is None or snapshot_frame.empty or "Timestamp" not in snapshot_frame.columns:
            return None

        frame = snapshot_frame.copy()
        frame["Timestamp"] = self._to_local_naive_timestamp(frame["Timestamp"])
        frame["predicted_pv_power_kw"] = pd.to_numeric(frame.get("predicted_pv_power_kw"), errors="coerce")
        frame = frame.dropna(subset=["Timestamp", "predicted_pv_power_kw"]).copy()
        if frame.empty:
            return None

        # Snapshot storage keeps historical forecast runs. For startup rendering we
        # must show only the latest run so it matches manual Refresh output.
        if "forecast_run_at" in frame.columns:
            frame["forecast_run_at"] = pd.to_datetime(frame["forecast_run_at"], errors="coerce")
            latest_run_at = frame["forecast_run_at"].dropna().max()
            if pd.notna(latest_run_at):
                frame = frame[frame["forecast_run_at"] == latest_run_at].copy()
        if frame.empty:
            return None
        if "lead_hours" in frame.columns:
            frame["lead_hours"] = pd.to_numeric(frame["lead_hours"], errors="coerce")
            frame = frame[
                frame["lead_hours"].notna()
                & (frame["lead_hours"] >= 0.0)
                & (frame["lead_hours"] <= 96.0)
            ].copy()
        if frame.empty:
            return None
        frame = frame.drop_duplicates(subset=["Timestamp"], keep="last").copy()
        frame.sort_values("Timestamp", inplace=True)
        frame.reset_index(drop=True, inplace=True)

        now = pd.Timestamp.now(tz="Europe/Kiev").tz_localize(None)
        today = now.normalize()
        tomorrow = today + pd.Timedelta(days=1)
        today_frame = frame[frame["Timestamp"].dt.normalize() == today]
        tomorrow_frame = frame[frame["Timestamp"].dt.normalize() == tomorrow]
        today_energy_kwh = float(today_frame["predicted_pv_power_kw"].sum()) if not today_frame.empty else 0.0
        tomorrow_energy_kwh = float(tomorrow_frame["predicted_pv_power_kw"].sum()) if not tomorrow_frame.empty else 0.0

        peak_idx = frame["predicted_pv_power_kw"].idxmax()
        peak_row = frame.loc[peak_idx] if peak_idx in frame.index else None
        peak_power_kw = float(peak_row["predicted_pv_power_kw"]) if peak_row is not None else 0.0
        peak_power_at = (
            pd.Timestamp(peak_row["Timestamp"]).strftime("%d.%m %H:%M")
            if peak_row is not None and pd.notna(peak_row.get("Timestamp"))
            else "--"
        )

        upcoming = frame[frame["Timestamp"] >= now]
        driver_row = upcoming.iloc[0] if not upcoming.empty else frame.iloc[-1]
        driver_snapshot = {
            "shortwave_radiation": float(driver_row["shortwave_radiation"])
            if "shortwave_radiation" in driver_row and pd.notna(driver_row["shortwave_radiation"])
            else None,
            "cloud_cover": float(driver_row["cloud_cover"])
            if "cloud_cover" in driver_row and pd.notna(driver_row["cloud_cover"])
            else None,
            "temperature_2m": float(driver_row["temperature_2m"])
            if "temperature_2m" in driver_row and pd.notna(driver_row["temperature_2m"])
            else None,
            "wind_speed_10m": float(driver_row["wind_speed_10m"])
            if "wind_speed_10m" in driver_row and pd.notna(driver_row["wind_speed_10m"])
            else None,
        }

        compare_frame = self._build_cached_snapshot_compare_frame(frame)
        actual_today_energy_kwh: float | None = 0.0
        forecast_today_to_now_kwh: float | None = 0.0
        delta_today_kwh: float | None = 0.0
        if not compare_frame.empty and "actual_pv_power_kw" in compare_frame.columns:
            valid_actual = compare_frame[compare_frame["actual_pv_power_kw"].notna()].copy()
            if not valid_actual.empty:
                latest_actual_time = pd.to_datetime(valid_actual["Timestamp"], errors="coerce").max()
                today_actual = valid_actual[
                    pd.to_datetime(valid_actual["Timestamp"], errors="coerce").dt.normalize() == today
                ].copy()
                actual_today_energy_kwh = (
                    float(pd.to_numeric(today_actual["actual_pv_power_kw"], errors="coerce").fillna(0.0).sum())
                    if not today_actual.empty
                    else 0.0
                )
                upto = compare_frame[pd.to_datetime(compare_frame["Timestamp"], errors="coerce") <= latest_actual_time].copy()
                forecast_today_to_now_kwh = (
                    float(pd.to_numeric(upto["predicted_pv_power_kw"], errors="coerce").fillna(0.0).sum())
                    if not upto.empty
                    else 0.0
                )
                delta_today_kwh = (
                    float(actual_today_energy_kwh) - float(forecast_today_to_now_kwh)
                    if forecast_today_to_now_kwh is not None and actual_today_energy_kwh is not None
                    else None
                )
        notes = [
            "Loaded from cached PV forecast snapshot because live weather API is temporarily unavailable.",
            f"Snapshot rows: {len(frame)}",
        ]
        if not compare_frame.empty and compare_frame["actual_pv_power_kw"].notna().any():
            notes.append("Actual PV values were aligned from local DessMonitor history.")
        else:
            notes.append("No actual PV points for today yet; showing zero progress until telemetry arrives.")
        return PvForecastResult(
            source_pv_column="predicted_pv_power_kw",
            forecast_frame=frame,
            compare_frame=compare_frame,
            today_energy_kwh=today_energy_kwh,
            tomorrow_energy_kwh=tomorrow_energy_kwh,
            actual_today_energy_kwh=actual_today_energy_kwh,
            forecast_today_to_now_kwh=forecast_today_to_now_kwh,
            delta_today_kwh=delta_today_kwh,
            peak_power_kw=peak_power_kw,
            peak_power_at=peak_power_at,
            confidence_score=0.45,
            confidence_label="Cached snapshot",
            training_days=0,
            matched_history_rows=0,
            model_name="Cached PV Forecast Snapshot",
            driver_snapshot=driver_snapshot,
            notes=notes,
        )

    def _build_cached_snapshot_compare_frame(self, forecast_frame: pd.DataFrame) -> pd.DataFrame:
        today = pd.Timestamp.now(tz="Europe/Kiev").tz_localize(None).normalize()
        forecast_today = forecast_frame[["Timestamp", "predicted_pv_power_kw"]].copy()
        forecast_today["Timestamp"] = self._to_local_naive_timestamp(forecast_today["Timestamp"])
        forecast_today["predicted_pv_power_kw"] = pd.to_numeric(forecast_today["predicted_pv_power_kw"], errors="coerce")
        forecast_today.dropna(subset=["Timestamp", "predicted_pv_power_kw"], inplace=True)
        forecast_today = forecast_today[forecast_today["Timestamp"].dt.normalize() == today].copy()
        if forecast_today.empty:
            return pd.DataFrame(columns=["Timestamp", "predicted_pv_power_kw", "actual_pv_power_kw"])

        forecast_series = (
            forecast_today[["Timestamp", "predicted_pv_power_kw"]]
            .drop_duplicates(subset=["Timestamp"])
            .sort_values("Timestamp")
            .set_index("Timestamp")["predicted_pv_power_kw"]
        )

        metadata = self._active_profile_metadata()
        if metadata is None:
            return forecast_series.reset_index().assign(actual_pv_power_kw=pd.NA)
        import_id = latest_import_id_for_metadata(metadata)
        if import_id is None:
            return forecast_series.reset_index().assign(actual_pv_power_kw=pd.NA)
        dessmonitor_frame = load_import_dataframe(import_id)
        if dessmonitor_frame.empty:
            return forecast_series.reset_index().assign(actual_pv_power_kw=pd.NA)

        ts_column = next(
            (column for column in dessmonitor_frame.columns if str(column).strip().lower() == "timestamp"),
            None,
        )
        if ts_column is None:
            return forecast_series.reset_index().assign(actual_pv_power_kw=pd.NA)

        pv_column = self._infer_actual_pv_power_column(dessmonitor_frame)
        if pv_column is None:
            return forecast_series.reset_index().assign(actual_pv_power_kw=pd.NA)

        actual = dessmonitor_frame[[ts_column, pv_column]].copy()
        actual.columns = ["Timestamp", "actual_pv_power_kw"]
        actual["Timestamp"] = self._to_local_naive_timestamp(actual["Timestamp"])
        actual["actual_pv_power_kw"] = pd.to_numeric(actual["actual_pv_power_kw"], errors="coerce").clip(lower=0.0)
        actual.dropna(subset=["Timestamp", "actual_pv_power_kw"], inplace=True)
        actual = actual[actual["Timestamp"].dt.normalize() == today].copy()
        if actual.empty:
            return forecast_series.reset_index().assign(actual_pv_power_kw=pd.NA)

        actual_series = (
            actual[["Timestamp", "actual_pv_power_kw"]]
            .drop_duplicates(subset=["Timestamp"])
            .sort_values("Timestamp")
            .set_index("Timestamp")["actual_pv_power_kw"]
        )
        union_index = forecast_series.index.union(actual_series.index).sort_values()
        predicted = (
            forecast_series.reindex(union_index)
            .interpolate(method="time", limit_direction="both")
            .reindex(union_index)
        )
        actual_aligned = actual_series.reindex(union_index)
        merged = pd.DataFrame(
            {
                "Timestamp": union_index,
                "predicted_pv_power_kw": predicted.to_numpy(),
                "actual_pv_power_kw": actual_aligned.to_numpy(),
            }
        )
        return merged.reset_index(drop=True)

    @staticmethod
    def _infer_actual_pv_power_column(dataframe: pd.DataFrame) -> str | None:
        try:
            return _select_pv_power_column(dataframe)
        except Exception:
            LOGGER.exception("Failed to infer actual PV power column from dataframe")
            return None

    @staticmethod
    def _to_local_naive_timestamp(values: pd.Series) -> pd.Series:
        timestamps = pd.to_datetime(values, errors="coerce")
        tzinfo = getattr(timestamps.dt, "tz", None)
        if tzinfo is not None:
            timestamps = timestamps.dt.tz_convert("Europe/Kiev").dt.tz_localize(None)
        return timestamps

    def _clear_forecast_thread(self) -> None:
        self._forecast_thread = None
        self._forecast_refresh_inflight = False
        self._refresh_tab_badges()

    def _handle_energyflow_refresh_click(self) -> None:
        pass
        self._reconcile_energyflow_refresh_state()
        if self._energyflow_refresh_inflight:
            pass
            self._set_energyflow_refresh_active(True)
            self.statusBar().showMessage(tr("EnergyFlow refresh is already in progress."))
            return
        if self.active_device_profile is None:
            if not self._try_recover_active_profile():
                self.open_device_profiles_dialog()
                if self.active_device_profile is None:
                    self.energyflow_view.set_status_text(tr("Choose an active device profile to start."))
                    self.statusBar().showMessage(tr("Choose an active device profile to start."))
                    pass
                    return
        self.energyflow_refresh_timer.stop()
        self.energyflow_countdown_timer.stop()
        self._energyflow_start_timers_after_refresh = True
        self._energyflow_status_override = "Loading"
        self._reset_energyflow_countdown()
        self.statusBar().showMessage(tr("Refreshing current EnergyFlow snapshot..."))
        self._set_energyflow_refresh_active(True)
        pass
        self._refresh_energyflow_snapshot(force=True, manual=True, start_timer_after=True)

    def _start_manual_energyflow_refresh(self) -> None:
        if self._close_requested:
            self._energyflow_refresh_pending = False
            self._set_energyflow_refresh_active(False)
            return
        self._refresh_energyflow_snapshot(force=True, manual=True, start_timer_after=True)

    def _handle_energyflow_timer_timeout(self) -> None:
        self._refresh_energyflow_snapshot()

    def _begin_energyflow_refresh_cycle(self) -> None:
        self.energyflow_refresh_timer.stop()
        self.energyflow_countdown_timer.stop()
        if self._energyflow_refresh_inflight:
            self._energyflow_start_timers_after_refresh = True
            self._energyflow_status_override = "Loading"
            self._update_energyflow_countdown_text()
            return
        self._refresh_energyflow_snapshot(force=True, start_timer_after=True)

    def _refresh_energyflow_snapshot(
        self,
        *,
        force: bool = False,
        manual: bool = False,
        start_timer_after: bool = False,
        show_refresh_loader: bool = True,
    ) -> None:
        pass
        if self._energyflow_refresh_inflight:
            thread = self._energyflow_thread
            if thread is None or not thread.is_alive():
                self._clear_energyflow_thread()
            else:
                pass
                return
        if not force and self.tabs.currentWidget() is not self.energyflow_tab:
            pass
            return
        try:
            config = self._build_energyflow_config()
        except DessMonitorApiError as exc:
            self.energyflow_view.set_status_text(str(exc))
            pass
            self._energyflow_refresh_pending = False
            self._energyflow_status_override = None
            if show_refresh_loader:
                self._set_energyflow_refresh_active(False)
            self._update_energyflow_countdown_text()
            return

        self._energyflow_refresh_inflight = True
        self._energyflow_refresh_pending = False
        self._energyflow_refresh_started_monotonic = time.monotonic()
        self._energyflow_manual_refresh_active = manual
        self._energyflow_start_timers_after_refresh = start_timer_after
        self._energyflow_status_override = "Loading"
        if show_refresh_loader:
            self._set_energyflow_refresh_active(True)
        self._energyflow_request_id += 1
        request_id = self._energyflow_request_id
        self._energyflow_timeout_request_id = request_id
        self._energyflow_request_timeout_timer.start(ENERGYFLOW_REQUEST_TIMEOUT_MS)
        pass
        thread = threading.Thread(
            target=self._run_energyflow_snapshot_fetch,
            args=(request_id, config),
            daemon=True,
            name=f"energyflow-fetch-{request_id}",
        )
        self._energyflow_thread = thread
        thread.start()
        if manual:
            self._reset_energyflow_countdown()
        else:
            self._update_energyflow_countdown_text()

    def _handle_energyflow_request_timeout_tick(self) -> None:
        self._handle_energyflow_request_timeout(self._energyflow_timeout_request_id)

    def _handle_energyflow_request_timeout(self, request_id: int) -> None:
        if self._close_requested:
            return
        if not self._energyflow_refresh_inflight:
            return
        if request_id != self._energyflow_request_id:
            return
        self._handle_energyflow_error(
            request_id,
            tr("EnergyFlow request timed out. Please retry or check DessMonitor/API connectivity."),
        )

    def _run_energyflow_snapshot_fetch(self, request_id: int, config: DessMonitorConfig) -> None:
        try:
            profile = self.active_device_profile
            profile_fields = profile.resolved_inverter_control_fields() if profile is not None else []
            settings = (
                list(self._latest_inverter_settings)
                if self._latest_inverter_settings_key == self._active_profile_key()
                else []
            )
            snapshot = fetch_energy_flow_snapshot(
                config,
                profile_fields=profile_fields,
                inverter_settings=settings,
            )
            primary_values = (
                snapshot.pv_power,
                snapshot.grid_power,
                snapshot.load_power,
                snapshot.battery_power,
                snapshot.pv_voltage,
                snapshot.grid_voltage,
                snapshot.load_voltage,
                snapshot.battery_voltage,
                snapshot.load_frequency,
                snapshot.grid_frequency,
            )
            has_primary_data = False
            for value in primary_values:
                if value is None:
                    continue
                if isinstance(value, (int, float)):
                    if not math.isfinite(float(value)):
                        continue
                    has_primary_data = True
                    break
                has_primary_data = True
                break
            if not has_primary_data:
                raise RuntimeError(tr("EnergyFlow returned an empty snapshot (all primary values are missing)."))
            weather_snapshot: WeatherLiveSnapshot | None = None
            weather_status = "no_coords"
            weather_error = "Weather coordinates are missing."
            weather_latitude: str | None = None
            weather_longitude: str | None = None
            weather_timezone = "Europe/Kiev"
            try:
                location = fetch_device_location(config)
            except DessMonitorApiError as exc:
                self._latest_device_location_report = f"Failed to load device coordinates: {exc}"
                location = None
            else:
                if location is None:
                    self._latest_device_location_report = (
                        "DessMonitor did not return coordinates for this device or related project."
                    )
                else:
                    formatted_address = location.formatted_address() or "Address not specified"
                    self._latest_device_location_report = (
                        f"PID: {location.pid or '-'}\n"
                        f"Latitude: {location.latitude or '-'}\n"
                        f"Longitude: {location.longitude or '-'}\n"
                        f"Timezone: {location.timezone or '-'}\n"
                        f"Address: {formatted_address}"
                    )
                    if location.latitude and location.longitude:
                        weather_latitude = location.latitude.strip()
                        weather_longitude = location.longitude.strip()
                    if location.timezone and location.timezone.strip():
                        weather_timezone = location.timezone.strip()
            if profile is not None and self._active_profile_has_weather_location():
                weather_latitude = profile.inverter_latitude.strip()
                weather_longitude = profile.inverter_longitude.strip()
                weather_timezone = "Europe/Kiev"
            if weather_latitude and weather_longitude:
                weather_status = "fetching"
                weather_error = ""
                try:
                    weather_snapshot = fetch_weather_live_snapshot(
                        WeatherLiveRequest(
                            latitude=weather_latitude,
                            longitude=weather_longitude,
                            timezone=weather_timezone,
                            location_label=(
                                (profile.inverter_location_description.strip() if profile is not None else "")
                                or (profile.inverter_location_label.strip() if profile is not None else "")
                            ),
                        )
                    )
                    weather_status = "live"
                except WeatherApiError as exc:
                    weather_snapshot = None
                    weather_status = "failed"
                    weather_error = str(exc)
        except Exception as exc:
            if not self._close_requested:
                self._energyflow_bridge.failed.emit(request_id, str(exc))
            return
        if not self._close_requested:
            self._energyflow_bridge.finished.emit(
                request_id,
                {
                    "snapshot": snapshot,
                    "weather": weather_snapshot,
                    "weather_status": weather_status,
                    "weather_error": weather_error,
                },
            )

    def _apply_energyflow_snapshot(self, request_id: int, payload: object) -> None:
        pass
        if request_id != self._energyflow_request_id or self._close_requested:
            pass
            return
        try:
            snapshot: EnergyFlowSnapshot
            weather_snapshot: WeatherLiveSnapshot | None = None
            weather_status = "unknown"
            weather_error = ""
            if isinstance(payload, dict):
                snapshot = payload.get("snapshot")  # type: ignore[assignment]
                weather_snapshot = payload.get("weather") if isinstance(payload.get("weather"), WeatherLiveSnapshot) else None
                weather_status = str(payload.get("weather_status") or "unknown").strip().lower()
                weather_error = str(payload.get("weather_error") or "").strip()
            else:
                snapshot = payload  # type: ignore[assignment]
            if self._profile_activation_in_progress:
                self._set_progress(30, tr("Updating EnergyFlow"))
                self._append_log(tr("EnergyFlow snapshot loaded successfully."))
            effective_weather = weather_snapshot
            if effective_weather is None:
                effective_weather = self._last_weather_snapshot
                if effective_weather is not None:
                    self._weather_live_state = "cached"
                    self._weather_live_error = weather_error
                else:
                    self._weather_live_state = weather_status if weather_status != "fetching" else "failed"
                    self._weather_live_error = weather_error or "No weather snapshot available."
            else:
                self._last_weather_snapshot = effective_weather
                self._weather_live_state = "live"
                self._weather_live_error = ""
            pass
            self._last_energyflow_snapshot = snapshot
            self._last_energyflow_snapshot_key = self._active_profile_key()
            self._last_energyflow_updated_at_text = pd.Timestamp.now().strftime("%H:%M:%S")
            self.energyflow_summary_label.setText(self._build_energyflow_summary(snapshot))
            self._set_tab_message(
                self.energyflow_meta_label,
                tr_fragment(
                    f"Last updated {self._last_energyflow_updated_at_text} • Source: DessMonitor + Weather • Weather: {self._localized_weather_state()}"
                ),
                error=False,
            )
            self._update_energyflow_status_bar()
            try:
                self.energyflow_view.set_snapshot(snapshot, effective_weather)
            except Exception as exc:
                self._append_log(f"EnergyFlow canvas update warning: {exc}")
                pass
            self._reset_energyflow_countdown()
            pass
            self._clear_energyflow_thread()
        except Exception as exc:
            self._append_log(f"EnergyFlow UI update warning: {exc}")
            pass
            self._clear_energyflow_thread()
        finally:
            if self._profile_activation_in_progress:
                self._finish_profile_activation()

    def _schedule_profile_activation_energyflow_retry(self, message: str) -> bool:
        if not _is_profile_activation_retryable_energyflow_error(message):
            return False
        if self._profile_activation_energyflow_retry_count >= len(PROFILE_ACTIVATION_ENERGYFLOW_RETRY_DELAYS_MS):
            return False
        delay_ms = PROFILE_ACTIVATION_ENERGYFLOW_RETRY_DELAYS_MS[self._profile_activation_energyflow_retry_count]
        self._profile_activation_energyflow_retry_count += 1
        attempt = self._profile_activation_energyflow_retry_count
        total = len(PROFILE_ACTIVATION_ENERGYFLOW_RETRY_DELAYS_MS)
        delay_text = f"{delay_ms / 1000:.1f} s"
        self._append_log(
            tr("EnergyFlow request timed out during profile load. Retrying in {delay_text} ({attempt}/{total}).").format(
                delay_text=delay_text,
                attempt=attempt,
                total=total,
            )
        )
        self._set_progress(
            22,
            tr("Retrying EnergyFlow ({attempt}/{total})").format(attempt=attempt, total=total),
        )
        self._energyflow_status_override = "Loading"
        self._update_energyflow_countdown_text()
        self._profile_activation_retry_timer.start(delay_ms)
        return True

    def _retry_profile_activation_energyflow_snapshot(self) -> None:
        if self._close_requested or not self._profile_activation_in_progress:
            return
        self._append_log(tr_fragment("Retrying current EnergyFlow snapshot for the selected profile."))
        self._refresh_energyflow_snapshot(force=True, start_timer_after=True, show_refresh_loader=False)

    def _reset_profile_activation_retry_state(self) -> None:
        self._profile_activation_energyflow_retry_count = 0
        self._profile_activation_retry_timer.stop()

    def _handle_energyflow_error(self, request_id: int, message: str) -> None:
        pass
        if request_id != self._energyflow_request_id or self._close_requested:
            pass
            return
        if self._last_weather_snapshot is not None:
            self.energyflow_view.set_weather_snapshot(self._last_weather_snapshot)
            self._weather_live_state = "cached"
            self._weather_live_error = message
        else:
            self._weather_live_state = "failed"
            self._weather_live_error = message
        self.energyflow_view.set_status_text(message)
        self.energyflow_summary_label.setText("Live snapshot unavailable.")
        self._set_tab_message(self.energyflow_meta_label, message, error=True)
        self._set_energyflow_refresh_button_label("Error")
        self._clear_energyflow_thread()
        if self._profile_activation_in_progress:
            if self._schedule_profile_activation_energyflow_retry(message):
                return
            self._reset_profile_activation_retry_state()
            self._profile_activation_in_progress = False
            self._profile_activation_restore_saved_import = False
            if _is_profile_activation_retryable_energyflow_error(message):
                self._append_log(
                    f"Profile load finished with warning: {message}. Profile remains active; data will refresh in background."
                )
                self.progress_popup.stop()
                self.statusBar().showMessage(tr("Profile is active. EnergyFlow timed out; background refresh will continue."))
                self._resume_energyflow_auto_cycle()
                QTimer.singleShot(
                    5000,
                    lambda: self._refresh_energyflow_snapshot(
                        force=True,
                        start_timer_after=True,
                        show_refresh_loader=False,
                    ),
                )
                return
            self._append_log(f"Profile load failed: {message}")
            self.progress_popup.finish_error(message)
            self._resume_energyflow_auto_cycle()

    def _clear_energyflow_thread(self) -> None:
        pass
        self._energyflow_request_timeout_timer.stop()
        self._energyflow_thread = None
        self._energyflow_worker = None
        self._energyflow_refresh_inflight = False
        self._energyflow_refresh_started_monotonic = None
        self._energyflow_status_override = None
        should_run_refresh_cycle = self._energyflow_auto_refresh_enabled and self._can_run_energyflow_auto_cycle()
        self._energyflow_start_timers_after_refresh = False
        if should_run_refresh_cycle:
            self.energyflow_refresh_timer.start()
            if not self.energyflow_countdown_timer.isActive():
                self.energyflow_countdown_timer.start()
            self._reset_energyflow_countdown()
        else:
            self.energyflow_refresh_timer.stop()
            self.energyflow_countdown_timer.stop()
            self._update_energyflow_countdown_text()
        self._energyflow_manual_refresh_active = False
        self._set_energyflow_refresh_active(False)
        pass

    def _reset_energyflow_countdown(self) -> None:
        self._energyflow_refresh_remaining = max(1, self.energyflow_refresh_timer.interval() // 1000)
        self._update_energyflow_countdown_text()

    def _apply_energyflow_refresh_interval(self, profile: DeviceProfile | None) -> None:
        interval_seconds = (
            profile.resolved_energyflow_refresh_seconds()
            if profile is not None
            else DEFAULT_ENERGYFLOW_REFRESH_SECONDS
        )
        self.energyflow_refresh_timer.setInterval(interval_seconds * 1000)
        self._reset_energyflow_countdown()

    def _apply_auto_sync_interval(self, profile: DeviceProfile | None) -> None:
        self._auto_sync_timer.stop()
        if profile is None or not profile.auto_sync_enabled or self._close_requested:
            self._set_auto_sync_active(False)
            return
        self._auto_sync_timer.start()

    def _set_auto_sync_active(self, active: bool) -> None:
        self._auto_sync_inflight = active
        if hasattr(self, "device_profile_button"):
            self.device_profile_button.setSyncActive(active)
        self._refresh_tab_badges()

    def _active_profile_metadata(self) -> dict[str, object] | None:
        profile = self.active_device_profile
        if profile is None:
            return None
        return {
            "provider": "dessmonitor",
            "source": profile.source,
            "pn": profile.pn,
            "devcode": profile.devcode,
            "devaddr": profile.devaddr,
            "sn": profile.sn,
            "device_label": profile.device_label,
        }

    def _active_profile_has_weather_location(self) -> bool:
        profile = self.active_device_profile
        if profile is None:
            return False
        return bool(profile.inverter_latitude.strip() and profile.inverter_longitude.strip())

    def _load_active_profile_weather_history_dataframe(self) -> pd.DataFrame | None:
        dessmonitor_metadata = self._active_profile_metadata()
        if dessmonitor_metadata is None:
            return None
        weather_metadata = self._weather_history_metadata(dessmonitor_metadata=dessmonitor_metadata)
        if weather_metadata is None:
            return None
        import_id = latest_import_id_for_metadata(weather_metadata)
        if import_id is None:
            return None
        dataframe = load_import_dataframe(import_id)
        if dataframe.empty:
            return None
        return dataframe

    def _weather_history_metadata(
        self,
        *,
        dessmonitor_metadata: dict[str, object],
    ) -> dict[str, object] | None:
        profile = self.active_device_profile
        if profile is None or not self._active_profile_has_weather_location():
            return None
        return {
            "provider": "weather",
            "weather_provider": DEFAULT_WEATHER_PROVIDER,
            "series_type": "history",
            "source": f"Weather History {profile.device_label or profile.profile_name or 'Device'}",
            "source_dataset_provider": "dessmonitor",
            "pn": dessmonitor_metadata.get("pn", profile.pn),
            "devcode": dessmonitor_metadata.get("devcode", profile.devcode),
            "devaddr": dessmonitor_metadata.get("devaddr", profile.devaddr),
            "sn": dessmonitor_metadata.get("sn", profile.sn),
            "device_label": dessmonitor_metadata.get("device_label", profile.device_label),
            "latitude": profile.inverter_latitude.strip(),
            "longitude": profile.inverter_longitude.strip(),
            "timezone": "Europe/Kiev",
            "location_label": profile.inverter_location_description.strip() or profile.inverter_location_label.strip(),
            "hourly_fields": list(DEFAULT_WEATHER_HISTORY_FIELDS),
        }

    def _sync_weather_history_for_dataframe(
        self,
        dataframe: pd.DataFrame,
        *,
        dessmonitor_metadata: dict[str, object],
        open_saved_import_after_sync: bool = False,
    ) -> int | None:
        weather_metadata = self._weather_history_metadata(dessmonitor_metadata=dessmonitor_metadata)
        if weather_metadata is None:
            return None
        timestamp_column = next(
            (column for column in dataframe.columns if str(column).strip().lower() == "timestamp"),
            None,
        )
        if timestamp_column is None:
            return None
        timestamps = [
            str(value).strip()
            for value in dataframe[timestamp_column].tolist()
            if str(value).strip()
        ]
        if not timestamps:
            return None
        duplicate_timestamp_count = len(timestamps) - len(set(timestamps))
        if duplicate_timestamp_count > 0:
            self._append_log(
                f"DessMonitor timestamp grid contains {duplicate_timestamp_count} duplicate row(s); "
                "weather sync will deduplicate them automatically."
            )
        weather_request = WeatherDatasetRequest(
            latitude=str(weather_metadata.get("latitude", "")).strip(),
            longitude=str(weather_metadata.get("longitude", "")).strip(),
            timezone=str(weather_metadata.get("timezone", "")).strip(),
            location_label=str(weather_metadata.get("location_label", "")).strip(),
            timestamps=timestamps,
            hourly_fields=list(weather_metadata.get("hourly_fields", DEFAULT_WEATHER_HISTORY_FIELDS)),
        )
        self._append_log(
            "Syncing weather history from API for the DessMonitor timestamp grid..."
        )
        weather_dataframe = fetch_weather_history_dataset(weather_request)
        source_label = (
            f"Weather History {str(weather_metadata.get('device_label', '')).strip() or 'Device'} "
            f"({timestamps[0][:10]} to {timestamps[-1][:10]})"
        )
        import_id, _db_path = save_import_dataframe(
            weather_dataframe,
            source_label=source_label,
            metadata={"source": source_label, **weather_metadata},
        )
        self.saved_data_section.reload_records()
        self._append_log(f"Saved weather history dataset #{import_id}.")
        self._forecast_loaded_profile_key = None
        if hasattr(self, "tabs") and self.tabs.currentWidget() is self.forecast_tab:
            self._refresh_forecast(force=True)
        if open_saved_import_after_sync:
            self.open_saved_import(import_id)
        return import_id

    def _sync_weather_history_for_current_dataset(self) -> None:
        metadata = self.current_dataset_metadata or {}
        if str(metadata.get("provider", "")).strip().lower() == "weather":
            dessmonitor_metadata = {
                "provider": "dessmonitor",
                "pn": metadata.get("pn", ""),
                "devcode": metadata.get("devcode", ""),
                "devaddr": metadata.get("devaddr", ""),
                "sn": metadata.get("sn", ""),
                "device_label": metadata.get("device_label", ""),
            }
            import_id = latest_import_id_for_metadata(dessmonitor_metadata)
            if import_id is None:
                self._show_message(
                    QMessageBox.Icon.Information,
                    "Weather History",
                    "No matching DessMonitor dataset was found for this weather history record.",
                )
                return
            dataframe = load_import_dataframe(import_id)
            if dataframe.empty:
                self._show_message(
                    QMessageBox.Icon.Information,
                    "Weather History",
                    "The matching DessMonitor dataset does not contain any rows.",
                )
                return
            self._begin_load("Weather history sync", summary="Syncing weather history for the current device.")
            try:
                self._sync_weather_history_for_dataframe(
                    dataframe,
                    dessmonitor_metadata=dessmonitor_metadata,
                    open_saved_import_after_sync=True,
                )
                self.progress_popup.stop()
            except (WeatherApiError, DataProcessorError, RuntimeError) as exc:
                self._handle_load_error(exc)
            return

        self._show_message(
            QMessageBox.Icon.Information,
            "Update from API",
            "Weather history sync is available only for saved Weather History datasets.",
        )

    def import_weather_history(self) -> None:
        profile = self.active_device_profile
        if profile is None:
            self._show_message(
                QMessageBox.Icon.Information,
                "Import Weather",
                "Choose an active profile first.",
            )
            return
        if not self._active_profile_has_weather_location():
            self._show_message(
                QMessageBox.Icon.Warning,
                "Import Weather",
                "Save inverter coordinates in the profile before importing weather history.",
            )
            return
        dessmonitor_metadata = self._active_profile_metadata()
        if dessmonitor_metadata is None:
            self._show_message(
                QMessageBox.Icon.Information,
                "Import Weather",
                "No active DessMonitor profile is available.",
            )
            return
        import_id = latest_import_id_for_metadata(dessmonitor_metadata)
        if import_id is None:
            self._show_message(
                QMessageBox.Icon.Information,
                "Import Weather",
                "Import DessMonitor data first so weather history can use the same timestamps.",
            )
            return
        dataframe = load_import_dataframe(import_id)
        if dataframe.empty:
            self._show_message(
                QMessageBox.Icon.Information,
                "Import Weather",
                "The saved DessMonitor dataset does not contain any rows.",
            )
            return
        self._begin_load(
            "Weather history import",
            summary="Importing weather history for the active DessMonitor dataset.",
        )
        try:
            self._sync_weather_history_for_dataframe(
                dataframe,
                dessmonitor_metadata=dessmonitor_metadata,
                open_saved_import_after_sync=True,
            )
            self.progress_popup.stop()
        except (WeatherApiError, DataProcessorError, RuntimeError) as exc:
            self._handle_load_error(exc)

    def _filter_dataframe_to_date_range(
        self,
        dataframe: pd.DataFrame,
        *,
        date_from: str,
        date_to: str,
    ) -> pd.DataFrame:
        timestamp_column = next(
            (column for column in dataframe.columns if str(column).strip().lower() == "timestamp"),
            None,
        )
        if timestamp_column is None or dataframe.empty:
            return dataframe
        timestamps = pd.to_datetime(dataframe[timestamp_column], errors="coerce")
        start = pd.Timestamp(date_from).normalize()
        end = pd.Timestamp(date_to).normalize() + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
        mask = timestamps.notna() & (timestamps >= start) & (timestamps <= end)
        return dataframe.loc[mask].reset_index(drop=True)

    def _import_weather_history_for_config(
        self,
        config: DessMonitorConfig,
        *,
        open_saved_import_after_sync: bool,
    ) -> None:
        if not self._active_profile_has_weather_location():
            self._show_message(
                QMessageBox.Icon.Warning,
                "Weather History",
                "Save inverter coordinates in the profile before importing weather history.",
            )
            return
        dessmonitor_metadata = {
            "provider": "dessmonitor",
            "pn": config.pn,
            "devcode": config.devcode,
            "devaddr": config.devaddr,
            "sn": config.sn,
            "device_label": config.device_label,
        }
        import_id = latest_import_id_for_metadata(dessmonitor_metadata)
        if import_id is None:
            self._show_message(
                QMessageBox.Icon.Information,
                "Weather History",
                "Import DessMonitor data first so weather history can use the same timestamps.",
            )
            return
        dataframe = load_import_dataframe(import_id)
        dataframe = self._filter_dataframe_to_date_range(
            dataframe,
            date_from=config.date_from,
            date_to=config.date_to,
        )
        if dataframe.empty:
            self._show_message(
                QMessageBox.Icon.Information,
                "Weather History",
                "No saved DessMonitor rows were found for the selected sync period.",
            )
            return
        self._begin_load(
            "Weather history import",
            summary="Importing weather history for the selected sync range.",
        )
        try:
            self._sync_weather_history_for_dataframe(
                dataframe,
                dessmonitor_metadata=dessmonitor_metadata,
                open_saved_import_after_sync=open_saved_import_after_sync,
            )
            self.progress_popup.stop()
        except (WeatherApiError, DataProcessorError, RuntimeError) as exc:
            self._handle_load_error(exc)

    def _handle_saved_data_update_requested(self) -> None:
        self.open_api_update_dialog()

    def _handle_inverter_settings_refresh_click(self) -> None:
        self._load_inverter_settings_if_needed(force=True)

    def _handle_inverter_setting_edit_requested(self, field_id: str) -> None:
        if self._inverter_edit_sync_inflight:
            return
        profile = self.active_device_profile
        if profile is None:
            return

        current = self.inverter_settings_tab.setting_by_field_id(field_id)
        if current is None:
            self._show_message(
                QMessageBox.Icon.Warning,
                "Inverter settings",
                "The selected parameter is not available anymore. Refresh the list and try again.",
            )
            return

        self._inverter_edit_sync_inflight = True
        self._inverter_edit_sync_field_id = field_id
        self._refresh_tab_badges()
        self.inverter_settings_tab.set_edit_buttons_enabled(False)
        self._show_inverter_edit_sync_dialog(current.display_name)

        def run_sync() -> None:
            refreshed = current
            error_text: str | None = None
            try:
                config = self._build_inverter_settings_config()
                raw_value = fetch_device_control_value(config, field_id, force_refresh=True)
                refreshed = InverterSetting(
                    field_id=current.field_id,
                    name=current.name,
                    display_name=current.display_name,
                    raw_value=raw_value,
                    display_value=self._format_setting_value(raw_value, current.options),
                    unit=current.unit,
                    category=current.category,
                    writable=current.writable,
                    options=current.options,
                    hint=current.hint,
                    raw_name=current.raw_name,
                )
            except DessMonitorApiError as exc:
                error_text = humanize_error_text(str(exc))

            self.inverter_edit_sync_finished.emit(field_id, refreshed, error_text)

        thread = threading.Thread(target=run_sync, daemon=True, name=f"inverter-edit-sync-{field_id}")
        thread.start()

    def _show_inverter_edit_sync_dialog(self, setting_name: str) -> None:
        self._close_inverter_edit_sync_dialog()
        localized_setting_name = tr_fragment(setting_name)
        summary = (
            tr("Parameter: {setting_name}").format(setting_name=localized_setting_name)
            + "\n"
            + tr("Action: Synchronizing the latest value from API")
        )
        self.inverter_sync_popup.start(tr("Starting parameter sync"), summary=summary)
        self.inverter_sync_popup.update_status(
            10,
            tr("Preparing API sync for {setting_name}").format(setting_name=localized_setting_name),
        )
        self.inverter_sync_popup.append_log(
            tr("Sync requested for parameter: {setting_name}").format(setting_name=localized_setting_name)
        )
        self._inverter_edit_sync_dialog = self.inverter_sync_popup
        self.inverter_sync_popup.raise_()
        self.inverter_sync_popup.activateWindow()
        QTimer.singleShot(0, self.inverter_sync_popup.update)

    def _close_inverter_edit_sync_dialog(self) -> None:
        dialog = self._inverter_edit_sync_dialog
        self._inverter_edit_sync_dialog = None
        if dialog is None or not isValid(dialog):
            return
        try:
            if isinstance(dialog, ImportProgressDialog):
                dialog.stop()
            else:
                dialog.close()
        except RuntimeError as exc:
            LOGGER.warning("Failed to close inverter edit sync dialog: %s", exc)
            return

    def _on_inverter_edit_sync_finished(self, field_id: str, refreshed: InverterSetting, error_text: object) -> None:
        self._finish_inverter_setting_edit_sync(
            field_id=field_id,
            refreshed=refreshed,
            error_text=str(error_text).strip() if error_text else None,
        )

    def _finish_inverter_setting_edit_sync(
        self,
        *,
        field_id: str,
        refreshed: InverterSetting,
        error_text: str | None,
    ) -> None:
        if not self._inverter_edit_sync_inflight or self._inverter_edit_sync_field_id != field_id:
            return
        if self._inverter_edit_sync_dialog is self.inverter_sync_popup:
            if error_text:
                self.inverter_sync_popup.append_log(
                    tr("Sync failed: {error_text}").format(error_text=tr_fragment(error_text))
                )
                self.inverter_sync_popup.update_status(100, tr("Sync completed with warning"))
            else:
                self.inverter_sync_popup.append_log(tr("Sync completed successfully."))
                self.inverter_sync_popup.update_status(100, tr("Sync completed"))
        self._close_inverter_edit_sync_dialog()
        self._inverter_edit_sync_inflight = False
        self._inverter_edit_sync_field_id = None
        self._refresh_tab_badges()
        self.inverter_settings_tab.set_edit_buttons_enabled(True)

        # First sync the latest API value into in-memory state and table/cache,
        # then show the edit dialog on the next UI tick.
        self._replace_latest_inverter_setting(refreshed)
        if error_text:
            self.statusBar().showMessage(tr_fragment(f"Could not refresh current value before edit: {error_text}"))
        QTimer.singleShot(0, lambda: self.inverter_settings_tab.open_edit_dialog(refreshed))

    @staticmethod
    def _format_setting_value(raw_value: str | int | float | None, options: tuple[tuple[str, str], ...]) -> str:
        if raw_value is None:
            return "Not available"
        raw_text = str(raw_value).strip()
        for value, label in options:
            if value == raw_text:
                return label
        return raw_text or "Not available"

    def _replace_latest_inverter_setting(self, updated: InverterSetting) -> None:
        for index, item in enumerate(self._latest_inverter_settings):
            if item.field_id == updated.field_id:
                self._latest_inverter_settings[index] = updated
                break
        else:
            self._latest_inverter_settings.append(updated)

        active_key = self._active_profile_key()
        if active_key is None:
            return
        loaded_at = pd.Timestamp.now().strftime("%d.%m.%Y %H:%M")
        self._inverter_settings_cache[active_key] = (list(self._latest_inverter_settings), loaded_at)
        profile = self.active_device_profile
        if profile is not None:
            self._persist_inverter_settings_cache(profile, list(self._latest_inverter_settings), loaded_at)
        if profile is not None:
            self.inverter_settings_tab.set_settings(profile, list(self._latest_inverter_settings), loaded_at)

    def _handle_inverter_setting_cached(self, updated: object) -> None:
        if not isinstance(updated, InverterSetting):
            return
        self._replace_latest_inverter_setting(updated)
        self.statusBar().showMessage(tr_fragment(f"Cached local value for {updated.display_name}."))

    def _load_inverter_settings_if_needed(self, *, force: bool) -> None:
        profile = self.active_device_profile
        if profile is None:
            self.inverter_settings_tab.set_profile(None)
            return
        if self._settings_thread is not None:
            return
        if not force and self.inverter_settings_tab.has_loaded_settings_for(profile):
            return
        active_key = self._active_profile_key()
        if not force and active_key is not None:
            cached = self._inverter_settings_cache.get(active_key)
            if cached is None:
                cached = load_inverter_settings_cache(
                    profile_name=profile.profile_name,
                    pn=profile.pn,
                    devcode=profile.devcode,
                    devaddr=profile.devaddr,
                )
                if cached is not None:
                    self._inverter_settings_cache[active_key] = cached
            if cached is not None:
                self._latest_inverter_settings = list(cached[0])
                self._latest_inverter_settings_key = active_key
                self.inverter_settings_tab.set_settings(profile, list(cached[0]), cached[1])
                self.inverter_settings_tab.set_edit_buttons_enabled(True)
                return

        try:
            config = self._build_inverter_settings_config()
        except DessMonitorApiError as exc:
            self.inverter_settings_tab.set_error(profile, str(exc))
            return

        self._settings_profile_key = self._active_profile_key()
        self.inverter_settings_tab.set_loading(profile)
        self.inverter_settings_tab.set_edit_buttons_enabled(False)

        thread = QThread(self)
        worker = InverterSettingsWorker(
            config,
            force_refresh=force,
            profile_fields=profile.resolved_inverter_control_fields(),
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._set_settings_progress, Qt.ConnectionType.QueuedConnection)
        worker.log.connect(self._append_settings_log, Qt.ConnectionType.QueuedConnection)
        worker.setting_loading.connect(self._set_inverter_setting_loading, Qt.ConnectionType.QueuedConnection)
        worker.setting_loaded.connect(self._append_inverter_setting_row)
        worker.finished.connect(self._finish_inverter_settings_load)
        worker.failed.connect(self._handle_inverter_settings_error)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_inverter_settings_thread)

        self._settings_thread = thread
        self._settings_worker = worker
        self._refresh_tab_badges()
        thread.start()

    def _finish_inverter_settings_load(self, settings: list[InverterSetting]) -> None:
        profile = self.active_device_profile
        expected_key = self._settings_profile_key
        if profile is None or expected_key != self._active_profile_key():
            return
        self._latest_inverter_settings = list(settings)
        self._latest_inverter_settings_key = expected_key
        loaded_at = pd.Timestamp.now().strftime("%d.%m.%Y %H:%M")
        if expected_key is not None:
            self._inverter_settings_cache[expected_key] = (list(settings), loaded_at)
        self._persist_inverter_settings_cache(profile, list(settings), loaded_at)
        self.inverter_settings_tab.set_settings(profile, settings, loaded_at)
        self.inverter_settings_tab.set_edit_buttons_enabled(True)

    def _persist_inverter_settings_cache(
        self,
        profile: DeviceProfile,
        settings: list[InverterSetting],
        loaded_at: str,
    ) -> None:
        try:
            save_inverter_settings_cache(
                profile_name=profile.profile_name,
                pn=profile.pn,
                devcode=profile.devcode,
                devaddr=profile.devaddr,
                settings=settings,
                loaded_at=loaded_at,
            )
        except Exception as exc:
            self._append_log(f"Warning: failed to persist inverter cache to disk: {exc}")

    def _handle_inverter_settings_error(self, message: str) -> None:
        self.inverter_settings_tab.set_error(self.active_device_profile, message)
        self.inverter_settings_tab.set_edit_buttons_enabled(True)
        self.statusBar().showMessage(tr_fragment(f"Inverter settings refresh failed: {message}"))

    def _append_inverter_setting_row(self, setting: InverterSetting) -> None:
        profile = self.active_device_profile
        expected_key = self._settings_profile_key
        if profile is None or expected_key != self._active_profile_key():
            return
        self.inverter_settings_tab.add_setting(profile, setting)

    def _set_inverter_setting_loading(self, field_id: str, display_name: str) -> None:
        del display_name
        profile = self.active_device_profile
        expected_key = self._settings_profile_key
        if profile is None or expected_key != self._active_profile_key():
            return
        self.inverter_settings_tab.set_active_loading_field(field_id)

    def _clear_inverter_settings_thread(self) -> None:
        self._settings_thread = None
        self._settings_worker = None
        self._settings_profile_key = None
        self._refresh_tab_badges()
        if (
            not self._close_requested
            and hasattr(self, "tabs")
            and self.tabs.currentWidget() is self.inverter_settings_tab
            and self.active_device_profile is not None
            and not self.inverter_settings_tab.has_loaded_settings_for(self.active_device_profile)
        ):
            QTimer.singleShot(0, lambda: self._load_inverter_settings_if_needed(force=True))

    def _set_settings_progress(self, value: int, message: str) -> None:
        self.inverter_settings_tab.append_progress(message)

    def _append_settings_log(self, message: str) -> None:
        self.saved_data_section.append_activity(message)

    def _handle_auto_sync_timer_timeout(self) -> None:
        self._start_background_auto_sync()

    def _start_background_auto_sync(self) -> None:
        profile = self.active_device_profile
        if (
            profile is None
            or not profile.auto_sync_enabled
            or self._close_requested
            or self._profile_activation_in_progress
            or self._import_thread is not None
            or self._auto_sync_inflight
        ):
            return

        metadata = self._active_profile_metadata()
        if metadata is None:
            return
        last_timestamp = latest_timestamp_for_metadata(metadata)
        if not last_timestamp:
            return

        start_day = (
            pd.Timestamp(last_timestamp) - pd.Timedelta(minutes=AUTO_SYNC_LOOKBACK_MINUTES)
        ).date().isoformat()
        today = pd.Timestamp.now().date().isoformat()
        config = DessMonitorConfig(
            username=profile.username,
            password=profile.password,
            company_key=profile.company_key,
            source=profile.source,
            pn=profile.pn,
            devcode=profile.devcode,
            devaddr=profile.devaddr,
            sn=profile.sn,
            device_label=profile.device_label or profile.profile_name,
            parameter_keys=profile.resolved_parameter_keys(),
            date_from=min(start_day, today),
            date_to=today,
            period_mode="custom",
        )
        self.load_dessmonitor_data(config, background=True, include_weather_sync=True)

    def _set_energyflow_refresh_active(self, active: bool) -> None:
        if self._has_visible_popup_dialogs():
            self._energyflow_refresh_spinner_timer.stop()
            self._energyflow_refresh_spinner_phase = 0
            return
        pass
        if hasattr(self, "energyflow_refresh_button"):
            if active:
                self._energyflow_refresh_spinner_phase = 0
                self.energyflow_refresh_button.setEnabled(False)
                self.energyflow_refresh_button.setText("Refreshing ⠋")
                self._energyflow_refresh_spinner_timer.start()
            else:
                self._energyflow_refresh_spinner_timer.stop()
                self._energyflow_refresh_spinner_phase = 0
                self.energyflow_refresh_button.setEnabled(self.active_device_profile is not None)
                self._set_energyflow_refresh_button_label()
            pass
        self._refresh_tab_badges()

    def _has_visible_popup_dialogs(self) -> bool:
        return any(self._is_visible_dialog(item) for item in self._open_popup_dialogs)

    def _tick_energyflow_refresh_spinner(self) -> None:
        if self._has_visible_popup_dialogs():
            return
        frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
        self._energyflow_refresh_spinner_phase = (self._energyflow_refresh_spinner_phase + 1) % len(frames)
        self.energyflow_refresh_button.setText(f"Refreshing {frames[self._energyflow_refresh_spinner_phase]}")
        if self._energyflow_refresh_spinner_phase == 0:
            pass

    def _tick_energyflow_countdown(self) -> None:
        if self.active_device_profile is None:
            return
        if not self._energyflow_auto_refresh_enabled:
            self.energyflow_refresh_timer.stop()
            self.energyflow_countdown_timer.stop()
            return
        if self._profile_activation_in_progress and not self._energyflow_refresh_inflight:
            self._update_energyflow_countdown_text()
            return
        if self._energyflow_refresh_inflight:
            thread = self._energyflow_thread
            if thread is None or not thread.is_alive():
                self._clear_energyflow_thread()
            elif self._energyflow_refresh_started_monotonic is not None:
                elapsed = time.monotonic() - self._energyflow_refresh_started_monotonic
                timeout_sec = (ENERGYFLOW_REQUEST_TIMEOUT_MS / 1000.0) + 20.0
                if elapsed > timeout_sec:
                    self._handle_energyflow_error(
                        self._energyflow_request_id,
                        tr("EnergyFlow request timed out. Please retry or check DessMonitor/API connectivity."),
                    )
                    return
            else:
                self._update_energyflow_countdown_text()
                return
        if self._energyflow_refresh_remaining > 0:
            self._energyflow_refresh_remaining -= 1
            self._update_energyflow_countdown_text()
            return
        if not self._energyflow_refresh_inflight:
            self._begin_energyflow_refresh_cycle()
            return
        self._update_energyflow_countdown_text()

    def _update_energyflow_countdown_text(self) -> None:
        self._reconcile_energyflow_refresh_state()
        if self._energyflow_refresh_pending:
            self._update_energyflow_status_bar()
            self.energyflow_view.set_status_text(tr("Loading"))
            return
        if self.active_device_profile is None:
            if self._try_recover_active_profile():
                self._begin_energyflow_refresh_cycle()
                return
            self.energyflow_summary_label.setText(tr("Waiting for a live snapshot."))
            self._set_tab_message(self.energyflow_meta_label, tr("Choose an active device profile to start."), error=False)
            self._set_energyflow_refresh_button_label("--:--")
            self._update_energyflow_status_bar()
            self.energyflow_view.set_status_text("")
            pass
            return
        if self._energyflow_status_override:
            if self._energyflow_refresh_inflight or self._energyflow_refresh_pending:
                self._update_energyflow_status_bar()
                self.energyflow_view.set_status_text(tr("Loading"))
                pass
                return
            self._energyflow_status_override = None
        minutes, seconds = divmod(max(self._energyflow_refresh_remaining, 0), 60)
        self._set_energyflow_refresh_button_label(f"{minutes:02d}:{seconds:02d}")
        self._update_energyflow_status_bar()
        self.energyflow_view.set_status_text("")

    def _set_energyflow_refresh_button_label(self, countdown_text: str | None = None) -> None:
        if not hasattr(self, "energyflow_refresh_button"):
            return
        if self._has_visible_popup_dialogs():
            return
        if self._energyflow_refresh_pending or self._energyflow_refresh_inflight:
            pass
            return
        if countdown_text:
            self.energyflow_refresh_button.setText(f"{tr('Refresh')} {countdown_text}")
        else:
            self.energyflow_refresh_button.setText(tr("Refresh"))
        pass

    def _update_energyflow_status_bar(self) -> None:
        if self.active_device_profile is None:
            self.statusBar().showMessage(tr("Choose an active device profile to start."))
            return

        summary = self.energyflow_summary_label.text().strip() if hasattr(self, "energyflow_summary_label") else ""
        meta = self.energyflow_meta_label.text().strip() if hasattr(self, "energyflow_meta_label") else ""
        parts = [part for part in (summary, meta) if part]
        self.statusBar().showMessage("    •    ".join(parts) if parts else tr("Waiting for a live snapshot."))

    def _localized_weather_state(self) -> str:
        state = str(self._weather_live_state or "unknown").strip().lower()
        if not state:
            state = "unknown"
        return tr(state)

    def _build_energyflow_summary(self, snapshot: EnergyFlowSnapshot) -> str:
        def _fmt_power(value: float | None) -> str:
            if value is None:
                return "--"
            return f"{int(round(value))} {tr('W')}"

        battery_value = "--"
        if snapshot.battery_power is not None:
            battery_state = tr("charge") if snapshot.battery_power > 0 else tr("discharge") if snapshot.battery_power < 0 else tr("idle")
            battery_value = f"{int(round(abs(snapshot.battery_power)))} {tr('W')} {battery_state}"

        return (
            f"{tr('PV')} {_fmt_power(snapshot.pv_power)} • "
            f"{tr('Grid')} {_fmt_power(snapshot.grid_power)} • "
            f"{tr('Battery')} {battery_value} • "
            f"{tr('Home')} {_fmt_power(snapshot.load_power)}"
        )

    def _prime_forecast_for_active_profile(self, *, build_if_missing: bool) -> None:
        profile = self.active_device_profile
        if profile is None:
            self.forecast_tab.set_unavailable("Choose an active device profile and sync DessMonitor data to generate a forecast.")
            self._forecast_loaded_profile_key = None
            return
        if not self._active_profile_has_weather_location():
            self.forecast_tab.set_unavailable("Save inverter coordinates in the profile to use weather-based forecasting.")
            self._forecast_loaded_profile_key = None
            return

        cached_snapshot = self._load_latest_pv_forecast_snapshot_for_active_profile()
        cached_result = self._build_forecast_result_from_snapshot(cached_snapshot)
        snapshot_is_fresh_today = self._is_forecast_snapshot_fresh_today(cached_snapshot)
        if cached_result is not None and snapshot_is_fresh_today:
            self._forecast_best_result = cached_result
            self._forecast_best_result_key = self._active_profile_key()
            self.forecast_tab.set_result(cached_result)
            self._forecast_loaded_profile_key = self._active_profile_key()
            self._trigger_forecast_actual_autorefresh_if_needed(cached_result, build_if_missing=build_if_missing)
            return

        self.forecast_tab.set_unavailable("Forecast is being prepared for this profile.")
        self._forecast_loaded_profile_key = None
        if not build_if_missing or self._forecast_refresh_inflight:
            return

        dessmonitor_metadata = {
            "provider": "dessmonitor",
            "pn": profile.pn,
            "devcode": profile.devcode,
            "devaddr": profile.devaddr,
            "sn": profile.sn,
            "device_label": profile.device_label,
        }
        if latest_import_id_for_metadata(dessmonitor_metadata) is None:
            self.forecast_tab.set_unavailable("Sync DessMonitor data first to generate a forecast.")
            return
        self._refresh_forecast(force=True)

    def _trigger_forecast_actual_autorefresh_if_needed(
        self,
        result: PvForecastResult,
        *,
        build_if_missing: bool,
    ) -> None:
        if not build_if_missing or self._forecast_refresh_inflight:
            return
        profile_key = self._active_profile_key()
        if profile_key is None:
            return
        now_local = pd.Timestamp.now(tz="Europe/Kiev").tz_localize(None)
        compare = result.compare_frame
        has_actual_points = bool(
            not compare.empty
            and "actual_pv_power_kw" in compare.columns
            and compare["actual_pv_power_kw"].notna().any()
        )
        if has_actual_points:
            return
        today_key = now_local.strftime("%Y-%m-%d")
        marker = (profile_key, today_key)
        if self._forecast_actual_autorefresh_marker == marker:
            return
        self._forecast_actual_autorefresh_marker = marker
        self.statusBar().showMessage(tr("Updating today actual PV values for forecast..."))
        self._refresh_forecast(force=True)

    @staticmethod
    def _is_forecast_snapshot_fresh_today(snapshot_frame: pd.DataFrame) -> bool:
        if snapshot_frame is None or snapshot_frame.empty:
            return False
        latest_run: pd.Timestamp | None = None
        if "forecast_run_at" in snapshot_frame.columns:
            run_values = pd.to_datetime(snapshot_frame["forecast_run_at"], errors="coerce")
            if not run_values.dropna().empty:
                latest_run = run_values.dropna().max()
        if latest_run is None and "Timestamp" in snapshot_frame.columns:
            ts_values = pd.to_datetime(snapshot_frame["Timestamp"], errors="coerce")
            if not ts_values.dropna().empty:
                latest_run = ts_values.dropna().max()
        if latest_run is None or pd.isna(latest_run):
            return False
        if latest_run.tzinfo is not None:
            latest_run = latest_run.tz_convert("Europe/Kiev").tz_localize(None)
        today = pd.Timestamp.now(tz="Europe/Kiev").tz_localize(None).normalize()
        return latest_run.normalize() == today

    def _initialize_startup_profile(self) -> None:
        profiles = load_device_profiles()
        last_loaded_name = load_last_loaded_profile_name().strip()
        if not profiles:
            self.open_device_profiles_dialog()
            self._maybe_offer_startup_sync()
            return
        profile: DeviceProfile | None = None
        if last_loaded_name:
            profile = next((item for item in profiles if item.profile_name == last_loaded_name), None)
        if profile is None:
            profile = profiles[0]
        self._activate_device_profile(profile, restore_saved_import=True, show_progress=True)

    def _try_recover_active_profile(self) -> bool:
        if self.active_device_profile is not None:
            return True
        if self._profile_recovery_attempted:
            return False
        self._profile_recovery_attempted = True

        profiles = load_device_profiles()
        if not profiles:
            return False
        last_loaded_name = load_last_loaded_profile_name().strip()
        profile = next((item for item in profiles if item.profile_name == last_loaded_name), None)
        if profile is None:
            profile = profiles[0]
        if profile is None:
            return False
        self._apply_device_profile(profile, start_energyflow=False)
        save_last_loaded_profile_name(profile.profile_name)
        return True

    def _activate_device_profile(
        self,
        profile: DeviceProfile,
        *,
        restore_saved_import: bool,
        show_progress: bool,
        tuya_refresh_now: bool = True,
        tuya_refresh_if_empty: bool = True,
    ) -> None:
        pass
        activation_signature = (
            profile.profile_name,
            profile.pn,
            profile.devcode,
            profile.devaddr,
            profile.sn,
            restore_saved_import,
            show_progress,
            tuya_refresh_now,
            tuya_refresh_if_empty,
        )
        now_monotonic = time.monotonic()
        if (
            self._last_profile_activation_signature == activation_signature
            and (now_monotonic - self._last_profile_activation_started_monotonic) < 5.0
        ):
            pass
            return
        self._last_profile_activation_signature = activation_signature
        self._last_profile_activation_started_monotonic = now_monotonic
        self._apply_device_profile(
            profile,
            start_energyflow=False,
            tuya_refresh_now=tuya_refresh_now,
            tuya_refresh_if_empty=tuya_refresh_if_empty,
        )
        self._reset_profile_activation_retry_state()
        if not show_progress:
            save_last_loaded_profile_name(profile.profile_name)
            self._begin_energyflow_refresh_cycle()
            if restore_saved_import:
                dataset_id = self._matching_saved_dataset_id(profile)
                if dataset_id is not None:
                    self.open_saved_import(dataset_id)
            return

        self._profile_activation_in_progress = True
        self._profile_activation_restore_saved_import = restore_saved_import
        self.saved_data_section.clear_activity()
        device_name = profile.device_label or profile.sn or profile.pn or "Selected device"
        device_value = _primary_device_name(device_name, profile.sn or profile.pn or "Selected device")
        details_text = (
            f"PN {profile.pn or '-'}"
            f" • addr {profile.devaddr or '-'}"
            f" • code {profile.devcode or '-'}"
        )
        self.progress_popup.start(
            tr("Loading profile"),
            summary=(
                f"Profile: {profile.profile_name}\n"
                f"Device: {device_value}\n"
                f"Details: {details_text}\n"
                "Action: Loading current EnergyFlow snapshot"
            ),
        )
        self._append_log(tr_fragment(f"Starting profile load for '{profile.profile_name}'."))
        self._set_progress(15, tr("Applying saved profile"))
        self._append_log(tr_fragment("Refreshing current EnergyFlow snapshot for the selected profile."))
        self._refresh_energyflow_snapshot(force=True, start_timer_after=True, show_refresh_loader=False)

    def _finish_profile_activation(self) -> None:
        pass
        profile = self.active_device_profile
        restore_saved_import = self._profile_activation_restore_saved_import
        self._reset_profile_activation_retry_state()
        self._profile_activation_in_progress = False
        self._profile_activation_restore_saved_import = False
        if profile is None:
            self.progress_popup.stop()
            self._maybe_offer_startup_sync()
            self._reconcile_energyflow_refresh_state()
            self._resume_energyflow_auto_cycle()
            pass
            return

        save_last_loaded_profile_name(profile.profile_name)
        dataset_id = self._matching_saved_dataset_id(profile) if restore_saved_import else None
        if dataset_id is None:
            self._energyflow_auto_refresh_ready = True
            self._set_progress(88, "Preparing forecast")
            self._prime_forecast_for_active_profile(build_if_missing=True)
            self._set_progress(100, "Profile ready")
            self._append_log("Profile loaded successfully.")
            self.progress_popup.stop()
            self._maybe_offer_startup_sync()
            self._reconcile_energyflow_refresh_state()
            self._resume_energyflow_auto_cycle()
            pass
            return

        self._set_progress(35, "Opening saved profile history")
        self._append_log(f"Opening saved dataset #{dataset_id} for profile '{profile.profile_name}'.")
        dataframe = load_import_dataframe(dataset_id)
        if dataframe.empty:
            self._energyflow_auto_refresh_ready = True
            self._set_progress(88, "Preparing forecast")
            self._prime_forecast_for_active_profile(build_if_missing=True)
            self._append_log(tr("The saved dataset is empty, so only EnergyFlow was refreshed."))
            self._set_progress(100, "Profile ready")
            self.progress_popup.stop()
            self._maybe_offer_startup_sync()
            self._reconcile_energyflow_refresh_state()
            self._resume_energyflow_auto_cycle()
            pass
            return

        record = next((item for item in list_saved_imports() if item.import_id == dataset_id), None)
        source_label = record.source_label if record is not None else f"Saved import #{dataset_id}"
        try:
            self._load_dataframe(
                dataframe,
                source_label,
                metadata=(record.metadata if record is not None else {"provider": "database"}),
                save_to_db=False,
            )
        except DataProcessorError as exc:
            self._handle_load_error(exc)
        finally:
            self._energyflow_auto_refresh_ready = True
            self._set_progress(88, "Preparing forecast")
            self._prime_forecast_for_active_profile(build_if_missing=True)
            self._set_progress(100, "Profile ready")
            self.progress_popup.stop()
            self._reconcile_energyflow_refresh_state()
            self._resume_energyflow_auto_cycle()
            pass

    def _maybe_offer_startup_sync(self) -> None:
        if self._startup_sync_offer_shown or self._close_requested:
            return
        if list_saved_imports():
            return
        self._startup_sync_offer_shown = True
        self.saved_data_section.reload_records()
        should_sync = ask_compact_confirmation(
            self,
            title="Local Database Is Empty",
            text=(
                "No saved telemetry was found in the local database yet.\n\n"
                "Would you like to open DessMonitor sync and import the full history now?"
            ),
            accept_text="Start sync",
            reject_text="Later",
        )
        if should_sync:
            if self.active_device_profile is not None:
                self.open_api_update_dialog()
            else:
                self.open_dessmonitor_dialog()

    def open_device_profiles_dialog(self) -> None:
        dialog = DeviceProfilesDialog(self)
        if self._run_popup_dialog_blocking(dialog) != int(dialog.DialogCode.Accepted):
            return

        profile = dialog.selected_profile()
        if profile is None:
            return
        self._activate_device_profile(profile, restore_saved_import=True, show_progress=True)

    def _current_profile_from_settings(self) -> DeviceProfile | None:
        profile = self.active_device_profile
        if profile is None:
            return None
        persisted_profile = next(
            (item for item in load_device_profiles() if item.profile_name == profile.profile_name),
            None,
        )
        if persisted_profile is not None:
            self.active_device_profile = persisted_profile
            self._profile_recovery_attempted = False
            return persisted_profile
        return profile

    def open_active_profile_editor(self) -> None:
        profile = self._current_profile_from_settings()
        if profile is None:
            self.open_device_profiles_dialog()
            return
        dialog = DeviceProfileEditDialog(self, profile=profile, persist_on_accept=True)

        def handle_profile_saved(updated_profile: DeviceProfile) -> None:
            persisted_profile = next(
                (item for item in load_device_profiles() if item.profile_name == updated_profile.profile_name),
                updated_profile,
            )
            previous_profile = self.active_device_profile
            tuya_changed = bool(
                previous_profile is None
                or previous_profile.tuya_enabled != persisted_profile.tuya_enabled
                or previous_profile.tuya_client_id != persisted_profile.tuya_client_id
                or previous_profile.tuya_client_secret != persisted_profile.tuya_client_secret
                or previous_profile.resolved_tuya_endpoint() != persisted_profile.resolved_tuya_endpoint()
            )
            should_restore_import = dialog.check_performed_in_session()
            if should_restore_import:
                self._activate_device_profile(
                    persisted_profile,
                    restore_saved_import=True,
                    show_progress=True,
                    tuya_refresh_now=tuya_changed,
                    tuya_refresh_if_empty=tuya_changed,
                )
            else:
                self._apply_device_profile(
                    persisted_profile,
                    start_energyflow=False,
                    tuya_refresh_now=tuya_changed,
                    tuya_refresh_if_empty=tuya_changed,
                )
            save_last_loaded_profile_name(persisted_profile.profile_name)

        dialog.profile_saved.connect(handle_profile_saved)
        self._run_popup_dialog_blocking(dialog)

    def open_file_dialog(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Open Excel File",
            "",
            "Excel Files (*.xlsx)",
        )
        if file_path:
            self.load_file(file_path)

    def open_dessmonitor_dialog(self) -> None:
        dialog = DessMonitorDialog(self, initial_state=self._profile_initial_state())
        if self._run_popup_dialog_blocking(dialog) != int(dialog.DialogCode.Accepted):
            return
        try:
            config = dialog.config()
        except DessMonitorApiError as exc:
            self._show_message(QMessageBox.Icon.Critical, "DessMonitor Error", str(exc))
            return
        include_dessmonitor, include_weather = dialog.selected_dataset_targets()
        self._sync_profile_from_config(config)
        if include_dessmonitor:
            self.load_dessmonitor_data(config, include_weather_sync=include_weather)
        elif include_weather:
            self._import_weather_history_for_config(
                config,
                open_saved_import_after_sync=True,
            )
        self.saved_data_section.reload_records()

    def open_settings_dialog(self) -> None:
        dialog = SettingsDialog(self)
        self._run_popup_dialog_blocking(dialog)

    def open_saved_data_tab(self) -> None:
        self.tabs.setCurrentWidget(self.saved_data_section)
        self.saved_data_section.reload_records()

    def open_api_update_dialog(self) -> None:
        latest_metadata = latest_dessmonitor_metadata() or {}
        profile_state = self._profile_initial_state()
        lookup_metadata = {
            "provider": "dessmonitor",
            "pn": profile_state.get("pn") or latest_metadata.get("pn", ""),
            "devcode": profile_state.get("devcode") or latest_metadata.get("devcode", ""),
            "devaddr": profile_state.get("devaddr") or latest_metadata.get("devaddr", ""),
            "sn": profile_state.get("sn") or latest_metadata.get("sn", ""),
        }
        last_timestamp = latest_timestamp_for_metadata(lookup_metadata) or latest_timestamp_in_database()
        initial_state: dict[str, object] = {
            "company_key": latest_metadata.get("company_key", ""),
            "source": latest_metadata.get("source", 1),
        }
        initial_state.update(self._profile_initial_state())
        if last_timestamp:
            initial_state["period"] = "from_last_saved"
            initial_state["date_from"] = str(last_timestamp)[:10]
            initial_state["date_to"] = pd.Timestamp.today().date().isoformat()
        else:
            initial_state["period"] = "all_data"
            initial_state["date_from"] = pd.Timestamp.today().date().isoformat()
            initial_state["date_to"] = pd.Timestamp.today().date().isoformat()
        for key in ("pn", "devcode", "devaddr", "sn"):
            if key in latest_metadata:
                initial_state[key] = latest_metadata[key]

        dialog = DessMonitorDialog(self, initial_state=initial_state)
        if self._run_popup_dialog_blocking(dialog) != int(dialog.DialogCode.Accepted):
            return
        try:
            config = dialog.config()
        except DessMonitorApiError as exc:
            self._show_message(QMessageBox.Icon.Critical, "DessMonitor Error", str(exc))
            return
        include_dessmonitor, include_weather = dialog.selected_dataset_targets()
        self._sync_profile_from_config(config)
        if include_dessmonitor:
            self.load_dessmonitor_data(config, include_weather_sync=include_weather)
            return
        if include_weather:
            self._import_weather_history_for_config(
                config,
                open_saved_import_after_sync=True,
            )
        self.saved_data_section.reload_records()

    def _sync_profile_from_config(self, config: DessMonitorConfig) -> None:
        if self.active_device_profile is None:
            return
        updated_profile = DeviceProfile(
            profile_name=self.active_device_profile.profile_name,
            username=config.username,
            password=config.password,
            company_key=config.company_key,
            source=config.source,
            pn=config.pn,
            devcode=config.devcode,
            devaddr=config.devaddr,
            sn=config.sn,
            device_label=config.device_label or self.active_device_profile.device_label,
            energyflow_refresh_seconds=self.active_device_profile.resolved_energyflow_refresh_seconds(),
            auto_sync_enabled=self.active_device_profile.auto_sync_enabled,
            available_parameter_keys=list(self.active_device_profile.available_parameter_keys or []),
            selected_parameter_keys=list(self.active_device_profile.selected_parameter_keys or []),
            inverter_control_fields=list(self.active_device_profile.inverter_control_fields or []),
            inverter_location_mode=self.active_device_profile.resolved_inverter_location_mode(),
            inverter_location_label=self.active_device_profile.inverter_location_label,
            inverter_location_description=self.active_device_profile.inverter_location_description,
            inverter_latitude=self.active_device_profile.inverter_latitude,
            inverter_longitude=self.active_device_profile.inverter_longitude,
            tuya_enabled=self.active_device_profile.tuya_enabled,
            tuya_client_id=self.active_device_profile.tuya_client_id,
            tuya_client_secret=self.active_device_profile.tuya_client_secret,
            tuya_endpoint=self.active_device_profile.resolved_tuya_endpoint(),
            tuya_poll_interval_sec=self.active_device_profile.resolved_tuya_poll_interval_sec(),
            battery_capability_ah=self.active_device_profile.resolved_battery_capability_ah(),
            ui_language=self.active_device_profile.resolved_ui_language(),
            day_zone_tariff_uah_per_kwh=self.active_device_profile.resolved_day_zone_tariff_uah_per_kwh(),
            night_zone_tariff_uah_per_kwh=self.active_device_profile.resolved_night_zone_tariff_uah_per_kwh(),
        )
        profiles = load_device_profiles()
        replaced = False
        for index, profile in enumerate(profiles):
            if profile.profile_name == updated_profile.profile_name:
                profiles[index] = updated_profile
                replaced = True
                break
        if not replaced:
            profiles.append(updated_profile)
        save_device_profiles(profiles)
        self.active_device_profile = updated_profile
        self._apply_ui_language(updated_profile.resolved_ui_language(), persist=True)
        self._profile_recovery_attempted = False
        self._apply_energyflow_refresh_interval(updated_profile)
        self._apply_auto_sync_interval(updated_profile)
        self._tuya_settings = self._tuya_settings_from_profile(updated_profile)
        self._apply_tuya_settings(refresh_now=True)
        self.inverter_settings_tab.set_profile(updated_profile)
        self.energyflow_view.set_device_meta(
            updated_profile.profile_name or updated_profile.device_label or "Inverter",
            updated_profile.pn,
            updated_profile.sn,
        )
        self.energyflow_view.set_battery_capability_ah(updated_profile.resolved_battery_capability_ah())
        self._sync_energyflow_tuya_icons()
        self._update_device_profile_button()
        if self.tabs.currentWidget() is self.inverter_settings_tab:
            self._load_inverter_settings_if_needed(force=True)

    def load_file(self, file_path: str) -> None:
        self._begin_load(file_path)
        try:
            self._set_progress(15, "Reading Excel workbook")
            self._append_log(f"Opening file: {file_path}")
            dataframe = load_excel(file_path)
            self._load_dataframe(
                dataframe,
                f"Excel file {Path(file_path).name}",
                metadata={"provider": "excel", "file_name": Path(file_path).name},
            )
        except (ExcelLoaderError, DataProcessorError) as exc:
            self._handle_load_error(exc)

    def load_dessmonitor_data(
        self,
        config: DessMonitorConfig,
        *,
        background: bool = False,
        include_weather_sync: bool = False,
    ) -> None:
        metadata = {
            "provider": "dessmonitor",
            "company_key": config.company_key,
            "source": config.source,
            "pn": config.pn,
            "devcode": config.devcode,
            "devaddr": config.devaddr,
            "sn": config.sn,
            "device_label": config.device_label,
            "date_from": config.date_from,
            "date_to": config.date_to,
        }
        cached_remote_first = cached_remote_first_data_for_metadata(metadata)
        if config.period_mode == "all_data" and cached_remote_first:
            config.date_from = cached_remote_first
            metadata["date_from"] = cached_remote_first
        profile_name = (
            self.active_device_profile.profile_name
            if self.active_device_profile is not None
            else "Manual import"
        )
        device_name = config.device_label or config.sn or config.pn or "Selected device"
        if background:
            self._set_auto_sync_active(True)
            self.statusBar().showMessage(tr_fragment(f"Auto-sync started for {device_name}"))
            self.saved_data_section.append_activity(
                f"Auto-sync started for {device_name}: {config.date_from} to {config.date_to}"
            )
        else:
            self._begin_load(
                "DessMonitor import",
                summary=_dessmonitor_import_summary(config, profile_name),
            )
        if config.period_mode == "all_data" and cached_remote_first:
            self._append_log(f"Using cached earliest remote date: {cached_remote_first}.")
        if self._import_thread is not None:
            if background:
                self.saved_data_section.append_activity("Auto-sync skipped because another import is already running.")
                self.statusBar().showMessage(tr("Auto-sync skipped while another import is running."))
                self._set_auto_sync_active(False)
            else:
                self._handle_load_error(RuntimeError("Another import is already running."))
            return
        source_label = (
            f"DessMonitor {config.device_label} ({config.date_from} to {config.date_to})"
            if config.device_label
            else f"DessMonitor data ({config.date_from} to {config.date_to})"
        )
        self._pending_import_source_label = source_label
        self._pending_import_metadata = metadata
        self._pending_import_is_background = background
        self._pending_import_include_weather = include_weather_sync

        thread = QThread()
        worker = DessMonitorImportWorker(config)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._set_progress, Qt.ConnectionType.QueuedConnection)
        worker.log.connect(self._append_log, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._finish_pending_dessmonitor_import, Qt.ConnectionType.QueuedConnection)
        worker.failed.connect(self._handle_pending_dessmonitor_import_error, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        worker.failed.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._clear_import_thread)

        self._import_thread = thread
        self._import_worker = worker
        self._refresh_tab_badges()
        thread.start()

    def _finish_pending_dessmonitor_import(self) -> None:
        try:
            dataframe = self._import_worker.result_dataframe if self._import_worker is not None else None
            if dataframe is None:
                raise RuntimeError("DessMonitor import finished without returning any data.")
            config = self._import_worker._config if self._import_worker is not None else None
            if self._import_worker is not None and self._import_worker.resolved_first_remote_date:
                self._pending_import_metadata["date_from"] = self._import_worker.resolved_first_remote_date
                save_cached_remote_first_data_for_metadata(
                    self._pending_import_metadata,
                    self._import_worker.resolved_first_remote_date,
                )
            if config is not None:
                self._pending_import_metadata["date_from"] = config.date_from
                self._pending_import_metadata["date_to"] = config.date_to
                self._pending_import_source_label = (
                    f"DessMonitor {config.device_label} ({config.date_from} to {config.date_to})"
                    if config.device_label
                    else f"DessMonitor data ({config.date_from} to {config.date_to})"
                )
            if self._pending_import_is_background:
                self._finish_background_sync(dataframe)
            else:
                self._load_dataframe(
                    dataframe,
                    self._pending_import_source_label,
                    metadata=self._pending_import_metadata,
                )
                if self._pending_import_include_weather:
                    self._import_weather_history_for_config(
                        config,
                        open_saved_import_after_sync=False,
                    )
        except Exception as exc:
            if self._pending_import_is_background:
                self.saved_data_section.append_activity(f"Auto-sync failed: {exc}")
                self.statusBar().showMessage(tr("Auto-sync failed."))
                self._set_auto_sync_active(False)
            else:
                self._handle_load_error(exc)

    def _finish_background_sync(self, dataframe: pd.DataFrame) -> None:
        import_id, _db_path = save_import_dataframe(
            dataframe,
            source_label=self._pending_import_source_label,
            metadata={"source": self._pending_import_source_label, **self._pending_import_metadata},
        )
        self.saved_data_section.append_activity(
            f"Auto-sync completed: {len(dataframe.index)} row(s) merged into dataset #{import_id}."
        )
        if self._pending_import_include_weather and self._active_profile_has_weather_location():
            try:
                weather_import_id = self._sync_weather_history_for_dataframe(
                    dataframe,
                    dessmonitor_metadata=self._pending_import_metadata,
                    open_saved_import_after_sync=False,
                )
                if weather_import_id is not None:
                    self.saved_data_section.append_activity(
                        f"Auto-sync updated Weather History dataset #{weather_import_id}."
                    )
            except (WeatherApiError, DataProcessorError, RuntimeError) as exc:
                self.saved_data_section.append_activity(f"Weather auto-sync failed: {exc}")
        self.saved_data_section.reload_records()
        self._refresh_active_profile_dataset_from_database()
        self._forecast_loaded_profile_key = None
        if hasattr(self, "tabs") and self.tabs.currentWidget() is self.forecast_tab:
            self._refresh_forecast(force=True)
        self.statusBar().showMessage(tr_fragment(f"Auto-sync completed at {pd.Timestamp.now().strftime('%H:%M')}"))
        self._set_auto_sync_active(False)

    def _handle_pending_dessmonitor_import_error(self, message: str) -> None:
        if self._pending_import_is_background:
            self.saved_data_section.append_activity(f"Auto-sync failed: {message}")
            self.statusBar().showMessage(tr("Auto-sync failed."))
            self._set_auto_sync_active(False)
            return
        self._handle_load_error(RuntimeError(message))

    def _clear_import_thread(self) -> None:
        self._import_thread = None
        self._import_worker = None
        self._pending_import_is_background = False
        self._pending_import_source_label = ""
        self._pending_import_metadata = {}
        self._pending_import_include_weather = False
        self._refresh_tab_badges()

    def _load_dataframe(
        self,
        dataframe: pd.DataFrame,
        source_label: str,
        metadata: dict[str, object] | None = None,
        *,
        save_to_db: bool = True,
    ) -> None:
        active_dataframe = dataframe
        self._set_progress(40, "Inspecting columns")
        self._append_log(f"Loaded raw data with {len(dataframe.columns)} columns.")
        if save_to_db:
            import_id, db_path = save_import_dataframe(
                dataframe,
                source_label=source_label,
                metadata={"source": source_label, **(metadata or {})},
            )
            self._append_log(f"Saved import #{import_id} to {db_path}.")
            active_dataframe = load_import_dataframe(import_id)
            if not active_dataframe.empty:
                self._append_log(
                    f"Reloaded merged saved dataset with {len(active_dataframe.index)} row(s) from the local database."
                )
        self.processed_data = process_dataframe(active_dataframe)
        self._forecast_loaded_profile_key = None

        self._set_progress(65, "Grouping series")
        self._append_log(
            f"Detected timestamp column: {self.processed_data.timestamp_column}. "
            f"Numeric columns: {len(self.processed_data.numeric_columns)}."
        )
        self._set_progress(80, "Updating interface")
        self.sidebar.set_groups(self.processed_data.grouped_columns)
        min_date = self.processed_data.dataframe[self.processed_data.timestamp_column].min()
        max_date = self.processed_data.dataframe[self.processed_data.timestamp_column].max()
        self.start_date.setDate(QDate(min_date.year, min_date.month, min_date.day))
        self.end_date.setDate(QDate(max_date.year, max_date.month, max_date.day))
        self._append_log(
            f"Prepared {len(self.processed_data.dataframe)} rows from "
            f"{min_date} to {max_date}."
        )

        self._set_progress(90, "Rendering chart")
        status_message, status_tooltip = _format_loaded_status_message(
            source_label,
            metadata,
            row_count=len(self.processed_data.dataframe),
            metric_count=len(self.processed_data.numeric_columns),
            min_date=min_date,
            max_date=max_date,
            active_profile_name=(
                self.active_device_profile.profile_name
                if self.active_device_profile is not None
                else ""
            ),
        )
        self.statusBar().showMessage(status_message)
        self.statusBar().setToolTip(tr_fragment(status_tooltip))
        self.current_source_label = source_label
        self.current_dataset_metadata = dict(metadata or {})
        self.saved_data_section.set_activity_dataset(source_label, self.current_dataset_metadata)
        self.refresh_chart()
        if self._profile_activation_in_progress:
            self._set_progress(88, "Preparing forecast")
        else:
            self._set_progress(100, "Load complete")
        self._append_log("Load completed successfully.")
        if not self._profile_activation_in_progress:
            self.progress_popup.stop()
        self.saved_data_section.reload_records()
        if hasattr(self, "tabs") and self.tabs.currentWidget() is self.forecast_tab:
            self._refresh_forecast(force=True)

    def _handle_load_error(self, exc: Exception) -> None:
        self._append_log(f"Load failed: {exc}")
        self.progress_popup.finish_error(str(exc))
        self.statusBar().showMessage(tr("Failed to load data."))

    def _begin_load(self, file_path: str, *, summary: str | None = None) -> None:
        self.saved_data_section.clear_activity()
        self.progress_popup.start(
            tr("Starting import"),
            summary=summary or f"Source: {Path(file_path).name}",
        )
        self._append_log(tr_fragment(f"Import started for {Path(file_path).name}"))

    def _set_progress(self, value: int, message: str) -> None:
        self.progress_popup.update_status(value, message)
        QTimer.singleShot(0, self.progress_popup.update)

    def _append_log(self, message: str) -> None:
        self.progress_popup.append_log(message)
        self.saved_data_section.append_activity(message)
        QTimer.singleShot(0, self.progress_popup.update)

    def open_saved_import(self, import_id: int) -> None:
        dataframe = load_import_dataframe(import_id)
        if dataframe.empty:
            self._show_message(QMessageBox.Icon.Information, "Data", "The selected import does not contain any rows.")
            return
        self.tabs.setCurrentIndex(0)
        self._begin_load(f"Saved import #{import_id}")
        try:
            self._append_log(f"Opening saved import #{import_id} from the local database.")
            record = next((item for item in list_saved_imports() if item.import_id == import_id), None)
            source_label = record.source_label if record is not None else f"Saved import #{import_id}"
            self._load_dataframe(
                dataframe,
                source_label,
                metadata=(record.metadata if record is not None else {"provider": "database"}),
                save_to_db=False,
            )
        except DataProcessorError as exc:
            self._handle_load_error(exc)

    def _refresh_active_profile_dataset_from_database(self) -> None:
        profile = self.active_device_profile
        if profile is None:
            return
        current_metadata = self.current_dataset_metadata or {}
        if current_metadata.get("provider") != "dessmonitor":
            return
        if (
            str(current_metadata.get("pn", "")) != profile.pn
            or str(current_metadata.get("devcode", "")) != profile.devcode
            or str(current_metadata.get("devaddr", "")) != profile.devaddr
        ):
            return

        metadata = self._active_profile_metadata()
        if metadata is None:
            return
        import_id = latest_import_id_for_metadata(metadata)
        if import_id is None:
            return
        dataframe = load_import_dataframe(import_id)
        if dataframe.empty:
            return

        try:
            self.processed_data = process_dataframe(dataframe)
        except DataProcessorError as exc:
            LOGGER.warning("Failed to process active profile dataframe for current session: %s", exc)
            return

        record = next((item for item in list_saved_imports() if item.import_id == import_id), None)
        self.sidebar.set_groups(self.processed_data.grouped_columns)
        min_date = self.processed_data.dataframe[self.processed_data.timestamp_column].min()
        max_date = self.processed_data.dataframe[self.processed_data.timestamp_column].max()
        self.start_date.setDate(QDate(min_date.year, min_date.month, min_date.day))
        self.end_date.setDate(QDate(max_date.year, max_date.month, max_date.day))
        if record is not None:
            self.current_source_label = record.source_label
        self.current_dataset_metadata = metadata
        self.saved_data_section.set_activity_dataset(self.current_source_label, self.current_dataset_metadata)
        self.refresh_chart()

    def export_saved_import_to_excel(self, import_id: int) -> None:
        dataframe = load_import_dataframe(import_id)
        if dataframe.empty:
            self._show_message(QMessageBox.Icon.Information, "Export Excel", "The selected dataset does not contain any rows.")
            return
        profile = self.active_device_profile
        export_frame = dataframe.copy()
        if self._profile_tariffs_enabled(profile):
            assert profile is not None
            export_frame = self._append_tariff_cost_columns_for_export(export_frame, profile)
        default_name = f"saved_dataset_{import_id}.xlsx"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Data to Excel",
            default_name,
            "Excel Files (*.xlsx)",
        )
        if not file_path:
            return
        try:
            export_frame.to_excel(file_path, index=False)
        except Exception as exc:
            self._show_message(QMessageBox.Icon.Critical, "Export Error", str(exc))
            return
        self.saved_data_section.append_activity(f"Exported saved dataset to {file_path}")
        self.statusBar().showMessage(tr_fragment(f"Saved dataset exported to {file_path}"))

    def _append_tariff_cost_columns_for_export(self, dataframe: pd.DataFrame, profile: DeviceProfile) -> pd.DataFrame:
        if dataframe.empty:
            return dataframe
        timestamp_column = next(
            (column for column in dataframe.columns if str(column).strip().lower() == "timestamp"),
            None,
        )
        if timestamp_column is None:
            return dataframe
        grid_column = next(
            (
                column
                for column in dataframe.columns
                if str(column).strip() in {"GRID_ACTIVE_POWER", "grid_active_power"}
            ),
            None,
        )
        if grid_column is None:
            grid_column = next(
                (
                    column
                    for column in dataframe.columns
                    if "grid" in str(column).strip().lower() and "power" in str(column).strip().lower()
                ),
                None,
            )
        if grid_column is None:
            return dataframe

        frame = dataframe.copy()
        frame["timestamp_norm"] = pd.to_datetime(frame[timestamp_column], errors="coerce")
        frame["grid_power_norm"] = pd.to_numeric(frame[grid_column], errors="coerce")
        frame.sort_values("timestamp_norm", inplace=True)
        frame["next_timestamp_norm"] = frame["timestamp_norm"].shift(-1)
        frame["next_grid_power_norm"] = frame["grid_power_norm"].shift(-1)
        frame["duration_hours"] = (frame["next_timestamp_norm"] - frame["timestamp_norm"]).dt.total_seconds() / 3600.0

        midpoint = frame["timestamp_norm"] + (frame["next_timestamp_norm"] - frame["timestamp_norm"]) / 2
        frame["tariff_period"] = midpoint.dt.hour.map(
            lambda hour: tr("Day") if pd.notna(hour) and 7 <= int(hour) < 23 else tr("Night")
        )
        frame.loc[frame["timestamp_norm"].isna() | frame["next_timestamp_norm"].isna(), "tariff_period"] = ""
        frame["tariff_uah_per_kwh"] = frame["tariff_period"].map(
            {
                tr("Day"): profile.resolved_day_zone_tariff_uah_per_kwh(),
                tr("Night"): profile.resolved_night_zone_tariff_uah_per_kwh(),
            }
        ).fillna(0.0)

        valid = frame["duration_hours"].notna() & (frame["duration_hours"] > 0)
        frame["grid_import_kwh_interval"] = 0.0
        frame.loc[valid, "grid_import_kwh_interval"] = (
            (
                frame.loc[valid, "grid_power_norm"].clip(lower=0.0)
                + frame.loc[valid, "next_grid_power_norm"].clip(lower=0.0)
            )
            / 2.0
        ) * frame.loc[valid, "duration_hours"]
        frame["grid_import_cost_uah_interval"] = frame["grid_import_kwh_interval"] * frame["tariff_uah_per_kwh"]

        frame.drop(
            columns=[
                "timestamp_norm",
                "grid_power_norm",
                "next_timestamp_norm",
                "next_grid_power_norm",
                "duration_hours",
            ],
            inplace=True,
            errors="ignore",
        )
        return frame

    def clear_saved_database(self) -> None:
        if not self._confirm_action(
            "Clear database",
            "Delete all saved datasets from the local database?",
        ):
            return
        clear_database()
        self.saved_data_section.clear_activity()
        self.saved_data_section.append_activity("Cleared the local saved-data database.")
        self.saved_data_section.reload_records()
        self.statusBar().showMessage(tr("The local saved-data database was cleared."))

    def on_filter_changed(self) -> None:
        self._update_filter_controls()
        self.refresh_chart()

    def _update_filter_controls(self) -> None:
        is_custom = self.filter_combo.currentData() == "custom"
        helper_text = tr("Pick a custom date to switch this filter to Custom range.")
        self.start_date.setToolTip(helper_text if not is_custom else tr("Start date"))
        self.end_date.setToolTip(helper_text if not is_custom else tr("End date"))

    def _handle_manual_date_change(self) -> None:
        if self.filter_combo.currentData() != "custom":
            custom_index = self.filter_combo.findData("custom")
            if custom_index >= 0:
                self.filter_combo.blockSignals(True)
                self.filter_combo.setCurrentIndex(custom_index)
                self.filter_combo.blockSignals(False)
                self._update_filter_controls()
        self.refresh_chart()

    def _open_pv_history_dialog(self) -> None:
        self._open_power_history_dialog(
            branch_key="pv",
            dialog_title="PV History",
            series_label="PV",
            accent_color="#f5cf55",
            summary_title="Total generation",
            history_noun="generation",
            clip_negative=True,
        )

    def _open_grid_history_dialog(self) -> None:
        self._open_power_history_dialog(
            branch_key="grid",
            dialog_title="Grid History",
            series_label="Grid",
            accent_color="#22c55e",
        )

    def _open_battery_history_dialog(self) -> None:
        self._open_power_history_dialog(
            branch_key="battery",
            dialog_title="Battery History",
            series_label="Battery",
            accent_color="#3b82f6",
        )

    def _open_home_history_dialog(self) -> None:
        self._open_power_history_dialog(
            branch_key="home",
            dialog_title="Home History",
            series_label="Home",
            accent_color="#38d6ff",
        )

    def _open_weather_metric_history_dialog(self, metric_key: str) -> None:
        metric_map: dict[str, tuple[str, str, str, int, str]] = {
            "temp": ("temperature_2m", "Temperature", "C", 1, "#fb7185"),
            "cloud": ("cloud_cover", "Cloud cover", "%", 0, "#93c5fd"),
            "rain": ("precipitation", "Precipitation", "mm", 1, "#38bdf8"),
            "wind": ("wind_speed_10m", "Wind speed", "m/s", 1, "#22c55e"),
            "sun": ("shortwave_radiation", "Solar radiation", "W/m2", 0, "#f5cf55"),
        }
        column_name, metric_label, unit, decimals, accent = metric_map.get(
            str(metric_key).strip().lower(),
            ("temperature_2m", "Temperature", "C", 1, "#fb7185"),
        )
        profile = self.active_device_profile
        if profile is None:
            self._show_message(
                QMessageBox.Icon.Information,
                "Weather History",
                "Choose an active device profile first.",
            )
            return
        weather_frame = self._load_active_profile_weather_history_dataframe()
        if weather_frame is None or weather_frame.empty:
            self._show_message(
                QMessageBox.Icon.Information,
                "Weather History",
                "Weather History dataset is empty. Run weather import first.",
            )
            return
        timestamp_column = next(
            (column for column in weather_frame.columns if str(column).strip().lower() == "timestamp"),
            None,
        )
        if timestamp_column is None:
            self._show_message(
                QMessageBox.Icon.Information,
                "Weather History",
                "Weather History dataset does not contain a Timestamp column.",
            )
            return
        metric_column = next(
            (column for column in weather_frame.columns if str(column).strip().lower() == column_name.lower()),
            None,
        )
        if metric_column is None:
            self._show_message(
                QMessageBox.Icon.Information,
                "Weather History",
                f"Weather History dataset does not contain '{column_name}'.",
            )
            return
        try:
            forecast_snapshot = self._best_forecast_dataframe_for_active_profile()
            dialog = WeatherMetricHistoryDialog(
                weather_frame,
                timestamp_column,
                metric_column,
                profile.device_label or profile.profile_name or "Weather",
                metric_label=metric_label,
                metric_unit=unit,
                decimals=decimals,
                accent_color=accent,
                forecast_dataframe=forecast_snapshot,
                forecast_horizon_days=3,
                forecast_refresh_requested=self._trigger_pv_forecast_refresh,
                forecast_snapshot_loader=self._load_latest_pv_forecast_snapshot_for_active_profile,
                parent=self,
            )
            # Keep graph dialogs managed by popup manager but non-modal at Qt
            # level; modal-like blocking is handled by backdrop + focus guard.
            dialog.setProperty("popup_disable_backdrop", False)
            dialog.setProperty("popup_disable_focus_guard", True)
            dialog.setProperty("popup_disable_focus_recovery", True)
            dialog.setProperty("popup_force_window_modal", False)
            dialog.setProperty("popup_key", f"weather_history:{metric_key}")
            self._show_popup_dialog(dialog)
        except Exception as exc:
            self._show_message(QMessageBox.Icon.Critical, "Weather History", f"Failed to open dialog: {exc}")

    def _open_inverter_analysis_dialog(self) -> None:
        if self.active_device_profile is None:
            self._show_message(QMessageBox.Icon.Information, "Inverter Analysis", "Choose an active device profile first.")
            return
        processed = self._resolved_active_profile_processed_data()
        if processed is None:
            self._show_message(
                QMessageBox.Icon.Information,
                "Inverter Analysis",
                "No DessMonitor history is available for the active device profile.",
            )
            return

        try:
            dialog = InverterAnalysisDialog(
                processed,
                device_name=self.active_device_profile.device_label or self.active_device_profile.profile_name or "Inverter",
                day_zone_tariff_uah_per_kwh=self.active_device_profile.resolved_day_zone_tariff_uah_per_kwh(),
                night_zone_tariff_uah_per_kwh=self.active_device_profile.resolved_night_zone_tariff_uah_per_kwh(),
                parent=self,
            )
            # Heavy render dialogs (QWebEngine/Plotly) use delayed focus
            # recovery path to avoid flicker on macOS during initial compose.
            dialog.setProperty("popup_disable_backdrop", False)
            dialog.setProperty("popup_disable_focus_guard", True)
            dialog.setProperty("popup_disable_focus_recovery", True)
            dialog.setProperty("popup_heavy_render", True)
            dialog.setProperty("popup_force_window_modal", False)
            dialog.setProperty("popup_key", "inverter_analysis")
            self._show_embedded_history_dialog(dialog, popup_key="inverter_analysis")
        except Exception as exc:
            self._show_message(QMessageBox.Icon.Critical, "Inverter Analysis", f"Failed to open dialog: {exc}")

    def _open_power_history_dialog(
        self,
        *,
        branch_key: str,
        dialog_title: str,
        series_label: str,
        accent_color: str,
        summary_title: str = "Total energy",
        history_noun: str = "energy",
        clip_negative: bool = False,
    ) -> None:
        if self.active_device_profile is None:
            self._popup_modal_diag(
                "power_history:open_skipped_no_profile",
                branch=branch_key,
                title=dialog_title,
            )
            self._show_message(QMessageBox.Icon.Information, dialog_title, "Choose an active device profile first.")
            return
        self._popup_modal_diag(
            "power_history:open_begin",
            branch=branch_key,
            title=dialog_title,
            profile=self.active_device_profile.profile_name,
            pn=self.active_device_profile.pn,
            devcode=self.active_device_profile.devcode,
            devaddr=self.active_device_profile.devaddr,
        )
        processed = self._resolved_active_profile_processed_data()
        if processed is None:
            self._popup_modal_diag(
                "power_history:open_skipped_no_processed",
                branch=branch_key,
                title=dialog_title,
            )
            self._show_message(
                QMessageBox.Icon.Information,
                dialog_title,
                "No DessMonitor history is available for the active device profile.",
            )
            return
        self._popup_modal_diag(
            "power_history:processed_ready",
            branch=branch_key,
            title=dialog_title,
            rows=len(processed.dataframe.index),
            timestamp_column=processed.timestamp_column,
            numeric_count=len(processed.numeric_columns),
        )

        power_column = self._find_power_history_column(processed, branch_key)
        self._popup_modal_diag(
            "power_history:column_lookup",
            branch=branch_key,
            title=dialog_title,
            found_column=power_column or "",
        )
        if power_column is None:
            self._show_message(
                QMessageBox.Icon.Information,
                dialog_title,
                f"The saved dataset does not contain a {series_label} power column.",
            )
            return

        try:
            if branch_key == "pv":
                forecast_snapshot = self._best_forecast_dataframe_for_active_profile()
                dialog = PvHistoryDialog(
                    processed.dataframe,
                    processed.timestamp_column,
                    power_column,
                    self.active_device_profile.device_label or self.active_device_profile.profile_name or "PV",
                    forecast_dataframe=forecast_snapshot,
                    forecast_horizon_days=3,
                    forecast_refresh_requested=self._trigger_pv_forecast_refresh,
                    forecast_snapshot_loader=self._load_latest_pv_forecast_snapshot_for_active_profile,
                    parent=self,
                )
            else:
                dialog = PowerHistoryDialog(
                    processed.dataframe,
                    processed.timestamp_column,
                    power_column,
                    self.active_device_profile.device_label or self.active_device_profile.profile_name or series_label,
                    series_label=series_label,
                    accent_color=accent_color,
                    summary_title=summary_title,
                    history_noun=history_noun,
                    clip_negative=clip_negative,
                    parent=self,
                )
            # Use one popup policy for PV/Grid/Home/Battery history dialogs so
            # backdrop, focus and reuse behavior remains consistent.
            dialog.setProperty("popup_disable_backdrop", False)
            dialog.setProperty("popup_disable_focus_guard", True)
            dialog.setProperty("popup_disable_focus_recovery", True)
            dialog.setProperty("popup_heavy_render", True)
            dialog.setProperty("popup_force_window_modal", False)
            dialog.setProperty("popup_key", f"power_history:{branch_key}")
            self._show_embedded_history_dialog(dialog, popup_key=f"power_history:{branch_key}")
        except Exception as exc:
            self._show_message(QMessageBox.Icon.Critical, dialog_title, f"Failed to open dialog: {exc}")

    def _resolved_active_profile_processed_data(self) -> ProcessedData | None:
        if self.active_device_profile is None:
            self._popup_modal_diag("power_history:resolve_processed_no_profile")
            return None

        current_metadata = self.current_dataset_metadata or {}
        current_provider = str(current_metadata.get("provider", "")).strip().lower()
        if (
            self.processed_data is not None
            and current_provider == "dessmonitor"
            and str(current_metadata.get("pn", "")) == self.active_device_profile.pn
            and str(current_metadata.get("devcode", "")) == self.active_device_profile.devcode
            and str(current_metadata.get("devaddr", "")) == self.active_device_profile.devaddr
        ):
            self._popup_modal_diag(
                "power_history:resolve_processed_use_current",
                provider=current_provider,
                rows=len(self.processed_data.dataframe.index),
                timestamp_column=self.processed_data.timestamp_column,
                numeric_count=len(self.processed_data.numeric_columns),
            )
            return self.processed_data

        metadata = {
            "provider": "dessmonitor",
            "source": self.active_device_profile.source,
            "pn": self.active_device_profile.pn,
            "devcode": self.active_device_profile.devcode,
            "devaddr": self.active_device_profile.devaddr,
            "sn": self.active_device_profile.sn,
        }
        import_id = latest_import_id_for_metadata(metadata)
        if import_id is None:
            self._popup_modal_diag(
                "power_history:resolve_processed_no_import",
                pn=self.active_device_profile.pn,
                devcode=self.active_device_profile.devcode,
                devaddr=self.active_device_profile.devaddr,
            )
            return None

        dataframe = load_import_dataframe(import_id)
        if dataframe.empty:
            self._popup_modal_diag(
                "power_history:resolve_processed_empty_import",
                import_id=import_id,
            )
            return None

        try:
            processed = process_dataframe(dataframe)
            self._popup_modal_diag(
                "power_history:resolve_processed_from_saved",
                import_id=import_id,
                rows=len(processed.dataframe.index),
                timestamp_column=processed.timestamp_column,
                numeric_count=len(processed.numeric_columns),
            )
            return processed
        except DataProcessorError:
            self._popup_modal_diag(
                "power_history:resolve_processed_failed_parse",
                import_id=import_id,
            )
            return None

    def _trigger_pv_forecast_refresh(self) -> None:
        # Reuse the standard async forecast pipeline; snapshot persistence is handled there.
        self._refresh_forecast(force=True)

    def _load_latest_pv_forecast_snapshot_for_active_profile(self) -> pd.DataFrame:
        profile = self.active_device_profile
        if profile is None:
            return pd.DataFrame()
        return latest_profile_forecast_snapshot(
            pn=profile.pn,
            devcode=profile.devcode,
            devaddr=profile.devaddr,
        )

    def _best_forecast_dataframe_for_active_profile(self) -> pd.DataFrame:
        profile_key = self._active_profile_key()
        if (
            profile_key is not None
            and self._forecast_best_result_key == profile_key
            and self._forecast_best_result is not None
            and isinstance(self._forecast_best_result.forecast_frame, pd.DataFrame)
            and not self._forecast_best_result.forecast_frame.empty
        ):
            return self._forecast_best_result.forecast_frame.copy()
        return self._load_latest_pv_forecast_snapshot_for_active_profile()

    def _register_popup_dialog(
        self,
        dialog: QDialog,
        *,
        delete_on_close: bool,
        origin: str,
    ) -> None:
        """Track dialog in popup registry and attach lifecycle cleanup hooks."""
        if not any(item is dialog for item in self._open_popup_dialogs):
            self._open_popup_dialogs.append(dialog)
            self._popup_modal_diag(
                "popup_manager:registered",
                origin=origin,
                dialog=f"{dialog.__class__.__name__}:{dialog.objectName()}",
                delete_on_close=delete_on_close,
            )
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, delete_on_close)
        dialog.destroyed.connect(lambda *_: self._prune_popup_dialogs(reason="destroyed"))
        dialog.finished.connect(lambda *_: self._prune_popup_dialogs(reason="finished"))

    def _open_registered_modal(self, dialog: QDialog, *, source: str) -> None:
        """Open a dialog via centralized popup manager/backdrop orchestration."""
        self._prune_popup_dialogs(reason=f"open_registered:{source}")
        popup_key = str(dialog.property("popup_key") or "").strip()
        force_window_modal_property = dialog.property("popup_force_window_modal")
        force_window_modal = True if force_window_modal_property is None else bool(force_window_modal_property)
        modality_label = "ManagedWindowModal" if force_window_modal else "ManagedNonModal"
        if not any(item is dialog for item in self._open_popup_dialogs):
            self._register_popup_dialog(dialog, delete_on_close=False, origin=f"{source}:autoregister")
        if self._is_visible_dialog(dialog):
            self._popup_modal_diag(
                "popup_manager:reactivate_visible",
                source=source,
                popup_key=popup_key,
                dialog=f"{dialog.__class__.__name__}:{dialog.objectName()}",
            )
            dialog.raise_()
            dialog.activateWindow()
            self._sync_popup_backdrop_state()
            return
        self._popup_modal_diag(
            "popup_manager:open_modal",
            source=source,
            popup_key=popup_key,
            dialog=f"{dialog.__class__.__name__}:{dialog.objectName()}",
            modality=modality_label,
            requires_backdrop=self._dialog_requires_backdrop(dialog),
            popup_disable_backdrop=bool(dialog.property("popup_disable_backdrop")),
        )
        # Show backdrop before opening a modal dialog. On macOS this avoids
        # an extra z-order correction pass that can cause a brief hide/show
        # flicker right after dialog creation.
        if (
            self._dialog_requires_backdrop(dialog)
            and self._popup_backdrop is not None
            and not self._popup_backdrop.isVisible()
        ):
            self._popup_modal_diag(
                "popup_backdrop:preopen_show",
                source=source,
                popup_key=popup_key,
                dialog=f"{dialog.__class__.__name__}:{dialog.objectName()}",
            )
            self._popup_backdrop_last_state = True
            self._set_popup_backdrop_visible(True)
        dialog.setProperty("popup_open_monotonic", time.monotonic())
        dialog.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        dialog.setWindowFlag(Qt.WindowType.Dialog, True)
        dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
        if force_window_modal:
            dialog.setModal(True)
            dialog.setWindowModality(Qt.WindowModality.WindowModal)
        else:
            # Managed non-modal dialogs still participate in backdrop/focus
            # handling, but do not use Qt modality (required for heavy graph
            # dialogs to avoid platform-specific rendering regressions).
            dialog.setModal(False)
            dialog.setWindowModality(Qt.WindowModality.NonModal)
        self._popup_modal_diag(
            "popup_manager:open_step",
            step="before_show",
            dialog=f"{dialog.__class__.__name__}:{dialog.objectName()}",
            visible=dialog.isVisible(),
            modality=str(dialog.windowModality()),
            geometry=f"{dialog.x()},{dialog.y()},{dialog.width()}x{dialog.height()}",
        )
        dialog.show()
        self._popup_modal_diag(
            "popup_manager:open_step",
            step="after_show",
            dialog=f"{dialog.__class__.__name__}:{dialog.objectName()}",
            visible=dialog.isVisible(),
            active=dialog.isActiveWindow(),
        )
        dialog.raise_()
        self._popup_modal_diag(
            "popup_manager:open_step",
            step="after_raise",
            dialog=f"{dialog.__class__.__name__}:{dialog.objectName()}",
            visible=dialog.isVisible(),
            active=dialog.isActiveWindow(),
        )
        dialog.activateWindow()
        self._popup_modal_diag(
            "popup_manager:open_step",
            step="after_activate",
            dialog=f"{dialog.__class__.__name__}:{dialog.objectName()}",
            visible=dialog.isVisible(),
            active=dialog.isActiveWindow(),
        )
        self._popup_modal_diag(
            "popup_manager:opened_modal",
            source=source,
            popup_key=popup_key,
            dialog=f"{dialog.__class__.__name__}:{dialog.objectName()}",
            visible=dialog.isVisible(),
            modality=modality_label,
        )
        self._sync_popup_backdrop_state()

    def _show_popup_dialog(self, dialog: QDialog, *, delete_on_close: bool = True) -> None:
        """Show popup and reuse an existing instance when popup_key matches."""
        self._prune_popup_dialogs(reason="show_requested")
        popup_key = str(dialog.property("popup_key") or "").strip()
        self._popup_modal_diag(
            "popup_manager:show_requested",
            popup_key=popup_key,
            dialog=f"{dialog.__class__.__name__}:{dialog.objectName()}",
            delete_on_close=delete_on_close,
        )
        for existing in self._open_popup_dialogs:
            existing_key = str(existing.property("popup_key") or "").strip()
            if popup_key and existing_key == popup_key:
                self._popup_modal_diag(
                    "popup_manager:reuse_existing",
                    popup_key=popup_key,
                    dialog=f"{existing.__class__.__name__}:{existing.objectName()}",
                    visible=existing.isVisible(),
                )
                existing.raise_()
                existing.activateWindow()
                self._sync_popup_backdrop_state()
                return
        self._register_popup_dialog(dialog, delete_on_close=delete_on_close, origin="popup_manager:show")
        self._open_registered_modal(dialog, source="popup_manager:show")

    def _run_popup_dialog_blocking(self, dialog: QDialog) -> int:
        """Execute popup in blocking mode while preserving managed lifecycle."""
        popup_key = str(dialog.property("popup_key") or "").strip()
        force_window_modal_property = dialog.property("popup_force_window_modal")
        force_window_modal = True if force_window_modal_property is None else bool(force_window_modal_property)
        modality_label = "ManagedWindowModal" if force_window_modal else "ManagedNonModal"
        self._register_popup_dialog(dialog, delete_on_close=False, origin="popup_manager:blocking")
        self._popup_modal_diag(
            "popup_manager:blocking_exec_start",
            popup_key=popup_key,
            dialog=f"{dialog.__class__.__name__}:{dialog.objectName()}",
            modality=modality_label,
        )
        self._sync_popup_backdrop_state()
        result = exec_modal_dialog(dialog, frameless=True, application_modal=False)
        self._popup_modal_diag(
            "popup_manager:blocking_exec_done",
            popup_key=popup_key,
            dialog=f"{dialog.__class__.__name__}:{dialog.objectName()}",
            result=int(result),
            visible=dialog.isVisible(),
            modality=modality_label,
        )
        self._prune_popup_dialogs(reason="blocking_exec_done")
        return int(result)

    def _prune_popup_dialogs(self, *, reason: str = "manual") -> None:
        """Drop dead dialog references and refresh backdrop/focus state."""
        self._open_popup_dialogs = [item for item in self._open_popup_dialogs if self._is_live_dialog(item)]
        self._popup_modal_diag("popup_manager:pruned", reason=reason, open_count=len(self._open_popup_dialogs))
        self._sync_popup_backdrop_state()

    def _is_live_dialog(self, dialog: QDialog | None) -> bool:
        if dialog is None:
            return False
        try:
            if dialog.isVisible():
                return True
        except RuntimeError:
            return False
        try:
            return bool(isValid(dialog))
        except RuntimeError:
            return False

    def _is_visible_dialog(self, dialog: QDialog | None) -> bool:
        if not self._is_live_dialog(dialog):
            return False
        try:
            return dialog.isVisible()
        except RuntimeError:
            return False

    def _dialog_requires_backdrop(self, dialog: QDialog | None) -> bool:
        """Return True when dialog should block background with overlay."""
        if dialog is None:
            return False
        try:
            return not bool(dialog.property("popup_disable_backdrop"))
        except RuntimeError:
            return True

    def _resize_popup_backdrop(self) -> None:
        if self._popup_backdrop is None:
            return
        # Avoid redundant geometry writes on macOS. Reapplying identical
        # geometry can trigger an extra compositor pass and cause brief popup
        # flicker with heavy dialog contents (for example QWebEngine graphs).
        target_rect = self.rect()
        if self._popup_backdrop.geometry() == target_rect:
            return
        self._popup_backdrop.setGeometry(target_rect)

    def _set_popup_backdrop_visible(self, visible: bool) -> None:
        """Toggle shared backdrop and optional background input blocking."""
        if self._popup_backdrop is None:
            return
        if self._popup_block_target is not None:
            self._popup_block_target.setEnabled(not visible)
        current_visible = self._popup_backdrop.isVisible()
        if current_visible == visible:
            return
        self._resize_popup_backdrop()
        if visible:
            self._popup_backdrop.show()
            self._popup_backdrop.raise_()
            # Keep any visible modal dialog above the backdrop. On macOS, raising
            # the overlay can briefly trigger a hide/show cycle if dialog z-order
            # is not immediately restored.
            self._raise_visible_popups_over_backdrop()
            self._raise_embedded_history_overlay()
            return
        self._popup_backdrop.hide()

    def _should_redirect_main_focus(self, event) -> bool:
        """Keep keyboard/mouse focus on topmost popup while backdrop is active."""
        if event is None:
            return False
        popup_backdrop = getattr(self, "_popup_backdrop", None)
        if popup_backdrop is None or not popup_backdrop.isVisible():
            return False
        if event.type() not in {QEvent.Type.WindowActivate, QEvent.Type.FocusIn}:
            return False

        open_popup_dialogs = getattr(self, "_open_popup_dialogs", ())
        active_dialog = next((item for item in reversed(open_popup_dialogs) if self._is_visible_dialog(item)), None)
        if active_dialog is None:
            active_modal = QApplication.activeModalWidget()
            if isinstance(active_modal, QDialog) and self._is_visible_dialog(active_modal):
                active_dialog = active_modal
        if active_dialog is None:
            return False
        if bool(active_dialog.property("popup_disable_focus_guard")):
            return False
        opened_at = active_dialog.property("popup_open_monotonic")
        if isinstance(opened_at, (float, int)) and (time.monotonic() - float(opened_at)) < 0.7:
            return False
        if QApplication.activeWindow() is active_dialog:
            return False

        self._popup_modal_diag(
            "popup_focus_guard:redirect",
            dialog=f"{active_dialog.__class__.__name__}:{active_dialog.objectName()}",
        )
        QTimer.singleShot(0, active_dialog.raise_)
        QTimer.singleShot(0, active_dialog.activateWindow)
        return True

    def _raise_visible_popups_over_backdrop(self) -> None:
        """Restore z-order so dialogs remain above backdrop after overlay raise."""
        active_modal = QApplication.activeModalWidget()
        raised_any = False
        if isinstance(active_modal, QDialog) and self._is_visible_dialog(active_modal):
            active_modal.raise_()
            active_modal.activateWindow()
            raised_any = True
        for dialog in self._open_popup_dialogs:
            if not self._is_visible_dialog(dialog):
                continue
            if isinstance(active_modal, QDialog) and dialog is active_modal:
                continue
            dialog.raise_()
            if not raised_any:
                dialog.activateWindow()
            raised_any = True
        if raised_any:
            self._popup_modal_diag("popup_backdrop:raised_visible_dialogs")

    def _raise_embedded_history_overlay(self) -> None:
        overlay = self._embedded_history_overlay
        if overlay is None or not overlay.isVisible():
            return
        overlay.raise_()

    def _resize_embedded_history_overlay(self) -> None:
        overlay = self._embedded_history_overlay
        if overlay is None:
            return
        overlay.setGeometry(self.rect())
        dialog = self._embedded_history_dialog
        if dialog is None or not overlay.isVisible():
            return
        bounds = overlay.rect().adjusted(80, 50, -80, -50)
        target_width = min(max(dialog.width(), 960), max(960, bounds.width()))
        target_height = min(max(dialog.height(), 620), max(620, bounds.height()))
        x = bounds.x() + max(0, (bounds.width() - target_width) // 2)
        y = bounds.y() + max(0, (bounds.height() - target_height) // 2)
        dialog.setGeometry(x, y, target_width, target_height)

    def _close_embedded_history_dialog(self, *, reason: str) -> None:
        dialog = self._embedded_history_dialog
        self._embedded_history_dialog = None
        self._embedded_history_key = ""
        if dialog is not None:
            try:
                dialog.hide()
            except Exception:
                LOGGER.exception("Failed to hide embedded history dialog")
            try:
                dialog.deleteLater()
            except Exception:
                LOGGER.exception("Failed to schedule embedded history dialog deletion")
        if self._embedded_history_overlay is not None:
            self._embedded_history_overlay.hide()
        self._popup_modal_diag("embedded_history:closed", reason=reason)
        self._sync_popup_backdrop_state()

    def _show_embedded_history_dialog(self, dialog: QDialog, *, popup_key: str) -> None:
        overlay = self._embedded_history_overlay
        if overlay is None:
            self._show_popup_dialog(dialog)
            return
        if (
            self._embedded_history_dialog is not None
            and self._embedded_history_key == popup_key
            and self._embedded_history_dialog.isVisible()
        ):
            self._embedded_history_dialog.raise_()
            return
        if self._embedded_history_dialog is not None:
            self._close_embedded_history_dialog(reason="replace")
        self._embedded_history_dialog = dialog
        self._embedded_history_key = popup_key
        dialog.setParent(overlay)
        dialog.setWindowFlag(Qt.WindowType.Widget, True)
        dialog.setWindowFlag(Qt.WindowType.Dialog, False)
        dialog.setModal(False)
        dialog.setWindowModality(Qt.WindowModality.NonModal)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
        dialog.finished.connect(lambda *_: self._close_embedded_history_dialog(reason="finished"))
        overlay.show()
        self._resize_embedded_history_overlay()
        dialog.show()
        dialog.raise_()
        self._popup_modal_diag(
            "embedded_history:opened",
            popup_key=popup_key,
            dialog=f"{dialog.__class__.__name__}:{dialog.objectName()}",
        )
        self._sync_popup_backdrop_state()

    def _schedule_popup_focus_recovery(self, *, reason: str) -> None:
        """Schedule two-phase focus recovery after popup stack changes."""
        if self._close_requested:
            return
        self._popup_focus_recovery_token += 1
        token = self._popup_focus_recovery_token
        delay_ms = 0
        active_dialog = next((item for item in reversed(self._open_popup_dialogs) if self._is_visible_dialog(item)), None)
        if active_dialog is not None and bool(active_dialog.property("popup_disable_focus_recovery")):
            self._popup_modal_diag(
                "popup_focus_recovery:skip_disabled",
                reason=reason,
                dialog=f"{active_dialog.__class__.__name__}:{active_dialog.objectName()}",
            )
            return
        if reason == "visible_changed" and active_dialog is not None and bool(active_dialog.property("popup_heavy_render")):
            delay_ms = 460
        self._popup_modal_diag("popup_focus_recovery:scheduled", reason=reason, token=token)
        QTimer.singleShot(delay_ms, lambda _t=token: self._recover_popup_focus(_t, stage="immediate"))
        QTimer.singleShot(delay_ms + 90, lambda _t=token: self._recover_popup_focus(_t, stage="settle"))

    def _recover_popup_focus(self, token: int, *, stage: str) -> None:
        """Restore focus either to active popup or back to main window."""
        if self._close_requested:
            return
        if token != self._popup_focus_recovery_token:
            return

        active_dialog = next((item for item in self._open_popup_dialogs if self._is_visible_dialog(item)), None)
        if active_dialog is not None:
            dialog_window = active_dialog.windowHandle()
            if dialog_window is not None:
                dialog_window.requestActivate()
            QApplication.setActiveWindow(active_dialog)
            active_dialog.raise_()
            active_dialog.activateWindow()
            active_dialog.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
            self._popup_modal_diag(
                "popup_focus_recovery:dialog",
                stage=stage,
                dialog=f"{active_dialog.__class__.__name__}:{active_dialog.objectName()}",
            )
            return

        popup_backdrop = getattr(self, "_popup_backdrop", None)
        if popup_backdrop is not None and popup_backdrop.isVisible():
            return

        main_window = self.windowHandle()
        if main_window is not None:
            main_window.requestActivate()
        QApplication.setActiveWindow(self)
        self.raise_()
        self.activateWindow()
        focus_target = self.focusWidget()
        if focus_target is not None and focus_target is not self:
            focus_target.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        elif self.centralWidget() is not None:
            self.centralWidget().setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self._popup_modal_diag("popup_focus_recovery:main_window", stage=stage)

    def _sync_popup_backdrop_state(self) -> None:
        """Recompute whether backdrop must be visible for current popup stack."""
        visible_popup_dialogs = [
            item
            for item in self._open_popup_dialogs
            if self._is_visible_dialog(item)
        ]
        visible_popups = [
            f"{item.__class__.__name__}:{item.objectName()}"
            for item in visible_popup_dialogs
        ]
        has_popup = any(self._dialog_requires_backdrop(item) for item in visible_popup_dialogs)
        if self._embedded_history_overlay is not None and self._embedded_history_overlay.isVisible():
            has_popup = True
            visible_popups.append("EmbeddedHistoryOverlay")

        active_modal = QApplication.activeModalWidget()
        if (
            not has_popup
            and isinstance(active_modal, QDialog)
            and active_modal.isVisible()
            and active_modal.parentWidget() is not None
            and active_modal.parentWidget().window() is self
            and self._dialog_requires_backdrop(active_modal)
        ):
            has_popup = True

        visible_signature = ",".join(visible_popups)
        if visible_signature != self._popup_visible_signature:
            previous_signature = self._popup_visible_signature
            self._popup_visible_signature = visible_signature
            self._popup_modal_diag(
                "popup_backdrop:visible_changed",
                previous=previous_signature,
                current=visible_signature,
            )
            self._schedule_popup_focus_recovery(reason="visible_changed")

        if self._popup_backdrop_last_state is None or self._popup_backdrop_last_state != has_popup:
            self._popup_modal_diag(
                "popup_backdrop:state_changed",
                has_popup=has_popup,
                visible_popups=",".join(visible_popups),
            )
            self._popup_backdrop_last_state = has_popup
        self._set_popup_backdrop_visible(has_popup)

    def _find_pv_power_column(self, processed: ProcessedData) -> str | None:
        return self._find_power_history_column(processed, "pv")

    def _find_power_history_column(self, processed: ProcessedData, branch_key: str) -> str | None:
        preferred_map = {
            "pv": ("PV_OUTPUT_POWER", "pv_output_power", "PV", "pv"),
            "grid": ("GRID_ACTIVE_POWER", "grid_active_power", "GRID", "grid"),
            "battery": ("BATTERY_ACTIVE_POWER", "battery_active_power", "bt_battery_power", "battery_power", "BATTERY", "battery", "battary"),
            "home": ("LOAD_ACTIVE_POWER", "load_active_power", "home_active_power", "HOME", "home", "load"),
        }
        keyword_map = {
            "pv": ("pv",),
            "grid": ("grid",),
            "battery": ("battery", "battary", "bt_"),
            "home": ("load", "home"),
        }

        preferred = preferred_map.get(branch_key, ())
        for column in processed.numeric_columns:
            if column in preferred:
                return column

        keywords = keyword_map.get(branch_key, ())
        for column in processed.numeric_columns:
            normalized = column.strip().lower()
            # Accept short aliases like PV/Home/Grid/Battery/Battary, and classic power columns.
            if normalized in keywords:
                return column
            if any(keyword in normalized for keyword in keywords) and ("power" in normalized or "w" in normalized):
                return column
        return None

    def current_filter(self) -> DateFilter:
        return DateFilter(
            preset=self.filter_combo.currentData(),
            start=pd.Timestamp(self.start_date.date().toPython()),
            end=pd.Timestamp(self.end_date.date().toPython()),
        )

    def refresh_chart(self) -> None:
        if not self.processed_data:
            self.current_figure = None
            self._update_chart_action_states()
            return

        selected_columns = self.sidebar.selected_columns()
        if not selected_columns:
            self.current_figure = None
            self.graph_view.set_placeholder("Select at least one series to display the chart.")
            self._update_chart_action_states()
            return

        self.filtered_data = apply_date_filter(
            self.processed_data.dataframe,
            self.processed_data.timestamp_column,
            self.current_filter(),
        )
        if self.filtered_data.empty:
            self.current_figure = None
            self.graph_view.set_placeholder("No rows match the selected date range.")
            self._update_chart_action_states()
            return

        self.current_figure = build_chart_figure(
            self.filtered_data,
            self.processed_data.timestamp_column,
            selected_columns,
        )
        self.graph_view.set_figure(self.current_figure)
        self._update_chart_action_states()

    def export_chart(self) -> None:
        if self.current_figure is None:
            self._show_message(QMessageBox.Icon.Information, "Export", "Load data before exporting a chart.")
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Chart",
            "",
            "HTML (*.html);;PNG (*.png);;PDF (*.pdf)",
        )
        if not file_path:
            return
        try:
            export_figure(self.current_figure, file_path)
        except Exception as exc:
            self._show_message(QMessageBox.Icon.Critical, "Export Error", str(exc))
            return
        self.statusBar().showMessage(tr_fragment(f"Chart exported to {file_path}"))

    def export_all_charts(self) -> None:
        if self.current_figure is None:
            self._show_message(QMessageBox.Icon.Information, "Export", "Load data before exporting charts.")
            return
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export All Formats",
            "chart_export",
            "Base name (*)",
        )
        if not file_path:
            return
        try:
            exported = export_all_formats(self.current_figure, file_path)
        except Exception as exc:
            self._show_message(QMessageBox.Icon.Critical, "Export Error", str(exc))
            return
        self.statusBar().showMessage(tr_fragment(f"Exported: {', '.join(str(path.name) for path in exported)}"))

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # noqa: N802
        for url in event.mimeData().urls():
            local_file = url.toLocalFile()
            if local_file.lower().endswith(".xlsx"):
                self.load_file(local_file)
                break
IMPORT_PROGRESS_DIALOG_STYLE = """
#ImportProgressDialog {
    background: #09111f;
    border: 1px solid #31415f;
    border-radius: 24px;
}
#ProgressMessage {
    font-size: 24px;
    font-weight: 700;
    color: #e2e8f0;
    padding-top: 6px;
}
#ProgressStep {
    font-size: 13px;
    font-weight: 700;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    color: #22d3ee;
    padding-top: 4px;
}
#ProgressDetail {
    font-size: 14px;
    color: #94a3b8;
    padding-bottom: 6px;
}
#ProgressSummary {
    background: #0f172a;
    border: 1px solid #334155;
    border-radius: 14px;
    color: #e2e8f0;
    font-size: 14px;
    line-height: 1.4;
    padding: 12px 14px;
    margin-bottom: 6px;
}
#ImportProgressDialog QTextBrowser {
    background: #0b1220;
    border: 1px solid #334155;
    border-radius: 12px;
    color: #cbd5e1;
    padding: 8px;
}
#ImportProgressDialog QProgressBar {
    background: #111827;
    border: 1px solid #334155;
    border-radius: 10px;
    text-align: center;
    min-height: 26px;
    color: #eff6ff;
}
#ImportProgressDialog QProgressBar::chunk {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #0ea5e9, stop:1 #22c55e);
    border-radius: 9px;
}
#ImportProgressDialog QPushButton {
    min-width: 110px;
    min-height: 30px;
    border-radius: 9px;
    font-weight: 600;
    padding: 5px 12px;
}
"""
