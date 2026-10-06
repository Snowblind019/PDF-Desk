"""Tests for document-wide tools and Prepare Form. Run with:  python -m pytest -q tests"""
import gc
import os
import sys
import tempfile

import pymupdf as fitz
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("PDFDESK_HOME", tempfile.mkdtemp(prefix="pdfdesk-test-home-"))

from pdfdesk import docfeatures as DF  # noqa: E402
from pdfdesk import forms, pdfops  # noqa: E402
from pdfdesk import richtext as RT  # noqa: E402


def five_pages() -> fitz.Document:
    doc = fitz.open()
    for k in range(5):
        p = doc.new_page(width=612, height=792)
        if k != 2:
            p.insert_text((72, 100), f"Page {k + 1} text", fontsize=14)
    return doc


def fresh_links(doc, pno):
    gc.collect()  # the PDF engine keeps a page's links until the page object is gone
    return doc[pno].get_links()


def test_links_only_go_to_pages_or_web():
    doc = five_pages()
    doc[3].set_rotation(90)
    DF.add_link(doc[0], fitz.Rect(72, 200, 200, 220), {"page": 4})
    DF.add_link(doc[0], fitz.Rect(72, 240, 200, 260), {"uri": "https://example.com/x"})
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "smb://host/share", "https://user@evil.example/"):
        with pytest.raises(ValueError):
            DF.add_link(doc[0], fitz.Rect(72, 280, 200, 300), {"uri": bad})
    links = fresh_links(doc, 0)
    assert [(lk["kind"], lk.get("page"), lk.get("uri")) for lk in links] == [
        (fitz.LINK_GOTO, 4, None), (fitz.LINK_URI, None, "https://example.com/x")]
    # on a turned page the link lands where it was drawn
    DF.add_link(doc[3], fitz.Rect(72, 72, 200, 100), {"page": 0})
    assert fresh_links(doc, 3)[0]["from"] == fitz.Rect(72, 72, 200, 100)
    xref = fresh_links(doc, 0)[0]["xref"]
    DF.replace_link(doc[0], xref, {"uri": "mailto:a@example.com"})
    assert {lk.get("uri") for lk in fresh_links(doc, 0)} == {"https://example.com/x", "mailto:a@example.com"}
    DF.delete_link(doc[0], fresh_links(doc, 0)[0]["xref"])
    assert len(fresh_links(doc, 0)) == 1


def test_attachments_and_labels(tmp_path):
    doc = five_pages()
    f = tmp_path / "notes.txt"
    f.write_text("hello")
    assert DF.add_attachment(doc, str(f)) == "notes.txt"
    assert DF.add_attachment(doc, str(f), "again") == "notes (2).txt"
    items = DF.list_attachments(doc)
    assert [i["name"] for i in items] == ["notes.txt", "notes (2).txt"]
    assert DF.attachment_bytes(doc, items[0]) == b"hello"
    DF.delete_attachment(doc, items[0])
    assert len(DF.list_attachments(doc)) == 1
    DF.set_labels(doc, [{"startpage": 0, "style": "r"}, {"startpage": 2, "style": "D", "prefix": "A-"},
                        {"startpage": 99, "style": "evil"}])
    assert [DF.page_label(doc, i) for i in range(5)] == ["i", "ii", "A-1", "A-2", "A-3"]


def test_background_and_grayscale():
    doc = five_pages()
    DF.add_background(doc, [1], color=(1, 1, 0.8))
    assert doc[1].get_pixmap().pixel(300, 400) == (255, 255, 204)
    assert "Page 2 text" in doc[1].get_text()  # the text stays on top
    gray = fitz.open("pdf", DF.grayscale_bytes(doc.tobytes()))
    r, g, b = gray[1].get_pixmap().pixel(300, 400)
    assert r == g == b


def test_resize_scales_content_and_comments():
    doc = five_pages()
    page = doc[0]
    a = page.add_rect_annot(fitz.Rect(72, 300, 172, 350))
    RT.add(page, fitz.Rect(72, 400, 200, 420), RT.from_plain("Box text", "Helvetica", 12))
    forms.add_field(page, fitz.Rect(72, 500, 272, 520), "text", "Name")
    DF.add_link(page, fitz.Rect(72, 90, 160, 104), {"page": 1})
    before = page.search_for("Page 1 text")[0]
    del page, a
    DF.resize_pages(doc, [0], 595, 842, "scale")
    page = doc[0]
    assert page.rect == fitz.Rect(0, 0, 595, 842)
    word = page.search_for("Page 1 text")[0]
    s = 595 / 612
    expected = before * fitz.Matrix(s, 0, 0, s, 0, (842 - 792 * s) / 2)
    assert abs(word.x0 - expected.x0) < 1 and abs(word.y1 - expected.y1) < 1
    rect_annot = [x for x in page.annots() if x.type[1] == "Square"][0]
    assert abs(rect_annot.rect.x0 - (72 * s - 1 * s)) < 3
    link = fresh_links(doc, 0)[0]
    assert fitz.Rect(link["from"]).intersects(word)  # the link still covers its text
    widget = next(doc[0].widgets())
    assert abs(widget.rect.width - 200 * s) < 1
    # margins mode keeps the content size
    DF.resize_pages(doc, [1], 842, 595, "margins")
    assert doc[1].rect == fitz.Rect(0, 0, 842, 595)
    assert abs(doc[1].search_for("Page 2 text")[0].x0 - (72 + (842 - 612) / 2)) < 1


def test_nup_and_booklet():
    data = five_pages().tobytes()
    nup = fitz.open("pdf", DF.nup_bytes(data, 4))
    assert nup.page_count == 2 and "Page 4 text" in nup[0].get_text()
    assert DF.booklet_order(5) == [(None, 0), (1, None), (None, 2), (3, 4)]
    booklet = fitz.open("pdf", DF.booklet_bytes(data))
    assert booklet.page_count == 4 and booklet[0].rect.width > booklet[0].rect.height


def test_blank_pages_and_images(tmp_path):
    doc = five_pages()
    assert DF.find_blank_pages(doc.tobytes()) == [2]
    pm = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 60, 40), 0)
    pm.set_rect(pm.irect, (200, 30, 30))
    doc[2].insert_image(fitz.Rect(72, 72, 172, 140), pixmap=pm)
    assert DF.find_blank_pages(doc.tobytes()) == []
    saved = DF.extract_images(doc.tobytes(), str(tmp_path), "../../evil")
    assert len(saved) == 1 and os.path.dirname(saved[0]) == str(tmp_path)
    assert fitz.Pixmap(saved[0]).width == 60


def test_comment_summary(tmp_path):
    doc = five_pages()
    page = doc[0]
    hl = page.add_highlight_annot(page.search_for("Page 1")[0])
    hl.set_info(content="=HYPERLINK(\"http://evil\")", title="Ana")
    hl.update()
    md = tmp_path / "c.md"
    csv_path = tmp_path / "c.csv"
    assert DF.export_comments(doc, str(md), "Report <b>") == 1
    text = md.read_text(encoding="utf-8")
    assert "Page 1" in text and "<b>" not in text and "Ana" in text
    DF.export_comments(doc, str(csv_path))
    assert "'=HYPERLINK" in csv_path.read_text(encoding="utf-8-sig")


def test_forms_detect_data_and_radio(tmp_path):
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Full name: ______________________", fontsize=11)
    page.insert_text((72, 140), "Email:", fontsize=11)
    page.draw_line((120, 142), (330, 142))
    page.draw_rect(fitz.Rect(72, 170, 84, 182))
    page.insert_text((90, 180), "I agree", fontsize=11)
    for r in range(3):  # a table must not turn into fields
        page.draw_line((72, 300 + r * 20), (372, 300 + r * 20))
    for c in range(4):
        page.draw_line((72 + c * 100, 300), (72 + c * 100, 340))
    found = forms.detect_fields(doc[0])
    assert [(k, n) for k, _r, n in found] == [("text", "Full_name"), ("text", "Email"), ("check", "I_agree")]
    forms.add_detected(doc[0], found)
    forms.add_radio_button(doc[0], fitz.Rect(72, 400, 86, 414), "Size", "Small")
    forms.add_radio_button(doc[0], fitz.Rect(120, 400, 134, 414), "Size", "Large")
    assert list(forms.radio_groups(doc)) == ["Size"]
    values = {"Full_name": "Ana ș", "Email": "=cmd|' /C calc'!A0", "I_agree": "Yes", "Size": "Large"}
    assert forms.apply_values(doc, values) == 4
    for ext in ("json", "csv", "xfdf"):
        path = str(tmp_path / f"data.{ext}")
        forms.export_data(doc, path)
        assert forms.read_data(path) == forms.collect_values(doc) == values
    evil = tmp_path / "evil.xfdf"
    evil.write_text('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><xfdf><fields><field name="n">'
                    '<value>&a;</value></field></fields></xfdf>')
    with pytest.raises(ValueError):
        forms.read_data(str(evil))
    forms.reset_form(doc)
    assert all(v in ("", "Off") for v in forms.collect_values(doc).values())
    x = next(w.xref for w in doc[0].widgets() if w.field_name == "Full_name")
    props = forms.properties(doc[0], x)
    props.update(name="Name <x>", required=True, multiline=True, align=1)
    forms.set_properties(doc[0], x, props)
    after = forms.properties(doc[0], x)
    assert after["name"] == "Name_x" and after["required"] and after["multiline"] and after["align"] == 1
    again = fitz.open("pdf", doc.tobytes())
    assert {w.field_name for w in again[0].widgets()} >= {"Name_x", "Email", "I_agree", "Size"}
    assert pdfops is not None


def test_compare_report_shows_comment_text():
    """Text in comments counts as page text, so the report pictures must show it too."""
    from pdfdesk import compare
    old = fitz.open()
    old.new_page().insert_text((72, 72), "Body text here", fontsize=12)
    new = fitz.open("pdf", old.tobytes())
    new[0].add_freetext_annot(fitz.Rect(72, 100, 300, 130), "Draft for the board", fontsize=12)
    data, info = compare.report_bytes(old.tobytes(), new.tobytes(), "old.pdf", "new.pdf")
    assert any("Draft for the board" in c["new_text"] for c in info["changes"])
    report = fitz.open("pdf", data)
    assert "Draft for the board" in report[report.page_count - 1].get_text()
