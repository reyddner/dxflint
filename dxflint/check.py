# -*- coding: utf-8 -*-
"""dxflint - automatic review of DXF drawing sheets (paper-space layouts).

Reads every paper-space layout (sheet) of a DXF file and reports:
  ERROR    text overlapping other text (on paper, with each viewport's scale applied)
  ERROR    text clipped by a viewport edge
  ERROR    viewport, image or text outside the drawing frame, or over the title block
  ERROR    overlapping viewports
  ERROR    image covering text
  ERROR    text containing "nan", "None", "NaN", "Traceback", "{" or "}" (script leftovers)
  ERROR    text smaller than --min-text mm on paper (outside the title block)
  WARNING  nearly empty sheet (occupied area below --min-fill of the usable area)
  WARNING  hatch pattern that ezdxf's PDF backend does not draw (AR-CONC, AR-SAND)

    dxflint drawing.dxf [--frame x0,y0,x1,y1] [--title-block auto|none|x0,y0,x1,y1] [--json out.json]

Exit code: 0 without errors, 1 with errors. Coordinates are paper millimetres.
"""
import argparse
import json
import math
import re
import sys
from pathlib import Path

import ezdxf
from shapely.geometry import Polygon, box
from shapely.ops import unary_union
from shapely.strtree import STRtree

BAD_PATTERNS = {'AR-CONC', 'AR-SAND'}      # ezdxf's PDF backend leaves these blank
MIN_TEXT = 1.8                              # mm on paper
MIN_FILL = .45                              # share of the usable area (frame minus the title-block column)
FRAME_MARGINS = (25, 7, 7, 7)               # left, bottom, right, top (ISO 5457 / NBR 10068 filing margin)
TITLE_BLOCK_LAYERS = r'carimbo|title|legend|stamp|selo|cartouche|schriftfeld'
A1_TITLE_BLOCK = (696, 7, 831, 79)          # fallback on A1 (841 x 594) when no title block is drawn
FORBIDDEN = re.compile(r'\b(nan|NaN|None|Traceback)\b|[{}]')
WIDTH = {c: .32 for c in "il.,;:!|'I1()[] "}   # relative glyph widths, used only when the font cannot be loaded
WIDTH.update({c: .80 for c in 'MWmw@%'})

_FONTS = {}


def text_width(s, h, t=None):
    """Text width. With the entity, measure with the style's font (the same metrics ezdxf uses to plot);
    the WIDTH table is a fallback."""
    if t is not None:
        try:
            name = t.doc.styles.get(t.dxf.style).dxf.font
            if name not in _FONTS:
                from ezdxf.fonts import fonts
                _FONTS[name] = fonts.make_font(name, 1.0)
            return _FONTS[name].text_width(s) * h
        except Exception:
            pass
    return h * sum(WIDTH.get(c, .70 if c.isupper() else .58) for c in s)


def text_box(t):
    """Rectangle (polygon) of a TEXT entity in its own space."""
    h = t.dxf.height
    s = t.plain_text() if hasattr(t, 'plain_text') else t.dxf.text
    w = text_width(s, h, t)
    ha, va = t.dxf.get('halign', 0), t.dxf.get('valign', 0)
    p = t.dxf.insert if (ha == 0 and va == 0) or ha in (3, 5) else t.dxf.get('align_point', t.dxf.insert)
    dx = {0: 0, 1: -w / 2, 2: -w, 4: -w / 2}.get(ha, 0)
    dy = {0: -.2 * h, 1: -.2 * h, 2: -h / 2, 3: -h}.get(va, 0)
    r = math.radians(t.dxf.get('rotation', 0))
    c, sn = math.cos(r), math.sin(r)
    pts = [(dx, dy), (dx + w, dy), (dx + w, dy + h * 1.2), (dx, dy + h * 1.2)]
    return Polygon([(p.x + x * c - y * sn, p.y + x * sn + y * c) for x, y in pts]), s


def viewports(layout):
    out = []
    for vp in layout.query('VIEWPORT'):
        if vp.dxf.get('id', 2) == 1 or vp.dxf.status == 0:       # viewport 1 is the sheet itself
            continue
        cx, cy = vp.dxf.center.x, vp.dxf.center.y
        w, h = vp.dxf.width, vp.dxf.height
        vh = vp.dxf.view_height
        k = h / vh if vh else 1
        vc = vp.dxf.view_center_point
        out.append(dict(paper=box(cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2), k=k, vc=(vc.x, vc.y), c=(cx, cy),
                        model=box(vc.x - w / 2 / k, vc.y - h / 2 / k, vc.x + w / 2 / k, vc.y + h / 2 / k)))
    return out


def to_paper(poly, vp):
    (vx, vy), (cx, cy), k = vp['vc'], vp['c'], vp['k']
    return Polygon([(cx + (x - vx) * k, cy + (y - vy) * k) for x, y in poly.exterior.coords])


def tb_polylines(layout, layers):
    rx = re.compile(layers, re.I)
    for e in layout.query('LWPOLYLINE'):
        if rx.search(e.dxf.layer) and len(e) >= 4:
            yield Polygon(e.get_points('xy')).envelope


def find_title_block(layout, W, H, layers=TITLE_BLOCK_LAYERS):
    """Largest rectangle drawn on a title-block layer in the lower-right corner; on A1 without one,
    the A1_TITLE_BLOCK box (135 x 72 mm in the lower-right corner)."""
    best = None
    for b in tb_polylines(layout, layers):
        x0, y0, x1, y1 = b.bounds
        if x1 > W * .6 and y0 < H * .25 and (best is None or b.area > best.area):
            best = b
    if best is None and abs(W - 841) < 2 and abs(H - 594) < 2:
        best = box(*A1_TITLE_BLOCK)
    return best


def check(path, frame=None, title_block='auto', min_text=MIN_TEXT, min_fill=MIN_FILL,
          title_block_layers=TITLE_BLOCK_LAYERS):
    """Return (sheets, warnings): sheets = [{'sheet': name, 'findings': [(level, message, bbox)]}]."""
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()
    msp_texts = [text_box(t) + (t,) for t in msp.query('TEXT')]
    tree = STRtree([c for c, _, _ in msp_texts]) if msp_texts else None
    report = []
    for lay in doc.layouts:
        if lay.name.lower() == 'model':
            continue
        W, H = lay.dxf.paper_width, lay.dxf.paper_height
        if not W:
            continue
        ml, mb, mr, mt = FRAME_MARGINS
        frm = box(*frame) if frame else box(ml, mb, W - mr, H - mt)
        if title_block == 'none':
            tb = None
        elif title_block in (None, 'auto'):
            tb = find_title_block(lay, W, H, title_block_layers)
        else:
            tb = box(*title_block)
        found = []
        vps = viewports(lay)
        for i, v in enumerate(vps):
            if not frm.buffer(.5).contains(v['paper']):
                found.append(('ERROR', f'viewport {i + 1} goes outside the frame', v['paper'].bounds))
            if tb is not None and v['paper'].intersection(tb).area > 1:
                found.append(('ERROR', f'viewport {i + 1} overlaps the title block', v['paper'].bounds))
            for j in range(i):
                if v['paper'].intersection(vps[j]['paper']).area > 1:
                    found.append(('ERROR', f'viewports {j + 1} and {i + 1} overlap', v['paper'].bounds))
        # texts on the sheet: the layout's own + model-space texts seen through each viewport
        boxes, heights = [], []
        for t in lay.query('TEXT'):
            c, s = text_box(t)
            boxes.append((c, s, 'sheet')); heights.append(t.dxf.height)
        for i, v in enumerate(vps):
            if tree is None:
                continue
            for idx in tree.query(v['model']):
                c, s, t = msp_texts[idx]
                if not c.intersects(v['model']):
                    continue
                cp = to_paper(c, v)
                if not v['paper'].buffer(.2).contains(cp):
                    found.append(('ERROR', f'text clipped by viewport {i + 1}: "{s[:40]}"', cp.bounds))
                boxes.append((cp, s, f'vp{i + 1}')); heights.append(t.dxf.height * v['k'])
        # raster images are drawn over the vector plot: checked on their own
        images = []
        for im in lay.query('IMAGE'):
            p = im.dxf.insert; u = im.dxf.u_pixel; v = im.dxf.v_pixel
            sx, sy = im.dxf.image_size.x, im.dxf.image_size.y
            pol = Polygon([(p.x, p.y), (p.x + sx * u.x, p.y + sx * u.y),
                           (p.x + sx * u.x + sy * v.x, p.y + sx * u.y + sy * v.y), (p.x + sy * v.x, p.y + sy * v.y)])
            images.append(pol)
            if not frm.buffer(.5).contains(pol):
                found.append(('ERROR', 'image goes outside the frame', pol.bounds))
            if tb is not None and pol.intersection(tb).area > 1:
                found.append(('ERROR', 'image overlaps the title block', pol.bounds))
            for c, s, where in boxes:
                if pol.buffer(-.2).intersection(c).area > .3:
                    found.append(('ERROR', f'image covers text ({where}): "{s[:35]}"', c.bounds))
        # proportion: text too small on paper (outside the title block) and nearly empty sheet
        small = [(alt, s) for (c, s, where), alt in zip(boxes, heights)
                 if alt < min_text and not (tb is not None and tb.buffer(1).contains(c))]
        if small:
            found.append(('ERROR', f'{len(small)} text(s) smaller than {min_text} mm on paper (smallest '
                                   f'{min(small)[0]:.2f} mm: "{min(small)[1][:30]}")', (0, 0, W, H)))
        usable = frm.difference(box(tb.bounds[0], 0, W, H)) if tb is not None else frm
        occupied = images + [v['paper'] for v in vps] + [c for c, _, where in boxes if where == 'sheet'] + \
            [b for b in tb_polylines(lay, title_block_layers) if not (tb is not None and tb.buffer(1).contains(b))]
        if occupied and usable.area > 0:
            rate = unary_union(occupied).intersection(usable).area / usable.area
            if rate < min_fill:
                found.append(('WARNING', f'nearly empty sheet: {rate:.0%} occupied (minimum {min_fill:.0%}) - '
                                         f'enlarge the scale, merge views or use a smaller sheet', usable.bounds))
        for c, s, where in boxes:
            if FORBIDDEN.search(s):
                found.append(('ERROR', f'suspicious text ({where}): "{s[:40]}"', c.bounds))
            if where == 'sheet' and not frm.buffer(.5).contains(c):
                found.append(('ERROR', f'text outside the frame: "{s[:40]}"', c.bounds))
        # text x text overlap (shrunk ~8 % to tolerate letter spacing)
        if boxes:
            shr = [c.buffer(-min(.15, .08 * math.sqrt(c.area))) if c.area > .5 else c for c, _, _ in boxes]
            tr = STRtree(shr)
            for i, ci in enumerate(shr):
                for j in tr.query(ci):
                    j = int(j)
                    if j <= i:
                        continue
                    a = ci.intersection(shr[j]).area
                    if a > .15 * min(ci.area, shr[j].area) and a > .3:
                        found.append(('ERROR', f'overlapping texts: "{boxes[i][1][:30]}" x "{boxes[j][1][:30]}"',
                                      boxes[i][0].bounds))
        report.append(dict(sheet=lay.name, findings=found))
    hatches = {}
    for h in doc.entitydb.values():
        if h.dxftype() == 'HATCH' and h.dxf.get('pattern_name', '').upper() in BAD_PATTERNS:
            hatches[h.dxf.pattern_name] = hatches.get(h.dxf.pattern_name, 0) + 1
    warnings = [f'{n} hatch(es) {p}: the ezdxf PDF backend does not draw this pattern' for p, n in hatches.items()]
    return report, warnings


def _numbers(s):
    return tuple(float(v) for v in s.split(',')) if s else None


def main(argv=None):
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')   # Windows console (cp1252)
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(prog='dxflint', description=__doc__.split('\n')[0])
    ap.add_argument('dxf')
    ap.add_argument('--frame', help='drawing frame x0,y0,x1,y1 in mm (default: 25 mm left margin, 7 mm elsewhere)')
    ap.add_argument('--title-block', '--carimbo', default='auto',
                    help='auto (default), none, or x0,y0,x1,y1 in mm')
    ap.add_argument('--title-block-layers', default=TITLE_BLOCK_LAYERS,
                    help=f'regex of layer names searched by --title-block auto (default: {TITLE_BLOCK_LAYERS})')
    ap.add_argument('--min-text', type=float, default=MIN_TEXT, help=f'minimum text height on paper, mm ({MIN_TEXT})')
    ap.add_argument('--min-fill', type=float, default=MIN_FILL, help=f'minimum occupied share of the sheet ({MIN_FILL})')
    ap.add_argument('--json', help='also write the findings to this JSON file')
    a = ap.parse_args(argv)
    tb = a.title_block if a.title_block in ('auto', 'none') else _numbers(a.title_block)
    report, warnings = check(a.dxf, _numbers(a.frame), tb, a.min_text, a.min_fill, a.title_block_layers)
    n_err = 0
    for r in report:
        errors = [x for x in r['findings'] if x[0] == 'ERROR']
        n_err += len(errors)
        print(f"{r['sheet']}: {'OK' if not errors else f'{len(errors)} error(s)'}")
        for level, msg, bb in r['findings'][:40]:
            print(f'   {level}  {msg}  [x {bb[0]:.0f}-{bb[2]:.0f} mm, y {bb[1]:.0f}-{bb[3]:.0f} mm]')
        if len(r['findings']) > 40:
            print(f"   ... {len(r['findings']) - 40} more")
    for w in warnings:
        print('WARNING', w)
    if a.json:
        Path(a.json).write_text(json.dumps(dict(sheets=report, warnings=warnings), ensure_ascii=False, indent=1),
                                encoding='utf-8')
    n_warn = len(warnings) + sum(1 for r in report for level, _, _ in r['findings'] if level == 'WARNING')
    print(f'TOTAL: {n_err} error(s), {n_warn} warning(s)')
    return 1 if n_err else 0


if __name__ == '__main__':
    sys.exit(main())
