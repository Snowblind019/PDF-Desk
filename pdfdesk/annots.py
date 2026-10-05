"""Creating, moving and restyling annotations and form fields. Coordinates are unrotated page
coordinates (the ones PyMuPDF uses for annotations) unless a name says otherwise."""
from __future__ import annotations

import datetime as _dt
import math
import re

import pymupdf as fitz

from pdfdesk import fonts

MARKUP_TYPES = {fitz.PDF_ANNOT_HIGHLIGHT, fitz.PDF_ANNOT_UNDERLINE, fitz.PDF_ANNOT_STRIKE_OUT,
                fitz.PDF_ANNOT_SQUIGGLY}
SKIP_TYPES = {fitz.PDF_ANNOT_WIDGET, fitz.PDF_ANNOT_POPUP, fitz.PDF_ANNOT_LINK}
POINT_TYPES = {fitz.PDF_ANNOT_LINE, fitz.PDF_ANNOT_INK, fitz.PDF_ANNOT_POLYGON, fitz.PDF_ANNOT_POLY_LINE}
RECT_TYPES = {fitz.PDF_ANNOT_SQUARE, fitz.PDF_ANNOT_CIRCLE, fitz.PDF_ANNOT_FREE_TEXT, fitz.PDF_ANNOT_STAMP,
              fitz.PDF_ANNOT_REDACT, fitz.PDF_ANNOT_TEXT, fitz.PDF_ANNOT_FILE_ATTACHMENT,
              fitz.PDF_ANNOT_CARET, fitz.PDF_ANNOT_SOUND}
FIXED_SIZE_TYPES = {fitz.PDF_ANNOT_TEXT, fitz.PDF_ANNOT_FILE_ATTACHMENT, fitz.PDF_ANNOT_SOUND, fitz.PDF_ANNOT_CARET}

STAMPS = [
    ("Approved", fitz.STAMP_Approved), ("Not Approved", fitz.STAMP_NotApproved),
    ("Draft", fitz.STAMP_Draft), ("Final", fitz.STAMP_Final), ("Confidential", fitz.STAMP_Confidential),
    ("For Comment", fitz.STAMP_ForComment), ("For Public Release", fitz.STAMP_ForPublicRelease),
    ("Not For Public Release", fitz.STAMP_NotForPublicRelease), ("Experimental", fitz.STAMP_Experimental),
    ("Expired", fitz.STAMP_Expired), ("As Is", fitz.STAMP_AsIs), ("Departmental", fitz.STAMP_Departmental),
    ("Sold", fitz.STAMP_Sold), ("Top Secret", fitz.STAMP_TopSecret),
]
CUSTOM_STAMPS = ["Received {date}", "Reviewed {date}", "Paid {date}", "Signed {date}", "Void", "Copy"]

TYPE_NAMES = {
    fitz.PDF_ANNOT_TEXT: "Note", fitz.PDF_ANNOT_FREE_TEXT: "Text box", fitz.PDF_ANNOT_LINE: "Line",
    fitz.PDF_ANNOT_SQUARE: "Rectangle", fitz.PDF_ANNOT_CIRCLE: "Ellipse", fitz.PDF_ANNOT_POLYGON: "Polygon",
    fitz.PDF_ANNOT_POLY_LINE: "Polyline", fitz.PDF_ANNOT_HIGHLIGHT: "Highlight",
    fitz.PDF_ANNOT_UNDERLINE: "Underline", fitz.PDF_ANNOT_SQUIGGLY: "Squiggly",
    fitz.PDF_ANNOT_STRIKE_OUT: "Strikeout", fitz.PDF_ANNOT_STAMP: "Stamp", fitz.PDF_ANNOT_INK: "Drawing",
    fitz.PDF_ANNOT_REDACT: "Redaction", fitz.PDF_ANNOT_FILE_ATTACHMENT: "Attachment",
    fitz.PDF_ANNOT_CARET: "Caret",
}


def now_pdf_date() -> str:
    return fitz.get_pdf_now()


def type_name(annot) -> str:
    if annot.type[0] == fitz.PDF_ANNOT_FREE_TEXT and (annot.info or {}).get("subject") == "Stamp":
        return "Stamp"
    return TYPE_NAMES.get(annot.type[0], annot.type[1])


def _finish(annot, author: str | None = None, content: str | None = None, subject: str | None = None):
    info = {}
    if author is not None:
        info["title"] = author
    if content is not None:
        info["content"] = content
    if subject is not None:
        info["subject"] = subject
    if info:
        annot.set_info(**info)
    annot.update()
    return annot


# --------------------------------------------------------------------------- creating

def add_text_markup(page: fitz.Page, kind: str, rects: list[fitz.Rect], color, opacity: float = 1.0,
                    author: str = "", content: str = ""):
    quads = [r.quad for r in rects]
    if kind == "highlight":
        annot = page.add_highlight_annot(quads=quads)
    elif kind == "underline":
        annot = page.add_underline_annot(quads=quads)
    elif kind == "strikeout":
        annot = page.add_strikeout_annot(quads=quads)
    else:
        annot = page.add_squiggly_annot(quads=quads)
    annot.set_colors(stroke=color)
    annot.set_opacity(opacity if kind != "highlight" else min(opacity, 1.0))
    return _finish(annot, author, content)


def add_note(page: fitz.Page, point: fitz.Point, text: str, color, author: str = ""):
    annot = page.add_text_annot(point, text, icon="Note")
    annot.set_colors(stroke=color)
    return _finish(annot, author)


def freetext_size(text: str, fontsize: float) -> tuple[float, float]:
    lines = text.splitlines() or [""]
    width = max(fitz.get_text_length(ln, "helv", fontsize) if fonts.is_latin1(ln)
                else fonts.text_width(ln, fontsize) for ln in lines)
    return width + fontsize * 0.9 + 6, len(lines) * fontsize * 1.25 + fontsize * 0.5 + 4


def add_textbox(page: fitz.Page, rect: fitz.Rect, text: str, fontsize: float, color,
                fill=None, border_color=None, border_width: float = 0, author: str = "",
                align: int = 0):
    # Plain FreeText boxes draw their border in the text color (border_color only works for rich text).
    annot = page.add_freetext_annot(rect, text, fontsize=fontsize, fontname="Helv", text_color=color,
                                    fill_color=fill, border_width=border_width, rotate=page.rotation, align=align)
    return _finish(annot, author)


def freetext_style(doc: fitz.Document, annot) -> tuple[float, tuple]:
    """Read font size and text color from a FreeText annotation's /DA string."""
    size, color = 12.0, (0, 0, 0)
    try:
        kind, da = doc.xref_get_key(annot.xref, "DA")
        if kind == "string":
            m = re.search(r"([\d.]+)\s+Tf", da)
            if m:
                size = float(m.group(1)) or 12.0
                size = size if 1.0 <= size <= 500.0 else 12.0  # ignore absurd sizes from the PDF
            m = re.search(r"([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+rg", da)
            if m:
                color = tuple(float(x) for x in m.groups())
            else:
                m = re.search(r"([\d.]+)\s+g\b", da)
                if m:
                    g = float(m.group(1))
                    color = (g, g, g)
    except Exception:
        pass
    return size, color


def set_textbox_text(doc: fitz.Document, annot, text: str) -> None:
    size, color = freetext_style(doc, annot)
    annot.set_info(content=text)
    annot.update(fontsize=size, text_color=color)


def add_shape(page: fitz.Page, kind: str, p1: fitz.Point, p2: fitz.Point, color, width: float = 2,
              opacity: float = 1.0, fill=None, author: str = ""):
    if kind in ("rect", "ellipse"):
        rect = fitz.Rect(p1, p2).normalize()
        annot = page.add_rect_annot(rect) if kind == "rect" else page.add_circle_annot(rect)
        annot.set_colors(stroke=color, fill=fill)
    else:
        annot = page.add_line_annot(p1, p2)
        annot.set_colors(stroke=color, fill=color if kind == "arrow" else None)
        if kind == "arrow":
            annot.set_line_ends(fitz.PDF_ANNOT_LE_NONE, fitz.PDF_ANNOT_LE_CLOSED_ARROW)
    annot.set_border(width=width)
    annot.set_opacity(opacity)
    return _finish(annot, author)


def add_ink(page: fitz.Page, strokes: list[list[fitz.Point]], color, width: float = 2,
            opacity: float = 1.0, author: str = ""):
    annot = page.add_ink_annot([[(p.x, p.y) for p in stroke] for stroke in strokes])
    annot.set_colors(stroke=color)
    annot.set_border(width=width)
    annot.set_opacity(opacity)
    return _finish(annot, author)


def stamp_code(name: str) -> int | None:
    for label, code in STAMPS:
        if label == name:
            return code
    return None


def add_stamp(page: fitz.Page, rect: fitz.Rect, name: str, author: str = ""):
    code = stamp_code(name)
    if code is not None:
        annot = page.add_stamp_annot(rect, stamp=code)
        return _finish(annot, author)
    # A custom text stamp, drawn as a bordered text box.
    text = name.replace("{date}", _dt.date.today().strftime("%b %d, %Y")).upper()
    size = max(8.0, min(rect.height * 0.5, 28.0))
    w, _h = freetext_size(text, size)
    if rect.width < w:
        size = max(6.0, size * rect.width / w)
    # snug box around one line of text, centered where the user clicked or dragged
    h = size * 1.45 + 6
    cy = (rect.y0 + rect.y1) / 2
    rect = fitz.Rect(rect.x0, cy - h / 2, rect.x1, cy + h / 2)
    annot = page.add_freetext_annot(rect, text, fontsize=size, fontname="Helv", text_color=(0.75, 0.1, 0.12),
                                    border_width=2, align=1, rotate=page.rotation)
    return _finish(annot, author, subject="Stamp")


def add_image(page: fitz.Page, rect: fitz.Rect, data: bytes) -> None:
    """Place an image (or signature) on the page as page content."""
    fonts.ensure_wrapped(page)
    page.insert_image(rect, stream=data, keep_proportion=True, rotate=page.rotation)


def add_redaction(page: fitz.Page, rect: fitz.Rect, author: str = ""):
    annot = page.add_redact_annot(rect, fill=(0, 0, 0))
    return _finish(annot, author)


# --------------------------------------------------------------------------- finding

def _dist_to_segment(p: fitz.Point, a: fitz.Point, b: fitz.Point) -> float:
    dx, dy = b.x - a.x, b.y - a.y
    if dx == dy == 0:
        return math.hypot(p.x - a.x, p.y - a.y)
    t = max(0.0, min(1.0, ((p.x - a.x) * dx + (p.y - a.y) * dy) / (dx * dx + dy * dy)))
    return math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy))


def hit_test(annot, pt: fitz.Point, tol: float = 4.0) -> bool:
    t = annot.type[0]
    if t in SKIP_TYPES:
        return False
    rect = annot.rect
    if not (rect + (-tol, -tol, tol, tol)).contains(pt):
        return False
    if t == fitz.PDF_ANNOT_LINE:
        v = annot.vertices or []
        if len(v) >= 2:
            width = (annot.border or {}).get("width", 1) or 1
            return _dist_to_segment(pt, fitz.Point(v[0]), fitz.Point(v[1])) <= tol + width
    if t == fitz.PDF_ANNOT_INK:
        width = (annot.border or {}).get("width", 1) or 1
        for stroke in annot.vertices or []:
            for a, b in zip(stroke, stroke[1:]):
                if _dist_to_segment(pt, fitz.Point(a), fitz.Point(b)) <= tol + width:
                    return True
        return False
    if t in MARKUP_TYPES:
        v = annot.vertices or []
        for i in range(0, len(v) - 3, 4):
            if fitz.Quad(v[i:i + 4]).rect.contains(pt):
                return True
        return False
    return True


def annot_at(page: fitz.Page, pt: fitz.Point, tol: float = 4.0):
    """Topmost annotation under a point (skips form fields, links and popups)."""
    found = None
    for annot in page.annots():
        if hit_test(annot, pt, tol):
            found = annot  # later annotations are drawn on top
    return found


def can_move(annot) -> bool:
    t = annot.type[0]
    return t not in MARKUP_TYPES and t not in SKIP_TYPES


def can_resize(annot) -> bool:
    t = annot.type[0]
    return can_move(annot) and t not in FIXED_SIZE_TYPES


# --------------------------------------------------------------------------- moving & restyling

def _copy_style(src_props: dict, annot) -> None:
    colors = src_props.get("colors") or {}
    annot.set_colors(stroke=colors.get("stroke"), fill=colors.get("fill"))
    border = src_props.get("border") or {}
    if border.get("width") is not None:
        annot.set_border(width=border.get("width"), dashes=border.get("dashes") or None)
    if src_props.get("opacity") is not None and src_props["opacity"] >= 0:
        annot.set_opacity(src_props["opacity"])
    info = src_props.get("info") or {}
    annot.set_info(title=info.get("title", ""), content=info.get("content", ""), subject=info.get("subject", ""))
    if src_props.get("line_ends"):
        annot.set_line_ends(*src_props["line_ends"])
    annot.update()


def _props(annot) -> dict:
    return {"colors": annot.colors, "border": annot.border, "opacity": annot.opacity, "info": annot.info,
            "line_ends": annot.line_ends if annot.type[0] in (fitz.PDF_ANNOT_LINE, fitz.PDF_ANNOT_POLY_LINE) else None}


def transform_annot(page: fitz.Page, annot, new_rect: fitz.Rect):
    """Move/resize an annotation so its rectangle becomes new_rect. Returns the (possibly new) annotation."""
    t = annot.type[0]
    old = annot.rect
    if t in POINT_TYPES:
        sx = new_rect.width / old.width if old.width else 1
        sy = new_rect.height / old.height if old.height else 1

        def tp(p):
            return fitz.Point(new_rect.x0 + (p[0] - old.x0) * sx, new_rect.y0 + (p[1] - old.y0) * sy)
        props = _props(annot)
        verts = annot.vertices
        page.delete_annot(annot)
        if t == fitz.PDF_ANNOT_LINE:
            new = page.add_line_annot(tp(verts[0]), tp(verts[1]))
        elif t == fitz.PDF_ANNOT_INK:
            new = page.add_ink_annot([[tuple(tp(p)) for p in stroke] for stroke in verts])
        elif t == fitz.PDF_ANNOT_POLYGON:
            new = page.add_polygon_annot([tp(p) for p in verts])
        else:
            new = page.add_polyline_annot([tp(p) for p in verts])
        _copy_style(props, new)
        return new
    if t in FIXED_SIZE_TYPES:
        new_rect = fitz.Rect(new_rect.x0, new_rect.y0, new_rect.x0 + old.width, new_rect.y0 + old.height)
    if t == fitz.PDF_ANNOT_FREE_TEXT:
        size, color = freetext_style(page.parent, annot)
        annot.set_rect(new_rect)
        annot.update(fontsize=size, text_color=color)
        return annot
    annot.set_rect(new_rect)
    annot.update()
    return annot


def restyle(doc: fitz.Document, annot, color=None, width: float | None = None, opacity: float | None = None) -> None:
    t = annot.type[0]
    if t == fitz.PDF_ANNOT_FREE_TEXT:
        size, old_color = freetext_style(doc, annot)
        annot.update(fontsize=size, text_color=color if color is not None else old_color)
        if opacity is not None:
            annot.set_opacity(opacity)
            annot.update(fontsize=size, text_color=color if color is not None else old_color)
        return
    if color is not None and t not in (fitz.PDF_ANNOT_STAMP, fitz.PDF_ANNOT_REDACT):
        fill = (annot.colors or {}).get("fill")
        if t == fitz.PDF_ANNOT_LINE and fill:
            fill = color
        annot.set_colors(stroke=color, fill=fill)
    if width is not None and t in (fitz.PDF_ANNOT_SQUARE, fitz.PDF_ANNOT_CIRCLE, fitz.PDF_ANNOT_LINE,
                                   fitz.PDF_ANNOT_INK, fitz.PDF_ANNOT_POLYGON, fitz.PDF_ANNOT_POLY_LINE):
        annot.set_border(width=width)
    if opacity is not None:
        annot.set_opacity(opacity)
    annot.update()


# --------------------------------------------------------------------------- form fields

WIDGET_NAMES = {
    fitz.PDF_WIDGET_TYPE_TEXT: "Text field", fitz.PDF_WIDGET_TYPE_CHECKBOX: "Check box",
    fitz.PDF_WIDGET_TYPE_RADIOBUTTON: "Radio button", fitz.PDF_WIDGET_TYPE_COMBOBOX: "Drop-down",
    fitz.PDF_WIDGET_TYPE_LISTBOX: "List", fitz.PDF_WIDGET_TYPE_BUTTON: "Button",
    fitz.PDF_WIDGET_TYPE_SIGNATURE: "Signature field",
}


def widget_at(page: fitz.Page, pt: fitz.Point):
    for w in page.widgets():
        if w.rect.contains(pt):
            return w
    return None


def toggle_checkbox(widget) -> None:
    on = widget.on_state() or "Yes"
    widget.field_value = "Off" if widget.field_value not in ("Off", "", None, False) else on
    widget.update()


def select_radio(page: fitz.Page, widget) -> None:
    name, xref = widget.field_name, widget.xref
    on = widget.on_state() or "Yes"
    # turn the others in the same group off (they can live on any page)
    for p in page.parent:
        for other in p.widgets(types=[fitz.PDF_WIDGET_TYPE_RADIOBUTTON]):
            if other.field_name == name and other.xref != xref and other.field_value not in ("Off", False):
                other.field_value = "Off"
                other.update()
    for w in page.widgets(types=[fitz.PDF_WIDGET_TYPE_RADIOBUTTON]):
        if w.xref == xref:
            w.field_value = on
            w.update()
            break


def set_choice(widget, value: str) -> None:
    widget.field_value = value
    widget.update()


def choice_options(widget) -> list[tuple[str, str]]:
    """[(export value, label)]"""
    out = []
    for item in widget.choice_values or []:
        if isinstance(item, (list, tuple)):
            out.append((str(item[0]), str(item[1] if len(item) > 1 else item[0])))
        else:
            out.append((str(item), str(item)))
    return out


def set_text_field(doc: fitz.Document, page: fitz.Page, widget, value: str) -> None:
    widget.field_value = value
    widget.update()
    if fonts.is_latin1(value):
        return
    # MuPDF can only draw form text with Latin-1 fonts, so write our own appearance with a
    # Unicode font so letters like ș/ț show up (the value itself is stored either way).
    try:
        _unicode_field_appearance(doc, page, widget, value)
    except Exception:
        pass


def _unicode_field_appearance(doc, page, widget, value: str) -> None:
    kind, ap = doc.xref_get_key(widget.xref, "AP/N")
    if kind != "xref":
        return
    ap_xref = int(ap.split()[0])
    choice = fonts.unicode_font()
    font = choice.font
    font_xref = page.insert_font(fontname="PDUni", fontbuffer=choice.buffer)
    rect = widget.rect
    w, h = rect.width, rect.height
    size = widget.text_fontsize or 0
    multiline = bool(widget.field_flags & fitz.PDF_TX_FIELD_IS_MULTILINE)
    if not size:
        size = min(12.0, h * 0.65) if not multiline else 10.0
    color = widget.text_color or (0, 0, 0)
    lines = []
    if multiline:
        for para in value.splitlines() or [""]:
            cur = ""
            for word in para.split(" "):
                test = (cur + " " + word).strip()
                if font.text_length(test, fontsize=size) > w - 4 and cur:
                    lines.append(cur)
                    cur = word
                else:
                    cur = test
            lines.append(cur)
    else:
        lines = [value.replace("\n", " ")]

    def hexstr(text):
        return "".join(f"{font.has_glyph(ord(c)) or 0:04x}" for c in text)

    def rgb(c):
        c = list(c) if isinstance(c, (list, tuple)) else [c, c, c]
        c = (c * 3)[:3] if len(c) == 1 else c[:3]
        return " ".join(f"{v:.3f}" for v in c)

    ops = []
    if widget.fill_color:
        ops.append(f"{rgb(widget.fill_color)} rg 0 0 {w:.2f} {h:.2f} re f")
    if widget.border_color and (widget.border_width or 1) > 0:
        bw = widget.border_width or 1
        ops.append(f"{rgb(widget.border_color)} RG {bw:.2f} w {bw / 2:.2f} {bw / 2:.2f} "
                   f"{w - bw:.2f} {h - bw:.2f} re S")
    ops += ["/Tx BMC", "q", "BT", f"/PDUni {size:.2f} Tf", f"{rgb(color)} rg"]
    if multiline:
        y = h - 2 - size
        for i, line in enumerate(lines):
            ops.append(f"1 0 0 1 2 {y - i * size * 1.15:.2f} Tm <{hexstr(line)}> Tj")
    else:
        y = (h - size) / 2 + size * 0.22
        ops.append(f"1 0 0 1 2 {y:.2f} Tm <{hexstr(lines[0])}> Tj")
    ops += ["ET", "Q", "EMC"]
    doc.update_stream(ap_xref, "\n".join(ops).encode())
    doc.xref_set_key(ap_xref, "Resources", f"<</Font<</PDUni {font_xref} 0 R>>>>")
    doc.xref_set_key(ap_xref, "BBox", f"[0 0 {w:.2f} {h:.2f}]")
