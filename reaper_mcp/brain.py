"""Rules for agents: the user's copy of Hibiki's conventions (defaults.md) plus their rules.md, composed into one text.

Used by the MCP server (the `instructions` of every `initialize`) and by the agent gateway (the marked block in the
ACP agents' AGENTS.md / CLAUDE.md / GEMINI.md), so every agent gets the same rules.

The package's brain/defaults.md is only the seed: `hibiki setup` (or the first read) copies it to the user's brain
folder, and from then on agents get the user's copy, which they edit freely. On every setup `upgrade` merges a new
package file in, per rule id, against .defaults-state.json (the text last shipped for each id):
  unmodified (text == last shipped)   -> the new text
  modified                            -> kept
  deleted (known id, gone from copy)  -> stays deleted
  new id                              -> appended
Lines without an id are the user's and never touched. `reset` puts the shipped text back.

defaults.md: one rule per bullet, `- [id] text`, indented lines continue it. rules.md (free text) also understands
  off: <id>       (or `- off: <id>`) leaves that default out
  [id] text       (or `- [id] text`) replaces that default's text
  anything else   is the user's own rules, passed on as written (HTML comments dropped)

Stdlib only, Python 3.9. Reading never raises on a missing or unreadable file: no file means no rules.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

BRAIN_MAX = 12000  # characters of composed text (~3k tokens): every session of every agent pays for it
RULE_MAX = 500
_ROOT = Path(__file__).resolve().parents[1]
# a source checkout or the installed app keeps brain/ next to reaper_mcp/; a wheel carries it inside the package
PACKAGE_DEFAULTS = (_ROOT / "brain" / "defaults.md", Path(__file__).resolve().parent / "brain_defaults.md")
STATE_FILE = ".defaults-state.json"
ID = r"[a-z0-9][a-z0-9._-]*"
_DEFAULT = re.compile(r"^[-*]\s+\[(" + ID + r")\]\s*(.*)$")
_OFF = re.compile(r"^\s*(?:[-*]\s+)?off:\s*(" + ID + r")\s*$", re.I)
_OVERRIDE = re.compile(r"^\s*(?:[-*]\s+)?\[(" + ID + r")\]\s*(\S.*)$")
_BULLET = re.compile(r"^\s*([-*]|\d+[.)])\s+\S")
_COMMENT = re.compile(r"<!--.*?-->", re.S)

TEMPLATE = """# Your rules for agents working in REAPER
<!--
Every Hibiki agent (Codex, Claude Code, Kimi, Gemini, Gamut agents through the MCP, ...) reads these rules after
the conventions in defaults.md next to this file (`hibiki rules` prints both). Edit defaults.md freely to change or
delete a convention. Here: one rule per line; `/remember <rule>` in the Hibiki panel adds a line.
Also understood here:  off: <id>  (leave a convention out)  and  [<id>] <text>  (replace its text).
Comments like this one are not sent.
-->

"""
# header of the template before 0.6 (no comment block): not a rule, so it isn't passed on
_OLD_HEADER = ("Every Hibiki agent on this Mac (Codex, Claude Code, Kimi, Gemini, ...) reads these rules at the start of "
               "a chat.", "Write one rule per line. `/remember <rule>` in the Hibiki panel adds a line here.")


def user_dir() -> Path:
    return Path(os.environ.get("HIBIKI_BRAIN") or "~/.config/hibiki/brain").expanduser()


def rules_path(d=None) -> Path:
    return Path(d if d is not None else user_dir()) / "rules.md"


def _read(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return ""


def package_defaults_path():
    return next((p for p in PACKAGE_DEFAULTS if p.is_file()), None)


def defaults_file(d=None) -> Path:
    return Path(d if d is not None else user_dir()) / "defaults.md"


def state_path(d=None) -> Path:
    return Path(d if d is not None else user_dir()) / STATE_FILE


def _norm(text: str) -> str:
    return " ".join(text.split())


def _sha(text: str) -> str:
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()[:16]


def blocks(text: str) -> list:
    """[{id, text, start, end}]: each `- [id]` bullet with its indented continuation lines (end is exclusive);
    bullets inside HTML comments don't count."""
    out, cur = [], None
    text = _COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), text)  # comments out, line numbers kept
    for n, line in enumerate(text.splitlines()):
        m = _DEFAULT.match(line)
        if m:
            cur = {"id": m.group(1), "text": m.group(2).strip(), "start": n, "end": n + 1}
            out.append(cur)
        elif cur and line[:1] in (" ", "\t") and line.strip():
            cur["text"] += " " + line.strip()
            cur["end"] = n + 1
        elif line.strip():
            cur = None  # a heading or free text ends the bullet above
    for b in out:
        b["text"] = _norm(b["text"])
    return out


def parse_defaults(text: str) -> list:
    """[(id, text)] in file order; continuation lines are joined with single spaces."""
    return [(b["id"], b["text"]) for b in blocks(text)]


def _load_state(d) -> dict:
    try:
        st = json.loads(state_path(d).read_text(encoding="utf-8"))
        return dict(st.get("shipped") or {}) if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(path))


def _save_state(d, shipped: dict) -> None:
    _write(state_path(d), json.dumps({"shipped": shipped}, indent=1, sort_keys=True) + "\n")


def _package(package=None) -> str:
    p = package or package_defaults_path()
    return _read(p) if p else ""


def seed(d=None, package=None) -> bool:
    """Copy the package's defaults.md to the user's folder when there is no copy yet. True when it did."""
    path = defaults_file(d)
    text = _package(package)
    if path.exists() or not text:
        return False
    _write(path, text)
    _save_state(d, {b["id"]: _sha(b["text"]) for b in blocks(text)})
    return True


def _splice(user: str, pkg: str, ids_: list, state: dict) -> str:
    """Put the package's lines for ids_ into user: in place where the id exists, appended where it doesn't."""
    ulines, plines = user.splitlines(), pkg.splitlines()
    pblocks = {b["id"]: b for b in blocks(pkg)}
    have = {b["id"]: b for b in blocks(user)}
    tail = []
    for i in ids_:
        pb = pblocks[i]
        new = plines[pb["start"]:pb["end"]]
        state[i] = _sha(pb["text"])
        if i in have:
            ub = have[i]
            ulines[ub["start"]:ub["end"]] = [None] * (ub["end"] - ub["start"])
            ulines[ub["start"]] = new
        else:
            tail.extend(new)
    flat = []
    for x in ulines:
        if isinstance(x, list):
            flat.extend(x)
        elif x is not None:
            flat.append(x)
    while tail and flat and not flat[-1].strip():
        flat.pop()
    return "\n".join(flat + tail) + "\n"


def upgrade(d=None, package=None) -> dict:
    """Merge a (new) package defaults.md into the user's copy; see the module doc. Run by `hibiki setup`."""
    if seed(d, package):
        return {"seeded": True, "updated": [], "added": [], "kept": []}
    pkg, path = _package(package), defaults_file(d)
    if not pkg or not path.exists():
        return {"seeded": False, "updated": [], "added": [], "kept": []}
    user, state = _read(path), _load_state(d)
    have = {b["id"]: b["text"] for b in blocks(user)}
    updated, added, kept = [], [], []
    for pb in blocks(pkg):
        i, new = pb["id"], pb["text"]
        if i in have:
            if have[i] == new:
                pass
            elif state.get(i) == _sha(have[i]):
                updated.append(i)
            else:
                kept.append(i)
        elif i not in state:
            added.append(i)
        state[i] = _sha(new)  # the text last shipped, whatever the user did with it
    if updated or added:
        _write(path, _splice(user, pkg, updated + added, state))
    _save_state(d, state)
    return {"seeded": False, "updated": updated, "added": added, "kept": kept}


def reset(rule_ids=None, d=None, package=None) -> list:
    """Put the shipped text back for these ids (None: every shipped id, deleted ones included). Returns the ids."""
    pkg = _package(package)
    shipped = [b["id"] for b in blocks(pkg)]
    rule_ids = shipped if rule_ids is None else list(rule_ids)
    bad = [i for i in rule_ids if i not in shipped]
    if bad:
        raise ValueError("no shipped rule %s; ids: %s" % (", ".join(map(repr, bad)), ", ".join(shipped)))
    if seed(d, package):
        return rule_ids
    state = _load_state(d)
    _write(defaults_file(d), _splice(_read(defaults_file(d)), pkg, rule_ids, state))
    _save_state(d, state)
    return rule_ids


def defaults(d=None, path=None) -> list:
    """[(id, text)] from `path`, else the user's copy (seeded from the package if missing; the package file itself
    when the folder can't be written)."""
    if path is not None:
        return parse_defaults(_read(path))
    p = defaults_file(d)
    if not p.exists():
        try:
            seed(d)
        except OSError:
            pass
    return parse_defaults(_read(p) if p.exists() else _package())


def ids(d=None, path=None) -> list:
    return [i for i, _ in defaults(d, path)]


def parse_user(text: str, known=None) -> dict:
    """{'off': [ids], 'override': {id: text}, 'rules': text}. With `known`, only those ids count as off/override;
    other `[x]` lines stay rules, unknown `off:` lines are dropped (listed in 'unknown')."""
    off, override, unknown, keep = [], {}, [], []
    text = _COMMENT.sub("", text.replace("\r\n", "\n"))
    for line in text.splitlines():
        m = _OFF.match(line)
        if m:
            i = m.group(1).lower()
            if known is None or i in known:
                off.append(i) if i not in off else None
            else:
                unknown.append(i)
            continue
        m = _OVERRIDE.match(line)
        if m and (known is None or m.group(1) in known):
            override[m.group(1)] = " ".join(m.group(2).split())
            continue
        if line.strip() in _OLD_HEADER:
            continue
        keep.append(line.rstrip())
    while keep and (not keep[0].strip() or re.match(r"^#\s", keep[0])):  # the file's own title goes, our heading replaces it
        keep.pop(0)
    rules = re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()
    return {"off": off, "override": override, "rules": rules, "unknown": unknown}


def count(text: str) -> int:
    return sum(1 for line in text.splitlines() if _BULLET.match(line))


def effective(user_dir_=None, defaults_path=None) -> dict:
    """Everything `compose` and status readers need: the defaults after off/override, and the user's own rules."""
    ds = defaults(user_dir_, defaults_path)
    known = {i for i, _ in ds}
    path = rules_path(user_dir_)
    u = parse_user(_read(path), known)
    rows = [{"id": i, "text": u["override"].get(i, t), "source": "yours" if i in u["override"] else "default",
             "off": i in u["off"]} for i, t in ds]
    return {"defaults": rows, "off": [r["id"] for r in rows if r["off"]], "rules": u["rules"],
            "count": count(u["rules"]), "unknown": u["unknown"], "path": str(path), "exists": path.is_file(),
            "defaults_path": str(defaults_path or defaults_file(user_dir_))}


def compose(user_dir_=None, defaults_path=None, limit=BRAIN_MAX) -> str:
    """Markdown: '## Hibiki conventions' (the defaults in effect) + '## Your rules', at most `limit` characters."""
    e = effective(user_dir_, defaults_path)
    parts = []
    on = ["- [%s] %s" % (r["id"], r["text"]) for r in e["defaults"] if not r["off"]]
    if on:
        parts.append("## Hibiki conventions\n" + "\n".join(on))
    if e["rules"]:
        parts.append("## Your rules\n" + e["rules"])
    text = "\n\n".join(parts)
    if len(text) > limit:
        note = "\n(cut: the rules are over %d characters)" % limit
        text = text[:max(0, limit - len(note))]
        text = text[:text.rfind("\n")] if "\n" in text else text
        text += note
    return text


def ensure(d=None, package=None) -> Path:
    """Both files exist afterwards: defaults.md seeded from the package, rules.md from TEMPLATE. Returns rules.md."""
    seed(d, package)
    path = rules_path(d)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TEMPLATE, encoding="utf-8")
    return path


def add(rule, d=None, defaults_path=None) -> bool:
    """Append one rule line (False when it's already there). `off: <id>` must name a shipped default."""
    rule = " ".join(str(rule or "").split())
    if not rule or len(rule) > RULE_MAX:
        raise ValueError("a rule is one line of 1-%d characters" % RULE_MAX)
    m = _OFF.match(rule)
    if m:
        known = ids(d, defaults_path)
        if m.group(1).lower() not in known:
            raise ValueError("no default rule %r; ids: %s" % (m.group(1), ", ".join(known)))
        rule = "off: " + m.group(1).lower()
    path = rules_path(d)
    path.parent.mkdir(parents=True, exist_ok=True)
    old = _read(path) if path.exists() else TEMPLATE
    if any(line.strip().lstrip("-* ").strip() == rule for line in old.splitlines()):
        return False
    path.write_text(old.rstrip("\n") + "\n- " + rule + "\n", encoding="utf-8")
    return True


def set_off(rule_id: str, off: bool = True, d=None, defaults_path=None) -> bool:
    """Switch a default off (append `- off: <id>`) or back on (drop its off lines). True when the file changed."""
    known = ids(d, defaults_path)
    if rule_id not in known:
        raise ValueError("no default rule %r; ids: %s" % (rule_id, ", ".join(known)))
    if off:
        return add("off: " + rule_id, d, defaults_path)
    path = rules_path(d)
    old = _read(path)
    def is_off(line):
        m = _OFF.match(line.rstrip("\r\n"))
        return bool(m) and m.group(1).lower() == rule_id

    lines = [ln for ln in old.splitlines(True) if not is_off(ln)]
    if "".join(lines) == old:
        return False
    path.write_text("".join(lines), encoding="utf-8")
    return True
