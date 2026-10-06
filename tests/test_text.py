"""Tests for fonts, formatted text boxes and editing page text. Run with:  python -m pytest -q tests"""
import json
import os
import sys
import tempfile

import pymupdf as fitz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("PDFDESK_HOME", tempfile.mkdtemp(prefix="pdfdesk-test-home-"))

from pdfdesk import appearance, fontcatalog, fonts, pdfops, textedit  # noqa: E402
from pdfdesk import richtext as RT  # noqa: E402


def ink(page, rect) -> int:
    pix = page.get_pixmap(clip=rect, alpha=False)
    s = pix.samples
    return sum(1 for i in range(0, len(s), 3) if s[i] < 160)


def sample_box(font=None) -> RT.Box:
    font = font or fontcatalog.catalog().default_family()
    return RT.Box(paras=[
        RT.Para([RT.Run("Hello ", font, 14), RT.Run("bold", font, 14, bold=True),
                 RT.Run(" red ș", "Noto Sans", 14, italic=True, color="#cc0000")]),
        RT.Para([RT.Run("Centered", "Helvetica", 11)], align="center"),
        RT.Para([RT.Run("Point one", font, 10)], list="bullet"),
    ], fill="#eef6ff", border="#2050a0", border_width=1)


def test_font_catalog_has_bundled_and_builtin_fonts():
    cat = fontcatalog.catalog()
    names = cat.names()
    for name in ("Noto Sans", "Helvetica", "Times", "Courier", "FiraGO"):
        assert cat.resolve(name) in names
    assert cat.resolve("No Such Font") == cat.default_family()
    face, real = cat.face("Noto Sans", True, False)
    assert real and face.source in ("bundled", "system")
    # subsetting keeps only the letters used, and the result is a font the PDF engine can load
    small = fontcatalog.subset_bytes(face, "Hi ș")
    assert len(small) < len(fontcatalog.face_bytes(face)) / 4
    assert fitz.Font(fontbuffer=small).has_glyph(ord("ș"))


def test_pdf_font_names_match_installed_fonts():
    cat = fontcatalog.catalog()
    fam, bold, italic = cat.match_pdf_font("ABCDEF+Helvetica-BoldOblique")
    assert fam == "Helvetica" and bold and italic
    assert cat.match_pdf_font("NimbusSans-Regular")[0] == "Helvetica"
    assert cat.match_pdf_font("TimesNewRomanPSMT")[0] is not None
    assert cat.match_pdf_font("Zzqx-Unknown")[0] is None


def test_rich_box_round_trip_and_rotation():
    doc = fitz.open()
    for rot in (0, 90, 180, 270):
        doc.new_page(width=400, height=500)
        doc[-1].set_rotation(rot)
    box = sample_box()
    for pno in range(doc.page_count):
        page = doc[pno]
        xref = RT.add(page, fitz.Rect(40, 50, 300, 80), box, "Tester")
        vis = RT.current_vis_rect(doc[pno], xref)
        assert abs(vis.x0 - 40) < 0.5 and abs(vis.y0 - 50) < 0.5, (pno, vis)
        assert ink(doc[pno], doc[pno].rect) > 200
    data = doc.tobytes(garbage=1, deflate=True)
    again = fitz.open("pdf", data)
    for page in again:
        annot = page.first_annot
        assert RT.is_rich(again, annot.xref)
        assert RT.read_box(again, annot.xref).to_dict() == box.to_dict()
        assert "Hello bold red ș" in annot.info["content"]
        assert annot.info["title"] == "Tester"
        # other PDF viewers see the text, and Acrobat gets rich text it can edit
        assert "Hello" in again.xref_get_key(annot.xref, "RC")[1]
    # text inside the boxes can be found by search
    assert again[0].search_for("Centered")


def test_moving_keeps_the_custom_look():
    doc = fitz.open()
    doc.new_page()
    page = doc[0]
    xref = RT.add(page, fitz.Rect(50, 50, 200, 80), sample_box())
    before = RT.current_vis_rect(page, xref)
    target = before + (100, 200, 100, 200)
    appearance.move_annot(page, xref, target)
    page = doc[0]
    assert ink(page, target) > 200 and ink(page, before) < 20
    # PyMuPDF setters make the engine redraw an annotation; the custom look must survive save + reload
    again = fitz.open("pdf", doc.tobytes())
    assert ink(again[0], target) > 200


def test_untrusted_box_data_is_checked():
    good = sample_box().to_dict()
    hostile = json.loads(json.dumps(good))
    hostile["p"][0]["r"][0].update({"c": "red;}</style><script>", "f": "x" * 500, "s": 1e9, "h": "url(x)"})
    hostile["p"][1]["a"] = "javascript"
    box = RT.Box.from_dict(hostile)
    run = box.paras[0].runs[0]
    assert run.color == "#000000" and len(run.font) <= 80 and run.size == RT.MAX_SIZE and run.highlight is None
    assert box.paras[1].align == "left"
    # text is escaped before it reaches the layout engine
    box.paras[0].runs[0].text = "<b>not bold</b> & <img src='file:///etc/passwd'>"
    html_text, css, _arch = RT.to_html(box)
    assert "<img" not in html_text and "&lt;img" in html_text
    for bad in ("not json", json.dumps({"p": "x"}), json.dumps({"p": [{"r": [{"t": 5}]}]})):
        try:
            RT.Box.from_json(bad)
            raise AssertionError("accepted bad data")
        except (ValueError, TypeError):
            pass
    doc = fitz.open()
    doc.new_page()
    page = doc[0]
    xref = RT.add(page, fitz.Rect(50, 50, 200, 80), sample_box())
    doc.xref_set_key(xref, RT.KEY, appearance.pdf_string("{broken"))
    assert RT.read_box(doc, xref) is None


def test_acrobat_rich_text_is_read():
    rc = ('<?xml version="1.0"?><body xmlns="http://www.w3.org/1999/xhtml"><p style="text-align:center">'
          '<span style="font-family:Helvetica;font-size:14pt;color:#ff0000;font-weight:bold">Big</span> small'
          '</p><p>second<br/>third</p></body>')
    box = RT.from_rc(rc, RT.Run("", "Helvetica", 10))
    assert [p.text() for p in box.paras][:2] == ["Big small", "second"]
    first = box.paras[0].runs[0]
    assert first.bold and first.size == 14 and first.color == "#ff0000" and box.paras[0].align == "center"


def test_legacy_text_box_converts():
    doc = fitz.open()
    doc.new_page()
    page = doc[0]
    a = page.add_freetext_annot(fitz.Rect(50, 50, 250, 90), "Old box\nsecond line", fontsize=13,
                                fontname="Helv", text_color=(0, 0, 1))
    box = RT.box_from_legacy(page, a)
    assert [p.text() for p in box.paras] == ["Old box", "second line"]
    assert box.first_run().size == 13 and box.first_run().color == "#0000ff"
    RT.update(page, a.xref, box, vis_rect=fitz.Rect(50, 50, 250, 90))
    assert RT.is_rich(doc, a.xref)


def test_edit_paragraph_keeps_position_and_font():
    para = ("PDF Desk edits paragraphs of text on the page. This block has several lines so that "
            "we can check the wrapping and the line spacing after an edit.")
    for rot in (0, 90):
        doc = fitz.open()
        doc.new_page(width=420, height=500)
        page = doc[0]
        page.set_rotation(rot)
        page = doc[0]
        d = page.derotation_matrix
        page.insert_textbox((fitz.Rect(50, 60, 330, 200) * d).normalize(), para, fontname="helv", fontsize=11,
                            rotate=rot)
        page = doc[0]
        td = page.get_text("dict", flags=pdfops.WORD_FLAGS)
        info = textedit.block_at(page, td, fitz.Point(60, 66) * d)
        assert info and info["editable"] and len(info["lines"]) >= 2
        box = textedit.block_to_box(page, info)
        assert box.first_run().font == "Helvetica"
        run = box.paras[0].runs[0]
        run.text = run.text.replace("several lines", "SEVERAL NEW lines ș")
        textedit.replace_block(page, info, box)
        page = doc[0]
        text = page.get_text()
        assert "SEVERAL NEW lines ș" in text.replace("\n", " ") and "several lines" not in text
        first = [w for w in page.get_text("words") if w[4] == "PDF"][0]
        vis = (fitz.Rect(first[:4]) * page.rotation_matrix).normalize()
        assert abs(vis.x0 - 50) < 1 and abs(vis.y0 - 60) < 3, vis


def test_single_line_edit_stays_on_its_baseline():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Invoice total: 120 USD", fontname="tiro", fontsize=12)
    page = doc[0]
    td = page.get_text("dict", flags=pdfops.WORD_FLAGS)
    info = textedit.block_at(page, td, fitz.Point(80, 96))
    box = textedit.block_to_box(page, info)
    box.paras[0].runs[0].text = "Invoice total: 125 USD"
    box.paras[0].runs[0].bold = True
    textedit.replace_block(page, info, box)
    page = doc[0]
    span = [s for b in page.get_text("dict")["blocks"] for ln in b["lines"] for s in ln["spans"]][0]
    assert span["text"] == "Invoice total: 125 USD" and abs(span["origin"][1] - 100) < 0.1
    assert "Bold" in span["font"]


def test_text_with_catalog_fonts():
    doc = fitz.open()
    doc.new_page()
    pdfops.add_text_watermark(doc, [0], "DRAFT ș", family="Noto Sans")
    pdfops.add_header_footer(doc, [0], {"footer_center": "Page {page}"}, family="Times")
    page = doc[0]
    assert "DRAFT ș" in page.get_text() and "Page 1" in page.get_text()
    assert abs(fonts.text_width("Hello", 10, "Helvetica") - fitz.get_text_length("Hello", "helv", 10)) < 0.01


def test_custom_opacity():
    doc = fitz.open()
    doc.new_page()
    page = doc[0]
    xref = appearance.new_annot(page, "Stamp", fitz.Rect(50, 50, 150, 150), "Image")
    tmp = fitz.open()
    tp = tmp.new_page(width=100, height=100)
    tp.draw_rect(tp.rect, color=None, fill=(0, 0, 0))
    appearance.apply(doc, xref, tmp, 0)
    page = doc[0]
    assert page.get_pixmap().pixel(100, 100) == (0, 0, 0)
    appearance.set_opacity(page, xref, 0.5)
    appearance.set_opacity(page, xref, 0.5)  # twice must not stack
    page = doc[0]
    value = page.get_pixmap().pixel(100, 100)[0]
    assert 120 <= value <= 135 and appearance.opacity_of(doc, xref) == 0.5
