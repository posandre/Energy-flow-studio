from __future__ import annotations

import os
import sys
import logging
from pathlib import Path
import threading

from PySide6.QtCore import QLockFile, QStandardPaths, QTimer, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QAbstractButton, QApplication, QTabBar

from app.services.app_settings import load_ui_language
from app.services.i18n import set_language
from app.services.logging_utils import configure_app_logging, get_logger
from app.ui.design_system import GLOBAL_BUTTON_QSS, GLOBAL_CHECKBOX_QSS, compose_styles
from app.ui.main_window import MainWindow


APP_BASE_STYLE = """
* {
    font-family: "SF Pro Text", "Inter", "Segoe UI", sans-serif;
}
QScrollBar:vertical {
    background: #152238;
    width: 12px;
    border: 1px solid #3a4d68;
    border-radius: 6px;
    margin: 12px 0 12px 0;
}
QScrollBar::handle:vertical {
    background: #4a5f7e;
    min-height: 26px;
    border: 1px solid #9fb3ca;
    border-radius: 5px;
    margin: 1px;
}
QScrollBar::handle:vertical:hover {
    background: #5b7396;
}
QScrollBar::sub-line:vertical,
QScrollBar::add-line:vertical {
    background: transparent;
    height: 0px;
    border: none;
    subcontrol-origin: margin;
    width: 0px;
}
QScrollBar::sub-line:vertical {
    subcontrol-position: top;
}
QScrollBar::add-line:vertical {
    subcontrol-position: bottom;
}
QScrollBar::up-arrow:vertical,
QScrollBar::down-arrow:vertical {
    width: 0px;
    height: 0px;
    background: transparent;
}
QScrollBar:horizontal {
    background: #152238;
    height: 12px;
    border: 1px solid #3a4d68;
    border-radius: 6px;
    margin: 0 12px 0 12px;
}
QScrollBar::handle:horizontal {
    background: #4a5f7e;
    min-width: 26px;
    border: 1px solid #9fb3ca;
    border-radius: 5px;
    margin: 1px;
}
QScrollBar::handle:horizontal:hover {
    background: #5b7396;
}
QScrollBar::sub-line:horizontal,
QScrollBar::add-line:horizontal {
    background: transparent;
    width: 0px;
    border: none;
    subcontrol-origin: margin;
    height: 0px;
}
QScrollBar::sub-line:horizontal {
    subcontrol-position: left;
}
QScrollBar::add-line:horizontal {
    subcontrol-position: right;
}
QScrollBar::left-arrow:horizontal,
QScrollBar::right-arrow:horizontal {
    width: 0px;
    height: 0px;
    background: transparent;
}
QScrollBar::add-page:vertical,
QScrollBar::sub-page:vertical,
QScrollBar::add-page:horizontal,
QScrollBar::sub-page:horizontal {
    background: transparent;
    border: none;
}
"""

APP_STYLE = compose_styles(
    APP_BASE_STYLE,
    GLOBAL_CHECKBOX_QSS,
    GLOBAL_BUTTON_QSS,
)

_INSTANCE_LOCK: QLockFile | None = None
LOGGER = get_logger(__name__)


def _install_global_exception_logging() -> None:
    def _handle_exception(exc_type, exc_value, exc_traceback):
        logging.getLogger("uncaught").error(
            "Unhandled exception in main thread",
            exc_info=(exc_type, exc_value, exc_traceback),
        )

    def _handle_thread_exception(args: threading.ExceptHookArgs):
        logging.getLogger("uncaught").error(
            "Unhandled exception in thread %s",
            getattr(args.thread, "name", "<unknown>"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )

    sys.excepthook = _handle_exception
    threading.excepthook = _handle_thread_exception


def _install_global_button_cursor_policy(app: QApplication) -> None:
    """Apply pointer cursor policy for all interactive button-like widgets."""
    for widget in app.allWidgets():
        if isinstance(widget, (QAbstractButton, QTabBar)):
            try:
                widget.setCursor(Qt.CursorShape.PointingHandCursor)
            except Exception:
                LOGGER.exception("Failed to set pointing cursor policy for widget %r", widget)


def _icon_path() -> Path:
    """Resolve app icon path for both source and packaged (.app) runs."""
    if getattr(sys, "frozen", False):
        base_path = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    else:
        base_path = Path(__file__).resolve().parents[1]
    return base_path / "app" / "assets" / "icons" / "energyflow_studio.png"


def _configure_tls_cert_bundle() -> None:
    """Configure cert bundle env vars so HTTPS works in bundled deployments.

    PyInstaller/macOS app layouts can place certifi in different locations,
    therefore we probe several candidates in priority order.
    """
    candidates: list[Path] = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        meipass_path = Path(meipass)
        candidates.append(meipass_path / "certifi" / "cacert.pem")
        candidates.append(meipass_path / "_internal" / "certifi" / "cacert.pem")

    executable_path = Path(sys.executable).resolve()
    # macOS .app layout: .../MyApp.app/Contents/MacOS/MyApp
    candidates.append(executable_path.parents[1] / "Resources" / "certifi" / "cacert.pem")

    try:
        import certifi

        certifi_path = Path(certifi.where())
        candidates.append(certifi_path)
    except Exception:
        LOGGER.exception("Failed to resolve certifi path while configuring TLS bundle")

    for candidate in candidates:
        try:
            if candidate.exists() and candidate.is_file():
                resolved = str(candidate)
                os.environ["SSL_CERT_FILE"] = resolved
                os.environ["REQUESTS_CA_BUNDLE"] = resolved
                return
        except Exception:
            LOGGER.exception("Failed to inspect TLS certificate bundle candidate: %s", candidate)
            continue


def _acquire_single_instance_lock() -> bool:
    """Acquire lock file to prevent launching duplicate app instances."""
    global _INSTANCE_LOCK
    try:
        app_data_dir = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
        base_dir = Path(app_data_dir).expanduser() if app_data_dir else (Path.home() / ".energyflow-studio")
        base_dir.mkdir(parents=True, exist_ok=True)
        lock_file = base_dir / "energyflow_studio.lock"
        lock = QLockFile(str(lock_file))
        lock.setStaleLockTime(0)
        if not lock.tryLock(100):
            return False
        _INSTANCE_LOCK = lock
        return True
    except Exception:
        LOGGER.exception("Failed to acquire single-instance lock; continuing without lock")
        return True


def main() -> int:
    """Application entry point: initialize app, window and startup hooks."""
    configure_app_logging()
    _install_global_exception_logging()
    set_language(load_ui_language(), persist=False)
    _configure_tls_cert_bundle()
    app = QApplication(sys.argv)
    if not _acquire_single_instance_lock():
        return 0
    app.setApplicationName("EnergyFlow Studio")
    app.setOrganizationName("Codex")
    app.setStyleSheet(APP_STYLE)
    icon_path = _icon_path()
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    window = MainWindow()
    window.show()

    # Enforce a centralized cursor policy for every button (existing and future).
    _install_global_button_cursor_policy(app)
    app.focusChanged.connect(lambda *_: _install_global_button_cursor_policy(app))
    cursor_sync_timer = QTimer(app)
    cursor_sync_timer.setInterval(1500)
    cursor_sync_timer.timeout.connect(lambda: _install_global_button_cursor_policy(app))
    cursor_sync_timer.start()

    QTimer.singleShot(0, window.initialize_startup_profile)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
