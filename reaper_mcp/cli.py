from __future__ import annotations

import sys

SUITE = {"setup", "doctor", "connect", "watchdog", "agents", "uninstall"}
USAGE = """usage: reaper-mcp [serve | http | install-bridge | version | setup | doctor | connect | watchdog | agents | uninstall]

  serve            stdio MCP server (the default when stdin isn't a terminal)
  http             bearer-protected HTTP front on 127.0.0.1:$REAPER_MCP_PORT (default 8787)
  install-bridge   copy the Lua bridge into REAPER's Scripts folder
  version          print the version
  setup ...        macOS: install / upgrade the whole suite for this user (see `setup -h`)
"""


def main(argv: list | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    if cmd in SUITE:
        from reaper_mcp.suite import main as suite
        return suite(argv)
    if cmd in ("install-bridge", "--install-bridge"):
        from reaper_mcp.install_bridge import main as install
        install()
        return 0
    if cmd in ("version", "--version"):
        from reaper_mcp import __version__
        print(__version__)
        return 0
    if cmd == "http":
        from reaper_mcp.http_server import main as http
        http()
        return 0
    if cmd == "serve" or (not cmd and not sys.stdin.isatty()):
        from reaper_mcp.server import main as serve
        serve()
        return 0
    print(USAGE, file=sys.stderr if cmd else sys.stdout)
    return 2 if cmd else 0


if __name__ == "__main__":
    sys.exit(main())
