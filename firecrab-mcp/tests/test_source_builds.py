from __future__ import annotations

import unittest
import uuid

from firecrab_mcp.builds import (
    BuildSource,
    FireCrabBuildError,
    FireCrabBuildExecutor,
    FireCrabRunnerProfile,
    _build_script,
)


SOURCE = {
    "schemaVersion": 1,
    "provider": "cloudflare-artifacts",
    "namespace": "gitflare",
    "repo": "firecrab",
    "sha": "a" * 40,
    "ref": "refs/heads/assurance",
    "remote": "https://0123456789abcdef0123456789abcdef.artifacts.cloudflare.net/git/gitflare/firecrab.git",
    "token": "short-lived-read-token",
}


class FakeExecutor:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []
        self.vm_id = str(uuid.uuid4())

    def execute(self, method: str, path: str, body: dict | None = None) -> dict:
        self.calls.append((method, path, body))
        if path == "/api/shells":
            return {"requestId": "shell-request", "data": {"shellId": str(uuid.uuid4())}}
        if path == "/api/vms":
            return {"requestId": "vm-request", "data": {"id": self.vm_id}}
        if path == f"/api/vms/{self.vm_id}/start":
            return {"requestId": "start-request", "data": {"state": "running"}}
        raise AssertionError(path)


class SourceBoundBuildTests(unittest.TestCase):
    def test_source_contract_is_strict_and_gitflare_scoped(self) -> None:
        source = BuildSource.parse(SOURCE.copy())
        self.assertEqual(source.sha, "a" * 40)
        self.assertNotIn("token", source.evidence())

        bad = SOURCE | {"remote": "https://github.com/example/firecrab.git"}
        with self.assertRaisesRegex(FireCrabBuildError, "Gitflare namespace"):
            BuildSource.parse(bad)

        bad = SOURCE | {"remote": "https://secret@example.artifacts.cloudflare.net/git/gitflare/firecrab.git"}
        with self.assertRaisesRegex(FireCrabBuildError, "Gitflare namespace"):
            BuildSource.parse(bad)

        bad = SOURCE | {"repo": "../firecrab"}
        with self.assertRaisesRegex(FireCrabBuildError, "repo"):
            BuildSource.parse(bad)

    def test_source_bootstrap_verifies_exact_head_before_command(self) -> None:
        script = _build_script("python3 -m unittest", source_bound=True)
        verify = 'test "$(git rev-parse HEAD)" = "$GITFLARE_SOURCE_SHA"'
        self.assertIn(verify, script)
        self.assertIn("unset GITFLARE_SOURCE_TOKEN", script)
        self.assertLess(script.index(verify), script.index("python3 -m unittest"))
        self.assertLess(script.index("unset GITFLARE_SOURCE_TOKEN"), script.index("python3 -m unittest"))
        self.assertNotIn(SOURCE["token"], script)

    def test_lease_token_lives_only_in_vm_environment(self) -> None:
        fake = FakeExecutor()
        builds = FireCrabBuildExecutor(
            fake,
            FireCrabRunnerProfile(
                template="ci-template",
                micro_network_id=str(uuid.uuid4()),
            ),
        )
        result = builds.trigger_build("assurance", "true", SOURCE.copy())

        shell_body = fake.calls[0][2]
        vm_body = fake.calls[1][2]
        assert shell_body is not None and vm_body is not None
        self.assertNotIn(SOURCE["token"], shell_body["content"])
        self.assertEqual(vm_body["env"]["GITFLARE_SOURCE_TOKEN"], SOURCE["token"])
        self.assertEqual(result["source"]["sha"], SOURCE["sha"])
        self.assertNotIn("token", result["source"])


if __name__ == "__main__":
    unittest.main()
