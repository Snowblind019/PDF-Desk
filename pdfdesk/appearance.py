"""Custom annotation appearances.

PDF Desk draws some annotations itself (formatted text boxes, images, signatures, measurements,
Fill & Sign marks): the drawing is made on a small temporary page and then copied into the
annotation's appearance stream. Other PDF viewers simply show that appearance.

Important: the PDF engine redraws an annotation's appearance from scratch after any of PyMuPDF's
annot.set_*() or update() calls, which would wipe out a custom drawing. Annotations with a custom
appearance are therefore only ever changed through the functions in this module, which write the
PDF keys directly.
"""
from __future__ import annotations

from contextlib import contextmanager

import pymupdf as fitz

m = fitz.mupdf

# Marks an annotation whose appearance PDF Desk made (value: the kind of annotation).
KIND_KEY = "PDFDeskKind"


def pdf_string(text: str) -> str:
    """A PDF text string literal (UTF-16 hex when needed) safe to pass to xref_set_key."""
    try:
        text.encode("ascii")
        if all(32 <= ord(c) < 127 for c in text):
            return "<" + text.encode("ascii").hex() + ">"
    except UnicodeEncodeError:
        pass
    return "<feff" + text.encode("utf-16-be", "surrogatepass").hex() + ">"


def pdf_name(value: str) -> str:
    safe = "".join(c for c in value if c.isalnum() or c in "-_.")
    return "/" + (safe or "X")


def get_key(doc: fitz.Document, xref: int, key: str) -> tuple[str, str]:
    try:
        return doc.xref_get_key(xref, key)
    except Exception:
        return "null", "null"


def kind_of(doc: fitz.Document, xref: int) -> str:
    t, v = get_key(doc, xref, KIND_KEY)
    return v.lstrip("/") if t == "name" else ""


def _new_ap(doc: fitz.Document, annot_xref: int) -> int:
    """A fresh appearance stream for this annotation only (an optimizer may have made several
    annotations share one stream, and changing a shared one would change all of them)."""
    xref = doc.get_new_xref()
    doc.update_object(xref, "<</Type/XObject/Subtype/Form/BBox[0 0 1 1]>>")
    doc.update_stream(xref, b" ", new=True)
    doc.xref_set_key(annot_xref, "AP", f"<</N {xref} 0 R>>")
    return xref


def _own_ap(doc: fitz.Document, annot_xref: int) -> int:
    """Copy the current appearance (and its resources) into objects that belong to this annotation."""
    t, v = get_key(doc, annot_xref, "AP/N")
    if t != "xref":
        return _new_ap(doc, annot_xref)
    old = int(v.split()[0])
    content = doc.xref_stream(old) or b" "
    keys = {k: get_key(doc, old, k) for k in ("BBox", "Matrix", "Resources")}
    new = _new_ap(doc, annot_xref)
    doc.update_stream(new, content)
    doc.xref_set_key(new, "Subtype", "/Form")
    for k, (kt, kv) in keys.items():
        if kt == "null":
            continue
        if k == "Resources":
            body = doc.xref_object(int(kv.split()[0])) if kt == "xref" else kv
            res = doc.get_new_xref()
            doc.update_object(res, body)
            doc.xref_set_key(new, "Resources", f"{res} 0 R")
        else:
            doc.xref_set_key(new, k, kv)
    return new


def _graft_resources(doc: fitz.Document, tmp: fitz.Document) -> int | None:
    """Copy the first page's resources (fonts, images...) of `tmp` into `doc`; return the xref."""
    dst = fitz._as_pdf_document(doc)
    src = fitz._as_pdf_document(tmp)
    page_obj = m.pdf_lookup_page_obj(src, 0)
    res = m.pdf_dict_get(page_obj, m.pdf_new_name("Resources"))
    if not m.pdf_is_dict(res):
        return None
    gmap = m.pdf_new_graft_map(dst)
    new = m.pdf_graft_mapped_object(gmap, res)
    if not m.pdf_is_indirect(new):
        new = m.pdf_add_object(dst, new)
    return m.pdf_to_num(new)


ROTATION_MATRIX = {0: "[1 0 0 1 0 0]", 90: "[0 1 -1 0 0 0]", 180: "[-1 0 0 -1 0 0]", 270: "[0 -1 1 0 0 0]"}


def _floats(text: str, count: int) -> list[float] | None:
    try:
        vals = [float(v) for v in text.strip().strip("[]").split()]
    except ValueError:
        return None
    return vals if len(vals) == count else None


def flatten_annot(page: fitz.Page, annot_xref: int) -> bool:
    """Make one annotation a permanent part of the page (like "Flatten", but for a single item).
    Returns False if it has no appearance to keep."""
    doc = page.parent
    t, v = get_key(doc, annot_xref, "AP/N")
    if t != "xref":
        return False
    ap = int(v.split()[0])
    bbox = _floats(get_key(doc, ap, "BBox")[1], 4)
    if not bbox or bbox[2] - bbox[0] <= 0 or bbox[3] - bbox[1] <= 0:
        return False
    mat = _floats(get_key(doc, ap, "Matrix")[1], 6) or [1, 0, 0, 1, 0, 0]
    # Build a one-page document showing the appearance, then stamp that page onto this one.
    tmp = fitz.open()
    tp = tmp.new_page(width=bbox[2] - bbox[0], height=bbox[3] - bbox[1])
    tmp.xref_set_key(tp.xref, "MediaBox", f"[{bbox[0]} {bbox[1]} {bbox[2]} {bbox[3]}]")
    cx = tmp.get_new_xref()
    tmp.update_object(cx, "<<>>")
    tmp.update_stream(cx, doc.xref_stream(ap) or b" ", new=True)
    tmp.xref_set_key(tp.xref, "Contents", f"{cx} 0 R")
    src = fitz._as_pdf_document(doc)
    dst = fitz._as_pdf_document(tmp)
    res = m.pdf_dict_get(m.pdf_load_object(src, ap), m.pdf_new_name("Resources"))
    if m.pdf_is_dict(res):
        new = m.pdf_graft_mapped_object(m.pdf_new_graft_map(dst), res)
        if not m.pdf_is_indirect(new):
            new = m.pdf_add_object(dst, new)
        tmp.xref_set_key(tp.xref, "Resources", f"{m.pdf_to_num(new)} 0 R")
    rotate = {(1, 0, 0, 1): 0, (0, 1, -1, 0): 90, (-1, 0, 0, -1): 180, (0, -1, 1, 0): 270}.get(
        tuple(round(x) for x in mat[:4]), 0)
    annot = page.load_annot(annot_xref)
    rect = annot.rect
    from pdfdesk import fonts as fonts_mod
    fonts_mod.ensure_wrapped(page)
    tmp = fitz.open("pdf", tmp.tobytes())
    with unrotated(page):
        page.show_pdf_page(rect, tmp, 0, keep_proportion=False, rotate=rotate)
    page.delete_annot(page.load_annot(annot_xref))
    return True


def apply(doc: fitz.Document, annot_xref: int, tmp: fitz.Document, rotation: int = 0) -> None:
    """Use page 1 of `tmp` (drawn in the upright, visible orientation) as the annotation's look.
    The annotation's /Rect must already be set; the drawing is fitted into it."""
    tpage = tmp[0]
    w, h = tpage.rect.width, tpage.rect.height
    content = tpage.read_contents()
    res_xref = _graft_resources(doc, tmp)
    ap = _new_ap(doc, annot_xref)
    doc.update_stream(ap, content or b" ")
    doc.xref_set_key(ap, "Type", "/XObject")
    doc.xref_set_key(ap, "Subtype", "/Form")
    doc.xref_set_key(ap, "BBox", f"[0 0 {w:.3f} {h:.3f}]")
    doc.xref_set_key(ap, "Matrix", ROTATION_MATRIX.get(rotation % 360, ROTATION_MATRIX[0]))
    doc.xref_set_key(ap, "Resources", f"{res_xref} 0 R" if res_xref else "<<>>")
    # other states (rollover/down) would show the old look in some viewers
    for state in ("R", "D"):
        if get_key(doc, annot_xref, f"AP/{state}")[0] != "null":
            doc.xref_set_key(annot_xref, f"AP/{state}", "null")


def _page_ctm(page: fitz.Page) -> fitz.Matrix:
    """The engine's own matrix from PDF space to the visible page (handles rotation together with
    media and crop boxes that don't start at 0,0, which page.transformation_matrix doesn't)."""
    ctm = m.FzMatrix()
    m.pdf_page_transform(page._pdf_page(), m.FzRect(m.FzRect.Fixed_UNIT), ctm)
    return fitz.Matrix(ctm.a, ctm.b, ctm.c, ctm.d, ctm.e, ctm.f)


def page_rect_to_pdf(page: fitz.Page, rect: fitz.Rect) -> fitz.Rect:
    """PyMuPDF page coordinates (top-down, unrotated) to raw PDF coordinates (bottom-up, media box based)."""
    try:
        return (fitz.Rect(rect) * page.rotation_matrix * ~_page_ctm(page)).normalize()
    except Exception:
        return (fitz.Rect(rect) * ~page.transformation_matrix).normalize()


@contextmanager
def unrotated(page: fitz.Page):
    """Treat the page as unturned for a moment. show_pdf_page and insert_htmlbox place things wrongly
    on turned pages whose boxes don't start at 0,0; with no rotation they get it right."""
    rot = page.rotation
    if rot:
        page.set_rotation(0)
    try:
        yield
    finally:
        if rot:
            page.set_rotation(rot)


def move_annot(page: fitz.Page, annot_xref: int, rect: fitz.Rect) -> None:
    """Move an annotation without making the engine redraw it (its look is scaled into the new box)."""
    doc = page.parent
    r = page_rect_to_pdf(page, rect)
    doc.xref_set_key(annot_xref, "Rect", f"[{r.x0:.3f} {r.y0:.3f} {r.x1:.3f} {r.y1:.3f}]")


ASPECT_KINDS = {"Image", "Signature", "Initials", "Check", "Cross", "Dot", "DigitalSignature"}


def keeps_aspect(doc: fitz.Document, annot_xref: int) -> bool:
    return kind_of(doc, annot_xref) in ASPECT_KINDS


_OP_PREFIX = b"q /PDDeskOp gs\n"


def set_opacity(page: fitz.Page, annot_xref: int, opacity: float) -> None:
    """Change how see-through a custom-drawn annotation is (inside its appearance, which every
    viewer uses, instead of /CA which some viewers ignore and others apply twice)."""
    doc = page.parent
    op = max(0.05, min(1.0, float(opacity)))
    ap = _own_ap(doc, annot_xref)
    content = doc.xref_stream(ap) or b""
    if not content.startswith(_OP_PREFIX):
        doc.update_stream(ap, _OP_PREFIX + content + b"\nQ\n")
    t, res = get_key(doc, ap, "Resources")
    if t != "xref":
        res_xref = doc.get_new_xref()
        doc.update_object(res_xref, "<<>>")
        doc.xref_set_key(ap, "Resources", f"{res_xref} 0 R")
        target = res_xref
    else:
        target = int(res.split()[0])
    if get_key(doc, target, "ExtGState")[0] == "null":
        doc.xref_set_key(target, "ExtGState", "<<>>")
    doc.xref_set_key(target, "ExtGState/PDDeskOp", f"<</Type/ExtGState/ca {op:.3f}/CA {op:.3f}>>")
    doc.xref_set_key(annot_xref, "PDFDeskOpacity", f"{op:.3f}")


def opacity_of(doc: fitz.Document, annot_xref: int) -> float:
    t, v = get_key(doc, annot_xref, "PDFDeskOpacity")
    if t in ("null", "string", "name", "dict", "array", "xref"):
        return 1.0
    try:
        return max(0.05, min(1.0, float(v)))
    except ValueError:
        return 1.0


def set_contents(doc: fitz.Document, annot_xref: int, text: str) -> None:
    doc.xref_set_key(annot_xref, "Contents", pdf_string(text))


def set_info(doc: fitz.Document, annot_xref: int, author: str | None = None, subject: str | None = None,
             contents: str | None = None) -> None:
    if author is not None:
        doc.xref_set_key(annot_xref, "T", pdf_string(author))
    if subject is not None:
        doc.xref_set_key(annot_xref, "Subj", pdf_string(subject))
    if contents is not None:
        set_contents(doc, annot_xref, contents)
    doc.xref_set_key(annot_xref, "M", pdf_string(fitz.get_pdf_now()))


def new_annot(page: fitz.Page, subtype: str, rect: fitz.Rect, kind: str, author: str = "",
              contents: str = "") -> int:
    """Create an empty annotation of `subtype` ("FreeText", "Stamp", "Square"...) for a custom look."""
    doc = page.parent
    if subtype == "FreeText":
        annot = page.add_freetext_annot(rect, contents or " ", fontsize=10, fontname="Helv")
    elif subtype == "Stamp":
        annot = page.add_stamp_annot(rect, stamp=0)
    elif subtype == "Square":
        annot = page.add_rect_annot(rect)
    else:
        raise ValueError(subtype)
    xref = annot.xref
    if author:
        annot.set_info(title=author)
    annot.update()
    if subtype == "Stamp":
        doc.xref_set_key(xref, "Name", pdf_name(kind))
        move_annot(page, xref, rect)  # the engine resizes stamps to its own proportions
    doc.xref_set_key(xref, KIND_KEY, pdf_name(kind))
    set_info(doc, xref, contents=contents)
    return xref
