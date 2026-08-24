"""Jenkins-shaped build operations backed by FireCrab guest shells.

A build is deliberately just a FireCrab VM with exactly one pinned Shell
revision. FireCrab already runs pinned shell scripts after network readiness and
writes deterministic FIRECRAB_SHELL_* markers to the serial console. This
module turns that native lifecycle into build/inspect/log/stop semantics without
teaching MCP callers anything about Firecracker internals.

This first slice retains the VM and shell as build evidence. Retention/garbage
collection is intentionally separate from execution because deletion is not yet
part of the MCP capability surface.
"""

from __future__ import annotations

import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Protocol

_BUILD_PREFIX = "ci-"
_MAX_COMMAND_BYTES = 28 * 1024
_MAX_LABEL_BYTES = 160
_TERMINAL_RE = re.compile(r"^FIRECRAB_SHELL_(OK|FAILED) 00\.sh(?: (\d+))?$", re.MULTILINE)
_DONE_RE = re.compile(r"^FIRECRAB_SHELL_DONE (ok|failed)$", re.MULTILINE)
_START_RE = re.compile(r"^FIRECRAB_SHELL_START 00\.sh[^\n]*$", re.MULTILINE)


class FireCrabBuildError(RuntimeError):
    """Raised when a FireCrab VM cannot safely be treated as an MCP build."""


class Executor(Protocol):
    """Small protocol implemented by the TalkPipe-backed FireCrabExecutor."""

    def execute(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True, slots=True)
class FireCrabRunnerProfile:
    """Operator-owned FireCrab capacity used for MCP builds.

    Infrastructure placement is configuration, not a model/tool argument. This
    mirrors Jenkins agents/labels: a build asks to run, while the controller
    decides what execution capacity that means.
    """

    template: str | None
    micro_network_id: str | None
    cpu: int = 2
    ram: int = 2048
    disk_gb: int = 20
    egress_policy: str = "internet"
    storage_root: str | None = None

    @classmethod
    def from_env(cls) -> "FireCrabRunnerProfile":
        return cls(
            template=_optional_env("FIRECRAB_MCP_BUILD_TEMPLATE"),
            micro_network_id=_optional_env("FIRECRAB_MCP_BUILD_NETWORK_ID"),
            cpu=_positive_env_int("FIRECRAB_MCP_BUILD_CPU", 2),
            ram=_positive_env_int("FIRECRAB_MCP_BUILD_RAM", 2048),
            disk_gb=_positive_env_int("FIRECRAB_MCP_BUILD_DISK_GB", 20),
            egress_policy=(
                _optional_env("FIRECRAB_MCP_BUILD_EGRESS_POLICY") or "internet"
            ),
            storage_root=_optional_env("FIRECRAB_MCP_BUILD_STORAGE_ROOT"),
        )

    def validate(self) -> None:
        missing: list[str] = []
        if not self.template:
            missing.append("FIRECRAB_MCP_BUILD_TEMPLATE")
        if not self.micro_network_id:
            missing.append("FIRECRAB_MCP_BUILD_NETWORK_ID")
        if missing:
            raise FireCrabBuildError(
                "FireCrab build runner is not configured; set " + ", ".join(missing)
            )
        try:
            uuid.UUID(self.micro_network_id)
        except (ValueError, AttributeError) as error:
            raise FireCrabBuildError(
                "FIRECRAB_MCP_BUILD_NETWORK_ID must be a UUID"
            ) from error
        if self.egress_policy not in {"internet", "isolated"}:
            raise FireCrabBuildError(
                "FIRECRAB_MCP_BUILD_EGRESS_POLICY must be internet or isolated"
            )


class FireCrabBuildExecutor:
    """Build lifecycle composed from the existing TalkPipe API executor."""

    def __init__(
        self,
        executor: Executor,
        profile: FireCrabRunnerProfile | None = None,
    ) -> None:
        self.executor = executor
        self.profile = profile or FireCrabRunnerProfile.from_env()

    def trigger_build(self, label: str, command: str) -> dict[str, Any]:
        """Create one shell-backed VM and start it asynchronously."""
        self.profile.validate()
        label = _validate_label(label)
        command = _validate_command(command)
        build_name = _build_name(label)

        shell = self.executor.execute(
            "POST",
            "/api/shells",
            {
                "name": build_name,
                "description": f"FireCrab MCP build shell for {label}",
                "content": _build_script(command),
            },
        )
        shell_data = _mapping_data(shell, "create shell")
        shell_id = _required_string(shell_data, "shellId", "create shell")

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
            "env": {},
        }
        if self.profile.storage_root:
            vm_body["storageRoot"] = self.profile.storage_root

        created = self.executor.execute("POST", "/api/vms", vm_body)
        vm = _mapping_data(created, "create build VM")
        build_id = _required_string(vm, "id", "create build VM")

        started = self.executor.execute("POST", f"/api/vms/{build_id}/start")
        started_data = _mapping_data(started, "start build VM")

        return {
            "buildId": build_id,
            "label": label,
            "runner": "firecrab",
            "vmName": build_name,
            "shellId": shell_id,
            "phase": _phase_for_vm_state(str(started_data.get("state", "starting"))),
            "requestIds": {
                "shell": shell.get("requestId"),
                "create": created.get("requestId"),
                "start": started.get("requestId"),
            },
        }

    def get_build(self, build_id: str) -> dict[str, Any]:
        """Return Jenkins-like lifecycle/conclusion facts for one build."""
        build_id = _id_segment(build_id)
        vm_result, vm = self._get_build_vm(build_id)
        log_result, console, truncated = self._get_console(build_id)
        parsed = parse_build_console(console)
        state = str(vm.get("state", "unknown"))
        phase, conclusion = _build_outcome(state, parsed)
        return {
            "buildId": build_id,
            "runner": "firecrab",
            "vmName": vm.get("name"),
            "vmState": state,
            "phase": phase,
            "conclusion": conclusion,
            "exitCode": parsed.exit_code,
            "complete": parsed.complete or phase in {"completed", "error", "cancelled"},
            "consoleTruncated": truncated,
            "requestIds": {
                "vm": vm_result.get("requestId"),
                "log": log_result.get("requestId"),
            },
        }

    def get_build_log(self, build_id: str) -> dict[str, Any]:
        """Return command console output extracted from FireCrab serial logs."""
        build_id = _id_segment(build_id)
        vm_result, vm = self._get_build_vm(build_id)
        log_result, console, truncated = self._get_console(build_id)
        parsed = parse_build_console(console)
        return {
            "buildId": build_id,
            "vmName": vm.get("name"),
            "complete": parsed.complete,
            "exitCode": parsed.exit_code,
            "console": parsed.command_output,
            "consoleTruncated": truncated,
            "requestIds": {
                "vm": vm_result.get("requestId"),
                "log": log_result.get("requestId"),
            },
        }

    def stop_build(self, build_id: str) -> dict[str, Any]:
        """Stop a build VM after verifying it was created by this build layer."""
        build_id = _id_segment(build_id)
        _, vm = self._get_build_vm(build_id)
        stopped = self.executor.execute("POST", f"/api/vms/{build_id}/stop")
        stopped_data = _mapping_data(stopped, "stop build VM")
        return {
            "buildId": build_id,
            "vmName": vm.get("name"),
            "phase": _phase_for_vm_state(str(stopped_data.get("state", "stopping"))),
            "requestId": stopped.get("requestId"),
        }

    def _get_build_vm(self, build_id: str) -> tuple[dict[str, Any], dict[str, Any]]:
        result = self.executor.execute("GET", f"/api/vms/{build_id}")
        vm = _mapping_data(result, "get build VM")
        name = vm.get("name")
        if not isinstance(name, str) or not name.startswith(_BUILD_PREFIX):
            raise FireCrabBuildError(
                f"VM {build_id} is not an MCP build VM; refusing build operation"
            )
        return result, vm

    def _get_console(self, build_id: str) -> tuple[dict[str, Any], str, bool]:
        result = self.executor.execute("GET", f"/api/vms/{build_id}/log")
        data = _mapping_data(result, "get build log")
        console = data.get("consoleLog", "")
        if not isinstance(console, str):
            raise FireCrabBuildError("FireCrab build log consoleLog must be a string")
        return result, console, bool(data.get("truncated", False))


@dataclass(frozen=True, slots=True)
class ParsedBuildConsole:
    complete: bool
    exit_code: int | None
    command_output: str


def parse_build_console(console: str) -> ParsedBuildConsole:
    """Parse the final FireCrab shell protocol markers from a serial log.

    The shell runner emits its terminal marker after the user script has
    finished, then emits FIRECRAB_SHELL_DONE. We deliberately use the final
    occurrences so normal command output that happens to contain marker-like
    text cannot win over the runner's own final protocol line.
    """
    terminals = list(_TERMINAL_RE.finditer(console))
    dones = list(_DONE_RE.finditer(console))
    if not terminals or not dones:
        return ParsedBuildConsole(False, None, _partial_command_output(console))

    terminal = terminals[-1]
    done = next((item for item in reversed(dones) if item.start() > terminal.end()), None)
    if done is None:
        return ParsedBuildConsole(False, None, _partial_command_output(console))

    status = terminal.group(1)
    exit_code = 0 if status == "OK" else int(terminal.group(2) or "1")
    starts = [item for item in _START_RE.finditer(console) if item.end() <= terminal.start()]
    output_start = starts[-1].end() if starts else 0
    output = console[output_start : terminal.start()].strip("\r\n")
    return ParsedBuildConsole(True, exit_code, output)


def _partial_command_output(console: str) -> str:
    starts = list(_START_RE.finditer(console))
    if not starts:
        return ""
    return console[starts[-1].end() :].strip("\r\n")


def _build_outcome(state: str, parsed: ParsedBuildConsole) -> tuple[str, str | None]:
    if parsed.complete:
        return "completed", "success" if parsed.exit_code == 0 else "failure"
    if state == "error":
        return "error", "infrastructure_failure"
    if state == "stopped":
        return "cancelled", "cancelled"
    return _phase_for_vm_state(state), None


def _phase_for_vm_state(state: str) -> str:
    return {
        "created": "queued",
        "starting": "starting",
        "running": "running",
        "stopping": "stopping",
        "stopped": "cancelled",
        "error": "error",
    }.get(state, "unknown")


def _build_script(command: str) -> str:
    return "#!/bin/sh\nset -e\nexport CI=true\n" + command.rstrip() + "\n"


def _build_name(label: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-._").lower() or "build"
    # FireCrab VM and shell names are capped at 64 safe characters.
    suffix = uuid.uuid4().hex[:10]
    room = 64 - len(_BUILD_PREFIX) - len(suffix) - 1
    return f"{_BUILD_PREFIX}{slug[:room]}-{suffix}"


def _validate_label(value: str) -> str:
    value = value.strip()
    if not value:
        raise FireCrabBuildError("build label must not be empty")
    if len(value.encode("utf-8")) > _MAX_LABEL_BYTES:
        raise FireCrabBuildError(f"build label must be at most {_MAX_LABEL_BYTES} bytes")
    return value


def _validate_command(value: str) -> str:
    if not value.strip():
        raise FireCrabBuildError("build command must not be empty")
    if "\x00" in value:
        raise FireCrabBuildError("build command must not contain NUL bytes")
    size = len(value.encode("utf-8"))
    if size > _MAX_COMMAND_BYTES:
        raise FireCrabBuildError(
            f"build command must be at most {_MAX_COMMAND_BYTES} UTF-8 bytes"
        )
    return value


def _id_segment(value: str) -> str:
    value = value.strip()
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as error:
        raise FireCrabBuildError("buildId must be a FireCrab UUID") from error


def _mapping_data(result: dict[str, Any], operation: str) -> dict[str, Any]:
    data = result.get("data")
    if not isinstance(data, dict):
        raise FireCrabBuildError(f"{operation} returned an unexpected FireCrab payload")
    return data


def _required_string(data: dict[str, Any], key: str, operation: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise FireCrabBuildError(f"{operation} response is missing {key}")
    return value


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _positive_env_int(name: str, default: int) -> int:
    raw = _optional_env(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise FireCrabBuildError(f"{name} must be an integer") from error
    if value <= 0:
        raise FireCrabBuildError(f"{name} must be greater than zero")
    return value
