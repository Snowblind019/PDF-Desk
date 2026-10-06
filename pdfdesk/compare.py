"""Compare two PDFs: find words that were added, removed or changed, and make a report PDF that shows
both versions side by side with the differences marked (red = removed, green = added)."""
from __future__ import annotations

import difflib

import pymupdf as fitz

from pdfdesk import pdfops

RED = (0.86, 0.15, 0.15)
GREEN = (0.1, 0.62, 0.25)
MAX_WORDS = 150_000


def _words(doc: fitz.Document) -> list[tuple]:
    """(text, page, rect in visible page coordinates) for every word, in reading order."""
    out = []
    for page in doc:
        rot = page.rotation_matrix
        for w in page.get_text("words", sort=True, flags=pdfops.WORD_FLAGS):
            out.append((w[4], page.number, (fitz.Rect(w[:4]) * rot).normalize()))
            if len(out) >= MAX_WORDS:
                return out
    return out


def diff(old: fitz.Document, new: fitz.Document, progress=None) -> dict:
    a, b = _words(old), _words(new)
    pdfops._tick(progress, 1, 3, "Comparing the text")
    # exact matching gets slow on very long documents; the faster heuristic is fine there
    matcher = difflib.SequenceMatcher(None, [w[0] for w in a], [w[0] for w in b],
                                      autojunk=len(a) + len(b) > 40_000)
    removed, added, changes = [], [], []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        gone, came = a[i1:i2], b[j1:j2]
        removed += gone
        added += came
        changes.append({
            "kind": {"replace": "Changed", "delete": "Removed", "insert": "Added"}[tag],
            "old_text": " ".join(w[0] for w in gone)[:300], "new_text": " ".join(w[0] for w in came)[:300],
            "old_page": gone[0][1] if gone else (a[i1 - 1][1] if i1 else 0),
            "new_page": came[0][1] if came else (b[j1 - 1][1] if j1 else 0),
        })
    return {"removed": removed, "added": added, "changes": changes, "old_words": len(a), "new_words": len(b),
            "ratio": matcher.ratio() if a or b else 1.0}


def _flatten(doc: fitz.Document) -> None:
    """Make comments and form entries part of the pages (in this private copy only). MuPDF's word list
    includes the text of text boxes, stamps and fields, but the page pictures in the report don't show
    them, so without this the report marks words over empty space."""
    try:
        doc.bake()
        return
    except Exception:  # noqa: BLE001 - a broken comment: leave all of them out instead
        pass
    for page in doc:
        for xref in [a.xref for a in page.annots()]:
            page.delete_annot(page.load_annot(xref))
        for xref in [w.xref for w in page.widgets()]:
            page.delete_widget(page.load_widget(xref))


def report_bytes(old_data: bytes, new_data: bytes, old_name: str, new_name: str, old_password=None,
                 new_password=None, progress=None) -> tuple[bytes, dict]:
    old = pdfops.open_bytes(old_data, old_password)
    new = pdfops.open_bytes(new_data, new_password)
    _flatten(old)
    _flatten(new)
    result = diff(old, new, progress)
    out = fitz.open()
    _summary_pages(out, result, old_name, new_name, old.page_count, new.page_count)
    n = max(old.page_count, new.page_count)
    marks_old: dict[int, list] = {}
    marks_new: dict[int, list] = {}
    for _t, p, r in result["removed"]:
        marks_old.setdefault(p, []).append(r)
    for _t, p, r in result["added"]:
        marks_new.setdefault(p, []).append(r)
    W, H, margin, gap, head = 842, 595, 24, 18, 26
    half = (W - 2 * margin - gap) / 2
    for k in range(n):
        sheet = out.new_page(width=W, height=H)
        sheet.insert_text((margin, margin + 6), f"Page {k + 1}", fontname="hebo", fontsize=11)
        changed = bool(marks_old.get(k) or marks_new.get(k))
        sheet.insert_text((margin + 70, margin + 6), "differences marked" if changed else "no differences",
                          fontname="helv", fontsize=9, color=RED if changed else (0.4, 0.4, 0.4))
        for side, doc, marks, color, title in ((0, old, marks_old, RED, old_name),
                                               (1, new, marks_new, GREEN, new_name)):
            x0 = margin + side * (half + gap)
            area = fitz.Rect(x0, margin + head, x0 + half, H - margin)
            sheet.insert_text((x0, margin + head - 6), (("Old: " if side == 0 else "New: ") + title)[:80],
                              fontname="helv", fontsize=8, color=color)
            if k >= doc.page_count:
                sheet.draw_rect(area, color=(0.8, 0.8, 0.8), width=0.5, dashes="[3] 0")
                sheet.insert_text((area.x0 + 10, area.y0 + 20), "(no such page)", fontname="helv", fontsize=9,
                                  color=(0.5, 0.5, 0.5))
                continue
            src = doc[k]
            pr = src.rect
            s = min(area.width / pr.width, area.height / pr.height)
            target = fitz.Rect(area.x0, area.y0, area.x0 + pr.width * s, area.y0 + pr.height * s)
            sheet.show_pdf_page(target, doc, k)
            sheet.draw_rect(target, color=(0.75, 0.75, 0.75), width=0.5)
            for r in marks.get(k, []):
                box = fitz.Rect(target.x0 + r.x0 * s - 1, target.y0 + r.y0 * s - 1, target.x0 + r.x1 * s + 1,
                                target.y0 + r.y1 * s + 1)
                sheet.draw_rect(box, color=color, fill=color, width=0.6, fill_opacity=0.22, stroke_opacity=0.9)
        pdfops._tick(progress, k + 1, n, "Making the report")
    first = out.page_count - n
    out.set_toc([[1, "Summary", 1]] + [[1, f"Page {k + 1}", first + k + 1] for k in range(n)])
    return out.tobytes(garbage=3, deflate=True), {k: v for k, v in result.items() if k in ("changes", "ratio")}


def _summary_pages(out: fitz.Document, result: dict, old_name: str, new_name: str, old_pages: int,
                   new_pages: int) -> None:
    import html
    import io
    changes = result["changes"]
    rows = []
    for c in changes[:400]:
        rows.append(f"<tr><td>{c['kind']}</td><td>{c['old_page'] + 1} / {c['new_page'] + 1}</td>"
                    f"<td class='old'>{html.escape(c['old_text'])}</td>"
                    f"<td class='new'>{html.escape(c['new_text'])}</td></tr>")
    more = f"<p>...and {len(changes) - 400} more.</p>" if len(changes) > 400 else ""
    same = round(result["ratio"] * 100, 1)
    table = ("<table><tr><th>Change</th><th>Page (old / new)</th><th>Old text</th><th>New text</th></tr>"
             + "".join(rows) + "</table>" + more) if changes else "<p>No differences in the text.</p>"
    body = (f"<h1>Comparison report</h1>"
            f"<p><b>Old:</b> {html.escape(old_name)} ({old_pages} pages, {result['old_words']} words)<br/>"
            f"<b>New:</b> {html.escape(new_name)} ({new_pages} pages, {result['new_words']} words)</p>"
            f"<p>The text is {same}% the same. {len(changes)} difference(s) found: "
            f"<span class='old'>removed text is marked in red</span>, "
            f"<span class='new'>added text in green</span>.</p>" + table)
    css = ("body{font-family:sans-serif;font-size:10pt} h1{font-size:18pt} table{border-collapse:collapse;width:100%}"
           "td,th{border:1px solid #ccc;padding:3px;vertical-align:top;font-size:8.5pt} th{background:#eee}"
           ".old{color:#b02020} .new{color:#18803a}")
    story = fitz.Story(html=body, user_css=css)
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    more_text = True
    while more_text:
        device = writer.begin_page(fitz.Rect(0, 0, 595, 842))
        more_text, _filled = story.place(fitz.Rect(40, 40, 555, 802))
        story.draw(device)
        writer.end_page()
    writer.close()
    out.insert_pdf(fitz.open("pdf", buf.getvalue()))
