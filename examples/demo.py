"""
Generate demo.gds showing every route topology.

    python examples/demo.py            (needs "pip install klayout")
or inside KLayout: File > Macro Development, open and run this file.
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "python"))

try:
    import pya as db
except ImportError:
    import klayout.db as db

from klroute import RouteSpec, draw_route  # noqa: E402

ly = db.Layout()
ly.dbu = 0.001
top = ly.create_cell("KLROUTE_DEMO")
WG, CLAD, M1, PIN, TXT = (ly.layer(1, 0), ly.layer(111, 0), ly.layer(41, 0), ly.layer(1, 10), ly.layer(10, 0))

wg = RouteSpec(kind="waveguide", width=0.5, radius=10.0, cladding=2.0)
euler = RouteSpec(kind="waveguide", width=0.5, radius=10.0, bend="euler", cladding=2.0)
metal = RouteSpec(kind="metal", width=3.0)

# (label, spec, A, B, leave A towards, leave B towards)   -- each case in its own 200 x 200 um tile
cases = [
    ("straight", wg, (20, 100), (180, 100), "auto", "auto"),
    ("Z auto", wg, (20, 40), (180, 160), "auto", "auto"),
    ("S small offset", wg, (20, 95), (180, 105), "E", "W"),
    ("L", wg, (20, 40), (160, 180), "E", "S"),
    ("U turn", wg, (60, 60), (60, 140), "E", "E"),
    ("loop back", wg, (140, 60), (60, 140), "E", "W"),
    ("around (3 bends)", wg, (100, 120), (60, 40), "E", "N"),
    ("Euler Z", euler, (20, 40), (180, 160), "E", "W"),
    ("metal Z", metal, (20, 40), (180, 160), "auto", "auto"),
    ("metal L", metal, (20, 40), (160, 180), "E", "S"),
    ("metal loop", metal, (140, 60), (60, 140), "E", "W"),
    ("metal U", metal, (60, 60), (60, 140), "W", "W"),
]

for i, (label, spec, a, b, sd, ed) in enumerate(cases):
    ox, oy = 220 * (i % 4), -220 * (i // 4)
    a = db.DPoint(a[0] + ox, a[1] + oy)
    b = db.DPoint(b[0] + ox, b[1] + oy)
    layer = WG if spec.is_waveguide else M1
    rt = draw_route(top, layer, a, b, spec, sd, ed, cladding_layer=CLAD)
    for p in (a, b):
        top.shapes(PIN).insert(db.DBox(p.x - 1.5, p.y - 1.5, p.x + 1.5, p.y + 1.5))
    top.shapes(TXT).insert(db.DText("%s  L=%.2f um" % (label, rt.length), db.DTrans(ox + 5, oy + 5)))
    top.shapes(TXT).insert(db.DText("A", db.DTrans(a.x + 3, a.y + 3)))
    top.shapes(TXT).insert(db.DText("B", db.DTrans(b.x + 3, b.y + 3)))
    print("%-18s %s->%s  bends=%d  length=%.3f um" % (label, rt.start_dir, rt.end_dir, rt.bends, rt.length))

out = os.path.join(HERE, "demo.gds")
ly.write(out)
print("wrote", out)
