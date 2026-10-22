-- reaper-mcp: start the MCP bridge automatically every time REAPER launches (installed 2026-10-18).
local path = reaper.GetResourcePath() .. "/Scripts/reaper_mcp_bridge.lua"
local id = reaper.NamedCommandLookup("_RS70b9b9e08b3a1b47bef354e4c40290516371e9f9")
if id == 0 then id = reaper.AddRemoveReaScript(true, 0, path, true) end
if id ~= 0 then reaper.Main_OnCommand(id, 0) end

-- Hibiki panel: open on launch. Registered by path (re-adding a known script returns its id); the toggle check
-- keeps a second run from closing it.
do
  local ppath = reaper.GetResourcePath() .. "/Scripts/Hibiki/Hibiki.lua"
  local pid = reaper.file_exists(ppath) and reaper.AddRemoveReaScript(true, 0, ppath, true) or 0
  if pid ~= 0 and reaper.GetToggleCommandState(pid) ~= 1 then reaper.Main_OnCommand(pid, 0) end
end
