# Remote host (macOS + Cloudflare Tunnel)

Only needed when the agent runs somewhere else (e.g. a cloud container) and has to reach REAPER on a Mac over the
internet. Local MCP clients and the agent gateway talk to REAPER directly over stdio and don't need any of this.

**Gamut desktop on the same Mac doesn't need it either.** Run `python/http_server.py` (bearer auth) and register the
MCP in Gamut as `http://localhost:8787/mcp`: Gamut desktop makes the connection itself and allows loopback MCP URLs, so
it works whether its agent containers run in Lima, Docker or anything else.

`scripts/macos-host-bootstrap.sh` installs cloudflared, clones this repo to `~/reaper-mcp`, and writes LaunchAgents for
the HTTP MCP front (`python/http_server.py`, bearer auth) and the tunnel. Secrets come from `~/.config/reaper-mcp/env`
and never touch the repo.

`scripts/deploy_bridge.py stage | swap | restart | verify` pushes a new bridge into a running REAPER without restarting
it, and restarts the HTTP front so it loads a new `server.py`.
