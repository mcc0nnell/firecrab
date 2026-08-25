"""Installed FireCrab MCP composition root.

The base server remains independently testable. The installed command attaches
optional Gitflare immutable-source tools once, then delegates to the same server
transport and policy implementation.
"""
from __future__ import annotations

from .gitflare_tools import register_gitflare_tools
from .server import _executor, main as _server_main, mcp

register_gitflare_tools(mcp, _executor)


def main() -> None:
    _server_main()


if __name__ == "__main__":
    main()
