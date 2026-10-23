// Hibiki demo timeline. Everything is a function of t (seconds) plus a little sequential easing state
// (log scroll, input scroll), so frames must be rendered in increasing t order (record.mjs does that).
'use strict';
const D = window.DATA;
const $ = (id) => document.getElementById(id);
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));
const ease = (x) => { x = clamp(x, 0, 1); return x * x * (3 - 2 * x); };
const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

// ---------------------------------------------------------------- timeline
// ?social=45 | ?social=sq: the short social cut (faster typing, tighter tool phase, fast final answer,
// virtual camera, one caption, portrait/square end card). Without it: the original 1920x960 timeline.
const SOCIAL = new URLSearchParams(location.search).get('social') || '';
const PROMPT = D.convo.prompt;
const T_FOCUS = SOCIAL ? 1.5 : 0.55, T_TYPE0 = SOCIAL ? 1.85 : 1.0;
const TYPE_K = SOCIAL ? 0.45 : 1;
// human typing pace, deterministic: ~42 ms/char with jitter, longer after spaces and punctuation
const typeTimes = [];
{
  let t = T_TYPE0;
  for (let i = 0; i < PROMPT.length; i++) {
    const ch = PROMPT[i];
    typeTimes.push(t);
    t += TYPE_K * (ch === ' ' ? 0.055 : (/[,.]/.test(ch) ? 0.16 : 0.03 + ((i * 7919) % 5) * 0.006));
  }
}
const T_TYPED = typeTimes[typeTimes.length - 1];
const S0 = T_TYPED + (SOCIAL ? 0.3 : 0.5); // Enter
// offsets after Enter: [normal, social]
const O = SOCIAL ? {
  md1: 0.5, ts: 0.9, bash: 1.1, rl1: 1.35, rl2: 1.65, md2: 2.0, rl3: 2.55, md3: 2.9, proj: 3.35, rl4: 3.55, A: 3.9,
  md4: 5.3, rl5: 5.6, md5: 5.85, rl6: 6.35, rl7: 6.55, md6: 6.8, cap: 7.3, fxclose: 9.3, final: 10.3,
  per: 0.04, finalDur: 1.5, hold: 1.3, endDur: 2.3, saved: 6.4, tipOut: 2.7,
} : {
  md1: 1.3, ts: 2.0, bash: 2.35, rl1: 3.1, rl2: 4.2, md2: 5.4, rl3: 6.6, md3: 7.5, proj: 8.3, rl4: 8.75, A: 9.5,
  md4: 13.2, rl5: 14.1, md5: 15.0, rl6: 16.0, rl7: 16.9, md6: 17.6, cap: 18.75, fxclose: 19.7, final: 20.3,
  per: 0.07, finalDur: 0, hold: 3.2, endDur: 3.0, saved: 16.1, tipOut: 3.7,
};
const A = S0 + O.A;       // the big apply run_lua (FX + send + envelopes)
const T_CAP = S0 + O.cap; // envelopes end at bar 59
const T_FXCLOSE = S0 + O.fxclose;
const T_FINAL = S0 + O.final;
const CH = D.convo.chunks; // real SSE chunks: [0] GM Synth.., [1] The pizz only.., [2] Threshold.., [3] Scaling.., [4] Electric Piano.., [5] final
const TEXT1 = D.convo.texts[0];
const text1Chunks = TEXT1.match(/\S+\s*/g).reduce((acc, w, i) => { if (i % 2 === 0) acc.push(w); else acc[acc.length - 1] += w; return acc; }, []);
const PER = O.per, PER2 = SOCIAL ? 0.05 : 0.08;
// blocks in panel order. text blocks: {at, chunks, per}; lane blocks: {tools: [[at, label], ...]}
const BLOCKS = [
  { kind: 'md', at: S0 + O.md1, chunks: text1Chunks, per: PER },
  { kind: 'lane', tools: [[S0 + O.ts, 'ToolSearch'], [S0 + O.bash, 'Bash'], [S0 + O.rl1, 'run_lua'], [S0 + O.rl2, 'run_lua']] },
  { kind: 'md', at: S0 + O.md2, chunks: CH[0], per: PER },
  { kind: 'lane', tools: [[S0 + O.rl3, 'run_lua']] },
  { kind: 'md', at: S0 + O.md3, chunks: CH[1], per: PER },
  { kind: 'lane', tools: [[S0 + O.proj, 'project'], [S0 + O.rl4, 'run_lua'], [A, 'run_lua']] },
  { kind: 'md', at: S0 + O.md4, chunks: CH[2], per: PER2 },
  { kind: 'lane', tools: [[S0 + O.rl5, 'run_lua']] },
  { kind: 'md', at: S0 + O.md5, chunks: CH[3], per: PER },
  { kind: 'lane', tools: [[S0 + O.rl6, 'run_lua'], [S0 + O.rl7, 'run_lua']] },
  { kind: 'md', at: S0 + O.md6, chunks: CH[4], per: PER2 },
  { kind: 'lane', tools: [[T_CAP - 0.1, 'run_lua']] },
  { kind: 'md', at: T_FINAL, chunks: CH[5], per: SOCIAL ? O.finalDur / CH[5].length : 0.06 },
];
const lastFinal = BLOCKS[BLOCKS.length - 1];
const T_DONE = T_FINAL + lastFinal.chunks.length * lastFinal.per + 0.15;
const T_END0 = T_DONE + O.hold;   // end card fade starts
const T_TOTAL = T_END0 + O.endDur;
const T_CAPTION = S0 + O.md6 + 0.1; // social caption: the "stop at bar 59" decision
window.TIMELINE = { T_TOTAL, S0, A, T_CAP, T_FINAL, T_DONE, T_END0, T_TYPE0, T_TYPED, T_CAPTION, SOCIAL };

// ---------------------------------------------------------------- REAPER: title bar
const DOC_SVG = '<svg class="doc" viewBox="0 0 14 16"><path d="M2 .5h6.5l3.5 3.5v11.5H2z" fill="#fafafa" stroke="#9a9a9a"/><path d="M8.5.5V4H12" fill="none" stroke="#9a9a9a"/><path d="M4 7h6M4 9.5h6M4 12h4" stroke="#b5b5b5"/></svg>';
function renderTitle(t) {
  const dirty = (t >= A + 0.5 && t < S0 + O.saved) || (t >= T_CAP && t < T_CAP + 0.55);
  const s = 'Embark_demo' + (dirty ? ' [modified]' : '') + ' - REAPER v7.79';
  const el = $('title');
  if (el._s !== s) { el._s = s; el.innerHTML = DOC_SVG + '<span>' + esc(s) + '</span>'; }
}

// ---------------------------------------------------------------- REAPER: TCP
const VIS = D.tracks.slice(2, 14); // #3 .. #14
const RH = 44, EH = 38;
const GM = 9;
const envLaneH = (t, k) => EH * ease((t - (A + 1.55 + k * 0.25)) / 0.25);
function fmtVol(v) { return v === 0 ? '0.00' : v.toFixed(2); }
const tcpEls = [];
function buildTCP() {
  const tcp = $('tcp');
  for (const tr of VIS) {
    const r = document.createElement('div');
    r.className = 'trow' + (tr.sel ? ' sel' : '');
    r.style.height = RH + 'px';
    const black = tr.col === 0x1000000;
    const volPos = clamp((tr.vol + 60) / 72, 0, 1); // fader position: -60..+12 dB
    r.innerHTML = `<div class="strip"></div><div class="tnum${black ? ' black' : ''}">${tr.i + 1}</div>` +
      `<div class="tname">${esc(tr.name)}</div>` +
      `<div class="bev" style="left:168px;top:6px;width:20px"><span>M</span></div>` +
      `<div class="bev" style="left:190px;top:6px;width:20px"><span>S</span></div>` +
      `<div class="bev" style="left:212px;top:6px;width:32px"><span style="text-align:left;padding-left:5px">FX</span></div>` +
      `<div class="led${tr.nfx ? ' on' : ''}" style="left:236px;top:11px;z-index:3"></div>` +
      `<div class="bev" style="left:247px;top:6px;width:35px"><span style="text-align:left;padding-left:5px">I/O</span></div>` +
      `<div class="led io" style="left:274px;top:11px;z-index:3"></div>` +
      `<div class="fader" style="width:118px"><div class="fknob" style="left:${Math.round(volPos * 110)}px"></div></div>` +
      `<div class="tvol" style="left:152px;width:70px;text-align:left">${fmtVol(tr.vol)} dB</div>`;
    tcp.appendChild(r);
    tcpEls.push(r);
  }
  // envelope lanes under GM Synth
  const names = ['Wet / Pizz bright bar51+ (+2.5 @3k, shelf +4 @7k)', 'Threshold / Pizz kick pump bar51+ (aux, HP800 det, -40/10:1/1/120)'];
  for (let k = 0; k < 2; k++) {
    const e = document.createElement('div');
    e.className = 'envrow';
    e.innerHTML = `<div class="envlane"><div class="envname">${esc(names[k])}</div><div class="envval"></div><div class="envbar"><i></i></div></div>`;
    tcp.appendChild(e);
    tcpEls.push(e);
  }
}
// envelope value at the edit cursor (beat 200 = bar 51): what REAPER's lane fader shows
function renderTCP(t) {
  let y = 0;
  const h0 = envLaneH(t, 0), h1 = envLaneH(t, 1);
  for (let n = 0; n < VIS.length; n++) {
    tcpEls[n].style.top = y + 'px';
    y += RH;
    if (VIS[n].i === GM) {
      const e0 = tcpEls[VIS.length], e1 = tcpEls[VIS.length + 1];
      e0.style.top = y + 'px'; e0.style.height = h0 + 'px'; e0.style.opacity = h0 > 1 ? 1 : 0; y += h0;
      e1.style.top = y + 'px'; e1.style.height = h1 + 'px'; e1.style.opacity = h1 > 1 ? 1 : 0; y += h1;
    }
  }
  const e0 = tcpEls[VIS.length], e1 = tcpEls[VIS.length + 1];
  e0.querySelector('.envval').textContent = '100.0%';
  e0.querySelector('i').style.width = '100%';
  e1.querySelector('.envval').textContent = '-40.0 dB';
  e1.querySelector('i').style.width = '33%';
  // GM Synth I/O LED lights when the kick receive lands
  const gmRow = tcpEls[VIS.findIndex((x) => x.i === GM)];
  gmRow.querySelector('.led.io').classList.toggle('on', t >= A + 1.3);
  // status line under the TCP
  let info = 'Pizzicato (High)';
  if (t >= A) {
    const nfx = t >= A + 1.0 ? 5 : (t >= A + 0.5 ? 4 : 3);
    info = `GM Synth ${t >= A + 1.3 ? '[recvs] 1 ' : ''}[FX] ${nfx} (AUi: DLSMusicDevice (Apple))`;
  }
  const ti = $('tcpinfo');
  if (ti._s !== info) { ti._s = info; ti.textContent = info; }
}
function rowY(t, idx) { // y (relative to TCP top) of a visible track row and its height
  let y = 0;
  for (const tr of VIS) {
    if (tr.i === idx) return y;
    y += RH;
    if (tr.i === GM) y += envLaneH(t, 0) + envLaneH(t, 1);
  }
  return y;
}

// ---------------------------------------------------------------- REAPER: arrange (canvas)
const CVH = 637, CR = SOCIAL ? 3 : 1; // canvas CSS height, backing-store scale (crisp under the social zoom)
const AW = 1181, B0 = 168, B1 = 264, PPB = AW / (B1 - B0);
const RULER = 86; // canvas y where track lanes start (screen y 115)
const xb = (b) => (b - B0) * PPB;
// deterministic PRNG
function rng(seed) { let s = seed >>> 0; return () => { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; }; }
// chord chart from the real markers 37..47 (bars 51-59), repeated as the 8-bar loop either side
const MARKERS = [[200, 37, 'Fmaj7'], [204, 38, 'E/F'], [206, 39, 'E'], [208, 40, 'Em7'], [212, 41, 'Am7'], [214, 42, 'C6'],
  [216, 43, 'Fmaj7'], [220, 44, 'E/F'], [222, 45, 'E'], [224, 46, 'Em7'], [229, 47, 'C7']];
const CHORD = { 'Fmaj7': [53, 57, 60, 64], 'E/F': [53, 56, 59, 64], 'E': [52, 56, 59, 64], 'Em7': [52, 55, 59, 62],
  'Am7': [57, 60, 64, 67], 'C6': [60, 64, 67, 69], 'C7': [60, 64, 67, 70] };
function chordAt(b) {
  const rel = ((b - 200) % 32 + 32) % 32 + 200;
  let c = 'Fmaj7';
  for (const m of MARKERS) if (rel >= m[0]) c = m[2];
  return CHORD[c];
}
const NOTES = {}; // track index -> [{s, l, p}] with p normalised 0..1 inside the item
function genNotes() {
  const add = (i, s, l, p) => (NOTES[i] = NOTES[i] || []).push({ s, l, p });
  const norm = (p, lo, hi) => (p - lo) / (hi - lo);
  for (let bar = 43; bar <= 67; bar++) {
    const b = (bar - 1) * 4;
    const r = rng(bar * 31 + 7);
    // #3 Drums: kick / snare / hats
    for (const k of [0, 1.5, 2.5]) add(2, b + k, 0.2, 0.08);
    for (const k of [1, 3]) add(2, b + k, 0.2, 0.38);
    for (let k = 0; k < 8; k++) add(2, b + k * 0.5, 0.15, 0.78 + (k % 2) * 0.08);
    if (bar % 4 === 3) add(2, b, 0.25, 0.96);
    // #4 808: root-ish notes
    const roots = [0, 1.5, 2.5, 3.25];
    for (let k = 0; k < 4; k++) add(3, b + roots[k], k === 0 ? 1.4 : 0.6, 0.2 + (chordAt(b)[0] % 12) / 30 + r() * 0.15);
    // #5 Pizzicato (High): only bars 51-58 (C5-E6), 16th arpeggios
    if (bar >= 51 && bar < 59) {
      for (let k = 0; k < 16; k++) {
        const ch = chordAt(b + k * 0.25);
        const p = ch[(k + (k >> 2)) % ch.length] + 12 + (k % 8 >= 4 ? 12 : 0);
        add(4, b + k * 0.25, 0.2, clamp(norm(p, 72, 88), 0.02, 0.98));
        if (k % 2 === 0 && r() < 0.55) add(4, b + k * 0.25, 0.2, clamp(norm(ch[(k + 2) % ch.length] + 12, 72, 88), 0.02, 0.98));
      }
    }
    // #6/#7 Violin Ensemble: sustained chord tones C3-E4, one pair per chord change
    for (const [ti, off] of [[5, 0], [6, 0.03]]) {
      const changes = MARKERS.map((m) => m[0] - 200).filter((x) => x >= 0);
      const rel = ((b - 200) % 32 + 32) % 32;
      for (let n = 0; n < changes.length; n++) {
        const s = changes[n];
        if (s < rel || s >= rel + 4) continue;
        const e = n + 1 < changes.length ? changes[n + 1] : 32;
        const ch = chordAt(b + (s - rel));
        for (const p of [ch[0], ch[2]]) add(ti, b + (s - rel) + off, e - s - 0.1, clamp(norm(p, 48, 64), 0.03, 0.97));
      }
    }
    // #8 City Pop Synth: off-beat stabs C3-E5
    for (const k of [0.5, 1.5, 2, 2.75, 3.5]) {
      const ch = chordAt(b + k);
      for (const p of ch.slice(0, 3)) add(7, b + k, 0.35, clamp(norm(p + 12, 48, 76), 0.03, 0.97));
    }
    // #9 Electric Piano: rests in bars 51-58, comps elsewhere
    if (bar < 51 || bar >= 59) {
      for (const k of [0, 2]) { const ch = chordAt(b + k); for (const p of ch) add(8, b + k, 1.8, clamp(norm(p, 50, 72), 0.03, 0.97)); }
    }
    // #13/#14 Violin Fills: a few high notes at phrase ends
    if (bar % 2 === 0) for (const [ti, off] of [[12, 0], [13, 0.024]]) {
      add(ti, b + 2.5 + off, 0.45, 0.2 + r() * 0.6); add(ti, b + 3 + off, 0.9, 0.3 + r() * 0.5);
      if (r() < 0.4) add(ti, b + 1.5 + off, 0.5, 0.5 + r() * 0.4);
    }
  }
}
const ENV = D.gm_envs; // real points
function envPts(k, t) {
  const pts = ENV[k].pts.map((p) => p.slice());
  // before the "end at bar 59" run_lua the step stays on to the end of the view
  const capP = ease((t - T_CAP) / 0.3);
  const on = pts[2][1], off = pts[0][1];
  return { pts: pts.filter((p) => p[0] < 231 || capP > 0), capP, on, off };
}
function drawArrange(t) {
  const c = $('arr'), g = c.getContext('2d');
  g.setTransform(CR, 0, 0, CR, 0, 0);
  g.clearRect(0, 0, AW, CVH);
  // ruler
  g.fillStyle = '#414141'; g.fillRect(0, 0, AW, 33);
  g.fillStyle = '#444444'; g.fillRect(0, 33, AW, 1);
  g.fillStyle = '#3f3f3f'; g.fillRect(0, 34, AW, RULER - 34);
  // time selection band in the ruler
  g.fillStyle = 'rgba(255,255,255,0.10)'; g.fillRect(xb(200), 34, xb(232) - xb(200), RULER - 34);
  for (let bt = B0; bt <= B1; bt++) {
    const x = Math.round(xb(bt)) + 0.5;
    const bar = bt / 4 + 1;
    if (bt % 4 === 0) {
      const lab = bar % 2 === 1;
      g.strokeStyle = lab ? '#626262' : '#555555'; g.lineWidth = 1;
      g.beginPath(); g.moveTo(x, lab ? 49 : 70); g.lineTo(x, RULER); g.stroke();
      if (lab) {
        g.fillStyle = '#6c6c6c'; g.font = '600 12px "Fira Sans"';
        g.fillText(bar + '.1', x + 3, 61);
        const sec = (bar - 1) * 2;
        const ts = Math.floor(sec / 60) + ':' + String(sec % 60).padStart(2, '0') + '.000';
        g.fillStyle = '#5f5f5f'; g.fillRect(x + 2, 64, 42, 1);
        g.font = '600 11px "Fira Sans"'; g.fillText(ts, x + 3, 77);
      }
    } else {
      g.strokeStyle = '#4e4e4e'; g.beginPath(); g.moveTo(x, RULER - 6); g.lineTo(x, RULER); g.stroke();
    }
  }
  // markers (real: 37..47 from the context block)
  g.font = '500 11px "Fira Sans"';
  for (let n = 0; n < MARKERS.length; n++) {
    const [bt, num, name] = MARKERS[n];
    const x = Math.round(xb(bt));
    const nx = n + 1 < MARKERS.length ? xb(MARKERS[n + 1][0]) : AW;
    const nw = g.measureText(String(num)).width + 6;
    g.fillStyle = '#a5453f'; g.fillRect(x, 36, nw, 13);
    g.fillStyle = '#f2f2f2'; g.fillText(String(num), x + 3, 46.5);
    g.save(); g.beginPath(); g.rect(x + nw, 34, Math.max(0, nx - x - nw - 2), 16); g.clip();
    g.fillStyle = '#c9c9c9'; g.fillText(name, x + nw + 3, 46.5); g.restore();
  }
  // track lanes
  const laneTop = RULER;
  let y = laneTop;
  const drawLane = (tr, h) => {
    g.fillStyle = tr.sel ? '#626262' : '#575757'; g.fillRect(0, y, AW, h);
    g.fillStyle = '#3f3f3f'; g.fillRect(0, y + h - 1, AW, 1);
    // items
    for (const [s, e] of tr.items) {
      if (e <= B0 || s >= B1) continue;
      const x0 = Math.max(xb(s), -2), x1 = Math.min(xb(e), AW + 2);
      const iy = y + 1, ih = h - 3;
      g.fillStyle = tr.col === 0x1000000 ? '#34393e' : '#6f7b85'; g.fillRect(x0, iy, x1 - x0, ih);
      g.fillStyle = 'rgba(0,0,0,0.10)'; g.fillRect(x0, iy, x1 - x0, 1);
      if (xb(s) > 0) { g.fillStyle = 'rgba(0,0,0,0.45)'; g.fillRect(xb(s), iy, 1, ih); }
      const notes = NOTES[tr.i] || [];
      g.fillStyle = tr.col === 0x1000000 ? '#c9d2da' : '#151c22';
      for (const nt of notes) {
        if (nt.s + nt.l < s || nt.s > e) continue;
        const nx = xb(nt.s), nw = Math.max(2, nt.l * PPB - 1);
        const ny = iy + 4 + (1 - nt.p) * (ih - 10);
        g.fillRect(Math.round(nx), Math.round(ny), Math.round(nw), 2);
      }
    }
    y += h;
  };
  for (const tr of VIS) {
    drawLane(tr, RH);
    if (tr.i === GM) {
      for (let k = 0; k < 2; k++) {
        const h = envLaneH(t, k);
        if (h < 1) continue;
        g.save(); g.beginPath(); g.rect(0, y, AW, h); g.clip();
        g.fillStyle = '#404040'; g.fillRect(0, y, AW, h);
        g.fillStyle = '#2a2a2a'; g.fillRect(0, y + h - 2, AW, 2);
        const { pts, capP, on, off } = envPts(k, t);
        const top = y + 6, hh = EH - 13;
        const vy = (v) => top + (1 - v) * hh;
        const pa = ease((t - (A + 1.55 + k * 0.25)) / 0.25); // fade the line in with the lane
        g.globalAlpha = pa;
        g.strokeStyle = '#b48ce0'; g.lineWidth = 1.6; g.beginPath();
        let first = true;
        for (const p of pts) { const X = xb(p[0]), Y = vy(p[1]); if (first) { g.moveTo(X, Y); first = false; } else g.lineTo(X, Y); }
        let endV = on;
        if (capP > 0) endV = on + (off - on) * capP;
        if (capP <= 0) g.lineTo(AW + 2, vy(on)); else g.lineTo(AW + 2, vy(endV));
        g.stroke();
        g.fillStyle = 'rgba(180,140,224,0.10)';
        g.fillStyle = '#d9c4f2';
        for (const p of pts) { if (p[0] < B0) continue; g.fillRect(Math.round(xb(p[0])) - 2, Math.round(vy(p[1])) - 2, 4, 4); }
        g.globalAlpha = 1;
        g.restore();
        y += h;
      }
    }
  }
  if (y < CVH) { g.fillStyle = '#5f5f5f'; g.fillRect(0, y, AW, CVH - y); }
  // grid over the lanes
  for (let bt = B0; bt <= B1; bt++) {
    const x = Math.round(xb(bt)) + 0.5;
    g.strokeStyle = bt % 4 === 0 ? 'rgba(0,0,0,0.22)' : 'rgba(0,0,0,0.07)';
    g.beginPath(); g.moveTo(x, laneTop); g.lineTo(x, CVH); g.stroke();
  }
  // marker lines
  g.setLineDash([3, 4]); g.strokeStyle = 'rgba(190,85,78,0.40)';
  for (const m of MARKERS) { const x = Math.round(xb(m[0])) + 0.5; g.beginPath(); g.moveTo(x, laneTop); g.lineTo(x, CVH); g.stroke(); }
  g.setLineDash([]);
  // time selection
  g.fillStyle = 'rgba(255,255,255,0.11)'; g.fillRect(xb(200), laneTop, xb(232) - xb(200), CVH - laneTop);
  // edit cursor at bar 51
  const cx = Math.round(xb(200)) + 0.5;
  g.strokeStyle = '#e6e6e6'; g.lineWidth = 1; g.beginPath(); g.moveTo(cx, 34); g.lineTo(cx, CVH); g.stroke();
  g.fillStyle = '#e6e6e6'; g.beginPath(); g.moveTo(cx - 5, RULER - 7); g.lineTo(cx + 5, RULER - 7); g.lineTo(cx, RULER - 1); g.closePath(); g.fill();
}

// ---------------------------------------------------------------- REAPER: receive tooltip, mixer
function renderTip(t) {
  const el = $('tip');
  const a = ease((t - (A + 1.3)) / 0.18) * (1 - ease((t - (A + O.tipOut)) / 0.25));
  el.style.opacity = a;
  if (a > 0) {
    if (!el._s) { el._s = 1; el.innerHTML = 'Receive from track 18 “Kick” → GM Synth 3/4 <span style="color:#6e7781">(sidechain, pre-fader)</span>'; }
    const y = 115 + rowY(t, GM);
    if (SOCIAL) { el.style.left = '700px'; el.style.top = (y + 9) + 'px'; } else { el.style.left = '306px'; el.style.top = (y + 22) + 'px'; }
  }
}
function buildMixer() {
  const m = $('mixer');
  const clip = (sx, sy, w, h, dx, dy) => {
    const d = document.createElement('div'); d.className = 'clip';
    Object.assign(d.style, { left: dx + 'px', top: dy + 'px', width: w + 'px', height: h + 'px', backgroundPosition: `${-sx}px ${-sy}px` });
    m.appendChild(d); return d;
  };
  const slot = (n) => 202 + 86 * (n - 7);
  // the real screenshot is from a sibling project with one extra strip ("Pizz Bright") at #11: drop it so the
  // strips line up with Embark_demo (#11 Strings Sampler ... #21 Strings Room)
  clip(slot(12), 709, slot(21) - slot(12) - 2, 220, slot(11), 709);
  clip(slot(16), 709, 84, 220, slot(20), 709);            // #20 Strings Hall (0.00 dB return) = same look as Pizz Verb
  clip(slot(17), 709, 1479 - slot(21), 220, slot(21), 709);
  { const d = document.createElement('div'); Object.assign(d.style, { position: 'absolute', left: slot(17) + 'px', top: '709px', width: '86px', height: '8px', background: '#3d3d3d' }); m.appendChild(d); } // #21 Strings Room (partly visible)
  const name = (n, s) => { const d = document.createElement('div'); d.className = 'mxname'; d.style.left = slot(n) + 'px'; d.textContent = s; m.appendChild(d); };
  name(20, 'Strings Hall'); name(17, 'Synth Lead');
  const nm = document.createElement('div'); nm.className = 'mxname'; nm.style.left = slot(21) + 'px'; nm.style.width = (1479 - slot(21)) + 'px';
  nm.style.textAlign = 'left'; nm.style.paddingLeft = '4px'; nm.textContent = 'Strings Room (e'; m.appendChild(nm);
  for (let n = 11; n <= 21; n++) {
    const d = document.createElement('div'); d.className = 'mxnum' + (n === 17 ? ' black' : '');
    d.style.left = slot(n) + 'px'; if (n === 21) d.style.width = (1479 - slot(21)) + 'px';
    d.textContent = n; d.style.color = n === 17 ? '#c2c2c2' : '#bebebe'; m.appendChild(d);
  }
}
function renderMixer(t) {
  // GM Synth strip FX button blinks as each FX lands
  let a = 0;
  for (const tt of [A + 0.5, A + 1.0]) a = Math.max(a, 1 - Math.abs(t - tt - 0.12) / 0.3);
  $('mxflash').style.opacity = clamp(a, 0, 1) * 0.35;
}
function renderSelBox() {
  const el = $('selbox');
  if (el._b) return; el._b = 1;
  el.innerHTML = '<span style="left:86px;width:68px">51.1.00</span><span style="left:157px;width:68px">59.1.00</span><span style="left:228px;width:66px">8.0.00</span>';
}

// ---------------------------------------------------------------- REAPER: FX chain window
const FX_BEFORE = D.tracks[GM].fx;
const FX_AFTER = D.gm_after_fx;
function eqSVG() {
  const W = 322, H = 210;
  const fx = (f) => 8 + (Math.log10(f) - Math.log10(20)) / 3 * (W - 16);
  const dy = (db) => H / 2 - db * 7;
  const resp = (f) => {
    const o = Math.log2(f / 3000); const peak = 2.5 * Math.exp(-(o * o) / (2 * 0.55 * 0.55));
    const shelf = 4 / (1 + Math.exp(-(Math.log2(f / 7000)) * 4));
    return peak + shelf;
  };
  let d = '';
  for (let i = 0; i <= 200; i++) { const f = 20 * Math.pow(1000, i / 200); d += (i ? 'L' : 'M') + fx(f).toFixed(1) + ' ' + dy(resp(f)).toFixed(1); }
  let grid = '';
  for (const f of [50, 100, 200, 500, 1000, 2000, 5000, 10000]) grid += `<line x1="${fx(f)}" x2="${fx(f)}" y1="0" y2="${H}" stroke="#3c3c3c"/>`;
  for (const db of [-12, -6, 0, 6, 12]) grid += `<line x1="0" x2="${W}" y1="${dy(db)}" y2="${dy(db)}" stroke="${db === 0 ? '#555' : '#3c3c3c'}"/>`;
  const labs = [[100, '100'], [1000, '1k'], [10000, '10k']].map(([f, s]) => `<text x="${fx(f) + 3}" y="${H - 6}" fill="#7a7a7a" font-size="10" font-family="Inter">${s}</text>`).join('');
  const dots = [[3000, '2'], [7000, '3']].map(([f, s]) => `<circle cx="${fx(f)}" cy="${dy(resp(f))}" r="6" fill="none" stroke="#f0c050" stroke-width="1.5"/><text x="${fx(f) - 3}" y="${dy(resp(f)) - 9}" fill="#f0c050" font-size="10" font-family="Inter">${s}</text>`).join('');
  return `<svg width="${W}" height="${H}" style="position:absolute;left:0;top:0">${grid}${labs}<path d="${d}" fill="none" stroke="#7fd27f" stroke-width="2"/>${dots}</svg>` +
    `<div style="position:absolute;left:8px;top:6px;font:500 10.5px Inter;color:#9a9a9a">ReaEQ · band 2 band +2.5 dB @ 3.0 kHz · band 3 high shelf +4.0 dB @ 7.0 kHz</div>`;
}
function compHTML() {
  const rows = [['Threshold', '-40.0 dB', 0.33], ['Ratio', '10.0:1', 0.62], ['Attack', '1.0 ms', 0.05], ['Release', '120 ms', 0.24],
    ['Detector input', 'Auxiliary L+R (3/4)', 1], ['Detector highpass', '800 Hz', 0.55], ['Wet', '+0.0 dB', 0.8]];
  return `<div style="position:absolute;left:10px;top:7px;font:500 10.5px Inter;color:#9a9a9a">ReaComp · keyed by the kick receive on 3/4</div>` +
    rows.map(([k, v, p], i) => `<div class="prm" style="top:${26 + i * 25}px">${k}<span class="v">${v}</span><div class="bar"><i style="width:${p * 100}%"></i></div></div>`).join('');
}
const EQ_HTML = eqSVG(), COMP_HTML = compHTML();
function renderFX(t) {
  const w = $('fxwin');
  const a = ease((t - A) / 0.15) * (1 - ease((t - T_FXCLOSE) / 0.15));
  w.style.opacity = a;
  w.style.transform = `scale(${0.985 + 0.015 * a})`;
  if (a <= 0) return;
  const n = t >= A + 1.0 ? 5 : (t >= A + 0.5 ? 4 : 3);
  const selI = n - 1;
  const key = n;
  if (w._k === key) return;
  w._k = key;
  const list = FX_AFTER.slice(0, n);
  const label = (s, i) => (i === 0 ? s : (i < 3 ? 'JS/VST: ' + s : s));
  $('fxlist').innerHTML = list.map((s, i) => `<div class="fxi${i === selI ? ' sel' : ''}">${esc(i === 0 ? s : s)}</div>`).join('');
  const plug = selI === 3 ? 'VST: ReaEQ (Cockos)' : selI === 4 ? 'VST: ReaComp (Cockos)' : 'JS: RBJ Highpass';
  $('fxhead').textContent = n === 3 ? 'Pizz EQ bold (-3 @400, air +2.5 @9k)' : list[selI];
  $('fxui').innerHTML = n === 4 ? EQ_HTML : n === 5 ? COMP_HTML : '<div style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;color:#777;font:12px Inter">ReaEQ</div>';
}

// ---------------------------------------------------------------- Hibiki panel
const LANE_GLYPH = '<svg width="14" height="14"><path d="M1.5 4.6 L4.7 7 L1.5 9.4" fill="none" stroke="#8c8c8c" stroke-width="1.3"/><path d="M6 9.4 H11" stroke="#8c8c8c" stroke-width="1.3"/></svg>';
function inline(s) {
  // the panel's parse_inline: `code`, **bold** toggles (unterminated bold runs to the end, as while streaming)
  let out = '', bold = false, i = 0;
  while (i < s.length) {
    const c = s[i];
    if (c === '`') {
      const j = s.indexOf('`', i + 1);
      if (j > 0) { out += '<code>' + esc(s.slice(i + 1, j)) + '</code>'; i = j + 1; continue; }
    }
    if (c === '*' && s[i + 1] === '*') { out += bold ? '</b>' : '<b>'; bold = !bold; i += 2; continue; }
    out += esc(c); i++;
  }
  if (bold) out += '</b>';
  return out;
}
function mdHTML(text) {
  const lines = text.split('\n');
  let h = '';
  for (const l of lines) {
    if (/^\s*$/.test(l)) h += '<div class="gap"></div>';
    else if (/^\s*[-*+]\s/.test(l)) h += '<div class="li"><span class="bu">•</span>' + inline(l.replace(/^\s*[-*+]\s+/, '')) + '</div>';
    else h += '<div>' + inline(l) + '</div>';
  }
  return h;
}
function laneHTML(labels) {
  const order = [], cnt = {};
  for (const l of labels) { if (!cnt[l]) { cnt[l] = 0; order.push(l); } cnt[l]++; }
  const parts = order.map((l) => cnt[l] > 1 ? `${l} ×${cnt[l]}` : l);
  const count = labels.length === 1 ? '1 call' : `${labels.length} calls`;
  return `<div class="lane">${LANE_GLYPH}<span class="t">${esc(parts.join(' · '))}<span class="n">${count}</span></span></div>`;
}
const SPIN = ['·  ', '·· ', '···', ' ··', '  ·', '   '];
const CTX_SUM = 'Embark_demo · 51.1.00 · time selection';
function logHTML(t) {
  if (t < S0) return '';
  let h = `<div class="ctxtag"><div class="l"></div><div class="a">REAPER context<span class="b">${CTX_SUM}</span></div></div>`;
  h += `<div class="bubble">${esc(PROMPT)}</div>`;
  for (const b of BLOCKS) {
    if (b.kind === 'md') {
      if (t < b.at) break;
      const n = Math.min(b.chunks.length, Math.floor((t - b.at) / b.per) + 1);
      h += `<div class="md">${mdHTML(b.chunks.slice(0, n).join(''))}</div>`;
    } else {
      const labels = b.tools.filter(([at]) => t >= at).map(([, l]) => l);
      if (labels.length) h += laneHTML(labels);
      if (labels.length < b.tools.length) break;
    }
  }
  if (t < T_DONE) h += `<div class="working">Working ${SPIN[Math.floor(t * 6) % SPIN.length]}</div>`;
  return h;
}
let scrollY = 0, lastLog = '';
let inScroll = 0;
const meas = document.createElement('canvas').getContext('2d');
function renderPanel(t) {
  // header buttons: stop while a turn runs
  const active = t >= S0 && t < T_DONE;
  const hb = $('hbtns');
  const hk = active ? 'a' : 'i';
  if (hb._k !== hk) {
    hb._k = hk;
    const icons = [];
    if (active) icons.push('<svg width="25" height="25"><rect x="8.5" y="8.5" width="8" height="8" rx="1.5" fill="#f47067"/></svg>');
    icons.push('<svg width="25" height="25"><circle cx="12.5" cy="12.5" r="6" fill="none" stroke="#c8c8c8" stroke-width="1.5"/><path d="M12.5 13V9.5M12.5 13l2.5 1.5" stroke="#c8c8c8" stroke-width="1.5" fill="none"/></svg>');
    icons.push('<svg width="25" height="25"><path d="M7.5 12.5h10M12.5 7.5v10" stroke="#c8c8c8" stroke-width="1.6"/></svg>');
    const n = icons.length, w = n * 25 + (n - 1) * 3 + 10;
    hb.style.width = w + 'px';
    hb.innerHTML = icons.map((s, i) => `<div class="ib" style="left:${5 + i * 28}px">${s}</div>`).join('');
  }
  // title next to the agent field
  const ag = $('agent');
  $('ctitle').style.left = (5 + ag.offsetWidth + 8) + 'px';
  // log
  const html = logHTML(t);
  const lc = $('logc');
  if (html !== lastLog) { lastLog = html; lc.innerHTML = html; }
  const ch = lc.offsetHeight, vh = 722;
  const target = Math.max(0, ch - vh);
  scrollY += (target - scrollY) * 0.32;
  if (Math.abs(target - scrollY) < 0.5) scrollY = target;
  lc.style.transform = `translateY(${-Math.round(scrollY)}px)`;
  // composer
  const inp = $('input');
  const focused = t >= T_FOCUS;
  inp.classList.toggle('focus', focused);
  let typed = '';
  if (t >= T_TYPE0 && t < S0) { let n = 0; while (n < typeTimes.length && typeTimes[n] <= t) n++; typed = PROMPT.slice(0, n); }
  meas.font = '400 14px "Fira Sans"';
  const cw = meas.measureText(typed).width;
  const inner = 399 - 2 - 16;
  // ImGui's single-line scroll inside InputTextMultiline: jump by a quarter width when the caret leaves the box
  if (cw < inScroll) inScroll = Math.max(0, cw - inner * 0.25);
  else if (cw - inner >= inScroll) inScroll = cw - inner + inner * 0.25;
  if (!typed) inScroll = 0;
  const it = $('itext');
  it.textContent = typed;
  it.style.transform = `translateX(${-inScroll}px)`;
  const caret = $('caret');
  const blinkOn = (t < T_TYPE0 || t >= S0) ? (Math.floor((t - T_FOCUS) / 0.53) % 2 === 0) : true;
  const sinceKey = typed ? t - typeTimes[typed.length - 1] : 9;
  caret.style.left = (8 + cw - inScroll) + 'px';
  caret.style.opacity = focused && (blinkOn || sinceKey < 0.5) ? 1 : 0;
}
let laidOut = false;
function layoutStatic() {
  if (laidOut) return; laidOut = true;
  // pills: newest first, bevel widths = 8 + kind + 6 + value + 8
  const p1 = $('pill1'), p2 = $('pill2');
  const w1 = p1.firstChild.scrollWidth + 8, w2 = p2.firstChild.scrollWidth + 8;
  p1.style.left = '4px'; p1.style.width = w1 + 'px';
  p2.style.left = (4 + w1 + 4) + 'px'; p2.style.width = w2 + 'px';
  const f = $('cfile'), c = $('cctx');
  meas.font = '400 14px "Fira Sans"';
  const fw = meas.measureText('+ File').width + 18, cw = meas.measureText('Context').width + 30;
  f.style.left = '4px'; f.style.width = fw + 'px';
  c.style.left = (4 + fw + 2) + 'px'; c.style.width = cw + 'px';
  const led = c.querySelector('.led'); led.style.left = (cw - 11) + 'px'; led.style.top = '7.5px'; led.style.width = '4px'; led.style.height = '10px'; led.style.borderRadius = '2px';
}


// ---------------------------------------------------------------- social cut: virtual camera, caption, end card
const VW = SOCIAL === 'sq' ? 1080 : 1080, VH = SOCIAL === 'sq' ? 1080 : 1350;
const R = {
  wide: SOCIAL === 'sq' ? [1148, 178, 1910, 940] : [1170, 32, 1910, 957],
  composer: [1482, 560, 1908, 945],
  logtop: [1482, 30, 1908, 560],
  fx: SOCIAL === 'sq' ? [650, 40, 1330, 720] : [650, 30, 1330, 880],
  final: [1482, 250, 1908, 808],
};
function camOf(r) {
  const [x0, y0, x1, y1] = r;
  let s = Math.min(VW / (x1 - x0), VH / (y1 - y0));
  s = Math.max(s, VW / 1920, VH / 960);
  return { s, cx: (x0 + x1) / 2, cy: (y0 + y1) / 2 };
}
// [move start, move duration, target]
const CAM = [
  [0, 0, R.wide], [1.25, 0.75, R.composer], [S0 + 0.1, 0.7, R.logtop], [A - 0.35, 0.8, R.fx], [S0 + 9.75, 0.8, R.final],
];
function camera(t) {
  let cur = camOf(CAM[0][2]);
  for (let i = 1; i < CAM.length; i++) {
    const [t0, d, r] = CAM[i];
    if (t < t0) break;
    const nx = camOf(r), p = ease((t - t0) / d);
    cur = { s: Math.exp(Math.log(cur.s) + (Math.log(nx.s) - Math.log(cur.s)) * p), cx: cur.cx + (nx.cx - cur.cx) * p, cy: cur.cy + (nx.cy - cur.cy) * p };
  }
  const { s } = cur;
  const vx = clamp(cur.cx - VW / (2 * s), 0, 1920 - VW / s), vy = clamp(cur.cy - VH / (2 * s), 0, 960 - VH / s);
  return { s, vx, vy };
}
function setupSocial() {
  document.documentElement.classList.add('social', 'social-' + SOCIAL);
  document.documentElement.style.width = document.body.style.width = VW + 'px';
  document.documentElement.style.height = document.body.style.height = VH + 'px';
  $('fxwin').style.left = '700px'; $('fxwin').style.top = '112px';
  $('input').classList.add('padclip');
}
function renderSocial(t) {
  // opening shot: the brand badge sits centred in the first camera frame
  { const c = camera(0), b = $('brand'), w = b.offsetWidth, h = b.offsetHeight;
    b.style.right = 'auto'; b.style.left = (c.vx + VW / (2 * c.s) - w / 2) + 'px'; b.style.top = (c.vy + VH / (2 * c.s) * 0.8 - h / 2) + 'px'; }
  const c = camera(t);
  $('stage').style.transform = `scale(${c.s}) translate(${-c.vx}px, ${-c.vy}px)`;
  const cap = $('caption');
  const a = ease((t - T_CAPTION) / 0.25) * (1 - ease((t - (T_CAPTION + 2.5)) / 0.25));
  cap.style.opacity = a;
  cap.style.transform = `translate(-50%, ${(1 - a) * 18}px)`;
  $('send').style.opacity = ease((t - T_END0) / 0.4);
}

// ---------------------------------------------------------------- frame
function render(t) {
  layoutStatic();
  renderTitle(t);
  renderTCP(t);
  drawArrange(t);
  renderTip(t);
  renderMixer(t);
  renderSelBox();
  renderFX(t);
  renderPanel(t);
  $('brand').style.opacity = SOCIAL ? 1 - ease((t - 1.0) / 0.25) : (t < 0.3 ? ease(t / 0.3) : 1 - ease((t - 2.2) / 0.5));
  if (SOCIAL) renderSocial(t); else $('endcard').style.opacity = ease((t - T_END0) / 0.6);
}
{ const c = $('arr'); c.width = 1181 * CR; c.height = 637 * CR; c.style.width = '1181px'; c.style.height = '637px'; }
if (SOCIAL) setupSocial();
genNotes();
buildTCP();
buildMixer();
window.seek = (t) => { render(t); return true; };
window.ready = document.fonts.ready.then(() => document.fonts.load('400 14px "Fira Sans"')).then(() =>
  Promise.all(['600 12px "Fira Sans"', '700 14px "Fira Sans"', '500 11px "Fira Sans"', '600 14px Inter', '400 14px Inter', '400 13px Mono', '600 44px Inter'].map((f) => document.fonts.load(f))))
  .then(() => { render(0); return window.TIMELINE; });
