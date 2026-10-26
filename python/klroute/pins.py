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
    ly = cell.layout()
    dbu = ly.dbu
    core = _region(cell, core_layers, window)
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


def metal_contacts(cell, metal_layers, outline):
    """
    One contact per separate metal island: (layer index, DPoint centre, (ux, uy) towards the
    nearest side of ``outline`` (DBox), width).
    """
    dbu = cell.layout().dbu
    out, boxes = [], []
    for li in metal_layers:
        for poly in db.Region(cell.begin_shapes_rec(li)).merged().each():
            b = poly.bbox().to_dtype(dbu)
            # the same pad on stacked metals (e.g. metal 1 under metal 2): one contact, on the first layer listed
            if any(_same_pad(b, o) for o in boxes):
                continue
            boxes.append(b)
            c = b.center()
            dist = {"W": c.x - outline.left, "E": outline.right - c.x,
                    "S": c.y - outline.bottom, "N": outline.top - c.y}
            s = min(dist, key=dist.get)
            w = b.height() if s in "EW" else b.width()
            out.append((li, c, SIDES[s], max(min(w, 10.0), 0.5)))
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


def propose_pins(cell, core_layers, metal_layers, pin_layer, pinm_layer, boundary_only=True):
    """
    Pins this device should get: dicts kind ('optical'/'electrical'), name, pos, dir, width,
    layer (pin layer index).  Ports that already carry a pin are left out; numbering
    continues after the existing names.
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

    ends = [e for e in waveguide_ends(cell, core_layers, boundary_only=boundary_only) if not taken(have_o, e[0])]
    box = cell.dbbox()
    ends.sort(key=lambda e: _order_key(e[0], e[1], box))
    props = [{"kind": "optical", "name": n, "pos": c, "dir": u, "width": w, "layer": pin_layer}
             for (c, u, w), n in zip(ends, next_names("opt", have_o, len(ends)))]

    contacts = [m for m in metal_contacts(cell, metal_layers, box) if not taken(have_e, m[1])]
    contacts.sort(key=lambda m: _order_key(m[1], m[2], box))
    props += [{"kind": "electrical", "name": n, "pos": c, "dir": u, "width": w, "layer": pinm_layer}
              for (li, c, u, w), n in zip(contacts, next_names("elec", have_e, len(contacts)))]
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
