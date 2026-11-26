# -*- coding: utf-8 -*-
"""Tests for the Claude Code hook that checks every new DXF after a shell command (dxflint/hook.py)."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import ezdxf
import pytest

from dxflint import hook

HOOK = Path(hook.__file__).resolve()


def dxf(path: Path, text: str = "FLOOR PLAN") -> Path:
    """A1 sheet with one viewport filling the sheet (clean); text 'nan' becomes an ERROR."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = ezdxf.new()
    doc.modelspace().add_lwpolyline([(0, 0), (60, 0), (60, 40), (0, 40)], close=True)
    psp = doc.layout("Layout1")
    psp.page_setup(size=(841, 594), margins=(0, 0, 0, 0), units="mm")
    psp.add_viewport(center=(350, 300), size=(600, 500), view_center_point=(30, 20), view_height=50)
    psp.add_text(text, dxfattribs={"height": 5}).set_placement((100, 30))
    doc.saveas(path)
    return path


def run(event: str, data: dict, state: Path) -> str:
    env = dict(os.environ, DXFLINT_HOOK_STATE=str(state))
    r = subprocess.run([sys.executable, str(HOOK), event], input=json.dumps(data),
                       capture_output=True, text=True, encoding="utf-8", env=env, timeout=90)
    assert r.returncode == 0, r.stderr
    return r.stdout


def context(out: str) -> str:
    if not out.strip():
        return ""
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def event(cwd: Path, cmd: str, tid: str = "t1", tool: str = "Bash") -> dict:
    return {"session_id": "s", "cwd": str(cwd), "tool_name": tool, "tool_use_id": tid,
            "tool_input": {"command": cmd}}


# ---------- finding new DXF files ----------

def test_only_dxf_written_after_start_and_outside_archives(tmp_path):
    old = dxf(tmp_path / "old.dxf")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    start = time.time() - 1
    new = dxf(tmp_path / "OUT" / "new.dxf")
    dxf(tmp_path / "_archive" / "copy.dxf")
    dxf(tmp_path / "obsolete" / "R01" / "old.dxf")
    dxf(tmp_path / "node_modules" / "x.dxf")
    assert hook.new_dxfs([tmp_path], start) == [new]


def test_roots_include_folders_quoted_in_command(tmp_path):
    other = tmp_path / "Project X"
    other.mkdir()
    cmd = f'cd "{other}" && python gen.py'
    assert other.resolve() in [r.resolve() for r in hook.roots(tmp_path / "session", cmd)]


def test_single_quoted_path_inside_python_c(tmp_path):
    folder = tmp_path / "sheets out"
    folder.mkdir()
    target = (folder / "X.dxf").as_posix()
    cmd = f"python -c \"\nimport ezdxf\ndoc.saveas(r'{target}')\nprint('ok')\""
    assert folder.resolve() in [r.resolve() for r in hook.roots(tmp_path / "session", cmd)]


def test_unquoted_path(tmp_path):
    (tmp_path / "job").mkdir()
    cmd = f"python gen.py --out {(tmp_path / 'job').as_posix()} --json r.json"
    assert (tmp_path / "job").resolve() in [r.resolve() for r in hook.roots(tmp_path / "s", cmd)]


def test_urls_are_not_paths(tmp_path):
    cmd = "curl -o x.dxf https://example.com/a/b.dxf"
    assert hook.roots(tmp_path, cmd) == [tmp_path]


@pytest.mark.skipif(os.name != "nt", reason="Git Bash paths only exist on Windows")
def test_git_bash_path_becomes_windows():
    assert hook.to_native("/d/projects/job") == "D:/projects/job"


# ---------- hook end to end ----------

def test_pre_and_post_report_errors_in_the_new_dxf(tmp_path):
    st, job = tmp_path / "_state", tmp_path / "job"
    job.mkdir()
    ev = event(job, "python sheet.py")
    assert run("pre", ev, st) == ""
    dxf(job / "SHEET_R01.dxf", text="nan")
    ctx = context(run("post", ev, st))
    assert "SHEET_R01.dxf" in ctx
    assert "ERROR" in ctx and "nan" in ctx


@pytest.mark.parametrize("cmd", ["node export.js", "make sheets", "./gen.sh", "blender -b -P scene.py"])
def test_any_command_that_writes_a_dxf_is_checked(tmp_path, cmd):
    st = tmp_path / "_state"
    ev = event(tmp_path, cmd, tid="any")
    run("pre", ev, st)
    dxf(tmp_path / "ANY.dxf", text="nan")
    assert "ANY.dxf" in context(run("post", ev, st))


def test_clean_dxf_is_one_ok_line(tmp_path):
    st, job = tmp_path / "_state", tmp_path / "job"
    job.mkdir()
    ev = event(job, "python sheet.py", tid="t2")
    run("pre", ev, st)
    dxf(job / "CLEAN.dxf")
    ctx = context(run("post", ev, st))
    assert "CLEAN.dxf: OK (1 sheet(s))" in ctx and "ERROR" not in ctx


def test_same_error_on_several_sheets_is_one_line(tmp_path):
    p = dxf(tmp_path / "TWO.dxf", text="nan")
    doc = ezdxf.readfile(p)
    doc.layouts.rename("Layout1", "A-01")
    l2 = doc.layouts.new("A-02")
    l2.page_setup(size=(841, 594), margins=(0, 0, 0, 0), units="mm")
    l2.add_viewport(center=(350, 300), size=(600, 500), view_center_point=(30, 20), view_height=50)
    l2.add_text("nan", dxfattribs={"height": 5}).set_placement((100, 30))
    doc.saveas(p)
    out = hook.check(p)
    nan_lines = [ln for ln in out.splitlines() if "nan" in ln]
    assert len(nan_lines) == 1, out
    assert "A-01" in nan_lines[0] and "A-02" in nan_lines[0]


def test_no_new_dxf_stays_silent(tmp_path):
    st = tmp_path / "_state"
    ev = event(tmp_path, "python calc.py", tid="t3")
    run("pre", ev, st)
    assert run("post", ev, st) == ""


def test_dxf_older_than_the_command_stays_silent(tmp_path):
    st = tmp_path / "_state"
    old = dxf(tmp_path / "old.dxf", text="nan")
    os.utime(old, (time.time() - 60, time.time() - 60))
    ev = event(tmp_path, "git status", tid="t4")
    run("pre", ev, st)
    assert run("post", ev, st) == ""


def test_other_tools_are_ignored(tmp_path):
    st = tmp_path / "_state"
    ev = event(tmp_path, "python gen.py", tid="t6", tool="Read")
    run("pre", ev, st)
    dxf(tmp_path / "R.dxf", text="nan")
    assert run("post", ev, st) == ""


def test_powershell_works_too(tmp_path):
    st = tmp_path / "_state"
    ev = event(tmp_path, "python gen.py", tid="t5", tool="PowerShell")
    run("pre", ev, st)
    dxf(tmp_path / "PS.dxf", text="nan")
    assert "PS.dxf" in context(run("post", ev, st))


def test_broken_input_does_not_crash(tmp_path):
    env = dict(os.environ, DXFLINT_HOOK_STATE=str(tmp_path / "_state"))
    for ev in ("pre", "post"):
        r = subprocess.run([sys.executable, str(HOOK), ev], input="this is not json",
                           capture_output=True, text=True, encoding="utf-8", env=env, timeout=30)
        assert r.returncode == 0 and r.stdout == ""
