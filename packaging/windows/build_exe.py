#!/usr/bin/env python3
"""Build Hibiki-<version>-setup.exe: the suite + a bundled Python under %LOCALAPPDATA%\\Programs\\Hibiki, an Inno Setup
installer that checks for REAPER first and runs `hibiki setup` for the user.

    python packaging/windows/build_exe.py [out_dir]          (default dist/)

Staging works anywhere (handy to inspect the payload); compiling bytecode and the installer needs Windows with
Inno Setup 6 (ISCC.exe on PATH, or ISCC=<path>).
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
SUITE = ["reaper_mcp", "gateway", "panel", "reaper", "LICENSE", "README.md"]
# nothing in the suite uses these; ~30 MB less
PRUNE = ["tcl", "include", "libs", "Scripts", "Lib/test", "Lib/idlelib", "Lib/turtledemo", "Lib/tkinter",
         "Lib/ensurepip", "Lib/lib2to3", "Lib/site-packages/pip"]


def lock() -> dict:
    return dict(re.findall(r"^(\w+)=(\S+)", (HERE.parent / "python.lock").read_text(), re.M))


def version() -> str:
    return re.search(r'^__version__ = "([^"]+)"', (REPO / "reaper_mcp/__init__.py").read_text(), re.M).group(1)


def numeric(v: str) -> str:
    """VersionInfoVersion wants a.b.c.d: 0.5.0-rc.1 -> 0.5.0.0."""
    parts = re.match(r"(\d+)\.(\d+)\.(\d+)", v).groups()
    return ".".join(parts) + ".0"


def fetch_python(stage: Path) -> None:
    L = lock()
    name = "cpython-%s+%s-x86_64-pc-windows-msvc-install_only_stripped.tar.gz" % (L["PY"], L["TAG"])
    cache = Path(os.environ.get("PY_CACHE") or Path.home() / ".cache/hibiki")
    cache.mkdir(parents=True, exist_ok=True)
    tgz = cache / name
    if not tgz.exists():
        url = "https://github.com/astral-sh/python-build-standalone/releases/download/%s/%s" % (L["TAG"], name)
        with urllib.request.urlopen(url, timeout=300) as r:
            tgz.with_name(name + ".part").write_bytes(r.read())
        os.replace(tgz.with_name(name + ".part"), tgz)
    got = hashlib.sha256(tgz.read_bytes()).hexdigest()
    if got != L["SHA_win_x64"]:
        tgz.unlink()
        raise SystemExit("sha256 mismatch for %s: %s" % (name, got))
    with tempfile.TemporaryDirectory() as tmp:
        with tarfile.open(tgz) as t:
            if hasattr(tarfile, "data_filter"):
                t.extractall(tmp, filter="data")
            else:
                t.extractall(tmp)
        shutil.move(str(Path(tmp) / "python"), str(stage / "python"))
    for rel in PRUNE:
        shutil.rmtree(stage / "python" / rel, ignore_errors=True)
    for dll in (stage / "python/DLLs").glob("*"):
        if dll.name.lower().startswith(("_tkinter", "tcl", "tk")):
            dll.unlink()


def stage_suite(stage: Path) -> None:
    app = stage / "app"
    for rel in SUITE:
        src = REPO / rel
        if src.is_dir():
            shutil.copytree(src, app / rel, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "test_*.py", "fake_*"))
        else:
            app.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, app / rel)
    (stage / "bin").mkdir()
    # %~dp0 = this file's folder; -s -E: ignore the user's site-packages and PYTHON* variables
    (stage / "bin/hibiki.cmd").write_text(
        '@echo off\r\n"%~dp0..\\python\\python.exe" -s -E "%~dp0..\\app\\reaper_mcp\\cli.py" %*\r\n')
    (stage / "VERSION").write_text(version() + "\n")


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else REPO / "dist").resolve()
    out.mkdir(parents=True, exist_ok=True)
    stage = out / "stage-win"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir()
    v = version()
    print("hibiki %s, python %s (%s)" % (v, lock()["PY"], lock()["TAG"]))
    fetch_python(stage)
    stage_suite(stage)
    if os.name != "nt":
        print("staged in %s (bytecode and installer need Windows)" % stage)
        return 0
    # the install folder is per user but treat it as read-only: ship the bytecode
    subprocess.run([str(stage / "python/python.exe"), "-m", "compileall", "-q", str(stage / "app")], check=True)
    iscc = os.environ.get("ISCC") or shutil.which("ISCC") or r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    subprocess.run([iscc, "/Q", "/DAppVersion=" + v, "/DNumVersion=" + numeric(v), "/DStage=" + str(stage),
                    "/DOutDir=" + str(out), str(HERE / "hibiki.iss")], check=True)
    exe = out / ("Hibiki-%s-setup.exe" % v)
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    exe.with_name(exe.name + ".sha256").write_text("%s  %s\n" % (digest, exe.name))
    shutil.rmtree(stage, ignore_errors=True)
    print(exe)
    return 0


if __name__ == "__main__":
    sys.exit(main())
