"""
klroute.router -- point-to-point routing of waveguides and metal traces.

Coordinates are in microns (KLayout "D" types).  The router works in three steps:

1.  Manhattan waypoints are chosen between the two end points, given the
    direction the route leaves each end point.  Several candidate topologies
    (straight, L, Z, S-bend, U, loops) are generated and the shortest one that
    fits the bend radius / clearance rules is kept.
2.  For waveguides every corner is replaced by a circular or Euler bend.
3.  The centerline is turned into a polygon (waveguide) or a path (metal).

Direction convention
--------------------
Both ``start_dir`` and ``end_dir`` point *out of the end point into the route*,
like the orientation of a port.  A straight route from left to right therefore
has ``start_dir="E"`` and ``end_dir="W"``.  ``"auto"`` picks the dominant axis.
"""

import math
from dataclasses import dataclass, field

try:
    import pya as db  # inside KLayout
except ImportError:  # standalone "pip install klayout"
    import klayout.db as db


DIRECTIONS = {"E": (1.0, 0.0), "W": (-1.0, 0.0), "N": (0.0, 1.0), "S": (0.0, -1.0)}
EPS = 1e-9


class RouteError(Exception):
    """Raised when no route fits between the two points with the given rules."""


@dataclass
class RouteSpec:
    kind: str = "waveguide"     # "waveguide" or "metal"
    width: float = 0.5          # core / trace width [um]
    radius: float = 10.0        # minimum bend radius [um] (waveguide only)
    bend: str = "circular"      # "circular" or "euler" (waveguide only)
    clearance: float = None     # min. spacing used for detours; default: 2*width for metal, 0 for waveguides
    cladding: float = 0.0       # extra width per side for a cladding / exclusion layer [um]
    tolerance: float = 0.001    # max. deviation of the polygonised arc from the true arc [um]

    @property
    def is_waveguide(self):
        return self.kind.lower().startswith("w")

    @property
    def bend_radius(self):
        return self.radius if self.is_waveguide else 0.0

    @property
    def min_clearance(self):
        if self.clearance is not None:
            return self.clearance
        return 0.0 if self.is_waveguide else 2.0 * self.width


@dataclass
class Route:
    spec: RouteSpec
    waypoints: list          # Manhattan / corner points (DPoint)
    centerline: list         # centerline with the bends rendered (DPoint)
    length: float            # true centerline length [um]
    start_dir: str = ""
    end_dir: str = ""
    bends: int = field(default=0)

    def polygon(self, extra=0.0):
        """Polygon of the route widened by ``extra`` on each side (e.g. cladding)."""
        return db.DPath(self.centerline, self.spec.width + 2.0 * extra).polygon()

    def path(self, extra=0.0):
        return db.DPath(self.centerline, self.spec.width + 2.0 * extra)

    def component_path(self, width, offset=0.0):
        """Path of ``width`` whose center is ``offset`` to the left of the centerline."""
        pts = self.centerline if abs(offset) < EPS else offset_polyline(self.centerline, offset)
        return db.DPath(pts, width)

    def component_polygon(self, width, offset=0.0):
        return self.component_path(width, offset).polygon()

    def component_shape(self, width, offset=0.0, as_path=True):
        """Path (default) or polygon of one component of the route."""
        path = self.component_path(width, offset)
        return path if as_path else path.polygon()


def offset_polyline(points, offset):
    """Offset a (smooth) polyline by ``offset`` to the left of its direction of travel."""
    p = [(q.x, q.y) for q in points]
    n = len(p)
    normals = []
    for i in range(n - 1):
        u = _unit(_sub(p[i + 1], p[i]))
        normals.append((-u[1], u[0]))
    out = []
    for i in range(n):
        if i == 0:
            nx, ny = normals[0]
        elif i == n - 1:
            nx, ny = normals[-1]
        else:
            a, b = normals[i - 1], normals[i]
            mx, my = a[0] + b[0], a[1] + b[1]
            m = math.hypot(mx, my)
            mx, my = mx / m, my / m
            scale = 1.0 / max(0.2, mx * a[0] + my * a[1])  # miter
            nx, ny = mx * scale, my * scale
        out.append(db.DPoint(p[i][0] + nx * offset, p[i][1] + ny * offset))
    return out


# ---------------------------------------------------------------------------
# bend shapes
#
# A bend generator returns (points, tangent_length, arc_length) for a left turn
# by ``theta`` starting at (0, 0) heading +x.  ``tangent_length`` is the
# distance from the corner of the two straight lines to where the bend starts.


def _circular_bend(theta, r, tol):
    n = _segments_for(theta, r, tol)
    pts = [(r * math.sin(theta * i / n), r * (1.0 - math.cos(theta * i / n))) for i in range(n + 1)]
    return pts, r * math.tan(theta / 2.0), r * theta


def _euler_bend(theta, r, tol):
    # Symmetric clothoid: curvature ramps linearly 0 -> 1/r -> 0, so the
    # minimum radius is r.  Each half has length theta*r.
    half = theta * r
    total = 2.0 * half
    n_out = 2 * _segments_for(theta, r, tol)
    sub = 16
    steps = n_out * sub
    ds = total / steps
    x = y = 0.0
    pts = [(0.0, 0.0)]
    for i in range(steps):
        s = (i + 0.5) * ds
        if s < half:
            phi = s * s / (2.0 * r * half)
        else:
            u = total - s
            phi = theta - u * u / (2.0 * r * half)
        x += math.cos(phi) * ds
        y += math.sin(phi) * ds
        if (i + 1) % sub == 0:
            pts.append((x, y))
    tangent = y / math.sin(theta)
    return pts, tangent, total


def _segments_for(theta, r, tol):
    if r <= tol:
        return 1
    dphi = 2.0 * math.acos(max(-1.0, 1.0 - tol / r))
    return max(2, int(math.ceil(theta / max(dphi, 1e-3))))


_BENDS = {"circular": _circular_bend, "euler": _euler_bend}
_tangent_cache = {}


def _tangent(theta, spec):
    """Tangent length of a bend with turning angle ``theta`` (0 for metal)."""
    r = spec.bend_radius
    if r <= 0.0 or theta < 1e-9:
        return 0.0
    key = (spec.bend, round(theta, 12), r)
    if key not in _tangent_cache:
        _tangent_cache[key] = _BENDS[spec.bend](theta, r, spec.tolerance)[1]
    return _tangent_cache[key]


# ---------------------------------------------------------------------------
# small vector helpers on (x, y) tuples


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1])


def _norm(v):
    return math.hypot(v[0], v[1])


def _unit(v):
    n = _norm(v)
    return (v[0] / n, v[1] / n)


def _turn(u_in, u_out):
    """Signed turning angle from direction u_in to u_out (positive = left)."""
    cross = u_in[0] * u_out[1] - u_in[1] * u_out[0]
    dot = u_in[0] * u_out[0] + u_in[1] * u_out[1]
    return math.atan2(cross, dot)


def _dedupe(points):
    """Drop repeated and collinear points."""
    out = []
    for p in points:
        if out and _norm(_sub(p, out[-1])) < 1e-6:
            continue
        out.append(p)
    i = 1
    while i < len(out) - 1:
        a, b, c = out[i - 1], out[i], out[i + 1]
        if abs(_turn(_unit(_sub(b, a)), _unit(_sub(c, b)))) < 1e-9:
            del out[i]
        else:
            i += 1
    return out


# ---------------------------------------------------------------------------
# waypoint generation


def auto_directions(p0, p1):
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    if abs(dx) >= abs(dy):
        return ("E", "W") if dx >= 0 else ("W", "E")
    return ("N", "S") if dy >= 0 else ("S", "N")


def _candidates(ex, ey, d1, spec):
    """
    Candidate waypoint lists in a local frame: the route starts at (0, 0)
    heading +x and must arrive at (ex, ey) travelling in direction d1, where d1
    is +x, -x or +y (-y is handled by mirroring).
    """
    r = spec.bend_radius
    lead = max(r, spec.min_clearance / 2.0)     # straight piece next to an end point
    gap = max(2.0 * r, spec.min_clearance)       # spacing between two corners
    start, end = (0.0, 0.0), (ex, ey)
    c = []

    if d1 == (1, 0):
        if abs(ey) < EPS:
            c.append([start, end])
        t = ex / 2.0
        c.append([start, (t, 0.0), (t, ey), end])
        if r > 0.0 and abs(ey) > EPS:
            c.append(_s_bend(ex, ey, spec))
        # loop back around (end is behind the start or too close)
        xb = min(ex - lead, lead - gap)
        for m in (ey / 2.0, max(0.0, ey) + gap, min(0.0, ey) - gap):
            c.append([start, (lead, 0.0), (lead, m), (xb, m), (xb, ey), end])

    elif d1 == (-1, 0):
        x = max(lead, ex + lead)
        c.append([start, (x, 0.0), (x, ey), end])

    else:  # d1 == (0, 1)
        c.append([start, (ex, 0.0), end])
        a_opts = {lead, ex + gap, ex - gap, ex / 2.0}
        m_opts = {ey - lead, min(ey - lead, -gap), (ey - lead) / 2.0, -gap}
        for a in a_opts:
            for m in m_opts:
                c.append([start, (a, 0.0), (a, m), (ex, m), end])

    return [p for p in c if p is not None]


def _s_bend(ex, ey, spec):
    """Two-bend S with a diagonal middle section, for lateral offsets < 2r."""
    h = abs(ey)

    def fits(theta):
        return h / math.sin(theta) >= 2.0 * _tangent(theta, spec) - EPS

    lo, hi = 1e-6, math.pi / 2.0
    if fits(hi):
        theta = hi
    else:
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            lo, hi = (mid, hi) if fits(mid) else (lo, mid)
        theta = lo
    dx = h / math.tan(theta)
    x1 = (ex - dx) / 2.0
    return [(0.0, 0.0), (x1, 0.0), (x1 + dx, ey), (ex, ey)]


def _check(points, d0, d1, spec):
    """Return total centerline length if ``points`` is a legal route, else None."""
    pts = _dedupe(points)
    if len(pts) < 2:
        return None
    dirs = [_unit(_sub(pts[i + 1], pts[i])) for i in range(len(pts) - 1)]
    if _norm(_sub(dirs[0], d0)) > 1e-6 or _norm(_sub(dirs[-1], d1)) > 1e-6:
        return None
    tangents = [0.0]
    for i in range(1, len(pts) - 1):
        theta = abs(_turn(dirs[i - 1], dirs[i]))
        if theta > math.pi - 1e-6:
            return None  # reversal
        tangents.append(_tangent(theta, spec))
    tangents.append(0.0)
    for i in range(len(pts) - 1):
        if _norm(_sub(pts[i + 1], pts[i])) + 1e-6 < tangents[i] + tangents[i + 1]:
            return None
    return sum(_norm(_sub(pts[i + 1], pts[i])) for i in range(len(pts) - 1))


def manhattan_waypoints(p0, p1, start_dir, end_dir, spec):
    """Corner points of the route from p0 to p1 (as (x, y) tuples)."""
    p0, p1 = (p0[0], p0[1]), (p1[0], p1[1])
    if _norm(_sub(p1, p0)) < EPS:
        raise RouteError("Start and end point coincide.")
    cx, cy = DIRECTIONS[start_dir]
    ox, oy = DIRECTIONS[end_dir]
    # travel direction when arriving at p1 is opposite to the port direction
    tx, ty = -ox, -oy

    def to_local(v):
        return (v[0] * cx + v[1] * cy, -v[0] * cy + v[1] * cx)

    ex, ey = to_local(_sub(p1, p0))
    d1 = tuple(int(round(v)) for v in to_local((tx, ty)))
    flip = d1 == (0, -1)
    if flip:
        ey, d1 = -ey, (0, 1)

    def to_global(q):
        lx, ly = q[0], -q[1] if flip else q[1]
        return (p0[0] + lx * cx - ly * cy, p0[1] + lx * cy + ly * cx)

    best = None
    for cand in _candidates(ex, ey, d1, spec):
        length = _check(cand, (1.0, 0.0), (float(d1[0]), float(d1[1])), spec)
        if length is None:
            continue
        score = (round(length, 6), len(_dedupe(cand)))
        if best is None or score < best[0]:
            best = (score, cand)
    if best is None:
        raise RouteError(
            "No route fits: the points are too close for a %.3g um bend radius / "
            "%.3g um clearance with directions %s -> %s. Try a smaller radius or other directions."
            % (spec.bend_radius, spec.min_clearance, start_dir, end_dir))
    pts = _dedupe([to_global(q) for q in best[1]])
    pts[0], pts[-1] = p0, p1  # exact end points
    return pts


# ---------------------------------------------------------------------------
# bend rendering


def _round_corners(pts, spec):
    if spec.bend_radius <= 0.0 or len(pts) < 3:
        return list(pts), sum(_norm(_sub(pts[i + 1], pts[i])) for i in range(len(pts) - 1))

    gen = _BENDS[spec.bend]
    out = [pts[0]]
    length = 0.0
    prev_exit = pts[0]
    for i in range(1, len(pts) - 1):
        u_in = _unit(_sub(pts[i], pts[i - 1]))
        u_out = _unit(_sub(pts[i + 1], pts[i]))
        signed = _turn(u_in, u_out)
        theta = abs(signed)
        local, tangent, arc = gen(theta, spec.bend_radius, spec.tolerance)
        s = 1.0 if signed > 0 else -1.0
        n = (-u_in[1] * s, u_in[0] * s)
        bx, by = pts[i][0] - u_in[0] * tangent, pts[i][1] - u_in[1] * tangent
        length += _norm(_sub((bx, by), prev_exit)) + arc
        for lx, ly in local:
            out.append((bx + u_in[0] * lx + n[0] * ly, by + u_in[1] * lx + n[1] * ly))
        # snap the bend exit exactly onto the outgoing line
        prev_exit = (pts[i][0] + u_out[0] * tangent, pts[i][1] + u_out[1] * tangent)
        out[-1] = prev_exit
    length += _norm(_sub(pts[-1], prev_exit))
    out.append(pts[-1])
    return _dedupe(out), length


# ---------------------------------------------------------------------------
# public API


def route(p0, p1, spec=None, start_dir="auto", end_dir="auto"):
    """
    Compute a route between two points.

    p0, p1     -- DPoint or (x, y) in microns
    spec       -- RouteSpec (waveguide by default)
    start_dir  -- "E", "W", "N", "S" or "auto": direction the route leaves p0
    end_dir    -- "E", "W", "N", "S" or "auto": direction the route leaves p1
                  (i.e. the side of p1 it arrives from)
    """
    spec = spec or RouteSpec()
    if spec.bend not in _BENDS:
        raise RouteError("Unknown bend type %r (use 'circular' or 'euler')." % spec.bend)
    a = (p0.x, p0.y) if hasattr(p0, "x") else tuple(p0)
    b = (p1.x, p1.y) if hasattr(p1, "x") else tuple(p1)
    auto0, auto1 = auto_directions(a, b)
    sd = auto0 if start_dir in (None, "", "auto") else start_dir.upper()
    ed = auto1 if end_dir in (None, "", "auto") else end_dir.upper()
    for d in (sd, ed):
        if d not in DIRECTIONS:
            raise RouteError("Direction must be one of E, W, N, S or auto (got %r)." % d)

    corners = manhattan_waypoints(a, b, sd, ed, spec)
    center, length = _round_corners(corners, spec)
    return Route(
        spec=spec,
        waypoints=[db.DPoint(x, y) for x, y in corners],
        centerline=[db.DPoint(x, y) for x, y in center],
        length=length,
        start_dir=sd,
        end_dir=ed,
        bends=len(corners) - 2,
    )


def route_through(points, spec=None):
    """
    Route through a list of points, like a KLayout path: the points are the
    corners.  For waveguides every corner is rounded with the bend radius; a
    RouteError is raised if a segment is too short for the bends at its ends.
    """
    spec = spec or RouteSpec()
    if spec.bend not in _BENDS:
        raise RouteError("Unknown bend type %r (use 'circular' or 'euler')." % spec.bend)
    pts = _dedupe([(p.x, p.y) if hasattr(p, "x") else (p[0], p[1]) for p in points])
    if len(pts) < 2:
        raise RouteError("A route needs at least two different points.")
    dirs = [_unit(_sub(pts[i + 1], pts[i])) for i in range(len(pts) - 1)]
    tangents = [0.0]
    for i in range(1, len(pts) - 1):
        theta = abs(_turn(dirs[i - 1], dirs[i]))
        if theta > math.pi - 1e-6:
            raise RouteError("The path turns back on itself at point %d." % (i + 1))
        tangents.append(_tangent(theta, spec))
    tangents.append(0.0)
    for i in range(len(pts) - 1):
        seg = _norm(_sub(pts[i + 1], pts[i]))
        need = tangents[i] + tangents[i + 1]
        if seg + 1e-6 < need:
            raise RouteError(
                "Segment %d -> %d is %.3f um long but its bends need %.3f um "
                "(bend radius %.3g um). Move the points apart or reduce the radius."
                % (i + 1, i + 2, seg, need, spec.bend_radius))
    center, length = _round_corners(pts, spec)
    return Route(
        spec=spec,
        waypoints=[db.DPoint(x, y) for x, y in pts],
        centerline=[db.DPoint(x, y) for x, y in center],
        length=length,
        bends=len(pts) - 2,
    )


def insert_route(cell, layer, rt, cladding_layer=None, trans=None, as_path=None):
    """
    Put a computed route into ``cell``.

    layer           -- layer index of the core / metal
    cladding_layer  -- optional layer index for the cladding (uses spec.cladding)
    trans           -- optional DCplxTrans applied before insertion
    as_path         -- True: KLayout path shapes, False: polygons.
                       Default: path for metal, polygon for waveguides.
    Returns the list of inserted shapes.
    """
    if as_path is None:
        as_path = not rt.spec.is_waveguide
    shapes = []
    t = trans or db.DCplxTrans()
    shapes.append(cell.shapes(layer).insert(rt.component_shape(rt.spec.width, 0.0, as_path).transformed(t)))
    if cladding_layer is not None and rt.spec.cladding > 0.0:
        w = rt.spec.width + 2.0 * rt.spec.cladding
        shapes.append(cell.shapes(cladding_layer).insert(rt.component_shape(w, 0.0, as_path).transformed(t)))
    return shapes


def draw_route(cell, layer, p0, p1, spec=None, start_dir="auto", end_dir="auto", cladding_layer=None):
    """Convenience: compute and insert a route.  Returns the Route."""
    rt = route(p0, p1, spec, start_dir, end_dir)
    insert_route(cell, layer, rt, cladding_layer)
    return rt
