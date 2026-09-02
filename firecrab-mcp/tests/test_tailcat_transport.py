from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

from firecrab_mcp.tailcat_transport import (
    TailcatConfig,
    TailcatTransportError,
    _check_tailcat_version,
    _publish_address,
    _pump,
    _start_tailcat,
    _supported_tailcat_version,
    _tailcat_command,
)


def test_tailcat_command_forces_ephemeral_full_address() -> None:
    command = _tailcat_command(TailcatConfig(binary="/usr/local/bin/tailcat"))

    assert command == [
        "/usr/local/bin/tailcat",
        "--key=new",
        "--full-address",
    ]


def test_tailcat_command_can_pin_client_identity() -> None:
    command = _tailcat_command(
        TailcatConfig(allowed_client="nodekey:0123456789abcdef")
    )

    assert command[-1] == "--allow=nodekey:0123456789abcdef"


@pytest.mark.parametrize("version", ["v0.2.0", "0.2.7", "v0.2.1-rc1"])
def test_tailcat_v02_cli_contract_is_supported(version: str) -> None:
    assert _supported_tailcat_version(version)


@pytest.mark.parametrize("version", ["v0.1.0", "v0.3.0", "devel", ""])
def test_other_tailcat_cli_contracts_are_rejected(version: str) -> None:
    assert not _supported_tailcat_version(version)


def test_version_check_is_explicitly_pinned_to_v02() -> None:
    def run_fn(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(args[0], 0, "v0.3.0\n", "")

    with pytest.raises(TailcatTransportError, match=r"pins the v0\.2\.x CLI contract"):
        _check_tailcat_version("tailcat", run_fn=run_fn)


def test_address_file_is_created_private_and_not_overwritten(tmp_path: Path) -> None:
    destination = tmp_path / "capability"
    _publish_address("tc-secret-capability", destination)

    assert destination.read_text(encoding="utf-8") == "tc-secret-capability\n"
    if os.name != "nt":
        assert stat.S_IMODE(destination.stat().st_mode) == 0o600

    with pytest.raises(TailcatTransportError, match="refusing to overwrite"):
        _publish_address("tc-other", destination)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "not-a-tailcat-token",
        "tc token with spaces",
        "tc-token\nsecond-line",
    ],
)
def test_invalid_tailcat_tokens_are_never_published(
    tmp_path: Path,
    token: str,
) -> None:
    with pytest.raises(TailcatTransportError, match="invalid connection token"):
        _publish_address(token, tmp_path / "capability")


def test_tailcat_child_receives_secret_file_out_of_band(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class FakeProcess:
        pass

    fake_process = FakeProcess()

    def popen_fn(command: list[str], **kwargs: Any) -> Any:
        captured["command"] = command
        captured.update(kwargs)
        return fake_process

    internal = tmp_path / "internal-address"
    result = _start_tailcat(
        TailcatConfig(binary="tailcat"),
        internal,
        popen_fn=popen_fn,
    )

    assert result is fake_process
    assert captured["command"] == ["tailcat", "--key=new", "--full-address"]
    assert captured["env"]["TAILCAT_ADDR_FILE"] == str(internal)
    assert captured["stdin"] is subprocess.PIPE
    assert captured["stdout"] is subprocess.PIPE
    assert captured["stderr"] is subprocess.DEVNULL
    assert "shell" not in captured


def test_tailcat_address_never_enters_child_command(tmp_path: Path) -> None:
    captured: dict[str, Any] = {}

    class FakeProcess:
        pass

    def popen_fn(command: list[str], **kwargs: Any) -> Any:
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return FakeProcess()

    internal = tmp_path / "tc-address"
    _start_tailcat(TailcatConfig(), internal, popen_fn=popen_fn)

    assert str(internal) not in captured["command"]
    assert captured["env"]["TAILCAT_ADDR_FILE"] == str(internal)


def test_stdio_pump_flushes_each_chunk() -> None:
    class Source:
        def __init__(self) -> None:
            self.chunks = iter([b'{"jsonrpc":"2.0"}\n', b'{"id":1}\n', b""])

        def read1(self, size: int) -> bytes:
            return next(self.chunks)

    class Destination:
        def __init__(self) -> None:
            self.data = bytearray()
            self.flushes = 0
            self.closed = False

        def write(self, chunk: bytes) -> int:
            self.data.extend(chunk)
            return len(chunk)

        def flush(self) -> None:
            self.flushes += 1

        def close(self) -> None:
            self.closed = True

    source = Source()
    destination = Destination()
    _pump(source, destination)  # type: ignore[arg-type]

    assert bytes(destination.data) == b'{"jsonrpc":"2.0"}\n{"id":1}\n'
    assert destination.flushes == 2
    assert destination.closed is True
