#!/usr/bin/env python3
"""reaper_mcp/ears.py on synthetic WAVs (stdlib only, like the analysis itself).

    python tests/test_ears.py
"""

from __future__ import annotations

import json
import math
import random
import struct
import subprocess
import sys
import tempfile
from array import array
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from reaper_mcp import ears  # noqa: E402

failures = []


def check(label, cond, extra=""):
    print(("  PASS " if cond else "  FAIL ") + label, "" if cond else extra)
    if not cond:
        failures.append(label)


def wav(path, left, right, rate=48000, kind="pcm16"):
    inter = [v for pair in zip(left, right) for v in pair]
    if kind == "pcm16":
        data, tag, bits = array("h", [int(max(-1, min(1, v)) * 32767) for v in inter]).tobytes(), 1, 16
    elif kind == "pcm24":
        data = b"".join(struct.pack("<i", int(max(-1, min(1, v)) * 8388607))[:3] for v in inter)
        tag, bits = 1, 24
    else:
        data, tag, bits = array("f", inter).tobytes(), 3, 32
    blk = 2 * bits // 8
    Path(path).write_bytes(b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
                           + struct.pack("<IHHIIHH", 16, tag, 2, rate, rate * blk, blk, bits)
                           + b"data" + struct.pack("<I", len(data)) + data)


def main():
    tmp = Path(tempfile.mkdtemp())
    rate, n = 48000, 48000 * 2
    rnd = random.Random(3)
    noise = [rnd.uniform(-0.05, 0.05) for _ in range(n)]
    sine = [0.5 * math.sin(2 * math.pi * 1000 * i / rate) for i in range(n)]
    mono = [a + b for a, b in zip(sine, noise)]
    for kind in ("pcm16", "pcm24", "float"):
        p = tmp / ("t_%s.wav" % kind)
        wav(p, mono, mono, rate, kind)
        r, left, right = ears.read_wav(p)
        check("read %s: rate, length, values" % kind, r == rate and len(left) == n
              and abs(left[100] - mono[100]) < 1e-3 and left == right)
    res, plot = ears.analyse(tmp / "t_pcm24.wav", "bar 1", {"lufs_i": -9.0})
    peaks = [p["band"] for p in res["peaks_over_tilt"]]
    check("a 1 kHz tone stands out of the tilt at 1k", "1k" in peaks, peaks)
    check("identical channels: correlation 1, side far below mid",
          res["stereo"]["correlation"] > 0.99 and res["stereo"]["side_minus_mid_db"] < -60, res["stereo"])
    lv = res["level"]
    check("peak of a 0.5 sine + noise near -6 dBFS", -6.5 < lv["peak_dbfs"] < -5, lv)
    anti = [-v for v in mono]
    wav(tmp / "wide.wav", mono, anti, rate)
    res2, _ = ears.analyse(tmp / "wide.wav")
    check("opposite channels: correlation -1, side over mid", res2["stereo"]["correlation"] < -0.99
          and res2["stereo"]["side_minus_mid_db"] > 60, res2["stereo"])
    md = ears.summary_md(res)
    check("summary carries REAPER loudness and the band table", "LUFS-I -9.0" in md and "| 1k |" in md)
    out = subprocess.run([sys.executable, "-m", "reaper_mcp.cli", "ears", str(tmp / "t_pcm16.wav"), "--json",
                          "--label", "bars 1-2", "--tp", "-1.2"], cwd=ROOT, capture_output=True, text=True)
    d = json.loads(out.stdout)
    png = Path(d["png"]).read_bytes()
    check("cli: ok JSON, png + md written", out.returncode == 0 and d["ok"] and png[:8] == b"\x89PNG\r\n\x1a\n"
          and Path(d["md"]).read_text().startswith("# Ears: bars 1-2"), out.stderr)
    (tmp / "bad.wav").write_bytes(b"nope")
    out = subprocess.run([sys.executable, "-m", "reaper_mcp.cli", "ears", str(tmp / "bad.wav"), "--json"], cwd=ROOT,
                         capture_output=True, text=True)
    check("cli: a bad file is a JSON error, exit 1", out.returncode == 1 and json.loads(out.stdout)["ok"] is False)
    if failures:
        print("FAILED", failures)
        sys.exit(1)
    print("all passed")


if __name__ == "__main__":
    main()
