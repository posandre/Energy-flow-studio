from __future__ import annotations

from dataclasses import dataclass
import html

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from PySide6.QtCore import QDate, QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from app.services.data_processor import ProcessedData
from app.services.i18n import tr, tr_fragment
from app.services.logging_utils import get_logger
from app.ui.design_system import GLOBAL_TOOLTIP_QSS, apply_tab_widget_interaction, compose_styles, tab_widget_qss
from app.ui.dialogs import NEON_CLOSE_BUTTON_STYLE, NEON_HEADER_BAR_STYLE, create_neon_header_bar
from app.ui.graph_view import GraphView
from app.ui.pv_history_dialog import CalendarPopup, MonthPickerPopup, PeriodPickerButton, YearPickerPopup

SERIES_TOGGLE_STYLE_TEMPLATE = """
QCheckBox {{
    color: {color};
    spacing: 6px;
    padding: 4px 0;
    font-size: 13px;
    font-weight: 600;
    background: transparent;
}}
"""

LOGGER = get_logger(__name__)

INVERTER_ANALYSIS_DIALOG_STYLE = """
QDialog {
    background: #0b1220;
    color: #e2e8f0;
}
#InverterIntro, #InverterHint {
    color: #cbd5e1;
    font-size: 12px;
    padding: 0 4px;
}
#InverterCard {
    background: rgba(8, 15, 30, 0.82);
    border: 1px solid #243244;
    border-radius: 16px;
    padding: 10px 14px;
}
QPushButton#KpiHelpButton {
    min-width: 20px;
    max-width: 20px;
    min-height: 20px;
    max-height: 20px;
    border-radius: 10px;
    border: 1px solid #2e4663;
    background: #13213b;
    color: #9cc4dd;
    font-size: 12px;
    font-weight: 700;
}
QPushButton#KpiHelpButton:hover {
    background: #1a2f4f;
    color: #dbeafe;
    border-color: #3b5b81;
}
QPushButton#KpiHelpButton:pressed {
    background: #10203a;
}
QFrame#PeriodSectionFrame {
    border: 1px solid #23344d;
    background: rgba(10, 18, 32, 230);
}
QFrame#PeriodContentFrame {
    border: none;
    background: transparent;
}
"""
INVERTER_PERIOD_TABS_STYLE = tab_widget_qss(
    "QTabWidget#InverterPeriodTabs",
    pane_background="transparent",
    pane_border="none",
    tab_padding="10px 18px",
    tab_radius=8,
)
INVERTER_ANALYSIS_TABS_STYLE = tab_widget_qss(
    "QTabWidget#InverterAnalysisTabs",
    pane_background="rgba(10, 18, 32, 230)",
    tab_padding="8px 16px",
    tab_radius=8,
)


@dataclass(slots=True)
class InverterSeries:
    key: str
    label: str
    column: str
    color: str


class SeriesToggle(QCheckBox):
    """Colored checkbox used to toggle chart traces by series type."""
    def __init__(self, text: str, color: str, parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("SeriesToggle")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(SERIES_TOGGLE_STYLE_TEMPLATE.format(color=color))


class InverterAnalysisDialog(QDialog):
    _MONTH_ABBR = {
        1: "Jan",
        2: "Feb",
        3: "Mar",
        4: "Apr",
        5: "May",
        6: "Jun",
        7: "Jul",
        8: "Aug",
        9: "Sep",
        10: "Oct",
        11: "Nov",
        12: "Dec",
    }

    """Detailed inverter analytics dialog with multi-period Plotly views."""
    DAY_START_HOUR = 7
    NIGHT_START_HOUR = 23

    POWER_COLORS = {
        "pv_power": "#f5cf55",
        "load_power": "#38d6ff",
        "battery_power": "#3b82f6",
        "grid_power": "#22c55e",
        "soc": "#c084fc",
    }

    def __init__(
        self,
        processed: ProcessedData,
        *,
        device_name: str,
        day_zone_tariff_uah_per_kwh: float = 0.0,
        night_zone_tariff_uah_per_kwh: float = 0.0,
        parent: QWidget | None = None,
    ) -> None:
        """Initialize analysis datasets, controls and chart tabs."""
        super().__init__(parent)
        self._processed = processed
        self._device_name = device_name
        self._day_zone_tariff_uah_per_kwh = max(0.0, float(day_zone_tariff_uah_per_kwh or 0.0))
        self._night_zone_tariff_uah_per_kwh = max(0.0, float(night_zone_tariff_uah_per_kwh or 0.0))
        self._tariff_metrics_enabled = (
            self._day_zone_tariff_uah_per_kwh > 0.0
            and self._night_zone_tariff_uah_per_kwh > 0.0
        )
        self.setWindowTitle(tr_fragment(f"Inverter Analysis - {device_name}"))
        self.resize(1280, 860)
        self.setModal(True)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setProperty("popup_disable_backdrop", False)
        self.setProperty("popup_disable_focus_guard", False)
        self.setProperty("popup_disable_focus_recovery", False)
        # Mark as heavy-render popup so popup manager applies safer focus timing.
        self.setProperty("popup_heavy_render", True)

        self._analysis_frame = self._build_analysis_frame(processed)
        self._daily_energy = self._build_daily_energy_frame(self._analysis_frame)
        self._time_of_use_energy = self._build_time_of_use_energy_frame(self._analysis_frame)
        self._latest_timestamp = self._analysis_frame["timestamp"].max() if not self._analysis_frame.empty else pd.Timestamp.now()
        self._earliest_timestamp = self._analysis_frame["timestamp"].min() if not self._analysis_frame.empty else self._latest_timestamp
        self._selected_day = self._latest_timestamp.normalize()
        self._selected_month = self._latest_timestamp.to_period("M")
        self._selected_year = int(self._latest_timestamp.year)
        self._available_years = sorted(self._analysis_frame["timestamp"].dt.year.unique().tolist()) if not self._analysis_frame.empty else [self._selected_year]
        self._total_window_size = 6
        self._total_window_start = max(0, len(self._available_years) - self._total_window_size)

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(8)

        root.addWidget(
            create_neon_header_bar(
                self,
                self.reject,
                title=tr("Inverter Power Flow"),
            )
        )

        root.addSpacing(30)

        self.period_section_frame = QFrame()
        self.period_section_frame.setObjectName("PeriodSectionFrame")
        period_section_layout = QVBoxLayout(self.period_section_frame)
        period_section_layout.setContentsMargins(12, 12, 12, 12)
        period_section_layout.setSpacing(10)

        self.period_tabs = QTabWidget()
        self.period_tabs.setObjectName("InverterPeriodTabs")
        apply_tab_widget_interaction(self.period_tabs)
        self._period_pages: dict[str, QWidget] = {}
        for key, label in (("day", tr("Day")), ("month", tr("Month")), ("year", tr("Year")), ("total", tr("Total"))):
            page = QWidget()
            self._period_pages[key] = page
            self.period_tabs.addTab(page, self._tab_icon(key), label)
        self.period_tabs.currentChanged.connect(self._handle_period_tab_changed)
        period_section_layout.addWidget(self.period_tabs)

        self.period_content_frame = QFrame()
        self.period_content_frame.setObjectName("PeriodContentFrame")
        period_content_layout = QVBoxLayout(self.period_content_frame)
        period_content_layout.setContentsMargins(0, 0, 0, 0)
        period_content_layout.setSpacing(14)

        period_nav = QHBoxLayout()
        period_nav.setSpacing(8)
        self.period_prev_button = QPushButton(tr("Previous"))
        self.period_prev_button.setObjectName("SecondaryActionButton")
        self.period_period_button = PeriodPickerButton("--")
        self.period_period_button.setObjectName("PeriodLabelButton")
        self.period_period_button.setFlat(True)
        self.period_next_button = QPushButton(tr("Next"))
        self.period_next_button.setObjectName("SecondaryActionButton")
        self.period_prev_button.clicked.connect(self._go_prev_period)
        self.period_next_button.clicked.connect(self._go_next_period)
        self.period_period_button.clicked.connect(self._open_current_period_picker)
        period_nav.addWidget(self.period_prev_button)
        period_nav.addWidget(self.period_period_button, 1)
        period_nav.addWidget(self.period_next_button)
        period_content_layout.addLayout(period_nav)

        self.summary_cards = self._build_summary_cards()
        period_content_layout.addWidget(self.summary_cards)
        period_content_layout.addSpacing(8)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("InverterAnalysisTabs")
        apply_tab_widget_interaction(self.tabs)
        period_content_layout.addWidget(self.tabs, 1)

        self.power_tab, self.power_view, self.power_checks = self._build_chart_tab()
        self.energy_tab, self.energy_view, self.energy_checks = self._build_chart_tab()
        self.battery_tab, self.battery_view, self.battery_checks = self._build_chart_tab()
        self.kpi_tab = self._build_kpi_tab()

        self.tabs.addTab(self.power_tab, tr("Power"))
        self.tabs.addTab(self.energy_tab, tr("Energy"))
        self.tabs.addTab(self.battery_tab, tr("Battery"))
        self.tabs.addTab(self.kpi_tab, tr("KPIs"))
        self.tabs.currentChanged.connect(self._sync_summary_cards_visibility)

        period_section_layout.addWidget(self.period_content_frame, 1)
        root.addWidget(self.period_section_frame, 1)

        self._day_picker = CalendarPopup(
            self,
            minimum_date=QDate(self._earliest_timestamp.year, self._earliest_timestamp.month, self._earliest_timestamp.day),
            maximum_date=QDate(self._latest_timestamp.year, self._latest_timestamp.month, self._latest_timestamp.day),
        )
        self._month_picker = MonthPickerPopup(
            self._earliest_timestamp.year,
            self._latest_timestamp.year,
            self._latest_timestamp.month,
            self,
        )
        self._year_picker = YearPickerPopup(self._available_years, self)
        self._day_picker.dateSelected.connect(self._select_day_from_picker)
        self._month_picker.monthSelected.connect(self._select_month_from_picker)
        self._year_picker.yearSelected.connect(self._select_year_from_picker)

        self._apply_styles()
        self._refresh_period_controls()
        self._refresh_all_views()
        self._sync_summary_cards_visibility()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # Avoid surfacing Python exceptions through Qt's native show-event bridge.
        QTimer.singleShot(0, self._refresh_views_after_show_safe)

    def _refresh_views_after_show_safe(self) -> None:
        if not self.isVisible():
            return
        try:
            self._refresh_all_views()
            self.power_view.reload_current()
            self.energy_view.reload_current()
            self.battery_view.reload_current()
            self.kpi_view.reload_current()
        except Exception:
            LOGGER.exception("Failed to refresh inverter analysis views after show event")

    def _build_analysis_frame(self, processed: ProcessedData) -> pd.DataFrame:
        frame = processed.dataframe.copy()
        frame = frame[[processed.timestamp_column, *processed.numeric_columns]].copy()
        frame.rename(columns={processed.timestamp_column: "timestamp"}, inplace=True)
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
        frame.dropna(subset=["timestamp"], inplace=True)
        frame.sort_values("timestamp", inplace=True)
        frame.reset_index(drop=True, inplace=True)
        return frame

    def _build_daily_energy_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        columns = self._resolved_columns()
        working = frame[["timestamp", *[series.column for series in columns.values()]]].copy()
        working["next_timestamp"] = working["timestamp"].shift(-1)
        for key, series in columns.items():
            working[f"next_{key}"] = working[series.column].shift(-1)
        working.dropna(subset=["next_timestamp"], inplace=True)
        working["duration_hours"] = (working["next_timestamp"] - working["timestamp"]).dt.total_seconds() / 3600.0
        working = working[working["duration_hours"] > 0]
        if working.empty:
            return pd.DataFrame()

        segments = pd.DataFrame()
        segments["day"] = (working["timestamp"] + (working["next_timestamp"] - working["timestamp"]) / 2).dt.normalize()

        if "pv_power" in columns:
            segments["pv_generated_kwh"] = self._integrate_positive(working, "pv_power")
        if "load_power" in columns:
            segments["home_consumed_kwh"] = self._integrate_positive(working, "load_power")
        if "grid_power" in columns:
            segments["grid_import_kwh"] = self._integrate_positive(working, "grid_power")
            segments["grid_export_kwh"] = self._integrate_negative(working, "grid_power")
        if "battery_power" in columns:
            segments["battery_charge_kwh"] = self._integrate_positive(working, "battery_power")
            segments["battery_discharge_kwh"] = self._integrate_negative(working, "battery_power")

        grouped = segments.groupby("day", dropna=True).sum(numeric_only=True).reset_index()
        return grouped

    def _build_time_of_use_energy_frame(self, frame: pd.DataFrame) -> pd.DataFrame:
        columns = self._resolved_columns()
        load_series = columns.get("load_power")
        grid_series = columns.get("grid_power")
        if load_series is None and grid_series is None:
            return pd.DataFrame(columns=["period", "home_consumed_kwh", "grid_import_kwh"])

        selected_columns = ["timestamp"]
        if load_series is not None:
            selected_columns.append(load_series.column)
        if grid_series is not None:
            selected_columns.append(grid_series.column)
        working = frame[selected_columns].copy()
        working["next_timestamp"] = working["timestamp"].shift(-1)
        if load_series is not None:
            working["next_load_power"] = working[load_series.column].shift(-1)
        if grid_series is not None:
            working["next_grid_power"] = working[grid_series.column].shift(-1)
        working.dropna(subset=["next_timestamp"], inplace=True)
        working["duration_hours"] = (working["next_timestamp"] - working["timestamp"]).dt.total_seconds() / 3600.0
        working = working[working["duration_hours"] > 0]
        if working.empty:
            return pd.DataFrame(columns=["period", "home_consumed_kwh", "grid_import_kwh"])

        midpoint = working["timestamp"] + (working["next_timestamp"] - working["timestamp"]) / 2
        working["period"] = midpoint.dt.hour.map(self._classify_time_of_use_period)
        if load_series is not None:
            working["home_consumed_kwh"] = (
                (working[load_series.column].clip(lower=0) + working["next_load_power"].clip(lower=0)) / 2.0
            ) * working["duration_hours"]
        else:
            working["home_consumed_kwh"] = 0.0
        if grid_series is not None:
            working["grid_import_kwh"] = (
                (working[grid_series.column].clip(lower=0) + working["next_grid_power"].clip(lower=0)) / 2.0
            ) * working["duration_hours"]
        else:
            working["grid_import_kwh"] = 0.0
        return working.groupby("period", dropna=True)[["home_consumed_kwh", "grid_import_kwh"]].sum().reset_index()

    def _integrate_positive(self, frame: pd.DataFrame, key: str) -> pd.Series:
        return (
            (frame[self._resolved_columns()[key].column].clip(lower=0) + frame[f"next_{key}"].clip(lower=0)) / 2.0
        ) * frame["duration_hours"]

    def _integrate_negative(self, frame: pd.DataFrame, key: str) -> pd.Series:
        return (
            (frame[self._resolved_columns()[key].column].clip(upper=0).abs() + frame[f"next_{key}"].clip(upper=0).abs()) / 2.0
        ) * frame["duration_hours"]

    def _resolved_columns(self) -> dict[str, InverterSeries]:
        candidates = {
            "pv_power": ("PV_OUTPUT_POWER", "pv_output_power", "PV", "pv", "solar"),
            "grid_power": ("GRID_ACTIVE_POWER", "grid_active_power", "GRID", "grid"),
            "load_power": ("LOAD_ACTIVE_POWER", "load_active_power", "home_active_power", "HOME", "home", "load"),
            "battery_power": (
                "BATTERY_ACTIVE_POWER",
                "battery_active_power",
                "bt_battery_power",
                "battery_power",
                "BATTERY",
                "battery",
                "battary",
            ),
            "soc": ("SOC", "soc", "bt_battery_capacity"),
        }
        labels = {
            "pv_power": tr("PV generation"),
            "grid_power": tr("Grid"),
            "load_power": tr("Home load"),
            "battery_power": tr("Battery power"),
            "soc": tr("Battery SOC"),
        }
        resolved: dict[str, InverterSeries] = {}
        for key, names in candidates.items():
            match = next((column for column in self._processed.numeric_columns if column in names), None)
            if match is None:
                for column in self._processed.numeric_columns:
                    normalized = column.strip().lower()
                    if any(name.lower() in normalized for name in names):
                        match = column
                        break
            if match is None:
                continue
            resolved[key] = InverterSeries(
                key=key,
                label=labels[key],
                column=match,
                color=self.POWER_COLORS[key],
            )
        return resolved

    def _build_summary_cards(self) -> QWidget:
        widget = QWidget()
        grid = QGridLayout(widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(0)
        self._summary_cards_grid = grid

        cards = [
            ("generated", tr("Generated"), "#f5cf55"),
            ("consumed", tr("Consumed"), "#38d6ff"),
            ("grid_import", tr("Grid import"), "#22c55e"),
            ("grid_export", tr("Grid export"), "#86efac"),
            ("battery_charge", tr("Battery charge"), "#60a5fa"),
            ("battery_discharge", tr("Battery discharge"), "#93c5fd"),
        ]
        if self._tariff_metrics_enabled:
            cards.extend(
                [
                    ("home_cost", tr("Consumption cost"), "#f97316"),
                    ("grid_import_cost", tr("Grid import cost"), "#fb7185"),
                ]
            )
        self._summary_card_order = [key for key, _title, _color in cards]
        self._summary_card_values: dict[str, QLabel] = {}
        self._summary_cards_by_key: dict[str, QFrame] = {}
        for index, (key, title, color) in enumerate(cards):
            card = QFrame()
            card.setObjectName("InverterCard")
            card.setMinimumHeight(64)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(0, 0, 0, 0)
            card_layout.setSpacing(4)

            title_row = QHBoxLayout()
            title_row.setContentsMargins(0, 0, 0, 0)
            title_row.setSpacing(6)

            title_label = QLabel(title)
            title_label.setStyleSheet("color:#9cc4dd;font-size:11px;font-weight:600;")
            title_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            title_row.addWidget(title_label)
            title_row.addStretch(1)

            help_button = QPushButton("?")
            help_button.setObjectName("KpiHelpButton")
            help_button.setCursor(Qt.CursorShape.PointingHandCursor)
            help_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            help_button.clicked.connect(
                lambda _checked=False, metric_key=key, anchor=help_button: self._show_summary_help_tooltip(anchor, metric_key)
            )
            title_row.addWidget(help_button, 0, Qt.AlignmentFlag.AlignTop)

            value_label = QLabel("--")
            value_label.setStyleSheet(f"color:{color};font-size:20px;font-weight:700;")
            value_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

            card_layout.addLayout(title_row)
            card_layout.addWidget(value_label)
            grid.addWidget(card, 0, index)
            grid.setColumnStretch(index, 1)
            self._summary_card_values[key] = value_label
            self._summary_cards_by_key[key] = card
        return widget

    def _sum_daily(self, column: str, daily_frame: pd.DataFrame | None = None) -> str:
        source = self._daily_energy if daily_frame is None else daily_frame
        if source.empty or column not in source:
            return "--"
        return f"{source[column].sum():.1f} {tr('kWh')}"

    @staticmethod
    def _trim_trailing_zeros(value: float) -> str:
        return f"{float(value):.2f}".rstrip("0").rstrip(".")

    def _current_period_mode(self) -> str:
        index = self.period_tabs.currentIndex()
        return {0: "day", 1: "month", 2: "year", 3: "total"}.get(index, "day")

    def _period_bounds(self) -> tuple[pd.Timestamp, pd.Timestamp]:
        mode = self._current_period_mode()
        if mode == "day":
            start = pd.Timestamp(self._selected_day)
            return start, start + pd.Timedelta(days=1)
        if mode == "month":
            start = self._selected_month.to_timestamp()
            return start, start + pd.offsets.MonthEnd(0) + pd.Timedelta(days=1)
        if mode == "year":
            start = pd.Timestamp(year=self._selected_year, month=1, day=1)
            return start, pd.Timestamp(year=self._selected_year + 1, month=1, day=1)
        years = self._available_years[self._total_window_start : self._total_window_start + self._total_window_size]
        if not years:
            year = int(self._latest_timestamp.year)
            return pd.Timestamp(year=year, month=1, day=1), pd.Timestamp(year=year + 1, month=1, day=1)
        return pd.Timestamp(year=years[0], month=1, day=1), pd.Timestamp(year=years[-1] + 1, month=1, day=1)

    def _filtered_analysis_frame(self) -> pd.DataFrame:
        if self._analysis_frame.empty:
            return self._analysis_frame.copy()
        start, end = self._period_bounds()
        return self._analysis_frame[(self._analysis_frame["timestamp"] >= start) & (self._analysis_frame["timestamp"] < end)].copy()

    def _period_label(self) -> str:
        mode = self._current_period_mode()
        if mode == "day":
            return pd.Timestamp(self._selected_day).strftime("%d.%m.%Y")
        if mode == "month":
            return self._localized_month_year(self._selected_month.year, self._selected_month.month)
        if mode == "year":
            return str(self._selected_year)
        years = self._available_years[self._total_window_start : self._total_window_start + self._total_window_size]
        if not years:
            return str(int(self._latest_timestamp.year))
        return f"{years[0]} - {years[-1]}"

    def _localized_month_year(self, year: int, month: int) -> str:
        month_key = self._MONTH_ABBR.get(int(month), "")
        month_text = tr(month_key) if month_key else str(month)
        return f"{month_text} {int(year)}"

    def _refresh_period_controls(self) -> None:
        self.period_period_button.setText(self._period_label())
        mode = self._current_period_mode()
        nav_visible = mode != "total"
        self.period_prev_button.setVisible(nav_visible)
        self.period_period_button.setVisible(nav_visible)
        self.period_next_button.setVisible(nav_visible)
        self.period_period_button.setEnabled(mode != "total")
        if mode == "day":
            day = pd.Timestamp(self._selected_day)
            self.period_prev_button.setEnabled(day > self._earliest_timestamp.normalize())
            self.period_next_button.setEnabled(day < self._latest_timestamp.normalize())
        elif mode == "month":
            self.period_prev_button.setEnabled(self._selected_month > self._earliest_timestamp.to_period("M"))
            self.period_next_button.setEnabled(self._selected_month < self._latest_timestamp.to_period("M"))
        elif mode == "year":
            self.period_prev_button.setEnabled(self._selected_year > int(self._earliest_timestamp.year))
            self.period_next_button.setEnabled(self._selected_year < int(self._latest_timestamp.year))
        else:
            max_start = max(0, len(self._available_years) - self._total_window_size)
            self.period_prev_button.setEnabled(self._total_window_start > 0)
            self.period_next_button.setEnabled(self._total_window_start < max_start)

    def _handle_period_tab_changed(self, _index: int) -> None:
        self._refresh_period_controls()
        self._refresh_all_views()

    def _sync_summary_cards_visibility(self) -> None:
        current_tab = self.tabs.currentWidget()
        energy_active = current_tab is self.energy_tab
        kpi_active = current_tab is self.kpi_tab
        visible_keys: list[str] = []
        for key in self._summary_card_order:
            card = self._summary_cards_by_key[key]
            is_tariff_card = key in {"home_cost", "grid_import_cost"}
            visible = (energy_active and not is_tariff_card) or (kpi_active and is_tariff_card)
            card.setVisible(visible)
            if visible:
                visible_keys.append(key)

        for column, key in enumerate(visible_keys):
            self._summary_cards_grid.addWidget(self._summary_cards_by_key[key], 0, column)
            self._summary_cards_grid.setColumnStretch(column, 1)
        for column in range(len(visible_keys), len(self._summary_card_order)):
            self._summary_cards_grid.setColumnStretch(column, 0)

        self.summary_cards.setVisible(bool(visible_keys))

    def _open_current_period_picker(self) -> None:
        mode = self._current_period_mode()
        if mode == "day":
            selected = pd.Timestamp(self._selected_day)
            self._day_picker.open_for(self.period_period_button, QDate(selected.year, selected.month, selected.day))
            return
        if mode == "month":
            self._month_picker.open_for(self.period_period_button, self._selected_month.year, self._selected_month.month)
            return
        if mode == "year":
            self._year_picker.set_years(self._available_years)
            self._year_picker.open_for(self.period_period_button, self._selected_year)

    def _select_day_from_picker(self, value: QDate) -> None:
        self._selected_day = pd.Timestamp(year=value.year(), month=value.month(), day=value.day())
        self._refresh_period_controls()
        self._refresh_all_views()

    def _select_month_from_picker(self, year: int, month: int) -> None:
        self._selected_month = pd.Period(year=year, month=month, freq="M")
        self._refresh_period_controls()
        self._refresh_all_views()

    def _select_year_from_picker(self, year: int) -> None:
        self._selected_year = int(year)
        self._refresh_period_controls()
        self._refresh_all_views()

    def _go_prev_period(self) -> None:
        mode = self._current_period_mode()
        if mode == "day":
            min_day = self._earliest_timestamp.normalize()
            self._selected_day = max(min_day, pd.Timestamp(self._selected_day) - pd.Timedelta(days=1))
        elif mode == "month":
            min_month = self._earliest_timestamp.to_period("M")
            self._selected_month = max(min_month, self._selected_month - 1)
        elif mode == "year":
            self._selected_year = max(int(self._earliest_timestamp.year), self._selected_year - 1)
        else:
            self._total_window_start = max(0, self._total_window_start - 1)
        self._refresh_period_controls()
        self._refresh_all_views()

    def _go_next_period(self) -> None:
        mode = self._current_period_mode()
        if mode == "day":
            max_day = self._latest_timestamp.normalize()
            self._selected_day = min(max_day, pd.Timestamp(self._selected_day) + pd.Timedelta(days=1))
        elif mode == "month":
            max_month = self._latest_timestamp.to_period("M")
            self._selected_month = min(max_month, self._selected_month + 1)
        elif mode == "year":
            self._selected_year = min(int(self._latest_timestamp.year), self._selected_year + 1)
        else:
            max_start = max(0, len(self._available_years) - self._total_window_size)
            self._total_window_start = min(max_start, self._total_window_start + 1)
        self._refresh_period_controls()
        self._refresh_all_views()

    def _tab_icon(self, kind: str) -> QIcon:
        pixmap = QPixmap(18, 18)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        accent = QColor("#38bdf8")
        pen = QPen(accent, 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)

        if kind == "day":
            painter.drawEllipse(3, 3, 12, 12)
            painter.drawLine(9, 5, 9, 9)
            painter.drawLine(9, 9, 12, 11)
        elif kind == "month":
            painter.drawRoundedRect(2, 3, 14, 12, 3, 3)
            painter.drawLine(5, 2, 5, 6)
            painter.drawLine(13, 2, 13, 6)
            painter.drawLine(4, 8, 14, 8)
        elif kind == "year":
            painter.drawRect(3, 3, 12, 12)
            painter.drawLine(6, 6, 6, 12)
            painter.drawLine(9, 6, 9, 12)
            painter.drawLine(12, 6, 12, 12)
        else:
            painter.drawRect(3, 11, 2, 4)
            painter.drawRect(8, 8, 2, 7)
            painter.drawRect(13, 5, 2, 10)
        painter.end()
        return QIcon(pixmap)

    def _build_chart_tab(self) -> tuple[QWidget, GraphView, dict[str, QCheckBox]]:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(14)

        toggle_wrap = QScrollArea()
        toggle_wrap.setWidgetResizable(True)
        toggle_wrap.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        toggle_wrap.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        toggle_wrap.setFrameShape(QScrollArea.Shape.NoFrame)
        toggle_wrap.setFixedHeight(42)
        toggle_wrap.setStyleSheet("background: transparent; border: none;")

        toggle_content = QWidget()
        toggle_layout = QHBoxLayout(toggle_content)
        toggle_layout.setContentsMargins(0, 4, 0, 6)
        toggle_layout.setSpacing(16)
        toggle_layout.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        toggle_wrap.setWidget(toggle_content)
        layout.addWidget(toggle_wrap)

        graph = GraphView()
        graph.setContentsMargins(0, 6, 0, 0)
        layout.addWidget(graph, 1)
        return root, graph, {}

    def _build_kpi_tab(self) -> QWidget:
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        self.kpi_cards = self._build_kpi_summary_cards()
        layout.addWidget(self.kpi_cards)

        self.kpi_view = GraphView()
        layout.addWidget(self.kpi_view, 1)
        return root

    def _build_kpi_summary_cards(self) -> QWidget:
        widget = QWidget()
        grid = QGridLayout(widget)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(0)

        cards = [
            ("self_consumption", tr("Self-consumption")),
            ("self_sufficiency", tr("Self-sufficiency")),
            ("battery_support", tr("Battery support")),
        ]
        self._kpi_card_values: dict[str, QLabel] = {}
        for index, (key, title) in enumerate(cards):
            card = QFrame()
            card.setObjectName("InverterCard")
            card.setMinimumHeight(64)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(0, 0, 0, 0)
            card_layout.setSpacing(4)

            title_row = QHBoxLayout()
            title_row.setContentsMargins(0, 0, 0, 0)
            title_row.setSpacing(6)

            title_label = QLabel(title)
            title_label.setStyleSheet("color:#9cc4dd;font-size:11px;font-weight:600;")
            title_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            title_row.addWidget(title_label)
            title_row.addStretch(1)

            help_button = QPushButton("?")
            help_button.setObjectName("KpiHelpButton")
            help_button.setCursor(Qt.CursorShape.PointingHandCursor)
            help_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            help_button.clicked.connect(lambda _checked=False, metric_key=key, anchor=help_button: self._show_kpi_help_tooltip(anchor, metric_key))
            title_row.addWidget(help_button, 0, Qt.AlignmentFlag.AlignTop)

            value_label = QLabel("--")
            value_label.setStyleSheet("color:#f8fafc;font-size:20px;font-weight:700;")
            value_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)

            card_layout.addLayout(title_row)
            card_layout.addWidget(value_label)
            grid.addWidget(card, 0, index)
            grid.setColumnStretch(index, 1)
            self._kpi_card_values[key] = value_label
        return widget

    def _show_kpi_help_tooltip(self, button: QWidget, metric_key: str) -> None:
        tooltip_texts = {
            "self_consumption": tr("Shows what share of generated solar energy was used at home instead of being exported to the grid."),
            "self_sufficiency": tr("Shows what share of home consumption was covered by your own energy without importing from the grid."),
            "battery_support": tr("Shows what share of home consumption was covered by battery discharge during the selected period."),
        }
        text = tooltip_texts.get(metric_key, tr("KPI description is unavailable."))
        anchor = button.mapToGlobal(QPoint(button.width() // 2, button.height() + 8))
        QToolTip.showText(anchor, self._tooltip_html(text), button)

    def _show_summary_help_tooltip(self, button: QWidget, metric_key: str) -> None:
        tooltip_texts = {
            "generated": tr("Total PV energy generated for the selected period."),
            "consumed": tr("Total home energy consumption for the selected period."),
            "grid_import": tr("Total energy imported from grid for the selected period."),
            "grid_export": tr("Total energy exported to grid for the selected period."),
            "battery_charge": tr("Total energy used to charge battery for the selected period."),
            "battery_discharge": tr("Total energy discharged from battery for the selected period."),
            "home_cost": tr("Total home consumption cost for the selected period using day/night tariffs."),
            "grid_import_cost": tr("Total grid import cost for the selected period using day/night tariffs."),
        }
        text = tooltip_texts.get(metric_key, tr("Metric description is unavailable."))
        anchor = button.mapToGlobal(QPoint(button.width() // 2, button.height() + 8))
        QToolTip.showText(anchor, self._tooltip_html(text), button)

    @staticmethod
    def _tooltip_html(text: str, *, max_width: int = 320) -> str:
        safe = html.escape(text)
        return (
            f"<div style='max-width:{max_width}px; min-width:220px; white-space:normal;'>"
            f"{safe}"
            "</div>"
        )

    def _kpi_cards_html(self) -> str:
        pv_generated = self._metric_sum("pv_generated_kwh")
        home_consumed = self._metric_sum("home_consumed_kwh")
        grid_import = self._metric_sum("grid_import_kwh")
        grid_export = self._metric_sum("grid_export_kwh")
        battery_discharge = self._metric_sum("battery_discharge_kwh")

        self_consumption = ((pv_generated - grid_export) / pv_generated * 100.0) if pv_generated > 0 else 0.0
        self_sufficiency = ((home_consumed - grid_import) / home_consumed * 100.0) if home_consumed > 0 else 0.0
        battery_support = (battery_discharge / home_consumed * 100.0) if home_consumed > 0 else 0.0

        cards = [
            (tr("Self-consumption"), f"{self_consumption:.1f}%"),
            (tr("Self-sufficiency"), f"{self_sufficiency:.1f}%"),
            (tr("Battery support"), f"{battery_support:.1f}%"),
        ]
        chunks = [
            (
                "<div style='display:inline-block;width:31%;min-width:220px;margin:0 1% 10px 0;"
                "background:rgba(8,15,30,0.82);border:1px solid #243244;border-radius:16px;padding:16px;'>"
                f"<div style='font-size:12px;color:#9cc4dd;font-weight:600;'>{title}</div>"
                f"<div style='font-size:30px;color:#f8fafc;font-weight:700;margin-top:8px;'>{value}</div>"
                "</div>"
            )
            for title, value in cards
        ]
        return "".join(chunks)

    def _metric_sum(self, column: str, daily_frame: pd.DataFrame | None = None) -> float:
        source = self._daily_energy if daily_frame is None else daily_frame
        if source.empty or column not in source:
            return 0.0
        return float(source[column].sum())

    def _time_of_use_sum(
        self,
        period: str,
        tou_frame: pd.DataFrame | None = None,
        *,
        column: str = "home_consumed_kwh",
    ) -> float:
        source = self._time_of_use_energy if tou_frame is None else tou_frame
        if source.empty or column not in source:
            return 0.0
        matched = source[source["period"] == period]
        if matched.empty:
            return 0.0
        return float(matched[column].sum())

    def _classify_time_of_use_period(self, hour: int) -> str:
        if self.DAY_START_HOUR <= hour < self.NIGHT_START_HOUR:
            return "day"
        return "night"

    def _self_consumption(self, daily_frame: pd.DataFrame | None = None) -> float:
        pv_generated = self._metric_sum("pv_generated_kwh", daily_frame)
        grid_export = self._metric_sum("grid_export_kwh", daily_frame)
        return ((pv_generated - grid_export) / pv_generated * 100.0) if pv_generated > 0 else 0.0

    def _self_sufficiency(self, daily_frame: pd.DataFrame | None = None) -> float:
        home_consumed = self._metric_sum("home_consumed_kwh", daily_frame)
        grid_import = self._metric_sum("grid_import_kwh", daily_frame)
        return ((home_consumed - grid_import) / home_consumed * 100.0) if home_consumed > 0 else 0.0

    def _battery_support(self, daily_frame: pd.DataFrame | None = None) -> float:
        home_consumed = self._metric_sum("home_consumed_kwh", daily_frame)
        battery_discharge = self._metric_sum("battery_discharge_kwh", daily_frame)
        return (battery_discharge / home_consumed * 100.0) if home_consumed > 0 else 0.0

    def _refresh_all_views(self) -> None:
        self._refresh_summary_cards()
        self._build_power_controls()
        self._build_energy_controls()
        self._build_battery_controls()
        self._refresh_power_view()
        self._refresh_energy_view()
        self._refresh_battery_view()
        self._refresh_kpi_view()

    def _refresh_summary_cards(self) -> None:
        daily_frame = self._build_daily_energy_frame(self._filtered_analysis_frame())
        time_of_use = self._build_time_of_use_energy_frame(self._filtered_analysis_frame())
        values = {
            "generated": self._sum_daily("pv_generated_kwh", daily_frame),
            "consumed": self._sum_daily("home_consumed_kwh", daily_frame),
            "grid_import": self._sum_daily("grid_import_kwh", daily_frame),
            "grid_export": self._sum_daily("grid_export_kwh", daily_frame),
            "battery_charge": self._sum_daily("battery_charge_kwh", daily_frame),
            "battery_discharge": self._sum_daily("battery_discharge_kwh", daily_frame),
        }
        if self._tariff_metrics_enabled:
            day_consumed = self._time_of_use_sum("day", time_of_use)
            night_consumed = self._time_of_use_sum("night", time_of_use)
            day_grid_import = self._time_of_use_sum("day", time_of_use, column="grid_import_kwh")
            night_grid_import = self._time_of_use_sum("night", time_of_use, column="grid_import_kwh")
            values["home_cost"] = (
                f"{(day_consumed * self._day_zone_tariff_uah_per_kwh + night_consumed * self._night_zone_tariff_uah_per_kwh):.2f} {tr('UAH')}"
            )
            values["grid_import_cost"] = (
                f"{(day_grid_import * self._day_zone_tariff_uah_per_kwh + night_grid_import * self._night_zone_tariff_uah_per_kwh):.2f} {tr('UAH')}"
            )
        for key, card in self._summary_card_values.items():
            card.setText(values[key])

    def _replace_toggle_row(self, tab: QWidget, controls: dict[str, QCheckBox]) -> None:
        layout = tab.layout()
        row = layout.itemAt(0).widget()
        if row is not None:
            row.setVisible(len(controls) > 1)
        if isinstance(row, QScrollArea):
            container = row.widget()
            row_layout = container.layout() if container is not None else None
        else:
            row_layout = row.layout()
        if row_layout is None:
            return
        while row_layout.count():
            item = row_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for checkbox in controls.values():
            row_layout.addWidget(checkbox)

    def _build_power_controls(self) -> None:
        available = {key: series for key, series in self._resolved_columns().items() if key != "soc"}
        self.power_checks = self._build_checks(available, {"pv_power", "load_power", "battery_power", "grid_power"}, self._refresh_power_view)
        self._replace_toggle_row(self.power_tab, self.power_checks)

    def _build_energy_controls(self) -> None:
        daily_frame = self._build_daily_energy_frame(self._filtered_analysis_frame())
        energy_labels = {
            "pv_generated_kwh": (tr("PV generated"), "#f5cf55"),
            "home_consumed_kwh": (tr("Home consumed"), "#38d6ff"),
            "grid_import_kwh": (tr("Grid import"), "#22c55e"),
            "grid_export_kwh": (tr("Grid export"), "#86efac"),
            "battery_charge_kwh": (tr("Battery charge"), "#60a5fa"),
            "battery_discharge_kwh": (tr("Battery discharge"), "#93c5fd"),
        }
        available = {
            key: InverterSeries(key=key, label=label, column=key, color=color)
            for key, (label, color) in energy_labels.items()
            if not daily_frame.empty and key in daily_frame
        }
        self.energy_checks = self._build_checks(available, set(available.keys()), self._refresh_energy_view)
        self._replace_toggle_row(self.energy_tab, self.energy_checks)

    def _build_battery_controls(self) -> None:
        available = {
            key: series
            for key, series in self._resolved_columns().items()
            if key in {"battery_power", "soc"}
        }
        self.battery_checks = self._build_checks(available, set(available.keys()), self._refresh_battery_view)
        self._replace_toggle_row(self.battery_tab, self.battery_checks)

    def _build_checks(
        self,
        available: dict[str, InverterSeries],
        defaults: set[str],
        callback,
    ) -> dict[str, QCheckBox]:
        controls: dict[str, QCheckBox] = {}
        for key, series in available.items():
            check = SeriesToggle(series.label, series.color)
            check.setChecked(key in defaults)
            check.toggled.connect(callback)
            controls[key] = check
        return controls

    def _selected_keys(self, controls: dict[str, QCheckBox]) -> list[str]:
        return [key for key, checkbox in controls.items() if checkbox.isChecked()]

    def _refresh_power_view(self) -> None:
        selected = self._selected_keys(self.power_checks)
        if not selected:
            self.power_view.set_placeholder(tr("Enable at least one power series to inspect inverter behavior."))
            return
        frame = self._filtered_analysis_frame()
        if frame.empty:
            self.power_view.set_placeholder(tr("No power data for the selected period."))
            return
        mode = self._current_period_mode()
        figure = go.Figure()
        resolved = self._resolved_columns()
        for key in selected:
            series = resolved[key]
            x_values, y_values = self._power_series_for_period(frame, series.column, mode=mode)
            figure.add_trace(
                go.Scatter(
                    x=x_values,
                    y=y_values,
                    mode="lines",
                    name=series.label,
                    line={"color": series.color, "width": 2.6},
                    hovertemplate=f"{series.label}: %{{y:.2~f}} kW<extra></extra>",
                )
            )
        figure.add_hline(y=0, line_color="#64748b", line_dash="dot")
        self._style_figure(figure, y_title=tr("Power (kW)"), xaxis_extra=self._xaxis_extra_for_period(x_values, mode=mode))
        self.power_view.set_figure(figure)

    def _refresh_energy_view(self) -> None:
        selected = self._selected_keys(self.energy_checks)
        if not selected:
            self.energy_view.set_placeholder(tr("Enable at least one energy series to compare daily totals."))
            return
        mode = self._current_period_mode()
        frame = self._energy_frame_for_period(mode=mode)
        if frame.empty:
            self.energy_view.set_placeholder(tr("Not enough saved history was found to calculate energy totals for this period."))
            return
        figure = go.Figure()
        for key in selected:
            checkbox = self.energy_checks[key]
            del checkbox
            label = self._energy_label(key)
            color = self._energy_color(key)
            figure.add_trace(
                go.Bar(
                    x=frame["period"],
                    y=frame[key],
                    name=label,
                    marker_color=color,
                    hovertemplate=f"{label}: %{{y:.2~f}} kWh<extra></extra>",
                )
            )
        self._style_figure(
            figure,
            y_title=tr("Energy (kWh)"),
            barmode="stack",
            xaxis_extra=self._xaxis_extra_for_period(frame["period"], mode=mode),
        )
        self.energy_view.set_figure(figure)

    def _energy_label(self, key: str) -> str:
        return {
            "pv_generated_kwh": tr("PV generated"),
            "home_consumed_kwh": tr("Home consumed"),
            "grid_import_kwh": tr("Grid import"),
            "grid_export_kwh": tr("Grid export"),
            "battery_charge_kwh": tr("Battery charge"),
            "battery_discharge_kwh": tr("Battery discharge"),
        }.get(key, key)

    def _energy_color(self, key: str) -> str:
        return {
            "pv_generated_kwh": "#f5cf55",
            "home_consumed_kwh": "#38d6ff",
            "grid_import_kwh": "#22c55e",
            "grid_export_kwh": "#86efac",
            "battery_charge_kwh": "#60a5fa",
            "battery_discharge_kwh": "#93c5fd",
        }.get(key, "#cbd5e1")

    def _refresh_battery_view(self) -> None:
        selected = self._selected_keys(self.battery_checks)
        if not selected:
            self.battery_view.set_placeholder(tr("Enable battery power or SOC to explore storage behavior."))
            return
        frame = self._filtered_analysis_frame()
        if frame.empty:
            self.battery_view.set_placeholder(tr("No battery data for the selected period."))
            return
        mode = self._current_period_mode()
        resolved = self._resolved_columns()
        figure = go.Figure()
        use_secondary = "soc" in selected and "battery_power" in selected
        for key in selected:
            series = resolved[key]
            x_values, y_values = self._power_series_for_period(frame, series.column, mode=mode)
            trace = go.Scatter(
                x=x_values,
                y=y_values,
                mode="lines",
                name=series.label,
                line={"color": series.color, "width": 2.6},
                yaxis="y2" if use_secondary and key == "soc" else "y",
                hovertemplate=(
                    f"{series.label}: %{{y:.2~f}} %<extra></extra>"
                    if key == "soc"
                    else f"{series.label}: %{{y:.2~f}} kW<extra></extra>"
                ),
            )
            figure.add_trace(trace)
        self._style_figure(figure, y_title=tr("Power (kW) / SOC (%)"), xaxis_extra=self._xaxis_extra_for_period(x_values, mode=mode))
        if use_secondary:
            figure.update_layout(
                yaxis2={
                    "title": tr("SOC (%)"),
                    "overlaying": "y",
                    "side": "right",
                    "showgrid": False,
                    "color": "#c084fc",
                }
            )
        self.battery_view.set_figure(figure)

    def _style_figure(
        self,
        figure: go.Figure,
        *,
        y_title: str,
        barmode: str = "group",
        xaxis_extra: dict[str, object] | None = None,
    ) -> None:
        xaxis = {"gridcolor": "#22304a", "title": ""}
        if xaxis_extra:
            xaxis.update(xaxis_extra)
        figure.update_layout(
            paper_bgcolor="#0b1220",
            plot_bgcolor="#0b1220",
            font={"color": "#dbeafe"},
            hoverlabel={
                "bgcolor": "#0b1220",
                "bordercolor": "#3b4d63",
                "font": {"color": "#dbeafe", "size": 13},
            },
            hovermode="x unified",
            margin={"l": 40, "r": 40, "t": 20, "b": 88},
            showlegend=False,
            barmode=barmode,
            xaxis=xaxis,
            yaxis={"gridcolor": "#22304a", "title": y_title},
        )

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            compose_styles(
                INVERTER_PERIOD_TABS_STYLE,
                INVERTER_ANALYSIS_TABS_STYLE,
                INVERTER_ANALYSIS_DIALOG_STYLE,
                GLOBAL_TOOLTIP_QSS,
                NEON_CLOSE_BUTTON_STYLE,
                NEON_HEADER_BAR_STYLE,
            )
        )

    def _power_series_for_period(self, frame: pd.DataFrame, column: str, *, mode: str) -> tuple[pd.Series, pd.Series]:
        if mode == "day":
            return frame["timestamp"], pd.to_numeric(frame[column], errors="coerce").fillna(0.0)
        if mode == "month":
            grouped = frame.groupby(frame["timestamp"].dt.normalize())[column].mean().reset_index()
            grouped.columns = ["timestamp", "value"]
            return grouped["timestamp"], pd.to_numeric(grouped["value"], errors="coerce").fillna(0.0)
        if mode == "year":
            grouped = frame.groupby(frame["timestamp"].dt.to_period("M"))[column].mean().reset_index()
            grouped["timestamp"] = grouped["timestamp"].dt.to_timestamp()
            return grouped["timestamp"], pd.to_numeric(grouped[column], errors="coerce").fillna(0.0)
        grouped = frame.groupby(frame["timestamp"].dt.year)[column].mean().reset_index()
        grouped["timestamp"] = pd.to_datetime(grouped["timestamp"], format="%Y", errors="coerce")
        return grouped["timestamp"], pd.to_numeric(grouped[column], errors="coerce").fillna(0.0)

    def _energy_frame_for_period(self, *, mode: str) -> pd.DataFrame:
        analysis = self._filtered_analysis_frame()
        daily = self._build_daily_energy_frame(analysis)
        if daily.empty:
            return pd.DataFrame()
        if mode == "day":
            hourly = analysis.copy()
            if hourly.empty:
                return pd.DataFrame()
            cols = self._resolved_columns()
            working = hourly[["timestamp", *[series.column for series in cols.values()]]].copy()
            working["next_timestamp"] = working["timestamp"].shift(-1)
            for key, series in cols.items():
                working[f"next_{key}"] = working[series.column].shift(-1)
            working.dropna(subset=["next_timestamp"], inplace=True)
            working["duration_hours"] = (working["next_timestamp"] - working["timestamp"]).dt.total_seconds() / 3600.0
            working = working[working["duration_hours"] > 0]
            if working.empty:
                return pd.DataFrame()
            segments = pd.DataFrame()
            segments["period"] = working["timestamp"].dt.floor("h")
            if "pv_power" in cols:
                segments["pv_generated_kwh"] = self._integrate_positive(working, "pv_power")
            if "load_power" in cols:
                segments["home_consumed_kwh"] = self._integrate_positive(working, "load_power")
            if "grid_power" in cols:
                segments["grid_import_kwh"] = self._integrate_positive(working, "grid_power")
                segments["grid_export_kwh"] = self._integrate_negative(working, "grid_power")
            if "battery_power" in cols:
                segments["battery_charge_kwh"] = self._integrate_positive(working, "battery_power")
                segments["battery_discharge_kwh"] = self._integrate_negative(working, "battery_power")
            return segments.groupby("period", dropna=True).sum(numeric_only=True).reset_index()
        if mode == "month":
            frame = daily.copy()
            frame = frame.rename(columns={"day": "period"})
            return frame
        if mode == "year":
            frame = daily.copy()
            frame["period"] = frame["day"].dt.to_period("M").dt.to_timestamp()
            numeric_columns = [column for column in frame.columns if column.endswith("_kwh")]
            return frame.groupby("period", dropna=True)[numeric_columns].sum().reset_index()
        frame = daily.copy()
        frame["period"] = frame["day"].dt.year
        numeric_columns = [column for column in frame.columns if column.endswith("_kwh")]
        grouped = frame.groupby("period", dropna=True)[numeric_columns].sum().reset_index()
        grouped["period"] = pd.to_datetime(grouped["period"], format="%Y", errors="coerce")
        return grouped

    def _xaxis_extra_for_period(self, x_values, *, mode: str) -> dict[str, object]:
        xaxis_extra: dict[str, object] = {"showgrid": True, "zeroline": False, "showline": False}
        x_dt = pd.to_datetime(x_values, errors="coerce")
        if x_dt.empty or x_dt.isna().any():
            return xaxis_extra
        if mode == "day":
            xaxis_extra.update(
                {
                    "tickformat": "%H:%M",
                    "hoverformat": "%H:%M:%S",
                }
            )
        if mode == "month":
            weekend_mask = x_dt.dt.weekday >= 5
            xaxis_extra.update(
                {
                    "tickmode": "array",
                    "tickvals": x_dt.tolist(),
                    "ticktext": [
                        f"<span style='color:#ef4444'>{day}</span>" if weekend else str(day)
                        for day, weekend in zip(x_dt.dt.day.tolist(), weekend_mask.tolist(), strict=False)
                    ],
                }
            )
        elif mode == "year":
            month_labels = {
                1: tr("Jan"),
                2: tr("Feb"),
                3: tr("Mar"),
                4: tr("Apr"),
                5: tr("May"),
                6: tr("Jun"),
                7: tr("Jul"),
                8: tr("Aug"),
                9: tr("Sep"),
                10: tr("Oct"),
                11: tr("Nov"),
                12: tr("Dec"),
            }
            xaxis_extra.update(
                {
                    "tickmode": "array",
                    "tickvals": x_dt.tolist(),
                    "ticktext": [month_labels.get(int(month), str(month)) for month in x_dt.dt.month.tolist()],
                    "hoverformat": "%m.%Y",
                }
            )
        elif mode == "total":
            xaxis_extra.update(
                {
                    "tickmode": "array",
                    "tickvals": x_dt.tolist(),
                    "ticktext": x_dt.dt.year.astype(str).tolist(),
                    "hoverformat": "%Y",
                }
            )
        return xaxis_extra

    def _refresh_kpi_view(self) -> None:
        filtered = self._filtered_analysis_frame()
        daily_energy = self._build_daily_energy_frame(filtered)
        time_of_use = self._build_time_of_use_energy_frame(filtered)
        pv_generated = self._metric_sum("pv_generated_kwh", daily_energy)
        home_consumed = self._metric_sum("home_consumed_kwh", daily_energy)
        grid_import = self._metric_sum("grid_import_kwh", daily_energy)
        grid_export = self._metric_sum("grid_export_kwh", daily_energy)
        battery_discharge = self._metric_sum("battery_discharge_kwh", daily_energy)
        day_consumed = self._time_of_use_sum("day", time_of_use)
        night_consumed = self._time_of_use_sum("night", time_of_use)
        day_grid_import = self._time_of_use_sum("day", time_of_use, column="grid_import_kwh")
        night_grid_import = self._time_of_use_sum("night", time_of_use, column="grid_import_kwh")
        day_percent = (day_consumed / home_consumed * 100.0) if home_consumed > 0 else 0.0
        night_percent = (night_consumed / home_consumed * 100.0) if home_consumed > 0 else 0.0
        day_grid_percent = (day_grid_import / grid_import * 100.0) if grid_import > 0 else 0.0
        night_grid_percent = (night_grid_import / grid_import * 100.0) if grid_import > 0 else 0.0

        self._kpi_card_values["self_consumption"].setText(f"{self._self_consumption(daily_energy):.1f}%")
        self._kpi_card_values["self_sufficiency"].setText(f"{self._self_sufficiency(daily_energy):.1f}%")
        self._kpi_card_values["battery_support"].setText(f"{self._battery_support(daily_energy):.1f}%")

        pv_direct = max(0.0, home_consumed - grid_import - battery_discharge)

        figure = make_subplots(
            rows=1,
            cols=2,
            specs=[[{"type": "domain"}, {"type": "xy"}]],
            column_widths=[0.42, 0.58],
        )
        figure.add_trace(
            go.Pie(
                labels=[tr("PV direct"), tr("Battery"), tr("Grid")],
                values=[pv_direct, battery_discharge, grid_import],
                hole=0.56,
                marker={"colors": ["#f5cf55", "#3b82f6", "#22c55e"]},
                sort=False,
                textinfo="label+percent",
                customdata=["#f5cf55", "#3b82f6", "#22c55e"],
                hovertemplate=(
                    "<span style='color:%{customdata};font-size:14px;'>■</span> "
                    "%{label}: %{value:.2~f} kWh (%{percent})<extra></extra>"
                ),
            ),
            row=1,
            col=1,
        )
        figure.add_trace(
            go.Bar(
                x=[tr("PV generated"), tr("Home consumed"), tr("Grid import"), tr("Grid export")],
                y=[pv_generated, None, None, None],
                marker_color="#f5cf55",
                name=tr("PV generated"),
                hovertemplate=f"{tr('PV generated')}: "+"%{y:.2~f} kWh<extra></extra>",
            ),
            row=1,
            col=2,
        )
        figure.add_trace(
            go.Bar(
                x=[tr("PV generated"), tr("Home consumed"), tr("Grid import"), tr("Grid export")],
                y=[None, day_consumed, day_grid_import, None],
                marker_color="#0ea5e9",
                name=tr("Day"),
                text=["", f"{day_percent:.0f}%", f"{day_grid_percent:.0f}%", ""],
                textposition="inside",
                customdata=[
                    [None, None, None],
                    [day_consumed, day_percent, tr("Home consumed")],
                    [day_grid_import, day_grid_percent, tr("Grid import")],
                    [None, None, None],
                ],
                hovertemplate=f"%{{customdata[2]}} {tr('Day')}: %{{customdata[0]:.2~f}} kWh (%{{customdata[1]:.2~f}}%)<extra></extra>",
            ),
            row=1,
            col=2,
        )
        figure.add_trace(
            go.Bar(
                x=[tr("PV generated"), tr("Home consumed"), tr("Grid import"), tr("Grid export")],
                y=[None, night_consumed, night_grid_import, None],
                marker_color="#2563eb",
                name=tr("Night"),
                text=["", f"{night_percent:.0f}%", f"{night_grid_percent:.0f}%", ""],
                textposition="inside",
                customdata=[
                    [None, None, None],
                    [night_consumed, night_percent, tr("Home consumed")],
                    [night_grid_import, night_grid_percent, tr("Grid import")],
                    [None, None, None],
                ],
                hovertemplate=f"%{{customdata[2]}} {tr('Night')}: %{{customdata[0]:.2~f}} kWh (%{{customdata[1]:.2~f}}%)<extra></extra>",
            ),
            row=1,
            col=2,
        )
        figure.add_trace(
            go.Scatter(
                x=[tr("Home consumed")],
                y=[home_consumed],
                mode="markers",
                marker={"size": 0.1, "color": "rgba(0,0,0,0)"},
                name=tr("Total consumed"),
                hovertemplate=f"{tr('Total')}: {self._trim_trailing_zeros(home_consumed)} kWh (100%)<extra></extra>",
                showlegend=False,
            ),
            row=1,
            col=2,
        )
        figure.add_trace(
            go.Scatter(
                x=[tr("Grid import")],
                y=[grid_import],
                mode="markers",
                marker={"size": 0.1, "color": "rgba(0,0,0,0)"},
                name=tr("Total grid import"),
                hovertemplate=f"{tr('Total')}: {self._trim_trailing_zeros(grid_import)} kWh (100%)<extra></extra>",
                showlegend=False,
            ),
            row=1,
            col=2,
        )
        figure.add_trace(
            go.Bar(
                x=[tr("PV generated"), tr("Home consumed"), tr("Grid import"), tr("Grid export")],
                y=[None, None, None, grid_export],
                marker_color="#86efac",
                name=tr("Grid export"),
                hovertemplate=f"{tr('Grid export')}: "+"%{y:.2~f} kWh<extra></extra>",
            ),
            row=1,
            col=2,
        )
        self._style_figure(figure, y_title=tr("Energy (kWh)"), barmode="stack")
        self.kpi_view.set_figure(figure)
