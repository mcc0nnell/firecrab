"""Gitflare R2 evidence capability handoff executed through TalkPipe."""
from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import quote, urlsplit

import requests
from talkpipe.pipe import core

_GIT_SHA1 = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)
_REPO = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RUN_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})
HOST_EVIDENCE_ARTIFACTS = (
    "result",
    "archive",
    "sha256s",
    "notices",
    "license-inventory",
)


class GitflareEvidenceError(RuntimeError):
    """Raised when Gitflare cannot produce a safe evidence upload capability."""


@dataclass(frozen=True, slots=True)
class GitflareEvidenceRequest:
    repo: str
    sha: str


@dataclass(frozen=True, slots=True)
class GitflareEvidenceHandoff:
    run_id: str
    repo: str
    sha: str
    upload_base_url: str
    upload_token: str
    expires_at: int
    artifacts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GitflareEvidenceCall:
    request: GitflareEvidenceRequest
    status_code: int
    data: Any
    duration_ms: float


class ValidateEvidenceRequest(
    core.AbstractSegment[GitflareEvidenceRequest, GitflareEvidenceRequest]
):
    def transform(
        self, items: Iterable[GitflareEvidenceRequest]
    ) -> Iterable[GitflareEvidenceRequest]:
        for item in items:
            repo = item.repo.strip()
            sha = item.sha.strip().lower()
            if not _REPO.fullmatch(repo):
                raise GitflareEvidenceError("invalid Gitflare repository name")
            if not _GIT_SHA1.fullmatch(sha):
                raise GitflareEvidenceError(
                    "Gitflare evidence SHA must be a full 40-hex object id"
                )
            yield GitflareEvidenceRequest(repo=repo, sha=sha)


class CallGitflareEvidence(
    core.AbstractSegment[GitflareEvidenceRequest, GitflareEvidenceCall]
):
    def __init__(
        self,
        *,
        base_url: str,
        admin_token: str,
        timeout_seconds: float = 10.0,
        request_fn: Any = requests.request,
    ) -> None:
        super().__init__()
        self.base_url = _normalize_base_url(base_url)
        if not admin_token:
            raise GitflareEvidenceError("GITFLARE_ADMIN_TOKEN is required")
        self.admin_token = admin_token
        self.timeout_seconds = timeout_seconds
        self.request_fn = request_fn

    def transform(
        self, items: Iterable[GitflareEvidenceRequest]
    ) -> Iterable[GitflareEvidenceCall]:
        for item in items:
            started = time.perf_counter()
            response = self.request_fn(
                "POST",
                f"{self.base_url}/repos/{quote(item.repo, safe='')}/evidence-handoffs",
                headers={
                    "authorization": f"Bearer {self.admin_token}",
                    "content-type": "application/json",
                },
                json={"sha": item.sha},
                timeout=self.timeout_seconds,
            )
            duration_ms = (time.perf_counter() - started) * 1000.0
            try:
                data = response.json()
            except Exception as error:
                raise GitflareEvidenceError(
                    "Gitflare returned a non-JSON evidence handoff"
                ) from error
            yield GitflareEvidenceCall(
                request=item,
                status_code=int(response.status_code),
                data=data,
                duration_ms=duration_ms,
            )


class NormalizeEvidenceHandoff(
    core.AbstractSegment[GitflareEvidenceCall, GitflareEvidenceHandoff]
):
    def transform(
        self, items: Iterable[GitflareEvidenceCall]
    ) -> Iterable[GitflareEvidenceHandoff]:
        for item in items:
            if item.status_code != 201 or not isinstance(item.data, dict):
                code = item.data.get("code") if isinstance(item.data, dict) else None
                raise GitflareEvidenceError(
                    f"Gitflare evidence handoff failed with HTTP {item.status_code}"
                    + (f" ({code})" if isinstance(code, str) else "")
                )
            data = item.data
            if data.get("authority") != "gitflare-r2":
                raise GitflareEvidenceError(
                    "Gitflare evidence handoff has an unexpected authority"
                )
            if (
                data.get("repo") != item.request.repo
                or str(data.get("sha", "")).lower() != item.request.sha
            ):
                raise GitflareEvidenceError(
                    "Gitflare evidence handoff identity does not match the request"
                )
            run_id = data.get("runId")
            upload_base_url = data.get("uploadBaseUrl")
            upload_token = data.get("uploadToken")
            expires_at = data.get("expiresAt")
            artifacts = data.get("artifacts")
            if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
                raise GitflareEvidenceError(
                    "Gitflare evidence handoff has an invalid run id"
                )
            if not isinstance(upload_base_url, str) or not _safe_upload_url(
                upload_base_url
            ):
                raise GitflareEvidenceError(
                    "Gitflare evidence upload URL must be credential-free HTTPS"
                )
            if not isinstance(upload_token, str) or not upload_token:
                raise GitflareEvidenceError(
                    "Gitflare evidence handoff is missing its upload capability"
                )
            if not isinstance(expires_at, int) or expires_at <= 0:
                raise GitflareEvidenceError(
                    "Gitflare evidence handoff has an invalid expiry"
                )
            if artifacts != list(HOST_EVIDENCE_ARTIFACTS):
                raise GitflareEvidenceError(
                    "Gitflare evidence artifact contract does not match FireCrab host v1"
                )
            yield GitflareEvidenceHandoff(
                run_id=run_id,
                repo=item.request.repo,
                sha=item.request.sha,
                upload_base_url=upload_base_url,
                upload_token=upload_token,
                expires_at=expires_at,
                artifacts=HOST_EVIDENCE_ARTIFACTS,
            )


class GitflareEvidenceAuthority:
    """Typed TalkPipe pipeline from immutable source identity to one upload capability."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        admin_token: str | None = None,
        timeout_seconds: float = 10.0,
        request_fn: Any = requests.request,
    ) -> None:
        caller = CallGitflareEvidence(
            base_url=base_url
            or os.getenv("GITFLARE_API_URL", "http://127.0.0.1:8787"),
            admin_token=admin_token
            if admin_token is not None
            else os.getenv("GITFLARE_ADMIN_TOKEN", ""),
            timeout_seconds=timeout_seconds,
            request_fn=request_fn,
        )
        pipeline = ValidateEvidenceRequest() | caller | NormalizeEvidenceHandoff()
        self._run = pipeline.as_function(single_in=True, single_out=True)

    def create_handoff(self, repo: str, sha: str) -> GitflareEvidenceHandoff:
        return self._run(GitflareEvidenceRequest(repo=repo, sha=sha))


def _normalize_base_url(value: str) -> str:
    value = value.rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise GitflareEvidenceError(
            "GITFLARE_API_URL must be an absolute HTTP(S) origin"
        )
    if (
        parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise GitflareEvidenceError(
            "GITFLARE_API_URL must be a credential-free origin"
        )
    if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK:
        raise GitflareEvidenceError(
            "non-loopback GITFLARE_API_URL must use HTTPS"
        )
    return value


def _safe_upload_url(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and parsed.path.startswith("/evidence/uploads/")
    )
