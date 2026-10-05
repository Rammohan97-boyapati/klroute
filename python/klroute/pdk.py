"""
klroute.pdk -- read waveguide types from a PDK (SiEPIC-style WAVEGUIDES.xml)
and resolve layer names (e.g. "Si") to layer/datatype via the PDK's .lyp.
"""

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field


@dataclass
class Component:
    layer: str          # layer name as in the .lyp ("Si") or "1/0"
    width: float
    offset: float = 0.0


@dataclass
class WaveguideType:
    name: str
    width: float
    radius: float
    components: list = field(default_factory=list)
    model: str = ""     # compact model name (<model>) from WAVEGUIDES.xml
    cml: str = ""       # compact model library (<CML>)


def _float(el, tag, default=0.0):
    t = el.findtext(tag)
    try:
        return float(t)
    except (TypeError, ValueError):
        return default


def read_waveguides(path):
    """Parse a WAVEGUIDES.xml file. Returns a list of WaveguideType (empty if missing)."""
    if not path or not os.path.isfile(path):
        return []
    root = ET.parse(path).getroot()
    types = []
    for wg in root.iter("waveguide"):
        comps = [Component(c.findtext("layer", "").strip(), _float(c, "width"), _float(c, "offset"))
                 for c in wg.findall("component")]
        width = _float(wg, "width", comps[0].width if comps else 0.5)
        types.append(WaveguideType(wg.findtext("name", "?").strip(), width, _float(wg, "radius", 10.0), comps,
                                   (wg.findtext("model") or "").strip(), (wg.findtext("CML") or "").strip()))
    return types


def read_lyp_names(path):
    """Map layer name -> (layer, datatype) from a .lyp file."""
    names = {}
    if not path or not os.path.isfile(path):
        return names
    text = open(path, encoding="utf-8", errors="replace").read()
    for name, src in re.findall(r"<name>([^<]*)</name>\s*<source>([^<]*)</source>", text):
        m = re.match(r"\s*(\d+)/(\d+)", src)
        if name.strip() and m:
            names[name.strip()] = (int(m.group(1)), int(m.group(2)))
    return names
