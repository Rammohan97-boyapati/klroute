"""
Tests for klroute.router.  Run with:   python tests/test_router.py
(needs "pip install klayout"; pytest also works if installed)
"""

import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

import klayout.db as db  # noqa: E402

from klroute import RouteError, RouteSpec, route  # noqa: E402

WG = RouteSpec(kind="waveguide", width=0.5, radius=10.0)
EULER = RouteSpec(kind="waveguide", width=0.5, radius=10.0, bend="euler")
METAL = RouteSpec(kind="metal", width=2.0)


def _close(a, b, tol=1e-6):
    return abs(a - b) <= tol


def _endpoints_ok(rt, p0, p1):
    c = rt.centerline
    assert _close(c[0].x, p0[0]) and _close(c[0].y, p0[1]), c[0]
    assert _close(c[-1].x, p1[0]) and _close(c[-1].y, p1[1]), c[-1]


def _min_radius_ok(rt, r):
    """Every vertex of the centerline must have a local radius >= r (approx.)."""
    c = [(p.x, p.y) for p in rt.centerline]
    for i in range(1, len(c) - 1):
        a, b, d = c[i - 1], c[i], c[i + 1]
        ab, bd, ad = math.dist(a, b), math.dist(b, d), math.dist(a, d)
        cross = abs((b[0] - a[0]) * (d[1] - a[1]) - (b[1] - a[1]) * (d[0] - a[0]))
        if cross < 1e-9:
            continue
        circumradius = ab * bd * ad / (2.0 * cross)
        assert circumradius >= r * 0.98, (i, circumradius)


def _tangent_continuous(rt):
    c = [(p.x, p.y) for p in rt.centerline]
    for i in range(1, len(c) - 1):
        u = (c[i][0] - c[i - 1][0], c[i][1] - c[i - 1][1])
        v = (c[i + 1][0] - c[i][0], c[i + 1][1] - c[i][1])
        ang = abs(math.atan2(u[0] * v[1] - u[1] * v[0], u[0] * v[0] + u[1] * v[1]))
        assert ang < 0.1, (i, ang)


def test_straight():
    rt = route((0, 0), (100, 0), WG)
    assert rt.bends == 0 and _close(rt.length, 100.0)
    _endpoints_ok(rt, (0, 0), (100, 0))


def test_auto_z_route():
    rt = route((0, 0), (100, 50), WG)
    assert (rt.start_dir, rt.end_dir) == ("E", "W") and rt.bends == 2
    _endpoints_ok(rt, (0, 0), (100, 50))
    _min_radius_ok(rt, 10.0)
    _tangent_continuous(rt)
    expected = 100 + 50 - 4 * 10 + 2 * (math.pi / 2 * 10)
    assert _close(rt.length, expected, 1e-6), rt.length


def test_l_route():
    rt = route((0, 0), (100, 80), WG, "E", "S")
    assert rt.bends == 1
    _endpoints_ok(rt, (0, 0), (100, 80))
    assert _close(rt.length, 180 - 20 + math.pi / 2 * 10)


def test_small_offset_s_bend():
    rt = route((0, 0), (100, 5), WG, "E", "W")
    assert rt.bends == 2
    _endpoints_ok(rt, (0, 0), (100, 5))
    _min_radius_ok(rt, 10.0)
    _tangent_continuous(rt)


def test_u_turn():
    rt = route((0, 0), (0, 40), WG, "E", "E")
    assert rt.bends == 2
    _endpoints_ok(rt, (0, 0), (0, 40))
    assert max(p.x for p in rt.waypoints) >= 10.0


def test_backwards_loop():
    rt = route((0, 0), (-50, 30), WG, "E", "W")
    assert rt.bends == 4
    _endpoints_ok(rt, (0, 0), (-50, 30))
    _min_radius_ok(rt, 10.0)
    _tangent_continuous(rt)


def test_perpendicular_around():
    # end port faces north but end point lies south-west: needs 3 bends
    rt = route((0, 0), (-40, -60), WG, "E", "N")
    assert rt.bends == 3
    _endpoints_ok(rt, (0, 0), (-40, -60))
    _min_radius_ok(rt, 10.0)


def test_euler():
    rt = route((0, 0), (100, 60), EULER)
    _endpoints_ok(rt, (0, 0), (100, 60))
    _min_radius_ok(rt, 10.0)
    _tangent_continuous(rt)
    # Euler bends start earlier (longer tangent) than circular ones of the same min. radius
    circ = route((0, 0), (100, 60), WG)
    assert rt.centerline[1].x < circ.centerline[1].x


def test_metal_manhattan():
    rt = route((0, 0), (30, 7), METAL)
    assert len(rt.centerline) == 4  # sharp corners
    for a, b in zip(rt.centerline, rt.centerline[1:]):
        assert _close(a.x, b.x) or _close(a.y, b.y)
    assert _close(rt.length, 37.0)


def test_metal_vertical_auto():
    rt = route((0, 0), (5, -60), METAL)
    assert (rt.start_dir, rt.end_dir) == ("S", "N")


def test_impossible_raises():
    try:
        route((0, 0), (0, 5), WG, "E", "E")  # U-turn narrower than 2r
    except RouteError:
        return
    raise AssertionError("expected RouteError")


def test_polygon_and_insert():
    from klroute import insert_route

    ly = db.Layout()
    ly.dbu = 0.001
    top = ly.create_cell("TOP")
    core, clad = ly.layer(1, 0), ly.layer(111, 0)
    spec = RouteSpec(width=0.5, radius=10.0, cladding=2.0)
    rt = route((0, 0), (100, 50), spec)
    insert_route(top, core, rt, clad)
    r_core = db.Region(top.begin_shapes_rec(core))
    r_clad = db.Region(top.begin_shapes_rec(clad))
    assert r_core.count() == 1 and r_clad.count() == 1
    assert (r_core - r_clad).is_empty()
    area = r_core.area() * ly.dbu ** 2
    assert _close(area, rt.length * 0.5, 0.05 * rt.length), (area, rt.length * 0.5)


def test_route_through_points():
    from klroute import route_through

    pts = [(0, 0), (50, 0), (50, 40), (120, 40)]
    rt = route_through(pts, WG)
    assert rt.bends == 2
    _endpoints_ok(rt, (0, 0), (120, 40))
    _min_radius_ok(rt, 10.0)
    _tangent_continuous(rt)
    assert _close(rt.length, 50 + 40 + 70 - 4 * 10 + 2 * (math.pi / 2 * 10))


def test_route_through_any_angle_and_metal():
    from klroute import route_through

    rt = route_through([(0, 0), (60, 0), (100, 40), (100, 100)], WG)  # 45 deg corners
    _min_radius_ok(rt, 10.0)
    _tangent_continuous(rt)
    m = route_through([(0, 0), (30, 0), (30, 7)], METAL)
    assert len(m.centerline) == 3 and _close(m.length, 37.0)


def test_route_through_too_short():
    from klroute import route_through

    try:
        route_through([(0, 0), (50, 0), (50, 15), (100, 15)], WG)  # 15 um < 2 r
    except RouteError as e:
        assert "Segment 2 -> 3" in str(e)
        return
    raise AssertionError("expected RouteError")


def test_insert_as_path():
    from klroute import insert_route

    ly = db.Layout()
    top = ly.create_cell("TOP")
    core, clad = ly.layer(1, 0), ly.layer(111, 0)
    rt = route((0, 0), (100, 50), RouteSpec(width=0.5, radius=10.0, cladding=2.0))
    insert_route(top, core, rt, clad, as_path=True)
    s = next(iter(top.shapes(core).each()))
    assert s.is_path() and _close(s.dpath.width, 0.5)
    assert next(iter(top.shapes(clad).each())).is_path()


def _crossings(conns):
    segs = [(c.port.name, a, b) for c in conns for a, b in zip(c.points, c.points[1:])]

    def o(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    def cross(s, t):
        p1, p2, p3, p4 = s[1], s[2], t[1], t[2]
        return o(p3, p4, p1) * o(p3, p4, p2) < 0 and o(p1, p2, p3) * o(p1, p2, p4) < 0

    return [(s[0], t[0]) for i, s in enumerate(segs) for t in segs[i + 1:] if s[0] != t[0] and cross(s, t)]


def test_gc_array_plans():
    from klroute.autoconnect import Port, plan
    from klroute import route_through

    ring = [Port("in", -40, -20, -1, 0), Port("thru", 40, -20, 1, 0),
            Port("add", 40, 20, 1, 0), Port("drop", -40, 20, -1, 0)]
    mixed = [Port("w", -50, 0, -1, 0), Port("n1", -20, 40, 0, 1), Port("n2", 20, 40, 0, 1),
             Port("s", 0, -40, 0, -1), Port("e", 50, 10, 1, 0)]
    cases = [((-71, -60, 71, 60), [Port("opt1", -71, 0, -1, 0), Port("opt2", 71, 0, 1, 0)]),
             ((-40, -30, 40, 30), ring), ((-50, -40, 50, 40), mixed)]
    for box, ports in cases:
        for side in ("left", "right", "top", "bottom"):
            gcs, conns = plan(box, ports, WG, side=side, pitch=127.0, spacing=10.5)
            assert len(gcs) == len(ports) == len(conns)
            for c in conns:
                route_through(c.points, WG)  # every bend fits the radius
                assert _close(c.points[-1][0], c.port.x) and _close(c.points[-1][1], c.port.y)
            assert not _crossings(conns), (side, _crossings(conns))
            # GC pins on one line, at the requested pitch
            coord = [g[1] if side in ("left", "right") else g[0] for g in gcs]
            gaps = sorted(abs(a - b) for a, b in zip(coord, coord[1:]))
            assert all(_close(g, 127.0, 1e-6) for g in gaps)


def test_test_coordinate_rows():
    from klroute import gc as gcmod

    ly = db.Layout()
    top = ly.create_cell("TOP")
    sub = ly.create_cell("ROUTE")
    tl = ly.layer(10, 0)
    lab = gcmod.opt_in_label("TE", "1550", "MZI_1")          # underscore is cleaned to '-'
    sub.shapes(tl).insert(db.DText(lab, db.DTrans(-76.4, 0.0)))
    sub.shapes(tl).insert(db.DText(gcmod.out_label(lab), db.DTrans(-76.4, 127.0)))
    sub.shapes(tl).insert(db.DText(gcmod.out_label(lab), db.DTrans(-76.4, 254.0)))
    top.insert(db.DCellInstArray(sub.cell_index(), db.DTrans(1000.0, 500.0)))
    top.shapes(tl).insert(db.DText("not a label", db.DTrans(0, 0)))
    rows = gcmod.test_coordinate_rows(top, tl)
    assert [(r["device"], r["role"]) for r in rows] == [("MZI-1", "in"), ("MZI-1", "out"), ("MZI-1", "out")]
    txt = gcmod.format_test_coordinates(rows).splitlines()
    assert txt[0] == "% device name,x,y,polarization,wavelength,in/out"
    assert txt[1] == "MZI-1,923.600,500.000,TE,1550,in"
    assert txt[2] == "MZI-1,923.600,754.000,TE,1550,out"
    assert txt[3] == "MZI-1,923.600,627.000,TE,1550,out"


def test_route_around_obstacles():
    from klroute.maze import route_around
    from klroute import route_through

    clear = 5.25
    cases = [
        ((0, 0), (1, 0), (400, 0), (-1, 0), [(150, -80, 200, 80)]),                       # wall in the way
        ((0, 0), (1, 0), (300, 200), (0, -1), [(250, -40, 350, 40)]),                     # block on the L corner
        ((0, 0), (1, 0), (700, 0), (-1, 0), [(150, -200, 180, 20), (350, -20, 380, 200),
                                             (520, -200, 550, 20)]),                       # slalom
        ((0, 0), (1, 0), (400, 0), (1, 0), [(340, -30, 400, 30)]),                        # port on the far side
    ]
    for p0, d0, p1, d1, boxes in cases:
        obst = [db.DPolygon(db.DBox(*b).enlarged(db.DVector(clear, clear))) for b in boxes]
        pts = route_around(p0, d0, p1, d1, WG, obst, clear)
        rt = route_through(pts, WG)                      # every bend fits the radius
        _endpoints_ok(rt, p0, p1)
        c = rt.centerline
        for a, b in zip(c, c[1:]):                       # the centerline never enters an obstacle
            for k in range(11):
                q = db.DPoint(a.x + (b.x - a.x) * k / 10, a.y + (b.y - a.y) * k / 10)
                # the lead into the last case's port starts at the device edge: skip the port itself
                if q.distance(db.DPoint(*p1)) < 10.5:
                    continue
                assert not any(o.inside(q) for o in obst), (boxes, q)


def test_via_block():
    import klroute.pcell as pc

    ly = db.Layout()
    c = ly.create_cell("V")
    m1, m2, cut = ly.layer(41, 0), ly.layer(45, 0), ly.layer(44, 0)
    # 20 um block with the default via geometry: 4 x 4 units of 3 x 3 cuts (0.8 um, pitch 1.4 um), 20 x 20 landings
    side = pc.draw_via(c, db.DPoint(7.5, 7.5), [m1, m2], cut, block=20)
    cuts = db.Region(c.shapes(cut)).merged()
    assert side == 20 and cuts.count() == 144
    assert all(_close(p.bbox().to_dtype(ly.dbu).width(), 0.8) for p in cuts.each())
    for l in (m1, m2):
        land = db.Region(c.shapes(l)).merged()
        assert land.count() == 1 and land.bbox().to_dtype(ly.dbu) == db.DBox(-2.5, -2.5, 17.5, 17.5)
        assert cuts.not_inside(land).is_empty()
    # auto size: a 10 um wide route gets a 10 x 10 block (2 x 2 units, 36 cuts)
    c2 = ly.create_cell("V2")
    assert pc.draw_via(c2, db.DPoint(0, 0), [m1, m2], cut, width=10) == 10
    assert db.Region(c2.shapes(cut)).merged().count() == 36


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except Exception as e:  # noqa: BLE001
                failed += 1
                print("FAIL", name, type(e).__name__, e)
    sys.exit(1 if failed else 0)
