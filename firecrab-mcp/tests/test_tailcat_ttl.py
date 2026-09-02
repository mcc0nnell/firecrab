from __future__ import annotations

import pytest

from firecrab_mcp.tailcat_transport import (
    TailcatConfig,
    TailcatTransportError,
    run,
)


def test_tailcat_capability_has_ten_minute_default_ttl() -> None:
    assert TailcatConfig().session_ttl_seconds == 600


@pytest.mark.parametrize("ttl", [0, 59.999, 3600.001, 7200])
def test_tailcat_ttl_fails_closed_before_starting_tailcat(ttl: float) -> None:
    with pytest.raises(
        TailcatTransportError,
        match="session TTL must be between 60 and 3600 seconds",
    ):
        run(TailcatConfig(session_ttl_seconds=ttl))
