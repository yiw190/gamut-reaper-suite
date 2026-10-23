// usage: node record.mjs stills t1,t2,... | node record.mjs video out.mp4
import { chromium } from 'playwright';
import { spawn } from 'node:child_process';
import path from 'node:path';
import url from 'node:url';

const here = path.dirname(url.fileURLToPath(import.meta.url));
const FPS = 30;
const [mode, arg, social = ''] = process.argv.slice(2);
const VP = social === 'sq' ? { width: 1080, height: 1080 } : social ? { width: 1080, height: 1350 } : { width: 1920, height: 960 };
const browser = await chromium.launch({ args: ['--font-render-hinting=none', '--disable-lcd-text'] });
const page = await browser.newPage({ viewport: VP, deviceScaleFactor: 1 });
page.on('console', (m) => console.log('[page]', m.text()));
page.on('pageerror', (e) => console.log('[pageerror]', e.message));
await page.goto(url.pathToFileURL(path.join(here, 'index.html')).href + (social ? `?social=${social}` : ''));
const TL = await page.evaluate(() => window.ready);
console.log('timeline', JSON.stringify(TL));
const total = Math.ceil(TL.T_TOTAL * FPS);

if (mode === 'stills') {
  const want = arg.split(',').map((s) => {
    const [k, v] = s.split('=');
    return { name: k, t: v === undefined ? Number(k) : (isNaN(Number(v)) ? TL[v] : Number(v)) };
  }).sort((a, b) => a.t - b.t);
  let w = 0;
  for (let f = 0; f <= total && w < want.length; f++) {
    const t = f / FPS;
    await page.evaluate((t) => window.seek(t), t);
    while (w < want.length && want[w].t <= t + 1e-9) {
      const out = path.join(here, '..', 'out', 'stills', `${want[w].name}.png`);
      await page.screenshot({ path: out });
      console.log('still', out, t.toFixed(2));
      w++;
    }
  }
} else {
  const out = arg;
  const ff = spawn('ffmpeg', ['-y', '-loglevel', 'error', '-f', 'image2pipe', '-framerate', String(FPS), '-c:v', 'png', '-i', '-',
    '-c:v', 'libx264', '-preset', 'slow', '-crf', '20', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', '-r', String(FPS), out],
  { stdio: ['pipe', 'inherit', 'inherit'] });
  const t0 = Date.now();
  for (let f = 0; f < total; f++) {
    await page.evaluate((t) => window.seek(t), f / FPS);
    const buf = await page.screenshot({ type: 'png' });
    if (!ff.stdin.write(buf)) await new Promise((r) => ff.stdin.once('drain', r));
    if (f % 150 === 0) console.log(`frame ${f}/${total} ${((Date.now() - t0) / 1000).toFixed(0)}s`);
  }
  ff.stdin.end();
  await new Promise((r) => ff.on('close', r));
  console.log('wrote', out);
}
await browser.close();
