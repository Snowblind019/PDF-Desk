"""Document-wide tools: links, attachments, layers, page labels, backgrounds, page sizes, N-up and
booklets, grayscale, blank pages, image extraction and comment summaries.

Functions that take `progress` run in a background job (see jobs.run_job) on their own copy of the
document, so they never touch the open document from another thread.
"""
from __future__ import annotations

import csv
import datetime as _dt
import io
import os
import re
from pathlib import Path

import pymupdf as fitz

from pdfdesk import annots, appearance, fonts, forms, pdfops, safety

Progress = object


def _tick(progress, done, total, text=""):
    pdfops._tick(progress, done, total, text)  # raises Cancelled when the person clicks Cancel


# --------------------------------------------------------------------------- links

def add_link(page: fitz.Page, vis_rect: fitz.Rect, target: dict) -> None:
    """target: {"page": n, "y": float} for a page in this PDF, or {"uri": "https://..."} for a web page."""
    unrot = (fitz.Rect(vis_rect) * page.derotation_matrix).normalize()
    if "uri" in target:
        url, info = safety.check_web_link(target["uri"])
        if url is None:
            raise ValueError(info)
        # PyMuPDF writes the address into the PDF without escaping it, so a ")" in an address could
        # add an action of its own. Insert a fixed address, then store the real one as a safe string.
        doc = page.parent
        before = set(pdfops._annot_refs(doc, page))
        page.insert_link({"kind": fitz.LINK_URI, "from": unrot, "uri": "https://example.invalid/"})
        new = [n for n, _g in set(pdfops._annot_refs(doc, page)) - before]
        if len(new) != 1:
            raise ValueError("The link couldn't be added.")
        doc.xref_set_key(int(new[0]), "A", f"<</S/URI/URI{_ascii_pdf_string(url)}>>")
    else:
        n = int(target["page"])
        if not 0 <= n < page.parent.page_count:
            raise ValueError("That page doesn't exist.")
        page.insert_link({"kind": fitz.LINK_GOTO, "from": unrot, "page": n,
                          "to": fitz.Point(0, max(0.0, float(target.get("y", 0))))})


def _ascii_pdf_string(url: str) -> str:
    """A link address as a PDF hex string: plain ASCII (as links must be), with nothing that can break out."""
    from urllib.parse import quote
    url = quote(url, safe=":/?#[]@!$&'()*+,;=%~-._")
    return "<" + url.encode("ascii", "replace").hex() + ">"


def find_link(page: fitz.Page, xref: int) -> dict | None:
    for link in page.get_links():
        if link.get("xref") == xref:
            return link
    return None


def delete_link(page: fitz.Page, xref: int) -> None:
    link = find_link(page, xref)
    if link is not None:
        page.delete_link(link)


def replace_link(page: fitz.Page, xref: int, target: dict) -> None:
    link = find_link(page, xref)
    if link is None:
        return
    vis = fitz.Rect(link["from"])
    page.delete_link(link)
    add_link(page, vis, target)


def link_target(link: dict) -> dict:
    if link.get("kind") == fitz.LINK_URI:
        return {"uri": link.get("uri", "")}
    if link.get("kind") in (fitz.LINK_GOTO, fitz.LINK_NAMED) and (link.get("page") or 0) >= 0:
        return {"page": int(link.get("page") or 0)}
    return {}


# --------------------------------------------------------------------------- attachments

MAX_ATTACHMENT = 200 * 1024 * 1024


def list_attachments(doc: fitz.Document) -> list[dict]:
    """Files attached to the whole document, and files attached to a spot on a page."""
    out = []
    for name in doc.embfile_names():
        try:
            info = doc.embfile_info(name)
        except Exception:
            continue
        out.append({"where": "doc", "key": name, "name": info.get("ufilename") or info.get("filename") or name,
                    "size": info.get("size") or info.get("length") or 0, "desc": info.get("description") or "",
                    "page": None})
    for page in doc:
        for a in page.annots(types=[fitz.PDF_ANNOT_FILE_ATTACHMENT]):
            try:
                info = a.file_info
            except Exception:
                continue
            out.append({"where": "annot", "key": a.xref, "name": info.get("filename") or "attachment",
                        "size": info.get("size") or info.get("length") or 0, "desc": info.get("description") or "",
                        "page": page.number})
    return out


def attachment_bytes(doc: fitz.Document, item: dict) -> bytes:
    if item["where"] == "doc":
        return doc.embfile_get(item["key"])
    page = doc[item["page"]]
    return page.load_annot(item["key"]).get_file()


def add_attachment(doc: fitz.Document, path: str, description: str = "") -> str:
    if os.path.getsize(path) > MAX_ATTACHMENT:
        raise ValueError("That file is too large to attach (over 200 MB).")
    data = Path(path).read_bytes()
    base = safety.safe_filename_part(Path(path).name) or "attachment"
    name = base
    existing = set(doc.embfile_names())
    k = 2
    while name in existing:
        stem, ext = os.path.splitext(base)
        name = f"{stem} ({k}){ext}"
        k += 1
    doc.embfile_add(name, data, filename=name, ufilename=name, desc=description[:200])
    return name


def delete_attachment(doc: fitz.Document, item: dict) -> None:
    if item["where"] == "doc":
        doc.embfile_del(item["key"])
    else:
        page = doc[item["page"]]
        page.delete_annot(page.load_annot(item["key"]))


# --------------------------------------------------------------------------- page labels

LABEL_STYLES = [("D", "1, 2, 3"), ("r", "i, ii, iii"), ("R", "I, II, III"), ("a", "a, b, c"), ("A", "A, B, C"),
                ("", "No number (prefix only)")]


def get_labels(doc: fitz.Document) -> list[dict]:
    try:
        out = []
        for r in doc.get_page_labels()[:1000]:
            r = dict(r)
            r["firstpagenum"] = max(1, min(MAX_LABEL_NUMBER, int(r.get("firstpagenum", 1) or 1)))
            out.append(r)
        return out
    except Exception:
        return []


def set_labels(doc: fitz.Document, rules: list[dict]) -> None:
    clean = []
    seen = set()
    for r in sorted(rules, key=lambda r: int(r.get("startpage", 0))):
        start = int(r.get("startpage", 0))
        if start in seen or not 0 <= start < doc.page_count:
            continue
        seen.add(start)
        style = r.get("style", "D")
        clean.append({"startpage": start, "prefix": str(r.get("prefix", ""))[:40],
                      "style": style if style in ("D", "r", "R", "a", "A", "") else "D",
                      "firstpagenum": max(1, min(MAX_LABEL_NUMBER, int(r.get("firstpagenum", 1))))})
    doc.set_page_labels(clean)


MAX_LABEL_NUMBER = 100_000


def _roman(n: int) -> str:
    out = []
    for value, sym in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"),
                       (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        count, n = divmod(n, value)
        out.append(sym * count)
    return "".join(out)


def _label_number(style: str, n: int) -> str:
    n = max(1, min(MAX_LABEL_NUMBER, n))
    if style == "D":
        return str(n)
    if style in ("r", "R"):
        r = _roman(n)
        return r.upper() if style == "R" else r
    if style in ("a", "A"):
        letter = chr(ord("a") + (n - 1) % 26) * min(20, (n - 1) // 26 + 1)
        return letter.upper() if style == "A" else letter
    return ""


def all_labels(doc: fitz.Document) -> list[str]:
    """Every page's label, worked out here (with sane limits) instead of by the PDF engine, which can
    build enormous strings from a crafted label start number."""
    try:
        rules = [r for r in doc.get_page_labels() if 0 <= int(r.get("startpage", -1)) < doc.page_count]
    except Exception:
        return []
    if not rules:
        return []
    rules = sorted(rules[: doc.page_count], key=lambda r: int(r["startpage"]))
    labels = []
    k = 0
    for pno in range(doc.page_count):
        while k + 1 < len(rules) and int(rules[k + 1]["startpage"]) <= pno:
            k += 1
        r = rules[k]
        if pno < int(r["startpage"]):
            labels.append(str(pno + 1))
            continue
        try:
            first = int(r.get("firstpagenum", 1) or 1)
        except (TypeError, ValueError):
            first = 1
        number = _label_number(str(r.get("style", "") or ""), first + pno - int(r["startpage"]))
        labels.append((str(r.get("prefix", "") or "")[:40] + number)[:60])
    return labels


def page_label(doc: fitz.Document, pno: int) -> str:
    labels = all_labels(doc)
    return labels[pno] if 0 <= pno < len(labels) else ""


# --------------------------------------------------------------------------- backgrounds

def add_background(doc: fitz.Document, pages, color=None, image: bytes | None = None, opacity: float = 1.0,
                   fit: str = "fill") -> None:
    """Put a color or picture behind the page's content."""
    for pno in pages:
        page = doc[pno]
        fonts.ensure_wrapped(page)
        if color is not None:
            page.draw_rect(_unrot_page_rect(page), color=None, fill=color, fill_opacity=opacity, overlay=False)
        if image is not None:
            vis = page.rect
            box = fitz.Rect(0, 0, vis.width, vis.height)
            page.insert_image((box * page.derotation_matrix).normalize(), stream=image,
                              keep_proportion=(fit != "stretch"), overlay=False, rotate=page.rotation)


def _unrot_page_rect(page: fitz.Page) -> fitz.Rect:
    return (fitz.Rect(page.rect) * page.derotation_matrix).normalize()


# --------------------------------------------------------------------------- page size

def resize_pages(doc: fitz.Document, pages, width: float, height: float, mode: str = "scale") -> None:
    """Change the paper size of pages (width x height as seen on screen).
    mode "scale": shrink or enlarge the content (and comments, links, fields) to fit.
    mode "margins": keep the content as it is and center it on the new paper."""
    for pno in pages:
        page = doc[pno]
        mb, cb = page.mediabox, page.cropbox  # media box in PDF numbers, crop box measured from the top
        visible = fitz.Rect(cb.x0, mb.y1 - cb.y1, cb.x1, mb.y1 - cb.y0)
        if visible != mb:  # a cropped page keeps what is visible
            doc.xref_set_key(page.xref, "MediaBox", f"[{visible.x0:.3f} {visible.y0:.3f} {visible.x1:.3f} {visible.y1:.3f}]")
            doc.xref_set_key(page.xref, "CropBox", "null")
            page = doc[pno]
        rot = page.rotation % 360
        nw, nh = (height, width) if rot in (90, 270) else (width, height)   # unrotated size of the new page
        old = page.mediabox
        ow, oh = old.width, old.height
        if mode == "margins":
            dx, dy = (nw - ow) / 2, (nh - oh) / 2
            box = fitz.Rect(old.x0 - dx, old.y0 - dy, old.x0 - dx + nw, old.y0 - dy + nh)
            doc.xref_set_key(page.xref, "MediaBox", f"[{box.x0:.3f} {box.y0:.3f} {box.x1:.3f} {box.y1:.3f}]")
            for key in ("CropBox", "TrimBox", "BleedBox", "ArtBox"):
                doc.xref_set_key(page.xref, key, "null")
            continue
        s = min(nw / ow, nh / oh)
        off_x, off_y = (nw - ow * s) / 2, (nh - oh * s) / 2
        tx, ty = off_x - old.x0 * s, off_y - old.y0 * s
        # what used to be at point p (PyMuPDF coordinates) ends up at p * move
        move = fitz.Matrix(s, 0, 0, s, off_x, off_y)
        derot = page.derotation_matrix
        links = [(link["xref"], (fitz.Rect(link["from"]) * derot).normalize()) for link in page.get_links()
                 if link.get("xref")]
        widgets = [(w.xref, fitz.Rect(w.rect)) for w in page.widgets()]
        items = [(a.xref, fitz.Rect(a.rect)) for a in page.annots() if a.type[0] not in annots.SKIP_TYPES]
        _prepend_content(doc, page, f"q {s:.6f} 0 0 {s:.6f} {tx:.4f} {ty:.4f} cm\n".encode(), b"\nQ\n")
        doc.xref_set_key(page.xref, "MediaBox", f"[0 0 {nw:.3f} {nh:.3f}]")
        for key in ("CropBox", "TrimBox", "BleedBox", "ArtBox"):
            doc.xref_set_key(page.xref, key, "null")
        page = doc[pno]
        for xref, rect in items:
            annot = page.load_annot(xref)
            if annot is not None:
                annots.transform_annot(page, annot, rect * move)
                page = doc[pno]
        for xref, rect in widgets:
            forms.move_field(page, xref, rect * move)
        for xref, rect in links:  # move each link's box; its action is left exactly as it was
            try:
                r = appearance.page_rect_to_pdf(page, (rect * move).normalize())
                doc.xref_set_key(xref, "Rect", f"[{r.x0:.3f} {r.y0:.3f} {r.x1:.3f} {r.y1:.3f}]")
            except Exception:
                continue


def _prepend_content(doc: fitz.Document, page: fitz.Page, before: bytes, after: bytes) -> None:
    fonts.ensure_wrapped(page)
    first = doc.get_new_xref()
    doc.update_object(first, "<<>>")
    doc.update_stream(first, before, new=True)
    last = doc.get_new_xref()
    doc.update_object(last, "<<>>")
    doc.update_stream(last, after, new=True)
    xrefs = page.get_contents()
    refs = " ".join(f"{x} 0 R" for x in [first] + list(xrefs) + [last])
    doc.xref_set_key(page.xref, "Contents", f"[{refs}]")


PAPER_SIZES = [("Letter", 612, 792), ("Legal", 612, 1008), ("Tabloid", 792, 1224), ("A3", 842, 1191),
               ("A4", 595, 842), ("A5", 420, 595), ("B5", 499, 709), ("Executive", 522, 756)]


# --------------------------------------------------------------------------- N-up and booklets

def _baked_copy(data: bytes):
    src = fitz.open("pdf", data)
    try:
        src.bake()  # comments and form entries become part of the pages so they print too
    except Exception:
        pass
    return src


def nup_bytes(data: bytes, per_sheet: int = 2, paper: tuple[float, float] = (842, 595), margin: float = 18,
              gap: float = 10, borders: bool = False, progress=None) -> bytes:
    """Several pages on each sheet, in reading order."""
    src = _baked_copy(data)
    layouts = {2: (2, 1), 4: (2, 2), 6: (3, 2), 8: (4, 2), 9: (3, 3), 16: (4, 4)}
    cols, rows = layouts.get(per_sheet, (2, 1))
    w, h = paper
    if per_sheet in (6, 8) and w < h:
        cols, rows = rows, cols
    out = fitz.open()
    cell_w = (w - 2 * margin - gap * (cols - 1)) / cols
    cell_h = (h - 2 * margin - gap * (rows - 1)) / rows
    n = src.page_count
    for k in range(0, n, cols * rows):
        sheet = out.new_page(width=w, height=h)
        for j in range(cols * rows):
            pno = k + j
            if pno >= n:
                break
            r, c = divmod(j, cols)
            cell = fitz.Rect(margin + c * (cell_w + gap), margin + r * (cell_h + gap),
                             margin + c * (cell_w + gap) + cell_w, margin + r * (cell_h + gap) + cell_h)
            sheet.show_pdf_page(cell, src, pno)
            if borders:
                sheet.draw_rect(cell, color=(0.6, 0.6, 0.6), width=0.5)
        _tick(progress, min(n, k + cols * rows), n, "Arranging pages")
    return out.tobytes(garbage=3, deflate=True)


def booklet_order(n: int) -> list[tuple[int | None, int | None]]:
    """Page pairs for a folded booklet (saddle stitch). None means a blank half."""
    total = (n + 3) // 4 * 4
    pages = list(range(n)) + [None] * (total - n)
    pairs = []
    for i in range(total // 4):
        pairs.append((pages[total - 1 - 2 * i], pages[2 * i]))          # front of sheet
        pairs.append((pages[2 * i + 1], pages[total - 2 - 2 * i]))      # back of sheet
    return pairs


def booklet_bytes(data: bytes, paper: tuple[float, float] = (842, 595), margin: float = 12,
                  progress=None) -> bytes:
    src = _baked_copy(data)
    w, h = paper
    if h > w:
        w, h = h, w  # booklets print two pages side by side on landscape sheets
    out = fitz.open()
    pairs = booklet_order(src.page_count)
    half = (w - 2 * margin) / 2
    for k, (left, right) in enumerate(pairs):
        sheet = out.new_page(width=w, height=h)
        for pno, x0 in ((left, margin), (right, margin + half)):
            if pno is not None:
                sheet.show_pdf_page(fitz.Rect(x0, margin, x0 + half, h - margin), src, pno)
        _tick(progress, k + 1, len(pairs), "Making the booklet")
    return out.tobytes(garbage=3, deflate=True)


# --------------------------------------------------------------------------- grayscale, blank pages, images

def grayscale_bytes(data: bytes, password: str | None = None, progress=None) -> bytes:
    doc = pdfops.open_bytes(data, password)
    _tick(progress, 0, 1, "Converting colors to gray")
    doc.recolor(components=1)
    _tick(progress, 1, 1, "Converting colors to gray")
    return doc.tobytes(garbage=3, deflate=True, encryption=fitz.PDF_ENCRYPT_KEEP)


def find_blank_pages(data: bytes, password: str | None = None, threshold: float = 0.0015, progress=None) -> list[int]:
    """Pages that are (almost) completely white: no text, and almost no dark pixels."""
    doc = pdfops.open_bytes(data, password)
    blank = []
    n = doc.page_count
    for pno in range(n):
        page = doc[pno]
        if page.get_text("text").strip():
            _tick(progress, pno + 1, n, "Looking for blank pages")
            continue
        z = fonts.safe_zoom(page.rect, 40 / 72)
        pix = page.get_pixmap(matrix=fitz.Matrix(z, z), colorspace=fitz.csGRAY, alpha=False)
        samples = pix.samples
        dark = sum(1 for v in samples if v < 230)
        if dark <= max(1, int(len(samples) * threshold)):
            blank.append(pno)
        _tick(progress, pno + 1, n, "Looking for blank pages")
    return blank


def _write_new_file(folder: str, stem: str, ext: str, data: bytes) -> str:
    """Create a brand-new file (never overwriting one, never following a link planted in the folder)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    for k in range(1, 10000):
        name = f"{stem}.{ext}" if k == 1 else f"{stem} ({k}).{ext}"
        path = os.path.join(folder, name)
        try:
            fd = os.open(path, flags, 0o666)
        except FileExistsError:
            continue
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        return path
    raise OSError("Too many files with the same name.")


def extract_images(data: bytes, folder: str, stem: str, min_size: int = 24, password: str | None = None,
                   progress=None) -> list[str]:
    """Save every picture in the PDF as its own file (JPEG pictures keep their original bytes)."""
    doc = pdfops.open_bytes(data, password)
    saved: list[str] = []
    seen = set()
    n = doc.page_count
    safe_stem = safety.safe_filename_part(stem) or "image"
    for pno in range(n):
        for img in doc[pno].get_images(full=True):
            xref, smask = img[0], img[1]
            if xref in seen:
                continue
            seen.add(xref)
            try:
                if img[2] < min_size or img[3] < min_size:
                    continue
                info = doc.extract_image(xref)
                ext = (info.get("ext") or "png").lower()
                if smask or ext not in ("jpeg", "jpg", "png", "jp2", "jpx"):
                    pix = fitz.Pixmap(doc, xref)
                    if smask:
                        pix = fitz.Pixmap(pix, fitz.Pixmap(doc, smask))
                    if pix.n - pix.alpha >= 4:
                        pix = fitz.Pixmap(fitz.csRGB, pix)
                    blob, ext = pix.tobytes("png"), "png"
                else:
                    blob = info["image"]
                    ext = "jpg" if ext == "jpeg" else ext
                path = _write_new_file(folder, f"{safe_stem} p{pno + 1} img{len(saved) + 1}", ext, blob)
                saved.append(path)
            except Exception:
                continue
        _tick(progress, pno + 1, n, "Saving pictures")
    return saved


# --------------------------------------------------------------------------- comment summary

def collect_comments(doc: fitz.Document) -> list[dict]:
    rows = []
    for page in doc:
        words = None
        for a in page.annots():
            t = a.type[0]
            if t in annots.SKIP_TYPES:
                continue
            info = a.info or {}
            text = ""
            if len(rows) >= 20000:
                break
            if t in annots.MARKUP_TYPES:
                if words is None:
                    words = page.get_text("words", flags=pdfops.WORD_FLAGS)
                verts = (a.vertices or [])[:4000]
                rects = [fitz.Quad(verts[i:i + 4]).rect for i in range(0, len(verts) - 3, 4)]
                area = fitz.Rect(a.rect)
                near = [w for w in words if fitz.Rect(w[:4]).intersects(area)]
                picked = [w[4] for w in near if any(fitz.Rect(w[:4]).intersects(r) and
                                                     (fitz.Rect(w[:4]) & r).get_area() > 0.4 * fitz.Rect(w[:4]).get_area()
                                                     for r in rects)]
                text = " ".join(picked)
            date = info.get("modDate") or info.get("creationDate") or ""
            m = re.match(r"D:(\d{4})(\d{2})(\d{2})(\d{2})?(\d{2})?", date)
            when = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" + (f" {m.group(4)}:{m.group(5)}" if m and m.group(5) else "") if m else ""
            rows.append({"page": page.number + 1, "type": annots.type_name(a), "author": info.get("title", ""),
                         "date": when, "comment": info.get("content", ""), "text": text})
    return rows


def _md(text: str) -> str:
    text = (text or "").replace("\r", " ").replace("\n", " ")
    return re.sub(r"([\\`*_\[\]#|<>~])", r"\\\1", text)


def export_comments(doc: fitz.Document, path: str, title: str = "") -> int:
    rows = collect_comments(doc)
    if path.lower().endswith(".csv"):
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Page", "Type", "Author", "Date", "Comment", "Marked text"])
        for r in rows:
            cells = [r["page"], r["type"], r["author"], r["date"], r["comment"], r["text"]]
            writer.writerow([safety.csv_cell(c) for c in cells])
        data = buf.getvalue().encode("utf-8-sig")
    else:
        lines = [f"# Comments: {_md(title)}", "", f"{len(rows)} comment(s), exported "
                 f"{_dt.date.today().isoformat()}.", ""]
        page = None
        for r in rows:
            if r["page"] != page:
                page = r["page"]
                lines += ["", f"## Page {page}", ""]
            who = " ".join(x for x in (_md(r["author"]), _md(r["date"])) if x)
            line = f"- **{_md(r['type'])}**" + (f" ({who})" if who else "")
            if r["text"]:
                line += f": “{_md(r['text'])}”"
            if r["comment"] and r["comment"] != r["text"]:
                line += f" {_md(r['comment'])}"
            lines.append(line)
        data = ("\n".join(lines) + "\n").encode("utf-8")
    with open(path, "wb") as fh:
        fh.write(data)
    return len(rows)


# --------------------------------------------------------------------------- bookmarks from headings

def headings_toc_bytes(data: bytes, password: str | None = None, progress=None) -> list[list]:
    return headings_toc(pdfops.open_bytes(data, password), progress=progress)


def headings_toc(doc: fitz.Document, max_pages: int = 2000, progress=None) -> list[list]:
    """Guess the document's headings from text size (bigger than the body text, short, on their own)
    and turn them into bookmarks: [[level, title, page, {"kind": GOTO, "to": point}], ...]."""
    from collections import Counter
    n = min(doc.page_count, max_pages)
    sizes: Counter = Counter()
    candidates = []
    for pno in range(n):
        page = doc[pno]
        rot = page.rotation_matrix
        for block in page.get_text("dict", flags=pdfops.WORD_FLAGS, sort=True).get("blocks", []):
            if block.get("type") != 0:
                continue
            lines = [ln for ln in block.get("lines", []) if any(s.get("text", "").strip() for s in ln.get("spans", []))]
            for ln in lines:
                for s in ln.get("spans", []):
                    sizes[round(s.get("size", 0) * 2) / 2] += len(s.get("text", "").strip())
            if not lines or len(lines) > 3:
                continue
            spans = [s for ln in lines for s in ln.get("spans", []) if s.get("text", "").strip()]
            size = min(s.get("size", 0) for s in spans)
            title = re.sub(r"\s+", " ", " ".join("".join(s.get("text", "") for s in ln.get("spans", []))
                                                 for ln in lines)).strip()
            if not (2 <= len(title) <= 140) or not re.search(r"[^\W\d_]", title):
                continue
            top = (fitz.Point(block["bbox"][0], block["bbox"][1]) * rot)
            candidates.append((round(size * 2) / 2, title, pno, top.y))
        _tick(progress, pno + 1, n, "Looking for headings")
    if not sizes:
        return []
    body = sizes.most_common(1)[0][0]
    heads = [c for c in candidates if c[0] >= body * 1.18]
    # titles that repeat on many pages are running headers, not headings
    counts = Counter(c[1] for c in heads)
    heads = [c for c in heads if counts[c[1]] <= max(2, n * 0.3)]
    levels = sorted({c[0] for c in heads}, reverse=True)
    toc = []
    prev = 0
    for size, title, pno, y in heads[:1000]:
        level = min(3, levels.index(size) + 1)
        level = min(level, prev + 1)
        toc.append([level, title[:120], pno + 1, {"kind": fitz.LINK_GOTO, "page": pno,
                                                  "to": fitz.Point(0, max(0.0, y - 4)), "zoom": 0}])
        prev = level
    return toc
