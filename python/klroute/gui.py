"""
klroute.gui -- interactive "Route" tool for the KLayout GUI.

* Toolbar mode "Route": click point A, move the mouse (live preview), click
  point B -> the route is drawn.  Right click or Esc cancels.
* "Route" dock: PDK waveguide preset (from the technology's WAVEGUIDES.xml),
  or a custom waveguide / metal with its target layer, width, radius, bend,
  directions, optional cladding, and A/B coordinate fields.
* Tools > Route menu: start the tool, show the dock, or route along rulers.
"""

import math
import os
import re
import traceback

import pya

from . import autoconnect
from . import maze
from . import pins as pinmod
from . import gc as gcmod
from . import pcell
from . import userlib
from .profile import P
from . import profile
from .pdk import read_lyp_names, read_waveguides
from .router import RouteError, RouteSpec, route, route_through

CFG = P["package"] + "-"
DIR_ITEMS = ["auto", "E", "W", "N", "S"]
ICON = os.path.join(os.path.dirname(os.path.abspath(__file__)), "route_icon.png")
ICON_METAL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "route_metal_icon.png")
TOOL = P["package"] + "_tool"                # waveguide routing tool
TOOL_METAL = P["package"] + "_metal_tool"    # metal routing tool
SHORTCUT = "Ctrl+Shift+R"
SHORTCUT_METAL = "Ctrl+Shift+M"
WG, METAL = 0, 1
KIND_NAMES = {WG: "Waveguide", METAL: "Metal"}
KIND_PREFIX = {WG: "wg-", METAL: "metal-"}   # separate saved settings per window
POPUP_ERRORS = True  # show problems in a message box (not only in the status bar)
MODES = ["Path: click corners, double-click to end", "Auto-route: click A, then B",
         "Chain: click components one after another - each pair is linked",
         "Pin: click a waveguide end or contact to add a pin to the current cell",
         "Attach: click the component to move, then the one to attach it to",
         "Bridge: click two points on a metal route - the part between goes to the other metal, with vias"]
ANGLES = ["Manhattan (90\u00b0)", "45\u00b0", "Any angle"]
SHAPES = ["Editable cell (PDK Waveguide / Wire, else klroute Route)", "Path (fixed)", "Polygon (fixed)"]
GC_MODES = ["None", "Input (A) and output (B)", "Input (A) only", "Output (B) only"]
GC_AUTO = "auto (band of the waveguide)"
LABEL_ROLES = ["auto (1st GC of a device: opt_in, 2nd: out)", "input (opt_in)", "output (out_)"]
ARRAY_SIDES = ["left", "right", "top", "bottom"]
SNAP_PIXELS = 12      # a click this close (screen pixels) to a device port snaps onto it
PORT_TOL = 0.005      # um: an end point this close to a port counts as "on the port"
CUSTOM = "Custom (settings below)"
WIRE_AUTO = "auto (the PDK wire cell of the chosen layer)"
BEND_DRAWN = "none (draw the bends: circular / euler)"
BEND_AUTO = "auto (the PDK bend cell of the waveguide type)"
WIRE_NONE = "none (klroute Route cell)"
PANEL = "(layer selected in layer panel)"


def _cfg_get(name, default):
    try:
        v = pya.Application.instance().get_config(CFG + name)
    except Exception:
        v = None
    return default if v in (None, "") else v


def _cfg_set(name, value):
    try:
        pya.Application.instance().set_config(CFG + name, str(value))
    except Exception:
        pass


def _status(msg):
    mw = pya.Application.instance().main_window()
    if mw is not None:
        mw.message(msg, 8000)


def _error(msg):
    _status("Route: " + msg)
    d = _docks.get(_active_kind)
    if d is not None:
        d.info_lbl.setText("⚠ " + msg)
    if POPUP_ERRORS:
        pya.QMessageBox.warning(pya.Application.instance().main_window(), "Route", msg)


# ---------------------------------------------------------------------------
# technology / layers


def _technology(cv):
    try:
        name = cv.technology
        tech = pya.Technology.technology_by_name(name) if pya.Technology.has_technology(name) else None
    except Exception:
        tech = None
    return tech


def tech_waveguides(cv):
    """Waveguide presets of the cellview's technology (WAVEGUIDES.xml next to the .lyt)."""
    tech = _technology(cv)
    if tech is None:
        return []
    try:
        return read_waveguides(os.path.join(tech.base_path(), "WAVEGUIDES.xml"))
    except Exception:
        return []


def _tech_layer_names(cv):
    tech = _technology(cv)
    if tech is None:
        return {}
    try:
        return read_lyp_names(tech.eff_layer_properties_file())
    except Exception:
        return {}


def panel_layers(view):
    """[(label, layer, datatype)] for every layer (not group) in the layer panel."""
    out = []
    it = view.begin_layers()
    while not it.at_end():
        node = it.current()
        if not node.has_children() and node.source_layer >= 0 and node.source_datatype >= 0:
            name = node.source_name or node.name
            label = "%s  (%d/%d)" % (name, node.source_layer, node.source_datatype) if name else \
                "%d/%d" % (node.source_layer, node.source_datatype)
            out.append((label, node.source_layer, node.source_datatype))
        it.next()
    return out


def resolve_layer(view, cv, text):
    """Layer index for 'Si', 'Si  (1/0)' or '1/0'; the layer is created if needed."""
    text = (text or "").strip()
    m = re.search(r"(\d+)\s*/\s*(\d+)\s*\)?\s*$", text)
    if m:
        ld = (int(m.group(1)), int(m.group(2)))
    else:
        ld = None
        for label, l, d in panel_layers(view):
            if label.split("  (")[0] == text:
                ld = (l, d)
                break
        if ld is None:
            ld = _tech_layer_names(cv).get(text)
        if ld is None:
            raise RouteError("Unknown layer %r: not in the layer panel or the technology's .lyp." % text)
    return cv.layout().layer(pya.LayerInfo(ld[0], ld[1]))


def _panel_layer(view, cv):
    lp = view.current_layer
    if lp.is_null():
        raise RouteError("No layer selected. Click a layer in the layer panel, "
                         "or choose one in the Route panel's 'Layer' list.")
    node = lp.current()
    if node.has_children():
        raise RouteError("'%s' is a layer group. Select a single layer." % node.name)
    if node.layer_index() >= 0 and node.cellview() == cv.index():
        return node.layer_index()
    if node.source_layer < 0 or node.source_datatype < 0:
        raise RouteError("The selected layer '%s' has no layer/datatype. Choose a layer in the "
                         "Route panel's 'Layer' list." % node.name)
    return cv.layout().layer(pya.LayerInfo(node.source_layer, node.source_datatype))


# ---------------------------------------------------------------------------
# settings dock


class RouteDock(pya.QDockWidget):
    """
    One routing window.  There are two of them: "Waveguide Route" (kind WG) and "Metal Route"
    (kind METAL); each shows only its own settings, keeps its own saved values and drives its
    own tool button.
    """

    def __init__(self, parent, kind=WG):
        super().__init__("%s%s Route" % (P["prefix"], KIND_NAMES[kind]), parent)
        self.kind = kind
        self.setObjectName("%s_dock_%s" % (P["package"], "wg" if kind == WG else "metal"))
        self.presets = {}
        w = pya.QWidget(self)
        form = pya.QFormLayout(w)

        # the type is fixed per window (kept as a hidden combo: the routing code reads it)
        self.kind_cb = pya.QComboBox(w)
        self.kind_cb.addItems(["Waveguide", "Metal"])
        self.kind_cb.setCurrentIndex(kind)
        self.kind_cb.setEnabled(False)
        self.kind_cb.hide()
        self.draw_btn = pya.QPushButton("Draw %s route in the layout (%s)" % (
            KIND_NAMES[kind].lower(), SHORTCUT if kind == WG else SHORTCUT_METAL), w)
        self.draw_btn.clicked(lambda *args: start_tool(kind))
        form.addRow(self.draw_btn)

        self.mode_cb = pya.QComboBox(w)
        self.mode_cb.addItems(MODES)
        form.addRow("Click mode", self.mode_cb)
        self.angle_cb = pya.QComboBox(w)
        self.angle_cb.addItems(ANGLES)
        form.addRow("Segments", self.angle_cb)
        self.shape_cb = pya.QComboBox(w)
        self.shape_cb.addItems(SHAPES)
        form.addRow("Create as", self.shape_cb)

        self.preset_cb = pya.QComboBox(w)
        form.addRow("PDK waveguide", self.preset_cb)

        self.layer_cb = pya.QComboBox(w)
        self.layer_cb.setEditable(True)
        form.addRow("Layer", self.layer_cb)

        self.width_sb = self._spin(w, 0.001, 1000.0, 3)
        form.addRow("Width [um]", self.width_sb)
        self.radius_sb = self._spin(w, 0.0, 10000.0, 3)
        form.addRow("Bend radius [um]", self.radius_sb)
        self.bend_cb = pya.QComboBox(w)
        self.bend_cb.addItems(["circular", "euler"])
        form.addRow("Bend", self.bend_cb)
        self.bendcell_cb = pya.QComboBox(w)
        self.bendcell_cb.addItems([BEND_DRAWN, BEND_AUTO])
        form.addRow("Bend cell", self.bendcell_cb)
        self.clear_sb = self._spin(w, -1.0, 10000.0, 3)
        self.clear_sb.setSpecialValueText("auto")
        form.addRow("Detour clearance [um]", self.clear_sb)

        self.start_cb = pya.QComboBox(w)
        self.start_cb.addItems(DIR_ITEMS)
        form.addRow("Leave A towards", self.start_cb)
        self.end_cb = pya.QComboBox(w)
        self.end_cb.addItems(DIR_ITEMS)
        form.addRow("Leave B towards", self.end_cb)

        self.clad_le = pya.QComboBox(w)
        self.clad_le.setEditable(True)
        form.addRow("Cladding layer", self.clad_le)
        self.clad_sb = self._spin(w, 0.0, 1000.0, 3)
        self.clad_sb.setSpecialValueText("none")
        form.addRow("Cladding / side [um]", self.clad_sb)

        self.sipins_ck = pya.QCheckBox("SiEPIC pins (opt1/opt2) + DevRec on waveguide ends", w)
        form.addRow(self.sipins_ck)

        self.gc_mode_cb = pya.QComboBox(w)
        self.gc_mode_cb.addItems(GC_MODES)
        form.addRow("Grating couplers", self.gc_mode_cb)
        self.gc_cell_cb = pya.QComboBox(w)
        self.gc_cell_cb.addItem(GC_AUTO)
        form.addRow("GC cell", self.gc_cell_cb)
        self.label_ck = pya.QCheckBox("Test label at the GC fiber target (SiEPIC opt_in / out_)", w)
        form.addRow(self.label_ck)
        self.role_cb = pya.QComboBox(w)
        self.role_cb.addItems(LABEL_ROLES)
        form.addRow("Label", self.role_cb)
        self.portsnap_ck = pya.QCheckBox("Snap route ends to device ports (no GC on a port)", w)
        form.addRow(self.portsnap_ck)
        self.avoid_ck = pya.QCheckBox("Route around other devices (auto-route and chain)", w)
        form.addRow(self.avoid_ck)
        self.metaldev_ck = pya.QCheckBox("Metal: also avoid optical devices (off = may cross them, e.g. to a heater)", w)
        form.addRow(self.metaldev_ck)
        self.wire_cb = pya.QComboBox(w)
        self.wire_cb.addItems([WIRE_AUTO, WIRE_NONE])
        form.addRow("PDK wire cell", self.wire_cb)
        self.wire_names, self.wire_layers, self.wire_info = {}, {}, {}   # combo text -> PCell name, its metal, bands
        # kept (hidden) for the settings / scripts: checked = a PDK wire cell may be used
        self.pdkwire_ck = pya.QCheckBox(P["wire_label"], w)
        self.pdkwire_ck.hide()
        self.switch_btn = pya.QPushButton("Change metal layer here (via)  -  or Shift+click / V while drawing", w)
        self.switch_btn.clicked(lambda *args: switch_layer_now())
        form.addRow(self.switch_btn)
        self.viaset_btn = pya.QPushButton("Via settings (cut, spacing, enclosure, exclusion)...", w)
        self.viaset_btn.clicked(lambda *args: via_settings())
        form.addRow(self.viaset_btn)
        self.via_ck = pya.QCheckBox("Metal: " + P["via_label"], w)
        form.addRow(self.via_ck)
        self.viablock_sb = self._spin(w, 0.0, 1000.0, 3)
        self.viablock_sb.setSpecialValueText("auto (route width, 5 um units)")
        form.addRow("Via block [um]", self.viablock_sb)
        self.askpin_ck = pya.QCheckBox("Chain: ask ports and label for every link", w)
        form.addRow(self.askpin_ck)

        self.array_side_cb = pya.QComboBox(w)
        self.array_side_cb.addItems(ARRAY_SIDES)
        form.addRow("GC array side", self.array_side_cb)
        self.pitch_sb = self._spin(w, 1.0, 10000.0, 3)
        form.addRow("GC array pitch [um]", self.pitch_sb)
        self.rspace_sb = self._spin(w, 0.0, 1000.0, 3)
        self.rspace_sb.setSpecialValueText("auto")
        form.addRow("Route spacing [um]", self.rspace_sb)
        self.inport_le = pya.QLineEdit(w)
        self.inport_le.setPlaceholderText("opt1")
        form.addRow("Input port (opt_in)", self.inport_le)
        self.connect_btn = pya.QPushButton("Connect selected device \u2192 GC array", w)
        self.connect_btn.clicked(lambda *args: connect_selected_device())
        form.addRow(self.connect_btn)
        self.attach_btn = pya.QPushButton("Attach selected: 1st selected \u2192 2nd selected...", w)
        self.attach_btn.clicked(lambda *args: attach_selected(self.kind))
        form.addRow(self.attach_btn)
        self.pins_btn = pya.QPushButton("Add pins to selected device (or current cell)...", w)
        self.pins_btn.clicked(lambda *args: add_device_pins())
        form.addRow(self.pins_btn)
        self.devrec_btn = pya.QPushButton("Add DevRec box to selected device (or current cell)...", w)
        self.devrec_btn.clicked(lambda *args: add_devrec_box())
        form.addRow(self.devrec_btn)
        self.lib_btn = pya.QPushButton("Add selected cell to a library...", w)
        self.lib_btn.clicked(lambda *args: add_to_library())
        form.addRow(self.lib_btn)
        self.pol_cb = pya.QComboBox(w)
        self.pol_cb.addItems(["TE", "TM"])
        form.addRow("Polarization", self.pol_cb)
        self.devid_le = pya.QLineEdit(w)
        self.devid_le.setPlaceholderText("device ID (empty = route1, route2, ...)")
        form.addRow("Device ID", self.devid_le)

        self.points_le = pya.QLineEdit(w)
        self.points_le.setPlaceholderText("x1,y1; x2,y2; x3,y3 ...")
        form.addRow("Points [um]", self.points_le)
        self.ax, self.ay = self._spin(w, -1e7, 1e7, 4), self._spin(w, -1e7, 1e7, 4)
        self.bx, self.by = self._spin(w, -1e7, 1e7, 4), self._spin(w, -1e7, 1e7, 4)
        self.a_row = self._pair(w, self.ax, self.ay)
        self.b_row = self._pair(w, self.bx, self.by)
        form.addRow("A  x / y [um]", self.a_row)
        form.addRow("B  x / y [um]", self.b_row)

        self.create_btn = pya.QPushButton("Create route from these points", w)
        self.create_btn.clicked(lambda *args: self.create_from_fields())
        form.addRow(self.create_btn)

        self.info_lbl = pya.QLabel("Use the 'Route' tool or enter A/B and click the button.", w)
        self.info_lbl.setWordWrap(True)
        form.addRow(self.info_lbl)

        # hide the rows that do not belong to this window
        wg_only = [self.preset_cb, self.radius_sb, self.bend_cb, self.bendcell_cb, self.clad_le, self.clad_sb, self.sipins_ck,
                   self.gc_mode_cb, self.gc_cell_cb, self.label_ck, self.role_cb, self.array_side_cb,
                   self.pitch_sb, self.rspace_sb, self.inport_le, self.connect_btn, self.pol_cb, self.devid_le]
        metal_only = [self.metaldev_ck, self.wire_cb, self.switch_btn, self.viaset_btn, self.via_ck,
                      self.viablock_sb]
        for widget in (metal_only if kind == WG else wg_only):
            label = form.labelForField(widget)
            if label is not None:
                label.hide()
            widget.hide()
        if kind == WG:
            # slot 5: Bridge in the metal window, Label in the waveguide window
            self.mode_cb.setItemText(5, "Label: click any grating coupler to add a test label (opt_in / out_)")
        if kind == METAL:
            self.mode_cb.addItem("Label: click a pad, wire or contact to add an electrical label (elec_<ID>_<pad>)")
            self.avoid_ck.setText("Route around other devices and metal (auto-route and chain)")
            self.metaldev_ck.setText("Also avoid optical devices (off = may cross them, e.g. to a heater)")
            self.via_ck.setText(P["via_label"][0].upper() + P["via_label"][1:])
            self.askpin_ck.setText("Chain: ask the ports for every link")
            self.mode_cb.setItemText(3, "Pin: click a metal contact (or waveguide end) to add a pin to the current cell")

        self.setWidget(w)
        self.load()
        self.kind_cb.setCurrentIndex(kind)
        self.mode_cb.currentIndexChanged(lambda *args: self._update_enabled())
        self.gc_mode_cb.currentIndexChanged(lambda *args: self._update_enabled())
        self.label_ck.stateChanged(lambda *args: self._update_enabled())
        self.preset_cb.currentIndexChanged(lambda *args: self._apply_preset())
        self.wire_cb.currentIndexChanged(lambda *args: self._apply_wire())
        self.bendcell_cb.currentIndexChanged(lambda *args: self._apply_bend())
        self.pdkwire_ck.stateChanged(lambda *args: (not self.pdkwire_ck.isChecked() and self.wire_cb.currentIndex != 1)
                                     and self.wire_cb.setCurrentIndex(1))
        self._update_enabled()

    @staticmethod
    def _spin(parent, lo, hi, decimals):
        s = pya.QDoubleSpinBox(parent)
        s.setRange(lo, hi)
        s.setDecimals(decimals)
        s.setKeyboardTracking(False)
        return s

    @staticmethod
    def _pair(parent, a, b):
        box = pya.QWidget(parent)
        lay = pya.QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(a)
        lay.addWidget(b)
        return box

    def path_mode(self):
        return self.mode_cb.currentIndex == 0

    def pin_mode(self):
        return self.mode_cb.currentIndex == 3

    def chain_mode(self):
        return self.mode_cb.currentIndex == 2

    def attach_mode(self):
        return self.mode_cb.currentIndex == 4

    def bridge_mode(self):
        return self.kind == METAL and self.mode_cb.currentIndex == 5

    def label_mode(self):
        return self.mode_cb.currentIndex == (5 if self.kind == WG else 6)

    def editable(self):
        return self.shape_cb.currentIndex == 0

    def as_path(self):
        return self.shape_cb.currentIndex == 1

    def constrain(self, last, p):
        """Apply the segment angle constraint to point p relative to the previous point."""
        if last is None:
            return p
        dx, dy = p.x - last.x, p.y - last.y
        k = self.angle_cb.currentIndex
        if k == 0:
            return pya.DPoint(p.x, last.y) if abs(dx) >= abs(dy) else pya.DPoint(last.x, p.y)
        if k == 1:
            if abs(dx) > 2.414 * abs(dy):
                return pya.DPoint(p.x, last.y)
            if abs(dy) > 2.414 * abs(dx):
                return pya.DPoint(last.x, p.y)
            m = 0.5 * (abs(dx) + abs(dy))
            return pya.DPoint(last.x + (m if dx >= 0 else -m), last.y + (m if dy >= 0 else -m))
        return p

    def set_points(self, pts):
        self.points_le.setText("; ".join("%.6g,%.6g" % (p.x, p.y) for p in pts))

    def typed_points(self):
        pts = []
        for item in re.split(r"[;\n]+", self.points_le.text):
            item = item.strip().strip("()")
            if not item:
                continue
            xy = re.split(r"[,\s]+", item)
            try:
                pts.append(pya.DPoint(float(xy[0]), float(xy[1])))
            except (ValueError, IndexError):
                raise RouteError("Cannot read point %r. Write points as  x,y; x,y; ..." % item)
        return pts

    def preset(self):
        """The selected PDK WaveguideType, or None for custom / metal."""
        if self.kind_cb.currentIndex != 0:
            return None
        return self.presets.get(self.preset_cb.currentText)

    def _update_enabled(self):
        wg = self.kind_cb.currentIndex == 0
        pre = self.preset() is not None
        self.preset_cb.setEnabled(wg and len(self.presets) > 0)
        for widget in (self.layer_cb, self.width_sb):
            widget.setEnabled(not pre)
        bent = self.kind == WG and self.bend_cell() is not None
        self.radius_sb.setEnabled(wg and not bent)
        self.bend_cb.setEnabled(wg and not bent)
        gcs = wg and self.gc_mode_cb.currentIndex > 0
        self.gc_mode_cb.setEnabled(wg)
        self.gc_cell_cb.setEnabled(gcs)
        self.label_ck.setEnabled(gcs)
        self.pol_cb.setEnabled(gcs and self.label_ck.isChecked())
        self.role_cb.setEnabled(gcs and self.label_ck.isChecked())
        self.devid_le.setEnabled(gcs and self.label_ck.isChecked())
        path = self.path_mode()
        self.angle_cb.setEnabled(path)
        self.points_le.setEnabled(path)
        for widget in (self.start_cb, self.end_cb, self.a_row, self.b_row):
            widget.setEnabled(not path and not self.chain_mode() and not self.attach_mode() and not self.bridge_mode()
                              and not self.label_mode())
        for widget in (self.clad_le, self.clad_sb):
            widget.setEnabled(wg and not pre)

    def bend_cell(self):
        """(bend cell, straight cell, library) to build waveguide routes from, or None (drawn bends)."""
        if self.kind != WG or self.bendcell_cb.currentIndex <= 0:
            return None
        view = pya.LayoutView.current()
        if view is None or not view.active_cellview().is_valid():
            return None
        lib = _pdk_library(view.active_cellview())
        if lib is None:
            return None
        names = list(lib.layout().pcell_names())
        if self.bendcell_cb.currentIndex == 1:
            pre = self.preset()
            name = (pre.name + "_Bend") if pre else ""
            if name not in names:
                return None
        else:
            name = self.bendcell_cb.currentText.split("  (")[0]
            if name not in names:
                return None
        straight = name[:-len("_Bend")] + "_Straight" if name.endswith("_Bend") else ""
        return name, (straight if straight in names else ""), lib

    def _apply_bend(self):
        """A PDK bend cell fixes the bend radius: show it (and lock the radius / bend fields)."""
        bc = self.bend_cell()
        if bc is not None:
            try:
                r = pdk_bend_radius(bc[2], bc[0])
                self.radius_sb.setValue(r)
                self.info_lbl.setText("Bend cell %s: radius %g um%s" % (
                    bc[0], r, (", straights: %s" % bc[1]) if bc[1] else ", straights drawn"))
            except RouteError as e:
                self.info_lbl.setText(str(e))
        elif self.bendcell_cb.currentIndex == 1 and self.kind == WG:
            pre = self.preset()
            self.info_lbl.setText("No PDK bend cell for %s - bends are drawn" % (pre.name if pre else "Custom"))
        self._update_enabled()

    def wire_choice(self):
        """'auto', 'none' or the PDK wire PCell name chosen in 'PDK wire cell'."""
        if self.kind != METAL or self.wire_cb.currentIndex == 1 or not self.pdkwire_ck.isChecked():
            return "none"
        if self.wire_cb.currentIndex == 0:
            return "auto"
        return self.wire_names.get(self.wire_cb.currentText, "auto")

    def _apply_wire(self):
        if self.kind != METAL:
            return
        self.pdkwire_ck.setChecked(self.wire_cb.currentIndex != 1)
        info = self.wire_names.get(self.wire_cb.currentText)
        layer = getattr(self, "wire_layers", {}).get(info)
        if layer:
            # the wire cell defines the metal: select its layer
            i = next((k for k in range(self.layer_cb.count) if self.layer_cb.itemText(k).split("  (")[0] == layer), -1)
            if i >= 0:
                self.layer_cb.setCurrentIndex(i)
            else:
                self.layer_cb.setEditText(layer)
            self.info_lbl.setText("PDK wire cell %s: %s" % (info, ", ".join(self.wire_info.get(info, []))))

    def _apply_preset(self):
        if self.kind == WG and self.bend_cell() is not None:
            p = self.preset()
            if p is not None:
                self.width_sb.setValue(p.width)
            self._apply_bend()
            return
        p = self.preset()
        if p is not None:
            self.width_sb.setValue(p.width)
            self.radius_sb.setValue(p.radius)
            self.info_lbl.setText("%s: %s" % (p.name, ", ".join(
                "%s %.3g um%s" % (c.layer, c.width, (" @ %+.3g" % c.offset) if c.offset else "")
                for c in p.components)))
        self._update_enabled()

    def refresh(self, view=None):
        """Reload PDK presets and the layer list for the current view's technology."""
        view = view or pya.LayoutView.current()
        if view is None or not view.active_cellview().is_valid():
            return
        cv = view.active_cellview()
        sel_preset = self.preset_cb.currentText or self._get("preset", "")
        # no layer is preselected (waveguide and metal): the Layer box stays empty until you choose one
        # (or pick a PDK waveguide type / PDK wire cell); the choice is kept while KLayout runs
        sel_layer = self.layer_cb.currentText

        sel_gc = self.gc_cell_cb.currentText or self._get("gc_cell", GC_AUTO)
        self.gc_cell_cb.clear()
        self.gc_cell_cb.addItem(GC_AUTO)
        self.gc_cell_cb.addItems(gcmod.gc_cells(_pdk_library(cv)))
        self.gc_cell_cb.setCurrentIndex(max(0, self.gc_cell_cb.findText(sel_gc)))

        if self.kind == METAL:
            sel_wire = self.wire_cb.currentText or self._get("wire_cell", WIRE_AUTO)
            self.wire_cb.blockSignals(True)
            self.wire_cb.clear()
            self.wire_cb.addItems([WIRE_AUTO, WIRE_NONE])
            self.wire_names, self.wire_layers, self.wire_info = {}, {}, {}
            for name, layers, widths in pdk_wire_cells(_pdk_library(cv)):
                text = "%s  (%s)" % (name, " + ".join(layers))
                self.wire_names[text] = name
                self.wire_layers[name] = layers[0] if layers else ""
                self.wire_info[name] = ["%s %s um" % (l, w) for l, w in zip(layers, widths)]
                self.wire_cb.addItem(text)
            i = self.wire_cb.findText(sel_wire)
            if i < 0:   # a saved choice by name only
                i = next((k for k in range(self.wire_cb.count) if self.wire_cb.itemText(k).split("  (")[0] == sel_wire), 0)
            self.wire_cb.setCurrentIndex(i if self.pdkwire_ck.isChecked() or i == 1 else 1)
            self.wire_cb.blockSignals(False)

        if self.kind == WG:
            sel_bend = self.bendcell_cb.currentText or self._get("bend_cell", BEND_DRAWN)
            self.bendcell_cb.blockSignals(True)
            self.bendcell_cb.clear()
            self.bendcell_cb.addItems([BEND_DRAWN, BEND_AUTO])
            for name in pdk_bend_cells(_pdk_library(cv)):
                try:
                    self.bendcell_cb.addItem("%s  (radius %g um)" % (name, pdk_bend_radius(_pdk_library(cv), name)))
                except RouteError:
                    pass
            i = self.bendcell_cb.findText(sel_bend)
            if i < 0:
                i = next((k for k in range(self.bendcell_cb.count)
                          if self.bendcell_cb.itemText(k).split("  (")[0] == sel_bend.split("  (")[0]), 0)
            self.bendcell_cb.setCurrentIndex(i)
            self.bendcell_cb.blockSignals(False)

        self.presets = {p.name: p for p in tech_waveguides(cv)}
        self.preset_cb.clear()
        self.preset_cb.addItem(CUSTOM)
        self.preset_cb.addItems(list(self.presets))
        i = self.preset_cb.findText(sel_preset)
        self.preset_cb.setCurrentIndex(max(0, i))

        self.layer_cb.clear()
        self.layer_cb.addItem(PANEL)
        labels = [label for label, _, _ in panel_layers(view)]
        self.layer_cb.addItems(labels)
        i = self.layer_cb.findText(sel_layer)
        if i < 0:   # a bare layer name such as "M2_router": pick its "M2_router  (12/0)" entry
            i = next((k + 1 for k, label in enumerate(labels) if label.startswith(sel_layer + "  (")), -1)
        if not sel_layer:
            self.layer_cb.setCurrentIndex(-1)
            self.layer_cb.setEditText("")
        elif i >= 0:
            self.layer_cb.setCurrentIndex(i)
        else:
            self.layer_cb.setEditText(sel_layer)
        try:
            self.layer_cb.lineEdit.setPlaceholderText("choose a layer")
        except Exception:
            pass
        # cladding layer: every layer of the layer panel; empty = no cladding
        sel_clad = self.clad_le.currentText
        self.clad_le.clear()
        self.clad_le.addItems(labels)
        i = self.clad_le.findText(sel_clad)
        if i < 0 and sel_clad:
            i = next((k for k, label in enumerate(labels) if label.startswith(sel_clad + "  (")), -1)
        if i >= 0:
            self.clad_le.setCurrentIndex(i)
        else:
            self.clad_le.setCurrentIndex(-1)
            self.clad_le.setEditText(sel_clad)
        try:
            self.clad_le.lineEdit.setPlaceholderText("choose a layer (empty = no cladding)")
        except Exception:
            pass
        if self.kind == METAL and not sel_layer and self.wire_choice() not in ("auto", "none"):
            self._apply_wire()      # a chosen PDK wire cell brings its metal layer
        self._apply_preset()

    # -- persistence -------------------------------------------------------

    def _get(self, name, default):
        """Saved value of this window; falls back to the single shared panel of klroute <= 0.15."""
        return _cfg_get(KIND_PREFIX[self.kind] + name, _cfg_get(name, default))

    def _set(self, name, value):
        _cfg_set(KIND_PREFIX[self.kind] + name, value)

    def load(self):
        self.mode_cb.setCurrentIndex(int(self._get("mode", "0")))
        self.angle_cb.setCurrentIndex(int(self._get("angle", "0")))
        self.shape_cb.setCurrentIndex(int(self._get("output", "0")))
        self.width_sb.setValue(float(self._get("width", "0.5" if self.kind == WG else "10")))
        self.radius_sb.setValue(float(self._get("radius", "10")))
        self.bend_cb.setCurrentIndex(int(self._get("bend", "0")))
        self.clear_sb.setValue(float(self._get("clearance", "-1")))
        self.start_cb.setCurrentIndex(int(self._get("start_dir", "0")))
        self.end_cb.setCurrentIndex(int(self._get("end_dir", "0")))
        # cladding: nothing preset - empty layer and "none" width each time KLayout starts
        self.clad_le.setCurrentIndex(-1)
        self.clad_le.setEditText("")
        self.clad_sb.setValue(0.0)
        self.gc_mode_cb.setCurrentIndex(int(self._get("gc_mode", "0")))
        self.label_ck.setChecked(self._get("gc_label", "1") == "1")
        self.pol_cb.setCurrentIndex(int(self._get("gc_pol", "0")))
        self.sipins_ck.setChecked(self._get("sipins", "1") == "1")
        self.role_cb.setCurrentIndex(int(self._get("label_role", "0")))
        self.portsnap_ck.setChecked(self._get("portsnap", "1") == "1")
        self.askpin_ck.setChecked(self._get("askpin", "1") == "1")
        self.avoid_ck.setChecked(self._get("avoid", "1") == "1")
        self.metaldev_ck.setChecked(self._get("metal_avoid_devices", "1") == "1")
        self.via_ck.setChecked(self._get("auto_via", "1") == "1")
        self.pdkwire_ck.setChecked(self._get("pdk_wire", "1") == "1")
        if not self.pdkwire_ck.isChecked():
            self.wire_cb.setCurrentIndex(1)
        self.viablock_sb.setValue(float(self._get("via_block", "0")))
        self.array_side_cb.setCurrentIndex(int(self._get("array_side", "0")))
        self.pitch_sb.setValue(float(self._get("array_pitch", "127")))
        self.rspace_sb.setValue(float(self._get("route_spacing", "0")))
        self.inport_le.setText(self._get("input_port", ""))

    def save(self):
        self._set("mode", self.mode_cb.currentIndex)
        self._set("angle", self.angle_cb.currentIndex)
        self._set("output", self.shape_cb.currentIndex)
        self._set("preset", self.preset_cb.currentText)
        self._set("layer", self.layer_cb.currentText)
        self._set("width", self.width_sb.value)
        self._set("radius", self.radius_sb.value)
        self._set("bend", self.bend_cb.currentIndex)
        self._set("clearance", self.clear_sb.value)
        self._set("start_dir", self.start_cb.currentIndex)
        self._set("end_dir", self.end_cb.currentIndex)
        self._set("clad_layer", self.clad_le.currentText)
        self._set("clad_width", self.clad_sb.value)
        self._set("gc_mode", self.gc_mode_cb.currentIndex)
        self._set("gc_cell", self.gc_cell_cb.currentText)
        self._set("gc_label", "1" if self.label_ck.isChecked() else "0")
        self._set("gc_pol", self.pol_cb.currentIndex)
        self._set("sipins", "1" if self.sipins_ck.isChecked() else "0")
        self._set("label_role", self.role_cb.currentIndex)
        self._set("portsnap", "1" if self.portsnap_ck.isChecked() else "0")
        self._set("askpin", "1" if self.askpin_ck.isChecked() else "0")
        self._set("avoid", "1" if self.avoid_ck.isChecked() else "0")
        self._set("metal_avoid_devices", "1" if self.metaldev_ck.isChecked() else "0")
        self._set("auto_via", "1" if self.via_ck.isChecked() else "0")
        self._set("pdk_wire", "1" if self.pdkwire_ck.isChecked() else "0")
        self._set("wire_cell", self.wire_cb.currentText)
        self._set("bend_cell", self.bendcell_cb.currentText)
        self._set("via_block", self.viablock_sb.value)
        self._set("array_side", self.array_side_cb.currentIndex)
        self._set("array_pitch", self.pitch_sb.value)
        self._set("route_spacing", self.rspace_sb.value)
        self._set("input_port", self.inport_le.text)

    # -- settings ------------------------------------------------------------

    def spec(self):
        wg = self.kind_cb.currentIndex == 0
        clr = self.clear_sb.value
        pre = self.preset()
        bc = self.bend_cell() if wg else None
        radius = self.radius_sb.value
        if bc is not None:
            try:
                radius = pdk_bend_radius(bc[2], bc[0])
            except RouteError:
                pass
        return RouteSpec(
            kind="waveguide" if wg else "metal",
            width=pre.width if pre else self.width_sb.value,
            radius=radius,
            bend=self.bend_cb.currentText,
            clearance=None if clr < 0 else clr,
            cladding=self.clad_sb.value if (wg and not pre and self.clad_le.currentText.strip()) else 0.0,
        )

    def directions(self):
        return self.start_cb.currentText, self.end_cb.currentText

    def set_a(self, p):
        self.ax.setValue(p.x)
        self.ay.setValue(p.y)

    def set_b(self, p):
        self.bx.setValue(p.x)
        self.by.setValue(p.y)

    def create_from_fields(self):
        view = pya.LayoutView.current()
        if view is None:
            _error("Open or create a layout first (File > New Layout).")
            return
        if self.path_mode():
            try:
                pts = self.typed_points()
            except RouteError as e:
                _error(str(e))
                return
        else:
            pts = [pya.DPoint(self.ax.value, self.ay.value), pya.DPoint(self.bx.value, self.by.value)]
        create_route(view, pts, self)


_docks = {}
_active_kind = WG


def dock(view=None, kind=None):
    """
    The routing window of ``kind`` (WG / METAL); without ``kind`` the one in use - the window
    whose tool was started last.  Windows are created on first use.
    Also switches the PDK settings to the technology of the layout in use.
    """
    use_profile(view)
    if kind is None:
        kind = _active_kind
    if kind not in _docks:
        mw = pya.Application.instance().main_window()
        d = RouteDock(mw, kind)
        mw.addDockWidget(pya.Qt_DockWidgetArea.RightDockWidgetArea, d)
        other = _docks.get(WG if kind == METAL else METAL)
        if other is not None:
            mw.tabifyDockWidget(other, d)   # both windows share the right side as tabs
        _docks[kind] = d
        d.refresh(view)
    return _docks[kind]


def set_active(kind):
    global _active_kind
    _active_kind = kind


def show_window(kind):
    set_active(kind)
    d = dock(None, kind)
    d.refresh()
    d.show()
    d.raise_()
    return d


def start_tool(kind=WG):
    """Start the waveguide or metal routing tool and show its window."""
    view = pya.LayoutView.current()
    if view is None:
        _error("Open or create a layout first (File > New Layout).")
        return
    set_active(kind)
    show_window(kind)
    view.switch_mode(TOOL if kind == WG else TOOL_METAL)


# ---------------------------------------------------------------------------
# creating shapes


def _cellview(view):
    cv = view.active_cellview()
    if not cv.is_valid() or cv.cell is None:
        raise RouteError("No cell to draw into. Open a layout and select a top cell.")
    try:
        to_cell = pya.DCplxTrans(cv.context_dtrans().inverted())
    except Exception:
        to_cell = pya.DCplxTrans()
    return cv, to_cell


def _dir_letter(u):
    if abs(u[0]) >= abs(u[1]):
        return "E" if u[0] > 0 else "W"
    return "N" if u[1] > 0 else "S"


def align_to_ports(points, sp, ep, lead):
    """
    Path mode: make the route leave a start port / arrive at an end port along the
    port direction, by inserting a corner on the port's axis where needed.
    """
    pts = list(points)

    def fix(q, u, nxt):
        dx, dy = nxt.x - q.x, nxt.y - q.y
        t = dx * u[0] + dy * u[1]
        perp = abs(-dx * u[1] + dy * u[0])
        if perp < 1e-6 and t > 0:
            return None  # already straight out of the port
        t = max(t, lead)
        return pya.DPoint(q.x + u[0] * t, q.y + u[1] * t)

    if sp is not None and len(pts) >= 2:
        c = fix(pts[0], sp["dir"], pts[1])
        if c is not None:
            pts.insert(1, c)
    if ep is not None and len(pts) >= 2:
        c = fix(pts[-1], ep["dir"], pts[-2])
        if c is not None:
            pts.insert(len(pts) - 1, c)
    # drop clicked corners that the new port corners made redundant (path doubling back on itself)
    i = 1
    while i < len(pts) - 1:
        a, b, c = pts[i - 1], pts[i], pts[i + 1]
        u = (b.x - a.x, b.y - a.y)
        v = (c.x - b.x, c.y - b.y)
        cross = u[0] * v[1] - u[1] * v[0]
        dot = u[0] * v[0] + u[1] * v[1]
        if (a - b).abs() < 1e-9 or (abs(cross) < 1e-9 and dot < 0):
            del pts[i]
            i = max(1, i - 1)
        else:
            i += 1
    return pts


_DFT_CACHE = {}


def _tech_xml(cv, name):
    tech = _technology(cv)
    if tech is None:
        return None
    path = os.path.join(tech.base_path(), name)
    return path if os.path.isfile(path) else None


def dft_rules(cv):
    """
    SiEPIC design-for-test rules of the technology (DFT.xml next to the .lyt), or None:
    {"orient": {GC cell: degrees}, "pitch", "above", "below", "max_label"}.
    """
    path = _tech_xml(cv, "DFT.xml")
    if path is None:
        return None
    if path not in _DFT_CACHE:
        import xml.etree.ElementTree as ET
        try:
            root = ET.parse(path).getroot()
            gcs = root.find("grating-couplers")
            num = lambda node, tag, dflt: float(node.findtext(tag)) if node is not None and node.findtext(tag) else dflt
            orient = {}
            if gcs is not None and gcs.find("gc-orientation") is not None:
                for e in gcs.find("gc-orientation"):
                    try:
                        orient[e.tag] = float(e.text)
                    except (TypeError, ValueError):
                        pass
            _DFT_CACHE[path] = {"orient": orient, "pitch": num(gcs, "gc-pitch", 127.0),
                                "above": num(gcs, "detectors-above-laser", 1), "below": num(gcs, "detectors-below-laser", 2),
                                "max_label": num(root.find("opt_in"), "max-distance-to-grating-coupler", 10.0)}
        except Exception:
            _DFT_CACHE[path] = None
    return _DFT_CACHE[path]


def device_only_layers(cv):
    """Layers that SiEPIC verification wants inside a component (Verification.xml), as 'l/d' strings."""
    path = _tech_xml(cv, "Verification.xml")
    if path is None:
        return set()
    try:
        text = open(path, encoding="utf-8").read()
        m = re.search(r"<deviceonly-layers>([^<]*)</deviceonly-layers>", text)
        return {"%s/%s" % (a, b) for a, b in re.findall(r"\[\s*(\d+)\s*,\s*(\d+)\s*\]", m.group(1))} if m else set()
    except Exception:
        return set()


_ORIENT_DIR = {0: "E", 90: "N", 180: "W", 270: "S"}


def gc_end_dirs(view, d, sp=None, ep=None):
    """
    (start, end) directions forced at the route ends that will get a grating coupler, so the GC
    gets the orientation the technology's DFT rules ask for (SiEPIC EBeam: 0 deg = route leaves
    the GC towards +x).  None where nothing is forced.
    """
    if view is None or not d.spec().is_waveguide or d.gc_mode_cb.currentIndex == 0:
        return None, None
    cv = view.active_cellview()
    dft = dft_rules(cv)
    if not dft or not dft["orient"]:
        return None, None
    try:
        gc_name = _choose_gc(d, _pdk_library(cv), d.preset())
    except RouteError:
        return None, None
    deg = dft["orient"].get(gc_name)
    if deg is None:
        return None, None
    letter = _ORIENT_DIR.get(int(round(deg)) % 360)
    mode = d.gc_mode_cb.currentIndex
    return (letter if mode in (1, 2) and sp is None else None,
            letter if mode in (1, 3) and ep is None else None)


def compute(d, points, sp=None, ep=None, view=None):
    """
    Path mode: route through all points.  Auto mode: auto-route first -> last point.
    ``sp`` / ``ep``: device ports at the first / last point (route leaves / arrives along them).
    ``view``: also apply the DFT grating-coupler orientation (SiEPIC) at ends that get a GC.
    """
    spec = d.spec()
    if d.path_mode():
        lead = max(2.0 * spec.bend_radius, spec.min_clearance, 2.0 * spec.width)
        return route_through(align_to_ports(points, sp, ep, lead), spec)
    sd, ed = d.directions()
    gs, ge = gc_end_dirs(view, d, sp, ep)
    sd, ed = gs or sd, ge or ed
    if sp is not None:
        sd = _dir_letter(sp["dir"])
    if ep is not None:
        ed = _dir_letter(ep["dir"])
    return route(points[0], points[-1], spec, sd, ed)


# ---------------------------------------------------------------------------
# device ports (SiEPIC optical pins: a 2-point PinRec path + a text with the pin name)


def find_ports(view, cv, box=None, metal=False):
    """
    Ports below the current cell, in display coordinates: dicts pos, dir, name, device, origin.
    Optical pins are on PinRec, electrical pins (metal=True) on PinRecM - both a 2-point path
    pointing out of the device plus a text with the pin name.
    """
    layout = cv.layout()
    pin_l = _layer_or(view, cv, "PinRecM", "1/11") if metal else _layer_or(view, cv, "PinRec", "1/10")
    try:
        ctx = pya.DCplxTrans(cv.context_dtrans())
    except Exception:
        ctx = pya.DCplxTrans()
    if box is not None:
        it = cv.cell.begin_shapes_rec_touching(pin_l, box.transformed(ctx.inverted()))
    else:
        it = cv.cell.begin_shapes_rec(pin_l)
    ports = []
    while not it.at_end():
        sh = it.shape()
        if sh.is_path() and sh.path.num_points() == 2:
            t = ctx * it.dtrans()
            a, b = [t * q for q in sh.dpath.each_point()]
            v = b - a
            n = v.abs()
            if n > 0:
                name = ""
                for tx in it.cell().shapes(pin_l).each_touching(sh.bbox()):
                    if tx.is_text():
                        name = tx.text_string
                ports.append({"pos": pya.DPoint((a.x + b.x) / 2, (a.y + b.y) / 2), "dir": (v.x / n, v.y / n),
                              "name": name, "device": it.cell().basic_name().strip(),
                              "origin": t * pya.DPoint(0, 0)})
        it.next()
    return ports


def port_near(view, cv, p, radius):
    best = None
    for q in find_ports(view, cv, pya.DBox(p.x - radius, p.y - radius, p.x + radius, p.y + radius)):
        dist = q["pos"].distance(p)
        if dist <= radius and (best is None or dist < best[0]):
            best = (dist, q)
    return best[1] if best else None


def _end_ports(view, d, points, force=False):
    """Ports at the first and last point (or None), if port snapping is on (or ``force``)."""
    if not (force or d.portsnap_ck.isChecked()) or len(points) < 2:
        return None, None
    cv = view.active_cellview()
    return port_near(view, cv, points[0], PORT_TOL), port_near(view, cv, points[-1], PORT_TOL)


def _layer_name(view, cv, li):
    """Name of a layer (e.g. 'M2_router') from the layer panel or the technology's .lyp, or ''."""
    info = cv.layout().get_info(li)
    for label, l, d in panel_layers(view):
        if (l, d) == (info.layer, info.datatype) and "  (" in label:
            return label.split("  (")[0]
    for name, ld in _tech_layer_names(cv).items():
        if ld == (info.layer, info.datatype):
            return name
    return info.name or ""


def _pdk_library(cv):
    """The technology's PCell library that has a SiEPIC-style 'Waveguide' PCell, or None."""
    tech = cv.technology
    if not tech:
        return None
    for name in pya.Library.library_names():
        try:
            lib = pya.Library.library_by_name(name, tech)
        except Exception:
            continue
        if lib is None or not lib.is_for_technology(tech):
            continue
        decl = lib.layout().pcell_declaration("Waveguide")
        if decl is not None and "waveguide_type" in [q.name for q in decl.get_parameters()]:
            return lib
    return None


def ensure_wire_aliases():
    """
    Some PDKs register PCells with a leading space in the name (e.g. ' Wire_X').
    KLayout strips that space when it reads a saved layout back, so it cannot find the PCell
    and the cells become fixed.  Register the same PCells once more under the clean
    names ('Wire_X', ...) - new wires are placed with those - and refresh the library, which
    also turns wires in already opened layouts back into editable PCells.
    Returns the names added.
    """
    added = []
    try:
        names = pya.Library.library_names()
    except Exception:
        return added
    seen = set()
    for lname in names:
        for tech in [None] + [t for t in pya.Technology.technology_names()]:
            try:
                lib = pya.Library.library_by_name(lname, tech) if tech else pya.Library.library_by_name(lname)
            except Exception:
                lib = None
            if lib is None or lib.id() in seen:
                continue
            seen.add(lib.id())
            ly = lib.layout()
            new = False
            for n in list(ly.pcell_names()):
                clean = n.strip()
                if clean != n and clean and ly.pcell_declaration(clean) is None:
                    try:
                        ly.register_pcell(clean, type(ly.pcell_declaration(n))())
                        added.append("%s.%s" % (lname, clean))
                        new = True
                    except Exception:
                        pass
            if new:
                try:
                    lib.refresh()
                except Exception:
                    pass
    return added


_BEND_R = {}


def pdk_bend_cells(lib):
    """Names of the 90-degree bend PCells of a PDK library (e.g. <waveguide type>_Bend)."""
    if lib is None:
        return []
    return sorted(n.strip() for n in lib.layout().pcell_names() if "bend" in n.lower() and n == n.strip())


def pdk_bend_radius(lib, name):
    """Effective bend radius of a PDK bend cell (entry pin to corner), from its pins."""
    key = (lib.name(), name)
    if key not in _BEND_R:
        decl = lib.layout().pcell_declaration(name)
        if decl is None:
            raise RouteError("Bend cell %s not found." % name)
        ly = pya.Layout()
        try:
            ly.technology_name = lib.layout().technology_name
        except Exception:
            pass
        c = ly.cell(ly.add_pcell_variant(lib, decl.id(), {}))
        g = pcell.bend_geometry(c, ly.layer(1, 10))
        if abs(g[5] - g[6]) > 1e-6:
            raise RouteError("Bend cell %s is not symmetric (%g / %g um)." % (name, g[5], g[6]))
        _BEND_R[key] = g[5]
    return _BEND_R[key]


def _bend_params(view, cv, d):
    """Route PCell parameters to build the route from PDK bend / straight cells ({} for drawn bends)."""
    bc = d.bend_cell() if d.kind == WG else None
    if bc is None:
        return {}
    return {"bend_cell": bc[0], "straight_cell": bc[1], "pdk_lib": bc[2].name(), "technology": cv.technology,
            "gc_pin_layer": cv.layout().get_info(_layer_or(view, cv, "PinRec", "1/10")).to_s()}


def pdk_wire_cells(lib):
    """[(name, default layers, default widths)] of the wire PCells (path + layers + widths) of a PDK library."""
    if lib is None:
        return []
    ensure_wire_aliases()
    out, seen = [], set()
    for name in sorted(lib.layout().pcell_names(), key=lambda n: (n != n.strip(), n)):
        clean = name.strip()
        if clean in seen:
            continue                  # ' Wire_X' and its alias 'Wire_X': list once
        params = {q.name: q for q in lib.layout().pcell_declaration(name).get_parameters()}
        if "path" in params and "layers" in params and "widths" in params:
            seen.add(clean)
            out.append((clean, [str(x).strip() for x in (params["layers"].default or [])],
                        [str(x) for x in (params["widths"].default or [])]))
    return out


def _chosen_wire(d, lib, layer_name):
    """
    The PDK wire cell to draw a metal route on ``layer_name`` with, from the window's 'PDK wire cell'
    choice: (name, decl) or None.  A chosen cell is used when its metal is the route's layer
    (a layer change to another metal falls back to that metal's own wire cell).
    """
    choice = d.wire_choice()
    if choice == "none" or lib is None:
        return None
    if choice != "auto":
        decl = lib.layout().pcell_declaration(choice)
        if decl is not None:
            layers = {q.name: q for q in decl.get_parameters()}["layers"].default or []
            if layers and str(layers[0]).strip() == layer_name:
                return choice, decl
    return _pdk_wire_cell(lib, layer_name)


def _pdk_wire_cell(lib, layer_name):
    """PDK metal-routing PCell (e.g. 'Wireguide') whose first layer is ``layer_name``."""
    if lib is None or not layer_name:
        return None
    ensure_wire_aliases()
    names = sorted(lib.layout().pcell_names(), key=lambda n: n != n.strip())   # clean names first
    for name in names:
        decl = lib.layout().pcell_declaration(name)
        params = {q.name: q for q in decl.get_parameters()}
        if "path" in params and "layers" in params and "widths" in params:
            layers = params["layers"].default or []
            if layers and str(layers[0]).strip() == layer_name:
                return name, decl
    return None


def _siepic_layers(view, cv, d):
    """Layer indexes for SiEPIC pins / DevRec (None if switched off or not in the technology)."""
    if not d.sipins_ck.isChecked():
        return None
    try:
        res = {"pin": resolve_layer(view, cv, "PinRec"), "devrec": resolve_layer(view, cv, "DevRec")}
    except RouteError:
        return None
    try:
        res["waveguide"] = resolve_layer(view, cv, "Waveguide")
    except RouteError:
        res["waveguide"] = None
    return res


def _siepic_params(view, cv, d, pre):
    """Route PCell parameters for SiEPIC pins, DevRec and compact model (empty dict if off)."""
    sp = _siepic_layers(view, cv, d)
    if sp is None:
        return {}
    info = cv.layout().get_info
    return {"pin_layer": info(sp["pin"]).to_s(), "pin_names": "opt1,opt2",
            "devrec_layer": info(sp["devrec"]).to_s(),
            "model": pre.model if pre else "", "cml": pre.cml if pre else ""}


def _on_grid(dpath, dbu):
    """Corner points snapped to the database grid, so that collinear corners compare exactly equal
    (SiEPIC's Manhattan check uses exact comparison)."""
    return dpath.to_itype(dbu).to_dtype(dbu)


def _place_pcell(cv, lib, decl, params):
    if lib.name() == pcell.LIB_NAME and not params.get("technology"):
        params = dict(params, technology=cv.technology)    # the route remembers its technology (profile)
    ci = cv.layout().add_pcell_variant(lib, decl.id(), params)
    return cv.cell.insert(pya.DCellInstArray(ci, pya.DTrans()))


def _band_width(d, spec):
    """Widest band of the route (its DevRec for PDK types), used for the clearance to other devices."""
    pre = d.preset()
    if pre is not None and pre.components:
        return max(c.width + 2 * abs(c.offset) for c in pre.components)
    return spec.width + 2.0


def obstacle_region(view, cv, d, spec, ends, layers=None):
    """
    Region (display coordinates, dbu units) of everything a new route must stay away from,
    grown by the clearance.  Returns (region, clearance).

    Waveguide: DevRec shapes of all instances of the current cell (bbox for cells without
               DevRec); clearance = half the route's widest band + 0.5 um.
    Metal:     all shapes on the route's own metal ``layers`` (except the pad / wire touching an
               end point - that is the same net), plus the DevRec shapes of devices if
               'Metal: also avoid optical devices' is on; clearance = half width + spacing
               (spacing = Detour clearance, or the width on 'auto').
    ``ends``: [(pos, dir)] of the route ends - a window is cut out in front of each.
    """
    layout = cv.layout()
    dbu = layout.dbu
    devrec = _layer_or(view, cv, "DevRec", "68/0")
    try:
        ctx = pya.DCplxTrans(cv.context_dtrans())
    except Exception:
        ctx = pya.DCplxTrans()
    ictx = ctx.to_itrans(dbu)   # display <- cell, in database units
    reg = pya.Region()
    if spec.is_waveguide:
        for inst in cv.cell.each_inst():
            sub = pya.Region(inst.cell.begin_shapes_rec(devrec))
            if sub.is_empty():
                reg.insert(inst.bbox())
            else:
                reg += sub.transformed(inst.cplx_trans)
        clearance = _band_width(d, spec) / 2.0 + 0.5
    else:
        spacing = d.clear_sb.value if d.clear_sb.value > 0 else spec.width
        clearance = spec.width / 2.0 + spacing
        metal = pya.Region()
        for li in layers or []:
            metal += pya.Region(cv.cell.begin_shapes_rec(li))
        metal.merge()
        # the pad / wire the route starts or ends on belongs to the same net: not an obstacle
        here = pya.Region()
        for pos, _ in ends:
            if pos is not None:
                here.insert(pya.DBox(pos, pos).enlarged(pya.DVector(0.01, 0.01)).transformed(ctx.inverted())
                            .to_itype(dbu))
        reg += metal.not_interacting(here)
        if d.metaldev_ck.isChecked():
            reg += pya.Region(cv.cell.begin_shapes_rec(devrec)).not_interacting(here)
    reg = reg.transformed(ictx).merged().sized(int(round(clearance / dbu)))
    base = max(spec.bend_radius, spec.min_clearance / 2.0, spec.width, 1.0)
    leads = []
    raw = reg.dup()
    for pos, u in ends:
        if pos is None:
            leads.append(base)
            continue
        # a pin inside its device: go straight out (along the pin) until clear of the obstacles
        lead = base
        while lead < 2000.0:
            q = pya.DPoint(pos.x + u[0] * lead, pos.y + u[1] * lead)
            if raw.interacting(pya.Region(pya.DBox(q, q).enlarged(pya.DVector(0.01, 0.01)).to_itype(dbu))).is_empty():
                break
            lead += 0.5
        leads.append(lead)
        half = clearance + spec.width
        far = pya.DPoint(pos.x + u[0] * (lead + clearance), pos.y + u[1] * (lead + clearance))
        box = pya.DBox(pos, far).enlarged(pya.DVector(half if abs(u[1]) > 0.5 else 0, half if abs(u[0]) > 0.5 else 0))
        reg -= pya.Region(box.to_itype(dbu))
    obstacle_region.leads = leads
    return reg, clearance


def avoid_points(view, cv, d, spec, pts, start, end, layers=None):
    """
    If the route through ``pts`` touches another device, replace it by a route around them.
    start / end: (pos, unit dir out of the end point into the route).
    Returns (points, rerouted?).
    """
    if not d.avoid_ck.isChecked():
        return pts, False
    dbu = cv.layout().dbu
    obst, clearance = obstacle_region(view, cv, d, spec, [start, end], layers)
    if obst.is_empty():
        return pts, False
    try:
        line = route_through(pts, spec)
    except RouteError:
        line = None
    if line is not None:
        core = pya.Region(pya.DPath(line.centerline, spec.width).polygon().to_itype(dbu))
        if (core & obst).is_empty():
            return pts, False
    polys = [q.to_dtype(dbu) for q in obst.each()]
    leads = getattr(obstacle_region, "leads", [0.0, 0.0])
    corners = maze.route_around((start[0].x, start[0].y), start[1], (end[0].x, end[0].y), end[1],
                                spec, polys, clearance, lead0=leads[0], lead1=leads[1])
    return [pya.DPoint(x, y) for x, y in corners], True


# metal layer -> (the other metal it can connect to, the via layer between them)
# metals a route / bridge can be put on, and the cut layer between two of them (from the profile
# of the technology in use - updated whenever it changes)
VIA_PAIRS, METALS, VIA_CUTS = {}, [], {}


def _profile_changed():
    VIA_PAIRS.clear()
    VIA_PAIRS.update(P["via_pairs"])
    METALS[:] = list(P["metals"])
    VIA_CUTS.clear()
    VIA_CUTS.update({frozenset(k): v for k, v in P["via_cuts"].items()})


profile.on_change(_profile_changed)


def use_profile(view=None):
    """Switch the PDK settings to the technology of the layout in ``view`` (or the current one)."""
    view = view or pya.LayoutView.current()
    try:
        cv = view.active_cellview() if view is not None else None
        tech = cv.technology if cv is not None and cv.is_valid() else None
    except Exception:
        tech = None
    profile.use(tech)


def cut_for(a, b):
    """Cut (via) layer between metals a and b; '' = direct contact (no cut); None = not known."""
    return VIA_CUTS.get(frozenset((a, b)))


def _cut_text(c):
    return "?" if c is None else (c or "contact, no cut")


def tech_metals(view, cv):
    """The METALS that exist in the technology / layer panel."""
    names = set(_tech_layer_names(cv)) | {label.split("  (")[0] for label, _, _ in panel_layers(view)}
    return [m for m in METALS if m in names] or list(METALS[:2])


def via_spec_saved(d=None, cut_layer=""):
    """The last via settings (cut layer, block, cut size, spacing, enclosure, exclusion)."""
    return {"layer": _cfg_get("via_cut_layer", cut_layer or P["default_cut"]),
            "block": d.viablock_sb.value if d is not None else float(_cfg_get("via_block_last", "0")),
            "cut": float(_cfg_get("via_cut", str(pcell.via_default("cut")))),
            "space": float(_cfg_get("via_space", str(pcell.via_default("pitch") - pcell.via_default("cut")))),
            "enclosure": float(_cfg_get("via_enclosure", str(pcell.via_default("enclosure")))),
            "excl": _cfg_get("via_excl", ""),
            "excl_margin": float(_cfg_get("via_excl_margin", "0"))}


def _save_via_spec(spec):
    if not spec.get("metal"):
        _cfg_set("via_cut_layer", spec["layer"])
    _cfg_set("via_block_last", spec["block"])
    for k in ("cut", "space", "enclosure", "excl", "excl_margin"):
        _cfg_set("via_" + k, spec[k])


def via_summary(spec, width):
    side, n, m, unit = pcell.via_layout(spec["block"], width, None, spec["cut"],
                                        spec["cut"] + spec["space"], spec["enclosure"])
    txt = "Block %g x %g um: %d x %d tile(s) of %g um, %d x %d cuts each (%d cuts of %g um)" % (
        side, side, n, n, unit, m, m, (n * m) ** 2, spec["cut"])
    if spec["excl"].strip():
        txt += "; exclusion %g x %g um on %s" % (side + 2 * spec["excl_margin"], side + 2 * spec["excl_margin"],
                                                spec["excl"])
    return txt


def ask_via(d, width, where="", metals=None, current="", label="Continue on metal"):
    """
    Dialog before a via is added: (optionally) the metal to go to, cut layer, block size,
    cut size, cut spacing, metal enclosure, exclusion layers and margin.
    ``metals`` / ``current``: offer the metals (other than ``current``) to change to; the
    choice is returned as spec["metal"].
    Returns the via spec, or None (cancelled).  Skipped (saved values used) when 'Ask every
    time' was unticked.
    """
    spec = via_spec_saved(d)
    choices = [m for m in (metals or []) if m != current]
    default_metal = ""
    if choices:
        last = _cfg_get("via_next_" + current, "")
        default_metal = last if last in choices else \
            (VIA_PAIRS[current][0] if current in VIA_PAIRS and VIA_PAIRS[current][0] in choices else choices[0])
        spec["metal"] = default_metal
        c = cut_for(current, default_metal)
        if c is not None:
            spec["layer"] = c
    if _cfg_get("via_ask", "1") != "1":
        return spec
    mw = pya.Application.instance().main_window()
    dlg = pya.QDialog(mw)
    dlg.setWindowTitle("Via")
    form = pya.QFormLayout(dlg)
    if where:
        form.addRow(pya.QLabel("Via %s, route width %g um" % (where, width), dlg))
    metal_cb = None
    if choices:
        metal_cb = pya.QComboBox(dlg)
        metal_cb.setEditable(True)
        metal_cb.addItems(choices)
        metal_cb.setCurrentIndex(choices.index(default_metal))
        form.addRow("%s (now %s)" % (label, current or "?"), metal_cb)

    def spin(lo, hi, val, special=None):
        sb = pya.QDoubleSpinBox(dlg)
        sb.setRange(lo, hi)
        sb.setDecimals(3)
        sb.setValue(val)
        if special:
            sb.setSpecialValueText(special)
        return sb

    # the via layer is chosen from the layers that exist (nothing preselected)
    view = pya.LayoutView.current()
    layer_items = [label for label, _, _ in panel_layers(view)] if view is not None else []
    layer_value = lambda label: label.split("  (")[0] if "  (" in label else label
    NO_CUT = "none (contact pad only, no cut layer)"
    cut_items = ([NO_CUT] if P["default_cut"] == "" else []) + layer_items
    layer_le = pya.QComboBox(dlg)
    layer_le.addItems(cut_items)
    layer_le.setCurrentIndex(-1)
    form.addRow("Via layer", layer_le)
    block_sb = spin(0.0, 10000.0, spec["block"], "auto (route width, 5 um tiles)")
    form.addRow("Via block / landing size [um]", block_sb)
    cut_sb = spin(0.01, 100.0, spec["cut"])
    form.addRow("Cut width [um]", cut_sb)
    space_sb = spin(0.01, 100.0, spec["space"])
    form.addRow("Cut spacing [um]", space_sb)
    enc_sb = spin(0.0, 100.0, spec["enclosure"])
    form.addRow("Metal enclosure of the cuts [um]", enc_sb)
    excl_le = pya.QComboBox(dlg)
    excl_le.addItems(["none"] + layer_items)
    excl_le.setCurrentIndex(0)
    form.addRow("Exclusion layer", excl_le)
    margin_sb = spin(0.0, 1000.0, spec["excl_margin"])
    form.addRow("Exclusion margin around the block [um]", margin_sb)
    info = pya.QLabel("", dlg)
    info.setWordWrap(True)
    form.addRow(info)
    ask_ck = pya.QCheckBox("Ask every time (untick: use these values without asking)", dlg)
    ask_ck.setChecked(True)
    form.addRow(ask_ck)

    def values():
        cut_text = layer_le.currentText if layer_le.currentIndex >= 0 else ""
        res = {"layer": "" if cut_text in ("", NO_CUT) else layer_value(cut_text),
               "block": block_sb.value, "cut": cut_sb.value,
               "space": space_sb.value, "enclosure": enc_sb.value,
               "excl": "" if excl_le.currentIndex <= 0 else layer_value(excl_le.currentText),
               "excl_margin": margin_sb.value}
        if metal_cb is not None:
            res["metal"] = metal_cb.currentText.strip()
        return res


    def show(*args):
        info.setText(via_summary(values(), width))
    for sb in (block_sb, cut_sb, space_sb, enc_sb, margin_sb):
        sb.valueChanged(show)
    excl_le.currentIndexChanged(show)
    show()
    row = pya.QHBoxLayout()
    ok = pya.QPushButton("Add via", dlg)
    cancel = pya.QPushButton("Cancel", dlg)

    def try_accept(*args):
        if layer_le.currentIndex < 0:
            info.setText("\u26a0 Choose the via layer first.")
            return
        dlg.accept()
    ok.clicked(try_accept)
    cancel.clicked(lambda *args: dlg.reject())
    row.addStretch(1)
    row.addWidget(ok)
    row.addWidget(cancel)
    form.addRow(row)
    if dlg.exec_() != 1:
        return None
    spec = values()
    if spec.get("metal") == current:
        _error("The new metal must differ from %s." % current)
        return None
    _save_via_spec(spec)
    if spec.get("metal"):
        _cfg_set("via_next_" + current, spec["metal"])
    _cfg_set("via_ask", "1" if ask_ck.isChecked() else "0")
    return spec


VIA_ASKER = ask_via   # replaceable (tests)


def via_settings():
    """Panel button: edit the via settings (and switch 'ask every time' back on)."""
    _cfg_set("via_ask", "1")
    d = dock(None, METAL)
    spec = VIA_ASKER(d, d.width_sb.value, "settings (used for the next vias)")
    if spec is not None:
        d.viablock_sb.setValue(spec["block"])
        d.info_lbl.setText("Via: " + via_summary(spec, d.width_sb.value))


def _excl_layers(view, cv, spec):
    out = []
    for name in spec.get("excl", "").split(","):
        if name.strip():
            out.append(resolve_layer(view, cv, name.strip()))
    return out


def same_cell(new):
    """
    A cell with exactly the same shapes as the just-made ``new`` (same base name, e.g. Via_X, Via_X$1), if the layout
    has one: ``new`` is deleted and that cell returned, so equal vias share one cell.  Else ``new``.
    """
    layout = new.layout()
    base = new.name.split("$")[0]
    layers = [li for li in layout.layer_indexes() if not new.shapes(li).is_empty()]
    for c in layout.each_cell():
        if c.cell_index() == new.cell_index() or c.name.split("$")[0] != base or c.child_instances():
            continue
        if any(not c.shapes(li).is_empty() for li in layout.layer_indexes() if li not in layers):
            continue
        if all(c.shapes(li).size() == new.shapes(li).size() and
               (pya.Region(c.shapes(li)) ^ pya.Region(new.shapes(li))).is_empty() for li in layers):
            layout.delete_cell(new.cell_index())
            return c
    return new


def _draw_via_spec(view, cv, cell, center, landing, cut_layer, spec, width):
    return pcell.draw_via(cell, center, landing, cut_layer, spec["block"], width, cut=spec["cut"],
                          pitch=spec["cut"] + spec["space"], enclosure=spec["enclosure"],
                          excl_layers=_excl_layers(view, cv, spec), excl_margin=spec["excl_margin"])


def _metal_at(cv, layer, pos, ctx):
    """Region of the shapes on ``layer`` (in the current cell's hierarchy) touching point ``pos``."""
    dbu = cv.layout().dbu
    box = pya.DBox(pos, pos).enlarged(pya.DVector(0.01, 0.01)).transformed(ctx.inverted()).to_itype(dbu)
    return pya.Region(cv.cell.begin_shapes_rec_touching(layer, box))


def via_needs(view, cv, d, route_layer, rt, force=(), spec=None):
    """
    Where a via is needed: at a route end that touches metal of the partner layer (e.g. M2
    for an M1 route) but none of the route's own layer.  Returns (via params for the Route
    PCell, [(end, center, other layer index)], via layer index) - empty if none needed.
    """
    if rt.spec.is_waveguide or not (d.via_ck.isChecked() or force):
        return {}, [], None, None
    own_name = _layer_name(view, cv, route_layer)
    if spec and spec.get("from"):
        # layer change: the via joins the previous piece's metal (any pair, cut from the spec)
        other_name = spec["from"]
        cut_name = spec["layer"] if spec.get("layer") is not None else cut_for(own_name, other_name)
        if cut_name is None:
            return {}, [], None, None
        force = tuple(e for e in force if e == "start")
    elif own_name in VIA_PAIRS:
        other_name, cut_name = VIA_PAIRS[own_name]
    else:
        return {}, [], None, None
    if spec:
        spec = dict(spec)
    else:
        spec = via_spec_saved(d)
        spec["layer"] = cut_name          # an automatic via: the cut of this metal pair
    try:
        other = resolve_layer(view, cv, other_name)
        cut = resolve_layer(view, cv, spec["layer"]) if spec["layer"] else None   # None: contact pad only
        excl = _excl_layers(view, cv, spec)
    except RouteError:
        return {}, [], None, None
    try:
        ctx = pya.DCplxTrans(cv.context_dtrans())
    except Exception:
        ctx = pya.DCplxTrans()
    dbu = cv.layout().dbu
    block = spec["block"]
    side = pcell.via_layout(block, rt.spec.width, None, spec["cut"], spec["cut"] + spec["space"],
                            spec["enclosure"])[0]
    params, where = {}, []
    pts = rt.waypoints
    for end, pos, prev in (("start", pts[0], pts[1]), ("end", pts[-1], pts[-2])):
        if not d.via_ck.isChecked() and end not in force:
            continue                      # auto vias off: only the forced (layer change) ends
        if spec.get("from") and end not in force:
            continue                      # a layer-change piece: only its start via
        if not _metal_at(cv, route_layer, pos, ctx).is_empty():
            continue                      # lands on its own metal: no via
        there = _metal_at(cv, other, pos, ctx)
        if there.is_empty():
            continue                      # nothing on the other metal either
        # end on the edge of a pad: move the via fully inside it
        v = pos - prev
        u = v * (1.0 / (v.abs() or 1.0))
        probe = lambda q: not there.interacting(pya.Region(
            pya.DBox(q, q).enlarged(pya.DVector(0.01, 0.01)).transformed(ctx.inverted()).to_itype(dbu))).is_empty()
        inside_ahead = probe(pos + u * 0.2)
        inside_behind = probe(pos - u * 0.2)
        # (a layer change keeps its via centred on the change point: both wires end there)
        shift = side / 2.0 if inside_ahead and not inside_behind and end not in force else 0.0
        params["via_" + end] = cv.layout().get_info(other).to_s()
        params["via_shift_" + end] = shift
        where.append((end, pos + u * shift, other))
    if params:
        info = cv.layout().get_info
        params.update({"via_layer": info(cut).to_s() if cut is not None else "", "via_block": block,
                       "via_cut": spec["cut"],
                       "via_space": spec["space"], "via_enclosure": spec["enclosure"],
                       "via_excl": ",".join(info(li).to_s() for li in excl),
                       "via_excl_margin": spec["excl_margin"]})
    return params, where, cut, spec


def create_route(view, points, d=None, through=False, layer=None, width=None, force_via=(), transact=True,
                 via_spec=None, extend_end=0.0):
    """
    Compute the route through ``points`` (display coordinates) and insert it. Returns the Route or None.
    ``through``: ``points`` are the final corners (port to port); route exactly through them.
    ``layer`` / ``width``: instead of the window's layer / width (layer changes, Bridge).
    ``force_via``: ends ("start"/"end") that get a via even with automatic vias off.
    ``transact``: False when the caller wraps several routes into one undo step.
    ``via_spec``: via settings for this route's vias (default: the saved ones).
    ``extend_end``: run the end this much further (over the via of a following layer change).
    An end with a via is run across the whole via, so the metal covers it completely.
    """
    d = d or dock(view)
    d.save()
    try:
        if len(points) < 2:
            raise RouteError("Give at least two points.")
        sp, ep = _end_ports(view, d, points, force=through)
        if through:
            spec = d.spec()
            if width:
                spec.width = width
            rt = route_through(points, spec)
        else:
            rt = compute(d, points, sp, ep, view)  # also validates that every corner fits the bend radius
        cv, to_cell = _cellview(view)
        layout = cv.layout()
        pre = d.preset()
        if pre is not None:
            parts = [(resolve_layer(view, cv, c.layer), c.width, c.offset) for c in pre.components] or \
                [(_layer_or(view, cv, *P["core"][0]), rt.spec.width, 0.0)]
        else:
            text = layer or d.layer_cb.currentText.strip()
            core = _chosen_layer(view, cv, text)
            parts = [(core, rt.spec.width, 0.0)]
            if rt.spec.is_waveguide and d.clad_le.currentText.strip() and rt.spec.cladding > 0:
                parts.append((resolve_layer(view, cv, d.clad_le.currentText), rt.spec.width + 2 * rt.spec.cladding, 0.0))
        if not through and not d.path_mode() and not d.chain_mode() and len(rt.waypoints) >= 2:
            # auto-route: go around other devices (waveguide) / other metal (metal)
            spec = rt.spec
            wp = rt.waypoints
            u0 = (wp[1].x - wp[0].x, wp[1].y - wp[0].y)
            u1 = (wp[-2].x - wp[-1].x, wp[-2].y - wp[-1].y)
            n0, n1 = math.hypot(*u0), math.hypot(*u1)
            new, moved = avoid_points(view, cv, d, spec, wp, (wp[0], (u0[0] / n0, u0[1] / n0)),
                                      (wp[-1], (u1[0] / n1, u1[1] / n1)),
                                      None if spec.is_waveguide else [li for li, _, _ in parts])
            if moved:
                rt = route_through(new, spec)
        corners = _on_grid(pya.DPath(rt.waypoints, rt.spec.width).transformed(to_cell), layout.dbu)
        via_params, via_where, via_cut, vspec = via_needs(view, cv, d, parts[0][0], rt, force_via, via_spec)
        # the metal covers the whole via: run each end that has a via across it
        ext = {"start": 0.0, "end": max(extend_end or 0.0, 0.0)}
        if via_where:
            side = pcell.via_layout(vspec["block"], rt.spec.width, None, vspec["cut"],
                                    vspec["cut"] + vspec["space"], vspec["enclosure"])[0]
            for end, center, other in via_where:
                shift = via_params.get("via_shift_" + end, 0.0)
                ext[end] = max(ext[end], shift + side / 2.0)
                via_params["via_shift_" + end] = shift - ext[end]      # same via centre on the longer route
        if ext["start"] > 0 or ext["end"] > 0:
            rt = route_through(_extend_ends(rt.waypoints, ext["start"], ext["end"]), rt.spec)
            corners = _on_grid(pya.DPath(rt.waypoints, rt.spec.width).transformed(to_cell), layout.dbu)
    except RouteError as e:
        _error(str(e))
        return None

    what = pre.name if pre else ("Waveguide" if rt.spec.is_waveguide else "Metal")
    # grating couplers only at free ends: never on top of a device port
    mode = d.gc_mode_cb.currentIndex if rt.spec.is_waveguide else 0
    gc_ends = [e for e, wanted, port in (("A", mode in (1, 2), sp), ("B", mode in (1, 3), ep)) if wanted and port is None]
    hint = sp or ep
    with_gcs = bool(gc_ends)
    gc_msg = None
    if transact:
        view.transaction("Route")
    try:
        if d.editable() and with_gcs:
            # one klroute cell: route + grating couplers + label, all following the corner points
            what, gc_msg = _place_route_with_gcs(view, cv, corners, rt, d, pre, parts, gc_ends, hint)
            what += " - editable: drag corners with the Partial tool (GCs and label follow), double-click for parameters"
        elif d.editable():
            lib = _pdk_library(cv)
            wire = None if rt.spec.is_waveguide else _pdk_wire_cell(lib, _layer_name(view, cv, parts[0][0]))
            wp = rt.waypoints
            manhattan_ends = all(abs(a.x - b.x) < 1e-9 or abs(a.y - b.y) < 1e-9
                                 for a, b in ((wp[0], wp[1]), (wp[-2], wp[-1])))
            bendp = _bend_params(view, cv, d) if rt.spec.is_waveguide else {}
            if bendp:
                klib = pcell.register()
                _place_pcell(cv, klib, klib.layout().pcell_declaration(pcell.PCELL_NAME), {
                    "path": corners, "kind": "waveguide", "radius": rt.spec.radius, "bend": "circular",
                    "layers": [layout.get_info(li).to_s() for li, _, _ in parts],
                    "widths": ["%g" % w for _, w, _ in parts],
                    "offsets": ["%g" % o for _, _, o in parts], **bendp})
                what = "klroute Route cell of PDK cells: %s at the corners, %s between" % (
                    bendp["bend_cell"], bendp["straight_cell"] or "drawn straights")
            elif pre is not None and lib is not None and manhattan_ends:
                decl = lib.layout().pcell_declaration("Waveguide")
                _place_pcell(cv, lib, decl, {"path": corners, "waveguide_type": pre.name})
                what = "%s cell '%s'" % (lib.name(), pre.name)
            elif wire is not None and _chosen_wire(d, lib, _layer_name(view, cv, parts[0][0])) is not None:
                wire = _chosen_wire(d, lib, _layer_name(view, cv, parts[0][0]))
                # the PDK library's own wire cell, editable like a path
                name, decl = wire
                defaults = {q.name: q.default for q in decl.get_parameters()}
                base = float(defaults["widths"][0])
                w = rt.spec.width
                _place_pcell(cv, lib, decl, {
                    "path": pya.DPath(list(corners.each_point()), w), "width": w,
                    "layers": list(defaults["layers"]),
                    "widths": [w + float(x) - base for x in defaults["widths"]],
                    "offsets": list(defaults["offsets"])})
                for end, center, other in via_where:
                    vc = layout.create_cell("Via_%s" % ((_layer_name(view, cv, via_cut) or layout.get_info(via_cut).name
                                                         or "via") if via_cut is not None else "contact"))
                    _draw_via_spec(view, cv, vc, pya.DPoint(0, 0), [parts[0][0], other], via_cut, vspec, w)
                    if any(layout.get_info(li).to_s() in device_only_layers(cv) for li in (parts[0][0], other)):
                        vc.shapes(_layer_or(view, cv, "DevRec", "68/0")).insert(vc.dbbox())
                    vc = same_cell(vc)
                    c = to_cell * center
                    cv.cell.insert(pya.DCellInstArray(vc.cell_index(), pya.DTrans(pya.DVector(c.x, c.y))))
                what = "%s cell '%s' (%s)" % (lib.name(), name.strip(), "+".join(str(n).strip() for n in defaults["layers"]))
            elif wire is not None:
                # Same layers / margins / pins as the PDK's wire cell, but as a klroute
                # Route cell (vias included in the cell).
                name, decl = wire
                defaults = {q.name: q.default for q in decl.get_parameters()}
                dw = [float(x) - float(defaults["widths"][0]) for x in defaults["widths"]]
                w = rt.spec.width
                try:
                    pin_layer = layout.get_info(resolve_layer(view, cv, "PinRecM")).to_s()
                except RouteError:
                    pin_layer = ""
                klib = pcell.register()
                _place_pcell(cv, klib, klib.layout().pcell_declaration(pcell.PCELL_NAME), {
                    "path": corners, "kind": "metal",
                    "layers": [layout.get_info(resolve_layer(view, cv, str(n).strip())).to_s()
                               for n in defaults["layers"]],
                    "widths": ["%g" % (w + x) for x in dw],
                    "offsets": ["%g" % float(o) for o in defaults["offsets"]],
                    "pin_layer": pin_layer, "pin_width": w, **via_params, **_metal_devrec(view, cv, parts[0][0])})
                what = "klroute Route cell like PDK '%s' (%s)" % (
                    name.strip(), "+".join(str(n).strip() for n in defaults["layers"]))
            else:
                klib = pcell.register()
                decl = klib.layout().pcell_declaration(pcell.PCELL_NAME)
                _place_pcell(cv, klib, decl, {
                    "path": corners,
                    "kind": "waveguide" if rt.spec.is_waveguide else "metal",
                    "radius": rt.spec.radius, "bend": rt.spec.bend,
                    "layers": [layout.get_info(li).to_s() for li, _, _ in parts],
                    "widths": ["%g" % w for _, w, _ in parts],
                    "offsets": ["%g" % o for _, _, o in parts],
                    **(_siepic_params(view, cv, d, pre) if rt.spec.is_waveguide else
                       dict(via_params, **_metal_devrec(view, cv, parts[0][0])))})
                what = "klroute Route cell (%s)" % what
                if pre is not None and lib is not None:
                    what += " [PDK Waveguide cell needs horizontal/vertical end segments]"
            what += " - editable: select it, drag corners with the Partial tool, double-click for parameters"
        else:
            sl = _siepic_layers(view, cv, d) if rt.spec.is_waveguide else None
            if sl is None:
                for layer, width, offset in parts:
                    cv.cell.shapes(layer).insert(rt.component_shape(width, offset, d.as_path()).transformed(to_cell))
                for end, center, other in via_where:
                    _draw_via_spec(view, cv, cv.cell, to_cell * center, [parts[0][0], other], via_cut,
                                   vspec, rt.spec.width)
            else:
                # a small static component cell: shapes + SiEPIC pins + DevRec + model texts
                wg_l = sl.get("waveguide")
                sub = layout.create_cell("Route_wg")
                for layer, width, offset in parts:
                    # SiEPIC rejects multi-point paths on its Waveguide layer -> polygon there
                    as_path = d.as_path() and layer != wg_l
                    sub.shapes(layer).insert(rt.component_shape(width, offset, as_path))
                pcell.draw_route(sub, rt, [li for li, _, _ in parts], [w for _, w, _ in parts],
                                 [o for _, _, o in parts], pin_layer=sl["pin"], devrec_layer=sl["devrec"],
                                 model=pre.model if pre else "", cml=pre.cml if pre else "",
                                 waveguide_type=pre.name if pre else "", bands=False)
                cv.cell.insert(pya.DCellInstArray(sub.cell_index(), to_cell))
                what += " (cell %s with SiEPIC pins)" % sub.name
    except Exception:
        _error("Could not insert the route:\n" + traceback.format_exc())
        return None
    finally:
        if transact:
            view.commit()
    view.add_missing_layers()
    msg = "%s: length %.4f um, %d bend(s)" % (what, rt.length, rt.bends)
    if via_where:
        msg += "\nVia (%s) to %s at the %s" % (((layout.get_info(via_cut).name or layout.get_info(via_cut).to_s())
                                               if via_cut is not None else "contact, no cut"),
                                              _layer_name(view, cv, via_where[0][2]),
                                              " and ".join(e for e, _, _ in via_where))
    if gc_msg is not None:
        msg += "\n" + gc_msg
    elif with_gcs:
        msg += "\n" + _add_gcs(view, cv, to_cell, rt, d, pre, gc_ends, hint)
    if with_gcs:
        msg += _dft_warnings(view, cv, d, rt, gc_ends)
    ports_msg = ["%s at %s.%s" % (e, q["device"], q["name"]) for e, q in (("A", sp), ("B", ep)) if q]
    if ports_msg:
        msg += "\nConnected to port " + ", ".join(ports_msg)
    d.info_lbl.setText(msg)
    _status(msg)
    return rt


def _extend_ends(wp, a, b):
    """Corner points with the first / last segment made longer by a / b (straight on)."""
    wp = [pya.DPoint(q.x, q.y) for q in wp]
    if a > 0:
        u = wp[0] - wp[1]
        wp[0] = wp[0] + u * (a / u.abs())
    if b > 0:
        u = wp[-1] - wp[-2]
        wp[-1] = wp[-1] + u * (b / u.abs())
    return wp


def create_layered_route(view, points, switches, d=None, layer=None, width=None, replace=None, via_specs=None):
    """
    Metal route that changes layer: the first layer (e.g. M1) up to the first switch point,
    the partner layer (M2) up to the next one, and so on, with a via at every switch
    point.  ``switches``: indexes into ``points``.  ``layer`` / ``width``: of the first piece
    (default: the window's).  ``replace``: an instance deleted first (Bridge: the route that
    is broken).  ``via_specs``: {switch index: via settings} (default: the saved ones).
    One undo step.  Returns the list of (layer name, Route) or None.
    """
    via_specs = via_specs or {}
    d = d or dock(view, METAL)
    d.save()
    try:
        cv, _ = _cellview(view)
        text = layer or d.layer_cb.currentText.strip()
        li = _chosen_layer(view, cv, text)
        base = _layer_name(view, cv, li) or text
        cuts = sorted(k for k in set(switches) if 0 < k < len(points) - 1)
        bounds = [0] + cuts + [len(points) - 1]
        spec = d.spec()
        if width:
            spec.width = width
        # layer of every piece: the metal chosen at each change, else the usual partner (profile)
        names = [base]
        for k in cuts:
            nxt = (via_specs.get(k) or {}).get("metal") or (VIA_PAIRS.get(names[-1], ("",))[0])
            if not nxt:
                raise RouteError("No via is defined for layer '%s'. Choose the metal to change to." % names[-1])
            if nxt == names[-1]:
                raise RouteError("The layer change at point %d stays on %s." % (k + 1, nxt))
            names.append(nxt)
        pieces = []
        for k, (a, b) in enumerate(zip(bounds, bounds[1:])):
            pts = points[a:b + 1]
            route_through(pts, spec)          # check every piece before changing anything
            pieces.append((pts, names[k]))
    except RouteError as e:
        _error(str(e))
        return None
    view.transaction("Route with layer change" if replace is None else "Bridge")
    made = []
    try:
        if replace is not None:
            replace.delete()
        for k, (pts, name) in enumerate(pieces):
            vs = None
            if k > 0:
                vs = dict(via_specs.get(cuts[k - 1]) or via_spec_saved(d))
                vs["from"] = pieces[k - 1][1]
                c = cut_for(pieces[k - 1][1], name)
                if not vs.get("metal") and c is not None:
                    vs["layer"] = c
            ext_end = 0.0
            if k + 1 < len(pieces):     # a layer change follows: run this piece across that via too
                nv = via_specs.get(cuts[k]) or via_spec_saved(d)
                ext_end = pcell.via_layout(nv["block"], spec.width, None, nv["cut"], nv["cut"] + nv["space"],
                                           nv["enclosure"])[0] / 2.0
            rt = create_route(view, pts, d, through=True, layer=name, width=spec.width,
                              force_via=("start",) if k > 0 else (), transact=False, via_spec=vs,
                              extend_end=ext_end)
            if rt is None:
                break
            made.append((name, rt))
    finally:
        view.commit()
    if made:
        msg = "%s: %s, %d via(s) (%s), length %.3f um" % (
            "Bridge" if replace is not None else "Route with layer change",
            " \u2192 ".join(n for n, _ in made), len(made) - 1,
            ", ".join(sorted({_cut_text(cut_for(a, b)) for (a, _), (b, _) in zip(made, made[1:])})),
            sum(r.length for _, r in made))
        d.info_lbl.setText(msg)
        _status(msg)
    return made


def _metal_route_info(view, cv, inst):
    """(layer text, width) of a metal route cell (klroute metal Route or PDK Wire_*), else None."""
    params = inst.pcell_parameters_by_name()
    name = inst.pcell_declaration().name().strip()
    if name.startswith("Wire"):
        layers = params.get("layers") or []
        return (str(layers[0]).strip() if layers else P["default_metal"]), float(params.get("width") or params["path"].width)
    if name == pcell.PCELL_NAME and str(params.get("kind", "")).startswith("m"):
        layers = params.get("layers") or []
        widths = _floats_list(params.get("widths"))
        return (str(layers[0]).strip() if layers else ""), (widths[0] if widths else params["path"].width)
    return None


def _project(pts, p):
    """Nearest point of polyline ``pts`` to p: (arc length, point, segment index)."""
    best, s0 = None, 0.0
    for k, (a, b) in enumerate(zip(pts, pts[1:])):
        v = b - a
        n2 = v.sq_abs()
        t = 0.0 if n2 == 0 else max(0.0, min(1.0, ((p.x - a.x) * v.x + (p.y - a.y) * v.y) / n2))
        q = pya.DPoint(a.x + v.x * t, a.y + v.y * t)
        dist = q.distance(p)
        if best is None or dist < best[0]:
            best = (dist, s0 + v.abs() * t, q, k)
        s0 += v.abs()
    return best[1], best[2], best[3]


def metal_route_at(view, cv, p, radius):
    """The metal route cell whose centre line passes nearest to p (within its half width or
    ``radius``): dict inst, pts (display), layer, width, s, q, seg - or None."""
    try:
        ctx = pya.DCplxTrans(cv.context_dtrans())
    except Exception:
        ctx = pya.DCplxTrans()
    dbu = cv.layout().dbu
    best = None
    for inst in cv.cell.each_inst():
        if not is_route(inst):
            continue
        info = _metal_route_info(view, cv, inst)
        if info is None:
            continue
        t = ctx * inst.dcplx_trans
        pts = [t * q for q in inst.pcell_parameters_by_name()["path"].each_point()]
        if len(pts) < 2:
            continue
        sv, q, seg = _project(pts, p)
        dist = q.distance(p)
        if dist <= max(radius, info[1] / 2.0) and (best is None or dist < best["dist"]):
            q = pya.DPoint(round(q.x / dbu) * dbu, round(q.y / dbu) * dbu)
            best = {"inst": inst, "pts": pts, "layer": info[0], "width": info[1], "s": sv, "q": q,
                    "seg": seg, "dist": dist}
    return best


def bridge_points(pts, a, b):
    """Corner list of the route with the two break points inserted; returns (points, [i1, i2])."""
    if a["s"] > b["s"]:
        a, b = b, a
    tagged = [(q, False) for q in pts[:a["seg"] + 1]] + [(a["q"], True)] + \
             [(q, False) for q in pts[a["seg"] + 1:b["seg"] + 1]] + [(b["q"], True)] + \
             [(q, False) for q in pts[b["seg"] + 1:]]
    out = []
    for q, tag in tagged:
        if out and out[-1][0].distance(q) < 1e-6:
            out[-1] = (out[-1][0], out[-1][1] or tag)
        else:
            out.append((q, tag))
    idx = [k for k, (_, tag) in enumerate(out) if tag]
    if len(idx) != 2 or idx[0] == 0 or idx[-1] == len(out) - 1:
        raise RouteError("Click two different points between the two ends of the route.")
    return [q for q, _ in out], idx


_plugins = {}


def switch_layer_now():
    """Panel button: change the metal layer at the last clicked point of the path being drawn."""
    pl = _plugins.get(METAL)
    if pl is None or not pl.points:
        _status("Layer change: start drawing a metal path first (Click mode Path), then press this button, "
                "Shift+click a corner, or press V")
        return
    pl.toggle_switch()


def _metal_devrec(view, cv, layer):
    """Route PCell parameters for a DevRec band when ``layer`` must be inside a component (SiEPIC)."""
    if cv.layout().get_info(layer).to_s() not in device_only_layers(cv):
        return {}
    return {"devrec_layer": cv.layout().get_info(_layer_or(view, cv, "DevRec", "68/0")).to_s()}


def _chosen_layer(view, cv, text):
    """Layer index of the window's Layer choice; an empty choice is an error (nothing is preselected)."""
    text = (text or "").strip()
    if not text:
        raise RouteError("Choose a layer in the window's Layer list first "
                         "(or a PDK waveguide type / PDK wire cell, which bring their own layers).")
    if text == PANEL:
        return _panel_layer(view, cv)
    return resolve_layer(view, cv, text)


def _layer_or(view, cv, name, fallback):
    try:
        return resolve_layer(view, cv, name)
    except RouteError:
        return cv.layout().layer(pya.LayerInfo.from_string(fallback))


def _choose_gc(d, lib, pre):
    names = gcmod.gc_cells(lib)
    if not names:
        raise RouteError("No grating-coupler cell found in the technology's PDK library.")
    sel = d.gc_cell_cb.currentText
    if sel and sel != GC_AUTO and sel in names:
        return sel
    return gcmod.best_gc(names, pre.name if pre else "")


def _gc_label(view, cv, d, gc_name, n_gc=2, hint=None):
    """
    Label for the first GC of a new route ('' if switched off) and a warning line or ''.
    opt_in_... (input) or out_... (output); with two GCs the route cell adds the out_ label itself.
    The device ID defaults to the device at a connected port, so the input and the output
    route of one device share it.
    """
    if not d.label_ck.isChecked():
        return "", ""
    text_l = _layer_or(view, cv, "Text", "10/0")
    existing = gcmod.measurement_labels(cv.cell, text_l)
    outputs = gcmod.output_labels(cv.cell, text_l)
    dev = gcmod.clean_device_id(d.devid_le.text)
    if not dev and hint is not None:
        o = hint["origin"]
        dev = gcmod.clean_device_id("%s-X%d-Y%d" % (hint["device"], int(round(o.x)), int(round(o.y))))
    if not dev:
        dev = gcmod.next_device_id(existing)
    has_in = any(e["deviceID"] == dev for e in existing)
    role = d.role_cb.currentIndex
    output = role == 2 or (role == 0 and n_gc == 1 and has_in)
    label = gcmod.opt_in_label(d.pol_cb.currentText, gcmod.wavelength_for(gc_name), dev)
    warn = ""
    if output and n_gc == 1:
        label = gcmod.out_label(label)
        if dev in outputs:
            warn = "\u26a0 device '%s' already has an output label" % dev
    elif has_in:
        warn = "\u26a0 device ID '%s' already has an opt_in label - SiEPIC needs unique IDs" % dev
    return label, warn


def _place_route_with_gcs(view, cv, corners, rt, d, pre, parts, ends=("A", "B"), hint=None):
    lib = _pdk_library(cv)
    gc_name = _choose_gc(d, lib, pre)
    layout = cv.layout()
    label, warn = _gc_label(view, cv, d, gc_name, len(ends), hint)
    info = lambda name, fb: layout.get_info(_layer_or(view, cv, name, fb)).to_s()
    klib = pcell.register()
    decl = klib.layout().pcell_declaration(pcell.PCELL_NAME)
    inst = _place_pcell(cv, klib, decl, {
        "path": corners, "kind": "waveguide", "radius": rt.spec.radius, "bend": rt.spec.bend,
        "layers": [layout.get_info(li).to_s() for li, _, _ in parts],
        "widths": ["%g" % w for _, w, _ in parts],
        "offsets": ["%g" % o for _, _, o in parts],
        **_siepic_params(view, cv, d, pre),
        "technology": cv.technology, "pdk_lib": lib.name(),
        "waveguide_type": pre.name if pre else "",
        "gc_cell": gc_name, "gc_ends": "both" if len(ends) == 2 else ("start" if ends[0] == "A" else "end"),
        "label": label,
        "text_layer": info("Text", "10/0"),
        "gc_pin_layer": info("PinRec", "1/10"),
        "gc_fiber_layer": info("FbrTgt", "81/0"),
        **_bend_params(view, cv, d)})
    what = "klroute Route cell: %s + %s" % (pre.name if pre else "waveguide", gc_name)
    lines = []
    if label:
        lines.append(_coord_line(view, cv, label))
    if warn:
        lines.append(warn)
    return what, "\n".join(lines)


def _coord_line(view, cv, label):
    """'Test coordinates <id>: in (x, y), out (x, y)' for the device of ``label``."""
    text_l = _layer_or(view, cv, "Text", "10/0")
    dev = label.split("_")[5] if label.startswith("opt_in_") else label.split("_")[4]
    ins = [e for e in gcmod.measurement_labels(cv.cell, text_l) if e["deviceID"] == dev]
    outs = gcmod.output_labels(cv.cell, text_l).get(dev, [])
    parts = []
    if ins:
        parts.append("in (%d, %d)" % (int(ins[0]["x"]), int(ins[0]["y"])))
    for o in outs:
        parts.append("out (%d, %d)" % (int(o[0]), int(o[1])))
    return "Test coordinates %s: %s" % (dev, ", ".join(parts) or "-")


def _dft_warnings(view, cv, d, rt, gc_ends):
    """SiEPIC DFT hints for the GCs just placed at the route ends ('' if fine / no DFT rules)."""
    dft = dft_rules(cv)
    if not dft:
        return ""
    lines = []
    want = gc_end_dirs(view, d)
    wp = rt.waypoints
    for tag, (p, q), forced in (("A", (wp[0], wp[1]), want[0]), ("B", (wp[-1], wp[-2]), want[1])):
        if tag in gc_ends and forced and _dir_letter((q.x - p.x, q.y - p.y)) != forced:
            lines.append("\u26a0 SiEPIC DFT: the GC at %s must face %s - let the route leave %s towards %s "
                         "(auto-route does this)" % (tag, forced, tag, forced))
    if len(gc_ends) == 2:
        a, b = wp[0], wp[-1]
        pitch = dft["pitch"]
        if abs(a.x - b.x) > 0.01 or abs(abs(a.y - b.y) / pitch - round(abs(a.y - b.y) / pitch)) > 1e-3:
            lines.append("\u26a0 SiEPIC DFT: put the two GCs in one column (same x), %g um apart (fiber array)" % pitch)
    return "".join("\n" + l for l in lines)


def _add_gcs(view, cv, to_cell, rt, d, pre, ends=("A", "B"), hint=None):
    """Grating couplers at the route ends (+ opt_in label). Returns a status line."""
    try:
        lib = _pdk_library(cv)
        gc_name = _choose_gc(d, lib, pre)
    except RouteError as e:
        _error(str(e))
        return "\u26a0 " + str(e)
    pin_l = _layer_or(view, cv, "PinRec", "1/10")
    fbr_l = _layer_or(view, cv, "FbrTgt", "81/0")
    wp = rt.waypoints
    ends = [(e, wp[0], wp[1]) if e == "A" else (e, wp[-1], wp[-2]) for e in ends]

    lines = []
    view.transaction("Route grating couplers")
    try:
        fibers = []
        for tag, port, nxt in ends:
            v = nxt - port
            n = v.abs()
            _, fiber = gcmod.place_gc(cv.cell, lib, gc_name, port, (v.x / n, v.y / n), pin_l, fbr_l, to_cell)
            fibers.append((tag, fiber))
            f = to_cell * fiber
            lines.append("%s at %s: fiber (%.3f, %.3f)" % (gc_name, tag, f.x, f.y))
        label, warn = _gc_label(view, cv, d, gc_name, len(fibers), hint)
        if label and fibers:
            if warn:
                lines.append(warn)
            text_l = _layer_or(view, cv, "Text", "10/0")
            gcmod.add_opt_in(cv.cell, text_l, fibers[0][1], label, to_cell)
            if len(fibers) > 1 and gcmod.out_label(label):
                gcmod.add_opt_in(cv.cell, text_l, fibers[1][1], gcmod.out_label(label), to_cell)
            lines.append(_coord_line(view, cv, label))
    except Exception:
        _error("Could not place the grating couplers:\n" + traceback.format_exc())
    finally:
        view.commit()
    view.add_missing_layers()
    return "\n".join(lines)


def _route_parts(view, cv, d, pre, spec):
    """(layer index, width, offset) bands of the waveguide, like create_route uses."""
    if pre is not None:
        return [(resolve_layer(view, cv, c.layer), c.width, c.offset) for c in pre.components] or \
            [(_layer_or(view, cv, *P["core"][0]), spec.width, 0.0)]
    text = d.layer_cb.currentText.strip()
    core = _chosen_layer(view, cv, text)
    parts = [(core, spec.width, 0.0)]
    if d.clad_le.currentText.strip() and spec.cladding > 0:
        parts.append((resolve_layer(view, cv, d.clad_le.currentText), spec.width + 2 * spec.cladding, 0.0))
    return parts


def _selected_device(view, cv):
    """(instance, display bbox) of the single selected cell instance."""
    insts = [o for o in view.each_object_selected() if o.is_cell_inst()]
    if len(insts) != 1:
        raise RouteError("Select exactly one device instance first (Select tool, click the device).")
    o = insts[0]
    dbu = cv.layout().dbu
    try:
        ctx = pya.DCplxTrans(cv.context_dtrans())
    except Exception:
        ctx = pya.DCplxTrans()
    # o.trans() already includes the instance's own transformation -> use the cell's bbox
    box = o.inst().cell.bbox().transformed(o.trans()).to_dtype(dbu).transformed(ctx)
    return o.inst(), box


def device_ports(view, cv, box, metal=False):
    """Free ports on a device (optical, or electrical with metal=True): pins inside its bbox that no other pin sits on."""
    found = find_ports(view, cv, box.enlarged(pya.DVector(1.0, 1.0)), metal)
    inside = [q for q in found if box.enlarged(pya.DVector(0.05, 0.05)).contains(q["pos"])]
    free = []
    for q in inside:
        if not any(r is not q and r["pos"].distance(q["pos"]) < PORT_TOL for r in found):
            free.append(q)
    return free


def connect_selected_device():
    """Route every free optical port of the selected device to its own GC in a fibre-array column."""
    view = pya.LayoutView.current()
    if view is None:
        _error("Open a layout first.")
        return
    d = dock(view, WG)
    d.save()
    try:
        cv, to_cell = _cellview(view)
        layout = cv.layout()
        inst, box = _selected_device(view, cv)
        ports = device_ports(view, cv, box)
        if not ports:
            raise RouteError("The selected device has no free optical ports (PinRec pins).")
        pre = d.preset()
        spec = d.spec()
        parts = _route_parts(view, cv, d, pre, spec)
        lib = _pdk_library(cv)
        gc_name = _choose_gc(d, lib, pre)
        spacing = d.rspace_sb.value
        if spacing <= 0:
            # parallel routes must not overlap (incl. SiEPIC DevRec): widest band + 1 um
            spacing = max([w for _, w, _ in parts] + [spec.width + 2.0]) + 1.0
        side = ARRAY_SIDES[d.array_side_cb.currentIndex]
        aports = [autoconnect.Port(q["name"], q["pos"].x, q["pos"].y, q["dir"][0], q["dir"][1]) for q in ports]
        gc_pins, conns = autoconnect.plan((box.left, box.bottom, box.right, box.top), aports, spec,
                                          side=side, pitch=d.pitch_sb.value, spacing=spacing)
        for c in conns:  # validate all bends before placing anything
            route_through(c.points, spec)

        # labels: the input port's GC gets opt_in, all others out_, with one device ID
        hint = ports[0]
        dev = gcmod.clean_device_id(d.devid_le.text)
        if not dev:
            o = hint["origin"]
            dev = gcmod.clean_device_id("%s-X%d-Y%d" % (hint["device"], int(round(o.x)), int(round(o.y))))
        names = [c.port.name for c in conns]
        in_name = d.inport_le.text.strip() or ("opt1" if "opt1" in names else sorted(names)[0])
        if in_name not in names:
            raise RouteError("Input port '%s' not found on the device (ports: %s)." % (in_name, ", ".join(names)))
        opt_in = gcmod.opt_in_label(d.pol_cb.currentText, gcmod.wavelength_for(gc_name), dev)
        text_l = _layer_or(view, cv, "Text", "10/0")
        warn = ""
        if any(e["deviceID"] == dev for e in gcmod.measurement_labels(cv.cell, text_l)):
            warn = "\u26a0 device ID '%s' already has an opt_in label - SiEPIC needs unique IDs" % dev
    except RouteError as e:
        _error(str(e))
        return

    info = lambda name, fb: layout.get_info(_layer_or(view, cv, name, fb)).to_s()
    klib = pcell.register()
    decl = klib.layout().pcell_declaration(pcell.PCELL_NAME)
    view.transaction("Connect device to GC array")
    try:
        for c in conns:
            corners = _on_grid(pya.DPath([pya.DPoint(x, y) for x, y in c.points], spec.width).transformed(to_cell),
                               layout.dbu)
            label = ""
            if d.label_ck.isChecked():
                label = opt_in if c.port.name == in_name else gcmod.out_label(opt_in)
            _place_pcell(cv, klib, decl, {
                "path": corners, "kind": "waveguide", "radius": spec.radius, "bend": spec.bend,
                "layers": [layout.get_info(li).to_s() for li, _, _ in parts],
                "widths": ["%g" % w for _, w, _ in parts],
                "offsets": ["%g" % o for _, _, o in parts],
                **_siepic_params(view, cv, d, pre),
                "technology": cv.technology, "pdk_lib": lib.name(),
                "waveguide_type": pre.name if pre else "",
                "gc_cell": gc_name, "gc_ends": "start", "label": label,
                "text_layer": info("Text", "10/0"),
                "gc_pin_layer": info("PinRec", "1/10"),
                "gc_fiber_layer": info("FbrTgt", "81/0"),
                **_bend_params(view, cv, d)})
    except Exception:
        _error("Could not place the GC array:\n" + traceback.format_exc())
        return
    finally:
        view.commit()
    view.add_missing_layers()
    lines = ["%s: %d port(s) connected to a %s GC array on the %s, pitch %g um (editable route cells)"
             % (hint["device"], len(conns), gc_name, side, d.pitch_sb.value)]
    lines += ["  %s -> GC %d" % (c.port.name, c.gc_index + 1) for c in conns]
    if d.label_ck.isChecked():
        lines.append(_coord_line(view, cv, opt_in))
    if warn:
        lines.append(warn)
    msg = "\n".join(lines)
    d.info_lbl.setText(msg)
    _status(lines[0])


def instance_ports(view, cv, comp, metal=False, d=None):
    """
    All pins of one placed component (display coordinates): dicts pos, dir, name, device, used.
    'used' = another pin already sits on it (something is connected there).  Metal: PinRecM
    pins; a component without any (e.g. a bond pad) gets the middles of its metal edges.
    """
    inst, box = comp
    pin_l = _layer_or(view, cv, "PinRecM", "1/11") if metal else _layer_or(view, cv, "PinRec", "1/10")
    try:
        ctx = pya.DCplxTrans(cv.context_dtrans())
    except Exception:
        ctx = pya.DCplxTrans()
    t0 = ctx * inst.dcplx_trans
    ports = []
    it = inst.cell.begin_shapes_rec(pin_l)
    while not it.at_end():
        sh = it.shape()
        if sh.is_path() and sh.path.num_points() == 2:
            t = t0 * it.dtrans()
            a, b = [t * q for q in sh.dpath.each_point()]
            v = b - a
            n = v.abs()
            if n > 0:
                name = ""
                for tx in it.cell().shapes(pin_l).each_touching(sh.bbox()):
                    if tx.is_text():
                        name = tx.text_string
                ports.append({"pos": pya.DPoint((a.x + b.x) / 2, (a.y + b.y) / 2), "dir": (v.x / n, v.y / n),
                              "name": name or "pin%d" % (len(ports) + 1),
                              "device": inst.cell.basic_name().strip(), "origin": t0 * pya.DPoint(0, 0)})
        it.next()
    if not ports and metal:
        d = d or dock(view, METAL)
        layers = [li for li, _, _ in _route_parts(view, cv, d, None, d.spec())]
        ports = pad_ports(view, cv, comp, layers)
    around = find_ports(view, cv, box.enlarged(pya.DVector(1.0, 1.0)), metal)
    for q in ports:
        q["used"] = sum(1 for r in around if r["pos"].distance(q["pos"]) < PORT_TOL) > 1
    return ports


def suggest_attach(pa_list, pb_list):
    """The pair (free port of the 1st, free port of the 2nd) that is closest now."""
    best = None
    for qa in [q for q in pa_list if not q["used"]] or pa_list:
        for qb in [q for q in pb_list if not q["used"]] or pb_list:
            dist = qa["pos"].distance(qb["pos"])
            if best is None or dist < best[0]:
                best = (dist, qa, qb)
    return best[1], best[2]


def ask_attach(first, second):
    """
    Dialog: which pin of the 1st component (it moves) goes onto which pin of the 2nd.
    first / second : (component name, ports, suggested port)
    Returns (port_a, port_b) or None.
    """
    mw = pya.Application.instance().main_window()
    dlg = pya.QDialog(mw)
    dlg.setWindowTitle("Attach")
    form = pya.QFormLayout(dlg)
    form.addRow(pya.QLabel("'%s' is moved (and turned) so that its pin sits on the pin of '%s'."
                           % (first[0], second[0]), dlg))

    def port_combo(ports, default):
        c = pya.QComboBox(dlg)
        for q in ports:
            c.addItem("%s   (%s side, x=%.3f, y=%.3f)%s%s" % (
                q["name"], _side(q["dir"]), q["pos"].x, q["pos"].y,
                "   - connected" if q.get("used") else "", "   - suggested" if q is default else ""))
        c.setCurrentIndex(ports.index(default))
        return c

    ca = port_combo(first[1], first[2])
    form.addRow("1st: %s  pin (moves)" % first[0], ca)
    cb = port_combo(second[1], second[2])
    form.addRow("2nd: %s  pin (stays)" % second[0], cb)
    row = pya.QHBoxLayout()
    ok = pya.QPushButton("Attach", dlg)
    cancel = pya.QPushButton("Cancel", dlg)
    ok.clicked(lambda *args: dlg.accept())
    cancel.clicked(lambda *args: dlg.reject())
    row.addStretch(1)
    row.addWidget(ok)
    row.addWidget(cancel)
    form.addRow(row)
    if dlg.exec_() != 1:
        return None
    return first[1][ca.currentIndex], second[1][cb.currentIndex]


ATTACH_ASKER = ask_attach   # replaceable (tests)


def attach_trans(pa, pb, dbu):
    """
    Display-coordinate transformation that puts port pa onto port pb, facing it (pa's direction
    becomes the opposite of pb's).  Manhattan turns are exact; the shift is snapped to the grid.
    """
    ua, ub = pa["dir"], pb["dir"]
    ang = math.degrees(math.atan2(-ub[1], -ub[0]) - math.atan2(ua[1], ua[0]))
    ang = (ang + 360.0) % 360.0
    q = round(ang / 90.0) * 90.0
    if abs(ang - q) < 1e-6:
        ang = q % 360.0
    rot = pya.DCplxTrans(1.0, ang, False, 0.0, 0.0)
    shift = pb["pos"] - rot * pa["pos"]
    shift = pya.DVector(round(shift.x / dbu) * dbu, round(shift.y / dbu) * dbu)
    return pya.DCplxTrans(shift) * rot, ang


def attach(view, a, b, d=None, metal=None):
    """
    Attach component a to component b: ask which pins, then move (and turn) a so that its pin
    sits on b's pin.  a, b: (instance, display bbox).  Returns (port_a, port_b, angle) or None.
    """
    cv = view.active_cellview()
    d = d or dock(view)
    if metal is None:
        metal = d.kind == METAL
    try:
        if a[0] == b[0]:
            raise RouteError("Pick two different components.")
        pa_list = instance_ports(view, cv, a, metal, d)
        pb_list = instance_ports(view, cv, b, metal, d)
        what = "electrical (PinRecM)" if metal else "optical (PinRec)"
        if not pa_list:
            raise RouteError("'%s' has no %s pins to attach." % (a[0].cell.basic_name(), what))
        if not pb_list:
            raise RouteError("'%s' has no %s pins to attach to." % (b[0].cell.basic_name(), what))
    except RouteError as e:
        _error(str(e))
        return None
    sa, sb = suggest_attach(pa_list, pb_list)
    ans = ATTACH_ASKER((a[0].cell.basic_name().strip(), pa_list, sa), (b[0].cell.basic_name().strip(), pb_list, sb))
    if ans is None:
        return None
    pa, pb = ans
    t, ang = attach_trans(pa, pb, cv.layout().dbu)
    try:
        ctx = pya.DCplxTrans(cv.context_dtrans())
    except Exception:
        ctx = pya.DCplxTrans()
    group, routes = attached_group(view, cv, a[0], b[0])
    view.transaction("Attach %s to %s" % (a[0].cell.basic_name(), b[0].cell.basic_name()))
    try:
        moved, labels, rerouted, failed = move_group(view, cv, group, routes, t)
    finally:
        view.commit()
    msg = "Attached %s.%s to %s.%s at (%.3f, %.3f)%s" % (
        pa["device"], pa["name"], pb["device"], pb["name"], pb["pos"].x, pb["pos"].y,
        (", turned %g\u00b0" % ang) if abs(ang) > 1e-9 else "")
    extra = []
    if moved > 1:
        extra.append("%d attached component(s) moved along" % (moved - 1))
    if labels:
        extra.append("%d label(s) moved along" % labels)
    if rerouted:
        extra.append("%d route(s) re-routed to the new position" % rerouted)
    if extra:
        msg += "\n" + ", ".join(extra)
    for name, err in failed:
        msg += "\n\u26a0 route %s could not follow (%s) - redraw it" % (name, err)
    d.info_lbl.setText(msg)
    _status(msg.split("\n")[0])
    return pa, pb, ang


def _inst_pins(view, cv, inst, ctx):
    """[(pos, dir)] of all optical and electrical pins of a placed instance (display coordinates)."""
    out = []
    t0 = ctx * inst.dcplx_trans
    for name, fb in (("PinRec", "1/10"), ("PinRecM", "1/11")):
        it = inst.cell.begin_shapes_rec(_layer_or(view, cv, name, fb))
        while not it.at_end():
            sh = it.shape()
            if sh.is_path() and sh.path.num_points() == 2:
                t = t0 * it.dtrans()
                a, b = [t * q for q in sh.dpath.each_point()]
                v = b - a
                n = v.abs()
                if n > 0:
                    out.append((pya.DPoint((a.x + b.x) / 2, (a.y + b.y) / 2), (v.x / n, v.y / n)))
            it.next()
    return out


def is_route(inst):
    """
    A route cell that can be re-routed when one of its ends moves: a klroute Route, or a PDK
    waveguide / wire PCell drawn along a path (e.g. the PDK's 'Waveguide').
    """
    try:
        if not inst.is_pcell():
            return False
        name = inst.pcell_declaration().name().strip()
        if name == pcell.PCELL_NAME and inst.cell.library().name() == pcell.LIB_NAME:
            return True
        return (name == "Waveguide" or name.startswith("Wire")) and \
            isinstance(inst.pcell_parameters_by_name().get("path"), pya.DPath)
    except Exception:
        return False


def _route_spec(cv, inst, params, path):
    """RouteSpec of an existing route cell (klroute Route or PDK waveguide / wire)."""
    if "kind" in params:
        widths = _floats_list(params.get("widths"))
        return RouteSpec(kind=params.get("kind", "waveguide"), width=widths[0] if widths else path.width,
                         radius=params.get("radius", 10.0), bend=params.get("bend", "circular"))
    name = inst.pcell_declaration().name().strip()
    if name.startswith("Wire"):
        return RouteSpec(kind="metal", width=path.width, radius=0.0)
    wt = params.get("waveguide_type", "")
    pre = next((w for w in tech_waveguides(cv) if w.name == wt), None)
    return RouteSpec(kind="waveguide", width=pre.width if pre else path.width,
                     radius=pre.radius if pre else 10.0)


def attached_group(view, cv, first, keep):
    """
    What moves together with ``first``: every component attached to it pin-on-pin (and what is
    attached to those), except ``keep`` (the component it is being attached to).
    klroute routes are not moved: an attached route end follows instead (re-routed), unless
    both of its ends are on the group - then the route moves as a whole.
    Returns (instances, [(route instance, [(pos, dir) of its ends on the group])]).
    """
    try:
        ctx = pya.DCplxTrans(cv.context_dtrans())
    except Exception:
        ctx = pya.DCplxTrans()
    others = [(i, _inst_pins(view, cv, i, ctx)) for i in cv.cell.each_inst()
              if i != first and i != keep]
    group, todo = [first], [first]
    route_ends = []
    while todo:
        g = todo.pop()
        for pos, u in _inst_pins(view, cv, g, ctx):
            for o, pins in others:
                if any(o == x for x in group) or not any(q.distance(pos) < PORT_TOL for q, _ in pins):
                    continue
                if is_route(o):
                    for r, ends in route_ends:
                        if r == o:
                            if all(e[0].distance(pos) >= PORT_TOL for e in ends):
                                ends.append((pos, u))
                            break
                    else:
                        route_ends.append((o, [(pos, u)]))
                else:
                    group.append(o)
                    todo.append(o)
    routes = []
    for r, ends in route_ends:
        if len(ends) >= 2:
            group.append(r)     # both ends on the moving group: move it whole
        else:
            routes.append((r, ends))
    return group, routes


def move_group(view, cv, group, routes, t):
    """
    Apply the display transformation ``t`` to the group's instances and to the labels (Text
    layer, e.g. opt_in) lying on them, and re-route the moved end of each attached route.
    Returns (instances moved, labels moved, routes re-routed, [(route, error)]).
    """
    layout = cv.layout()
    dbu = layout.dbu
    try:
        ctx = pya.DCplxTrans(cv.context_dtrans())
    except Exception:
        ctx = pya.DCplxTrans()
    m = ctx.inverted() * t * ctx           # the same move in the current cell's coordinates
    text_l = _layer_or(view, cv, "Text", "10/0")
    texts = []
    for inst in group:
        for sh in cv.cell.shapes(text_l).each_touching(inst.bbox()):
            if sh.is_text() and inst.bbox().contains(sh.text_pos) and not any(sh == x for x in texts):
                texts.append(sh)
    for inst in group:
        inst.transform(m)
    for sh in texts:
        sh.transform(m)

    rerouted, failed = 0, []
    for r, ends in routes:
        name = r.cell.display_title() if hasattr(r.cell, "display_title") else r.cell.name
        try:
            params = r.pcell_parameters_by_name()
            path = params["path"]
            tr = ctx * r.dcplx_trans                       # route cell -> display
            pts = [tr * q for q in path.each_point()]
            (pos, u), = ends
            new_pos = t * pos
            nu = t * pya.DVector(u[0], u[1])
            nu = (nu.x / nu.abs(), nu.y / nu.abs())
            moved_start = pts[0].distance(pos) < PORT_TOL
            if not moved_start and pts[-1].distance(pos) >= PORT_TOL:
                raise RouteError("its end is not on the pin")
            far, nxt = (pts[-1], pts[-2]) if moved_start else (pts[0], pts[1])
            v = nxt - far
            fu = (v.x / v.abs(), v.y / v.abs()) if v.abs() > 0 else (1.0, 0.0)
            spec = _route_spec(cv, r, params, path)
            manh = lambda w: abs(w[0]) < 1e-9 or abs(w[1]) < 1e-9
            new = route(far, new_pos, spec, _dir_letter(fu) if manh(fu) else "auto",
                        _dir_letter(nu) if manh(nu) else "auto")
            wps = list(new.waypoints)
            if moved_start:
                wps.reverse()
            local = _on_grid(pya.DPath([tr.inverted() * q for q in wps], path.width), dbu)
            r.change_pcell_parameter("path", local)
            rerouted += 1
        except Exception as e:
            failed.append((name, str(e).split("\n")[0]))
    return len(group), len(texts), rerouted, failed


def _floats_list(v):
    try:
        return [float(x) for x in (v or [])]
    except (TypeError, ValueError):
        return []


def attach_selected(kind=None):
    """Panel button: attach the 1st selected instance to the 2nd selected one."""
    view = pya.LayoutView.current()
    if view is None:
        _error("Open a layout first.")
        return
    cv = view.active_cellview()
    dbu = cv.layout().dbu
    try:
        ctx = pya.DCplxTrans(cv.context_dtrans())
    except Exception:
        ctx = pya.DCplxTrans()
    comps = []
    for o in view.each_object_selected():
        if o.is_cell_inst() and o.path_length() == 1:
            comps.append((o.inst(), o.inst().cell.bbox().transformed(o.trans()).to_dtype(dbu).transformed(ctx)))
    if len(comps) != 2:
        _error("Select exactly two components (Select tool: click the one to move, then Shift+click the one "
               "to attach it to), or use Click mode 'Attach' and click them.")
        return
    attach(view, comps[0], comps[1], dock(view, kind))


def object_at(view, cv, p):
    """(instance, display bbox) of the smallest instance of the current cell under point p, or None."""
    try:
        ctx = pya.DCplxTrans(cv.context_dtrans())
    except Exception:
        ctx = pya.DCplxTrans()
    best = None
    for inst in cv.cell.each_inst():
        box = inst.dbbox().transformed(ctx)
        if box.contains(p) and (best is None or box.area() < best[1].area()):
            best = (inst, box)
    return best


WRAP_PREFIX = "LBL_"     # cells made to keep a pad / GC together with its label


def _ctx(cv):
    try:
        return pya.DCplxTrans(cv.context_dtrans())
    except Exception:
        return pya.DCplxTrans()


def label_entries(view, cv, inst, zone=None):
    """
    The labels (Text layer) on an instance: free texts of the current cell lying on it, and texts
    kept in the instance's own cell (a label wrapper or a cell labelled from inside).  ``zone``:
    display DBox to look in (default: the instance's bbox).  Returns [(shapes, shape, text)].
    """
    text_l = _layer_or(view, cv, "Text", "10/0")
    ctx = _ctx(cv)
    dbu = cv.layout().dbu
    zone = zone if zone is not None else inst.dbbox().transformed(ctx)
    zc = zone.transformed(ctx.inverted()).enlarged(pya.DVector(0.001, 0.001))
    out = []
    shapes = cv.cell.shapes(text_l)
    for sh in shapes.each_touching(zc.to_itype(dbu)):
        if sh.is_text() and zc.contains(sh.text_dpos):
            out.append((shapes, sh, sh.text_string))
    if inst is not None and not inst.cell.is_library_cell() and not inst.cell.is_pcell_variant() and             _instance_count(inst.cell) == 1:
        t = inst.dcplx_trans
        own = inst.cell.shapes(text_l)
        for sh in own.each():
            if sh.is_text() and zc.contains(t * sh.text_dpos):
                out.append((own, sh, sh.text_string))
    return out


def _labels_on(view, cv, inst):
    return [sh for _, sh, _ in label_entries(view, cv, inst)]


def _instance_count(cell):
    n = 0
    for pi in cell.each_parent_inst():
        n += pi.inst().size()
    return n


def attach_label(view, cv, inst, pos, text):
    """
    Put label ``text`` at display point ``pos`` so that it moves with ``inst`` (call it inside a
    transaction):
    * a static cell placed only once (your own device / pad): the label goes inside that cell;
    * a label wrapper made before: inside the wrapper;
    * otherwise (a PDK / PCell pad or GC, or a cell used several times) the instance and the label
      are grouped into a new cell LBL_<cell>, placed where the instance was.
    Returns (the cell holding the label, how, the instance now carrying it).  Moving / rotating / copying the instance with
    KLayout's own tools then keeps the label on it; undo works as usual.
    """
    layout = cv.layout()
    text_l = _layer_or(view, cv, "Text", "10/0")
    ctx = _ctx(cv)
    cell = inst.cell
    static = not cell.is_library_cell() and not cell.is_pcell_variant() and not cell.is_proxy()
    if static and (cell.name.startswith(WRAP_PREFIX) or _instance_count(cell) == 1) and inst.size() == 1:
        local = (ctx * inst.dcplx_trans).inverted() * pos
        cell.shapes(text_l).insert(pya.DText(text, pya.DTrans(local.x, local.y)))
        return cell, ("inside" if not cell.name.startswith(WRAP_PREFIX) else "grouped"), inst
    # group the instance and its label in a wrapper cell at the instance's position
    idisp = inst.cplx_trans.disp                                  # database units
    disp = pya.DVector(idisp.x * layout.dbu, idisp.y * layout.dbu)
    wrap = layout.create_cell(WRAP_PREFIX + cell.name.strip().replace(" ", "_"))
    arr = inst.cell_inst
    arr.transform(pya.Trans(pya.Vector(-idisp.x, -idisp.y)))
    if inst.has_prop_id():
        wrap.insert(arr, inst.prop_id)
    else:
        wrap.insert(arr)
    local = (ctx * pya.DCplxTrans(disp)).inverted() * pos
    wrap.shapes(text_l).insert(pya.DText(text, pya.DTrans(local.x, local.y)))
    new = cv.cell.insert(pya.DCellInstArray(wrap.cell_index(), pya.DTrans(disp)))
    inst.delete()
    return wrap, "grouped", new


def ask_gc_label(info):
    """
    Dialog for a test label on a grating coupler.  ``info``: name, positions {key: (title, point)},
    suggested pol / wavelength / device ID / role, existing labels.
    Returns {"text", "pos_key", "replace"} or None.
    """
    mw = pya.Application.instance().main_window()
    dlg = pya.QDialog(mw)
    dlg.setWindowTitle("Label grating coupler %s" % info["name"])
    form = pya.QFormLayout(dlg)
    role_cb = pya.QComboBox(dlg)
    role_cb.addItems(["Input (opt_in_...)", "Output (out_...)", "Custom text"])
    role_cb.setCurrentIndex({"in": 0, "out": 1}.get(info["role"], 0))
    form.addRow("Label type", role_cb)
    pol_cb = pya.QComboBox(dlg)
    pol_cb.addItems(["TE", "TM"])
    pol_cb.setCurrentIndex(1 if info["pol"] == "TM" else 0)
    form.addRow("Polarization", pol_cb)
    wl_le = pya.QLineEdit(dlg)
    wl_le.setText(info["wl"])
    form.addRow("Wavelength [nm]", wl_le)
    kind_le = pya.QLineEdit(dlg)
    kind_le.setText("device")
    form.addRow("Type (opt_in_<pol>_<wl>_<type>_<ID>)", kind_le)
    dev_le = pya.QLineEdit(dlg)
    dev_le.setText(info["dev"])
    form.addRow("Device ID", dev_le)
    custom_le = pya.QLineEdit(dlg)
    custom_le.setPlaceholderText("any text, e.g. opt_in_TE_1550_device_myring_param1")
    form.addRow("Custom text", custom_le)
    keys = list(info["positions"])
    pos_cb = pya.QComboBox(dlg)
    pos_cb.addItems([info["positions"][k][0] for k in keys])
    pos_cb.setCurrentIndex(keys.index(info["pos_default"]) if info["pos_default"] in keys else 0)
    form.addRow("Position", pos_cb)
    rep_ck = pya.QCheckBox("Replace the label(s) already on this GC: %s" % ", ".join(info["existing"]), dlg)
    rep_ck.setChecked(True)
    if info["existing"]:
        form.addRow(rep_ck)
    preview = pya.QLabel("", dlg)
    preview.setWordWrap(True)
    form.addRow("Label", preview)

    def text():
        role = role_cb.currentIndex
        if role == 2:
            return custom_le.text.strip()
        lab = gcmod.opt_in_label(pol_cb.currentText, wl_le.text.strip() or "1550", dev_le.text.strip() or "device1",
                                 gcmod.clean_device_id(kind_le.text) or "device")
        return lab if role == 0 else gcmod.out_label(lab)

    def update(*args):
        custom = role_cb.currentIndex == 2
        for w in (pol_cb, wl_le, kind_le, dev_le):
            w.setEnabled(not custom)
        custom_le.setEnabled(custom)
        preview.setText(text() or "(empty)")
    role_cb.currentIndexChanged(update)
    pol_cb.currentIndexChanged(update)
    for le in (wl_le, kind_le, dev_le, custom_le):
        le.textChanged(update)
    update()
    row = pya.QHBoxLayout()
    ok = pya.QPushButton("Add label", dlg)
    cancel = pya.QPushButton("Cancel", dlg)
    ok.clicked(lambda *args: dlg.accept())
    cancel.clicked(lambda *args: dlg.reject())
    row.addStretch(1)
    row.addWidget(ok)
    row.addWidget(cancel)
    form.addRow(row)
    if dlg.exec_() != 1:
        return None
    return {"text": text(), "pos_key": keys[pos_cb.currentIndex], "replace": bool(info["existing"]) and rep_ck.isChecked()}


GC_LABEL_ASKER = ask_gc_label   # replaceable (tests)


def label_gc_at(view, p, d=None):
    """
    Label mode: add a test label (opt_in / out_ / custom) to the grating coupler clicked at p -
    any cell, also one klroute does not know (your own GC, another library's GC).
    """
    cv = view.active_cellview()
    d = d or dock(view, WG)
    hit = object_at(view, cv, p)
    if hit is None:
        _status("Label: nothing here - click a grating coupler")
        return None
    inst = hit[0]
    name = inst.cell.basic_name().strip()
    try:
        ctx = pya.DCplxTrans(cv.context_dtrans())
    except Exception:
        ctx = pya.DCplxTrans()
    t = ctx * inst.dcplx_trans
    positions = {}
    fbr = _layer_or(view, cv, "FbrTgt", "81/0")
    if not inst.cell.begin_shapes_rec(fbr).at_end():
        box = pya.DBox()
        it = inst.cell.begin_shapes_rec(fbr)
        while not it.at_end():
            if not it.shape().is_text():
                box += it.shape().dbbox().transformed(it.dtrans())
            it.next()
        positions["fiber"] = ("Fiber target (FbrTgt) of the GC", t * box.center())
    positions["tip"] = ("GC tip / cell origin (SiEPIC DFT rule)", t * pya.DPoint(0, 0))
    positions["click"] = ("Where I clicked", p)
    text_l = _layer_or(view, cv, "Text", "10/0")
    existing = [txt for _, _, txt in label_entries(view, cv, inst)]
    labels = gcmod.measurement_labels(cv.cell, text_l)
    dev = gcmod.clean_device_id(d.devid_le.text) or gcmod.next_device_id(labels)
    # a device that already has an input label -> suggest an output label for it
    role = "in"
    last = getattr(label_gc_at, "last_dev", None)
    if last and any(e["deviceID"] == last for e in labels) and not d.devid_le.text.strip():
        dev, role = last, "out"
    default = P.get("label_at", "fiber")
    info = {"name": name, "positions": positions,
            "pos_default": "tip" if default == "gc_origin" else ("fiber" if "fiber" in positions else "tip"),
            "pol": gcmod.polarization_for(name, d.pol_cb.currentText), "wl": gcmod.wavelength_for(name),
            "dev": dev, "role": role, "existing": existing}
    ans = GC_LABEL_ASKER(info)
    if ans is None:
        return None
    if not ans["text"]:
        _error("The label is empty.")
        return None
    pos = positions[ans["pos_key"]][1]
    view.transaction("Label grating coupler")
    try:
        if ans["replace"]:
            for shapes, sh, _ in label_entries(view, cv, inst):
                shapes.erase(sh)
        holder, how, _ = attach_label(view, cv, inst, pos, ans["text"])
    finally:
        view.commit()
    view.add_missing_layers()
    if ans["text"].startswith("opt_in_"):
        label_gc_at.last_dev = ans["text"].split("_", 5)[-1] if ans["text"].count("_") >= 5 else None
    msg = "Label '%s' on %s at (%.3f, %.3f) - %s, moves with it" % (
        ans["text"], name, pos.x, pos.y,
        "kept inside cell %s" % holder.name if how == "inside" else "grouped with it in cell %s" % holder.name)
    if ans["text"].startswith(("opt_in_", "out_")):
        msg += "\n" + _coord_line(view, cv, ans["text"] if ans["text"].startswith("opt_in_")
                                   else "opt_in_" + ans["text"][4:])
    d.info_lbl.setText(msg)
    _status(msg.split("\n")[0])
    return ans["text"]


PAD_NAMES = ["G", "S", "G2", "S2", "P", "N", "HEATER+", "HEATER-", "GND", "SIG"]


def ask_elec_label(info):
    """
    Dialog for an electrical test label.  ``info``: name, positions {key: (title, point)},
    pos_default, dev, pad, existing.  Returns {"text", "pos_key", "replace"} or None.
    """
    mw = pya.Application.instance().main_window()
    dlg = pya.QDialog(mw)
    dlg.setWindowTitle("Electrical label on %s" % info["name"])
    form = pya.QFormLayout(dlg)
    role_cb = pya.QComboBox(dlg)
    role_cb.addItems(["Electrical pad (elec_<ID>_<pad>)", "Custom text"])
    form.addRow("Label type", role_cb)
    dev_le = pya.QLineEdit(dlg)
    dev_le.setText(info["dev"])
    form.addRow("Device ID", dev_le)
    pad_cb = pya.QComboBox(dlg)
    pad_cb.setEditable(True)
    pad_cb.addItems(PAD_NAMES)
    pad_cb.setEditText(info["pad"])
    form.addRow("Pad name", pad_cb)
    custom_le = pya.QLineEdit(dlg)
    custom_le.setPlaceholderText("any text, e.g. elec_ring1_HEATER+ or VDD")
    form.addRow("Custom text", custom_le)
    keys = list(info["positions"])
    pos_cb = pya.QComboBox(dlg)
    pos_cb.addItems([info["positions"][k][0] for k in keys])
    pos_cb.setCurrentIndex(keys.index(info["pos_default"]) if info["pos_default"] in keys else 0)
    form.addRow("Position", pos_cb)
    rep_ck = pya.QCheckBox("Replace the label(s) already here: %s" % ", ".join(info["existing"]), dlg)
    rep_ck.setChecked(True)
    if info["existing"]:
        form.addRow(rep_ck)
    preview = pya.QLabel("", dlg)
    form.addRow("Label", preview)

    def text():
        if role_cb.currentIndex == 1:
            return custom_le.text.strip()
        return gcmod.elec_label(dev_le.text.strip() or "device1", pad_cb.currentText.strip() or "G")

    def update(*args):
        custom = role_cb.currentIndex == 1
        dev_le.setEnabled(not custom)
        pad_cb.setEnabled(not custom)
        custom_le.setEnabled(custom)
        preview.setText(text() or "(empty)")
    role_cb.currentIndexChanged(update)
    pad_cb.editTextChanged(update)
    for le in (dev_le, custom_le):
        le.textChanged(update)
    update()
    row = pya.QHBoxLayout()
    ok = pya.QPushButton("Add label", dlg)
    cancel = pya.QPushButton("Cancel", dlg)
    ok.clicked(lambda *args: dlg.accept())
    cancel.clicked(lambda *args: dlg.reject())
    row.addStretch(1)
    row.addWidget(ok)
    row.addWidget(cancel)
    form.addRow(row)
    if dlg.exec_() != 1:
        return None
    return {"text": text(), "pos_key": keys[pos_cb.currentIndex], "replace": bool(info["existing"]) and rep_ck.isChecked()}


ELEC_LABEL_ASKER = ask_elec_label   # replaceable (tests)


def _metal_island_at(view, cv, p):
    """(layer name, display bbox) of the metal shape (merged island) under display point p, or None."""
    try:
        ctx = pya.DCplxTrans(cv.context_dtrans())
    except Exception:
        ctx = pya.DCplxTrans()
    dbu = cv.layout().dbu
    q = ctx.inverted() * p
    probe = pya.Region(pya.DBox(q, q).enlarged(pya.DVector(0.01, 0.01)).to_itype(dbu))
    around = pya.DBox(q, q).enlarged(pya.DVector(500.0, 500.0))
    for name in tech_metals(view, cv):
        try:
            li = resolve_layer(view, cv, name)
        except RouteError:
            continue
        isl = pya.Region(cv.cell.begin_shapes_rec_touching(li, around)).merged().interacting(probe)
        if not isl.is_empty():
            return name, isl.bbox().to_dtype(dbu).transformed(ctx)
    return None


def label_elec_at(view, p, d=None):
    """Metal Label mode: add an electrical test label (elec_<deviceID>_<pad>) on the pad / wire clicked at p."""
    cv = view.active_cellview()
    d = d or dock(view, METAL)
    island = _metal_island_at(view, cv, p)
    hit = object_at(view, cv, p)
    if island is None and hit is None:
        _status("Label: no metal or component here - click a pad, wire or contact")
        return None
    positions = {}
    if island is not None:
        positions["metal"] = ("Centre of the %s metal under the click" % island[0], island[1].center())
    if hit is not None:
        positions["inst"] = ("Centre of %s" % hit[0].cell.basic_name(), hit[1].center())
    positions["click"] = ("Where I clicked", p)
    name = hit[0].cell.basic_name() if hit is not None else island[0]
    text_l = _layer_or(view, cv, "Text", "10/0")
    zone = island[1] if island is not None else hit[1]
    here = label_entries(view, cv, hit[0] if hit is not None else None, zone)
    rows = gcmod.elec_rows(cv.cell, text_l)
    dev = gcmod.clean_device_id(d.devid_le.text) or getattr(label_elec_at, "last_dev", None) or \
        getattr(label_gc_at, "last_dev", None) or "device1"
    used = {r["pad"] for r in rows if r["device"] == dev}
    pad = next((n for n in PAD_NAMES if n not in used), "P%d" % (len(used) + 1))
    info = {"name": name, "positions": positions, "pos_default": next(iter(positions)), "dev": dev, "pad": pad,
            "existing": [txt for _, _, txt in here]}
    ans = ELEC_LABEL_ASKER(info)
    if ans is None:
        return None
    if not ans["text"]:
        _error("The label is empty.")
        return None
    pos = positions[ans["pos_key"]][1]
    _, to_cell = _cellview(view)
    how, holder = "free", None
    view.transaction("Electrical label")
    try:
        if ans["replace"]:
            for shapes, sh, _ in here:
                shapes.erase(sh)
        if hit is not None:
            holder, how, _ = attach_label(view, cv, hit[0], pos, ans["text"])
        else:
            gcmod.add_opt_in(cv.cell, text_l, pos, ans["text"], to_cell)   # bare metal: no cell to move with
    finally:
        view.commit()
    view.add_missing_layers()
    if ans["text"].startswith("elec_") and ans["text"].count("_") >= 2:
        label_elec_at.last_dev = ans["text"].split("_", 2)[1]
    pads = ["%s (%.0f, %.0f)" % (r["pad"], r["x"], r["y"]) for r in gcmod.elec_rows(cv.cell, text_l)
            if ans["text"].startswith("elec_") and r["device"] == ans["text"].split("_", 2)[1]]
    msg = "Label '%s' at (%.3f, %.3f) - %s" % (
        ans["text"], pos.x, pos.y,
        "kept inside cell %s, moves with it" % holder.name if how == "inside" else
        ("grouped with the pad in cell %s, moves with it" % holder.name if how == "grouped" else
         "free text (metal drawn directly in this cell)"))
    if pads:
        msg += "\nElectrical pads of %s: %s" % (ans["text"].split("_", 2)[1], ", ".join(pads))
    d.info_lbl.setText(msg)
    _status(msg.split("\n")[0])
    return ans["text"]


def is_gc(view, cv, inst):
    """A grating coupler: a cell with a fiber target (FbrTgt) shape."""
    fbr = _layer_or(view, cv, "FbrTgt", "81/0")
    return not inst.cell.begin_shapes_rec(fbr).at_end()


def gc_fiber(view, cv, inst):
    """Label point of a placed GC (display coordinates): its fiber-target centre, or with the
    SiEPIC profile its tip (cell origin), where SiEPIC's DFT rules want the opt_in label."""
    if P.get("label_at") == "gc_origin":
        try:
            ctx = pya.DCplxTrans(cv.context_dtrans())
        except Exception:
            ctx = pya.DCplxTrans()
        return (ctx * inst.dcplx_trans) * pya.DPoint(0, 0)
    fbr = _layer_or(view, cv, "FbrTgt", "81/0")
    try:
        ctx = pya.DCplxTrans(cv.context_dtrans())
    except Exception:
        ctx = pya.DCplxTrans()
    box = pya.DBox()
    it = inst.cell.begin_shapes_rec(fbr)
    while not it.at_end():
        if not it.shape().is_text():
            box += it.shape().dbbox().transformed(it.dtrans())
        it.next()
    c = box.center() if not box.empty() else inst.cell.dbbox().center()
    return (ctx * inst.dcplx_trans) * c


def _side(u):
    return {"E": "east", "W": "west", "N": "north", "S": "south"}[_dir_letter(u)]


ROLES = ["No label", "Input (opt_in)", "Output (out_)"]
ROLE_KEYS = [None, "in", "out"]


def ask_link(first, second, labels, dev_id, types=None, bends=None):
    """
    One dialog per chain link.
    first / second : (component name, free ports, suggested port)
    labels         : {"A" or "B": (GC name, suggested role None/"in"/"out")} for the GCs of the link
    dev_id         : suggested device ID
    types          : (title, choices, current) - the PDK waveguide type (or PDK wire cell) of this link
    bends          : (title, choices, current) - the bend cell of this link (waveguides)
    Returns {"pa", "pb", "roles": {"A"/"B": role}, "dev", "type"} or None if cancelled.
    """
    mw = pya.Application.instance().main_window()
    dlg = pya.QDialog(mw)
    dlg.setWindowTitle("Route chain - link")
    form = pya.QFormLayout(dlg)

    def port_combo(ports, default):
        c = pya.QComboBox(dlg)
        for q in ports:
            c.addItem("%s   (%s side, x=%.3f, y=%.3f)%s" % (q["name"], _side(q["dir"]), q["pos"].x, q["pos"].y,
                                                         "   - suggested" if q is default else ""))
        c.setCurrentIndex(ports.index(default))
        c.setEnabled(len(ports) > 1)
        return c

    type_cb = None
    if types and types[1]:
        type_cb = pya.QComboBox(dlg)
        type_cb.addItems(list(types[1]))
        type_cb.setCurrentIndex(max(0, list(types[1]).index(types[2]) if types[2] in types[1] else 0))
        form.addRow(types[0], type_cb)
    bend_cb = None
    if bends and bends[1]:
        bend_cb = pya.QComboBox(dlg)
        bend_cb.addItems(list(bends[1]))
        bend_cb.setCurrentIndex(list(bends[1]).index(bends[2]) if bends[2] in bends[1] else 0)
        form.addRow(bends[0], bend_cb)
    ca = port_combo(first[1], first[2])
    form.addRow("1st: %s  port" % first[0], ca)
    cb = port_combo(second[1], second[2])
    form.addRow("2nd: %s  port" % second[0], cb)

    role_combos = {}
    for tag in ("A", "B"):
        if tag in labels:
            name, role = labels[tag]
            c = pya.QComboBox(dlg)
            c.addItems(ROLES)
            c.setCurrentIndex(ROLE_KEYS.index(role))
            form.addRow("Label at %s (%s)" % (name, "1st" if tag == "A" else "2nd"), c)
            role_combos[tag] = c
    dev_le = None
    if role_combos:
        dev_le = pya.QLineEdit(dlg)
        dev_le.setText(dev_id)
        form.addRow("Device ID", dev_le)
    else:
        form.addRow(pya.QLabel("No grating coupler in this link - no label.", dlg))

    row = pya.QHBoxLayout()
    ok = pya.QPushButton("Connect", dlg)
    cancel = pya.QPushButton("Cancel", dlg)
    ok.clicked(lambda *args: dlg.accept())
    cancel.clicked(lambda *args: dlg.reject())
    row.addStretch(1)
    row.addWidget(ok)
    row.addWidget(cancel)
    form.addRow(row)
    if dlg.exec_() != 1:
        return None
    return {"pa": first[1][ca.currentIndex], "pb": second[1][cb.currentIndex],
            "roles": {tag: ROLE_KEYS[c.currentIndex] for tag, c in role_combos.items()},
            "dev": gcmod.clean_device_id(dev_le.text) if dev_le is not None else dev_id,
            "type": type_cb.currentText if type_cb is not None else None,
            "bend": bend_cb.currentText if bend_cb is not None else None}


LINK_ASKER = ask_link   # replaceable (tests)


def pad_ports(view, cv, comp, layers):
    """
    Virtual electrical ports for a component without PinRecM pins (e.g. a bond pad): the middle
    of each side of its metal on the route's layer(s) (its bounding box if it has none there),
    pointing outwards.
    """
    dbu = cv.layout().dbu
    try:
        ctx = pya.DCplxTrans(cv.context_dtrans())
    except Exception:
        ctx = pya.DCplxTrans()
    inst, box = comp
    metal = pya.Region()
    for li in layers:
        metal += pya.Region(inst.cell.begin_shapes_rec(li)).transformed(inst.cplx_trans)
    b = metal.bbox().to_dtype(dbu).transformed(ctx) if not metal.is_empty() else box
    c = b.center()
    name = inst.cell.basic_name().strip()
    return [{"pos": pya.DPoint(b.right, c.y), "dir": (1.0, 0.0), "name": "east edge", "device": name, "origin": c},
            {"pos": pya.DPoint(c.x, b.top), "dir": (0.0, 1.0), "name": "north edge", "device": name, "origin": c},
            {"pos": pya.DPoint(b.left, c.y), "dir": (-1.0, 0.0), "name": "west edge", "device": name, "origin": c},
            {"pos": pya.DPoint(c.x, b.bottom), "dir": (0.0, -1.0), "name": "south edge", "device": name, "origin": c}]


def connect_link(view, a, b, d=None, labels=None, dev_id=""):
    """
    Link two components with one route: by default the pair of free ports (one on each)
    that gives no route through either component and the shortest route.
    a, b   : (instance, display bbox)
    labels : {"A"/"B": (GC name, suggested role)} for the GCs in this link
    With 'ask' on, a dialog lets the user pick both ports and the labels.
    Returns (Route, port_a, port_b, roles, device_id) or None.
    """
    labels = labels or {}
    d = d or dock(view)
    d.save()
    cv = view.active_cellview()
    try:
        spec = d.spec()
        dbu = cv.layout().dbu
        metal = not spec.is_waveguide
        metal_layers = [li for li, _, _ in _route_parts(view, cv, d, None, spec)] if metal else None
        if metal:
            labels = {}   # test labels are for grating couplers only
            pa_list = device_ports(view, cv, a[1], metal=True) or pad_ports(view, cv, a, metal_layers)
            pb_list = device_ports(view, cv, b[1], metal=True) or pad_ports(view, cv, b, metal_layers)
            bodies = []   # metal may run over its own devices (e.g. to a heater contact)
        else:
            pa_list = device_ports(view, cv, a[1])
            pb_list = device_ports(view, cv, b[1])
            for comp, ports in ((a, pa_list), (b, pb_list)):
                if not ports:
                    raise RouteError("%s has no free optical port (all ports already connected?)."
                                     % comp[0].cell.basic_name())
            bodies = [pya.Region(c[1].enlarged(pya.DVector(-0.5, -0.5)).to_itype(dbu)) for c in (a, b)]

        def pick(pa_list, pb_list):
            best = None
            for pa in pa_list:
                for pb in pb_list:
                    try:
                        rt = route(pa["pos"], pb["pos"], spec, _dir_letter(pa["dir"]), _dir_letter(pb["dir"]))
                    except RouteError:
                        continue
                    pts = [pya.DPoint(q.x, q.y) for q in rt.waypoints]
                    inner = pts[1:-1]
                    hits = 0
                    if len(inner) >= 2:
                        mid = pya.Region(pya.DPath(inner, 0.1).polygon().to_itype(dbu))
                        hits = sum(0 if (mid & body).is_empty() else 1 for body in bodies)
                    score = (hits, rt.length)
                    if best is None or score < best[0]:
                        best = (score, pts, pa, pb)
            return best

        best = pick(pa_list, pb_list)
        roles = {tag: role for tag, (_, role) in labels.items()} if d.label_ck.isChecked() else {}
        if d.askpin_ck.isChecked():
            combo = d.wire_cb if metal else d.preset_cb
            choices = [combo.itemText(k) for k in range(combo.count)]
            types = ("PDK wire cell" if metal else "PDK waveguide", choices, combo.currentText)
            bend_choices = [d.bendcell_cb.itemText(k) for k in range(d.bendcell_cb.count)]
            ans = LINK_ASKER((a[0].cell.basic_name(), pa_list, best[2] if best else pa_list[0]),
                             (b[0].cell.basic_name(), pb_list, best[3] if best else pb_list[0]),
                             labels if d.label_ck.isChecked() else {}, dev_id, types=types,
                             bends=None if metal else ("Bend cell", bend_choices, d.bendcell_cb.currentText))
            if ans is None:
                _status("Route chain: link cancelled")
                return None
            changed = False
            if ans.get("type") and ans["type"] != combo.currentText and ans["type"] in choices:
                # this link (and the next ones, until changed again) uses the chosen type
                combo.setCurrentIndex(choices.index(ans["type"]))
                changed = True
            if ans.get("bend") and ans["bend"] != d.bendcell_cb.currentText and ans["bend"] in bend_choices:
                d.bendcell_cb.setCurrentIndex(bend_choices.index(ans["bend"]))   # bend cell for this link and on
                changed = True
            if changed:
                spec = d.spec()
                if metal:
                    metal_layers = [li for li, _, _ in _route_parts(view, cv, d, None, spec)]
            best = pick([ans["pa"]], [ans["pb"]])
            roles = ans["roles"]
            dev_id = ans["dev"] or dev_id
        if best is None:
            raise RouteError("No route fits between %s and %s (bend radius %g um). Move them further apart."
                             % (a[0].cell.basic_name(), b[0].cell.basic_name(), spec.bend_radius))
    except RouteError as e:
        _error(str(e))
        return None
    (hits, _), pts, pa, pb = best
    try:
        pts, moved = avoid_points(view, cv, d, spec, pts, (pa["pos"], pa["dir"]), (pb["pos"], pb["dir"]),
                                  metal_layers)
        if moved:
            hits = 0
    except RouteError as e:
        _error(str(e))
        return None
    rt = create_route(view, pts, d, through=True)
    if rt is None:
        return None
    if hits:
        _status("Route chain: the link %s.%s -> %s.%s runs through a component - move it and route again"
                % (pa["device"], pa["name"], pb["device"], pb["name"]))
    return rt, pa, pb, roles, dev_id


def device_id_for(view, cv, d, device):
    """Device ID for labels: the panel's Device ID, else <cell>-X<x>-Y<y> of the device instance."""
    dev = gcmod.clean_device_id(d.devid_le.text)
    if dev:
        return dev
    if device is None:
        return gcmod.next_device_id(gcmod.measurement_labels(cv.cell, _layer_or(view, cv, "Text", "10/0")))
    try:
        ctx = pya.DCplxTrans(cv.context_dtrans())
    except Exception:
        ctx = pya.DCplxTrans()
    o = (ctx * device[0].dcplx_trans) * pya.DPoint(0, 0)
    return gcmod.clean_device_id("%s-X%d-Y%d" % (device[0].cell.basic_name(), int(round(o.x)), int(round(o.y))))


def _profile_layers(view, cv, key):
    return [_layer_or(view, cv, name, fb) for name, fb in P[key]]


def _pin_layers(view, cv):
    lay = lambda name, fb: _layer_or(view, cv, name, fb)
    core = _profile_layers(view, cv, "core") + _profile_layers(view, cv, "core_extra")
    metal = _profile_layers(view, cv, "contact_metals")
    return core, metal, lay("PinRec", "1/10"), lay("PinRecM", "1/11")


def pin_candidate(view, cv, p, radius):
    """
    What a click at display point ``p`` would get a pin on: the nearest open waveguide end
    within ``radius`` (anywhere, not only on a device's outer edge), else the metal contact
    under the click.  The pin goes into the CURRENT cell (the cell shown in the view, e.g.
    your device opened with 'Descend' / 'Show As New Top'), in that cell's coordinates;
    the shapes of its sub-cells count too.
    Returns a dict (cell, trans cell->display, kind, pos, dir, width) or None.
    """
    try:
        ctx = pya.DCplxTrans(cv.context_dtrans())
    except Exception:
        ctx = pya.DCplxTrans()
    cell, t = cv.cell, ctx
    if cell is None:
        return None
    dbu = cell.layout().dbu
    q = t.inverted() * p
    rad = radius / max(t.mag, 1e-9)
    core, metal, pin_l, pinm_l = _pin_layers(view, cv)
    near = pya.DBox(q, q).enlarged(pya.DVector(rad + 5.0, rad + 5.0))
    best = None
    for c, u, w in pinmod.waveguide_ends(cell, core, boundary_only=False, window=near):
        dist = c.distance(q)
        if dist <= rad and (best is None or dist < best[0]):
            best = (dist, c, u, w)
    if best is not None:
        return {"cell": cell, "trans": t, "kind": "optical", "pos": best[1], "dir": best[2], "width": best[3]}
    # metal contact under the click: centre of its metal island, pointing to the nearest side of the cell
    outline = cell.dbbox()
    probe = pya.Region(pya.DBox(q, q).enlarged(pya.DVector(0.01, 0.01)).to_itype(dbu))
    around = pya.DBox(q, q).enlarged(pya.DVector(200.0, 200.0))
    for li in metal:
        islands = pya.Region(cell.begin_shapes_rec_touching(li, around)).merged().interacting(probe)
        for poly in islands.each():
            b = poly.bbox().to_dtype(dbu)
            c = b.center()
            dist = {"W": c.x - outline.left, "E": outline.right - c.x,
                    "S": c.y - outline.bottom, "N": outline.top - c.y}
            side = min(dist, key=dist.get)
            w = b.height() if side in "EW" else b.width()
            return {"cell": cell, "trans": t, "kind": "electrical", "pos": c, "dir": pinmod.SIDES[side],
                    "width": max(min(w, 10.0), 0.5)}
    return None


def _next_pin_name(cell, layer, prefix):
    used = {nm for _, nm in pinmod.existing_pins(cell, layer)}
    k = 1
    while "%s%d" % (prefix, k) in used:
        k += 1
    return "%s%d" % (prefix, k)


def ask_pin_details(cand, name):
    """Dialog: name, type, direction and width of the new pin. Returns (name, kind, dir, width) or None."""
    mw = pya.Application.instance().main_window()
    dlg = pya.QDialog(mw)
    dlg.setWindowTitle("Add pin to %s" % cand["cell"].name)
    form = pya.QFormLayout(dlg)
    form.addRow(pya.QLabel("At x=%.3f, y=%.3f in cell %s" % (cand["pos"].x, cand["pos"].y, cand["cell"].name), dlg))
    name_le = pya.QLineEdit(dlg)
    name_le.setText(name)
    form.addRow("Name", name_le)
    kind_cb = pya.QComboBox(dlg)
    kind_cb.addItems(["optical (PinRec)", "electrical (PinRecM)"])
    kind_cb.setCurrentIndex(0 if cand["kind"] == "optical" else 1)
    form.addRow("Type", kind_cb)
    dir_cb = pya.QComboBox(dlg)
    sides = ["E", "N", "W", "S"]
    dir_cb.addItems(["E (east, +x)", "N (north, +y)", "W (west, -x)", "S (south, -y)"])
    dir_cb.setCurrentIndex(sides.index(pinmod.side_of(cand["dir"])))
    form.addRow("Points towards", dir_cb)
    w_sb = pya.QDoubleSpinBox(dlg)
    w_sb.setRange(0.01, 1000.0)
    w_sb.setDecimals(3)
    w_sb.setValue(cand["width"])
    form.addRow("Width [um]", w_sb)
    row = pya.QHBoxLayout()
    ok = pya.QPushButton("Add pin", dlg)
    cancel = pya.QPushButton("Cancel", dlg)
    ok.clicked(lambda *args: dlg.accept())
    cancel.clicked(lambda *args: dlg.reject())
    row.addStretch(1)
    row.addWidget(ok)
    row.addWidget(cancel)
    form.addRow(row)
    if dlg.exec_() != 1:
        return None
    kind = "optical" if kind_cb.currentIndex == 0 else "electrical"
    return name_le.text.strip(), kind, pinmod.SIDES[sides[dir_cb.currentIndex]], w_sb.value


PIN_ASKER = ask_pin_details   # replaceable (tests)


def add_pin_at(view, p, radius):
    """Pin click mode: add one pin at the waveguide end / contact clicked at display point p."""
    cv = view.active_cellview()
    cand = pin_candidate(view, cv, p, radius)
    if cand is None:
        _status("Pin: no waveguide end or metal contact here - click closer to a waveguide end or on a contact")
        return None
    cell = cand["cell"]
    if cell.is_library_cell() or cell.is_pcell_variant():
        _error("The current cell '%s' is a PDK / library cell and cannot be edited.\n"
               "Make your own cell the current cell first (right-click it in the cell list > Show As New Top, "
               "or Descend into it)." % cell.basic_name())
        return None
    core, metal, pin_l, pinm_l = _pin_layers(view, cv)
    for layer in (pin_l, pinm_l):
        for c, nm in pinmod.existing_pins(cell, layer):
            if c.distance(cand["pos"]) < 0.05:
                _error("There is already a pin '%s' here." % nm)
                return None
    layer = pin_l if cand["kind"] == "optical" else pinm_l
    name = _next_pin_name(cell, layer, "opt" if cand["kind"] == "optical" else "elec")
    ans = PIN_ASKER(cand, name)
    if ans is None:
        return None
    name, kind, u, width = ans
    if not name:
        _error("A pin needs a name.")
        return None
    layer = pin_l if kind == "optical" else pinm_l
    view.transaction("Add pin")
    try:
        pinmod.add_pin(cell, layer, name, cand["pos"], u, width)
    finally:
        view.commit()
    view.add_missing_layers()
    msg = "Added %s pin '%s' to the current cell %s at (%.3f, %.3f), pointing %s" % (kind, name, cell.name, cand["pos"].x,
                                                                  cand["pos"].y, pinmod.side_of(u))
    dock(view).info_lbl.setText(msg)
    _status(msg)
    return name


def add_device_pins():
    """
    Add SiEPIC pins to your own device: optical pins (PinRec) at the open waveguide ends on
    its outer edge and electrical pins (PinRecM) on each metal contact (the DevRec box
    is a separate command: add_devrec_box).  Works on the selected instance's cell, else on the current cell.  A list of the
    proposed pins is shown first; untick the ones you do not want.
    """
    view = pya.LayoutView.current()
    if view is None:
        _error("Open a layout first.")
        return
    cv = view.active_cellview()
    ly = cv.layout()
    sel = [o for o in view.each_object_selected() if o.is_cell_inst()]
    cell = sel[0].inst().cell if len(sel) == 1 else cv.cell
    if cell.is_library_cell() or cell.is_pcell_variant():
        _error("'%s' is a PDK / library cell - it already has its pins and cannot be edited here.\n"
               "This command is for your own (static) device cells." % cell.basic_name())
        return
    lay = lambda name, fb: _layer_or(view, cv, name, fb)
    core = _profile_layers(view, cv, "core")
    if all(pya.Region(cell.begin_shapes_rec(li)).is_empty() for li in core):
        core += _profile_layers(view, cv, "core_extra")
    metal = _profile_layers(view, cv, "contact_metals")
    pin_l, pinm_l, devrec_l = lay("PinRec", "1/10"), lay("PinRecM", "1/11"), lay("DevRec", "68/0")
    props = pinmod.propose_pins(cell, core, metal, pin_l, pinm_l)

    mw = pya.Application.instance().main_window()
    dlg = pya.QDialog(mw)
    dlg.setWindowTitle("Add pins to %s" % cell.name)
    v = pya.QVBoxLayout(dlg)
    v.addWidget(pya.QLabel("Pins found in '%s' (ticked ones are added; double-click a row to rename):" % cell.name, dlg))
    lst = pya.QListWidget(dlg)
    items = []          # keep the items alive (and in order) on the Python side
    for q in props:
        it = pya.QListWidgetItem("%s" % q["name"])
        lst.addItem(it)
        items.append(it)
        it.setToolTip("%s pin" % q["kind"])
        it.setText("%s   (%s, %s side, x=%.3f, y=%.3f, width %.3g)" % (
            q["name"], q["kind"], pinmod.side_of(q["dir"]), q["pos"].x, q["pos"].y, q["width"]))
        it.setFlags(it.flags | pya.Qt.ItemIsUserCheckable | pya.Qt.ItemIsEditable)
        it.setCheckState(pya.Qt.Checked)
    v.addWidget(lst)
    row = pya.QHBoxLayout()
    ok = pya.QPushButton("Add", dlg)
    cancel = pya.QPushButton("Cancel", dlg)
    ok.clicked(lambda *args: dlg.accept())
    cancel.clicked(lambda *args: dlg.reject())
    row.addStretch(1)
    row.addWidget(ok)
    row.addWidget(cancel)
    v.addLayout(row)
    if not props:
        v.insertWidget(1, pya.QLabel("No new ports found (all ends / contacts already have pins).", dlg))
    if dlg.exec_() != 1:
        return

    view.transaction("Add device pins")
    added = []
    try:
        for k, q in enumerate(props):
            it = items[k]
            if it.checkState != pya.Qt.Checked:
                continue
            name = it.text.split("(")[0].strip() or q["name"]
            pinmod.add_pin(cell, q["layer"], name, q["pos"], q["dir"], q["width"])
            added.append(name)
    finally:
        view.commit()
    view.add_missing_layers()
    msg = "Added %d pin(s) to %s: %s" % (len(added), cell.name, ", ".join(added) or "-")
    if pya.Region(cell.begin_shapes_rec(devrec_l)).is_empty():
        msg += "\nThe cell has no DevRec yet - use 'Add DevRec box...'"
    dock(view).info_lbl.setText(msg)
    _status(msg)


DEVREC_SKIP = (("PinRec", "1/10"), ("PinRecM", "1/11"), ("DevRec", "68/0"), ("Text", "10/0"))


def devrec_outline(view, cv, cell, margin=0.0):
    """Bounding box of the device (all layers except pins, DevRec and text), grown by ``margin``."""
    skip = {_layer_or(view, cv, n, fb) for n, fb in DEVREC_SKIP}
    box = pya.DBox()
    for li in cell.layout().layer_indexes():
        if li not in skip:
            b = cell.dbbox_per_layer(li)
            if not b.empty():
                box += b
    if box.empty():
        return box
    return box.enlarged(pya.DVector(margin, margin))


def add_devrec_box():
    """
    Add a DevRec box (SiEPIC device outline) to your own device: the selected instance's cell,
    else the current cell.  The box is the device's extent (pins, DevRec and texts not counted)
    plus an optional margin; an existing DevRec in the cell can be replaced.
    """
    view = pya.LayoutView.current()
    if view is None:
        _error("Open a layout first.")
        return
    cv = view.active_cellview()
    sel = [o for o in view.each_object_selected() if o.is_cell_inst()]
    cell = sel[0].inst().cell if len(sel) == 1 else cv.cell
    if cell is None:
        _error("Open a layout first.")
        return
    if cell.is_library_cell() or cell.is_pcell_variant():
        _error("'%s' is a PDK / library cell - it already has its DevRec and cannot be edited here." % cell.basic_name())
        return
    devrec_l = _layer_or(view, cv, "DevRec", "68/0")
    own = [sh for sh in cell.shapes(devrec_l).each()]
    below = 0 if own else (0 if pya.Region(cell.begin_shapes_rec(devrec_l)).is_empty() else 1)
    if devrec_outline(view, cv, cell).empty():
        _error("'%s' has no shapes to put a DevRec box around." % cell.name)
        return
    ans = DEVREC_ASKER(view, cv, cell, len(own), below)
    if ans is None:
        return
    margin, replace = ans
    box = devrec_outline(view, cv, cell, margin)
    view.transaction("Add DevRec box")
    try:
        if replace:
            for sh in own:
                cell.shapes(devrec_l).erase(sh)
        cell.shapes(devrec_l).insert(box)
    finally:
        view.commit()
    view.add_missing_layers()
    msg = "DevRec box added to %s: (%.3f, %.3f) - (%.3f, %.3f), %.3f x %.3f um%s" % (
        cell.name, box.left, box.bottom, box.right, box.top, box.width(), box.height(),
        ("; %d old DevRec shape(s) replaced" % len(own)) if replace and own else "")
    dock(view).info_lbl.setText(msg)
    _status(msg)
    return box


def ask_devrec(view, cv, cell, n_own, n_below):
    """Dialog: margin and whether to replace an existing DevRec. Returns (margin, replace) or None."""
    mw = pya.Application.instance().main_window()
    dlg = pya.QDialog(mw)
    dlg.setWindowTitle("Add DevRec box to %s" % cell.name)
    form = pya.QFormLayout(dlg)
    lbl = pya.QLabel("", dlg)
    form.addRow(lbl)
    m_sb = pya.QDoubleSpinBox(dlg)
    m_sb.setRange(0.0, 1000.0)
    m_sb.setDecimals(3)
    m_sb.setValue(float(_cfg_get("devrec_margin", "0")))
    form.addRow("Margin around the device [um]", m_sb)

    def show(*args):
        b = devrec_outline(view, cv, cell, m_sb.value)
        lbl.setText("Box: (%.3f, %.3f) - (%.3f, %.3f), %.3f x %.3f um" % (b.left, b.bottom, b.right, b.top,
                                                                         b.width(), b.height()))
    m_sb.valueChanged(show)
    show()
    rep_ck = pya.QCheckBox("Replace the %d DevRec shape(s) already in this cell" % n_own, dlg)
    rep_ck.setChecked(True)
    if n_own:
        form.addRow(rep_ck)
    elif n_below:
        form.addRow(pya.QLabel("Note: sub-cells of '%s' already have DevRec shapes (kept)." % cell.name, dlg))
    row = pya.QHBoxLayout()
    ok = pya.QPushButton("Add DevRec", dlg)
    cancel = pya.QPushButton("Cancel", dlg)
    ok.clicked(lambda *args: dlg.accept())
    cancel.clicked(lambda *args: dlg.reject())
    row.addStretch(1)
    row.addWidget(ok)
    row.addWidget(cancel)
    form.addRow(row)
    if dlg.exec_() != 1:
        return None
    _cfg_set("devrec_margin", m_sb.value)
    return m_sb.value, bool(n_own) and rep_ck.isChecked()


DEVREC_ASKER = ask_devrec   # replaceable (tests)


# ---------------------------------------------------------------------------
# your own cell libraries


def cells_to_add(view, cv):
    """Cells to put in a library: the selected instances' cells, else the cell(s) selected in the Cells tree,
    else the current cell."""
    cells, seen = [], set()
    for o in view.each_object_selected():
        if o.is_cell_inst() and o.inst().cell_index not in seen:
            seen.add(o.inst().cell_index)
            cells.append(o.inst().cell)
    if not cells:
        try:
            for path in view.selected_cells_paths(cv.index()):
                if path and path[-1] not in seen:
                    seen.add(path[-1])
                    cells.append(cv.layout().cell(path[-1]))
        except Exception:
            pass
    if not cells and cv.cell is not None:
        cells.append(cv.cell)
    return cells


def _lib_cell_name(cell):
    """A plain name for a cell in a library (PCell variants are called e.g. 'Waveguide$3')."""
    n = re.sub(r"\$\d+$", "", cell.name)
    return re.sub(r"[^A-Za-z0-9_\-.]", "_", n) or "cell"


READ_ONLY = "  (PDK / package library - read-only)"


def other_libraries(tech):
    """Libraries that are not yours (PDK, packages) available for technology ``tech``, sorted."""
    mine = set(userlib.libraries())
    names = []
    for n in pya.Library.library_names():
        if n in mine or n in names:
            continue
        try:
            lib = pya.Library.library_by_name(n, tech) if tech else pya.Library.library_by_name(n)
        except Exception:
            lib = None
        if lib is not None:
            names.append(n)
    return sorted(names, key=lambda n: (n == "Basic", n.lower()))


def _own_name(base):
    """Suggested name for your own library next to library / technology ``base``."""
    b = re.sub(r"[^A-Za-z0-9_\-]", "_", (base or "").strip()).strip("_")
    return (b + "_my_cells") if b else "my_cells"


def ask_add_to_library(view, cv, cells):
    """Dialog: library (existing or new), names in the library, technology. Returns the answers or None."""
    mw = pya.Application.instance().main_window()
    dlg = pya.QDialog(mw)
    dlg.setWindowTitle("Add to library")
    form = pya.QFormLayout(dlg)
    form.addRow(pya.QLabel("Cell(s) to add, with their sub-cells: %s" % ", ".join(c.name for c in cells), dlg))
    tech = cv.technology or ""
    mine = userlib.libraries()
    others = other_libraries(tech)
    lib_cb = pya.QComboBox(dlg)
    lib_cb.setEditable(True)
    lib_cb.addItems(mine + [n + READ_ONLY for n in others])
    lib_cb.setCurrentIndex(-1)
    lib_cb.lineEdit.setPlaceholderText("choose a library or type a new name")
    last = _cfg_get("userlib", "")
    if last in mine:
        lib_cb.setCurrentIndex(lib_cb.findText(last))
    elif not mine:
        lib_cb.setEditText(_own_name(others[0] if others else tech))
    form.addRow("Library", lib_cb)

    def picked(*args):
        # a PDK / package library cannot be written to: put the cells in your own library next to it
        t = lib_cb.currentText
        if t.endswith(READ_ONLY):
            lib_cb.setEditText(_own_name(t[:-len(READ_ONLY)]))
    lib_cb.activated(picked)
    name_les = []
    for c in cells:
        le = pya.QLineEdit(_lib_cell_name(c), dlg)
        name_les.append(le)
        form.addRow("Name of '%s' in the library" % c.name, le)
    desc_le = pya.QLineEdit(dlg)
    desc_le.setPlaceholderText("shown in the Library browser (new library)")
    form.addRow("Description", desc_le)
    tech_ck = pya.QCheckBox("Only for technology '%s'" % tech, dlg)
    tech_ck.setChecked(bool(tech))
    if tech:
        form.addRow(tech_ck)
    rep_ck = pya.QCheckBox("Replace cells of the same name already in the library", dlg)
    form.addRow(rep_ck)
    info = pya.QLabel("", dlg)
    info.setWordWrap(True)
    form.addRow(info)

    def show(*args):
        n = lib_cb.currentText.strip()
        if not n:
            info.setText("Saved in %s" % userlib.folder())
        elif n.endswith(READ_ONLY.strip()):
            info.setText("PDK and package libraries cannot be changed - your cells go to a library of your own "
                         "(it is shown next to the PDK library in the Libraries panel).")
        elif n in userlib.libraries():
            have = set(userlib.cells_in(n))
            dup = [le.text.strip() for le in name_les if le.text.strip() in have]
            m = userlib.meta(n)
            info.setText("Existing library (%d cells%s).%s" % (
                len(have), (", technology " + m["technology"]) if m.get("technology") else "",
                (" Already has: %s" % ", ".join(dup)) if dup else ""))
        else:
            info.setText(userlib.check_name(n) or "New library %s.gds in %s" % (n, userlib.folder()))
    lib_cb.editTextChanged(show)
    lib_cb.currentIndexChanged(show)
    for le in name_les:
        le.textChanged(show)
    show()
    row = pya.QHBoxLayout()
    ok = pya.QPushButton("Add to library", dlg)
    cancel = pya.QPushButton("Cancel", dlg)

    def try_accept(*args):
        n = lib_cb.currentText.strip()
        if not n:
            info.setText("⚠ Choose a library or type a name for a new one.")
        elif n.endswith(READ_ONLY.strip()):
            lib_cb.setEditText(_own_name(n[:-len(READ_ONLY.strip())]))
            info.setText("⚠ A PDK library cannot be changed - press Add again to use your own library '%s'."
                         % lib_cb.currentText)
        elif userlib.check_name(n):
            info.setText("⚠ " + userlib.check_name(n))
        elif any(not le.text.strip() for le in name_les):
            info.setText("⚠ Give every cell a name.")
        else:
            dlg.accept()
    ok.clicked(try_accept)
    cancel.clicked(lambda *args: dlg.reject())
    row.addStretch(1)
    row.addWidget(ok)
    row.addWidget(cancel)
    form.addRow(row)
    if dlg.exec_() != 1:
        return None
    name = lib_cb.currentText.strip()
    _cfg_set("userlib", name)
    return dict(library=name, names=[le.text.strip() for le in name_les], description=desc_le.text.strip(),
                technology=tech if tech and tech_ck.isChecked() else "", replace=rep_ck.isChecked())


LIBRARY_ASKER = ask_add_to_library   # replaceable (tests)


def add_to_library():
    """
    Put the selected cell(s) into one of your own libraries (new or existing): the selected instances' cells,
    else the cell(s) selected in the Cells tree, else the current cell.  They can then be placed from the
    Library browser in any layout.
    """
    view = pya.LayoutView.current()
    if view is None:
        _error("Open a layout first.")
        return
    cv = view.active_cellview()
    cells = cells_to_add(view, cv)
    if not cells:
        _error("Select an instance (or a cell in the Cells tree) to add to a library.")
        return
    ans = LIBRARY_ASKER(view, cv, cells)
    if ans is None:
        return
    try:
        names = userlib.add_cells(cells, ans["library"], ans["technology"], ans["description"],
                                  ans["names"], ans["replace"])
    except Exception as ex:
        _error(str(ex))
        return
    msg = "Added %s to library '%s' - place it from the Library browser (%s)." % (
        ", ".join(names), ans["library"], userlib._paths(ans["library"])[0])
    dock(view).info_lbl.setText(msg)
    _status(msg)
    return names


def confirm_delete_library(name, n_cells):
    """Ask before deleting a whole library. Returns True to delete."""
    ans = pya.QMessageBox.question(
        pya.Application.instance().main_window(), "Delete library",
        "Delete library '%s' with its %d cell(s)?\n\nIts files are removed from %s. Cells of it already placed "
        "in layouts are no longer updated from it. This cannot be undone." % (name, n_cells, userlib.folder()),
        pya.QMessageBox.Yes | pya.QMessageBox.No)
    return ans == pya.QMessageBox.Yes


DELETE_CONFIRM = confirm_delete_library   # replaceable (tests)


def remove_from_library():
    """Dialog: pick one of your libraries, then delete cells from it or delete the whole library."""
    libs = userlib.libraries()
    if not libs:
        _error("You have no libraries yet - use 'Add Selected Cell to Library...' first.")
        return
    mw = pya.Application.instance().main_window()
    dlg = pya.QDialog(mw)
    dlg.setWindowTitle("Remove cells / delete a library")
    form = pya.QFormLayout(dlg)
    lib_cb = pya.QComboBox(dlg)
    lib_cb.addItems(libs)
    form.addRow("Library", lib_cb)
    lst = pya.QListWidget(dlg)
    lst.setSelectionMode(pya.QAbstractItemView.MultiSelection)
    form.addRow("Cells to remove", lst)

    def fill(*args):
        lst.clear()
        lst.addItems(userlib.cells_in(lib_cb.currentText))
    lib_cb.currentIndexChanged(fill)
    fill()
    row = pya.QHBoxLayout()
    del_btn = pya.QPushButton("Delete whole library...", dlg)
    ok = pya.QPushButton("Remove selected cells", dlg)
    cancel = pya.QPushButton("Cancel", dlg)

    def delete_lib(*args):
        if DELETE_CONFIRM(lib_cb.currentText, lst.count):
            dlg.done(2)
    del_btn.clicked(delete_lib)
    ok.clicked(lambda *args: dlg.accept())
    cancel.clicked(lambda *args: dlg.reject())
    row.addWidget(del_btn)
    row.addStretch(1)
    row.addWidget(ok)
    row.addWidget(cancel)
    form.addRow(row)
    res = dlg.exec_()
    name = lib_cb.currentText
    if res == 2:
        try:
            userlib.delete_library(name)
        except Exception as ex:
            _error(str(ex))
            return
        _cfg_set("userlib", "")
        _status("Deleted library '%s'." % name)
        return
    if res != 1:
        return
    gone = [it.text for it in lst.selectedItems()]
    if gone:
        try:
            userlib.remove_cells(name, gone)
        except Exception as ex:
            _error(str(ex))
            return
        _status("Removed %s from library '%s'." % (", ".join(gone), name))


def show_test_coordinates():
    """SiEPIC-style automated measurement coordinates of the current cell; optionally save them."""
    view = pya.LayoutView.current()
    if view is None:
        _error("Open a layout first.")
        return
    cv = view.active_cellview()
    text_l = _layer_or(view, cv, "Text", "10/0")
    rows = gcmod.test_coordinate_rows(cv.cell, text_l)
    txt = gcmod.format_test_coordinates(rows)
    erows = gcmod.elec_rows(cv.cell, text_l)
    if erows:
        txt += "\n" + gcmod.format_elec_rows(erows)
    mw = pya.Application.instance().main_window()
    dlg = pya.QDialog(mw)
    dlg.setWindowTitle("Test coordinates (%d device(s), %d grating coupler(s), %d electrical pad(s))"
                       % (len({r["device"] for r in rows + erows}), len(rows), len(erows)))
    dlg.resize(760, 360)
    lay = pya.QVBoxLayout(dlg)
    edit = pya.QPlainTextEdit(dlg)
    edit.setPlainText(txt)
    edit.setReadOnly(True)
    lay.addWidget(edit)
    row = pya.QHBoxLayout()
    save = pya.QPushButton("Save as .txt / .csv ...", dlg)
    close = pya.QPushButton("Close", dlg)
    row.addStretch(1)
    row.addWidget(save)
    row.addWidget(close)
    lay.addLayout(row)

    def do_save(*args):
        fn = cv.filename()
        default = (fn.rsplit(".", 1)[0] + ".txt") if fn else "test_coordinates.txt"
        path = pya.QFileDialog.getSaveFileName(dlg, "Save test coordinates", default,
                                               "Text (*.txt);;CSV (*.csv)")
        if path:
            with open(path, "w") as f:
                f.write(txt)
            _status("Test coordinates saved to " + path)

    save.clicked(do_save)
    close.clicked(lambda *args: dlg.accept())
    dlg.exec_()


def route_selected_rulers():
    view = pya.LayoutView.current()
    if view is None:
        return
    rulers = list(view.each_annotation_selected()) or list(view.each_annotation())
    if not rulers:
        _error("Draw a ruler from A to B first (or use the Route tool).")
        return
    for ant in rulers:
        create_route(view, [ant.p1, ant.p2])


# ---------------------------------------------------------------------------
# interactive tool


class RoutePlugin(pya.Plugin):
    """
    Path mode : click corners like the Path tool; double-click (or Enter) ends,
                Backspace removes the last point.
    Auto mode : click A, click B.
    Right click / Esc cancels.
    """

    def __init__(self, view, kind=WG):
        super().__init__()
        self._view = view
        self.kind = kind
        self.points = []
        self.markers = []
        self.chain = []      # chain mode: clicked components [(instance, bbox), ...]
        self._labeled = []           # chain mode: GC instances already labelled in this chain
        self.attach_first = None     # attach mode: the component that will move
        self.switches = []           # metal path: indexes of the points where the layer changes
        self.via_specs = {}          # metal path: via settings per layer-change point
        self.bridge = None           # bridge mode: first break point (metal_route_at result)

    def activated(self):
        ensure_wire_aliases()
        set_active(self.kind)
        _plugins[self.kind] = self
        d = dock(self._view, self.kind)
        d.refresh(self._view)
        d.show()
        d.raise_()
        self._reset()
        if d.pin_mode():
            _status("Pin: click a waveguide end or a metal contact - the pin goes into the current cell %s" % (self._view.active_cellview().cell.name if self._view.active_cellview().cell else ""))
        elif d.attach_mode():
            _status("Attach: click the component to move")
        elif d.bridge_mode():
            _status("Bridge: click the first break point on a metal route")
        elif d.label_mode():
            _status("Label: click a grating coupler (any cell) to add an opt_in / out_ test label" if self.kind == WG
                    else "Label: click a pad, wire or contact to add an electrical label (elec_<ID>_<pad>)")
        elif d.chain_mode():
            _status("Route chain: click the first component (GC or device); each next click links it to the previous one")
        elif d.path_mode():
            _status("Route: click the corner points, double-click to finish (Backspace: undo point, Esc: cancel)"
                    + ("; Shift+click or V: change metal layer (via)" if self.kind == METAL else ""))
        else:
            _status("Route: click start point A (right click / Esc cancels)")

    def deactivated(self):
        self._reset()

    def _reset(self):
        self.points = []
        self.switches = []
        self.via_specs = {}
        self.bridge = None
        self.chain = []
        self.attach_first = None
        self._labeled = []
        self._clear_markers()
        try:
            self.ungrab_mouse()
        except Exception:
            pass

    def _clear_markers(self):
        for m in self.markers:
            m._destroy()
        self.markers = []

    def _snap(self, p):
        for fn in ("snap2", "snap"):
            try:
                return getattr(self, fn)(p)
            except Exception:
                continue
        return p

    def _port(self, p):
        """Device port within SNAP_PIXELS of p (or None)."""
        d = dock(None, self.kind)
        if not d.portsnap_ck.isChecked():
            return None
        cv = self._view.active_cellview()
        if not cv.is_valid():
            return None
        try:
            mag = self._view.viewport_trans().mag
        except Exception:
            mag = 1.0
        return port_near(self._view, cv, p, SNAP_PIXELS / max(mag, 1e-9))

    def _point(self, p):
        """(point, port): snapped to a device port if one is close, else to the grid (+ angle constraint)."""
        d = dock(None, self.kind)
        port = self._port(p)
        if port is not None:
            return port["pos"], port
        p = self._snap(p)
        if d.path_mode() and self.points:
            p = d.constrain(self.points[-1], p)
        return p, None

    def _port_marker(self, port):
        if port is None:
            return
        r = 1.0
        m = pya.Marker(self._view)
        m.set(pya.DBox(port["pos"].x - r, port["pos"].y - r, port["pos"].x + r, port["pos"].y + r))
        m.line_width = 2
        m.vertex_size = 0
        self.markers.append(m)
        _status("Route: port %s.%s - click to snap" % (port["device"], port["name"]))

    def _preview(self, pts, port=None):
        self._clear_markers()
        self._port_marker(port)
        d = dock(None, self.kind)
        try:
            sp, ep = _end_ports(self._view, d, pts)
            rt = compute(d, pts, sp, ep, self._view)
        except RouteError as e:
            _status("Route: " + str(e))
            m = pya.Marker(self._view)
            m.set(pya.DPath(pts, 0))
            m.line_style = 2
            self.markers.append(m)
            return
        m = pya.Marker(self._view)
        m.set(rt.path())
        m.dither_pattern = 1
        m.line_width = 1
        self.markers.append(m)
        sw = [k for k in self.switches if 0 < k < len(pts) - 1 or k == len(self.points) - 1]
        for k in sw:
            c, r = pts[k], max(rt.spec.width, 2.0) / 2.0
            v = pya.Marker(self._view)
            v.set(pya.DBox(c.x - r, c.y - r, c.x + r, c.y + r))
            v.line_width = 2
            v.vertex_size = 0
            self.markers.append(v)
        if port is not None:
            return
        layers = ""
        if self.switches:
            layers = "  [%s]" % " \u2192 ".join(self._layer_sequence())
        _status(("Route preview: %.4f um, %d bend(s)  -  double-click to finish" % (rt.length, rt.bends)
                 if d.path_mode() else "Route preview: %.4f um, %d bend(s)" % (rt.length, rt.bends)) + layers)

    def _layer_sequence(self):
        d = dock(None, self.kind)
        seq = [d.layer_cb.currentText.split("  (")[0].strip() or P["default_metal"]]
        for k in sorted(self.switches):
            seq.append(self.via_specs.get(k, {}).get("metal") or VIA_PAIRS.get(seq[-1], ("other metal",))[0])
        return seq

    def toggle_switch(self):
        """Change the metal layer at the last clicked point (again: undo the change)."""
        if self.kind != METAL or not self.points or not dock(None, self.kind).path_mode():
            return
        k = len(self.points) - 1
        if k == 0:
            _status("Layer change: click at least one segment first - the via goes on a corner")
            return
        if k in self.switches:
            self.switches.remove(k)
            self.via_specs.pop(k, None)
        else:
            d = dock(None, self.kind)
            c = self.points[k]
            seq = self._layer_sequence()
            spec = VIA_ASKER(d, d.width_sb.value, "at (%.3f, %.3f)" % (c.x, c.y),
                             tech_metals(self._view, self._view.active_cellview()),
                             seq[len([x for x in self.switches if x < k])], "Continue on metal")
            if spec is None:
                _status("Layer change cancelled")
                return
            self.switches.append(k)
            self.via_specs[k] = spec
        self.switches.sort()
        self._preview(self.points)
        _status("Layers: %s - via at %d point(s); keep clicking corners, double-click to finish"
                % (" \u2192 ".join(self._layer_sequence()), len(self.switches)))

    def _finish(self):
        pts, sw, specs = self.points, list(self.switches), dict(self.via_specs)
        self._reset()
        if len(pts) >= 2:
            if sw and self.kind == METAL:
                create_layered_route(self._view, pts, sw, dock(None, self.kind), via_specs=specs)
            else:
                create_route(self._view, pts, dock(None, self.kind))

    # -- bridge mode --------------------------------------------------------

    def _bridge_markers(self, hover=None):
        self._clear_markers()
        for k, h in enumerate([x for x in (self.bridge, hover) if x is not None]):
            m = pya.Marker(self._view)
            m.set(pya.DPath(h["pts"], 0))
            m.line_width = 1
            m.line_style = 2
            self.markers.append(m)
            c, r = h["q"], max(h["width"], 2.0) / 2.0
            v = pya.Marker(self._view)
            v.set(pya.DBox(c.x - r, c.y - r, c.x + r, c.y + r))
            v.line_width = 2 if (k == 0 and self.bridge is not None) else 1
            v.vertex_size = 0
            self.markers.append(v)

    def _bridge_click(self, p):
        view = self._view
        cv = view.active_cellview()
        hit = metal_route_at(view, cv, p, self._snap_radius())
        if hit is None:
            _status("Bridge: no metal route here - click on a metal route (%s)" % " / ".join(METALS[:2]))
            return
        if self.bridge is None:
            self.bridge = hit
            self._bridge_markers()
            _status("Bridge: first break at (%.3f, %.3f) - click the second break point on the same route"
                    % (hit["q"].x, hit["q"].y))
            return
        first = self.bridge
        if hit["inst"] != first["inst"]:
            _status("Bridge: click the second point on the same route")
            return
        try:
            pts, idx = bridge_points(first["pts"], first, hit)
        except RouteError as e:
            _status("Bridge: " + str(e))
            return
        self._reset()
        d = dock(None, self.kind)
        own = first["layer"]
        try:
            own = _layer_name(view, view.active_cellview(), resolve_layer(view, view.active_cellview(), own)) or own
        except RouteError:
            pass
        spec = VIA_ASKER(d, first["width"], "at both ends of the bridge (%.3f, %.3f) and (%.3f, %.3f)"
                         % (pts[idx[0]].x, pts[idx[0]].y, pts[idx[1]].x, pts[idx[1]].y),
                         tech_metals(view, view.active_cellview()), own, "Bridge metal")
        if spec is None:
            _status("Bridge cancelled")
            return
        back = dict(spec)
        back["metal"] = own
        create_layered_route(view, pts, idx, d, layer=first["layer"], width=first["width"],
                             replace=first["inst"], via_specs={idx[0]: spec, idx[1]: back})
        _status("Bridge: click the first break point on a metal route")

    # -- attach mode --------------------------------------------------------

    def _attach_markers(self, hover=None):
        self._clear_markers()
        for k, comp in enumerate([c for c in (self.attach_first, hover) if c is not None]):
            m = pya.Marker(self._view)
            m.set(comp[1])
            first = k == 0 and self.attach_first is not None
            m.line_width = 2 if first else 1
            m.line_style = 0 if first else 2
            m.vertex_size = 0
            self.markers.append(m)

    def _attach_click(self, p):
        view = self._view
        hit = object_at(view, view.active_cellview(), p)
        if hit is None:
            _status("Attach: nothing here - click a component instance")
            return
        if self.attach_first is None:
            self.attach_first = hit
            self._attach_markers()
            _status("Attach: %s will move - click the component to attach it to (Esc cancels)"
                    % hit[0].cell.basic_name())
            return
        if hit[0] == self.attach_first[0]:
            return
        first = self.attach_first
        self._reset()
        attach(view, first, hit, dock(None, self.kind))
        _status("Attach: click the next component to move")

    # -- chain mode ---------------------------------------------------------

    def _chain_markers(self, hover=None):
        self._clear_markers()
        shown = self.chain[-1:] + ([hover] if hover else [])   # the component the next link starts from
        for k, (inst, box) in enumerate(shown):
            m = pya.Marker(self._view)
            m.set(box)
            m.line_width = 2 if k == 0 and self.chain else 1
            m.line_style = 0 if k == 0 and self.chain else 2
            m.vertex_size = 0
            self.markers.append(m)

    def _chain_device(self):
        cv = self._view.active_cellview()
        return next((c for c in self.chain if not is_gc(self._view, cv, c[0])), None)

    def _chain_click(self, p):
        view = self._view
        cv = view.active_cellview()
        d = dock(None, self.kind)
        hit = object_at(view, cv, p)
        if hit is None:
            _status("Route chain: nothing here - click a grating coupler or device instance")
            return
        if self.chain and hit[0] == self.chain[-1][0]:
            return
        prev = self.chain[-1] if self.chain else None
        if prev is None:
            self.chain.append(hit)
            self._chain_markers()
            _status("Route chain: %s selected - click the component to link it to" % hit[0].cell.basic_name())
            return

        # suggestions: a GC before the chain's device is an input, after it an output
        device_before = self._chain_device()
        link_device = next((c for c in (prev, hit) if not is_gc(view, cv, c[0])), None)
        device = link_device or device_before
        dev_id = device_id_for(view, cv, d, device)
        text_l = _layer_or(view, cv, "Text", "10/0")
        has_in = any(e["deviceID"] == dev_id for e in gcmod.measurement_labels(cv.cell, text_l))
        labels = {}
        for tag, comp in (("A", prev), ("B", hit)):
            if is_gc(view, cv, comp[0]) and all(comp[0] != x for x in self._labeled):
                # the 1st component, clicked before any device of this chain -> input
                before_device = tag == "A" and device_before is None
                role = "in" if before_device and not has_in else "out"
                labels[tag] = (comp[0].cell.basic_name(), role)

        res = connect_link(view, prev, hit, d, labels, dev_id)
        if res is None:
            self._chain_markers()
            return
        rt, pa, pb, roles, dev_id = res
        self.chain.append(hit)
        lines = ["Link %s.%s -> %s.%s: %.3f um, %d bend(s)" % (pa["device"], pa["name"], pb["device"], pb["name"],
                                                             rt.length, rt.bends)]
        lines += self._write_labels({"A": prev, "B": hit}, roles, dev_id)
        d.info_lbl.setText("\n".join(lines))
        self._chain_markers()
        _status(lines[0] + " - click the next component, or double-click / Enter / Esc to end the chain")

    def _write_labels(self, comps, roles, dev_id):
        """Put the chosen labels (opt_in / out_) at the fiber targets of the link's GCs."""
        roles = {tag: r for tag, r in roles.items() if r}
        if not roles:
            return []
        view = self._view
        cv = view.active_cellview()
        text_l = _layer_or(view, cv, "Text", "10/0")
        gc_name = comps[next(iter(roles))][0].cell.basic_name()
        opt_in = gcmod.opt_in_label(dock(None, self.kind).pol_cb.currentText, gcmod.wavelength_for(gc_name), dev_id)
        _, to_cell = _cellview(view)
        lines = []
        if "in" in roles.values() and any(e["deviceID"] == dev_id
                                           for e in gcmod.measurement_labels(cv.cell, text_l)):
            lines.append("\u26a0 device '%s' already has an input label - SiEPIC needs one opt_in per device" % dev_id)
        view.transaction("Chain labels")
        try:
            for tag, role in roles.items():
                comp = comps[tag]
                label = opt_in if role == "in" else gcmod.out_label(opt_in)
                old = comp[0]
                _, _, new = attach_label(view, cv, old, gc_fiber(view, cv, old), label)   # moves with the GC
                if new != old:      # grouped into a wrapper: the chain continues with the wrapper
                    box = new.dbbox().transformed(_ctx(cv))
                    self.chain = [(new, box) if c[0] is old else c for c in self.chain]
                    comps[tag] = (new, box)
                self._labeled.append(new)
                lines.append("%s GC labelled %s" % (role, label))
        finally:
            view.commit()
        return lines

    def _chain_finish(self):
        n = max(len(self.chain) - 1, 0)
        self._reset()
        _status("Route chain ended: %d link(s)" % n)

    # -- events -----------------------------------------------------------

    def _snap_radius(self):
        try:
            return SNAP_PIXELS / max(self._view.viewport_trans().mag, 1e-9)
        except Exception:
            return 2.0

    def mouse_moved_event(self, p, buttons, prio):
        if not prio:
            return False
        if dock(None, self.kind).pin_mode():
            self._clear_markers()
            cand = pin_candidate(self._view, self._view.active_cellview(), p, self._snap_radius())
            if cand is not None:
                c = cand["trans"] * cand["pos"]
                r = max(cand["width"], 1.0)
                m = pya.Marker(self._view)
                m.set(pya.DBox(c.x - r, c.y - r, c.x + r, c.y + r))
                m.line_width = 2
                m.vertex_size = 0
                self.markers.append(m)
                _status("Pin: %s %s at (%.3f, %.3f) in %s - click to add a pin" % (
                    cand["kind"], "waveguide end" if cand["kind"] == "optical" else "contact",
                    cand["pos"].x, cand["pos"].y, cand["cell"].name))
            return False
        if dock(None, self.kind).label_mode():
            cv_ = self._view.active_cellview()
            self._clear_markers()
            island = _metal_island_at(self._view, cv_, p) if self.kind == METAL else None
            hit = (None, island[1]) if island is not None else object_at(self._view, cv_, p)
            if hit is not None:
                m = pya.Marker(self._view)
                m.set(hit[1])
                m.line_width = 2
                m.vertex_size = 0
                self.markers.append(m)
                _status("Label: %s - click to label it" % (hit[0].cell.basic_name() if hit[0] is not None
                                                           else island[0] + " metal"))
            return False
        if dock(None, self.kind).bridge_mode():
            hit = metal_route_at(self._view, self._view.active_cellview(), p, self._snap_radius())
            if hit is not None and self.bridge is not None and hit["inst"] != self.bridge["inst"]:
                hit = None
            self._bridge_markers(hit)
            return False
        if dock(None, self.kind).attach_mode():
            hit = object_at(self._view, self._view.active_cellview(), p)
            first = self.attach_first
            self._attach_markers(hit if hit and (first is None or hit[0] != first[0]) else None)
            return False
        if dock(None, self.kind).chain_mode():
            hit = object_at(self._view, self._view.active_cellview(), p)
            self._chain_markers(hit if hit and (not self.chain or hit[0] != self.chain[-1][0]) else None)
            return False
        q, port = self._point(p)
        if self.points:
            self._preview(self.points + [q], port)
            return True
        self._clear_markers()
        self._port_marker(port)
        return False

    def mouse_click_event(self, p, buttons, prio):
        if not prio:
            return False
        if buttons & pya.ButtonState.RightButton:
            if self.bridge is not None:
                self._reset()
                _status("Bridge: cancelled - click the first break point on a metal route")
                return True
            if self.attach_first is not None:
                self._reset()
                _status("Attach: cancelled - click the component to move")
                return True
            if self.chain:
                self._chain_finish()
                return True
            self._reset()
            _status("Route: cancelled")
            return True
        if not (buttons & pya.ButtonState.LeftButton):
            return False
        d = dock(None, self.kind)
        if d.pin_mode():
            self._clear_markers()
            add_pin_at(self._view, p, self._snap_radius())
            return True
        if d.attach_mode():
            self._attach_click(p)
            return True
        if d.bridge_mode():
            self._bridge_click(p)
            return True
        if d.label_mode():
            self._clear_markers()
            if self.kind == WG:
                label_gc_at(self._view, p, d)
            else:
                label_elec_at(self._view, p, d)
            return True
        if d.chain_mode():
            self._chain_click(p)
            return True
        p, port = self._point(p)
        if self.points and (p - self.points[-1]).abs() < 1e-9:
            return True
        self.points.append(p)
        if port is not None and d.path_mode() and len(self.points) >= 2:
            self._finish()  # ending on a device port completes the route
            return True
        if buttons & pya.ButtonState.ShiftKey and self.kind == METAL and d.path_mode() and len(self.points) >= 2:
            self.toggle_switch()   # Shift+click: change the metal layer at this corner (via)
            return True
        if len(self.points) == 1:
            try:
                self.grab_mouse()
            except Exception:
                pass
        if d.path_mode():
            d.set_points(self.points)
            _status("Route: %d point(s) - click the next corner, double-click to finish" % len(self.points))
        elif len(self.points) == 1:
            d.set_a(p)
            _status("Route: click end point B")
        else:
            d.set_b(p)
            self._finish()
        return True

    def mouse_double_click_event(self, p, buttons, prio):
        if prio and dock(None, self.kind).chain_mode() and self.chain:
            self._chain_finish()
            return True
        if not prio or not self.points:
            return False
        if dock(None, self.kind).path_mode():
            p, _ = self._point(p)
            if (p - self.points[-1]).abs() > 1e-9:
                self.points.append(p)
            self._finish()
        return True

    def key_event(self, key, buttons):
        if self.bridge is not None and key == pya.KeyCode.Escape:
            self._reset()
            _status("Bridge: cancelled - click the first break point on a metal route")
            return True
        if key == ord("V") and self.points and self.kind == METAL and dock(None, self.kind).path_mode():
            self.toggle_switch()
            return True
        if self.attach_first is not None and key == pya.KeyCode.Escape:
            self._reset()
            _status("Attach: cancelled - click the component to move")
            return True
        if self.chain:
            if key in (pya.KeyCode.Escape, pya.KeyCode.Return, pya.KeyCode.Enter):
                self._chain_finish()   # links already made stay; pending GC labels are written
                return True
            return False
        if not self.points:
            return False
        if key == pya.KeyCode.Escape:
            self._reset()
            _status("Route: cancelled")
            return True
        if key in (pya.KeyCode.Return, pya.KeyCode.Enter) and dock(None, self.kind).path_mode():
            self._finish()
            return True
        if key == pya.KeyCode.Backspace and dock(None, self.kind).path_mode():
            self.points.pop()
            self.switches = [k for k in self.switches if k < len(self.points)]
            self.via_specs = {k: v for k, v in self.via_specs.items() if k in self.switches}
            dock(None, self.kind).set_points(self.points)
            self._clear_markers()
            if not self.points:
                self._reset()
            return True
        return False


class RoutePluginFactory(pya.PluginFactory):
    def __init__(self, kind=WG):
        super().__init__()
        self.kind = kind
        self.has_tool_entry = True
        if kind == WG:
            self.register(-900, TOOL, P["prefix"] + "Waveguide Route", ICON)
        else:
            self.register(-899, TOOL_METAL, P["prefix"] + "Metal Route", ICON_METAL if os.path.exists(ICON_METAL) else ICON)

    def create_plugin(self, manager, root, view):
        return RoutePlugin(view, self.kind)


_factory = None
_factory_metal = None
_timers = []


def install():
    """Register the tool and menu entries (call once from an autorun macro)."""
    global _factory, _factory_metal, SHORTCUT, SHORTCUT_METAL
    if _factory is not None:
        return
    pcell.register()
    _factory = RoutePluginFactory(WG)
    _factory_metal = RoutePluginFactory(METAL)
    # the PDK library may be registered after this package: retry once startup is done and
    # whenever a layout is opened (wire cells in it turn back into editable PCells)
    ensure_wire_aliases()
    userlib.register_all()
    for ms in (0, 3000):
        tm = pya.QTimer()
        tm.setSingleShot(True)
        # user libraries read before the PDK library was there: read them again
        tm.timeout(lambda *args: (ensure_wire_aliases(), userlib.register_all(only_cold=True)))
        tm.start(ms)
        _timers.append(tm)
    try:
        pya.Application.instance().main_window().on_current_view_changed += lambda *args: ensure_wire_aliases()
    except Exception:
        pass

    menu = pya.Application.instance().main_window().menu()
    # another klroute package already loaded: do not take its shortcuts
    if any(menu.is_valid("tools_menu.%s_menu" % other) for other in ("klroute", "klroute_siepic")
           if other != P["package"]):
        SHORTCUT, SHORTCUT_METAL = "Ctrl+Alt+R", "Ctrl+Alt+M"
    if not menu.is_valid("tools_menu.%s_menu" % P["package"]):
        menu.insert_menu("tools_menu.end", P["package"] + "_menu", P["menu"])

    def action(title, fn):
        a = pya.Action()
        a.title = title
        a.on_triggered(fn)
        return a

    tool_action = action("Waveguide Route Tool", lambda: start_tool(WG))
    tool_action.shortcut = SHORTCUT
    tool_action.icon = ICON
    menu.insert_item("tools_menu.%s_menu.end" % P["package"], P["package"] + "_start", tool_action)
    metal_action = action("Metal Route Tool", lambda: start_tool(METAL))
    metal_action.shortcut = SHORTCUT_METAL
    metal_action.icon = ICON_METAL if os.path.exists(ICON_METAL) else ICON
    menu.insert_item("tools_menu.%s_menu.end" % P["package"], P["package"] + "_start_metal", metal_action)
    menu.insert_item("tools_menu.%s_menu.end" % P["package"], P["package"] + "_dock",
                     action("Show Waveguide Route Window", lambda: show_window(WG)))
    menu.insert_item("tools_menu.%s_menu.end" % P["package"], P["package"] + "_dock_metal",
                     action("Show Metal Route Window", lambda: show_window(METAL)))
    menu.insert_item("tools_menu.%s_menu.end" % P["package"], P["package"] + "_gcarray",
                     action("Connect Selected Device to GC Array", connect_selected_device))
    menu.insert_item("tools_menu.%s_menu.end" % P["package"], P["package"] + "_addpins",
                     action("Add Pins to Device...", add_device_pins))
    menu.insert_item("tools_menu.%s_menu.end" % P["package"], P["package"] + "_adddevrec",
                     action("Add DevRec Box to Device...", add_devrec_box))
    menu.insert_item("tools_menu.%s_menu.end" % P["package"], P["package"] + "_addlib",
                     action("Add Selected Cell to Library...", add_to_library))
    menu.insert_item("tools_menu.%s_menu.end" % P["package"], P["package"] + "_rmlib",
                     action("Remove Cells / Delete Library...", remove_from_library))
    menu.insert_item("tools_menu.%s_menu.end" % P["package"], P["package"] + "_testcoords",
                     action("Test Coordinates (opt_in labels) ...", show_test_coordinates))
    menu.insert_item("tools_menu.%s_menu.end" % P["package"], P["package"] + "_rulers",
                     action("Route Along Selected Rulers", route_selected_rulers))
