from __future__ import annotations

from typing import Any, Callable

from firecrab_mcp.gitflare_tools import register_gitflare_tools


class FakeMcp:
    def __init__(self) -> None:
        self.tools: dict[str, tuple[Callable[..., Any], dict[str, Any]]] = {}

    def tool(self, **metadata: Any):
        def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[fn.__name__] = (fn, metadata)
            return fn

        return decorate


def test_registration_is_lazy_and_publishes_explicit_gitflare_tools() -> None:
    mcp = FakeMcp()
    executor = object()

    # Registration itself must not require GITFLARE_ADMIN_TOKEN or a runner
    # profile; those are resolved only when a mutating tool is invoked.
    register_gitflare_tools(mcp, executor)

    assert set(mcp.tools) == {"triggerGitflareBuild", "triggerAssuranceBuild"}
    for _, metadata in mcp.tools.values():
        annotations = metadata["annotations"]
        assert annotations.read_only_hint is False
        assert annotations.destructive_hint is False
        assert metadata["structured_output"] is True
