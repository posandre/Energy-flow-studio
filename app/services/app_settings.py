from __future__ import annotations

from dataclasses import dataclass
import json

from PySide6.QtCore import QSettings

from app.services.dessmonitor_api import DEFAULT_KEY_PARAMETERS


SETTINGS_ORG = "Codex"
SETTINGS_APP = "EnergyFlow Studio"
DEFAULT_ENERGYFLOW_REFRESH_SECONDS = 180
MIN_ENERGYFLOW_REFRESH_SECONDS = 60
MAX_ENERGYFLOW_REFRESH_SECONDS = 3600
DEFAULT_TUYA_POLL_INTERVAL_SEC = 30
MIN_TUYA_POLL_INTERVAL_SEC = 5
MAX_TUYA_POLL_INTERVAL_SEC = 3600
DEFAULT_TUYA_ENDPOINT = "https://openapi-weaz.tuyaeu.com"
DEFAULT_UI_LANGUAGE = "en"
UI_LANGUAGE_CHOICES: tuple[tuple[str, str], ...] = (
    ("English", "en"),
    ("Українська", "uk"),
)
TUYA_ENDPOINT_CHOICES: tuple[tuple[str, str], ...] = (
    ("Western Europe", "https://openapi-weaz.tuyaeu.com"),
    ("Central Europe", "https://openapi.tuyaeu.com"),
    ("Central Europe Data Center", "https://openapi.tuyaeu.com"),
    ("Western America", "https://openapi.tuyaus.com"),
    ("Eastern America", "https://openapi-ueaz.tuyaus.com"),
    ("China", "https://openapi.tuyacn.com"),
    ("India", "https://openapi.tuyain.com"),
)

@dataclass(slots=True)
class DessMonitorImportSettings:
    import_all_parameters: bool = True
    parameter_keys: list[str] | None = None

    def normalized_keys(self) -> list[str]:
        keys = [key.strip() for key in (self.parameter_keys or []) if key.strip()]
        return keys or DEFAULT_KEY_PARAMETERS.copy()


@dataclass(slots=True)
class TuyaSettings:
    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    endpoint: str = DEFAULT_TUYA_ENDPOINT
    poll_interval_sec: int = DEFAULT_TUYA_POLL_INTERVAL_SEC

    def normalized_endpoint(self) -> str:
        endpoint = self.endpoint.strip()
        if endpoint:
            return endpoint
        return DEFAULT_TUYA_ENDPOINT

    def normalized_poll_interval_sec(self) -> int:
        try:
            interval = int(self.poll_interval_sec)
        except (TypeError, ValueError):
            interval = DEFAULT_TUYA_POLL_INTERVAL_SEC
        return max(MIN_TUYA_POLL_INTERVAL_SEC, min(MAX_TUYA_POLL_INTERVAL_SEC, interval))


@dataclass(slots=True)
class DeviceControlFieldProfile:
    field_id: str
    name: str
    prog_number: str = ""
    alias: str = ""
    comment: str = ""
    unit: str = ""
    category: str = "System"
    writable: bool = False
    options: list[tuple[str, str]] | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "field_id": self.field_id,
            "name": self.name,
            "prog_number": self.prog_number,
            "alias": self.alias,
            "comment": self.comment,
            "unit": self.unit,
            "category": self.category,
            "writable": bool(self.writable),
            "options": [[value, label] for value, label in (self.options or [])],
        }

    @classmethod
    def from_dict(cls, entry: dict[str, object]) -> "DeviceControlFieldProfile":
        raw_options = entry.get("options", []) or []
        options: list[tuple[str, str]] = []
        if isinstance(raw_options, list):
            for item in raw_options:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    options.append((str(item[0]).strip(), str(item[1]).strip()))
        return cls(
            field_id=str(entry.get("field_id", "")).strip(),
            name=str(entry.get("name", "")).strip(),
            prog_number=str(entry.get("prog_number", entry.get("prog", ""))).strip(),
            alias=str(entry.get("alias", "")).strip(),
            comment=str(entry.get("comment", "")).strip(),
            unit=str(entry.get("unit", "")).strip(),
            category=str(entry.get("category", "System")).strip() or "System",
            writable=bool(entry.get("writable", False)),
            options=options,
        )

    def display_name(self) -> str:
        return self.alias.strip() or self.name.strip() or self.field_id


@dataclass(slots=True)
class DeviceProfile:
    profile_name: str
    username: str
    password: str
    company_key: str
    source: int
    pn: str
    devcode: str
    devaddr: str
    sn: str
    device_label: str
    energyflow_refresh_seconds: int = DEFAULT_ENERGYFLOW_REFRESH_SECONDS
    auto_sync_enabled: bool = False
    available_parameter_keys: list[str] | None = None
    selected_parameter_keys: list[str] | None = None
    inverter_control_fields: list[DeviceControlFieldProfile] | None = None
    inverter_location_mode: str = "auto"
    inverter_location_label: str = ""
    inverter_location_description: str = ""
    inverter_latitude: str = ""
    inverter_longitude: str = ""
    tuya_enabled: bool = False
    tuya_client_id: str = ""
    tuya_client_secret: str = ""
    tuya_endpoint: str = DEFAULT_TUYA_ENDPOINT
    tuya_poll_interval_sec: int = DEFAULT_TUYA_POLL_INTERVAL_SEC
    battery_capability_ah: float = 0.0
    ui_language: str = DEFAULT_UI_LANGUAGE
    day_zone_tariff_uah_per_kwh: float = 0.0
    night_zone_tariff_uah_per_kwh: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "profile_name": self.profile_name,
            "username": self.username,
            "password": self.password,
            "company_key": self.company_key,
            "source": self.source,
            "pn": self.pn,
            "devcode": self.devcode,
            "devaddr": self.devaddr,
            "sn": self.sn,
            "device_label": self.device_label,
            "energyflow_refresh_seconds": self.resolved_energyflow_refresh_seconds(),
            "auto_sync_enabled": bool(self.auto_sync_enabled),
            "available_parameter_keys": list(self.available_parameter_keys or []),
            "selected_parameter_keys": list(self.selected_parameter_keys or []),
            "inverter_control_fields": [field.to_dict() for field in (self.inverter_control_fields or [])],
            "inverter_location_mode": self.resolved_inverter_location_mode(),
            "inverter_location_label": self.inverter_location_label.strip(),
            "inverter_location_description": self.inverter_location_description.strip(),
            "inverter_latitude": self.inverter_latitude.strip(),
            "inverter_longitude": self.inverter_longitude.strip(),
            "tuya_enabled": bool(self.tuya_enabled),
            "tuya_client_id": self.tuya_client_id.strip(),
            "tuya_client_secret": self.tuya_client_secret.strip(),
            "tuya_endpoint": self.resolved_tuya_endpoint(),
            "tuya_poll_interval_sec": self.resolved_tuya_poll_interval_sec(),
            "battery_capability_ah": self.resolved_battery_capability_ah(),
            "ui_language": self.resolved_ui_language(),
            "day_zone_tariff_uah_per_kwh": self.resolved_day_zone_tariff_uah_per_kwh(),
            "night_zone_tariff_uah_per_kwh": self.resolved_night_zone_tariff_uah_per_kwh(),
        }

    def resolved_parameter_keys(self) -> list[str]:
        available = [key.strip() for key in (self.available_parameter_keys or []) if key.strip()]
        selected = [key.strip() for key in (self.selected_parameter_keys or []) if key.strip()]
        if selected:
            return selected
        if available:
            return available
        return DEFAULT_KEY_PARAMETERS.copy()

    def resolved_energyflow_refresh_seconds(self) -> int:
        try:
            seconds = int(self.energyflow_refresh_seconds)
        except (TypeError, ValueError):
            seconds = DEFAULT_ENERGYFLOW_REFRESH_SECONDS
        return max(MIN_ENERGYFLOW_REFRESH_SECONDS, min(MAX_ENERGYFLOW_REFRESH_SECONDS, seconds))

    def resolved_inverter_control_fields(self) -> list[DeviceControlFieldProfile]:
        return [field for field in (self.inverter_control_fields or []) if field.field_id.strip()]

    def resolved_inverter_location_mode(self) -> str:
        mode = self.inverter_location_mode.strip().lower()
        if mode in {"auto", "manual"}:
            return mode
        return "auto"

    def resolved_tuya_endpoint(self) -> str:
        endpoint = self.tuya_endpoint.strip()
        if endpoint:
            return endpoint
        return DEFAULT_TUYA_ENDPOINT

    def resolved_tuya_poll_interval_sec(self) -> int:
        try:
            interval = int(self.tuya_poll_interval_sec)
        except (TypeError, ValueError):
            interval = DEFAULT_TUYA_POLL_INTERVAL_SEC
        return max(MIN_TUYA_POLL_INTERVAL_SEC, min(MAX_TUYA_POLL_INTERVAL_SEC, interval))

    def resolved_battery_capability_ah(self) -> float:
        try:
            value = float(self.battery_capability_ah)
        except (TypeError, ValueError):
            value = 0.0
        return max(0.0, value)

    def resolved_ui_language(self) -> str:
        language = str(self.ui_language or DEFAULT_UI_LANGUAGE).strip().lower()
        allowed = {code for _, code in UI_LANGUAGE_CHOICES}
        if language in allowed:
            return language
        return DEFAULT_UI_LANGUAGE

    def resolved_day_zone_tariff_uah_per_kwh(self) -> float:
        try:
            value = float(self.day_zone_tariff_uah_per_kwh)
        except (TypeError, ValueError):
            value = 0.0
        return max(0.0, value)

    def resolved_night_zone_tariff_uah_per_kwh(self) -> float:
        try:
            value = float(self.night_zone_tariff_uah_per_kwh)
        except (TypeError, ValueError):
            value = 0.0
        return max(0.0, value)


def load_ui_language() -> str:
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    language = settings.value("ui/language", DEFAULT_UI_LANGUAGE, type=str)
    language_code = str(language or DEFAULT_UI_LANGUAGE).strip().lower()
    allowed = {code for _, code in UI_LANGUAGE_CHOICES}
    if language_code in allowed:
        return language_code
    return DEFAULT_UI_LANGUAGE


def save_ui_language(language_code: str) -> None:
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    language = str(language_code or DEFAULT_UI_LANGUAGE).strip().lower()
    allowed = {code for _, code in UI_LANGUAGE_CHOICES}
    settings.setValue("ui/language", language if language in allowed else DEFAULT_UI_LANGUAGE)
    settings.sync()


def load_dessmonitor_import_settings() -> DessMonitorImportSettings:
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    import_all = settings.value("dessmonitor/import_all_parameters", True, type=bool)
    raw_keys = settings.value("dessmonitor/parameter_keys", DEFAULT_KEY_PARAMETERS)
    if isinstance(raw_keys, str):
        keys = [chunk.strip() for chunk in raw_keys.replace("\n", ",").split(",") if chunk.strip()]
    elif isinstance(raw_keys, list):
        keys = [str(item).strip() for item in raw_keys if str(item).strip()]
    else:
        keys = DEFAULT_KEY_PARAMETERS.copy()
    return DessMonitorImportSettings(import_all_parameters=import_all, parameter_keys=keys)


def save_dessmonitor_import_settings(config: DessMonitorImportSettings) -> None:
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    settings.setValue("dessmonitor/import_all_parameters", config.import_all_parameters)
    settings.setValue("dessmonitor/parameter_keys", config.normalized_keys())


def load_tuya_settings() -> TuyaSettings:
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    endpoint = settings.value("tuya/endpoint", DEFAULT_TUYA_ENDPOINT, type=str)
    return TuyaSettings(
        enabled=settings.value("tuya/enabled", False, type=bool),
        client_id=settings.value("tuya/client_id", "", type=str),
        client_secret=settings.value("tuya/client_secret", "", type=str),
        endpoint=(endpoint or DEFAULT_TUYA_ENDPOINT).strip(),
        poll_interval_sec=settings.value("tuya/poll_interval_sec", DEFAULT_TUYA_POLL_INTERVAL_SEC, type=int),
    )


def save_tuya_settings(config: TuyaSettings) -> None:
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    settings.setValue("tuya/enabled", bool(config.enabled))
    settings.setValue("tuya/client_id", config.client_id.strip())
    settings.setValue("tuya/client_secret", config.client_secret.strip())
    settings.setValue("tuya/endpoint", config.normalized_endpoint())
    settings.setValue("tuya/poll_interval_sec", config.normalized_poll_interval_sec())
    settings.sync()


def load_device_profiles() -> list[DeviceProfile]:
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    raw = settings.value("dessmonitor/device_profiles", "[]")
    try:
        entries = json.loads(raw if isinstance(raw, str) else "[]")
    except json.JSONDecodeError:
        entries = []
    profiles: list[DeviceProfile] = []
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            profiles.append(
                DeviceProfile(
                    profile_name=str(entry.get("profile_name", "")).strip(),
                    username=str(entry.get("username", "")).strip(),
                    password=str(entry.get("password", "")),
                    company_key=str(entry.get("company_key", "")).strip(),
                    source=int(entry.get("source", 1)),
                    pn=str(entry.get("pn", "")).strip(),
                    devcode=str(entry.get("devcode", "")).strip(),
                    devaddr=str(entry.get("devaddr", "")).strip(),
                    sn=str(entry.get("sn", "")).strip(),
                    device_label=str(entry.get("device_label", "")).strip(),
                    energyflow_refresh_seconds=int(
                        entry.get("energyflow_refresh_seconds", DEFAULT_ENERGYFLOW_REFRESH_SECONDS)
                    ),
                    auto_sync_enabled=bool(entry.get("auto_sync_enabled", False)),
                    available_parameter_keys=[
                        str(item).strip()
                        for item in entry.get("available_parameter_keys", []) or []
                        if str(item).strip()
                    ],
                    selected_parameter_keys=[
                        str(item).strip()
                        for item in entry.get("selected_parameter_keys", []) or []
                        if str(item).strip()
                    ],
                    inverter_control_fields=[
                        DeviceControlFieldProfile.from_dict(item)
                        for item in entry.get("inverter_control_fields", []) or []
                        if isinstance(item, dict)
                    ],
                    inverter_location_mode=str(entry.get("inverter_location_mode", "auto")).strip() or "auto",
                    inverter_location_label=str(entry.get("inverter_location_label", "")).strip(),
                    inverter_location_description=str(entry.get("inverter_location_description", "")).strip(),
                    inverter_latitude=str(entry.get("inverter_latitude", "")).strip(),
                    inverter_longitude=str(entry.get("inverter_longitude", "")).strip(),
                    tuya_enabled=bool(entry.get("tuya_enabled", False)),
                    tuya_client_id=str(entry.get("tuya_client_id", "")).strip(),
                    tuya_client_secret=str(entry.get("tuya_client_secret", "")).strip(),
                    tuya_endpoint=str(entry.get("tuya_endpoint", DEFAULT_TUYA_ENDPOINT)).strip() or DEFAULT_TUYA_ENDPOINT,
                    tuya_poll_interval_sec=int(entry.get("tuya_poll_interval_sec", DEFAULT_TUYA_POLL_INTERVAL_SEC)),
                    battery_capability_ah=float(entry.get("battery_capability_ah", 0.0) or 0.0),
                    ui_language=str(entry.get("ui_language", DEFAULT_UI_LANGUAGE)).strip().lower() or DEFAULT_UI_LANGUAGE,
                    day_zone_tariff_uah_per_kwh=float(entry.get("day_zone_tariff_uah_per_kwh", 0.0) or 0.0),
                    night_zone_tariff_uah_per_kwh=float(entry.get("night_zone_tariff_uah_per_kwh", 0.0) or 0.0),
                )
            )
    loaded_profiles = [profile for profile in profiles if profile.profile_name]
    return loaded_profiles


def save_device_profiles(profiles: list[DeviceProfile]) -> None:
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    settings.setValue(
        "dessmonitor/device_profiles",
        json.dumps([profile.to_dict() for profile in profiles], ensure_ascii=False),
    )
    settings.sync()


def load_active_profile_name() -> str:
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    return settings.value("dessmonitor/active_profile", "", type=str)


def save_active_profile_name(profile_name: str) -> None:
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    settings.setValue("dessmonitor/active_profile", profile_name)
    settings.sync()


def load_last_loaded_profile_name() -> str:
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    return settings.value("dessmonitor/last_loaded_profile", "", type=str)


def save_last_loaded_profile_name(profile_name: str) -> None:
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    settings.setValue("dessmonitor/last_loaded_profile", profile_name)
    settings.sync()


def load_profile_automations(profile_name: str) -> list[dict[str, object]]:
    normalized_profile = str(profile_name or "").strip()
    if not normalized_profile:
        return []
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    raw = settings.value(f"automations/{normalized_profile}/rules", "[]")
    try:
        payload = json.loads(raw if isinstance(raw, str) else "[]")
    except json.JSONDecodeError:
        payload = []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, dict)]


def save_profile_automations(profile_name: str, rules: list[dict[str, object]]) -> None:
    normalized_profile = str(profile_name or "").strip()
    if not normalized_profile:
        return
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    settings.setValue(
        f"automations/{normalized_profile}/rules",
        json.dumps([item for item in rules if isinstance(item, dict)], ensure_ascii=False),
    )
    settings.sync()


def load_profile_automation_logs(profile_name: str) -> list[str]:
    normalized_profile = str(profile_name or "").strip()
    if not normalized_profile:
        return []
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    raw = settings.value(f"automations/{normalized_profile}/logs", "[]")
    try:
        payload = json.loads(raw if isinstance(raw, str) else "[]")
    except json.JSONDecodeError:
        payload = []
    if not isinstance(payload, list):
        return []
    return [str(item) for item in payload if str(item).strip()]


def save_profile_automation_logs(profile_name: str, lines: list[str]) -> None:
    normalized_profile = str(profile_name or "").strip()
    if not normalized_profile:
        return
    trimmed = [str(item) for item in lines if str(item).strip()][-400:]
    settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
    settings.setValue(
        f"automations/{normalized_profile}/logs",
        json.dumps(trimmed, ensure_ascii=False),
    )
    settings.sync()
