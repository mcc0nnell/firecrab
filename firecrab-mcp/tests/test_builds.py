from __future__ import annotations

from typing import Any

import pytest

from firecrab_mcp.builds import (
    FireCrabBuildError,
    FireCrabBuildExecutor,
    FireCrabRunnerProfile,
    parse_build_console,
)

NETWORK_ID = "11111111-1111-4111-8111-111111111111"
SHELL_ID = "22222222-2222-4222-8222-222222222222"
BUILD_ID = "33333333-3333-4333-8333-333333333333"


class FakeExecutor:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []

    def execute(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, path, body))
        if not self.responses:
            raise AssertionError(f"unexpected call: {method} {path}")
        return self.responses.pop(0)


def profile(**overrides: Any) -> FireCrabRunnerProfile:
    values: dict[str, Any] = {
        "template": "ubuntu-26.04",
        "micro_network_id": NETWORK_ID,
        "cpu": 4,
        "ram": 4096,
        "disk_gb": 30,
        "egress_policy": "isolated",
        "storage_root": "fast",
    }
    values.update(overrides)
    return FireCrabRunnerProfile(**values)


def envelope(data: dict[str, Any], request_id: str) -> dict[str, Any]:
    return {
        "statusCode": 200,
        "requestId": request_id,
        "data": data,
    }


def test_profile_requires_operator_owned_template_and_network() -> None:
    executor = FakeExecutor([])
    builds = FireCrabBuildExecutor(
        executor,
        profile=FireCrabRunnerProfile(template=None, micro_network_id=None),
    )

    with pytest.raises(FireCrabBuildError, match="FIRECRAB_MCP_BUILD_TEMPLATE"):
        builds.trigger_build("unit", "npm test")

    assert executor.calls == []


def test_trigger_build_creates_shell_vm_and_start_in_order() -> None:
    executor = FakeExecutor(
        [
            envelope({"shellId": SHELL_ID}, "req-shell"),
            envelope({"id": BUILD_ID, "state": "created"}, "req-create"),
            envelope({"id": BUILD_ID, "state": "starting"}, "req-start"),
        ]
    )
    builds = FireCrabBuildExecutor(executor, profile=profile())

    result = builds.trigger_build("Unit / Linux", "npm ci\nnpm test")

    assert result["buildId"] == BUILD_ID
    assert result["phase"] == "starting"
    assert result["requestIds"] == {
        "shell": "req-shell",
        "create": "req-create",
        "start": "req-start",
    }

    assert len(executor.calls) == 3
    shell_call, vm_call, start_call = executor.calls
    assert shell_call[0:2] == ("POST", "/api/shells")
    assert shell_call[2] is not None
    assert shell_call[2]["name"].startswith("ci-unit-linux-")
    assert shell_call[2]["content"] == (
        "#!/bin/sh\nset -e\nexport CI=true\nnpm ci\nnpm test\n"
    )

    assert vm_call[0:2] == ("POST", "/api/vms")
    assert vm_call[2] == {
        "name": shell_call[2]["name"],
        "template": "ubuntu-26.04",
        "ram": 4096,
        "cpu": 4,
        "diskGb": 30,
        "egressPolicy": "isolated",
        "microNetworkId": NETWORK_ID,
        "shellIds": [SHELL_ID],
        "portForwards": [],
        "env": {},
        "storageRoot": "fast",
    }
    assert start_call == ("POST", f"/api/vms/{BUILD_ID}/start", None)


def test_parse_build_console_success_uses_runner_terminal_markers() -> None:
    parsed = parse_build_console(
        "boot noise\n"
        "FIRECRAB_SHELL_START 00.sh interp=/bin/sh\n"
        "hello\n"
        "FIRECRAB_SHELL_FAILED 00.sh 99\n"
        "still command output\n"
        "FIRECRAB_SHELL_OK 00.sh\n"
        "FIRECRAB_SHELL_DONE ok\n"
    )

    assert parsed.complete is True
    assert parsed.exit_code == 0
    assert parsed.command_output == (
        "hello\nFIRECRAB_SHELL_FAILED 00.sh 99\nstill command output"
    )


def test_parse_build_console_failure_preserves_exit_code() -> None:
    parsed = parse_build_console(
        "FIRECRAB_SHELL_START 00.sh interp=/bin/sh\n"
        "tests failed\n"
        "FIRECRAB_SHELL_FAILED 00.sh 7\n"
        "FIRECRAB_SHELL_DONE failed\n"
    )

    assert parsed.complete is True
    assert parsed.exit_code == 7
    assert parsed.command_output == "tests failed"


def test_parse_build_console_partial_is_not_complete() -> None:
    parsed = parse_build_console(
        "boot\nFIRECRAB_SHELL_START 00.sh interp=/bin/sh\nstill running\n"
    )

    assert parsed.complete is False
    assert parsed.exit_code is None
    assert parsed.command_output == "still running"


def test_get_build_maps_firecrab_console_to_completed_build() -> None:
    executor = FakeExecutor(
        [
            envelope({"id": BUILD_ID, "name": "ci-unit-abcdef", "state": "running"}, "req-vm"),
            envelope(
                {
                    "consoleLog": (
                        "FIRECRAB_SHELL_START 00.sh interp=/bin/sh\n"
                        "ok\n"
                        "FIRECRAB_SHELL_OK 00.sh\n"
                        "FIRECRAB_SHELL_DONE ok\n"
                    ),
                    "truncated": False,
                },
                "req-log",
            ),
        ]
    )
    builds = FireCrabBuildExecutor(executor, profile=profile())

    result = builds.get_build(BUILD_ID)

    assert result["phase"] == "completed"
    assert result["conclusion"] == "success"
    assert result["exitCode"] == 0
    assert result["complete"] is True
    assert result["requestIds"] == {"vm": "req-vm", "log": "req-log"}


def test_get_build_log_returns_command_output_only() -> None:
    executor = FakeExecutor(
        [
            envelope({"id": BUILD_ID, "name": "ci-unit-abcdef", "state": "running"}, "req-vm"),
            envelope(
                {
                    "consoleLog": (
                        "kernel boot\n"
                        "FIRECRAB_SHELL_START 00.sh interp=/bin/sh\n"
                        "line one\nline two\n"
                        "FIRECRAB_SHELL_FAILED 00.sh 2\n"
                        "FIRECRAB_SHELL_DONE failed\n"
                    ),
                    "truncated": True,
                },
                "req-log",
            ),
        ]
    )
    builds = FireCrabBuildExecutor(executor, profile=profile())

    result = builds.get_build_log(BUILD_ID)

    assert result["console"] == "line one\nline two"
    assert result["exitCode"] == 2
    assert result["consoleTruncated"] is True


def test_stop_build_refuses_unowned_vm_before_mutating() -> None:
    executor = FakeExecutor(
        [envelope({"id": BUILD_ID, "name": "database", "state": "running"}, "req-vm")]
    )
    builds = FireCrabBuildExecutor(executor, profile=profile())

    with pytest.raises(FireCrabBuildError, match="not an MCP build VM"):
        builds.stop_build(BUILD_ID)

    assert executor.calls == [("GET", f"/api/vms/{BUILD_ID}", None)]


@pytest.mark.parametrize(
    "command",
    ["", "echo ok\x00oops", "x" * (28 * 1024 + 1)],
)
def test_trigger_build_rejects_invalid_command_before_http(command: str) -> None:
    executor = FakeExecutor([])
    builds = FireCrabBuildExecutor(executor, profile=profile())

    with pytest.raises(FireCrabBuildError):
        builds.trigger_build("unit", command)

    assert executor.calls == []
