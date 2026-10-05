"""
klroute.gc -- grating couplers at the route ends and SiEPIC-style test labels.

* place_gc():  puts a PDK grating-coupler cell so that its waveguide pin
  ("opt1") sits on a route end and points into the route.
* add_opt_in(): SiEPIC automated-measurement label
      opt_in_<pol>_<wavelength>_<type>_<deviceID>[_<params>]
  on the Text layer at the GC's fiber target (input GC only, like SiEPIC).
* the output GC gets  out_<pol>_<wavelength>_<type>_<deviceID>  (no "opt" in
  it, so SiEPIC's own tools ignore it).
* test_coordinate_rows() / format_test_coordinates(): one row per GC
  (device name,x,y,polarization,wavelength,in/out).
* measurement_labels() / output_labels() / test_coordinates_text(): test
  coordinates with the input (opt_in) and output (out_) fiber positions.
"""

import math
import re

try:
    import pya
except ImportError:  # standalone "pip install klayout"
    import klayout.db as pya

from .profile import P as _P

HEADER = "% device name,x,y,polarization,wavelength,in/out"


_GC_NAME = re.compile(r"(^|_)(sub)?gc(_|$)", re.I)


def gc_cells(lib):
    """
    Grating couplers in a PDK library: PCells named *_GC and fixed cells with 'gc' in the name
    and a fiber target (e.g. SiEPIC EBeam: ebeam_gc_te1550, GC_TE_1550_...).
    """
    if lib is None:
        return []
    ly = lib.layout()
    names = [n for n in ly.pcell_names() if re.search(r"(^|_)GC$", n.strip())]
    fbr = ly.find_layer(81, 0)
    for c in ly.each_cell():
        if c.is_pcell_variant() or c.is_proxy() or c.name in names or not _GC_NAME.search(c.name):
            continue
        if fbr is not None and c.begin_shapes_rec(fbr).at_end():
            continue                                  # no fiber target: not a GC
        if c.parent_cells() == 0:
            names.append(c.name)
    return names


def wavelength_for(gc_name):
    """Nominal test wavelength [nm] from a GC / waveguide name (O-band / 1310 / 1550 / 895 ...)."""
    n = gc_name.lower()
    if n.startswith("oband"):
        return "1310"
    for m in re.findall(r"(\d{3,4})", n):
        if 800 <= int(m) <= 2000:
            return m
    return "1550"


def polarization_for(name, default="TE"):
    n = name.lower()
    if re.search(r"(^|[^a-z])tm|tm\d", n):
        return "TM"
    if re.search(r"(^|[^a-z])te|te\d", n):
        return "TE"
    return default


def best_gc(names, hint=""):
    """The GC that fits a waveguide type best: same band / wavelength and polarization."""
    if not names:
        return None
    h = hint.lower()
    band = hint.split("_")[0].lower() if "_" in hint else ""
    wl, pol = wavelength_for(hint) if hint else "", polarization_for(hint, "")

    def score(n):
        sc = 0
        if band and n.lower().startswith(band):
            sc += 4                                   # same band prefix: <band>_..._WG -> <band>_GC
        if wl and wavelength_for(n) == wl:
            sc += 2
        if pol and polarization_for(n, "") == pol:
            sc += 1
        return sc
    return max(names, key=lambda n: (score(n), -names.index(n)))


def _gc_geometry(cell, pin_layer, fbr_layer):
    """(pin point, pin angle [deg], fiber point) of a GC cell in its own coordinates (um)."""
    pin = None
    it = cell.begin_shapes_rec(pin_layer)
    while not it.at_end():
        s = it.shape()
        if s.is_text() and s.text_string == "opt1":
            pin = s.dtext.transformed(it.dtrans())
            pin = pya.DPoint(pin.x, pin.y)
            break
        it.next()
    if pin is None:
        raise RuntimeError("Grating coupler cell %s has no 'opt1' pin label." % cell.name)
    box = pya.DBox()
    it = cell.begin_shapes_rec(fbr_layer)
    while not it.at_end():
        if not it.shape().is_text():
            box += it.shape().dbbox().transformed(it.dtrans())
        it.next()
    fiber = box.center() if not box.empty() else cell.dbbox().center()
    c = cell.dbbox().center()
    angle = math.degrees(math.atan2(pin.y - c.y, pin.x - c.x))  # pin points away from the GC body
    angle = 90.0 * round(angle / 90.0)
    return pin, angle, fiber


def place_gc(cell, lib, gc_name, port, direction, pin_layer, fbr_layer, to_cell=None, label_at=None):
    """
    Place GC ``gc_name`` with its opt1 pin on ``port`` (display coordinates).
    ``direction`` is the unit vector pointing from the port into the route.
    Returns (instance, label point in display coordinates): the fiber target, or with
    label_at="gc_origin" (SiEPIC DFT rule) the GC cell's origin, i.e. its tip.
    """
    layout = cell.layout()
    decl = lib.layout().pcell_declaration(gc_name)
    if decl is not None:
        ci = layout.add_pcell_variant(lib, decl.id(), {})
    else:                                             # a fixed library cell (SiEPIC EBeam GCs)
        ci = layout.add_lib_cell(lib, lib.layout().cell(gc_name).cell_index())
    gc = layout.cell(ci)
    pin, pin_angle, fiber = _gc_geometry(gc, pin_layer, fbr_layer)
    rot = math.degrees(math.atan2(direction[1], direction[0])) - pin_angle
    rot = round(rot, 9) % 360.0
    t = pya.DCplxTrans(1.0, rot, False, 0, 0)
    disp = port - t * pin
    t = pya.DCplxTrans(1.0, rot, False, disp.x, disp.y)
    tc = to_cell or pya.DCplxTrans()
    inst = cell.insert(pya.DCellInstArray(ci, tc * t))
    if (label_at or _P.get("label_at", "fiber")) == "gc_origin":
        return inst, t * pya.DPoint(0, 0)
    return inst, t * fiber


def clean_device_id(text):
    """SiEPIC splits labels on '_', so the device ID must not contain it."""
    return re.sub(r"[^A-Za-z0-9\-\.]+", "-", (text or "").strip()).strip("-")


def opt_in_label(pol, wavelength, device_id, kind="device", params=""):
    label = "opt_in_%s_%s_%s_%s" % (pol, wavelength, kind, clean_device_id(device_id))
    if params:
        label += "_" + params
    return label


def out_label(opt_in):
    """Output-GC label belonging to an opt_in label: opt_in_TE_1550_device_X -> out_TE_1550_device_X."""
    return "out_" + opt_in[len("opt_in_"):] if opt_in.startswith("opt_in_") else ""


def add_opt_in(cell, text_layer, fiber_point, label, to_cell=None):
    tc = to_cell or pya.DCplxTrans()
    p = tc * fiber_point
    return cell.shapes(text_layer).insert(pya.DText(label, pya.DTrans(p.x, p.y)))


def measurement_labels(top, text_layer):
    """All opt_in labels below ``top``: list of dicts (x, y in um), like SiEPIC."""
    out = []
    it = top.begin_shapes_rec(text_layer)
    while not it.at_end():
        s = it.shape()
        if s.is_text() and "opt" in s.text_string:
            label = s.text_string
            if "opt_in" not in label:
                label = label.replace("opt_", "opt_in_")
            f = label.split("_")
            while len(f) < 7:
                f.append("comment")
            t = s.dtext.transformed(it.dtrans())
            out.append({"label": label, "x": t.x, "y": t.y, "pol": f[2], "wavelength": f[3],
                        "type": f[4], "deviceID": f[5], "params": f[6:]})
        it.next()
    return out


def output_labels(top, text_layer):
    """deviceID -> list of (x, y) of the output-GC labels (out_<pol>_<wl>_<type>_<deviceID>...)."""
    outs = {}
    it = top.begin_shapes_rec(text_layer)
    while not it.at_end():
        s = it.shape()
        if s.is_text() and s.text_string.startswith("out_"):
            f = s.text_string.split("_")
            if len(f) >= 5:
                t = s.dtext.transformed(it.dtrans())
                outs.setdefault(f[4], []).append((t.x, t.y))
        it.next()
    for v in outs.values():
        v.sort(key=lambda q: (-q[1], q[0]))
    return outs


def test_coordinate_rows(top, text_layer):
    """
    One row per grating-coupler label below ``top``: dicts with device, x, y (um),
    pol, wavelength, label and role ("in" for opt_in labels, "out" for out_ labels).
    Sorted by device, then the input before the outputs (outputs top to bottom).
    """
    rows = []
    it = top.begin_shapes_rec(text_layer)
    while not it.at_end():
        s = it.shape()
        if s.is_text():
            label = s.text_string
            role = None
            if label.startswith("out_"):
                f = label.split("_")
                if len(f) >= 5:
                    role, pol, wl, dev = "out", f[1], f[2], f[4]
            elif "opt" in label:
                f = (label if "opt_in" in label else label.replace("opt_", "opt_in_")).split("_")
                if len(f) >= 6:
                    role, pol, wl, dev = "in", f[2], f[3], f[5]
            if role:
                t = s.dtext.transformed(it.dtrans())
                rows.append({"device": dev, "x": t.x, "y": t.y, "pol": pol, "wavelength": wl,
                             "label": label, "role": role})
        it.next()
    rows.sort(key=lambda r: (r["device"], r["role"] != "in", -r["y"], r["x"]))
    return rows


ELEC_HEADER = "% electrical pads: device name,x,y,pad"


def clean_pad_name(text):
    """Pad name for elec_<deviceID>_<pad> labels: no '_' (SiEPIC splits on it)."""
    return re.sub(r"[^A-Za-z0-9\-\.\+]+", "", (text or "").strip())


def elec_label(device_id, pad):
    return "elec_%s_%s" % (clean_device_id(device_id), clean_pad_name(pad))


def elec_rows(top, text_layer):
    """One row per electrical label (elec_<deviceID>_<pad>) below ``top``: device, x, y, pad, label."""
    rows = []
    it = top.begin_shapes_rec(text_layer)
    while not it.at_end():
        s = it.shape()
        if s.is_text() and s.text_string.startswith("elec_"):
            f = s.text_string.split("_", 2)
            if len(f) == 3:
                t = s.dtext.transformed(it.dtrans())
                rows.append({"device": f[1], "x": t.x, "y": t.y, "pad": f[2], "label": s.text_string})
        it.next()
    rows.sort(key=lambda r: (r["device"], -r["y"], r["x"]))
    return rows


def format_elec_rows(rows):
    lines = [ELEC_HEADER]
    for r in rows:
        lines.append("%s,%.3f,%.3f,%s" % (r["device"], r["x"], r["y"], r["pad"]))
    return "\n".join(lines) + "\n"


def format_test_coordinates(rows):
    """device name,x,y,polarization,wavelength,in/out  (plain commas, x / y in microns with 3 decimals)."""
    lines = [HEADER]
    for r in rows:
        lines.append("%s,%.3f,%.3f,%s,%s,%s" % (r["device"], r["x"], r["y"], r["pol"], r["wavelength"], r["role"]))
    return "\n".join(lines) + "\n"


def test_coordinates_text(labels, outputs=None):
    """
    Test coordinates (integer microns, truncated like SiEPIC): input = opt_in label,
    output = an out_ label with the same device ID ('-' if there is none).
    A device with several outputs gets one line per input/output pair.
    """
    outputs = outputs or {}
    lines = [HEADER]
    for d in labels:
        params = "".join(", " + p for p in d["params"])
        outs = outputs.get(d["deviceID"]) or [None]
        for o in outs:
            xo, yo = ("%d" % int(o[0]), "%d" % int(o[1])) if o else ("-", "-")
            lines.append("%d, %d, %s, %s, %s, %s, %s, %s%s" % (int(d["x"]), int(d["y"]), xo, yo, d["pol"],
                                                             d["wavelength"], d["type"], d["deviceID"], params))
    return "\n".join(lines) + "\n"


def next_device_id(labels, prefix="route"):
    used = {d["deviceID"] for d in labels}
    i = 1
    while "%s%d" % (prefix, i) in used:
        i += 1
    return "%s%d" % (prefix, i)
