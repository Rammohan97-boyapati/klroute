"""
klroute is a KLayout Salt package: this folder (grain.xml, pymacros/, python/) is the package.

    python install.py              install / update into ~/KLayout/salt/klroute
    python install.py --uninstall  remove it
    python install.py --zip        build dist/klroute-<version>.zip to share

Restart KLayout afterwards.  The package then shows up in Tools > Manage Packages.
On another computer: unzip the zip into  <KLayout user folder>/salt/
(%USERPROFILE%\\KLayout\\salt on Windows, ~/.klayout/salt on Linux/macOS).

PDK-specific settings live in klroute_profile.json files in the technology folders (or the
KLayout user folder) - see python/klroute/profile.py.  They are never part of the package or the
zip; --zip refuses to build if a layer name from one of them appears in the package files.
"""

import json
import os
import re
import shutil
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "klroute"
CONTENT = ["grain.xml", "README.md", "LICENSE", "docs", "pymacros", "python", "examples", "tests"]
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc")


def klayout_home():
    if os.environ.get("KLAYOUT_HOME"):
        return os.environ["KLAYOUT_HOME"]
    home = os.path.expanduser("~")
    if os.name == "nt":
        return os.path.join(home, "KLayout")
    return os.path.join(home, ".klayout")


KL = klayout_home()
DST = os.path.join(KL, "salt", NAME)
# locations used by earlier, non-package versions of this tool
OLD = [os.path.join(KL, "python", NAME), os.path.join(KL, "pymacros", "klroute_autorun.lym")]


def version():
    text = open(os.path.join(HERE, "grain.xml"), encoding="utf-8").read()
    return re.search(r"<version>([^<]*)</version>", text).group(1)


def remove(path):
    if os.path.isdir(path):
        shutil.rmtree(path)
    elif os.path.isfile(path):
        os.remove(path)
    else:
        return False
    return True


def uninstall():
    for p in [DST] + OLD:
        if remove(p):
            print("removed", p)


def install():
    try:
        uninstall()
    except PermissionError as e:
        # a file of the installed copy is open (e.g. by a running KLayout): copy over it instead
        print("note: could not remove the old copy (%s) - overwriting it" % e.filename)
    os.makedirs(DST, exist_ok=True)
    for item in CONTENT:
        src = os.path.join(HERE, item)
        if os.path.isdir(src):
            shutil.copytree(src, os.path.join(DST, item), ignore=SKIP, dirs_exist_ok=True)
        elif os.path.isfile(src):
            shutil.copy2(src, os.path.join(DST, item))
    print("installed %s %s into %s" % (NAME, version(), DST))
    print("restart KLayout: toolbar 'Waveguide Route' (Ctrl+Shift+R) and 'Metal Route' (Ctrl+Shift+M), "
          "Tools > Route (Waveguide / Metal)")


def profile_files():
    """Every klroute_profile.json on this computer: the user folder, the KLayout tech / salt folders
    and the technology folders registered in KLayout's settings (klayoutrc)."""
    found = set()
    cand = [os.path.join(KL, "klroute_profile.json")]
    for sub in ("tech", "salt"):
        for root, dirs, fs in os.walk(os.path.join(KL, sub)):
            if "klroute_profile.json" in fs:
                cand.append(os.path.join(root, "klroute_profile.json"))
    rc = os.path.join(KL, "klayoutrc")
    if os.path.isfile(rc):
        text = open(rc, encoding="utf-8", errors="ignore").read()
        for base in re.findall(r"(?:original-)?base-path&gt;([^&<]+)&lt;", text):
            cand.append(os.path.join(base.strip(), "klroute_profile.json"))
    for c in cand:
        if os.path.isfile(c):
            found.add(os.path.normpath(c))
    return sorted(found)


def private_names():
    """Layer / metal names from the klroute_profile.json files: they must not appear in the package."""
    names = set()
    for path in profile_files():
        data = json.load(open(path, encoding="utf-8"))
        for key in ("core", "core_extra", "contact_metals"):
            names |= {x[0] for x in data.get(key, [])}
        names |= set(data.get("metals", []))
        for a, b, c in data.get("via_cuts", []):
            names |= {a, b, c}
    builtin = {"Si", "SiN", "Waveguide", "M1", "M2", "M3", "Metal1", "Metal2", "Metal3", "M1_heater", "M2_router"}
    return {n for n in names if n and n not in builtin}


def files():
    for item in CONTENT:
        src = os.path.join(HERE, item)
        if os.path.isfile(src):
            yield src
        for root, dirs, fs in os.walk(src):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in fs:
                if not f.endswith(".pyc"):
                    yield os.path.join(root, f)


def build_zip():
    names = private_names()
    found = []
    for full in files():
        if full.endswith((".py", ".md", ".html", ".xml", ".lym", ".txt")):
            text = open(full, encoding="utf-8", errors="ignore").read()
            hits = sorted(n for n in names if re.search(r"\b%s\b" % re.escape(n), text))
            if hits:
                found.append((os.path.relpath(full, HERE), hits))
    if found:
        print("not written: names from your private klroute_profile.json appear in the package:")
        for f, hits in found:
            print("   %s: %s" % (f, ", ".join(hits)))
        sys.exit(1)
    out_dir = os.path.join(HERE, "dist")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, "%s-%s.zip" % (NAME, version()))
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for full in files():
            z.write(full, os.path.join(NAME, os.path.relpath(full, HERE)))
    print("wrote", out)
    print("to install: unzip into %s (gives %s)" % (os.path.join(KL, "salt"), DST))


if __name__ == "__main__":
    if "--uninstall" in sys.argv:
        uninstall()
    elif "--zip" in sys.argv:
        build_zip()
    else:
        install()
