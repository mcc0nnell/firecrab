from __future__ import annotations

from typing import Any

import pytest

from firecrab_mcp.gitflare_evidence import (
    HOST_EVIDENCE_ARTIFACTS,
    GitflareEvidenceAuthority,
    GitflareEvidenceError,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
RUN_ID = "33333333-3333-4333-8333-333333333333"


class Response:
    def __init__(self, status_code: int, data: Any) -> None:
        self.status_code = status_code
        self._data = data

    def json(self) -> Any:
        return self._data


def payload(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "schemaVersion": 1,
        "authority": "gitflare-r2",
        "runId": RUN_ID,
        "repo": "firecrab",
        "sha": SHA,
        "uploadBaseUrl": f"https://gitflare.example/evidence/uploads/{RUN_ID}",
        "uploadToken": "evidence-secret",
        "expiresAt": 2_000_000_000,
        "artifacts": list(HOST_EVIDENCE_ARTIFACTS),
    }
    value.update(overrides)
    return value


def test_evidence_handoff_is_typed_and_bound_to_source_identity() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def request_fn(method: str, url: str, **kwargs: Any) -> Response:
        calls.append((method, url, kwargs))
        return Response(201, payload())

    authority = GitflareEvidenceAuthority(
        base_url="https://gitflare.example",
        admin_token="admin",
        request_fn=request_fn,
    )
    handoff = authority.create_handoff("firecrab", SHA)

    assert handoff.run_id == RUN_ID
    assert handoff.repo == "firecrab"
    assert handoff.sha == SHA
    assert handoff.upload_token == "evidence-secret"
    assert handoff.artifacts == HOST_EVIDENCE_ARTIFACTS
    assert calls == [
        (
            "POST",
            "https://gitflare.example/repos/firecrab/evidence-handoffs",
            {
                "headers": {
                    "authorization": "Bearer admin",
                    "content-type": "application/json",
                },
                "json": {"sha": SHA},
                "timeout": 10.0,
            },
        )
    ]


def test_evidence_handoff_rejects_identity_drift() -> None:
    authority = GitflareEvidenceAuthority(
        base_url="https://gitflare.example",
        admin_token="admin",
        request_fn=lambda *args, **kwargs: Response(201, payload(sha="a" * 40)),
    )

    with pytest.raises(GitflareEvidenceError, match="identity does not match"):
        authority.create_handoff("firecrab", SHA)


def test_evidence_handoff_rejects_changed_artifact_contract() -> None:
    authority = GitflareEvidenceAuthority(
        base_url="https://gitflare.example",
        admin_token="admin",
        request_fn=lambda *args, **kwargs: Response(
            201, payload(artifacts=["result", "archive"])
        ),
    )

    with pytest.raises(GitflareEvidenceError, match="artifact contract"):
        authority.create_handoff("firecrab", SHA)
