"""The list of fonts PDF Desk can write with, like the font list in Word.

It combines the fonts installed on the computer with the fonts that ship with PDF Desk (so text
looks the same on every machine). Font files are only read from the usual system and user font
folders. Fonts whose license says they may not be embedded in documents are left out.

Text written into a PDF only embeds the letters it actually uses (a "subset"), which keeps files
small. Everything here works offline.
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
import re
import sys
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from functools import lru_cache

IS_WINDOWS = sys.platform.startswith("win")
logging.getLogger("fontTools").setLevel(logging.ERROR)  # its subsetter chats about tables it skips
CACHE_VERSION = 2
MAX_FONT_FILE = 60 * 1024 * 1024        # skip absurdly large files
FONT_EXTS = (".ttf", ".otf", ".ttc", ".otc")

# Fonts that come with PDF Desk (pymupdf-fonts) and with the PDF engine itself.
BUNDLED = {
    "FiraGO": {(False, False): "figo", (True, False): "figbo", (False, True): "figit", (True, True): "figbi"},
    "Fira Mono": {(False, False): "fimo", (True, False): "fimbo"},
    "Noto Sans": {(False, False): "notos", (True, False): "notosbo", (False, True): "notosit", (True, True): "notosbi"},
    "Ubuntu": {(False, False): "ubuntu", (True, False): "ubuntubo", (False, True): "ubuntuit", (True, True): "ubuntubi"},
    "Ubuntu Mono": {(False, False): "ubuntm", (True, False): "ubuntmbo", (False, True): "ubuntmit",
                    (True, True): "ubuntmbi"},
    "Space Mono": {(False, False): "spacemo", (True, False): "spacembo", (False, True): "spacemit",
                   (True, True): "spacembi"},
    "Cascadia Mono": {(False, False): "cascadia", (True, False): "cascadiab", (False, True): "cascadiai",
                      (True, True): "cascadiabi"},
}
# The three classic PDF fonts, built into the PDF engine (Latin letters only).
BUILTIN = {
    "Helvetica": {(False, False): "helv", (True, False): "hebo", (False, True): "heit", (True, True): "hebi"},
    "Times": {(False, False): "tiro", (True, False): "tibo", (False, True): "tiit", (True, True): "tibi"},
    "Courier": {(False, False): "cour", (True, False): "cobo", (False, True): "coit", (True, True): "cobi"},
}
# What Qt should use to show the built-in fonts on screen while typing.
QT_FALLBACKS = {
    "Helvetica": ["Nimbus Sans", "Arial", "Liberation Sans", "Helvetica", "DejaVu Sans"],
    "Times": ["Nimbus Roman", "Times New Roman", "Liberation Serif", "Times", "DejaVu Serif"],
    "Courier": ["Nimbus Mono PS", "Courier New", "Liberation Mono", "Courier", "DejaVu Sans Mono"],
}
DEFAULT_CANDIDATES = ["Arial", "Liberation Sans", "Helvetica Neue", "Noto Sans"]

# Common PDF font names and the families that look the same (metric-compatible where possible).
ALIASES = {
    "arial": ["Arial", "Liberation Sans", "Arimo", "Helvetica"],
    "arialmt": ["Arial", "Liberation Sans", "Arimo", "Helvetica"],
    "helvetica": ["Helvetica", "Arial", "Liberation Sans"],
    "helveticaneue": ["Helvetica Neue", "Arial", "Liberation Sans", "Helvetica"],
    "timesnewroman": ["Times New Roman", "Liberation Serif", "Tinos", "Times"],
    "timesnewromanps": ["Times New Roman", "Liberation Serif", "Tinos", "Times"],
    "times": ["Times New Roman", "Liberation Serif", "Times"],
    "timesroman": ["Times New Roman", "Liberation Serif", "Times"],
    "couriernew": ["Courier New", "Liberation Mono", "Cousine", "Courier"],
    "courier": ["Courier New", "Liberation Mono", "Courier"],
    "calibri": ["Calibri", "Carlito"],
    "cambria": ["Cambria", "Caladea"],
    "arialnarrow": ["Arial Narrow", "Liberation Sans Narrow"],
    "georgia": ["Georgia", "Gelasio"],
    "segoeui": ["Segoe UI", "Noto Sans", "Open Sans"],
    "verdana": ["Verdana", "DejaVu Sans"],
    "tahoma": ["Tahoma", "DejaVu Sans"],
    "symbol": ["Symbol"],
    "nimbussans": ["Helvetica", "Nimbus Sans", "Arial", "Liberation Sans"],
    "nimbussansl": ["Helvetica", "Nimbus Sans", "Arial", "Liberation Sans"],
    "nimbusroman": ["Times", "Nimbus Roman", "Times New Roman", "Liberation Serif"],
    "nimbusromno9l": ["Times", "Nimbus Roman", "Times New Roman", "Liberation Serif"],
    "nimbusmono": ["Courier", "Nimbus Mono PS", "Courier New", "Liberation Mono"],
    "nimbusmonol": ["Courier", "Nimbus Mono PS", "Courier New", "Liberation Mono"],
    "nimbusmonops": ["Courier", "Nimbus Mono PS", "Courier New", "Liberation Mono"],
    "notosans": ["Noto Sans"],
    "dejavusans": ["DejaVu Sans"],
}


@dataclass
class Face:
    family: str
    bold: bool
    italic: bool
    path: str = ""          # a font file, or "" for a bundled/built-in font
    index: int = 0          # face number inside a .ttc collection
    code: str = ""          # pymupdf-fonts or built-in code
    source: str = "system"  # system | bundled | builtin
    subset_ok: bool = True  # the license allows embedding only the used letters

    @property
    def key(self) -> str:
        return f"{self.source}:{self.path or self.code}:{self.index}"


@dataclass
class Family:
    name: str
    source: str
    faces: dict = field(default_factory=dict)  # (bold, italic) -> Face

    def face(self, bold: bool, italic: bool) -> tuple[Face, bool]:
        """The best face for a style, and whether it really has that style."""
        f = self.faces.get((bold, italic))
        if f is not None:
            return f, True
        for alt in ((bold, False), (False, italic), (False, False)):
            if alt in self.faces:
                return self.faces[alt], False
        return next(iter(self.faces.values())), False

    def has_style(self, bold: bool, italic: bool) -> bool:
        return (bold, italic) in self.faces


def font_dirs() -> list[str]:
    home = os.path.expanduser("~")
    if IS_WINDOWS:
        windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot") or "C:\\Windows"
        dirs = [os.path.join(windir, "Fonts")]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
        return dirs
    if sys.platform == "darwin":
        return ["/System/Library/Fonts", "/Library/Fonts", os.path.join(home, "Library", "Fonts")]
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return ["/usr/share/fonts", "/usr/local/share/fonts", os.path.join(data_home, "fonts"),
            os.path.join(home, ".fonts")]


def _font_files() -> list[str]:
    out = []
    seen = set()
    for root in font_dirs():
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames.sort()
            for name in sorted(filenames):
                if not name.lower().endswith(FONT_EXTS):
                    continue
                path = os.path.join(dirpath, name)
                try:
                    real = os.path.realpath(path)
                    if real in seen or not os.path.isfile(real):
                        continue
                    seen.add(real)
                except OSError:
                    continue
                out.append(path)
    return out


def _clean_name(value) -> str:
    text = str(value or "").strip()
    text = "".join(ch for ch in text if ch.isprintable())
    return text[:80]


def _read_faces(path: str) -> list[dict]:
    """Read family and style of every usable face in a font file (with fontTools, no rendering)."""
    from fontTools.ttLib import TTCollection, TTFont

    if os.path.getsize(path) > MAX_FONT_FILE:
        return []
    faces = []
    if path.lower().endswith((".ttc", ".otc")):
        col = TTCollection(path, lazy=True)
        items = list(enumerate(col.fonts))
    else:
        items = [(0, TTFont(path, lazy=True))]
    for index, font in items:
        try:
            if not ("glyf" in font or "CFF " in font or "CFF2" in font):
                continue  # bitmap or color-only font
            name = font["name"]
            family = _clean_name(name.getDebugName(1))
            sub = (name.getDebugName(2) or "").lower()
            if not family or family.startswith("."):
                continue
            fs_type = 0
            bold = "bold" in sub or "black" in sub or "heavy" in sub
            italic = "italic" in sub or "oblique" in sub
            if "OS/2" in font:
                os2 = font["OS/2"]
                fs_type = int(getattr(os2, "fsType", 0))
                sel = int(getattr(os2, "fsSelection", 0))
                italic = bool(sel & 1) or italic
                bold = bool(sel & 32) or bold
            if (fs_type & 0x000F) == 2 or fs_type & 0x0200:
                continue  # "restricted license" or "bitmap embedding only": may not be embedded
            if "cmap" not in font:
                continue
            faces.append({"family": family, "bold": bold, "italic": italic, "index": index,
                          "subset_ok": not (fs_type & 0x0100)})
        except Exception:
            continue
    return faces


class Catalog:
    def __init__(self):
        self.families: dict[str, Family] = {}
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._started = False

    # ------------------------------------------------------------------ building
    def start(self) -> None:
        """Scan fonts in the background (reading font files only, no PDF engine calls)."""
        with self._lock:
            if self._started:
                return
            self._started = True
        threading.Thread(target=self._build, name="pdfdesk-fonts", daemon=True).start()

    def wait(self, timeout: float | None = 30.0) -> None:
        self.start()
        self._ready.wait(timeout)

    def _cache_path(self):
        try:
            from pdfdesk.config import cache_dir
            return cache_dir() / "fonts.json"
        except Exception:
            return None

    def _build(self) -> None:
        try:
            families = self._scan()
        except Exception:
            families = {}
        for name, styles in BUNDLED.items():
            if name not in families:
                fam = Family(name, "bundled")
                for (b, i), code in styles.items():
                    fam.faces[(b, i)] = Face(name, b, i, code=code, source="bundled")
                families[name] = fam
        for name, styles in BUILTIN.items():
            key = name if name not in families else f"{name} (PDF)"
            fam = Family(key, "builtin")
            for (b, i), code in styles.items():
                fam.faces[(b, i)] = Face(key, b, i, code=code, source="builtin", subset_ok=False)
            families[key] = fam
        self.families = families  # swapped in whole, so the window never sees a half-built list
        self._ready.set()

    def _scan(self) -> dict[str, Family]:
        cache_path = self._cache_path()
        cache = {}
        if cache_path is not None:
            try:
                with open(cache_path, "r", encoding="utf-8") as fh:
                    stored = json.load(fh)
                if isinstance(stored, dict) and stored.get("version") == CACHE_VERSION:
                    cache = stored.get("files") or {}
            except Exception:
                cache = {}
        new_cache = {}
        families: dict[str, Family] = {}
        for path in _font_files():
            try:
                st = os.stat(path)
            except OSError:
                continue
            stamp = [int(st.st_mtime), st.st_size]
            entry = cache.get(path)
            if isinstance(entry, dict) and entry.get("stamp") == stamp and isinstance(entry.get("faces"), list):
                faces = entry["faces"]
            else:
                try:
                    faces = _read_faces(path)
                except Exception:
                    faces = []
            new_cache[path] = {"stamp": stamp, "faces": faces}
            for f in faces:
                try:
                    family = _clean_name(f["family"])
                    key = (bool(f["bold"]), bool(f["italic"]))
                    fam = families.setdefault(family, Family(family, "system"))
                    if key not in fam.faces:
                        fam.faces[key] = Face(family, key[0], key[1], path=path, index=int(f.get("index", 0)),
                                              subset_ok=bool(f.get("subset_ok", True)))
                except Exception:
                    continue
        if cache_path is not None and new_cache != cache:
            try:
                tmp = cache_path.with_suffix(".tmp")
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump({"version": CACHE_VERSION, "files": new_cache}, fh)
                os.replace(tmp, cache_path)
            except Exception:
                pass
        return families

    # ------------------------------------------------------------------ lookups
    def names(self) -> list[str]:
        self.wait()
        return sorted(self.families, key=lambda s: s.casefold())

    def get(self, name: str) -> Family | None:
        self.wait()
        return self.families.get(name)

    def resolve(self, name: str | None) -> str:
        """A family that exists, given a wanted name (falls back to the default)."""
        self.wait()
        if name and name in self.families:
            return name
        if name:
            low = name.casefold()
            for fam in self.families:
                if fam.casefold() == low:
                    return fam
        return self.default_family()

    def default_family(self) -> str:
        self.wait()
        for cand in DEFAULT_CANDIDATES:
            if cand in self.families:
                return cand
        return "Helvetica"

    def face(self, family: str | None, bold: bool = False, italic: bool = False) -> tuple[Face, bool]:
        fam = self.get(self.resolve(family))
        if fam is None:
            fam = self.families.get("Helvetica")
        if fam is None:  # the list isn't ready (very slow disk): use the built-in Helvetica
            code = BUILTIN["Helvetica"].get((bold, italic), "helv")
            return Face("Helvetica", bold, italic, code=code, source="builtin", subset_ok=False), True
        return fam.face(bold, italic)

    def match_pdf_font(self, pdf_name: str, bold: bool = False, italic: bool = False) -> tuple[str | None, bool, bool]:
        """Find an installed family for a font name found in a PDF, such as "ABCDEF+Calibri-Bold".
        Returns (family or None, bold, italic)."""
        self.wait()
        name = (pdf_name or "")[:100]  # names come from the PDF: keep them short before any matching
        if "+" in name[:8]:
            name = name.split("+", 1)[1]
        style = ""
        for sep in (",", "-"):
            if sep in name:
                name, style = name.split(sep, 1)
                break
        low_style = style.lower()
        lowname = name.lower()
        bold = bold or any(w in low_style for w in ("bold", "black", "heavy", "semibold", "demi")) or lowname.endswith("bold")
        italic = italic or "italic" in low_style or "oblique" in low_style or lowname.endswith("italic")
        base = lowname.replace(" ", "")
        changed = True
        while changed:  # strip style words and vendor endings one at a time (no backtracking regex)
            changed = False
            for suffix in ("psmt", "mt", "ps", "std", "pro", "bold", "italic", "oblique", "regular"):
                if base.endswith(suffix) and len(base) > len(suffix):
                    base = base[: -len(suffix)]
                    changed = True
        base = base or lowname.replace(" ", "")
        norm = {re.sub(r"[^a-z0-9]", "", f.lower()): f for f in self.families}
        candidates = ALIASES.get(base, []) + ALIASES.get(lowname.replace(" ", ""), [])
        for cand in candidates:
            if cand in self.families:
                return cand, bold, italic
        key = re.sub(r"[^a-z0-9]", "", base)
        if key in norm:
            return norm[key], bold, italic
        for nkey, fam in norm.items():
            if len(key) >= 4 and (nkey.startswith(key) or key.startswith(nkey)) and len(nkey) >= 4:
                return fam, bold, italic
        return None, bold, italic


_catalog = Catalog()


def catalog() -> Catalog:
    return _catalog


# --------------------------------------------------------------------------- font data

@lru_cache(maxsize=16)
def _face_bytes(source: str, path: str, index: int, code: str) -> bytes:
    if source == "bundled":
        import pymupdf_fonts
        return pymupdf_fonts.myfont(code)
    if source == "builtin":
        import pymupdf as fitz
        return fitz.Font(code).buffer
    if path.lower().endswith((".ttc", ".otc")):
        # Pull the one face out of the collection so it can be embedded on its own.
        from fontTools.ttLib import TTFont
        font = TTFont(path, fontNumber=index)
        buf = io.BytesIO()
        font.save(buf)
        return buf.getvalue()
    with open(path, "rb") as fh:
        return fh.read()


def face_bytes(face: Face) -> bytes:
    return _face_bytes(face.source, face.path, face.index, face.code)


_subset_cache: OrderedDict = OrderedDict()
_subset_lock = threading.Lock()


def subset_bytes(face: Face, text: str) -> bytes:
    """The font with only the letters in `text` (plus basics), ready to embed.
    Falls back to the whole font if subsetting isn't allowed or fails."""
    data = face_bytes(face)
    if not face.subset_ok or face.source == "builtin":
        return data
    chars = set(text) | set(" .,-")
    key = (face.key, hashlib.sha256("".join(sorted(chars)).encode("utf-8", "surrogatepass")).hexdigest())
    with _subset_lock:
        hit = _subset_cache.get(key)
        if hit is not None:
            _subset_cache.move_to_end(key)
            return hit
    try:
        from fontTools import subset as ft_subset
        from fontTools.ttLib import TTFont
        font = TTFont(io.BytesIO(data), lazy=False)
        opts = ft_subset.Options()
        opts.layout_features = ["*"]
        opts.name_IDs = ["*"]
        opts.name_languages = ["*"]
        opts.notdef_outline = True
        opts.glyph_names = False
        opts.hinting = True
        opts.ignore_missing_unicodes = True
        sub = ft_subset.Subsetter(opts)
        sub.populate(text="".join(chars))
        sub.subset(font)
        buf = io.BytesIO()
        font.save(buf)
        result = buf.getvalue()
    except Exception:
        result = data
    with _subset_lock:
        _subset_cache[key] = result
        while len(_subset_cache) > 128:
            _subset_cache.popitem(last=False)
    return result


# --------------------------------------------------------------------------- PDF engine fonts (GUI thread)

_mupdf_fonts: dict = {}


def mupdf_font(face: Face):
    """A PyMuPDF Font for measuring text. Call from the GUI thread only."""
    import pymupdf as fitz
    font = _mupdf_fonts.get(face.key)
    if font is None:
        if face.source == "builtin":
            font = fitz.Font(face.code)
        else:
            font = fitz.Font(fontbuffer=face_bytes(face))
        if len(_mupdf_fonts) > 64:
            _mupdf_fonts.clear()
        _mupdf_fonts[face.key] = font
    return font


def text_width(family: str, bold: bool, italic: bool, text: str, size: float) -> float:
    face, _real = catalog().face(family, bold, italic)
    try:
        return mupdf_font(face).text_length(text, fontsize=size)
    except Exception:
        return len(text) * size * 0.55


# --------------------------------------------------------------------------- on-screen fonts (Qt)

_qt_names: dict[str, list[str]] = {}


def qt_families(family: str) -> list[str]:
    """Family names Qt should use to draw this font on screen (registers bundled fonts with Qt)."""
    if family in _qt_names:
        return _qt_names[family]
    names = [family]
    fam = catalog().get(family)
    try:
        from PySide6.QtCore import QByteArray
        from PySide6.QtGui import QFontDatabase
        known = set(QFontDatabase.families())
        if fam is not None and fam.source == "builtin":
            base = family.replace(" (PDF)", "")
            names = [n for n in QT_FALLBACKS.get(base, []) if n in known] or [family]
        elif fam is not None and family not in known:
            registered = []
            for face in fam.faces.values():
                try:
                    fid = QFontDatabase.addApplicationFontFromData(QByteArray(face_bytes(face)))
                    if fid >= 0:
                        registered += QFontDatabase.applicationFontFamilies(fid)
                except Exception:
                    continue
            names = list(dict.fromkeys(registered + [family]))
    except Exception:
        pass
    _qt_names[family] = names
    return names


def family_from_qt(qt_name: str) -> str | None:
    """Map a family name Qt reports back to a catalog family."""
    for fam, names in _qt_names.items():
        if qt_name in names:
            return fam
    cat = catalog()
    cat.wait()
    if qt_name in cat.families:
        return qt_name
    return None


def is_monospace(family: str) -> bool:
    low = family.lower()
    return any(w in low for w in ("mono", "courier", "consol", "code", "typewriter"))
