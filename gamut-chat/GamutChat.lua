-- Gamut Chat: Cursor-style chat panel inside REAPER, talking to a Gamut agent through the
-- local Gamut desktop API (127.0.0.1:47891). Needs ReaImGui >= 0.10.
-- agent gateway (gateway/agent_gateway.py: Gamut + ACP agents, model selectors) first, Gamut's own API as fallback
local HOSTS = { 'http://127.0.0.1:47892', 'http://127.0.0.1:47891' }
local HOST = HOSTS[1]
local EXT = 'GamutChat'

if not reaper.ImGui_GetBuiltinPath then
  reaper.MB('ReaImGui is not installed.', 'Gamut Chat', 0)
  return
end
package.path = reaper.ImGui_GetBuiltinPath() .. '/?.lua;' .. package.path
local ImGui = require 'imgui' '0.10'

---------------------------------------------------------------- utils
local function sh(cmd)
  local p = io.popen(cmd)
  if not p then return '' end
  local o = p:read('*a'); p:close(); return o or ''
end
local function q(s) return "'" .. tostring(s):gsub("'", "'\\''") .. "'" end

local DIR = (os.getenv('TMPDIR') or '/tmp/'):gsub('/?$', '/') .. 'gamutchat/'
sh('mkdir -p ' .. q(DIR))

local function u8(cp)
  if cp < 0x80 then return string.char(cp)
  elseif cp < 0x800 then return string.char(0xC0 | (cp >> 6), 0x80 | (cp & 0x3F))
  elseif cp < 0x10000 then return string.char(0xE0 | (cp >> 12), 0x80 | ((cp >> 6) & 0x3F), 0x80 | (cp & 0x3F))
  else return string.char(0xF0 | (cp >> 18), 0x80 | ((cp >> 12) & 0x3F), 0x80 | ((cp >> 6) & 0x3F), 0x80 | (cp & 0x3F)) end
end

---------------------------------------------------------------- json
local NULL = {}
local decode
do
  local esc = { ['"'] = '"', ['\\'] = '\\', ['/'] = '/', b = '\b', f = '\f', n = '\n', r = '\r', t = '\t' }
  local function skip(s, i) local _, e = s:find('^[ \n\r\t]*', i); return e + 1 end
  local function str(s, i)
    local out, j = {}, i + 1
    while true do
      local k = s:find('["\\]', j)
      if not k then error('unterminated string') end
      out[#out + 1] = s:sub(j, k - 1)
      if s:sub(k, k) == '"' then return table.concat(out), k + 1 end
      local n = s:sub(k + 1, k + 1)
      if n == 'u' then
        local cp = tonumber(s:sub(k + 2, k + 5), 16) or 63
        j = k + 6
        if cp >= 0xD800 and cp <= 0xDBFF and s:sub(j, j + 1) == '\\u' then
          local lo = tonumber(s:sub(j + 2, j + 5), 16)
          if lo and lo >= 0xDC00 and lo <= 0xDFFF then
            cp = 0x10000 + (cp - 0xD800) * 0x400 + (lo - 0xDC00); j = j + 6
          end
        end
        out[#out + 1] = u8(cp)
      else
        out[#out + 1] = esc[n] or n; j = k + 2
      end
    end
  end
  local function val(s, i)
    i = skip(s, i)
    local c = s:sub(i, i)
    if c == '{' then
      local t = {}; i = skip(s, i + 1)
      if s:sub(i, i) == '}' then return t, i + 1 end
      while true do
        local k; k, i = str(s, skip(s, i))
        i = skip(s, skip(s, i) + 1)
        local v; v, i = val(s, i); t[k] = v
        i = skip(s, i)
        if s:sub(i, i) == '}' then return t, i + 1 end
        i = i + 1
      end
    elseif c == '[' then
      local t = {}; i = skip(s, i + 1)
      if s:sub(i, i) == ']' then return t, i + 1 end
      while true do
        local v; v, i = val(s, i); t[#t + 1] = v
        i = skip(s, i)
        if s:sub(i, i) == ']' then return t, i + 1 end
        i = i + 1
      end
    elseif c == '"' then return str(s, i)
    elseif s:sub(i, i + 3) == 'true' then return true, i + 4
    elseif s:sub(i, i + 4) == 'false' then return false, i + 5
    elseif s:sub(i, i + 3) == 'null' then return NULL, i + 4
    else
      local num = s:match('^-?%d+%.?%d*[eE]?[-+]?%d*', i)
      if not num or num == '' then error('bad json at ' .. i) end
      return tonumber(num), i + #num
    end
  end
  decode = function(s)
    local ok, v = pcall(val, s or '', 1)
    if ok then return v end
  end
end
local function jstr(s)
  return '"' .. (s:gsub('[%c"\\]', function(c)
    local m = { ['"'] = '\\"', ['\\'] = '\\\\', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t' }
    return m[c] or string.format('\\u%04x', c:byte())
  end)) .. '"'
end

---------------------------------------------------------------- http (background curl)
local reqn, pending = 0, {}
local function http_url(method, url, body, cb)
  reqn = reqn + 1
  local b = DIR .. 'req' .. reqn
  local args = '-s -m 60 -X ' .. method .. ' -H "Content-Type: application/json"'
  if body then
    local f = io.open(b .. '.json', 'wb'); f:write(body); f:close()
    args = args .. ' --data-binary @' .. q(b .. '.json')
  end
  sh('( /usr/bin/curl ' .. args .. ' -o ' .. q(b .. '.out') .. ' -w "%{http_code}" ' .. q(url) ..
    ' > ' .. q(b .. '.code') .. '; mv ' .. q(b .. '.code') .. ' ' .. q(b .. '.done') .. ' ) >/dev/null 2>&1 &')
  pending[#pending + 1] = { b = b, cb = cb }
end
local function poll_http()
  for i = #pending, 1, -1 do
    local r = pending[i]
    local f = io.open(r.b .. '.done', 'rb')
    if f then
      local code = tonumber(f:read('*a')) or 0; f:close()
      local o = io.open(r.b .. '.out', 'rb')
      local body = o and o:read('*a') or ''
      if o then o:close() end
      os.remove(r.b .. '.done'); os.remove(r.b .. '.out'); os.remove(r.b .. '.json')
      table.remove(pending, i)
      if r.cb then r.cb(code, body) end
    end
  end
end

---------------------------------------------------------------- state
local S = {
  agent = reaper.GetExtState(EXT, 'agent'), agent_name = reaper.GetExtState(EXT, 'agent_name'),
  agents = {}, agents_at = 0,
  sid = '', title = '',
  msgs = {}, live = {},
  active = false, waiting = false, err = nil,
  input = '', attach = true, focus = true, scroll = true,
  sessions = {}, sess_at = 0,
  stream = { pid = nil, file = nil, off = 0, buf = '', check = 0 },
  cfg_opts = {}, cfg_sel = {}, perms = {}, auth = nil, acp_status = {},
}
local function sval(v) return type(v) == 'string' and v or (type(v) == 'number' and tostring(v)) or nil end
local function is_acp() return S.agent:sub(1, 4) == 'acp-' end
local function via_gateway() return HOST == HOSTS[1] end
-- per-agent selector picks (model / effort / mode), stored as "id\tvalue" lines
local function cfg_load()
  S.cfg_sel = {}
  for id, v in (reaper.GetExtState(EXT, 'cfg_' .. S.agent) .. '\n'):gmatch('([^\t\n]+)\t([^\n]*)\n') do S.cfg_sel[id] = v end
end
local function cfg_save()
  local L = {}
  for id, v in pairs(S.cfg_sel) do L[#L + 1] = id .. '\t' .. v end
  reaper.SetExtState(EXT, 'cfg_' .. S.agent, table.concat(L, '\n'), true)
end
local function cfg_json()
  if not via_gateway() then return '' end
  local L = {}
  for id, v in pairs(S.cfg_sel) do L[#L + 1] = jstr(id) .. ':' .. jstr(v) end
  return ',"config":{' .. table.concat(L, ',') .. '}'
end
local function load_agent_session()
  S.sid = reaper.GetExtState(EXT, 'session_' .. S.agent)
  S.title = reaper.GetExtState(EXT, 'title_' .. S.agent)
end
local function save_agent_session()
  reaper.SetExtState(EXT, 'session_' .. S.agent, S.sid, true)
  reaper.SetExtState(EXT, 'title_' .. S.agent, S.title, true)
end
if S.agent ~= '' then load_agent_session() end

local function api(path) return HOST .. '/api/agents/' .. S.agent .. path end
local function http(method, path, body, cb) http_url(method, api(path), body, cb) end
local function scroll() S.scroll = true end

local function refresh_messages()
  if S.sid == '' then return end
  local sid = S.sid
  http('GET', '/sessions/' .. sid .. '/messages', nil, function(code, body)
    if code ~= 200 or sid ~= S.sid then return end
    local list = decode(body)
    if type(list) ~= 'table' then return end
    if list.messages then list = list.messages end
    local out = {}
    for _, m in ipairs(list) do
      local role = m.type
      local text = type(m.content) == 'table' and m.content.text or (type(m.content) == 'string' and m.content) or ''
      if type(text) ~= 'string' then text = '' end
      local tools = {}
      if type(m.toolCalls) == 'table' then
        for _, tc in ipairs(m.toolCalls) do tools[#tools + 1] = tc.name end
      end
      if role == 'user' and text ~= '' then
        out[#out + 1] = { role = 'user', text = text }
      elseif role == 'assistant' and (text ~= '' or #tools > 0) then
        out[#out + 1] = { role = 'assistant', text = text, tools = tools }
      end
    end
    local old = {}
    for _, m in ipairs(S.msgs) do old[m.role .. '\0' .. m.text] = m end
    for i, m in ipairs(out) do
      local o = old[m.role .. '\0' .. m.text]
      if o then m._md, m._bubble = o._md, o._bubble end
    end
    if #S.msgs == 0 then scroll() end
    S.msgs = out
    S.live = {}
  end)
end

---------------------------------------------------------------- SSE stream
local function stream_stop()
  if S.stream.pid then sh('pkill -P ' .. S.stream.pid .. ' 2>/dev/null; kill ' .. S.stream.pid .. ' 2>/dev/null') end
  S.stream.pid = nil
end

local function stream_start()
  stream_stop()
  if S.sid == '' then return end
  local st = S.stream
  st.file = DIR .. 'stream.sse'
  local f = io.open(st.file, 'wb'); f:close()
  st.off, st.buf, st.check = 0, '', reaper.time_precise()
  os.remove(st.file .. '.dead')
  -- the wrapper drops a .dead file when curl exits, so liveness is a file check instead of a per-3s popen (fork stalls the UI)
  local out = sh('( /usr/bin/curl -s -N ' .. q(api('/sessions/' .. S.sid .. '/stream')) ..
    ' >> ' .. q(st.file) .. '; : > ' .. q(st.file .. '.dead') .. ' ) >/dev/null 2>&1 & echo $!')
  st.pid = tonumber(out:match('%d+'))
end

local function tool_label(name)
  if type(name) ~= 'string' then return 'tool' end
  return (name:gsub('^mcp__reaper_mcp__', 'reaper · '):gsub('^mcp__', ''))
end

-- Gamut's agent deep link (SuperAgent src/main/agent-deep-link.ts) focuses the app on this exact session
local function open_in_gamut()
  if S.agent ~= '' and S.sid ~= '' then
    sh('/usr/bin/open ' .. q('superagent://agent/' .. S.agent .. '/sessions/' .. S.sid) .. ' >/dev/null 2>&1 &')
  else
    sh('/usr/bin/open -a Gamut >/dev/null 2>&1 &')
  end
end

local function on_event(d)
  local t = d.type
  if t == 'connected' then
    S.active = d.isActive == true
  elseif t == 'stream_start' or t == 'message_start' or t == 'session_active' then
    S.active = true
  elseif t == 'stream_delta' then
    if type(d.text) == 'string' then
      local last = S.live[#S.live]
      if not last or last.kind ~= 'text' then last = { kind = 'text', text = '' }; S.live[#S.live + 1] = last end
      last.text = last.text .. d.text
      S.active = true
    end
  elseif t == 'tool_use_ready' then
    S.live[#S.live + 1] = { kind = 'tool', text = tool_label(d.toolName) }
  elseif t == 'session_awaiting_input' then
    if not S.waiting then open_in_gamut() end -- questions/approvals are answered in Gamut, so jump there once
    S.waiting = true
  elseif t == 'permission_request' then
    for _, p in ipairs(S.perms) do if p.id == d.requestId then return end end
    S.perms[#S.perms + 1] = { id = d.requestId, title = d.title or 'Run tool', options = d.options or {} }
    scroll()
  elseif t == 'permission_resolved' then
    for i = #S.perms, 1, -1 do if S.perms[i].id == d.requestId then table.remove(S.perms, i) end end
  elseif t == 'config_options' then
    if type(d.configOptions) == 'table' then S.cfg_opts = d.configOptions end
  elseif t == 'notice' then
    S.live[#S.live + 1] = { kind = 'tool', text = tostring(d.text or '') }
  elseif t == 'session_input_provided' then
    S.waiting = false
  elseif t == 'session_idle' then
    S.active, S.waiting, S.perms = false, false, {}
    refresh_messages()
  elseif t == 'session_error' then
    S.active, S.perms = false, {}
    if d.apiErrorCode == 'auth_required' then S.auth = S.auth or { methods = {} } end
    local p = d.errorPresentation
    S.err = (type(p) == 'table' and (p.message or p.title)) or d.apiErrorCode or 'error'
    refresh_messages()
  end
end

local function poll_stream()
  local st = S.stream
  if not st.file then return end
  local f = io.open(st.file, 'rb')
  if f then
    f:seek('set', st.off)
    local chunk = f:read('*a') or ''
    f:close()
    if #chunk > 0 then
      st.off = st.off + #chunk
      st.buf = (st.buf .. chunk):gsub('\r\n', '\n')
      while true do
        local e = st.buf:find('\n\n', 1, true)
        if not e then break end
        local ev = st.buf:sub(1, e - 1)
        st.buf = st.buf:sub(e + 2)
        local data = {}
        for line in (ev .. '\n'):gmatch('(.-)\n') do
          local v = line:match('^data: ?(.*)$')
          if v then data[#data + 1] = v end
        end
        local d = decode(table.concat(data, '\n'))
        if type(d) == 'table' then on_event(d) end
      end
    end
  end
  local now = reaper.time_precise()
  if S.sid ~= '' and now - st.check > 3 then
    st.check = now
    local dead = io.open(st.file .. '.dead', 'rb')
    if dead then dead:close() end
    if not st.pid or dead then stream_start() end
  end
end

---------------------------------------------------------------- REAPER context
local function pos(t) return reaper.format_timestr_pos(t, '', 2) end
local NOTE = { 'C', 'C#', 'D', 'D#', 'E', 'F', 'F#', 'G', 'G#', 'A', 'A#', 'B' }
local function note_name(p) return NOTE[p % 12 + 1] .. (p // 12 - 1) end
local function trnum(tr) return math.floor(reaper.GetMediaTrackInfo_Value(tr, 'IP_TRACKNUMBER')) end

local function midi_ctx(proj, full)
  local ed = reaper.MIDIEditor_GetActive()
  if not ed then return nil end
  local take = reaper.MIDIEditor_GetTake(ed)
  if not take or not reaper.ValidatePtr2(proj, take, 'MediaItem_Take*') then return nil end
  local tr = reaper.GetMediaItemTake_Track(take)
  local _, tn = reaper.GetTrackName(tr)
  local _, total = reaper.MIDI_CountEvts(take)
  local sel, list = 0, {}
  local idx = reaper.MIDI_EnumSelNotes(take, -1)
  while idx ~= -1 do
    sel = sel + 1
    if full and #list < 32 then
      local _, _, _, sp, ep, ch, pitch, vel = reaper.MIDI_GetNote(take, idx)
      local t0 = reaper.MIDI_GetProjTimeFromPPQPos(take, sp)
      local t1 = reaper.MIDI_GetProjTimeFromPPQPos(take, ep)
      local len = reaper.TimeMap2_timeToQN(proj, t1) - reaper.TimeMap2_timeToQN(proj, t0)
      list[#list + 1] = ('  note %d: %s @ %s, %.3g qn, vel %d, ch %d'):format(idx, note_name(pitch), pos(t0), len, vel, ch + 1)
    end
    idx = reaper.MIDI_EnumSelNotes(take, idx)
  end
  local r = { track = ('#%d %s'):format(trnum(tr), tn), sel = sel, total = total, list = list }
  if full then
    local item = reaper.GetMediaItemTake_Item(take)
    r.item = pos(reaper.GetMediaItemInfo_Value(item, 'D_POSITION'))
    local _, tk = reaper.GetSetMediaItemTakeInfo_String(take, 'P_NAME', '', false)
    r.take = tk
  end
  return r
end

local function marker_ctx(proj, cur)
  local mk, rg = reaper.GetLastMarkerAndCurRegion(proj, cur)
  local r = {}
  if rg >= 0 then
    local _, _, p0, p1, name, num = reaper.EnumProjectMarkers3(proj, rg)
    r.region_name = name
    r.region = ('"%s" (region %d, %s–%s)'):format(name, num, pos(p0), pos(p1))
  end
  if mk >= 0 then
    local _, _, p0, _, name, num = reaper.EnumProjectMarkers3(proj, mk)
    r.marker = ('"%s" (marker %d @ %s)'):format(name, num, pos(p0))
  end
  return r
end

local function razor_ctx(proj)
  local out = {}
  for i = 0, reaper.CountTracks(proj) - 1 do
    local tr = reaper.GetTrack(proj, i)
    local _, s = reaper.GetSetMediaTrackInfo_String(tr, 'P_RAZOREDITS', '', false)
    if s ~= '' then
      local segs = {}
      for a, b, g in s:gmatch('(%S+) (%S+) "([^"]*)"') do
        segs[#segs + 1] = pos(tonumber(a)) .. '–' .. pos(tonumber(b)) .. (g ~= '' and ' (envelope lane)' or '')
      end
      local _, tn = reaper.GetTrackName(tr)
      out[#out + 1] = ('#%d %s: %s'):format(i + 1, tn, table.concat(segs, ', '))
    end
  end
  return out
end

local function fx_ctx(proj, full)
  local rv, tnum, inum, fnum = reaper.GetFocusedFX2()
  if not rv or rv == 0 then return nil end
  local tr = tnum == 0 and reaper.GetMasterTrack(proj) or reaper.GetTrack(proj, tnum - 1)
  if not tr then return nil end
  local _, tname = reaper.GetTrackName(tr)
  local where = tnum == 0 and 'MASTER' or ('#%d %s'):format(tnum, tname)
  local name, nparams, pname, pval, enabled
  if rv & 2 ~= 0 then
    local item = reaper.GetTrackMediaItem(tr, inum)
    local take = item and reaper.GetMediaItemTake(item, fnum >> 16)
    if not take then return nil end
    local fx = fnum & 0xFFFF
    name = select(2, reaper.TakeFX_GetFXName(take, fx))
    nparams = reaper.TakeFX_GetNumParams(take, fx)
    pname = function(p) return select(2, reaper.TakeFX_GetParamName(take, fx, p)) end
    pval = function(p) return select(2, reaper.TakeFX_GetFormattedParamValue(take, fx, p)) end
    enabled = reaper.TakeFX_GetEnabled(take, fx)
    where = where .. (', item %d take FX %d'):format(inum, fx)
  else
    name = select(2, reaper.TrackFX_GetFXName(tr, fnum))
    nparams = reaper.TrackFX_GetNumParams(tr, fnum)
    pname = function(p) return select(2, reaper.TrackFX_GetParamName(tr, fnum, p)) end
    pval = function(p) return select(2, reaper.TrackFX_GetFormattedParamValue(tr, fnum, p)) end
    enabled = reaper.TrackFX_GetEnabled(tr, fnum)
    where = where .. ((fnum & 0x1000000 ~= 0) and (', input FX %d'):format(fnum & 0xFFFFFF) or (', FX %d'):format(fnum))
  end
  local r = { name = name, short = (name:gsub('^%w+i?: ', ''):gsub(' %(.-%)$', '')), where = where,
    enabled = enabled, focused = rv & 4 == 0, n = nparams, params = {} }
  if full then
    for p = 0, nparams - 1 do
      if #r.params >= 48 then break end
      local pn = pname(p)
      if pn and pn ~= '' and pn ~= 'Bypass' and pn ~= 'Wet' and pn ~= 'Delta' then
        r.params[#r.params + 1] = ('  p%d %s = %s'):format(p, pn, pval(p) or '')
      end
    end
  end
  return r
end

local function context(full)
  local proj = reaper.EnumProjects(-1)
  local name = reaper.GetProjectName(proj):gsub('%.[Rr][Pp][Pp]$', '')
  if name == '' then name = '(unsaved)' end
  local ps = reaper.GetPlayState()
  local state = (ps & 4 ~= 0) and 'recording' or (ps & 1 ~= 0) and 'playing' or (ps & 2 ~= 0) and 'paused' or 'stopped'
  local cur = (ps & 1 ~= 0) and reaper.GetPlayPosition() or reaper.GetCursorPosition()
  local bpm = math.floor(reaper.Master_GetTempo() * 100 + 0.5) / 100
  local num, den = reaper.TimeMap_GetTimeSigAtTime(proj, cur)
  local L = {
    ('project: %s'):format(name),
    ('tempo: %g BPM %d/%d'):format(bpm, num, den),
    ('cursor: %s (%s)'):format(pos(cur), state),
  }
  local chip = { name, ('%g BPM'):format(bpm), pos(cur) }

  local mk = marker_ctx(proj, cur)
  if mk.region then L[#L + 1] = 'region at cursor: ' .. mk.region; chip[#chip + 1] = mk.region_name end
  if mk.marker then L[#L + 1] = 'last marker: ' .. mk.marker end

  local s, e = reaper.GetSet_LoopTimeRange2(proj, false, false, 0, 0, false)
  if e > s then L[#L + 1] = ('time selection: %s – %s'):format(pos(s), pos(e)); chip[#chip + 1] = 'time sel' end

  local tr = {}
  for i = 0, reaper.CountSelectedTracks(proj) - 1 do
    local t = reaper.GetSelectedTrack(proj, i)
    local _, tn = reaper.GetTrackName(t)
    tr[#tr + 1] = { ('#%d %s'):format(trnum(t), tn), tn }
  end
  if #tr > 0 then
    local names = {}
    for _, x in ipairs(tr) do names[#names + 1] = x[1] end
    L[#L + 1] = 'selected tracks: ' .. table.concat(names, ', ')
    chip[#chip + 1] = #tr == 1 and tr[1][2] or (#tr .. ' tracks')
  end

  local n = reaper.CountSelectedMediaItems(proj)
  if n > 0 then
    if full then
      local it = {}
      for i = 0, math.min(n, 8) - 1 do
        local m = reaper.GetSelectedMediaItem(proj, i)
        local _, tn = reaper.GetTrackName(reaper.GetMediaItem_Track(m))
        local p0 = reaper.GetMediaItemInfo_Value(m, 'D_POSITION')
        local len = reaper.GetMediaItemInfo_Value(m, 'D_LENGTH')
        it[#it + 1] = ('%s @ %s–%s'):format(tn, pos(p0), pos(p0 + len))
      end
      if n > 8 then it[#it + 1] = ('… +%d more'):format(n - 8) end
      L[#L + 1] = 'selected items: ' .. table.concat(it, '; ')
    end
    chip[#chip + 1] = n .. (n == 1 and ' item' or ' items')
  end

  local rz = razor_ctx(proj)
  if #rz > 0 then L[#L + 1] = 'razor edits: ' .. table.concat(rz, '; '); chip[#chip + 1] = 'razor' end

  local md = midi_ctx(proj, full)
  if md then
    if md.sel > 0 then
      chip[#chip + 1] = md.sel .. (md.sel == 1 and ' note' or ' notes')
      if full then
        L[#L + 1] = ('MIDI editor: %s, item @ %s, take "%s" — %d of %d notes selected%s:'):format(
          md.track, md.item, md.take, md.sel, md.total, md.sel > #md.list and (' (first ' .. #md.list .. ' listed)') or '')
        for _, x in ipairs(md.list) do L[#L + 1] = x end
      end
    elseif full then
      L[#L + 1] = ('MIDI editor open: %s, item @ %s, take "%s", %d notes, none selected'):format(md.track, md.item, md.take, md.total)
    end
  end

  local fx = fx_ctx(proj, full)
  if fx then
    chip[#chip + 1] = 'FX: ' .. fx.short
    if full then
      L[#L + 1] = ('%s FX: %s on %s (%s) — %d params%s:'):format(fx.focused and 'focused' or 'open (last focused)',
        fx.name, fx.where, fx.enabled and 'enabled' or 'bypassed', fx.n,
        fx.n > #fx.params and (', first ' .. #fx.params .. ' shown') or '')
      for _, x in ipairs(fx.params) do L[#L + 1] = x end
    end
  end
  return table.concat(L, '\n'), table.concat(chip, ' · ')
end

local chip_cache, chip_at = '', 0
local function chip_text()
  local now = reaper.time_precise()
  if now - chip_at > 0.25 then
    chip_at = now
    local _, c = context(false)
    chip_cache = c
  end
  return chip_cache
end

local CTX_OPEN = '[REAPER context — auto-attached by the Gamut Chat panel in REAPER; #N = REAPER track number, MCP index = N-1]'
local CTX_CLOSE = '[/REAPER context]'

local function split_context(text)
  local a, b = text:find(CTX_OPEN, 1, true)
  if a == 1 then
    local c, d = text:find(CTX_CLOSE, b, true)
    if c then return text:sub(d + 1):gsub('^%s+', ''), true end
  end
  return text, false
end

---------------------------------------------------------------- actions
local function send()
  if S.agent == '' then return end
  local text = S.input:gsub('^%s+', ''):gsub('%s+$', '')
  if text == '' then return end
  local payload = text
  if S.attach then
    local ctx_text = context(true)
    payload = CTX_OPEN .. '\n' .. ctx_text .. '\n' .. CTX_CLOSE .. '\n\n' .. text
  end
  S.msgs[#S.msgs + 1] = { role = 'user', text = payload }
  S.live, S.err, S.input, S.focus, S.active = {}, nil, '', true, true
  scroll()
  if S.sid == '' then
    local agent = S.agent
    http('POST', '/sessions', '{"message":' .. jstr(payload) .. cfg_json() .. '}', function(code, body)
      local r = decode(body)
      if agent ~= S.agent then return end
      if code >= 200 and code < 300 and type(r) == 'table' and type(r.id) == 'string' then
        S.sid = r.id
        S.title = text:sub(1, 60)
        save_agent_session()
        stream_start()
      elseif code == 401 then
        S.active, S.auth = false, S.auth or { methods = {} }
        S.err = (S.agent_name ~= '' and S.agent_name or S.agent) .. ' needs a login first'
      else
        S.active = false
        S.err = ('create session failed (%d)'):format(code)
      end
    end)
  else
    http('POST', '/sessions/' .. S.sid .. '/messages', '{"content":' .. jstr(payload) .. cfg_json() .. '}', function(code)
      if code < 200 or code >= 300 then
        S.active = false
        S.err = ('send failed (%d)'):format(code)
      end
    end)
  end
end

local function stop()
  if S.sid ~= '' then http('POST', '/sessions/' .. S.sid .. '/interrupt', '{}') end
end

local function reset_view()
  stream_stop()
  S.msgs, S.live, S.active, S.err, S.waiting, S.sessions, S.sess_at = {}, {}, false, nil, false, {}, 0
  S.perms = {}
end

local function new_chat()
  reset_view()
  S.sid, S.title = '', ''
  save_agent_session()
  S.focus = true
end

local function iso_age(iso)
  local y, mo, d, h, mi, se = tostring(iso):match('(%d+)-(%d+)-(%d+)T(%d+):(%d+):(%d+)')
  if not y then return '' end
  local t = os.time({ year = tonumber(y), month = tonumber(mo), day = tonumber(d), hour = tonumber(h),
    min = tonumber(mi), sec = tonumber(se), isdst = false })
  local a = os.time(os.date('!*t')) - t
  if a < 3600 then return math.max(1, a // 60) .. 'm'
  elseif a < 86400 then return a // 3600 .. 'h'
  else return a // 86400 .. 'd' end
end

local function load_sessions()
  S.sess_at = reaper.time_precise()
  local agent = S.agent
  http('GET', '/sessions', nil, function(code, body)
    if code ~= 200 or agent ~= S.agent then return end
    local list = decode(body)
    if type(list) ~= 'table' then return end
    if list.sessions then list = list.sessions end
    local out = {}
    for _, x in ipairs(list) do
      if type(x.id) == 'string' then
        out[#out + 1] = { id = x.id, name = type(x.name) == 'string' and x.name or x.id:sub(1, 8),
          age = iso_age(x.lastActivityAt), active = x.isActive == true }
      end
    end
    S.sessions = out
  end)
end

local load_config
local function load_agents(try)
  try = try or 1
  S.agents_at = reaper.time_precise()
  local host = HOSTS[try]
  http_url('GET', host .. '/api/agents', nil, function(code, body)
    if code ~= 200 then
      if try < #HOSTS then return load_agents(try + 1) end
      S.agents_err = 'Agent gateway and Gamut not reachable (' .. code .. ')'
      S.boot = false
      return
    end
    local list = decode(body)
    if type(list) ~= 'table' then return end
    if list.agents then list = list.agents end
    local changed = host ~= HOST
    HOST = host
    local out = {}
    for _, a in ipairs(list) do
      if type(a.slug) == 'string' then
        out[#out + 1] = { slug = a.slug, name = type(a.name) == 'string' and a.name or a.slug,
          desc = type(a.description) == 'string' and a.description or '', acp = a.backend == 'acp' }
      end
    end
    S.agents, S.agents_err = out, nil
    if (changed or not S.cfg_loaded) and S.agent ~= '' then load_config() end
    if S.boot then
      S.boot = false -- the saved session is reopened once we know which host answers
      if S.sid ~= '' then stream_start(); refresh_messages() end
    end
  end)
end

-- ACP agent status for the picker dots and the login card; probe=true starts the agent to find out
local function load_acp_status(slug, probe)
  if not via_gateway() or slug:sub(1, 4) ~= 'acp-' then return end
  if slug == S.agent then S.auth_at = reaper.time_precise() end
  http_url('GET', HOST .. '/api/agents/' .. slug .. '/status' .. (probe and '?probe=1' or ''), nil, function(code, body)
    local r = decode(body)
    if code ~= 200 or type(r) ~= 'table' then return end
    local err = sval(r.error)
    local st = { state = sval(r.state) or '', connected = r.connected == true, err = err,
      methods = type(r.authMethods) == 'table' and r.authMethods or {} }
    S.acp_status[slug] = st
    if slug ~= S.agent then return end
    if st.connected then
      if S.auth then S.auth, S.err = nil, nil end
      if #S.cfg_opts == 0 and not S.cfg_pending then load_config() end
    elseif st.state == 'auth_required' then
      local waiting = S.auth and S.auth.waiting and err == nil
      S.auth = { methods = st.methods, err = err, waiting = waiting }
    elseif st.state == 'authenticating' then
      S.auth = S.auth or { methods = st.methods }
      S.auth.waiting = true
    end
  end)
end
local function load_status() if is_acp() then load_acp_status(S.agent, false) end end

function load_config()
  S.cfg_loaded = true
  S.cfg_opts = {}
  if not via_gateway() or S.agent == '' then return end
  local agent = S.agent
  S.cfg_pending = true
  http('GET', '/config', nil, function(code, body)
    if agent == S.agent then S.cfg_pending = false end
    if agent ~= S.agent then return end
    local r = decode(body)
    if code == 200 and type(r) == 'table' and type(r.configOptions) == 'table' then
      S.cfg_opts = r.configOptions
    elseif code == 401 then
      S.auth = S.auth or { methods = {} }
      load_status()
    end
  end)
end

local function answer_perm(p, option_id)
  for i = #S.perms, 1, -1 do if S.perms[i] == p then table.remove(S.perms, i) end end
  local opt = option_id and jstr(option_id) or 'null'
  http('POST', '/sessions/' .. S.sid .. '/permission', '{"requestId":' .. jstr(p.id) .. ',"optionId":' .. opt .. '}')
end

local function start_login(method_id)
  S.auth = S.auth or { methods = {} }
  S.auth.waiting, S.auth.err = true, nil
  http('POST', '/auth', '{"methodId":' .. jstr(method_id) .. '}', function() load_status() end)
end

local function switch_session(id, name)
  if id == S.sid then return end
  reset_view()
  S.sid, S.title = id, name or ''
  save_agent_session()
  stream_start()
  refresh_messages()
  S.focus = true
end

local function choose_agent(slug, name)
  if slug == S.agent then return end
  reset_view()
  S.agent, S.agent_name = slug, name
  reaper.SetExtState(EXT, 'agent', slug, true)
  reaper.SetExtState(EXT, 'agent_name', name, true)
  load_agent_session()
  cfg_load()
  S.auth = nil
  load_config()
  if is_acp() then load_acp_status(S.agent, true) end
  if S.sid ~= '' then stream_start(); refresh_messages() end
  S.focus = true
end

---------------------------------------------------------------- UI setup
local ctx = ImGui.CreateContext('Gamut Chat')
local function font_file(path, index)
  local f = io.open(path, 'rb')
  if not f then return nil end
  f:close()
  return ImGui.CreateFontFromFile(path, index or 0)
end
local HIRA = '/System/Library/Fonts/Hiragino Sans GB.ttc'
local F = {
  reg = font_file(HIRA, 0) or ImGui.CreateFont('sans-serif'),
  bold = font_file(HIRA, 2) or ImGui.CreateFont('sans-serif', ImGui.FontFlags_Bold),
  mono = font_file('/System/Library/Fonts/Menlo.ttc', 0) or ImGui.CreateFont('monospace'),
}
for _, f in pairs(F) do ImGui.Attach(ctx, f) end
-- key + char of one keystroke must land in the same frame for the IME guard below
ImGui.SetConfigVar(ctx, ImGui.ConfigVar_InputTrickleEventQueue, 0)

-- ReaImGui (cocoa_inputview.mm keyDown) hands every key to the IME *and* to ImGui, so while a pinyin
-- composition is open Backspace/arrows/Esc also edit the field and Enter also sends. We infer the composition
-- (letter keys that produced no characters) and this callback undoes ImGui's side of those keys.
local ime_cb = ImGui.CreateFunctionFromEEL([[
restore ? (
  InputTextCallback_DeleteChars(0, strlen(#Buf));
  InputTextCallback_InsertChars(0, #prev);
  CursorPos = prev_cursor; SelectionStart = prev_cursor; SelectionEnd = prev_cursor;
  restore = 0;
) : (
  strip_nl && CursorPos > 0 && str_getchar(#Buf, CursorPos - 1) == 10 ? (
    InputTextCallback_DeleteChars(CursorPos - 1, 1);
    strcpy(#prev, #Buf); str_delsub(#prev, CursorPos - 1, 1);
    CursorPos -= 1; SelectionStart = CursorPos; SelectionEnd = CursorPos;
  ) : strcpy(#prev, #Buf);
  strip_nl = 0;
  prev_cursor = CursorPos;
);
]])
ImGui.Attach(ctx, ime_cb)
local FONT_SIZE = 14

-- palette follows the active REAPER theme
local function native(name, fallback)
  local c = reaper.GetThemeColor(name, 0)
  if not c or c < 0 then return fallback end
  local r, g, b = reaper.ColorFromNative(c)
  return (r << 24) | (g << 16) | (b << 8) | 0xFF
end
local function lum(c) return (((c >> 24) & 255) * 299 + ((c >> 16) & 255) * 587 + ((c >> 8) & 255) * 114) / 1000 end
local function alpha(c, a) return (c & 0xFFFFFF00) | a end

local C, COLORS, theme_file, theme_at, pal_ver = {}, {}, nil, 0, 0
local function build_palette()
  local bg = native('col_tracklistbg', 0x3B3B3BFF)
  local dark = lum(bg) < 128
  C.bg = bg
  C.panel = native('windowtab_bg', dark and 0x424242FF or 0xF6F8FAFF)
  C.input = native('docker_bg', dark and 0x2D2D2DFF or 0xFFFFFFFF)
  C.border = native('col_main_3dsh', dark and 0x323232FF or 0xD0D7DEFF)
  C.text = native('col_tcp_text', dark and 0xF0F0F0FF or 0x1F2328FF)
  if (lum(C.text) < 128) == (not dark) then C.text = dark and 0xF0F0F0FF or 0x1F2328FF end
  C.muted = native('col_main_text2', dark and 0x8C8C8CFF or 0x6E7781FF)
  C.accent = native('col_toolbar_text_on', 0x44B3F6FF)
  C.user = native('genlist_selbg', dark and 0x3E5568FF or 0xDDF4FFFF)
  C.code = native('docker_bg', dark and 0x2D2D2DFF or 0xF6F8FAFF)
  C.codebg = alpha(C.text, 0x1C)
  C.rule = dark and 0x5A5A5AFF or 0xD0D7DEFF
  C.hover = dark and 0x4A4A4AFF or 0xEAEEF2FF
  C.danger = dark and 0xF47067FF or 0xCF222EFF
  C.ok = dark and 0x3FB950FF or 0x1A7F37FF
  C.warn = dark and 0xD29922FF or 0x9A6700FF
  C.body = alpha(C.text, 0xE6)
  COLORS = {
    { ImGui.Col_WindowBg, C.bg }, { ImGui.Col_ChildBg, 0x00000000 }, { ImGui.Col_PopupBg, C.panel },
    { ImGui.Col_Text, C.text }, { ImGui.Col_TextDisabled, C.muted }, { ImGui.Col_Border, C.border },
    { ImGui.Col_FrameBg, C.input }, { ImGui.Col_FrameBgHovered, C.input }, { ImGui.Col_FrameBgActive, C.input },
    { ImGui.Col_Button, C.panel }, { ImGui.Col_ButtonHovered, C.hover }, { ImGui.Col_ButtonActive, C.border },
    { ImGui.Col_Header, alpha(C.accent, 0x55) }, { ImGui.Col_HeaderHovered, alpha(C.accent, 0x33) },
    { ImGui.Col_HeaderActive, alpha(C.accent, 0x77) },
    { ImGui.Col_TitleBg, C.panel }, { ImGui.Col_TitleBgActive, C.panel }, { ImGui.Col_Separator, C.border },
    { ImGui.Col_ScrollbarBg, 0x00000000 }, { ImGui.Col_ScrollbarGrab, C.hover },
    { ImGui.Col_CheckMark, C.accent }, { ImGui.Col_InputTextCursor, C.accent },
    { ImGui.Col_TextSelectedBg, alpha(C.accent, 0x66) }, { ImGui.Col_NavCursor, C.accent },
  }
  pal_ver = pal_ver + 1
end
local function check_theme()
  local now = reaper.time_precise()
  if now - theme_at < 1 then return end
  theme_at = now
  local f = reaper.GetLastColorThemeFile()
  if f ~= theme_file then theme_file = f; build_palette() end
end
build_palette()

local VARS = {
  { ImGui.StyleVar_FrameRounding, 6 }, { ImGui.StyleVar_ChildRounding, 6 }, { ImGui.StyleVar_PopupRounding, 6 },
  { ImGui.StyleVar_WindowPadding, 10, 10 }, { ImGui.StyleVar_FramePadding, 8, 5 },
  { ImGui.StyleVar_ItemSpacing, 8, 6 }, { ImGui.StyleVar_WindowBorderSize, 0 },
}

---------------------------------------------------------------- markdown
local wcache, wcache_n = {}, 0
local function tw(font, size, s)
  local fc = wcache[font]
  if not fc then fc = {}; wcache[font] = fc end
  local sc = fc[size]
  if not sc then sc = {}; fc[size] = sc end
  local w = sc[s]
  if not w then
    ImGui.PushFont(ctx, font, size)
    w = ImGui.CalcTextSize(ctx, s)
    ImGui.PopFont(ctx)
    sc[s] = w
    wcache_n = wcache_n + 1
    if wcache_n > 30000 then wcache, wcache_n = {}, 0 end
  end
  return w
end

local function parse_inline(s)
  local runs, buf = {}, {}
  local bold = false
  local function flush()
    if #buf > 0 then runs[#runs + 1] = { text = table.concat(buf), bold = bold }; buf = {} end
  end
  local i, n = 1, #s
  while i <= n do
    local c = s:sub(i, i)
    local handled = false
    if c == '`' then
      local j = s:find('`', i + 1, true)
      if j then flush(); runs[#runs + 1] = { text = s:sub(i + 1, j - 1), code = true }; i = j + 1; handled = true end
    elseif (c == '*' or c == '_') and s:sub(i + 1, i + 1) == c then
      flush(); bold = not bold; i = i + 2; handled = true
    elseif c == '[' then
      local t, u, e = s:match('^%[([^%]]+)%]%(([^%)%s]+)%)()', i)
      if t then flush(); runs[#runs + 1] = { text = t, link = u }; i = e; handled = true end
    elseif c == '*' then
      local j = s:find('*', i + 1, true)
      if j and j > i + 1 and not s:sub(i + 1, i + 1):match('%s') then
        flush(); runs[#runs + 1] = { text = s:sub(i + 1, j - 1), italic = true, bold = bold }; i = j + 1; handled = true
      end
    end
    if not handled then buf[#buf + 1] = c; i = i + 1 end
  end
  flush()
  return runs
end

local function tokens(s)
  local out, i, n = {}, 1, #s
  while i <= n do
    local b = s:byte(i)
    if b == 10 then
      out[#out + 1] = '\n'; i = i + 1
    elseif b < 0x80 then
      local tok = s:match('^[^ \n\128-\255]+ *', i) or s:match('^ +', i)
      out[#out + 1] = tok; i = i + #tok
    else
      local len = b >= 0xF0 and 4 or b >= 0xE0 and 3 or b >= 0xC0 and 2 or 1
      out[#out + 1] = s:sub(i, i + len - 1); i = i + len
    end
  end
  return out
end

-- ops: {'t', x, y, font, size, col, text} | {'r', x1, y1, x2, y2, col, rounding} | {'l', x1, y1, x2, y2, col, thick}
local function layout_inline(ops, runs, x0, y, maxw, size, base_col, mono)
  local lh = math.floor(size * 1.5 + 0.5)
  local dy = math.floor((lh - size) / 2)
  local x, has = x0, false
  for _, r in ipairs(runs) do
    local font = (mono or r.code) and F.mono or (r.bold and F.bold or F.reg)
    local col = r.link and C.accent or (r.italic and C.muted) or base_col
    for _, tok in ipairs(tokens(r.text)) do
      if tok == '\n' then
        x, y, has = x0, y + lh, false
      else
        local f = (font == F.mono and tok:byte(1) >= 0x80) and F.reg or font
        local w = tw(f, size, tok)
        if x + w > x0 + maxw and has then
          x, y, has = x0, y + lh, false
          if not mono then tok = tok:gsub('^ +', ''); w = tw(f, size, tok) end
        end
        if tok ~= '' then
          if w > maxw then
            for ch in tok:gmatch('[%z\1-\127\194-\244][\128-\191]*') do
              local cw = tw(f, size, ch)
              if x + cw > x0 + maxw and has then x, y = x0, y + lh end
              ops[#ops + 1] = { 't', x, y + dy, f, size, col, ch }
              x, has = x + cw, true
            end
          else
            if r.code and not mono then ops[#ops + 1] = { 'r', x, y + 1, x + w, y + lh - 1, C.codebg, 2 } end
            ops[#ops + 1] = { 't', x, y + dy, f, size, col, tok }
            if r.link then ops[#ops + 1] = { 'l', x, y + dy + size + 1, x + w, y + dy + size + 1, alpha(C.accent, 0x88), 1 } end
            x, has = x + w, true
          end
        end
      end
    end
  end
  return y + lh
end

local function parse_md(text)
  local blocks, lines = {}, {}
  for l in (text .. '\n'):gmatch('(.-)\r?\n') do lines[#lines + 1] = l end
  local function cells(row)
    row = row:gsub('^%s*|', ''):gsub('|%s*$', '')
    local c = {}
    for cell in (row .. '|'):gmatch('(.-)|') do c[#c + 1] = parse_inline((cell:gsub('^%s+', ''):gsub('%s+$', ''))) end
    return c
  end
  local i = 1
  while i <= #lines do
    local l = lines[i]
    if l:match('^%s*```') then
      local buf = {}
      i = i + 1
      while i <= #lines and not lines[i]:match('^%s*```%s*$') do buf[#buf + 1] = lines[i]; i = i + 1 end
      blocks[#blocks + 1] = { t = 'code', text = table.concat(buf, '\n') }
      i = i + 1
    elseif l:match('^%s*$') then
      blocks[#blocks + 1] = { t = 'blank' }; i = i + 1
    elseif l:match('^#+%s') then
      local h, rest = l:match('^(#+)%s+(.*)$')
      blocks[#blocks + 1] = { t = 'h', level = #h, runs = parse_inline(rest) }; i = i + 1
    elseif l:match('^%s*[-*_][-*_ ]*$') and select(2, l:gsub('[-*_]', '')) >= 3 then
      blocks[#blocks + 1] = { t = 'hr' }; i = i + 1
    elseif l:match('^%s*|') and lines[i + 1] and lines[i + 1]:match('^%s*|?%s*:?%-%-') then
      local head = cells(l)
      i = i + 2
      local rows = {}
      while i <= #lines and lines[i]:match('^%s*|') do rows[#rows + 1] = cells(lines[i]); i = i + 1 end
      blocks[#blocks + 1] = { t = 'table', head = head, rows = rows }
    elseif l:match('^%s*>') then
      blocks[#blocks + 1] = { t = 'quote', runs = parse_inline((l:gsub('^%s*>%s?', ''))) }; i = i + 1
    elseif l:match('^%s*[-*+]%s') then
      local ind, rest = l:match('^(%s*)[-*+]%s+(.*)$')
      blocks[#blocks + 1] = { t = 'li', level = #ind // 2, bullet = '•', runs = parse_inline(rest) }; i = i + 1
    elseif l:match('^%s*%d+[.)]%s') then
      local ind, num, rest = l:match('^(%s*)(%d+)[.)]%s+(.*)$')
      blocks[#blocks + 1] = { t = 'li', level = #ind // 2, bullet = num .. '.', runs = parse_inline(rest) }; i = i + 1
    else
      blocks[#blocks + 1] = { t = 'p', runs = parse_inline(l) }; i = i + 1
    end
  end
  return blocks
end

local function layout_table(ops, b, y, maxw, size)
  local ncol = #b.head
  for _, r in ipairs(b.rows) do ncol = math.max(ncol, #r) end
  local pad = 6
  local function natural(runs, bold)
    local w = 0
    for _, r in ipairs(runs) do
      local f = r.code and F.mono or ((bold or r.bold) and F.bold or F.reg)
      w = w + tw(f, size, (r.text:gsub('\n', ' ')))
    end
    return w
  end
  local nat = {}
  for c = 1, ncol do nat[c] = 24 end
  for c, cell in ipairs(b.head) do nat[c] = math.max(nat[c], natural(cell, true) + 2 * pad) end
  for _, row in ipairs(b.rows) do
    for c, cell in ipairs(row) do if c <= ncol then nat[c] = math.max(nat[c], natural(cell) + 2 * pad) end end
  end
  local total = 0
  for c = 1, ncol do total = total + nat[c] end
  local widths, sum = {}, 0
  for c = 1, ncol do
    widths[c] = total <= maxw and nat[c] or math.max(36, math.floor(nat[c] * maxw / total))
    sum = sum + widths[c]
  end
  local function row(cells, yy, header)
    local sub, h, x = {}, 0, 0
    for c = 1, ncol do
      local runs = cells[c] or {}
      if header then for _, r in ipairs(runs) do r.bold = true end end
      local yb = layout_inline(sub, runs, x + pad, yy + 3, widths[c] - 2 * pad, size, C.body)
      h = math.max(h, yb - yy + 3)
      x = x + widths[c]
    end
    if header then ops[#ops + 1] = { 'r', 0, yy, sum, yy + h, C.panel, 0 } end
    for _, o in ipairs(sub) do ops[#ops + 1] = o end
    ops[#ops + 1] = { 'l', 0, yy + h, sum, yy + h, C.rule, 1 }
    return yy + h
  end
  local y0 = y
  ops[#ops + 1] = { 'l', 0, y, sum, y, C.rule, 1 }
  y = row(b.head, y, true)
  for _, r in ipairs(b.rows) do y = row(r, y, false) end
  local x = 0
  for c = 0, ncol do
    ops[#ops + 1] = { 'l', x, y0, x, y, C.rule, 1 }
    if c < ncol then x = x + widths[c + 1] end
  end
  return y + 6
end

local function layout_md(text, maxw, size)
  local ops, y = {}, 0
  local lh = math.floor(size * 1.5 + 0.5)
  for _, b in ipairs(parse_md(text)) do
    if b.t == 'blank' then
      y = y + math.floor(lh * 0.45)
    elseif b.t == 'p' then
      y = layout_inline(ops, b.runs, 0, y, maxw, size, C.body)
    elseif b.t == 'h' then
      local hs = b.level == 1 and size + 5 or b.level == 2 and size + 3 or size + 1
      if y > 0 then y = y + math.floor(lh * 0.4) end
      for _, r in ipairs(b.runs) do if not r.code then r.bold = true end end
      y = layout_inline(ops, b.runs, 0, y, maxw, hs, C.text)
      if b.level <= 2 then ops[#ops + 1] = { 'l', 0, y + 1, maxw, y + 1, C.rule, 1 }; y = y + 5 end
    elseif b.t == 'li' then
      local ind = 2 + b.level * 16
      local bw = math.max(tw(F.reg, size, b.bullet) + 6, 14)
      ops[#ops + 1] = { 't', ind, y + math.floor((lh - size) / 2), F.reg, size, C.muted, b.bullet }
      y = layout_inline(ops, b.runs, ind + bw, y, maxw - ind - bw, size, C.body)
    elseif b.t == 'quote' then
      local y0 = y
      y = layout_inline(ops, b.runs, 12, y, maxw - 12, size, C.muted)
      ops[#ops + 1] = { 'r', 0, y0 + 2, 3, y - 2, alpha(C.accent, 0x99), 1 }
    elseif b.t == 'hr' then
      y = y + 6; ops[#ops + 1] = { 'l', 0, y, maxw, y, C.rule, 1 }; y = y + 7
    elseif b.t == 'code' then
      local pad, y0, sub = 8, y, {}
      y = layout_inline(sub, { { text = b.text } }, pad, y + pad, maxw - 2 * pad, size - 1, C.text, true)
      ops[#ops + 1] = { 'r', 0, y0, maxw, y + pad, C.code, 6 }
      for _, o in ipairs(sub) do ops[#ops + 1] = o end
      y = y + pad + 6
    elseif b.t == 'table' then
      y = layout_table(ops, b, y, maxw, size)
    end
  end
  return ops, y
end

local function draw_ops(ops, h, w)
  local x0, y0 = ImGui.GetCursorScreenPos(ctx)
  local _, wy = ImGui.GetWindowPos(ctx)
  local _, wh = ImGui.GetWindowSize(ctx)
  if y0 + h >= wy and y0 <= wy + wh then
    local dl = ImGui.GetWindowDrawList(ctx)
    local top, bot = wy - y0 - 40, wy + wh - y0
    for _, o in ipairs(ops) do
      local k = o[1]
      if k == 't' then
        if o[3] > top and o[3] < bot then ImGui.DrawList_AddTextEx(dl, o[4], o[5], x0 + o[2], y0 + o[3], o[6], o[7]) end
      elseif o[5] > top and o[3] < bot then
        if k == 'r' then ImGui.DrawList_AddRectFilled(dl, x0 + o[2], y0 + o[3], x0 + o[4], y0 + o[5], o[6], o[7])
        else ImGui.DrawList_AddLine(dl, x0 + o[2], y0 + o[3], x0 + o[4], y0 + o[5], o[6], o[7]) end
      end
    end
  end
  ImGui.Dummy(ctx, w, math.max(1, h))
end

local function md(holder, text)
  local w = math.floor(ImGui.GetContentRegionAvail(ctx))
  local L = holder._md
  if not L or L.w ~= w or L.text ~= text or L.pal ~= pal_ver then
    local ops, h = layout_md(text, w, FONT_SIZE)
    L = { w = w, text = text, pal = pal_ver, ops = ops, h = h }
    holder._md = L
  end
  draw_ops(L.ops, L.h, w)
  if ImGui.IsItemClicked(ctx, 1) then ImGui.SetClipboardText(ctx, text) end
end

local function bubble(holder, text)
  local w = math.floor(ImGui.GetContentRegionAvail(ctx))
  local L = holder._bubble
  if not L or L.w ~= w or L.text ~= text or L.pal ~= pal_ver then
    local pad, sub = 8, {}
    local y = layout_inline(sub, { { text = text } }, pad, pad, w - 2 * pad, FONT_SIZE, C.text)
    local ops = { { 'r', 0, 0, w, y + pad, C.user, 8 } }
    for _, o in ipairs(sub) do ops[#ops + 1] = o end
    L = { w = w, text = text, pal = pal_ver, ops = ops, h = y + pad }
    holder._bubble = L
  end
  draw_ops(L.ops, L.h, w)
  if ImGui.IsItemClicked(ctx, 1) then ImGui.SetClipboardText(ctx, text) end
end

local function tools_line(tools)
  if not tools or #tools == 0 then return end
  local names = {}
  for _, n in ipairs(tools) do names[#names + 1] = tool_label(n) end
  ImGui.PushTextWrapPos(ctx, 0)
  ImGui.TextDisabled(ctx, '→ ' .. table.concat(names, ' · '))
  ImGui.PopTextWrapPos(ctx)
end

---------------------------------------------------------------- UI
local function truncate(str, w)
  if ImGui.CalcTextSize(ctx, str) <= w then return str end
  local n = utf8.len(str) or #str
  while n > 1 do
    n = n - 1
    local cut = str:sub(1, (utf8.offset(str, n + 1) or (#str + 1)) - 1) .. '…'
    if ImGui.CalcTextSize(ctx, cut) <= w then return cut end
  end
  return '…'
end

local function flat_push(col)
  ImGui.PushStyleColor(ctx, ImGui.Col_Button, 0)
  ImGui.PushStyleColor(ctx, ImGui.Col_ButtonHovered, C.hover)
  ImGui.PushStyleColor(ctx, ImGui.Col_ButtonActive, C.hover)
  ImGui.PushStyleColor(ctx, ImGui.Col_Text, col or C.muted)
  ImGui.PushStyleVar(ctx, ImGui.StyleVar_FramePadding, 6, 3)
end
local function flat_pop()
  ImGui.PopStyleVar(ctx)
  ImGui.PopStyleColor(ctx, 4)
end
local function chevron(x2, y1, y2, col)
  local cx, cy = x2 - 11, (y1 + y2) / 2 + 1
  ImGui.DrawList_AddTriangleFilled(ImGui.GetWindowDrawList(ctx), cx - 3.5, cy - 2, cx + 3.5, cy - 2, cx, cy + 2.5, col)
end
local function dot(col, x, y) ImGui.DrawList_AddCircleFilled(ImGui.GetWindowDrawList(ctx), x, y, 3.5, col) end

-- frameless muted button; chevron=false for toggles
local function chip(id, label, col, with_chevron)
  flat_push(col)
  local clicked = ImGui.Button(ctx, label .. (with_chevron == false and '' or '     ') .. '##chip_' .. id)
  flat_pop()
  if with_chevron ~= false then
    local _, y1 = ImGui.GetItemRectMin(ctx)
    local x2, y2 = ImGui.GetItemRectMax(ctx)
    chevron(x2, y1, y2, col or C.muted)
  end
  return clicked
end

local function icon_button(id, kind, col, tip)
  local sz = ImGui.GetFrameHeight(ctx)
  local clicked = ImGui.InvisibleButton(ctx, '##icon_' .. id, sz, sz)
  local x1, y1 = ImGui.GetItemRectMin(ctx)
  local dl = ImGui.GetWindowDrawList(ctx)
  if ImGui.IsItemHovered(ctx) then
    ImGui.DrawList_AddRectFilled(dl, x1, y1, x1 + sz, y1 + sz, C.hover, 6)
    if tip then ImGui.SetTooltip(ctx, tip) end
  end
  local cx, cy = x1 + sz / 2, y1 + sz / 2
  col = col or C.muted
  if kind == 'clock' then
    ImGui.DrawList_AddCircle(dl, cx, cy, 6.5, col, 0, 1.5)
    ImGui.DrawList_AddLine(dl, cx, cy + 0.5, cx, cy - 3.5, col, 1.5)
    ImGui.DrawList_AddLine(dl, cx, cy + 0.5, cx + 3, cy + 2, col, 1.5)
  elseif kind == 'plus' then
    ImGui.DrawList_AddLine(dl, cx - 5.5, cy, cx + 5.5, cy, col, 1.6)
    ImGui.DrawList_AddLine(dl, cx, cy - 5.5, cx, cy + 5.5, col, 1.6)
  else
    ImGui.DrawList_AddRectFilled(dl, cx - 4.5, cy - 4.5, cx + 4.5, cy + 4.5, col, 2)
  end
  return clicked
end

local function agent_dot(a)
  if not a.acp then return C.ok end
  local st = S.acp_status[a.slug]
  if not st then return C.muted end
  if st.connected then return C.ok end
  if st.state == 'starting' or st.state == 'authenticating' then return C.warn end
  return C.muted
end

local NO_CLOSE = ImGui.SelectableFlags_NoAutoClosePopups or ImGui.SelectableFlags_DontClosePopups or 0

-- one picker for every backend: Gamut agents and local ACP agents, each with a status dot and Connect
local function agent_picker()
  local cur
  for _, a in ipairs(S.agents) do if a.slug == S.agent then cur = a end end
  local name = S.agent == '' and 'Choose an agent' or (S.agent_name ~= '' and S.agent_name or S.agent)
  flat_push(C.text)
  local clicked = ImGui.Button(ctx, '    ' .. name .. '     ##agentpick')
  flat_pop()
  local x1, y1 = ImGui.GetItemRectMin(ctx)
  local x2, y2 = ImGui.GetItemRectMax(ctx)
  dot(cur and agent_dot(cur) or C.muted, x1 + 11, (y1 + y2) / 2)
  chevron(x2, y1, y2, C.muted)
  if clicked then
    ImGui.OpenPopup(ctx, 'agentpop')
    load_agents()
    for _, a in ipairs(S.agents) do if a.acp then load_acp_status(a.slug, true) end end
    S.agentpop_at = reaper.time_precise()
  end
  ImGui.SetNextWindowSizeConstraints(ctx, 250, 0, 420, 480)
  if ImGui.BeginPopup(ctx, 'agentpop') then
    if reaper.time_precise() - (S.agentpop_at or 0) > 2 then
      S.agentpop_at = reaper.time_precise()
      for _, a in ipairs(S.agents) do if a.acp then load_acp_status(a.slug, false) end end
    end
    local function section(title, acp)
      local first = true
      for _, a in ipairs(S.agents) do
        if (a.acp == true) == acp then
          if first then ImGui.TextDisabled(ctx, title); first = false end
          local st = a.acp and S.acp_status[a.slug]
          local need = st and not st.connected and st.state == 'auth_required'
          local busy = st and not st.connected and (st.state == 'starting' or st.state == 'authenticating')
          local tag = need and 'Connect' or (busy and 'checking…' or nil)
          local tw_ = tag and (ImGui.CalcTextSize(ctx, tag) + 18) or 0
          if ImGui.Selectable(ctx, '    ' .. a.name .. '##' .. a.slug, a.slug == S.agent, 0,
              ImGui.GetContentRegionAvail(ctx) - tw_, 0) then
            choose_agent(a.slug, a.name)
          end
          if a.desc ~= '' and ImGui.IsItemHovered(ctx) then ImGui.SetTooltip(ctx, a.desc) end
          local sx1, sy1 = ImGui.GetItemRectMin(ctx)
          local _, sy2 = ImGui.GetItemRectMax(ctx)
          dot(agent_dot(a), sx1 + 8, (sy1 + sy2) / 2)
          if need then
            ImGui.SameLine(ctx)
            if ImGui.SmallButton(ctx, 'Connect##c' .. a.slug) then
              choose_agent(a.slug, a.name)
              S.auth = { methods = st.methods }
              ImGui.CloseCurrentPopup(ctx)
            end
          elseif busy then
            ImGui.SameLine(ctx)
            ImGui.TextDisabled(ctx, tag)
          end
        end
      end
      if not first then ImGui.Spacing(ctx) end
    end
    section('Gamut', false)
    section('On this Mac', true)
    if #S.agents == 0 then ImGui.TextDisabled(ctx, S.agents_err or 'loading…') end
    ImGui.EndPopup(ctx)
  end
end

local function history_popup()
  ImGui.SetNextWindowSizeConstraints(ctx, 260, 0, 460, 480)
  if ImGui.BeginPopup(ctx, 'histpop') then
    if reaper.time_precise() - S.sess_at > 2 then load_sessions() end
    ImGui.TextDisabled(ctx, 'Recent chats')
    local dl = ImGui.GetWindowDrawList(ctx)
    for _, x in ipairs(S.sessions) do
      local aw = ImGui.CalcTextSize(ctx, x.age)
      local label = truncate((x.active and '● ' or '') .. x.name, ImGui.GetContentRegionAvail(ctx) - aw - 16)
      if ImGui.Selectable(ctx, label .. '##' .. x.id, x.id == S.sid) then switch_session(x.id, x.name) end
      local _, sy1 = ImGui.GetItemRectMin(ctx)
      local sx2 = ImGui.GetItemRectMax(ctx)
      ImGui.DrawList_AddText(dl, sx2 - aw - 4, sy1, C.muted, x.age)
    end
    if #S.sessions == 0 then ImGui.TextDisabled(ctx, 'No chats yet') end
    ImGui.EndPopup(ctx)
  end
end

local function header()
  agent_picker()
  if S.agent == '' then return end
  local sz = ImGui.GetFrameHeight(ctx)
  local n = S.active and 3 or 2
  local right = n * sz + (n - 1) * 2
  ImGui.SameLine(ctx)
  local title = S.sid == '' and 'New chat' or (S.title ~= '' and S.title or 'Chat')
  local room = ImGui.GetContentRegionAvail(ctx) - right - 8
  if room > 40 then
    ImGui.AlignTextToFramePadding(ctx)
    ImGui.TextDisabled(ctx, truncate(title, room))
    if ImGui.IsItemHovered(ctx) then ImGui.SetTooltip(ctx, title) end
    ImGui.SameLine(ctx)
  end
  ImGui.SetCursorPosX(ctx, ImGui.GetCursorPosX(ctx) + math.max(0, ImGui.GetContentRegionAvail(ctx) - right))
  if S.active then
    if icon_button('stop', 'stop', C.danger, 'Stop') then stop() end
    ImGui.SameLine(ctx, 0, 2)
  end
  if icon_button('hist', 'clock', nil, 'Recent chats') then
    load_sessions()
    ImGui.OpenPopup(ctx, 'histpop')
  end
  ImGui.SameLine(ctx, 0, 2)
  if icon_button('new', 'plus', nil, 'New chat') then new_chat() end
  history_popup()
end

local function opt_items(o) -- ACP options may be grouped: {group, name, options = {...}}
  local out = {}
  for _, x in ipairs(type(o.options) == 'table' and o.options or {}) do
    if type(x.options) == 'table' then
      for _, y in ipairs(x.options) do out[#out + 1] = { value = sval(y.value) or '', name = sval(y.name), group = sval(x.name) } end
    else
      out[#out + 1] = { value = sval(x.value) or '', name = sval(x.name), group = sval(x.group) }
    end
  end
  return out
end

-- one chip for model + effort (+ mode); the popup has a section for each
local function model_chip()
  local model, effort, mode
  for _, o in ipairs(S.cfg_opts) do
    if type(o) == 'table' and o.type == 'select' then
      if o.category == 'model' and not model then model = o
      elseif (o.category == 'thought_level' or o.category == 'model_config') and not effort then effort = o
      elseif o.category == 'mode' and not mode then mode = o end
    end
  end
  if not (model or effort or mode) then return end
  local function cur_of(o) local id = sval(o.id) or o.category; return id, S.cfg_sel[id] or sval(o.currentValue) or '' end
  local function groups_of(items)
    local g, n = {}, 0
    for _, it in ipairs(items) do if it.group and it.group ~= '' and not g[it.group] then g[it.group] = true; n = n + 1 end end
    return n
  end
  local parts = {}
  if model then
    local _, v = cur_of(model)
    local items = opt_items(model)
    local multi = groups_of(items) > 1
    local label = 'Auto'
    for _, it in ipairs(items) do
      if it.value == v and v ~= '' then label = (multi and it.group and it.group ~= '') and (it.group .. ' · ' .. (it.name or v)) or (it.name or v) end
    end
    parts[#parts + 1] = label
  end
  if effort then
    local _, v = cur_of(effort)
    for _, it in ipairs(opt_items(effort)) do if it.value == v and v ~= '' then parts[#parts + 1] = it.name or v end end
  end
  if #parts == 0 and mode then
    local _, v = cur_of(mode)
    for _, it in ipairs(opt_items(mode)) do if it.value == v then parts[#parts + 1] = it.name or v end end
  end
  if chip('model', table.concat(parts, ' · '), C.muted) then ImGui.OpenPopup(ctx, 'modelpop') end
  if ImGui.IsItemHovered(ctx) then ImGui.SetTooltip(ctx, 'Model and effort for the next message') end
  ImGui.SetNextWindowSizeConstraints(ctx, 240, 0, 380, 520)
  if ImGui.BeginPopup(ctx, 'modelpop') then
    local function pick(id, value)
      S.cfg_sel[id] = value
      cfg_save()
    end
    local function vertical(o, title)
      local id, cur = cur_of(o)
      local items = opt_items(o)
      local multi = groups_of(items) > 1
      ImGui.TextDisabled(ctx, title)
      local group
      for _, it in ipairs(items) do
        if multi and it.group and it.group ~= group then
          group = it.group
          ImGui.TextColored(ctx, C.muted, '  ' .. group)
        end
        local on = it.value == cur
        if on then ImGui.PushStyleColor(ctx, ImGui.Col_Text, C.accent) end
        if ImGui.Selectable(ctx, '    ' .. (it.name or it.value) .. '##' .. id .. it.value, on, NO_CLOSE) then pick(id, it.value) end
        if on then ImGui.PopStyleColor(ctx) end
      end
    end
    local sections = 0
    if model then vertical(model, 'Model'); sections = sections + 1 end
    if effort then
      if sections > 0 then ImGui.Separator(ctx) end
      local id, cur = cur_of(effort)
      ImGui.TextDisabled(ctx, 'Effort')
      for k, it in ipairs(opt_items(effort)) do
        if k > 1 then ImGui.SameLine(ctx, 0, 4) end
        local on = it.value == cur
        local label = it.name or it.value
        if on then ImGui.PushStyleColor(ctx, ImGui.Col_Text, C.accent) end
        if ImGui.Selectable(ctx, label .. '##eff' .. it.value, on, NO_CLOSE, ImGui.CalcTextSize(ctx, label) + 12, 0) then
          pick(id, it.value)
        end
        if on then ImGui.PopStyleColor(ctx) end
      end
      sections = sections + 1
    end
    if mode then
      if sections > 0 then ImGui.Separator(ctx) end
      vertical(mode, 'Mode')
    end
    ImGui.EndPopup(ctx)
  end
end

local function context_chip()
  local on = S.attach
  if chip('ctx', '◆ REAPER', on and C.accent or C.muted, false) then S.attach = not S.attach end
  if ImGui.IsItemHovered(ctx) then
    ImGui.SetTooltip(ctx, on and ('Sent with every message: ' .. chip_text() .. '\nClick to turn off')
      or 'REAPER context is off. Click to attach it')
  end
end

-- cards above the input: things that need you (tool permission, login, a question waiting in Gamut)
local CARD_H = 66
local function card_list()
  local L = {}
  for _, p in ipairs(S.perms) do L[#L + 1] = { kind = 'perm', p = p } end
  if S.auth and is_acp() then L[#L + 1] = { kind = 'auth' } end
  if S.waiting then L[#L + 1] = { kind = 'wait' } end
  return L
end
local function primary(label)
  ImGui.PushStyleColor(ctx, ImGui.Col_Button, C.accent)
  ImGui.PushStyleColor(ctx, ImGui.Col_ButtonHovered, alpha(C.accent, 0xDD))
  ImGui.PushStyleColor(ctx, ImGui.Col_ButtonActive, alpha(C.accent, 0xBB))
  ImGui.PushStyleColor(ctx, ImGui.Col_Text, 0xFFFFFFFF)
  local clicked = ImGui.Button(ctx, label)
  ImGui.PopStyleColor(ctx, 4)
  return clicked
end
local CHILD_BORDERS = ImGui.ChildFlags_Borders or ImGui.ChildFlags_Border or 1
local function card(c, i)
  ImGui.PushStyleColor(ctx, ImGui.Col_ChildBg, C.panel)
  ImGui.PushStyleColor(ctx, ImGui.Col_Border, C.border)
  if ImGui.BeginChild(ctx, 'card' .. i, 0, CARD_H, CHILD_BORDERS, ImGui.WindowFlags_NoScrollbar) then
    local who = S.agent_name ~= '' and S.agent_name or S.agent
    local w = ImGui.GetContentRegionAvail(ctx)
    if c.kind == 'perm' then
      local line = who .. ' wants to: ' .. c.p.title
      ImGui.Text(ctx, truncate(line, w))
      if ImGui.IsItemHovered(ctx) then ImGui.SetTooltip(ctx, line) end
      local primed = false
      for k, o in ipairs(c.p.options) do
        if k > 1 then ImGui.SameLine(ctx) end
        local label = (sval(o.name) or sval(o.optionId) or '?') .. '##' .. c.p.id .. k
        local allow = (sval(o.kind) or ''):match('^allow')
        local clicked
        if allow and not primed then primed = true; clicked = primary(label) else clicked = ImGui.Button(ctx, label) end
        if clicked then answer_perm(c.p, sval(o.optionId)) end
      end
      if #c.p.options == 0 and ImGui.Button(ctx, 'Dismiss##' .. c.p.id) then answer_perm(c.p, nil) end
    elseif c.kind == 'auth' then
      if S.auth.waiting then
        ImGui.Text(ctx, truncate('Finish the ' .. who .. ' login in your browser', w))
        ImGui.TextDisabled(ctx, 'This card closes by itself once you are in.')
      else
        ImGui.Text(ctx, truncate(who .. ' is not connected yet', w))
        for k, m in ipairs(S.auth.methods) do
          if k > 1 then ImGui.SameLine(ctx) end
          local label = (k == 1 and 'Connect with ' or '') .. (sval(m.name) or sval(m.id) or '?') .. '##auth' .. k
          local clicked = (k == 1) and primary(label) or ImGui.Button(ctx, label)
          if clicked then start_login(sval(m.id)) end
          if sval(m.description) and ImGui.IsItemHovered(ctx) then ImGui.SetTooltip(ctx, m.description) end
        end
        if #S.auth.methods == 0 then ImGui.TextDisabled(ctx, 'Checking…') end
      end
      if S.auth.err then
        ImGui.SameLine(ctx)
        ImGui.TextColored(ctx, C.danger, truncate(S.auth.err, ImGui.GetContentRegionAvail(ctx)))
      end
    else
      ImGui.Text(ctx, truncate(who .. ' is asking you something', w))
      if primary('Answer in Gamut') then open_in_gamut() end
    end
    ImGui.EndChild(ctx)
  end
  ImGui.PopStyleColor(ctx, 2)
end

local spin = { '·  ', '·· ', '···', ' ··', '  ·', '   ' }
local DOCK_FLAG = 'docked_v2'

local perf = { n = 0, sum = 0, max = 0, gap = 0, last = reaper.time_precise(), at = reaper.time_precise() }
local function perf_tick(t0)
  local t1 = reaper.time_precise()
  local dt = t1 - t0
  perf.n, perf.sum = perf.n + 1, perf.sum + dt
  if dt > perf.max then perf.max = dt end
  if t0 - perf.last > perf.gap then perf.gap = t0 - perf.last end
  perf.last = t0
  if t1 - perf.at >= 1 then
    reaper.SetExtState(EXT, 'perf', ('fps=%.1f lua_avg_ms=%.2f lua_max_ms=%.2f max_gap_ms=%.1f'):format(
      perf.n / (t1 - perf.at), perf.sum / perf.n * 1000, perf.max * 1000, perf.gap * 1000), false)
    perf.n, perf.sum, perf.max, perf.gap, perf.at = 0, 0, 0, 0, t1
  end
end

-- Port of Gamut's follow engine (SuperAgent use-message-list-scroll.ts): following pins the live edge as content
-- grows; an upward wheel escapes at once; coming back within ATTACH_PX re-engages. Motion is a time-based
-- exponential chase so it is frame-rate independent at REAPER's ~33 fps defer.
local ATTACH_PX, SNAP_GAP_PX, CHASE_RATE = 70, 120, 30
local function scroll_engine()
  local now = reaper.time_precise()
  local dt = math.min(0.064, now - (S.sc_t or now))
  S.sc_t = now
  local cur, maxy = ImGui.GetScrollY(ctx), ImGui.GetScrollMaxY(ctx)
  if S.follow == nil then S.follow = true end
  if not S.sy_target or math.abs(cur - (S.sy_last or cur)) > 0.5 then
    S.sy_target = cur
    if S.sy_last then S.follow = maxy - cur <= ATTACH_PX end
  end
  if S.scroll then S.follow, S.trip, S.scroll = true, true, false end
  local wheel = ImGui.IsWindowHovered(ctx) and ImGui.GetMouseWheel(ctx) or 0
  if wheel ~= 0 then
    S.trip = false
    S.sy_target = math.max(0, math.min(maxy, S.sy_target - wheel * FONT_SIZE * 5))
    if wheel > 0 then S.follow = false elseif maxy - S.sy_target <= ATTACH_PX then S.follow = true end
  end
  if S.follow then
    S.sy_target = maxy
    if not S.trip and wheel == 0 and maxy - cur > SNAP_GAP_PX then cur = maxy end
  end
  local d = S.sy_target - cur
  local ny = cur
  if math.abs(d) > 0.5 then ny = cur + d * (1 - math.exp(-dt * CHASE_RATE)) end
  if math.abs(S.sy_target - ny) <= 1 then ny = S.sy_target; S.trip = false end
  if ny ~= ImGui.GetScrollY(ctx) then ImGui.SetScrollY(ctx, ny) end
  S.sy_last = ny
end

local perf = { n = 0, sum = 0, max = 0, gap = 0, last = reaper.time_precise(), at = reaper.time_precise() }
local function perf_tick(t0)
  local t1 = reaper.time_precise()
  local dt = t1 - t0
  perf.n, perf.sum = perf.n + 1, perf.sum + dt
  if dt > perf.max then perf.max = dt end
  if t0 - perf.last > perf.gap then perf.gap = t0 - perf.last end
  perf.last = t0
  if t1 - perf.at >= 1 then
    reaper.SetExtState(EXT, 'perf', ('fps=%.1f lua_avg_ms=%.2f lua_max_ms=%.2f max_gap_ms=%.1f'):format(
      perf.n / (t1 - perf.at), perf.sum / perf.n * 1000, perf.max * 1000, perf.gap * 1000), false)
    perf.n, perf.sum, perf.max, perf.gap, perf.at = 0, 0, 0, 0, t1
  end
end

-- REAPER's defer caps ReaImGui at ~33 fps, so ImGui's 5-line wheel jumps look choppy; ease toward a target instead
local function smooth_scroll()
  local cur, maxy = ImGui.GetScrollY(ctx), ImGui.GetScrollMaxY(ctx)
  if not S.sy_target or math.abs(cur - (S.sy_last or cur)) > 0.5 then S.sy_target = cur end
  local wheel = ImGui.IsWindowHovered(ctx) and ImGui.GetMouseWheel(ctx) or 0
  if wheel ~= 0 then
    S.sy_target = math.max(0, math.min(maxy, S.sy_target - wheel * FONT_SIZE * 5))
  end
  local d = S.sy_target - cur
  if math.abs(d) > 0.5 then
    local ny = cur + d * 0.45
    ImGui.SetScrollY(ctx, ny)
    S.sy_last = ny
  else
    S.sy_last = cur
  end
end

local IME_NAV = { 'Key_LeftArrow', 'Key_RightArrow', 'Key_UpArrow', 'Key_DownArrow', 'Key_Home', 'Key_End',
  'Key_PageUp', 'Key_PageDown', 'Key_Delete' }
local function pressed(name) return ImGui.IsKeyPressed(ctx, ImGui[name]) end
-- returns 'enter' (IME commit, don't send), 'restore' (undo ImGui's edit) or nil; tracks S.ime = typed pinyin
local function ime_scan()
  if not S.comp_active then S.ime = ''; return nil end
  local chars = 0
  while ImGui.GetInputQueueCharacter(ctx, chars) do chars = chars + 1 end
  local act
  if #S.ime > 0 then
    if pressed('Key_Enter') or pressed('Key_KeypadEnter') then act = 'enter'; S.ime = ''
    elseif pressed('Key_Escape') then act = 'restore'; S.ime = ''
    elseif pressed('Key_Backspace') then act = 'restore'; S.ime = S.ime:sub(1, -2)
    else
      for _, k in ipairs(IME_NAV) do if pressed(k) then act = 'restore'; break end end
    end
    if chars > 0 then S.ime = '' end
  end
  local mods = ImGui.GetKeyMods(ctx)
  if chars == 0 and mods & (ImGui.Mod_Ctrl | ImGui.Mod_Super | ImGui.Mod_Alt) == 0 then
    for i = 0, 25 do
      if ImGui.IsKeyPressed(ctx, ImGui.Key_A + i, false) then S.ime = S.ime .. string.char(97 + i) end
    end
    if #S.ime > 0 and ImGui.IsKeyPressed(ctx, ImGui.Key_Apostrophe, false) then S.ime = S.ime .. "'" end
  end
  return act
end

local function frame()
  local t0 = reaper.time_precise()
  if S.auth and (S.auth.waiting or #S.auth.methods == 0) and t0 - (S.auth_at or 0) > 2 then load_status() end
  check_theme()
  poll_http()
  poll_stream()
  ImGui.PushFont(ctx, F.reg, FONT_SIZE)
  for _, c in ipairs(COLORS) do ImGui.PushStyleColor(ctx, c[1], c[2]) end
  for _, v in ipairs(VARS) do ImGui.PushStyleVar(ctx, v[1], v[2], v[3]) end
  if reaper.GetExtState(EXT, DOCK_FLAG) ~= '1' then
    ImGui.SetNextWindowDockID(ctx, -2) -- REAPER docker #2 (right side)
    reaper.SetExtState(EXT, DOCK_FLAG, '1', true)
  end
  ImGui.SetNextWindowSize(ctx, 420, 680, ImGui.Cond_FirstUseEver)
  local visible, open = ImGui.Begin(ctx, 'Gamut', true, ImGui.WindowFlags_NoCollapse)
  -- with the panel focused REAPER never sees the key, so Cmd+L has to close it from inside (Cmd = Mod_Ctrl on macOS)
  if visible and ImGui.IsWindowFocused(ctx, ImGui.FocusedFlags_RootAndChildWindows) and
      (ImGui.IsKeyChordPressed(ctx, ImGui.Mod_Ctrl | ImGui.Key_L) or ImGui.IsKeyChordPressed(ctx, ImGui.Mod_Super | ImGui.Key_L)) then
    open = false
  end
  if visible then
    header()
    ImGui.Separator(ctx)

    if S.agent == '' then
      ImGui.Dummy(ctx, 0, 40)
      ImGui.PushTextWrapPos(ctx, 0)
      ImGui.TextDisabled(ctx, 'Pick an agent at the top left to start.')
      ImGui.PopTextWrapPos(ctx)
    else
      local cards = card_list()
      local composer_h = 98 + #cards * (CARD_H + 6)
      if ImGui.BeginChild(ctx, 'log', 0, -composer_h, 0, ImGui.WindowFlags_NoScrollWithMouse) then
        scroll_engine()
        for _, m in ipairs(S.msgs) do
          if m.role == 'user' then
            local body, had = split_context(m.text)
            if had then ImGui.TextDisabled(ctx, '◆ REAPER context') end
            bubble(m, body)
            ImGui.Spacing(ctx)
          else
            if m.text ~= '' then md(m, m.text) end
            tools_line(m.tools)
            ImGui.Spacing(ctx)
          end
        end
        for _, b in ipairs(S.live) do
          if b.kind == 'text' then md(b, b.text) else ImGui.TextDisabled(ctx, '→ ' .. b.text) end
        end
        if S.active and not S.waiting then
          local s = spin[(math.floor(reaper.time_precise() * 6) % #spin) + 1]
          ImGui.TextColored(ctx, C.accent, 'Working ' .. s)
        end
        if S.err then ImGui.TextColored(ctx, C.danger, S.err) end
        ImGui.EndChild(ctx)
      end

      for i, c in ipairs(cards) do card(c, i) end

      if S.focus then ImGui.SetKeyboardFocusHere(ctx); S.focus = false end
      local ime = ime_scan()
      local flags = ImGui.InputTextFlags_CallbackAlways
      if ime == 'enter' then
        -- plain multiline flags: Enter becomes a newline after the committed letters, which the callback strips
        ImGui.Function_SetValue(ime_cb, 'strip_nl', 1)
      else
        flags = flags | ImGui.InputTextFlags_EnterReturnsTrue
        -- Dear ImGui has no Shift+Enter option: dropping CtrlEnterForNewLine while Shift is held makes Enter a newline
        if not ImGui.IsKeyDown(ctx, ImGui.Mod_Shift) then flags = flags | ImGui.InputTextFlags_CtrlEnterForNewLine end
        if ime == 'restore' then ImGui.Function_SetValue(ime_cb, 'restore', 1) end
      end
      local before = S.input
      local entered
      entered, S.input = ImGui.InputTextMultiline(ctx, '##input', S.input, -1, 62, flags, ime_cb)
      local x1, y1 = ImGui.GetItemRectMin(ctx)
      local x2, y2 = ImGui.GetItemRectMax(ctx)
      local focused = ImGui.IsItemActive(ctx)
      if ime == 'restore' and not focused then S.input = before; S.focus = true; focused = true end -- Esc deactivated it
      S.comp_active = focused
      ImGui.DrawList_AddRect(ImGui.GetWindowDrawList(ctx), x1, y1, x2, y2,
        focused and C.accent or C.border, 6, 0, focused and 2 or 1)
      if entered then send() end
      context_chip()
      ImGui.SameLine(ctx, 0, 2)
      model_chip()
      local hint = (S.ime and #S.ime > 0) and ('pinyin: ' .. S.ime) or 'Enter to send'
      local hw = ImGui.CalcTextSize(ctx, hint)
      ImGui.SameLine(ctx)
      local gap = ImGui.GetContentRegionAvail(ctx) - hw
      if gap > 0 then ImGui.SetCursorPosX(ctx, ImGui.GetCursorPosX(ctx) + gap) end
      ImGui.AlignTextToFramePadding(ctx)
      if S.ime and #S.ime > 0 then ImGui.TextColored(ctx, C.accent, hint) else ImGui.TextDisabled(ctx, hint) end
      if ImGui.IsItemHovered(ctx) then
        ImGui.SetTooltip(ctx, 'Enter send · Shift+Enter newline · Cmd+L toggle · right-click a message to copy')
      end
    end
    ImGui.End(ctx)
  end
  ImGui.PopStyleVar(ctx, #VARS)
  ImGui.PopStyleColor(ctx, #COLORS)
  ImGui.PopFont(ctx)
  perf_tick(t0)
  if open then reaper.defer(frame) end
end

-- remember our own action id so gamut-chat/restart_apply.sh can bind Cmd+L on any machine
do
  local _, _, _, cmd = reaper.get_action_context()
  local named = reaper.ReverseNamedCommandLookup(cmd)
  if named then reaper.SetExtState(EXT, 'action', '_' .. named, true) end
end
if reaper.set_action_options then reaper.set_action_options(1 | 4) end
reaper.atexit(function()
  stream_stop()
  if reaper.set_action_options then reaper.set_action_options(8) end
end)
if S.agent ~= '' then cfg_load() end
S.boot = true
load_agents()
reaper.defer(frame)
