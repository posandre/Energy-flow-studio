from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QComboBox, QLineEdit, QPushButton, QTabWidget

_MINIMAL_BUTTON_QSS = """
QPushButton {
    min-height: 28px;
    border-radius: 10px;
    font-weight: 600;
    padding: 4px 10px;
    background: #1e293b;
    color: #e2e8f0;
    border: 1px solid #475569;
}
QPushButton:hover {
    background: #334155;
}
QPushButton:disabled {
    background: #172033;
    color: #94a3b8;
    border: 1px solid #334155;
}
"""

_MINIMAL_LINE_EDIT_QSS = """
QLineEdit {
    min-height: 36px;
    padding: 0 10px;
    font-size: 14px;
    color: #e2e8f0;
    background: rgba(8, 15, 30, 0.78);
    border: 2px solid #00c2ff;
    border-radius: 18px;
}
QLineEdit:focus {
    border-color: #38bdf8;
}
"""

_MINIMAL_COMBO_QSS = """
QComboBox {
    min-height: 36px;
    padding: 0 30px 0 10px;
    font-size: 14px;
    color: #e2e8f0;
    background: rgba(8, 15, 30, 0.78);
    border: 2px solid #00c2ff;
    border-radius: 18px;
    combobox-popup: 0;
}
QComboBox:hover,
QComboBox:focus {
    border-color: #38bdf8;
    background: rgba(12, 22, 44, 0.9);
}
QComboBox::drop-down {
    width: 30px;
    border: none;
    background: transparent;
}
QComboBox::down-arrow {
    image: none;
    width: 9px;
    height: 9px;
    border-right: 2px solid #cbd5e1;
    border-bottom: 2px solid #cbd5e1;
    margin-right: 10px;
}
QComboBox QAbstractItemView {
    background: #111827;
    color: #e2e8f0;
    border: 1px solid #334155;
    selection-background-color: #0ea5e9;
    selection-color: #eff6ff;
    outline: 0;
}
"""

GLOBAL_CHECKBOX_QSS = """
QCheckBox {
    color: #e2e8f0;
    spacing: 8px;
    background: transparent;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border-radius: 4px;
    border: 1px solid #22d3ee;
    background: rgba(8, 20, 40, 0.96);
}
QCheckBox::indicator:hover {
    border-color: #38bdf8;
    background: rgba(12, 28, 54, 0.96);
}
QCheckBox::indicator:checked {
    border-color: #22d3ee;
    background: #0ea5e9;
}
QCheckBox:disabled {
    color: #7f93ab;
}
QCheckBox::indicator:disabled {
    border-color: #35506d;
    background: rgba(15, 23, 42, 0.7);
}
"""

GLOBAL_TOOLTIP_QSS = """
QToolTip {
    background: #0b1220;
    color: #dbeafe;
    border: 1px solid #3b4d63;
    border-radius: 8px;
    padding: 8px 10px;
    font-size: 12px;
}
"""

GLOBAL_BUTTON_QSS = """
QPushButton#PrimaryActionButton,
QPushButton#SecondaryButton,
QPushButton#SecondaryActionButton {
    border-radius: 12px;
    padding: 6px 12px;
    font-weight: 600;
    min-height: 28px;
    min-width: 84px;
}
QPushButton#PrimaryButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #0ea5e9, stop:1 #22c55e);
    color: #eff6ff;
    border: 1px solid #67e8f9;
    border-radius: 12px;
    padding: 8px 14px;
    font-weight: 600;
}
QPushButton#PrimaryButton:hover {
    border-color: #38bdf8;
}
QPushButton#PrimaryButton:pressed {
    background: #0f766e;
}
QPushButton#SecondaryButton,
QPushButton#SecondaryActionButton {
    background: #1e293b;
    color: #e2e8f0;
    border: 1px solid #475569;
}
QPushButton#SecondaryButton:hover,
QPushButton#SecondaryActionButton:hover {
    border-color: #38bdf8;
    background: #334155;
}
QPushButton#DangerButton {
    background: #dc2626;
    color: #eff6ff;
    border: 1px solid #f87171;
}
QPushButton#DangerButton:hover {
    background: #ef4444;
}
QPushButton#EnergyFlowRefreshButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #166534, stop:1 #22c55e);
    color: #effaf3;
    border: 1px solid #4ade80;
    border-radius: 12px;
    padding: 2px 12px;
    min-width: 108px;
    min-height: 30px;
    font-weight: 700;
}
QPushButton#EnergyFlowRefreshButton:hover {
    border-color: #38bdf8;
}
QPushButton#EnergyFlowRefreshButton:disabled {
    color: #cbd5e1;
    background: #334155;
    border-color: #64748b;
}
QPushButton#ChartPrimaryActionButton {
    min-width: 38px;
    max-width: 38px;
    min-height: 38px;
    max-height: 38px;
    padding: 0;
    color: #eff6ff;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #0284c7, stop:1 #0ea5e9);
    border: 1px solid #67e8f9;
    border-radius: 12px;
    font-weight: 700;
}
QPushButton#ChartActionButton {
    min-width: 38px;
    max-width: 38px;
    min-height: 38px;
    max-height: 38px;
    padding: 0;
    color: #e2e8f0;
    background: #162033;
    border: 1px solid #334155;
    border-radius: 12px;
    font-weight: 600;
}
QPushButton#ChartPrimaryActionButton:hover,
QPushButton#ChartActionButton:hover {
    border-color: #38bdf8;
}
QPushButton#ChartPrimaryActionButton:disabled,
QPushButton#ChartActionButton:disabled {
    color: #64748b;
    background: #111827;
    border-color: #1e293b;
}
QPushButton#TuyaFilterButton {
    min-height: 34px;
    min-width: 94px;
    border-radius: 12px;
    border: 1px solid #224163;
    background: rgba(7, 20, 42, 0.92);
    color: #c6d9ea;
    font-size: 13px;
    font-weight: 600;
    padding: 0 14px;
}
QPushButton#TuyaFilterButton:hover {
    border-color: #2f6b9a;
    background: rgba(9, 26, 52, 0.96);
    color: #e5f3ff;
}
QPushButton#TuyaFilterButton:checked {
    border-color: #22d3ee;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #0f5f91, stop:1 #1696c9);
    color: #f3fbff;
}
QPushButton#TuyaFilterButton:pressed {
    border-color: #67e8f9;
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #0b4f79, stop:1 #127ca5);
}
QPushButton#TuyaFilterButton:disabled {
    color: #6e859d;
    border-color: #20344a;
    background: rgba(7, 16, 30, 0.85);
}
QPushButton#DeviceProfileLink {
    color: #67e8f9;
    font-size: 12px;
    font-weight: 600;
    text-align: left;
    padding: 2px 0;
    min-height: 22px;
    border: none;
    background: transparent;
}
QPushButton#DeviceProfileLink:hover {
    color: #a5f3fc;
    text-decoration: underline;
}
QPushButton#DeviceProfileLink:pressed {
    color: #cffafe;
}
QPushButton#PeriodLabelButton {
    background: transparent;
    border: 1px solid rgba(56, 189, 248, 0.14);
    border-radius: 10px;
    color: #bff6ff;
    font-size: 13px;
    font-weight: 500;
    padding: 8px 12px;
    text-align: center;
    min-height: 30px;
}
QPushButton#PeriodLabelButton:hover {
    border-color: rgba(56, 189, 248, 0.42);
    background: rgba(14, 165, 233, 0.08);
}
QPushButton#PeriodLabelButton:disabled {
    border-color: rgba(71, 85, 105, 0.35);
    color: #94a3b8;
    background: rgba(15, 23, 42, 0.42);
}
QPushButton#VerifyButton {
    min-width: 132px;
    background: #0f172a;
    color: #dbeafe;
    border: 1px solid #2563eb;
    padding: 5px 14px;
}
QPushButton#VerifyButton:hover {
    background: #13203a;
    border: 1px solid #38bdf8;
}
QPushButton#VerifyButton[state="pending"] {
    background: #13203a;
    color: #e0f2fe;
    border: 1px solid #38bdf8;
}
QPushButton#VerifyButton[state="success"] {
    background: rgba(8, 145, 178, 0.16);
    color: #ccfbf1;
    border: 1px solid #14b8a6;
}
QPushButton#VerifyButton[state="error"] {
    background: rgba(180, 83, 9, 0.16);
    color: #fef3c7;
    border: 1px solid #f59e0b;
}
QPushButton#GhostButton {
    min-width: 0;
    min-height: 28px;
    border-radius: 8px;
    padding: 4px 10px;
    background: #0f172a;
    color: #cbd5e1;
    border: 1px solid #334155;
}
QPushButton#GhostButton:hover {
    background: #13203a;
    border: 1px solid #38bdf8;
}
QPushButton#PrimaryActionButton {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #0ea5e9, stop:1 #10b981);
    color: #f8fafc;
    border: 1px solid #22d3ee;
}
QPushButton#PrimaryActionButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #38bdf8, stop:1 #34d399);
}
QPushButton#PrimaryActionButton:disabled {
    color: #cbd5e1;
    background: #172033;
    border-color: #334155;
}
QPushButton#MinimalDialogButton {
    background: #1e293b;
    color: #e2e8f0;
    border: 1px solid #475569;
    border-radius: 10px;
    padding: 6px 12px;
    font-size: 14px;
    font-weight: 600;
    min-width: 132px;
    min-height: 32px;
}
QPushButton#MinimalDialogButton:hover {
    background: #24334a;
}
QPushButton#MinimalDialogButton:disabled {
    color: #cbd5e1;
    background: #172033;
    border-color: #334155;
}
"""

_MINIMAL_DIALOG_SURFACE = """
background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
    stop:0 #09111f, stop:0.55 #0f172a, stop:1 #11233b);
border: none;
border-radius: 0;
"""


def apply_minimal_button(button: QPushButton) -> QPushButton:
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    return button


def apply_minimal_line_edit(line_edit: QLineEdit) -> QLineEdit:
    line_edit.setStyleSheet(_MINIMAL_LINE_EDIT_QSS)
    return line_edit


def apply_minimal_combo(combo: QComboBox) -> QComboBox:
    combo.setCursor(Qt.CursorShape.PointingHandCursor)
    combo.setStyleSheet(_MINIMAL_COMBO_QSS)
    return combo


def apply_minimal_checkbox(checkbox: QCheckBox) -> QCheckBox:
    checkbox.setCursor(Qt.CursorShape.PointingHandCursor)
    return checkbox


def apply_tab_widget_interaction(tab_widget: QTabWidget) -> QTabWidget:
    """Apply unified pointer interaction for tab widgets."""
    tab_widget.tabBar().setCursor(Qt.CursorShape.PointingHandCursor)
    return tab_widget


def tab_widget_qss(
    selector: str,
    *,
    pane_background: str = "rgba(10, 18, 32, 0.90)",
    pane_border: str = "1px solid #23344d",
    pane_radius: int = 0,
    pane_margin_top: int = 0,
    tab_background: str = "#101a2d",
    tab_color: str = "#9fb7d1",
    tab_border: str = "1px solid #23344d",
    tab_padding: str = "8px 16px",
    tab_margin_right: int = 6,
    tab_radius: int = 0,
    tab_min_width: int = 0,
    tab_selected_background: str = "#13213b",
    tab_selected_color: str = "#f8fafc",
    tab_hover_background: str = "#13233c",
    tab_hover_color: str = "#dbeafe",
    tab_border_bottom: str | None = None,
    use_tabbar_id: str | None = None,
) -> str:
    """Build unified QSS for a tab widget, with optional tab-bar selector override."""
    tab_selector = use_tabbar_id or f"{selector} QTabBar"
    min_width_rule = f"min-width: {tab_min_width}px;" if tab_min_width > 0 else ""
    border_bottom_rule = f"border-bottom: {tab_border_bottom};" if tab_border_bottom is not None else ""
    return f"""
{selector}::pane {{
    border: {pane_border};
    border-radius: {pane_radius}px;
    background: {pane_background};
    margin-top: {pane_margin_top}px;
}}
{tab_selector}::tab {{
    background: {tab_background};
    color: {tab_color};
    border: {tab_border};
    padding: {tab_padding};
    margin-right: {tab_margin_right}px;
    border-radius: {tab_radius}px;
    {min_width_rule}
    {border_bottom_rule}
}}
{tab_selector}::tab:selected {{
    background: {tab_selected_background};
    color: {tab_selected_color};
}}
{tab_selector}::tab:hover:!selected {{
    background: {tab_hover_background};
    color: {tab_hover_color};
}}
"""


def scoped_form_input_combo_qss(scope: str, *, include_text_edit: bool = False) -> str:
    """Return scoped QSS for line edits/combos used in dark-theme forms.

    The scope selector keeps the rules local to a dialog/component so shared
    styles do not accidentally override unrelated widgets elsewhere.
    """
    text_edit_selector = f",\n{scope} QTextEdit" if include_text_edit else ""
    return f"""
{scope} QLineEdit,
{scope} QComboBox{text_edit_selector} {{
    background: #111827;
    color: #f8fafc;
    border: 1px solid #334155;
    border-radius: 10px;
    padding: 8px 12px;
    min-height: 22px;
    font-size: 13px;
}}
{scope} QComboBox {{
    combobox-popup: 0;
    color: #f8fafc;
    min-height: 36px;
    padding: 0 36px 0 14px;
}}
{scope} QComboBox::drop-down,
{scope} QDateEdit::drop-down {{
    border: none;
    width: 34px;
    background: transparent;
}}
{scope} QComboBox::down-arrow {{
    image: none;
    width: 10px;
    height: 10px;
    border-right: 2px solid #e2e8f0;
    border-bottom: 2px solid #e2e8f0;
    margin-right: 10px;
}}
{scope} QComboBox QAbstractItemView {{
    background: #111827;
    color: #e2e8f0;
    border: 1px solid #334155;
    border-radius: 10px;
    padding: 8px;
    selection-background-color: #0ea5e9;
    selection-color: #eff6ff;
    outline: 0;
}}
"""


def dialog_surface_qss(selector: str) -> str:
    """Build the base dialog surface style for a specific root selector.

    This centralizes the shared "glass" surface and button rules so dialog
    modules only provide component-specific overrides.
    """
    return f"""
{selector} {{
    {_MINIMAL_DIALOG_SURFACE}
}}
{GLOBAL_BUTTON_QSS}
"""


def compose_styles(*parts: str) -> str:
    """Join non-empty QSS chunks with predictable spacing.

    Stripping each part avoids accidental selector breaks caused by leading or
    trailing newlines in triple-quoted strings.
    """
    chunks = [part.strip() for part in parts if part and part.strip()]
    return "\n\n".join(chunks)
