from __future__ import annotations

from pathlib import Path
import re
import unicodedata

import pandas as pd
from PySide6.QtCore import QObject, QRect, QSize, QThread, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QScrollArea,
    QSizePolicy,
    QStyledItemDelegate,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)
try:
    from PySide6.QtWebEngineWidgets import QWebEngineView
except ImportError:  # pragma: no cover
    QWebEngineView = None

from app.services.app_settings import (
    DEFAULT_ENERGYFLOW_REFRESH_SECONDS,
    DEFAULT_UI_LANGUAGE,
    DEFAULT_TUYA_POLL_INTERVAL_SEC,
    DEFAULT_TUYA_ENDPOINT,
    DeviceControlFieldProfile,
    DeviceProfile,
    MAX_TUYA_POLL_INTERVAL_SEC,
    MIN_TUYA_POLL_INTERVAL_SEC,
    UI_LANGUAGE_CHOICES,
    TUYA_ENDPOINT_CHOICES,
    load_active_profile_name,
    load_device_profiles,
    save_active_profile_name,
    save_device_profiles,
)
from app.services.i18n import tr, tr_fragment, translate_widget_tree
from app.services.logging_utils import get_logger
from app.services.dessmonitor_api import (
    DessMonitorApiError,
    DessMonitorConfig,
    DeviceControlField,
    DeviceLocation,
    DessMonitorDevice,
    fetch_device_control_fields,
    fetch_device_location,
    fetch_devices,
    fetch_key_parameters,
    humanize_error_text,
)
from app.ui.dialogs import (
    NEON_CLOSE_BUTTON_STYLE,
    NEON_HEADER_BAR_STYLE,
    ask_compact_confirmation,
    create_neon_header_bar,
    exec_modal_dialog,
    show_compact_message,
)
from app.ui.design_system import (
    GLOBAL_BUTTON_QSS,
    apply_tab_widget_interaction,
    apply_minimal_button,
    apply_minimal_checkbox,
    compose_styles,
    dialog_surface_qss,
    scoped_form_input_combo_qss,
    tab_widget_qss,
)


LOGGER = get_logger(__name__)


class ConnectionCheckWorker(QObject):
    """Background worker that validates credentials and preloads device metadata."""
    progress = Signal(str)
    success = Signal(object, object, object, object, object)
    failure = Signal(str)
    finished = Signal()

    def __init__(self, config: DessMonitorConfig, selected_device: DessMonitorDevice | None) -> None:
        super().__init__()
        self._config = config
        self._selected_device = selected_device

    @Slot()
    def run(self) -> None:
        """Authenticate and fetch everything required for profile verification."""
        try:
            self.progress.emit(tr("Authenticating with DessMonitor..."))
            devices = fetch_devices(self._config, logger=self._emit_progress)
            self.progress.emit(tr("Loading supported import fields..."))

            selected_device = self._selected_device
            if selected_device is not None:
                matched = next(
                    (
                        item
                        for item in devices
                        if (item.pn, item.devcode, item.devaddr, item.sn)
                        == (selected_device.pn, selected_device.devcode, selected_device.devaddr, selected_device.sn)
                    ),
                    None,
                )
                selected_device = matched or devices[0]
            else:
                selected_device = devices[0]

            keys = fetch_key_parameters(self._config, devcode=selected_device.devcode, logger=self._emit_progress)
            self.progress.emit(tr("Loading inverter control fields..."))
            selected_config = DessMonitorConfig(
                username=self._config.username,
                password=self._config.password,
                company_key=self._config.company_key,
                source=self._config.source,
                pn=selected_device.pn,
                devcode=selected_device.devcode,
                devaddr=selected_device.devaddr,
                sn=selected_device.sn,
                device_label=selected_device.display_name,
                parameter_keys=[],
                date_from=self._config.date_from,
                date_to=self._config.date_to,
                period_mode=self._config.period_mode,
                i18n=self._config.i18n,
                app_client=self._config.app_client,
                app_id=self._config.app_id,
                app_version=self._config.app_version,
            )
            control_fields = fetch_device_control_fields(selected_config, logger=self._emit_progress)
            self.progress.emit(tr("Loading inverter location..."))
            try:
                location = fetch_device_location(selected_config, logger=self._emit_progress)
            except DessMonitorApiError as exc:
                LOGGER.warning("ConnectionCheckWorker: failed to load inverter location: %s", exc)
                location = None
            self.progress.emit(tr_fragment(f"Loaded {len(keys)} available field(s) for {selected_device.display_name}."))
            self.success.emit(devices, selected_device, keys, control_fields, location)
        except DessMonitorApiError as exc:
            LOGGER.exception("ConnectionCheckWorker failed during verification flow")
            self.failure.emit(humanize_error_text(str(exc)))
        finally:
            self.finished.emit()

    def _emit_progress(self, message: str) -> None:
        self.progress.emit(humanize_error_text(message))


class InverterFieldsItemDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):  # noqa: N802
        size = super().sizeHint(option, index)
        return QSize(size.width(), max(size.height(), 48))

    def updateEditorGeometry(self, editor, option, index):  # noqa: N802
        del index
        rect = QRect(option.rect)
        rect.adjust(6, 6, -6, -6)
        editor.setGeometry(rect)


class MapPickerDialog(QDialog):
    """Leaflet-based map picker embedded into a themed Qt dialog."""
    def __init__(
        self,
        parent=None,
        *,
        location_label: str = "",
        latitude: str = "",
        longitude: str = "",
    ) -> None:
        super().__init__(parent)
        self.setObjectName("MapPickerDialog")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle(tr("Choose Location"))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(760, 560)
        self._selected_label = location_label.strip()
        self._selected_latitude = latitude.strip()
        self._selected_longitude = longitude.strip()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        self.setStyleSheet(
            compose_styles(
                # Keep map shell styling local to this dialog to avoid leaking
                # map-specific selectors into global dialog themes.
                """
            QDialog#MapPickerDialog {
                background: #08111e;
            }
            QFrame#MapPickerPanel {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #09111f, stop:0.55 #0f172a, stop:1 #11233b);
                border: 1px solid rgba(34, 211, 238, 0.18);
                border-radius: 20px;
            }
            QLabel#MapPickerHeader {
                color: #cfefff;
                font-size: 13px;
                font-weight: 600;
            }
            QLabel#MapPickerStatus {
                color: #d8f6ff;
                font-size: 13px;
            }
            QLabel#MapPickerHint {
                color: #7fc9ea;
                font-size: 12px;
            }
                """
            )
        )

        panel = QFrame()
        panel.setObjectName("MapPickerPanel")
        panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout.addWidget(panel, 1)

        panel_layout = QVBoxLayout(panel)
        panel_layout.setContentsMargins(18, 18, 18, 18)
        panel_layout.setSpacing(10)

        header = QLabel(tr("Search for a place or click directly on the map to set the inverter location."))
        header.setObjectName("MapPickerHeader")
        header.setWordWrap(True)
        panel_layout.addWidget(header)

        if QWebEngineView is not None:
            self.browser = QWebEngineView()
            self.browser.titleChanged.connect(self._handle_title_changed)
            self.browser.setHtml(self._build_map_html(), QUrl("https://local.energyflow.map/"))
            panel_layout.addWidget(self.browser, 1)
        else:
            self.browser = None
            fallback = QLabel(tr("Map view is unavailable in this environment. Please enter location manually."))
            fallback.setObjectName("MapPickerStatus")
            fallback.setWordWrap(True)
            panel_layout.addWidget(fallback, 1)

        self.selection_label = QLabel(self._selection_summary())
        self.selection_label.setObjectName("MapPickerStatus")
        self.selection_label.setWordWrap(True)
        panel_layout.addWidget(self.selection_label)

        hint_label = QLabel(tr("Tip: click a point on the map or search by city, village, or full address."))
        hint_label.setObjectName("MapPickerHint")
        hint_label.setWordWrap(True)
        panel_layout.addWidget(hint_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_button = buttons.button(QDialogButtonBox.Ok)
        if ok_button is not None:
            ok_button.setText(tr("Use location"))
            ok_button.setObjectName("PrimaryActionButton")
            apply_minimal_button(ok_button)
            ok_button.setEnabled(bool(self._selected_latitude and self._selected_longitude))
        cancel_button = buttons.button(QDialogButtonBox.Cancel)
        if cancel_button is not None:
            cancel_button.setObjectName("SecondaryActionButton")
            apply_minimal_button(cancel_button)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        self._ok_button = ok_button
        panel_layout.addWidget(buttons)
        translate_widget_tree(self)

    def selected_location(self) -> tuple[str, str, str]:
        """Return current map selection as label/lat/lon tuple."""
        return (
            self._selected_label.strip(),
            self._selected_latitude.strip(),
            self._selected_longitude.strip(),
        )

    def _selection_summary(self) -> str:
        if self._selected_latitude and self._selected_longitude:
            label = self._selected_label or tr("Custom map point")
            return tr_fragment(f"Selected: {label} ({self._selected_latitude}, {self._selected_longitude})")
        return tr("No map point selected yet.")

    @Slot(str)
    def _handle_title_changed(self, title: str) -> None:
        # Bridge JS -> Qt via document.title updates ("coords:lat|lon|label").
        # This keeps integration lightweight without a dedicated web channel.
        if not title.startswith("coords:"):
            return
        payload = title.removeprefix("coords:")
        parts = payload.split("|", 2)
        if len(parts) < 2:
            return
        self._selected_latitude = parts[0].strip()
        self._selected_longitude = parts[1].strip()
        self._selected_label = parts[2].strip() if len(parts) > 2 else ""
        self.selection_label.setText(self._selection_summary())
        if self._ok_button is not None:
            self._ok_button.setEnabled(bool(self._selected_latitude and self._selected_longitude))

    def _build_map_html(self) -> str:
        initial_lat = self._selected_latitude or "50.4501"
        initial_lon = self._selected_longitude or "30.5234"
        initial_zoom = "12" if self._selected_latitude and self._selected_longitude else "6"
        initial_label = (self._selected_label or "").replace("\\", "\\\\").replace("'", "\\'")
        search_placeholder = tr("Search city, village or address")
        search_button_label = tr("Search")
        map_status_hint = tr("Click on the map to place the inverter.")
        selected_prefix = tr("Selected")
        selected_coordinates_prefix = tr("Selected coordinates")
        location_not_found = tr("Location not found. Try another query.")
        return f"""
        <!DOCTYPE html>
        <html>
        <head>
          <meta charset="utf-8" />
          <meta name="viewport" content="width=device-width, initial-scale=1.0">
          <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
          <style>
            html, body {{
              margin: 0;
              padding: 0;
              background: #0b1220;
              color: #e2e8f0;
              font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
              height: 100%;
            }}
            .shell {{
              display: flex;
              flex-direction: column;
              height: 100vh;
              background:
                radial-gradient(circle at top, rgba(10, 36, 58, 0.95) 0%, rgba(8, 16, 30, 0.98) 52%, #09111f 100%);
            }}
            .toolbar {{
              display: flex;
              gap: 8px;
              padding: 12px;
              background: rgba(8, 16, 30, 0.96);
              border-bottom: 1px solid rgba(56, 189, 248, 0.22);
              box-shadow: inset 0 -1px 0 rgba(103, 232, 249, 0.10);
            }}
            .toolbar input {{
              flex: 1;
              background: #111827;
              color: #f8fafc;
              border: 1px solid rgba(71, 85, 105, 0.92);
              border-radius: 10px;
              padding: 10px 12px;
              font-size: 14px;
              box-shadow: inset 0 0 0 1px rgba(103, 232, 249, 0.05);
            }}
            .toolbar button {{
              background: linear-gradient(180deg, rgba(10, 65, 101, 0.96) 0%, rgba(7, 35, 56, 0.98) 100%);
              color: #d9f7ff;
              border: 1px solid rgba(34, 211, 238, 0.72);
              border-radius: 10px;
              padding: 0 14px;
              font-size: 14px;
              font-weight: 600;
              box-shadow: 0 0 18px rgba(34, 211, 238, 0.18);
            }}
            .toolbar button:hover {{
              border-color: rgba(103, 232, 249, 0.95);
              box-shadow: 0 0 24px rgba(34, 211, 238, 0.28);
            }}
            #map {{
              flex: 1;
              outline: 1px solid rgba(56, 189, 248, 0.18);
            }}
            .status {{
              padding: 10px 12px;
              background: rgba(8, 16, 30, 0.96);
              border-top: 1px solid rgba(56, 189, 248, 0.22);
              color: #8fd8ff;
              font-size: 13px;
            }}
            .leaflet-control-zoom a {{
              background: rgba(8, 16, 30, 0.92) !important;
              color: #d9f7ff !important;
              border-bottom: 1px solid rgba(56, 189, 248, 0.18) !important;
            }}
            .leaflet-control-zoom a:hover {{
              background: rgba(16, 43, 67, 0.96) !important;
            }}
            .leaflet-bar {{
              border: 1px solid rgba(56, 189, 248, 0.35) !important;
              box-shadow: 0 0 16px rgba(34, 211, 238, 0.16) !important;
            }}
            .leaflet-control-attribution {{
              background: rgba(8, 16, 30, 0.72) !important;
              color: #8fb0cb !important;
              border-top-left-radius: 8px;
            }}
            .leaflet-control-attribution a {{
              color: #67e8f9 !important;
            }}
          </style>
        </head>
        <body>
          <div class="shell">
            <div class="toolbar">
              <input id="search" type="text" placeholder="{search_placeholder}" value="{initial_label}" />
              <button id="searchBtn" type="button">{search_button_label}</button>
            </div>
            <div id="map"></div>
            <div class="status" id="status">{map_status_hint}</div>
          </div>
          <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
          <script>
            const map = L.map('map', {{ zoomControl: true }}).setView([{initial_lat}, {initial_lon}], {initial_zoom});
            L.tileLayer('https://{{s}}.tile.openstreetmap.org/{{z}}/{{x}}/{{y}}.png', {{
              maxZoom: 19,
              attribution: '&copy; OpenStreetMap contributors'
            }}).addTo(map);
            let marker = null;

            function emitSelection(lat, lon, label) {{
              document.title = `coords:${{lat}}|${{lon}}|${{label || ''}}`;
              document.getElementById('status').textContent =
                label ? `{selected_prefix}: ${{label}} (${{lat}}, ${{lon}})` : `{selected_coordinates_prefix}: ${{lat}}, ${{lon}}`;
            }}

            function placeMarker(lat, lon, label) {{
              if (marker) {{
                marker.setLatLng([lat, lon]);
              }} else {{
                marker = L.marker([lat, lon]).addTo(map);
              }}
              emitSelection(lat, lon, label);
            }}

            async function reverseGeocode(lat, lon) {{
              try {{
                const url = `https://nominatim.openstreetmap.org/reverse?format=jsonv2&lat=${{lat}}&lon=${{lon}}`;
                const response = await fetch(url, {{ headers: {{ 'Accept': 'application/json' }} }});
                const data = await response.json();
                const label = data.display_name || '';
                placeMarker(lat, lon, label);
              }} catch (_error) {{
                placeMarker(lat, lon, '');
              }}
            }}

            async function searchLocation() {{
              const query = document.getElementById('search').value.trim();
              if (!query) return;
              const url = `https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&q=${{encodeURIComponent(query)}}`;
              const response = await fetch(url, {{ headers: {{ 'Accept': 'application/json' }} }});
              const data = await response.json();
              if (!data.length) {{
                document.getElementById('status').textContent = '{location_not_found}';
                return;
              }}
              const result = data[0];
              const lat = Number(result.lat).toFixed(6);
              const lon = Number(result.lon).toFixed(6);
              map.setView([lat, lon], 13);
              placeMarker(lat, lon, result.display_name || query);
            }}

            document.getElementById('searchBtn').addEventListener('click', searchLocation);
            document.getElementById('search').addEventListener('keydown', (event) => {{
              if (event.key === 'Enter') {{
                event.preventDefault();
                searchLocation();
              }}
            }});

            map.on('click', (event) => {{
              const lat = event.latlng.lat.toFixed(6);
              const lon = event.latlng.lng.toFixed(6);
              reverseGeocode(lat, lon);
            }});

            if ({'true' if self._selected_latitude and self._selected_longitude else 'false'}) {{
              placeMarker('{initial_lat}', '{initial_lon}', '{initial_label}');
            }}
          </script>
        </body>
        </html>
        """


class DeviceProfileEditDialog(QDialog):
    profile_saved = Signal(object)
    REFRESH_INTERVAL_OPTIONS = [
        ("1 min", 60),
        ("3 min", 180),
        ("5 min", 300),
        ("10 min", 600),
    ]
    AUTO_SYNC_INTERVAL_MINUTES = 12

    def __init__(
        self,
        parent=None,
        profile: DeviceProfile | None = None,
        *,
        persist_on_accept: bool = False,
    ) -> None:
        super().__init__(parent)
        self._persist_on_accept = persist_on_accept
        self._is_edit_mode = profile is not None
        self._dialog_title = tr("Edit Device Profile") if self._is_edit_mode else tr("Add Profile")
        self.setWindowTitle(self._dialog_title)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(560, 560)
        self.setObjectName("DeviceProfileEditDialog")
        self._original_profile_name = profile.profile_name if profile else ""
        profile = profile or DeviceProfile(
            "",
            "",
            "",
            "",
            1,
            "",
            "",
            "",
            "",
            "",
            DEFAULT_ENERGYFLOW_REFRESH_SECONDS,
            False,
            [],
            [],
            [],
        )

        self.profile_name = QLineEdit(profile.profile_name)
        self.username = QLineEdit(profile.username)
        self.password = QLineEdit(profile.password)
        self.password.setEchoMode(QLineEdit.Password)
        self.company_key = QLineEdit(profile.company_key)
        self.source = QComboBox()
        self.source.addItem(tr("Energy storage"), 1)
        self.source.addItem(tr("Photovoltaic"), 0)
        source_index = self.source.findData(profile.source)
        self.source.setCurrentIndex(max(0, source_index))
        self.refresh_interval = QComboBox()
        for label, seconds in self.REFRESH_INTERVAL_OPTIONS:
            self.refresh_interval.addItem(tr(label), seconds)
        refresh_index = self.refresh_interval.findData(profile.resolved_energyflow_refresh_seconds())
        if refresh_index < 0:
            refresh_index = self.refresh_interval.findData(DEFAULT_ENERGYFLOW_REFRESH_SECONDS)
        self.refresh_interval.setCurrentIndex(max(0, refresh_index))
        self.refresh_interval.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.auto_sync_checkbox = QCheckBox(tr_fragment(f"Run every {self.AUTO_SYNC_INTERVAL_MINUTES} minutes"))
        self.auto_sync_checkbox.setObjectName("AutoSyncCheckbox")
        apply_minimal_checkbox(self.auto_sync_checkbox)
        self.auto_sync_checkbox.setChecked(bool(profile.auto_sync_enabled))
        self.auto_sync_checkbox.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.auto_sync_field = QWidget()
        self.auto_sync_field.setObjectName("AutoSyncField")
        self.auto_sync_field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.auto_sync_field.setFixedHeight(36)
        auto_sync_layout = QHBoxLayout(self.auto_sync_field)
        auto_sync_layout.setContentsMargins(12, 0, 12, 0)
        auto_sync_layout.setSpacing(8)
        auto_sync_layout.addWidget(self.auto_sync_checkbox, 1, Qt.AlignmentFlag.AlignVCenter)
        self.automation_cooldown = QSpinBox()
        self.automation_cooldown.setRange(0, 86400)
        self.automation_cooldown.setSuffix(f" {tr('sec')}")
        self.automation_cooldown.setValue(profile.resolved_automation_cooldown_sec())
        self.tuya_enabled_checkbox = QCheckBox(tr("Enable Tuya"))
        self.tuya_enabled_checkbox.setObjectName("AutoSyncCheckbox")
        apply_minimal_checkbox(self.tuya_enabled_checkbox)
        self.tuya_enabled_checkbox.setChecked(bool(profile.tuya_enabled))
        self.tuya_enabled_checkbox.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.tuya_enabled_field = QWidget()
        self.tuya_enabled_field.setObjectName("AutoSyncField")
        self.tuya_enabled_field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.tuya_enabled_field.setFixedHeight(36)
        tuya_enabled_layout = QHBoxLayout(self.tuya_enabled_field)
        tuya_enabled_layout.setContentsMargins(12, 0, 12, 0)
        tuya_enabled_layout.setSpacing(8)
        tuya_enabled_layout.addWidget(self.tuya_enabled_checkbox, 1, Qt.AlignmentFlag.AlignVCenter)
        self.tuya_client_id_field = QLineEdit(profile.tuya_client_id)
        self.tuya_client_id_field.setPlaceholderText(tr("Enter Tuya client ID"))
        self.tuya_client_secret_field = QLineEdit(profile.tuya_client_secret)
        self.tuya_client_secret_field.setEchoMode(QLineEdit.EchoMode.Password)
        self.tuya_client_secret_field.setPlaceholderText(tr("Enter Tuya client secret"))
        self.tuya_endpoint_combo = QComboBox()
        for label, endpoint in TUYA_ENDPOINT_CHOICES:
            self.tuya_endpoint_combo.addItem(f"{label} ({endpoint})", endpoint)
        tuya_endpoint_index = self.tuya_endpoint_combo.findData(profile.resolved_tuya_endpoint())
        self.tuya_endpoint_combo.setCurrentIndex(max(0, tuya_endpoint_index))
        self.tuya_poll_interval_field = QLineEdit(str(profile.resolved_tuya_poll_interval_sec()))
        self.tuya_poll_interval_field.setPlaceholderText("30")
        self.battery_capability_field = QLineEdit(
            f"{profile.resolved_battery_capability_ah():g}" if profile.resolved_battery_capability_ah() > 0 else "0"
        )
        self.battery_capability_field.setPlaceholderText("0")
        self.language_combo = QComboBox()
        for label, code in UI_LANGUAGE_CHOICES:
            self.language_combo.addItem(label, code)
        language_index = self.language_combo.findData(profile.resolved_ui_language())
        if language_index < 0:
            language_index = self.language_combo.findData(DEFAULT_UI_LANGUAGE)
        self.language_combo.setCurrentIndex(max(0, language_index))
        self.day_zone_tariff_field = QLineEdit(
            f"{profile.resolved_day_zone_tariff_uah_per_kwh():g}"
            if profile.resolved_day_zone_tariff_uah_per_kwh() > 0
            else "0"
        )
        self.day_zone_tariff_field.setPlaceholderText("0")
        self.night_zone_tariff_field = QLineEdit(
            f"{profile.resolved_night_zone_tariff_uah_per_kwh():g}"
            if profile.resolved_night_zone_tariff_uah_per_kwh() > 0
            else "0"
        )
        self.night_zone_tariff_field.setPlaceholderText("0")
        self.tuya_enabled_checkbox.toggled.connect(self._sync_tuya_fields_enabled)
        self.location_mode = QComboBox()
        self.location_mode.addItem(tr("Auto from DessMonitor"), "auto")
        self.location_mode.addItem(tr("Set manually"), "manual")
        location_mode_index = self.location_mode.findData(profile.resolved_inverter_location_mode())
        self.location_mode.setCurrentIndex(max(0, location_mode_index))
        self.location_field = QLineEdit(profile.inverter_location_label)
        self.location_field.setPlaceholderText(tr("City, region or coordinates"))
        self.location_status = QLabel("")
        self.location_status.setObjectName("FieldStatus")
        self.location_hint = QLabel(tr("Required for weather-based generation forecast."))
        self.location_hint.setObjectName("FieldHint")
        self.location_hint.setWordWrap(True)
        self.pick_map_button = QPushButton(tr("Choose on map"))
        self.pick_map_button.setObjectName("SecondaryActionButton")
        self.pick_map_button.clicked.connect(self._open_map_picker)
        self.use_detected_button = QPushButton(tr("Use detected"))
        self.use_detected_button.setObjectName("SecondaryActionButton")
        self.use_detected_button.clicked.connect(self._use_detected_location)

        self.verify_button = QPushButton(tr("Check connection"))
        self.verify_button.setObjectName("VerifyButton")
        self.verify_button.clicked.connect(self.verify_connection)
        self.verify_status = QLabel(tr("Step 1. Fill in the credentials and run Check."))
        self.verify_status.setObjectName("VerifyStatus")
        self.verify_status.setWordWrap(True)

        self.detected_device = QComboBox()
        self.detected_device.setEnabled(False)
        self.detected_device.currentIndexChanged.connect(self._apply_device_selection)
        self.detected_device_label = QLabel("")
        self.detected_device_label.setVisible(False)
        self.detected_device_title = QLabel(tr("Step 2. Choose device"))
        self.import_fields_note = QLabel(tr("All available import fields will be imported automatically."))
        self.import_fields_note.setObjectName("FieldHint")
        self.import_fields_note.setWordWrap(True)
        self.edit_inverter_fields_button = QPushButton(tr("Edit inverter fields"))
        self.edit_inverter_fields_button.setObjectName("SecondaryActionButton")
        self.edit_inverter_fields_button.clicked.connect(self._open_inverter_fields_dialog)

        def build_field_stack(title: str, field: QWidget) -> QVBoxLayout:
            label = QLabel(title)
            field.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            stack = QVBoxLayout()
            stack.setContentsMargins(0, 0, 0, 0)
            stack.setSpacing(6)
            stack.addWidget(label)
            stack.addWidget(field)
            return stack

        def build_section_card(title: str, body_layout: QVBoxLayout, *, caption: str = "") -> QFrame:
            card = QFrame()
            card.setObjectName("SectionCard")
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(16, 14, 16, 16)
            card_layout.setSpacing(12)
            if title:
                title_label = QLabel(title)
                title_label.setObjectName("SectionTitle")
                card_layout.addWidget(title_label)
            if caption:
                caption_label = QLabel(caption)
                caption_label.setObjectName("SectionCaption")
                caption_label.setWordWrap(True)
                card_layout.addWidget(caption_label)
            card_layout.addLayout(body_layout)
            return card

        left_form = QVBoxLayout()
        left_form.setContentsMargins(0, 0, 0, 0)
        left_form.setSpacing(12)
        left_form.addLayout(build_field_stack(tr("Password"), self.password))
        left_form.addLayout(build_field_stack(tr("Platform"), self.source))

        right_form = QVBoxLayout()
        right_form.setContentsMargins(0, 0, 0, 0)
        right_form.setSpacing(12)
        right_form.addLayout(build_field_stack(tr("Username"), self.username))
        right_form.addLayout(build_field_stack(tr("Company key"), self.company_key))
        right_form.addStretch(1)
        location_stack = build_field_stack(tr("Inverter location *"), self.location_field)
        location_stack.addWidget(self.location_status)
        location_stack.addWidget(self.location_hint)
        location_actions = QHBoxLayout()
        location_actions.setContentsMargins(0, 0, 0, 0)
        location_actions.setSpacing(8)
        location_actions.addWidget(self.pick_map_button)
        location_actions.addWidget(self.use_detected_button)
        location_actions.addStretch(1)
        location_stack.addLayout(location_actions)
        auto_sync_row = build_field_stack(tr("Automatic background sync"), self.auto_sync_field)
        sync_stack = build_field_stack(tr("EnergyFlow refresh interval"), self.refresh_interval)

        verify_row = QHBoxLayout()
        verify_row.addWidget(self.verify_button)
        verify_row.addWidget(self.verify_status, 1)
        verify_row.addStretch()

        connection_grid = QGridLayout()
        connection_grid.setContentsMargins(0, 0, 0, 0)
        connection_grid.setHorizontalSpacing(24)
        connection_grid.setVerticalSpacing(14)
        connection_grid.setColumnStretch(0, 1)
        connection_grid.setColumnStretch(1, 1)
        connection_grid.addLayout(build_field_stack(tr("Profile name"), self.profile_name), 0, 0, 1, 2)
        connection_grid.addLayout(left_form, 1, 0)
        connection_grid.addLayout(right_form, 1, 1)
        connection_grid.addLayout(verify_row, 2, 0, 1, 2)

        connection_body = QVBoxLayout()
        connection_body.setContentsMargins(0, 0, 0, 0)
        connection_body.setSpacing(0)
        connection_body.addLayout(connection_grid)
        self.connection_section = build_section_card(
            "",
            connection_body,
            caption=tr("Profile access and DessMonitor authentication for this inverter."),
        )

        location_grid = QGridLayout()
        location_grid.setContentsMargins(0, 0, 0, 0)
        location_grid.setHorizontalSpacing(24)
        location_grid.setVerticalSpacing(14)
        location_grid.setColumnStretch(0, 1)
        location_grid.setColumnStretch(1, 1)
        location_grid.addLayout(build_field_stack(tr("Location source"), self.location_mode), 0, 0, 1, 2)
        location_grid.addLayout(location_stack, 1, 0, 1, 2)

        location_body = QVBoxLayout()
        location_body.setContentsMargins(0, 0, 0, 0)
        location_body.setSpacing(0)
        location_body.addLayout(location_grid)
        self.location_section = build_section_card(
            "",
            location_body,
            caption=tr("Weather and forecast features use this location. You can keep the detected location, search for a place, or choose a point on the map."),
        )

        sync_grid = QGridLayout()
        sync_grid.setContentsMargins(0, 0, 0, 0)
        sync_grid.setHorizontalSpacing(24)
        sync_grid.setVerticalSpacing(14)
        sync_grid.setColumnStretch(0, 1)
        sync_grid.setColumnStretch(1, 1)
        sync_grid.addLayout(sync_stack, 0, 0)
        sync_grid.addLayout(auto_sync_row, 0, 1)
        sync_grid.addLayout(build_field_stack(tr("Tuya polling interval (seconds)"), self.tuya_poll_interval_field), 1, 0)
        sync_grid.addLayout(build_field_stack(tr("Automation repeat delay (seconds)"), self.automation_cooldown), 1, 1)

        sync_body = QVBoxLayout()
        sync_body.setContentsMargins(0, 0, 0, 0)
        sync_body.setSpacing(0)
        sync_body.addLayout(sync_grid)
        self.sync_section = build_section_card(
            "",
            sync_body,
            caption=tr("Controls how often the app refreshes EnergyFlow, runs background sync, and polls Tuya devices for this profile."),
        )

        tuya_grid = QGridLayout()
        tuya_grid.setContentsMargins(0, 0, 0, 0)
        tuya_grid.setHorizontalSpacing(24)
        tuya_grid.setVerticalSpacing(14)
        tuya_grid.setColumnStretch(0, 1)
        tuya_grid.setColumnStretch(1, 1)
        tuya_grid.addLayout(build_field_stack(tr("Tuya integration"), self.tuya_enabled_field), 0, 0)
        tuya_grid.addLayout(build_field_stack(tr("Tuya client ID"), self.tuya_client_id_field), 1, 0, 1, 2)
        tuya_grid.addLayout(build_field_stack(tr("Tuya client secret"), self.tuya_client_secret_field), 2, 0, 1, 2)
        tuya_grid.addLayout(build_field_stack(tr("Tuya API endpoint"), self.tuya_endpoint_combo), 3, 0, 1, 2)

        tuya_body = QVBoxLayout()
        tuya_body.setContentsMargins(0, 0, 0, 0)
        tuya_body.setSpacing(0)
        tuya_body.addLayout(tuya_grid)
        self.tuya_section = build_section_card(
            "",
            tuya_body,
            caption=tr("Stores Tuya credentials and endpoint for this profile. The main Tuya tab is visible only when enabled."),
        )

        battery_grid = QGridLayout()
        battery_grid.setContentsMargins(0, 0, 0, 0)
        battery_grid.setHorizontalSpacing(24)
        battery_grid.setVerticalSpacing(14)
        battery_grid.setColumnStretch(0, 1)
        battery_grid.setColumnStretch(1, 1)
        battery_grid.addLayout(build_field_stack(tr("Battery capability (Ah)"), self.battery_capability_field), 0, 0)

        battery_body = QVBoxLayout()
        battery_body.setContentsMargins(0, 0, 0, 0)
        battery_body.setSpacing(0)
        battery_body.addLayout(battery_grid)
        self.battery_section = build_section_card(
            "",
            battery_body,
            caption=tr("Used to estimate battery charge/discharge time on EnergyFlow. Set 0 to disable the estimate."),
        )
        language_grid = QGridLayout()
        language_grid.setContentsMargins(0, 0, 0, 0)
        language_grid.setHorizontalSpacing(24)
        language_grid.setVerticalSpacing(14)
        language_grid.setColumnStretch(0, 1)
        language_grid.addLayout(build_field_stack(tr("Interface language"), self.language_combo), 0, 0)

        language_body = QVBoxLayout()
        language_body.setContentsMargins(0, 0, 0, 0)
        language_body.setSpacing(0)
        language_body.addLayout(language_grid)
        self.language_section = build_section_card(
            "",
            language_body,
            caption=tr("Select app interface language for this profile."),
        )
        tariff_grid = QGridLayout()
        tariff_grid.setContentsMargins(0, 0, 0, 0)
        tariff_grid.setHorizontalSpacing(24)
        tariff_grid.setVerticalSpacing(14)
        tariff_grid.setColumnStretch(0, 1)
        tariff_grid.setColumnStretch(1, 1)
        tariff_grid.addLayout(build_field_stack(tr("Day zone (7-23), UAH/kWh"), self.day_zone_tariff_field), 0, 0)
        tariff_grid.addLayout(build_field_stack(tr("Night zone (23-7), UAH/kWh"), self.night_zone_tariff_field), 0, 1)

        tariff_body = QVBoxLayout()
        tariff_body.setContentsMargins(0, 0, 0, 0)
        tariff_body.setSpacing(0)
        tariff_body.addLayout(tariff_grid)
        self.tariff_section = build_section_card(
            "",
            tariff_body,
            caption=tr("Set electricity tariffs used for day and night cost calculations."),
        )

        self.device_section_note = QLabel(tr("Run Check to load the device list. All available import fields are added automatically."))
        self.device_section_note.setObjectName("SectionCaption")
        self.device_section_note.setWordWrap(True)
        self.device_fields_container = QWidget()
        device_fields_layout = QVBoxLayout(self.device_fields_container)
        device_fields_layout.setContentsMargins(0, 0, 0, 0)
        device_fields_layout.setSpacing(12)
        device_fields_layout.addWidget(self.detected_device_title)
        device_fields_layout.addWidget(self.detected_device)
        device_fields_layout.addWidget(self.detected_device_label)
        device_fields_layout.addWidget(self.import_fields_note)
        device_fields_layout.addWidget(self.edit_inverter_fields_button, 0, Qt.AlignmentFlag.AlignLeft)

        device_body = QVBoxLayout()
        device_body.setContentsMargins(0, 0, 0, 0)
        device_body.setSpacing(12)
        device_body.addWidget(self.device_section_note)
        device_body.addWidget(self.device_fields_container)
        self.device_section = build_section_card(
            "",
            device_body,
            caption=tr("Choose the verified inverter. The app always imports every available field."),
        )

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ok_button = buttons.button(QDialogButtonBox.Ok)
        if self.ok_button is not None:
            self.ok_button.setText(tr("Save"))
            self.ok_button.setObjectName("PrimaryActionButton")
            apply_minimal_button(self.ok_button)
            self.ok_button.setEnabled(False)
        cancel_button = buttons.button(QDialogButtonBox.Cancel)
        if cancel_button is not None:
            cancel_button.setObjectName("SecondaryActionButton")
            apply_minimal_button(cancel_button)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        self.tabs = QTabWidget()
        self.tabs.setObjectName("ProfileTabs")
        self.tabs.setIconSize(QSize(16, 16))
        apply_tab_widget_interaction(self.tabs)

        self.connection_tab = QWidget()
        connection_tab_layout = QVBoxLayout(self.connection_tab)
        connection_tab_layout.setContentsMargins(0, 0, 0, 0)
        connection_tab_layout.setSpacing(16)
        connection_tab_layout.addSpacing(12)
        connection_tab_layout.addWidget(self.connection_section)
        connection_tab_layout.addWidget(self.device_section)
        connection_tab_layout.addStretch(1)

        self.location_tab = QWidget()
        location_tab_layout = QVBoxLayout(self.location_tab)
        location_tab_layout.setContentsMargins(0, 0, 0, 0)
        location_tab_layout.setSpacing(16)
        location_tab_layout.addSpacing(12)
        location_tab_layout.addWidget(self.location_section)
        location_tab_layout.addStretch(1)

        self.sync_tab = QWidget()
        sync_tab_layout = QVBoxLayout(self.sync_tab)
        sync_tab_layout.setContentsMargins(0, 0, 0, 0)
        sync_tab_layout.setSpacing(16)
        sync_tab_layout.addSpacing(12)
        sync_tab_layout.addWidget(self.sync_section)
        sync_tab_layout.addStretch(1)

        self.tuya_tab = QWidget()
        tuya_tab_layout = QVBoxLayout(self.tuya_tab)
        tuya_tab_layout.setContentsMargins(0, 0, 0, 0)
        tuya_tab_layout.setSpacing(16)
        tuya_tab_layout.addSpacing(12)
        tuya_tab_layout.addWidget(self.tuya_section)
        tuya_tab_layout.addStretch(1)

        self.battery_tab = QWidget()
        battery_tab_layout = QVBoxLayout(self.battery_tab)
        battery_tab_layout.setContentsMargins(0, 0, 0, 0)
        battery_tab_layout.setSpacing(16)
        battery_tab_layout.addSpacing(12)
        battery_tab_layout.addWidget(self.battery_section)
        battery_tab_layout.addStretch(1)

        self.language_tab = QWidget()
        language_tab_layout = QVBoxLayout(self.language_tab)
        language_tab_layout.setContentsMargins(0, 0, 0, 0)
        language_tab_layout.setSpacing(16)
        language_tab_layout.addSpacing(12)
        language_tab_layout.addWidget(self.language_section)
        language_tab_layout.addStretch(1)

        self.tariff_tab = QWidget()
        tariff_tab_layout = QVBoxLayout(self.tariff_tab)
        tariff_tab_layout.setContentsMargins(0, 0, 0, 0)
        tariff_tab_layout.setSpacing(16)
        tariff_tab_layout.addSpacing(12)
        tariff_tab_layout.addWidget(self.tariff_section)
        tariff_tab_layout.addStretch(1)

        self.tabs.addTab(self.connection_tab, self._tab_icon("connection"), tr("Connection"))
        self.tabs.addTab(self.location_tab, self._tab_icon("location"), tr("Location"))
        self.tabs.addTab(self.sync_tab, self._tab_icon("sync"), tr("Sync"))
        self.tabs.addTab(self.tuya_tab, self._tab_icon("tuya"), tr("Tuya"))
        self.tabs.addTab(self.battery_tab, self._tab_icon("battery"), tr("Battery"))
        self.tabs.addTab(self.language_tab, self._tab_icon("language"), tr("Language"))
        self.tabs.addTab(self.tariff_tab, self._tab_icon("tariff"), tr("Tariff"))

        layout = QVBoxLayout(self)
        layout.addWidget(create_neon_header_bar(self, self.reject, title=self._dialog_title))
        layout.addSpacing(14)
        layout.addWidget(self.tabs, 1)
        layout.addSpacing(10)
        layout.addWidget(buttons)
        translate_widget_tree(self)

        self._devices: list[DessMonitorDevice] = []
        self._parameter_cache: dict[str, list[str]] = {}
        self._control_field_cache: dict[str, list[DeviceControlFieldProfile]] = {}
        self._available_parameter_keys: list[str] = list(profile.available_parameter_keys or [])
        self._verified_profile: DeviceProfile | None = None
        self._accepted_profile: DeviceProfile | None = None
        self._selected_device: DessMonitorDevice | None = None
        self._location_cache: dict[str, tuple[str, str, str]] = {}
        manual_mode = profile.resolved_inverter_location_mode() == "manual"
        manual_coordinates_text = self._format_coordinates_text(profile.inverter_latitude, profile.inverter_longitude)
        self._manual_location_text = (
            manual_coordinates_text or profile.inverter_location_label if manual_mode else ""
        )
        self._manual_location_description = (
            profile.inverter_location_description.strip()
            or (
                profile.inverter_location_label.strip()
                if manual_mode and manual_coordinates_text and profile.inverter_location_label.strip() != manual_coordinates_text
                else ""
            )
        )
        self._manual_location_description = (
            self._manual_location_description
            if manual_mode
            else ""
        )
        self._manual_location_latitude = profile.inverter_latitude if profile.resolved_inverter_location_mode() == "manual" else ""
        self._manual_location_longitude = profile.inverter_longitude if profile.resolved_inverter_location_mode() == "manual" else ""
        self._auto_location_label = profile.inverter_location_label if profile.resolved_inverter_location_mode() == "auto" else ""
        self._auto_location_latitude = profile.inverter_latitude if profile.resolved_inverter_location_mode() == "auto" else ""
        self._auto_location_longitude = profile.inverter_longitude if profile.resolved_inverter_location_mode() == "auto" else ""
        self._control_fields: list[DeviceControlFieldProfile] = [
            DeviceControlFieldProfile(
                field_id=item.field_id,
                name=item.name,
                prog_number=item.prog_number,
                alias=item.alias,
                comment=item.comment,
                unit=item.unit,
                category=item.category,
                writable=item.writable,
                options=list(item.options or []),
            )
            for item in profile.resolved_inverter_control_fields()
        ]
        self._verify_thread: QThread | None = None
        self._verify_worker: ConnectionCheckWorker | None = None
        self._verification_signature: tuple[str, str, int] | None = None
        self._check_performed_in_session = False
        self.username.textChanged.connect(self._handle_verification_inputs_changed)
        self.company_key.textChanged.connect(self._handle_verification_inputs_changed)
        self.source.currentIndexChanged.connect(self._handle_verification_inputs_changed)
        self.location_field.textEdited.connect(self._handle_manual_location_text_edited)
        self.location_mode.currentIndexChanged.connect(self._apply_location_mode)
        self._apply_styles()
        self._apply_location_mode()
        self._sync_tuya_fields_enabled()
        self._set_verify_button_state("idle")
        self._set_verified_ui_visible(False)
        self.tabs.setCurrentWidget(self.connection_tab)
        self._update_verification_controls()

        if profile.device_label and profile.pn and profile.devcode and profile.devaddr and profile.sn:
            if profile.available_parameter_keys:
                cache_key = f"{profile.devcode}:{profile.sn}:{profile.devaddr}:{profile.pn}"
                self._parameter_cache[cache_key] = list(profile.available_parameter_keys)
                if self._control_fields:
                    self._control_field_cache[cache_key] = [
                        DeviceControlFieldProfile(
                            field_id=item.field_id,
                            name=item.name,
                            prog_number=item.prog_number,
                            alias=item.alias,
                            comment=item.comment,
                            unit=item.unit,
                            category=item.category,
                            writable=item.writable,
                            options=list(item.options or []),
                        )
                        for item in self._control_fields
                    ]
            self._selected_device = DessMonitorDevice(
                pn=profile.pn,
                devcode=profile.devcode,
                devaddr=profile.devaddr,
                sn=profile.sn,
                alias=profile.device_label,
            )
            self._devices = [self._selected_device]
            self.detected_device.blockSignals(True)
            self.detected_device.clear()
            self.detected_device.addItem(profile.device_label)
            self.detected_device.setCurrentIndex(0)
            self.detected_device.blockSignals(False)
            self.detected_device.setEnabled(True)
            cache_key = self._device_cache_key(self._selected_device)
            self._location_cache[cache_key] = (
                profile.inverter_location_label,
                profile.inverter_latitude,
                profile.inverter_longitude,
            )
            self._populate_parameter_keys(profile.available_parameter_keys or [], profile.available_parameter_keys or [])
            self._verified_profile = profile
            self._verification_signature = self._current_verification_signature()
            self._set_verify_button_state("success")
            self._set_verified_ui_visible(True)
            self.verify_status.setText(tr("Connection verified. You can review the device and save."))
            self._apply_location_mode()
            if not self._control_fields:
                try:
                    self._control_fields = self._load_control_fields_for_device(self._selected_device)
                except DessMonitorApiError:
                    LOGGER.exception("Failed to preload inverter control fields for existing profile")
            self._update_verification_controls()
        translate_widget_tree(self)

    def _tab_icon(self, kind: str) -> QIcon:
        pixmap = QPixmap(18, 18)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        accent = QColor("#7dd3fc")
        pen = QPen(accent, 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)

        if kind == "connection":
            painter.drawEllipse(2, 6, 4, 4)
            painter.drawEllipse(12, 2, 4, 4)
            painter.drawEllipse(12, 12, 4, 4)
            painter.drawLine(6, 8, 12, 4)
            painter.drawLine(6, 8, 12, 14)
        elif kind == "location":
            painter.drawEllipse(5, 3, 8, 8)
            painter.drawLine(9, 11, 9, 15)
            painter.drawLine(7, 13, 9, 15)
            painter.drawLine(11, 13, 9, 15)
        elif kind == "sync":
            painter.drawArc(3, 3, 12, 12, 40 * 16, 250 * 16)
            painter.drawLine(13, 4, 15, 4)
            painter.drawLine(15, 4, 15, 6)
            painter.drawArc(3, 3, 12, 12, 220 * 16, 250 * 16)
            painter.drawLine(3, 12, 3, 14)
            painter.drawLine(3, 14, 5, 14)
        elif kind == "tuya":
            painter.drawRoundedRect(3, 3, 12, 12, 3, 3)
            painter.drawLine(6, 1, 6, 3)
            painter.drawLine(9, 1, 9, 3)
            painter.drawLine(12, 1, 12, 3)
            painter.drawLine(6, 15, 6, 17)
            painter.drawLine(9, 15, 9, 17)
            painter.drawLine(12, 15, 12, 17)
        elif kind == "battery":
            painter.drawRoundedRect(3, 5, 11, 8, 2, 2)
            painter.drawRect(14, 7, 2, 4)
            painter.drawLine(6, 9, 11, 9)
            painter.drawLine(8, 7, 8, 11)
        elif kind == "language":
            painter.drawEllipse(3, 3, 12, 12)
            painter.drawLine(3, 9, 15, 9)
            painter.drawLine(9, 3, 9, 15)
        else:
            painter.drawLine(3, 5, 15, 5)
            painter.drawLine(3, 9, 15, 9)
            painter.drawLine(3, 13, 15, 13)
            painter.drawLine(6, 3, 6, 15)
            painter.drawLine(12, 3, 12, 15)
        painter.end()
        return QIcon(pixmap)

    def _show_message(self, icon, title: str, text: str) -> None:
        kind = "info"
        if icon == QMessageBox.Critical:
            kind = "error"
        elif icon == QMessageBox.Warning:
            kind = "warning"
        show_compact_message(self, kind=kind, title=tr(title), text=tr(text))

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            compose_styles(
                """
            #DeviceProfileEditDialog {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #09111f, stop:0.55 #0f172a, stop:1 #11233b);
            }
            #DeviceProfileEditDialog QLabel {
                color: #e2e8f0;
                font-size: 13px;
            }
            QFrame#SectionCard {
                background: transparent;
                border: none;
                border-radius: 0;
            }
            #SectionTitle {
                color: #d9f7ff;
                font-size: 15px;
                font-weight: 700;
            }
            #SectionCaption {
                color: #8fb0cb;
                font-size: 12px;
            }
            #FieldStatus {
                color: #7dd3fc;
                font-size: 12px;
                font-weight: 600;
                padding-left: 2px;
            }
            #FieldHint {
                color: #94a3b8;
                font-size: 12px;
                padding-left: 2px;
            }
            #VerifyStatus {
                color: #94a3b8;
                padding-left: 6px;
            }
            #VerifyStatus[state="success"] {
                color: #99f6e4;
            }
            #VerifyStatus[state="error"] {
                color: #fca5a5;
            }
            #DeviceProfileEditDialog QScrollArea,
            #DeviceProfileEditDialog QWidget#AutoSyncField {
                background: #111827;
                color: #f8fafc;
                border: 1px solid #334155;
                border-radius: 10px;
                padding: 8px 10px;
            }
            #DeviceProfileEditDialog QWidget#AutoSyncField {
                padding: 0;
            }
            #DeviceProfileEditDialog QCheckBox {
                color: #e2e8f0;
                padding: 4px 0;
                font-size: 13px;
            }
            #DeviceProfileEditDialog QCheckBox#AutoSyncCheckbox {
                padding: 0;
                spacing: 10px;
            }
            #DeviceProfileEditDialog QScrollArea {
                background: #111827;
            }
            #DeviceProfileEditDialog QScrollArea > QWidget > QWidget {
                background: transparent;
                color: #f8fafc;
            }
                """,
                tab_widget_qss(
                    "#ProfileTabs",
                    pane_background="transparent",
                    pane_border="none",
                    pane_margin_top=20,
                    tab_padding="8px 16px",
                    tab_min_width=120,
                    tab_radius=10,
                    tab_border="1px solid rgba(71, 85, 105, 0.75)",
                    tab_background="rgba(15, 23, 42, 0.88)",
                    tab_color="#8fb0cb",
                    tab_selected_background="rgba(8, 16, 30, 0.96)",
                    tab_selected_color="#d9f7ff",
                    tab_hover_background="rgba(15, 23, 42, 0.96)",
                    tab_hover_color="#cfefff",
                    tab_border_bottom="1px solid rgba(71, 85, 105, 0.75)",
                ),
                scoped_form_input_combo_qss("#DeviceProfileEditDialog"),
                GLOBAL_BUTTON_QSS,
                NEON_CLOSE_BUTTON_STYLE,
                NEON_HEADER_BAR_STYLE,
            )
        )

    def _set_verified_ui_visible(self, visible: bool) -> None:
        self.device_section_note.setVisible(not visible)
        self.device_fields_container.setVisible(visible)

    def _set_check_in_progress(self, in_progress: bool) -> None:
        self.verify_button.setEnabled(not in_progress)
        self.profile_name.setEnabled(not in_progress)
        self.username.setEnabled(not in_progress)
        self.password.setEnabled(not in_progress)
        self.company_key.setEnabled(not in_progress)
        self.source.setEnabled(not in_progress)
        self.refresh_interval.setEnabled(not in_progress)
        self.location_mode.setEnabled(not in_progress)
        self.location_field.setEnabled((not in_progress) and self.location_mode.currentData() == "manual")
        self.pick_map_button.setEnabled((not in_progress) and self.location_mode.currentData() == "manual")
        self.use_detected_button.setEnabled((not in_progress) and bool(self._auto_location_label))
        self.tuya_enabled_checkbox.setEnabled(not in_progress)
        self.battery_capability_field.setEnabled(not in_progress)
        self.day_zone_tariff_field.setEnabled(not in_progress)
        self.night_zone_tariff_field.setEnabled(not in_progress)
        self.language_combo.setEnabled(not in_progress)
        self._sync_tuya_fields_enabled()
        self.edit_inverter_fields_button.setEnabled(not in_progress)
        self.detected_device.setEnabled(not in_progress and len(self._devices) > 1 and self.detected_device.count() > 0)
        self._update_verification_controls()

    def _current_verification_signature(self) -> tuple[str, str, int]:
        return (
            self.username.text().strip(),
            self.company_key.text().strip(),
            int(self.source.currentData()),
        )

    def _verification_required(self) -> bool:
        if self._verified_profile is None:
            return True
        if self._verification_signature is None:
            return True
        return self._current_verification_signature() != self._verification_signature

    def _update_verification_controls(self) -> None:
        verification_required = self._verification_required()
        if self.verify_button is not None:
            self.verify_button.setVisible(verification_required)
        if self.ok_button is not None:
            self.ok_button.setEnabled(
                self._verify_thread is None
                and self._verified_profile is not None
                and not verification_required
            )

    def _handle_verification_inputs_changed(self, *_args) -> None:
        if self._verify_thread is not None:
            return
        if self._verification_required():
            self._invalidate_verification()
        else:
            self._update_verification_controls()

    def _sync_tuya_fields_enabled(self) -> None:
        enabled = self.tuya_enabled_checkbox.isChecked() and self._verify_thread is None
        self.tuya_client_id_field.setEnabled(enabled)
        self.tuya_client_secret_field.setEnabled(enabled)
        self.tuya_endpoint_combo.setEnabled(enabled)
        self.tuya_poll_interval_field.setEnabled(enabled)

    def _resolved_tuya_poll_interval_sec(self) -> int:
        text = self.tuya_poll_interval_field.text().strip()
        try:
            value = int(text)
        except (TypeError, ValueError):
            return DEFAULT_TUYA_POLL_INTERVAL_SEC
        return max(MIN_TUYA_POLL_INTERVAL_SEC, min(MAX_TUYA_POLL_INTERVAL_SEC, value))

    def _resolved_battery_capability_ah(self) -> float:
        text = self.battery_capability_field.text().strip().replace(",", ".")
        if not text:
            return 0.0
        try:
            value = float(text)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, value)

    def _resolved_day_zone_tariff_uah_per_kwh(self) -> float:
        text = self.day_zone_tariff_field.text().strip().replace(",", ".")
        if not text:
            return 0.0
        try:
            value = float(text)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, value)

    def _resolved_night_zone_tariff_uah_per_kwh(self) -> float:
        text = self.night_zone_tariff_field.text().strip().replace(",", ".")
        if not text:
            return 0.0
        try:
            value = float(text)
        except (TypeError, ValueError):
            return 0.0
        return max(0.0, value)

    def _open_map_picker(self) -> None:
        if str(self.location_mode.currentData() or "auto") != "manual":
            manual_index = self.location_mode.findData("manual")
            if manual_index >= 0:
                self.location_mode.setCurrentIndex(manual_index)
        dialog = MapPickerDialog(
            self,
            location_label=self.location_field.text().strip(),
            latitude=self._manual_location_latitude,
            longitude=self._manual_location_longitude,
        )
        if exec_modal_dialog(dialog, frameless=True, application_modal=False) != int(QDialog.DialogCode.Accepted):
            return
        label, latitude, longitude = dialog.selected_location()
        if not latitude or not longitude:
            return
        coordinates_text = self._format_coordinates_text(latitude, longitude)
        self._manual_location_text = coordinates_text
        self._manual_location_description = label.strip()
        self._manual_location_latitude = latitude.strip()
        self._manual_location_longitude = longitude.strip()
        self.location_field.setText(coordinates_text)
        self.location_status.setText(tr("Manual location from map"))
        self.location_hint.setText(self._manual_location_hint_text())

    @Slot(str)
    def _handle_manual_location_text_edited(self, _text: str) -> None:
        if str(self.location_mode.currentData() or "auto") != "manual":
            return
        self._manual_location_description = ""
        self._manual_location_latitude = ""
        self._manual_location_longitude = ""
        self.location_status.setText(tr("Manual location"))
        if QWebEngineView is not None:
            self.location_hint.setText(tr("Required for weather-based generation forecast. You can type a place or choose it on the map."))
        else:
            self.location_hint.setText(tr("Required for weather-based generation forecast."))

    def _use_detected_location(self) -> None:
        if not self._auto_location_label:
            return
        auto_index = self.location_mode.findData("auto")
        self.location_mode.setCurrentIndex(max(0, auto_index))
        self._apply_location_mode()

    def _device_cache_key(self, device: DessMonitorDevice) -> str:
        return f"{device.devcode}:{device.sn}:{device.devaddr}:{device.pn}"

    def _format_location_label(self, label: str, latitude: str, longitude: str) -> str:
        trimmed_label = label.strip()
        trimmed_lat = latitude.strip()
        trimmed_lon = longitude.strip()
        if trimmed_label:
            return trimmed_label
        if trimmed_lat and trimmed_lon:
            return f"{trimmed_lat}, {trimmed_lon}"
        return ""

    def _format_coordinates_text(self, latitude: str, longitude: str) -> str:
        trimmed_lat = latitude.strip()
        trimmed_lon = longitude.strip()
        if trimmed_lat and trimmed_lon:
            return f"{trimmed_lat}, {trimmed_lon}"
        return ""

    def _normalize_coordinate_input(self, text: str) -> str:
        normalized = unicodedata.normalize("NFKC", text or "")
        cleaned_chars: list[str] = []
        for char in normalized:
            category = unicodedata.category(char)
            if category == "Cf":
                continue
            if category.startswith("C"):
                cleaned_chars.append(" ")
                continue
            if char in {";", "，"}:
                cleaned_chars.append(",")
                continue
            cleaned_chars.append(char)
        return "".join(cleaned_chars).strip()

    def _parse_coordinates_text(self, text: str) -> tuple[str, str]:
        normalized = self._normalize_coordinate_input(text).rstrip(".")
        if not normalized:
            return "", ""
        numbers = re.findall(r"[+-]?\d+(?:\.\d+)?", normalized)
        if len(numbers) < 2:
            return "", ""
        return numbers[0], numbers[1]

    def _manual_location_hint_text(self) -> str:
        description = self._manual_location_description.strip()
        if description:
            return tr_fragment(f"Map address: {description}")
        if QWebEngineView is not None:
            return tr("Required for weather-based generation forecast. You can type coordinates manually or choose a point on the map.")
        return tr("Required for weather-based generation forecast.")

    def _apply_auto_location(self, location: DeviceLocation | None) -> None:
        if location is None:
            self._auto_location_label = ""
            self._auto_location_latitude = ""
            self._auto_location_longitude = ""
            self._apply_location_mode()
            return
        self._auto_location_label = location.formatted_address().strip() or self._format_location_label(
            "",
            location.latitude,
            location.longitude,
        )
        self._auto_location_latitude = location.latitude.strip()
        self._auto_location_longitude = location.longitude.strip()
        self._apply_location_mode()

    def _apply_location_mode(self, *_args) -> None:
        mode = str(self.location_mode.currentData() or "auto")
        if mode == "manual":
            if self.location_field.text().strip() != self._auto_location_label.strip():
                self._manual_location_text = self.location_field.text().strip()
            self.location_field.setReadOnly(False)
            self.location_field.setEnabled(self._verify_thread is None)
            self.pick_map_button.setEnabled(self._verify_thread is None and QWebEngineView is not None)
            self.use_detected_button.setEnabled(bool(self._auto_location_label))
            self.location_field.setText(self._manual_location_text)
            self.location_field.setPlaceholderText(tr("Latitude, longitude"))
            self.location_status.setText(tr("Manual location"))
            if self._manual_location_latitude and self._manual_location_longitude:
                self.location_hint.setText(self._manual_location_hint_text())
            elif QWebEngineView is not None:
                self.location_hint.setText(tr("Required for weather-based generation forecast. You can type coordinates manually or choose a point on the map."))
            else:
                self.location_hint.setText(tr("Required for weather-based generation forecast."))
            return

        self._manual_location_text = self.location_field.text().strip()
        self.location_field.setReadOnly(True)
        self.location_field.setEnabled(False)
        self.pick_map_button.setEnabled(False)
        self.use_detected_button.setEnabled(bool(self._auto_location_label))
        self.location_field.setText(self._auto_location_label)
        self.location_field.setPlaceholderText(tr("Run Check to load location from DessMonitor"))
        if self._auto_location_label:
            self.location_status.setText(tr("Loaded from DessMonitor"))
            if self._auto_location_latitude and self._auto_location_longitude:
                self.location_hint.setText(tr("Location loaded automatically from DessMonitor."))
            else:
                self.location_hint.setText(tr("Location loaded automatically from DessMonitor."))
        else:
            self.location_status.setText(tr("Waiting for auto location"))
            self.location_hint.setText(tr("Required. If DessMonitor does not return it, switch to manual mode."))

    def _set_verify_status(self, text: str, state: str = "idle") -> None:
        self.verify_status.setProperty("state", state)
        self.verify_status.setText(text)
        self.verify_status.style().unpolish(self.verify_status)
        self.verify_status.style().polish(self.verify_status)
        self.verify_status.update()

    def _set_verify_button_state(self, state: str) -> None:
        labels = {
            "idle": tr("Check"),
            "pending": tr("Checking..."),
            "success": tr("Verified"),
            "error": tr("Try again"),
        }
        self.verify_button.setProperty("state", state)
        self.verify_button.setText(labels.get(state, tr("Check connection")))
        self.verify_button.style().unpolish(self.verify_button)
        self.verify_button.style().polish(self.verify_button)
        self.verify_button.update()

    def _auth_config(self) -> DessMonitorConfig:
        return DessMonitorConfig(
            username=self.username.text().strip(),
            password=self.password.text(),
            company_key=self.company_key.text().strip(),
            source=int(self.source.currentData()),
            pn="",
            devcode="",
            devaddr="",
            sn="",
            device_label="",
            parameter_keys=[],
            date_from="2000-01-01",
            date_to="2000-01-01",
        )

    def _clear_parameter_layout(self) -> None:
        self._available_parameter_keys = []

    def _populate_parameter_keys(self, available: list[str], selected: list[str]) -> None:
        del selected
        self._available_parameter_keys = [key.strip() for key in available if key.strip()]
        if self._available_parameter_keys:
            self.import_fields_note.setText(
                tr_fragment(
                    f"All available import fields will be imported automatically ({len(self._available_parameter_keys)} loaded)."
                )
            )
        else:
            self.import_fields_note.setText(tr("All available import fields will be imported automatically."))

    def _selected_parameter_keys(self) -> list[str]:
        return list(self._available_parameter_keys)

    def _set_fields_expanded(self, expanded: bool) -> None:
        del expanded

    def _invalidate_verification(self, *_args) -> None:
        if self._verify_thread is not None:
            return
        self._verified_profile = None
        self._devices = []
        self._selected_device = None
        self.detected_device.clear()
        self.detected_device.setEnabled(False)
        self._populate_parameter_keys([], [])
        self._set_verified_ui_visible(False)
        self._set_verify_button_state("idle")
        self._set_verify_status(tr("Step 1. Fill in the credentials and run Check."))
        self._update_verification_controls()

    def _load_keys_for_device(self, device: DessMonitorDevice) -> list[str]:
        cache_key = self._device_cache_key(device)
        if cache_key in self._parameter_cache:
            return self._parameter_cache[cache_key]
        keys = fetch_key_parameters(self._auth_config(), devcode=device.devcode, logger=None)
        self._parameter_cache[cache_key] = keys
        return keys

    def _load_control_fields_for_device(self, device: DessMonitorDevice) -> list[DeviceControlFieldProfile]:
        cache_key = self._device_cache_key(device)
        if cache_key in self._control_field_cache:
            cached_fields = self._control_field_cache[cache_key]
            if cached_fields:
                return [
                    DeviceControlFieldProfile(
                        field_id=item.field_id,
                        name=item.name,
                        prog_number=item.prog_number,
                        alias=item.alias,
                        comment=item.comment,
                        unit=item.unit,
                        category=item.category,
                        writable=item.writable,
                        options=list(item.options or []),
                    )
                    for item in cached_fields
                ]
        fields = fetch_device_control_fields(
            DessMonitorConfig(
                username=self.username.text().strip(),
                password=self.password.text(),
                company_key=self.company_key.text().strip(),
                source=int(self.source.currentData()),
                pn=device.pn,
                devcode=device.devcode,
                devaddr=device.devaddr,
                sn=device.sn,
                device_label=device.display_name,
                parameter_keys=[],
                date_from="2000-01-01",
                date_to="2000-01-01",
            ),
            logger=None,
        )
        merged = self._merge_control_fields(fields, self._control_fields)
        self._control_field_cache[cache_key] = [
            DeviceControlFieldProfile(
                field_id=item.field_id,
                name=item.name,
                prog_number=item.prog_number,
                alias=item.alias,
                comment=item.comment,
                unit=item.unit,
                category=item.category,
                writable=item.writable,
                options=list(item.options or []),
            )
            for item in merged
        ]
        return merged

    def _merge_control_fields(
        self,
        api_fields: list[DeviceControlField],
        existing_fields: list[DeviceControlFieldProfile],
    ) -> list[DeviceControlFieldProfile]:
        existing_by_id = {item.field_id: item for item in existing_fields if item.field_id}
        merged: list[DeviceControlFieldProfile] = []
        for field in api_fields:
            existing = existing_by_id.get(field.field_id)
            merged.append(
                DeviceControlFieldProfile(
                    field_id=field.field_id,
                    name=field.name,
                    prog_number=existing.prog_number if existing else "",
                    alias=existing.alias if existing else "",
                    comment=existing.comment if existing else "",
                    unit=field.unit,
                    category=field.category,
                    writable=field.writable,
                    options=list(field.options or []),
                )
            )
        return merged

    def _apply_device_selection(self, index: int) -> None:
        if index < 0 or index >= len(self._devices):
            return
        self._selected_device = self._devices[index]
        try:
            keys = self._load_keys_for_device(self._selected_device)
            self._control_fields = self._load_control_fields_for_device(self._selected_device)
        except DessMonitorApiError as exc:
            self._show_message(QMessageBox.Critical, "Field loading failed", humanize_error_text(str(exc)))
            return
        self._populate_parameter_keys(keys, keys)
        cached_location = self._location_cache.get(self._device_cache_key(self._selected_device))
        if cached_location is not None:
            self._auto_location_label, self._auto_location_latitude, self._auto_location_longitude = cached_location
        elif self.location_mode.currentData() == "auto":
            self._auto_location_label = ""
            self._auto_location_latitude = ""
            self._auto_location_longitude = ""
        self._apply_location_mode()
        self._verified_profile = DeviceProfile(
            profile_name=self.profile_name.text().strip(),
            username=self.username.text().strip(),
            password=self.password.text(),
            company_key=self.company_key.text().strip(),
            source=int(self.source.currentData()),
            pn=self._selected_device.pn,
            devcode=self._selected_device.devcode,
            devaddr=self._selected_device.devaddr,
            sn=self._selected_device.sn,
            device_label=self._selected_device.display_name,
            energyflow_refresh_seconds=int(self.refresh_interval.currentData()),
            auto_sync_enabled=self.auto_sync_checkbox.isChecked(),
            available_parameter_keys=keys,
            selected_parameter_keys=keys,
            inverter_control_fields=[
                DeviceControlFieldProfile(
                    field_id=item.field_id,
                    name=item.name,
                    prog_number=item.prog_number,
                    alias=item.alias,
                    comment=item.comment,
                    unit=item.unit,
                    category=item.category,
                    writable=item.writable,
                    options=list(item.options or []),
                )
                for item in self._control_fields
            ],
            inverter_location_mode=self._effective_location_mode(),
            inverter_location_label=self._resolved_location_label(),
            inverter_location_description=self._resolved_location_description(),
            inverter_latitude=self._resolved_location_latitude(),
            inverter_longitude=self._resolved_location_longitude(),
            battery_capability_ah=self._resolved_battery_capability_ah(),
            ui_language=str(self.language_combo.currentData() or DEFAULT_UI_LANGUAGE).strip().lower() or DEFAULT_UI_LANGUAGE,
            day_zone_tariff_uah_per_kwh=self._resolved_day_zone_tariff_uah_per_kwh(),
            night_zone_tariff_uah_per_kwh=self._resolved_night_zone_tariff_uah_per_kwh(),
            automation_cooldown_sec=int(self.automation_cooldown.value()),
        )
        if self.ok_button is not None:
            self.ok_button.setEnabled(True)

    def verify_connection(self) -> None:
        if self._verify_thread is not None:
            return
        if not self._verification_required():
            return
        required = {
            tr("Profile name"): self.profile_name.text().strip(),
            tr("Username"): self.username.text().strip(),
            tr("Password"): self.password.text(),
        }
        missing = [label for label, value in required.items() if not value]
        if missing:
            self._show_message(QMessageBox.Warning, "Missing fields", tr_fragment(f"Please fill in: {', '.join(missing)}."))
            return

        self._set_verify_button_state("pending")
        self._set_verify_status(tr("Checking connection..."))
        self._set_check_in_progress(True)

        self._verify_thread = QThread(self)
        self._verify_worker = ConnectionCheckWorker(self._auth_config(), self._selected_device)
        self._verify_worker.moveToThread(self._verify_thread)
        self._verify_thread.started.connect(self._verify_worker.run)
        self._verify_worker.progress.connect(self._on_verify_progress)
        self._verify_worker.success.connect(self._on_verify_success)
        self._verify_worker.failure.connect(self._on_verify_failure)
        self._verify_worker.finished.connect(self._verify_thread.quit)
        self._verify_worker.finished.connect(self._verify_worker.deleteLater)
        self._verify_thread.finished.connect(self._verify_thread.deleteLater)
        self._verify_thread.finished.connect(self._on_verify_finished)
        self._verify_thread.start()

    @Slot(str)
    def _on_verify_progress(self, message: str) -> None:
        self._set_verify_status(message)

    @Slot(object, object, object, object, object)
    def _on_verify_success(self, devices_obj, selected_device_obj, keys_obj, control_fields_obj, location_obj) -> None:
        self._check_performed_in_session = True
        devices = list(devices_obj)
        selected_device = selected_device_obj
        keys = list(keys_obj)
        control_fields = list(control_fields_obj)
        location = location_obj if isinstance(location_obj, DeviceLocation) else None
        cache_key = self._device_cache_key(selected_device)
        self._parameter_cache[cache_key] = keys
        if location is not None:
            location_label = location.formatted_address().strip() or self._format_location_label(
                "",
                location.latitude,
                location.longitude,
            )
            self._location_cache[cache_key] = (
                location_label,
                location.latitude.strip(),
                location.longitude.strip(),
            )
        self._control_fields = self._merge_control_fields(control_fields, self._control_fields)
        self._control_field_cache[cache_key] = [
            DeviceControlFieldProfile(
                field_id=item.field_id,
                name=item.name,
                prog_number=item.prog_number,
                alias=item.alias,
                comment=item.comment,
                unit=item.unit,
                category=item.category,
                writable=item.writable,
                options=list(item.options or []),
            )
            for item in self._control_fields
        ]
        self._devices = devices
        self.detected_device.clear()
        for device in devices:
            self.detected_device.addItem(device.display_name)
        selected_index = max(
            0,
            next(
                (
                    index
                    for index, device in enumerate(devices)
                    if (device.pn, device.devcode, device.devaddr, device.sn)
                    == (selected_device.pn, selected_device.devcode, selected_device.devaddr, selected_device.sn)
                ),
                0,
            ),
        )
        self.detected_device.setCurrentIndex(selected_index)
        self._selected_device = devices[selected_index]
        self._apply_auto_location(location)
        self._populate_parameter_keys(keys, keys)
        self._set_verified_ui_visible(True)
        self._set_verify_button_state("success")
        self.tabs.setCurrentWidget(self.connection_tab)
        self._set_verify_status(tr("Connection verified. Review the device and save."), "success")
        self._apply_location_mode()
        self._verification_signature = self._current_verification_signature()
        self._verified_profile = DeviceProfile(
            profile_name=self.profile_name.text().strip(),
            username=self.username.text().strip(),
            password=self.password.text(),
            company_key=self.company_key.text().strip(),
            source=int(self.source.currentData()),
            pn=self._selected_device.pn,
            devcode=self._selected_device.devcode,
            devaddr=self._selected_device.devaddr,
            sn=self._selected_device.sn,
            device_label=self._selected_device.display_name,
            energyflow_refresh_seconds=int(self.refresh_interval.currentData()),
            auto_sync_enabled=self.auto_sync_checkbox.isChecked(),
            available_parameter_keys=keys,
            selected_parameter_keys=keys,
            inverter_control_fields=[
                DeviceControlFieldProfile(
                    field_id=item.field_id,
                    name=item.name,
                    prog_number=item.prog_number,
                    alias=item.alias,
                    comment=item.comment,
                    unit=item.unit,
                    category=item.category,
                    writable=item.writable,
                    options=list(item.options or []),
                )
                for item in self._control_fields
            ],
            inverter_location_mode=self._effective_location_mode(),
            inverter_location_label=self._resolved_location_label(),
            inverter_location_description=self._resolved_location_description(),
            inverter_latitude=self._resolved_location_latitude(),
            inverter_longitude=self._resolved_location_longitude(),
            battery_capability_ah=self._resolved_battery_capability_ah(),
            ui_language=str(self.language_combo.currentData() or DEFAULT_UI_LANGUAGE).strip().lower() or DEFAULT_UI_LANGUAGE,
            day_zone_tariff_uah_per_kwh=self._resolved_day_zone_tariff_uah_per_kwh(),
            night_zone_tariff_uah_per_kwh=self._resolved_night_zone_tariff_uah_per_kwh(),
            automation_cooldown_sec=int(self.automation_cooldown.value()),
        )

    @Slot(str)
    def _on_verify_failure(self, message: str) -> None:
        self._verified_profile = None
        self._devices = []
        self._selected_device = None
        self.detected_device.clear()
        self.detected_device.setEnabled(False)
        self._populate_parameter_keys([], [])
        self._set_verified_ui_visible(False)
        self._set_verify_button_state("error")
        self._set_verify_status(message, "error")
        self._update_verification_controls()

    @Slot()
    def _on_verify_finished(self) -> None:
        self._verify_worker = None
        self._verify_thread = None
        self._set_check_in_progress(False)

    def _resolved_location_label(self) -> str:
        mode = self._effective_location_mode()
        if mode == "manual":
            parsed_latitude, parsed_longitude = self._parse_coordinates_text(self.location_field.text())
            if parsed_latitude and parsed_longitude:
                return self._format_coordinates_text(parsed_latitude, parsed_longitude)
            return self.location_field.text().strip()
        return self._auto_location_label.strip()

    def _resolved_location_description(self) -> str:
        mode = self._effective_location_mode()
        if mode == "manual":
            return self._manual_location_description.strip()
        return ""

    def _resolved_location_latitude(self) -> str:
        mode = self._effective_location_mode()
        if mode == "manual":
            parsed_latitude, parsed_longitude = self._parse_coordinates_text(self.location_field.text())
            if parsed_latitude and parsed_longitude:
                return parsed_latitude
            return self._manual_location_latitude.strip()
        return self._auto_location_latitude.strip()

    def _resolved_location_longitude(self) -> str:
        mode = self._effective_location_mode()
        if mode == "manual":
            parsed_latitude, parsed_longitude = self._parse_coordinates_text(self.location_field.text())
            if parsed_latitude and parsed_longitude:
                return parsed_longitude
            return self._manual_location_longitude.strip()
        return self._auto_location_longitude.strip()

    def _effective_location_mode(self) -> str:
        explicit_mode = str(self.location_mode.currentData() or "auto")
        manual_label = self.location_field.text().strip() or self._manual_location_text.strip()
        parsed_latitude, parsed_longitude = self._parse_coordinates_text(manual_label)
        has_manual_coordinates = bool(
            (self._manual_location_latitude.strip() and self._manual_location_longitude.strip())
            or (parsed_latitude and parsed_longitude)
        )
        if explicit_mode == "manual":
            return "manual"
        if has_manual_coordinates:
            return "manual"
        if manual_label and manual_label != self._auto_location_label.strip():
            return "manual"
        return "auto"

    def profile(self) -> DeviceProfile:
        if self.result() == QDialog.DialogCode.Accepted and self._accepted_profile is not None:
            return self._accepted_profile
        base = self._verified_profile or DeviceProfile(
            "",
            "",
            "",
            "",
            1,
            "",
            "",
            "",
            "",
            "",
            DEFAULT_ENERGYFLOW_REFRESH_SECONDS,
            False,
            [],
            [],
            [],
        )
        return DeviceProfile(
            profile_name=self.profile_name.text().strip(),
            username=self.username.text().strip(),
            password=self.password.text(),
            company_key=self.company_key.text().strip(),
            source=int(self.source.currentData()),
            pn=base.pn,
            devcode=base.devcode,
            devaddr=base.devaddr,
            sn=base.sn,
            device_label=base.device_label,
            energyflow_refresh_seconds=int(self.refresh_interval.currentData()),
            auto_sync_enabled=self.auto_sync_checkbox.isChecked(),
            available_parameter_keys=list(base.available_parameter_keys or []),
            selected_parameter_keys=list(base.available_parameter_keys or []),
            inverter_control_fields=[
                DeviceControlFieldProfile(
                    field_id=item.field_id,
                    name=item.name,
                    prog_number=item.prog_number,
                    alias=item.alias,
                    comment=item.comment,
                    unit=item.unit,
                    category=item.category,
                    writable=item.writable,
                    options=list(item.options or []),
                )
                for item in self._control_fields
            ],
            inverter_location_mode=self._effective_location_mode(),
            inverter_location_label=self._resolved_location_label(),
            inverter_location_description=self._resolved_location_description(),
            inverter_latitude=self._resolved_location_latitude(),
            inverter_longitude=self._resolved_location_longitude(),
            tuya_enabled=self.tuya_enabled_checkbox.isChecked(),
            tuya_client_id=self.tuya_client_id_field.text().strip(),
            tuya_client_secret=self.tuya_client_secret_field.text().strip(),
            tuya_endpoint=str(self.tuya_endpoint_combo.currentData() or DEFAULT_TUYA_ENDPOINT).strip(),
            tuya_poll_interval_sec=self._resolved_tuya_poll_interval_sec(),
            battery_capability_ah=self._resolved_battery_capability_ah(),
            ui_language=str(self.language_combo.currentData() or DEFAULT_UI_LANGUAGE).strip().lower() or DEFAULT_UI_LANGUAGE,
            day_zone_tariff_uah_per_kwh=self._resolved_day_zone_tariff_uah_per_kwh(),
            night_zone_tariff_uah_per_kwh=self._resolved_night_zone_tariff_uah_per_kwh(),
            automation_cooldown_sec=int(self.automation_cooldown.value()),
        )

    def check_performed_in_session(self) -> bool:
        return self._check_performed_in_session

    def accept(self) -> None:
        if self._verified_profile is None or self._verification_required():
            self._show_message(QMessageBox.Warning, "Verify first", tr("Verify the API connection before saving the profile."))
            return
        profile = self.profile()
        if profile.resolved_inverter_location_mode() == "auto" and not profile.inverter_location_label.strip():
            self._show_message(
                QMessageBox.Warning,
                "Location required",
                tr("DessMonitor did not return the inverter location. Switch Location source to manual and enter it."),
            )
            return
        if profile.resolved_inverter_location_mode() == "manual" and not profile.inverter_location_label.strip():
            self._show_message(QMessageBox.Warning, "Location required", tr("Enter the inverter location before saving."))
            return
        if not profile.selected_parameter_keys:
            self._show_message(
                QMessageBox.Warning,
                "No import fields",
                tr("DessMonitor did not return any import fields for this device. Run Check again and verify the profile."),
            )
            return
        if profile.tuya_enabled and (not profile.tuya_client_id or not profile.tuya_client_secret):
            self._show_message(
                QMessageBox.Warning,
                "Tuya settings incomplete",
                tr("Fill in Tuya client ID and Tuya client secret, or disable Tuya for this profile."),
            )
            self.tabs.setCurrentWidget(self.tuya_tab)
            return
        if self._persist_on_accept:
            existing_profiles = load_device_profiles()
            existing_profiles = [
                item
                for item in existing_profiles
                if item.profile_name not in {self._original_profile_name, profile.profile_name}
            ]
            existing_profiles.append(profile)
            save_device_profiles(existing_profiles)
        self._accepted_profile = profile
        super().accept()
        self.profile_saved.emit(profile)

    def _open_inverter_fields_dialog(self) -> None:
        if not self._control_fields:
            base = self._verified_profile
            if base is None or not all((base.pn, base.devcode, base.devaddr, base.sn)):
                self._show_message(
                    QMessageBox.Information,
                    "No inverter fields",
                    "Run Check first to read the inverter control fields from DessMonitor.",
                )
                return
            try:
                self._control_fields = self._load_control_fields_for_device(
                    DessMonitorDevice(
                        pn=base.pn,
                        devcode=base.devcode,
                        devaddr=base.devaddr,
                        sn=base.sn,
                        alias=base.device_label,
                    )
                )
            except DessMonitorApiError as exc:
                self._show_message(QMessageBox.Critical, "Field loading failed", humanize_error_text(str(exc)))
                return
            if not self._control_fields:
                self._show_message(
                    QMessageBox.Information,
                    "No inverter fields",
                    "DessMonitor did not return any inverter fields for this profile.",
                )
                return
        dialog = InverterControlFieldsDialog(self, self._control_fields)
        if exec_modal_dialog(dialog, frameless=True, application_modal=False) != int(dialog.DialogCode.Accepted):
            return
        self._control_fields = dialog.fields()
        if self._verified_profile is not None:
            self._verified_profile = self.profile()


class DeviceProfileListCard(QFrame):
    """Visual card shown inside the profile selection list."""
    def __init__(self, profile: DeviceProfile, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("DeviceProfileListCard")
        self.setProperty("selected", False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)

        title = QLabel(profile.profile_name or tr("Unnamed profile"))
        title.setObjectName("DeviceProfileListTitle")
        title.setWordWrap(True)

        pn_label = QLabel(f"{tr('PN')}: {profile.pn or '-'}")
        pn_label.setObjectName("DeviceProfileListMeta")
        address_label = QLabel(f"{tr('Address')}: {profile.devaddr or '-'}")
        address_label.setObjectName("DeviceProfileListMeta")
        protocol_label = QLabel(f"{tr('Protocol code')}: {profile.devcode or '-'}")
        protocol_label.setObjectName("DeviceProfileListMeta")

        layout.addWidget(title)
        layout.addWidget(pn_label)
        layout.addWidget(address_label)
        layout.addWidget(protocol_label)

    def set_selected(self, selected: bool) -> None:
        """Toggle custom `selected` dynamic property and refresh QSS state."""
        # We style cards as custom QFrame widgets, so native QListWidget
        # selection highlighting is not used. Re-polish applies property-based
        # selectors like QFrame#DeviceProfileListCard[selected="true"].
        self.setProperty("selected", selected)
        self.style().unpolish(self)
        self.style().polish(self)
        self.update()


class InverterControlFieldsDialog(QDialog):
    """Editor dialog for inverter field aliases/comments imported from API."""
    def __init__(self, parent, fields: list[DeviceControlFieldProfile]) -> None:
        super().__init__(parent)
        self.setWindowTitle("Inverter Fields")
        self.setModal(True)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setObjectName("InverterControlFieldsDialog")
        self.resize(860, 520)
        self._fields = [
            DeviceControlFieldProfile(
                field_id=item.field_id,
                name=item.name,
                prog_number=item.prog_number,
                alias=item.alias,
                comment=item.comment,
                unit=item.unit,
                category=item.category,
                writable=item.writable,
                options=list(item.options or []),
            )
            for item in fields
        ]

        self.table = QTableWidget(len(self._fields), 5)
        self.table.setObjectName("InverterFieldsTable")
        self.table.setItemDelegate(InverterFieldsItemDelegate(self.table))
        self.table.setHorizontalHeaderLabels(["Prog number", "Key", "Name", "Replacement name", "Comment"])
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(52)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setShowGrid(True)
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked
            | QTableWidget.EditTrigger.EditKeyPressed
            | QTableWidget.EditTrigger.SelectedClicked
        )
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, header.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, header.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, header.ResizeMode.Stretch)
        header.setSectionResizeMode(3, header.ResizeMode.Stretch)
        header.setSectionResizeMode(4, header.ResizeMode.Stretch)
        self._populate_table()

        table_frame = QFrame()
        table_frame.setObjectName("InverterFieldsTableFrame")
        table_layout = QVBoxLayout(table_frame)
        table_layout.setContentsMargins(0, 0, 0, 0)
        table_layout.addWidget(self.table)

        actions = QWidget()
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(8)
        self.import_button = QPushButton("Import Excel")
        self.import_button.setObjectName("SecondaryActionButton")
        self.import_button.clicked.connect(self._import_from_excel)
        self.export_button = QPushButton("Export Excel")
        self.export_button.setObjectName("SecondaryActionButton")
        self.export_button.clicked.connect(self._export_to_excel)
        actions_layout.addWidget(self.import_button)
        actions_layout.addWidget(self.export_button)
        actions_layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_button = buttons.button(QDialogButtonBox.Ok)
        if ok_button is not None:
            ok_button.setText("Apply")
            ok_button.setObjectName("PrimaryActionButton")
        cancel_button = buttons.button(QDialogButtonBox.Cancel)
        if cancel_button is not None:
            cancel_button.setObjectName("SecondaryActionButton")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(14)
        layout.addWidget(create_neon_header_bar(self, self.reject, title="Inverter Fields"))
        layout.addWidget(table_frame, stretch=1)
        layout.addWidget(actions)
        layout.addWidget(buttons)
        self._apply_styles()

    @staticmethod
    def _normalize_prog_number(value: object) -> str:
        text = str(value).strip()
        if not text:
            return ""
        if re.fullmatch(r"\d+\.0+", text):
            return text.split(".", 1)[0]
        return text

    @staticmethod
    def _saved_prog_number(value: str) -> str:
        normalized = InverterControlFieldsDialog._normalize_prog_number(value)
        return normalized or "xx"

    def _populate_table(self) -> None:
        """Fill table from current in-memory field profile list."""
        for row, field in enumerate(self._fields):
            index_value = field.prog_number.strip()
            index_item = QTableWidgetItem(index_value)
            self.table.setItem(row, 0, index_item)

            key_item = QTableWidgetItem(field.field_id)
            key_item.setFlags(key_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 1, key_item)

            name_item = QTableWidgetItem(field.name)
            self.table.setItem(row, 2, name_item)

            alias_item = QTableWidgetItem(field.alias)
            self.table.setItem(row, 3, alias_item)

            comment_item = QTableWidgetItem(field.comment)
            self.table.setItem(row, 4, comment_item)

    def _export_to_excel(self) -> None:
        default_name = "inverter_fields.xlsx"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "Export Inverter Fields to Excel",
            str(Path.home() / default_name),
            "Excel Files (*.xlsx)",
        )
        if not file_path:
            return
        if not file_path.lower().endswith(".xlsx"):
            file_path = f"{file_path}.xlsx"
        dataframe = pd.DataFrame(
            [
                {
                    "Prog number": self._normalize_prog_number(self.table.item(row, 0).text())
                    if self.table.item(row, 0)
                    else "",
                    "Key": field.field_id,
                    "Name": self.table.item(row, 2).text().strip() if self.table.item(row, 2) else field.name,
                    "Replacement name": self.table.item(row, 3).text().strip() if self.table.item(row, 3) else "",
                    "Comment": self.table.item(row, 4).text().strip() if self.table.item(row, 4) else "",
                }
                for row, field in enumerate(self._fields)
            ]
        )
        try:
            dataframe.to_excel(file_path, index=False)
        except Exception as exc:  # pragma: no cover - filesystem/engine issues
            LOGGER.exception("Failed to export inverter fields to Excel: %s", file_path)
            self._show_message("error", "Export failed", humanize_error_text(str(exc)))
            return
        self._show_message("info", "Export complete", f"Saved inverter fields to {Path(file_path).name}.")

    def _import_from_excel(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Import Inverter Fields from Excel",
            str(Path.home()),
            "Excel Files (*.xlsx)",
        )
        if not file_path:
            return
        try:
            dataframe = pd.read_excel(file_path, engine="openpyxl").fillna("")
        except Exception as exc:  # pragma: no cover - filesystem/engine issues
            LOGGER.exception("Failed to import inverter fields from Excel: %s", file_path)
            self._show_message("error", "Import failed", humanize_error_text(str(exc)))
            return

        required_columns = {"Prog number", "Key", "Name", "Replacement name", "Comment"}
        missing_columns = [column for column in required_columns if column not in dataframe.columns]
        if missing_columns:
            self._show_message(
                "warning",
                "Invalid file",
                f"Missing column(s): {', '.join(missing_columns)}.",
            )
            return

        imported_by_key = {
            str(row["Key"]).strip(): row
            for _, row in dataframe.iterrows()
            if str(row["Key"]).strip()
        }
        updated_rows = 0
        for row_index, field in enumerate(self._fields):
            imported = imported_by_key.get(field.field_id)
            if imported is None:
                continue
            prog_item = self.table.item(row_index, 0)
            name_item = self.table.item(row_index, 2)
            alias_item = self.table.item(row_index, 3)
            comment_item = self.table.item(row_index, 4)
            if prog_item is not None:
                prog_item.setText(self._normalize_prog_number(imported["Prog number"]))
            if name_item is not None:
                name_item.setText(str(imported["Name"]).strip())
            if alias_item is not None:
                alias_item.setText(str(imported["Replacement name"]).strip())
            if comment_item is not None:
                comment_item.setText(str(imported["Comment"]).strip())
            updated_rows += 1

        self._show_message("info", "Import complete", f"Updated {updated_rows} inverter field row(s).")

    def fields(self) -> list[DeviceControlFieldProfile]:
        """Collect edited values back into field profile dataclasses."""
        updated: list[DeviceControlFieldProfile] = []
        for row, field in enumerate(self._fields):
            prog_item = self.table.item(row, 0)
            name_item = self.table.item(row, 2)
            alias_item = self.table.item(row, 3)
            comment_item = self.table.item(row, 4)
            updated.append(
                DeviceControlFieldProfile(
                    field_id=field.field_id,
                    name=name_item.text().strip() if name_item is not None else field.name,
                    prog_number=self._saved_prog_number(prog_item.text() if prog_item is not None else ""),
                    alias=alias_item.text().strip() if alias_item is not None else "",
                    comment=comment_item.text().strip() if comment_item is not None else "",
                    unit=field.unit,
                    category=field.category,
                    writable=field.writable,
                    options=list(field.options or []),
                )
            )
        return updated

    def _show_message(self, kind: str, title: str, text: str) -> None:
        show_compact_message(self, kind=kind, title=title, text=text)

    def _apply_styles(self) -> None:
        self.setStyleSheet(
            compose_styles(
                """
            #InverterControlFieldsDialog {
                background: qlineargradient(x1:0, y1:0, x2:1, y2:1,
                    stop:0 #09111f, stop:0.55 #0f172a, stop:1 #11233b);
            }
            #InverterControlFieldsDialog QLabel {
                color: #e2e8f0;
                font-size: 14px;
            }
            #InverterControlFieldsDialog QLabel#InverterFieldsIntro {
                color: #cbd5e1;
                font-size: 15px;
                padding: 4px 2px 8px 2px;
            }
            #InverterControlFieldsDialog QFrame#InverterFieldsTableFrame {
                background: rgba(9, 17, 31, 0.88);
                border: 1px solid #31415f;
                border-radius: 16px;
                padding: 0;
            }
            #InverterControlFieldsDialog QTableWidget#InverterFieldsTable {
                background: transparent;
                color: #e2e8f0;
                font-size: 12px;
                alternate-background-color: rgba(20, 32, 54, 0.78);
                gridline-color: #223247;
                border: none;
                border-radius: 16px;
                selection-background-color: rgba(56, 189, 248, 0.16);
                selection-color: #f8fafc;
                outline: 0;
            }
            #InverterControlFieldsDialog QTableWidget#InverterFieldsTable::item {
                padding: 10px 12px;
                border-bottom: 1px solid rgba(51, 65, 85, 0.5);
            }
            #InverterControlFieldsDialog QTableWidget#InverterFieldsTable::item:selected {
                background: rgba(56, 189, 248, 0.16);
                color: #f8fafc;
                border-top: 1px solid rgba(34, 211, 238, 0.95);
                border-bottom: 1px solid rgba(34, 211, 238, 0.95);
            }
            #InverterControlFieldsDialog QTableWidget#InverterFieldsTable QLineEdit {
                background: #0f172a;
                color: #f8fafc;
                font-size: 12px;
                border: 1px solid #38bdf8;
                border-radius: 8px;
                padding: 6px 10px;
                selection-background-color: rgba(56, 189, 248, 0.35);
                selection-color: #f8fafc;
            }
            #InverterControlFieldsDialog QHeaderView::section {
                background: #0b1220;
                color: #cbd5e1;
                border: none;
                border-bottom: 1px solid #334155;
                padding: 14px 12px;
                font-size: 12px;
                font-weight: 600;
            }
            #InverterControlFieldsDialog QTableCornerButton::section {
                background: #0b1220;
                border: none;
                border-bottom: 1px solid #334155;
                border-right: 1px solid #223247;
            }
            #InverterControlFieldsDialog QScrollBar:vertical {
                background: rgba(15, 23, 42, 0.96);
                width: 14px;
                margin: 12px 4px 12px 0;
                border-radius: 7px;
                border: 1px solid #334155;
            }
            #InverterControlFieldsDialog QScrollBar::handle:vertical {
                background: rgba(56, 189, 248, 0.35);
                min-height: 32px;
                border-radius: 6px;
                border: 1px solid rgba(125, 211, 252, 0.4);
            }
            #InverterControlFieldsDialog QScrollBar::add-line:vertical,
            #InverterControlFieldsDialog QScrollBar::sub-line:vertical,
            #InverterControlFieldsDialog QScrollBar::add-page:vertical,
            #InverterControlFieldsDialog QScrollBar::sub-page:vertical {
                background: transparent;
                height: 0;
            }
                """,
                NEON_CLOSE_BUTTON_STYLE,
                NEON_HEADER_BAR_STYLE,
            )
        )


class DeviceProfilesDialog(QDialog):
    """Dialog for selecting, creating and editing stored device profiles."""
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("Choose Device"))
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self.resize(560, 420)
        self.setObjectName("DeviceProfilesDialog")

        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(lambda *_: self.accept())
        self.list_widget.currentRowChanged.connect(self._refresh_profile_cards)

        self.add_button = QPushButton(tr("Add"))
        self.add_button.setObjectName("SecondaryActionButton")
        apply_minimal_button(self.add_button)
        self.add_button.clicked.connect(self.add_profile)
        self.edit_button = QPushButton(tr("Edit"))
        self.edit_button.setObjectName("SecondaryActionButton")
        apply_minimal_button(self.edit_button)
        self.edit_button.clicked.connect(self.edit_profile)
        self.delete_button = QPushButton(tr("Delete"))
        self.delete_button.setObjectName("SecondaryActionButton")
        apply_minimal_button(self.delete_button)
        self.delete_button.clicked.connect(self.delete_profile)

        controls = QHBoxLayout()
        controls.addWidget(self.add_button)
        controls.addWidget(self.edit_button)
        controls.addWidget(self.delete_button)
        controls.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        ok_button = buttons.button(QDialogButtonBox.Ok)
        if ok_button is not None:
            ok_button.setText(tr("Open"))
            ok_button.setObjectName("PrimaryActionButton")
            apply_minimal_button(ok_button)
        cancel_button = buttons.button(QDialogButtonBox.Cancel)
        if cancel_button is not None:
            cancel_button.setObjectName("SecondaryActionButton")
            apply_minimal_button(cancel_button)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(create_neon_header_bar(self, self.reject, title=tr("Choose Device")))
        layout.addSpacing(20)
        layout.addWidget(QLabel(tr("Select the device profile to use in this session.")))
        layout.addWidget(self.list_widget, stretch=1)
        layout.addLayout(controls)
        layout.addWidget(buttons)
        self._apply_styles()

        self._profiles: list[DeviceProfile] = []
        self.reload_profiles()
        translate_widget_tree(self)

    def _apply_styles(self) -> None:
        """Compose shared dialog surface styles with list-card overrides."""
        self.setStyleSheet(
            compose_styles(
                dialog_surface_qss("#DeviceProfilesDialog"),
                """
            #DeviceProfilesDialog QLabel {
                color: #e2e8f0;
                font-size: 14px;
            }
            #DeviceProfilesDialog QListWidget {
                background: rgba(15, 23, 42, 0.96);
                color: #f8fafc;
                border: 1px solid #31415f;
                border-radius: 14px;
                padding: 10px;
                outline: 0;
            }
            #DeviceProfilesDialog QListWidget::item {
                margin: 4px 0;
                background: transparent;
            }
            QFrame#DeviceProfileListCard {
                background: rgba(30, 41, 59, 0.72);
                border: 1px solid rgba(71, 85, 105, 0.72);
                border-radius: 12px;
            }
            QFrame#DeviceProfileListCard[selected="true"] {
                background: rgba(56, 189, 248, 0.14);
                border-color: rgba(34, 211, 238, 0.72);
            }
            #DeviceProfileListTitle {
                color: #f8fafc;
                font-size: 16px;
                font-weight: 700;
            }
            #DeviceProfileListMeta {
                color: #dbe7f5;
                font-size: 13px;
            }
            QFrame#DeviceProfileListCard[selected="true"] #DeviceProfileListTitle,
            QFrame#DeviceProfileListCard[selected="true"] #DeviceProfileListMeta {
                color: #f8fafc;
            }
                """,
                NEON_CLOSE_BUTTON_STYLE,
                NEON_HEADER_BAR_STYLE,
            )
        )

    def reload_profiles(self) -> None:
        """Reload profiles from settings storage and rebuild list cards."""
        self._profiles = load_device_profiles()
        active_name = load_active_profile_name()
        self.list_widget.clear()
        active_row = 0
        for index, profile in enumerate(self._profiles):
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setSizeHint(QSize(0, 104))
            self.list_widget.addItem(item)
            card = DeviceProfileListCard(profile, self.list_widget)
            self.list_widget.setItemWidget(item, card)
            if profile.profile_name == active_name:
                active_row = index
        if self._profiles:
            self.list_widget.setCurrentRow(active_row)
        self._refresh_profile_cards()

    def _refresh_profile_cards(self) -> None:
        """Sync card `selected` property with current list row."""
        current_row = self.list_widget.currentRow()
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            if item is None:
                continue
            widget = self.list_widget.itemWidget(item)
            if isinstance(widget, DeviceProfileListCard):
                widget.set_selected(row == current_row)

    def selected_profile(self) -> DeviceProfile | None:
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self._profiles):
            return None
        return self._profiles[row]

    def add_profile(self) -> None:
        dialog = DeviceProfileEditDialog(self)
        if exec_modal_dialog(dialog, frameless=True, application_modal=False) != int(dialog.DialogCode.Accepted):
            return
        new_profile = dialog.profile()
        self._profiles = [profile for profile in self._profiles if profile.profile_name != new_profile.profile_name]
        self._profiles.append(new_profile)
        save_device_profiles(self._profiles)
        self.reload_profiles()

    def edit_profile(self) -> None:
        profile = self.selected_profile()
        if profile is None:
            return
        dialog = DeviceProfileEditDialog(self, profile=profile)
        if exec_modal_dialog(dialog, frameless=True, application_modal=False) != int(dialog.DialogCode.Accepted):
            return
        updated = dialog.profile()
        self._profiles = [
            item
            for item in self._profiles
            if item.profile_name not in {profile.profile_name, updated.profile_name}
        ]
        self._profiles.append(updated)
        save_device_profiles(self._profiles)
        self.reload_profiles()

    def delete_profile(self) -> None:
        profile = self.selected_profile()
        if profile is None:
            return
        confirmed = ask_compact_confirmation(
            self,
            title="Delete device profile",
            text=f"Delete '{profile.profile_name}' permanently?",
            accept_text="Delete",
            reject_text="Cancel",
            destructive=True,
        )
        if not confirmed:
            return
        self._profiles = [item for item in self._profiles if item.profile_name != profile.profile_name]
        save_device_profiles(self._profiles)
        if load_active_profile_name() == profile.profile_name:
            save_active_profile_name(self._profiles[0].profile_name if self._profiles else "")
        self.reload_profiles()

    def accept(self) -> None:
        profile = self.selected_profile()
        if profile is None:
            show_compact_message(self, kind="warning", title="Choose device", text="Add or select a device profile first.")
            return
        save_active_profile_name(profile.profile_name)
        super().accept()
