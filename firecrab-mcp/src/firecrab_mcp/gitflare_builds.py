"""Gitflare-aware FireCrab builds with immutable source materialization."""
from __future__ import annotations

import re
import uuid
from typing import Any, Protocol

from .gitflare_evidence import GitflareEvidenceHandoff
from .gitflare_source import GitflareHandoff

_BUILD_PREFIX = "ci-"
_MAX_COMMAND_BYTES = 28 * 1024
_MAX_LABEL_BYTES = 160


class Executor(Protocol):
    def execute(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> dict[str, Any]: ...


class RunnerProfile(Protocol):
    template: str | None
    micro_network_id: str | None
    cpu: int
    ram: int
    disk_gb: int
    egress_policy: str
    storage_root: str | None

    def validate(self) -> None: ...


class SourceAuthority(Protocol):
    def create_handoff(
        self, repo: str, sha: str, ttl: int = 900
    ) -> GitflareHandoff: ...


class EvidenceAuthority(Protocol):
    def create_handoff(self, repo: str, sha: str) -> GitflareEvidenceHandoff: ...


class GitflareBuildError(RuntimeError):
    pass


class GitflareBuildExecutor:
    def __init__(
        self,
        executor: Executor,
        source: SourceAuthority,
        profile: RunnerProfile,
        evidence: EvidenceAuthority | None = None,
    ) -> None:
        self.executor = executor
        self.source = source
        self.profile = profile
        self.evidence = evidence

    def trigger_build(
        self,
        *,
        label: str,
        repo: str,
        sha: str,
        command: str,
        ttl: int = 900,
    ) -> dict[str, Any]:
        self.profile.validate()
        label = _validate_label(label)
        command = _validate_command(command)
        source = self.source.create_handoff(repo, sha, ttl)
        return self._create_build(label=label, command=command, source=source)

    def trigger_host_assurance(
        self, *, repo: str, sha: str, ttl: int = 900
    ) -> dict[str, Any]:
        self.profile.validate()
        if self.evidence is None:
            raise GitflareBuildError(
                "Gitflare evidence authority is required for host assurance"
            )
        source = self.source.create_handoff(repo, sha, ttl)
        evidence = self.evidence.create_handoff(repo, source.sha)
        if evidence.repo != source.repo or evidence.sha.lower() != source.sha.lower():
            raise GitflareBuildError(
                "Gitflare source and evidence handoffs do not bind the same immutable object"
            )

        argv = [
            "bash",
            "scripts/gitflare-host-assurance.sh",
            "--target",
            "x86_64-unknown-linux-gnu",
        ]
        result = self._create_build(
            label=_validate_label(f"assurance-host-{source.sha[:12]}"),
            command=" ".join(argv),
            source=source,
            evidence=evidence,
        )
        result["adapter"] = {
            "family": "firecrab",
            "version": "v1",
            "operation": "host.assure",
            "argv": argv,
        }
        result["expectedSha"] = source.sha
        return result

    def _create_build(
        self,
        *,
        label: str,
        command: str,
        source: GitflareHandoff,
        evidence: GitflareEvidenceHandoff | None = None,
    ) -> dict[str, Any]:
        build_name = _build_name(label)
        shell = self.executor.execute(
            "POST",
            "/api/shells",
            {
                "name": build_name,
                "description": f"Gitflare {source.repo}@{source.sha} FireCrab build",
                "content": _gitflare_build_script(
                    command, upload_evidence=evidence is not None
                ),
            },
        )
        shell_data = _mapping_data(shell, "create shell")
        shell_id = _required_string(shell_data, "shellId", "create shell")

        env = {
            "GITFLARE_SOURCE_REMOTE": source.remote,
            "GITFLARE_SOURCE_TOKEN": source.credential.token,
            "GITFLARE_EXPECTED_SHA": source.sha,
        }
        if evidence is not None:
            env.update(
                {
                    "GITFLARE_EVIDENCE_UPLOAD_URL": evidence.upload_base_url,
                    "GITFLARE_EVIDENCE_UPLOAD_TOKEN": evidence.upload_token,
                }
            )

        # Secrets are only VM bootstrap environment. The versioned Shell holds
        # variable references, not credentials; the wrapper unsets both source
        # and evidence tokens before the subject assurance command executes.
        vm_body: dict[str, Any] = {
            "name": build_name,
            "template": self.profile.template,
            "ram": self.profile.ram,
            "cpu": self.profile.cpu,
            "diskGb": self.profile.disk_gb,
            "egressPolicy": self.profile.egress_policy,
            "microNetworkId": self.profile.micro_network_id,
            "shellIds": [shell_id],
            "portForwards": [],
            "env": env,
        }
        if self.profile.storage_root:
            vm_body["storageRoot"] = self.profile.storage_root

        created = self.executor.execute("POST", "/api/vms", vm_body)
        build_id = _required_string(
            _mapping_data(created, "create build VM"), "id", "create build VM"
        )
        started = self.executor.execute("POST", f"/api/vms/{build_id}/start")
        started_data = _mapping_data(started, "start build VM")
        result: dict[str, Any] = {
            "buildId": build_id,
            "label": label,
            "runner": "firecrab",
            "vmName": build_name,
            "phase": _phase_for_vm_state(str(started_data.get("state", "starting"))),
            "source": {
                "authority": "gitflare",
                "provider": "cloudflare-artifacts",
                "namespace": source.namespace,
                "repo": source.repo,
                "sha": source.sha,
            },
            "requestIds": {
                "gitflare": source.request_id,
                "shell": shell.get("requestId"),
                "create": created.get("requestId"),
                "start": started.get("requestId"),
            },
        }
        if evidence is not None:
            result["evidence"] = {
                "authority": "gitflare-r2",
                "runId": evidence.run_id,
                "sourceSha": evidence.sha,
                "artifacts": list(evidence.artifacts),
            }
        return result


def _gitflare_build_script(command: str, *, upload_evidence: bool = False) -> str:
    evidence_preamble = ""
    if upload_evidence:
        evidence_preamble = '''
: "${GITFLARE_EVIDENCE_UPLOAD_URL:?missing Gitflare evidence upload URL}"
: "${GITFLARE_EVIDENCE_UPLOAD_TOKEN:?missing Gitflare evidence upload token}"
evidence_upload_url=$GITFLARE_EVIDENCE_UPLOAD_URL
evidence_upload_token=$GITFLARE_EVIDENCE_UPLOAD_TOKEN
unset GITFLARE_EVIDENCE_UPLOAD_URL GITFLARE_EVIDENCE_UPLOAD_TOKEN
'''

    prefix = f'''#!/bin/sh
set -eu
: "${{GITFLARE_SOURCE_REMOTE:?missing Gitflare source remote}}"
: "${{GITFLARE_SOURCE_TOKEN:?missing Gitflare source token}}"
: "${{GITFLARE_EXPECTED_SHA:?missing Gitflare expected SHA}}"{evidence_preamble}
workspace="${{FIRECRAB_BUILD_WORKSPACE:-/workspace/source}}"
rm -rf "$workspace"
mkdir -p "$(dirname "$workspace")"
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=http.extraHeader
export GIT_CONFIG_VALUE_0="Authorization: Bearer $GITFLARE_SOURCE_TOKEN"
git clone --quiet --no-checkout "$GITFLARE_SOURCE_REMOTE" "$workspace"
cd "$workspace"
git fetch --quiet --no-tags origin "$GITFLARE_EXPECTED_SHA"
git checkout --quiet --detach "$GITFLARE_EXPECTED_SHA"
actual_sha="$(git rev-parse HEAD | tr '[:upper:]' '[:lower:]')"
expected_sha="$(printf '%s' "$GITFLARE_EXPECTED_SHA" | tr '[:upper:]' '[:lower:]')"
if [ "$actual_sha" != "$expected_sha" ]; then
  echo "GITFLARE_SOURCE_SHA_MISMATCH expected=$expected_sha actual=$actual_sha" >&2
  exit 66
fi
unset GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0 GITFLARE_SOURCE_TOKEN
export CI=true
'''
    if not upload_evidence:
        return prefix + command.rstrip() + "\n"

    return (
        prefix
        + '''set +e
'''
        + command.rstrip()
        + '''
subject_rc=$?
set -e
set +e
EVIDENCE_UPLOAD_URL="$evidence_upload_url" \
EVIDENCE_UPLOAD_TOKEN="$evidence_upload_token" \
python3 - <<'PY_EVIDENCE'
import hashlib
import http.client
import os
from pathlib import Path
from urllib.parse import urlsplit

base = urlsplit(os.environ["EVIDENCE_UPLOAD_URL"])
if base.scheme != "https" or not base.hostname:
    raise SystemExit("evidence upload URL is not HTTPS")
token = os.environ["EVIDENCE_UPLOAD_TOKEN"]
artifacts = {
    "result": (
        Path("dist/assurance/host/x86_64-unknown-linux-gnu/result.json"),
        "application/json",
    ),
    "archive": (
        Path("dist/assurance/host/x86_64-unknown-linux-gnu/firecrab-host-x86_64-gnu.tar.gz"),
        "application/gzip",
    ),
    "sha256s": (
        Path("dist/assurance/host/x86_64-unknown-linux-gnu/SHA256SUMS"),
        "text/plain; charset=utf-8",
    ),
    "notices": (
        Path("dist/compliance/THIRD_PARTY_NOTICES.txt"),
        "text/plain; charset=utf-8",
    ),
    "license-inventory": (
        Path("dist/compliance/release-license-inventory.json"),
        "application/json",
    ),
}

uploaded = 0
for artifact, (path, content_type) in artifacts.items():
    if not path.is_file() or path.stat().st_size <= 0:
        continue
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    size = path.stat().st_size
    conn = http.client.HTTPSConnection(base.hostname, base.port or 443, timeout=60)
    target = f"{base.path.rstrip('/')}/{artifact}"
    conn.putrequest("PUT", target)
    conn.putheader("Authorization", f"Bearer {token}")
    conn.putheader("Content-Type", content_type)
    conn.putheader("Content-Length", str(size))
    conn.putheader("X-Gitflare-Sha256", digest.hexdigest())
    conn.endheaders()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            conn.send(chunk)
    response = conn.getresponse()
    detail = response.read(4096)
    if response.status not in (201, 204):
        raise SystemExit(
            f"evidence upload failed for {artifact}: HTTP {response.status} {detail!r}"
        )
    conn.close()
    uploaded += 1

if uploaded == 0:
    raise SystemExit("assurance produced no evidence artifacts")
PY_EVIDENCE
upload_rc=$?
set -e
unset evidence_upload_url evidence_upload_token
if [ "$upload_rc" -ne 0 ]; then
  echo "GITFLARE_EVIDENCE_UPLOAD_FAILED rc=$upload_rc" >&2
  exit 74
fi
exit "$subject_rc"
'''
    )


def _validate_label(value: str) -> str:
    value = value.strip()
    if not value or len(value.encode("utf-8")) > _MAX_LABEL_BYTES:
        raise GitflareBuildError("invalid build label")
    return value


def _validate_command(value: str) -> str:
    if (
        not value.strip()
        or "\x00" in value
        or len(value.encode("utf-8")) > _MAX_COMMAND_BYTES
    ):
        raise GitflareBuildError("invalid build command")
    return value


def _build_name(label: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-._").lower() or "build"
    suffix = uuid.uuid4().hex[:10]
    room = 64 - len(_BUILD_PREFIX) - len(suffix) - 1
    return f"{_BUILD_PREFIX}{slug[:room]}-{suffix}"


def _mapping_data(result: dict[str, Any], operation: str) -> dict[str, Any]:
    data = result.get("data")
    if not isinstance(data, dict):
        raise GitflareBuildError(
            f"{operation} returned an unexpected FireCrab payload"
        )
    return data


def _required_string(data: dict[str, Any], key: str, operation: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise GitflareBuildError(f"{operation} response is missing {key}")
    return value


def _phase_for_vm_state(state: str) -> str:
    return {
        "created": "queued",
        "starting": "starting",
        "running": "running",
        "stopping": "stopping",
        "stopped": "cancelled",
        "error": "error",
    }.get(state, "unknown")
