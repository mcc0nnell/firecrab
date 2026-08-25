"""Gitflare source-authority handoff executed through TalkPipe.

The short-lived repository credential never becomes MCP tool output. It exists
only inside this process long enough to be injected into one FireCrab build VM.
"""
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
_LOOPBACK = frozenset({"127.0.0.1", "localhost", "::1"})


class GitflareSourceError(RuntimeError):
    """Raised when Gitflare cannot produce a safe immutable-source handoff."""


@dataclass(frozen=True, slots=True)
class GitflareRequest:
    repo: str
    sha: str
    ttl: int = 900


@dataclass(frozen=True, slots=True)
class GitflareCredential:
    token: str
    expires_at: str | None
    ttl: int


@dataclass(frozen=True, slots=True)
class GitflareHandoff:
    repo: str
    sha: str
    remote: str
    namespace: str
    credential: GitflareCredential
    request_id: str | None


@dataclass(frozen=True, slots=True)
class GitflareCall:
    request: GitflareRequest
    status_code: int
    request_id: str | None
    data: Any
    duration_ms: float


class ValidateGitflareRequest(core.AbstractSegment[GitflareRequest, GitflareRequest]):
    def transform(self, items: Iterable[GitflareRequest]) -> Iterable[GitflareRequest]:
        for item in items:
            repo = item.repo.strip()
            sha = item.sha.strip().lower()
            if not _REPO.fullmatch(repo):
                raise GitflareSourceError("invalid Gitflare repository name")
            if not _GIT_SHA1.fullmatch(sha):
                raise GitflareSourceError("Gitflare source SHA must be a full 40-hex object id")
            if item.ttl < 60 or item.ttl > 900:
                raise GitflareSourceError("Gitflare handoff TTL must be between 60 and 900 seconds")
            yield GitflareRequest(repo=repo, sha=sha, ttl=item.ttl)


class CallGitflare(core.AbstractSegment[GitflareRequest, GitflareCall]):
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
            raise GitflareSourceError("GITFLARE_ADMIN_TOKEN is required")
        self.admin_token = admin_token
        self.timeout_seconds = timeout_seconds
        self.request_fn = request_fn

    def transform(self, items: Iterable[GitflareRequest]) -> Iterable[GitflareCall]:
        for item in items:
            started = time.perf_counter()
            response = self.request_fn(
                "POST",
                f"{self.base_url}/repos/{quote(item.repo, safe='')}/execution-handoffs",
                headers={
                    "authorization": f"Bearer {self.admin_token}",
                    "content-type": "application/json",
                },
                json={"sha": item.sha, "ttl": item.ttl},
                timeout=self.timeout_seconds,
            )
            duration_ms = (time.perf_counter() - started) * 1000.0
            request_id = response.headers.get("x-gitflare-request-id")
            try:
                data = response.json()
            except Exception as error:
                raise GitflareSourceError("Gitflare returned a non-JSON handoff response") from error
            yield GitflareCall(
                request=item,
                status_code=int(response.status_code),
                request_id=request_id,
                data=data,
                duration_ms=duration_ms,
            )


class NormalizeGitflareHandoff(core.AbstractSegment[GitflareCall, GitflareHandoff]):
    def transform(self, items: Iterable[GitflareCall]) -> Iterable[GitflareHandoff]:
        for item in items:
            if item.status_code != 201 or not isinstance(item.data, dict):
                code = item.data.get("code") if isinstance(item.data, dict) else None
                # Do not include response data: a successful response contains a credential.
                raise GitflareSourceError(
                    f"Gitflare source handoff failed with HTTP {item.status_code}"
                    + (f" ({code})" if isinstance(code, str) else "")
                )
            data = item.data
            credential = data.get("credential")
            if data.get("authority") != "gitflare" or data.get("provider") != "cloudflare-artifacts":
                raise GitflareSourceError("Gitflare handoff has an unexpected source authority")
            if data.get("repo") != item.request.repo or str(data.get("sha", "")).lower() != item.request.sha:
                raise GitflareSourceError("Gitflare handoff identity does not match the request")
            if not isinstance(credential, dict) or credential.get("scope") != "read":
                raise GitflareSourceError("Gitflare handoff credential is not read-only")
            token = credential.get("token")
            remote = data.get("remote")
            if not isinstance(token, str) or not token:
                raise GitflareSourceError("Gitflare handoff is missing its repository credential")
            if not isinstance(remote, str) or not _safe_remote(remote):
                raise GitflareSourceError("Gitflare handoff remote is not a credential-free HTTPS URL")
            ttl = credential.get("ttl")
            if not isinstance(ttl, int) or ttl < 60 or ttl > 900:
                raise GitflareSourceError("Gitflare handoff returned an invalid credential TTL")
            yield GitflareHandoff(
                repo=item.request.repo,
                sha=item.request.sha,
                remote=remote,
                namespace=str(data.get("namespace") or "gitflare"),
                credential=GitflareCredential(
                    token=token,
                    expires_at=credential.get("expiresAt") if isinstance(credential.get("expiresAt"), str) else None,
                    ttl=ttl,
                ),
                request_id=item.request_id or (data.get("requestId") if isinstance(data.get("requestId"), str) else None),
            )


class GitflareSourceAuthority:
    """One typed TalkPipe pipeline from immutable source request to handoff."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        admin_token: str | None = None,
        timeout_seconds: float = 10.0,
        request_fn: Any = requests.request,
    ) -> None:
        caller = CallGitflare(
            base_url=base_url or os.getenv("GITFLARE_API_URL", "http://127.0.0.1:8787"),
            admin_token=admin_token if admin_token is not None else os.getenv("GITFLARE_ADMIN_TOKEN", ""),
            timeout_seconds=timeout_seconds,
            request_fn=request_fn,
        )
        pipeline = ValidateGitflareRequest() | caller | NormalizeGitflareHandoff()
        self._run = pipeline.as_function(single_in=True, single_out=True)

    def create_handoff(self, repo: str, sha: str, ttl: int = 900) -> GitflareHandoff:
        return self._run(GitflareRequest(repo=repo, sha=sha, ttl=ttl))


def _normalize_base_url(value: str) -> str:
    value = value.rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise GitflareSourceError("GITFLARE_API_URL must be an absolute HTTP(S) origin")
    if parsed.username is not None or parsed.password is not None or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise GitflareSourceError("GITFLARE_API_URL must be a credential-free origin")
    if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK:
        raise GitflareSourceError("non-loopback GITFLARE_API_URL must use HTTPS")
    return value


def _safe_remote(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.netloc)
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )
