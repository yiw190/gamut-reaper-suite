from __future__ import annotations

import os
import sys

if not __package__:  # run by path (the .pkg's bin/hibiki): make the package importable
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SUITE = {"setup", "doctor", "connect", "watchdog", "agents", "recover", "stop", "start", "update", "uninstall", "use", "rules", "run"}  # run = the Windows supervisor
USAGE = """usage: reaper-mcp [serve | http | install-bridge | version | setup | doctor | connect | watchdog | agents | recover | stop | start | update | uninstall | use | rules | ears]

  serve            stdio MCP server (the default when stdin isn't a terminal)
  http             bearer-protected HTTP front on 127.0.0.1:$REAPER_MCP_PORT (default 8787)
  install-bridge   copy the Lua bridge into REAPER's Scripts folder
  version          print the version
  setup ...        macOS / Windows: install / upgrade the whole suite for this user (see `setup -h`)
  use dev|release  run the whole install from a git checkout, or back from the installed package
  rules            the rules every agent gets (your defaults.md + rules.md); `rules edit`, `rules reset <id>`
  ears <wav>       measure a short excerpt (spectrum, tilt, stereo, level) into <wav>.png / .md for an agent
"""


def main(argv: list | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else ""
    mode = os.path.join(os.path.expanduser("~"), ".config", "reaper-mcp", "mode.json")
    if os.environ.get("HIBIKI_USE_RELEASE") or os.path.exists(mode):
        from reaper_mcp.suite import forward
        rc = forward(argv)
        if rc is not None:
            return rc
    if cmd in SUITE:
        from reaper_mcp.suite import main as suite
        return suite(argv)
    if cmd in ("install-bridge", "--install-bridge"):
        from reaper_mcp.install_bridge import main as install
        install()
        return 0
    if cmd == "ears":
        from reaper_mcp.ears import main as ears
        return ears(argv[1:])
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
