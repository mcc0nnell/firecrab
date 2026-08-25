"""Gitflare-aware FireCrab builds with immutable source materialization."""
from __future__ import annotations

import re
import uuid
from typing import Any, Protocol

from .gitflare_source import GitflareHandoff

_BUILD_PREFIX = "ci-"
_MAX_COMMAND_BYTES = 28 * 1024
_MAX_LABEL_BYTES = 160


class Executor(Protocol):
    def execute(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]: ...


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
    def create_handoff(self, repo: str, sha: str, ttl: int = 900) -> GitflareHandoff: ...


class GitflareBuildError(RuntimeError):
    pass


class GitflareBuildExecutor:
    def __init__(self, executor: Executor, source: SourceAuthority, profile: RunnerProfile) -> None:
        self.executor = executor
        self.source = source
        self.profile = profile

    def trigger_build(self, *, label: str, repo: str, sha: str, command: str, ttl: int = 900) -> dict[str, Any]:
        self.profile.validate()
        label = _validate_label(label)
        command = _validate_command(command)
        handoff = self.source.create_handoff(repo, sha, ttl)
        build_name = _build_name(label)

        shell = self.executor.execute("POST", "/api/shells", {
            "name": build_name,
            "description": f"Gitflare {handoff.repo}@{handoff.sha} FireCrab build",
            "content": _gitflare_build_script(command),
        })
        shell_data = _mapping_data(shell, "create shell")
        shell_id = _required_string(shell_data, "shellId", "create shell")

        # Credential is not present in the versioned Shell or MCP output. It is a
        # short-lived, read-only VM environment value and is unset before the
        # user/build command runs.
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
            "env": {
                "GITFLARE_SOURCE_REMOTE": handoff.remote,
                "GITFLARE_SOURCE_TOKEN": handoff.credential.token,
                "GITFLARE_EXPECTED_SHA": handoff.sha,
            },
        }
        if self.profile.storage_root:
            vm_body["storageRoot"] = self.profile.storage_root

        created = self.executor.execute("POST", "/api/vms", vm_body)
        build_id = _required_string(_mapping_data(created, "create build VM"), "id", "create build VM")
        started = self.executor.execute("POST", f"/api/vms/{build_id}/start")
        started_data = _mapping_data(started, "start build VM")
        return {
            "buildId": build_id,
            "label": label,
            "runner": "firecrab",
            "vmName": build_name,
            "phase": _phase_for_vm_state(str(started_data.get("state", "starting"))),
            "source": {
                "authority": "gitflare",
                "provider": "cloudflare-artifacts",
                "namespace": handoff.namespace,
                "repo": handoff.repo,
                "sha": handoff.sha,
            },
            "requestIds": {
                "gitflare": handoff.request_id,
                "shell": shell.get("requestId"),
                "create": created.get("requestId"),
                "start": started.get("requestId"),
            },
        }

    def trigger_host_assurance(self, *, repo: str, sha: str, ttl: int = 900) -> dict[str, Any]:
        argv = ["bash", "scripts/gitflare-host-assurance.sh", "--target", "x86_64-unknown-linux-gnu"]
        result = self.trigger_build(
            label=f"assurance-host-{sha[:12]}",
            repo=repo,
            sha=sha,
            command=" ".join(argv),
            ttl=ttl,
        )
        result["adapter"] = {
            "family": "firecrab",
            "version": "v1",
            "operation": "host.assure",
            "argv": argv,
        }
        result["expectedSha"] = result["source"]["sha"]
        return result


def _gitflare_build_script(command: str) -> str:
    return f'''#!/bin/sh
set -eu
: "${{GITFLARE_SOURCE_REMOTE:?missing Gitflare source remote}}"
: "${{GITFLARE_SOURCE_TOKEN:?missing Gitflare source token}}"
: "${{GITFLARE_EXPECTED_SHA:?missing Gitflare expected SHA}}"
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
{command.rstrip()}
'''


def _validate_label(value: str) -> str:
    value = value.strip()
    if not value or len(value.encode("utf-8")) > _MAX_LABEL_BYTES:
        raise GitflareBuildError("invalid build label")
    return value


def _validate_command(value: str) -> str:
    if not value.strip() or "\x00" in value or len(value.encode("utf-8")) > _MAX_COMMAND_BYTES:
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
        raise GitflareBuildError(f"{operation} returned an unexpected FireCrab payload")
    return data


def _required_string(data: dict[str, Any], key: str, operation: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise GitflareBuildError(f"{operation} response is missing {key}")
    return value


def _phase_for_vm_state(state: str) -> str:
    return {"created":"queued","starting":"starting","running":"running","stopping":"stopping","stopped":"cancelled","error":"error"}.get(state,"unknown")
