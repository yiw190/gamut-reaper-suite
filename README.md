# Hibiki

<img src="icons/reaper-mcp.svg" alt="" width="96" height="96" align="left" hspace="12" vspace="4">

**Hibiki (響, "resonance")** puts AI agents inside [REAPER](https://reaper.fm): an MCP server that drives REAPER, the
Hibiki panel docked in REAPER for chatting with an agent about what you have selected, and a local gateway so the
panel can talk to [Gamut](https://github.com/SkillfulAgents/SuperAgent) agents, Codex, Claude Code, Kimi or any other
ACP agent.

| Path | What |
| --- | --- |
| `reaper_mcp/` | MCP server (`server.py` stdio, `http_server.py` bearer HTTP) + `bridge.lua`, the Lua bridge that runs inside REAPER |
| `panel/` | Hibiki — chat panel docked in REAPER (ReaImGui) for Gamut, Codex and other ACP agents |
| `reaper/__startup.lua` | Starts the bridge and the chat panel whenever REAPER launches |
| `gateway/` | Agent gateway: one local API for the panel in front of Gamut and ACP agents (Codex, …) |
| `scripts/` | Remote-host bootstrap (Cloudflare Tunnel) and hot-swap deploy of the bridge — see `docs/remote-host.md` |

[![ci](https://github.com/yiw190/hibiki/actions/workflows/ci.yml/badge.svg)](https://github.com/yiw190/hibiki/actions/workflows/ci.yml)

```
MCP client  --stdio JSON-RPC-->  reaper_mcp/server.py  --JSON mailbox-->  reaper_mcp/bridge.lua (inside REAPER)
```

Fourteen grouped tools. Multi-step edits go through `batch`; anything else through `reaper_call` / `run_lua`. Track index `-1` is the master. Time is project quarter notes. Call `status` first; on a new project pass `detail=true` for arrangement facts (time signature, length in beats, markers, MIDI vs audio) — not chords. `midi cc` is CC and program change; `item import` takes a path on the REAPER host; `project save` writes without a dialog (pass `path` if the project is unsaved). `midi get` omits default fields; `batch` slots are the inner result or `{error}`.

## Install (macOS)

You need REAPER. Download `Hibiki-<version>.pkg` from
[Releases](https://github.com/yiw190/hibiki/releases), open it, then restart REAPER. The package brings its
own Python, so it doesn't need the Xcode command line tools. To upgrade, install a newer package.

The package isn't signed yet. If macOS says it can't verify the developer, open **System Settings → Privacy & Security**
and click **Open Anyway**.

What it sets up for the logged-in user (`hibiki setup` runs the same steps; safe to re-run):

| Piece | Where |
| --- | --- |
| Lua bridge, Hibiki panel | `<REAPER resource path>/Scripts/reaper_mcp_bridge.lua`, `Scripts/Hibiki/` |
| ReaImGui (if missing, pinned + sha256-checked) | `<REAPER resource path>/UserPlugins/` |
| Startup hook: starts both with REAPER | a marked block in `Scripts/__startup.lua`; your own lines stay |
| MCP over HTTP on `127.0.0.1:8787` (bearer in `~/.config/reaper-mcp/token`, 0600) | LaunchAgent `com.hibiki.reaper-mcp` |
| Agent gateway on `127.0.0.1:47892` | LaunchAgent `com.hibiki.agent-gateway` |
| MCP registration | Gamut (as **REAPER**), plus Claude Desktop, Claude Code and Cursor when they're installed |

```bash
hibiki doctor                         # what's installed, what's running, what's wrong
hibiki connect gamut|claude-desktop|claude-code|cursor [--remove]
hibiki agents add codex claude kimi   # ACP agents for the panel (Node goes in ~/.local/node)
hibiki watchdog on|off                # relaunch REAPER after crashes, for an unattended Mac
hibiki uninstall [--purge]            # --purge also deletes tokens and gateway config
```

From a clone instead (the LaunchAgents then run from the clone, so keep it; re-run after `git pull`):

```bash
git clone https://github.com/yiw190/hibiki.git ~/hibiki
sh ~/hibiki/install.sh                # WITH_AGENTS=1 also adds Codex, Claude Code and Kimi
```

## Manual setup (any MCP client, any OS)

```bash
python3 -m reaper_mcp install-bridge
```

Then in REAPER: **Actions → Load ReaScript → `reaper_mcp_bridge.lua` → Run**. Console: `reaper-mcp bridge 0.4.0 ready`.

MCP client config (any client that runs stdio servers):

```json
{
  "mcpServers": {
    "reaper": {
      "command": "python3",
      "args": ["/absolute/path/to/hibiki/reaper_mcp/server.py"]
    }
  }
}
```

On Windows use native Python, not WSL (they don't share the mailbox).

```
REAPER_MCP_IPC_DIR     mailbox override (must match on both sides)
REAPER_MCP_TIMEOUT     default 10s
REAPER_MCP_DEBUG=1     Lua request log
```

Mailbox: `~/Library/Application Support/reaper-mcp/ipc` on macOS, `%APPDATA%/reaper-mcp/ipc` on Windows,
`$XDG_DATA_HOME/reaper-mcp/ipc` on Linux. Time is **beats**. Indices are 0-based.

## Hibiki panel (macOS)

Needs [ReaImGui](https://github.com/cfillion/reaimgui) ≥ 0.10 and the Gamut desktop app running on the same Mac — the
panel uses the app's local REST + SSE API (`127.0.0.1:47891`), the same one Gamut's own UI uses.

The installer puts it in place. By hand: 1. Copy `panel/Hibiki.lua` to `<REAPER resource path>/Scripts/Hibiki/`, then **Actions → Load ReaScript** and run it.
2. Optional: run `panel/restart_apply.sh` while REAPER is open and quit REAPER — it docks the panel on the right,
   binds **Cmd+L** to toggle it, and relaunches REAPER.

What it does: the header shows the agent and the model in effect (Gamut, Claude Code, Kimi, …); click it for
**Agent settings**, a Routing-style window with the provider, the Gamut agent, model / effort / mode, the context
switch and the live REAPER selections; the clock lists recent chats, `+` starts one. For Claude Code and Kimi,
**Open in …** reopens the chat in the agent's own CLI in Terminal. Replies stream with a markdown renderer; tool calls
collapse into one line per run. Every message carries a `[REAPER context]` block: project, tempo, cursor and nearby
markers, then each selection kind — time selection, razor areas, tracks, items, MIDI notes, envelope, FX — with how
long ago it was set and, for time/razor ranges, the chord markers and what plays there. Pills above the input show
the same selections, newest first. Enter sends, Shift+Enter is a newline. Drag to select message text and Cmd+C to
copy. Attach files with **+ File** or by dropping them on the panel: they're uploaded to the agent (Gamut's
`upload-file`; for ACP agents the gateway stores them and sends images as image blocks) and listed in the message as
`[Attached files:]`. When a Gamut agent asks a question or needs an approval, the panel jumps to that chat in Gamut
(`superagent://agent/<slug>/sessions/<id>`). Scrolling follows Gamut's own follow engine. Chinese/Japanese IME:
ReaImGui passes keys to both the IME and ImGui, so the panel undoes ImGui's half of Backspace / arrows / Esc / Enter
while a composition is open and shows the pending pinyin under the input.

> Gamut's local API has no auth in desktop mode and listens on all interfaces. Block port 47891 in the macOS
> firewall if the Mac is on a network you don't trust.

## Start with REAPER

The installer merges `reaper/__startup.lua` into `<REAPER resource path>/Scripts/__startup.lua` as a marked block. It starts the
MCP bridge and opens the chat panel on every launch, registering either script if REAPER doesn't know it yet.

## Agent gateway: Codex, Claude Code, Kimi, … in the same panel

`gateway/agent_gateway.py` is a small local server (`127.0.0.1:47892`, stdlib Python) that the panel talks to first.
It proxies Gamut agents unchanged and adds [ACP](https://agentclientprotocol.com) agents — Codex via
`@agentclientprotocol/codex-acp`, Claude Code via `@agentclientprotocol/claude-agent-acp`, Kimi Code CLI (`kimi acp`),
and anything else that speaks ACP — under the same API. Subscriptions work: Codex logs in with ChatGPT, Claude Code
reuses the local `claude` login, Kimi's login button opens `kimi login` (device code) in Terminal. An agent's
`loginVariants` turn its terminal login into one button per variant, each appending its `args` to the login
command (the Kimi entry ships kimi.ai = `--region global` and kimi.com = `--region mainland-cn`). Each ACP session gets this
repo's MCP server, so every backend can drive REAPER. The panel shows model / effort / mode pickers for both kinds
(Gamut's model catalog, ACP config options), permission buttons when an ACP agent asks before a tool call, and a
login button when the agent needs one. If the gateway isn't running the panel falls back to Gamut directly.
Every request except `/health` needs `Authorization: Bearer <token>` from `~/.config/agent-gateway/token` (created
with mode 0600 on first start; the panel reads it). Docker and Lima forward `host.docker.internal` /
`host.lima.internal` to the Mac's loopback, so without the token any container on the Mac could start a host agent.

```bash
hibiki agents add codex claude kimi   # Node (~/.local/node) + the ACP adapters, restarts the gateway
python3 gateway/test_gateway.py                   # fake Gamut + fake ACP agent, no network
```

Agents are listed in `~/.config/agent-gateway/config.json` (`acp: [{slug, name, command, env, cwd}]`,
`mcpServers: [...]`). Transcripts of ACP sessions are kept in `~/.config/agent-gateway/sessions/`.

Gamut desktop on the same Mac: `install.sh` registers the MCP as `http://localhost:8787/mcp` (no tunnel). Agent running somewhere
else and need to reach REAPER over the internet? See [docs/remote-host.md](docs/remote-host.md).

## Tools

`status` `transport` `track` `midi` `fx` `send` `item` `envelope` `project` `render` `action` `reaper_call` `run_lua` `batch`

## Tests

```
python3 tests/test_server.py                         # MCP server + fake bridge, versions in step
uv run --with lupa python tests/test_bridge_lua.py   # bridge.lua against a fake ReaScript API
uv run --with lupa python panel/test_panel.py   # chat panel events, sends and cards, fake reaper/ImGui
python3 gateway/test_gateway.py                      # agent gateway against fake Gamut + fake ACP agent
python3 tests/test_suite.py                          # setup / doctor / connect / uninstall on a fake HOME
uv run --with lupa python scripts/check_syntax.py    # every .lua compiles under Lua 5.4, every .sh parses
```

CI runs all of these on every push: the Python suites on 3.9 (the macOS host's `/usr/bin/python3`) and 3.12.

None of these need REAPER. The Lua-based ones need `lupa` (CI installs it).

## License

MIT
