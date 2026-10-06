"""Regression tests for problems found in the security reviews. Run with:  python -m pytest -q tests"""
import gc
import os
import sys
import tempfile
import time

import pymupdf as fitz

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("PDFDESK_HOME", tempfile.mkdtemp(prefix="pdfdesk-test-home-"))

from pdfdesk import annots, forms, pdfops  # noqa: E402
from pdfdesk import docfeatures as DF  # noqa: E402


def test_link_address_cannot_add_an_action():
    doc = fitz.open()
    page = doc.new_page()
    url = "https://example.com/a)/S/Launch/F(calc.exe)>>"
    DF.add_link(page, fitz.Rect(72, 72, 200, 90), {"uri": url})
    page = None
    gc.collect()  # the PDF engine keeps a page's links until the page object is gone
    links = doc[0].get_links()
    assert len(links) == 1 and links[0]["kind"] == fitz.LINK_URI
    xref = links[0]["xref"]
    assert doc.xref_get_key(xref, "A/S") == ("name", "/URI")
    assert doc.xref_get_key(xref, "A/F")[0] == "null"    # no file to launch was added
    assert links[0]["uri"].startswith("https://example.com/a)")


def test_moving_a_radio_button_keeps_the_choice():
    doc = fitz.open()
    page = doc.new_page()
    xrefs = forms.add_radio_group(page, [fitz.Rect(72, 72, 90, 90), fitz.Rect(72, 100, 90, 118)], "Pick")
    first = next(w for w in page.widgets() if w.xref == xrefs[0])
    annots.select_radio(page, first)

    def states():
        return [doc.xref_get_key(x, "AS")[1] for x in xrefs]
    before = states()
    assert before[0] != "/Off" and before[1] == "/Off"
    forms.move_field(page, xrefs[1], fitz.Rect(200, 100, 218, 118))
    forms.move_field(page, xrefs[0], fitz.Rect(200, 72, 218, 90))
    assert states() == before


def test_text_edits_keep_your_own_redaction_marks():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Edit this line", fontsize=12)
    page.insert_text((72, 300), "Secret to redact later", fontsize=12)
    page.add_redact_annot(fitz.Rect(70, 285, 300, 305))   # marked, not applied yet
    page = pdfops.remove_text(page, [fitz.Rect(70, 88, 200, 104)])
    text = page.get_text()
    assert "Edit this line" not in text and "Secret to redact later" in text
    assert len(list(page.annots(types=[fitz.PDF_ANNOT_REDACT]))) == 1


def test_huge_page_label_numbers_are_harmless():
    doc = fitz.open()
    for _ in range(3):
        doc.new_page()
    doc.xref_set_key(doc.pdf_catalog(), "PageLabels", "<</Nums[0<</S/R/St 1000000000000>>]>>")
    t = time.time()
    labels = DF.all_labels(doc)
    assert time.time() - t < 1 and len(labels) == 3 and all(len(lab) <= 60 for lab in labels)
    assert DF.get_labels(doc)[0]["firstpagenum"] <= DF.MAX_LABEL_NUMBER


def test_field_detection_has_a_budget():
    doc = fitz.open()
    page = doc.new_page()
    shape = page.new_shape()
    for k in range(3000):
        x, y = 20 + (k % 60) * 9, 20 + (k // 60) * 15
        shape.draw_rect(fitz.Rect(x, y, x + 7, y + 7))
    shape.finish(color=(0, 0, 0))
    shape.commit()
    t = time.time()
    forms.detect_fields(page)
    assert time.time() - t < 5


def test_filling_a_field_keeps_its_other_settings():
    doc = fitz.open()
    page = doc.new_page()
    w = fitz.Widget()
    w.field_type, w.field_name, w.rect = fitz.PDF_WIDGET_TYPE_TEXT, "amount", fitz.Rect(72, 72, 300, 100)
    page.add_widget(w)
    w = next(iter(page.widgets()))
    keep = {k: doc.xref_get_key(w.xref, k) for k in doc.xref_get_keys(w.xref) if k not in ("V", "AP", "AS")}
    annots.set_text_field(doc, page, w, "1000 ș")
    after = {k: doc.xref_get_key(w.xref, k) for k in doc.xref_get_keys(w.xref) if k not in ("V", "AP", "AS")}
    assert after == keep and doc.xref_get_key(w.xref, "V")[1] == "1000 ș"
    assert "PDUni" not in doc.xref_object(page.xref) and doc.xref_get_key(page.xref, "Resources")[0] in ("null", "dict", "xref")
