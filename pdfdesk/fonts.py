"""Font helpers for writing text into PDFs.

The 14 standard PDF fonts only cover Latin-1, so text with characters such as
Romanian ș/ț or ă falls back to a Unicode font that ships with pymupdf-fonts
(Noto Sans / Space Mono). Everything is bundled, so this works offline.
"""
from __future__ import annotations

import os
import secrets
from functools import lru_cache

import pymupdf as fitz

try:  # optional, but listed in requirements.txt
    import pymupdf_fonts  # noqa: F401  (only checks that the font pack is installed)
    HAVE_FONT_PACK = True
except Exception:
    HAVE_FONT_PACK = False

FAMILIES = ("sans", "serif", "mono")

_BASE14 = {
    ("sans", False, False): "helv",
    ("sans", True, False): "hebo",
    ("sans", False, True): "heit",
    ("sans", True, True): "hebi",
    ("serif", False, False): "tiro",
    ("serif", True, False): "tibo",
    ("serif", False, True): "tiit",
    ("serif", True, True): "tibi",
    ("mono", False, False): "cour",
    ("mono", True, False): "cobo",
    ("mono", False, True): "coit",
    ("mono", True, True): "cobi",
}

_UNICODE_PACK = {
    ("sans", False, False): "notos",
    ("sans", True, False): "notosbo",
    ("sans", False, True): "notosit",
    ("sans", True, True): "notosbi",
    ("mono", False, False): "spacemo",
    ("mono", True, False): "spacembo",
    ("mono", False, True): "spacemit",
    ("mono", True, True): "spacembi",
}

_SYSTEM_FALLBACKS = [
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/google-noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "arial.ttf"),
    os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "segoeui.ttf"),
]


def is_latin1(text: str) -> bool:
    try:
        text.encode("latin-1")
        return True
    except UnicodeEncodeError:
        return False


@lru_cache(maxsize=None)
def _unicode_font_buffer(family: str, bold: bool, italic: bool) -> tuple[str, bytes]:
    """Return (code, font file bytes) for a Unicode-capable font."""
    if HAVE_FONT_PACK:
        code = _UNICODE_PACK.get((family, bold, italic)) or _UNICODE_PACK[("sans", bold, italic)]
        try:
            return code, fitz.Font(code).buffer
        except Exception:
            pass
    for path in _SYSTEM_FALLBACKS:
        if os.path.exists(path):
            with open(path, "rb") as fh:
                return "pdfdeskuni", fh.read()
    # MuPDF's built-in CJK fallback also contains Latin glyphs.
    font = fitz.Font(ordering=0)
    return "pdfdeskcjk", font.buffer


class FontChoice:
    """A font that can be registered on a page and measured."""

    def __init__(self, code: str, buffer: bytes | None):
        self.code = code
        self.buffer = buffer
        self._font = None

    @property
    def font(self) -> fitz.Font:
        if self._font is None:
            self._font = fitz.Font(fontbuffer=self.buffer) if self.buffer else fitz.Font(self.code)
        return self._font

    def register(self, page: fitz.Page) -> str:
        if not self.buffer and _name_is_free(page, self.code):
            page.insert_font(fontname=self.code)
            return self.code
        return _insert_private(page, self.buffer or fitz.Font(self.code).buffer)

    def width(self, text: str, size: float) -> float:
        return self.font.text_length(text, fontsize=size)


def choose_font(text: str, family: str = "sans", bold: bool = False, italic: bool = False) -> FontChoice:
    if family not in FAMILIES:
        family = "sans"
    if is_latin1(text):
        return FontChoice(_BASE14[(family, bold, italic)], None)
    code, buf = _unicode_font_buffer(family, bold, italic)
    return FontChoice(code, buf)


def unicode_font() -> FontChoice:
    code, buf = _unicode_font_buffer("sans", False, False)
    return FontChoice(code, buf)


def ensure_wrapped(page: fitz.Page) -> None:
    """Some PDFs leave their coordinate system changed at the end of the page content.
    Wrapping the old content in q/Q keeps anything we add in the right place."""
    try:
        if not page.is_wrapped:
            page.wrap_contents()
    except Exception:
        pass


def _name_is_free(page: fitz.Page, name: str) -> bool:
    """True if the page has no font of its own under this resource name. A PDF could define its own
    "helv" (with other glyphs), and new text must never be drawn with that by accident."""
    try:
        return name not in {f[4] for f in page.get_fonts(full=False)}
    except Exception:
        return False


def _insert_private(page: fitz.Page, buffer: bytes) -> str:
    """Add a font to the page under a fresh random resource name nobody can predict."""
    for _ in range(5):
        name = "PD" + secrets.token_hex(5)
        if _name_is_free(page, name):
            break
    page.insert_font(fontname=name, fontbuffer=buffer)
    return name


def register_font(page: fitz.Page, family: str, bold: bool, italic: bool, text: str) -> str:
    """Make a font from PDF Desk's font list usable on `page` for `text`; returns its resource name.
    Only the letters in `text` are embedded. The classic PDF fonts need no embedding at all."""
    from pdfdesk import fontcatalog
    face, _real = fontcatalog.catalog().face(family, bold, italic)
    if face.source == "builtin":
        if is_latin1(text):
            return FontChoice(face.code, None).register(page)
        generic = "serif" if "Times" in face.family else "mono" if "Courier" in face.family else "sans"
        return choose_font(text, generic, bold, italic).register(page)
    return _insert_private(page, fontcatalog.subset_bytes(face, text))


def insert_text(page: fitz.Page, point, text: str, size: float = 12, color=(0, 0, 0),
                family: str = "sans", bold: bool = False, italic: bool = False,
                rotate: int = 0, morph=None, opacity: float = 1.0, overlay: bool = True,
                render_mode: int = 0) -> None:
    """Insert one line of text with a font that can actually show it. `family` is "sans", "serif",
    "mono" or the name of any font in PDF Desk's font list."""
    ensure_wrapped(page)
    if family in FAMILIES:
        name = choose_font(text, family, bold, italic).register(page)
    else:
        name = register_font(page, family, bold, italic, text)
    page.insert_text(point, text, fontsize=size, fontname=name, color=color, rotate=rotate,
                     morph=morph, fill_opacity=opacity, stroke_opacity=opacity,
                     overlay=overlay, render_mode=render_mode)


def text_width(text: str, size: float, family: str = "sans", bold: bool = False, italic: bool = False) -> float:
    if family not in FAMILIES:
        from pdfdesk import fontcatalog
        return fontcatalog.text_width(family, bold, italic, text, size)
    return choose_font(text, family, bold, italic).width(text, size)


def span_style(span: dict) -> tuple[str, bool, bool]:
    """Guess (family, bold, italic) from a text span returned by get_text('dict')."""
    name = (span.get("font") or "").lower()
    flags = span.get("flags", 0)
    bold = bool(flags & 16) or "bold" in name or "black" in name or "heavy" in name
    italic = bool(flags & 2) or "italic" in name or "oblique" in name
    sans_names = ("sans", "arial", "helvet", "verdana", "calibri", "tahoma", "segoe", "inter", "roboto", "carlito")
    if flags & 8 or "mono" in name or "courier" in name or "consol" in name:
        family = "mono"
    elif any(w in name for w in sans_names):
        family = "sans"  # the name wins: some PDFs mark sans fonts as serif
    elif (flags & 4 or "times" in name or "serif" in name or "georgia" in name or "garamond" in name
          or "cambria" in name or "roman" in name):
        family = "serif"
    else:
        family = "sans"
    return family, bold, italic


def int_to_rgb(value: int) -> tuple[float, float, float]:
    return (((value >> 16) & 255) / 255.0, ((value >> 8) & 255) / 255.0, (value & 255) / 255.0)


def hex_to_rgb(value: str) -> tuple[float, float, float]:
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    try:
        return (int(value[0:2], 16) / 255.0, int(value[2:4], 16) / 255.0, int(value[4:6], 16) / 255.0)
    except Exception:
        return (0.0, 0.0, 0.0)


def to_rgb(color) -> tuple[float, float, float]:
    """Accept gray (1 value), RGB (3) or CMYK (4) colors as stored in PDFs and return RGB."""
    try:
        vals = [max(0.0, min(1.0, float(c))) for c in (color if isinstance(color, (list, tuple)) else [color])]
    except (TypeError, ValueError):
        return (0.0, 0.0, 0.0)
    if len(vals) == 1:
        return (vals[0], vals[0], vals[0])
    if len(vals) == 4:
        c, m, y, k = vals
        return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    if len(vals) >= 3:
        return (vals[0], vals[1], vals[2])
    return (0.0, 0.0, 0.0)


def rgb_to_hex(rgb) -> str:
    if not rgb:
        return "#000000"
    r, g, b = (max(0, min(255, round(c * 255))) for c in to_rgb(rgb))
    return f"#{r:02x}{g:02x}{b:02x}"

# The largest page picture PDF Desk will make in one go (about 120 MB of memory).
MAX_PIXELS = 40_000_000


def safe_zoom(rect, zoom: float) -> float:
    """Lower a render zoom so a (possibly huge) page can't use unlimited memory."""
    area = max(1.0, rect.width * rect.height) * zoom * zoom
    if area > MAX_PIXELS:
        zoom *= (MAX_PIXELS / area) ** 0.5
    return zoom
