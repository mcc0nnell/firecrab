from __future__ import annotations

from typing import Any

import pytest

from firecrab_mcp.gitflare_source import GitflareSourceAuthority, GitflareSourceError

SHA = "0123456789abcdef0123456789abcdef01234567"


class Response:
    def __init__(self, status_code: int, data: Any):
        self.status_code = status_code
        self._data = data
        self.headers = {"x-gitflare-request-id": "gf-1"}

    def json(self) -> Any:
        return self._data


def test_source_authority_flows_through_typed_pipeline_and_preserves_identity() -> None:
    calls: list[tuple[Any, ...]] = []

    def request_fn(method: str, url: str, **kwargs: Any) -> Response:
        calls.append((method, url, kwargs))
        return Response(
            201,
            {
                "authority": "gitflare",
                "provider": "cloudflare-artifacts",
                "namespace": "gitflare",
                "repo": "firecrab",
                "sha": SHA,
                "remote": "https://acct.artifacts.cloudflare.net/git/gitflare/firecrab.git",
                "credential": {"scope": "read", "token": "secret", "ttl": 300, "expiresAt": "soon"},
                "requestId": "gf-body",
            },
        )

    authority = GitflareSourceAuthority(
        base_url="https://gitflare.example",
        admin_token="admin",
        request_fn=request_fn,
    )
    handoff = authority.create_handoff("firecrab", SHA, 300)

    assert handoff.repo == "firecrab"
    assert handoff.sha == SHA
    assert handoff.credential.token == "secret"
    assert handoff.credential.ttl == 300
    assert handoff.request_id == "gf-1"
    assert calls[0][0:2] == (
        "POST",
        "https://gitflare.example/repos/firecrab/execution-handoffs",
    )
    assert calls[0][2]["json"] == {"sha": SHA, "ttl": 300}
    assert calls[0][2]["headers"]["authorization"] == "Bearer admin"


def test_identity_mismatch_fails_closed_without_leaking_credential_in_error() -> None:
    def request_fn(*args: Any, **kwargs: Any) -> Response:
        return Response(
            201,
            {
                "authority": "gitflare",
                "provider": "cloudflare-artifacts",
                "namespace": "gitflare",
                "repo": "firecrab",
                "sha": "f" * 40,
                "remote": "https://acct.artifacts.cloudflare.net/git/gitflare/firecrab.git",
                "credential": {"scope": "read", "token": "DO-NOT-LEAK", "ttl": 300},
            },
        )

    authority = GitflareSourceAuthority(
        base_url="https://gitflare.example",
        admin_token="admin",
        request_fn=request_fn,
    )
    with pytest.raises(GitflareSourceError) as exc:
        authority.create_handoff("firecrab", SHA, 300)
    assert "DO-NOT-LEAK" not in str(exc.value)


def test_non_loopback_plain_http_is_rejected() -> None:
    with pytest.raises(GitflareSourceError, match="must use HTTPS"):
        GitflareSourceAuthority(base_url="http://gitflare.example", admin_token="admin")
