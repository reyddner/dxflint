# -*- coding: utf-8 -*-
"""Writes two synthetic A1 sheets next to this script:

    example.dxf         a clean sheet (dxflint reports no errors)
    example_broken.dxf  the same sheet with typical mistakes (overlapping and leftover texts, tiny text,
                        a viewport running over the title block)

    python examples/make_example.py [output folder]
"""
import sys
from pathlib import Path

import ezdxf
from ezdxf.enums import TextEntityAlignment


def make(path, broken=False):
    doc = ezdxf.new(setup=True)
    doc.layers.add("TITLE-BLOCK")
    msp = doc.modelspace()
    # a small floor plan in model space (metres): two rooms and their labels
    msp.add_lwpolyline([(0, 0), (12, 0), (12, 8), (0, 8)], close=True)
    msp.add_line((7, 0), (7, 8))
    msp.add_text("LIVING ROOM", dxfattribs={"height": .25}).set_placement((3.5, 4), align=TextEntityAlignment.MIDDLE_CENTER)
    msp.add_text("KITCHEN", dxfattribs={"height": .25}).set_placement((9.5, 4), align=TextEntityAlignment.MIDDLE_CENTER)
    if broken:
        msp.add_text("KITCHEN", dxfattribs={"height": .25}).set_placement((9.6, 4.05), align=TextEntityAlignment.MIDDLE_CENTER)
        msp.add_text("AREA nan m2", dxfattribs={"height": .25}).set_placement((3.5, 3), align=TextEntityAlignment.MIDDLE_CENTER)

    psp = doc.layout("Layout1")
    doc.layouts.rename("Layout1", "A-01")
    psp.page_setup(size=(841, 594), margins=(0, 0, 0, 0), units="mm")
    psp.add_lwpolyline([(25, 7), (834, 7), (834, 587), (25, 587)], close=True)            # frame
    psp.add_lwpolyline([(684, 7), (834, 7), (834, 87), (684, 87)], close=True,            # title block
                       dxfattribs={"layer": "TITLE-BLOCK"})
    psp.add_text("SAMPLE PROJECT", dxfattribs={"height": 5}).set_placement((690, 70))
    psp.add_text("FLOOR PLAN - 1:20", dxfattribs={"height": 3.5}).set_placement((690, 55))
    psp.add_text("SHEET A-01", dxfattribs={"height": 3.5}).set_placement((690, 40))
    # one viewport at 1:20 (1 m of model = 50 mm of paper); the broken one runs over the title block
    width = 640 if not broken else 760
    psp.add_viewport(center=(30 + width / 2, 300), size=(width, 460), view_center_point=(6, 4), view_height=9.2)
    psp.add_text("FLOOR PLAN", dxfattribs={"height": 6}).set_placement((40, 545))
    if broken:
        psp.add_text("note in 1 mm text", dxfattribs={"height": 1}).set_placement((40, 535))
    doc.saveas(path)
    return Path(path)


def main(folder=None):
    out = Path(folder) if folder else Path(__file__).resolve().parent
    out.mkdir(parents=True, exist_ok=True)
    return make(out / "example.dxf"), make(out / "example_broken.dxf", broken=True)


if __name__ == "__main__":
    for p in main(sys.argv[1] if len(sys.argv) > 1 else None):
        print(p)
