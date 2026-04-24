from __future__ import annotations

import requests

from app.services import tuya_api


class _DummyResponse:
    def __init__(self, *, status_code: int = 200, payload: dict[str, object] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {"success": True, "result": {}}

    def json(self) -> dict[str, object]:
        return self._payload


def test_tuya_request_retries_retryable_transport_errors(monkeypatch) -> None:
    calls: list[int] = []

    def _fake_request(**_kwargs):
        calls.append(1)
        if len(calls) < 3:
            raise requests.exceptions.ConnectionError(
                "NameResolutionError: Failed to resolve 'openapi.tuyaeu.com'"
            )
        return _DummyResponse(payload={"success": True, "result": {"ok": True}})

    monkeypatch.setattr(tuya_api.requests, "request", _fake_request)
    monkeypatch.setattr(tuya_api.time, "sleep", lambda _seconds: None)

    payload = tuya_api._tuya_request(
        endpoint="https://openapi.tuyaeu.com",
        client_id="client",
        client_secret="secret",
        method="GET",
        path="/v1.0/token?grant_type=1",
        access_token=None,
    )

    assert payload["success"] is True
    assert len(calls) == 3


def test_tuya_request_does_not_retry_non_retryable_transport_errors(monkeypatch) -> None:
    calls: list[int] = []

    def _fake_request(**_kwargs):
        calls.append(1)
        raise requests.exceptions.InvalidURL("Invalid URL")

    monkeypatch.setattr(tuya_api.requests, "request", _fake_request)
    monkeypatch.setattr(tuya_api.time, "sleep", lambda _seconds: None)

    try:
        tuya_api._tuya_request(
            endpoint="https://openapi.tuyaeu.com",
            client_id="client",
            client_secret="secret",
            method="GET",
            path="/v1.0/token?grant_type=1",
            access_token=None,
        )
    except tuya_api.TuyaApiError as exc:
        assert "Could not reach Tuya endpoint" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected TuyaApiError")

    assert len(calls) == 1
