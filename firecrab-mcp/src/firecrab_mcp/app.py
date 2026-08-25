"""Composition root for FireCrab MCP plus Gitflare immutable-source builds."""
from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations

from .builds import FireCrabRunnerProfile
from .gitflare_builds import GitflareBuildExecutor
from .gitflare_source import GitflareSourceAuthority
from .server import _executor, main as _base_main, mcp

_MUTATING_TOOL = ToolAnnotations(read_only_hint=False, destructive_hint=False)
_gitflare_builds = GitflareBuildExecutor(
    _executor,
    GitflareSourceAuthority(),
    FireCrabRunnerProfile.from_env(),
)


@mcp.tool(title="Trigger Gitflare FireCrab build", annotations=_MUTATING_TOOL, structured_output=True)
def triggerGitflareBuild(label: str, repo: str, sha: str, command: str) -> dict[str, Any]:
    """Build one exact Gitflare revision in a fresh FireCrab guest."""
    return _gitflare_builds.trigger_build(label=label, repo=repo, sha=sha, command=command)


@mcp.tool(title="Trigger FireCrab host assurance", annotations=_MUTATING_TOOL, structured_output=True)
def triggerAssuranceBuild(repo: str, sha: str) -> dict[str, Any]:
    """Run the fixed FireCrab v1 host assurance adapter at one exact Gitflare SHA."""
    return _gitflare_builds.trigger_host_assurance(repo=repo, sha=sha)


def main() -> None:
    _base_main()


if __name__ == "__main__":
    main()
