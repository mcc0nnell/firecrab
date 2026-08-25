from __future__ import annotations

import asyncio

from firecrab_mcp.app import mcp


def test_composed_mcp_surface_adds_gitflare_tools_without_startup_credentials() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}

    assert "triggerGitflareBuild" in tools
    assert "triggerAssuranceBuild" in tools

    for name in {"triggerGitflareBuild", "triggerAssuranceBuild"}:
        annotations = tools[name].annotations
        assert annotations is not None
        assert annotations.read_only_hint is False
        assert annotations.destructive_hint is False
        assert tools[name].output_schema is not None

    # Existing Jenkins-shaped lifecycle remains present in the composed server.
    assert {"triggerBuild", "getBuild", "getBuildLog", "stopBuild"} <= set(tools)
