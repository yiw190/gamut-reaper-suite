# Hibiki conventions
<!-- Hibiki's conventions, given to every agent (MCP instructions and the gateway's AGENTS.md / CLAUDE.md /
GEMINI.md block). This is your copy: rewrite or delete any rule. One rule per bullet, `- [id] text`; indented lines
continue it. When Hibiki updates, rules you left untouched get the new text, edited or deleted ones stay as they
are, new ids are added at the end; `hibiki rules reset <id>` brings the shipped text back. -->

- [version-title] A reply that changes REAPER becomes an entry in the panel's Version history, titled with the
  reply's first sentence: open such a reply with one line saying what you changed. Start run_lua code with a
  "-- <what it does>" comment (it titles the change).
- [prefer-verbs] Prefer the MCP verbs (track / fx / send / item / envelope / midi / project / render) over run_lua;
  use run_lua or reaper_call only for APIs they don't cover.
- [rack-audible] Audition rack is for audible alternatives (sounds, parts, arrangements, effects), not levels or
  balance (a few dB is hard to hear by switching: set the level and say which fader to ride). Preferred: one track
  in fixed lanes (I_FREEMODE 2, I_NUMFIXEDLANES n), one variant per lane (I_FIXEDLANE, P_LANENAME:k), only lane 0
  playing (C_LANEPLAYS:k = 1 for it, 0 for the rest), tagged P_EXT:hibiki_lanes = "<group>". Also understood:
  variant tracks tagged P_EXT:hibiki_var = "<group>/<n>", or FX lines in P_EXT:hibiki_fxvar =
  "<group>/<n><TAB><fx GUID>". Set the time selection to where the variants differ: a number button switches and
  loops it. The panel's Keep deletes the other variants itself. Match the variants' loudness first.
- [meter-context] "meter" lines in the REAPER context are measured on the master during the user's last playback
  (Hibiki Meter).
- [ears-files] Files named ears-*.wav/.png/.md are the user's selection rendered from the master and measured for you.
- [single-dls] Exactly one DLSMusicDevice per project: instances share one synth state and REAPER renders tracks on
  parallel threads, so two crash REAPER. Never duplicate a track holding it or add a second; route parts into the
  one instance by MIDI sends on separate channels. After a crash, check it isn't offline (TrackFX_GetOffline).
- [render-new-name] Never render onto an existing file: use a new file name every time (the render tool refuses an
  existing target anyway; REAPER's overwrite prompt would hang the bridge). Save the project before rendering.
- [media-online] render sets all media online itself (action 40101); still check every part is audible in the
  bounce (REAPER can take audio items offline while it isn't the frontmost app).
- [no-long-run-lua] No long blocking work inside run_lua (ExecProcess, long loops): it stalls REAPER and the bridge
  looks dead. Start external work in the background with io.popen and poll for its result.
- [restore-point] Before the first edit of a project from an earlier session, save a copy (project save
  path=<name>_vN.RPP) as a restore point; project save after each editing step.
