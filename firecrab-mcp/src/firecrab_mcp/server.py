"""MCP adapter for FireCrab.

The shape deliberately follows Jenkins' MCP server: expose explicit,
auditable tools over the product's existing management API, while keeping
transport concerns separate from tool implementation.

TalkPipe is the execution spine between MCP tools and the FireCrab REST API:

    MCP tool -> typed request -> TalkPipe validation/call/normalization -> API

No tool accepts an arbitrary URL or API path.
"""

from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests
from mcp.server import MCPServer
from talkpipe.pipe import core

_ALLOWED_METHODS = frozenset({"GET", "POST", "DELETE"})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class FireCrabMcpError(RuntimeError):
    """Base error exposed by the FireCrab MCP adapter."""


class FireCrabApiError(FireCrabMcpError):
    """Raised when the FireCrab API rejects a request."""


@dataclass(frozen=True, slots=True)
class ApiRequest:
    method: str
    path: str
    body: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ApiResponse:
    status_code: int
    request_id: str | None
    data: Any


RequestFn = Callable[..., requests.Response]


class FireCrabClient:
    """Small HTTP client for the native FireCrab API.

    The base URL is operator configuration, never MCP tool input. That prevents
    an MCP caller from turning this adapter into a generic HTTP/SSRF primitive.
    """

    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout_seconds: float = 10.0,
        request_fn: RequestFn = requests.request,
    ) -> None:
        configured = base_url or os.getenv(
            "FIRECRAB_API_URL", "http://127.0.0.1:5523"
        )
        self.base_url = _normalize_base_url(configured)
        self.timeout_seconds = timeout_seconds
        self._request_fn = request_fn

    def request(self, request: ApiRequest) -> ApiResponse:
        response = self._request_fn(
            request.method,
            f"{self.base_url}{request.path}",
            json=request.body,
            timeout=self.timeout_seconds,
        )

        request_id = response.headers.get("x-request-id")
        data = _decode_response(response)

        if response.status_code >= 400:
            detail = _bounded_error_detail(data)
            raise FireCrabApiError(
                f"FireCrab API returned HTTP {response.status_code}"
                f" for {request.method} {request.path}: {detail}"
            )

        return ApiResponse(
            status_code=response.status_code,
            request_id=request_id,
            data=data,
        )


class ValidateRequest(core.AbstractSegment[ApiRequest, ApiRequest]):
    """Reject requests outside the small MCP-to-FireCrab contract."""

    def transform(self, items: Iterable[ApiRequest]) -> Iterable[ApiRequest]:
        for item in items:
            method = item.method.upper()
            if method not in _ALLOWED_METHODS:
                raise FireCrabMcpError(f"unsupported FireCrab method: {method}")
            if not item.path.startswith("/api/"):
                raise FireCrabMcpError("FireCrab MCP paths must stay under /api/")
            if ".." in item.path.split("/"):
                raise FireCrabMcpError("FireCrab MCP paths may not traverse")
            yield ApiRequest(method=method, path=item.path, body=item.body)


class CallFireCrab(core.AbstractSegment[ApiRequest, ApiResponse]):
    """TalkPipe segment that invokes the native FireCrab API."""

    def __init__(self, client: FireCrabClient) -> None:
        super().__init__()
        self.client = client

    def transform(self, items: Iterable[ApiRequest]) -> Iterable[ApiResponse]:
        for item in items:
            yield self.client.request(item)


class NormalizeResult(core.AbstractSegment[ApiResponse, dict[str, Any]]):
    """Return a stable MCP result envelope while preserving FireCrab payloads."""

    def transform(
        self, items: Iterable[ApiResponse]
    ) -> Iterable[dict[str, Any]]:
        for item in items:
            yield {
                "statusCode": item.status_code,
                "requestId": item.request_id,
                "data": item.data,
            }


class FireCrabExecutor:
    """Typed FireCrab operations executed through one TalkPipe pipeline."""

    def __init__(self, client: FireCrabClient | None = None) -> None:
        self.client = client or FireCrabClient()
        pipeline = ValidateRequest() | CallFireCrab(self.client) | NormalizeResult()
        self._run = pipeline.as_function(single_in=True, single_out=True)

    def execute(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._run(ApiRequest(method=method, path=path, body=body))


_executor = FireCrabExecutor()
mcp = MCPServer("FireCrab")


@mcp.tool()
def getStatus() -> dict[str, Any]:
    """Get FireCrab host health/readiness information."""
    return _executor.execute("GET", "/api/host")


@mcp.tool()
def listVMs() -> dict[str, Any]:
    """List FireCrab virtual machines."""
    return _executor.execute("GET", "/api/vms")


@mcp.tool()
def getVM(vmId: str) -> dict[str, Any]:
    """Get one FireCrab virtual machine by ID."""
    return _executor.execute("GET", f"/api/vms/{_id_segment(vmId)}")


@mcp.tool()
def createVM(spec: dict[str, Any]) -> dict[str, Any]:
    """Create a FireCrab VM from the native FireCrab create payload."""
    return _executor.execute("POST", "/api/vms", spec)


@mcp.tool()
def startVM(vmId: str) -> dict[str, Any]:
    """Start a FireCrab virtual machine."""
    return _executor.execute("POST", f"/api/vms/{_id_segment(vmId)}/start")


@mcp.tool()
def stopVM(vmId: str) -> dict[str, Any]:
    """Stop a FireCrab virtual machine."""
    return _executor.execute("POST", f"/api/vms/{_id_segment(vmId)}/stop")


@mcp.tool()
def deleteVM(vmId: str) -> dict[str, Any]:
    """Delete a FireCrab virtual machine."""
    return _executor.execute("DELETE", f"/api/vms/{_id_segment(vmId)}")


@mcp.tool()
def getVMLog(vmId: str) -> dict[str, Any]:
    """Read the FireCrab log for one virtual machine."""
    return _executor.execute("GET", f"/api/vms/{_id_segment(vmId)}/log")


def _id_segment(value: str) -> str:
    value = value.strip()
    if not value or "/" in value or value in {".", ".."}:
        raise FireCrabMcpError("invalid FireCrab VM id")
    return value


def _normalize_base_url(value: str) -> str:
    value = value.rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise FireCrabMcpError(
            "FIRECRAB_API_URL must be an absolute http(s) URL"
        )
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise FireCrabMcpError(
            "FIRECRAB_API_URL must not include a path, query, or fragment"
        )
    return value


def _decode_response(response: requests.Response) -> Any:
    if response.status_code == 204 or not response.content:
        return None
    content_type = response.headers.get("content-type", "")
    if "json" in content_type.lower():
        return response.json()
    try:
        return response.json()
    except requests.exceptions.JSONDecodeError:
        return response.text


def _bounded_error_detail(value: Any, limit: int = 2048) -> str:
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="FireCrab MCP server")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http"),
        default=os.getenv("FIRECRAB_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument(
        "--host",
        default=os.getenv("FIRECRAB_MCP_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("FIRECRAB_MCP_PORT", "8000")),
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    if args.transport == "stdio":
        mcp.run()
        return

    # Unlike Jenkins, this MVP does not yet inherit an authenticated controller
    # session. Keep the HTTP transport local until MCP-side auth is wired.
    if args.host not in _LOOPBACK_HOSTS:
        raise SystemExit(
            "refusing non-loopback MCP bind before MCP authentication is configured"
        )
    mcp.run(
        transport="streamable-http",
        host=args.host,
        port=args.port,
        stateless_http=True,
        json_response=True,
    )


if __name__ == "__main__":
    main()
