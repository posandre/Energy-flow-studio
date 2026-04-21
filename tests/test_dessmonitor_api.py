from __future__ import annotations

import pytest

from app.services import dessmonitor_api
from app.services.dessmonitor_api import DessMonitorApiError, DessMonitorConfig


def _config(company_key: str) -> DessMonitorConfig:
    return DessMonitorConfig(
        username="user@example.com",
        password="password",
        company_key=company_key,
        source=1,
        pn="PN",
        devcode="DEV",
        devaddr="1",
        sn="SN",
        device_label="Device",
        parameter_keys=[],
        date_from="2026-04-01",
        date_to="2026-04-01",
    )


def test_authenticate_deduplicates_repeated_attempt_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dessmonitor_api, "_AUTH_CACHE", {})

    def _always_timeout(_config: DessMonitorConfig, *, include_company_key: bool):
        raise DessMonitorApiError("DessMonitor API accepted the connection but did not respond in time. Try again later.")

    monkeypatch.setattr(dessmonitor_api, "_authenticate_once", _always_timeout)

    with pytest.raises(DessMonitorApiError) as exc_info:
        dessmonitor_api.authenticate(_config(company_key="company-key"))

    assert str(exc_info.value) == "DessMonitor API accepted the connection but did not respond in time. Try again later."


def test_authenticate_preserves_unique_errors_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dessmonitor_api, "_AUTH_CACHE", {})
    responses = iter(
        [
            DessMonitorApiError("First error"),
            DessMonitorApiError("Second error"),
        ]
    )

    def _fail_with_different_messages(_config: DessMonitorConfig, *, include_company_key: bool):
        raise next(responses)

    monkeypatch.setattr(dessmonitor_api, "_authenticate_once", _fail_with_different_messages)

    with pytest.raises(DessMonitorApiError) as exc_info:
        dessmonitor_api.authenticate(_config(company_key="company-key"))

    assert str(exc_info.value) == "First error | Second error"
