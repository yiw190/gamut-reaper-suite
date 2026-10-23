"""`hibiki ears`: what a short rendered excerpt sounds like, in numbers and one picture, for the agent.

Stdlib only (the installer's Python has no numpy): reads a PCM / float WAV, averages a mid + side spectrum over
up to 160 Hann frames of 4096 samples (one complex FFT per frame carries both), folds it into 1/3-octave bands,
fits the spectral tilt and lists the bands that stick out of it, then writes <stem>.png (spectrum + level over
time) and <stem>.md (the same as text) next to the WAV and prints the numbers as JSON.

    hibiki ears excerpt.wav [--label "bars 49-55"] [--lufs-i -8.4] [--lufs-s-max -7] [--tp -0.6]
"""

from __future__ import annotations

import argparse
import cmath
import json
import math
import struct
import sys
import zlib
from array import array
from pathlib import Path

N_FFT = 4096
MAX_FRAMES = 160
BANDS = [1000.0 * 2 ** (n / 3) for n in range(-16, 13)]  # 25 Hz .. 16 kHz, ISO 1/3-octave centres
NOMINAL = ["25", "31", "40", "50", "63", "80", "100", "125", "160", "200", "250", "315", "400", "500", "630", "800",
           "1k", "1.25k", "1.6k", "2k", "2.5k", "3.15k", "4k", "5k", "6.3k", "8k", "10k", "12.5k", "16k"]
REGIONS = [("sub", 20, 60), ("bass", 60, 250), ("low-mid", 250, 500), ("mid", 500, 2000),
           ("upper-mid", 2000, 6000), ("presence", 6000, 10000), ("air", 10000, 20000)]
FIT = (63.0, 12500.0)  # tilt fit range: below it the kick/808 dominates, above it the air rolls off
PEAK_DB, DIP_DB = 3.0, -4.0


def read_wav(path):
    """(sample rate, left, right) as float lists in -1..1; mono is duplicated."""
    data = Path(path).read_bytes()
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    fmt, pcm, i = None, None, 12
    while i + 8 <= len(data):
        cid, size = data[i:i + 4], struct.unpack("<I", data[i + 4:i + 8])[0]
        body = data[i + 8:i + 8 + size]
        if cid == b"fmt ":
            fmt = body
        elif cid == b"data":
            pcm = body
        i += 8 + size + (size & 1)
    if not fmt or pcm is None:
        raise ValueError("WAV without fmt or data chunk")
    tag, ch, rate = struct.unpack("<HHI", fmt[:8])
    bits = struct.unpack("<H", fmt[14:16])[0]
    if tag == 0xFFFE and len(fmt) >= 26:
        tag = struct.unpack("<H", fmt[24:26])[0]
    if tag == 3 and bits == 32:
        a, scale = array("f"), 1.0
        a.frombytes(pcm[:len(pcm) // 4 * 4])
    elif tag == 1 and bits == 16:
        a, scale = array("h"), 1 / 32768
        a.frombytes(pcm[:len(pcm) // 2 * 2])
    elif tag == 1 and bits == 24:
        n = len(pcm) // 3
        wide = bytearray(n * 4)  # each 3-byte sample becomes the top of an int32
        wide[1::4], wide[2::4], wide[3::4] = pcm[0:n * 3:3], pcm[1:n * 3:3], pcm[2:n * 3:3]
        a, scale = array("i"), 1 / 2147483648
        a.frombytes(bytes(wide))
    elif tag == 1 and bits == 32:
        a, scale = array("i"), 1 / 2147483648
        a.frombytes(pcm[:len(pcm) // 4 * 4])
    else:
        raise ValueError("unsupported WAV format %d / %d bit" % (tag, bits))
    if sys.byteorder == "big":
        a.byteswap()
    left = [v * scale for v in a[0::ch]]
    right = [v * scale for v in a[1::ch]] if ch > 1 else left
    n = min(len(left), len(right))
    return rate, left[:n], right[:n]


def fft(x):
    """In-place iterative radix-2 FFT of a complex list (len a power of two)."""
    n = len(x)
    j = 0
    for i in range(1, n):
        bit = n >> 1
        while j & bit:
            j ^= bit
            bit >>= 1
        j |= bit
        if i < j:
            x[i], x[j] = x[j], x[i]
    size = 2
    while size <= n:
        half = size // 2
        tw = [cmath.exp(-2j * math.pi * k / size) for k in range(half)]
        for start in range(0, n, size):
            for k in range(half):
                a = start + k
                b = a + half
                t = x[b] * tw[k]
                x[b] = x[a] - t
                x[a] = x[a] + t
        size *= 2
    return x


def spectra(rate, left, right):
    """Average one-sided power per bin for mid and side (RMS^2 scale), over evenly spread frames."""
    n = len(left)
    if n < N_FFT:
        pad = N_FFT - n
        left, right = left + [0.0] * pad, right + [0.0] * pad
        n = N_FFT
    hop = N_FFT // 2
    count = min(MAX_FRAMES, (n - N_FFT) // hop + 1)
    step = (n - N_FFT) / max(1, count - 1) if count > 1 else 0
    win = [0.5 - 0.5 * math.cos(2 * math.pi * i / N_FFT) for i in range(N_FFT)]
    norm = 2.0 / (sum(win) ** 2)  # one-sided, so a sine of amplitude A sums to A^2 / 2 around its bin
    half = N_FFT // 2
    pm, ps = [0.0] * (half + 1), [0.0] * (half + 1)
    for f in range(count):
        s = int(round(f * step))
        z = [complex((left[s + i] + right[s + i]) * 0.5 * win[i], (left[s + i] - right[s + i]) * 0.5 * win[i])
             for i in range(N_FFT)]
        fft(z)
        for k in range(half + 1):
            a, b = z[k], z[-k].conjugate()
            m, sd = (a + b) * 0.5, (a - b) * -0.5j
            pm[k] += m.real * m.real + m.imag * m.imag
            ps[k] += sd.real * sd.real + sd.imag * sd.imag
    scale = norm / count
    return [v * scale for v in pm], [v * scale for v in ps], count


def db(p):
    return 10 * math.log10(p) if p > 1e-20 else -200.0


def band_levels(rate, power, centres, width=2 ** (1 / 6)):
    df = rate / N_FFT
    out = []
    for f in centres:
        lo, hi = f / width, f * width
        k0, k1 = max(1, int(math.ceil(lo / df))), min(len(power) - 1, int(math.floor(hi / df)))
        out.append(db(sum(power[k0:k1 + 1])) if k1 >= k0 else db(power[min(len(power) - 1, max(1, int(round(f / df))))]))
    return out


def region_power(rate, power, lo, hi):
    df = rate / N_FFT
    return sum(power[max(1, int(lo / df)):min(len(power), int(hi / df) + 1)])


def tilt_fit(levels):
    pts = [(math.log2(f), lv) for f, lv in zip(BANDS, levels) if FIT[0] <= f <= FIT[1] and lv > -150]
    if len(pts) < 3:
        return 0.0, 0.0
    mx = sum(p[0] for p in pts) / len(pts)
    my = sum(p[1] for p in pts) / len(pts)
    sxx = sum((p[0] - mx) ** 2 for p in pts)
    slope = sum((p[0] - mx) * (p[1] - my) for p in pts) / sxx if sxx else 0.0
    return slope, my - slope * mx


def level_curve(rate, left, right, win_s=0.4):
    w = max(1, int(rate * win_s))
    out = []
    for s in range(0, len(left) - w + 1, w):
        acc = 0.0
        for i in range(s, s + w):
            acc += left[i] * left[i] + right[i] * right[i]
        out.append(db(acc / (2 * w)))
    return out


def analyse(path, label=None, reaper=None):
    rate, left, right = read_wav(path)
    dur = len(left) / rate
    pm, ps, frames = spectra(rate, left, right)
    mid = band_levels(rate, pm, BANDS)
    side = band_levels(rate, ps, BANDS)
    slope, icept = tilt_fit(mid)
    resid = [lv - (icept + slope * math.log2(f)) for f, lv in zip(BANDS, mid)]
    total = sum(pm[1:])
    regions = {name: round(db(region_power(rate, pm, lo, hi)) - db(total), 1) for name, lo, hi in REGIONS}
    sll = sum(v * v for v in left)
    srr = sum(v * v for v in right)
    slr = sum(a * b for a, b in zip(left, right))
    corr = slr / math.sqrt(sll * srr) if sll > 1e-12 and srr > 1e-12 else 1.0
    lows = (region_power(rate, ps, 20, 120), region_power(rate, pm, 20, 120))
    peak = max(max(abs(v) for v in left), max(abs(v) for v in right)) if left else 0.0
    rms = math.sqrt((sll + srr) / (2 * len(left))) if left else 0.0
    curve = level_curve(rate, left, right)
    fit_band = [FIT[0] <= f <= FIT[1] for f in BANDS]
    res = {
        "file": str(path), "label": label or "", "seconds": round(dur, 2), "sample_rate": rate, "frames": frames,
        "reaper": reaper or {},
        "tilt_db_per_octave": round(slope, 2),
        "peaks_over_tilt": [{"band": NOMINAL[i], "db": round(r, 1)} for i, r in enumerate(resid) if fit_band[i] and r >= PEAK_DB],
        "dips_under_tilt": [{"band": NOMINAL[i], "db": round(r, 1)} for i, r in enumerate(resid) if fit_band[i] and r <= DIP_DB],
        "regions_db_of_total": regions,
        "stereo": {"correlation": round(corr, 2), "side_minus_mid_db": round(db(sum(ps[1:])) - db(total), 1),
                   "below_120hz_side_minus_mid_db": round(db(lows[0]) - db(lows[1]), 1)},
        "level": {"peak_dbfs": round(db(peak * peak), 1), "rms_dbfs": round(db(rms * rms), 1),
                  "crest_db": round(db(peak * peak) - db(rms * rms), 1),
                  "rms_400ms_min": round(min(curve), 1) if curve else None,
                  "rms_400ms_max": round(max(curve), 1) if curve else None},
        "bands": [{"band": NOMINAL[i], "mid_db": round(m, 1), "side_db": round(s, 1), "over_tilt_db": round(r, 1)}
                  for i, (m, s, r) in enumerate(zip(mid, side, resid))],
    }
    return res, (mid, side, slope, icept, resid, curve)


def summary_md(res):
    r = res["reaper"]
    lines = ["# Ears: %s (%.1f s)" % (res["label"] or Path(res["file"]).name, res["seconds"]), ""]
    if r:
        names = [("lufs_i", "LUFS-I %.1f"), ("lufs_s_max", "LUFS-S max %.1f"), ("lufs_m_max", "LUFS-M max %.1f"),
                 ("tp", "true peak %.1f dBTP"), ("peak", "sample peak %.1f dBFS")]
        lines.append("REAPER: " + " · ".join(f % r[k] for k, f in names if isinstance(r.get(k), (int, float))))
    lines.append("Spectrum (mid, 1/3-octave bands): tilt %.1f dB/oct over 63 Hz-12.5 kHz (pink noise reads 0; "
                 "darker than pink is negative)" % res["tilt_db_per_octave"])
    pk = ", ".join("%s +%.1f" % (p["band"], p["db"]) for p in res["peaks_over_tilt"]) or "none"
    dp = ", ".join("%s %.1f" % (p["band"], p["db"]) for p in res["dips_under_tilt"]) or "none"
    lines.append("Bands over the tilt (>= +%g dB): %s" % (PEAK_DB, pk))
    lines.append("Bands under the tilt (<= %g dB): %s" % (DIP_DB, dp))
    lines.append("Regions, dB of total energy: " + " · ".join("%s %.1f" % kv for kv in res["regions_db_of_total"].items()))
    st = res["stereo"]
    def side(v):
        return "below -60 dB vs mid" if v < -60 else "%.1f dB vs mid" % v
    lines.append("Stereo: correlation %.2f · side %s · below 120 Hz side %s"
                 % (st["correlation"], side(st["side_minus_mid_db"]), side(st["below_120hz_side_minus_mid_db"])))
    lv = res["level"]
    lines.append("Level: peak %.1f dBFS · RMS %.1f dBFS · crest %.1f dB · 400 ms RMS %s..%s dBFS"
                 % (lv["peak_dbfs"], lv["rms_dbfs"], lv["crest_db"], lv["rms_400ms_min"], lv["rms_400ms_max"]))
    lines.append("")
    lines.append("| band | mid dB | side dB | vs tilt |")
    lines.append("|---|---|---|---|")
    for b in res["bands"]:
        lines.append("| %s | %.1f | %.1f | %+.1f |" % (b["band"], b["mid_db"], b["side_db"], b["over_tilt_db"]))
    lines.append("")
    lines.append("Measured from a render of the master mix; spectrum numbers are approximate (Hann, %d frames)."
                 % res["frames"])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- picture

FONT = {  # 5x7 bitmap glyphs, rows top to bottom, '#' = ink
    '0': ' ### #   ##  ### # ###  ##   # ### ',
    '1': '  #   ##    #    #    #    #   ### ',
    '2': ' ### #   #    #   #   #   #   #####',
    '3': '#####   #   #     #     ##   # ### ',
    '4': '   #   ##  # # #  # #####   #    # ',
    '5': '######    ####     #    ##   # ### ',
    '6': '  ##  #   #    #### #   ##   # ### ',
    '7': '#####    #   #   #   #    #    #   ',
    '8': ' ### #   ##   # ### #   ##   # ### ',
    '9': ' ### #   ##   # ####    #   #  ##  ',
    '.': '                          ##   ##  ',
    '-': '               #####               ',
    '+': '       #    #  #####  #    #       ',
    ' ': '                                   ',
    '/': '    #    #   #   #   #   #    #    ',
    ':': '      ##   ##        ##   ##       ',
    'A': ' ### #   ##   #######   ##   ##   #',
    'B': '#### #   ##   ##### #   ##   ##### ',
    'C': ' ### #   ##    #    #    #   # ### ',
    'D': '###  #  # #   ##   ##   ##  # ###  ',
    'E': '######    #    #### #    #    #####',
    'F': '######    #    #### #    #    #    ',
    'G': ' ### #   ##    # ####   ##   # ### ',
    'H': '#   ##   ##   #######   ##   ##   #',
    'I': ' ###   #    #    #    #    #   ### ',
    'K': '#   ##  # # #  ##   # #  #  # #   #',
    'L': '#    #    #    #    #    #    #####',
    'M': '#   ### ### # ##   ##   ##   ##   #',
    'N': '#   ###  ## # ##  ###   ##   ##   #',
    'O': ' ### #   ##   ##   ##   ##   # ### ',
    'P': '#### #   ##   ##### #    #    #    ',
    'R': '#### #   ##   ##### # #  #  # #   #',
    'S': ' #####    #     ###     #    ##### ',
    'T': '#####  #    #    #    #    #    #  ',
    'U': '#   ##   ##   ##   ##   ##   # ### ',
    'V': '#   ##   ##   ##   ##   # # #   #  ',
    'W': '#   ##   ##   ## # ## # ### ###   #',
    'Z': '#####    #   #   #   #   #    #####',
}


class Canvas:
    def __init__(self, w, h, bg=(255, 255, 255)):
        self.w, self.h = w, h
        self.px = bytearray(bytes(bg) * (w * h))

    def dot(self, x, y, c):
        if 0 <= x < self.w and 0 <= y < self.h:
            i = (y * self.w + x) * 3
            self.px[i:i + 3] = bytes(c)

    def rect(self, x0, y0, x1, y1, c):
        for y in range(max(0, y0), min(self.h, y1)):
            i = (y * self.w + max(0, x0)) * 3
            n = max(0, min(self.w, x1) - max(0, x0))
            self.px[i:i + 3 * n] = bytes(c) * n

    def line(self, x0, y0, x1, y1, c, width=1, dash=0):
        dx, dy = abs(x1 - x0), abs(y1 - y0)
        steps = max(dx, dy, 1)
        for s in range(steps + 1):
            if dash and (s // dash) % 2:
                continue
            x = int(round(x0 + (x1 - x0) * s / steps))
            y = int(round(y0 + (y1 - y0) * s / steps))
            for o in range(width):
                self.dot(x, y + o - width // 2, c)

    def text(self, x, y, s, c, scale=1):
        for ch in s.upper():
            g = FONT.get(ch)
            if g:
                for r in range(7):
                    for col in range(5):
                        if g[r * 5 + col] == "#":
                            self.rect(x + col * scale, y + r * scale, x + (col + 1) * scale, y + (r + 1) * scale, c)
            x += 6 * scale

    def png(self, path):
        raw = b"".join(b"\x00" + bytes(self.px[y * self.w * 3:(y + 1) * self.w * 3]) for y in range(self.h))

        def chunk(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)

        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0))
                               + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


INK, GRID, BLUE, GREY, AMBER, RED, GREEN = (31, 35, 40), (216, 222, 228), (9, 105, 218), (140, 149, 159), \
    (154, 103, 0), (207, 34, 46), (26, 127, 55)


def picture(res, plot, path):
    mid, side, slope, icept, resid, curve = plot
    W, H = 760, 420
    cv = Canvas(W, H)
    x0, x1, y0, y1 = 48, W - 16, 30, 300
    finite = [v for v in mid + side if v > -150]
    top = 10 * math.ceil((max(finite) if finite else 0) / 10)
    bot = top - 60
    fx = lambda f: x0 + (math.log10(f) - math.log10(20)) / (math.log10(20000) - math.log10(20)) * (x1 - x0)
    fy = lambda v: y0 + (top - max(bot, min(top, v))) / (top - bot) * (y1 - y0)
    for v in range(int(bot), int(top) + 1, 10):
        y = int(fy(v))
        cv.line(x0, y, x1, y, GRID)
        cv.text(4, y - 3, "%d" % v, GREY)
    for f, lab in ((50, "50"), (100, "100"), (200, "200"), (500, "500"), (1000, "1K"), (2000, "2K"), (5000, "5K"),
                   (10000, "10K"), (20000, "20K")):
        x = int(fx(f))
        cv.line(x, y0, x, y1, GRID)
        cv.text(x - 3 * len(lab), y1 + 6, lab, GREY)
    pts = lambda vals: [(int(fx(f)), int(fy(v))) for f, v in zip(BANDS, vals)]
    a = (int(fx(FIT[0])), int(fy(icept + slope * math.log2(FIT[0]))))
    b = (int(fx(FIT[1])), int(fy(icept + slope * math.log2(FIT[1]))))
    cv.line(a[0], a[1], b[0], b[1], AMBER, 1, dash=6)
    for series, col, wd in ((side, GREY, 2), (mid, BLUE, 3)):
        p = pts(series)
        for (ax, ay), (bx, by) in zip(p, p[1:]):
            cv.line(ax, ay, bx, by, col, wd)
    for i, r in enumerate(resid):
        if FIT[0] <= BANDS[i] <= FIT[1] and (r >= PEAK_DB or r <= DIP_DB):
            x, y = int(fx(BANDS[i])), int(fy(mid[i]))
            cv.rect(x - 4, y - 4, x + 5, y + 5, RED if r > 0 else GREEN)
    title = "%s  %.1fS" % ((res["label"] or "EXCERPT").upper(), res["seconds"])
    cv.text(x0, 8, title, INK, 2)
    cv.text(x1 - 6 * 24, 12, "MID", BLUE)
    cv.text(x1 - 6 * 19, 12, "SIDE", GREY)
    cv.text(x1 - 6 * 13, 12, "TILT %.1F/OCT" % slope, AMBER)
    # level over time (400 ms RMS)
    ly0, ly1 = 334, H - 12
    cv.text(4, ly0 - 12, "RMS 400MS", GREY)
    if curve:
        hi = 3 * math.ceil(max(curve) / 3)
        lo = hi - 24
        cv.line(x0, ly0, x1, ly0, GRID)
        cv.line(x0, ly1, x1, ly1, GRID)
        cv.text(4, ly0 - 3 + 8, "%d" % hi, GREY)
        cv.text(4, ly1 - 3, "%d" % lo, GREY)
        n = len(curve)
        p = [(int(x0 + (x1 - x0) * i / max(1, n - 1)), int(ly0 + (hi - max(lo, min(hi, v))) / (hi - lo) * (ly1 - ly0)))
             for i, v in enumerate(curve)]
        for (ax, ay), (bx, by) in zip(p, p[1:]):
            cv.line(ax, ay, bx, by, INK, 2)
    cv.png(path)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="hibiki ears", description="measure a short WAV excerpt for the agent")
    ap.add_argument("wav")
    ap.add_argument("--label", default="")
    for k in ("lufs-i", "lufs-s-max", "lufs-m-max", "tp", "peak"):
        ap.add_argument("--" + k, type=float, default=None)
    ap.add_argument("--json", action="store_true", help="print the full result as JSON (default: the summary)")
    a = ap.parse_args(argv)
    reaper = {k: getattr(a, k.replace("-", "_")) for k in ("lufs-i", "lufs-s-max", "lufs-m-max", "tp", "peak")}
    reaper = {k.replace("-", "_"): v for k, v in reaper.items() if v is not None}
    try:
        res, plot = analyse(a.wav, a.label, reaper)
    except (OSError, ValueError) as e:
        print(json.dumps({"ok": False, "error": str(e)}) if a.json else "ears: %s" % e)
        return 1
    stem = Path(a.wav).with_suffix("")
    png, md = str(stem) + ".png", str(stem) + ".md"
    picture(res, plot, png)
    Path(md).write_text(summary_md(res), encoding="utf-8")
    res.update(ok=True, png=png, md=md)
    print(json.dumps(res) if a.json else summary_md(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
