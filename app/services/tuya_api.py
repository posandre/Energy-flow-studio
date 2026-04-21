from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode

import requests

from app.services.logging_utils import get_logger

REQUEST_TIMEOUT_SECONDS = 20
POWER_COMMAND_CONFIRMATION_TIMEOUT_SECONDS = 8
POWER_COMMAND_POLL_DELAY_SECONDS = 0.5
ACCESS_TOKEN_SAFETY_SECONDS = 60

_ACCESS_TOKEN_CACHE: dict[tuple[str, str], tuple[str, float]] = {}
DpMetaMap = dict[str, dict[str, object]]
LOGGER = get_logger(__name__)


class TuyaApiError(Exception):
    """Raised when Tuya API requests fail."""


@dataclass(slots=True)
class TuyaDevice:
    device_id: str
    name: str
    device_name: str
    icon_url: str
    online: bool
    status_text: str
    measurements_text: str = "No measurements"
    last_update_epoch: float | None = None
    power_on: bool | None = None


@dataclass(slots=True)
class TuyaAutomation:
    automation_id: str
    name: str
    enabled: bool
    home_id: str
    home_name: str
    action_count: int
    condition_count: int


def fetch_devices(
    *,
    client_id: str,
    client_secret: str,
    endpoint: str,
) -> list[TuyaDevice]:
    normalized_endpoint = endpoint.strip().rstrip("/")
    if not normalized_endpoint:
        raise TuyaApiError("TUYA_ENDPOINT is empty.")
    if not client_id.strip() or not client_secret.strip():
        raise TuyaApiError("TUYA_CLIENT_ID and TUYA_CLIENT_SECRET are required.")

    access_token = _fetch_access_token_cached(
        client_id=client_id.strip(),
        client_secret=client_secret.strip(),
        endpoint=normalized_endpoint,
    )
    payload = _tuya_request(
        endpoint=normalized_endpoint,
        client_id=client_id.strip(),
        client_secret=client_secret.strip(),
        method="GET",
        path="/v2.0/cloud/thing/device?page_size=20",
        access_token=access_token,
    )
    result = payload.get("result", [])
    if isinstance(result, dict):
        items = result.get("list", result.get("devices", []))
    else:
        items = result

    devices: list[TuyaDevice] = []
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            device_id = str(item.get("id", "")).strip()
            runtime_name, runtime_icon_url, online, status_text, measurements_text, power_on, _dp_meta, _detail, _spec, _status = _fetch_device_runtime_state(
                endpoint=normalized_endpoint,
                client_id=client_id.strip(),
                client_secret=client_secret.strip(),
                access_token=access_token,
                device_id=device_id,
                fallback_name=str(item.get("name", "")).strip(),
                fallback_online=_as_bool_or_none(item.get("online")),
                fallback_status=item.get("status"),
            )
            devices.append(
                TuyaDevice(
                    device_id=device_id,
                    name=runtime_name or "Unnamed device",
                    device_name=str(item.get("name", "")).strip() or runtime_name or "Unnamed device",
                    icon_url=runtime_icon_url or _extract_icon_url(item),
                    online=online,
                    status_text=status_text,
                    measurements_text=measurements_text,
                    last_update_epoch=time.time(),
                    power_on=power_on,
                )
            )
    return devices


def set_device_power(
    *,
    client_id: str,
    client_secret: str,
    endpoint: str,
    device_id: str,
    turn_on: bool,
 ) -> TuyaDevice:
    normalized_endpoint = endpoint.strip().rstrip("/")
    normalized_device_id = device_id.strip()
    if not normalized_endpoint:
        raise TuyaApiError("TUYA_ENDPOINT is empty.")
    if not client_id.strip() or not client_secret.strip():
        raise TuyaApiError("TUYA_CLIENT_ID and TUYA_CLIENT_SECRET are required.")
    if not normalized_device_id:
        raise TuyaApiError("Device ID is required.")

    clean_client_id = client_id.strip()
    clean_client_secret = client_secret.strip()
    access_token = _fetch_access_token_cached(
        client_id=clean_client_id,
        client_secret=clean_client_secret,
        endpoint=normalized_endpoint,
    )

    name, icon_url, online, status_text, measurements_text, power_on, dp_meta, detail_payload, specification_payload, status_payload = _fetch_device_runtime_state(
        endpoint=normalized_endpoint,
        client_id=clean_client_id,
        client_secret=clean_client_secret,
        access_token=access_token,
        device_id=normalized_device_id,
        fallback_name="",
        fallback_online=None,
        fallback_status=[],
    )
    switch_code = _pick_switch_code(status_payload)
    if not switch_code:
        raise TuyaApiError("Device does not expose a switch datapoint (switch/switch_1).")

    command_payload = _tuya_request(
        endpoint=normalized_endpoint,
        client_id=clean_client_id,
        client_secret=clean_client_secret,
        method="POST",
        path=f"/v1.0/iot-03/devices/{normalized_device_id}/commands",
        access_token=access_token,
        body={"commands": [{"code": switch_code, "value": turn_on}]},
    )

    started_at = time.monotonic()
    latest_status_text = status_text
    latest_measurements_text = measurements_text
    latest_power = power_on
    latest_status_payload: object = status_payload
    confirmed = False
    while time.monotonic() - started_at <= POWER_COMMAND_CONFIRMATION_TIMEOUT_SECONDS:
        polled_status_text, polled_measurements_text, polled_power, polled_status_payload = _fetch_device_status_state(
            endpoint=normalized_endpoint,
            client_id=clean_client_id,
            client_secret=clean_client_secret,
            access_token=access_token,
            device_id=normalized_device_id,
            fallback_status=[],
            dp_meta=dp_meta,
        )
        latest_status_text = polled_status_text
        latest_measurements_text = polled_measurements_text
        latest_power = polled_power
        latest_status_payload = polled_status_payload
        confirmed_power = polled_power
        if confirmed_power is not None and confirmed_power is turn_on:
            confirmed = True
            break
        time.sleep(POWER_COMMAND_POLL_DELAY_SECONDS)

    runtime_name = name
    runtime_icon_url = icon_url
    runtime_online = online
    runtime_status_text = latest_status_text
    runtime_measurements_text = latest_measurements_text
    runtime_power = latest_power
    runtime_detail = detail_payload
    runtime_status = latest_status_payload
    if not confirmed:
        raise TuyaApiError("Command accepted, but status change was not confirmed yet.")
    return TuyaDevice(
        device_id=normalized_device_id,
        name=runtime_name or "Unnamed device",
        device_name=runtime_name or "Unnamed device",
        icon_url=runtime_icon_url,
        online=runtime_online,
        status_text=runtime_status_text,
        measurements_text=runtime_measurements_text,
        last_update_epoch=time.time(),
        power_on=runtime_power,
    )


def _fetch_access_token(*, client_id: str, client_secret: str, endpoint: str) -> tuple[str, int]:
    payload = _tuya_request(
        endpoint=endpoint,
        client_id=client_id,
        client_secret=client_secret,
        method="GET",
        path="/v1.0/token?grant_type=1",
        access_token=None,
    )
    result = payload.get("result") if isinstance(payload, dict) else {}
    token = str((result or {}).get("access_token", "")).strip()
    if not token:
        raise TuyaApiError("Failed to get Tuya access token.")
    expires_in = int((result or {}).get("expire_time", 7200))
    return token, expires_in


def _fetch_access_token_cached(*, client_id: str, client_secret: str, endpoint: str) -> str:
    cache_key = (endpoint, client_id)
    now = time.time()
    cached = _ACCESS_TOKEN_CACHE.get(cache_key)
    if cached is not None:
        token, valid_until = cached
        if token and now < valid_until:
            return token
    token, expires_in = _fetch_access_token(client_id=client_id, client_secret=client_secret, endpoint=endpoint)
    valid_until = now + max(30, expires_in - ACCESS_TOKEN_SAFETY_SECONDS)
    _ACCESS_TOKEN_CACHE[cache_key] = (token, valid_until)
    return token


def _tuya_request(
    *,
    endpoint: str,
    client_id: str,
    client_secret: str,
    method: str,
    path: str,
    access_token: str | None,
    body: object | None = None,
) -> dict:
    timestamp = str(int(time.time() * 1000))
    nonce = uuid.uuid4().hex
    body_text = json.dumps(body, ensure_ascii=False, separators=(",", ":")) if body is not None else ""
    content_hash = hashlib.sha256(body_text.encode("utf-8")).hexdigest()

    path_only, _, query_string = path.partition("?")
    canonical_query = _canonicalize_query(query_string)
    signed_path = f"{path_only}?{canonical_query}" if canonical_query else path_only

    string_to_sign = f"{method}\n{content_hash}\n\n{signed_path}"
    sign_payload = f"{client_id}{access_token or ''}{timestamp}{nonce}{string_to_sign}"
    signature = hmac.new(
        client_secret.encode("utf-8"),
        sign_payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest().upper()

    headers = {
        "client_id": client_id,
        "sign": signature,
        "t": timestamp,
        "nonce": nonce,
        "sign_method": "HMAC-SHA256",
    }
    if access_token:
        headers["access_token"] = access_token
    if body_text:
        headers["Content-Type"] = "application/json"

    full_url = f"{endpoint}{path_only}"
    if canonical_query:
        full_url = f"{full_url}?{canonical_query}"

    try:
        response = requests.request(
            method=method,
            url=full_url,
            headers=headers,
            data=body_text if body_text else None,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        raise TuyaApiError(f"Could not reach Tuya endpoint: {exc}") from exc

    try:
        payload = response.json()
    except ValueError as exc:
        raise TuyaApiError("Tuya returned a non-JSON response.") from exc

    if response.status_code >= 400 or payload.get("success") is False:
        code = payload.get("code", response.status_code)
        message = payload.get("msg", "Unknown Tuya error")
        raise TuyaApiError(f"Tuya API error ({code}): {message}")

    return payload


def _canonicalize_query(query: str) -> str:
    if not query:
        return ""
    pairs = sorted(parse_qsl(query, keep_blank_values=True), key=lambda item: (item[0], item[1]))
    return urlencode(pairs, doseq=True)


def _format_status_text(raw_status: object) -> str:
    if not isinstance(raw_status, list):
        return "No status"
    chunks: list[str] = []
    for entry in raw_status:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code", "")).strip()
        value = entry.get("value")
        if not code:
            continue
        chunks.append(f"{code}={value}")
        if len(chunks) >= 4:
            break
    return ", ".join(chunks) if chunks else "No status"


def _extract_measurements_text(raw_status: object, dp_meta: DpMetaMap | None = None) -> str:
    if not isinstance(raw_status, list):
        return "No measurements"

    preferred: list[str] = []
    fallback: list[str] = []
    for entry in raw_status:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code", "")).strip().lower()
        base_code, _channel = _split_dp_code(code)
        value = entry.get("value")
        if not code or _is_switch_like_code(code) or _is_boolean_like_value(value):
            continue
        if not _is_numeric_like(value):
            continue
        meta = _resolve_dp_meta(code, dp_meta)
        normalized_value = _normalize_measurement_value(base_code, value, scale=_read_scale(meta))
        label = _friendly_measurement_label(code)
        unit = _measurement_unit(code, meta)
        normalized_value, unit = _normalize_measurement_unit_for_display(base_code, normalized_value, unit)
        unit_suffix = f" {unit}" if unit else ""
        chunk = f"{label}: {_format_measurement_number(normalized_value)}{unit_suffix}"
        if _is_measurement_priority_code(base_code):
            preferred.append(chunk)
        else:
            fallback.append(chunk)
        if len(preferred) + len(fallback) >= 6:
            break

    items = (preferred + fallback)[:4]
    return ", ".join(items) if items else "No measurements"


def _friendly_measurement_label(code: str) -> str:
    base_code, channel = _split_dp_code(code)
    mapping = {
        "cur_power": "Power",
        "cur_current": "Current",
        "cur_voltage": "Voltage",
        "add_ele": "Energy",
        "temp_current": "Temperature",
        "va_temperature": "Temperature",
        "humidity_value": "Humidity",
        "bright_value": "Brightness",
        "countdown": "Auto-off timer",
        "alarm_countdown": "Alarm timer",
        "door_time": "Door open time",
    }
    base_label = mapping.get(base_code, base_code.replace("_", " ").title())
    if channel:
        return f"{base_label} ({channel})"
    return base_label


def _measurement_unit(code: str, meta: dict[str, object] | None = None) -> str:
    if isinstance(meta, dict):
        unit = str(meta.get("unit", "")).strip()
        if unit:
            return f" {unit}"
    base_code, _channel = _split_dp_code(code)
    units = {
        "cur_power": " W",
        "cur_current": " A",
        "cur_voltage": " V",
        "add_ele": " kWh",
        "temp_current": " °C",
        "va_temperature": " °C",
        "humidity_value": " %",
        "bright_value": " %",
        "countdown": " s",
        "alarm_countdown": " s",
        "door_time": " s",
    }
    return units.get(base_code, "")


def _split_dp_code(code: str) -> tuple[str, str]:
    normalized = code.strip().lower()
    parts = normalized.rsplit("_", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0], f"channel {parts[1]}"
    return normalized, ""


def _is_measurement_priority_code(code: str) -> bool:
    keywords = ("power", "curr", "volt", "temp", "energy", "ele", "humid")
    return any(keyword in code for keyword in keywords)


def _is_switch_like_code(code: str) -> bool:
    return code.startswith("switch")


def _is_boolean_like_value(value: object) -> bool:
    if isinstance(value, bool):
        return True
    if isinstance(value, str):
        normalized = value.strip().lower()
        return normalized in {"true", "false", "online", "offline"}
    return False


def _is_numeric_like(value: object) -> bool:
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False
        try:
            float(text)
            return True
        except ValueError:
            return False
    return False


def _normalize_measurement_value(base_code: str, value: object, *, scale: int | None = None) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0

    if scale is not None and scale >= 0:
        try:
            return numeric / (10 ** scale)
        except OverflowError:
            return numeric

    if base_code == "cur_voltage":
        # Common Tuya raw voltage comes as scaled integer (for example 20600 -> 206.00 V).
        if abs(numeric) > 1000:
            numeric = numeric / 100.0
    elif base_code == "cur_current":
        # Common Tuya raw current comes as mA-like scaled integer (for example 12345 -> 12.345 A).
        if abs(numeric) > 200:
            numeric = numeric / 1000.0
    return numeric


def _format_measurement_number(value: float) -> str:
    return str(int(round(value)))


def _normalize_measurement_unit_for_display(base_code: str, value: float, unit: str) -> tuple[float, str]:
    normalized_unit = (unit or "").strip().lower()
    if base_code == "cur_current" and normalized_unit in {"ma", "milliamp", "milliamps"}:
        return value / 1000.0, " A"
    return value, unit


def _resolve_dp_meta(code: str, dp_meta: DpMetaMap | None) -> dict[str, object] | None:
    if not isinstance(dp_meta, dict):
        return None
    normalized = code.strip().lower()
    exact = dp_meta.get(normalized)
    if isinstance(exact, dict):
        return exact
    base_code, _channel = _split_dp_code(normalized)
    base = dp_meta.get(base_code)
    if isinstance(base, dict):
        return base
    return None


def _read_scale(meta: dict[str, object] | None) -> int | None:
    if not isinstance(meta, dict):
        return None
    raw_scale = meta.get("scale")
    try:
        return int(raw_scale)
    except (TypeError, ValueError):
        LOGGER.debug("Tuya DP meta has invalid scale value: %r", raw_scale)
        return None


def _fetch_device_runtime_state(
    *,
    endpoint: str,
    client_id: str,
    client_secret: str,
    access_token: str,
    device_id: str,
    fallback_name: str,
    fallback_online: bool | None,
    fallback_status: object,
) -> tuple[str, str, bool, str, str, bool | None, DpMetaMap, object, object, object]:
    if not device_id:
        return (
            fallback_name.strip(),
            "",
            bool(fallback_online),
            _format_status_text(fallback_status),
            _extract_measurements_text(fallback_status),
            _extract_power_state(fallback_status),
            {},
            {"success": False, "msg": "missing_device_id"},
            {"success": False, "msg": "missing_device_id"},
            {"success": False, "msg": "missing_device_id"},
        )

    resolved_name = fallback_name.strip()
    resolved_icon_url = ""
    online: bool | None = fallback_online
    status_text = _format_status_text(fallback_status)
    measurements_text = _extract_measurements_text(fallback_status, {})
    power_on = _extract_power_state(fallback_status)
    dp_meta: DpMetaMap = {}
    detail_payload: object = {"success": False, "msg": "not_requested"}
    specification_payload: object = {"success": False, "msg": "not_requested"}
    status_payload: object = {"success": False, "msg": "not_requested"}

    try:
        detail_payload = _tuya_request(
            endpoint=endpoint,
            client_id=client_id,
            client_secret=client_secret,
            method="GET",
            path=f"/v1.1/iot-03/devices/{device_id}",
            access_token=access_token,
        )
        detail_result = detail_payload.get("result", {}) if isinstance(detail_payload, dict) else {}
        if isinstance(detail_result, dict):
            detail_name = str(detail_result.get("name", "")).strip()
            if detail_name:
                resolved_name = detail_name
            resolved_icon_url = _extract_icon_url(detail_result)
            maybe_online = _as_bool_or_none(detail_result.get("online"))
            if maybe_online is not None:
                online = maybe_online
    except TuyaApiError as exc:
        detail_payload = {"success": False, "msg": str(exc)}

    try:
        specification_payload = _tuya_request(
            endpoint=endpoint,
            client_id=client_id,
            client_secret=client_secret,
            method="GET",
            path=f"/v1.1/iot-03/devices/{device_id}/specification",
            access_token=access_token,
        )
        dp_meta = _extract_dp_meta_map(specification_payload)
    except TuyaApiError as exc:
        specification_payload = {"success": False, "msg": str(exc)}

    try:
        status_payload = _tuya_request(
            endpoint=endpoint,
            client_id=client_id,
            client_secret=client_secret,
            method="GET",
            path=f"/v1.0/iot-03/devices/{device_id}/status",
            access_token=access_token,
        )
        status_result = status_payload.get("result", []) if isinstance(status_payload, dict) else []
        status_text = _format_status_text(status_result)
        measurements_text = _extract_measurements_text(status_result, dp_meta)
        power_on = _extract_power_state(status_result)
    except TuyaApiError as exc:
        status_payload = {"success": False, "msg": str(exc)}

    return resolved_name, resolved_icon_url, bool(online), status_text, measurements_text, power_on, dp_meta, detail_payload, specification_payload, status_payload


def _fetch_device_status_state(
    *,
    endpoint: str,
    client_id: str,
    client_secret: str,
    access_token: str,
    device_id: str,
    fallback_status: object,
    dp_meta: DpMetaMap | None = None,
) -> tuple[str, str, bool | None, object]:
    status_text = _format_status_text(fallback_status)
    measurements_text = _extract_measurements_text(fallback_status, dp_meta)
    power_on = _extract_power_state(fallback_status)
    status_payload: object = {"success": False, "msg": "not_requested"}
    try:
        status_payload = _tuya_request(
            endpoint=endpoint,
            client_id=client_id,
            client_secret=client_secret,
            method="GET",
            path=f"/v1.0/iot-03/devices/{device_id}/status",
            access_token=access_token,
        )
        status_result = status_payload.get("result", []) if isinstance(status_payload, dict) else []
        status_text = _format_status_text(status_result)
        measurements_text = _extract_measurements_text(status_result, dp_meta)
        power_on = _extract_power_state(status_result)
    except TuyaApiError as exc:
        status_payload = {"success": False, "msg": str(exc)}
    return status_text, measurements_text, power_on, status_payload


def _as_bool_or_none(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "online"}:
            return True
        if normalized in {"false", "0", "offline"}:
            return False
    return None


def _extract_power_state(raw_status: object) -> bool | None:
    if not isinstance(raw_status, list):
        return None
    for entry in raw_status:
        if not isinstance(entry, dict):
            continue
        code = str(entry.get("code", "")).strip().lower()
        if code not in {"switch", "switch_1"}:
            continue
        return _as_bool_or_none(entry.get("value"))
    return None


def _pick_switch_code(status_payload: object) -> str | None:
    if not isinstance(status_payload, dict):
        return None
    result = status_payload.get("result")
    if not isinstance(result, list):
        return None
    preferred = ("switch_1", "switch")
    available_codes = {
        str(entry.get("code", "")).strip().lower()
        for entry in result
        if isinstance(entry, dict)
    }
    for code in preferred:
        if code in available_codes:
            return code
    for code in available_codes:
        if code.startswith("switch"):
            return code
    return None


def _extract_icon_url(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    candidates = (
        payload.get("icon"),
        payload.get("icon_url"),
        payload.get("product_icon"),
        payload.get("product_icon_url"),
    )
    for candidate in candidates:
        value = str(candidate or "").strip()
        if not value:
            continue
        if value.startswith("http://") or value.startswith("https://"):
            return value
        if "/" in value:
            return value
    return ""


def _extract_uid_from_device_list_payload(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    result = payload.get("result", [])
    items = result.get("list", result.get("devices", [])) if isinstance(result, dict) else result
    if not isinstance(items, list):
        return ""
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("uid", "owner_id", "owner_uid"):
            value = str(item.get(key, "")).strip()
            if value:
                return value
    return ""


def _extract_list_result(result: object, *, keys: tuple[str, ...]) -> list[object]:
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in keys:
            candidate = result.get(key)
            if isinstance(candidate, list):
                return candidate
    return []


def _extract_device_ids_from_device_list_payload(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    result = payload.get("result", [])
    items = result.get("list", result.get("devices", [])) if isinstance(result, dict) else result
    if not isinstance(items, list):
        return []
    out: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        device_id = str(item.get("id", "")).strip()
        if device_id:
            out.append(device_id)
    return out


def _extract_uid_from_device_payload(payload: object) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("uid", "owner_id", "owner_uid"):
        value = str(payload.get(key, "")).strip()
        if value:
            return value
    return ""


def _extract_dp_meta_map(specification_payload: object) -> DpMetaMap:
    if not isinstance(specification_payload, dict):
        return {}
    result = specification_payload.get("result", {})
    if not isinstance(result, dict):
        return {}

    out: DpMetaMap = {}
    for section in ("status", "functions", "status_set"):
        entries = result.get(section)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            code = str(entry.get("code", "")).strip().lower()
            if not code:
                continue
            meta = _extract_single_dp_meta(entry)
            if meta:
                out[code] = meta
    return out


def _extract_single_dp_meta(entry: dict[str, object]) -> dict[str, object]:
    raw_values = entry.get("values")
    parsed_values: dict[str, object] = {}
    if isinstance(raw_values, dict):
        parsed_values = raw_values
    elif isinstance(raw_values, str):
        text = raw_values.strip()
        if text:
            try:
                maybe_dict = json.loads(text)
                if isinstance(maybe_dict, dict):
                    parsed_values = maybe_dict
            except json.JSONDecodeError:
                parsed_values = {}

    scale: int | None = None
    for source in (parsed_values.get("scale"), entry.get("scale")):
        try:
            scale = int(source)  # type: ignore[arg-type]
            break
        except (TypeError, ValueError):
            LOGGER.debug("Tuya DP meta scale parse failed for source value: %r", source)
            continue

    unit = ""
    for source in (parsed_values.get("unit"), entry.get("unit")):
        candidate = str(source or "").strip()
        if candidate:
            unit = candidate
            break

    out: dict[str, object] = {}
    if scale is not None:
        out["scale"] = scale
    if unit:
        out["unit"] = unit
    return out
