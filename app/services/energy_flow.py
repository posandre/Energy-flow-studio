from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from datetime import date
from typing import TYPE_CHECKING

import pandas as pd

from app.services.app_settings import DeviceControlFieldProfile
from app.services.dessmonitor_api import (
    DessMonitorApiError,
    DessMonitorConfig,
    DessMonitorTransportError,
    InverterSetting,
    authenticate,
    call_api,
    fetch_device_control_value,
    fetch_device_range_data,
)
from app.services.logging_utils import get_logger

if TYPE_CHECKING:
    from app.services.dessmonitor_api import AuthTokens

LOGGER = get_logger(__name__)


@dataclass(slots=True)
class EnergyFlowSnapshot:
    timestamp: str
    device_label: str
    device_pn: str
    device_sn: str
    pv_power: float | None
    grid_power: float | None
    load_power: float | None
    battery_power: float | None
    battery_soc: float | None
    pv_voltage: float | None = None
    battery_voltage: float | None = None
    load_voltage: float | None = None
    load_frequency: float | None = None
    grid_voltage: float | None = None
    grid_frequency: float | None = None
    pv_current: float | None = None
    pv_charge_current: float | None = None
    pv_to_home_current: float | None = None
    grid_charge_current: float | None = None
    grid_to_home_current: float | None = None
    battery_current: float | None = None
    load_current: float | None = None
    pv_to_battery_power: float | None = None
    pv_to_home_power: float | None = None
    grid_to_battery_power: float | None = None
    grid_to_home_power: float | None = None
    battery_to_home_power: float | None = None
    pv_direction: int | None = None
    grid_direction: int | None = None
    load_direction: int | None = None
    battery_direction: int | None = None


@dataclass(slots=True)
class EnergyFlowPriorityModes:
    output_priority: str = "default"
    charger_priority: str = "default"


ENERGY_FLOW_PARAMETER_KEYS = [
    "PV_OUTPUT_POWER",
    "GRID_ACTIVE_POWER",
    "LOAD_ACTIVE_POWER",
    "BATTERY_ACTIVE_POWER",
    "SOC",
    "BT_BATTERY_CAPACITY",
]


def fetch_energy_flow_snapshot(
    config: DessMonitorConfig,
    *,
    profile_fields: list[DeviceControlFieldProfile] | None = None,
    inverter_settings: list[InverterSetting] | None = None,
) -> EnergyFlowSnapshot:
    auth = authenticate(config)
    priority_modes = _resolve_energy_flow_priorities(
        config,
        auth=auth,
        profile_fields=profile_fields,
        inverter_settings=inverter_settings,
    )

    flow_data: dict = {}
    try:
        flow_payload = call_api(
            action="webQueryDeviceEnergyFlowEs",
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
                ]
            ),
        )
        flow_data = flow_payload.get("dat") or {}
        if not flow_data:
            raise DessMonitorApiError("DessMonitor energy flow endpoint returned an empty response.")
    except DessMonitorTransportError as exc:
        # Some accounts/devices return HTTP 404 on webQueryDeviceEnergyFlowEs.
        # Keep profile activation alive and derive snapshot from last-data/fallback streams.
        if "404" not in str(exc).lower() or "not found" not in str(exc).lower():
            raise

    last_data = _fetch_last_data_payload(config, auth)

    pv_item = _pick_flow_item(flow_data.get("pv_status"), preferred_keys=("pv_output_power",))
    grid_item = _pick_flow_item(flow_data.get("gd_status"), preferred_keys=("grid_active_power",))
    load_item = _pick_flow_item(flow_data.get("bc_status"), preferred_keys=("load_active_power",))
    battery_item = _pick_flow_item(
        flow_data.get("bt_status"),
        preferred_keys=("battery_active_power", "bt_battery_power", "battery_power"),
    )
    battery_soc_item = _pick_flow_item(
        flow_data.get("bt_status"),
        preferred_keys=("bt_battery_capacity",),
    )

    today = date.today().isoformat()
    live_config = replace(
        config,
        parameter_keys=ENERGY_FLOW_PARAMETER_KEYS.copy(),
        date_from=today,
        date_to=today,
    )
    fallback_frame: pd.DataFrame | None = None

    def get_fallback_frame() -> pd.DataFrame:
        nonlocal fallback_frame
        if fallback_frame is None:
            fallback_frame = _fetch_fallback_frame(live_config, auth)
        return fallback_frame

    pv_power = _extract_power_from_flow_item(pv_item)
    pv_power_last_data = _extract_last_data_power(
        last_data,
        section_prefixes=("pv_",),
        preferred_keywords=("pv_output_power", "pv_power", "active_power"),
        preferred_units=("w", "kw", "mw"),
    )
    if pv_power_last_data is not None:
        pv_power = pv_power_last_data
    pv_voltage = _extract_last_data_metric(
        last_data,
        section_prefixes=("pv_",),
        preferred_keywords=("pv_voltage", "input_voltage", "voltage"),
        preferred_units=("v", "kv"),
        metric="voltage",
    )
    pv_current = _extract_last_data_item_value(last_data, item_id="pv_eybond_read_33")
    pv_charge_current = _extract_last_data_item_value(last_data, item_id="pv_eybond_read_46")
    battery_voltage = _extract_last_data_metric(
        last_data,
        section_prefixes=("bt_",),
        preferred_keywords=("battery_voltage", "bt_voltage", "voltage"),
        preferred_units=("v", "kv"),
        metric="voltage",
    )
    pv_to_home_current = None
    pv_charge_current_on_pv_side = _convert_current_between_voltages(
        max(0.0, pv_charge_current) if pv_charge_current is not None else None,
        from_voltage=battery_voltage,
        to_voltage=pv_voltage,
    )
    if pv_current is not None and pv_charge_current_on_pv_side is not None:
        pv_to_home_current = max(0.0, pv_current - pv_charge_current_on_pv_side)
    if pv_power is None:
        pv_power = _last_numeric(get_fallback_frame(), "PV_OUTPUT_POWER")

    grid_power = _extract_power_from_flow_item(grid_item)
    grid_power_last_data = _extract_last_data_power(
        last_data,
        section_prefixes=("gd_", "grid_"),
        preferred_keywords=("grid_active_power", "grid_power", "active_power"),
        preferred_units=("w", "kw", "mw"),
    )
    if grid_power_last_data is not None:
        grid_power = grid_power_last_data
    if grid_power is None:
        grid_power = _last_numeric(get_fallback_frame(), "GRID_ACTIVE_POWER")

    load_power = _extract_power_from_flow_item(load_item)
    load_power_last_data = _extract_last_data_power(
        last_data,
        section_prefixes=("bc_", "load_"),
        preferred_keywords=("load_active_power", "load_power", "active_power", "output_power"),
        preferred_units=("w", "kw", "mw"),
    )
    if load_power_last_data is not None:
        load_power = load_power_last_data
    if load_power is None:
        load_power = _last_numeric(get_fallback_frame(), "LOAD_ACTIVE_POWER")

    battery_power = _extract_power_from_flow_item(battery_item)
    battery_power_last_data = _extract_last_data_power(
        last_data,
        section_prefixes=("bt_",),
        preferred_keywords=("battery_active_power", "bt_battery_power", "battery_power", "active_power"),
        preferred_units=("w", "kw", "mw"),
    )
    if battery_power_last_data is not None:
        battery_power = battery_power_last_data
    load_voltage = _extract_last_data_metric(
        last_data,
        section_prefixes=("bc_", "load_"),
        preferred_keywords=("load_voltage", "ac_voltage", "voltage", "output_voltage"),
        preferred_units=("v", "kv"),
        metric="voltage",
    )
    load_current = _extract_last_data_item_value(last_data, item_id="bc_eybond_read_24")
    battery_current = _extract_last_data_item_value(last_data, item_id="bt_eybond_read_29")
    direct_battery_power = None
    direct_battery_power_output_voltage = None
    if battery_current is not None and battery_voltage is not None:
        # Per inverter semantics shared by the user:
        # battery current > 0 means discharge, < 0 means charge.
        direct_battery_power = -battery_current * battery_voltage
    if battery_current is not None and load_voltage is not None:
        direct_battery_power_output_voltage = -battery_current * load_voltage
    if battery_power is None:
        battery_power = _extract_last_data_value(
            last_data,
            section="bt_",
            preferred_keys=("battery_active_power", "bt_battery_power", "battery_power"),
            preferred_units=("w", "kw", "mw"),
        )
    if battery_power is None:
        battery_power = _last_numeric(get_fallback_frame(), "BATTERY_ACTIVE_POWER")

    battery_soc = _extract_soc_from_flow_item(battery_soc_item)
    if battery_soc is None:
        battery_soc = _extract_last_data_soc(last_data)
    load_frequency = _extract_last_data_metric(
        last_data,
        section_prefixes=("bc_", "load_"),
        preferred_keywords=("load_frequency", "ac_frequency", "frequency", "freq"),
        preferred_units=("hz",),
        metric="frequency",
    )
    grid_voltage = _extract_last_data_metric(
        last_data,
        section_prefixes=("gd_", "grid_"),
        preferred_keywords=("grid_voltage", "ac_voltage", "voltage"),
        preferred_units=("v", "kv"),
        metric="voltage",
    )
    grid_frequency = _extract_last_data_metric(
        last_data,
        section_prefixes=("gd_", "grid_"),
        preferred_keywords=("grid_frequency", "ac_frequency", "frequency", "freq"),
        preferred_units=("hz",),
        metric="frequency",
    )
    grid_charge_current = _extract_last_data_item_value(last_data, item_id="gd_eybond_read_45")
    direct_breakdown = _derive_power_breakdown_from_last_data(
        pv_power=pv_power,
        battery_power=battery_power,
        battery_voltage=battery_voltage,
        pv_voltage=pv_voltage,
        pv_current=pv_current,
        pv_charge_current=pv_charge_current,
        grid_charge_current=grid_charge_current,
        grid_voltage=grid_voltage,
        load_voltage=load_voltage,
        load_power=load_power,
        grid_power=grid_power,
    )

    legacy_breakdown = _derive_power_breakdown(
        pv_power=pv_power,
        grid_power=grid_power,
        load_power=load_power,
        battery_power=battery_power,
        output_priority_mode=priority_modes.output_priority,
        charger_priority_mode=priority_modes.charger_priority,
    )
    if direct_breakdown is not None:
        (
            pv_to_battery_power,
            pv_to_home_power,
            grid_to_battery_power,
            grid_to_home_power,
            grid_to_home_current,
            battery_to_home_power,
        ) = direct_breakdown
    else:
        (
            pv_to_battery_power,
            pv_to_home_power,
            grid_to_battery_power,
            grid_to_home_power,
        ) = legacy_breakdown
        grid_to_home_current = None

    (
        pv_to_battery_power,
        pv_to_home_power,
    ) = _normalize_pv_allocations(
        pv_power=pv_power,
        pv_to_battery_power=pv_to_battery_power,
        pv_to_home_power=pv_to_home_power,
    )
    if direct_breakdown is not None:
        direct_breakdown = (
            pv_to_battery_power,
            pv_to_home_power,
            grid_to_battery_power,
            grid_to_home_power,
            grid_to_home_current,
            battery_to_home_power,
        )
    else:
        legacy_breakdown = (
            pv_to_battery_power,
            pv_to_home_power,
            grid_to_battery_power,
            grid_to_home_power,
        )

    battery_direction = _signed_direction_from_battery_current(battery_current)
    if battery_direction is None:
        battery_direction = _signed_direction_from_power(battery_power, invert=True)
    if battery_direction is None:
        battery_direction = _extract_direction(battery_item)

    grid_direction = _signed_direction_from_power(grid_power, invert=False)
    if grid_direction is None:
        grid_direction = _extract_direction(grid_item)

    return EnergyFlowSnapshot(
        timestamp=_extract_last_timestamp(last_data),
        device_label=config.device_label or "Device",
        device_pn=config.pn,
        device_sn=config.sn,
        pv_power=pv_power,
        grid_power=grid_power,
        load_power=load_power,
        battery_power=battery_power,
        battery_soc=battery_soc,
        pv_voltage=pv_voltage,
        battery_voltage=battery_voltage,
        load_voltage=load_voltage,
        load_frequency=load_frequency,
        grid_voltage=grid_voltage,
        grid_frequency=grid_frequency,
        pv_current=pv_current,
        pv_charge_current=pv_charge_current,
        pv_to_home_current=pv_to_home_current,
        grid_charge_current=grid_charge_current,
        grid_to_home_current=grid_to_home_current,
        battery_current=battery_current,
        load_current=load_current,
        pv_to_battery_power=pv_to_battery_power,
        pv_to_home_power=pv_to_home_power,
        grid_to_battery_power=grid_to_battery_power,
        grid_to_home_power=grid_to_home_power,
        battery_to_home_power=battery_to_home_power,
        pv_direction=_extract_direction(pv_item),
        grid_direction=grid_direction,
        load_direction=_extract_direction(load_item),
        battery_direction=battery_direction,
    )


def _fetch_last_data_payload(config: DessMonitorConfig, auth) -> dict:
    try:
        return call_api(
            action="querySPDeviceLastData",
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
                    ("i18n", config.i18n),
                ]
            ),
        )
    except DessMonitorApiError:
        return {}


def _fetch_fallback_frame(config: DessMonitorConfig, auth) -> pd.DataFrame:
    try:
        return fetch_device_range_data(config, auth=auth)
    except DessMonitorApiError:
        return pd.DataFrame()


def _pick_flow_item(raw_items, *, preferred_keys: tuple[str, ...]) -> dict | None:
    items = raw_items if isinstance(raw_items, list) else []
    normalized = [item for item in items if isinstance(item, dict)]
    if not normalized:
        return None
    preferred_lower = tuple(key.lower() for key in preferred_keys)
    for item in normalized:
        par = str(item.get("par", "")).strip().lower()
        if par in preferred_lower:
            return item
    for item in normalized:
        par = _normalize_key(item.get("par"))
        if any(preferred in par for preferred in preferred_lower):
            return item
    return normalized[0]


def _extract_direction(item: dict | None) -> int | None:
    if not item:
        return None
    try:
        return int(item.get("status"))
    except (TypeError, ValueError):
        LOGGER.debug("EnergyFlow direction parse failed for payload: %r", item)
        return None


def _extract_power_from_flow_item(item: dict | None) -> float | None:
    if not item:
        return None
    unit = str(item.get("unit", "")).strip().lower()
    return _to_power_watts(item.get("val"), unit)


def _extract_soc_from_flow_item(item: dict | None) -> float | None:
    if not item:
        return None
    par = str(item.get("par", "")).strip().lower()
    unit = str(item.get("unit", "")).strip().lower()
    value = _to_float(item.get("val"))
    if value is None:
        return None
    if unit == "%" and 0 <= value <= 100:
        return value
    if ("soc" in par or "capacity" in par) and 0 <= value <= 100:
        return value
    return None


def _extract_last_timestamp(payload: dict) -> str:
    timestamp = str(((payload.get("dat") or {}).get("gts")) or "").strip()
    if not timestamp:
        return "--"
    parsed = pd.to_datetime(timestamp, errors="coerce")
    if pd.isna(parsed):
        return timestamp
    return parsed.strftime("%d.%m.%Y %H:%M:%S")


def _extract_last_data_value(
    payload: dict,
    *,
    section: str,
    preferred_keys: tuple[str, ...],
    preferred_units: tuple[str, ...],
) -> float | None:
    pars = ((payload.get("dat") or {}).get("pars") or {}).get(section) or []
    items = [item for item in pars if isinstance(item, dict)]
    if not items:
        return None

    preferred_lower = tuple(key.lower() for key in preferred_keys)
    units_lower = tuple(unit.lower() for unit in preferred_units)

    for item in items:
        par = str(item.get("par", "")).strip().lower().replace(" ", "_")
        unit = str(item.get("unit", "")).strip().lower()
        if par in preferred_lower or any(key in par for key in preferred_lower):
            value = _to_power_watts(item.get("val"), unit)
            if value is not None:
                return value

    for item in items:
        unit = str(item.get("unit", "")).strip().lower()
        if unit in units_lower:
            value = _to_power_watts(item.get("val"), unit)
            if value is not None:
                return value
    return None


def _extract_last_data_item_value(payload: dict, *, item_id: str) -> float | None:
    for item in _iter_last_data_items(payload, ("pv_", "gd_", "bt_", "bc_", "load_", "grid_", "sy_")):
        if str(item.get("id", "")).strip() != item_id:
            continue
        return _to_float(item.get("val"))
    return None


def _extract_last_data_soc(payload: dict) -> float | None:
    pars = ((payload.get("dat") or {}).get("pars") or {}).get("bt_") or []
    items = [item for item in pars if isinstance(item, dict)]
    for item in items:
        par = str(item.get("par", "")).strip().lower()
        unit = str(item.get("unit", "")).strip().lower()
        value = _to_float(item.get("val"))
        if value is None:
            continue
        if unit == "%" and 0 <= value <= 100:
            return value
        if ("soc" in par or "capacity" in par) and 0 <= value <= 100:
            return value
    return None


def _extract_last_data_metric(
    payload: dict,
    *,
    section_prefixes: tuple[str, ...],
    preferred_keywords: tuple[str, ...],
    preferred_units: tuple[str, ...],
    metric: str,
) -> float | None:
    items = list(_iter_last_data_items(payload, section_prefixes))
    if not items:
        return None

    normalized_keywords = tuple(_normalize_key(keyword) for keyword in preferred_keywords)
    normalized_units = tuple(unit.lower() for unit in preferred_units)

    for item in items:
        par = _normalize_key(item.get("par"))
        unit = str(item.get("unit", "")).strip().lower()
        if any(keyword in par for keyword in normalized_keywords):
            value = _to_metric_value(item.get("val"), unit, metric)
            if value is not None:
                return value

    for item in items:
        unit = str(item.get("unit", "")).strip().lower()
        if unit in normalized_units:
            value = _to_metric_value(item.get("val"), unit, metric)
            if value is not None:
                return value
    return None


def _extract_last_data_power(
    payload: dict,
    *,
    section_prefixes: tuple[str, ...],
    preferred_keywords: tuple[str, ...],
    preferred_units: tuple[str, ...],
) -> float | None:
    items = list(_iter_last_data_items(payload, section_prefixes))
    if not items:
        return None

    normalized_keywords = tuple(_normalize_key(keyword) for keyword in preferred_keywords)
    normalized_units = tuple(unit.lower() for unit in preferred_units)

    for item in items:
        par = _normalize_key(item.get("par"))
        unit = str(item.get("unit", "")).strip().lower()
        if any(keyword in par for keyword in normalized_keywords):
            value = _to_power_watts(item.get("val"), unit)
            if value is not None:
                return value

    for item in items:
        unit = str(item.get("unit", "")).strip().lower()
        if unit in normalized_units:
            value = _to_power_watts(item.get("val"), unit)
            if value is not None:
                return value
    return None


def _iter_last_data_items(payload: dict, section_prefixes: tuple[str, ...]):
    sections = ((payload.get("dat") or {}).get("pars") or {})
    if not isinstance(sections, dict):
        return
    for section_name, raw_items in sections.items():
        if not any(str(section_name).startswith(prefix) for prefix in section_prefixes):
            continue
        for item in raw_items or []:
            if isinstance(item, dict):
                yield item


def _convert_current_between_voltages(
    current: float | None,
    *,
    from_voltage: float | None,
    to_voltage: float | None,
) -> float | None:
    if current is None or from_voltage is None or to_voltage is None:
        return None
    if from_voltage <= 0 or to_voltage <= 0:
        return None
    return current * from_voltage / to_voltage


def _derive_power_breakdown_from_last_data(
    *,
    pv_power: float | None,
    battery_power: float | None,
    battery_voltage: float | None,
    pv_voltage: float | None,
    pv_current: float | None,
    pv_charge_current: float | None,
    grid_charge_current: float | None,
    grid_voltage: float | None,
    load_voltage: float | None,
    load_power: float | None,
    grid_power: float | None,
) -> tuple[float | None, float | None, float | None, float | None, float | None, float | None] | None:
    if pv_power is None or load_power is None or grid_power is None:
        return None

    pv_total_power = max(0.0, float(pv_power))
    grid_total_power = max(0.0, float(grid_power))
    home_total_power = max(0.0, float(load_power))
    battery_total_power = float(battery_power) if battery_power is not None else None
    battery_charge_total = max(0.0, battery_total_power) if battery_total_power is not None else 0.0
    battery_discharge_total = max(0.0, -battery_total_power) if battery_total_power is not None else 0.0

    pv_to_battery_raw = 0.0
    if pv_charge_current is not None and battery_voltage is not None:
        pv_to_battery_raw = max(0.0, pv_charge_current) * battery_voltage
    grid_to_battery_raw = 0.0
    if grid_charge_current is not None and battery_voltage is not None:
        grid_to_battery_raw = max(0.0, grid_charge_current) * battery_voltage

    source_capacities = {
        "pv": pv_total_power,
        "grid": grid_total_power,
    }
    battery_allocations = _allocate_by_preferred_weights(
        total=battery_charge_total,
        capacities=source_capacities,
        preferred_weights={
            "pv": pv_to_battery_raw,
            "grid": grid_to_battery_raw,
        },
    )
    remaining_source_capacities = {
        source: max(0.0, capacity - battery_allocations[source])
        for source, capacity in source_capacities.items()
    }
    pv_to_home_raw = max(0.0, remaining_source_capacities["pv"])
    grid_to_home_raw = max(0.0, remaining_source_capacities["grid"])
    battery_to_home_power = min(battery_discharge_total, home_total_power)
    non_battery_home_total = max(0.0, home_total_power - battery_to_home_power)
    home_allocations = _allocate_by_preferred_weights(
        total=non_battery_home_total,
        capacities=remaining_source_capacities,
        preferred_weights={
            "pv": pv_to_home_raw,
            "grid": grid_to_home_raw,
        },
    )

    pv_to_home_power = home_allocations["pv"]
    grid_to_home_power = home_allocations["grid"]
    pv_to_battery_power = battery_allocations["pv"]
    grid_to_battery_power = battery_allocations["grid"]

    grid_to_home_current = None
    if grid_to_home_power is not None and grid_voltage is not None and grid_voltage > 0:
        grid_to_home_current = grid_to_home_power / grid_voltage

    return (
        pv_to_battery_power,
        pv_to_home_power,
        grid_to_battery_power,
        grid_to_home_power,
        grid_to_home_current,
        battery_to_home_power,
    )


def _allocate_by_preferred_weights(
    *,
    total: float,
    capacities: dict[str, float],
    preferred_weights: dict[str, float],
) -> dict[str, float]:
    remaining_total = max(0.0, float(total))
    allocations = {key: 0.0 for key in capacities}
    remaining_capacities = {key: max(0.0, float(value)) for key, value in capacities.items()}
    epsilon = 1e-9

    while remaining_total > epsilon:
        active_sources = [key for key, value in remaining_capacities.items() if value > epsilon]
        if not active_sources:
            break

        positive_weight_sources = [
            key for key in active_sources if max(0.0, float(preferred_weights.get(key, 0.0))) > epsilon
        ]
        if positive_weight_sources:
            basis = {
                key: max(0.0, float(preferred_weights.get(key, 0.0)))
                for key in positive_weight_sources
            }
        else:
            break

        basis_total = sum(basis.values())
        if basis_total <= epsilon:
            break

        progressed = False
        for key, weight in basis.items():
            if remaining_total <= epsilon:
                break
            share = remaining_total * (weight / basis_total)
            allocated = min(remaining_capacities[key], share)
            if allocated <= epsilon:
                continue
            allocations[key] += allocated
            remaining_capacities[key] -= allocated
            remaining_total -= allocated
            progressed = True

        if progressed:
            continue

        for key in active_sources:
            if remaining_total <= epsilon:
                break
            allocated = min(remaining_capacities[key], remaining_total)
            if allocated <= epsilon:
                continue
            allocations[key] += allocated
            remaining_capacities[key] -= allocated
            remaining_total -= allocated
            progressed = True

        if not progressed:
            break

    return allocations


def _normalize_key(value) -> str:
    return str(value or "").strip().lower().replace(" ", "_").replace("-", "_")


def _derive_power_breakdown(
    *,
    pv_power: float | None,
    grid_power: float | None,
    load_power: float | None,
    battery_power: float | None,
    output_priority_mode: str = "default",
    charger_priority_mode: str = "default",
) -> tuple[float | None, float | None, float | None, float | None]:
    if any(value is None for value in (pv_power, grid_power, load_power, battery_power)):
        return None, None, None, None

    available = {
        "pv": max(0.0, float(pv_power)),
        "grid": max(0.0, float(grid_power)),
        "battery": max(0.0, -float(battery_power)),
    }
    home_remaining = max(0.0, float(load_power))
    battery_charge_remaining = max(0.0, float(battery_power))
    home_allocations = {"pv": 0.0, "grid": 0.0, "battery": 0.0}
    battery_allocations = {"pv": 0.0, "grid": 0.0}

    for source in _home_source_order(output_priority_mode):
        if home_remaining <= 0:
            break
        allocated = min(available[source], home_remaining)
        if allocated <= 0:
            continue
        available[source] -= allocated
        home_allocations[source] += allocated
        home_remaining -= allocated

    for source in _charge_source_order(charger_priority_mode):
        if battery_charge_remaining <= 0:
            break
        if source not in {"pv", "grid"}:
            continue
        allocated = min(available[source], battery_charge_remaining)
        if allocated <= 0:
            continue
        available[source] -= allocated
        battery_allocations[source] += allocated
        battery_charge_remaining -= allocated

    return (
        battery_allocations["pv"],
        home_allocations["pv"],
        battery_allocations["grid"],
        home_allocations["grid"],
    )


def _normalize_pv_allocations(
    *,
    pv_power: float | None,
    pv_to_battery_power: float | None,
    pv_to_home_power: float | None,
) -> tuple[float | None, float | None]:
    if pv_power is None:
        return pv_to_battery_power, pv_to_home_power

    available_pv = max(0.0, float(pv_power))
    pv_to_battery = max(0.0, float(pv_to_battery_power or 0.0))
    pv_to_home = max(0.0, float(pv_to_home_power or 0.0))
    allocated_total = pv_to_battery + pv_to_home

    if available_pv <= 0.0 or allocated_total <= available_pv:
        return pv_to_battery, pv_to_home

    scale = available_pv / allocated_total
    return pv_to_battery * scale, pv_to_home * scale


def _signed_direction_from_power(value: float | None, *, invert: bool) -> int | None:
    if value is None or abs(float(value)) < 1.0:
        return None
    direction = 1 if value > 0 else -1
    return -direction if invert else direction


def _signed_direction_from_battery_current(current: float | None) -> int | None:
    if current is None:
        return None
    if current > 0:
        return 1
    if current < 0:
        return -1
    return 0


def _home_source_order(mode: str) -> tuple[str, ...]:
    normalized = (mode or "").strip().lower()
    if normalized == "uti":
        return ("grid", "pv", "battery")
    if normalized == "sbu":
        return ("pv", "battery", "grid")
    if normalized == "sub":
        return ("pv", "grid", "battery")
    return ("pv", "grid", "battery")


def _charge_source_order(mode: str) -> tuple[str, ...]:
    normalized = (mode or "").strip().lower()
    if normalized == "solar_only":
        return ("pv",)
    if normalized == "utility_only":
        return ("grid",)
    if normalized == "utility_first":
        return ("grid", "pv")
    if normalized in {"solar_first", "solar_utility"}:
        return ("pv", "grid")
    return ("pv", "grid")


def _resolve_energy_flow_priorities(
    config: DessMonitorConfig,
    *,
    auth: "AuthTokens",
    profile_fields: list[DeviceControlFieldProfile] | None = None,
    inverter_settings: list[InverterSetting] | None = None,
) -> EnergyFlowPriorityModes:
    modes = EnergyFlowPriorityModes()
    settings = list(inverter_settings or [])
    if settings:
        output_setting = _find_priority_setting(
            settings,
            kind="output",
        )
        charger_setting = _find_priority_setting(
            settings,
            kind="charger",
        )
        if output_setting is not None:
            modes.output_priority = _normalize_output_priority(str(output_setting.display_value or output_setting.raw_value or ""))
        if charger_setting is not None:
            modes.charger_priority = _normalize_charger_priority(str(charger_setting.display_value or charger_setting.raw_value or ""))
        if modes.output_priority != "default" or modes.charger_priority != "default":
            return modes

    candidate_fields = list(profile_fields or [])
    output_field = _find_priority_field(candidate_fields, kind="output")
    charger_field = _find_priority_field(candidate_fields, kind="charger")
    if output_field is not None:
        output_value = _read_priority_field_value(config, output_field, auth)
        if output_value:
            modes.output_priority = _normalize_output_priority(output_value)
    if charger_field is not None:
        charger_value = _read_priority_field_value(config, charger_field, auth)
        if charger_value:
            modes.charger_priority = _normalize_charger_priority(charger_value)
    return modes


def _find_priority_setting(settings: list[InverterSetting], *, kind: str) -> InverterSetting | None:
    for setting in settings:
        haystack = " ".join(
            [
                setting.field_id,
                setting.name,
                setting.raw_name,
                setting.display_name,
            ]
        )
        if _matches_priority_field(haystack, kind=kind):
            return setting
    return None


def _find_priority_field(fields: list[DeviceControlFieldProfile], *, kind: str) -> DeviceControlFieldProfile | None:
    for field in fields:
        haystack = " ".join([field.field_id, field.name, field.display_name()])
        if _matches_priority_field(haystack, kind=kind):
            return field
    return None


def _matches_priority_field(text: str, *, kind: str) -> bool:
    normalized = _normalize_key(text)
    if kind == "output":
        return (
            ("output" in normalized and "priority" in normalized)
            or ("ac_output" in normalized and "priority" in normalized)
            or ("output_source" in normalized and "priority" in normalized)
        )
    return (
        ("charger" in normalized and "priority" in normalized)
        or ("charge_source" in normalized and "priority" in normalized)
        or ("charger_source" in normalized and "priority" in normalized)
    )


def _read_priority_field_value(
    config: DessMonitorConfig,
    field: DeviceControlFieldProfile,
    auth: "AuthTokens",
) -> str:
    try:
        raw_value = fetch_device_control_value(config, field.field_id, auth=auth)
    except DessMonitorApiError:
        return ""
    raw_text = "" if raw_value is None else str(raw_value).strip()
    for value, label in field.options or []:
        if raw_text == str(value).strip():
            return label
    return raw_text


def _normalize_output_priority(value: str) -> str:
    normalized = _normalize_key(value)
    if not normalized:
        return "default"
    if normalized == "sol" or normalized.startswith("sol_") or "solar_first" in normalized:
        return "sbu"
    if "sbu" in normalized:
        return "sbu"
    if normalized == "suf" or normalized.startswith("suf_") or "suf_priority" in normalized:
        return "sub"
    if "sub" in normalized:
        return "sub"
    if "uti" in normalized or "utility_first" in normalized or "grid_first" in normalized:
        return "uti"
    if "solar_battery_utility" in normalized:
        return "sbu"
    if "solar_utility_battery" in normalized:
        return "sub"
    return "default"


def _normalize_charger_priority(value: str) -> str:
    normalized = _normalize_key(value)
    if not normalized:
        return "default"
    if (
        "only_solar" in normalized
        or "solar_only" in normalized
        or "only_pv" in normalized
        or "pv_only" in normalized
    ):
        return "solar_only"
    if "only_utility" in normalized or "utility_only" in normalized or "only_grid" in normalized:
        return "utility_only"
    if "utility_first" in normalized or "grid_first" in normalized:
        return "utility_first"
    if "solar_and_utility" in normalized or "solar_utility" in normalized or "solar+utility" in value.lower():
        return "solar_utility"
    if "solar_first" in normalized:
        return "solar_first"
    return "default"


def _output_priority_label(mode: str) -> str:
    normalized = (mode or "").strip().lower()
    if normalized == "uti":
        return "utility first (grid -> solar -> battery)"
    if normalized == "sbu":
        return "solar first / SBU (solar -> battery -> grid)"
    if normalized == "sub":
        return "SUB / SUF (solar -> grid -> battery)"
    return "default (solar -> grid -> battery)"


def _charger_priority_label(mode: str) -> str:
    normalized = (mode or "").strip().lower()
    if normalized == "solar_only":
        return "only PV (solar only)"
    if normalized == "utility_only":
        return "utility only (grid only)"
    if normalized == "utility_first":
        return "utility first (grid -> solar)"
    if normalized in {"solar_first", "solar_utility"}:
        return "solar first (solar -> grid)"
    return "default (solar -> grid)"


def _to_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        LOGGER.debug("EnergyFlow numeric conversion failed for value: %r", value)
        return None


def _to_power_watts(value, unit: str) -> float | None:
    numeric = _to_float(value)
    if numeric is None:
        return None
    normalized = unit.strip().lower()
    if normalized == "kw":
        return numeric * 1000.0
    if normalized == "mw":
        return numeric * 1000000.0
    if normalized in {"w", ""}:
        return numeric
    return None


def _to_metric_value(value, unit: str, metric: str) -> float | None:
    numeric = _to_float(value)
    if numeric is None:
        return None
    normalized = unit.strip().lower()
    if metric == "voltage":
        if normalized == "kv":
            return numeric * 1000.0
        if normalized in {"v", ""}:
            return numeric
        return None
    if metric == "frequency":
        if normalized in {"hz", ""}:
            return numeric
        return None
    return numeric


def _last_numeric(frame: pd.DataFrame, column_name: str) -> float | None:
    if column_name not in frame.columns:
        return None
    series = pd.to_numeric(frame[column_name], errors="coerce").dropna()
    if series.empty:
        return None
    return float(series.iloc[-1])
