from __future__ import annotations

import calendar
import time
from dataclasses import dataclass
from typing import Callable

import pandas as pd
import plotly.graph_objects as go
from PySide6.QtCore import QDate, QEvent, QLocale, QPoint, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPen, QPixmap
from PySide6.QtWidgets import (
    QCalendarWidget,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QSizePolicy,
)

from app.components.chart import apply_energyflow_chart_style
from app.services.i18n import current_language, tr, tr_fragment
from app.services.logging_utils import get_logger
from app.ui.design_system import apply_tab_widget_interaction, compose_styles, tab_widget_qss
from app.ui.dialogs import NEON_CLOSE_BUTTON_STYLE, NEON_HEADER_BAR_STYLE, create_neon_header_bar
from app.ui.graph_view import GraphView


PV_COLOR = "#f5cf55"
LOGGER = get_logger(__name__)

CALENDAR_POPUP_STYLE = """
    #CalendarPopup {
        background: #0f172a;
        border: 1px solid #334155;
        border-radius: 12px;
    }
    #CalendarPopup QLabel {
        color: #e2e8f0;
        font-size: 14px;
        font-weight: 600;
    }
    #CalendarPopup QPushButton {
        background: #13213b;
        color: #d9f7ff;
        border: 1px solid #2e4663;
        border-radius: 10px;
        padding: 6px 12px;
        min-height: 22px;
        font-size: 13px;
        font-weight: 600;
    }
    #CalendarPopup QPushButton:hover {
        background: #1a2f4f;
        color: #f0fdff;
        border-color: #38bdf8;
    }
    #CalendarPopup QPushButton:pressed {
        background: #11243f;
    }
    #CalendarPopup QPushButton:disabled {
        background: #172031;
        color: #64748b;
        border-color: #263244;
    }
    #CalendarPopup QPushButton[selectedYear="true"] {
        background: rgba(14, 165, 233, 0.18);
        color: #f8fafc;
        border-color: #22d3ee;
    }
    #CalendarPopup QCalendarWidget QWidget {
        background: #111827;
        color: #e2e8f0;
    }
    #CalendarPopup QCalendarWidget QToolButton {
        background: #1e293b;
        color: #e2e8f0;
        border: 1px solid #334155;
        border-radius: 6px;
        padding: 4px 8px;
    }
    #CalendarPopup QCalendarWidget QToolButton:disabled {
        background: #172031;
        color: #64748b;
        border-color: #263244;
    }
    #CalendarPopup QCalendarWidget QToolButton#qt_calendar_prevmonth,
    #CalendarPopup QCalendarWidget QToolButton#qt_calendar_nextmonth {
        color: #f8fafc;
        font-size: 16px;
        font-weight: 700;
        min-width: 44px;
    }
    #CalendarPopup QCalendarWidget QToolButton#qt_calendar_monthbutton,
    #CalendarPopup QCalendarWidget QToolButton#qt_calendar_yearbutton {
        color: #f8fafc;
        font-weight: 700;
    }
    #CalendarPopup QCalendarWidget QToolButton#qt_calendar_monthbutton::menu-indicator {
        image: none;
        width: 0;
    }
    #CalendarPopup QCalendarWidget QMenu {
        background: rgba(15, 23, 42, 252);
        color: #e2e8f0;
        border: 1px solid #334155;
        border-radius: 8px;
        padding: 4px;
    }
    #CalendarPopup QCalendarWidget QMenu::item {
        background: rgba(15, 23, 42, 252);
        padding: 6px 10px;
        border-radius: 6px;
    }
    #CalendarPopup QCalendarWidget QMenu::item:selected {
        background: rgba(30, 41, 59, 252);
        color: #f8fafc;
    }
    #CalendarPopup QCalendarWidget QSpinBox {
        background: #111827;
        color: #e2e8f0;
        selection-background-color: #0ea5e9;
        selection-color: #eff6ff;
    }
    #CalendarPopup QCalendarWidget QAbstractItemView {
        background: #111827;
        color: #e2e8f0;
        selection-background-color: #2563eb;
        selection-color: #eff6ff;
        alternate-background-color: #0f172a;
        gridline-color: #1f2937;
    }
    #CalendarPopup QCalendarWidget QAbstractItemView:disabled {
        color: #64748b;
        background: #0d1424;
    }
"""

CALENDAR_NAV_BUTTON_STYLE = "color: #f8fafc; background: #1e293b;"
CALENDAR_MONTH_BUTTON_STYLE = "color: #f8fafc; padding-right: 12px; background: transparent;"
CALENDAR_YEAR_BUTTON_STYLE = "color: #f8fafc; background: transparent;"
CALENDAR_POPUP_TITLE_STYLE = "color:#f8fafc;font-size:15px;font-weight:600;"
SUMMARY_LABEL_STYLE = """
color:#f8fafc;
padding:14px 18px;
border-radius:14px;
background:qlineargradient(
    x1:0, y1:0, x2:1, y2:1,
    stop:0 rgba(22, 30, 48, 245),
    stop:1 rgba(12, 20, 34, 238)
);
"""

POWER_HISTORY_DIALOG_STYLE = """
QDialog {
    background: #0b1220;
    color: #e2e8f0;
}
QPushButton {
    background: #13213b;
    border: 1px solid #26405e;
    border-radius: 7px;
    padding: 6px 11px;
    color: #e6fbff;
}
QPushButton:hover {
    border-color: #3cc8f4;
}
QPushButton:disabled {
    background: #162132;
    border-color: #2c394d;
    color: #64748b;
}
QPushButton[flat="true"] {
    background: transparent;
    border: 1px solid rgba(56, 189, 248, 0.14);
    border-radius: 10px;
    color: #bff6ff;
    font-size: 13px;
    font-weight: 500;
    padding: 8px 12px;
    text-align: center;
}
QPushButton[flat="true"]:hover {
    border-color: rgba(56, 189, 248, 0.42);
    background: rgba(14, 165, 233, 0.08);
}
QPushButton[flat="true"]:disabled {
    border-color: rgba(71, 85, 105, 0.35);
    color: #64748b;
    background: rgba(15, 23, 42, 0.42);
}
QPushButton[selectedYear="true"] {
    border-color: #3cc8f4;
    background: rgba(14, 165, 233, 0.16);
    color: #f8fafc;
}
"""
POWER_HISTORY_TABS_STYLE = tab_widget_qss(
    "QTabWidget#HistoryTabs",
    pane_background="rgba(10, 18, 32, 230)",
    tab_padding="10px 18px",
    tab_radius=8,
)


class PeriodPickerButton(QPushButton):
    """Flat trigger button used for day/month/year period pickers."""
    def __init__(self, text: str = "--", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFlat(True)


class CalendarPopup(QDialog):
    """Popup calendar styled to match the project dark modal theme."""
    dateSelected = Signal(QDate)

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        minimum_date: QDate | None = None,
        maximum_date: QDate | None = None,
    ) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("CalendarPopup")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(CALENDAR_POPUP_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.calendar_widget = QCalendarWidget()
        if current_language() == "uk":
            self.calendar_widget.setLocale(QLocale(QLocale.Language.Ukrainian, QLocale.Country.Ukraine))
        else:
            self.calendar_widget.setLocale(QLocale(QLocale.Language.English, QLocale.Country.UnitedStates))
        if minimum_date is not None:
            self.calendar_widget.setMinimumDate(minimum_date)
        if maximum_date is not None:
            self.calendar_widget.setMaximumDate(maximum_date)
        self.calendar_widget.clicked.connect(self._handle_clicked)
        self.calendar_widget.currentPageChanged.connect(self._refresh_navigation)
        calendar_palette = self.calendar_widget.palette()
        disabled_gray = QColor("#64748b")
        calendar_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled_gray)
        calendar_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled_gray)
        calendar_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled_gray)
        self.calendar_widget.setPalette(calendar_palette)
        layout.addWidget(self.calendar_widget)
        self._refresh_navigation()

    def open_for(self, anchor: QWidget, selected_date: QDate) -> None:
        """Show popup right under anchor and preselect the provided date."""
        self.calendar_widget.setSelectedDate(selected_date)
        self.calendar_widget.setCurrentPage(selected_date.year(), selected_date.month())
        popup_anchor = anchor.mapToGlobal(QPoint(0, anchor.height()))
        self.move(popup_anchor)
        self.show()
        QTimer.singleShot(0, self._refresh_navigation)

    def _handle_clicked(self, value: QDate) -> None:
        self.dateSelected.emit(value)
        self.hide()

    def _refresh_navigation(self) -> None:
        # Override native Qt calendar arrows/buttons to keep visual consistency
        # with neon controls used in the rest of the dialogs.
        prev_button = self.calendar_widget.findChild(QToolButton, "qt_calendar_prevmonth")
        next_button = self.calendar_widget.findChild(QToolButton, "qt_calendar_nextmonth")
        month_button = self.calendar_widget.findChild(QToolButton, "qt_calendar_monthbutton")
        year_button = self.calendar_widget.findChild(QToolButton, "qt_calendar_yearbutton")
        if prev_button is not None:
            prev_button.setText("◀")
            prev_button.setStyleSheet(CALENDAR_NAV_BUTTON_STYLE)
            prev_button.setArrowType(Qt.ArrowType.NoArrow)
            prev_button.setMinimumWidth(44)
        if next_button is not None:
            next_button.setText("▶")
            next_button.setStyleSheet(CALENDAR_NAV_BUTTON_STYLE)
            next_button.setArrowType(Qt.ArrowType.NoArrow)
            next_button.setMinimumWidth(44)
        if month_button is not None:
            locale = self.calendar_widget.locale()
            short_month = locale.monthName(self.calendar_widget.monthShown(), QLocale.FormatType.ShortFormat).rstrip(". ")
            full_month = locale.monthName(self.calendar_widget.monthShown(), QLocale.FormatType.LongFormat)
            month_button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            month_button.setMinimumWidth(88)
            month_button.setMaximumWidth(16777215)
            available = month_button.width() - 16
            if available < 56:
                available = 120
            month_label = month_button.fontMetrics().elidedText(
                f"{short_month} ▾",
                Qt.TextElideMode.ElideRight,
                available,
            )
            month_button.setText(month_label)
            month_button.setToolTip(full_month)
            month_button.setStyleSheet(CALENDAR_MONTH_BUTTON_STYLE)
        if year_button is not None:
            year_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            year_button.setFixedWidth(84)
            year_button.setToolTip(str(self.calendar_widget.yearShown()))
            year_button.setStyleSheet(CALENDAR_YEAR_BUTTON_STYLE)


class MonthPickerPopup(QDialog):
    """Popup month selector with explicit year navigation."""
    monthSelected = Signal(int, int)

    def __init__(self, minimum_year: int, maximum_year: int, maximum_month: int, parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("CalendarPopup")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(CALENDAR_POPUP_STYLE)
        self._minimum_year = minimum_year
        self._maximum_year = maximum_year
        self._maximum_month = maximum_month
        self._selected_year = maximum_year

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        header = QHBoxLayout()
        header.setSpacing(8)
        self._prev_year_button = QPushButton("◀")
        self._prev_year_button.clicked.connect(self._go_prev_year)
        self._year_label = QLabel("--")
        self._year_label.setStyleSheet(CALENDAR_POPUP_TITLE_STYLE)
        self._year_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._next_year_button = QPushButton("▶")
        self._next_year_button.clicked.connect(self._go_next_year)
        header.addWidget(self._prev_year_button)
        header.addWidget(self._year_label, 1)
        header.addWidget(self._next_year_button)
        layout.addLayout(header)

        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(8)
        self._grid.setVerticalSpacing(8)
        self._month_buttons: dict[int, QPushButton] = {}
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
        for index in range(12):
            button = QPushButton(month_labels.get(index + 1, calendar.month_abbr[index + 1]))
            button.clicked.connect(lambda _checked=False, month=index + 1: self._select_month(month))
            self._grid.addWidget(button, index // 3, index % 3)
            self._month_buttons[index + 1] = button
        layout.addLayout(self._grid)
        self._refresh_year_state()

    def open_for(self, anchor: QWidget, selected_year: int, selected_month: int) -> None:
        self._selected_year = max(self._minimum_year, min(self._maximum_year, selected_year))
        self._refresh_year_state(selected_month)
        popup_anchor = anchor.mapToGlobal(QPoint(0, anchor.height()))
        self.move(popup_anchor)
        self.show()

    def _select_month(self, month: int) -> None:
        self.monthSelected.emit(self._selected_year, month)
        self.hide()

    def _go_prev_year(self) -> None:
        if self._selected_year > self._minimum_year:
            self._selected_year -= 1
            self._refresh_year_state()

    def _go_next_year(self) -> None:
        if self._selected_year < self._maximum_year:
            self._selected_year += 1
            self._refresh_year_state()

    def _refresh_year_state(self, selected_month: int | None = None) -> None:
        # `selectedYear` is a custom dynamic property used by QSS to highlight
        # the active month button. Re-polish is required for runtime updates.
        self._year_label.setText(str(self._selected_year))
        self._prev_year_button.setEnabled(self._selected_year > self._minimum_year)
        self._next_year_button.setEnabled(self._selected_year < self._maximum_year)
        for month, button in self._month_buttons.items():
            is_future = self._selected_year == self._maximum_year and month > self._maximum_month
            button.setEnabled(not is_future)
            button.setProperty("selectedYear", month == selected_month)
            button.style().unpolish(button)
            button.style().polish(button)
            button.update()


class YearPickerPopup(QDialog):
    """Popup selector for year-only navigation mode."""
    yearSelected = Signal(int)

    def __init__(self, years: list[int], parent: QWidget | None = None) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("CalendarPopup")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(CALENDAR_POPUP_STYLE)
        self._years = years
        self._buttons: dict[int, QPushButton] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title = QLabel(tr("Select year"))
        title.setStyleSheet(CALENDAR_POPUP_TITLE_STYLE)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        self._grid = QGridLayout()
        self._grid.setHorizontalSpacing(8)
        self._grid.setVerticalSpacing(8)
        layout.addLayout(self._grid)
        self._rebuild_buttons()

    def set_years(self, years: list[int]) -> None:
        self._years = years
        self._rebuild_buttons()

    def open_for(self, anchor: QWidget, selected_year: int) -> None:
        for year, button in self._buttons.items():
            button.setProperty("selectedYear", year == selected_year)
            button.style().unpolish(button)
            button.style().polish(button)
            button.update()
        popup_anchor = anchor.mapToGlobal(QPoint(0, anchor.height()))
        self.move(popup_anchor)
        self.show()

    def _rebuild_buttons(self) -> None:
        while self._grid.count():
            item = self._grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._buttons.clear()
        for index, year in enumerate(self._years):
            button = QPushButton(str(year))
            button.clicked.connect(lambda _checked=False, value=year: self._select_year(value))
            self._grid.addWidget(button, index // 3, index % 3)
            self._buttons[year] = button

    def _select_year(self, year: int) -> None:
        self.yearSelected.emit(year)
        self.hide()


@dataclass(slots=True)
class PvTabPage:
    root: QWidget
    summary_label: QLabel
    period_button: PeriodPickerButton
    prev_button: QPushButton
    next_button: QPushButton
    graph_view: GraphView


class PowerHistoryDialog(QDialog):
    DAY_START_HOUR = 7
    NIGHT_START_HOUR = 23
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

    def __init__(
        self,
        dataframe: pd.DataFrame,
        timestamp_column: str,
        power_column: str,
        device_name: str,
        *,
        series_label: str,
        accent_color: str | QColor,
        summary_title: str = "Total energy",
        history_noun: str = "energy",
        clip_negative: bool = False,
        forecast_dataframe: pd.DataFrame | None = None,
        forecast_timestamp_column: str = "Timestamp",
        forecast_power_column: str = "predicted_pv_power_kw",
        forecast_horizon_days: int = 0,
        forecast_refresh_requested: Callable[[], None] | None = None,
        forecast_snapshot_loader: Callable[[], pd.DataFrame] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._series_label = series_label
        self._summary_title = tr_fragment(summary_title)
        self._history_noun = history_noun
        self._accent_color = QColor(accent_color)
        self._clip_negative = clip_negative
        self.setWindowTitle(tr_fragment(f"{series_label} History - {device_name}"))
        self.resize(1180, 760)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setProperty("popup_disable_focus_guard", True)
        self.setProperty("popup_disable_focus_recovery", True)
        # Mark as heavy-render popup so focus recovery can be delayed until the
        # first chart paint settles.
        self.setProperty("popup_heavy_render", True)
        self.setProperty("popup_disable_backdrop", False)
        self._initial_tabs_rendered = False
        self._initial_render_started = False
        self._activation_render_scheduled = False
        self._shown_monotonic: float | None = None

        normalized = dataframe[[timestamp_column, power_column]].copy()
        normalized.columns = ["timestamp", "power_kw"]
        normalized["timestamp"] = pd.to_datetime(normalized["timestamp"], errors="coerce")
        normalized["power_kw"] = pd.to_numeric(normalized["power_kw"], errors="coerce").fillna(0.0)
        normalized.dropna(subset=["timestamp"], inplace=True)
        normalized.sort_values("timestamp", inplace=True)
        normalized.reset_index(drop=True, inplace=True)
        if self._clip_negative:
            normalized["power_kw"] = normalized["power_kw"].clip(lower=0.0)

        self._dataframe = normalized
        self._forecast_dataframe = self._normalize_forecast_dataframe(
            forecast_dataframe,
            timestamp_column=forecast_timestamp_column,
            power_column=forecast_power_column,
        )
        self._forecast_refresh_requested = forecast_refresh_requested
        self._forecast_snapshot_loader = forecast_snapshot_loader
        self._forecast_refresh_inflight = False
        self._forecast_pending_day: pd.Timestamp | None = None
        self._forecast_failed_day: pd.Timestamp | None = None
        self._forecast_poll_started_at: pd.Timestamp | None = None
        self._forecast_poll_timer = QTimer(self)
        self._forecast_poll_timer.setInterval(1200)
        self._forecast_poll_timer.timeout.connect(self._poll_forecast_snapshot)
        self._energy_segments = self._build_energy_segments(normalized)
        self._latest_timestamp = normalized["timestamp"].max() if not normalized.empty else pd.Timestamp.now()
        self._max_forecast_day = self._latest_timestamp.normalize() + pd.Timedelta(days=max(0, int(forecast_horizon_days)))
        self._selected_day = self._latest_timestamp.normalize()
        self._selected_month = self._latest_timestamp.to_period("M")
        self._selected_year = int(self._latest_timestamp.year)
        self._available_years = sorted(self._energy_segments["period_year"].unique().tolist()) if not self._energy_segments.empty else []
        if not self._available_years:
            self._available_years = [int(self._latest_timestamp.year)]
        self._total_window_size = 6
        self._total_window_start = max(0, len(self._available_years) - self._total_window_size)
        earliest_timestamp = normalized["timestamp"].min() if not normalized.empty else self._latest_timestamp
        self._minimum_qdate = QDate(earliest_timestamp.year, earliest_timestamp.month, earliest_timestamp.day)
        day_picker_max = max(self._latest_timestamp.normalize(), self._max_forecast_day)
        self._maximum_qdate = QDate(day_picker_max.year, day_picker_max.month, day_picker_max.day)

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        top_bar_shell = create_neon_header_bar(self, self.reject, title="")
        self._header_context_label = top_bar_shell.findChild(QLabel, "NeonHeaderTitle")
        if self._header_context_label is None:
            self._header_context_label = QLabel("")
        root.addWidget(top_bar_shell)
        root.addSpacing(20)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("HistoryTabs")
        apply_tab_widget_interaction(self.tabs)
        root.addWidget(self.tabs, 1)

        self.day_page = self._build_tab_page()
        self.month_page = self._build_tab_page()
        self.year_page = self._build_tab_page()
        self.total_page = self._build_tab_page()

        self.tabs.addTab(self.day_page.root, self._tab_icon("day"), tr("Day"))
        self.tabs.addTab(self.month_page.root, self._tab_icon("month"), tr("Month"))
        self.tabs.addTab(self.year_page.root, self._tab_icon("year"), tr("Year"))
        self.tabs.addTab(self.total_page.root, self._tab_icon("total"), tr("Total"))
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._rendered_tab_indexes: set[int] = set()

        self.day_page.prev_button.clicked.connect(self._go_prev_day)
        self.day_page.next_button.clicked.connect(self._go_next_day)
        self.month_page.prev_button.clicked.connect(self._go_prev_month)
        self.month_page.next_button.clicked.connect(self._go_next_month)
        self.year_page.prev_button.clicked.connect(self._go_prev_year)
        self.year_page.next_button.clicked.connect(self._go_next_year)
        self.total_page.prev_button.clicked.connect(self._go_prev_total_window)
        self.total_page.next_button.clicked.connect(self._go_next_total_window)
        self.day_page.period_button.clicked.connect(self._open_day_picker)
        self.month_page.period_button.clicked.connect(self._open_month_picker)
        self.year_page.period_button.clicked.connect(self._open_year_picker)

        self._day_picker = CalendarPopup(self, minimum_date=self._minimum_qdate, maximum_date=self._maximum_qdate)
        self._month_picker = MonthPickerPopup(
            self._minimum_qdate.year(),
            self._maximum_qdate.year(),
            self._maximum_qdate.month(),
            self,
        )
        self._year_picker = YearPickerPopup(self._available_years, self)
        self._day_picker.dateSelected.connect(self._select_day_from_picker)
        self._month_picker.monthSelected.connect(self._select_month_from_picker)
        self._year_picker.yearSelected.connect(self._select_year_from_picker)

        self.total_page.period_button.setEnabled(False)
        self.total_page.period_button.setCursor(Qt.CursorShape.ArrowCursor)
        self.total_page.period_button.setFlat(False)
        self.total_page.prev_button.setVisible(False)
        self.total_page.period_button.setVisible(False)
        self.total_page.next_button.setVisible(False)

        self._apply_styles()
        self.finished.connect(lambda code: self._dialog_diag("history_dialog:finished", code=int(code), visible=self.isVisible()))
        self._dialog_diag(
            "history_dialog:init_ready",
            series=self._series_label,
            source_rows=len(dataframe.index),
            normalized_rows=len(self._dataframe.index),
            segments_rows=len(self._energy_segments.index),
            timestamp_column=timestamp_column,
            power_column=power_column,
            clip_negative=self._clip_negative,
            latest_ts=str(self._latest_timestamp),
            selected_day=str(self._selected_day),
        )
        # Defer heavy Plotly/QWebEngine rendering to show-time. Rendering during
        # construction can trigger a transient hide/show cycle on macOS.

    def _dialog_diag(self, event: str, **details: object) -> None:
        _ = (event, details)
        return

    def _trace_window_state(self, event_name: str, **details: object) -> None:
        _ = (event_name, details)
        return

    def _schedule_initial_render(self) -> None:
        if self._initial_tabs_rendered or self._initial_render_started or self._activation_render_scheduled:
            return
        self._activation_render_scheduled = True
        delay_ms = 140

        def _attempt_render() -> None:
            self._activation_render_scheduled = False
            if not self.isVisible():
                return
            self.force_initial_render()

        QTimer.singleShot(delay_ms, _attempt_render)

    def _schedule_initial_render_watchdog(self) -> None:
        def _watchdog() -> None:
            if not self.isVisible():
                return
            if self._initial_tabs_rendered:
                return
            self._dialog_diag(
                "history_dialog:initial_render_watchdog_retry",
                started=self._initial_render_started,
                scheduled=self._activation_render_scheduled,
                active=self.isActiveWindow(),
            )
            self.force_initial_render()

        QTimer.singleShot(1200, _watchdog)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # Keep render scheduling independent from diagnostics so logging issues
        # cannot block chart initialization.
        self._schedule_initial_render()
        self._schedule_initial_render_watchdog()
        try:
            self._shown_monotonic = time.monotonic()
            self._dialog_diag("history_dialog:show_event", visible=self.isVisible())
            self._trace_window_state("history_dialog:lifecycle_show")
        except Exception:
            LOGGER.exception("PV history dialog showEvent diagnostics failed")

    def _render_initial_tabs_once(self) -> None:
        self._start_initial_render(trigger="internal_once")

    def _start_initial_render(self, *, trigger: str) -> None:
        if self._initial_tabs_rendered or self._initial_render_started:
            return
        if not self.isVisible():
            self._schedule_initial_render()
            return
        current_index = int(self.tabs.currentIndex())
        if current_index < 0:
            # Tab widget may transiently report no current page while the popup
            # is still settling in the event loop on macOS.
            self._schedule_initial_render()
            return
        self._initial_render_started = True
        try:
            self._dialog_diag("history_dialog:initial_render_started", trigger=trigger, tab=current_index)
            self._trace_window_state("history_dialog:render_initial_begin", tab=current_index)
        except Exception:
            LOGGER.exception("PV history dialog failed to start initial render diagnostics")
        try:
            # Render only the active tab on open. Rendering all tabs at once
            # can trigger parallel QWebEngine loads in hidden pages, which is
            # unstable on macOS and causes flicker/blank charts.
            self._render_tab_if_needed(current_index, force=True)
            self._initial_tabs_rendered = True
            try:
                self._trace_window_state("history_dialog:render_initial_done", tab=current_index)
            except Exception:
                LOGGER.exception("PV history dialog post-render diagnostics failed")
        except Exception as exc:
            self._initial_render_started = False
            message = f"Failed to initialize chart: {exc}"
            self.day_page.graph_view.set_placeholder(message)
            self.month_page.graph_view.set_placeholder(message)
            self.year_page.graph_view.set_placeholder(message)
            self.total_page.graph_view.set_placeholder(message)
            try:
                self._trace_window_state("history_dialog:render_initial_failed", error=str(exc))
            except Exception:
                LOGGER.exception("PV history dialog failed-render diagnostics failed")

    def force_initial_render(self) -> None:
        """Force-initialize tab content after dialog opening."""
        try:
            self._dialog_diag("history_dialog:force_initial_render_requested")
            self._start_initial_render(trigger="external_force")
            self._dialog_diag("history_dialog:force_initial_render_done", rendered=self._initial_tabs_rendered)
        except Exception as exc:
            self._dialog_diag("history_dialog:force_initial_render_failed", error=str(exc))

    def _on_tab_changed(self, index: int) -> None:
        self._sync_header_context()
        self._trace_window_state("history_dialog:tab_changed", tab=index)
        if not self._initial_tabs_rendered:
            self._dialog_diag(
                "history_dialog:tab_changed_deferred",
                tab=index,
                initial_render_started=self._initial_render_started,
            )
            return
        self._render_tab_if_needed(index, force=False)

    def _render_tab_if_needed(self, index: int, *, force: bool) -> None:
        if index < 0:
            return
        if not force and index in self._rendered_tab_indexes:
            return
        updaters = (
            self._update_day_tab,
            self._update_month_tab,
            self._update_year_tab,
            self._update_total_tab,
        )
        if index >= len(updaters):
            return
        updaters[index]()
        self._rendered_tab_indexes.add(index)
        self._trace_window_state(
            "history_dialog:tab_rendered",
            tab=index,
            force=force,
            rendered=",".join(str(i) for i in sorted(self._rendered_tab_indexes)),
        )

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        try:
            self._dialog_diag("history_dialog:hide_event", visible=self.isVisible())
            self._trace_window_state("history_dialog:lifecycle_hide")
        except Exception:
            LOGGER.exception("PV history dialog hideEvent diagnostics failed")

    def closeEvent(self, event) -> None:  # noqa: N802
        self._dialog_diag(
            "history_dialog:close_event",
            visible=self.isVisible(),
            accepted=event.isAccepted(),
        )
        super().closeEvent(event)

    def event(self, event):  # noqa: N802
        try:
            if event is not None:
                tracked = {
                    QEvent.Type.WindowActivate: "window_activate",
                    QEvent.Type.WindowDeactivate: "window_deactivate",
                    QEvent.Type.FocusIn: "focus_in",
                    QEvent.Type.FocusOut: "focus_out",
                    QEvent.Type.Move: "move",
                    QEvent.Type.Resize: "resize",
                }
                name = tracked.get(event.type())
                if name is not None:
                    self._trace_window_state(
                        "history_dialog:qt_event",
                        qt_type=int(event.type()),
                        qt_name=name,
                        spontaneous=bool(event.spontaneous()),
                    )
        except Exception:
            LOGGER.exception("PV history dialog Qt event diagnostics failed")
        return super().event(event)

    def accept(self) -> None:
        self._dialog_diag("history_dialog:accept_called", visible=self.isVisible())
        super().accept()

    def reject(self) -> None:
        self._dialog_diag("history_dialog:reject_called", visible=self.isVisible())
        super().reject()

    def done(self, result: int) -> None:
        self._dialog_diag("history_dialog:done_called", result=int(result), visible=self.isVisible())
        super().done(result)

    def _build_tab_page(self) -> PvTabPage:
        """Create one tab page containing summary, period controls and graph."""
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        summary_label = QLabel(f"{self._summary_title}: --")
        summary_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        summary_label.setTextFormat(Qt.TextFormat.RichText)
        summary_label.setStyleSheet(SUMMARY_LABEL_STYLE)
        layout.addWidget(summary_label)

        toolbar = QHBoxLayout()
        toolbar.setSpacing(8)
        prev_button = QPushButton(tr("Previous"))
        next_button = QPushButton(tr("Next"))
        period_button = PeriodPickerButton("--")
        prev_button.setCursor(Qt.CursorShape.PointingHandCursor)
        next_button.setCursor(Qt.CursorShape.PointingHandCursor)
        period_button.setCursor(Qt.CursorShape.PointingHandCursor)
        toolbar.addWidget(prev_button)
        toolbar.addWidget(period_button, 1)
        toolbar.addWidget(next_button)
        layout.addLayout(toolbar)

        graph_view = GraphView()
        layout.addWidget(graph_view, 1)

        return PvTabPage(
            root=root,
            summary_label=summary_label,
            period_button=period_button,
            prev_button=prev_button,
            next_button=next_button,
            graph_view=graph_view,
        )

    def _apply_styles(self) -> None:
        """Apply base dialog styles plus shared neon header/close overrides."""
        self.setStyleSheet(
            compose_styles(
                POWER_HISTORY_TABS_STYLE,
                POWER_HISTORY_DIALOG_STYLE,
                NEON_CLOSE_BUTTON_STYLE,
                NEON_HEADER_BAR_STYLE,
            )
        )

    def _build_energy_segments(self, dataframe: pd.DataFrame) -> pd.DataFrame:
        """Integrate power samples into per-segment energy values.

        Uses trapezoidal integration between adjacent points to better handle
        uneven sampling intervals than a naive fixed-step assumption.
        """
        if len(dataframe.index) < 2:
            return pd.DataFrame(columns=["midpoint", "energy_wh", "period_day", "period_month", "period_year", "time_of_use"])

        segments = dataframe.copy()
        segments["next_timestamp"] = segments["timestamp"].shift(-1)
        segments["next_power_kw"] = segments["power_kw"].shift(-1)
        segments.dropna(subset=["next_timestamp", "next_power_kw"], inplace=True)
        segments["duration_hours"] = (
            (segments["next_timestamp"] - segments["timestamp"]).dt.total_seconds() / 3600.0
        )
        segments = segments[segments["duration_hours"] > 0]
        if segments.empty:
            return pd.DataFrame(columns=["midpoint", "energy_wh", "period_day", "period_month", "period_year"])

        # The stored source values are in kW, so integrated kWh is converted to Wh.
        segments["energy_wh"] = (
            (segments["power_kw"] + segments["next_power_kw"]) / 2.0
        ) * segments["duration_hours"] * 1000.0
        segments["midpoint"] = segments["timestamp"] + (segments["next_timestamp"] - segments["timestamp"]) / 2
        segments["period_day"] = segments["midpoint"].dt.normalize()
        segments["period_month"] = segments["midpoint"].dt.to_period("M")
        segments["period_year"] = segments["midpoint"].dt.year
        segments["time_of_use"] = segments["midpoint"].dt.hour.map(self._classify_time_of_use_period)
        return segments[["midpoint", "energy_wh", "period_day", "period_month", "period_year", "time_of_use"]].copy()

    def _normalize_forecast_dataframe(
        self,
        dataframe: pd.DataFrame | None,
        *,
        timestamp_column: str,
        power_column: str,
    ) -> pd.DataFrame:
        if dataframe is None or dataframe.empty:
            return pd.DataFrame(columns=["timestamp", "power_kw"])
        if timestamp_column not in dataframe.columns or power_column not in dataframe.columns:
            return pd.DataFrame(columns=["timestamp", "power_kw"])
        normalized = dataframe[[timestamp_column, power_column]].copy()
        normalized.columns = ["timestamp", "power_kw"]
        normalized["timestamp"] = pd.to_datetime(normalized["timestamp"], errors="coerce")
        normalized["power_kw"] = pd.to_numeric(normalized["power_kw"], errors="coerce").fillna(0.0).clip(lower=0.0)
        normalized.dropna(subset=["timestamp"], inplace=True)
        normalized.sort_values("timestamp", inplace=True)
        normalized.reset_index(drop=True, inplace=True)
        return normalized

    def _request_forecast_for_day(self, day_start: pd.Timestamp) -> bool:
        """Trigger asynchronous forecast refresh and start polling window."""
        if self._forecast_refresh_requested is None or self._forecast_snapshot_loader is None:
            return False
        if self._forecast_refresh_inflight:
            return True
        self._forecast_refresh_inflight = True
        self._forecast_pending_day = pd.Timestamp(day_start).normalize()
        self._forecast_poll_started_at = pd.Timestamp.now()
        self.day_page.graph_view.set_loading("Building forecast for selected day...")
        try:
            self._forecast_refresh_requested()
        except Exception:
            self._forecast_refresh_inflight = False
            self._forecast_pending_day = None
            self._forecast_poll_started_at = None
            self._forecast_failed_day = pd.Timestamp(day_start).normalize()
            return False
        self._forecast_poll_timer.start()
        return True

    def _poll_forecast_snapshot(self) -> None:
        """Poll persisted forecast snapshot until data arrives or timeout hits."""
        pending_day = self._forecast_pending_day
        if pending_day is None:
            self._forecast_poll_timer.stop()
            self._forecast_refresh_inflight = False
            return
        latest_snapshot = pd.DataFrame()
        if self._forecast_snapshot_loader is not None:
            try:
                latest_snapshot = self._forecast_snapshot_loader()
            except Exception:
                latest_snapshot = pd.DataFrame()
        if latest_snapshot is not None and not latest_snapshot.empty:
            self._forecast_dataframe = self._normalize_forecast_dataframe(
                latest_snapshot,
                timestamp_column="Timestamp",
                power_column="predicted_pv_power_kw",
            )
        day_end = pending_day + pd.Timedelta(days=1)
        has_forecast = not self._forecast_dataframe[
            (self._forecast_dataframe["timestamp"] >= pending_day) & (self._forecast_dataframe["timestamp"] < day_end)
        ].empty
        now = pd.Timestamp.now()
        timed_out = False
        if self._forecast_poll_started_at is not None:
            timed_out = (now - self._forecast_poll_started_at).total_seconds() > 75
        if has_forecast or timed_out:
            self._forecast_poll_timer.stop()
            self._forecast_refresh_inflight = False
            self._forecast_failed_day = None if has_forecast else pending_day
            self._forecast_pending_day = None
            self._forecast_poll_started_at = None
            self._update_day_tab()

    def _refresh_all_tabs(self) -> None:
        self._update_day_tab()
        self._update_month_tab()
        self._update_year_tab()
        self._update_total_tab()

    def _set_summary_content(self, label: QLabel, *, amount_text: str, breakdown_html: str = "") -> None:
        label.setText(
            f"""
            <div style="line-height:1.25;">
                <div style="font-size:24px; font-weight:700; color:#f8fafc;">
                    {self._summary_title}: {amount_text}
                </div>
                {breakdown_html}
            </div>
            """
        )

    def _summary_breakdown_html(self, *, day_wh: float, night_wh: float, total_wh: float) -> str:
        if total_wh <= 0:
            return ""
        day_pct = day_wh / total_wh * 100.0
        night_pct = night_wh / total_wh * 100.0
        return (
            "<div style=\"margin-top:8px;font-size:13px;font-weight:600;color:#cbd5e1;\">"
            f"<span style=\"color:#0ea5e9;\">{tr('Day')}:</span> {self._format_energy(day_wh)} ({day_pct:.1f}%)"
            " &nbsp;•&nbsp; "
            f"<span style=\"color:#2563eb;\">{tr('Night')}:</span> {self._format_energy(night_wh)} ({night_pct:.1f}%)"
            "</div>"
        )

    def _supports_time_of_use(self) -> bool:
        return self._series_label.lower() in {"home", "grid"}

    def _classify_time_of_use_period(self, hour: int) -> str:
        if self.DAY_START_HOUR <= hour < self.NIGHT_START_HOUR:
            return "day"
        return "night"

    def _time_of_use_breakdown(self, segments: pd.DataFrame) -> tuple[float, float]:
        if segments.empty or "time_of_use" not in segments:
            return 0.0, 0.0
        grouped = segments.groupby("time_of_use")["energy_wh"].sum()
        return float(grouped.get("day", 0.0)), float(grouped.get("night", 0.0))

    def _scaled_energy_frame(self, frame: pd.DataFrame, columns: list[str]) -> tuple[pd.DataFrame, str]:
        numeric = frame[columns].apply(pd.to_numeric, errors="coerce").fillna(0.0)
        use_kwh = float(numeric.max().max()) >= 1000.0 if not numeric.empty else False
        divisor = 1000.0 if use_kwh else 1.0
        unit = "kWh" if use_kwh else "Wh"
        scaled = frame.copy()
        for column in columns:
            scaled[f"{column}_display"] = pd.to_numeric(scaled[column], errors="coerce").fillna(0.0) / divisor
        return scaled, f"{self._history_noun.title()} ({unit})"

    def _home_time_of_use_figure(self, dataframe: pd.DataFrame, *, x: str, x_title: str, y_title: str) -> go.Figure:
        figure = go.Figure()
        totals = pd.to_numeric(dataframe["total_wh"], errors="coerce").fillna(0.0)
        days = pd.to_numeric(dataframe["day_wh"], errors="coerce").fillna(0.0)
        nights = pd.to_numeric(dataframe["night_wh"], errors="coerce").fillna(0.0)
        day_pct = (days / totals.replace(0.0, pd.NA) * 100.0).fillna(0.0)
        night_pct = (nights / totals.replace(0.0, pd.NA) * 100.0).fillna(0.0)
        unit = "kWh" if "(kWh)" in y_title else "Wh"
        divisor = 1000.0 if unit == "kWh" else 1.0
        x_values, xaxis_extra = self._resolve_x_axis_values(dataframe[x], x_title=x_title)

        figure.add_trace(
            go.Bar(
                x=x_values,
                y=dataframe["day_wh_display"],
                marker_color="#0ea5e9",
                name=tr("Day"),
                customdata=list(zip(days / divisor, day_pct)),
                hovertemplate=f"{tr('Day')}: %{{customdata[0]:.2~f}} {unit} (%{{customdata[1]:.2~f}}%)<extra></extra>",
            )
        )
        figure.add_trace(
            go.Bar(
                x=x_values,
                y=dataframe["night_wh_display"],
                marker_color="#2563eb",
                name=tr("Night"),
                customdata=list(zip(nights / divisor, night_pct)),
                hovertemplate=f"{tr('Night')}: %{{customdata[0]:.2~f}} {unit} (%{{customdata[1]:.2~f}}%)<extra></extra>",
            )
        )
        figure.add_trace(
            go.Scatter(
                x=x_values,
                y=dataframe["total_wh_display"],
                mode="markers",
                marker={"size": 0.1, "color": "rgba(0,0,0,0)"},
                name=tr("Total"),
                customdata=list(zip(totals / divisor,)),
                hovertemplate=f"{tr('Total')}: %{{customdata[0]:.2~f}} {unit} (100%)<extra></extra>",
                showlegend=False,
            )
        )
        apply_energyflow_chart_style(
            figure,
            y_title=y_title,
            x_title=x_title,
            margin={"l": 40, "r": 20, "t": 20, "b": 40},
            barmode="stack",
            xaxis_extra=xaxis_extra,
            yaxis_extra={"showgrid": True, "zeroline": True, "showline": False},
        )
        return figure

    def _set_header_context(self, text: str) -> None:
        self._header_context_label.setText(tr_fragment(text))

    def _sync_header_context(self) -> None:
        current_widget = self.tabs.currentWidget()
        context_text = current_widget.property("headerContext") if current_widget is not None else ""
        self._set_header_context(str(context_text or ""))

    def _set_page_context(self, page: PvTabPage, text: str) -> None:
        page.root.setProperty("headerContext", text)
        if self.tabs.currentWidget() is page.root:
            self._set_header_context(text)

    def _update_day_tab(self) -> None:
        day_start = pd.Timestamp(self._selected_day)
        day_end = day_start + pd.Timedelta(days=1)
        day_frame = self._dataframe[
            (self._dataframe["timestamp"] >= day_start) & (self._dataframe["timestamp"] < day_end)
        ]
        forecast_day_frame = self._forecast_dataframe[
            (self._forecast_dataframe["timestamp"] >= day_start) & (self._forecast_dataframe["timestamp"] < day_end)
        ]
        now_local = pd.Timestamp.now(tz="Europe/Kiev").tz_localize(None)
        is_today = day_start == now_local.normalize()
        today_forecast_tail = forecast_day_frame[forecast_day_frame["timestamp"] > now_local]
        is_forecast_day = day_start > self._latest_timestamp.normalize() and not forecast_day_frame.empty
        day_segments = self._energy_segments[self._energy_segments["period_day"] == day_start]
        energy_wh = float(forecast_day_frame["power_kw"].sum() * 1000.0) if is_forecast_day else self._sum_energy_for_day(day_start)
        day_wh, night_wh = self._time_of_use_breakdown(day_segments)
        self._dialog_diag(
            "history_dialog:day_tab_inputs",
            selected_day=str(day_start),
            data_rows=len(self._dataframe.index),
            day_rows=len(day_frame.index),
            day_segments=len(day_segments.index),
            forecast_rows=len(forecast_day_frame.index),
            is_forecast_day=is_forecast_day,
            is_today=is_today,
            energy_wh=round(energy_wh, 3),
            latest_day=str(self._latest_timestamp.normalize()),
            max_forecast_day=str(self._max_forecast_day),
        )

        self._set_summary_content(
            self.day_page.summary_label,
            amount_text=self._format_energy(energy_wh),
            breakdown_html=self._summary_breakdown_html(day_wh=day_wh, night_wh=night_wh, total_wh=energy_wh)
            if self._supports_time_of_use()
            else "",
        )
        context_suffix = tr(" (Forecast)") if is_forecast_day else ""
        self._set_page_context(self.day_page, tr_fragment(f"{self._series_label} power for {day_start.strftime('%d.%m.%Y')}{context_suffix}"))
        self.day_page.period_button.setText(day_start.strftime("%d.%m.%Y"))
        self.day_page.next_button.setEnabled(day_start < self._max_forecast_day)
        if is_today and not today_forecast_tail.empty:
            self._dialog_diag(
                "history_dialog:day_tab_render_observed_plus_forecast",
                selected_day=str(day_start),
                observed_rows=len(day_frame.index),
                forecast_tail_rows=len(today_forecast_tail.index),
                now=str(now_local),
            )
            self.day_page.graph_view.set_figure(
                self._line_figure(
                    day_frame,
                    title="",
                    x_title="Time",
                    y_title=tr("Power (kW)"),
                    forecast_dataframe=today_forecast_tail,
                )
            )
            return
        if is_forecast_day:
            self._forecast_failed_day = None
            self._dialog_diag("history_dialog:day_tab_render_forecast", selected_day=str(day_start))
            self.day_page.graph_view.set_figure(
                self._line_figure(
                    forecast_day_frame,
                    title="",
                    x_title="Time",
                    y_title=tr("Power (kW)"),
                )
            )
            return
        if day_start > self._latest_timestamp.normalize():
            if self._forecast_failed_day is not None and day_start == self._forecast_failed_day:
                self._dialog_diag("history_dialog:day_tab_placeholder_forecast_unavailable", selected_day=str(day_start))
                self.day_page.graph_view.set_placeholder(tr("Forecast is unavailable for this future day."))
                return
            requested = self._request_forecast_for_day(day_start)
            if requested:
                self._dialog_diag("history_dialog:day_tab_wait_forecast_refresh", selected_day=str(day_start))
                return
            self._dialog_diag("history_dialog:day_tab_placeholder_forecast_source_unavailable", selected_day=str(day_start))
            self.day_page.graph_view.set_placeholder(tr("Forecast source is unavailable for this future day."))
            return
        if day_frame.empty:
            self._dialog_diag("history_dialog:day_tab_placeholder_no_samples", selected_day=str(day_start))
            self.day_page.graph_view.set_placeholder(tr_fragment(f"No {self._series_label} samples for the selected day."))
            return
        self._dialog_diag("history_dialog:day_tab_render_observed", selected_day=str(day_start), day_rows=len(day_frame.index))
        self.day_page.graph_view.set_figure(
            self._line_figure(
                day_frame,
                title="",
                x_title="Time",
                y_title=tr("Power (kW)"),
            )
        )

    def _update_month_tab(self) -> None:
        month_label = self._localized_month_year(self._selected_month.year, self._selected_month.month)
        month_segments = self._energy_segments[self._energy_segments["period_month"] == self._selected_month]
        month_start = self._selected_month.to_timestamp()
        month_days = pd.date_range(month_start, month_start + pd.offsets.MonthEnd(0), freq="D")
        grouped = month_segments.groupby("period_day")["energy_wh"].sum() if not month_segments.empty else pd.Series(dtype=float)
        month_frame = pd.DataFrame({"period": month_days})
        month_frame["energy_wh"] = month_frame["period"].map(grouped).fillna(0.0)
        month_day_wh, month_night_wh = self._time_of_use_breakdown(month_segments)
        self._dialog_diag(
            "history_dialog:month_tab_inputs",
            selected_month=str(self._selected_month),
            segments_rows=len(month_segments.index),
            month_days=len(month_days),
            month_frame_rows=len(month_frame.index),
            energy_total_wh=round(float(month_frame["energy_wh"].sum()), 3),
            tou_mode=self._supports_time_of_use(),
        )

        self._set_summary_content(
            self.month_page.summary_label,
            amount_text=self._format_energy(month_frame["energy_wh"].sum()),
            breakdown_html=self._summary_breakdown_html(
                day_wh=month_day_wh,
                night_wh=month_night_wh,
                total_wh=float(month_frame["energy_wh"].sum()),
            )
            if self._supports_time_of_use()
            else "",
        )
        self._set_page_context(
            self.month_page,
            tr_fragment(f"Daily {self._series_label} {self._history_noun} for {month_label}"),
        )
        self.month_page.period_button.setText(month_label)
        self.month_page.next_button.setEnabled(self._selected_month < self._latest_timestamp.to_period("M"))
        if month_frame.empty:
            self._dialog_diag("history_dialog:month_tab_placeholder_no_data", selected_month=str(self._selected_month))
            self.month_page.graph_view.set_placeholder(tr_fragment(f"No {self._series_label} {self._history_noun} for the selected month."))
            return
        if self._supports_time_of_use():
            tou = month_segments.groupby(["period_day", "time_of_use"])["energy_wh"].sum().unstack(fill_value=0.0)
            plot_frame = month_frame.copy()
            plot_frame["day_wh"] = plot_frame["period"].map(tou.get("day", pd.Series(dtype=float))).fillna(0.0)
            plot_frame["night_wh"] = plot_frame["period"].map(tou.get("night", pd.Series(dtype=float))).fillna(0.0)
            plot_frame["total_wh"] = plot_frame["energy_wh"]
            plot_frame, y_axis_title = self._scaled_energy_frame(plot_frame, ["day_wh", "night_wh", "total_wh"])
            self._dialog_diag(
                "history_dialog:month_tab_render_tou",
                selected_month=str(self._selected_month),
                plot_rows=len(plot_frame.index),
                y_title=y_axis_title,
            )
            self.month_page.graph_view.set_figure(
                self._home_time_of_use_figure(
                    plot_frame,
                    x="period",
                    x_title="Day",
                    y_title=y_axis_title,
                )
            )
        else:
            y_values, y_axis_title = self._energy_axis_values(month_frame["energy_wh"])
            plot_frame = month_frame.copy()
            plot_frame["energy_display"] = y_values
            self._dialog_diag(
                "history_dialog:month_tab_render_bar",
                selected_month=str(self._selected_month),
                plot_rows=len(plot_frame.index),
                y_title=y_axis_title,
            )
            self.month_page.graph_view.set_figure(
                self._bar_figure(
                    plot_frame,
                    x="period",
                    y="energy_display",
                    title="",
                    x_title="Day",
                    y_title=y_axis_title,
                )
            )

    def _update_year_tab(self) -> None:
        year_segments = self._energy_segments[self._energy_segments["period_year"] == self._selected_year]
        month_frame = pd.DataFrame({"period": pd.period_range(f"{self._selected_year}-01", f"{self._selected_year}-12", freq="M")})
        grouped = year_segments.groupby("period_month")["energy_wh"].sum() if not year_segments.empty else pd.Series(dtype=float)
        month_frame["energy_wh"] = month_frame["period"].map(grouped).fillna(0.0)
        year_day_wh, year_night_wh = self._time_of_use_breakdown(year_segments)
        month_frame["period"] = month_frame["period"].dt.to_timestamp()
        self._dialog_diag(
            "history_dialog:year_tab_inputs",
            selected_year=int(self._selected_year),
            segments_rows=len(year_segments.index),
            month_frame_rows=len(month_frame.index),
            energy_total_wh=round(float(month_frame["energy_wh"].sum()), 3),
            tou_mode=self._supports_time_of_use(),
        )

        self._set_summary_content(
            self.year_page.summary_label,
            amount_text=self._format_energy(month_frame["energy_wh"].sum()),
            breakdown_html=self._summary_breakdown_html(
                day_wh=year_day_wh,
                night_wh=year_night_wh,
                total_wh=float(month_frame["energy_wh"].sum()),
            )
            if self._supports_time_of_use()
            else "",
        )
        self._set_page_context(
            self.year_page,
            tr_fragment(f"Monthly {self._series_label} {self._history_noun} for {self._selected_year}"),
        )
        self.year_page.period_button.setText(str(self._selected_year))
        self.year_page.next_button.setEnabled(self._selected_year < int(self._latest_timestamp.year))
        if month_frame.empty:
            self._dialog_diag("history_dialog:year_tab_placeholder_no_data", selected_year=int(self._selected_year))
            self.year_page.graph_view.set_placeholder(tr_fragment(f"No {self._series_label} {self._history_noun} for the selected year."))
            return
        if self._supports_time_of_use():
            period_source = year_segments.assign(period=year_segments["period_month"].dt.to_timestamp())
            tou = period_source.groupby(["period", "time_of_use"])["energy_wh"].sum().unstack(fill_value=0.0)
            plot_frame = month_frame.copy()
            plot_frame["day_wh"] = plot_frame["period"].map(tou.get("day", pd.Series(dtype=float))).fillna(0.0)
            plot_frame["night_wh"] = plot_frame["period"].map(tou.get("night", pd.Series(dtype=float))).fillna(0.0)
            plot_frame["total_wh"] = plot_frame["energy_wh"]
            plot_frame, y_axis_title = self._scaled_energy_frame(plot_frame, ["day_wh", "night_wh", "total_wh"])
            self._dialog_diag(
                "history_dialog:year_tab_render_tou",
                selected_year=int(self._selected_year),
                plot_rows=len(plot_frame.index),
                y_title=y_axis_title,
            )
            self.year_page.graph_view.set_figure(
                self._home_time_of_use_figure(
                    plot_frame,
                    x="period",
                    x_title="Month",
                    y_title=y_axis_title,
                )
            )
        else:
            y_values, y_axis_title = self._energy_axis_values(month_frame["energy_wh"])
            plot_frame = month_frame.copy()
            plot_frame["energy_display"] = y_values
            self._dialog_diag(
                "history_dialog:year_tab_render_bar",
                selected_year=int(self._selected_year),
                plot_rows=len(plot_frame.index),
                y_title=y_axis_title,
            )
            self.year_page.graph_view.set_figure(
                self._bar_figure(
                    plot_frame,
                    x="period",
                    y="energy_display",
                    title="",
                    x_title="Month",
                    y_title=y_axis_title,
                )
            )

    def _update_total_tab(self) -> None:
        total_by_year = self._energy_segments.groupby("period_year")["energy_wh"].sum() if not self._energy_segments.empty else pd.Series(dtype=float)
        window_years = self._available_years[self._total_window_start : self._total_window_start + self._total_window_size]
        total_frame = pd.DataFrame({"period": window_years})
        total_frame["energy_wh"] = total_frame["period"].map(total_by_year).fillna(0.0)
        total_day_wh, total_night_wh = self._time_of_use_breakdown(self._energy_segments)
        self._dialog_diag(
            "history_dialog:total_tab_inputs",
            window_start=int(self._total_window_start),
            available_years=len(self._available_years),
            window_years=len(window_years),
            total_frame_rows=len(total_frame.index),
            total_energy_wh=round(float(total_by_year.sum()), 3),
            tou_mode=self._supports_time_of_use(),
        )

        range_label = f"{window_years[0]} - {window_years[-1]}" if window_years else "--"
        self._set_summary_content(
            self.total_page.summary_label,
            amount_text=self._format_energy(total_by_year.sum()),
            breakdown_html=self._summary_breakdown_html(
                day_wh=total_day_wh,
                night_wh=total_night_wh,
                total_wh=float(total_by_year.sum()),
            )
            if self._supports_time_of_use()
            else "",
        )
        self._set_page_context(self.total_page, tr_fragment(f"{self._series_label} {self._history_noun} across {range_label}"))
        self.total_page.period_button.setText(range_label)
        max_start = max(0, len(self._available_years) - self._total_window_size)
        self.total_page.next_button.setEnabled(self._total_window_start < max_start)
        if total_frame.empty:
            self._dialog_diag("history_dialog:total_tab_placeholder_no_data")
            self.total_page.graph_view.set_placeholder(tr_fragment(f"No {self._series_label} history available."))
            return
        if self._supports_time_of_use():
            tou = self._energy_segments.groupby(["period_year", "time_of_use"])["energy_wh"].sum().unstack(fill_value=0.0)
            plot_frame = total_frame.copy()
            plot_frame["day_wh"] = plot_frame["period"].map(tou.get("day", pd.Series(dtype=float))).fillna(0.0)
            plot_frame["night_wh"] = plot_frame["period"].map(tou.get("night", pd.Series(dtype=float))).fillna(0.0)
            plot_frame["total_wh"] = plot_frame["energy_wh"]
            plot_frame, y_axis_title = self._scaled_energy_frame(plot_frame, ["day_wh", "night_wh", "total_wh"])
            self._dialog_diag(
                "history_dialog:total_tab_render_tou",
                plot_rows=len(plot_frame.index),
                y_title=y_axis_title,
            )
            self.total_page.graph_view.set_figure(
                self._home_time_of_use_figure(
                    plot_frame,
                    x="period",
                    x_title="Year",
                    y_title=y_axis_title,
                )
            )
        else:
            y_values, y_axis_title = self._energy_axis_values(total_frame["energy_wh"])
            plot_frame = total_frame.copy()
            plot_frame["energy_display"] = y_values
            self._dialog_diag(
                "history_dialog:total_tab_render_bar",
                plot_rows=len(plot_frame.index),
                y_title=y_axis_title,
            )
            self.total_page.graph_view.set_figure(
                self._bar_figure(
                    plot_frame,
                    x="period",
                    y="energy_display",
                    title="",
                    x_title="Year",
                    y_title=y_axis_title,
                )
            )

    def _line_figure(
        self,
        dataframe: pd.DataFrame,
        *,
        title: str,
        x_title: str,
        y_title: str,
        forecast_dataframe: pd.DataFrame | None = None,
    ) -> go.Figure:
        figure = go.Figure()
        accent_hex = self._accent_color.name()
        accent_transparent_80 = (
            f"rgba({self._accent_color.red()}, {self._accent_color.green()}, {self._accent_color.blue()}, 0.80)"
        )
        accent_transparent_50 = (
            f"rgba({self._accent_color.red()}, {self._accent_color.green()}, {self._accent_color.blue()}, 0.50)"
        )
        forecast_hex = self._accent_color.lighter(128).name()
        forecast_rgba = f"rgba({self._accent_color.red()}, {self._accent_color.green()}, {self._accent_color.blue()}, 0.20)"
        zero_line_rgba = f"rgba({self._accent_color.red()}, {self._accent_color.green()}, {self._accent_color.blue()}, 0.28)"
        top_margin = 48 if title else 20
        unit = ""
        if "(" in y_title and ")" in y_title:
            unit = y_title.split("(", 1)[1].split(")", 1)[0].strip()
        unit_suffix = f" {unit}" if unit else ""
        observed = dataframe.copy()
        observed["timestamp"] = pd.to_datetime(observed["timestamp"], errors="coerce")
        observed["power_kw"] = pd.to_numeric(observed["power_kw"], errors="coerce")
        observed = observed.dropna(subset=["timestamp", "power_kw"]).sort_values("timestamp")
        forecast = pd.DataFrame(columns=["timestamp", "power_kw"])
        if forecast_dataframe is not None and not forecast_dataframe.empty:
            forecast = forecast_dataframe.copy()
            forecast["timestamp"] = pd.to_datetime(forecast["timestamp"], errors="coerce")
            forecast["power_kw"] = pd.to_numeric(forecast["power_kw"], errors="coerce")
            forecast = forecast.dropna(subset=["timestamp", "power_kw"]).sort_values("timestamp")
            if not observed.empty:
                forecast = forecast[~forecast["timestamp"].isin(observed["timestamp"])]
                # Bridge the last observed point with the first forecast point to
                # avoid a visual gap when forecast starts at the next hourly slot.
                if not forecast.empty:
                    bridge_point = pd.DataFrame(
                        {
                            "timestamp": [observed["timestamp"].iloc[-1]],
                            "power_kw": [observed["power_kw"].iloc[-1]],
                        }
                    )
                    forecast = pd.concat([bridge_point, forecast], ignore_index=True).drop_duplicates(
                        subset=["timestamp"],
                        keep="first",
                    )
        timestamp_series = pd.concat(
            [observed["timestamp"], forecast["timestamp"]],
            ignore_index=True,
        )
        is_single_day_time_axis = (
            x_title == "Time"
            and not timestamp_series.empty
            and timestamp_series.notna().all()
            and timestamp_series.min().normalize() == timestamp_series.max().normalize()
        )
        x_hover_format = "%H:%M:%S" if is_single_day_time_axis else "%d.%m.%Y %H:%M:%S"

        has_forecast = not forecast.empty
        if not observed.empty:
            figure.add_trace(
                go.Scatter(
                    x=observed["timestamp"],
                    y=observed["power_kw"],
                    mode="lines",
                    fill="tozeroy",
                    fillcolor=accent_transparent_80 if has_forecast else accent_transparent_50,
                    line=dict(color=accent_hex, width=2.2, shape="linear"),
                    name=tr_fragment(f"{self._series_label} Power"),
                    hovertemplate=f"{tr_fragment(self._series_label)}: %{{y:.2~f}}{unit_suffix}<extra></extra>",
                    xhoverformat=x_hover_format,
                )
            )
        if not forecast.empty:
            figure.add_trace(
                go.Scatter(
                    x=forecast["timestamp"],
                    y=forecast["power_kw"],
                    mode="lines",
                    fill="tozeroy",
                    fillcolor=forecast_rgba,
                    line=dict(color=forecast_hex, width=2.2, shape="linear"),
                    name=tr_fragment(f"{self._series_label} Forecast"),
                    hovertemplate=f"{tr('Forecast')}: %{{y:.2~f}}{unit_suffix}<extra></extra>",
                    xhoverformat=x_hover_format,
                )
            )
        xaxis_extra = {
            "showgrid": True,
            "nticks": 16,
            "zeroline": False,
            "showline": False,
        }
        if x_title == "Time":
            xaxis_extra.update(
                {
                    "tickformat": "%H:%M" if is_single_day_time_axis else "%H:%M<br>%d.%m.%Y",
                    "hoverformat": x_hover_format,
                }
            )
        apply_energyflow_chart_style(
            figure,
            y_title=y_title,
            x_title=x_title,
            margin={"l": 40, "r": 20, "t": top_margin, "b": 40},
            xaxis_extra=xaxis_extra,
            yaxis_extra={
                "showgrid": True,
                "nticks": 12,
                "zeroline": True,
                "zerolinecolor": zero_line_rgba,
                "showline": False,
            },
        )
        figure.update_layout(title=title)
        return figure

    def _bar_figure(self, dataframe: pd.DataFrame, *, x: str, y: str, title: str, x_title: str, y_title: str) -> go.Figure:
        figure = go.Figure()
        accent_fill = f"rgba({self._accent_color.red()}, {self._accent_color.green()}, {self._accent_color.blue()}, 0.34)"
        top_margin = 48 if title else 20
        x_values, xaxis_extra = self._resolve_x_axis_values(dataframe[x], x_title=x_title)
        unit = ""
        if "(" in y_title and ")" in y_title:
            unit = y_title.split("(", 1)[1].split(")", 1)[0].strip()
        unit_suffix = f" {unit}" if unit else ""

        figure.add_trace(
            go.Bar(
                x=x_values,
                y=dataframe[y],
                marker=dict(
                    color=accent_fill,
                    line=dict(color=self._accent_color.lighter(135).name(), width=1.4),
                ),
                name=tr_fragment(f"{self._series_label} {self._history_noun.title()}"),
                hovertemplate=f"{tr_fragment(self._series_label)} {tr_fragment(self._history_noun.title())}: %{{y:.2~f}}{unit_suffix}<extra></extra>",
            )
        )
        apply_energyflow_chart_style(
            figure,
            y_title=y_title,
            x_title=x_title,
            margin={"l": 40, "r": 20, "t": top_margin, "b": 40},
            xaxis_extra=xaxis_extra,
            yaxis_extra={
                "showgrid": True,
                "zeroline": True,
                "zerolinecolor": f"rgba({self._accent_color.red()}, {self._accent_color.green()}, {self._accent_color.blue()}, 0.24)",
                "showline": False,
            },
        )
        figure.update_layout(title=title, bargap=0.18)
        return figure

    def _resolve_x_axis_values(self, x_values, *, x_title: str):
        x_raw = pd.Series(x_values)
        # Keep year axes categorical to prevent Pandas datetime coercion from
        # interpreting plain years (e.g. 2025) as epoch-based timestamps.
        if x_title == "Year":
            categories = [str(value).strip() for value in x_raw.tolist()]
            xaxis_extra = {
                "showgrid": True,
                "zeroline": False,
                "showline": False,
                "type": "category",
                "categoryorder": "array",
                "categoryarray": categories,
            }
            return categories, xaxis_extra

        x_dt = pd.to_datetime(x_raw, errors="coerce")
        has_datetime_axis = bool(len(x_dt.index)) and x_dt.notna().all()
        resolved = x_dt if has_datetime_axis else x_raw
        return resolved, self._axis_ticks_for_period(resolved, x_title=x_title)

    def _axis_ticks_for_period(self, x_values, *, x_title: str) -> dict[str, object]:
        xaxis_extra: dict[str, object] = {
            "showgrid": True,
            "zeroline": False,
            "showline": False,
        }
        x_dt = pd.to_datetime(x_values, errors="coerce")
        has_datetime_axis = bool(len(x_dt.index)) and x_dt.notna().all()
        if not has_datetime_axis:
            return xaxis_extra
        if x_title == "Day":
            weekend_mask = x_dt.dt.weekday >= 5
            tickvals = x_dt.tolist()
            ticktext = [
                f"<span style='color:#ef4444'>{day}</span>" if is_weekend else str(day)
                for day, is_weekend in zip(x_dt.dt.day.tolist(), weekend_mask.tolist(), strict=False)
            ]
            xaxis_extra.update({"tickmode": "array", "tickvals": tickvals, "ticktext": ticktext})
        elif x_title == "Month":
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
                }
            )
        return xaxis_extra

    def _localized_month_year(self, year: int, month: int) -> str:
        month_key = self._MONTH_ABBR.get(int(month), "")
        month_text = tr(month_key) if month_key else str(month)
        return f"{month_text} {int(year)}"

    def _energy_axis_values(self, series: pd.Series) -> tuple[pd.Series, str]:
        numeric = pd.to_numeric(series, errors="coerce").fillna(0.0)
        if numeric.max() >= 1000.0:
            return numeric / 1000.0, f"{self._history_noun.title()} (kWh)"
        return numeric, f"{self._history_noun.title()} (Wh)"

    def _sum_energy_for_day(self, day: pd.Timestamp) -> float:
        if self._energy_segments.empty:
            return 0.0
        return float(self._energy_segments.loc[self._energy_segments["period_day"] == day, "energy_wh"].sum())

    def _format_energy(self, energy_wh: float) -> str:
        return f"{energy_wh / 1000.0:.2f} {tr('kWh')}"

    def _tab_icon(self, kind: str) -> QIcon:
        pixmap = QPixmap(18, 18)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        accent = QColor(self._accent_color)
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
            painter.drawRect(13, 4, 2, 11)

        painter.end()
        return QIcon(pixmap)

    def _go_prev_day(self) -> None:
        self._selected_day = pd.Timestamp(self._selected_day) - pd.Timedelta(days=1)
        self._update_day_tab()

    def _go_next_day(self) -> None:
        next_day = pd.Timestamp(self._selected_day) + pd.Timedelta(days=1)
        if next_day > self._max_forecast_day:
            return
        self._selected_day = next_day
        self._update_day_tab()

    def _go_prev_month(self) -> None:
        self._selected_month = self._selected_month - 1
        self._update_month_tab()

    def _go_next_month(self) -> None:
        next_month = self._selected_month + 1
        if next_month > self._latest_timestamp.to_period("M"):
            return
        self._selected_month = next_month
        self._update_month_tab()

    def _go_prev_year(self) -> None:
        self._selected_year -= 1
        self._update_year_tab()

    def _go_next_year(self) -> None:
        if self._selected_year >= int(self._latest_timestamp.year):
            return
        self._selected_year += 1
        self._update_year_tab()

    def _go_prev_total_window(self) -> None:
        self._total_window_start = max(0, self._total_window_start - self._total_window_size)
        self._update_total_tab()

    def _go_next_total_window(self) -> None:
        max_start = max(0, len(self._available_years) - self._total_window_size)
        self._total_window_start = min(max_start, self._total_window_start + self._total_window_size)
        self._update_total_tab()

    def _open_day_picker(self) -> None:
        selected = pd.Timestamp(self._selected_day)
        self._day_picker.open_for(self.day_page.period_button, QDate(selected.year, selected.month, selected.day))

    def _open_month_picker(self) -> None:
        self._month_picker.open_for(self.month_page.period_button, self._selected_month.year, self._selected_month.month)

    def _open_year_picker(self) -> None:
        self._year_picker.set_years(self._available_years)
        self._year_picker.open_for(self.year_page.period_button, self._selected_year)

    def _select_day_from_picker(self, value: QDate) -> None:
        selected = pd.Timestamp(year=value.year(), month=value.month(), day=value.day())
        if selected > self._max_forecast_day:
            selected = self._max_forecast_day
        self._selected_day = selected
        self._update_day_tab()

    def _select_month_from_picker(self, year: int, month: int) -> None:
        selected_month = pd.Period(f"{year}-{month:02d}", freq="M")
        if selected_month > self._latest_timestamp.to_period("M"):
            return
        self._selected_month = selected_month
        self._update_month_tab()

    def _select_year_from_picker(self, year: int) -> None:
        self._selected_year = year
        self._update_year_tab()


class PvHistoryDialog(PowerHistoryDialog):
    def __init__(
        self,
        dataframe: pd.DataFrame,
        timestamp_column: str,
        pv_column: str,
        device_name: str,
        *,
        forecast_dataframe: pd.DataFrame | None = None,
        forecast_horizon_days: int = 0,
        forecast_refresh_requested: Callable[[], None] | None = None,
        forecast_snapshot_loader: Callable[[], pd.DataFrame] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(
            dataframe,
            timestamp_column,
            pv_column,
            device_name,
            series_label="PV",
            accent_color=PV_COLOR,
            summary_title=tr("Total generation"),
            history_noun="generation",
            clip_negative=True,
            forecast_dataframe=forecast_dataframe,
            forecast_horizon_days=forecast_horizon_days,
            forecast_refresh_requested=forecast_refresh_requested,
            forecast_snapshot_loader=forecast_snapshot_loader,
            parent=parent,
        )


class WeatherMetricHistoryDialog(PowerHistoryDialog):
    def __init__(
        self,
        dataframe: pd.DataFrame,
        timestamp_column: str,
        metric_column: str,
        device_name: str,
        *,
        metric_label: str,
        metric_unit: str,
        decimals: int,
        accent_color: str,
        forecast_dataframe: pd.DataFrame | None = None,
        forecast_horizon_days: int = 0,
        forecast_refresh_requested: Callable[[], None] | None = None,
        forecast_snapshot_loader: Callable[[], pd.DataFrame] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        self._metric_label = metric_label
        self._metric_unit = metric_unit
        self._metric_decimals = max(0, int(decimals))
        super().__init__(
            dataframe,
            timestamp_column,
            metric_column,
            device_name,
            series_label=tr_fragment(metric_label),
            accent_color=accent_color,
            summary_title=tr("Average {metric_label}").format(metric_label=tr_fragment(metric_label)),
            history_noun="trend",
            clip_negative=False,
            forecast_dataframe=forecast_dataframe,
            forecast_power_column=metric_column,
            forecast_horizon_days=forecast_horizon_days,
            forecast_refresh_requested=forecast_refresh_requested,
            forecast_snapshot_loader=forecast_snapshot_loader,
            parent=parent,
        )

    def _format_metric(self, value: float | None) -> str:
        if value is None or pd.isna(value):
            return "--"
        numeric = float(value)
        if self._metric_unit == "%":
            return f"{numeric:.0f}%"
        return f"{numeric:.{self._metric_decimals}f} {self._metric_unit}"

    def _range_html(self, values: pd.Series) -> str:
        if values.empty:
            return ""
        min_value = float(values.min())
        max_value = float(values.max())
        return (
            "<div style=\"margin-top:8px;font-size:13px;font-weight:600;color:#cbd5e1;\">"
            f"<span style=\"color:#0ea5e9;\">{tr('Min')}:</span> {self._format_metric(min_value)}"
            " &nbsp;•&nbsp; "
            f"<span style=\"color:#2563eb;\">{tr('Max')}:</span> {self._format_metric(max_value)}"
            "</div>"
        )

    def _update_day_tab(self) -> None:
        day_start = pd.Timestamp(self._selected_day)
        day_end = day_start + pd.Timedelta(days=1)
        day_frame = self._dataframe[
            (self._dataframe["timestamp"] >= day_start) & (self._dataframe["timestamp"] < day_end)
        ]
        forecast_day_frame = self._forecast_dataframe[
            (self._forecast_dataframe["timestamp"] >= day_start) & (self._forecast_dataframe["timestamp"] < day_end)
        ]
        now_local = pd.Timestamp.now(tz="Europe/Kiev").tz_localize(None)
        is_today = day_start == now_local.normalize()
        today_forecast_tail = forecast_day_frame[forecast_day_frame["timestamp"] > now_local]
        is_forecast_day = day_start > self._latest_timestamp.normalize() and not forecast_day_frame.empty
        active_day_frame = day_frame if is_today else (forecast_day_frame if is_forecast_day else day_frame)
        values = pd.to_numeric(active_day_frame["power_kw"], errors="coerce").dropna()
        average_value = float(values.mean()) if not values.empty else None
        self._set_summary_content(
            self.day_page.summary_label,
            amount_text=self._format_metric(average_value),
            breakdown_html=self._range_html(values),
        )
        context_suffix = tr(" (Forecast)") if is_forecast_day else ""
        self._set_page_context(
            self.day_page,
            tr("{series_label} for {date}{context_suffix}").format(
                series_label=tr_fragment(self._series_label),
                date=day_start.strftime("%d.%m.%Y"),
                context_suffix=context_suffix,
            ),
        )
        self.day_page.period_button.setText(day_start.strftime("%d.%m.%Y"))
        self.day_page.next_button.setEnabled(day_start < self._max_forecast_day)
        should_render_today_with_forecast = is_today and not today_forecast_tail.empty
        if active_day_frame.empty and not should_render_today_with_forecast:
            if day_start > self._latest_timestamp.normalize():
                if self._forecast_failed_day is not None and day_start == self._forecast_failed_day:
                    self.day_page.graph_view.set_placeholder(tr("Forecast is unavailable for this future day."))
                    return
                requested = self._request_forecast_for_day(day_start)
                if requested:
                    return
            placeholder = (
                tr("No {series_label} forecast for the selected future day.").format(
                    series_label=tr_fragment(self._series_label).lower()
                )
                if day_start > self._latest_timestamp.normalize()
                else tr("No {series_label} samples for the selected day.").format(
                    series_label=tr_fragment(self._series_label).lower()
                )
            )
            self.day_page.graph_view.set_placeholder(placeholder)
            return
        chart_title = ""
        self.day_page.graph_view.set_figure(
            self._line_figure(
                active_day_frame,
                title=chart_title,
                x_title="Time",
                y_title=f"{self._series_label} ({self._metric_unit})",
                forecast_dataframe=today_forecast_tail if is_today else None,
            )
        )

    def _update_month_tab(self) -> None:
        month_label = self._localized_month_year(self._selected_month.year, self._selected_month.month)
        month_start = self._selected_month.to_timestamp()
        month_end = month_start + pd.offsets.MonthEnd(0) + pd.Timedelta(days=1)
        month_frame = self._dataframe[
            (self._dataframe["timestamp"] >= month_start) & (self._dataframe["timestamp"] < month_end)
        ].copy().sort_values("timestamp")
        values = pd.to_numeric(month_frame["power_kw"], errors="coerce").dropna()
        average_value = float(values.mean()) if not values.empty else None
        self._set_summary_content(
            self.month_page.summary_label,
            amount_text=self._format_metric(average_value),
            breakdown_html=self._range_html(values),
        )
        self._set_page_context(
            self.month_page,
            tr("{series_label} trend for {period_label}").format(
                series_label=tr_fragment(self._series_label),
                period_label=month_label,
            ),
        )
        self.month_page.period_button.setText(month_label)
        self.month_page.next_button.setEnabled(self._selected_month < self._latest_timestamp.to_period("M"))
        if month_frame.empty:
            self.month_page.graph_view.set_placeholder(
                tr("No {series_label} samples for the selected month.").format(
                    series_label=tr_fragment(self._series_label).lower()
                )
            )
            return
        self.month_page.graph_view.set_figure(
            self._line_figure(
                month_frame,
                title="",
                x_title="Time",
                y_title=f"{self._series_label} ({self._metric_unit})",
            )
        )

    def _update_year_tab(self) -> None:
        year_start = pd.Timestamp(year=self._selected_year, month=1, day=1)
        year_end = pd.Timestamp(year=self._selected_year + 1, month=1, day=1)
        year_frame = self._dataframe[
            (self._dataframe["timestamp"] >= year_start) & (self._dataframe["timestamp"] < year_end)
        ].copy().sort_values("timestamp")
        values = pd.to_numeric(year_frame["power_kw"], errors="coerce").dropna()
        average_value = float(values.mean()) if not values.empty else None
        self._set_summary_content(
            self.year_page.summary_label,
            amount_text=self._format_metric(average_value),
            breakdown_html=self._range_html(values),
        )
        self._set_page_context(
            self.year_page,
            tr("{series_label} trend for {period_label}").format(
                series_label=tr_fragment(self._series_label),
                period_label=self._selected_year,
            ),
        )
        self.year_page.period_button.setText(str(self._selected_year))
        self.year_page.next_button.setEnabled(self._selected_year < int(self._latest_timestamp.year))
        if year_frame.empty:
            self.year_page.graph_view.set_placeholder(
                tr("No {series_label} samples for the selected year.").format(
                    series_label=tr_fragment(self._series_label).lower()
                )
            )
            return
        self.year_page.graph_view.set_figure(
            self._line_figure(
                year_frame,
                title="",
                x_title="Time",
                y_title=f"{self._series_label} ({self._metric_unit})",
            )
        )

    def _update_total_tab(self) -> None:
        window_years = self._available_years[self._total_window_start : self._total_window_start + self._total_window_size]
        if not window_years:
            self.total_page.graph_view.set_placeholder(
                tr("No {series_label} history available.").format(
                    series_label=tr_fragment(self._series_label).lower()
                )
            )
            return
        window_start = pd.Timestamp(year=window_years[0], month=1, day=1)
        window_end = pd.Timestamp(year=window_years[-1] + 1, month=1, day=1)
        total_frame = self._dataframe[
            (self._dataframe["timestamp"] >= window_start) & (self._dataframe["timestamp"] < window_end)
        ].copy().sort_values("timestamp")
        values = pd.to_numeric(total_frame["power_kw"], errors="coerce").dropna()
        average_value = float(values.mean()) if not values.empty else None
        range_label = f"{window_years[0]} - {window_years[-1]}" if window_years else "--"
        self._set_summary_content(
            self.total_page.summary_label,
            amount_text=self._format_metric(average_value),
            breakdown_html=self._range_html(values),
        )
        self._set_page_context(
            self.total_page,
            tr("{series_label} across {range_label}").format(
                series_label=tr_fragment(self._series_label),
                range_label=range_label,
            ),
        )
        self.total_page.period_button.setText(range_label)
        max_start = max(0, len(self._available_years) - self._total_window_size)
        self.total_page.next_button.setEnabled(self._total_window_start < max_start)
        if total_frame.empty:
            self.total_page.graph_view.set_placeholder(
                tr("No {series_label} history available.").format(
                    series_label=tr_fragment(self._series_label).lower()
                )
            )
            return
        self.total_page.graph_view.set_figure(
            self._line_figure(
                total_frame,
                title="",
                x_title="Time",
                y_title=f"{self._series_label} ({self._metric_unit})",
            )
        )
