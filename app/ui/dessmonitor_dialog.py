from __future__ import annotations

import calendar
from datetime import datetime

from PySide6.QtCore import QDate, QTimer, Qt, Signal, QPoint, QLocale
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import (
    QApplication,
    QCalendarWidget,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTextEdit,
    QToolButton,
    QWidget,
    QVBoxLayout,
    QListView,
    QSizePolicy,
)

from app.services.dessmonitor_api import (
    DessMonitorApiError,
    DessMonitorConfig,
    DessMonitorDevice,
    fetch_devices,
    fetch_key_parameters,
    humanize_error_text,
    sanitize_error_text,
)
from app.services.i18n import tr, tr_fragment, translate_widget_tree
from app.services.storage import latest_timestamp_for_metadata
from app.ui.design_system import compose_styles, scoped_form_input_combo_qss
from app.ui.dialogs import NEON_CLOSE_BUTTON_STYLE, NEON_HEADER_BAR_STYLE, create_neon_header_bar, show_compact_message


CALENDAR_POPUP_STYLE = """
    #CalendarPopup {
        background: #0f172a;
        border: 1px solid #334155;
        border-radius: 12px;
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
    }
"""

CALENDAR_NAV_BUTTON_STYLE = "color: #f8fafc; background: #1e293b;"
CALENDAR_MONTH_BUTTON_STYLE = "color: #f8fafc; padding-right: 12px; background: transparent;"
CALENDAR_YEAR_BUTTON_STYLE = "color: #f8fafc; background: transparent;"


class BusySpinner(QWidget):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._frames = ["|", "/", "-", "\\"]
        self._index = 0
        self._timer = QTimer(self)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._tick)

        self.glyph_label = QLabel("")
        self.glyph_label.setObjectName("SpinnerGlyph")
        self.text_label = QLabel(tr("Idle"))
        self.text_label.setObjectName("SpinnerText")

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self.glyph_label)
        layout.addWidget(self.text_label, stretch=1)
        self.stop(tr("Idle"))

    def _tick(self) -> None:
        self._index = (self._index + 1) % len(self._frames)
        self.glyph_label.setText(self._frames[self._index])

    def start(self, message: str) -> None:
        self._index = 0
        self.glyph_label.setText(self._frames[self._index])
        self.text_label.setText(message)
        if not self._timer.isActive():
            self._timer.start()

    def update_message(self, message: str) -> None:
        self.text_label.setText(message)

    def stop(self, message: str) -> None:
        self._timer.stop()
        self.glyph_label.setText("")
        self.text_label.setText(message)


class CalendarDateField(QWidget):
    """Composite date input with custom popup calendar styling.

    Qt's built-in calendar popup is disabled so we can fully control navigation
    button look-and-feel and max-date clamping behavior.
    """
    dateChanged = Signal(QDate)

    def __init__(self, initial_date: QDate, parent=None, display_format: str = "yyyy-MM-dd", maximum_date: QDate | None = None) -> None:
        super().__init__(parent)
        self._maximum_date = maximum_date
        self.setObjectName("DateFieldShell")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.date_edit = QDateEdit(calendarPopup=False)
        self.date_edit.setObjectName("DateFieldEditor")
        self.date_edit.setDisplayFormat(display_format)
        self.date_edit.setDate(initial_date)
        if self._maximum_date is not None:
            self.date_edit.setMaximumDate(self._maximum_date)
        self.date_edit.dateChanged.connect(self._forward_date_changed)
        self.date_edit.editingFinished.connect(self._clamp_to_maximum)
        self.date_edit.setButtonSymbols(QDateEdit.ButtonSymbols.NoButtons)

        self.calendar_button = QToolButton()
        self.calendar_button.setText("📅")
        self.calendar_button.setObjectName("DateFieldTrigger")
        self.calendar_button.setToolTip(tr("Open calendar"))
        self.calendar_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self.calendar_button.clicked.connect(self._open_calendar_popup)

        self.calendar_popup = QDialog(self, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.calendar_popup.setObjectName("CalendarPopup")
        self.calendar_popup.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.calendar_popup.setStyleSheet(CALENDAR_POPUP_STYLE)
        popup_layout = QVBoxLayout(self.calendar_popup)
        popup_layout.setContentsMargins(8, 8, 8, 8)
        self.calendar_widget = QCalendarWidget()
        if self._maximum_date is not None:
            self.calendar_widget.setMaximumDate(self._maximum_date)
        self.calendar_widget.setSelectedDate(initial_date)
        self.calendar_widget.clicked.connect(self._select_date_from_popup)
        self.calendar_widget.currentPageChanged.connect(self._refresh_calendar_navigation)
        calendar_palette = self.calendar_widget.palette()
        disabled_gray = QColor("#64748b")
        calendar_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, disabled_gray)
        calendar_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, disabled_gray)
        calendar_palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, disabled_gray)
        self.calendar_widget.setPalette(calendar_palette)
        popup_layout.addWidget(self.calendar_widget)
        self._refresh_calendar_navigation()

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.date_edit, stretch=1)
        layout.addWidget(self.calendar_button)

    def _forward_date_changed(self, value: QDate) -> None:
        self.dateChanged.emit(value)

    def _open_calendar_popup(self) -> None:
        """Open calendar popup anchored below the field shell."""
        self.calendar_widget.setSelectedDate(self.date_edit.date())
        popup_anchor = self.mapToGlobal(QPoint(0, self.height()))
        self.calendar_popup.move(popup_anchor)
        self.calendar_popup.show()
        QTimer.singleShot(0, self._refresh_calendar_navigation)

    def _select_date_from_popup(self, value: QDate) -> None:
        if self._maximum_date is not None and value > self._maximum_date:
            value = self._maximum_date
        self.date_edit.setDate(value)
        self.calendar_popup.hide()

    def _refresh_calendar_navigation(self) -> None:
        # Replace Qt default calendar controls to match project neon calendar
        # visuals and keep month/year buttons readable on dark background.
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

    def _clamp_to_maximum(self) -> None:
        """Prevent manual edits from selecting dates above configured max."""
        if self._maximum_date is not None and self.date_edit.date() > self._maximum_date:
            self.date_edit.setDate(self._maximum_date)

    def setDate(self, value: QDate) -> None:  # noqa: N802
        if self._maximum_date is not None and value > self._maximum_date:
            value = self._maximum_date
        self.date_edit.setDate(value)

    def date(self) -> QDate:
        return self.date_edit.date()

    def setMaximumDate(self, value: QDate) -> None:  # noqa: N802
        self._maximum_date = value
        self.date_edit.setMaximumDate(value)
        self.calendar_widget.setMaximumDate(value)
        self._clamp_to_maximum()

    def setVisible(self, visible: bool) -> None:  # noqa: N802
        super().setVisible(visible)


class DessMonitorDialog(QDialog):
    """Credential/setup dialog for DessMonitor sync and profile activation."""
    _session_username = "postoluk"
    _session_password = ""
    _session_company_key = "bnrl_frRFjEz8Mkn"
    _session_period = "current_day"
    _session_date_from: str | None = None
    _session_date_to: str | None = None

    def __init__(self, parent=None, initial_state: dict[str, object] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect to DessMonitor")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(620, 340)
        self.setObjectName("DessMonitorDialog")
        initial_state = initial_state or {}
        self._initial_device_key = (
            str(initial_state.get("pn", "")),
            str(initial_state.get("devcode", "")),
            str(initial_state.get("devaddr", "")),
            str(initial_state.get("sn", "")),
        )
        self._profile_locked_mode = all(self._initial_device_key) and bool(str(initial_state.get("username", "")).strip())
        self._profile_parameter_keys = [
            str(item).strip()
            for item in initial_state.get("parameter_keys", []) or []
            if str(item).strip()
        ]
        self._last_saved_timestamp = ""
        self._last_saved_date: QDate | None = None

        self.username_input = QLineEdit(str(initial_state.get("username", self._session_username)))
        self.password_input = QLineEdit()
        self.password_input.setText(str(initial_state.get("password", self._session_password)))
        self.password_input.setEchoMode(QLineEdit.Password)
        company_key = str(initial_state.get("company_key", self._session_company_key))
        self.company_key_input = QLineEdit(company_key)
        self.company_key_input.setPlaceholderText(tr("Optional if your account allows auth without it"))

        self.source_combo = QComboBox()
        self.source_combo.addItem("Energy storage", 1)
        self.source_combo.addItem("Photovoltaic", 0)
        self.source_combo.setView(QListView())
        source_index = self.source_combo.findData(int(initial_state.get("source", self.source_combo.currentData())))
        if source_index >= 0:
            self.source_combo.setCurrentIndex(source_index)

        self.device_combo = QComboBox()
        self.device_combo.setEnabled(False)
        self.device_combo.addItem(tr("Load devices after sign in"))
        self.device_combo.setView(QListView())
        self.device_hint = QLabel(tr("Sign in to fetch devices automatically."))
        self.device_hint.setObjectName("DessMonitorHint")
        self.device_hint.setWordWrap(True)

        self.fetch_devices_button = QPushButton(tr("Load Devices"))
        self.fetch_devices_button.setObjectName("PrimaryButton")
        self.fetch_devices_button.clicked.connect(self.load_devices)

        self.i18n_input = QLineEdit("en")
        self.period_combo = QComboBox()
        self.period_combo.addItem(tr("Today only"), "current_day")
        self.period_combo.addItem(tr("From last saved record"), "from_last_saved")
        self.period_combo.addItem(tr("This week"), "current_week")
        self.period_combo.addItem(tr("This month"), "current_month")
        self.period_combo.addItem(tr("This year"), "current_year")
        self.period_combo.addItem(tr("All data"), "all_data")
        self.period_combo.addItem(tr("Custom range"), "custom")
        self.period_combo.setView(QListView())
        initial_period = str(initial_state.get("period", self._session_period))
        initial_period = {
            "last_day": "current_day",
            "last_month": "current_month",
            "last_year": "current_year",
        }.get(initial_period, initial_period)
        period_index = max(0, self.period_combo.findData(initial_period))
        self.period_combo.setCurrentIndex(period_index)
        self.period_combo.currentIndexChanged.connect(self._update_period_controls)
        self.to_label = QLabel(tr("To"))
        self.to_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.to_label.setFixedHeight(40)

        today = QDate.currentDate()
        initial_from = QDate.fromString(str(initial_state.get("date_from", self._session_date_from or today.toString("yyyy-MM-dd"))), "yyyy-MM-dd")
        initial_to = QDate.fromString(str(initial_state.get("date_to", self._session_date_to or today.toString("yyyy-MM-dd"))), "yyyy-MM-dd")
        if not initial_from.isValid() or initial_from > today:
            initial_from = today
        if not initial_to.isValid() or initial_to > today:
            initial_to = today
        self.date_input = CalendarDateField(initial_from, maximum_date=today)
        self.date_to_input = CalendarDateField(initial_to, maximum_date=today)
        if self.date_to_input.date() < self.date_input.date():
            self.date_to_input.setDate(self.date_input.date())
        self.date_input.dateChanged.connect(self._sync_date_range_from_start)
        self.date_to_input.dateChanged.connect(self._sync_date_range_from_end)
        self.custom_range_widget = QWidget()
        date_row = QHBoxLayout(self.custom_range_widget)
        date_row.setContentsMargins(0, 0, 0, 0)
        date_row.setSpacing(12)
        date_row.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        date_row.addWidget(self.date_input, stretch=1)
        date_row.addWidget(self.to_label)
        date_row.addWidget(self.date_to_input, stretch=1)

        self.auth_log = QTextEdit()
        self.auth_log.setReadOnly(True)
        self.auth_log.setPlaceholderText(tr("Authentication log will appear here."))
        self.auth_log.setMaximumHeight(140)

        self.spinner = BusySpinner()

        self.hero_card = QWidget()
        self.hero_card.setObjectName("HeroCard")
        hero_layout = QHBoxLayout(self.hero_card)
        hero_layout.setContentsMargins(16, 14, 16, 14)
        hero_layout.setSpacing(14)
        self.hero_badge = QLabel("↻")
        self.hero_badge.setObjectName("HeroBadge")
        self.hero_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hero_text = QVBoxLayout()
        hero_text.setContentsMargins(0, 0, 0, 0)
        hero_text.setSpacing(2)
        self.hero_eyebrow = QLabel(tr("Sync now"))
        self.hero_eyebrow.setObjectName("HeroEyebrow")
        self.hero_title = QLabel(tr("Pull fresh telemetry from DessMonitor"))
        self.hero_title.setObjectName("HeroTitle")
        self.hero_subtitle = QLabel(tr("Choose how much history to sync. Shorter periods finish faster."))
        self.hero_subtitle.setObjectName("HeroSubtitle")
        self.hero_subtitle.setWordWrap(True)
        hero_text.addWidget(self.hero_eyebrow)
        hero_text.addWidget(self.hero_title)
        hero_text.addWidget(self.hero_subtitle)
        hero_layout.addWidget(self.hero_badge, alignment=Qt.AlignmentFlag.AlignTop)
        hero_layout.addLayout(hero_text, stretch=1)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("DessMonitorSummary")
        self.summary_label.setWordWrap(True)
        hint = QLabel(tr("Choose a sync period. Larger periods may take longer to import."))
        hint.setObjectName("DessMonitorHint")
        hint.setWordWrap(True)
        self.hint_label = hint

        self.form = QFormLayout()
        self.form.setContentsMargins(0, 10, 0, 12)
        self.form.setHorizontalSpacing(16)
        self.form.setVerticalSpacing(16)
        self.form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        self.username_label = QLabel(tr("Username"))
        self.password_label = QLabel(tr("Password"))
        self.company_key_label = QLabel(tr("Company key"))
        self.platform_label = QLabel(tr("Platform"))
        self.device_label = QLabel(tr("Device"))
        self.period_label = QLabel(tr("Sync period"))
        self.datasets_label = QLabel(tr("Datasets"))
        self.selected_dates_label = QLabel(tr("Selected dates"))
        self.language_label = QLabel(tr("Language"))
        for form_label in (
            self.username_label,
            self.password_label,
            self.company_key_label,
            self.platform_label,
            self.device_label,
            self.period_label,
            self.datasets_label,
            self.selected_dates_label,
            self.language_label,
        ):
            form_label.setObjectName("FormRowLabel")
            form_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
            form_label.setMinimumHeight(40)

        self.form.addRow(self.username_label, self.username_input)
        self.form.addRow(self.password_label, self.password_input)
        self.form.addRow(self.company_key_label, self.company_key_input)
        self.form.addRow(self.platform_label, self.source_combo)

        self.device_row = QHBoxLayout()
        self.device_row.addWidget(self.device_combo, stretch=1)
        self.device_row.addWidget(self.fetch_devices_button)
        self.form.addRow(self.device_label, self.device_row)

        self.form.addRow(self.period_label, self.period_combo)
        self.import_dessmonitor_checkbox = QCheckBox(tr("DessMonitor"))
        self.import_dessmonitor_checkbox.setChecked(
            bool(initial_state.get("include_dessmonitor", True))
        )
        weather_available = bool(initial_state.get("weather_available", False))
        self.import_weather_checkbox = QCheckBox(tr("Weather History"))
        self.import_weather_checkbox.setChecked(
            bool(initial_state.get("include_weather", weather_available))
        )
        self.import_weather_checkbox.setEnabled(weather_available)
        self.import_dessmonitor_checkbox.clicked.connect(
            lambda checked: self._handle_dataset_checkbox_clicked(self.import_dessmonitor_checkbox, checked)
        )
        self.import_weather_checkbox.clicked.connect(
            lambda checked: self._handle_dataset_checkbox_clicked(self.import_weather_checkbox, checked)
        )

        self.dataset_selection_widget = QWidget()
        dataset_selection_layout = QVBoxLayout(self.dataset_selection_widget)
        dataset_selection_layout.setContentsMargins(0, 0, 0, 0)
        dataset_selection_layout.setSpacing(8)
        dataset_checkbox_row = QHBoxLayout()
        dataset_checkbox_row.setContentsMargins(0, 0, 0, 0)
        dataset_checkbox_row.setSpacing(18)
        dataset_checkbox_row.addWidget(self.import_dessmonitor_checkbox)
        dataset_checkbox_row.addWidget(self.import_weather_checkbox)
        dataset_checkbox_row.addStretch()
        dataset_selection_layout.addLayout(dataset_checkbox_row)
        self.dataset_hint_label = QLabel("")
        self.dataset_hint_label.setObjectName("DessMonitorHint")
        self.dataset_hint_label.setWordWrap(True)
        self.dataset_hint_label.setContentsMargins(2, 0, 0, 0)
        self.dataset_hint_label.hide()
        dataset_selection_layout.addWidget(self.dataset_hint_label)
        self.form.addRow(self.datasets_label, self.dataset_selection_widget)
        self.form.addRow(self.selected_dates_label, self.custom_range_widget)
        self.form.addRow(self.language_label, self.i18n_input)
        self.period_info_label = QLabel("")
        self.period_info_label.setObjectName("DessMonitorHint")
        self.period_info_label.setWordWrap(True)
        self.period_info_label.setContentsMargins(2, -6, 0, 0)
        self.form.addRow(QLabel(""), self.period_info_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_button = buttons.button(QDialogButtonBox.Ok)
        if ok_button is not None:
            ok_button.setText(tr("Sync now"))
            ok_button.setObjectName("PrimaryActionButton")
        cancel_button = buttons.button(QDialogButtonBox.Cancel)
        if cancel_button is not None:
            cancel_button.setObjectName("SecondaryActionButton")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(10)
        layout.addWidget(create_neon_header_bar(self, self.reject, title=tr("DessMonitor Sync")))
        layout.addSpacing(20)
        layout.addWidget(self.hero_card)
        layout.addWidget(self.summary_label)
        layout.addWidget(hint)
        layout.addSpacing(2)
        layout.addLayout(self.form)
        layout.addWidget(self.device_hint)
        layout.addWidget(self.spinner)
        layout.addWidget(self.auth_log)
        layout.addWidget(buttons)
        self._apply_styles()
        self._devices: list[DessMonitorDevice] = []
        self._available_parameter_keys: list[str] = []
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        self._update_period_controls()
        self._refresh_settings_summary()
        self._apply_profile_mode(initial_state)
        self._load_last_saved_record_info()
        self._refresh_dataset_hint()
        self._update_period_controls()
        translate_widget_tree(self)

    def _apply_styles(self) -> None:
        """Apply full dialog theme for cards, inputs, date controls and logs."""
        self.setStyleSheet(
            compose_styles(
                """
            #DessMonitorDialog {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #09111f, stop:0.55 #0f172a, stop:1 #11233b);
            }
            #DessMonitorDialog QLabel {
                color: #e2e8f0;
                font-size: 13px;
            }
            #FormRowLabel {
                color: #e2e8f0;
                font-size: 13px;
                padding: 0;
            }
            #HeroCard {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
                    stop:0 rgba(14, 165, 233, 0.16), stop:1 rgba(34, 197, 94, 0.10));
                border: 1px solid rgba(103, 232, 249, 0.24);
                border-radius: 18px;
            }
            #HeroBadge {
                min-width: 48px;
                max-width: 48px;
                min-height: 48px;
                max-height: 48px;
                border-radius: 24px;
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #0ea5e9, stop:1 #22c55e);
                color: #eff6ff;
                font-size: 24px;
                font-weight: 700;
                qproperty-alignment: 'AlignCenter';
            }
            #HeroEyebrow {
                color: #67e8f9;
                font-size: 12px;
                font-weight: 700;
            }
            #HeroTitle {
                color: #f8fafc;
                font-size: 17px;
                font-weight: 700;
            }
            #HeroSubtitle {
                color: #cbd5e1;
                font-size: 13px;
            }
            #DessMonitorSummary {
                color: #cbd5e1;
                font-size: 13px;
                background: rgba(15, 23, 42, 0.82);
                border: 1px solid rgba(51, 65, 85, 0.9);
                border-radius: 12px;
                padding: 10px 12px;
            }
            #DessMonitorHint {
                color: #cbd5e1;
                padding-bottom: 4px;
                font-size: 13px;
            }
            #DessSectionCard {
                background: rgba(8, 15, 30, 0.78);
                border: none;
                border-radius: 14px;
            }
            #DessSectionCard:hover {
                background: rgba(10, 19, 36, 0.86);
            }
            #DessMonitorDialog QComboBox,
            #DessMonitorDialog #DateFieldShell {
                min-height: 40px;
                background: transparent;
                border: 1px solid #243244;
                border-radius: 14px;
            }
            #DessMonitorDialog #DateFieldShell:hover,
            #DessMonitorDialog QComboBox:hover {
                border-color: #35506d;
            }
            #DessMonitorDialog #DateFieldShell {
                padding: 0;
            }
            #DessMonitorDialog #DateFieldEditor {
                background: transparent;
                color: #f8fafc;
                border: none;
                padding: 0 0 0 14px;
                min-height: 38px;
                font-size: 13px;
            }
            #DessMonitorDialog #DateFieldEditor::drop-down {
                width: 0px;
                border: none;
                background: transparent;
            }
            #DessMonitorDialog #DateFieldEditor::down-arrow {
                image: none;
                width: 0px;
                height: 0px;
            }
            #DessMonitorDialog #DateFieldTrigger {
                min-width: 36px;
                max-width: 36px;
                border: none;
                background: transparent;
                color: #cbd5e1;
                font-size: 14px;
                padding: 0 10px 0 0;
            }
            #DessMonitorDialog #DateFieldTrigger:hover {
                color: #f8fafc;
            }
            #CalendarPopup {
                background: #0f172a;
                border: 1px solid #334155;
                border-radius: 12px;
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
                background: #111827;
                color: #e2e8f0;
                border: 1px solid #334155;
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
            }
            #DessMonitorDialog QCheckBox {
                color: #e2e8f0;
                font-size: 13px;
                spacing: 10px;
                padding: 4px 0;
            }
            #DessMonitorDialog QCalendarWidget QWidget {
                background: #111827;
                color: #e2e8f0;
            }
            #DessMonitorDialog QCalendarWidget QToolButton {
                background: #1e293b;
                color: #e2e8f0;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 4px 8px;
            }
            #DessMonitorDialog QCalendarWidget QMenu {
                background: #111827;
                color: #e2e8f0;
                border: 1px solid #334155;
            }
            #DessMonitorDialog QCalendarWidget QSpinBox {
                background: #111827;
                color: #e2e8f0;
                selection-background-color: #0ea5e9;
                selection-color: #eff6ff;
            }
            #DessMonitorDialog QCalendarWidget QAbstractItemView {
                background: #111827;
                color: #e2e8f0;
                selection-background-color: #2563eb;
                selection-color: #eff6ff;
                alternate-background-color: #0f172a;
                gridline-color: #1f2937;
            }
            #DessMonitorDialog QCalendarWidget QAbstractItemView:disabled {
                color: #64748b;
            }
            #DessMonitorDialog QLabel#SpinnerGlyph {
                min-width: 18px;
                font-family: Menlo, Monaco, monospace;
                font-size: 16px;
                color: #38bdf8;
                font-weight: 700;
            }
            #DessMonitorDialog QLabel#SpinnerText {
                color: #cbd5e1;
                font-size: 14px;
            }
                """,
                scoped_form_input_combo_qss("#DessMonitorDialog", include_text_edit=True),
                NEON_CLOSE_BUTTON_STYLE,
                NEON_HEADER_BAR_STYLE,
            )
        )

    def _show_message(self, icon, title: str, text: str) -> None:
        kind = "info"
        if icon == QMessageBox.Critical:
            kind = "error"
        elif icon == QMessageBox.Warning:
            kind = "warning"
        show_compact_message(self, kind=kind, title=title, text=humanize_error_text(text))

    def _apply_profile_mode(self, initial_state: dict[str, object]) -> None:
        """Switch dialog to profile-locked mode when opened from active profile."""
        if not self._profile_locked_mode:
            return

        device_alias = str(initial_state.get("device_label", "")).strip()
        locked_device = DessMonitorDevice(
            pn=str(initial_state.get("pn", "")).strip(),
            devcode=str(initial_state.get("devcode", "")).strip(),
            devaddr=str(initial_state.get("devaddr", "")).strip(),
            sn=str(initial_state.get("sn", "")).strip(),
            alias=device_alias,
        )
        self._devices = [locked_device]
        self._available_parameter_keys = list(self._profile_parameter_keys)

        profile_name = str(initial_state.get("profile_name", "")).strip() or "Saved profile"
        device_text = locked_device.display_name or "Saved device"
        self.hero_title.setText(tr("Sync data from your saved profile"))
        self.hero_subtitle.setText(tr("Choose how much history to sync, then pull the newest telemetry."))
        self.summary_label.setText(tr_fragment(f"Profile: {profile_name}\nDevice: {device_text}"))
        self.hint_label.hide()
        self.device_hint.hide()
        self._spinner_stop("Ready")
        self.form.setRowVisible(self.username_input, False)
        self.form.setRowVisible(self.password_input, False)
        self.form.setRowVisible(self.company_key_input, False)
        self.form.setRowVisible(self.source_combo, False)
        self.form.setRowVisible(self.device_row, False)
        self.form.setRowVisible(self.i18n_input, False)

        self.device_combo.hide()
        self.fetch_devices_button.hide()
        self.auth_log.hide()
        self.spinner.hide()

        preferred_period = str(initial_state.get("period", self.period_combo.currentData()))
        period_index = self.period_combo.findData(preferred_period)
        if period_index < 0:
            period_index = self.period_combo.findData("current_day")
        self.period_combo.setCurrentIndex(max(0, period_index))

    def _append_log(self, message: str) -> None:
        self.auth_log.append(humanize_error_text(message))
        QTimer.singleShot(0, self.auth_log.update)

    def _spinner_start(self, label: str) -> None:
        self.spinner.start(label)
        QTimer.singleShot(0, self.spinner.update)

    def _spinner_update(self, label: str) -> None:
        self.spinner.update_message(label)
        QTimer.singleShot(0, self.spinner.update)

    def _spinner_stop(self, label: str) -> None:
        self.spinner.stop(label)
        QTimer.singleShot(0, self.spinner.update)

    def config(self) -> DessMonitorConfig:
        device = self.selected_device()
        date_from, date_to = self._resolved_date_range(device)
        return DessMonitorConfig(
            username=self.username_input.text().strip(),
            password=self.password_input.text().strip(),
            company_key=self.company_key_input.text().strip(),
            source=int(self.source_combo.currentData()),
            pn=device.pn,
            devcode=device.devcode,
            devaddr=device.devaddr,
            sn=device.sn,
            device_label=device.display_name,
            parameter_keys=list(self._profile_parameter_keys),
            date_from=date_from,
            date_to=date_to,
            period_mode=str(self.period_combo.currentData()),
            i18n=self.i18n_input.text().strip() or "en",
        )

    def selected_dataset_targets(self) -> tuple[bool, bool]:
        return (
            self.import_dessmonitor_checkbox.isChecked(),
            self.import_weather_checkbox.isChecked() and self.import_weather_checkbox.isEnabled(),
        )

    def _handle_dataset_checkbox_clicked(self, checkbox: QCheckBox, checked: bool) -> None:
        """Ensure at least one import target remains selected."""
        if checked:
            self._refresh_dataset_hint()
            return
        other_checkbox = (
            self.import_weather_checkbox
            if checkbox is self.import_dessmonitor_checkbox
            else self.import_dessmonitor_checkbox
        )
        if not other_checkbox.isChecked():
            checkbox.blockSignals(True)
            checkbox.setChecked(True)
            checkbox.blockSignals(False)
        self._refresh_dataset_hint()

    def _refresh_dataset_hint(self) -> None:
        """Show weather-target hint only when weather import is unavailable."""
        if not self.import_weather_checkbox.isEnabled():
            self.dataset_hint_label.setText(
                tr("Weather History will become available after saving inverter coordinates in the profile.")
            )
            self.dataset_hint_label.show()
            return
        self.dataset_hint_label.setText("")
        self.dataset_hint_label.hide()

    def selected_device(self) -> DessMonitorDevice:
        index = self.device_combo.currentIndex()
        if index < 0 or index >= len(self._devices):
            raise DessMonitorApiError("Select a DessMonitor device before importing data.")
        return self._devices[index]

    def _auth_config(self) -> DessMonitorConfig:
        today_iso = self.date_input.date().toPython().isoformat()
        return DessMonitorConfig(
            username=self.username_input.text().strip(),
            password=self.password_input.text().strip(),
            company_key=self.company_key_input.text().strip(),
            source=int(self.source_combo.currentData()),
            pn="",
            devcode="",
            devaddr="",
            sn="",
            device_label="",
            parameter_keys=[],
            date_from=today_iso,
            date_to=today_iso,
            period_mode=str(self.period_combo.currentData()),
            i18n=self.i18n_input.text().strip() or "en",
        )

    def _validate_auth_fields(self) -> list[str]:
        if self._profile_locked_mode:
            self._session_period = str(self.period_combo.currentData())
            self._session_date_from = self.date_input.date().toPython().isoformat()
            self._session_date_to = self.date_to_input.date().toPython().isoformat()
            return []
        self._session_username = self.username_input.text().strip()
        self._session_password = self.password_input.text()
        self._session_company_key = self.company_key_input.text().strip()
        self._session_period = str(self.period_combo.currentData())
        self._session_date_from = self.date_input.date().toPython().isoformat()
        self._session_date_to = self.date_to_input.date().toPython().isoformat()
        required_values = {
            "Username": self.username_input.text().strip(),
            "Password": self.password_input.text(),
        }
        return [label for label, value in required_values.items() if not value]

    def _sync_date_range_from_start(self, selected_date: QDate) -> None:
        if self.date_to_input.date() < selected_date:
            self.date_to_input.setDate(selected_date)

    def _sync_date_range_from_end(self, selected_date: QDate) -> None:
        if selected_date < self.date_input.date():
            self.date_input.setDate(selected_date)

    def _resolved_date_range(self, device: DessMonitorDevice) -> tuple[str, str]:
        today = QDate.currentDate()
        period = str(self.period_combo.currentData())
        if period == "current_day":
            return today.toPython().isoformat(), today.toPython().isoformat()
        if period == "from_last_saved":
            if self._last_saved_date is None or not self._last_saved_date.isValid():
                raise DessMonitorApiError("No saved history was found for this profile yet.")
            return self._last_saved_date.toPython().isoformat(), today.toPython().isoformat()
        if period == "current_week":
            start_date = today.addDays(1 - today.dayOfWeek())
            return start_date.toPython().isoformat(), today.toPython().isoformat()
        if period == "current_month":
            start_date = QDate(today.year(), today.month(), 1)
            return start_date.toPython().isoformat(), today.toPython().isoformat()
        if period == "current_year":
            start_date = QDate(today.year(), 1, 1)
            return start_date.toPython().isoformat(), today.toPython().isoformat()
        if period == "all_data":
            install = QDate.fromString(device.install_date, "yyyy-MM-dd")
            start_date = install if install.isValid() and install <= today else today.addDays(-364)
            return start_date.toPython().isoformat(), today.toPython().isoformat()
        return self.date_input.date().toPython().isoformat(), self.date_to_input.date().toPython().isoformat()

    def _update_period_controls(self) -> None:
        is_custom = self.period_combo.currentData() == "custom"
        self.custom_range_widget.setVisible(is_custom)
        self.form.setRowVisible(self.custom_range_widget, is_custom)
        period = str(self.period_combo.currentData())
        info_text = ""
        if period == "from_last_saved":
            if self._last_saved_date is not None and self._last_saved_date.isValid():
                today_text = QDate.currentDate().toString("dd.MM.yyyy")
                info_text = (
                    tr_fragment(f"Last saved record: {self._format_saved_timestamp(self._last_saved_timestamp)}\n")
                    + tr_fragment(f"Sync range: {self._last_saved_date.toString('dd.MM.yyyy')} -> {today_text}")
                )
            else:
                info_text = tr("No saved history for this profile yet.")
        self.period_info_label.setText(info_text)
        self.period_info_label.setVisible(bool(info_text))
        self.form.setRowVisible(self.period_info_label, bool(info_text))
        self._sync_dialog_size()

    def _sync_dialog_size(self) -> None:
        extra_height = 52 if self.period_info_label.isVisible() else 0
        if self._profile_locked_mode:
            self.resize(620, (420 if self.period_combo.currentData() == "custom" else 360) + extra_height)
            return
        self.resize(620, (640 if self.period_combo.currentData() == "custom" else 560) + extra_height)

    def _load_last_saved_record_info(self) -> None:
        metadata = self._current_profile_metadata()
        if metadata is None:
            self._last_saved_timestamp = ""
            self._last_saved_date = None
            return
        last_timestamp = latest_timestamp_for_metadata(metadata)
        self._last_saved_timestamp = str(last_timestamp or "").strip()
        self._last_saved_date = self._parse_saved_timestamp_to_qdate(self._last_saved_timestamp)
        if (self._last_saved_date is None or not self._last_saved_date.isValid()) and self.period_combo.currentData() == "from_last_saved":
            fallback_index = self.period_combo.findData("current_day")
            self.period_combo.setCurrentIndex(max(0, fallback_index))

    def _current_profile_metadata(self) -> dict[str, object] | None:
        if not self._profile_locked_mode:
            return None
        device = {
            "provider": "dessmonitor",
            "source": int(self.source_combo.currentData()),
            "pn": self._initial_device_key[0],
            "devcode": self._initial_device_key[1],
            "devaddr": self._initial_device_key[2],
            "sn": self._initial_device_key[3],
        }
        if not all(str(device[key]).strip() for key in ("pn", "devcode", "devaddr", "sn")):
            return None
        return device

    def _parse_saved_timestamp_to_qdate(self, timestamp: str) -> QDate | None:
        normalized = timestamp.strip()
        if not normalized:
            return None
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            date_part = normalized[:10]
            parsed_date = QDate.fromString(date_part, "yyyy-MM-dd")
            return parsed_date if parsed_date.isValid() else None
        return QDate(parsed.year, parsed.month, parsed.day)

    def _format_saved_timestamp(self, timestamp: str) -> str:
        normalized = timestamp.strip()
        if not normalized:
            return "-"
        try:
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            return normalized
        return parsed.strftime("%d.%m.%Y %H:%M:%S")

    def _refresh_settings_summary(self) -> None:
        if self._profile_locked_mode:
            return
        device_text = self.device_combo.currentText().strip() if self.device_combo.isEnabled() and self.device_combo.count() else "Not selected yet"
        platform_text = self.source_combo.currentText().strip()
        self.summary_label.setText(tr_fragment(f"Platform: {platform_text}\nDevice: {device_text}"))

    def _load_parameter_keys_for_device(self, device: DessMonitorDevice) -> None:
        auth_config = self._auth_config()
        self._spinner_update("Loading parameter keys")
        self._append_log(f"Loading available fields for {device.display_name}...")
        try:
            keys = fetch_key_parameters(auth_config, devcode=device.devcode, logger=self._append_log)
        except DessMonitorApiError as exc:
            safe_error = sanitize_error_text(str(exc))
            self._available_parameter_keys = []
            self._append_log(f"Field discovery failed for devcode {device.devcode}: {safe_error}")
            self._refresh_settings_summary()
            self._spinner_stop("Ready")
            return

        self._available_parameter_keys = keys
        self._append_log(f"Loaded {len(keys)} available field(s) for this device.")
        self._refresh_settings_summary()
        self._spinner_stop("Ready to import")

    def _on_device_changed(self, index: int) -> None:
        if index < 0 or index >= len(self._devices):
            return
        self._load_parameter_keys_for_device(self._devices[index])

    def load_devices(self) -> bool:
        missing = self._validate_auth_fields()
        if missing:
            self._show_message(
                QMessageBox.Warning,
                "Missing fields",
                f"Please fill in: {', '.join(missing)}.",
            )
            return False

        self.fetch_devices_button.setEnabled(False)
        self.fetch_devices_button.setText("Loading...")
        self.device_combo.setEnabled(False)
        self.auth_log.clear()
        self._available_parameter_keys = []
        self._refresh_settings_summary()
        self._spinner_start("Preparing authentication")
        self._append_log("Starting authentication flow...")
        if not self.company_key_input.text().strip():
            self._append_log("No company key provided. Trying optional-auth flow first.")
        auth_config = self._auth_config()
        try:
            self._spinner_update("Signing in to DessMonitor")
            devices = fetch_devices(auth_config, logger=self._append_log)
        except DessMonitorApiError as exc:
            safe_error = sanitize_error_text(str(exc))
            self._show_message(QMessageBox.Critical, "DessMonitor Error", safe_error)
            self._append_log(f"Device loading failed: {safe_error}")
            self.device_hint.setText("Could not load devices. Check credentials and company key.")
            self._spinner_stop("Load failed")
            return False
        finally:
            self.fetch_devices_button.setEnabled(True)
            self.fetch_devices_button.setText("Load Devices")

        self._devices = devices
        self._spinner_update("Devices loaded")
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for device in devices:
            self.device_combo.addItem(device.display_name)
        initial_index = 0
        for index, device in enumerate(devices):
            if (device.pn, device.devcode, device.devaddr, device.sn) == self._initial_device_key:
                initial_index = index
                break
        self.device_combo.blockSignals(False)
        if devices:
            self.device_combo.setCurrentIndex(initial_index)
            self._load_parameter_keys_for_device(devices[initial_index])
        self.device_combo.setEnabled(True)
        self.device_hint.setText(f"Loaded {len(devices)} device(s). Choose one to import.")
        self._append_log("Device discovery completed successfully.")
        return True

    def accept(self) -> None:
        missing = self._validate_auth_fields()
        if missing:
            self._show_message(
                QMessageBox.Warning,
                "Missing fields",
                f"Please fill in: {', '.join(missing)}.",
            )
            return
        if not self._devices:
            self._append_log("No device list loaded yet. Starting automatic device discovery before import...")
            if not self.load_devices() or not self._devices:
                self._show_message(
                    QMessageBox.Warning,
                    "Device required",
                    "Could not load devices for this account. Check the log and try again.",
                )
                return
        if self.period_combo.currentData() == "custom" and self.date_to_input.date() < self.date_input.date():
            self._show_message(
                QMessageBox.Warning,
                "Invalid date range",
                "The end date must be on or after the start date.",
            )
            return
        if self.period_combo.currentData() == "from_last_saved" and (
            self._last_saved_date is None or not self._last_saved_date.isValid()
        ):
            self._show_message(
                QMessageBox.Warning,
                "No saved history",
                "No saved history was found for this profile yet. Choose another sync period.",
            )
            return
        super().accept()
