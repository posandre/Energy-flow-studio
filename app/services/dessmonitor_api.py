from __future__ import annotations

import calendar
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from hashlib import sha1
import json
from threading import Lock
from time import sleep, time
from typing import Any
import re
from urllib.parse import urlencode

import pandas as pd

from app.services.logging_utils import get_logger

API_BASE_URL = "https://api.dessmonitor.com/public/"
WEB_AUTH_URL = "https://web.dessmonitor.com/public/"
DEFAULT_TIMEOUT_SECONDS = 15
DEFAULT_MAX_RETRIES = 2
DEFAULT_RETRY_DELAY_SECONDS = 0.45
DEFAULT_APP_CLIENT = "web"
DEFAULT_APP_ID = "energy-excel-analyzer"
DEFAULT_APP_VERSION = "0.1.0"
DEFAULT_I18N = "en"
AUTH_CACHE_TTL_SECONDS = 300
SETTINGS_CACHE_TTL_SECONDS = 300
DEFAULT_PARALLEL_DATA_REQUESTS = 4
DEFAULT_CTRL_WRITE_ACTION_CANDIDATES: tuple[str, ...] = (
    "setDeviceCtrlValue",
    "saveDeviceCtrlValue",
    "updateDeviceCtrlValue",
    "editDeviceCtrlValue",
)
DEFAULT_KEY_PARAMETERS = [
    "PV_OUTPUT_POWER",
    "GRID_ACTIVE_POWER",
    "LOAD_ACTIVE_POWER",
    "BATTERY_ACTIVE_POWER",
    "BT_BATTERY_CAPACITY",
]
DEFAULT_HISTORY_AGGREGATE_PARAMETERS = [
    "ENERGY_TOTAL_PV",
    "ENERGY_TOTAL_LOAD",
    "ENERGY_TOTAL_GRID",
    "ENERGY_TOTAL_BATTERY_CHARGE",
    "ENERGY_TOTAL_BATTERY_DISCHARGE",
    "ENERGY_TODAY_PV",
    "ENERGY_TODAY_LOAD",
    "ENERGY_TODAY_GRID",
]
DEFAULT_HISTORY_DAY_PARAMETERS = [
    "SOC",
    *DEFAULT_KEY_PARAMETERS,
]
SETTING_CATEGORY_ORDER = {
    "Battery": 0,
    "Charging": 1,
    "Grid": 2,
    "PV": 3,
    "System": 4,
    "Time-of-use": 5,
    "Protection": 6,
    "Other": 7,
}

ERROR_DESCRIPTIONS = {
    "0x0000": "Normal, no errors.",
    "0x0001": "Operation failed.",
    "0x0002": "The request timed out.",
    "0x0003": "DessMonitor reported a system exception.",
    "0x0004": "The request signature could not be verified.",
    "0x0005": "The salt value is invalid.",
    "0x0006": "One or more parameters have the wrong format.",
    "0x0007": "Required parameters are missing.",
    "0x0008": "The platform rejected the request.",
    "0x0009": "This operation is not supported.",
    "0x000A": "Authentication failed.",
    "0x000B": "The account does not have permission for this action.",
    "0x000C": "No matching records were found.",
    "0x000D": "A platform limit was exceeded.",
    "0x000E": "The same action was repeated.",
    "0x000F": "No company key was found.",
    "0x0010": "Password verification failed.",
    "0x0011": "The password format is invalid.",
    "0x0012": "This mobile number is already bound.",
    "0x0013": "The phone number format is invalid.",
    "0x0014": "The email address format is invalid.",
    "0x0015": "The username format is invalid.",
    "0x0016": "The company name format is invalid.",
    "0x0017": "The manufacturer code is invalid.",
    "0x0018": "That username already exists.",
    "0x0019": "This account has been suspended.",
    "0x0100": "The requested API endpoint was not found.",
    "0x0101": "The collector was not found.",
    "0x0102": "The device was not found.",
    "0x0103": "The collector number is invalid.",
    "0x0104": "The project was not found.",
    "0x0105": "The user was not found.",
    "0x0106": "The device is offline.",
    "0x0107": "The collector is offline.",
    "0x0108": "The device alarm was not found.",
    "0x0109": "The environmental monitor was not found.",
    "0x010F": "There are no changes to save.",
    "0x0110": "This binding already exists.",
    "0x0111": "Collectors from other vendors cannot be added.",
    "0x0112": "The account is already bound to WeChat.",
    "0x0113": "The account is not bound to WeChat.",
    "0x0114": "Moving plant owners with other-vendor collectors is not supported.",
    "0x0115": "The vendor was not found.",
    "0x0120": "The camera was not found.",
    "0x0160": "Firmware was not found.",
    "0x0161": "Protocol was not found.",
    "0x0170": "Role was not found.",
    "0x0171": "User group was not found.",
    "0x0172": "Project group was not found.",
    "0x0173": "GPRS top-up plan was not found.",
    "0x0174": "GPRS recharge order was not found.",
    "0x0180": "SMS verification code was not found.",
    "0x0181": "Device token was not found.",
    "0x0182": "Device configuration was not found.",
    "0x0183": "Collector type was not found.",
    "0x0184": "Permission group was not found.",
    "0x0185": "Latitude and longitude are not set.",
    "0x0200": "The SMS verification code does not match.",
    "0x0201": "The SMS verification code expired.",
    "0x0202": "This phone number is not bound.",
    "0x0203": "The phone number does not match the account.",
    "0x0204": "The name must be unique.",
    "0x0205": "The user or project still has collectors attached.",
    "0x0206": "The user still has subordinate users.",
    "0x0207": "The user already exists.",
    "0x0208": "The collector number has already been registered.",
    "0x0209": "The collector does not belong to this user.",
    "0x020A": "The collector already exists.",
    "0x020B": "This is not a viewer account.",
    "0x020C": "The SMS code was requested too recently.",
    "0x0300": "There are no optional protocol fields available.",
    "0x0400": "Only plant owners are supported for this action.",
    "0x0401": "Only distributors are supported for this action.",
    "0x0402": "Only equipment manufacturers are supported for this action.",
    "0x0403": "The parent account must be a distributor or manufacturer.",
    "0x0404": "Unsupported recharge method.",
    "0x0405": "Duplicate collector number.",
    "0x0406": "Invalid GPRS card.",
    "0x0407": "This user was not created by an administrator.",
    "0x0408": "Only manufacturer and distributor accounts are supported.",
    "0x0409": "The QR code has not been scanned yet.",
    "0x0410": "The QR code was scanned but not confirmed.",
    "0x0411": "The QR key expired or was not found.",
    "0x0412": "Invalid CCID.",
    "0x0700": "All collectors must belong to the distributor.",
    "ERR_NONE": "Normal, no errors.",
    "ERR_FAIL": "Operation failed.",
    "ERR_TIMEOUT": "The request timed out.",
    "ERR_SYSTEM_EXCEPTION": "DessMonitor reported a system exception.",
    "ERR_SIGN": "The request signature could not be verified.",
    "ERR_SALT": "The salt value is invalid.",
    "ERR_FORMAT_ERROR": "One or more parameters have the wrong format.",
    "ERR_MISSING_PARAMETER": "Required parameters are missing.",
    "ERR_FORBIDDEN": "The platform rejected the request.",
    "ERR_UNSUPPORTED": "This operation is not supported.",
    "ERR_NO_AUTH": "Authentication failed.",
    "ERR_NO_PERMISSION": "The account does not have permission for this action.",
    "ERR_NO_RECORD": "No matching records were found.",
    "ERR_OVER_LIMITED": "A platform limit was exceeded.",
    "ERR_DUPLICATE_OPER": "The same action was repeated.",
    "ERR_NOT_FOUND_COMPANY_KEY": "No company key was found.",
    "ERR_PASSWORD_VERIF_FAIL": "Password verification failed.",
    "ERR_PASSWORD_FORMAT_ERROR": "The password format is invalid.",
    "ERR_MOBILE_BINDED": "This mobile number is already bound.",
    "ERR_MOBILE_FORMAT": "The phone number format is invalid.",
    "ERR_EMAIL_FORMAT": "The email address format is invalid.",
    "ERR_USRNAME_FORMAT": "The username format is invalid.",
    "ERR_COMPANY_NAME_FORMAT": "The company name format is invalid.",
    "ERR_VCODE_INVALID": "The manufacturer code is invalid.",
    "ERR_USR_NAME_DUPLICATE": "That username already exists.",
    "ERR_USR_DISABLED": "This account has been suspended.",
    "ERR_NOT_FOUND_API": "The requested API endpoint was not found.",
    "ERR_NOT_FOUND_COLLECTOR": "The collector was not found.",
    "ERR_NOT_FOUND_DEVICE": "The device was not found.",
    "ERR_INVALID_PN": "The collector number is invalid.",
    "ERR_NOT_FOUND_PLANT": "The project was not found.",
    "ERR_NOT_FOUND_USR": "The user was not found.",
    "ERR_DEVICE_OFFLINE": "The device is offline.",
    "ERR_COLLECTOR_OFFLINE": "The collector is offline.",
    "ERR_NOT_FOUND_DEVICE_WARNING": "The device alarm was not found.",
    "ERR_NOT_FOUND_ENVMONITOR": "The environmental monitor was not found.",
}
LOGGER = get_logger(__name__)


class DessMonitorApiError(Exception):
    """Raised when DessMonitor API authentication or data loading fails."""


class DessMonitorTransportError(DessMonitorApiError):
    """Raised when the HTTP/TLS transport to DessMonitor fails."""


@dataclass(slots=True)
class AuthAttempt:
    description: str
    include_company_key: bool


@dataclass(slots=True)
class DessMonitorConfig:
    username: str
    password: str
    company_key: str
    source: int
    pn: str
    devcode: str
    devaddr: str
    sn: str
    device_label: str
    parameter_keys: list[str]
    date_from: str
    date_to: str
    period_mode: str = "custom"
    i18n: str = DEFAULT_I18N
    app_client: str = DEFAULT_APP_CLIENT
    app_id: str = DEFAULT_APP_ID
    app_version: str = DEFAULT_APP_VERSION


@dataclass(slots=True)
class DessMonitorDevice:
    pn: str
    devcode: str
    devaddr: str
    sn: str
    alias: str = ""
    pid: str = ""
    status: str = ""
    install_date: str = ""

    @property
    def display_name(self) -> str:
        alias = self.alias.strip()
        lead = alias if alias else self.sn
        return f"{lead} | PN {self.pn} | addr {self.devaddr} | code {self.devcode}"


@dataclass(slots=True)
class DeviceLocation:
    pid: str
    latitude: str = ""
    longitude: str = ""
    timezone: str = ""
    country: str = ""
    province: str = ""
    city: str = ""
    county: str = ""
    town: str = ""
    village: str = ""
    address: str = ""

    def formatted_address(self) -> str:
        parts = [
            self.address,
            self.village,
            self.town,
            self.county,
            self.city,
            self.province,
            self.country,
        ]
        normalized = [part.strip() for part in parts if str(part).strip()]
        return ", ".join(dict.fromkeys(normalized))


@dataclass(slots=True)
class DeviceControlField:
    field_id: str
    name: str
    unit: str = ""
    hint: str = ""
    category: str = "System"
    writable: bool = False
    access_status_text: str = "Readable"
    options: tuple[tuple[str, str], ...] = ()
    raw_name: str = ""


@dataclass(slots=True)
class InverterSetting:
    field_id: str
    name: str
    display_name: str
    raw_value: str | int | float | None
    display_value: str
    unit: str
    category: str
    writable: bool
    options: tuple[tuple[str, str], ...] = ()
    hint: str = ""
    raw_name: str = ""


@dataclass(slots=True)
class AuthTokens:
    token: str
    secret: str


@dataclass(slots=True)
class CachedAuthTokens:
    tokens: AuthTokens
    expires_at: float


DEFAULT_AUTH_ATTEMPTS = (
    AuthAttempt("Trying authentication without company key", include_company_key=False),
    AuthAttempt("Retrying authentication with company key", include_company_key=True),
)
_AUTH_CACHE_LOCK = Lock()
_AUTH_CACHE: dict[tuple[str, str, str, str], CachedAuthTokens] = {}
_SETTINGS_CACHE_LOCK = Lock()
_CONTROL_FIELDS_CACHE: dict[tuple[str, str, str, str], tuple[float, list["DeviceControlField"]]] = {}
_CONTROL_VALUE_CACHE: dict[tuple[str, str, str, str, str], tuple[float, str | int | float | None]] = {}
_CONTROL_WRITE_ACTION_CACHE: dict[tuple[str, str, str, str], str] = {}
_LAST_SOC_REQUEST_URL: str | None = None
_LAST_ENERGYFLOW_REQUEST_URL: str | None = None
_LAST_ENERGYFLOW_RESPONSE_TEXT: str | None = None
_LAST_LASTDATA_REQUEST_URL: str | None = None
_LAST_LASTDATA_RESPONSE_TEXT: str | None = None
_LAST_CTRL_FIELDS_REQUEST_URL: str | None = None
_LAST_CTRL_FIELDS_RESPONSE_TEXT: str | None = None
_LAST_CTRL_VALUE_REQUEST_URL: str | None = None
_LAST_CTRL_VALUE_RESPONSE_TEXT: str | None = None
_LAST_DEVICE_LOCATION_RESPONSE_TEXT: str | None = None


def _dedupe_preserving_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        unique.append(item)
    return unique


def _sha1_hex(value: str) -> str:
    return sha1(value.encode("utf-8")).hexdigest()


def _salt() -> str:
    return str(int(time() * 1000))


def _normalize_parameter_keys(raw_keys: str | list[str]) -> list[str]:
    if isinstance(raw_keys, list):
        keys: list[str] = []
        for key in raw_keys:
            keys.extend(chunk.strip() for chunk in str(key).replace("\n", ",").split(","))
    else:
        keys = [chunk.strip() for chunk in raw_keys.replace("\n", ",").split(",")]
    normalized = [key for key in keys if key]
    return normalized or DEFAULT_KEY_PARAMETERS.copy()


def _date_range(start_iso: str, end_iso: str) -> list[str]:
    start_date = date.fromisoformat(start_iso)
    end_date = date.fromisoformat(end_iso)
    if end_date < start_date:
        raise DessMonitorApiError("The end date must be on or after the start date.")

    dates: list[str] = []
    current = start_date
    while current <= end_date:
        dates.append(current.isoformat())
        current += timedelta(days=1)
    return dates


def _month_range(year: int, month: int) -> tuple[date, date]:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, 1), date(year, month, last_day)


def _transfer_uri_str(params: OrderedDict[str, str]) -> str:
    return (
        urlencode(list(params.items()))
        .replace("%20", "+")
        .replace("%2B", "+")
        .replace("%3A", ":")
        .replace("%2C", ",")
        .replace("%40", "@")
        .replace("%24", "$")
        .replace("%26", "&")
        .replace("%3D", "=")
        .replace("%28", "(")
        .replace("%29", ")")
    )


def _build_request_url(params: OrderedDict[str, str], *, base_url: str = API_BASE_URL) -> str:
    return f"{base_url}?{_transfer_uri_str(params)}"


def _remember_soc_request_url(params: OrderedDict[str, str], *, base_url: str = API_BASE_URL) -> None:
    global _LAST_SOC_REQUEST_URL
    if params.get("action") != "querySPDeviceKeyParameterOneDay":
        return
    if str(params.get("parameter", "")).upper() != "SOC":
        return
    _LAST_SOC_REQUEST_URL = _build_request_url(params, base_url=base_url)


def _remember_energyflow_request_url(params: OrderedDict[str, str], *, base_url: str = API_BASE_URL) -> None:
    global _LAST_ENERGYFLOW_REQUEST_URL
    if params.get("action") != "webQueryDeviceEnergyFlowEs":
        return
    _LAST_ENERGYFLOW_REQUEST_URL = _build_request_url(params, base_url=base_url)


def _remember_energyflow_response(params: OrderedDict[str, str], payload: dict[str, Any]) -> None:
    global _LAST_ENERGYFLOW_RESPONSE_TEXT
    if params.get("action") != "webQueryDeviceEnergyFlowEs":
        return
    _LAST_ENERGYFLOW_RESPONSE_TEXT = json.dumps(payload, indent=2, ensure_ascii=False)


def _remember_lastdata_request_url(params: OrderedDict[str, str], *, base_url: str = API_BASE_URL) -> None:
    global _LAST_LASTDATA_REQUEST_URL
    if params.get("action") != "querySPDeviceLastData":
        return
    _LAST_LASTDATA_REQUEST_URL = _build_request_url(params, base_url=base_url)


def _remember_lastdata_response(params: OrderedDict[str, str], payload: dict[str, Any]) -> None:
    global _LAST_LASTDATA_RESPONSE_TEXT
    if params.get("action") != "querySPDeviceLastData":
        return
    _LAST_LASTDATA_RESPONSE_TEXT = json.dumps(payload, indent=2, ensure_ascii=False)


def get_last_soc_request_url() -> str | None:
    return _LAST_SOC_REQUEST_URL


def get_last_energyflow_request_url() -> str | None:
    return _LAST_ENERGYFLOW_REQUEST_URL


def get_last_lastdata_request_url() -> str | None:
    return _LAST_LASTDATA_REQUEST_URL


def build_soc_request_url_preview(config: DessMonitorConfig, *, request_date: str | None = None) -> str:
    date_value = request_date or config.date_from or date.today().isoformat()
    preview_params = OrderedDict(
        [
            ("sign", "<sign>"),
            ("salt", "<salt>"),
            ("token", "<token>"),
            ("action", "querySPDeviceKeyParameterOneDay"),
            ("pn", config.pn),
            ("devcode", config.devcode),
            ("devaddr", config.devaddr),
            ("sn", config.sn),
            ("parameter", "SOC"),
            ("date", date_value),
            ("source", str(config.source)),
            ("_app_client_", config.app_client),
            ("_app_id_", config.app_id),
            ("_app_version_", config.app_version),
            ("i18n", config.i18n),
        ]
    )
    return _build_request_url(preview_params)


def build_energyflow_request_url_preview(config: DessMonitorConfig) -> str:
    preview_params = OrderedDict(
        [
            ("sign", "<sign>"),
            ("salt", "<salt>"),
            ("token", "<token>"),
            ("action", "webQueryDeviceEnergyFlowEs"),
            ("pn", config.pn),
            ("devcode", config.devcode),
            ("devaddr", config.devaddr),
            ("sn", config.sn),
            ("source", str(config.source)),
            ("_app_client_", config.app_client),
            ("_app_id_", config.app_id),
            ("_app_version_", config.app_version),
        ]
    )
    return _build_request_url(preview_params)


def _request_json(params: OrderedDict[str, str], *, base_url: str = API_BASE_URL) -> dict[str, Any]:
    _remember_soc_request_url(params, base_url=base_url)
    _remember_energyflow_request_url(params, base_url=base_url)
    _remember_lastdata_request_url(params, base_url=base_url)
    try:
        import requests
        from requests import exceptions as requests_exceptions
    except ModuleNotFoundError as exc:
        raise DessMonitorApiError(
            "DessMonitor support requires the 'requests' package. Reinstall dependencies to enable API loading."
        ) from exc

    last_error: Exception | None = None
    for attempt in range(DEFAULT_MAX_RETRIES + 1):
        try:
            response = requests.get(base_url, params=params, timeout=DEFAULT_TIMEOUT_SECONDS)
            response.raise_for_status()
            payload = response.json()
            break
        except (
            requests_exceptions.ConnectTimeout,
            requests_exceptions.ReadTimeout,
            requests_exceptions.ConnectionError,
            requests_exceptions.SSLError,
        ) as exc:
            last_error = exc
            if attempt == DEFAULT_MAX_RETRIES:
                details = str(exc).strip()
                normalized = details.lower()
                if "ssl" in normalized or "tls" in normalized or "eof" in normalized:
                    message = (
                        "DessMonitor TLS handshake failed while reading the response. "
                        "This is usually transient. Please retry in a few seconds."
                    )
                elif "timed out" in normalized:
                    message = "DessMonitor request timed out. Please retry in a few seconds."
                else:
                    message = "Connection to DessMonitor API failed. Please retry in a few seconds."
                if details:
                    message = f"{message} ({details})"
                raise DessMonitorTransportError(message) from exc
            sleep(DEFAULT_RETRY_DELAY_SECONDS * (attempt + 1))
        except requests.RequestException as exc:
            raise DessMonitorTransportError(f"Request to DessMonitor failed: {exc}") from exc
        except ValueError as exc:
            raise DessMonitorApiError("DessMonitor returned a non-JSON response.") from exc
    else:  # pragma: no cover - defensive fallback
        if last_error is not None:
            raise DessMonitorApiError(f"Request to DessMonitor failed: {last_error}") from last_error
        raise DessMonitorApiError("Request to DessMonitor failed for an unknown reason.")

    if payload.get("err") != 0:
        raise DessMonitorApiError(payload.get("desc", "DessMonitor returned an unknown API error."))

    _remember_energyflow_response(params, payload)
    _remember_lastdata_response(params, payload)
    return payload


def _authentication_params(config: DessMonitorConfig, *, include_company_key: bool) -> OrderedDict[str, str]:
    params = OrderedDict(
        [
            ("action", "authSource"),
            ("usr", config.username),
            ("source", str(config.source)),
        ]
    )
    if include_company_key and config.company_key.strip():
        params["company-key"] = config.company_key.strip()
    salt = _salt()
    password_hash = _sha1_hex(config.password)
    uri_str = _transfer_uri_str(params)
    signed_params = OrderedDict(
        [
            ("sign", _sha1_hex(f"{salt}{password_hash}&{uri_str}")),
            ("salt", salt),
        ]
    )
    signed_params.update(params)
    return signed_params


def _auth_cache_key(config: DessMonitorConfig) -> tuple[str, str, str, str]:
    return (
        config.username.strip(),
        config.company_key.strip(),
        str(config.source),
        config.password,
    )


def authenticate(config: DessMonitorConfig, logger: callable | None = None) -> AuthTokens:
    cache_key = _auth_cache_key(config)
    now = time()
    with _AUTH_CACHE_LOCK:
        cached = _AUTH_CACHE.get(cache_key)
        if cached is not None and cached.expires_at > now:
            if logger:
                logger("Using cached authentication token.")
            return cached.tokens

    attempts = list(DEFAULT_AUTH_ATTEMPTS)
    if config.company_key.strip():
        attempts = [
            AuthAttempt("Trying authentication with company key", include_company_key=True),
            AuthAttempt("Retrying authentication without company key", include_company_key=False),
        ]

    errors: list[str] = []
    for attempt in attempts:
        if attempt.include_company_key and not config.company_key.strip():
            continue
        if logger:
            logger(attempt.description)
        try:
            tokens = _authenticate_once(config, include_company_key=attempt.include_company_key)
            with _AUTH_CACHE_LOCK:
                _AUTH_CACHE[cache_key] = CachedAuthTokens(
                    tokens=tokens,
                    expires_at=now + AUTH_CACHE_TTL_SECONDS,
                )
            return tokens
        except DessMonitorTransportError as exc:
            errors.append(str(exc))
            if logger:
                logger(f"Authentication transport failure: {exc}")
            # Do not retry with/without company-key when transport failed:
            # the fallback may add misleading API errors on top of TLS/network failures.
            break
        except DessMonitorApiError as exc:
            errors.append(str(exc))
            if logger:
                logger(f"Authentication failed: {exc}")

    if not config.company_key.strip() and any("ERR_MISSING_PARAMETER" in error for error in errors):
        raise DessMonitorApiError(
            "ERR_MISSING_PARAMETER during authSource. This account likely requires a company-key."
        )

    joined_errors = " | ".join(_dedupe_preserving_order(errors)) if errors else "Authentication failed."
    raise DessMonitorApiError(joined_errors)


def sanitize_error_text(text: str) -> str:
    return re.sub(r"(?i)(pwd|password)=([^&'\s]+)", r"\1=***", text)


def humanize_error_text(text: str) -> str:
    sanitized = sanitize_error_text(text)

    def replace_known(match: re.Match[str]) -> str:
        code = match.group(0)
        return ERROR_DESCRIPTIONS.get(code, code)

    result = re.sub(r"0x[0-9A-Fa-f]{4}|ERR_[A-Z0-9_]+", replace_known, sanitized)
    result = re.sub(r"\s{2,}", " ", result).strip()
    return result


def _authenticate_once(config: DessMonitorConfig, *, include_company_key: bool) -> AuthTokens:
    params = _authentication_params(config, include_company_key=include_company_key)
    payload = _request_json(params, base_url=WEB_AUTH_URL)
    data = payload.get("dat") or {}
    token = data.get("token")
    secret = data.get("secret")
    if not token or not secret:
        raise DessMonitorApiError("DessMonitor authentication succeeded without returning token and secret.")
    return AuthTokens(token=token, secret=secret)


def call_api(
    *,
    action: str,
    auth: AuthTokens,
    source: int,
    app_client: str,
    app_id: str,
    app_version: str,
    params: OrderedDict[str, str],
) -> dict:
    salt = _salt()
    ordered = OrderedDict([("action", action)])
    ordered.update(params)
    ordered["source"] = str(source)
    ordered["_app_client_"] = app_client
    ordered["_app_id_"] = app_id
    ordered["_app_version_"] = app_version

    query_suffix = "".join(f"&{key}={value}" for key, value in ordered.items())
    sign = _sha1_hex(f"{salt}{auth.secret}{auth.token}{query_suffix}")

    request_params = OrderedDict(
        [
            ("sign", sign),
            ("salt", salt),
            ("token", auth.token),
        ]
    )
    request_params.update(ordered)
    return _request_json(request_params)


def _iter_numeric_values(value: Any, *, key_hint: str = ""):
    ignored_keys = {"year", "month", "day", "date", "ts", "gts", "id", "page", "pagesize", "total"}
    lowered_hint = key_hint.strip().lower()
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            yield from _iter_numeric_values(child_value, key_hint=str(child_key))
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_numeric_values(item, key_hint=key_hint)
        return
    if lowered_hint in ignored_keys:
        return
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        yield float(value)
        return
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return
        try:
            yield float(text)
        except ValueError:
            LOGGER.debug("DessMonitor payload numeric parse failed for value: %r", value)
            return


def _payload_has_time_series_data(payload: dict[str, Any]) -> bool:
    dat = payload.get("dat")
    if dat is None:
        return False
    return any(True for _ in _iter_numeric_values(dat))


def _iter_nested_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_nested_dicts(child)
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_nested_dicts(item)


def _stringify_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def _first_present(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
    lowered = {str(key).lower(): value for key, value in mapping.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, ""):
            return value
    return None


def _coerce_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = _stringify_scalar(value).lower()
    if text in {"1", "true", "yes", "y", "enabled", "writable", "write"}:
        return True
    if text in {"0", "false", "no", "n", "disabled", "readonly", "read"}:
        return False
    return None


def _category_for_setting(name: str, field_id: str) -> str:
    haystack = f"{name} {field_id}".lower()
    if any(token in haystack for token in ("battery", "soc", "bms", "cell", "temperature")):
        return "Battery"
    if any(token in haystack for token in ("charge", "discharge", "charging", "discharging", "eps", "backup")):
        return "Charging"
    if any(token in haystack for token in ("grid", "export", "import", "meter", "utility")):
        return "Grid"
    if any(token in haystack for token in ("pv", "solar", "mppt", "string")):
        return "PV"
    if any(token in haystack for token in ("tou", "time", "schedule", "period", "timer", "peak", "valley")):
        return "Time-of-use"
    if any(token in haystack for token in ("protection", "fault", "alarm", "limit", "voltage", "frequency")):
        return "Protection"
    if any(token in haystack for token in ("mode", "language", "system", "device", "firmware", "machine")):
        return "System"
    return "Other"


def _extract_control_options(item: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    option_container = _first_present(
        item,
        (
            "options",
            "option",
            "item",
            "items",
            "enum",
            "enums",
            "valueEnum",
            "valueEnums",
            "select",
            "selects",
        ),
    )
    if not isinstance(option_container, list):
        return ()

    normalized: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for option in option_container:
        if not isinstance(option, dict):
            continue
        # DessMonitor often returns control enumerations as {key: "<code>", val: "<label>"}.
        # Prefer code-like fields for raw value and text-like fields for display label.
        raw_value = _first_present(option, ("value", "id", "key", "code", "val"))
        raw_label = _first_present(option, ("label", "name", "text", "desc", "description", "title", "val"))
        value_text = _stringify_scalar(raw_value)
        label_text = _stringify_scalar(raw_label) or value_text
        if not value_text:
            continue
        pair = (value_text, label_text)
        if pair in seen:
            continue
        seen.add(pair)
        normalized.append(pair)
    return tuple(normalized)


def _parse_control_fields(payload: dict[str, Any]) -> list[DeviceControlField]:
    candidates: list[dict[str, Any]] = []
    for item in _iter_nested_dicts(payload.get("dat") or payload):
        field_id = _first_present(item, ("id", "fieldId", "ctrlFieldId", "key"))
        name = _first_present(item, ("name", "fieldName", "ctrlFieldName", "label", "title"))
        if _stringify_scalar(field_id) and _stringify_scalar(name):
            candidates.append(item)

    fields: list[DeviceControlField] = []
    seen_ids: set[str] = set()
    for item in candidates:
        field_id = _stringify_scalar(_first_present(item, ("id", "fieldId", "ctrlFieldId", "key")))
        if not field_id or field_id in seen_ids:
            continue
        seen_ids.add(field_id)
        raw_name = _stringify_scalar(_first_present(item, ("name", "fieldName", "ctrlFieldName", "label", "title")))
        unit = _stringify_scalar(_first_present(item, ("unit", "units", "unitName")))
        hint = _stringify_scalar(_first_present(item, ("hint",)))
        raw_write_access = _first_present(item, ("writable", "writeable", "editable", "canWrite", "write"))
        writable_value = _coerce_bool(raw_write_access)
        writable = bool(writable_value)
        if writable:
            access_status_text = "Writable"
        elif writable_value is False:
            access_status_text = "Readable (read-only from API)"
        else:
            access_status_text = "Readable (API did not provide write flag)"
        fields.append(
            DeviceControlField(
                field_id=field_id,
                name=raw_name,
                raw_name=raw_name,
                unit=unit,
                hint=hint,
                category=_category_for_setting(raw_name, field_id),
                writable=writable,
                access_status_text=access_status_text,
                options=_extract_control_options(item),
            )
        )

    if not fields:
        raise DessMonitorApiError("DessMonitor did not return any inverter control fields for this device.")

    fields.sort(key=lambda field: (SETTING_CATEGORY_ORDER.get(field.category, 999), field.name.lower(), field.field_id))
    return fields


def _extract_control_value(payload: dict[str, Any]) -> str | int | float | None:
    dat = payload.get("dat") or payload
    if isinstance(dat, dict):
        scalar = _first_present(
            dat,
            ("value", "val", "currentValue", "fieldValue", "ctrlValue", "data", "result"),
        )
        if isinstance(scalar, (str, int, float)) and _stringify_scalar(scalar):
            return scalar

    for item in _iter_nested_dicts(dat):
        candidate = _first_present(
            item,
            ("value", "val", "currentValue", "fieldValue", "ctrlValue", "result"),
        )
        if isinstance(candidate, (str, int, float)) and _stringify_scalar(candidate):
            return candidate
    return None


def _format_control_value(raw_value: str | int | float | None, options: tuple[tuple[str, str], ...]) -> str:
    if raw_value is None:
        return "Not available"
    raw_text = _stringify_scalar(raw_value)
    option_lookup = {value: label for value, label in options}
    if raw_text in option_lookup:
        return option_lookup[raw_text]
    return raw_text or "Not available"


def _settings_request_params(
    config: DessMonitorConfig,
    extra: list[tuple[str, str]] | None = None,
) -> OrderedDict[str, str]:
    params = OrderedDict(
        [
            ("pn", config.pn),
            ("devcode", config.devcode),
            ("devaddr", config.devaddr),
            ("sn", config.sn),
            ("i18n", config.i18n),
        ]
    )
    for key, value in extra or []:
        params[key] = value
    return params


def _remember_device_location_response(payload: dict[str, Any]) -> None:
    global _LAST_DEVICE_LOCATION_RESPONSE_TEXT
    _LAST_DEVICE_LOCATION_RESPONSE_TEXT = json.dumps(payload, indent=2, ensure_ascii=False)


def _normalize_address_dict(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key).strip().lower(): _stringify_scalar(item) for key, item in value.items()}


def _extract_device_pid(payload: dict[str, Any]) -> str:
    for item in _iter_nested_dicts(payload.get("dat") or payload):
        pid = _stringify_scalar(_first_present(item, ("pid", "plantid", "plantId")))
        if pid:
            return pid
    return ""


def _iter_plants(payload: dict[str, Any]):
    dat = payload.get("dat") or {}
    plants = dat.get("plant") or dat.get("plants") or []
    if isinstance(plants, list):
        for item in plants:
            if isinstance(item, dict):
                yield item


def _location_from_plant(plant: dict[str, Any], *, fallback_pid: str = "") -> DeviceLocation:
    address = _normalize_address_dict(plant.get("address"))
    pid = _stringify_scalar(_first_present(plant, ("pid", "plantid", "plantId"))) or fallback_pid
    return DeviceLocation(
        pid=pid,
        latitude=address.get("lat", ""),
        longitude=address.get("lon", ""),
        timezone=address.get("timezone", ""),
        country=address.get("country", ""),
        province=address.get("province", ""),
        city=address.get("city", ""),
        county=address.get("county", ""),
        town=address.get("town", ""),
        village=address.get("village", ""),
        address=address.get("address", ""),
    )


def fetch_device_location(config: DessMonitorConfig, logger: callable | None = None) -> DeviceLocation | None:
    auth = authenticate(config, logger=logger)
    if logger:
        logger("Loading device location metadata...")

    device_payload = call_api(
        action="queryDevices",
        auth=auth,
        source=config.source,
        app_client=config.app_client,
        app_id=config.app_id,
        app_version=config.app_version,
        params=OrderedDict(
            [
                ("page", "0"),
                ("pagesize", "50"),
            ]
        ),
    )
    target_key = (config.pn, config.devcode, config.devaddr, config.sn)
    matched_device = next(
        (
            item
            for item in _iter_nested_dicts(device_payload.get("dat") or device_payload)
            if (
                _stringify_scalar(_first_present(item, ("pn",))) == target_key[0]
                and _stringify_scalar(_first_present(item, ("devcode",))) == target_key[1]
                and _stringify_scalar(_first_present(item, ("devaddr",))) == target_key[2]
                and _stringify_scalar(_first_present(item, ("sn",))) == target_key[3]
            )
        ),
        None,
    )
    pid = _stringify_scalar(_first_present(matched_device or {}, ("pid", "plantid", "plantId")))
    if not pid:
        _remember_device_location_response(
            {
                "deviceInfo": device_payload,
                "plants": {"note": "Device location lookup stopped because PID was not returned."},
            }
        )
        return None

    page = 0
    page_size = 50
    matched_location: DeviceLocation | None = None
    plant_payloads: list[dict[str, Any]] = []

    while matched_location is None:
        plants_payload = call_api(
            action="queryPlants",
            auth=auth,
            source=config.source,
            app_client=config.app_client,
            app_id=config.app_id,
            app_version=config.app_version,
            params=OrderedDict(
                [
                    ("page", str(page)),
                    ("pagesize", str(page_size)),
                    ("i18n", config.i18n),
                ]
            ),
        )
        plant_payloads.append(plants_payload)
        matched_plant = next(
            (
                plant
                for plant in _iter_plants(plants_payload)
                if _stringify_scalar(_first_present(plant, ("pid", "plantid", "plantId"))) == pid
            ),
            None,
        )
        if matched_plant is not None:
            matched_location = _location_from_plant(matched_plant, fallback_pid=pid)
            break

        dat = plants_payload.get("dat") or {}
        current_page = int(dat.get("page", page))
        total = int(dat.get("total", 0) or 0)
        if total and (current_page + 1) * page_size >= total:
            break
        if not any(True for _ in _iter_plants(plants_payload)):
            break
        page += 1

    _remember_device_location_response(
        {
            "deviceInfo": device_payload,
            "plants": plant_payloads,
            "matchedLocation": asdict(matched_location) if matched_location is not None else None,
        }
    )
    return matched_location


def _control_fields_cache_key(config: DessMonitorConfig) -> tuple[str, str, str, str]:
    return (config.pn, config.devcode, config.devaddr, config.sn)


def _control_value_cache_key(config: DessMonitorConfig, field_id: str) -> tuple[str, str, str, str, str]:
    return (config.pn, config.devcode, config.devaddr, config.sn, field_id)


def _control_write_cache_key(config: DessMonitorConfig) -> tuple[str, str, str, str]:
    return (config.pn, config.devcode, config.devaddr, config.sn)


def _history_probe_params(
    config: DessMonitorConfig,
    *,
    parameter: str,
    extra: list[tuple[str, str]],
) -> OrderedDict[str, str]:
    params = OrderedDict(
        [
            ("pn", config.pn),
            ("devcode", config.devcode),
            ("devaddr", config.devaddr),
            ("sn", config.sn),
            ("parameter", parameter),
        ]
    )
    for key, value in extra:
        params[key] = value
    params["i18n"] = config.i18n
    return params


def _probe_year_has_data(
    config: DessMonitorConfig,
    auth: AuthTokens,
    *,
    parameter: str,
    target_year: int,
) -> bool:
    payload = call_api(
        action="querySPDeviceKeyParameterYearPerMonth",
        auth=auth,
        source=config.source,
        app_client=config.app_client,
        app_id=config.app_id,
        app_version=config.app_version,
        params=_history_probe_params(
            config,
            parameter=parameter,
            extra=[("year", str(target_year))],
        ),
    )
    return _payload_has_time_series_data(payload)


def _probe_month_has_data(
    config: DessMonitorConfig,
    auth: AuthTokens,
    *,
    parameter: str,
    target_year: int,
    target_month: int,
) -> bool:
    payload = call_api(
        action="querySPDeviceKeyParameterMonthPerDay",
        auth=auth,
        source=config.source,
        app_client=config.app_client,
        app_id=config.app_id,
        app_version=config.app_version,
        params=_history_probe_params(
            config,
            parameter=parameter,
            extra=[("year", str(target_year)), ("month", str(target_month))],
        ),
    )
    return _payload_has_time_series_data(payload)


def _probe_day_has_data(
    config: DessMonitorConfig,
    auth: AuthTokens,
    *,
    parameter: str,
    target_day: date,
) -> bool:
    payload = call_api(
        action="querySPDeviceKeyParameterOneDay",
        auth=auth,
        source=config.source,
        app_client=config.app_client,
        app_id=config.app_id,
        app_version=config.app_version,
        params=_history_probe_params(
            config,
            parameter=parameter,
            extra=[("date", target_day.isoformat())],
        ),
    )
    return _payload_has_time_series_data(payload)


def _detect_first_data_date_with_aggregate_probes(
    config: DessMonitorConfig,
    auth: AuthTokens,
    *,
    start_date: date,
    end_date: date,
    logger: callable | None = None,
) -> str | None:
    aggregate_errors: list[str] = []
    for parameter in DEFAULT_HISTORY_AGGREGATE_PARAMETERS:
        if logger:
            logger(f"Probing remote history with aggregate parameter {parameter}...")
        try:
            first_year: int | None = None
            for target_year in range(start_date.year, end_date.year + 1):
                if _probe_year_has_data(config, auth, parameter=parameter, target_year=target_year):
                    first_year = target_year
                    break
            if first_year is None:
                continue

            first_month: int | None = None
            month_start = start_date.month if first_year == start_date.year else 1
            month_end = end_date.month if first_year == end_date.year else 12
            for target_month in range(month_start, month_end + 1):
                if _probe_month_has_data(
                    config,
                    auth,
                    parameter=parameter,
                    target_year=first_year,
                    target_month=target_month,
                ):
                    first_month = target_month
                    break
            if first_month is None:
                continue

            month_first_day, month_last_day = _month_range(first_year, first_month)
            if first_year == start_date.year and first_month == start_date.month:
                month_first_day = max(month_first_day, start_date)
            if first_year == end_date.year and first_month == end_date.month:
                month_last_day = min(month_last_day, end_date)

            for day_parameter in DEFAULT_HISTORY_DAY_PARAMETERS:
                for offset in range((month_last_day - month_first_day).days + 1):
                    candidate_day = month_first_day + timedelta(days=offset)
                    if _probe_day_has_data(config, auth, parameter=day_parameter, target_day=candidate_day):
                        return candidate_day.isoformat()
        except DessMonitorApiError as exc:
            aggregate_errors.append(str(exc))
            if logger:
                logger(f"Aggregate history probe with {parameter} failed: {exc}")

    if logger and aggregate_errors:
        logger("Aggregate history probes were unavailable. Falling back to day-by-day history discovery.")
    return None


def _detect_first_data_date_with_day_probes(
    config: DessMonitorConfig,
    auth: AuthTokens,
    *,
    start_date: date,
    end_date: date,
    logger: callable | None = None,
) -> str | None:
    candidate_parameters = list(OrderedDict.fromkeys(DEFAULT_HISTORY_DAY_PARAMETERS + _normalize_parameter_keys(config.parameter_keys)))
    for target_day in _date_range(start_date.isoformat(), end_date.isoformat()):
        probe_day = date.fromisoformat(target_day)
        for parameter in candidate_parameters:
            try:
                if _probe_day_has_data(config, auth, parameter=parameter, target_day=probe_day):
                    return probe_day.isoformat()
            except DessMonitorApiError as exc:
                LOGGER.debug(
                    "DessMonitor first-data probe failed for %s parameter %s: %s",
                    probe_day.isoformat(),
                    parameter,
                    exc,
                )
                continue
        if logger and probe_day.day == 1:
            logger(f"Scanned up to {probe_day.isoformat()} while locating remote history start...")
    return None


def detect_remote_first_data_date(
    config: DessMonitorConfig,
    *,
    logger: callable | None = None,
    auth: AuthTokens | None = None,
) -> str:
    if auth is None:
        auth = authenticate(config, logger=logger)

    today = date.today()
    start_date = today - timedelta(days=365 * 5)
    normalized_start = str(getattr(config, "date_from", "") or "").strip()
    if normalized_start:
        try:
            candidate_start = date.fromisoformat(normalized_start)
        except ValueError:
            candidate_start = start_date
        else:
            if candidate_start <= today:
                start_date = candidate_start
    end_date = today
    if logger:
        logger(f"Detecting earliest remote data between {start_date.isoformat()} and {end_date.isoformat()}...")

    detected = _detect_first_data_date_with_aggregate_probes(
        config,
        auth,
        start_date=start_date,
        end_date=end_date,
        logger=logger,
    )
    if detected is not None:
        return detected

    detected = _detect_first_data_date_with_day_probes(
        config,
        auth,
        start_date=start_date,
        end_date=end_date,
        logger=logger,
    )
    if detected is not None:
        return detected

    raise DessMonitorApiError("Could not detect the first remote date with available data for this device.")

def _build_parameter_frame(parameter_key: str, payload: dict) -> pd.DataFrame:
    detail = (payload.get("dat") or {}).get("detail") or []
    rows: list[dict[str, object]] = []
    for item in detail:
        timestamp = item.get("gts") or item.get("ts")
        value = item.get("val")
        if timestamp is None or value is None:
            continue
        rows.append({"Timestamp": timestamp, parameter_key: value})

    if not rows:
        raise DessMonitorApiError(f"No detail rows were returned for parameter {parameter_key}.")

    return pd.DataFrame(rows)


def build_timeseries_dataframe(parameter_frames: list[pd.DataFrame]) -> pd.DataFrame:
    if not parameter_frames:
        raise DessMonitorApiError("DessMonitor did not return any parameter data.")

    normalized_frames: list[pd.DataFrame] = []
    for frame in parameter_frames:
        normalized = frame.copy()
        normalized["Timestamp"] = pd.to_datetime(normalized["Timestamp"], errors="coerce")
        normalized.dropna(subset=["Timestamp"], inplace=True)
        if normalized.empty:
            continue
        normalized.sort_values("Timestamp", inplace=True)
        normalized = normalized.drop_duplicates(subset=["Timestamp"], keep="last")
        normalized_frames.append(normalized)

    if not normalized_frames:
        raise DessMonitorApiError("DessMonitor did not return any valid timestamp rows.")

    merged = normalized_frames[0]
    for frame in normalized_frames[1:]:
        merged = merged.merge(frame, on="Timestamp", how="outer")

    merged.sort_values("Timestamp", inplace=True)
    merged = merged.drop_duplicates(subset=["Timestamp"], keep="last")
    merged["Timestamp"] = merged["Timestamp"].dt.strftime("%Y-%m-%d %H:%M:%S")
    merged.reset_index(drop=True, inplace=True)
    return merged


def fetch_devices(config: DessMonitorConfig, logger: callable | None = None) -> list[DessMonitorDevice]:
    auth = authenticate(config, logger=logger)
    if logger:
        logger("Authentication succeeded.")

    devices: list[DessMonitorDevice] = []
    page = 0
    page_size = 50
    total = None

    while total is None or len(devices) < total:
        if logger:
            logger(f"Loading devices page {page + 1}...")
        payload = call_api(
            action="queryDevices",
            auth=auth,
            source=config.source,
            app_client=config.app_client,
            app_id=config.app_id,
            app_version=config.app_version,
            params=OrderedDict(
                [
                    ("page", str(page)),
                    ("pagesize", str(page_size)),
                ]
            ),
        )
        data = payload.get("dat") or {}
        page_devices = data.get("device") or []
        total = int(data.get("total", len(page_devices)))
        if not page_devices:
            break

        for item in page_devices:
            pn = str(item.get("pn", "")).strip()
            devcode = str(item.get("devcode", "")).strip()
            devaddr = str(item.get("devaddr", "")).strip()
            sn = str(item.get("sn", "")).strip()
            if not (pn and devcode and devaddr and sn):
                continue
            devices.append(
                DessMonitorDevice(
                    pn=pn,
                    devcode=devcode,
                    devaddr=devaddr,
                    sn=sn,
                    alias=str(item.get("alias", "")).strip(),
                    pid=str(item.get("pid", "")).strip(),
                    status=str(item.get("status", "")).strip(),
                    install_date=str(item.get("install", "") or item.get("gts", "")).strip()[:10],
                )
            )

        if len(page_devices) < page_size:
            break
        page += 1

    unique_devices: list[DessMonitorDevice] = []
    seen: set[tuple[str, str, str, str]] = set()
    for device in devices:
        key = (device.pn, device.devcode, device.devaddr, device.sn)
        if key in seen:
            continue
        seen.add(key)
        unique_devices.append(device)

    if not unique_devices:
        raise DessMonitorApiError("No devices were returned for this DessMonitor account.")

    if logger:
        logger(f"Loaded {len(unique_devices)} unique device(s).")

    return unique_devices


def fetch_key_parameters(
    config: DessMonitorConfig,
    *,
    devcode: str,
    logger: callable | None = None,
) -> list[str]:
    auth = authenticate(config, logger=logger)
    if logger:
        logger(f"Loading available parameter keys for protocol {devcode}...")

    payload = call_api(
        action="querySPKeyParameters",
        auth=auth,
        source=config.source,
        app_client=config.app_client,
        app_id=config.app_id,
        app_version=config.app_version,
        params=OrderedDict([("devcode", devcode)]),
    )
    keys = (payload.get("dat") or {}).get("keys") or []
    normalized = [str(key).strip() for key in keys if str(key).strip()]
    if not normalized:
        raise DessMonitorApiError("DessMonitor did not return any parameter keys for this device protocol.")
    return normalized


def _fetch_key_parameters_with_auth(
    config: DessMonitorConfig,
    auth: AuthTokens,
    *,
    devcode: str,
) -> list[str]:
    payload = call_api(
        action="querySPKeyParameters",
        auth=auth,
        source=config.source,
        app_client=config.app_client,
        app_id=config.app_id,
        app_version=config.app_version,
        params=OrderedDict([("devcode", devcode)]),
    )
    keys = (payload.get("dat") or {}).get("keys") or []
    normalized = [str(key).strip() for key in keys if str(key).strip()]
    if not normalized:
        raise DessMonitorApiError("DessMonitor did not return any parameter keys for this device protocol.")
    return normalized


def fetch_device_control_fields(
    config: DessMonitorConfig,
    *,
    logger: callable | None = None,
    auth: AuthTokens | None = None,
    force_refresh: bool = False,
) -> list[DeviceControlField]:
    cache_key = _control_fields_cache_key(config)
    if not force_refresh:
        with _SETTINGS_CACHE_LOCK:
            cached = _CONTROL_FIELDS_CACHE.get(cache_key)
        if cached is not None and cached[0] > time():
            if logger:
                logger(f"Using cached inverter control fields ({len(cached[1])}).")
            return list(cached[1])

    if auth is None:
        auth = authenticate(config, logger=logger)
    if logger:
        logger("Loading inverter control fields from DessMonitor...")

    payload = call_api(
        action="queryDeviceCtrlField",
        auth=auth,
        source=config.source,
        app_client=config.app_client,
        app_id=config.app_id,
        app_version=config.app_version,
        params=_settings_request_params(config),
    )
    fields = _parse_control_fields(payload)
    with _SETTINGS_CACHE_LOCK:
        _CONTROL_FIELDS_CACHE[cache_key] = (time() + SETTINGS_CACHE_TTL_SECONDS, list(fields))
    if logger:
        logger(f"Loaded {len(fields)} inverter control field(s).")
    return fields


def fetch_device_control_value(
    config: DessMonitorConfig,
    field_id: str,
    *,
    auth: AuthTokens | None = None,
    force_refresh: bool = False,
) -> str | int | float | None:
    cache_key = _control_value_cache_key(config, field_id)
    if not force_refresh:
        with _SETTINGS_CACHE_LOCK:
            cached = _CONTROL_VALUE_CACHE.get(cache_key)
        if cached is not None and cached[0] > time():
            return cached[1]

    if auth is None:
        auth = authenticate(config)

    payload = call_api(
        action="queryDeviceCtrlValue",
        auth=auth,
        source=config.source,
        app_client=config.app_client,
        app_id=config.app_id,
        app_version=config.app_version,
        params=_settings_request_params(config, [("id", field_id)]),
    )
    value = _extract_control_value(payload)
    with _SETTINGS_CACHE_LOCK:
        _CONTROL_VALUE_CACHE[cache_key] = (time() + SETTINGS_CACHE_TTL_SECONDS, value)
    return value


def _is_unknown_action_error(error_text: str) -> bool:
    lowered = str(error_text or "").strip().lower()
    if not lowered:
        return False
    patterns = (
        "unknown action",
        "action not",
        "unsupported action",
        "not support",
        "not supported",
        "invalid action",
        "method not",
    )
    return any(pattern in lowered for pattern in patterns)


def _set_device_control_value_with_action(
    config: DessMonitorConfig,
    *,
    auth: AuthTokens,
    action: str,
    field_id: str,
    value: str,
) -> None:
    call_api(
        action=action,
        auth=auth,
        source=config.source,
        app_client=config.app_client,
        app_id=config.app_id,
        app_version=config.app_version,
        params=_settings_request_params(config, [("id", field_id), ("value", value)]),
    )
    cache_key = _control_value_cache_key(config, field_id)
    with _SETTINGS_CACHE_LOCK:
        _CONTROL_VALUE_CACHE[cache_key] = (time() + SETTINGS_CACHE_TTL_SECONDS, value)


def set_device_control_values(
    config: DessMonitorConfig,
    updates: list[tuple[str, str]],
    *,
    auth: AuthTokens | None = None,
) -> None:
    normalized_updates: list[tuple[str, str]] = []
    seen: set[str] = set()
    for field_id_raw, value_raw in updates:
        field_id = str(field_id_raw).strip()
        if not field_id or field_id in seen:
            continue
        seen.add(field_id)
        normalized_updates.append((field_id, str(value_raw).strip()))
    if not normalized_updates:
        return

    if auth is None:
        auth = authenticate(config)

    cache_key = _control_write_cache_key(config)
    with _SETTINGS_CACHE_LOCK:
        cached_action = _CONTROL_WRITE_ACTION_CACHE.get(cache_key)

    if cached_action:
        try:
            for field_id, value in normalized_updates:
                _set_device_control_value_with_action(
                    config,
                    auth=auth,
                    action=cached_action,
                    field_id=field_id,
                    value=value,
                )
            return
        except DessMonitorApiError:
            with _SETTINGS_CACHE_LOCK:
                _CONTROL_WRITE_ACTION_CACHE.pop(cache_key, None)

    candidate_actions = list(DEFAULT_CTRL_WRITE_ACTION_CANDIDATES)
    last_error: DessMonitorApiError | None = None
    for action_name in candidate_actions:
        try:
            for field_id, value in normalized_updates:
                _set_device_control_value_with_action(
                    config,
                    auth=auth,
                    action=action_name,
                    field_id=field_id,
                    value=value,
                )
            with _SETTINGS_CACHE_LOCK:
                _CONTROL_WRITE_ACTION_CACHE[cache_key] = action_name
            return
        except DessMonitorApiError as exc:
            last_error = exc
            if not _is_unknown_action_error(str(exc)):
                raise
            continue

    if last_error is not None:
        raise last_error
    raise DessMonitorApiError("DessMonitor did not accept any known inverter control write action.")


def fetch_inverter_settings(
    config: DessMonitorConfig,
    *,
    logger: callable | None = None,
) -> list[InverterSetting]:
    auth = authenticate(config, logger=logger)
    fields = fetch_device_control_fields(config, logger=logger, auth=auth)
    settings: list[InverterSetting] = []

    for index, field in enumerate(fields, start=1):
        if logger:
            logger(f"Reading setting {index}/{len(fields)}: {field.name or field.field_id}")
        try:
            raw_value = fetch_device_control_value(config, field.field_id, auth=auth)
            display_value = _format_control_value(raw_value, field.options)
        except DessMonitorApiError as exc:
            raw_value = None
            display_value = "Not available"
            if logger:
                logger(f"Failed to read {field.name or field.field_id}: {humanize_error_text(str(exc))}")

        settings.append(
            InverterSetting(
                field_id=field.field_id,
                name=field.name,
                display_name=field.name,
                raw_value=raw_value,
                display_value=display_value,
                unit=field.unit,
                hint=field.hint,
                category=field.category,
                writable=field.writable,
                options=field.options,
                raw_name=field.raw_name,
            )
        )

    settings.sort(
        key=lambda item: (
            SETTING_CATEGORY_ORDER.get(item.category, 999),
            item.display_name.lower(),
            item.field_id,
        )
    )
    return settings


def fetch_device_day_data(config: DessMonitorConfig) -> pd.DataFrame:
    return fetch_device_range_data(config)


def fetch_device_range_data(
    config: DessMonitorConfig,
    logger: callable | None = None,
    progress_callback: callable | None = None,
    auth: AuthTokens | None = None,
) -> pd.DataFrame:
    if auth is None:
        auth = authenticate(config, logger=logger)
    if config.parameter_keys:
        parameter_keys = _normalize_parameter_keys(config.parameter_keys)
        if logger:
            logger(f"Using {len(parameter_keys)} parameter key(s) from app settings.")
    else:
        if logger:
            logger("Loading all available parameter keys for the selected device protocol...")
        parameter_keys = _fetch_key_parameters_with_auth(config, auth, devcode=config.devcode)
        if logger:
            logger(f"Loaded {len(parameter_keys)} available parameter key(s) from DessMonitor.")

    parameter_frames: list[pd.DataFrame] = []
    errors: list[str] = []
    requested_dates = _date_range(config.date_from, config.date_to)
    total_steps = max(1, len(parameter_keys) * len(requested_dates))
    completed_steps = 0
    if logger:
        logger(
            f"Fetching {len(parameter_keys)} parameter(s) across {len(requested_dates)} day(s) "
            f"from {config.date_from} to {config.date_to}."
        )
    def fetch_parameter_day(parameter_key: str, requested_date: str) -> pd.DataFrame:
        payload = call_api(
            action="querySPDeviceKeyParameterOneDay",
            auth=auth,
            source=config.source,
            app_client=config.app_client,
            app_id=config.app_id,
            app_version=config.app_version,
            params=OrderedDict(
                [
                    ("pn", config.pn),
                    ("devcode", config.devcode),
                    ("devaddr", config.devaddr),
                    ("sn", config.sn),
                    ("parameter", parameter_key),
                    ("date", requested_date),
                    ("i18n", config.i18n),
                ]
            ),
        )
        return _build_parameter_frame(parameter_key, payload)

    daily_frames_by_parameter: dict[str, list[tuple[str, pd.DataFrame]]] = {
        parameter_key: [] for parameter_key in parameter_keys
    }
    max_workers = min(DEFAULT_PARALLEL_DATA_REQUESTS, total_steps)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {
            executor.submit(fetch_parameter_day, parameter_key, requested_date): (parameter_key, requested_date)
            for parameter_key in parameter_keys
            for requested_date in requested_dates
        }
        for future in as_completed(future_map):
            parameter_key, requested_date = future_map[future]
            try:
                frame = future.result()
                daily_frames_by_parameter[parameter_key].append((requested_date, frame))
                if logger:
                    logger(f"Received {parameter_key} data for {requested_date}.")
            except DessMonitorApiError as exc:
                errors.append(f"{parameter_key} on {requested_date}: {exc}")
                if logger:
                    logger(f"Failed {parameter_key} on {requested_date}: {exc}")

            completed_steps += 1
            if progress_callback:
                progress = 10 + int((completed_steps / total_steps) * 55)
                progress_callback(progress, f"Fetching {parameter_key} for {requested_date}")

    date_order = {requested_date: index for index, requested_date in enumerate(requested_dates)}
    for parameter_key in parameter_keys:
        daily_frames = [
            frame
            for _, frame in sorted(
                daily_frames_by_parameter[parameter_key],
                key=lambda item: date_order[item[0]],
            )
        ]
        if daily_frames:
            parameter_frames.append(pd.concat(daily_frames, ignore_index=True))
            if logger:
                logger(f"Completed parameter {parameter_key} with {len(daily_frames)} successful day(s).")

    if not parameter_frames:
        joined_errors = "; ".join(errors) if errors else "No data returned."
        raise DessMonitorApiError(f"Failed to fetch DessMonitor device data. {joined_errors}")

    if logger:
        logger("Merging all downloaded parameter series.")
    if progress_callback:
        progress_callback(70, "Merging downloaded data")
    return build_timeseries_dataframe(parameter_frames)
