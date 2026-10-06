"""Formatted text boxes (fonts, sizes, bold, colors, alignment, lists...), like a box in Word.

A box is stored as a normal PDF "FreeText" annotation, so every PDF viewer shows it. PDF Desk
draws its look itself with the PDF engine's HTML layout, and keeps the formatting in a private
key so the box can be edited again later. A plain-text copy goes into /Contents and an Acrobat
style rich-text copy into /RC, so other editors can still edit the text.

Everything read back from a PDF is treated as untrusted: the stored formatting is checked
field by field, fonts are only looked up in PDF Desk's own font list, and all text is escaped.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field, replace
from html.parser import HTMLParser

import pymupdf as fitz

from pdfdesk import appearance, fontcatalog

KEY = "PDFDeskRT"
KIND = "RichText"
VERSION = 1
MAX_CHARS = 50000
MAX_RUNS = 8000
MAX_PARAS = 5000
MIN_SIZE, MAX_SIZE = 1.0, 400.0
SIZES = [6, 7, 8, 9, 10, 10.5, 11, 12, 14, 16, 18, 20, 22, 24, 26, 28, 32, 36, 40, 48, 60, 72, 96]
ALIGNS = ("left", "center", "right", "justify")
LISTS = ("", "bullet", "number")
SPACINGS = (1.0, 1.15, 1.5, 2.0, 2.5, 3.0)
_COLOR = re.compile(r"#[0-9a-fA-F]{6}")
LIST_INDENT = 1.4   # em


def _color(value, default: str | None = "#000000") -> str | None:
    if isinstance(value, str) and _COLOR.fullmatch(value):
        return value.lower()
    return default


def _num(value, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


@dataclass
class Run:
    text: str
    font: str = ""
    size: float = 12.0
    bold: bool = False
    italic: bool = False
    underline: bool = False
    strike: bool = False
    color: str = "#000000"
    highlight: str | None = None
    valign: str = ""   # "" | "super" | "sub"

    def same_style(self, other: "Run") -> bool:
        return (self.font, self.size, self.bold, self.italic, self.underline, self.strike, self.color,
                self.highlight, self.valign) == (other.font, other.size, other.bold, other.italic, other.underline,
                                                 other.strike, other.color, other.highlight, other.valign)

    def to_dict(self) -> dict:
        d = {"t": self.text, "f": self.font, "s": round(self.size, 2), "c": self.color}
        for key, val in (("b", self.bold), ("i", self.italic), ("u", self.underline), ("x", self.strike)):
            if val:
                d[key] = 1
        if self.highlight:
            d["h"] = self.highlight
        if self.valign:
            d["v"] = self.valign
        return d

    @classmethod
    def from_dict(cls, d) -> "Run":
        if not isinstance(d, dict):
            raise ValueError("bad run")
        text = d.get("t", "")
        if not isinstance(text, str):
            raise ValueError("bad text")
        font = d.get("f", "")
        font = font[:80] if isinstance(font, str) else ""
        return cls(text=text, font=font, size=_num(d.get("s"), MIN_SIZE, MAX_SIZE, 12.0),
                   bold=bool(d.get("b")), italic=bool(d.get("i")), underline=bool(d.get("u")),
                   strike=bool(d.get("x")), color=_color(d.get("c")), highlight=_color(d.get("h"), None),
                   valign=d.get("v") if d.get("v") in ("super", "sub") else "")


@dataclass
class Para:
    runs: list[Run] = field(default_factory=list)
    align: str = "left"
    spacing: float = 1.0
    list: str = ""

    def text(self) -> str:
        return "".join(r.text for r in self.runs)

    def to_dict(self) -> dict:
        d = {"r": [r.to_dict() for r in self.runs]}
        if self.align != "left":
            d["a"] = self.align
        if self.spacing != 1.0:
            d["sp"] = self.spacing
        if self.list:
            d["l"] = self.list
        return d

    @classmethod
    def from_dict(cls, d) -> "Para":
        if not isinstance(d, dict) or not isinstance(d.get("r", []), list):
            raise ValueError("bad paragraph")
        return cls(runs=[Run.from_dict(r) for r in d.get("r", [])[:MAX_RUNS]],
                   align=d.get("a") if d.get("a") in ALIGNS else "left",
                   spacing=_num(d.get("sp"), 0.5, 5.0, 1.0),
                   list=d.get("l") if d.get("l") in LISTS else "")


@dataclass
class Box:
    paras: list[Para] = field(default_factory=list)
    fill: str | None = None
    border: str | None = None
    border_width: float = 0.0
    opacity: float = 1.0
    padding: float = 2.0
    auto_width: bool = True
    min_height: float = 0.0

    # ------------------------------------------------------------------ text
    def plain_text(self) -> str:
        lines = []
        number = 0
        for p in self.paras:
            number = number + 1 if p.list == "number" else 0
            prefix = "• " if p.list == "bullet" else (f"{number}. " if p.list == "number" else "")
            lines.append(prefix + p.text())
        return "\n".join(lines)

    def is_empty(self) -> bool:
        return not any(p.text().strip() for p in self.paras)

    def first_run(self) -> Run:
        for p in self.paras:
            for r in p.runs:
                return r
        return Run("")

    def runs(self):
        for p in self.paras:
            yield from p.runs

    def normalize(self) -> "Box":
        """Merge neighbouring runs with the same style and drop empty ones."""
        for p in self.paras:
            merged: list[Run] = []
            for r in p.runs:
                if not r.text:
                    continue
                if merged and merged[-1].same_style(r):
                    merged[-1].text += r.text
                else:
                    merged.append(r)
            if not merged and p.runs:
                merged = [replace(p.runs[0], text="")]
            p.runs = merged
        return self

    # ------------------------------------------------------------------ saving
    def to_dict(self) -> dict:
        d = {"v": VERSION, "p": [p.to_dict() for p in self.paras], "pad": self.padding,
             "aw": int(self.auto_width), "mh": round(self.min_height, 2), "op": round(self.opacity, 3)}
        if self.fill:
            d["fill"] = self.fill
        if self.border and self.border_width > 0:
            d["bc"] = self.border
            d["bw"] = self.border_width
        return d

    @classmethod
    def from_dict(cls, d) -> "Box":
        if not isinstance(d, dict) or not isinstance(d.get("p"), list):
            raise ValueError("bad box")
        box = cls(paras=[Para.from_dict(p) for p in d["p"][:MAX_PARAS]],
                  fill=_color(d.get("fill"), None), border=_color(d.get("bc"), None),
                  border_width=_num(d.get("bw"), 0, 20, 0), opacity=_num(d.get("op"), 0.05, 1, 1),
                  padding=_num(d.get("pad"), 0, 50, 2), auto_width=bool(d.get("aw", 1)),
                  min_height=_num(d.get("mh"), 0, 20000, 0))
        if sum(len(p.text()) for p in box.paras) > MAX_CHARS:
            raise ValueError("too much text")
        if sum(len(p.runs) for p in box.paras) > MAX_RUNS:
            raise ValueError("too many pieces")
        return box

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, text: str) -> "Box":
        if not isinstance(text, str) or len(text) > MAX_CHARS * 8 + 200000:
            raise ValueError("too large")
        return cls.from_dict(json.loads(text))

    # ------------------------------------------------------------------ whole-box formatting
    def set_all(self, **changes) -> None:
        """Apply run formatting (font, size, bold, color...) or paragraph formatting (align,
        spacing, list) to the whole box."""
        para_keys = {"align", "spacing", "list"}
        for p in self.paras:
            for key in para_keys & changes.keys():
                setattr(p, key, changes[key])
            for r in p.runs:
                for key, val in changes.items():
                    if key not in para_keys:
                        setattr(r, key, val)

    def all_have(self, attr: str) -> bool:
        runs = [r for r in self.runs() if r.text.strip()] or list(self.runs())
        return bool(runs) and all(getattr(r, attr) for r in runs)


def _bump(size: float, step: int) -> float:
    if step > 0:
        return next((s for s in SIZES if s > size + 0.01), min(MAX_SIZE, size + 12))
    return next((s for s in reversed(SIZES) if s < size - 0.01), max(MIN_SIZE, size - 1))


def apply_change(box: Box, change: dict) -> None:
    """Apply one change from the formatting toolbar to every piece of text in the box."""
    for key, value in change.items():
        if key == "grow":
            for r in box.runs():
                r.size = _bump(r.size, int(value))
        elif key == "clear":
            first = box.first_run()
            for r in box.runs():
                r.font, r.size, r.color = first.font, first.size, "#000000"
                r.bold = r.italic = r.underline = r.strike = False
                r.highlight, r.valign = None, ""
        elif key == "size":
            box.set_all(size=_num(value, MIN_SIZE, MAX_SIZE, 12.0))
        elif key in ("color", "highlight"):
            box.set_all(**{key: _color(value, None if key == "highlight" else "#000000")})
        elif key in ("align", "list"):
            if value in (ALIGNS if key == "align" else LISTS):
                box.set_all(**{key: value})
        elif key == "spacing":
            box.set_all(spacing=_num(value, 0.5, 5.0, 1.0))
        elif key == "valign":
            box.set_all(valign=value if value in ("super", "sub") else "")
        elif key in ("font", "bold", "italic", "underline", "strike"):
            box.set_all(**{key: value if key == "font" else bool(value)})


def from_plain(text: str, font: str, size: float = 12.0, color: str = "#000000", bold=False, italic=False,
               align: str = "left") -> Box:
    lines = text[:MAX_CHARS].replace("\r\n", "\n").split("\n")[:MAX_PARAS] or [""]
    paras = [Para([Run(line, font, size, bold, italic, color=color)], align=align) for line in lines]
    return Box(paras=paras)


# --------------------------------------------------------------------------- HTML for the PDF engine

class _Fonts:
    """Collects the font faces a box uses and makes the @font-face rules and font archive."""

    def __init__(self):
        self.cat = fontcatalog.catalog()
        self.faces: dict[str, tuple] = {}   # face key -> (alias, face, text)
        self.order: list[str] = []

    def alias(self, run: Run, extra_text: str = "") -> str:
        face, _real = self.cat.face(run.font, run.bold, run.italic)
        hit = self.faces.get(face.key)
        if hit is None:
            alias = f"PDFD{len(self.order)}"
            self.faces[face.key] = (alias, face, run.text + extra_text)
            self.order.append(face.key)
            return alias
        alias, f, text = hit
        self.faces[face.key] = (alias, f, text + run.text + extra_text)
        return alias

    def css_and_archive(self) -> tuple[str, fitz.Archive]:
        arch = fitz.Archive()
        rules = []
        for k, key in enumerate(self.order):
            alias, face, text = self.faces[key]
            data = fontcatalog.subset_bytes(face, text + "0123456789.•")
            name = f"font{k}.{'cff' if face.source == 'builtin' else 'ttf'}"
            arch.add((data, name))
            rules.append(f"@font-face {{font-family: {alias}; src: url({name});}}")
        return "\n".join(rules), arch


def _run_html(run: Run, fonts: _Fonts) -> str:
    alias = fonts.alias(run)
    styles = [f"font-family:{alias}", f"font-size:{run.size:.2f}pt", f"color:{run.color}",
              "font-weight:normal", "font-style:normal"]
    if run.highlight:
        styles.append(f"background-color:{run.highlight}")
    if run.underline:
        styles.append("text-decoration:underline")
    elif run.strike:
        styles.append("text-decoration:line-through")
    if run.valign:
        styles.append(f"vertical-align:{run.valign}")
        styles.append(f"font-size:{run.size * 0.65:.2f}pt")
    text = html.escape(run.text.replace("\t", "    "), quote=False) or "&#160;"
    return f'<span style="{";".join(styles)}">{text}</span>'


def _para_style(p: Para) -> str:
    first = p.runs[0] if p.runs else Run("")
    styles = [f"text-align:{p.align}", f"font-size:{first.size:.2f}pt"]
    if p.spacing != 1.0:
        styles.append(f"line-height:{1.2 * p.spacing:.3f}")
    return ";".join(styles)


def to_html(box: Box) -> tuple[str, str, fitz.Archive]:
    fonts = _Fonts()
    parts = []
    k = 0
    paras = box.paras or [Para([Run("")])]
    while k < len(paras):
        p = paras[k]
        if p.list:
            tag = "ul" if p.list == "bullet" else "ol"
            first = p.runs[0] if p.runs else Run("")
            alias = fonts.alias(first, "•1234567890.")
            parts.append(f'<{tag} style="margin:0;padding-left:{LIST_INDENT}em;font-family:{alias};'
                         f'font-size:{first.size:.2f}pt;color:{first.color}">')
            while k < len(paras) and paras[k].list == p.list:
                q = paras[k]
                body = "".join(_run_html(r, fonts) for r in q.runs) or "&#160;"
                parts.append(f'<li style="{_para_style(q)}">{body}</li>')
                k += 1
            parts.append(f"</{tag}>")
            continue
        body = "".join(_run_html(r, fonts) for r in p.runs) or "&#160;"
        parts.append(f'<p style="{_para_style(p)}">{body}</p>')
        k += 1
    font_css, arch = fonts.css_and_archive()
    css = font_css + "\nbody {margin:0; padding:0; white-space:pre-wrap;}\np, li {margin:0;}"
    return "".join(parts), css, arch


# --------------------------------------------------------------------------- measuring & drawing

def _inset(box: Box) -> float:
    return box.padding + (box.border_width if box.border else 0)


def natural_width(box: Box) -> float:
    """Width of the longest paragraph without wrapping, plus padding."""
    widest = 0.0
    for p in box.paras:
        w = 0.0
        for r in p.runs:
            size = r.size * (0.65 if r.valign else 1)
            w += fontcatalog.text_width(r.font, r.bold, r.italic, r.text.replace("\t", "    "), size)
        if p.list:
            w += LIST_INDENT * (p.runs[0].size if p.runs else 12)
        widest = max(widest, w)
    return widest + 2 * _inset(box) + 3


def content_height(box: Box, width: float, parts=None) -> float:
    html_text, css, arch = parts or to_html(box)
    inner = max(4.0, width - 2 * _inset(box))
    story = fitz.Story(html=html_text, user_css=css, archive=arch)
    _more, filled = story.place(fitz.Rect(0, 0, inner, 100000))
    return fitz.Rect(filled).y1 + 2 * _inset(box)


def render(box: Box, width: float, height: float | None = None) -> tuple[fitz.Document, float]:
    """Draw the box on a temporary page `width` wide. Returns (document, height used)."""
    parts = to_html(box)
    need = content_height(box, width, parts)
    h = max(need, box.min_height, height or 0, 4.0)
    tmp = fitz.open()
    page = tmp.new_page(width=width, height=h)
    bw = box.border_width if box.border else 0
    if box.fill or bw:
        page.draw_rect(fitz.Rect(bw / 2, bw / 2, width - bw / 2, h - bw / 2),
                       color=_rgb(box.border) if bw else None,
                       fill=_rgb(box.fill) if box.fill else None, width=bw,
                       fill_opacity=box.opacity, stroke_opacity=box.opacity)
    inset = _inset(box)
    html_text, css, arch = parts
    inner = fitz.Rect(inset, inset, max(inset + 4, width - inset), max(inset + 4, need - inset + 2))
    spare, _scale = page.insert_htmlbox(inner, html_text, css=css, archive=arch, scale_low=1, opacity=box.opacity)
    if spare < 0:
        # a word is wider than the box: shrink the text rather than show nothing
        spare, _scale = page.insert_htmlbox(inner, html_text, css=css, archive=arch, scale_low=0,
                                            opacity=box.opacity)
        if spare < 0:
            raise ValueError("The text doesn't fit in the box. Make the box wider or the text smaller.")
    return tmp, h


def _rgb(hex_color: str | None):
    if not hex_color:
        return None
    return tuple(int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5))


# --------------------------------------------------------------------------- Acrobat rich text

def _css_family(name: str) -> str:
    return "'" + re.sub(r"[^\w \-]", "", name)[:60] + "'"


def to_rc(box: Box) -> str:
    """Rich text in the XHTML form Acrobat uses (/RC), so Acrobat can edit the box with its formatting."""
    first = box.first_run()
    body_style = (f"font-size:{first.size:.1f}pt;text-align:left;color:{first.color};font-weight:normal;"
                  f"font-style:normal;font-family:{_css_family(first.font)};font-stretch:normal")
    out = ['<?xml version="1.0"?><body xmlns="http://www.w3.org/1999/xhtml" '
           'xmlns:xfa="http://www.xfa.org/schema/xfa-data/1.0/" xfa:APIVersion="Acrobat:11.0.0" '
           f'xfa:spec="2.0.2" style="{body_style}">']
    for p in box.paras:
        out.append(f'<p dir="ltr" style="text-align:{p.align}">')
        for r in p.runs:
            style = [f"font-family:{_css_family(r.font)}", f"font-size:{r.size:.1f}pt", f"color:{r.color}",
                     f"font-weight:{'bold' if r.bold else 'normal'}", f"font-style:{'italic' if r.italic else 'normal'}"]
            deco = " ".join(d for d, on in (("underline", r.underline), ("line-through", r.strike)) if on)
            if deco:
                style.append(f"text-decoration:{deco}")
            if r.valign:
                style.append(f"vertical-align:{'super' if r.valign == 'super' else 'sub'}")
            out.append(f'<span style="{";".join(style)}">{html.escape(r.text, quote=False)}</span>')
        out.append("</p>")
    out.append("</body>")
    return "".join(out)


class _RcParser(HTMLParser):
    """Reads Acrobat-style rich text (/RC) into a Box. Only text and simple styles are used."""

    def __init__(self, default: Run):
        super().__init__(convert_charrefs=True)
        self.default = default
        self.stack: list[Run] = [default]
        self.paras: list[Para] = []
        self.cur: Para | None = None
        self.chars = 0

    def _style(self, base: Run, attrs) -> Run:
        run = replace(base, text="")
        style = (dict(attrs).get("style") or "")[:2000]
        for decl in style.split(";")[:40]:
            if ":" not in decl:
                continue
            key, val = (s.strip().lower()[:200] for s in decl.split(":", 1))
            if key == "font-size":
                m = re.match(r"([\d.]+)", val)
                if m:
                    run.size = _num(m.group(1), MIN_SIZE, MAX_SIZE, run.size)
            elif key == "font-weight":
                run.bold = val in ("bold", "bolder") or (val.isdigit() and int(val) >= 600)
            elif key == "font-style":
                run.italic = val in ("italic", "oblique")
            elif key == "color":
                if re.match(r"^#[0-9a-f]{6}$", val):
                    run.color = val
                elif re.match(r"^#[0-9a-f]{3}$", val):
                    run.color = "#" + "".join(c * 2 for c in val[1:])
            elif key == "text-decoration":
                run.underline = "underline" in val
                run.strike = "line-through" in val
            elif key == "font-family":
                fam = val.split(",")[0].strip().strip("'\"")
                if fam:
                    run.font = fam[:80]
            elif key == "font":
                words = val.replace(",", " ").split()
                for k, word in enumerate(words):
                    if word.endswith("pt") and word[:-2].replace(".", "", 1).isdigit():
                        run.size = _num(word[:-2], MIN_SIZE, MAX_SIZE, run.size)
                        if k + 1 < len(words):
                            run.font = words[k + 1].strip("'\"")[:80]
                        break
        return run

    def handle_starttag(self, tag, attrs):
        if len(self.stack) > 200 or len(self.paras) >= MAX_PARAS:
            return
        base = self.stack[-1]
        if tag in ("p", "div"):
            self.cur = Para([], align=self._align(attrs))
            self.paras.append(self.cur)
        elif tag == "br":
            self.cur = Para([], align=self.cur.align if self.cur else "left")
            self.paras.append(self.cur)
            return
        run = self._style(base, attrs)
        if tag == "b":
            run.bold = True
        elif tag == "i":
            run.italic = True
        elif tag == "u":
            run.underline = True
        elif tag in ("s", "strike"):
            run.strike = True
        self.stack.append(run)

    def _align(self, attrs) -> str:
        style = (dict(attrs).get("style") or "").lower()
        m = re.search(r"text-align\s*:\s*(left|center|right|justify)", style)
        return m.group(1) if m else "left"

    def handle_endtag(self, tag):
        if tag == "br":
            return
        if len(self.stack) > 1:
            self.stack.pop()

    def handle_data(self, data):
        if self.chars > MAX_CHARS or len(self.paras) >= MAX_PARAS:
            return
        if self.cur is None:
            self.cur = Para([])
            self.paras.append(self.cur)
        data = data.replace("\r", "")
        for k, piece in enumerate(data.split("\n")):
            if k:
                self.cur = Para([], align=self.cur.align)
                self.paras.append(self.cur)
            if piece:
                self.chars += len(piece)
                self.cur.runs.append(replace(self.stack[-1], text=piece))


def from_rc(rc: str, default: Run) -> Box | None:
    try:
        parser = _RcParser(default)
        parser.feed(rc[:MAX_CHARS * 20])
        parser.close()
    except Exception:
        return None
    paras = parser.paras or [Para([replace(default, text="")])]
    cat = fontcatalog.catalog()
    for p in paras:
        for r in p.runs:
            fam, b, i = cat.match_pdf_font(r.font, r.bold, r.italic)
            r.font = fam or default.font
            r.bold, r.italic = b, i
    return Box(paras=paras).normalize()


# --------------------------------------------------------------------------- the annotation

def is_rich(doc: fitz.Document, xref: int) -> bool:
    return appearance.kind_of(doc, xref) == KIND


def read_box(doc: fitz.Document, xref: int) -> Box | None:
    t, v = appearance.get_key(doc, xref, KEY)
    if t != "string":
        return None
    try:
        return Box.from_json(v)
    except Exception:
        return None


def _da(box: Box) -> str:
    r = box.first_run()
    rgb = _rgb(r.color) or (0, 0, 0)
    return f"/Helv {r.size:.2f} Tf {rgb[0]:.3f} {rgb[1]:.3f} {rgb[2]:.3f} rg"


def _write(page: fitz.Page, xref: int, box: Box, vis_rect: fitz.Rect, keep_height: bool = False) -> fitz.Rect:
    """Lay out the box at vis_rect (visible page coordinates), write its look and keys.
    Returns the final visible rectangle."""
    doc = page.parent
    check_limits(box)
    pr = page.rect
    x0, y0 = vis_rect.x0, vis_rect.y0
    if box.auto_width:
        width = max(12.0, min(natural_width(box), max(24.0, pr.width - x0)))
    else:
        width = max(12.0, vis_rect.width)
    tmp, height = render(box, width, vis_rect.height if keep_height else None)
    final = fitz.Rect(x0, y0, x0 + width, y0 + height)
    unrot = (final * page.derotation_matrix).normalize()
    appearance.move_annot(page, xref, unrot)
    appearance.apply(doc, xref, tmp, page.rotation)
    doc.xref_set_key(xref, KEY, "<" + box.to_json().encode("ascii").hex() + ">")
    doc.xref_set_key(xref, appearance.KIND_KEY, "/" + KIND)
    appearance.set_contents(doc, xref, box.plain_text())
    doc.xref_set_key(xref, "RC", appearance.pdf_string(to_rc(box)))
    doc.xref_set_key(xref, "DA", appearance.pdf_string(_da(box)))
    first = box.first_run()
    doc.xref_set_key(xref, "DS", appearance.pdf_string(
        f"font: {first.size:.1f}pt {_css_family(first.font)}; color:{first.color}"))
    doc.xref_set_key(xref, "Q", str(ALIGNS.index(box.paras[0].align) if box.paras and box.paras[0].align != "justify" else 0))
    doc.xref_set_key(xref, "Rotate", str(page.rotation))
    doc.xref_set_key(xref, "CA", "null")
    for key in ("CL", "RD", "IT"):  # no callout line, no inner margins: the look is all in the appearance
        doc.xref_set_key(xref, key, "null")
    doc.xref_set_key(xref, "M", appearance.pdf_string(fitz.get_pdf_now()))
    if box.fill:
        r, g, b = _rgb(box.fill)
        doc.xref_set_key(xref, "C", f"[{r:.3f} {g:.3f} {b:.3f}]")
    else:
        doc.xref_set_key(xref, "C", "null")
    doc.xref_set_key(xref, "BS", f"<</W {box.border_width if box.border else 0:.2f}/S/S>>")
    return final


def check_limits(box: Box) -> None:
    """Boxes larger than what PDF Desk reads back would lose formatting later, so refuse them now."""
    chars = sum(len(r.text) for r in box.runs())
    if chars > MAX_CHARS:
        raise ValueError(f"This text box is too long ({chars:,} characters; the limit is {MAX_CHARS:,}).")
    if len(box.paras) > MAX_PARAS or sum(len(p.runs) for p in box.paras) > MAX_RUNS:
        raise ValueError("This text box has too many paragraphs or formatting changes.")


def add(page: fitz.Page, vis_rect: fitz.Rect, box: Box, author: str = "") -> int:
    """Create a formatted text box. vis_rect is in visible (rotated) page coordinates; with
    auto width only its top-left corner matters."""
    unrot = (fitz.Rect(vis_rect) * page.derotation_matrix).normalize()
    if unrot.is_empty:
        unrot = fitz.Rect(unrot.x0, unrot.y0, unrot.x0 + 20, unrot.y0 + 20)
    annot = page.add_freetext_annot(unrot, " ", fontsize=box.first_run().size, fontname="Helv",
                                    rotate=page.rotation)
    xref = annot.xref
    annot.set_info(title=author or "", subject="Text box")
    annot.update()
    _write(page, xref, box, fitz.Rect(vis_rect))
    return xref


def update(page: fitz.Page, xref: int, box: Box, vis_rect: fitz.Rect | None = None,
           keep_height: bool = False) -> fitz.Rect:
    """Redraw an existing box (after editing text or formatting, or resizing)."""
    if vis_rect is None:
        vis_rect = current_vis_rect(page, xref)
    return _write(page, xref, box, vis_rect, keep_height)


def current_vis_rect(page: fitz.Page, xref: int) -> fitz.Rect:
    annot = page.load_annot(xref)
    return (annot.rect * page.rotation_matrix).normalize()


def box_from_legacy(page: fitz.Page, annot, default_font: str | None = None) -> Box:
    """Turn a plain FreeText annotation (from PDF Desk 1.0 or another program) into a Box."""
    from pdfdesk import annots as annots_mod  # local import: annots imports nothing from here
    doc = page.parent
    size, color = annots_mod.freetext_style(doc, annot)
    cat = fontcatalog.catalog()
    family = default_font
    t, da = appearance.get_key(doc, annot.xref, "DA")
    bold = italic = False
    if t == "string":
        m = re.search(r"/([^\s/]{1,60})\s+[\d.]{1,12}\s+Tf", da[:300])
        if m:
            name = {"Helv": "Helvetica", "TiRo": "Times-Roman", "Cour": "Courier", "HeBo": "Helvetica-Bold",
                    "TiBo": "Times-Bold", "CoBo": "Courier-Bold"}.get(m.group(1), m.group(1))
            fam, bold, italic = cat.match_pdf_font(name)
            family = fam or family
    family = cat.resolve(family)
    hex_color = "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, round(c * 255))) for c in color[:3]))
    default = Run("", family, size, bold, italic, color=hex_color)
    box = None
    t, rc = appearance.get_key(doc, annot.xref, "RC")
    if t == "string" and rc.strip():
        box = from_rc(rc, default)
    if box is None or box.is_empty():
        text = (annot.info or {}).get("content", "")
        t, q = appearance.get_key(doc, annot.xref, "Q")
        align = {"1": "center", "2": "right"}.get(q.strip(), "left") if t == "int" else "left"
        box = from_plain(text, family, size, hex_color, bold, italic, align)
    colors = annot.colors or {}
    fill = colors.get("fill")
    if fill:
        box.fill = "#{:02x}{:02x}{:02x}".format(*(max(0, min(255, round(c * 255))) for c in fill[:3]))
    border = (annot.border or {}).get("width") or 0
    if border and border > 0:
        box.border = hex_color
        box.border_width = float(border)
    box.auto_width = False
    return box
