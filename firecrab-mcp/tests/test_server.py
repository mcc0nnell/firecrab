from __future__ import annotations

from typing import Any

import pytest
import requests

from firecrab_mcp.server import (
    ApiRequest,
    FireCrabClient,
    FireCrabExecutor,
    FireCrabMcpError,
)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        data: Any = None,
        *,
        request_id: str | None = "req-test",
        content_type: str = "application/json",
    ) -> None:
        self.status_code = status_code
        self._data = data
        self.headers = {"content-type": content_type}
        if request_id is not None:
            self.headers["x-request-id"] = request_id
        self.content = b"" if data is None else b"x"
        self.text = "" if data is None else str(data)

    def json(self) -> Any:
        return self._data


def test_talkpipe_pipeline_calls_firecrab_and_preserves_request_id() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def request_fn(method: str, url: str, **kwargs: Any) -> requests.Response:
        calls.append((method, url, kwargs))
        return FakeResponse(200, [{"id": "vm-1"}])  # type: ignore[return-value]

    executor = FireCrabExecutor(
        FireCrabClient(
            "http://127.0.0.1:5523",
            request_fn=request_fn,
        )
    )

    result = executor.execute("GET", "/api/vms")

    assert result == {
        "statusCode": 200,
        "requestId": "req-test",
        "data": [{"id": "vm-1"}],
    }
    assert calls == [
        (
            "GET",
            "http://127.0.0.1:5523/api/vms",
            {"json": None, "timeout": 10.0},
        )
    ]


def test_create_body_passes_through_without_reencoding_firecrab_schema() -> None:
    seen: dict[str, Any] = {}

    def request_fn(method: str, url: str, **kwargs: Any) -> requests.Response:
        seen.update({"method": method, "url": url, **kwargs})
        return FakeResponse(201, {"id": "vm-2"})  # type: ignore[return-value]

    executor = FireCrabExecutor(FireCrabClient(request_fn=request_fn))
    spec = {"name": "ci-runner", "vcpuCount": 2}

    result = executor.execute("POST", "/api/vms", spec)

    assert result["statusCode"] == 201
    assert seen["json"] is spec


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PATCH", "/api/vms/x"),
        ("GET", "/not-api/vms"),
        ("GET", "/api/../secret"),
    ],
)
def test_pipeline_rejects_requests_outside_explicit_contract(
    method: str, path: str
) -> None:
    executor = FireCrabExecutor(
        FireCrabClient(request_fn=lambda *args, **kwargs: None)  # type: ignore[arg-type]
    )

    with pytest.raises(FireCrabMcpError):
        executor.execute(method, path)


def test_invalid_base_url_is_rejected() -> None:
    with pytest.raises(FireCrabMcpError):
        FireCrabClient("file:///tmp/firecrab.sock")


def test_api_error_is_bounded() -> None:
    def request_fn(method: str, url: str, **kwargs: Any) -> requests.Response:
        return FakeResponse(500, {"error": "x" * 5000})  # type: ignore[return-value]

    executor = FireCrabExecutor(FireCrabClient(request_fn=request_fn))

    with pytest.raises(Exception) as exc_info:
        executor.execute("GET", "/api/vms")

    assert len(str(exc_info.value)) < 2300


def test_request_dataclass_is_typed_and_immutable() -> None:
    request = ApiRequest("GET", "/api/vms")
    assert request.method == "GET"
    with pytest.raises(Exception):
        request.path = "/api/host"  # type: ignore[misc]
