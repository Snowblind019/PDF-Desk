"""PDF operations that do not need the GUI: page management, split/merge,
watermarks, headers/footers, compression, passwords, OCR, redaction search.

Functions that take a `fitz.Document` change it in place. Functions ending in
`_bytes` return a new PDF as bytes. Long-running ones accept a `progress`
callback: progress(done, total, message) -> bool (return False to cancel)."""
from __future__ import annotations

import datetime as _dt
import os
import re
from pathlib import Path
from typing import Callable, Iterable

import pymupdf as fitz

from pdfdesk import fonts, safety

Progress = Callable[[int, int, str], bool] | None

# Text flags that expand ligatures (so "ﬁ" is found as "fi") but keep spacing as in the file.
WORD_FLAGS = fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP
SEARCH_FLAGS = fitz.TEXT_DEHYPHENATE | fitz.TEXT_PRESERVE_WHITESPACE | fitz.TEXT_MEDIABOX_CLIP


class Cancelled(Exception):
    pass


def _tick(progress: Progress, done: int, total: int, msg: str = "") -> None:
    if progress is not None and progress(done, total, msg) is False:
        raise Cancelled()


# --------------------------------------------------------------------------- page ranges

def parse_page_range(text: str, page_count: int) -> list[int]:
    """Parse '1-3, 5, 8-' / 'all' / 'odd' / 'even' / 'last' into 0-based page indices."""
    text = (text or "").strip().lower()
    if text in ("", "all", "*"):
        return list(range(page_count))
    if text == "odd":
        return list(range(0, page_count, 2))
    if text == "even":
        return list(range(1, page_count, 2))
    result: list[int] = []
    for part in re.split(r"[,;\s]+", text):
        if not part:
            continue
        part = part.replace("last", str(page_count)).replace("end", str(page_count))
        if "-" in part:
            a, _, b = part.partition("-")
            start = int(a) if a else 1
            end = int(b) if b else page_count
        else:
            start = end = int(part)
        if start < 1 or end < 1 or start > page_count or end > page_count:
            raise ValueError(f"Page {max(start, end)} is outside 1-{page_count}.")
        step = 1 if end >= start else -1
        result.extend(i - 1 for i in range(start, end + step, step))
    if not result:
        raise ValueError("No pages selected.")
    return result


def format_page_list(pages: Iterable[int]) -> str:
    """[0,1,2,4] -> '1-3, 5'"""
    pages = sorted(set(pages))
    out, start, prev = [], None, None
    for p in pages:
        if start is None:
            start = prev = p
        elif p == prev + 1:
            prev = p
        else:
            out.append(f"{start + 1}" if start == prev else f"{start + 1}-{prev + 1}")
            start = prev = p
    if start is not None:
        out.append(f"{start + 1}" if start == prev else f"{start + 1}-{prev + 1}")
    return ", ".join(out)


# --------------------------------------------------------------------------- basics

def plain_bytes(doc: fitz.Document, garbage: int = 1) -> bytes:
    """Unencrypted copy of the document as bytes."""
    return doc.tobytes(garbage=garbage, deflate=True, encryption=fitz.PDF_ENCRYPT_NONE)


def open_bytes(data: bytes, password: str | None = None) -> fitz.Document:
    doc = fitz.open("pdf", data)
    if doc.needs_pass and password:
        doc.authenticate(password)
    return doc


def extract_pages_bytes(doc: fitz.Document, pages: list[int]) -> bytes:
    tmp = fitz.open("pdf", plain_bytes(doc, garbage=0))
    tmp.select(pages)
    return tmp.tobytes(garbage=3, deflate=True)


def rotate_pages(doc: fitz.Document, pages: Iterable[int], delta: int) -> None:
    for pno in pages:
        page = doc[pno]
        page.set_rotation((page.rotation + delta) % 360)


def delete_pages(doc: fitz.Document, pages: Iterable[int]) -> None:
    pages = sorted(set(pages))
    if len(pages) >= doc.page_count:
        raise ValueError("A PDF needs at least one page, so not every page can be deleted.")
    doc.delete_pages(pages)


def reorder_pages(doc: fitz.Document, order: list[int]) -> None:
    doc.select(order)


def insert_blank_page(doc: fitz.Document, index: int, like_page: int | None = None,
                      size: tuple[float, float] | None = None) -> None:
    if size is None:
        if like_page is not None and 0 <= like_page < doc.page_count:
            r = doc[like_page].rect
            size = (r.width, r.height)
        else:
            r = fitz.paper_rect("letter")
            size = (r.width, r.height)
    doc.new_page(pno=index, width=size[0], height=size[1])


def duplicate_page(doc: fitz.Document, pno: int) -> None:
    doc.fullcopy_page(pno, pno + 1 if pno + 1 < doc.page_count else -1)


def insert_pdf_bytes(doc: fitz.Document, data: bytes, index: int, password: str | None = None) -> int:
    src = open_bytes(data, password)
    if src.needs_pass:
        raise PermissionError("That PDF is password protected.")
    count = src.page_count
    doc.insert_pdf(src, start_at=index)
    return count


def merge_pdf_bytes(parts: list[bytes]) -> bytes:
    out = fitz.open()
    for data in parts:
        src = fitz.open("pdf", data)
        out.insert_pdf(src)
    if out.page_count == 0:
        raise ValueError("Nothing to merge.")
    return out.tobytes(garbage=3, deflate=True)


def crop_pages(doc: fitz.Document, pages: Iterable[int], left: float, top: float,
               right: float, bottom: float) -> None:
    """Trim margins (in points, as seen on screen) from the given pages."""
    for pno in pages:
        page = doc[pno]
        vis = page.rect
        keep = fitz.Rect(vis.x0 + left, vis.y0 + top, vis.x1 - right, vis.y1 - bottom)
        if keep.is_empty or keep.width < 10 or keep.height < 10:
            raise ValueError(f"The margins are larger than page {pno + 1}.")
        unrot = (keep * page.derotation_matrix).normalize()
        cb = page.cropbox
        new = fitz.Rect(cb.x0 + unrot.x0, cb.y0 + unrot.y0, cb.x0 + unrot.x1, cb.y0 + unrot.y1)
        page.set_cropbox(new & page.mediabox)


def reset_crop(doc: fitz.Document, pages: Iterable[int]) -> None:
    for pno in pages:
        page = doc[pno]
        page.set_cropbox(page.mediabox)


# --------------------------------------------------------------------------- split

def split_document(doc: fitz.Document, mode: str, value, out_dir: str, base_name: str,
                   progress: Progress = None) -> list[str]:
    """mode: 'every' (value = n pages per file), 'ranges' (value = list of range strings),
    'bookmarks' (top-level bookmarks), 'single' (one page per file)."""
    count = doc.page_count
    groups: list[tuple[str, list[int]]] = []
    if mode == "single":
        groups = [(f"page {i + 1}", [i]) for i in range(count)]
    elif mode == "every":
        n = max(1, int(value))
        for start in range(0, count, n):
            pages = list(range(start, min(count, start + n)))
            groups.append((f"pages {pages[0] + 1}-{pages[-1] + 1}", pages))
    elif mode == "ranges":
        for text in value:
            pages = parse_page_range(text, count)
            groups.append((f"pages {format_page_list(pages).replace(', ', '_')}", pages))
    elif mode == "bookmarks":
        toc = [t for t in doc.get_toc() if t[0] == 1 and t[2] >= 1]
        if not toc:
            raise ValueError("This PDF has no bookmarks to split by.")
        starts = [(t[1], t[2] - 1) for t in toc]
        if starts[0][1] > 0:
            starts.insert(0, ("start", 0))
        for i, (title, start) in enumerate(starts):
            end = starts[i + 1][1] if i + 1 < len(starts) else count
            if end > start:
                groups.append((title, list(range(start, end))))
    else:
        raise ValueError(f"Unknown split mode {mode}")
    os.makedirs(out_dir, exist_ok=True)
    data = plain_bytes(doc, garbage=0)
    written = []
    for i, (label, pages) in enumerate(groups):
        _tick(progress, i, len(groups), f"Writing part {i + 1} of {len(groups)}")
        part = fitz.open("pdf", data)
        part.select(pages)
        safe = safety.safe_filename_part(label) or f"part {i + 1}"
        base = safety.safe_filename_part(base_name) or "Document"
        name = f"{base} - {i + 1:02d} {safe}.pdf" if mode == "bookmarks" else f"{base} - {safe}.pdf"
        path = _unique_path(os.path.join(out_dir, name))
        part.save(path, garbage=3, deflate=True)
        written.append(path)
    _tick(progress, len(groups), len(groups), "Done")
    return written


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(f"{stem} ({i}){ext}"):
        i += 1
    return f"{stem} ({i}){ext}"


# --------------------------------------------------------------------------- stamping text

def _place_text(page: fitz.Page, vis_point: fitz.Point, text: str, size: float, color,
                family: str = "sans", bold: bool = False, opacity: float = 1.0,
                overlay: bool = True, angle: float = 0.0, pivot_vis: fitz.Point | None = None) -> None:
    """Write text at a point given in on-screen (rotated) page coordinates so it reads upright."""
    point = vis_point * page.derotation_matrix
    morph = None
    if angle:
        pivot = (pivot_vis or vis_point) * page.derotation_matrix
        morph = (pivot, fitz.Matrix(angle))
    fonts.insert_text(page, point, text, size=size, color=color, family=family, bold=bold,
                      rotate=page.rotation, morph=morph, opacity=opacity, overlay=overlay)


def add_text_watermark(doc: fitz.Document, pages: Iterable[int], text: str, size: float = 60,
                       color=(0.8, 0.1, 0.1), opacity: float = 0.25, angle: float = 45,
                       behind: bool = False, family: str = "sans", bold: bool = True) -> None:
    lines = [ln for ln in text.splitlines() if ln.strip()] or [text]
    for pno in pages:
        page = doc[pno]
        vis = page.rect
        center = fitz.Point(vis.width / 2, vis.height / 2)
        line_h = size * 1.2
        total_h = line_h * len(lines)
        for i, line in enumerate(lines):
            w = fonts.text_width(line, size, family, bold)
            y = center.y - total_h / 2 + line_h * (i + 1) - size * 0.25
            _place_text(page, fitz.Point(center.x - w / 2, y), line, size, color, family, bold,
                        opacity, overlay=not behind, angle=angle, pivot_vis=center)


def add_image_watermark(doc: fitz.Document, pages: Iterable[int], image_path: str,
                        scale: float = 0.5, opacity: float = 0.3, behind: bool = False) -> None:
    from PIL import Image
    import io
    img = Image.open(image_path).convert("RGBA")
    if opacity < 1:
        alpha = img.getchannel("A").point(lambda a: int(a * opacity))
        img.putalpha(alpha)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    data = buf.getvalue()
    ratio = img.height / max(1, img.width)
    for pno in pages:
        page = doc[pno]
        vis = page.rect
        w = vis.width * scale
        h = w * ratio
        if h > vis.height * scale:
            h = vis.height * scale
            w = h / ratio
        box_vis = fitz.Rect((vis.width - w) / 2, (vis.height - h) / 2, (vis.width + w) / 2, (vis.height + h) / 2)
        box = (box_vis * page.derotation_matrix).normalize()
        fonts.ensure_wrapped(page)
        page.insert_image(box, stream=data, overlay=not behind, rotate=page.rotation)


def expand_tokens(text: str, page_num: int, total: int, filename: str = "", title: str = "") -> str:
    today = _dt.date.today()
    return (text.replace("{page}", str(page_num))
                .replace("{pages}", str(total))
                .replace("{date}", today.isoformat())
                .replace("{filename}", filename)
                .replace("{title}", title or filename))


def add_header_footer(doc: fitz.Document, pages: Iterable[int], slots: dict, size: float = 10,
                      color=(0, 0, 0), margin: float = 28, start_number: int = 1,
                      family: str = "sans", filename: str = "", count_from_selection: bool = False) -> None:
    """slots keys: header_left/center/right, footer_left/center/right. Tokens: {page} {pages} {date} {filename} {title}."""
    pages = list(pages)
    title = doc.metadata.get("title") or ""
    total = (len(pages) if count_from_selection else doc.page_count) + start_number - 1
    for n, pno in enumerate(pages):
        page = doc[pno]
        vis = page.rect
        number = (n if count_from_selection else pno) + start_number
        for key, raw in slots.items():
            if not raw:
                continue
            text = expand_tokens(raw, number, total, filename, title)
            w = fonts.text_width(text, size, family)
            y = margin + size if key.startswith("header") else vis.height - margin
            if key.endswith("left"):
                x = margin
            elif key.endswith("right"):
                x = vis.width - margin - w
            else:
                x = (vis.width - w) / 2
            _place_text(page, fitz.Point(x, y), text, size, color, family)


# --------------------------------------------------------------------------- security & cleanup

def protect_bytes(doc: fitz.Document, user_pw: str, owner_pw: str, allow_print: bool = True,
                  allow_copy: bool = True, allow_modify: bool = True, allow_annotate: bool = True,
                  allow_forms: bool = True, allow_assemble: bool = True) -> bytes:
    perms = fitz.PDF_PERM_ACCESSIBILITY
    if allow_print:
        perms |= fitz.PDF_PERM_PRINT | fitz.PDF_PERM_PRINT_HQ
    if allow_copy:
        perms |= fitz.PDF_PERM_COPY
    if allow_modify:
        perms |= fitz.PDF_PERM_MODIFY
    if allow_annotate:
        perms |= fitz.PDF_PERM_ANNOTATE
    if allow_forms:
        perms |= fitz.PDF_PERM_FORM
    if allow_assemble:
        perms |= fitz.PDF_PERM_ASSEMBLE
    owner_pw = owner_pw or user_pw
    if not owner_pw:
        raise ValueError("Enter at least one password.")
    return doc.tobytes(garbage=1, deflate=True, encryption=fitz.PDF_ENCRYPT_AES_256,
                       user_pw=user_pw or "", owner_pw=owner_pw, permissions=perms)


def flatten(doc: fitz.Document, annots: bool = True, widgets: bool = True) -> None:
    doc.bake(annots=annots, widgets=widgets)


def remove_hidden_data(doc: fitz.Document) -> None:
    """Strip metadata, JavaScript, attachments, hidden text and thumbnails."""
    doc.scrub(attached_files=True, clean_pages=True, embedded_files=True, hidden_text=True,
              javascript=True, metadata=True, redactions=False, remove_links=False,
              reset_fields=False, reset_responses=True, thumbnails=True, xml_metadata=True)


def compress_bytes(data: bytes, password: str | None = None, level: str = "medium",
                   dpi: int | None = None, quality: int | None = None,
                   progress: Progress = None) -> bytes:
    """level: lossless | medium | high | custom (uses dpi/quality)."""
    doc = open_bytes(data, password)
    _tick(progress, 0, 3, "Optimizing images")
    presets = {"medium": (150, 80), "high": (96, 55)}
    if level in presets or level == "custom":
        target, q = presets.get(level, (dpi or 150, quality or 75))
        if level == "custom":
            target, q = dpi or 150, quality or 75
        try:
            doc.rewrite_images(dpi_threshold=int(target * 1.3), dpi_target=int(target), quality=int(q))
        except Exception:
            pass
    _tick(progress, 1, 3, "Shrinking fonts")
    if level in ("medium", "high", "custom"):
        try:
            doc.subset_fonts()
        except Exception:
            pass
    _tick(progress, 2, 3, "Writing optimized file")
    out = doc.tobytes(garbage=4, clean=True, deflate=True, deflate_images=True, deflate_fonts=True,
                      use_objstms=1, encryption=fitz.PDF_ENCRYPT_KEEP)
    _tick(progress, 3, 3, "Done")
    return out


# --------------------------------------------------------------------------- OCR

def ocr_bytes(data: bytes, password: str | None, language: str, tessdata: str, dpi: int = 300,
              pages: list[int] | None = None, skip_text_pages: bool = True,
              progress: Progress = None) -> tuple[bytes, int]:
    """Add an invisible, searchable text layer to scanned pages. The page images are left
    untouched (a 'sandwich' PDF). Returns (new bytes, pages processed)."""
    doc = open_bytes(data, password)
    pages = list(range(doc.page_count)) if pages is None else pages
    font = fonts.unicode_font()
    done = 0
    for i, pno in enumerate(pages):
        _tick(progress, i, len(pages), f"Reading page {pno + 1} of {doc.page_count}")
        page = doc[pno]
        if skip_text_pages and page.get_text("text").strip():
            continue
        page_dpi = max(36, int(72 * fonts.safe_zoom(page.rect, dpi / 72)))  # huge pages: lower dpi
        tp = page.get_textpage_ocr(flags=0, language=language, dpi=page_dpi, full=True, tessdata=tessdata)
        words = page.get_text("words", textpage=tp)
        if not words:
            continue
        # Work out each word's box as seen on screen so the hidden text runs the same way
        # as the visible text, then write it upright with one content stream per page.
        fonts.ensure_wrapped(page)
        name = font.register(page)
        shape = page.new_shape()
        for *box, word, _b, _l, _w in words:
            r = fitz.Rect(box)
            if page.rotation:
                r = (r * page.rotation_matrix).normalize()
            w, h = r.width, r.height
            if w <= 0 or h <= 0 or not word.strip():
                continue
            unit = font.font.text_length(word, fontsize=1)
            size = max(2.0, min(h * 1.1, w / unit if unit else h))
            baseline = fitz.Point(r.x0, r.y1 - (h - size * 0.8) / 2 - size * 0.2)
            try:
                shape.insert_text(baseline * page.derotation_matrix, word, fontname=name,
                                  fontsize=size, rotate=page.rotation, render_mode=3)
            except Exception:
                continue
        shape.commit()
        done += 1
    _tick(progress, len(pages), len(pages), "Saving")
    return doc.tobytes(garbage=3, deflate=True, encryption=fitz.PDF_ENCRYPT_KEEP), done


# --------------------------------------------------------------------------- search & redact

REDACT_PRESETS = {
    "Email addresses": r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,8}\.[A-Za-z]{2,24}\b",
    "Phone numbers": r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}",
    "US Social Security numbers": r"\b\d{3}-\d{2}-\d{4}\b",
    "Card numbers": r"\b(?:\d[ -]?){13,16}\b",
    "IPv4 addresses": r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b",
    "Dates (numeric)": r"\b\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}\b",
    "AWS account IDs (12 digits)": r"\b\d{12}\b",
    "AWS access key IDs": r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b",
    "AWS ARNs": r"arn:aws[a-zA-Z-]*:[^\s]+",
}


def find_matches(page: fitz.Page, needle: str = "", regex: str | None = None,
                 whole_word: bool = False) -> list[tuple[str, list[fitz.Rect]]]:
    """Return (matched text, rects) pairs for plain text or a regular expression."""
    found: list[tuple[str, list[fitz.Rect]]] = []
    if regex:
        text = page.get_text("text", flags=WORD_FLAGS)
        seen = set()
        for m in re.finditer(regex, text):
            s = m.group(0).strip()
            if not s or s in seen:
                continue
            seen.add(s)
            # search_for cannot span line breaks, so search each line part separately.
            rects = []
            for part in s.splitlines():
                if part.strip():
                    rects += page.search_for(part.strip(), flags=SEARCH_FLAGS)
            if rects:
                found.append((s, rects))
    elif needle:
        rects = page.search_for(needle, flags=SEARCH_FLAGS)
        if whole_word:
            words = page.get_text("words", flags=WORD_FLAGS)
            keep = []
            for r in rects:
                for w in words:
                    wr = fitz.Rect(w[:4])
                    if wr.intersects(r) and w[4].strip(".,;:!?()[]\"'").lower() == needle.lower():
                        keep.append(r)
                        break
            rects = keep
        if rects:
            found.append((needle, rects))
    return found


def mark_redactions(doc: fitz.Document, pages: Iterable[int], needle: str = "", regex: str | None = None,
                    whole_word: bool = False, fill=(0, 0, 0)) -> int:
    count = 0
    for pno in pages:
        page = doc[pno]
        for _text, rects in find_matches(page, needle, regex, whole_word):
            for r in rects:
                page.add_redact_annot(r, fill=fill)
                count += 1
    return count


def redaction_pages(doc: fitz.Document) -> list[int]:
    out = []
    for page in doc:
        for annot in page.annots(types=[fitz.PDF_ANNOT_REDACT]):
            out.append(page.number)
            break
    return out


def apply_redactions(doc: fitz.Document) -> int:
    count = 0
    for pno in redaction_pages(doc):
        page = doc[pno]
        page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_PIXELS,
                              graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                              text=fitz.PDF_REDACT_TEXT_REMOVE)
        count += 1
    return count


# --------------------------------------------------------------------------- edit text

def replace_text_line(page: fitz.Page, bbox: fitz.Rect, origin: fitz.Point, new_text: str,
                      size: float, color, family: str, bold: bool, italic: bool) -> None:
    """Remove the text inside bbox and write new_text at origin (both unrotated page coords)."""
    shrink = bbox.height * 0.15
    area = fitz.Rect(bbox.x0, bbox.y0 + shrink, bbox.x1, bbox.y1 - shrink)
    page.add_redact_annot(area, fill=False)
    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE, graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                          text=fitz.PDF_REDACT_TEXT_REMOVE)
    if new_text.strip():
        fonts.insert_text(page, origin, new_text, size=size, color=color, family=family,
                          bold=bold, italic=italic, rotate=page.rotation)


# --------------------------------------------------------------------------- misc

def file_size_text(num: int) -> str:
    size = float(num)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{num} bytes"


def page_size_text(rect: fitz.Rect) -> str:
    w_in, h_in = rect.width / 72, rect.height / 72
    w_mm, h_mm = rect.width / 72 * 25.4, rect.height / 72 * 25.4
    name = ""
    for paper in ("letter", "legal", "a4", "a3", "a5", "tabloid"):
        pr = fitz.paper_rect(paper)
        if (abs(pr.width - rect.width) < 2 and abs(pr.height - rect.height) < 2) or \
           (abs(pr.width - rect.height) < 2 and abs(pr.height - rect.width) < 2):
            name = paper.upper() if paper.startswith("a") else paper.capitalize()
            break
    base = f"{w_in:.2f} x {h_in:.2f} in ({w_mm:.0f} x {h_mm:.0f} mm)"
    return f"{name}, {base}" if name else base


def base_name(path: str | None, fallback: str = "Document") -> str:
    return Path(path).stem if path else fallback
