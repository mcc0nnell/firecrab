"""Register immutable Gitflare source-build tools on the canonical FireCrab MCP server."""
from __future__ import annotations

from typing import Any

from mcp.types import ToolAnnotations

from .builds import FireCrabRunnerProfile
from .gitflare_builds import GitflareBuildExecutor
from .gitflare_evidence import GitflareEvidenceAuthority
from .gitflare_source import GitflareSourceAuthority

_MUTATING_TOOL = ToolAnnotations(read_only_hint=False, destructive_hint=False)


def register_gitflare_tools(mcp: Any, executor: Any) -> None:
    """Attach Gitflare tools without resolving optional credentials at startup."""

    def builds() -> GitflareBuildExecutor:
        return GitflareBuildExecutor(
            executor,
            GitflareSourceAuthority(),
            FireCrabRunnerProfile.from_env(),
            GitflareEvidenceAuthority(),
        )

    @mcp.tool(
        title="Trigger Gitflare FireCrab build",
        annotations=_MUTATING_TOOL,
        structured_output=True,
    )
    def triggerGitflareBuild(
        label: str, repo: str, sha: str, command: str
    ) -> dict[str, Any]:
        """Build one exact Gitflare revision in a fresh FireCrab guest."""
        return builds().trigger_build(
            label=label, repo=repo, sha=sha, command=command
        )

    @mcp.tool(
        title="Trigger FireCrab host assurance",
        annotations=_MUTATING_TOOL,
        structured_output=True,
    )
    def triggerAssuranceBuild(repo: str, sha: str) -> dict[str, Any]:
        """Run FireCrab v1 host assurance and upload its evidence to Gitflare R2."""
        return builds().trigger_host_assurance(repo=repo, sha=sha)
