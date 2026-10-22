# Contributing

Bug reports and pull requests are welcome. For a larger change, open an issue first so we can agree on the approach.

## Run from a clone

```bash
git clone https://github.com/yiw190/hibiki.git ~/hibiki
sh ~/hibiki/install.sh        # macOS; on Windows: py -m reaper_mcp setup
```

The LaunchAgents then run from the clone; re-run `install.sh` after `git pull`.

## Tests

None of these need REAPER. The Lua-based ones need `lupa`.

```
python3 tests/test_server.py                         # MCP server + fake bridge
uv run --with lupa python tests/test_bridge_lua.py   # bridge.lua against a fake ReaScript API
uv run --with lupa python panel/test_panel.py        # chat panel against fake reaper/ImGui
python3 gateway/test_gateway.py                      # agent gateway against fake Gamut + fake ACP agent
python3 tests/test_suite.py                          # setup / doctor / connect / uninstall on a fake HOME
uv run --with lupa python scripts/check_syntax.py    # every .lua compiles under Lua 5.4, every .sh parses
```

CI runs all of them on every push and pull request, on Python 3.9 and 3.12 and on Windows.

## Pull requests

- Keep each one to one change, with a test for it.
- The Python code uses only the standard library and must run on Python 3.9 (macOS's `/usr/bin/python3`).
- Security issues go through [SECURITY.md](SECURITY.md), not a public pull request.
