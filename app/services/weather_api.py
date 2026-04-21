from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Protocol

import pandas as pd

from app.services.logging_utils import get_logger

OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
DEFAULT_WEATHER_PROVIDER = "open-meteo"
DEFAULT_WEATHER_HISTORY_FIELDS = [
    "temperature_2m",
    "cloud_cover",
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
    "direct_normal_irradiance",
    "wind_speed_10m",
    "precipitation",
    "snowfall",
    "weather_code",
]
NUMERIC_WEATHER_FIELDS = {
    "temperature_2m",
    "cloud_cover",
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
    "direct_normal_irradiance",
    "wind_speed_10m",
    "precipitation",
    "snowfall",
}
DEFAULT_WEATHER_LIVE_FIELDS = [
    "temperature_2m",
    "cloud_cover",
    "shortwave_radiation",
    "wind_speed_10m",
    "precipitation",
    "weather_code",
]
DEFAULT_WEATHER_FORECAST_FIELDS = list(DEFAULT_WEATHER_HISTORY_FIELDS)
FALLBACK_WEATHER_FORECAST_FIELDS = [
    "temperature_2m",
    "cloud_cover",
    "shortwave_radiation",
    "wind_speed_10m",
    "precipitation",
    "weather_code",
]
LOGGER = get_logger(__name__)


class WeatherApiError(Exception):
    """Raised when a weather API request fails."""


@dataclass(slots=True)
class WeatherDatasetRequest:
    latitude: str
    longitude: str
    timestamps: list[str]
    timezone: str = ""
    location_label: str = ""
    hourly_fields: list[str] | None = None

    def resolved_hourly_fields(self) -> list[str]:
        fields = [str(field).strip() for field in (self.hourly_fields or DEFAULT_WEATHER_HISTORY_FIELDS) if str(field).strip()]
        return fields or list(DEFAULT_WEATHER_HISTORY_FIELDS)

    def resolved_timezone(self) -> str:
        return self.timezone.strip() or "auto"


@dataclass(slots=True)
class WeatherLiveRequest:
    latitude: str
    longitude: str
    timezone: str = ""
    location_label: str = ""
    hourly_fields: list[str] | None = None

    def resolved_hourly_fields(self) -> list[str]:
        fields = [str(field).strip() for field in (self.hourly_fields or DEFAULT_WEATHER_LIVE_FIELDS) if str(field).strip()]
        return fields or list(DEFAULT_WEATHER_LIVE_FIELDS)

    def resolved_timezone(self) -> str:
        return self.timezone.strip() or "auto"


@dataclass(slots=True)
class WeatherForecastRequest:
    latitude: str
    longitude: str
    timezone: str = ""
    location_label: str = ""
    hourly_fields: list[str] | None = None
    forecast_days: int = 3

    def resolved_hourly_fields(self) -> list[str]:
        fields = [str(field).strip() for field in (self.hourly_fields or DEFAULT_WEATHER_FORECAST_FIELDS) if str(field).strip()]
        return fields or list(DEFAULT_WEATHER_FORECAST_FIELDS)

    def resolved_timezone(self) -> str:
        return self.timezone.strip() or "auto"

    def resolved_forecast_days(self) -> int:
        try:
            days = int(self.forecast_days)
        except (TypeError, ValueError):
            days = 3
        return max(1, min(days, 16))


@dataclass(slots=True)
class WeatherLiveSnapshot:
    temperature_2m: float | None = None
    cloud_cover: float | None = None
    precipitation: float | None = None
    wind_speed_10m: float | None = None
    shortwave_radiation: float | None = None
    weather_code: int | None = None
    location_label: str = ""
    observed_at: str = ""
    provider_name: str = DEFAULT_WEATHER_PROVIDER


class WeatherProvider(Protocol):
    provider_name: str

    def fetch_hourly_history(self, request: WeatherDatasetRequest) -> pd.DataFrame:
        ...

    def fetch_hourly_forecast(self, request: WeatherForecastRequest) -> pd.DataFrame:
        ...

    def fetch_live_snapshot(self, request: WeatherLiveRequest) -> WeatherLiveSnapshot:
        ...


def _require_requests():
    try:
        import requests
    except ImportError as exc:  # pragma: no cover - dependency is expected in app env
        raise WeatherApiError(
            "Weather support requires the 'requests' package. Reinstall dependencies to enable weather sync."
        ) from exc
    return requests


def _request_open_meteo_json(
    *,
    url: str,
    params: dict[str, object],
    timeout_sec: int,
    error_prefix: str,
) -> dict:
    requests = _require_requests()
    last_exc: Exception | None = None
    headers = {"User-Agent": "EnergyFlow-Studio/1.0"}
    for attempt in range(2):
        try:
            response = requests.get(url, params=params, timeout=timeout_sec, headers=headers)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise WeatherApiError("Open-Meteo returned a non-JSON object payload.")
            return payload
        except Exception as exc:  # pragma: no cover - network path
            last_exc = exc
            if attempt < 1:
                time.sleep(0.3)
                continue
            raise WeatherApiError(f"{error_prefix}: {exc}") from exc
    if last_exc is None:  # pragma: no cover - safety
        raise WeatherApiError(f"{error_prefix}: unknown network failure")
    raise WeatherApiError(f"{error_prefix}: {last_exc}") from last_exc


def _prepare_target_index(timestamps: list[str]) -> tuple[pd.DatetimeIndex, list[str]]:
    cleaned_timestamps = [str(item).strip() for item in timestamps if str(item).strip()]
    if not cleaned_timestamps:
        raise WeatherApiError("No timestamps were provided for weather history sync.")
    try:
        target_series = pd.to_datetime(cleaned_timestamps)
    except Exception as exc:  # pragma: no cover - protects runtime inputs
        raise WeatherApiError(f"Could not parse DessMonitor timestamps for weather sync: {exc}") from exc
    if target_series.isna().any():
        raise WeatherApiError("Some DessMonitor timestamps could not be parsed for weather sync.")
    return pd.DatetimeIndex(target_series), cleaned_timestamps


def _align_hourly_weather_to_timestamps(hourly_frame: pd.DataFrame, timestamps: list[str]) -> pd.DataFrame:
    target_index, original_timestamps = _prepare_target_index(timestamps)
    if hourly_frame.empty:
        raise WeatherApiError("The weather provider returned an empty history response.")
    working = hourly_frame.copy()
    if "time" not in working.columns:
        raise WeatherApiError("The weather provider response did not contain an hourly time axis.")

    working["time"] = pd.to_datetime(working["time"])
    working = working.dropna(subset=["time"]).drop_duplicates(subset=["time"], keep="last").sort_values("time")
    if working.empty:
        raise WeatherApiError("The weather provider response did not contain valid hourly timestamps.")

    hourly_indexed = working.set_index("time")
    # Reindex/interpolate require a unique source index.
    hourly_indexed = hourly_indexed[~hourly_indexed.index.duplicated(keep="last")]
    hourly_indexed = hourly_indexed.sort_index()
    target_index_unique = pd.DatetimeIndex(target_index.unique()).sort_values()
    union_index = hourly_indexed.index.union(target_index_unique).sort_values()
    union_index = union_index[~union_index.duplicated(keep="last")]
    aligned = pd.DataFrame(index=target_index)

    for column in hourly_indexed.columns:
        series = hourly_indexed[column]
        series = series[~series.index.duplicated(keep="last")].sort_index()
        if column in NUMERIC_WEATHER_FIELDS:
            numeric_series = pd.to_numeric(series, errors="coerce")
            aligned[column] = (
                numeric_series.reindex(union_index)
                .interpolate(method="time", limit_direction="both")
                .reindex(target_index)
                .tolist()
            )
        else:
            aligned[column] = (
                series.reindex(union_index)
                .ffill()
                .bfill()
                .reindex(target_index)
                .tolist()
            )

    aligned.insert(0, "Timestamp", original_timestamps)
    return aligned.reset_index(drop=True)


def _select_nearest_weather_row(hourly_frame: pd.DataFrame, *, reference_time: pd.Timestamp) -> tuple[pd.Series, pd.Timestamp]:
    if hourly_frame.empty:
        raise WeatherApiError("The weather provider returned an empty forecast response.")
    working = hourly_frame.copy()
    if "time" not in working.columns:
        raise WeatherApiError("The weather provider response did not contain an hourly time axis.")
    working["time"] = pd.to_datetime(working["time"], errors="coerce")
    working = working.dropna(subset=["time"]).sort_values("time")
    if working.empty:
        raise WeatherApiError("The weather provider response did not contain valid hourly timestamps.")
    distances = (working["time"] - reference_time).abs()
    nearest_index = distances.idxmin()
    row = working.loc[nearest_index]
    observed_at = pd.Timestamp(row["time"])
    return row, observed_at


def _normalize_hourly_forecast_frame(hourly_frame: pd.DataFrame) -> pd.DataFrame:
    if hourly_frame.empty:
        raise WeatherApiError("The weather provider returned an empty forecast response.")
    working = hourly_frame.copy()
    if "time" not in working.columns:
        raise WeatherApiError("The weather provider response did not contain an hourly time axis.")
    working["time"] = pd.to_datetime(working["time"], errors="coerce")
    working = working.dropna(subset=["time"]).drop_duplicates(subset=["time"]).sort_values("time")
    if working.empty:
        raise WeatherApiError("The weather provider response did not contain valid hourly timestamps.")
    working.insert(0, "Timestamp", working["time"].dt.strftime("%Y-%m-%d %H:%M:%S"))
    return working.drop(columns=["time"]).reset_index(drop=True)


class OpenMeteoWeatherProvider:
    provider_name = DEFAULT_WEATHER_PROVIDER

    def fetch_hourly_history(self, request: WeatherDatasetRequest) -> pd.DataFrame:
        target_index, _ = _prepare_target_index(request.timestamps)
        params = {
            "latitude": request.latitude.strip(),
            "longitude": request.longitude.strip(),
            "hourly": ",".join(request.resolved_hourly_fields()),
            "start_date": target_index.min().date().isoformat(),
            "end_date": target_index.max().date().isoformat(),
            "timezone": request.resolved_timezone(),
        }
        payload = _request_open_meteo_json(
            url=OPEN_METEO_ARCHIVE_URL,
            params=params,
            timeout_sec=45,
            error_prefix="Open-Meteo weather history request failed",
        )
        hourly_payload = payload.get("hourly")
        if not isinstance(hourly_payload, dict):
            raise WeatherApiError("Open-Meteo did not return hourly weather data.")
        hourly_frame = pd.DataFrame(hourly_payload)
        return _align_hourly_weather_to_timestamps(hourly_frame, request.timestamps)

    def fetch_hourly_forecast(self, request: WeatherForecastRequest) -> pd.DataFrame:
        params = {
            "latitude": request.latitude.strip(),
            "longitude": request.longitude.strip(),
            "hourly": ",".join(request.resolved_hourly_fields()),
            "forecast_days": request.resolved_forecast_days(),
            "timezone": request.resolved_timezone(),
        }
        try:
            payload = _request_open_meteo_json(
                url=OPEN_METEO_FORECAST_URL,
                params=params,
                timeout_sec=10,
                error_prefix="Open-Meteo weather forecast request failed",
            )
        except WeatherApiError:
            # Fallback for transient upstream issues (e.g. 502 on heavy query).
            fallback_params = {
                "latitude": request.latitude.strip(),
                "longitude": request.longitude.strip(),
                "hourly": ",".join(FALLBACK_WEATHER_FORECAST_FIELDS),
                "forecast_days": max(1, min(request.resolved_forecast_days(), 2)),
                "timezone": "auto",
            }
            payload = _request_open_meteo_json(
                url=OPEN_METEO_FORECAST_URL,
                params=fallback_params,
                timeout_sec=8,
                error_prefix="Open-Meteo weather forecast request failed",
            )
        hourly_payload = payload.get("hourly")
        if not isinstance(hourly_payload, dict):
            raise WeatherApiError("Open-Meteo did not return hourly forecast data.")
        hourly_frame = pd.DataFrame(hourly_payload)
        return _normalize_hourly_forecast_frame(hourly_frame)

    def fetch_live_snapshot(self, request: WeatherLiveRequest) -> WeatherLiveSnapshot:
        params = {
            "latitude": request.latitude.strip(),
            "longitude": request.longitude.strip(),
            "hourly": ",".join(request.resolved_hourly_fields()),
            "forecast_days": 1,
            "timezone": request.resolved_timezone(),
        }
        try:
            payload = _request_open_meteo_json(
                url=OPEN_METEO_FORECAST_URL,
                params=params,
                timeout_sec=8,
                error_prefix="Open-Meteo live weather request failed",
            )
        except WeatherApiError:
            fallback_params = {
                "latitude": request.latitude.strip(),
                "longitude": request.longitude.strip(),
                "hourly": ",".join(DEFAULT_WEATHER_LIVE_FIELDS),
                "forecast_days": 1,
                "timezone": "auto",
            }
            payload = _request_open_meteo_json(
                url=OPEN_METEO_FORECAST_URL,
                params=fallback_params,
                timeout_sec=6,
                error_prefix="Open-Meteo live weather request failed",
            )
        hourly_payload = payload.get("hourly")
        if not isinstance(hourly_payload, dict):
            raise WeatherApiError("Open-Meteo did not return hourly forecast data.")
        hourly_frame = pd.DataFrame(hourly_payload)
        timezone_name = str(payload.get("timezone") or request.resolved_timezone()).strip() or "UTC"
        try:
            reference_time = pd.Timestamp.now(tz=timezone_name).tz_localize(None)
        except Exception:
            LOGGER.exception("Failed to build timezone-aware weather reference time for timezone %s", timezone_name)
            reference_time = pd.Timestamp.now().tz_localize(None)
        row, observed_at = _select_nearest_weather_row(hourly_frame, reference_time=reference_time)

        def numeric(name: str) -> float | None:
            value = row.get(name)
            if value is None or pd.isna(value):
                return None
            try:
                return float(value)
            except (TypeError, ValueError):
                LOGGER.debug("Weather numeric conversion failed for field %s value %r", name, value)
                return None

        weather_code = row.get("weather_code")
        code_value: int | None = None
        if weather_code is not None and not pd.isna(weather_code):
            try:
                code_value = int(float(weather_code))
            except (TypeError, ValueError):
                code_value = None

        return WeatherLiveSnapshot(
            temperature_2m=numeric("temperature_2m"),
            cloud_cover=numeric("cloud_cover"),
            precipitation=numeric("precipitation"),
            wind_speed_10m=numeric("wind_speed_10m"),
            shortwave_radiation=numeric("shortwave_radiation"),
            weather_code=code_value,
            location_label=request.location_label.strip(),
            observed_at=observed_at.strftime("%Y-%m-%d %H:%M:%S"),
            provider_name=self.provider_name,
        )


def default_weather_provider() -> WeatherProvider:
    return OpenMeteoWeatherProvider()


def fetch_weather_history_dataset(
    request: WeatherDatasetRequest,
    *,
    provider: WeatherProvider | None = None,
) -> pd.DataFrame:
    active_provider = provider or default_weather_provider()
    return active_provider.fetch_hourly_history(request)


def fetch_weather_forecast_dataset(
    request: WeatherForecastRequest,
    *,
    provider: WeatherProvider | None = None,
) -> pd.DataFrame:
    active_provider = provider or default_weather_provider()
    return active_provider.fetch_hourly_forecast(request)


def fetch_weather_live_snapshot(
    request: WeatherLiveRequest,
    *,
    provider: WeatherProvider | None = None,
) -> WeatherLiveSnapshot:
    active_provider = provider or default_weather_provider()
    return active_provider.fetch_live_snapshot(request)
