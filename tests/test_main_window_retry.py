from __future__ import annotations

from app.ui.main_window import _is_profile_activation_retryable_energyflow_error


def test_profile_activation_retryable_energyflow_error_for_timeouts() -> None:
    assert _is_profile_activation_retryable_energyflow_error(
        "DessMonitor API accepted the connection but did not respond in time. Try again later."
    )
    assert _is_profile_activation_retryable_energyflow_error(
        "Connection to DessMonitor API timed out. The server is reachable in a browser."
    )


def test_profile_activation_retryable_energyflow_error_ignores_non_timeout_errors() -> None:
    assert not _is_profile_activation_retryable_energyflow_error("No devices were returned for this DessMonitor account.")
