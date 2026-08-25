from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from firecrab_mcp.gitflare_builds import GitflareBuildError, GitflareBuildExecutor
from firecrab_mcp.gitflare_evidence import (
    HOST_EVIDENCE_ARTIFACTS,
    GitflareEvidenceHandoff,
)
from firecrab_mcp.gitflare_source import GitflareCredential, GitflareHandoff

SHA = "0123456789abcdef0123456789abcdef01234567"
BUILD_ID = "33333333-3333-4333-8333-333333333333"
RUN_ID = "44444444-4444-4444-8444-444444444444"


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def execute(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, path, body))
        if path == "/api/shells":
            return {"requestId": "shell", "data": {"shellId": "shell-id"}}
        if path == "/api/vms":
            return {
                "requestId": "create",
                "data": {"id": BUILD_ID, "state": "created"},
            }
        if path.endswith("/start"):
            return {
                "requestId": "start",
                "data": {"id": BUILD_ID, "state": "starting"},
            }
        raise AssertionError(path)


@dataclass
class Profile:
    template: str | None = "ubuntu-26.04"
    micro_network_id: str | None = "11111111-1111-4111-8111-111111111111"
    cpu: int = 2
    ram: int = 2048
    disk_gb: int = 20
    egress_policy: str = "internet"
    storage_root: str | None = None

    def validate(self) -> None:
        assert self.template and self.micro_network_id


class Source:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, int]] = []

    def create_handoff(
        self, repo: str, sha: str, ttl: int = 900
    ) -> GitflareHandoff:
        self.calls.append((repo, sha, ttl))
        return GitflareHandoff(
            repo=repo,
            sha=sha,
            remote="https://acct.artifacts.cloudflare.net/git/gitflare/firecrab.git",
            namespace="gitflare",
            credential=GitflareCredential("secret-token", "soon", ttl),
            request_id="gf-1",
        )


class Evidence:
    def __init__(self, *, sha: str = SHA) -> None:
        self.sha = sha
        self.calls: list[tuple[str, str]] = []

    def create_handoff(self, repo: str, sha: str) -> GitflareEvidenceHandoff:
        self.calls.append((repo, sha))
        return GitflareEvidenceHandoff(
            run_id=RUN_ID,
            repo=repo,
            sha=self.sha,
            upload_base_url=f"https://gitflare.example/evidence/uploads/{RUN_ID}",
            upload_token="evidence-secret",
            expires_at=2_000_000_000,
            artifacts=HOST_EVIDENCE_ARTIFACTS,
        )


def test_gitflare_build_injects_source_env_not_versioned_shell_or_result() -> None:
    executor = FakeExecutor()
    source = Source()
    builds = GitflareBuildExecutor(executor, source, Profile())

    result = builds.trigger_build(
        label="unit",
        repo="firecrab",
        sha=SHA,
        command="pytest",
    )

    shell = executor.calls[0][2]
    vm = executor.calls[1][2]
    assert shell is not None and vm is not None
    assert "secret-token" not in shell["content"]
    assert "GITFLARE_SOURCE_TOKEN" in shell["content"]
    assert "unset GIT_CONFIG_COUNT" in shell["content"]
    assert vm["env"]["GITFLARE_SOURCE_TOKEN"] == "secret-token"
    assert vm["env"]["GITFLARE_EXPECTED_SHA"] == SHA
    assert result["source"] == {
        "authority": "gitflare",
        "provider": "cloudflare-artifacts",
        "namespace": "gitflare",
        "repo": "firecrab",
        "sha": SHA,
    }
    assert "credential" not in result
    assert "evidence" not in result


def test_assurance_build_reconstructs_contract_and_hides_evidence_capability() -> None:
    executor = FakeExecutor()
    source = Source()
    evidence = Evidence()
    builds = GitflareBuildExecutor(executor, source, Profile(), evidence)

    result = builds.trigger_host_assurance(repo="firecrab", sha=SHA)

    assert result["expectedSha"] == SHA
    assert result["adapter"] == {
        "family": "firecrab",
        "version": "v1",
        "operation": "host.assure",
        "argv": [
            "bash",
            "scripts/gitflare-host-assurance.sh",
            "--target",
            "x86_64-unknown-linux-gnu",
        ],
    }
    assert result["evidence"] == {
        "authority": "gitflare-r2",
        "runId": RUN_ID,
        "sourceSha": SHA,
        "artifacts": list(HOST_EVIDENCE_ARTIFACTS),
    }
    assert "evidence-secret" not in repr(result)
    assert "upload" not in result["evidence"]

    shell = executor.calls[0][2]
    vm = executor.calls[1][2]
    assert shell is not None and vm is not None
    content = shell["content"]
    assurance = "bash scripts/gitflare-host-assurance.sh --target x86_64-unknown-linux-gnu"
    assert assurance in content
    assert "secret-token" not in content
    assert "evidence-secret" not in content
    assert content.index("unset GITFLARE_EVIDENCE_UPLOAD_URL") < content.index(assurance)
    assert "subject_rc=$?" in content
    assert 'exit "$subject_rc"' in content
    assert "GITFLARE_EVIDENCE_UPLOAD_FAILED" in content
    assert vm["env"]["GITFLARE_EVIDENCE_UPLOAD_TOKEN"] == "evidence-secret"
    assert vm["env"]["GITFLARE_EVIDENCE_UPLOAD_URL"].endswith(RUN_ID)
    assert evidence.calls == [("firecrab", SHA)]


def test_assurance_refuses_source_evidence_sha_drift_before_firecrab_api() -> None:
    executor = FakeExecutor()
    builds = GitflareBuildExecutor(
        executor,
        Source(),
        Profile(),
        Evidence(sha="a" * 40),
    )

    with pytest.raises(GitflareBuildError, match="same immutable object"):
        builds.trigger_host_assurance(repo="firecrab", sha=SHA)

    assert executor.calls == []
