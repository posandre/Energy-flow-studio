from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
import sys
import time

from PySide6.QtCore import Qt, Signal, QTimer, QEvent
from PySide6.QtGui import QBrush, QColor, QFont, QIcon, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.services.app_settings import DeviceControlFieldProfile, DeviceProfile
from app.services.dessmonitor_api import InverterSetting
from app.services.i18n import tr, tr_fragment
from app.services.logging_utils import get_logger
from app.ui.design_system import (
    apply_minimal_button,
    apply_minimal_combo,
    apply_minimal_line_edit,
    compose_styles,
    dialog_surface_qss,
)
from app.ui.dialogs import NEON_CLOSE_BUTTON_STYLE, NEON_HEADER_BAR_STYLE, create_neon_header_bar, exec_modal_dialog

LOGGER = get_logger(__name__)


class InverterSettingsTab(QWidget):
    """Inverter settings browser/editor embedded in Saved Data workspace."""
    refresh_requested = Signal()
    edit_requested = Signal(str)
    setting_cached = Signal(object)
    _CATEGORY_ORDER = (
        "Basic setting",
        "System setting",
        "Battery setting",
        "Other",
    )

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._profile: DeviceProfile | None = None
        self._settings: list[InverterSetting] = []
        self._loaded_at = ""
        self._refresh_spinner_phase = 0
        self._refresh_spinner_timer = QTimer(self)
        self._refresh_spinner_timer.setInterval(420)
        self._refresh_spinner_timer.timeout.connect(self._tick_refresh_spinner)
        self._loader_frames = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
        self._active_loading_field_id: str | None = None
        self._rows_by_field_id: dict[str, QTreeWidgetItem] = {}
        self._edit_buttons_by_field_id: dict[str, QPushButton] = {}
        self._category_toggle_buttons: dict[int, QPushButton] = {}
        self._branch_closed_icon, self._branch_open_icon = self._resolve_branch_icon_pixmaps()
        self._edit_buttons_enabled = True

        title = QLabel(tr("Inverter Settings"))
        title.setObjectName("SavedDataTitle")

        self.refresh_button = QPushButton(tr("Refresh"))
        self.refresh_button.setObjectName("EnergyFlowRefreshButton")
        self.refresh_button.clicked.connect(self.refresh_requested.emit)
        self.expand_button = QPushButton(tr("Expand all"))
        self.expand_button.setObjectName("SecondaryActionButton")
        self.expand_button.clicked.connect(self._expand_all_groups)
        self.collapse_button = QPushButton(tr("Collapse all"))
        self.collapse_button.setObjectName("SecondaryActionButton")
        self.collapse_button.clicked.connect(self._collapse_all_groups)

        header_row = QHBoxLayout()
        header_row.addWidget(title)
        header_row.addStretch()
        header_row.addWidget(self.expand_button)
        header_row.addWidget(self.collapse_button)
        header_row.addWidget(self.refresh_button)

        self.summary_label = QLabel(tr("Choose an active DessMonitor profile to load inverter settings."))
        self.summary_label.setObjectName("InverterSettingsSummaryText")
        self.summary_label.setVisible(True)
        self.summary_label.setWordWrap(True)

        self.status_label = QLabel(tr("Ready to load."))
        self.status_label.setObjectName("SavedDataStatus")

        self.search_field = QLineEdit()
        self.search_field.setObjectName("InverterSettingsSearch")
        self.search_field.setPlaceholderText(tr("Search by parameter name, value, or category"))
        self.search_field.textChanged.connect(self._refresh_tree)

        self.tree = QTreeWidget()
        self.tree.setObjectName("InverterSettingsTree")
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setItemsExpandable(True)
        self.tree.setIndentation(22)
        self.tree.setColumnCount(5)
        self.tree.setHeaderLabels(["", tr("Parameter"), tr("Value"), tr("Unit"), tr("Edit")])
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(4, QHeaderView.ResizeMode.Fixed)
        self.tree.header().resizeSection(0, 64)
        self.tree.header().resizeSection(4, 96)
        self.tree.itemExpanded.connect(self._sync_category_marker_for_item)
        self.tree.itemCollapsed.connect(self._sync_category_marker_for_item)

        content_panel = QWidget()
        content_layout = QVBoxLayout(content_panel)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(16)
        content_layout.addWidget(self.status_label)
        content_layout.addWidget(self.search_field)
        content_layout.addWidget(self.tree, stretch=1)

        section_card = QFrame()
        section_card.setObjectName("SavedDataFrame")
        section_layout = QVBoxLayout(section_card)
        section_layout.setContentsMargins(20, 20, 20, 20)
        section_layout.setSpacing(16)
        section_layout.addLayout(header_row)
        section_layout.addWidget(self.summary_label)
        section_layout.addWidget(content_panel, stretch=1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addWidget(section_card, stretch=1)

    def set_profile(self, profile: DeviceProfile | None) -> None:
        self._profile = profile
        self._settings = self._profile_stub_settings(profile)
        self._loaded_at = ""
        if profile is None:
            self.summary_label.setText(tr("Choose an active DessMonitor profile to load inverter settings."))
            self._set_status_message(tr("No active profile selected."), error=False)
        else:
            self.summary_label.setText("")
            preloaded_count = len(self._settings)
            if preloaded_count:
                self._set_status_message(
                    tr_fragment(f"Loaded {preloaded_count} saved field definition(s). Refresh to read current values."),
                    error=False,
                )
            else:
                self._set_status_message(tr("Ready to load inverter settings."), error=False)
        self._refresh_tree()

    def set_loading(self, profile: DeviceProfile | None) -> None:
        if profile is not None:
            self._profile = profile
        self._settings = self._profile_stub_settings(self._profile)
        self._loaded_at = ""
        self._active_loading_field_id = None
        self._set_refresh_loading(True)
        self._set_status_message(tr("Loading inverter settings from DessMonitor..."), error=False)
        self.summary_label.setText(
            self._build_summary_text(loaded_count=None)
        )
        self._refresh_tree()

    def add_setting(self, profile: DeviceProfile, setting: InverterSetting) -> None:
        self._profile = profile
        existing_index = next((index for index, item in enumerate(self._settings) if item.field_id == setting.field_id), -1)
        if existing_index >= 0:
            self._settings[existing_index] = setting
        else:
            self._settings.append(setting)
        self.summary_label.setText(self._build_summary_text(loaded_count=len(self._settings)))
        self._refresh_tree()

    def set_settings(self, profile: DeviceProfile, settings: list[InverterSetting], loaded_at: str) -> None:
        self._profile = profile
        self._settings = settings
        self._loaded_at = loaded_at
        self._active_loading_field_id = None
        self._set_refresh_loading(False)
        available_count = sum(1 for item in settings if item.raw_value is not None)
        self.summary_label.setText(self._build_summary_text(loaded_count=available_count))
        self._set_status_message(tr_fragment(f"Loaded {len(settings)} parameter(s)."), error=False)
        self._refresh_tree()

    def set_error(self, profile: DeviceProfile | None, message: str) -> None:
        if profile is not None:
            self._profile = profile
        self._active_loading_field_id = None
        self._set_refresh_loading(False)
        self._set_status_message(message, error=True)
        self.summary_label.setText(self._build_summary_text(loaded_count=None))
        self._refresh_tree()

    def _set_refresh_loading(self, loading: bool) -> None:
        if loading:
            self._refresh_spinner_phase = 0
            self.refresh_button.setEnabled(False)
            self.refresh_button.setText(f"{tr('Refreshing')} ⠋")
            self._refresh_spinner_timer.start()
            return
        self._refresh_spinner_timer.stop()
        self._refresh_spinner_phase = 0
        self.refresh_button.setEnabled(True)
        self.refresh_button.setText(tr("Refresh"))
        self._update_row_loader_state(scroll_to_active=False)

    def _tick_refresh_spinner(self) -> None:
        self._refresh_spinner_phase = (self._refresh_spinner_phase + 1) % len(self._loader_frames)
        self.refresh_button.setText(f"{tr('Refreshing')} {self._loader_frames[self._refresh_spinner_phase]}")
        self._update_row_loader_state(scroll_to_active=False)

    def append_progress(self, message: str) -> None:
        self._set_status_message(message, error=False)

    def has_loaded_settings_for(self, profile: DeviceProfile | None) -> bool:
        if profile is None or self._profile is None:
            return False
        return (
            self._profile.profile_name == profile.profile_name
            and self._profile.pn == profile.pn
            and self._profile.devcode == profile.devcode
            and self._profile.devaddr == profile.devaddr
            and bool(self._settings)
        )

    def _build_summary_text(self, loaded_count: int | None) -> str:
        profile = self._profile
        if profile is None:
            return tr("Choose an active DessMonitor profile to load inverter settings.")

        lines: list[str] = []
        if loaded_count is not None:
            lines.append(tr_fragment(f"Settings loaded: {loaded_count}"))
        if self._loaded_at:
            lines.append(tr_fragment(f"Updated: {self._loaded_at}"))
        return "\n".join(lines)

    def _refresh_tree(self) -> None:
        self.tree.clear()
        self._rows_by_field_id.clear()
        self._edit_buttons_by_field_id.clear()
        self._category_toggle_buttons.clear()
        profile_fields_by_id = self._profile_fields_by_id()
        filter_text = self.search_field.text().strip().lower()
        grouped: OrderedDict[str, list[InverterSetting]] = OrderedDict()
        for setting in self._settings:
            parameter_label = self._parameter_label(setting, profile_fields_by_id)
            category_label = self._normalized_category_label(setting, profile_fields_by_id)
            haystack = " ".join(
                [
                    parameter_label,
                    setting.display_name,
                    setting.display_value,
                    setting.unit,
                    category_label,
                    setting.field_id,
                ]
            ).lower()
            if filter_text and filter_text not in haystack:
                continue
            grouped.setdefault(category_label, []).append(setting)

        if not grouped:
            placeholder = QTreeWidgetItem([tr("No inverter settings loaded yet.")])
            placeholder.setFlags(Qt.ItemFlag.NoItemFlags)
            self.tree.addTopLevelItem(placeholder)
            return

        sorted_categories = sorted(
            grouped.items(),
            key=lambda item: (
                self._category_sort_index(item[0]),
                item[0].lower(),
            ),
        )

        for category, items in sorted_categories:
            sorted_items = sorted(
                items,
                key=lambda setting: self._setting_sort_key(setting, profile_fields_by_id),
            )
            category_item = QTreeWidgetItem(["", "", "", "", ""])
            category_item.setFirstColumnSpanned(False)
            category_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            category_item.setData(0, Qt.ItemDataRole.UserRole, "category")
            category_font = QFont(self.tree.font())
            category_font.setBold(True)
            category_font.setPointSize(max(category_font.pointSize(), 12))
            for col in range(self.tree.columnCount()):
                category_item.setBackground(col, QBrush(QColor(8, 16, 34, 220)))
                category_item.setForeground(col, QBrush(QColor("#dbeafe")))
            category_item.setFont(1, category_font)
            self.tree.addTopLevelItem(category_item)
            self.tree.setItemWidget(
                category_item,
                1,
                self._create_category_header_cell(category_item, category, len(sorted_items)),
            )

            for setting in sorted_items:
                row = QTreeWidgetItem(
                    [
                        "",
                        self._parameter_label(setting, profile_fields_by_id),
                        tr_fragment(setting.display_value),
                        tr_fragment(setting.unit) if setting.unit else "-",
                    ]
                )
                row.setTextAlignment(0, Qt.AlignmentFlag.AlignCenter)
                row.setData(0, Qt.ItemDataRole.UserRole, setting)
                if setting.raw_value is None:
                    row.setForeground(2, self.palette().brush(self.foregroundRole()))
                self._rows_by_field_id[setting.field_id] = row
                category_item.addChild(row)
                self.tree.setItemWidget(
                    row,
                    4,
                    self._create_edit_button_cell(
                        setting.field_id,
                        enabled=self._edit_buttons_enabled and setting.raw_value is not None,
                    ),
                )

            category_item.setExpanded(True)
            self._sync_category_marker_for_item(category_item)
        self._update_row_loader_state(scroll_to_active=True)

    def _category_sort_index(self, category: str) -> int:
        lowered = category.strip().lower()
        for index, expected in enumerate(self._CATEGORY_ORDER):
            if lowered == expected.lower():
                return index
        return len(self._CATEGORY_ORDER)

    def _normalized_category_label(
        self,
        setting: InverterSetting,
        profile_fields_by_id: dict[str, DeviceControlFieldProfile],
    ) -> str:
        profile_field = profile_fields_by_id.get(setting.field_id)
        category_source = (profile_field.category.strip() if profile_field is not None else setting.category.strip()).lower()
        field_name = (
            (profile_field.name.strip() if profile_field is not None else "")
            or setting.name.strip()
            or setting.raw_name.strip()
            or setting.display_name.strip()
        ).lower()
        haystack = f"{field_name} {setting.field_id.lower()} {category_source}"

        basic_name_patterns = (
            "output mode",
            "output priority",
            "input voltage range",
            "output voltage",
            "output frequency",
        )
        if any(pattern in haystack for pattern in basic_name_patterns):
            return "Basic setting"

        if (
            "battery" in haystack
            or "soc" in haystack
            or "charging" in haystack
            or "discharge" in haystack
            or "charger" in haystack
            or "charge" in haystack
            or "eq " in haystack
            or "floating voltage" in haystack
            or "bulk charging voltage" in haystack
            or "dc protection" in haystack
            or category_source == "battery"
        ):
            return "Battery setting"

        if category_source in {"basic", "basic setting"}:
            return "Basic setting"
        if category_source in {"system", "system setting", "grid", "pv", "time-of-use", "protection", "charging"}:
            return "System setting"
        if category_source in {"other", ""}:
            return "System setting"
        return "System setting"

    def _setting_sort_key(
        self,
        setting: InverterSetting,
        profile_fields_by_id: dict[str, DeviceControlFieldProfile],
    ) -> tuple[int, str, str]:
        profile_field = profile_fields_by_id.get(setting.field_id)
        raw_prog = (profile_field.prog_number.strip() if profile_field is not None else "").lower()
        if raw_prog and raw_prog != "xx":
            try:
                prog_sort = int(raw_prog)
            except ValueError:
                prog_sort = 999999
        else:
            prog_sort = 999999
        parameter_name = self._parameter_label(setting, profile_fields_by_id)
        return (
            prog_sort,
            parameter_name.lower(),
            setting.field_id.lower(),
        )

    def _sync_category_marker_for_item(self, item: QTreeWidgetItem) -> None:
        if item.data(0, Qt.ItemDataRole.UserRole) != "category":
            return
        button = self._category_toggle_buttons.get(id(item))
        if button is None:
            return
        if item.isExpanded():
            if not self._branch_open_icon.isNull():
                button.setIcon(QIcon(self._branch_open_icon))
                button.setText("")
            else:
                button.setIcon(QIcon())
                button.setText("▾")
        else:
            if not self._branch_closed_icon.isNull():
                button.setIcon(QIcon(self._branch_closed_icon))
                button.setText("")
            else:
                button.setIcon(QIcon())
                button.setText("▸")

    def _create_category_header_cell(self, item: QTreeWidgetItem, category: str, count: int) -> QWidget:
        wrapper = QWidget()
        wrapper.setObjectName("CategoryHeaderCell")
        layout = QHBoxLayout(wrapper)
        layout.setContentsMargins(6, 0, 6, 0)
        layout.setSpacing(8)

        toggle = QPushButton()
        toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        toggle.setFlat(True)
        toggle.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        toggle.setFixedSize(16, 16)
        toggle.setIconSize(toggle.size())
        toggle.setStyleSheet(
            "QPushButton {"
            "background: transparent;"
            "border: none;"
            "color: #22d3ee;"
            "font-size: 13px;"
            "font-weight: 700;"
            "padding: 0;"
            "}"
        )
        toggle.clicked.connect(lambda _checked=False, target=item: target.setExpanded(not target.isExpanded()))
        self._category_toggle_buttons[id(item)] = toggle
        layout.addWidget(toggle)

        name = QLabel(tr(category))
        name.setObjectName("CategoryHeaderLabel")
        name.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Preferred)
        layout.addWidget(name)

        badge = QLabel(str(count))
        badge.setObjectName("CategoryHeaderBadge")
        layout.addWidget(badge)
        layout.addStretch(1)
        return wrapper

    def _resolve_branch_icon_pixmaps(self) -> tuple[QPixmap, QPixmap]:
        source_assets_root = Path(__file__).resolve().parents[1] / "assets" / "icons"
        bundled_base = getattr(sys, "_MEIPASS", None)
        bundled_assets_root = Path(bundled_base) / "app" / "assets" / "icons" if bundled_base else None
        assets_root = (
            bundled_assets_root
            if bundled_assets_root is not None and bundled_assets_root.exists()
            else source_assets_root
        )
        closed_icon = QPixmap(str(assets_root / "tree_branch_closed.png"))
        open_icon = QPixmap(str(assets_root / "tree_branch_open.png"))
        return closed_icon, open_icon

    def _expand_all_groups(self) -> None:
        self.tree.expandAll()

    def _collapse_all_groups(self) -> None:
        root = self.tree.invisibleRootItem()
        for index in range(root.childCount()):
            root.child(index).setExpanded(False)

    def _allowed_values_text(self, setting: InverterSetting) -> str:
        if not setting.options:
            return ""
        return "\n".join(f"{value} -> {label}" for value, label in setting.options)

    def _create_edit_button_cell(self, field_id: str, *, enabled: bool) -> QWidget:
        wrapper = QWidget()
        wrapper_layout = QHBoxLayout(wrapper)
        wrapper_layout.setContentsMargins(3, 3, 3, 3)
        wrapper_layout.setSpacing(0)

        button = QPushButton(tr("Edit"))
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setStyleSheet(
            "QPushButton {"
            "padding: 4px 10px;"
            "border: 1px solid rgba(148, 163, 184, 0.45);"
            "border-radius: 8px;"
            "background: transparent;"
            "color: #dbe6ff;"
            "font-size: 12px;"
            "font-weight: 500;"
            "}"
            "QPushButton:hover {"
            "border-color: rgba(125, 211, 252, 0.75);"
            "background: rgba(56, 189, 248, 0.09);"
            "}"
            "QPushButton:pressed {"
            "background: rgba(56, 189, 248, 0.16);"
            "}"
            "QPushButton:disabled {"
            "color: #7f93ab;"
            "border-color: rgba(71, 85, 105, 0.5);"
            "background: rgba(30, 41, 59, 0.35);"
            "}"
        )
        button.setEnabled(enabled)
        button.clicked.connect(lambda _checked=False, fid=field_id: self.edit_requested.emit(fid))
        self._edit_buttons_by_field_id[field_id] = button
        wrapper_layout.addWidget(button)
        return wrapper

    def _profile_stub_settings(self, profile: DeviceProfile | None) -> list[InverterSetting]:
        if profile is None:
            return []
        stubs: list[InverterSetting] = []
        for field in profile.resolved_inverter_control_fields():
            stubs.append(self._stub_setting(field))
        return stubs

    def _stub_setting(self, field: DeviceControlFieldProfile) -> InverterSetting:
        return InverterSetting(
            field_id=field.field_id,
            name=field.name,
            display_name=field.display_name(),
            raw_value=None,
            display_value=tr("Pending load"),
            unit=field.unit or "",
            category=field.category or "System",
            writable=field.writable,
            options=tuple(field.options or ()),
            hint="",
            raw_name=field.name,
        )

    def _field_comment(self, field_id: str) -> str:
        profile = self._profile
        if profile is None:
            return ""
        for field in profile.resolved_inverter_control_fields():
            if field.field_id == field_id:
                return field.comment.strip()
        return ""

    def _profile_fields_by_id(self) -> dict[str, DeviceControlFieldProfile]:
        profile = self._profile
        if profile is None:
            return {}
        return {
            field.field_id: field
            for field in profile.resolved_inverter_control_fields()
            if field.field_id
        }

    def _parameter_label(
        self,
        setting: InverterSetting,
        profile_fields_by_id: dict[str, DeviceControlFieldProfile],
    ) -> str:
        profile_field = profile_fields_by_id.get(setting.field_id)
        prog_number = (profile_field.prog_number.strip() if profile_field is not None else "")
        parameter_name = (
            (profile_field.name.strip() if profile_field is not None else "")
            or setting.name.strip()
            or setting.raw_name.strip()
            or setting.display_name.strip()
            or setting.field_id
        )
        if prog_number and prog_number.lower() != "xx":
            return f"(#{prog_number})\t{tr_fragment(parameter_name)}".strip()
        return tr_fragment(parameter_name)

    def set_active_loading_field(self, field_id: str) -> None:
        self._active_loading_field_id = field_id
        self._update_row_loader_state(scroll_to_active=True)

    def setting_by_field_id(self, field_id: str) -> InverterSetting | None:
        return next((item for item in self._settings if item.field_id == field_id), None)

    def set_edit_buttons_enabled(self, enabled: bool) -> None:
        self._edit_buttons_enabled = enabled
        for field_id, button in self._edit_buttons_by_field_id.items():
            setting = self.setting_by_field_id(field_id)
            button.setEnabled(enabled and setting is not None and setting.raw_value is not None)

    def open_edit_dialog(self, setting: InverterSetting) -> None:
        dialog_parent = self.window() if isinstance(self.window(), QWidget) else self
        dialog = InverterSettingEditDialog(setting, dialog_parent)
        result_code = int(QDialog.DialogCode.Rejected)
        owner = self.window()
        run_blocking = getattr(owner, "_run_popup_dialog_blocking", None)
        if callable(run_blocking):
            result_code = int(run_blocking(dialog))
        else:
            result_code = exec_modal_dialog(dialog, frameless=True, application_modal=False)
        if result_code != int(QDialog.DialogCode.Accepted):
            return
        raw_value = dialog.selected_raw_value()
        updated = InverterSetting(
            field_id=setting.field_id,
            name=setting.name,
            display_name=setting.display_name,
            raw_value=raw_value,
            display_value=self._format_setting_display(raw_value, setting.options),
            unit=setting.unit,
            category=setting.category,
            writable=setting.writable,
            options=setting.options,
            hint=setting.hint,
            raw_name=setting.raw_name,
        )
        if self._profile is not None:
            self.add_setting(self._profile, updated)
        self.setting_cached.emit(updated)

    @staticmethod
    def _format_setting_display(raw_value: str, options: tuple[tuple[str, str], ...]) -> str:
        normalized = str(raw_value).strip()
        for value, label in options:
            if str(value).strip() == normalized:
                return label
        return tr_fragment(normalized) if normalized else tr("Not available")

    def _update_row_loader_state(self, *, scroll_to_active: bool) -> None:
        active_id = self._active_loading_field_id
        active_row = self._rows_by_field_id.get(active_id or "")
        frame = self._loader_frames[self._refresh_spinner_phase % len(self._loader_frames)]
        blink_on = (self._refresh_spinner_phase % 2) == 0
        active_text = QBrush(QColor(34, 197, 94))
        active_loader_text = QBrush(QColor("#22d3ee"))
        default_text = self.palette().brush(self.foregroundRole())

        for field_id, row in self._rows_by_field_id.items():
            is_active = bool(active_id) and field_id == active_id and self._refresh_spinner_timer.isActive()
            row.setText(0, frame if is_active else "")
            row.setForeground(0, active_loader_text if is_active else default_text)
            for col in range(1, self.tree.columnCount()):
                row.setForeground(col, active_text if (is_active and blink_on) else default_text)

        if scroll_to_active and active_row is not None:
            self.tree.scrollToItem(active_row, QTreeWidget.ScrollHint.PositionAtCenter)

    def _set_status_message(self, message: str, *, error: bool) -> None:
        self.status_label.setText(message)
        if error:
            self.status_label.setStyleSheet(
                "color: #fecaca; background: rgba(127, 29, 29, 0.35); "
                "border: 1px solid #ef4444; border-radius: 10px; padding: 8px;"
            )
            return
        self.status_label.setStyleSheet("")


class InverterSettingEditDialog(QDialog):
    """Frameless editor dialog for a single inverter control field value."""
    @staticmethod
    def _normalized_option_token(value: str | int | float | None) -> str:
        text = str(value).strip().casefold() if value is not None else ""
        if not text:
            return ""
        try:
            numeric = float(text)
        except (TypeError, ValueError):
            return text
        if numeric.is_integer():
            return str(int(numeric))
        return format(numeric, "g")

    def __init__(self, setting: InverterSetting, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._setting = setting
        self._selected_raw_value = str(setting.raw_value).strip() if setting.raw_value is not None else ""
        self.setObjectName("InverterEditDialog")
        self.setWindowTitle(tr("Edit parameter"))
        # This editor should behave as strict modal popup inside popup manager.
        self.setProperty("popup_force_window_modal", True)
        self.setProperty("popup_disable_focus_guard", True)
        self.setProperty("popup_disable_focus_recovery", True)
        self.setModal(True)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.resize(620, 360)
        self._shown_monotonic: float | None = None

        title = QLabel(tr_fragment(setting.display_name))
        title.setObjectName("DialogFieldLabel")
        title.setWordWrap(True)

        input_row = QHBoxLayout()
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(10)

        self._combo: QComboBox | None = None
        self._line_edit: QLineEdit | None = None
        if setting.options:
            combo = QComboBox()
            combo.setObjectName("EditValueInput")
            apply_minimal_combo(combo)
            for value, label in setting.options:
                combo.addItem(label, value)
            raw_text = str(setting.raw_value).strip() if setting.raw_value is not None else ""
            display_text = str(setting.display_value).strip() if setting.display_value is not None else ""
            raw_token = self._normalized_option_token(raw_text)
            display_token = self._normalized_option_token(display_text)
            selected_index = -1
            for idx, (value, label) in enumerate(setting.options):
                value_token = self._normalized_option_token(value)
                label_token = self._normalized_option_token(label)
                if raw_token and raw_token in {value_token, label_token}:
                    selected_index = idx
                    break
                if display_token and display_token in {value_token, label_token}:
                    selected_index = idx
                    break
            if selected_index >= 0:
                combo.setCurrentIndex(selected_index)
            self._combo = combo
            input_row.addWidget(combo, stretch=1)
        else:
            line_edit = QLineEdit(str(setting.raw_value).strip() if setting.raw_value is not None else "")
            line_edit.setObjectName("EditValueInput")
            line_edit.setPlaceholderText(tr("Enter value"))
            apply_minimal_line_edit(line_edit)
            self._line_edit = line_edit
            input_row.addWidget(line_edit, stretch=1)

        unit_label = QLabel(tr_fragment(setting.unit.strip()))
        unit_label.setObjectName("InverterSettingsSummaryText")
        input_row.addWidget(unit_label)

        hint_label = QLabel(setting.hint.strip())
        hint_label.setObjectName("InverterSettingsSummaryText")
        hint_label.setWordWrap(True)

        self.result_label = QLabel(tr("Saving is temporarily disabled at this stage."))
        self.result_label.setObjectName("InverterSettingsSummaryText")
        self.result_label.setWordWrap(True)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.setContentsMargins(0, 8, 0, 0)
        self.save_button = buttons.addButton(tr("Save"), QDialogButtonBox.ButtonRole.AcceptRole)
        self.save_button.setObjectName("PrimaryActionButton")
        apply_minimal_button(self.save_button)
        cancel_button = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel_button is not None:
            cancel_button.setObjectName("SecondaryActionButton")
            apply_minimal_button(cancel_button)
        buttons.accepted.connect(self._accept_save)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 26, 32, 28)
        layout.setSpacing(18)
        layout.addWidget(
            create_neon_header_bar(
                self,
                self.reject,
                title=tr("Edit: {display_name}").format(display_name=tr_fragment(setting.display_name)),
            )
        )
        layout.addSpacing(6)
        layout.addWidget(title)
        layout.addSpacing(6)
        layout.addLayout(input_row)
        if setting.hint.strip():
            layout.addSpacing(4)
            layout.addWidget(hint_label)
        layout.addSpacing(10)
        layout.addWidget(self.result_label)
        layout.addSpacing(12)
        layout.addStretch(1)
        layout.addWidget(buttons)

        self.setStyleSheet(
            compose_styles(
                dialog_surface_qss("#InverterEditDialog"),
                INVERTER_EDIT_DIALOG_STYLE,
                NEON_CLOSE_BUTTON_STYLE,
                NEON_HEADER_BAR_STYLE,
            )
        )
        self.result_label.setText(tr("Value will be saved to the local cache."))

    def _accept_save(self) -> None:
        """Capture selected raw value from either combo or text input."""
        if self._combo is not None:
            data_value = self._combo.currentData()
            self._selected_raw_value = str(data_value if data_value is not None else self._combo.currentText()).strip()
        elif self._line_edit is not None:
            self._selected_raw_value = self._line_edit.text().strip()
        else:
            self._selected_raw_value = ""
        self.accept()

    def selected_raw_value(self) -> str:
        return self._selected_raw_value

    def showEvent(self, event) -> None:  # noqa: N802
        """Sync backdrop/focus when dialog becomes visible."""
        super().showEvent(event)
        try:
            self._shown_monotonic = time.monotonic()
            self._diag("show_event", **self._focus_snapshot())
            owner = self._popup_owner()
            if owner is not None and hasattr(owner, "_sync_popup_backdrop_state"):
                owner._sync_popup_backdrop_state()
            self._enforce_dialog_focus()
            self._diag("show_event_post_enforce", **self._focus_snapshot())
        except Exception:
            LOGGER.exception("InverterSettingEditDialog showEvent handling failed")

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        try:
            self._diag("hide_event", **self._focus_snapshot())
            owner = self._popup_owner()
            if owner is not None and hasattr(owner, "_sync_popup_backdrop_state"):
                owner._sync_popup_backdrop_state()
        except Exception:
            LOGGER.exception("InverterSettingEditDialog hideEvent handling failed")

    def event(self, event):  # noqa: N802
        try:
            if event is not None and event.type() in {
                QEvent.Type.WindowActivate,
                QEvent.Type.WindowDeactivate,
                QEvent.Type.FocusIn,
                QEvent.Type.FocusOut,
                QEvent.Type.Move,
                QEvent.Type.Resize,
            }:
                elapsed_ms = -1
                if self._shown_monotonic is not None:
                    elapsed_ms = int((time.monotonic() - self._shown_monotonic) * 1000)
                self._diag(
                    "qt_event",
                    qt_type=int(event.type()),
                    spontaneous=bool(event.spontaneous()),
                    elapsed_ms=elapsed_ms,
                    geometry=f"{self.x()},{self.y()},{self.width()}x{self.height()}",
                    modality=str(self.windowModality()),
                    **self._focus_snapshot(),
                )
        except Exception:
            LOGGER.exception("InverterSettingEditDialog event tracing failed")
        return super().event(event)

    def _focus_target(self) -> QWidget:
        if self._combo is not None:
            return self._combo
        if self._line_edit is not None:
            return self._line_edit
        return self

    def _enforce_dialog_focus(self) -> None:
        """Schedule immediate focus enforcement after show/activation."""
        self._diag("focus_enforce_scheduled", **self._focus_snapshot())
        QTimer.singleShot(0, lambda: self._apply_focus_now("t0"))

    def _apply_focus_now(self, stage: str) -> None:
        self._diag("focus_apply_enter", stage=stage, **self._focus_snapshot())
        if not self.isVisible():
            self._diag("focus_apply_skip_not_visible", stage=stage)
            return
        dialog_window = self.windowHandle()
        if dialog_window is not None:
            dialog_window.requestActivate()
        QApplication.setActiveWindow(self)
        self.raise_()
        self.activateWindow()

        target = self._focus_target()
        if target.hasFocus():
            self._diag("focus_apply_skip_target_focused", stage=stage, **self._focus_snapshot())
            return
        target.setFocus(Qt.FocusReason.ActiveWindowFocusReason)
        self._diag("focus_apply_exit", stage=stage, **self._focus_snapshot())

    def _popup_owner(self) -> QWidget | None:
        owner: QWidget | None = self.parentWidget()
        while owner is not None:
            if hasattr(owner, "_popup_modal_diag"):
                return owner
            owner = owner.parentWidget()
        return None

    @staticmethod
    def _describe_widget(widget: QWidget | None) -> str:
        if widget is None:
            return ""
        return f"{widget.__class__.__name__}:{widget.objectName()}"

    def _focus_snapshot(self) -> dict[str, object]:
        app = QApplication.instance()
        active_window = app.activeWindow() if app is not None else None
        focus_widget = app.focusWidget() if app is not None else None
        focus_window = app.focusWindow() if app is not None else None
        target = self._focus_target()
        return {
            "dialog_active": self.isActiveWindow(),
            "dialog_has_focus": self.hasFocus(),
            "active_window": self._describe_widget(active_window),
            "focus_widget": self._describe_widget(focus_widget),
            "focus_window_title": focus_window.title() if focus_window is not None else "",
            "target_widget": self._describe_widget(target),
            "target_has_focus": target.hasFocus(),
        }

    def _diag(self, event: str, **details: object) -> None:
        owner = self._popup_owner()
        if owner is None or not hasattr(owner, "_popup_modal_diag"):
            return
        owner._popup_modal_diag(
            f"inverter_edit:{event}",
            dialog=f"{self.__class__.__name__}:{self.objectName()}",
            **details,
        )
INVERTER_EDIT_DIALOG_STYLE = """
#InverterEditDialog QLabel {
    color: #dbeafe;
}
#InverterEditDialog QLabel#DialogFieldLabel {
    font-size: 14px;
    font-weight: 600;
    color: #cbd5e1;
}
#InverterEditDialog QLabel#InverterSettingsSummaryText {
    font-size: 14px;
}
"""
