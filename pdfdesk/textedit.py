"""Editing text that is part of the page (not a comment): find the paragraph under the mouse,
turn it into formatted text for the editor, and write the changed text back with the chosen fonts.

Fonts: the original font is matched by name to an installed font (for example "Calibri" or its
look-alike "Carlito"), so edited text looks like the rest of the page. Only the letters that are
used get embedded.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right

import pymupdf as fitz

from pdfdesk import appearance, fontcatalog, fonts, pdfops
from pdfdesk import richtext as RT

SUPERSCRIPT_FLAG = 1


def _hex(rgb) -> str:
    r, g, b = (max(0, min(255, round(c * 255))) for c in fonts.to_rgb(rgb))
    return f"#{r:02x}{g:02x}{b:02x}"


def span_run(span: dict) -> RT.Run:
    cat = fontcatalog.catalog()
    family, bold, italic = fonts.span_style(span)
    fam, bold2, italic2 = cat.match_pdf_font(span.get("font") or "", bold, italic)
    if fam is None:
        fam = {"serif": "Times", "mono": "Courier"}.get(family, cat.default_family() if not fonts.is_latin1(
            span.get("text", "")) else "Helvetica")
        fam = cat.resolve(fam)
    size = span.get("size") or 12
    size = size if 1 <= size <= 400 else 12
    return RT.Run(span.get("text", ""), fam, round(size * 2) / 2, bold2, italic2,
                  color=_hex(fonts.int_to_rgb(span.get("color", 0) or 0)),
                  valign="super" if span.get("flags", 0) & SUPERSCRIPT_FLAG and size < 9 else "")


def page_lines(page: fitz.Page, text_dict: dict) -> list[dict]:
    """Every line of text on the page with its position in visible (rotated) coordinates."""
    rot = page.rotation_matrix
    derot = page.derotation_matrix
    expect = fitz.Point(derot.a, derot.b)
    out = []
    for block in text_dict.get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if s.get("text")]
            if not spans or not "".join(s["text"] for s in spans).strip():
                continue
            d = line.get("dir", (1, 0))
            bbox = fitz.Rect(line["bbox"])
            out.append({"bbox": bbox, "vis": (bbox * rot).normalize(), "origin": fitz.Point(spans[0]["origin"]),
                        "spans": spans, "upright": abs(d[0] * expect.x + d[1] * expect.y) > 0.98,
                        "size": max((s.get("size") or 0) for s in spans) or 10})
    out.sort(key=lambda ln: ln["vis"].y0)  # sorted top to bottom so neighbours can be found quickly
    return out


def _overlap(a: fitz.Rect, b: fitz.Rect) -> float:
    inter = min(a.x1, b.x1) - max(a.x0, b.x0)
    return inter / max(1.0, min(a.width, b.width))


def _neighbour(cur: dict, lines: list[dict], used: set, below: bool, gap_limit: float,
               ys: list[float]) -> dict | None:
    best, best_gap = None, None
    reach = cur["vis"].height * 3 + gap_limit + 2
    lo = bisect_left(ys, cur["vis"].y0 - reach)
    hi = bisect_right(ys, cur["vis"].y1 + reach)
    for ln in lines[lo:hi]:
        if id(ln) in used or ln["upright"] != cur["upright"]:
            continue
        if not (0.8 <= ln["size"] / cur["size"] <= 1.25) or _overlap(cur["vis"], ln["vis"]) < 0.3:
            continue
        gap = ln["vis"].y0 - cur["vis"].y1 if below else cur["vis"].y0 - ln["vis"].y1
        if gap < -cur["vis"].height * 0.35 or gap > gap_limit:
            continue
        if best_gap is None or gap < best_gap:
            best, best_gap = ln, gap
    return best


def block_at(page: fitz.Page, text_dict: dict, pt: fitz.Point, lines: list[dict] | None = None) -> dict | None:
    """The paragraph at an unrotated page point: the line there plus the lines just above and below
    it that line up with it, with what's needed to edit it."""
    lines = lines if lines is not None else page_lines(page, text_dict)
    ys = [ln["vis"].y0 for ln in lines]
    hit = next((ln for ln in lines if (ln["bbox"] + (-1, -1, 1, 1)).contains(pt)), None)
    if hit is None:
        return None
    group = [hit]
    used = {id(hit)}
    pitch_gap = None
    for below in (True, False):
        cur = hit
        while len(group) < 400:
            limit = cur["vis"].height * 0.5 if pitch_gap is None else pitch_gap + cur["size"] * 0.25
            nxt = _neighbour(cur, lines, used, below, limit, ys)
            if nxt is None:
                break
            gap = nxt["vis"].y0 - cur["vis"].y1 if below else cur["vis"].y0 - nxt["vis"].y1
            pitch_gap = gap if pitch_gap is None else max(pitch_gap, gap)
            used.add(id(nxt))
            group.append(nxt)
            cur = nxt
    group.sort(key=lambda ln: (ln["vis"].y0, ln["vis"].x0))
    bbox = fitz.Rect(group[0]["bbox"])
    for ln in group[1:]:
        bbox |= ln["bbox"]
    return {"pno": page.number, "bbox": bbox, "lines": group, "editable": all(ln["upright"] for ln in group),
            "text": "\n".join("".join(s["text"] for s in ln["spans"]) for ln in group)}


def _vis(page: fitz.Page, rect: fitz.Rect) -> fitz.Rect:
    return (fitz.Rect(rect) * page.rotation_matrix).normalize()


def detect_align(page: fitz.Page, info: dict) -> str:
    rects = [_vis(page, ln["bbox"]) for ln in info["lines"]]
    if len(rects) < 2:
        return "left"
    lefts = [r.x0 for r in rects]
    rights = [r.x1 for r in rects]
    centers = [(r.x0 + r.x1) / 2 for r in rects]
    same = lambda vals: max(vals) - min(vals) < 2.5  # noqa: E731
    if same(lefts) and len(rects) > 2 and same(rights[:-1]):
        return "justify"
    if same(lefts):
        return "left"
    if same(centers):
        return "center"
    if same(rights):
        return "right"
    return "left"


def line_pitch(page: fitz.Page, info: dict) -> float | None:
    if len(info["lines"]) < 2:
        return None
    ys = [(ln["origin"] * page.rotation_matrix).y for ln in info["lines"]]
    gaps = [b - a for a, b in zip(ys, ys[1:]) if b - a > 0.5]
    return sum(gaps) / len(gaps) if gaps else None


def block_to_box(page: fitz.Page, info: dict) -> RT.Box:
    """Paragraphs and formatting of a text block. Wrapped lines are joined back into paragraphs."""
    width = _vis(page, info["bbox"]).width
    align = detect_align(page, info)
    paras: list[RT.Para] = []
    cur: list[RT.Run] = []
    n = len(info["lines"])
    for k, ln in enumerate(info["lines"]):
        runs = [span_run(s) for s in ln["spans"]]
        if cur and runs:
            prev = cur[-1].text
            if prev and not prev.endswith(("-", " ")):
                cur[-1] = RT.Run(prev + " ", **{f: getattr(cur[-1], f) for f in (
                    "font", "size", "bold", "italic", "underline", "strike", "color", "highlight", "valign")})
        cur.extend(runs)
        line_w = _vis(page, ln["bbox"]).width
        ends_para = k == n - 1 or (align in ("left", "justify") and line_w < width * 0.72)
        if ends_para:
            paras.append(RT.Para(cur, align=align))
            cur = []
    if cur:
        paras.append(RT.Para(cur, align=align))
    box = RT.Box(paras=paras, padding=0, auto_width=False)
    return box.normalize()


def _removal_rects(page: fitz.Page, info: dict) -> list[fitz.Rect]:
    rects = []
    for ln in info["lines"]:
        bb = ln["bbox"]
        shrink_y = bb.height * 0.15 if abs(page.rotation) % 180 == 0 else 0
        shrink_x = bb.width * 0.15 if abs(page.rotation) % 180 == 90 else 0
        rects.append(fitz.Rect(bb.x0 + shrink_x, bb.y0 + shrink_y, bb.x1 - shrink_x, bb.y1 - shrink_y))
    return rects


def _font_name(page: fitz.Page, run: RT.Run, text: str) -> str:
    """Register the font for a run on the page and return its resource name."""
    return fonts.register_font(page, run.font, run.bold, run.italic, text)


def write_line(page: fitz.Page, origin: fitz.Point, runs: list[RT.Run]) -> None:
    """Write one line of differently formatted pieces starting at `origin` (unrotated page coords)."""
    fonts.ensure_wrapped(page)
    rot, derot = page.rotation_matrix, page.derotation_matrix
    vis = origin * rot
    for run in runs:
        text = run.text.replace("\t", "    ")
        if not text:
            continue
        size = run.size * (0.65 if run.valign else 1)
        rise = run.size * 0.33 if run.valign == "super" else (-run.size * 0.12 if run.valign == "sub" else 0)
        width = fontcatalog.text_width(run.font, run.bold, run.italic, text, size)
        color = RT._rgb(run.color) or (0, 0, 0)
        if run.highlight:
            hl = fitz.Rect(vis.x, vis.y - run.size * 0.95, vis.x + width, vis.y + run.size * 0.25)
            page.draw_rect((hl * derot).normalize(), color=None, fill=RT._rgb(run.highlight), overlay=True)
        name = _font_name(page, run, text)
        page.insert_text(fitz.Point(vis.x, vis.y - rise) * derot, text, fontsize=size, fontname=name, color=color,
                         rotate=page.rotation)
        for on, dy in ((run.underline, run.size * 0.12), (run.strike, -run.size * 0.28)):
            if on:
                a = fitz.Point(vis.x, vis.y + dy) * derot
                b = fitz.Point(vis.x + width, vis.y + dy) * derot
                page.draw_line(a, b, color=color, width=max(0.5, run.size * 0.06))
        vis = fitz.Point(vis.x + width, vis.y)


def _css_html(box: RT.Box, line_height: float | None) -> tuple[str, str, fitz.Archive]:
    html_text, css, arch = RT.to_html(box)
    if line_height:
        css += f"\np, li {{line-height: {line_height:.3f};}}"
    return html_text, css, arch


def replace_block(page: fitz.Page, info: dict, box: RT.Box) -> None:
    """Swap the text of a block for the edited text, keeping images and drawings around it.
    The new text is laid out first, so nothing is removed if it can't be placed."""
    pitch = line_pitch(page, info)
    first_line = info["lines"][0]
    rotation = page.rotation
    if box.is_empty():
        pdfops.remove_text(page, _removal_rects(page, info))
        return
    single = len(box.paras) == 1 and len(info["lines"]) == 1 and box.paras[0].align == "left" \
        and not box.paras[0].list
    if single:
        width = sum(fontcatalog.text_width(r.font, r.bold, r.italic, r.text, r.size) for r in box.paras[0].runs)
        if width <= page.rect.width:
            page = pdfops.remove_text(page, _removal_rects(page, info))
            write_line(page, first_line["origin"], box.paras[0].runs)
            return
    vis_bbox = _vis(page, info["bbox"])
    size = box.first_run().size or 12
    line_height = (pitch / size) if pitch else None
    html_text, css, arch = _css_html(box, line_height)
    # Start where the block started; allow it to grow down to the bottom margin.
    top = vis_bbox.y0 - size * 0.08
    slack = size * 0.35 + 1  # the original layout may have had a hair more room than its widest line
    right = min(page.rect.width, max(vis_bbox.x1, vis_bbox.x0 + 20) + slack)
    rect = fitz.Rect(vis_bbox.x0, top, right, page.rect.height - 4)
    trial = fitz.open()
    tpage = trial.new_page(width=page.rect.width, height=page.rect.height)
    spare, _scale = tpage.insert_htmlbox(rect, html_text, css=css, archive=arch, scale_low=1)
    if spare < 0:
        raise ValueError("The new text doesn't fit between here and the bottom of the page (or a word is "
                         "wider than the paragraph). Shorten it or choose a smaller size.")
    unrot = (rect * page.derotation_matrix).normalize()
    page = pdfops.remove_text(page, _removal_rects(page, info))
    fonts.ensure_wrapped(page)
    with appearance.unrotated(page):
        spare, _scale = page.insert_htmlbox(unrot, html_text, css=css, archive=arch, scale_low=1, rotate=rotation)
    if spare < 0:
        raise ValueError("The new text couldn't be placed on the page.")
