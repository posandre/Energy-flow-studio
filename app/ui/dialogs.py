from __future__ import annotations

from app.services.logging_utils import get_logger
from PySide6.QtCore import QEventLoop, Qt, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

from app.services.i18n import tr
from app.ui.design_system import compose_styles

LOGGER = get_logger(__name__)

ICON_MAP = {
    "info": ("i", "#38bdf8"),
    "warning": ("!", "#facc15"),
    "error": ("!", "#f87171"),
    "question": ("?", "#cbd5e1"),
    "success": ("✓", "#4ade80"),
}

NEON_CLOSE_BUTTON_STYLE = """
QPushButton#NeonCloseButton {
    min-width: 34px;
    max-width: 34px;
    min-height: 34px;
    max-height: 34px;
    padding: 0;
    border-radius: 17px;
    border: 1px solid rgba(103, 232, 249, 0.88);
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(8, 145, 178, 0.92), stop:1 rgba(34, 197, 94, 0.82));
    color: #effcff;
    font-size: 18px;
    font-weight: 700;
}
QPushButton#NeonCloseButton:hover {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
        stop:0 rgba(34, 211, 238, 0.98), stop:1 rgba(74, 222, 128, 0.9));
    border-color: rgba(165, 243, 252, 1);
}
QPushButton#NeonCloseButton:pressed {
    background: rgba(8, 47, 73, 0.96);
}
"""

NEON_HEADER_BAR_STYLE = """
QWidget#NeonHeaderBar {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 rgba(5, 10, 20, 0.98), stop:1 rgba(10, 18, 32, 0.94));
    border: 1px solid rgba(56, 189, 248, 0.18);
    border-radius: 16px;
}
QLabel#NeonHeaderTitle {
    color: #a7d5f5;
    font-size: 18px;
    font-weight: 700;
    padding: 10px 0;
}
"""

COMPACT_MESSAGE_DIALOG_STYLE = """
#CompactMessageDialog {
    background: #0f172a;
    border: 1px solid #31415f;
    border-radius: 18px;
}
#CompactMessageDialog QLabel {
    color: #e2e8f0;
}
#DialogIcon {
    background: rgba(148, 163, 184, 0.12);
    border: 1px solid #334155;
    border-radius: 27px;
    font-size: 28px;
    font-weight: 800;
    color: #f8fafc;
}
#DialogTitle {
    font-size: 16px;
    font-weight: 700;
    color: #f8fafc;
}
#DialogBody {
    font-size: 14px;
    color: #cbd5e1;
}
#CompactMessageDialog QPushButton {
    min-width: 82px;
    min-height: 32px;
    border-radius: 9px;
    font-weight: 600;
    padding: 5px 12px;
}
"""


def create_neon_close_button(parent: QWidget, close_handler) -> QPushButton:
    """Create a reusable close button matching the project's neon header style."""
    button = QPushButton("×", parent)
    button.setObjectName("NeonCloseButton")
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setToolTip(tr("Close"))
    button.clicked.connect(close_handler)
    return button


def create_neon_header_bar(parent: QWidget, close_handler, title: str = "") -> QWidget:
    """Build a centered dialog header with a fixed-size close control.

    The leading spacer keeps the title visually centered relative to the
    trailing close button.
    """
    shell = QWidget(parent)
    shell.setObjectName("NeonHeaderBar")
    shell.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    title_label = QLabel(title, shell)
    title_label.setObjectName("NeonHeaderTitle")
    title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    close_button = create_neon_close_button(shell, close_handler)
    close_button.setFixedSize(34, 34)

    layout = QHBoxLayout()
    layout.setContentsMargins(14, 6, 14, 6)
    layout.setSpacing(12)
    layout.addSpacing(34)
    layout.addWidget(title_label, 1)
    layout.addWidget(close_button)

    shell.setLayout(layout)
    return shell


class CompactMessageDialog(QDialog):
    """Small stylized confirmation/info dialog used across the application."""
    def __init__(
        self,
        parent,
        *,
        kind: str,
        title: str,
        text: str,
        accept_text: str = "OK",
        reject_text: str | None = None,
        destructive: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setObjectName("CompactMessageDialog")
        self.setMinimumWidth(420)
        self.setMaximumWidth(520)

        glyph, accent = ICON_MAP.get(kind, ICON_MAP["info"])

        icon_label = QLabel(glyph)
        icon_label.setObjectName("DialogIcon")
        icon_label.setProperty("accentColor", accent)
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_label.setFixedSize(54, 54)

        title_label = QLabel(title)
        title_label.setObjectName("DialogTitle")
        title_label.setWordWrap(True)

        body_label = QLabel(text)
        body_label.setObjectName("DialogBody")
        body_label.setWordWrap(True)

        header_layout = QHBoxLayout()
        header_layout.setSpacing(14)
        header_layout.addWidget(icon_label, alignment=Qt.AlignmentFlag.AlignTop)

        text_column = QVBoxLayout()
        text_column.setSpacing(6)
        text_column.addWidget(title_label)
        text_column.addWidget(body_label)
        header_layout.addLayout(text_column, stretch=1)

        self.accept_button = QPushButton(accept_text)
        self.accept_button.setObjectName("DangerButton" if destructive else "PrimaryActionButton")
        self.accept_button.setAutoDefault(True)
        self.accept_button.setDefault(True)
        self.accept_button.clicked.connect(self.accept)

        buttons_layout = QHBoxLayout()
        buttons_layout.addStretch()
        if reject_text:
            self.reject_button = QPushButton(reject_text)
            self.reject_button.setObjectName("SecondaryActionButton")
            self.reject_button.setAutoDefault(True)
            self.reject_button.clicked.connect(self.reject)
            buttons_layout.addWidget(self.reject_button)
        else:
            self.reject_button = None
        buttons_layout.addWidget(self.accept_button)

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(16)
        root.addWidget(create_neon_header_bar(self, self.reject, title=title))
        root.addSpacing(20)
        root.addLayout(header_layout)
        root.addLayout(buttons_layout)

        self.setStyleSheet(
            compose_styles(
                COMPACT_MESSAGE_DIALOG_STYLE,
                NEON_CLOSE_BUTTON_STYLE,
                NEON_HEADER_BAR_STYLE,
            )
        )

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._ensure_dialog_focus()
        QTimer.singleShot(0, self._ensure_dialog_focus)
        QTimer.singleShot(60, self._ensure_dialog_focus)

    def _ensure_dialog_focus(self) -> None:
        if not self.isVisible():
            return
        target = self.reject_button if self.reject_button is not None else self.accept_button
        if target is None:
            return
        window_handle = self.windowHandle()
        if window_handle is not None:
            window_handle.requestActivate()
        QApplication.setActiveWindow(self)
        self.raise_()
        self.activateWindow()
        target.setFocus(Qt.FocusReason.ActiveWindowFocusReason)


def prepare_modal_dialog(
    dialog: QDialog,
    *,
    frameless: bool = True,
    application_modal: bool = False,
) -> None:
    """Apply the baseline modal configuration expected by popup helpers."""
    if dialog.property("popup_force_window_modal") is None:
        # Keep modal behavior consistent across all managed dialogs unless a
        # dialog explicitly opts out.
        dialog.setProperty("popup_force_window_modal", True)
    if frameless:
        dialog.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        dialog.setWindowFlag(Qt.WindowType.Dialog, True)
    dialog.setModal(True)
    # ApplicationModal is intentionally not supported in this project.
    # Keep the argument for backward compatibility with existing call sites.
    _ = application_modal
    dialog.setWindowModality(Qt.WindowModality.WindowModal)


def _resolve_popup_manager(widget: QWidget | None):
    """Find the nearest parent that exposes popup-manager hooks."""
    owner = widget
    while owner is not None:
        if hasattr(owner, "_open_registered_modal") and hasattr(owner, "_register_popup_dialog"):
            return owner
        owner = owner.parentWidget()
    return None


def exec_modal_dialog(
    dialog: QDialog,
    *,
    frameless: bool = True,
    application_modal: bool = False,
) -> int:
    """Execute a dialog with popup-manager support and native fallback.

    Prefer the manager path because it keeps backdrop/focus behavior consistent
    with the rest of managed dialogs.
    """
    manager = _resolve_popup_manager(dialog.parentWidget())
    if manager is not None:
        try:
            manager._register_popup_dialog(dialog, delete_on_close=False, origin="dialogs:exec_modal_dialog")
            result_code = int(QDialog.DialogCode.Rejected)
            loop = QEventLoop(dialog)

            def _on_finished(code: int) -> None:
                nonlocal result_code
                result_code = int(code)
                if loop.isRunning():
                    loop.quit()

            dialog.finished.connect(_on_finished)
            manager._open_registered_modal(dialog, source="dialogs:exec_modal_dialog")
            if dialog.isVisible():
                loop.exec()
            else:
                result_code = int(QDialog.DialogCode.Rejected)
            try:
                dialog.finished.disconnect(_on_finished)
            except Exception:
                LOGGER.exception("Failed to disconnect dialog finished callback")
            if hasattr(manager, "_prune_popup_dialogs"):
                manager._prune_popup_dialogs(reason="dialogs_exec_done")
            return int(result_code)
        except Exception:
            # Fallback to native modal execution if popup-manager path fails.
            LOGGER.exception("Popup-manager exec path failed; falling back to native modal dialog")

    prepare_modal_dialog(
        dialog,
        frameless=frameless,
        application_modal=application_modal,
    )
    return int(dialog.exec())


def open_modal_dialog(
    dialog: QDialog,
    *,
    frameless: bool = True,
    application_modal: bool = False,
    raise_and_activate: bool = True,
) -> None:
    """Open a managed modal dialog without blocking the caller."""
    manager = _resolve_popup_manager(dialog.parentWidget())
    if manager is not None:
        try:
            manager._register_popup_dialog(dialog, delete_on_close=False, origin="dialogs:open_modal_dialog")
            manager._open_registered_modal(dialog, source="dialogs:open_modal_dialog")
            if raise_and_activate and dialog.isVisible():
                dialog.raise_()
                dialog.activateWindow()
            return
        except Exception:
            # Fallback to direct open path.
            LOGGER.exception("Popup-manager open path failed; falling back to direct open")

    prepare_modal_dialog(
        dialog,
        frameless=frameless,
        application_modal=application_modal,
    )
    dialog.open()
    if raise_and_activate:
        dialog.raise_()
        dialog.activateWindow()


def show_compact_message(
    parent,
    *,
    kind: str,
    title: str,
    text: str,
    accept_text: str = "OK",
) -> None:
    """Show a non-blocking compact info-style dialog using project theme."""
    dialog = CompactMessageDialog(
        parent,
        kind=kind,
        title=title,
        text=text,
        accept_text=accept_text,
    )
    exec_modal_dialog(dialog, frameless=True, application_modal=False)


def ask_compact_confirmation(
    parent,
    *,
    title: str,
    text: str,
    accept_text: str = "Confirm",
    reject_text: str = "Cancel",
    destructive: bool = False,
) -> bool:
    """Show a themed confirmation dialog and return True on accept."""
    dialog = CompactMessageDialog(
        parent,
        kind="question",
        title=title,
        text=text,
        accept_text=accept_text,
        reject_text=reject_text,
        destructive=destructive,
    )
    return exec_modal_dialog(dialog, frameless=True, application_modal=False) == int(QDialog.DialogCode.Accepted)
