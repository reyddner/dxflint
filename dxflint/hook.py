# -*- coding: utf-8 -*-
"""dxflint hook for Claude Code - checks every DXF a shell command just wrote and returns plain text.

Wired in ~/.claude/settings.json (PreToolUse and PostToolUse, matcher "Bash|PowerShell"):
    dxflint-hook pre     records when the command started
    dxflint-hook post    looks for DXF files written after that moment and checks them

Works with any command: it does not care which program wrote the DXF. It searches the session folder and the
folders quoted in the command, skipping .git, node_modules, virtualenvs and archive/backup/obsolete folders,
and checks up to MAX_DXF files. It never blocks or breaks the session: any failure becomes silence (exit 0).
State lives in ~/.claude/dxflint-hook-state (override with DXFLINT_HOOK_STATE).
"""
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

CHECKER = Path(__file__).resolve().with_name("check.py")
STATE = Path(os.environ.get("DXFLINT_HOOK_STATE", Path.home() / ".claude" / "dxflint-hook-state"))
TOOLS = {"Bash", "PowerShell"}
SKIP_DIRS = re.compile(r"^(\.git|\.hg|\.svn|node_modules|__pycache__|\.venv|venv|\.tox|\.mypy_cache|"
                       r"_?obsolete.*|_?archive.*|_?history.*|.*backup.*|_obsoleto.*|_historico.*|_arquivo.*)$", re.I)
MAX_DXF = 8
MAX_FILES = 40_000         # cap on files visited per command
MAX_SCAN_SECONDS = 2.0
WINDOW_WITHOUT_PRE = 600   # without the "pre" mark, look at the last 10 minutes
LINES_PER_DXF = 12
TIMEOUT = 40


def to_native(p: str) -> str:
    """Git Bash / MSYS path (/c/work/...) to Windows form (C:/work/...)."""
    m = re.match(r"^/([a-zA-Z])/(.*)$", p)
    return f"{m.group(1).upper()}:/{m.group(2)}" if m and os.name == "nt" else p


def roots(cwd: Path, cmd: str) -> list[Path]:
    """Session folder + folders (or parent folders of files) quoted in the command."""
    # double and single quotes searched separately (one can sit inside the other: python -c "...r'C:/x'...")
    cand = re.findall(r'"([^"\n]+)"', cmd) + re.findall(r"'([^'\n]+)'", cmd)
    cand += re.findall(r"(?<![\w:/])(?:[A-Za-z]:[\\/]|/)[^\s\"';|&<>()]*", cmd)
    out = [cwd]
    for c in cand:
        p = Path(to_native(c.strip()))
        try:
            if not p.is_absolute():
                continue
            if p.is_dir() and p != Path(p.anchor):
                out.append(p)
            elif p.parent.is_dir() and p.parent != Path(p.anchor):
                out.append(p.parent)
        except OSError:
            continue
    seen, final = set(), []
    for r in out:
        k = os.path.normcase(str(r.resolve()))
        if k not in seen:
            seen.add(k)
            final.append(r)
    return final


def new_dxfs(bases: list[Path], since: float) -> list[Path]:
    found, visited, t0 = {}, 0, time.time()
    for base in bases:
        for folder, dirs, files in os.walk(base):
            dirs[:] = [d for d in dirs if not SKIP_DIRS.match(d)]
            for f in files:
                visited += 1
                if not f.lower().endswith(".dxf"):
                    continue
                p = Path(folder, f)
                try:
                    mt = p.stat().st_mtime
                except OSError:
                    continue
                if mt >= since:
                    found[os.path.normcase(str(p.resolve()))] = (mt, p)
            if visited > MAX_FILES or time.time() - t0 > MAX_SCAN_SECONDS:
                break
    return [p for _, p in sorted(found.values(), key=lambda x: -x[0])]


def check(p: Path) -> str:
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run([sys.executable, str(CHECKER), str(p)], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return f"- {p.name}: check took over {TIMEOUT} s, run dxflint by hand"
    lines = r.stdout.splitlines()
    total = next((ln for ln in reversed(lines) if ln.startswith("TOTAL:")), None)
    if total is None:
        err = (r.stderr.strip().splitlines() or ["no output"])[-1]
        return f"- {p.name}: could not check ({err[:160]})"
    groups, sheet, sheets = {}, None, 0          # the same finding on several sheets becomes one line
    for ln in lines:
        m = re.match(r"^(\S.*): (OK|\d+ error)", ln)
        if m and not ln.startswith("TOTAL:"):
            sheet, sheets = m.group(1), sheets + 1
        elif ln.strip().startswith(("ERROR", "WARNING")):
            groups.setdefault(ln.strip(), []).append(sheet if ln.startswith(" ") else None)
    findings = []
    for msg, ss in sorted(groups.items(), key=lambda kv: not kv[0].startswith("ERROR")):
        ss = list(dict.fromkeys(s for s in ss if s))
        where = "" if not ss else f"  - sheet {ss[0]}" if len(ss) == 1 else \
            f"  - {len(ss)} sheets: {', '.join(ss[:6])}{', ...' if len(ss) > 6 else ''}"
        findings.append(msg + where)
    if not findings:
        return f"- {p.name}: OK ({sheets} sheet(s))"
    head = f"- {p.name} ({p.parent}): {total[7:].strip()}"
    more = [f"    ... {len(findings) - LINES_PER_DXF} more"] if len(findings) > LINES_PER_DXF else []
    return "\n".join([head] + ["    " + f for f in findings[:LINES_PER_DXF]] + more)


def mark(data: dict) -> Path:
    tid = re.sub(r"[^\w-]", "_", str(data.get("tool_use_id") or data.get("session_id") or "x"))
    return STATE / f"{tid}.t"


def command(data: dict) -> str | None:
    if data.get("tool_name") not in TOOLS:
        return None
    return (data.get("tool_input") or {}).get("command") or None


def cmd_pre(data: dict) -> None:
    if command(data) is None:
        return
    STATE.mkdir(parents=True, exist_ok=True)
    mark(data).write_text(repr(time.time()), encoding="utf-8")


def cmd_post(data: dict) -> None:
    cmd = command(data)
    if cmd is None:
        return
    m = mark(data)
    try:
        since = float(m.read_text(encoding="utf-8")) - 1
        m.unlink()
    except (OSError, ValueError):
        since = time.time() - WINDOW_WITHOUT_PRE
    clean_old_marks()
    found = new_dxfs(roots(Path(data.get("cwd") or "."), cmd), since)
    if not found:
        return
    with ThreadPoolExecutor(max_workers=4) as ex:
        lines = list(ex.map(check, found[:MAX_DXF]))
    if len(found) > MAX_DXF:
        lines.append(f"- ... {len(found) - MAX_DXF} more new DXF file(s) not checked")
    text = f"[dxflint] {len(found)} DXF file(s) written by this command:\n" + "\n".join(lines)
    if any("    ERROR" in ln for ln in lines):
        text += "\nFix the ERROR lines before rendering or reviewing images. Text-only check: not a full drawing review."
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": text}},
                     ensure_ascii=False))


def clean_old_marks() -> None:
    limit = time.time() - 86400
    for p in STATE.glob("*.t"):
        try:
            if p.stat().st_mtime < limit:
                p.unlink()
        except OSError:
            pass


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        data = json.loads(sys.stdin.buffer.read().decode("utf-8", errors="replace"))
        {"pre": cmd_pre, "post": cmd_post}[argv[0]](data)
    except Exception:
        pass   # a hook never takes the session down
    return 0


if __name__ == "__main__":
    sys.exit(main())
