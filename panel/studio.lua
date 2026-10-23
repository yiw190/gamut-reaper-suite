-- Hibiki studio: what the panel adds around the chat.
--   receipts  each reply's REAPER changes (the bridge labels its undo points "Hibiki <turn>: …" and lists them in
--             ExtState Hibiki/receipts), shown as a card that reverts or redoes exactly that reply
--   meter     Hibiki_Meter.jsfx on the master's monitoring FX writes 100 ms frames to gmem "Hibiki" while REAPER
--             plays; the last playback becomes a few context lines (loudness, true peak, bands, stereo, per bar)
--   rack      audition variants tagged by the agent (track P_EXT:hibiki_var = "group/n", or P_EXT:hibiki_fxvar
--             lines "group/n<TAB>{fx guid}"), switched with numbered buttons or keys 1-9
--   ears      renders the time selection / razor area, measures it (REAPER + `hibiki ears`) and attaches it
-- Hibiki.lua loads this file with dofile and calls the returned function once with its helpers.
return function(P)
local ImGui, ctx, S, C, EXT = P.ImGui, P.ctx, P.S, P.C, P.EXT
local M = {}
local function now() return reaper.time_precise() end
local function log10(x) return math.log(x, 10) end
local function bar_beat(t)
  local beats, measures = reaper.TimeMap2_timeToBeats(0, t)
  return ('%d.%d'):format((measures or 0) + 1, math.floor(beats or 0) + 1)
end
local function bar_of(t)
  local _, measures = reaper.TimeMap2_timeToBeats(0, t)
  return (measures or 0) + 1
end
local function exists(path)
  local f = io.open(path, 'rb')
  if f then f:close() end
  return f ~= nil
end
local function plural(n, word) return ('%d %s%s'):format(n, word, n == 1 and '' or 's') end

local note = nil -- { text, till }: a short line in the composer's hint spot
function M.note(text) note = { text = text, till = now() + 6 } end
function M.hint() return note and now() < note.till and note.text or nil end

---------------------------------------------------------------- receipts
local R = { seq = nil, list = {}, n = 0, current = nil, ended = nil, gone = {}, state = {} }
local RUN_ID = ('%x'):format(math.floor(now() * 1000) % 0xFFFFF)
reaper.SetExtState(EXT, 'turn', '', false) -- a panel that died mid-reply must not tag later changes as its own

-- queued: a message sent while a reply runs; it keeps that reply's id until the reply ends
function M.turn_start(queued)
  if queued and R.current then return end
  R.n = R.n + 1
  R.current = RUN_ID .. '.' .. R.n
  reaper.SetExtState(EXT, 'turn', R.current, false)
end
function M.turn_end()
  if R.current then R.ended = R.current end
  R.current = nil
  reaper.SetExtState(EXT, 'turn', '', false)
end

local function read_receipts()
  local seq = reaper.GetExtState(EXT, 'receipts_seq')
  if seq == R.seq then return end
  R.seq = seq
  local list = P.decode(reaper.GetExtState(EXT, 'receipts'))
  R.list = type(list) == 'table' and list or {}
end

local function receipt_shown()
  if S.active then return nil end
  local r = R.list[#R.list]
  if type(r) ~= 'table' or type(r.id) ~= 'string' or R.gone[r.id] then return nil end
  if r.id == R.ended then return r end
  -- changes an agent made outside this panel (the Gamut app, a CLI) show once they have settled
  if r.id:match('^mcp%-') and os.time() - (tonumber(r.at) or 0) >= 3 then return r end
end

local function prefix(id) return 'Hibiki ' .. id .. ': ' end
-- undo from the top while the top undo point is this reply's; stops at anything else (the user's own edits)
local function revert(r)
  local pre, n = prefix(r.id), 0
  reaper.PreventUIRefresh(1)
  while n < 1000 do
    local top = reaper.Undo_CanUndo2(0)
    if not top or top:sub(1, #pre) ~= pre then break end
    reaper.Undo_DoUndo2(0)
    n = n + 1
  end
  reaper.PreventUIRefresh(-1)
  reaper.UpdateArrange()
  local st = { undone = n }
  if n == 0 then st.blocked = reaper.Undo_CanUndo2(0) or ''
  else
    st.reverted = true
    local points = tonumber(r.points) or n
    if n < points then st.partial = points - n end
  end
  R.state[r.id] = st
end
local function redo(r)
  local st = R.state[r.id] or {}
  local pre, n = prefix(r.id), 0
  reaper.PreventUIRefresh(1)
  while n < (st.undone or 0) do
    local top = reaper.Undo_CanRedo2(0)
    if not top or top:sub(1, #pre) ~= pre then break end
    reaper.Undo_DoRedo2(0)
    n = n + 1
  end
  reaper.PreventUIRefresh(-1)
  reaper.UpdateArrange()
  R.state[r.id] = n > 0 and {} or { redo_failed = true }
end

local function receipt_card(r)
  local st = R.state[r.id] or {}
  local n = tonumber(r.n) or 0
  local lines = type(r.lines) == 'table' and r.lines or {}
  local w = ImGui.GetContentRegionAvail(ctx)
  if st.reverted then
    P.wrapped(('Reverted this reply: %s undone.'):format(plural(n, 'change')), C.muted)
    if st.partial then
      P.wrapped(('%s from earlier in the reply sit under your own edits and stayed.'):format(plural(st.partial, 'step')), C.warn)
    end
    if P.primary('Redo##rcpt') then redo(r) end
  else
    local who = r.id:match('^mcp%-') and 'An agent' or 'This reply'
    ImGui.Text(ctx, P.truncate(('%s changed %s in REAPER'):format(who, plural(n, 'thing')), w))
    for i = 1, math.min(3, #lines) do ImGui.TextColored(ctx, C.muted, P.truncate(lines[i], w)) end
    if #lines > 3 or n > #lines then
      ImGui.TextColored(ctx, C.muted, ('+%d more'):format(n - math.min(3, #lines)))
      if ImGui.IsItemHovered(ctx) then P.show_tip(table.concat(lines, '\n')) end
    end
    if P.primary('Revert##rcpt') then revert(r) end
    if ImGui.IsItemHovered(ctx) then P.show_tip('Undo exactly what this reply changed (Edit > Undo history shows each step).') end
    if st.blocked then
      P.wrapped('Your own edit is on top of the undo list ("' .. P.truncate(st.blocked, 200) .. '"). Undo it first, then Revert.', C.warn)
    elseif st.redo_failed then
      P.wrapped('Nothing to redo: something else was changed after the revert.', C.warn)
    end
  end
  ImGui.SameLine(ctx)
  if ImGui.Button(ctx, 'Dismiss##rcpt') then R.gone[r.id] = true end
end

---------------------------------------------------------------- meter
local MT = { on = reaper.GetExtState(EXT, 'meter') ~= '0', next = nil, seg = nil, prev = nil,
  alive = nil, alive_at = -math.huge, status = '' }
local GMEM = reaper.gmem_attach ~= nil and reaper.gmem_read ~= nil
if GMEM then reaper.gmem_attach('Hibiki') end
local MON = 0x1000000 -- the master's monitoring FX chain in TrackFX_* indices
local JS_NAMES = { 'JS:Hibiki/Hibiki_Meter.jsfx', 'JS:Hibiki/Hibiki_Meter', 'Hibiki Meter' }
local BAND_NAMES = { '40', '100', '250', '600', '1.5k', '3k', '6k', '12k' }

local function meter_slot()
  local m = reaper.GetMasterTrack(0)
  for i = 0, (reaper.TrackFX_GetRecCount and reaper.TrackFX_GetRecCount(m) or 0) - 1 do
    local _, name = reaper.TrackFX_GetFXName(m, MON + i, '')
    if (name or ''):find('Hibiki Meter', 1, true) then return i end
  end
  return -1
end
function MT.ensure()
  local m = reaper.GetMasterTrack(0)
  local slot = meter_slot()
  if MT.on and slot < 0 then
    if not exists(reaper.GetResourcePath() .. '/Effects/Hibiki/Hibiki_Meter.jsfx') then
      MT.status = 'not installed - run hibiki setup'
      return
    end
    local idx = -1
    for _, nm in ipairs(JS_NAMES) do
      idx = reaper.TrackFX_AddByName(m, nm, true, 1)
      if idx >= 0 then break end
    end
    MT.status = idx >= 0 and 'on the master monitoring FX' or 'REAPER would not load it'
  elseif not MT.on and slot >= 0 then
    reaper.TrackFX_Delete(m, MON + slot)
    MT.status = 'off'
  else
    MT.status = MT.on and 'on the master monitoring FX' or 'off'
  end
end

local RK -- the rack listens to the meter's frames
local function add_frame(fr)
  local seg = MT.seg
  -- a jump back, or forward by more than a hop, is a new playback
  if not seg or fr.pos < seg.last - 0.3 or fr.pos > seg.last + 1.5 then
    if seg and #seg.f >= 20 then MT.prev = seg end
    seg = { f = {} }
    MT.seg = seg
  end
  seg.f[#seg.f + 1] = fr
  seg.last, seg.at = fr.pos, now()
  if #seg.f > 6000 then table.remove(seg.f, 1) end -- 10 min
  RK.on_frame(fr)
end

function MT.tick()
  if not GMEM or not MT.on or reaper.gmem_read(0) ~= 1 then return end
  local alive = reaper.gmem_read(5)
  if alive ~= MT.alive then MT.alive, MT.alive_at = alive, now() end
  local total, ring, stride = reaper.gmem_read(1), reaper.gmem_read(2), reaper.gmem_read(3)
  if ring <= 0 or stride < 16 then return end
  if not MT.next or total < MT.next then MT.next = total end -- first look, or the meter restarted
  if total - MT.next > ring then MT.next = total - ring end
  local G = reaper.gmem_read
  while MT.next < total do
    local o = 8 + (MT.next % ring) * stride
    local b = {}
    for k = 0, 7 do b[k + 1] = G(o + 8 + k) end
    add_frame({ pos = G(o), m = G(o + 2), s = G(o + 3), tp = G(o + 4), sp = G(o + 5), corr = G(o + 6), sm = G(o + 7), b = b })
    MT.next = MT.next + 1
  end
end
function MT.running() return MT.on and now() - MT.alive_at < 2 end

local function energy(l) return 10 ^ ((l + 0.691) / 10) end
local function lufs(e) return -0.691 + 10 * log10(e) end

-- integrated loudness of a run of frames (each frame's LUFS-M is a 400 ms gating block, 75 % overlap: BS.1770)
local function integrated(frames)
  local sum, n = 0, 0
  for _, f in ipairs(frames) do if f.m > -70 then sum, n = sum + energy(f.m), n + 1 end end
  if n == 0 then return nil end
  local gate = lufs(sum / n) - 10
  local s2, n2 = 0, 0
  for _, f in ipairs(frames) do if f.m > -70 and f.m > gate then s2, n2 = s2 + energy(f.m), n2 + 1 end end
  return n2 > 0 and lufs(s2 / n2) or nil
end

function MT.lines(L)
  local seg = MT.seg
  if not seg or #seg.f < 20 then seg = MT.prev end
  if not seg or #seg.f < 20 then return end
  local f = seg.f
  local playing = reaper.GetPlayState() & 1 == 1 and now() - seg.at < 1
  local ago = now() - seg.at
  if not playing and ago > 900 then return end
  local li = integrated(f)
  local smax, tmax, cmin = f[1], f[1], nil
  local sm_e, sm_n, band_e, band_n = 0, 0, { 0, 0, 0, 0, 0, 0, 0, 0 }, 0
  for _, x in ipairs(f) do
    if x.s > smax.s then smax = x end
    if x.tp > tmax.tp then tmax = x end
    if x.m > -50 then
      if not cmin or x.corr < cmin.corr then cmin = x end
      sm_e, sm_n = sm_e + 10 ^ (x.sm / 10), sm_n + 1
      for k = 1, 8 do band_e[k] = band_e[k] + 10 ^ (x.b[k] / 10) end
      band_n = band_n + 1
    end
  end
  local head = ('meter (Hibiki Meter on the master, %s): bars %s-%s (%.0f s)'):format(
    playing and 'playing now' or ('last playback ' .. math.floor(ago / 60) .. ':' .. ('%02d'):format(math.floor(ago % 60)) .. ' ago'),
    bar_beat(f[1].pos), bar_beat(f[#f].pos), f[#f].pos - f[1].pos + 0.1)
  local parts = {}
  if li then parts[#parts + 1] = ('LUFS-I %.1f'):format(li) end
  parts[#parts + 1] = ('LUFS-S max %.1f @ %s'):format(smax.s, bar_beat(smax.pos))
  parts[#parts + 1] = ('true peak max %.1f dBTP @ %s'):format(tmax.tp, bar_beat(tmax.pos))
  if cmin then parts[#parts + 1] = ('correlation min %.2f @ %s'):format(cmin.corr, bar_beat(cmin.pos)) end
  if sm_n > 0 then parts[#parts + 1] = ('side %.1f dB vs mid'):format(10 * log10(sm_e / sm_n)) end
  L[#L + 1] = head .. ': ' .. table.concat(parts, ' · ')
  if band_n > 0 then
    local b = {}
    for k = 1, 8 do b[k] = BAND_NAMES[k] .. ' ' .. ('%.1f'):format(10 * log10(band_e[k] / band_n)) end
    L[#L + 1] = 'meter bands (mid, dB RMS, octave-wide band-pass): ' .. table.concat(b, ' · ')
  end
  -- per bar: max LUFS-M and true peak; long playbacks are grouped so the line stays short
  local bars, order = {}, {}
  for _, x in ipairs(f) do
    local k = bar_of(x.pos)
    local e = bars[k]
    if not e then e = { m = x.m, tp = x.tp }; bars[k] = e; order[#order + 1] = k
    else
      if x.m > e.m then e.m = x.m end
      if x.tp > e.tp then e.tp = x.tp end
    end
  end
  local step = math.max(1, math.ceil(#order / 24))
  local cells = {}
  for i = 1, #order, step do
    local m, tp = -200, -200
    for j = i, math.min(#order, i + step - 1) do
      local e = bars[order[j]]
      if e.m > m then m = e.m end
      if e.tp > tp then tp = e.tp end
    end
    local name = step == 1 and tostring(order[i]) or (order[i] .. '-' .. order[math.min(#order, i + step - 1)])
    cells[#cells + 1] = ('%s %.1f/%.1f'):format(name, m, tp)
  end
  L[#L + 1] = 'meter per bar (max LUFS-M / max dBTP): ' .. table.concat(cells, ' · ')
end

---------------------------------------------------------------- rack
RK = { groups = {}, at = -math.huge, focus = nil, stats = {} }
local function track_label(i, tr)
  if i == -1 then return 'master' end
  local _, nm = reaper.GetTrackName(tr)
  return ('#%d %s'):format(i + 1, nm)
end
local function fx_by_guid(tr, guid)
  for k = 0, reaper.TrackFX_GetCount(tr) - 1 do
    if reaper.TrackFX_GetFXGUID(tr, k) == guid then return k end
  end
end
local function active_of(g)
  for _, v in ipairs(g.vars) do
    if v.fx then
      if reaper.TrackFX_GetEnabled(v.tr, v.fx) then return v.n end
    elseif reaper.GetMediaTrackInfo_Value(v.tr, 'B_MUTE') == 0 then return v.n end
  end
end
function RK.scan()
  RK.at = now()
  local by, order = {}, {}
  local function add(name, v)
    local g = by[name]
    if not g then g = { name = name, vars = {} }; by[name] = g; order[#order + 1] = g end
    g.vars[#g.vars + 1] = v
  end
  for i = -1, reaper.CountTracks(0) - 1 do
    local tr = i == -1 and reaper.GetMasterTrack(0) or reaper.GetTrack(0, i)
    local ok, v = reaper.GetSetMediaTrackInfo_String(tr, 'P_EXT:hibiki_var', '', false)
    if ok and v ~= '' then
      local g, n = v:match('^(.-)/(%d+)$')
      if g and g ~= '' then add(g, { n = tonumber(n), tr = tr, label = track_label(i, tr) }) end
    end
    local ok2, fv = reaper.GetSetMediaTrackInfo_String(tr, 'P_EXT:hibiki_fxvar', '', false)
    if ok2 and fv ~= '' then
      for line in fv:gmatch('[^\n]+') do
        local g, n, guid = line:match('^(.-)/(%d+)\t(%b{})')
        local fx = g and fx_by_guid(tr, guid)
        if fx then
          local _, fxn = reaper.TrackFX_GetFXName(tr, fx, '')
          add(g, { n = tonumber(n), tr = tr, fx = fx, label = track_label(i, tr) .. ': ' .. fxn })
        end
      end
    end
  end
  local groups = {}
  for _, g in ipairs(order) do
    if #g.vars >= 2 then
      table.sort(g.vars, function(a, b) return a.n < b.n end)
      g.active = active_of(g)
      groups[#groups + 1] = g
    end
  end
  RK.groups = groups
end
function RK.switch(g, n)
  reaper.PreventUIRefresh(1)
  for _, v in ipairs(g.vars) do
    local on = v.n == n
    if v.fx then reaper.TrackFX_SetEnabled(v.tr, v.fx, on)
    else reaper.SetMediaTrackInfo_Value(v.tr, 'B_MUTE', on and 0 or 1) end
  end
  reaper.PreventUIRefresh(-1)
  reaper.TrackList_AdjustWindows(false)
  reaper.UpdateArrange()
  g.active = n
  RK.focus = g.name
end
-- what each variant measured on the master while it was the one playing
function RK.on_frame(fr)
  if fr.m <= -60 then return end
  for _, g in ipairs(RK.groups) do
    if g.active then
      local k = g.name .. '/' .. g.active
      local st = RK.stats[k] or { e = 0, n = 0 }
      st.e, st.n = st.e + energy(fr.m), st.n + 1
      RK.stats[k] = st
    end
  end
end
local function heard(g, n)
  local st = RK.stats[g.name .. '/' .. n]
  if not st or st.n < 10 then return nil end
  return lufs(st.e / st.n), st.n / 10
end

local MAX_ROWS = 3
function M.rack_height()
  local n = math.min(#RK.groups, MAX_ROWS)
  return n > 0 and n * ImGui.GetFrameHeightWithSpacing(ctx) or 0
end
function M.rack_row()
  if #RK.groups == 0 then return end
  local focused = ImGui.IsWindowFocused(ctx, ImGui.FocusedFlags_RootAndChildWindows)
  if focused and not S.comp_active then -- keys 1-9 switch the group last touched (or the first)
    local g = RK.groups[1]
    for _, x in ipairs(RK.groups) do if x.name == RK.focus then g = x end end
    for k = 1, 9 do
      if ImGui.IsKeyPressed(ctx, ImGui.Key_1 + k - 1, false) then
        for _, v in ipairs(g.vars) do if v.n == k then RK.switch(g, k) end end
      end
    end
  end
  for gi, g in ipairs(RK.groups) do
    if gi > MAX_ROWS then break end
    ImGui.AlignTextToFramePadding(ctx)
    ImGui.TextColored(ctx, RK.focus == g.name and C.value or C.strip_text, P.truncate(g.name, 110))
    if ImGui.IsItemHovered(ctx) then
      P.show_tip('Audition rack "' .. g.name .. '": click a number or press 1-9 to hear that variant.'
        .. (#RK.groups > MAX_ROWS and ('\n' .. (#RK.groups - MAX_ROWS) .. ' more groups not shown.') or ''))
    end
    for _, v in ipairs(g.vars) do
      ImGui.SameLine(ctx, 0, 4)
      if P.led_toggle('rk' .. gi .. '_' .. v.n, tostring(v.n), g.active == v.n) then RK.switch(g, v.n) end
      if ImGui.IsItemHovered(ctx) then
        local l, secs = heard(g, v.n)
        P.show_tip(v.label .. (l and ('\nheard %.0f s on the master: %.1f LUFS (average)'):format(secs, l) or ''))
      end
    end
    ImGui.SameLine(ctx, 0, 8)
    if P.chip('rkkeep' .. gi, 'Keep', C.muted, false) and g.active then
      local v
      for _, x in ipairs(g.vars) do if x.n == g.active then v = x end end
      S.input = ('Keep variant %d (%s) of audition group "%s": remove the other variants and their hibiki tags, '
        .. 'then save.'):format(v.n, v.label, g.name)
      S.focus = true
    end
    if ImGui.IsItemHovered(ctx) then P.show_tip('Write the request to keep the variant that is playing (you send it).') end
  end
end

---------------------------------------------------------------- ears
local E = { busy = false, n = 0, pending = nil }
local MAX_EARS = 30
local function ears_range()
  local s, e = reaper.GetSet_LoopTimeRange2(0, false, false, 0, 0, false)
  if e > s then return s, e, 'time selection' end
  local lo, hi
  for i = 0, reaper.CountTracks(0) - 1 do
    local _, rz = reaper.GetSetMediaTrackInfo_String(reaper.GetTrack(0, i), 'P_RAZOREDITS', '', false)
    for sa, sb in (rz or ''):gmatch('(%S+) (%S+) %b""') do
      local a, b = tonumber(sa), tonumber(sb)
      if a and b and b > a then lo, hi = math.min(lo or a, a), math.max(hi or b, b) end
    end
  end
  if lo then return lo, hi, 'razor area' end
end

local NUM_KEYS = { 'RENDER_BOUNDSFLAG', 'RENDER_STARTPOS', 'RENDER_ENDPOS', 'RENDER_SETTINGS', 'RENDER_SRATE',
  'RENDER_CHANNELS', 'RENDER_ADDTOPROJ', 'RENDER_NORMALIZE', 'RENDER_TAILFLAG' }
local STR_KEYS = { 'RENDER_FILE', 'RENDER_PATTERN', 'RENDER_FORMAT' }
local WAV24 = 'ZXZhdxgAAQ==' -- "evaw", 24 bit: REAPER's own default WAV sink config

local function render(s, e, dir, name)
  local saved = { n = {}, s = {} }
  for _, k in ipairs(NUM_KEYS) do saved.n[k] = reaper.GetSetProjectInfo(0, k, 0, false) end
  for _, k in ipairs(STR_KEYS) do saved.s[k] = select(2, reaper.GetSetProjectInfo_String(0, k, '', false)) end
  local wav
  local ok, err = pcall(function()
    reaper.GetSetProjectInfo_String(0, 'RENDER_FILE', dir, true)
    reaper.GetSetProjectInfo_String(0, 'RENDER_PATTERN', name, true)
    reaper.GetSetProjectInfo_String(0, 'RENDER_FORMAT', WAV24, true)
    local set = { RENDER_BOUNDSFLAG = 0, RENDER_STARTPOS = s, RENDER_ENDPOS = e, RENDER_SETTINGS = 0, RENDER_SRATE = 48000,
      RENDER_CHANNELS = 2, RENDER_ADDTOPROJ = 0, RENDER_NORMALIZE = 0, RENDER_TAILFLAG = 0 }
    for k, v in pairs(set) do reaper.GetSetProjectInfo(0, k, v, true) end
    local _, targets = reaper.GetSetProjectInfo_String(0, 'RENDER_TARGETS', '', false)
    wav = (targets or ''):match('[^;]+')
    if not wav then error('REAPER planned no file') end
    if exists(wav) then error('would overwrite ' .. wav) end -- REAPER's overwrite prompt would block the bridge
    reaper.Main_OnCommand(40101, 0) -- media goes offline while REAPER is in the background; a render drops it
    reaper.Main_OnCommand(42230, 0)
  end)
  for k, v in pairs(saved.n) do reaper.GetSetProjectInfo(0, k, v, true) end
  for k, v in pairs(saved.s) do reaper.GetSetProjectInfo_String(0, k, v, true) end
  if not ok then return nil, tostring(err) end
  if not exists(wav) then return nil, 'the render wrote nothing' end
  return wav
end

local function measure(wav)
  local m = {}
  if not (reaper.PCM_Source_CreateFromFile and reaper.CalculateNormalization) then return m end
  local src = reaper.PCM_Source_CreateFromFile(wav)
  if not src then return m end
  for key, mode in pairs({ lufs_i = 0, peak = 2, tp = 3, lufs_m_max = 4, lufs_s_max = 5 }) do
    local g = reaper.CalculateNormalization(src, mode, 0, 0, 0)
    if type(g) == 'number' and g > 0 and g < 1e7 then m[key] = -20 * log10(g) end
  end
  reaper.PCM_Source_Destroy(src)
  return m
end

function E.run(s, e, what)
  local cut = e - s > MAX_EARS
  if cut then e = s + MAX_EARS end
  E.n = E.n + 1
  local sep = P.WIN and '\\' or '/'
  local dir = P.DIR .. 'ears'
  reaper.RecursiveCreateDirectory(dir, 0)
  local name = ('ears-%s-%d'):format(os.date('%H%M%S'), E.n)
  local label = ('bars %s-%s'):format(bar_beat(s), bar_beat(e))
  local wav, err = render(s, e, dir, name)
  if not wav then M.note('Ears: ' .. err); return end
  local m = measure(wav)
  E.busy = true
  local args = { 'ears', P.q(wav), '--json', '--label', P.q(label) }
  for k, flag in pairs({ lufs_i = '--lufs-i', lufs_s_max = '--lufs-s-max', lufs_m_max = '--lufs-m-max', tp = '--tp', peak = '--peak' }) do
    if m[k] then args[#args + 1] = flag .. (' %.2f'):format(m[k]) end
  end
  local function nums()
    local p = {}
    if m.lufs_i then p[#p + 1] = ('LUFS-I %.1f'):format(m.lufs_i) end
    if m.lufs_s_max then p[#p + 1] = ('LUFS-S max %.1f'):format(m.lufs_s_max) end
    if m.tp then p[#p + 1] = ('true peak %.1f dBTP'):format(m.tp) end
    return table.concat(p, ' · ')
  end
  P.cli('ears', table.concat(args, ' '), function(code, out)
    E.busy = false
    local r = P.decode(out)
    local base = ('ears: %s (%s%s, %.1f s), rendered from the master: %s'):format(label, what,
      cut and (', first ' .. MAX_EARS .. ' s') or '', e - s, nums())
    if code == 0 and type(r) == 'table' and r.ok == true and type(r.png) == 'string' then
      P.add_file(r.png)
      P.add_file(r.md)
      P.add_file(wav)
      local peaks = {}
      for _, p in ipairs(type(r.peaks_over_tilt) == 'table' and r.peaks_over_tilt or {}) do
        peaks[#peaks + 1] = tostring(p.band) .. ' +' .. tostring(p.db)
      end
      E.pending = base .. ('; tilt %s dB/oct; over the tilt: %s; spectrum, stereo and levels in the attached .md/.png')
        :format(tostring(r.tilt_db_per_octave), #peaks > 0 and table.concat(peaks, ', ') or 'none')
      M.note('Ears: ' .. label .. ' attached')
    else
      P.add_file(wav)
      E.pending = base .. '; the WAV is attached (no spectrum: this Hibiki has no `hibiki ears` yet)'
      M.note('Ears: ' .. label .. ' attached as audio')
    end
  end)
end

function M.ears_chip()
  local s, e, what = ears_range()
  local off = E.busy or not s
  if off then ImGui.BeginDisabled(ctx) end
  local clicked = P.chip('ears', E.busy and 'Ears…' or 'Ears', C.muted, false)
  if off then ImGui.EndDisabled(ctx) end
  if ImGui.IsItemHovered(ctx, ImGui.HoveredFlags_AllowWhenDisabled or 0) then
    P.show_tip(not s and 'Ears: select a time range (or a razor area) to let the agent hear it.'
      or ('Render the %s (master, up to %d s) and attach its loudness, spectrum and stereo picture.'):format(what, MAX_EARS))
  end
  if clicked and not off then E.run(s, e, what) end
end

---------------------------------------------------------------- rules (/remember)
local BR = { info = nil, at = -math.huge }
function M.command(text)
  local rule = text:match('^/remember%s+(.+)$')
  if not rule then return false end
  P.http_url('POST', P.gateway .. '/api/hibiki/brain', '{"add":' .. P.jstr(rule) .. '}', function(code, body)
    local r = P.decode(body)
    if code == 200 and type(r) == 'table' then
      M.note(r.added == false and 'Already in the rules' or ('Remembered: ' .. plural(tonumber(r.count) or 0, 'rule') .. ' for the agents on this Mac'))
      BR.at = -math.huge
    else
      M.note('Could not save the rule (gateway answered ' .. tostring(code) .. ')')
    end
  end)
  return true
end
local function brain_info()
  if now() - BR.at < 10 then return end
  BR.at = now()
  P.http_url('GET', P.gateway .. '/api/hibiki/brain', nil, function(code, body)
    local r = P.decode(body)
    BR.info = code == 200 and type(r) == 'table' and r or false
  end)
end
local function brain_edit()
  P.http_url('POST', P.gateway .. '/api/hibiki/brain', '{"ensure":true}', function(code, body)
    local r = P.decode(body)
    if code ~= 200 or type(r) ~= 'table' or type(r.path) ~= 'string' then M.note('The gateway is not answering'); return end
    if P.WIN then P.win_bg('start "" notepad ' .. P.q(r.path))
    else P.sh('/usr/bin/open -t ' .. P.q(r.path) .. ' >/dev/null 2>&1 &') end
    BR.at = -math.huge
  end)
end

---------------------------------------------------------------- panel hooks
function M.tick(t)
  if reaper.GetExtState(EXT, 'open') == 'ears' then -- like open=settings / recover: another script can press Ears
    reaper.DeleteExtState(EXT, 'open', false)
    local s, e, what = ears_range()
    if s and not E.busy then E.run(s, e, what) else M.note('Ears: select a time range first') end
  end
  read_receipts()
  MT.tick()
  if t - RK.at > 1 then RK.scan() end
end

function M.cards(L)
  local r = receipt_shown()
  if r then L[#L + 1] = { kind = 'studio', key = 'receipt_' .. r.id, draw = function() receipt_card(r) end } end
end

-- extra lines for the REAPER context block (sent with each message)
function M.context_lines(L)
  local ok = pcall(MT.lines, L)
  if not ok then L[#L + 1] = 'meter: (could not summarise the last playback)' end
  for _, g in ipairs(RK.groups) do
    local cur
    for _, v in ipairs(g.vars) do if v.n == g.active then cur = v end end
    L[#L + 1] = ('audition rack "%s": %d variants, playing %s'):format(g.name, #g.vars,
      cur and ('#' .. cur.n .. ' (' .. cur.label .. ')') or 'none')
  end
  if E.pending then
    L[#L + 1] = E.pending
    E.pending = nil
  end
end

function M.settings(A)
  A.section('Studio', A.COL)
  if MT.on then MT.status = MT.status ~= '' and MT.status or '…' end
  brain_info()
  A.box('studio', A.COL, function(w, x0)
    if A.led_btn('meter', 'Measure playback', MT.on) then
      MT.on = not MT.on
      reaper.SetExtState(EXT, 'meter', MT.on and '1' or '0', true)
      MT.ensure()
    end
    if ImGui.IsItemHovered(ctx) then
      P.show_tip('Hibiki Meter on the master\'s monitoring FX (never rendered): loudness, true peak, bands and stereo '
        .. 'of what you play go into the context.')
    end
    ImGui.SetCursorScreenPos(ctx, x0, select(2, ImGui.GetCursorScreenPos(ctx)))
    ImGui.TextColored(ctx, A.dim, P.truncate('Meter: ' .. (MT.on and (MT.running() and 'measuring' or MT.status) or 'off'), w))
    ImGui.SetCursorScreenPos(ctx, x0, select(2, ImGui.GetCursorScreenPos(ctx)))
    ImGui.AlignTextToFramePadding(ctx)
    local info = BR.info
    ImGui.Text(ctx, info and ('Agent rules: ' .. (tonumber(info.count) or 0)) or 'Agent rules')
    if ImGui.IsItemHovered(ctx) then
      P.show_tip('One rules file for every agent on this Mac (Codex, Claude Code, Kimi, Gemini...). '
        .. 'Type  /remember <rule>  in the chat to add one.' .. (info and info.path and ('\n' .. info.path) or ''))
    end
    local bt = 'Edit'
    local bw = ImGui.CalcTextSize(ctx, bt) + 16
    ImGui.SameLine(ctx)
    ImGui.SetCursorScreenPos(ctx, x0 + w - bw, select(2, ImGui.GetCursorScreenPos(ctx)))
    if A.light_btn('brainedit', bt, bw, false, info == false) then brain_edit() end
  end)
end

function M.exit() reaper.SetExtState(EXT, 'turn', '', false) end

if MT.on then pcall(MT.ensure) end
M._R, M._MT, M._RK, M._E = R, MT, RK, E -- headless tests
return M
end
