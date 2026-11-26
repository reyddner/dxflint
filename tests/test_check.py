# -*- coding: utf-8 -*-
"""Tests for the sheet checker on the synthetic example sheets (examples/make_example.py)."""
import importlib.util
import json
from pathlib import Path

import pytest

from dxflint.check import check, main

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("make_example", ROOT / "examples" / "make_example.py")
make_example = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(make_example)


@pytest.fixture(scope="module")
def sheets(tmp_path_factory):
    return make_example.main(tmp_path_factory.mktemp("ex"))


def errors(path, **kw):
    report, _ = check(path, **kw)
    return [msg for r in report for level, msg, _ in r["findings"] if level == "ERROR"]


def test_clean_example_has_no_findings(sheets):
    report, warnings = check(sheets[0])
    assert [r["sheet"] for r in report] == ["A-01"]
    assert report[0]["findings"] == [] and warnings == []


def test_committed_example_is_clean():
    assert errors(ROOT / "examples" / "example.dxf") == []


def test_broken_example_reports_each_mistake(sheets):
    errs = errors(sheets[1])
    assert any("overlaps the title block" in e for e in errs)
    assert any("smaller than 1.8 mm" in e for e in errs)
    assert any('suspicious text' in e and "nan" in e for e in errs)
    assert any("overlapping texts" in e and "KITCHEN" in e for e in errs)
    assert len(errs) == 4


def test_title_block_none_and_min_text_options(sheets):
    errs = errors(sheets[1], title_block="none", min_text=0.5)
    assert not any("title block" in e or "smaller than" in e for e in errs)


def test_explicit_title_block(sheets):
    assert any("overlaps the title block" in e for e in errors(sheets[0], title_block=(400, 7, 834, 87)))


def test_cli_exit_code_and_json(sheets, tmp_path, capsys):
    assert main([str(sheets[0])]) == 0
    out = tmp_path / "r.json"
    assert main([str(sheets[1]), "--json", str(out)]) == 1
    assert "TOTAL: 4 error(s)" in capsys.readouterr().out
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["sheets"][0]["sheet"] == "A-01"
