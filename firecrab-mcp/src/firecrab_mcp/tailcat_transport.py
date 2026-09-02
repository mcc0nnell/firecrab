"""Ephemeral Tailcat transport for FireCrab MCP stdio.

This launcher keeps FireCrab MCP on its existing stdio transport and uses
Tailcat only as a bidirectional encrypted byte pipe. One launcher invocation
creates one fresh Tailcat server identity and accepts one Tailcat session.

The Tailcat connection token is a bearer capability. It is never passed to the
MCP child, embedded in command arguments, or written to ordinary logs by this
launcher. For automation, use --address-file; the token is written mode 0600.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable

_SUPPORTED_TAILCAT_VERSION = re.compile(r"^v?0\.2\.\d+(?:[-+].*)?$")
_COPY_CHUNK = 64 * 1024


class TailcatTransportError(RuntimeError):
    """Raised when the Tailcat-backed MCP transport cannot start safely."""


@dataclass(frozen=True, slots=True)
class TailcatConfig:
    binary: str = "tailcat"
    address_file: Path | None = None
    allowed_client: str | None = None
    startup_timeout_seconds: float = 15.0


def _tailcat_command(config: TailcatConfig) -> list[str]:
    command = [
        config.binary,
        "--key=new",
        "--full-address",
    ]
    if config.allowed_client:
        command.append(f"--allow={config.allowed_client}")
    return command


def _mcp_command() -> list[str]:
    return [
        sys.executable,
        "-m",
        "firecrab_mcp.server",
        "--transport",
        "stdio",
    ]


def _supported_tailcat_version(value: str) -> bool:
    return bool(_SUPPORTED_TAILCAT_VERSION.fullmatch(value.strip()))


def _check_tailcat_version(
    binary: str,
    *,
    run_fn: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    try:
        result = run_fn(
            [binary, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except FileNotFoundError as error:
        raise TailcatTransportError(
            f"tailcat binary not found: {binary!r}; install Tailcat v0.2.x "
            "or set FIRECRAB_MCP_TAILCAT_BIN"
        ) from error
    except (OSError, subprocess.SubprocessError) as error:
        raise TailcatTransportError(f"failed to inspect tailcat version: {error}") from error

    version = result.stdout.strip()
    if result.returncode != 0 or not _supported_tailcat_version(version):
        raise TailcatTransportError(
            "FireCrab Tailcat transport currently pins the v0.2.x CLI contract; "
            f"{binary!r} reported {version or 'no version'}"
        )
    return version


def _tailcat_env(address_file: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["TAILCAT_ADDR_FILE"] = str(address_file)
    return env


def _publish_address(token: str, destination: Path | None) -> None:
    token = token.strip()
    if not token.startswith("tc") or any(ch.isspace() for ch in token):
        raise TailcatTransportError("tailcat returned an invalid connection token")

    if destination is None:
        if not sys.stderr.isatty():
            raise TailcatTransportError(
                "non-interactive Tailcat launch requires --address-file or "
                "FIRECRAB_MCP_TAILCAT_ADDRESS_FILE"
            )
        print(f"# FireCrab Tailcat MCP capability: {token}", file=sys.stderr, flush=True)
        return

    parent = destination.parent
    if not parent.exists() or not parent.is_dir():
        raise TailcatTransportError(
            f"Tailcat address-file parent directory does not exist: {parent}"
        )

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    try:
        fd = os.open(destination, flags, 0o600)
    except FileExistsError as error:
        raise TailcatTransportError(
            f"refusing to overwrite existing Tailcat address file: {destination}"
        ) from error
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
    except Exception:
        try:
            destination.unlink()
        except OSError:
            pass
        raise


def _wait_for_address(
    path: Path,
    process: subprocess.Popen[bytes],
    timeout_seconds: float,
) -> str:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if path.exists():
            token = path.read_text(encoding="utf-8").strip()
            if token:
                return token
        code = process.poll()
        if code is not None:
            raise TailcatTransportError(
                f"tailcat exited before publishing a capability (exit {code})"
            )
        time.sleep(0.05)

    raise TailcatTransportError(
        f"tailcat did not publish a capability within {timeout_seconds:g} seconds"
    )


def _pump(source: BinaryIO, destination: BinaryIO) -> None:
    try:
        read = getattr(source, "read1", None)
        if read is None:
            read = source.read
        while chunk := read(_COPY_CHUNK):
            destination.write(chunk)
            destination.flush()
    except (BrokenPipeError, OSError, ValueError):
        pass
    finally:
        try:
            destination.close()
        except (OSError, ValueError):
            pass


def _terminate(process: subprocess.Popen[bytes], timeout_seconds: float = 2.0) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout_seconds)


def _start_tailcat(
    config: TailcatConfig,
    internal_address_file: Path,
    *,
    popen_fn: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> subprocess.Popen[bytes]:
    try:
        return popen_fn(
            _tailcat_command(config),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=_tailcat_env(internal_address_file),
        )
    except FileNotFoundError as error:
        raise TailcatTransportError(
            f"tailcat binary not found: {config.binary!r}"
        ) from error
    except OSError as error:
        raise TailcatTransportError(f"failed to start tailcat: {error}") from error


def _start_mcp(
    *,
    popen_fn: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> subprocess.Popen[bytes]:
    try:
        return popen_fn(
            _mcp_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=None,
        )
    except OSError as error:
        raise TailcatTransportError(f"failed to start FireCrab MCP stdio child: {error}") from error


def run(config: TailcatConfig) -> int:
    """Run one Tailcat capability-backed FireCrab MCP stdio session."""
    if config.startup_timeout_seconds <= 0:
        raise TailcatTransportError("Tailcat startup timeout must be greater than zero")

    _check_tailcat_version(config.binary)

    if config.address_file is not None and config.address_file.exists():
        raise TailcatTransportError(
            f"refusing to overwrite existing Tailcat address file: {config.address_file}"
        )

    with tempfile.TemporaryDirectory(prefix="firecrab-tailcat-") as temp_dir:
        internal_address = Path(temp_dir) / "address"
        tailcat = _start_tailcat(config, internal_address)
        mcp: subprocess.Popen[bytes] | None = None
        try:
            token = _wait_for_address(
                internal_address,
                tailcat,
                config.startup_timeout_seconds,
            )
            _publish_address(token, config.address_file)
            try:
                internal_address.unlink()
            except OSError:
                pass

            mcp = _start_mcp()
            if tailcat.stdin is None or tailcat.stdout is None:
                raise TailcatTransportError("tailcat stdio pipes were not created")
            if mcp.stdin is None or mcp.stdout is None:
                raise TailcatTransportError("FireCrab MCP stdio pipes were not created")

            pumps = [
                threading.Thread(
                    target=_pump,
                    args=(tailcat.stdout, mcp.stdin),
                    name="tailcat-to-mcp",
                    daemon=True,
                ),
                threading.Thread(
                    target=_pump,
                    args=(mcp.stdout, tailcat.stdin),
                    name="mcp-to-tailcat",
                    daemon=True,
                ),
            ]
            for thread in pumps:
                thread.start()

            while True:
                tailcat_code = tailcat.poll()
                mcp_code = mcp.poll()

                if tailcat_code is not None:
                    # Tailcat pipe mode is intentionally one-session. A clean
                    # peer disconnect therefore completes this capability.
                    if mcp_code is None:
                        try:
                            mcp.wait(timeout=2.0)
                        except subprocess.TimeoutExpired:
                            _terminate(mcp)
                    return 0 if tailcat_code == 0 else tailcat_code

                if mcp_code is not None:
                    _terminate(tailcat)
                    return mcp_code

                time.sleep(0.05)
        except KeyboardInterrupt:
            return 130
        finally:
            if mcp is not None:
                _terminate(mcp)
            _terminate(tailcat)
            if config.address_file is not None:
                try:
                    config.address_file.unlink()
                except FileNotFoundError:
                    pass


def _optional_env(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Expose one FireCrab MCP stdio session as an ephemeral Tailcat capability"
        )
    )
    parser.add_argument(
        "--tailcat-bin",
        default=os.getenv("FIRECRAB_MCP_TAILCAT_BIN", "tailcat"),
        help="Tailcat v0.2.x executable (default: tailcat)",
    )
    parser.add_argument(
        "--address-file",
        type=Path,
        default=(
            Path(value)
            if (value := _optional_env("FIRECRAB_MCP_TAILCAT_ADDRESS_FILE"))
            else None
        ),
        help=(
            "write the bearer connection token to a new mode-0600 file; "
            "required for non-interactive launches"
        ),
    )
    parser.add_argument(
        "--allow-client",
        default=_optional_env("FIRECRAB_MCP_TAILCAT_ALLOW_CLIENT"),
        help=(
            "optional Tailcat client node public key; when set, possession of "
            "the token alone is insufficient"
        ),
    )
    parser.add_argument(
        "--startup-timeout",
        type=float,
        default=float(os.getenv("FIRECRAB_MCP_TAILCAT_STARTUP_TIMEOUT", "15")),
        help="seconds to wait for Tailcat to publish the capability",
    )
    return parser


def main() -> None:
    args = _build_parser().parse_args()
    config = TailcatConfig(
        binary=args.tailcat_bin,
        address_file=args.address_file,
        allowed_client=args.allow_client,
        startup_timeout_seconds=args.startup_timeout,
    )
    try:
        raise SystemExit(run(config))
    except TailcatTransportError as error:
        raise SystemExit(f"firecrab-mcp-tailcat: {error}") from error


if __name__ == "__main__":
    main()
