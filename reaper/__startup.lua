-- reaper-mcp: start the MCP bridge automatically every time REAPER launches (installed 2026-10-18).
local path = reaper.GetResourcePath() .. "/Scripts/reaper_mcp_bridge.lua"
local id = reaper.NamedCommandLookup("_RS70b9b9e08b3a1b47bef354e4c40290516371e9f9")
if id == 0 then id = reaper.AddRemoveReaScript(true, 0, path, true) end
if id ~= 0 then reaper.Main_OnCommand(id, 0) end

-- Gamut Chat panel: open on launch (2026-10-18). Toggle check so a second run never closes it.
do
  local gpath = reaper.GetResourcePath() .. "/Scripts/GamutChat/GamutChat.lua"
  local gid = reaper.NamedCommandLookup("_RS24f59c095963d6e7a1cf187e50fff047f87d7381")
  if gid == 0 then gid = reaper.AddRemoveReaScript(true, 0, gpath, true) end
  if gid ~= 0 and reaper.GetToggleCommandState(gid) ~= 1 then reaper.Main_OnCommand(gid, 0) end
end
