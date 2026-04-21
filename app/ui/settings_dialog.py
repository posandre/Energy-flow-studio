from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QTextEdit,
    QVBoxLayout,
)

from app.services.app_settings import (
    DessMonitorImportSettings,
    load_dessmonitor_import_settings,
    save_dessmonitor_import_settings,
)
from app.ui.design_system import compose_styles
from app.ui.dialogs import NEON_CLOSE_BUTTON_STYLE, NEON_HEADER_BAR_STYLE, create_neon_header_bar, show_compact_message

SETTINGS_DIALOG_STYLE = """
#SettingsDialog {
    background: #0f172a;
}
#SettingsDialog QLabel,
#SettingsDialog QCheckBox {
    color: #e2e8f0;
    font-size: 13px;
}
#SettingsDialog QTextEdit {
    background: #111827;
    color: #f8fafc;
    border: 1px solid #334155;
    border-radius: 8px;
    padding: 6px 8px;
}
"""


class SettingsDialog(QDialog):
    """Global app settings focused on DessMonitor import behavior."""
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(500, 360)
        self.setObjectName("SettingsDialog")

        current = load_dessmonitor_import_settings()

        intro = QLabel(
            "Configure how DessMonitor imports work. By default the app imports all available "
            "parameter keys for the selected device."
        )
        intro.setWordWrap(True)

        self.import_all_checkbox = QCheckBox("Import all available DessMonitor parameter keys")
        self.import_all_checkbox.setChecked(current.import_all_parameters)
        self.import_all_checkbox.toggled.connect(self._sync_parameter_editor_state)

        self.parameter_keys_edit = QTextEdit()
        self.parameter_keys_edit.setPlaceholderText(
            "Enter one parameter key per line, for example:\nPV_OUTPUT_POWER\nGRID_ACTIVE_POWER"
        )
        self.parameter_keys_edit.setMaximumHeight(150)
        self.parameter_keys_edit.setPlainText("\n".join(current.normalized_keys()))

        self.parameter_help = QLabel(
            "These custom keys are used only when 'Import all available...' is turned off."
        )
        self.parameter_help.setWordWrap(True)

        form = QFormLayout()
        form.addRow("DessMonitor", self.import_all_checkbox)
        form.addRow("Parameter keys", self.parameter_keys_edit)
        form.addRow("", self.parameter_help)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_button = buttons.button(QDialogButtonBox.Ok)
        if ok_button is not None:
            ok_button.setText("Save")
            ok_button.setObjectName("PrimaryActionButton")
        cancel_button = buttons.button(QDialogButtonBox.Cancel)
        if cancel_button is not None:
            cancel_button.setObjectName("SecondaryActionButton")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(create_neon_header_bar(self, self.reject, title="Settings"))
        layout.addSpacing(20)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(buttons)

        self._apply_styles()
        self._sync_parameter_editor_state()

    def _apply_styles(self) -> None:
        """Apply dialog-local style plus shared neon header controls."""
        self.setStyleSheet(
            compose_styles(
                SETTINGS_DIALOG_STYLE,
                NEON_CLOSE_BUTTON_STYLE,
                NEON_HEADER_BAR_STYLE,
            )
        )

    def _sync_parameter_editor_state(self) -> None:
        """Disable manual key editor when automatic import mode is enabled."""
        enabled = not self.import_all_checkbox.isChecked()
        self.parameter_keys_edit.setEnabled(enabled)
        self.parameter_help.setEnabled(enabled)

    def _custom_keys(self) -> list[str]:
        """Return normalized non-empty custom parameter keys from textarea."""
        return [line.strip() for line in self.parameter_keys_edit.toPlainText().splitlines() if line.strip()]

    def accept(self) -> None:
        """Validate settings and persist them before closing the dialog."""
        if not self.import_all_checkbox.isChecked() and not self._custom_keys():
            show_compact_message(
                self,
                kind="warning",
                title="Missing keys",
                text="Enter at least one parameter key or enable importing all available keys.",
            )
            return

        save_dessmonitor_import_settings(
            DessMonitorImportSettings(
                import_all_parameters=self.import_all_checkbox.isChecked(),
                parameter_keys=self._custom_keys(),
            )
        )
        super().accept()
