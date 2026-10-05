"""
klroute.autoconnect -- connect every optical port of a device to a column of
grating couplers (fibre array) without crossings.

Planning is done in a canonical frame with the GC column on the LEFT of the
device (GCs facing +x).  Other sides are handled by transforming in and out.

Ports are ordered counter-clockwise around the device, starting at the middle
of the side facing away from the GCs, and assigned to the GCs from top to
bottom.  Ports that face away from the GC column escape over the top or under
the bottom of the device on nested "escape" levels; every route gets its own
vertical "lane" between the GC column and the device, nested so that no two
routes cross.  The result is a list of corner points per port; the bends are
added by the router.
"""

from dataclasses import dataclass

from .router import RouteError, RouteSpec, route

SIDES = {
    # canonical -> real:  (x, y) -> ...
    "left": lambda x, y: (x, y),
    "right": lambda x, y: (-x, y),
    "bottom": lambda x, y: (-y, x),     # rotate +90 deg: the left side goes to the bottom
    "top": lambda x, y: (y, -x),        # rotate -90 deg: the left side goes to the top
}
INVERSE = {
    "left": lambda x, y: (x, y),
    "right": lambda x, y: (-x, y),
    "bottom": lambda x, y: (y, -x),
    "top": lambda x, y: (-y, x),
}


@dataclass
class Port:
    name: str
    x: float
    y: float
    dx: float   # unit vector pointing out of the device
    dy: float


@dataclass
class Connection:
    port: Port
    points: list        # corner points (x, y), starting at the GC pin, ending at the port (real frame)
    gc_index: int       # 0 = first GC of the column (top in the canonical frame)


def _side_of(dx, dy):
    if abs(dx) > 0.99:
        return "E" if dx > 0 else "W"
    if abs(dy) > 0.99:
        return "N" if dy > 0 else "S"
    return None


def plan(device_box, ports, spec, side="left", pitch=127.0, spacing=0.0, distance=0.0):
    """
    device_box : (left, bottom, right, top) of the device in microns (real frame)
    ports      : list of Port (real frame)
    spec       : RouteSpec of the waveguide (bend radius, width)
    side       : where the GC column goes: left / right / top / bottom
    pitch      : GC pitch [um] (fibre array, typ. 127)
    spacing    : centre-to-centre spacing of parallel routes [um]; 0 = width + 2 + ...
    distance   : minimum distance from the device to the GC pins [um]
    Returns (gc_pins, connections): gc_pins = list of (x, y) of the GC pins (real frame), top to bottom.
    """
    if side not in SIDES:
        raise RouteError("GC array side must be left, right, top or bottom.")
    if not ports:
        raise RouteError("The device has no free optical ports.")
    to_c, to_r = INVERSE[side], SIDES[side]

    # --- into the canonical frame (GCs on the left)
    l, b, r_, t = device_box
    corners = [to_c(x, y) for x, y in ((l, b), (l, t), (r_, b), (r_, t))]
    left = min(c[0] for c in corners)
    right = max(c[0] for c in corners)
    bottom = min(c[1] for c in corners)
    top = max(c[1] for c in corners)
    yc = 0.5 * (bottom + top)
    cports = []
    for p in ports:
        x, y = to_c(p.x, p.y)
        dx, dy = to_c(p.dx, p.dy)
        s = _side_of(dx, dy)
        if s is None:
            raise RouteError("Port %s is not horizontal or vertical; cannot connect it to the GC array." % p.name)
        cports.append((p, x, y, s))

    radius = spec.bend_radius
    sp = spacing if spacing > 0 else max(spec.width + 2.0, 2.0)
    margin = max(2.0 * radius, sp, 2.0 * spec.width)

    # --- counter-clockwise order, starting at the middle of the east side going up
    e_up = sorted([c for c in cports if c[3] == "E" and c[2] >= yc], key=lambda c: c[2])
    north = sorted([c for c in cports if c[3] == "N"], key=lambda c: -c[1])
    west = sorted([c for c in cports if c[3] == "W"], key=lambda c: -c[2])
    south = sorted([c for c in cports if c[3] == "S"], key=lambda c: c[1])
    e_low = sorted([c for c in cports if c[3] == "E" and c[2] < yc], key=lambda c: c[2])
    order = e_up + north + west + south + e_low
    n = len(order)

    # --- escape levels (nested: outermost = first of the top group / last of the bottom group)
    top_group = e_up + north
    bot_group = south + e_low
    top_edge = max([top] + [c[2] for c in north])
    bot_edge = min([bottom] + [c[2] for c in south])
    right_edge = max([right] + [c[1] for c in e_up + e_low])
    target = {}      # id(port) -> canonical target y of the lane (the y the route turns east at)
    shape = {}       # id(port) -> extra corner points after the lane, before the port (canonical)
    m = len(top_group)
    for i, c in enumerate(top_group):
        y_esc = top_edge + margin + (m - 1 - i) * sp
        target[id(c)] = y_esc
        if c[3] == "N":
            shape[id(c)] = [(c[1], y_esc)]
        else:
            ne = len(e_up)
            x_e = right_edge + margin + (ne - 1 - i) * sp
            shape[id(c)] = [(x_e, y_esc), (x_e, c[2])]
    k = len(bot_group)
    for i, c in enumerate(bot_group):
        y_esc = bot_edge - margin - i * sp
        target[id(c)] = y_esc
        if c[3] == "S":
            shape[id(c)] = [(c[1], y_esc)]
        else:
            x_e = right_edge + margin + (i - len(south)) * sp
            shape[id(c)] = [(x_e, y_esc), (x_e, c[2])]
    for c in west:
        target[id(c)] = c[2]
        shape[id(c)] = []

    # --- GC rows, centred on the device, shifted by the smallest amount that leaves every
    #     route either straight or with room for its two bends (|dy| = 0 or >= 2 r)
    base = [yc + ((n - 1) / 2.0 - i) * pitch for i in range(n)]
    ty_list = [target[id(c)] for c in order]
    need = 2.0 * radius

    def ok(off):
        for b0, ty, c in zip(base, ty_list, order):
            dy = abs(b0 + off - ty)
            if 1e-6 < dy < need - 1e-9 and c[3] != "W":
                return False
        return True

    candidates = sorted({0.0} | {ty - b0 for b0, ty in zip(base, ty_list)} |
                        {round(s * 0.5, 3) for s in range(-int(pitch), int(pitch) + 1)}, key=abs)
    off = next((o for o in candidates if ok(o)), 0.0)
    gy = [b0 + off for b0 in base]

    # --- lanes: x positions left of the device; nested separately for downward and upward routes
    lane_of = {}
    down = [i for i in range(n) if gy[i] > target[id(order[i])] + 1e-9]
    up = [i for i in range(n) if gy[i] < target[id(order[i])] - 1e-9]
    for j, i in enumerate(sorted(down, key=lambda i: -gy[i])):
        lane_of[i] = j
    for j, i in enumerate(sorted(up, key=lambda i: gy[i])):
        lane_of[i] = j
    n_lanes = max([0] + [v + 1 for v in lane_of.values()])
    west_edge = min([left] + [c[1] for c in west])

    def lane_x(j):
        return west_edge - margin - j * sp

    xg = min(lane_x(max(n_lanes - 1, 0)) - 2.0 * max(radius, sp), west_edge - distance)

    connections = []
    for i, c in enumerate(order):
        p, px, py, s = c
        g = (xg, gy[i])
        ty = target[id(c)]
        if i in lane_of:
            lx = lane_x(lane_of[i])
            if s == "W" and abs(gy[i] - py) < 2.0 * radius:
                # small offset: let the router make an S-bend directly
                rt = route(g, (px, py), spec, "E", "W")
                pts = [(q.x, q.y) for q in rt.waypoints]
            else:
                pts = [g, (lx, gy[i]), (lx, ty)] + shape[id(c)] + [(px, py)]
        else:
            # GC row already at the target height: straight east, then the escape corners
            pts = [g] + shape[id(c)] + [(px, py)]
        real = [to_r(x, y) for x, y in pts]
        connections.append(Connection(port=p, points=real, gc_index=i))
    gc_pins = [to_r(xg, y) for y in gy]
    return gc_pins, connections
