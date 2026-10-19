# gamut-reaper-suite

<img src="icons/reaper-mcp.svg" alt="" width="96" height="96" align="left" hspace="12" vspace="4">

[REAPER](https://reaper.fm) tooling for AI agents, built around [Gamut](https://github.com/SkillfulAgents/SuperAgent):
an MCP server that drives REAPER, a chat panel docked inside REAPER, and a local gateway so the panel can talk to
Gamut, Codex or any other ACP agent.

| Path | What |
| --- | --- |
| `reaper_mcp/`, `python/`, `lua/bridge.lua` | MCP server (`reaper-mcp`) + the Lua bridge that runs inside REAPER |
| `gamut-chat/` | Gamut Chat — chat panel docked in REAPER (ReaImGui) for Gamut, Codex and other ACP agents |
| `reaper/__startup.lua` | Starts the bridge and the chat panel whenever REAPER launches |
| `gateway/` | Agent gateway: one local API for the panel in front of Gamut and ACP agents (Codex, …) |
| `scripts/` | Remote-host bootstrap (Cloudflare Tunnel) and hot-swap deploy of the bridge — see `docs/remote-host.md` |

[![ci](https://github.com/yiw190/gamut-reaper-suite/actions/workflows/ci.yml/badge.svg)](https://github.com/yiw190/gamut-reaper-suite/actions/workflows/ci.yml)

```
MCP client  --stdio JSON-RPC-->  python/server.py  --JSON mailbox-->  lua/bridge.lua (inside REAPER)
```

Fourteen grouped tools. Multi-step edits go through `batch`; anything else through `reaper_call` / `run_lua`. Track index `-1` is the master. Time is project quarter notes. Call `status` first; on a new project pass `detail=true` for arrangement facts (time signature, length in beats, markers, MIDI vs audio) — not chords. `midi cc` is CC and program change; `item import` takes a path on the REAPER host; `project save` writes without a dialog (pass `path` if the project is unsaved). `midi get` omits default fields; `batch` slots are the inner result or `{error}`.

## Setup

```bash
git clone https://github.com/yiw190/gamut-reaper-suite.git
cd gamut-reaper-suite
python3 python/install_bridge.py
```

Then in REAPER: **Actions → Load ReaScript → `reaper_mcp_bridge.lua` → Run**. Console: `reaper-mcp bridge 0.3.1 ready`.

MCP client config (any client that runs stdio servers):

```json
{
  "mcpServers": {
    "reaper": {
      "command": "python3",
      "args": ["/absolute/path/to/gamut-reaper-suite/python/server.py"]
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

## Gamut Chat panel (macOS)

Needs [ReaImGui](https://github.com/cfillion/reaimgui) ≥ 0.10 and the Gamut desktop app running on the same Mac — the
panel uses the app's local REST + SSE API (`127.0.0.1:47891`), the same one Gamut's own UI uses.

1. Copy `gamut-chat/GamutChat.lua` to `<REAPER resource path>/Scripts/GamutChat/`, then **Actions → Load ReaScript** and run it.
2. Optional: run `gamut-chat/restart_apply.sh` while REAPER is open and quit REAPER — it docks the panel on the right,
   binds **Cmd+L** to toggle it, and relaunches REAPER.

What it does: pick an agent and a session, streamed replies with a markdown renderer, every message carries a
`[REAPER context]` block (project, tempo, cursor, region, selection, MIDI editor notes, focused FX). Enter sends,
Shift+Enter is a newline. Drag to select message text and Cmd+C to copy. Attach files with **+ File** or by
dropping them on the panel: they're uploaded to the agent (Gamut's `upload-file`; for ACP agents the gateway stores
them and sends images as image blocks) and listed in the message as `[Attached files:]`. Latin text uses the REAPER
theme's UI font. When the agent asks a question or needs an approval, the panel jumps to that session in
Gamut (`superagent://agent/<slug>/sessions/<id>`). Scrolling follows Gamut's own follow engine. Chinese/Japanese IME:
ReaImGui passes keys to both the IME and ImGui, so the panel undoes ImGui's half of Backspace / arrows / Esc / Enter
while a composition is open and shows the pending pinyin under the input.

> Gamut's local API has no auth in desktop mode and listens on all interfaces. Block port 47891 in the macOS
> firewall if the Mac is on a network you don't trust.

## Start with REAPER

Copy `reaper/__startup.lua` to `<REAPER resource path>/Scripts/__startup.lua` (or merge it into yours). It starts the
MCP bridge and opens the chat panel on every launch, registering either script if REAPER doesn't know it yet.

## Agent gateway: Codex, Claude Code, Kimi, … in the same panel

`gateway/agent_gateway.py` is a small local server (`127.0.0.1:47892`, stdlib Python) that the panel talks to first.
It proxies Gamut agents unchanged and adds [ACP](https://agentclientprotocol.com) agents — Codex via
`@agentclientprotocol/codex-acp`, Claude Code via `@agentclientprotocol/claude-agent-acp`, Kimi Code CLI (`kimi acp`),
and anything else that speaks ACP — under the same API. Subscriptions work: Codex logs in with ChatGPT, Claude Code
reuses the local `claude` login, Kimi's login button opens `kimi login` (device code) in Terminal. Each ACP session gets this
repo's MCP server, so every backend can drive REAPER. The panel shows model / effort / mode pickers for both kinds
(Gamut's model catalog, ACP config options), permission buttons when an ACP agent asks before a tool call, and a
login button when the agent needs one. If the gateway isn't running the panel falls back to Gamut directly.

```bash
sh gateway/install_macos.sh          # LaunchAgent + Node (~/.local/node) + Codex/Claude/Kimi; WITH_CODEX=0 to skip
python3 gateway/test_gateway.py      # fake Gamut + fake ACP agent, no network
```

Agents are listed in `~/.config/agent-gateway/config.json` (`acp: [{slug, name, command, env, cwd}]`,
`mcpServers: [...]`). Transcripts of ACP sessions are kept in `~/.config/agent-gateway/sessions/`.

Gamut desktop on the same Mac: register the MCP as `http://localhost:8787/mcp` (no tunnel). Agent running somewhere
else and need to reach REAPER over the internet? See [docs/remote-host.md](docs/remote-host.md).

## Tools

`status` `transport` `track` `midi` `fx` `send` `item` `envelope` `project` `render` `action` `reaper_call` `run_lua` `batch`

## Tests

```
python3 python/test_server.py                        # MCP server + fake bridge
uv run --with lupa python python/test_bridge_lua.py  # bridge.lua against a fake ReaScript API
python3 gateway/test_gateway.py                      # agent gateway against fake Gamut + fake ACP agent
uv run --with lupa python scripts/check_syntax.py    # every .lua compiles under Lua 5.4, every .sh parses
```

CI runs all of these on every push: the Python suites on 3.9 (the macOS host's `/usr/bin/python3`) and 3.12, plus a
check that `lua/bridge.lua` and `reaper_mcp/bridge.lua` are identical.

None of these need REAPER. The Lua-based ones need `lupa` (CI installs it).

## License

MIT
