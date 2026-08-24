from __future__ import annotations

import asyncio
from typing import Any

import pytest
import requests

from firecrab_mcp.server import (
    ApiRequest,
    FireCrabClient,
    FireCrabExecutor,
    FireCrabMcpError,
    FireCrabPolicy,
    mcp,
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


def test_talkpipe_pipeline_calls_firecrab_and_preserves_evidence() -> None:
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

    assert result["operation"] == {
        "method": "GET",
        "path": "/api/vms",
        "risk": "read",
    }
    assert result["statusCode"] == 200
    assert result["requestId"] == "req-test"
    assert result["durationMs"] >= 0
    assert result["data"] == [{"id": "vm-1"}]
    assert calls == [
        (
            "GET",
            "http://127.0.0.1:5523/api/vms",
            {"json": None, "timeout": 10.0},
        )
    ]


def test_mutations_are_denied_before_http_by_default() -> None:
    calls: list[tuple[Any, ...]] = []

    def request_fn(*args: Any, **kwargs: Any) -> requests.Response:
        calls.append(args)
        return FakeResponse(201, {"id": "vm-2"})  # type: ignore[return-value]

    executor = FireCrabExecutor(
        FireCrabClient(request_fn=request_fn),
        policy=FireCrabPolicy(allow_mutations=False),
    )

    with pytest.raises(FireCrabMcpError, match="mutating FireCrab MCP tools are disabled"):
        executor.execute("POST", "/api/vms", {"name": "ci-runner"})

    assert calls == []


def test_create_body_passes_through_when_mutations_are_enabled() -> None:
    seen: dict[str, Any] = {}

    def request_fn(method: str, url: str, **kwargs: Any) -> requests.Response:
        seen.update({"method": method, "url": url, **kwargs})
        return FakeResponse(201, {"id": "vm-2"})  # type: ignore[return-value]

    executor = FireCrabExecutor(
        FireCrabClient(request_fn=request_fn),
        policy=FireCrabPolicy(allow_mutations=True),
    )
    spec = {"name": "ci-runner", "vcpuCount": 2}

    result = executor.execute("POST", "/api/vms", spec)

    assert result["operation"]["risk"] == "mutate"
    assert result["statusCode"] == 201
    assert seen["json"] is spec


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PATCH", "/api/vms/x"),
        ("DELETE", "/api/vms/x"),
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


def test_base_url_rejects_embedded_credentials() -> None:
    with pytest.raises(FireCrabMcpError):
        FireCrabClient("http://user:secret@127.0.0.1:5523")


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


def test_mcp_surface_publishes_jenkins_style_safety_annotations() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}

    assert set(tools) == {
        "getStatus",
        "listVMs",
        "getVM",
        "createVM",
        "startVM",
        "stopVM",
        "getVMLog",
    }

    for name in {"getStatus", "listVMs", "getVM", "getVMLog"}:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is True
        assert annotations.destructive_hint is False
        assert annotations.idempotent_hint is True
        assert tools[name].output_schema is not None

    for name in {"createVM", "startVM", "stopVM"}:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is False
        assert annotations.destructive_hint is False
        assert tools[name].output_schema is not None
