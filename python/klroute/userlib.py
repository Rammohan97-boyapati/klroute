"""
klroute.userlib -- your own cell libraries.

A user library is one GDS file in ``<KLayout folder>/klroute_libraries/<name>.gds`` (plus ``<name>.json`` with its
technology and description).  Cells you add are copied there with all their sub-cells; PCells and PDK cells inside
them stay PCell / library references.  Every library in the folder is registered when KLayout starts, so its cells
show in the Library browser and can be placed in any layout (of its technology, if it has one).
"""

import json
import os
import re

import pya

FOLDER_NAME = "klroute_libraries"
NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]*$")
_libs = {}      # name -> registered pya.Library (keeps the Python object alive)


def folder():
    try:
        base = pya.Application.instance().application_data_path()
    except Exception:
        base = ""
    if not base:
        base = os.path.join(os.path.expanduser("~"), "KLayout")
    return os.path.join(base, FOLDER_NAME)


def _paths(name):
    d = folder()
    return os.path.join(d, name + ".gds"), os.path.join(d, name + ".json")


def libraries():
    """Names of the user libraries in the folder."""
    d = folder()
    if not os.path.isdir(d):
        return []
    return sorted(f[:-4] for f in os.listdir(d) if f.lower().endswith(".gds") and NAME_RE.match(f[:-4]))


def meta(name):
    js = _paths(name)[1]
    try:
        with open(js, encoding="utf-8") as fh:
            m = json.load(fh)
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def _save_meta(name, m):
    with open(_paths(name)[1], "w", encoding="utf-8") as fh:
        json.dump(m, fh, indent=2)


def check_name(name):
    """Problem with a new library name, or "" if it can be used."""
    if not NAME_RE.match(name or ""):
        return "Library names may use letters, digits, '_' and '-' only (no spaces)."
    if name not in libraries() and name in pya.Library.library_names():
        return "'%s' is already the name of another library (PDK or package) - choose another name." % name
    return ""


def cells_in(name):
    """Top cells of a user library (the cells you added), sorted."""
    gds = _paths(name)[0]
    ly = pya.Layout()
    ly.technology_name = meta(name).get("made_in") or meta(name).get("technology") or ""
    try:
        ly.read(gds)
    except Exception:
        return []
    return sorted(c.name for c in ly.top_cells())


def register(name):
    """(Re-)register one user library from its file.  Layouts using it are updated by KLayout."""
    gds = _paths(name)[0]
    m = meta(name)
    lib = pya.Library()
    lib.description = m.get("description") or "klroute user library"
    tech = m.get("technology") or ""
    # PDK cells inside are found through the technology the cells were made in
    lib.layout().technology_name = m.get("made_in") or tech
    lib.layout().read(gds)
    if tech:
        try:
            lib.add_technology(tech)
        except Exception:
            lib.technology = tech
    lib.register(name)
    _libs[name] = lib
    return lib


def _has_cold_proxies(lib):
    """True if cells of the library refer to a library that was not there when it was read (e.g. the PDK)."""
    return any(c.qname().startswith("<defunct>") for c in lib.layout().each_cell())


def register_all(only_cold=False):
    """Register every user library; with ``only_cold`` only those whose PDK references were not resolved yet."""
    done = []
    for name in libraries():
        try:
            if only_cold and name in _libs and not _has_cold_proxies(_libs[name]):
                continue
            register(name)
            done.append(name)
        except Exception:
            pass
    return done


def _lib_ref(target, cell):
    """For a cell placed from a library (PDK cell or PCell): the same reference in ``target``, else None."""
    if not cell.is_library_cell():
        return None
    lib = cell.library()
    if lib is None:
        return None
    lcell = lib.layout().cell(cell.library_cell_index())
    if lcell.is_pcell_variant():
        decl = lcell.pcell_declaration()
        return target.add_pcell_variant(lib, lib.layout().pcell_id(decl.name()), lcell.pcell_parameters())
    return target.add_lib_cell(lib, lcell.cell_index())


def copy_cell(cell, target, name, memo=None):
    """
    Copy ``cell`` and its hierarchy into layout ``target`` as a new cell ``name``.  Cells placed from a library
    (PDK cells, PCells such as waveguides) stay references to that library; your own cells are copied.
    Returns the new cell's index.
    """
    memo = {} if memo is None else memo
    src = cell.layout()
    ref = _lib_ref(target, cell)
    new = target.create_cell(name)
    if ref is not None:          # the chosen cell itself comes from a library: wrap one instance of it
        new.insert(pya.CellInstArray(ref, pya.Trans()))
        return new.cell_index()
    scale = pya.ICplxTrans(src.dbu / target.dbu)
    for li in src.layer_indexes():
        if not cell.shapes(li).is_empty():
            new.shapes(target.layer(src.get_info(li))).insert(cell.shapes(li), scale)
    for inst in cell.each_inst():
        child = inst.cell
        ci = memo.get(child.cell_index())
        if ci is None:
            ci = _lib_ref(target, child)
            if ci is None:
                n = child.name
                if target.cell(n) is not None:
                    n = target.unique_cell_name(n) if hasattr(target, "unique_cell_name") else n + "_" + name
                ci = copy_cell(child, target, n, memo)
            memo[child.cell_index()] = ci
        a = inst.dcell_inst
        a.cell_index = ci
        if inst.has_prop_id():
            new.insert(a, target.properties_id(src.properties(inst.prop_id)))
        else:
            new.insert(a)
    return new.cell_index()


def add_cells(cells, name, technology="", description="", new_names=None, replace=False):
    """
    Copy ``cells`` (with their sub-cells) into user library ``name`` (created if new), save it and register it.
    ``new_names`` gives each cell's name in the library (default: its own name).  A cell of the same name already in
    the library is replaced if ``replace``, else a ValueError is raised.  Returns the names added.
    """
    err = check_name(name)
    if err:
        raise ValueError(err)
    gds, _ = _paths(name)
    os.makedirs(folder(), exist_ok=True)
    target = pya.Layout()
    made_in = cells[0].layout().technology_name
    target.technology_name = made_in
    is_new = not os.path.exists(gds)
    if is_new:
        target.dbu = cells[0].layout().dbu
    else:
        target.read(gds)
    names = list(new_names or [c.name for c in cells])
    clash = [n for n in names if target.cell(n) is not None]
    if clash and not replace:
        raise ValueError("Library '%s' already has cell(s) %s - tick 'Replace' or give another name."
                         % (name, ", ".join(clash)))
    for n in clash:
        target.prune_cell(target.cell(n).cell_index(), -1)
    for c, n in zip(cells, names):
        if not n:
            raise ValueError("Give every cell a name.")
        copy_cell(c, target, n)
    tmp = gds[:-4] + ".tmp.gds"
    target.write(tmp)
    os.replace(tmp, gds)
    m = meta(name) if not is_new else {}
    m["made_in"] = made_in
    if is_new or technology != m.get("technology", ""):
        m["technology"] = technology
    if description or is_new:
        m["description"] = description or m.get("description", "")
    _save_meta(name, m)
    register(name)
    return names


def remove_cells(name, cell_names):
    """Delete cells (and their sub-cells not used elsewhere) from a user library; re-register it."""
    gds = _paths(name)[0]
    target = pya.Layout()
    target.technology_name = meta(name).get("made_in") or meta(name).get("technology") or ""
    target.read(gds)
    for n in cell_names:
        c = target.cell(n)
        if c is not None:
            target.prune_cell(c.cell_index(), -1)
    tmp = gds[:-4] + ".tmp.gds"
    target.write(tmp)
    os.replace(tmp, gds)
    register(name)
