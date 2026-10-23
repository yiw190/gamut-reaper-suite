# Demo animation source

Re-creates the README demo from a real Hibiki session: the prompt, the agent's replies and the REAPER
before/after state (`data.js`, `convo.json`) replayed over a REAPER window (`reaper_window_clean.png`,
title bar and panel area blanked; both are redrawn in HTML). `window.seek(t)` drives every frame.

```sh
npm install && npx playwright install chromium
node record.mjs video ../out/hibiki-demo.mp4          # 1920×960
node record.mjs video ../out/hibiki-social.mp4 45     # 1080×1350 social cut (sq = 1080×1080)
node record.mjs stills start=0.5,fx=16                 # PNG stills into ../out/stills
```

Needs ffmpeg on PATH. MIDI notes, FX window values and time compression are reconstructed; see the
caption under the demo in the main README.
