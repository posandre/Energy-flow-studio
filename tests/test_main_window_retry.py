from __future__ import annotations

from app.ui.main_window import (
    MainWindow,
    _is_profile_activation_retryable_energyflow_error,
    _is_retryable_tuya_transport_error,
    _should_auto_retry_tuya_transient_error,
)


def test_profile_activation_retryable_energyflow_error_for_timeouts() -> None:
    assert _is_profile_activation_retryable_energyflow_error(
        "DessMonitor API accepted the connection but did not respond in time. Try again later."
    )
    assert _is_profile_activation_retryable_energyflow_error(
        "Connection to DessMonitor API timed out. The server is reachable in a browser."
    )


def test_profile_activation_retryable_energyflow_error_ignores_non_timeout_errors() -> None:
    assert not _is_profile_activation_retryable_energyflow_error("No devices were returned for this DessMonitor account.")


def test_retryable_tuya_transport_error_detects_dns_resolution_failure() -> None:
    message = (
        "Could not reach Tuya endpoint: HTTPSConnectionPool(host='openapi.tuyaeu.com', port=443): "
        "Max retries exceeded with url: /v1.0/token?grant_type=1 "
        "(Caused by NameResolutionError(\"Failed to resolve 'openapi.tuyaeu.com'\"))"
    )
    assert _is_retryable_tuya_transport_error(message)


def test_friendly_tuya_error_collapses_dns_noise() -> None:
    error = (
        "Could not reach Tuya endpoint: HTTPSConnectionPool(host='openapi.tuyaeu.com', port=443): "
        "Max retries exceeded with url: /v1.0/token?grant_type=1 "
        "(Caused by NameResolutionError(\"Failed to resolve 'openapi.tuyaeu.com'\"))"
    )
    friendly = MainWindow._friendly_tuya_error(object(), error)
    assert friendly == "DNS lookup failed after wake."


def test_should_auto_retry_tuya_transient_error_allows_retry_within_limit() -> None:
    message = "Could not reach Tuya endpoint: NameResolutionError: Failed to resolve host"
    assert _should_auto_retry_tuya_transient_error(message, 0, max_attempts=1)


def test_should_auto_retry_tuya_transient_error_blocks_when_limit_reached() -> None:
    message = "Could not reach Tuya endpoint: NameResolutionError: Failed to resolve host"
    assert not _should_auto_retry_tuya_transient_error(message, 1, max_attempts=1)
