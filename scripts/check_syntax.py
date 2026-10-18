#!/usr/bin/env python3
"""Syntax gate for the parts no test imports: every .lua compiles under Lua 5.4 (REAPER's version),
every .sh parses. Needs `pip install lupa`."""
import subprocess
import sys
from pathlib import Path

import lupa.lua54 as lua54

ROOT = Path(__file__).resolve().parents[1]
SKIP = {".git", ".venv", "node_modules"}


def files(ext):
    return sorted(p for p in ROOT.rglob("*" + ext) if not SKIP & set(p.relative_to(ROOT).parts))


rt = lua54.LuaRuntime()
compile_lua = rt.eval("function(src, name) local f, e = load(src, '=' .. name) return f and '' or e end")
bad = 0
for p in files(".lua"):
    err = compile_lua(p.read_text(), str(p.relative_to(ROOT)))
    print(("  ok   " if not err else "  FAIL ") + str(p.relative_to(ROOT)) + ("" if not err else "  " + err))
    bad += bool(err)
for p in files(".sh"):
    r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
    print(("  ok   " if r.returncode == 0 else "  FAIL ") + str(p.relative_to(ROOT)) + ("" if r.returncode == 0 else "  " + r.stderr.strip()))
    bad += r.returncode != 0
print("all passed" if not bad else "%d failed" % bad)
sys.exit(1 if bad else 0)
