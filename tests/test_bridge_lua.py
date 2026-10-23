#!/usr/bin/env python3
"""Headless tests for reaper_mcp/bridge.lua, driven through the real mailbox.

Loads the bridge into a Lua interpreter with a fake ReaScript API
(tests/fake_reaper.lua), then sends requests exactly like server.py does.

Needs lupa; skips cleanly when it is absent so a bare CI job still passes.

    uv run --with lupa python tests/test_bridge_lua.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = ROOT / "reaper_mcp" / "bridge.lua"
FAKE = Path(__file__).resolve().parent / "fake_reaper.lua"

PPQ = 960.0

# Shims for older Lua runtimes lupa may load; the bridge targets REAPER's Lua 5.4.
COMPAT = """
if not table.pack then
  table.pack = function(...) return {n = select('#', ...), ...} end
end
if not table.unpack then table.unpack = unpack end
local _load = load
load = function(chunk, name, mode, env)
  local f, err = _load(chunk, name)
  if not f then return f, err end
  if env and setfenv then setfenv(f, env) end
  return f
end
"""

REFERENCE_ESC = r"""
local esc = {['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t'}
local function utf8_len(s, i)
  local b = s:byte(i)
  if not b then return nil end
  if b < 0x80 then return 1 end
  if b < 0xC2 then return nil end
  if b < 0xE0 then return 2 end
  if b < 0xF0 then return 3 end
  if b < 0xF5 then return 4 end
  return nil
end
local function is_cont(s, i) local b = s:byte(i) return b ~= nil and b >= 0x80 and b < 0xC0 end
return function(s)
  local parts = {'"'}
  local i, n = 1, #s
  while i <= n do
    local b = s:byte(i)
    if b < 32 or b == 34 or b == 92 then
      parts[#parts + 1] = esc[s:sub(i, i)] or string.format('\\u%04x', b)
      i = i + 1
    elseif b < 128 then
      parts[#parts + 1] = s:sub(i, i)
      i = i + 1
    else
      local len = utf8_len(s, i)
      local ok = len ~= nil and (i + len - 1) <= n
      if ok then for j = i + 1, i + len - 1 do if not is_cont(s, j) then ok = false break end end end
      if ok then parts[#parts + 1] = s:sub(i, i + len - 1); i = i + len
      else parts[#parts + 1] = string.format('\\u%04x', b); i = i + 1 end
    end
  end
  parts[#parts + 1] = '"'
  return table.concat(parts)
end
"""


def json_checks(lupa, check) -> None:
    """bridge.lua's JSON codec on its own: fast paths must not change what goes over the mailbox."""
    import random
    src = BRIDGE.read_text(encoding="utf-8")
    block = src[src.index("local json = {}"):src.index("-" * 70 + "\n-- files")]
    lua = lupa.LuaRuntime(encoding=None)
    lua.execute(COMPAT)
    js = lua.execute(block + "\nreturn json")
    ref = lua.execute(REFERENCE_ESC)
    enc = js.encode

    def dec(raw: bytes):
        return lua.eval("function(j, s) return j.decode(s) end")(js, raw)

    cases = {
        "ascii": b"Piano 1", "cjk": "鼓 钢琴 Lead".encode(), "emoji": "\U0001F3B5 Lead".encode(),
        "escapes": b'a"b\\c\n\t\r\x08\x0c/', "controls": b"\x00\x01\x1f\x7f",
        "invalid utf-8": b"ok\xff\xc3(\xe4\xb8", "lone continuation": b"\x80\xbfx", "overlong lead": b"\xc0\xaf",
        "empty": b"",
    }
    for name, raw in cases.items():
        out = enc(raw)
        check("json encode %s: same bytes as the reference encoder" % name, out == ref(raw), (raw, out, ref(raw)))
    check("json encode cjk passes through unescaped", enc("鼓".encode()) == '"鼓"'.encode())
    check("json encode invalid utf-8 -> \\u00XX per bad byte",
          enc(b"ok\xff\xc3(") == b'"ok\\u00ff\\u00c3("', enc(b"ok\xff\xc3("))
    check("json encode controls", enc(b"\x01\n\x1f") == b'"\\u0001\\n\\u001f"', enc(b"\x01\n\x1f"))
    rng = random.Random(7)
    alphabet = [bytes([b]) for b in range(256)] + ["é".encode(), "鼓".encode(), "\U0001F3B5".encode()] * 20
    mismatch = None
    for _ in range(3000):
        raw = b"".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
        if enc(raw) != ref(raw):
            mismatch = raw
            break
    check("json encode: 3000 random byte strings match the reference encoder", mismatch is None, repr(mismatch))

    for name, value in {"cjk": "鼓 钢琴", "escapes": 'q"b\\s\n\t\r\x08\x0c/', "controls": "\x01\x1f",
                        "emoji": "\U0001F3B5x", "bmp": "é€"}.items():
        for ascii_only in (True, False):
            wire = json.dumps(value, ensure_ascii=ascii_only).encode()
            got = dec(wire)
            check("json decode %s (ensure_ascii=%s)" % (name, ascii_only), got == value.encode(), (wire, got))
            check("json round trip %s (ensure_ascii=%s)" % (name, ascii_only), dec(enc(dec(wire))) == value.encode())
    doc = {"notes": [{"pitch": 60 + i, "start_beats": i * 0.5, "name": "鼓 %d \"x\"" % i, "muted": i % 2 == 0}
                     for i in range(50)], "track": "Piano", "empty": [], "nested": {"a": [1, -2.5, 3e2]}}
    wire = json.dumps(doc, ensure_ascii=False).encode()
    back = json.loads(enc(dec(wire)).decode())
    check("json round trip: notes document", back == dict(doc, nested={"a": [1, -2.5, 300]}), back)
    bad = lua.eval("function(j, s) return select(2, j.decode(s)) end")
    check("json decode: unterminated string is an error", bad(js, b'"abc') is not None
          and bad(js, b'"ab\\') is not None and bad(js, b'"\\u00e9') is not None)


def main() -> None:
    try:
        import lupa
    except ImportError:
        print("SKIP  install lupa: uv run --with lupa python tests/test_bridge_lua.py")
        return

    ipc_dir = tempfile.mkdtemp(prefix="reaper_lua_")
    os.environ["REAPER_MCP_IPC_DIR"] = ipc_dir
    req = os.path.join(ipc_dir, "request.json")
    resp = os.path.join(ipc_dir, "response.json")

    lua = lupa.LuaRuntime()
    lua.execute(COMPAT)
    lua.execute(FAKE.read_text(encoding="utf-8"))
    lua.execute(BRIDGE.read_text(encoding="utf-8"))

    g = lua.globals()
    failures = []

    def count(name) -> int:
        v = g.calls[name]
        return 0 if v is None else int(v)

    def clear():
        lua.execute("calls = {}")

    def rpc(func, args=None, code=None):
        payload = {"id": "t", "func": func}
        if code is not None:
            payload["code"] = code
        else:
            payload["args"] = args or []
        with open(req, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        g.pump(1)
        for _ in range(300):
            if os.path.exists(resp):
                break
            g.pump(1)
        with open(resp, encoding="utf-8") as f:
            return json.loads(f.read())

    def check(label, cond, extra=""):
        print(("  PASS" if cond else "  FAIL"), label, "" if cond else extra)
        if not cond:
            failures.append(label)

    def seeded_track(index=0):
        """A track with one MIDI item holding one muted note."""
        lua.execute(f"""
            if not tracks[{index + 1}] then
              tracks[{index + 1}] = new_track("T{index}")
            end
            tracks[{index + 1}].items[1] = new_item(0.0, 2.0)
            tracks[{index + 1}].items[1].notes = {{
              {{sel = false, muted = true, sppq = 0.0, eppq = {PPQ},
                chan = 0, pitch = 60, vel = 96}},
            }}
        """)
        return g.tracks

    # --- baseline ----------------------------------------------------
    check("bridge loaded", rpc("ping").get("ok") is True)
    r = rpc("add_track", ["Solo", 0])
    check("add_track", r.get("ok") and r["ret"]["index"] == 0, r)
    check("one refresh per single call",
          count("UpdateArrange") == 1 and count("TrackList_AdjustWindows") == 1,
          f"arrange={count('UpdateArrange')} tracks={count('TrackList_AdjustWindows')}")

    # --- batch coalesces refresh ------------------------------------
    clear()
    r = rpc("batch", [[
        {"func": "add_track", "args": ["A", 1]},
        {"func": "add_track", "args": ["B", 2]},
        {"func": "update_track", "args": [0, {"volume_db": -3}]},
        {"func": "update_track", "args": [1, {"mute": True}]},
    ]])
    check("batch ok", r.get("ok"), r)
    check("batch refreshes arrange once",
          count("UpdateArrange") == 1, f"arrange={count('UpdateArrange')}")
    check("batch refreshes track layout once",
          count("TrackList_AdjustWindows") == 1, f"tracks={count('TrackList_AdjustWindows')}")

    # --- refresh state is released after the batch -------------------
    clear()
    r = rpc("update_track", [0, {"volume_db": -1}])
    check("refresh works after a batch",
          r.get("ok") and count("UpdateArrange") == 1, f"arrange={count('UpdateArrange')}")

    # --- MIDI note update -------------------------------------------
    seeded_track(0)
    clear()
    r = rpc("update_midi_note", [0, 0, 0, {"pitch": 62}])
    check("update_midi_note ok", r.get("ok"), r)
    check("update_midi_note sorts once", count("MIDI_Sort") == 1, f"sort={count('MIDI_Sort')}")
    check("update_midi_note asks the API not to sort",
          count("MIDI_SetNote.noSort") == 1 and count("MIDI_SetNote.sorted") == 0,
          f"noSort={count('MIDI_SetNote.noSort')} sorted={count('MIDI_SetNote.sorted')}")

    # lupa's LuaTable shadows Lua fields named like dict methods (items, keys),
    # so index with [] rather than attribute access.
    def note0():
        return g.tracks[1]["items"][1]["notes"][1]

    r = rpc("update_midi_note", [0, 0, 0, {"muted": False}])
    n = note0()
    check("update_midi_note can unmute", r.get("ok") and n["muted"] is False, dict(n))

    r = rpc("update_midi_note", [0, 0, 0, {"start_beats": 2.0}])
    n = note0()
    moved = abs(float(n["sppq"]) - 2.0 * PPQ) < 1e-6
    kept = abs(float(n["eppq"]) - float(n["sppq"]) - PPQ) < 1e-6
    check("moved note keeps its length", r.get("ok") and moved and kept, dict(n))

    # --- item index without a scan -----------------------------------
    clear()
    before = count("GetTrackMediaItem")
    r = rpc("create_midi_item", [0, 0.0, 4.0])
    check("create_midi_item index correct",
          r["ret"]["item_index"] == len(g.tracks[1]["items"]) - 1, r)
    check("create_midi_item does not scan items",
          count("GetTrackMediaItem") - before <= 1,
          f"lookups={count('GetTrackMediaItem') - before}")

    # --- list_items property reads -----------------------------------
    clear()
    n_items = len(g.tracks[1]["items"])
    r = rpc("list_items", [0])
    reads = sum(count(f"item.{k}") for k in ("D_POSITION", "D_LENGTH", "D_VOL"))
    check("list_items reads 3 properties per item",
          r.get("ok") and reads == 3 * n_items, f"reads={reads} items={n_items}")

    # --- read-only ops skip undo block --------------------------------
    clear()
    r = rpc("batch", [[
        {"func": "list_items", "args": [0]},
        {"func": "list_sends", "args": [0]},
        {"func": "list_envelopes", "args": [0]},
        {"func": "list_tracks", "args": []},
        {"func": "get_envelope", "args": [0, 0]},
    ]])
    check("read-only batch runs", r.get("ok"), r)
    check("read-only batch skips undo blocks",
          count("Undo_BeginBlock") == 0, f"undo={count('Undo_BeginBlock')}")

    # --- mutating ops keep their undo block ---------------------------
    clear()
    rpc("update_track", [0, {"volume_db": -2}])
    check("mutating op keeps its undo block", count("Undo_BeginBlock") == 1)

    # --- midi CC / program change --------------------------------------
    seeded_track(0)
    clear()
    r = rpc("add_midi_cc", [0, 0, [
        {"start_beats": 0, "program": 33, "channel": 1},
        {"start_beats": 0, "cc": 64, "value": 127, "channel": 0},
    ]])
    take_ccs = g.tracks[1]["items"][1]["ccs"]
    pc, cc64 = take_ccs[1], take_ccs[2]
    check("add_midi_cc ok", r.get("ok") and r["ret"]["inserted"] == 2, r)
    check("program change is 0xC0", int(pc["chanmsg"]) == 0xC0 and int(pc["msg2"]) == 33, dict(pc))
    check("cc 64 value 127", int(cc64["chanmsg"]) == 0xB0 and int(cc64["msg2"]) == 64 and int(cc64["msg3"]) == 127, dict(cc64))
    check("add_midi_cc sorts once", count("MIDI_Sort") == 1, f"sort={count('MIDI_Sort')}")

    # --- compact payloads ----------------------------------------------
    seeded_track(0)
    r = rpc("get_midi_notes", [0, 0])
    n0 = r["ret"][0]
    check("get omits index", "index" not in n0, n0)
    check("get omits channel 0", "channel" not in n0, n0)
    check("get omits selected false", "selected" not in n0, n0)
    check("get keeps muted true", n0.get("muted") is True, n0)

    r = rpc("get_project_summary")
    check("status has no ipc", r.get("ok") and "ipc" not in r["ret"], r)
    check("light status has no time_sig", "time_sig" not in r["ret"], r)

    seeded_track(0)
    lua.execute('markers = {{isrgn = false, pos = 0.0, name = "Intro", idx = 1}}')
    r = rpc("get_project_summary", [True])
    det = r.get("ret") or {}
    tr0 = (det.get("tracks") or [{}])[0]
    check("detailed status ok", r.get("ok"), r)
    check("detailed time_sig 4/4",
          (det.get("time_sig") or {}).get("numerator") == 4
          and (det.get("time_sig") or {}).get("denominator") == 4, det)
    check("detailed length_beats from project length",
          abs(float(det.get("length_beats") or 0) - 4.0) < 1e-6, det)
    check("detailed cursor_beats", "cursor_beats" in det, det)
    check("detailed markers",
          det.get("markers") and det["markers"][0].get("name") == "Intro", det)
    check("detailed track is midi", tr0.get("has_midi") is True
          and tr0.get("has_audio") is False, tr0)
    check("detailed midi_note_count", tr0.get("midi_note_count") == 1, tr0)
    check("detailed status does not guess chords",
          "chords" not in det and "chord" not in det, det)
    lua.execute("markers = {}")

    r = rpc("batch", [[{"func": "ping"}]])
    inner = r["ret"][0]
    check("batch unwraps ret", isinstance(inner, dict) and inner.get("pong") is True, inner)
    check("batch has no ok/ret shell", "ret" not in inner and "ok" not in inner, inner)

    r = rpc("run_lua", code="this is not lua !!!")
    err = r.get("error") or ""
    check("error has no bridge prefix", r.get("ok") is False and "bridge error" not in err, err)
    check("error strips chunk name", "mcp_run_lua" not in err, err)

    # --- import media ---------------------------------------------------
    clear()
    n_before = len(g.tracks)
    r = rpc("import_media", ["/tmp/x.mid", {"start_beats": 0, "as_new_track": True}])
    check("import_media new track", r.get("ok") and len(g.tracks) == n_before + 1, r)
    new_it = g.tracks[len(g.tracks)]["items"][1]
    check("import_media clears loop source", new_it["loop"] is False, dict(new_it))

    lua.execute("""
        tracks[1].selected = false
        tracks[2] = tracks[2] or new_track("T1")
        tracks[2].items = { new_item(0.0, 1.0), new_item(8.0, 1.0) }
    """)
    r = rpc("import_media", ["/tmp/y.wav", {"start_beats": 8, "track_index": 1}])
    items = g.tracks[2]["items"]
    check("import_media reports the item it created, between existing ones",
          r.get("ok") and r["ret"]["created"] == [{"track_index": 1, "item_index": 1}], r)
    check("import_media leaves existing items alone", items[3]["loop"] is True and items[2]["loop"] is False,
          [dict(items[i]) for i in (1, 2, 3)])
    n_before = len(g.tracks)
    r = rpc("import_media", ["/tmp/z.wav", {"as_new_track": True}])
    created = r.get("ret", {}).get("created")
    check("import_media new track mid-list: only the new item",
          r.get("ok") and len(created) == 1 and g.tracks[created[0]["track_index"] + 1]["name"] == "imported"
          and len(g.tracks) == n_before + 1, r)

    r = rpc("import_media", ["", {"track_index": 0}])
    check("import_media rejects empty path", r.get("ok") is False, r)

    # --- save project ---------------------------------------------------
    clear()
    r = rpc("save_project", [None])
    check("save_project unsaved needs path", r.get("ok") is False, r)
    r = rpc("save_project", ["/tmp/new dir/song.RPP"])
    check("save_project untitled adopts the path", r.get("ok") and r["ret"]["copy"] is False
          and count("Main_openProject") == 1, r)
    check("save_project creates the folder", count("RecursiveCreateDirectory") == 1)
    r = rpc("save_project", ["/tmp/song_v2.RPP"])
    check("save_project named project writes a copy", r.get("ok") and r["ret"]["copy"] is True
          and count("Main_openProject") == 1, r)
    check("save_project uses SaveProjectEx", count("Main_SaveProjectEx") == 2)
    r = rpc("save_project", [None])
    check("save_project named project saves in place", r.get("ok") and r["ret"]["path"] == "/tmp/new dir/song.RPP"
          and count("Main_SaveProject") == 1, r)
    check("save_project skips undo block", count("Undo_BeginBlock") == 0)
    r = rpc("save_project", ["/readonly/song.RPP"])
    check("save_project reports a failed write", r.get("ok") is False and "could not write" in (r.get("error") or ""), r)

    # --- idle heartbeat throttle --------------------------------------
    clear()
    g.pump(60)  # one second of idle ticks
    writes = count("heartbeat_writes")
    check("idle heartbeat writes throttled", 1 <= writes <= 3, f"writes={writes}")

    # --- JSON regressions (null args, emoji, nil batch slots) ----------
    n_tracks = len(g.tracks)
    r = rpc("add_track", [None, 0])
    check("null name keeps the index in place", r.get("ok") and r["ret"]["index"] == 0
          and len(g.tracks) == n_tracks + 1 and r["ret"].get("name") != "0", r)
    r = rpc("add_track", ["\U0001F3B5 Lead", 0])  # json.dump escapes it as a surrogate pair
    names = [t["name"] for t in rpc("list_tracks")["ret"]]
    check("emoji survives the mailbox", "\U0001F3B5 Lead" in names, names[:3])
    r = rpc("batch", [[{"func": "run_lua", "code": "return nil"}, {"func": "ping"}]])
    r2 = rpc("batch", [[{"func": "run_lua", "code": 'error("missing argument: index", 0)'}, {"func": "ping"}]])
    check("batch error slot carries the message", r2.get("ok") and r2["ret"][0] == {"error": "missing argument: index"}
          and r2["ret"][1]["pong"] is True, r2)
    check("batch keeps nil slots as null", r.get("ok") and isinstance(r["ret"], list) and len(r["ret"]) == 2
          and r["ret"][0] is None, r)

    # --- delete_midi_notes ignores repeats and out-of-range indices --
    item = rpc("create_midi_item", [0, 0.0, 4.0])["ret"]["item_index"]
    rpc("add_midi_notes", [0, item, [{"pitch": 60, "start_beats": 0, "length_beats": 1},
                                     {"pitch": 62, "start_beats": 1, "length_beats": 1}]])
    r = rpc("delete_midi_notes", [0, item, [0, 0, 99]])
    left = rpc("get_midi_notes", [0, item])["ret"]
    check("delete [0,0,99] removes one note", r.get("ok") and r["ret"]["deleted"] == 1 and len(left) == 1, r)

    # --- volume envelopes speak gain, whatever their scaling ----------
    lua.execute("""
        tracks[#tracks + 1] = new_track("Vox")
        tracks[#tracks].envelopes = { { name = "Volume", scaling = 1, points = {} } }
        tracks[#tracks + 1] = new_track("Amp")
        tracks[#tracks].envelopes = { { name = "Volume", points = {} } }
    """)
    vox, amp = len(g.tracks) - 2, len(g.tracks) - 1
    r = rpc("set_envelope", [vox, "Volume", [{"beats": 0, "value": 1.0}, {"beats": 4, "value": 0.5}]])
    raw = [float(g.tracks[vox + 1]["envelopes"][1]["points"][i][2]) for i in (1, 2)]
    want = [float(g.reaper.ScaleToEnvelopeMode(1, v)) for v in (1.0, 0.5)]
    check("set_envelope on a fader-scaled envelope stores fader positions (0 dB = 716.2)",
          r.get("ok") and all(abs(a - b) < 1e-6 for a, b in zip(raw, want)) and abs(raw[0] - 716.2178) < 1e-3, (raw, want))
    lua.execute("""tracks[%d].envelopes[1].points = {
        { 0.0, reaper.ScaleToEnvelopeMode(1, 1.0) }, { 2.0, reaper.ScaleToEnvelopeMode(1, 0.5) } }""" % (vox + 1))
    r = rpc("get_envelope", [vox, "Volume"])
    vals = [p["value"] for p in r.get("ret", {}).get("points", [])]
    check("get_envelope on a fader-scaled envelope returns gain (points drawn at 0 dB and -6 dB)", r.get("ok") and len(vals) == 2
          and abs(vals[0] - 1.0) < 1e-9 and abs(vals[1] - 0.5) < 1e-9, r)
    rpc("set_envelope", [amp, "Volume", [{"beats": 0, "value": 0.5}]])
    check("set_envelope on an amplitude envelope stores the gain as is",
          float(g.tracks[amp + 1]["envelopes"][1]["points"][1][2]) == 0.5)

    # --- a bus is a submix: sources reach the master through it only -
    n_before = len(g.tracks)
    r = rpc("create_bus", ["Drum Bus", [vox, amp]])
    bus = g.tracks[n_before + 1]
    srcs = [g.tracks[vox + 1], g.tracks[amp + 1]]
    check("create_bus sends each source to the new bus", r.get("ok") and r["ret"]["index"] == n_before
          and all(len(s["sends"]) == 1 and s["sends"][1]["dest"]["name"] == "Drum Bus" for s in srcs), r)
    check("create_bus turns off the sources' direct master send",
          all(int(g.reaper.GetMediaTrackInfo_Value(s, "B_MAINSEND")) == 0 for s in srcs)
          and int(g.reaper.GetMediaTrackInfo_Value(bus, "B_MAINSEND")) == 1)

    json_checks(lupa, check)

    # --- the bridge is still responsive -------------------------------
    r = rpc("ping")
    check("bridge still responsive", r.get("ok") and r["ret"]["pong"] is True, r)

    if failures:
        print("FAILED", failures)
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()
