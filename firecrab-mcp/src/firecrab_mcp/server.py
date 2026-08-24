"""MCP adapter for FireCrab.

The shape deliberately follows Jenkins' MCP server: expose explicit,
auditable tools over the product's existing management API, while keeping
transport concerns separate from tool implementation.

TalkPipe is the execution spine between MCP tools and the FireCrab REST API:

    MCP tool -> typed request -> TalkPipe validation/policy/call/evidence -> API

No tool accepts an arbitrary URL or API path. Destructive VM deletion is not
exposed by this first slice, and mutating operations are disabled by default.
"""

from __future__ import annotations

import argparse
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import requests
from mcp.server import MCPServer
from talkpipe.pipe import core

_ALLOWED_METHODS = frozenset({"GET", "POST"})
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


@dataclass(frozen=True, slots=True)
class ApiCall:
    request: ApiRequest
    response: ApiResponse
    duration_ms: float


@dataclass(frozen=True, slots=True)
class FireCrabPolicy:
    """Operator-controlled MCP capability policy.

    Reads are always available. POST operations are hidden behind an explicit
    opt-in so an MCP client cannot create/start/stop VMs merely because the
    server process was launched. DELETE is intentionally absent from the tool
    surface and rejected by request validation.
    """

    allow_mutations: bool = False

    @classmethod
    def from_env(cls) -> "FireCrabPolicy":
        return cls(
            allow_mutations=_env_flag("FIRECRAB_MCP_ALLOW_MUTATIONS"),
        )


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


class EnforcePolicy(core.AbstractSegment[ApiRequest, ApiRequest]):
    """TalkPipe policy gate between MCP intent and FireCrab side effects."""

    def __init__(self, policy: FireCrabPolicy) -> None:
        super().__init__()
        self.policy = policy

    def transform(self, items: Iterable[ApiRequest]) -> Iterable[ApiRequest]:
        for item in items:
            if item.method != "GET" and not self.policy.allow_mutations:
                raise FireCrabMcpError(
                    "mutating FireCrab MCP tools are disabled; set "
                    "FIRECRAB_MCP_ALLOW_MUTATIONS=1 to enable create/start/stop"
                )
            yield item


class CallFireCrab(core.AbstractSegment[ApiRequest, ApiCall]):
    """TalkPipe segment that invokes the native FireCrab API."""

    def __init__(self, client: FireCrabClient) -> None:
        super().__init__()
        self.client = client

    def transform(self, items: Iterable[ApiRequest]) -> Iterable[ApiCall]:
        for item in items:
            started = time.perf_counter()
            response = self.client.request(item)
            duration_ms = (time.perf_counter() - started) * 1000.0
            yield ApiCall(
                request=item,
                response=response,
                duration_ms=duration_ms,
            )


class NormalizeResult(core.AbstractSegment[ApiCall, dict[str, Any]]):
    """Return a stable MCP evidence envelope while preserving FireCrab data."""

    def transform(self, items: Iterable[ApiCall]) -> Iterable[dict[str, Any]]:
        for item in items:
            risk = "read" if item.request.method == "GET" else "mutate"
            yield {
                "operation": {
                    "method": item.request.method,
                    "path": item.request.path,
                    "risk": risk,
                },
                "statusCode": item.response.status_code,
                "requestId": item.response.request_id,
                "durationMs": round(item.duration_ms, 3),
                "data": item.response.data,
            }


class FireCrabExecutor:
    """Typed FireCrab operations executed through one TalkPipe pipeline."""

    def __init__(
        self,
        client: FireCrabClient | None = None,
        *,
        policy: FireCrabPolicy | None = None,
    ) -> None:
        self.client = client or FireCrabClient()
        self.policy = policy or FireCrabPolicy.from_env()
        pipeline = (
            ValidateRequest()
            | EnforcePolicy(self.policy)
            | CallFireCrab(self.client)
            | NormalizeResult()
        )
        self._run = pipeline.as_function(single_in=True, single_out=True)

    def execute(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._run(ApiRequest(method=method, path=path, body=body))


mcp = MCPServer(
    "FireCrab",
    version="0.1.0",
    instructions=(
        "Explicit FireCrab management tools backed by the native API and "
        "executed through TalkPipe. Reads are enabled by default. VM create, "
        "start, and stop require FIRECRAB_MCP_ALLOW_MUTATIONS=1. Destructive "
        "VM deletion is not exposed."
    ),
)


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
    """Create a FireCrab VM. Requires the mutation policy opt-in."""
    return _executor.execute("POST", "/api/vms", spec)


@mcp.tool()
def startVM(vmId: str) -> dict[str, Any]:
    """Start a FireCrab virtual machine. Requires the mutation policy opt-in."""
    return _executor.execute("POST", f"/api/vms/{_id_segment(vmId)}/start")


@mcp.tool()
def stopVM(vmId: str) -> dict[str, Any]:
    """Stop a FireCrab virtual machine. Requires the mutation policy opt-in."""
    return _executor.execute("POST", f"/api/vms/{_id_segment(vmId)}/stop")


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
    if parsed.username is not None or parsed.password is not None:
        raise FireCrabMcpError(
            "FIRECRAB_API_URL must not embed credentials"
        )
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise FireCrabMcpError(
            "FIRECRAB_API_URL must not include a path, query, or fragment"
        )
    return value


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


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


# Environment-dependent executor creation stays after helper definitions so
# invalid operator configuration fails with the intended FireCrabMcpError.
_executor = FireCrabExecutor()


if __name__ == "__main__":
    main()
