"""
klroute.maze -- Manhattan routing around obstacles.

A* search on a grid aligned to the route's start lead point.  The search state
keeps the direction of travel and the straight length run since the last turn,
so every corner has room for its bend (r after the start lead, 2 r between two
corners).  Turns cost extra, so routes with few bends are preferred.

Obstacles are polygons that are already grown by the required clearance.
"""

import bisect
import heapq
import math

from .router import RouteError, RouteSpec

try:
    import pya as db
except ImportError:
    import klayout.db as db

DIRS = [(1, 0), (0, 1), (-1, 0), (0, -1)]  # E N W S


def _dir_index(u):
    best = max(range(4), key=lambda i: DIRS[i][0] * u[0] + DIRS[i][1] * u[1])
    return best


class _Grid:
    def __init__(self, xs, ys, obstacles, edges=False):
        """
        Grid nodes inside an obstacle are blocked.  ``edges``: also block every grid step that crosses an
        obstacle's bounding box (needed when the pitch is coarser than the obstacles, which could otherwise
        lie between two free nodes).
        """
        self.xs, self.ys = xs, ys
        self.blocked = set()
        self.blocked_h, self.blocked_v = set(), set()   # (i, j): step i->i+1 on row j / j->j+1 on column i
        for poly in obstacles:
            b = poly.bbox()
            i0, i1 = bisect.bisect_left(xs, b.left), bisect.bisect_right(xs, b.right)
            j0, j1 = bisect.bisect_left(ys, b.bottom), bisect.bisect_right(ys, b.top)
            for i in range(i0, i1):
                x = xs[i]
                for j in range(j0, j1):
                    if (i, j) not in self.blocked and poly.inside(db.DPoint(x, ys[j])):
                        self.blocked.add((i, j))
            if edges:
                # rows / columns through the box: the steps that overlap it
                for j in range(j0, j1):
                    for i in range(max(i0 - 1, 0), min(i1, len(xs) - 1)):
                        if xs[i] < b.right and xs[i + 1] > b.left:
                            self.blocked_h.add((i, j))
                for i in range(i0, i1):
                    for j in range(max(j0 - 1, 0), min(j1, len(ys) - 1)):
                        if ys[j] < b.top and ys[j + 1] > b.bottom:
                            self.blocked_v.add((i, j))

    def step_blocked(self, i, j, ni, nj):
        if nj == j:
            return (min(i, ni), j) in self.blocked_h
        return (i, min(j, nj)) in self.blocked_v


def _axis(origin, extra, lo, hi, step):
    """Grid coordinates: origin + k*step inside [lo, hi], plus the exact ``extra`` coordinates."""
    k0 = int(math.floor((lo - origin) / step))
    k1 = int(math.ceil((hi - origin) / step))
    vals = {round(origin + k * step, 9) for k in range(k0, k1 + 1)}
    vals.update(round(v, 9) for v in extra)
    return sorted(vals)


def _to_grid(p, u, lead, g):
    """The lead length >= ``lead`` out of point p along the (axis) direction u that ends on a multiple of g."""
    k = 0 if abs(u[0]) >= abs(u[1]) else 1
    s = 1.0 if u[k] > 0 else -1.0
    target = p[k] + s * lead
    on = (math.ceil(target / g - 1e-9) if s > 0 else math.floor(target / g + 1e-9)) * g
    return abs(on - p[k])


def pitch_for(spec, clearance, view_grid=0.0):
    """
    Grid pitch of the search: about half the bend radius, at most 1.9 x the clearance (so no gap between devices
    is jumped), at least 0.5 um.  With ``view_grid`` (the layout view's grid): the largest multiple of it that is
    not above that pitch - or the view grid itself if it is coarser.
    """
    r = spec.bend_radius
    step = max(min(r / 2.0 if r > 0 else 2.0 * spec.width, 1.9 * clearance), 0.5)
    if view_grid and view_grid > 0:
        step = max(int(step / view_grid + 1e-9), 1) * view_grid
    return step


def route_around(p0, d0, p1, d1, spec, obstacles, clearance, grid=None, max_states=600000,
                 lead0=0.0, lead1=0.0, view_grid=0.0):
    """
    Manhattan corner points from p0 to p1 that stay out of ``obstacles``.

    p0, p1     : (x, y) end points (device ports)
    d0         : unit vector, the direction the route leaves p0
    d1         : unit vector pointing out of p1 into the route (port direction)
    spec       : RouteSpec (bend radius, width)
    obstacles  : list of DPolygon, already grown by the clearance
    clearance  : the clearance the obstacles were grown by (sets the grid pitch)
    lead0/1    : minimum straight lead out of p0 / p1 (e.g. to get clear of the port's own device)
    view_grid  : the layout view's grid (um): the search grid is a multiple of it and lies on it, so the
                 corners are on the view grid (0 = a grid starting at the start lead point)
    Returns a list of (x, y) corner points including p0 and p1, or raises RouteError.
    """
    spec = spec or RouteSpec()
    r = spec.bend_radius
    lead = max(r, spec.min_clearance / 2.0, spec.width, 1.0)
    step = grid or pitch_for(spec, clearance, view_grid)
    coarse = step > 1.9 * clearance + 1e-9      # coarser than the obstacles: check every step too
    lead_s, lead_e = max(lead, lead0), max(lead, lead1)
    if view_grid:
        # run each lead on to the next view-grid line, so the first / last corner lies on the view grid
        lead_s, lead_e = _to_grid(p0, d0, lead_s, view_grid), _to_grid(p1, d1, lead_e, view_grid)
    sx, sy = p0[0] + d0[0] * lead_s, p0[1] + d0[1] * lead_s
    ex, ey = p1[0] + d1[0] * lead_e, p1[1] + d1[1] * lead_e
    start_dir = _dir_index(d0)
    goal_dir = _dir_index((-d1[0], -d1[1]))           # travelling into p1
    need_first = max(r - lead_s, 0.0)                   # before the first corner (lead already counts)
    need = 2.0 * r                                      # between two corners
    turn_cost = 2.0 * step + r

    span = max(abs(ex - sx), abs(ey - sy))
    for margin in (max(8 * step, 4 * r, 60.0, 0.5 * span), max(30 * step, 12 * r, 250.0, span)):
        lo_x, hi_x = min(sx, ex) - margin, max(sx, ex) + margin
        lo_y, hi_y = min(sy, ey) - margin, max(sy, ey) + margin
        ox, oy = (0.0, 0.0) if view_grid else (sx, sy)     # on the view grid, else on the start lead point
        xs = _axis(ox, (sx, ex), lo_x, hi_x, step)
        ys = _axis(oy, (sy, ey), lo_y, hi_y, step)
        g = _Grid(xs, ys, obstacles, edges=coarse)
        si, sj = xs.index(round(sx, 9)), ys.index(round(sy, 9))
        gi, gj = xs.index(round(ex, 9)), ys.index(round(ey, 9))
        for (i, j), which, p in (((si, sj), "start", p0), ((gi, gj), "end", p1)):
            if (i, j) in g.blocked:
                raise RouteError(
                    "No room to leave the %s port at (%.3f, %.3f): straight out of it (along the pin "
                    "direction) another device lies within the clearance (%.2f um). Check that the pin "
                    "points out of its device, or move the device in front of it." % (which, p[0], p[1], clearance))
        path = _astar(g, (si, sj, start_dir), (gi, gj, goal_dir), need_first, need, turn_cost, step,
                      max_states)
        if path is not None:
            pts = [(p0[0], p0[1])] + [(xs[i], ys[j]) for i, j in path] + [(p1[0], p1[1])]
            return _corners(pts)
    raise RouteError("No route around the other devices was found. Move a device or the end points.")


def _astar(g, start, goal, need_first, need, turn_cost, step, max_states):
    xs, ys = g.xs, g.ys
    gx, gy = xs[goal[0]], ys[goal[1]]

    def h(i, j):
        return abs(xs[i] - gx) + abs(ys[j] - gy)

    def bucket(run):
        return min(int(run / step + 1e-6), int(math.ceil(need / step)) + 1)

    # state: (i, j, dir, turned?, run since last corner)
    i, j, d = start
    open_ = [(h(i, j), 0.0, i, j, d, False, 0.0)]
    best = {}
    parent = {}
    key0 = (i, j, d, False, bucket(0.0))
    best[key0] = 0.0
    parent[key0] = None
    n = 0
    while open_:
        f, cost, i, j, d, turned, run = heapq.heappop(open_)
        key = (i, j, d, turned, bucket(run))
        if best.get(key, float("inf")) < cost - 1e-9:
            continue
        at_goal = (i, j) == goal[:2] and (
            d == goal[2] or
            # one last corner exactly at the end lead point, if there is room for it
            ((d - goal[2]) % 4 in (1, 3) and run >= (need if turned else need_first) - 1e-9))
        if at_goal:
            # reconstruct the visited grid nodes
            nodes = []
            k = key
            while k is not None:
                nodes.append((k[0], k[1]))
                k = parent[k]
            nodes.reverse()
            out = []
            for q in nodes:
                if not out or out[-1] != q:
                    out.append(q)
            return out
        n += 1
        if n > max_states:
            return None
        moves = [(d, 0.0)]
        if run >= (need if turned else need_first) - 1e-9:
            moves += [((d + 1) % 4, turn_cost), ((d + 3) % 4, turn_cost)]
        for nd, extra in moves:
            di, dj = DIRS[nd]
            ni, nj = i + di, j + dj
            if not (0 <= ni < len(xs) and 0 <= nj < len(ys)) or (ni, nj) in g.blocked or g.step_blocked(i, j, ni, nj):
                continue
            dist = abs(xs[ni] - xs[i]) + abs(ys[nj] - ys[j])
            nturned = turned or nd != d
            nrun = (run if nd == d else 0.0) + dist
            ncost = cost + dist + extra
            nkey = (ni, nj, nd, nturned, bucket(nrun))
            if ncost < best.get(nkey, float("inf")) - 1e-9:
                best[nkey] = ncost
                parent[nkey] = key
                heapq.heappush(open_, (ncost + h(ni, nj), ncost, ni, nj, nd, nturned, nrun))
    return None


def _corners(pts):
    """Keep only the end points and the corners."""
    out = [pts[0]]
    for k in range(1, len(pts) - 1):
        a, b, c = out[-1], pts[k], pts[k + 1]
        if abs((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])) > 1e-9:
            out.append(b)
    out.append(pts[-1])
    return out
