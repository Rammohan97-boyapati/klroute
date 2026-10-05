"""
klroute.profile -- everything that depends on the PDK, in one place.

The built-in profile follows the SiEPIC conventions (SiEPIC EBeam and compatible PDKs):
Si / SiN waveguide core, M2_router / M1_heater metals, PinRec 1/10, PinRecM 1/11, DevRec 68/0,
FbrTgt 81/0, Text 10/0, the PDK's Waveguide / Wireguide cells and grating-coupler cells.

A PDK with other layer names, metals or via rules is set up without changing the code, with a
file ``klroute_profile.json``.  klroute reads, in this order (later ones win):

1. the built-in profile,
2. ``klroute_profile.json`` in the KLayout user folder (``%USERPROFILE%\\KLayout`` on Windows,
   ``~/.klayout`` on Linux / macOS) or the file named by the environment variable KLROUTE_PROFILE,
3. ``klroute_profile.json`` in the technology folder (next to the technology's .lyt file) of the
   layout you are working on.

So a PDK can carry its own klroute settings: every computer with that PDK installed uses them.
Example::

    {
      "core": [["Si", "1/0"]],
      "contact_metals": [["M1", "41/0"], ["M2", "45/0"]],
      "metals": ["M1", "M2", "M3"],
      "default_metal": "M1",
      "via_pairs": {"M1": ["M2", "VIA1"], "M2": ["M1", "VIA1"]},
      "via_cuts": [["M1", "M2", "VIA1"], ["M2", "M3", "VIA2"]],
      "default_cut": "VIA1",
      "via_unit": 5.0, "via_cut": 0.8, "via_pitch": 1.4, "via_enclosure": 0.7,
      "label_at": "fiber"
    }

The profile follows the technology of the layout in use (``use()``); ``P`` is updated in place.
"""

import json
import os

try:
    import pya as _db
except ImportError:  # standalone "pip install klayout"
    try:
        import klayout.db as _db
    except ImportError:
        _db = None

FILE_NAME = "klroute_profile.json"

_BUILTIN = {
    "package": "klroute",                     # python package / salt grain / ids
    "lib": "klroute",                         # PCell library of the editable Route cells
    "prefix": "",                             # in front of window and tool titles
    "menu": "Route (Waveguide / Metal)",
    # (layer name, fallback layer/datatype) - the fallback is only used if the name is unknown
    "core": [("Si", "1/0"), ("SiN", "4/0")],                 # waveguide core (pin finder)
    "core_extra": [],                                         # used if the core is empty
    "contact_metals": [("M2_router", "12/0"), ("M1_heater", "11/0")],   # pin finder: contacts
    "metals": ["M2_router", "M1_heater", "M1", "M2", "M3", "Metal1", "Metal2", "Metal3"],
    "default_metal": "M2_router",
    # metal -> (its usual partner, cut layer between them); "" = direct contact, no cut layer
    "via_pairs": {"M2_router": ("M1_heater", ""), "M1_heater": ("M2_router", "")},
    "via_cuts": {("M1_heater", "M2_router"): ""},
    "default_cut": "",
    # via geometry defaults: tiles of via_unit, cuts of via_cut at via_pitch, metal enclosure
    "via_unit": 5.0, "via_cut": 0.8, "via_pitch": 1.4, "via_enclosure": 0.7,
    "wire_label": "Use the PDK library wire cell as the route",
    "via_label": "add a via where an end lands on the other metal",
    "label_at": "gc_origin",     # opt_in label: "gc_origin" = GC tip (SiEPIC rule), "fiber" = fiber target
}
_FIXED = ("package", "lib", "prefix", "menu")      # identity of the package: never from a file

P = {}              # the profile in use (updated in place by use())
SOURCE = []         # the files it was read from
_current = [None]   # technology the profile was built for
_listeners = []


def user_file():
    env = os.environ.get("KLROUTE_PROFILE")
    if env:
        return env
    home = os.environ.get("KLAYOUT_HOME") or os.path.join(
        os.path.expanduser("~"), "KLayout" if os.name == "nt" else ".klayout")
    return os.path.join(home, FILE_NAME)


def tech_file(tech_name):
    """klroute_profile.json in the folder of technology ``tech_name`` (or None)."""
    if not tech_name or _db is None:
        return None
    try:
        tech = _db.Technology.technology_by_name(tech_name)
        base = tech.base_path() if tech is not None else ""
    except Exception:
        return None
    return os.path.join(base, FILE_NAME) if base else None


def _read(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print("klroute: could not read %s (%s) - ignored" % (path, e))
        return None


def _merge(prof, data):
    for key, value in data.items():
        if key in _FIXED or key.startswith("_"):
            continue
        if key in ("core", "core_extra", "contact_metals"):
            value = [tuple(x) if isinstance(x, (list, tuple)) else (x, "") for x in value]
        elif key == "via_pairs":
            value = {k: tuple(v) for k, v in value.items()}
        elif key == "via_cuts":
            value = {(a, b): c for a, b, c in value} if isinstance(value, list) else \
                {tuple(k.split("|")): v for k, v in value.items()}
        prof[key] = value


def use(tech_name=None, force=False):
    """
    Make ``P`` the profile for technology ``tech_name``: built-in, then the user file, then the
    technology folder's file.  Cheap when the technology did not change.
    """
    if not force and _current[0] == (tech_name or "") and P:
        return P
    prof = dict(_BUILTIN)
    used = []
    for path in (user_file(), tech_file(tech_name)):
        data = _read(path)
        if data:
            _merge(prof, data)
            used.append(path)
    P.clear()
    P.update(prof)
    SOURCE[:] = used
    _current[0] = tech_name or ""
    for fn in _listeners:
        fn()
    return P


def on_change(fn):
    """Call ``fn()`` whenever the profile in use changes (and once now)."""
    _listeners.append(fn)
    fn()


use(None)
