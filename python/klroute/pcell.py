"""
klroute.pcell -- the "klroute" library with an editable "Route" PCell.

The route is stored as a guiding path (the corner points).  Select the
instance and drag the corners with the Partial tool, or double-click it to
change width / radius / layers: everything is recomputed every time.

Optionally the cell also contains, regenerated from the same corner points:
* the PDK's own waveguide cell (``pdk_lib`` + ``waveguide_type``, e.g. the
  PDK's "Waveguide" cell) instead of klroute's own layers,
* PDK grating couplers at the start and/or end (``gc_cell`` / ``gc_ends``),
* a SiEPIC opt_in test label at the fiber target of the input GC (``label``).
So grating couplers and labels move with the route when it is edited.
"""

import math

import pya

from . import gc as gcmod
from .router import RouteError, RouteSpec, route_through

from .profile import P as _P
from . import profile as _profile

LIB_NAME = _P["lib"]
PCELL_NAME = "Route"


def _floats(values):
    return [float(v) for v in values]


# Via geometry (defaults from the profile): a block of UNIT x UNIT tiles, each with a grid of
# CUT x CUT cuts at PITCH inside a metal ENCLOSURE, and landings on both metals.
VIA_UNIT = float(_P["via_unit"])          # built-in values (PCell parameter defaults)
VIA_CUT = float(_P["via_cut"])
VIA_PITCH = float(_P["via_pitch"])
VIA_ENCLOSURE = float(_P["via_enclosure"])


def via_default(key):
    """Via geometry default of the profile in use: 'unit', 'cut', 'pitch' or 'enclosure'."""
    return float(_P["via_" + key])


def via_layout(block, width, unit=None, cut=None, pitch=None, enclosure=None):
    """(side, tiles per row, cuts per row in a tile, tile size) of a via block."""
    unit = via_default("unit") if unit is None else unit
    cut = via_default("cut") if cut is None else cut
    pitch = via_default("pitch") if pitch is None else pitch
    enclosure = via_default("enclosure") if enclosure is None else enclosure
    size = block if block > 0 else width
    if 0 < size < unit:
        unit = size                     # a block smaller than one tile: one tile of that size
    n = max(1, int(math.ceil(size / unit - 1e-9)))
    m = max(1, int((unit - 2 * enclosure - cut) / pitch + 1e-9) + 1)
    return n * unit, n, m, unit


def draw_via(cell, center, landing_layers, cut_layer, block=0.0, width=0.0,
             unit=None, cut=None, pitch=None, enclosure=None,
             excl_layers=(), excl_margin=0.0):
    """
    Via block centred at ``center``: n x n tiles of ``unit`` (n = block / unit, rounded up;
    block 0 = the route width rounded up), each with m x m cuts of ``cut`` at ``pitch``
    (m from the tile size, the cut size, the pitch and the metal ``enclosure`` around the
    cuts), cuts on ``cut_layer``, a landing square on every ``landing_layers`` and an
    exclusion square (block + ``excl_margin`` per side) on every ``excl_layers``.
    The defaults come from the profile (via_unit, via_cut, via_pitch, via_enclosure).
    Returns the side length.
    """
    side, n, m, unit = via_layout(block, width, unit, cut, pitch, enclosure)
    cut = via_default("cut") if cut is None else cut
    pitch = via_default("pitch") if pitch is None else pitch
    x0, y0 = center.x - side / 2.0, center.y - side / 2.0
    for li in landing_layers:
        cell.shapes(li).insert(pya.DBox(x0, y0, x0 + side, y0 + side))
    for li in excl_layers:
        cell.shapes(li).insert(pya.DBox(x0 - excl_margin, y0 - excl_margin,
                                        x0 + side + excl_margin, y0 + side + excl_margin))
    if cut_layer is None:
        return side            # direct contact between the two metals: landing pads only, no cuts
    off = -(m - 1) * pitch / 2.0
    for i in range(n):
        for j in range(n):
            ux, uy = x0 + (i + 0.5) * unit, y0 + (j + 0.5) * unit
            for a in range(m):
                for b in range(m):
                    cx, cy = ux + off + a * pitch, uy + off + b * pitch
                    cell.shapes(cut_layer).insert(pya.DBox(cx - cut / 2, cy - cut / 2, cx + cut / 2, cy + cut / 2))
    return side


def cell_pins(cell, pin_layer):
    """{pin name: (position, outward unit direction)} of a cell's SiEPIC pins (2-point paths + name text)."""
    pins = {}
    shapes = []
    it = cell.begin_shapes_rec(pin_layer)
    while not it.at_end():
        sh = it.shape()
        if sh.is_path() and sh.path.num_points() == 2:
            a, b = [it.dtrans() * q for q in sh.dpath.each_point()]
            v = b - a
            n = v.abs()
            if n > 0:
                shapes.append((pya.DPoint((a.x + b.x) / 2, (a.y + b.y) / 2), pya.DVector(v.x / n, v.y / n)))
        elif sh.is_text():
            t = sh.dtext.transformed(it.dtrans())
            pins.setdefault("_texts", []).append((sh.text_string, pya.DPoint(t.x, t.y)))
        it.next()
    texts = pins.pop("_texts", [])
    out = {}
    for k, (pos, u) in enumerate(shapes):
        name = min(texts, key=lambda tp: tp[1].distance(pos))[0] if texts else "pin%d" % (k + 1)
        out[name] = (pos, u)
    return out


def bend_geometry(cell, pin_layer):
    """
    A 90-degree bend cell (two pins at right angles, e.g. a PDK <waveguide type>_Bend cell):
    (entry point, travel direction in, exit point, travel direction out, corner point,
    entry->corner length, corner->exit length).  Raises RouteError if it is not such a bend.
    """
    pins = cell_pins(cell, pin_layer)
    if len(pins) != 2:
        raise RouteError("Bend cell %s needs exactly two pins (found %d)." % (cell.name, len(pins)))
    (n1, (p1, d1)), (n2, (p2, d2)) = sorted(pins.items())
    t_in, t_out = pya.DVector(-d1.x, -d1.y), d2           # travel: into the bend at pin 1, out at pin 2
    if abs(t_in.x * t_out.x + t_in.y * t_out.y) > 1e-6:
        raise RouteError("Bend cell %s is not a 90-degree bend." % cell.name)
    corner = pya.DPoint(p1.x, p2.y) if abs(t_in.x) < 0.5 else pya.DPoint(p2.x, p1.y)
    return p1, t_in, p2, t_out, corner, p1.distance(corner), corner.distance(p2)


def _unit(v):
    n = v.abs()
    return pya.DVector(v.x / n, v.y / n) if n > 0 else v


def _same(a, b):
    return abs(a.x - b.x) < 1e-6 and abs(a.y - b.y) < 1e-6


def via_center(rt, end, shift):
    """Via centre at the route's start or end, moved ``shift`` further along the route direction."""
    pts = rt.waypoints
    a, b = (pts[1], pts[0]) if end == "start" else (pts[-2], pts[-1])
    v = b - a
    n = v.abs() or 1.0
    return pya.DPoint(b.x + v.x / n * shift, b.y + v.y / n * shift)


def draw_pins(cell, rt, layer, names, width):
    """
    SiEPIC-style pins at the two route ends: a 20 dbu path on ``layer`` whose
    second point points out of the route, plus the pin name as a text.
    """
    half = 10 * cell.layout().dbu
    pts = rt.waypoints
    for name, end, prev in ((names[0], pts[0], pts[1]), (names[1], pts[-1], pts[-2])):
        angle = math.degrees(math.atan2(end.y - prev.y, end.x - prev.x))  # pointing out of the route
        t = pya.DCplxTrans(1.0, round(angle, 9), False, end.x, end.y)
        cell.shapes(layer).insert(pya.DPath([pya.DPoint(-half, 0), pya.DPoint(half, 0)], width).transformed(t))
        cell.shapes(layer).insert(pya.DText(name, pya.DTrans(int(round(angle / 90.0)) % 4, False, end.x, end.y),
                                            0.3, -1))


def draw_route(cell, rt, layers, widths, offsets, as_path=False, pin_layer=None, pin_names=("opt1", "opt2"),
               pin_width=0.0, devrec_layer=None, devrec_width=0.0, model="", cml="", waveguide_type="", bands=True):
    """
    Draw a route into ``cell``: one band per (layer index, width, offset), and optionally
    SiEPIC pins (PinRec), a DevRec outline and the compact-model texts SiEPIC reads
    (Component=, Lumerical_INTERCONNECT_library=, Spice_param:).
    """
    for li, w, off in zip(layers, widths, offsets) if bands else ():
        path = rt.component_path(w, off)
        cell.shapes(li).insert(path if as_path else path.polygon())
    if devrec_layer is not None and devrec_layer not in layers:
        dw = devrec_width if devrec_width > 0 else max(widths) + 2.0
        cell.shapes(devrec_layer).insert(rt.component_polygon(dw, 0.0))
    if pin_layer is not None:
        draw_pins(cell, rt, pin_layer, pin_names, pin_width if pin_width > 0 else widths[0])
    if devrec_layer is not None and rt.spec.is_waveguide:
        p0 = rt.waypoints[0]
        pts = str([[round(p.x, 3), round(p.y, 3)] for p in rt.waypoints]).replace(", ", ",")
        texts = []
        if cml:
            texts.append("Lumerical_INTERCONNECT_library=Design kits/%s" % cml)
        if model:
            texts.append("Component=%s" % model)
        texts.append('Spice_param:wg_length=%.9f wg_width=%.3g points="%s" radius=%.3g'
                     % (rt.length * 1e-6, widths[0] * 1e-6, pts, rt.spec.bend_radius * 1e-6))
        texts.append("Length=%.3f (microns)" % rt.length)
        if waveguide_type:
            texts.append("waveguide_type=%s" % waveguide_type)
        size = 0.1 * widths[0]
        for i, t in enumerate(texts):
            cell.shapes(devrec_layer).insert(pya.DText(t, pya.DTrans(p0.x, p0.y - i * 2 * size), size, -1))


def _lib(name, tech):
    if not name:
        return None
    lib = pya.Library.library_by_name(name, tech) if tech else pya.Library.library_by_name(name)
    if lib is None:
        raise RouteError("PDK library '%s' (technology '%s') is not loaded." % (name, tech))
    return lib


class RoutePCell(pya.PCellDeclarationHelper):
    def __init__(self):
        super().__init__()
        self.param("path", self.TypeShape, "Corner points (guiding path)",
                   default=pya.DPath([pya.DPoint(0, 0), pya.DPoint(20, 0)], 0.5))
        p = self.param("kind", self.TypeString, "Type", default="waveguide")
        p.add_choice("Waveguide (rounded bends)", "waveguide")
        p.add_choice("Metal (sharp corners)", "metal")
        self.param("radius", self.TypeDouble, "Bend radius [um]", default=10.0)
        p = self.param("bend", self.TypeString, "Bend", default="circular")
        p.add_choice("circular", "circular")
        p.add_choice("euler", "euler")
        self.param("layers", self.TypeList, "Layers (layer/datatype)", default=["1/0"])
        self.param("widths", self.TypeList, "Widths [um]", default=["0.5"])
        self.param("offsets", self.TypeList, "Offsets [um]", default=["0"])
        self.param("pin_layer", self.TypeString, "Pin layer (layer/datatype, empty = no pins)", default="")
        self.param("pin_width", self.TypeDouble, "Pin width [um] (0 = first width)", default=0.0)
        self.param("pin_names", self.TypeString, "Pin names", default="pin1,pin2")
        self.param("devrec_layer", self.TypeString, "DevRec layer (SiEPIC component outline, empty = none)", default="")
        self.param("devrec_width", self.TypeDouble, "DevRec width [um] (0 = widest band + 2)", default=0.0)
        self.param("model", self.TypeString, "Compact model (Component=)", default="")
        self.param("cml", self.TypeString, "Compact model library", default="")
        # vias to another metal at the route ends (metal routes, e.g. M1 -> M2)
        self.param("via_layer", self.TypeString, "Via cut layer (layer/datatype)", default="")
        self.param("via_start", self.TypeString, "Via at start: other metal layer (empty = none)", default="")
        self.param("via_end", self.TypeString, "Via at end: other metal layer (empty = none)", default="")
        self.param("via_shift_start", self.TypeDouble, "Via at start: shift into the pad [um]", default=0.0)
        self.param("via_shift_end", self.TypeDouble, "Via at end: shift into the pad [um]", default=0.0)
        # route built from PDK cells: a 90-degree bend cell at every corner, straight cells in between
        self.param("bend_cell", self.TypeString, "PDK bend cell (empty = drawn bends)", default="")
        self.param("straight_cell", self.TypeString, "PDK straight cell (with a length parameter)", default="")
        self.param("via_block", self.TypeDouble, "Via block size [um] (0 = route width, in 5 um units)", default=0.0)
        self.param("via_cut", self.TypeDouble, "Via cut size [um]", default=VIA_CUT)
        self.param("via_space", self.TypeDouble, "Via cut spacing [um]", default=VIA_PITCH - VIA_CUT)
        self.param("via_enclosure", self.TypeDouble, "Via metal enclosure around the cuts [um]", default=VIA_ENCLOSURE)
        self.param("via_excl", self.TypeString, "Via exclusion layers (layer/datatype, comma separated)", default="")
        self.param("via_excl_margin", self.TypeDouble, "Via exclusion margin [um]", default=0.0)
        # PDK waveguide cell, grating couplers and test label (all optional)
        self.param("technology", self.TypeString, "PDK technology", default="")
        self.param("pdk_lib", self.TypeString, "PDK library", default="")
        self.param("waveguide_type", self.TypeString, "PDK waveguide type (empty = layers above)", default="")
        self.param("gc_cell", self.TypeString, "Grating coupler cell (empty = none)", default="")
        p = self.param("gc_ends", self.TypeString, "Grating couplers at", default="both")
        p.add_choice("Start and end", "both")
        p.add_choice("Start only", "start")
        p.add_choice("End only", "end")
        self.param("label", self.TypeString, "Test label (opt_in_..., empty = none)", default="")
        self.param("text_layer", self.TypeString, "Text layer", default="10/0")
        self.param("gc_pin_layer", self.TypeString, "GC pin layer", default="1/10")
        self.param("gc_fiber_layer", self.TypeString, "GC fiber-target layer", default="81/0")

    def _route(self):
        widths = _floats(self.widths)
        spec = RouteSpec(kind=self.kind, width=widths[0] if widths else 0.5,
                         radius=self.radius, bend=self.bend)
        return route_through(list(self.path.each_point()), spec)

    def display_text_impl(self):
        what = self.waveguide_type or self.kind
        if self.gc_cell:
            what += "+GC"
        try:
            return "Route %s L=%.3fum" % (what, self._route().length)
        except Exception:
            return "Route %s" % what

    def coerce_parameters_impl(self):
        n = len(self.layers)
        widths, offsets = list(self.widths), list(self.offsets)
        if len(widths) < n:
            widths += [widths[-1] if widths else "0.5"] * (n - len(widths))
        if len(offsets) < n:
            offsets += ["0"] * (n - len(offsets))
        self.widths, self.offsets = widths[:n], offsets[:n]
        self.label = self.label.strip()

    def can_create_from_shape_impl(self):
        return self.shape.is_path()

    def parameters_from_shape_impl(self):
        self.path = self.shape.dpath
        self.layers = [self.layout.get_info(self.layer).to_s()]
        self.widths = ["%g" % self.shape.dpath.width]
        self.offsets = ["0"]

    def transformation_from_shape_impl(self):
        return pya.Trans()

    def _layer(self, text):
        return self.layout.layer(pya.LayerInfo.from_string(text.strip()))

    def produce_impl(self):
        if self.technology:
            _profile.use(self.technology)             # the settings of the route's technology
        rt = self._route()  # raises RouteError with a readable message if a corner does not fit
        lib = _lib(self.pdk_lib, self.technology)

        wp = rt.waypoints
        target = self.cell      # the cell holding the route (and its vias)
        # the PDK waveguide cell only accepts horizontal / vertical end segments (pin directions)
        manhattan_ends = all(abs(a.x - b.x) < 1e-9 or abs(a.y - b.y) < 1e-9
                             for a, b in ((wp[0], wp[1]), (wp[-2], wp[-1])))
        if self.bend_cell.strip() and lib is not None:
            self._from_cells(rt, lib)
        elif self.waveguide_type and lib is not None and manhattan_ends:
            decl = lib.layout().pcell_declaration("Waveguide")
            ci = self.layout.add_pcell_variant(lib, decl.id(), {
                "path": pya.DPath(wp, rt.spec.width), "waveguide_type": self.waveguide_type})
            self.cell.insert(pya.DCellInstArray(ci, pya.DTrans()))
        else:
            layers = [self._layer(l) for l in self.layers]
            pin_l = self._layer(self.pin_layer) if self.pin_layer.strip() else None
            devrec_l = self._layer(self.devrec_layer) if self.devrec_layer.strip() else None
            names = [n.strip() for n in self.pin_names.split(",")] + ["pin1", "pin2"]
            if devrec_l is not None:
                # own sub-cell, so that SiEPIC sees the waveguide as one component with
                # its own DevRec and pins, separate from the grating couplers
                target = self.layout.create_cell("Route_wg")
                self.cell.insert(pya.CellInstArray(target.cell_index(), pya.Trans()))
            draw_route(target, rt, layers, _floats(self.widths), _floats(self.offsets),
                       as_path=not rt.spec.is_waveguide, pin_layer=pin_l, pin_names=names[:2],
                       pin_width=self.pin_width, devrec_layer=devrec_l, devrec_width=self.devrec_width,
                       model=self.model, cml=self.cml, waveguide_type=self.waveguide_type)

        if self.via_start.strip() or self.via_end.strip():
            cut = self._layer(self.via_layer) if self.via_layer.strip() else None   # None: contact pad only
            own = self._layer(self.layers[0]) if self.layers else None
            for end, other, shift in (("start", self.via_start, self.via_shift_start),
                                      ("end", self.via_end, self.via_shift_end)):
                if other.strip() and own is not None:
                    if self.devrec_layer.strip():
                        # a component route (SiEPIC): its vias get a DevRec box too
                        vside = via_layout(self.via_block, rt.spec.width, None, self.via_cut,
                                           self.via_cut + self.via_space, self.via_enclosure)[0]
                        c = via_center(rt, end, shift)
                        target.shapes(self._layer(self.devrec_layer)).insert(
                            pya.DBox(c.x - vside / 2, c.y - vside / 2, c.x + vside / 2, c.y + vside / 2))
                    # in the route's own cell: with a DevRec that is the component, vias included
                    draw_via(target, via_center(rt, end, shift), [own, self._layer(other)], cut,
                             self.via_block, rt.spec.width, cut=self.via_cut,
                             pitch=self.via_cut + self.via_space, enclosure=self.via_enclosure,
                             excl_layers=[self._layer(x) for x in self.via_excl.split(",") if x.strip()],
                             excl_margin=self.via_excl_margin)

        if self.devrec_layer.strip() and target is not self.cell:
            # SiEPIC counts every DevRec shape as a component: route band + via boxes -> one shape
            dl = self._layer(self.devrec_layer)
            shapes = [sh for sh in target.shapes(dl).each() if not sh.is_text()]
            if len(shapes) > 1:
                merged = pya.Region([sh.polygon for sh in shapes]).merged()
                for sh in shapes:
                    target.shapes(dl).erase(sh)
                target.shapes(dl).insert(merged)

        if self.gc_cell and lib is not None:
            self._gcs(rt, lib)

    def _from_cells(self, rt, lib):
        """The route as PDK cells: the bend cell at each corner, the straight cell (stretched) between."""
        pin_l = self._layer(self.gc_pin_layer)
        dbu = self.layout.dbu
        bdecl = lib.layout().pcell_declaration(self.bend_cell.strip())
        if bdecl is None:
            raise RouteError("Bend cell '%s' is not in library %s." % (self.bend_cell, lib.name()))
        bci = self.layout.add_pcell_variant(lib, bdecl.id(), {})
        p_in, t_in, p_out, t_out, corner, d_in, d_out = bend_geometry(self.layout.cell(bci), pin_l)
        sdecl = lib.layout().pcell_declaration(self.straight_cell.strip()) if self.straight_cell.strip() else None
        length_param = None
        if sdecl is not None:
            names = [q.name for q in sdecl.get_parameters()]
            length_param = next((n for n in names if "length" in n.lower()), None)
            if length_param is None:
                sdecl = None
        transforms = [(r, m, pya.DTrans(r, m, 0.0, 0.0)) for m in (False, True) for r in range(4)]

        def straight(a, b):
            v = b - a
            length = round(v.abs() / dbu) * dbu
            if length < dbu / 2:
                return
            u = _unit(v)
            if sdecl is not None:
                ci = self.layout.add_pcell_variant(lib, sdecl.id(), {length_param: length})
                sp = cell_pins(self.layout.cell(ci), pin_l)
                (s1, (q1, e1)), _ = sorted(sp.items())
                travel = pya.DVector(-e1.x, -e1.y)
                r, m, t = next(x for x in transforms[:4] if _same(x[2] * travel, u))
                d = a - t * q1
                self.cell.insert(pya.DCellInstArray(ci, pya.DTrans(r, False, d.x, d.y)))
            else:   # no straight cell: draw the straight with the route's own layers
                seg = route_through([a, b], rt.spec)
                for li, w, o in zip([self._layer(l) for l in self.layers], _floats(self.widths), _floats(self.offsets)):
                    self.cell.shapes(li).insert(seg.component_polygon(w, o))

        wp = rt.waypoints
        cur = wp[0]
        for k in range(1, len(wp) - 1):
            c = wp[k]
            u, v = _unit(c - wp[k - 1]), _unit(wp[k + 1] - c)
            if _same(u, v):
                continue                                      # a straight-through point
            if min(abs(u.x), abs(u.y)) > 1e-6 or min(abs(v.x), abs(v.y)) > 1e-6:
                raise RouteError("A route of PDK bend cells needs horizontal / vertical segments only.")
            hit = next((x for x in transforms if _same(x[2] * t_in, u) and _same(x[2] * t_out, v)), None)
            if hit is None:
                raise RouteError("Bend cell %s cannot make the turn at (%.3f, %.3f)." % (self.bend_cell, c.x, c.y))
            r, m, t = hit
            d = c - t * corner
            self.cell.insert(pya.DCellInstArray(bci, pya.DTrans(r, m, d.x, d.y)))
            straight(cur, c - u * d_in)
            cur = c + v * d_out
        straight(cur, wp[-1])

    def _gcs(self, rt, lib):
        wp = rt.waypoints
        ends = []
        if self.gc_ends in ("both", "start"):
            ends.append((wp[0], wp[1]))
        if self.gc_ends in ("both", "end"):
            ends.append((wp[-1], wp[-2]))
        pin_l, fbr_l = self._layer(self.gc_pin_layer), self._layer(self.gc_fiber_layer)
        fibers = []
        for port, nxt in ends:
            v = nxt - port
            n = v.abs()
            _, fiber = gcmod.place_gc(self.cell, lib, self.gc_cell, port, (v.x / n, v.y / n), pin_l, fbr_l)
            fibers.append(fiber)
        if self.label and fibers:
            text_l = self._layer(self.text_layer)
            gcmod.add_opt_in(self.cell, text_l, fibers[0], self.label)
            if len(fibers) > 1 and gcmod.out_label(self.label):
                gcmod.add_opt_in(self.cell, text_l, fibers[1], gcmod.out_label(self.label))


class KlrouteLibrary(pya.Library):
    def __init__(self):
        self.description = "klroute: editable waveguide / metal routes"
        self.layout().register_pcell(PCELL_NAME, RoutePCell())
        self.register(LIB_NAME)


_lib_instance = None


def register():
    """Register the klroute library (idempotent)."""
    global _lib_instance
    if _lib_instance is None:
        _lib_instance = KlrouteLibrary()
    return _lib_instance


__all__ = ["register", "LIB_NAME", "PCELL_NAME", "RouteError"]
