#!/usr/bin/env python3
"""Syntax gate for the parts no test imports: every .lua compiles under Lua 5.4 (REAPER's version) and calls no
top-level local function above its declaration (that call hits a nil global at runtime), every .sh parses.
Needs `pip install lupa`."""
import re
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


def use_before_declare(src):
    lines = src.splitlines()
    decl = {}
    for i, line in enumerate(lines):
        m = re.match(r"^local function (\w+)", line) or re.match(r"^local (\w+)\s*=\s*function", line)
        if m:
            decl.setdefault(m.group(1), i)
        m = re.match(r"^local ([\w, ]+)$", line)  # forward declaration
        if m:
            for n in m.group(1).split(","):
                decl.setdefault(n.strip(), i)
    out = []
    for i, line in enumerate(lines):
        code = line.split("--")[0]
        for name, d in decl.items():
            if i < d and re.search(r"(?<![\w.:])" + name + r"\s*\(", code):
                out.append("%s() called on line %d, declared on line %d" % (name, i + 1, d + 1))
    return "; ".join(out)


bad = 0
for p in files(".lua"):
    src = p.read_text()
    err = compile_lua(src, str(p.relative_to(ROOT))) or use_before_declare(src)
    print(("  ok   " if not err else "  FAIL ") + str(p.relative_to(ROOT)) + ("" if not err else "  " + err))
    bad += bool(err)
for p in files(".sh") + sorted((ROOT / "packaging/macos/scripts").glob("*")):
    r = subprocess.run(["bash", "-n", str(p)], capture_output=True, text=True)
    print(("  ok   " if r.returncode == 0 else "  FAIL ") + str(p.relative_to(ROOT)) + ("" if r.returncode == 0 else "  " + r.stderr.strip()))
    bad += r.returncode != 0
print("all passed" if not bad else "%d failed" % bad)
sys.exit(1 if bad else 0)
