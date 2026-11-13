"""
klroute.pins -- add SiEPIC-style pins to your own device cells.

* waveguide_ends(): open ends of the waveguide core (short straight end edges with
  nothing beyond them), with their width and outward direction.
* metal_contacts(): one electrical contact per separate metal island.
* propose_pins(): what a device needs: optical pins at the ends on its outer boundary,
  electrical pins on its contacts, numbered like SiEPIC (opt1, opt2, ... / elec1, ...),
  skipping ports that already have a pin.
* add_pin() / ensure_devrec(): write the marker shapes.

A pin is a 2-point path on PinRec (optical) or PinRecM (electrical) whose direction
points out of the device, centred on the port, plus a text with its name.
"""

import math

try:
    import pya as db
except ImportError:
    import klayout.db as db

PIN_HALF = 0.01            # um: SiEPIC pins are 20 nm long paths centred on the port
SIDES = {"E": (1.0, 0.0), "N": (0.0, 1.0), "W": (-1.0, 0.0), "S": (0.0, -1.0)}


def side_of(u):
    return max(SIDES, key=lambda k: SIDES[k][0] * u[0] + SIDES[k][1] * u[1])


def _region(cell, layers, window=None):
    """Merged shapes of ``layers`` in the cell and below; with ``window`` (DBox) only the shapes touching it."""
    r = db.Region()
    for li in layers:
        if window is None:
            r += db.Region(cell.begin_shapes_rec(li))
        else:
            r += db.Region(cell.begin_shapes_rec_touching(li, window))
    return r.merged()


def waveguide_ends(cell, core_layers, max_width=3.0, min_stub=0.5, boundary_only=True, tol=0.05, window=None):
    """
    Open waveguide ends of the core: horizontal / vertical edges up to ``max_width`` long with
    core behind them (for at least ``min_stub``) and nothing in front of them.
    boundary_only: only the ends that lie on the bounding box of the core (the device's outer ports).
    window: only look at the core shapes touching this DBox (fast lookup around a click).
    Returns [(DPoint centre, (ux, uy) outward, width)].
    """
    return open_ends(_region(cell, core_layers, window), cell.layout().dbu, max_width, min_stub, boundary_only, tol)


def open_ends(core, dbu, max_width=3.0, min_stub=0.5, boundary_only=False, tol=0.05):
    """Open ends of the merged Region ``core`` (see waveguide_ends): [(DPoint centre, (ux, uy) outward, width)]."""
    if core.is_empty():
        return []
    box = core.bbox().to_dtype(dbu)
    ends = []

    def inside(p):
        q = db.DBox(p, p).enlarged(db.DVector(0.001, 0.001)).to_itype(dbu)
        return not core.interacting(db.Region(q)).is_empty()

    for e in core.edges().each():
        de = e.to_dtype(dbu)
        length = de.length()
        if length > max_width + 1e-9 or length < 0.05:
            continue
        dx, dy = de.dx(), de.dy()
        if abs(dx) > 1e-9 and abs(dy) > 1e-9:
            continue                                   # not horizontal / vertical
        c = db.DPoint((de.p1.x + de.p2.x) / 2, (de.p1.y + de.p2.y) / 2)
        n1 = (-dy / length, dx / length)
        along = (dx / length, dy / length)
        for u in (n1, (-n1[0], -n1[1])):
            out = db.DPoint(c.x + u[0] * 0.02, c.y + u[1] * 0.02)
            back = db.DPoint(c.x - u[0] * min_stub, c.y - u[1] * min_stub)
            if not inside(out) and inside(back) and _straight_sides(inside, de, along, u, min_stub / 2.0):
                if boundary_only:
                    on = ((u[0] > 0.5 and abs(c.x - box.right) < tol) or (u[0] < -0.5 and abs(c.x - box.left) < tol) or
                          (u[1] > 0.5 and abs(c.y - box.top) < tol) or (u[1] < -0.5 and abs(c.y - box.bottom) < tol))
                    if not on:
                        break
                ends.append((c, (round(u[0]), round(u[1])), length))
                break
    return ends


def _straight_sides(inside, de, along, u, depth, gap=0.06):
    """
    A waveguide end has side walls behind it: ``depth`` back from the end edge ``de`` the core is still there
    just inside both edge corners, and not just beyond them.  The flat top of a ring or a disk fails this
    (the core widens right behind its short end edge).
    """
    # ``along`` points from p1 to p2: beyond p1 is -along, beyond p2 is +along
    for p, s in ((de.p1, -1.0), (de.p2, 1.0)):
        base = db.DPoint(p.x - u[0] * depth, p.y - u[1] * depth)
        beyond = db.DPoint(base.x + s * along[0] * gap, base.y + s * along[1] * gap)
        within = db.DPoint(base.x - s * along[0] * 0.02, base.y - s * along[1] * 0.02)
        if inside(beyond) or not inside(within):
            return False
    return True


NARROW_METAL = 40.0        # um: metal narrower than this is a wire / strip (pin at its ends or its middle)


def is_narrow(island, dbu, limit=NARROW_METAL):
    """
    A metal island (Polygon, dbu units) narrower than ``limit`` everywhere: a wire or strip rather than a pad.
    Measured on the metal itself, so a bent wire (L, Z, U ...) counts too: shrunk by half the limit, nothing is left.
    """
    b = island.bbox().to_dtype(dbu)
    if min(b.width(), b.height()) < limit:
        return True
    return db.Region(island).sized(-int(round(limit / 2.0 / dbu))).is_empty()


def whole_island(cell, li, island):
    """
    The complete metal island (Polygon) that ``island`` is part of: a lookup in a window can cut a wire made of
    several shapes; this adds the shapes touching it until nothing more joins.
    """
    for _ in range(20):
        b = island.bbox()
        r = db.Region(cell.begin_shapes_rec_touching(li, b)).merged().interacting(db.Region(island))
        polys = list(r.each())
        if not polys:
            return island
        grown = max(polys, key=lambda p: p.area())
        if grown.bbox() == b:
            return grown
        island = grown
    return island


def wire_width(island, dbu):
    """Width of a wire (Polygon): the width of its open ends, else 2 x area / perimeter."""
    ends = open_ends(db.Region(island), dbu, max_width=NARROW_METAL, min_stub=0.5)
    if ends:
        return max(w for _, _, w in ends)
    p = island.perimeter()
    return 2.0 * island.area() / p * dbu if p > 0 else 0.0


def wire_middle(island, dbu):
    """A point ON the metal near the middle of the island: its bbox centre, else the centre of the piece of it nearest that."""
    r = db.Region(island)
    c = island.bbox().to_dtype(dbu).center()
    probe = db.Region(db.DBox(c, c).enlarged(db.DVector(0.001, 0.001)).to_itype(dbu))
    if not r.interacting(probe).is_empty():
        return c
    pieces = [p.bbox().to_dtype(dbu) for p in r.decompose_trapezoids_to_region().each()]
    if not pieces:
        return c
    best = min(pieces, key=lambda b: b.center().distance(c))
    return best.center()


def strip_ends(island, dbu, limit=NARROW_METAL):
    """
    The open ends of a metal wire (Polygon, dbu units), straight or bent: [(DPoint on the end edge,
    (ux, uy) outward, width of the wire there)].  A straight strip without clear ends: its two ends
    along its long side.
    """
    r = db.Region(island)
    ends = open_ends(r, dbu, max_width=limit, min_stub=0.5)
    if ends:
        return ends
    b = island.bbox().to_dtype(dbu)
    eps = 0.02
    out = []
    if b.width() >= b.height():
        for x0, x1, u in ((b.left, b.left + eps, (-1.0, 0.0)), (b.right - eps, b.right, (1.0, 0.0))):
            s = (r & db.Region(db.DBox(x0, b.bottom, x1, b.top).to_itype(dbu))).bbox().to_dtype(dbu)
            if not s.empty():
                out.append((db.DPoint(b.left if u[0] < 0 else b.right, s.center().y), u, s.height()))
    else:
        for y0, y1, u in ((b.bottom, b.bottom + eps, (0.0, -1.0)), (b.top - eps, b.top, (0.0, 1.0))):
            s = (r & db.Region(db.DBox(b.left, y0, b.right, y1).to_itype(dbu))).bbox().to_dtype(dbu)
            if not s.empty():
                out.append((db.DPoint(s.center().x, b.bottom if u[1] < 0 else b.top), u, s.width()))
    return out


def metal_contacts(cell, metal_layers, outline, window=None, narrow="middle", with_flag=False):
    """
    One contact per separate metal island: (layer index, DPoint centre, (ux, uy) towards the
    nearest side of ``outline`` (DBox), width).  With ``window`` (DBox): only the islands that touch it.
    narrow: for a metal narrower than NARROW_METAL, "middle" (one pin at its centre) or "ends" (a pin on each
    end of the strip, pointing out of it; with ``window`` only the ends inside it).
    with_flag: append True / False (narrow metal) to each tuple.
    """
    dbu = cell.layout().dbu
    out, boxes = [], []
    for li in metal_layers:
        if window is None:
            islands = db.Region(cell.begin_shapes_rec(li)).merged()
        else:
            islands = db.Region(cell.begin_shapes_rec_touching(li, window)).merged().interacting(
                db.Region(window.to_itype(dbu)))
        for poly in islands.each():
            if window is not None:
                poly = whole_island(cell, li, poly)
            b = poly.bbox().to_dtype(dbu)
            # the same pad on stacked metals (e.g. metal 1 under metal 2): one contact, on the first layer listed
            if any(_same_pad(b, o) for o in boxes):
                continue
            boxes.append(b)
            thin = is_narrow(poly, dbu)
            if thin and narrow == "ends":
                for c, u, w in strip_ends(poly, dbu):
                    if window is None or window.contains(c):
                        out.append((li, c, u, max(min(w, 10.0), 0.5)) + ((True,) if with_flag else ()))
                continue
            c = wire_middle(poly, dbu) if thin else b.center()
            dist = {"W": c.x - outline.left, "E": outline.right - c.x,
                    "S": c.y - outline.bottom, "N": outline.top - c.y}
            s = min(dist, key=dist.get)
            w = wire_width(poly, dbu) if thin else (b.height() if s in "EW" else b.width())
            out.append((li, c, SIDES[s], max(min(w, 10.0), 0.5)) + ((thin,) if with_flag else ()))
    return out


def _same_pad(a, b):
    """Two metal islands (DBox) that are one pad on stacked layers: the smaller lies mostly inside the larger."""
    common = a & b
    if common.empty():
        return False
    small = min(a.area(), b.area())
    return small > 0 and common.area() >= 0.5 * small


def existing_pins(cell, pin_layer):
    """[(DPoint centre, name)] of the pins already in the cell (its own shapes and below)."""
    pins = []
    it = cell.begin_shapes_rec(pin_layer)
    while not it.at_end():
        s = it.shape()
        if s.is_path() and s.path.num_points() == 2:
            pts = [it.dtrans() * q for q in s.dpath.each_point()]
            c = db.DPoint((pts[0].x + pts[1].x) / 2, (pts[0].y + pts[1].y) / 2)
            name = ""
            for t in it.cell().shapes(pin_layer).each_touching(s.bbox()):
                if t.is_text():
                    name = t.text_string
            pins.append((c, name))
        it.next()
    return pins


def _order_key(c, u, box):
    """Counter-clockwise around the device, starting at the top of the west side."""
    s = side_of(u)
    if s == "W":
        return (0, -c.y)
    if s == "S":
        return (1, c.x)
    if s == "E":
        return (2, c.y)
    return (3, -c.x)


def propose_pins(cell, core_layers, metal_layers, pin_layer, pinm_layer, boundary_only=True, window=None,
                 narrow="middle"):
    """
    Pins this device should get: dicts kind ('optical'/'electrical'), name, pos, dir, width,
    layer (pin layer index).  Ports that already carry a pin are left out; numbering
    continues after the existing names.
    window (DBox, cell coordinates): only the waveguide ends inside it (anywhere in the cell, not only on
    its outer edge) and the metal islands touching it.
    narrow: "middle" / "ends" - where metal narrower than NARROW_METAL gets its pin(s) (see metal_contacts);
    electrical pins on such metal have "narrow": True.
    """
    have_o = existing_pins(cell, pin_layer)
    have_e = existing_pins(cell, pinm_layer)
    taken = lambda pins, p: any(q.distance(p) < 0.05 for q, _ in pins)

    def next_names(prefix, have, n):
        used = {nm for _, nm in have}
        k, out = 1, []
        while len(out) < n:
            if "%s%d" % (prefix, k) not in used:
                out.append("%s%d" % (prefix, k))
            k += 1
        return out

    if window is None:
        ends = waveguide_ends(cell, core_layers, boundary_only=boundary_only)
    else:
        # shapes around the box (a waveguide end needs some core behind it), ends inside the box
        ends = [e for e in waveguide_ends(cell, core_layers, boundary_only=False, window=window.enlarged(db.DVector(5.0, 5.0)))
                if window.contains(e[0])]
    ends = [e for e in ends if not taken(have_o, e[0])]
    box = cell.dbbox()
    ends.sort(key=lambda e: _order_key(e[0], e[1], box))
    props = [{"kind": "optical", "name": n, "pos": c, "dir": u, "width": w, "layer": pin_layer}
             for (c, u, w), n in zip(ends, next_names("opt", have_o, len(ends)))]

    contacts = [m for m in metal_contacts(cell, metal_layers, box, window, narrow, with_flag=True)
                if not taken(have_e, m[1])]
    contacts.sort(key=lambda m: _order_key(m[1], m[2], box))
    props += [{"kind": "electrical", "name": n, "pos": c, "dir": u, "width": w, "layer": pinm_layer, "narrow": thin}
              for (li, c, u, w, thin), n in zip(contacts, next_names("elec", have_e, len(contacts)))]
    return props


def add_pin(cell, layer, name, pos, u, width):
    """SiEPIC pin: 20 nm path pointing out of the device (direction u) + name text."""
    h = PIN_HALF
    cell.shapes(layer).insert(db.DPath([db.DPoint(pos.x - u[0] * h, pos.y - u[1] * h),
                                        db.DPoint(pos.x + u[0] * h, pos.y + u[1] * h)], width))
    t = db.DText(name, db.DTrans(pos.x, pos.y))
    t.size = max(width / 2.0, 0.3)
    cell.shapes(layer).insert(t)


def ensure_devrec(cell, devrec_layer, layers):
    """Add a DevRec box (bounding box of the device's ``layers``) if the cell has no DevRec. Returns the box or None."""
    if not db.Region(cell.begin_shapes_rec(devrec_layer)).is_empty():
        return None
    r = _region(cell, layers)
    if r.is_empty():
        return None
    box = r.bbox().to_dtype(cell.layout().dbu)
    cell.shapes(devrec_layer).insert(box)
    return box
