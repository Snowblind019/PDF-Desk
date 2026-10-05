"""End-to-end GUI test. Runs headless (no window appears):  python -m pytest -q tests/test_gui.py

Set PDFDESK_SHOTS=/some/folder to also save screenshots of each step."""
import os
import sys
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ["PDFDESK_HOME"] = tempfile.mkdtemp(prefix="pdfdesk-gui-home-")

import pymupdf as fitz  # noqa: E402

pytest.importorskip("PySide6")
from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication, QFileDialog, QInputDialog, QMenu, QMessageBox  # noqa: E402

SHOTS = os.environ.get("PDFDESK_SHOTS")
FLAGS = fitz.TEXT_DEHYPHENATE | fitz.TEXT_PRESERVE_WHITESPACE


def make_sample(path: str) -> None:
    from pdfdesk import convert
    html = ("<h1>Network Maintenance Report</h1><p>This report covers the scheduled maintenance window for "
            "the regional transport ring. Work included optics replacement, firmware updates on the aggregation "
            "routers, and verification of MPLS label switched paths.</p><h2>Summary</h2><p>All circuits were "
            "restored. Contact noc@example.com or call 509-555-0142 for questions.</p>"
            + "".join(f"<p>Step {i}: verified counters and neighbors.</p>" for i in range(1, 25)))
    doc = fitz.open("pdf", convert.html_to_pdf_bytes(html))
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 80), "Change Request Form", fontsize=20)
    y = 120
    for label, kind in (("Requester name", fitz.PDF_WIDGET_TYPE_TEXT), ("Approved", fitz.PDF_WIDGET_TYPE_CHECKBOX),
                        ("Priority", fitz.PDF_WIDGET_TYPE_COMBOBOX)):
        page.insert_text((72, y + 14), label + ":", fontsize=11)
        w = fitz.Widget()
        w.field_type = kind
        w.field_name = label.replace(" ", "_")
        w.rect = fitz.Rect(200, y, 200 + (18 if kind == fitz.PDF_WIDGET_TYPE_CHECKBOX else 250), y + 20)
        w.border_color = (0.4, 0.4, 0.4)
        if kind == fitz.PDF_WIDGET_TYPE_COMBOBOX:
            w.choice_values = ["Low", "Medium", "High"]
            w.field_value = "Low"
        page.add_widget(w)
        y += 40
    page.insert_link({"kind": fitz.LINK_GOTO, "from": fitz.Rect(72, 300, 260, 320), "page": 0})
    page.insert_text((72, 315), "Back to the first page", fontsize=11, color=(0, 0, 1))
    doc.save(path)


@pytest.fixture(scope="module")
def app():
    a = QApplication.instance() or QApplication(sys.argv)
    from pdfdesk import theme
    theme.apply_theme(a, "light")
    yield a


class Driver:
    def __init__(self, app, window, folder):
        self.app, self.w, self.folder = app, window, folder

    @property
    def tab(self):
        return self.w.tab()

    @property
    def c(self):
        return self.tab.canvas

    @property
    def pdf(self):
        return self.tab.pdf

    def pump(self, n=10):
        for _ in range(n):
            self.app.processEvents()
            QTest.qWait(3)

    def shot(self, name):
        if SHOTS:
            os.makedirs(SHOTS, exist_ok=True)
            self.w.grab().save(os.path.join(SHOTS, f"{name}.png"))

    def pos(self, pno, x, y):
        p = self.c.vis_to_canvas(pno, fitz.Point(x, y))
        return QPoint(int(p.x()), int(p.y()))

    def drag(self, p1, p2, steps=6):
        QTest.mousePress(self.c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p1)
        for k in range(1, steps + 1):
            QTest.mouseMove(self.c, QPoint(p1.x() + (p2.x() - p1.x()) * k // steps,
                                           p1.y() + (p2.y() - p1.y()) * k // steps))
        QTest.mouseRelease(self.c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p2)
        self.pump(3)

    def click(self, p):
        QTest.mouseClick(self.c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, p)
        self.pump(3)

    def word(self, pno, text, n=0):
        hits = self.pdf.page(pno).search_for(text, flags=FLAGS)
        return hits[n]

    def drag_words(self, pno, first, last):
        a, b = self.word(pno, first), self.word(pno, last)
        self.drag(self.pos(pno, a.x0 + 1, (a.y0 + a.y1) / 2), self.pos(pno, b.x1 - 1, (b.y0 + b.y1) / 2))

    def types(self, pno=0):
        page = self.pdf.page(pno)
        return [a.type[1] for a in page.annots()]

    def find(self, pno, typ, pred=None):
        page = self.pdf.page(pno)
        for a in page.annots():
            if a.type[1] == typ and (pred is None or pred(a)):
                return page, a
        return page, None


@pytest.fixture()
def driver(app, tmp_path, monkeypatch):
    from pdfdesk import canvas as canvas_mod
    from pdfdesk.mainwindow import MainWindow

    img = QImage(300, 120, QImage.Format.Format_ARGB32)
    img.fill(QColor("#2f80ed"))
    img_path = str(tmp_path / "logo.png")
    img.save(img_path)
    monkeypatch.setattr(QInputDialog, "getMultiLineText", staticmethod(lambda *a, **k: ("A comment", True)))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (img_path, "")))
    from pdfdesk import ui
    questions, infos = [], []
    monkeypatch.setattr(ui, "question", lambda *a, **k: (questions.append(a), QMessageBox.StandardButton.Yes)[1])
    monkeypatch.setattr(ui, "information", lambda *a, **k: infos.append(a))
    warnings = []
    monkeypatch.setattr(ui, "warning", lambda *a, **k: warnings.append(a))

    class FakeMenu(QMenu):
        def exec(self, *a, **k):
            acts = [x for x in self.actions() if x.text()]
            if len(acts) > 1:
                acts[1].trigger()

    monkeypatch.setattr(canvas_mod, "QMenu", FakeMenu)
    sample = str(tmp_path / "sample.pdf")
    make_sample(sample)
    w = MainWindow()
    w.resize(1400, 900)
    w.show()
    d = Driver(app, w, tmp_path)
    d.pump()
    w.open_path(sample)
    d.pump(30)
    d.warnings = warnings
    d.questions = questions
    d.infos = infos
    yield d
    for t in w.doc_tabs():
        t.pdf.saved_id = t.pdf.state_id  # skip the "save changes?" prompt
    w.close()
    d.pump()


def test_full_editing_session(driver):
    from pdfdesk import tools as T
    d, w = driver, driver.w
    assert w.tab() is not None and d.pdf.page_count == 3

    # select + copy
    w.set_tool(T.SELECT)
    d.drag_words(0, "covers", "scheduled")
    assert d.c.selected_text() == "covers the scheduled"
    w.copy()
    assert QApplication.clipboard().text() == "covers the scheduled"
    d.shot("01_selection")

    # text markup
    for tool, a, b, typ in ((T.HIGHLIGHT, "optics", "replacement", "Highlight"),
                            (T.UNDERLINE, "firmware", "updates", "Underline"),
                            (T.STRIKEOUT, "verification", "MPLS", "StrikeOut")):
        w.set_tool(tool)
        d.drag_words(0, a, b)
        assert typ in d.types()

    # note, text box, drawing, shapes, stamps
    w.set_tool(T.NOTE)
    d.click(d.pos(0, 540, 120))
    assert "Text" in d.types()
    w.set_tool(T.TEXTBOX)
    d.click(d.pos(0, 80, 680))
    d.c.editor.setPlainText("Typed note ș ț")
    d.c.editor.commit()
    d.pump()
    assert "Typed note ș ț" in d.pdf.page(0).get_text()
    w.set_tool(T.PEN)
    d.drag(d.pos(0, 100, 740), d.pos(0, 200, 760), steps=12)
    assert "Ink" in d.types()
    for tool, typ, x, y in ((T.RECT, "Square", 380, 600), (T.ELLIPSE, "Circle", 470, 600),
                            (T.LINE, "Line", 380, 650), (T.ARROW, "Line", 470, 680)):
        w.set_tool(tool)
        d.drag(d.pos(0, x, y), d.pos(0, x + 70, y + 30))
        assert typ in d.types()
    w.opt.set_stamp("Approved")
    w.set_tool(T.STAMP)
    d.click(d.pos(0, 450, 80))
    assert "Stamp" in d.types()
    d.shot("02_annotations")

    # image and signature on page 2
    before = len(d.pdf.page(1).get_images())
    d.c.go_to_page(1)
    w.set_tool(T.IMAGE)
    d.click(d.pos(1, 300, 300))
    assert len(d.pdf.page(1).get_images()) == before + 1
    from pdfdesk.signature import SignaturePad
    pad = SignaturePad()
    pad.resize(520, 200)
    pad.strokes = [[QPointF(30, 120), QPointF(80, 60), QPointF(140, 130), QPointF(220, 70)]]
    sig = str(d.folder / "sig.png")
    pad.to_image().save(sig)
    w.opt.set_signature(sig)
    w.set_tool(T.SIGNATURE)
    d.click(d.pos(1, 300, 450))
    assert len(d.pdf.page(1).get_images()) == before + 2

    # redaction
    d.c.go_to_page(0)
    w.set_tool(T.REDACT)
    d.drag_words(0, "509-555-0142", "509-555-0142")
    assert "Redact" in d.types()
    w.apply_redactions()
    assert "509-555-0142" not in d.pdf.page(0).get_text()

    # edit a line of text
    w.set_tool(T.EDIT_TEXT)
    r = d.word(0, "Summary")
    d.click(d.pos(0, r.x0 + 3, (r.y0 + r.y1) / 2))
    assert d.c.editor is not None and d.c.editor.toPlainText() == "Summary"
    d.c.editor.setPlainText("Overview și rezumat")
    d.c.editor.commit()
    d.pump()
    text = d.pdf.page(0).get_text()
    assert "Overview și rezumat" in text and "Summary" not in text

    # move, resize, restyle, delete, undo, redo
    w.set_tool(T.SELECT)
    _p, sq = d.find(0, "Square")
    r0 = fitz.Rect(sq.rect)
    start = d.pos(0, r0.x0 + 0.5, (r0.y0 + r0.y1) / 2)
    d.drag(start, QPoint(start.x() + 40, start.y() + 20))
    _p, sq = d.find(0, "Square")
    assert sq.rect.x0 > r0.x0 + 10
    cr = d.c.vis_rect_to_canvas(0, d.c.sel_annot["vis"]).adjusted(-2, -2, 2, 2)
    corner = QPoint(int(cr.right()), int(cr.bottom()))
    r1 = fitz.Rect(sq.rect)
    d.drag(corner, QPoint(corner.x() + 50, corner.y() + 30))
    _p, sq = d.find(0, "Square")
    assert sq.rect.width > r1.width + 20
    w._color_picked("#2a9d8f")
    _p, sq = d.find(0, "Square")
    assert abs(sq.colors["stroke"][1] - 0x9d / 255) < 0.02
    n = len(d.types())
    QTest.keyClick(d.c, Qt.Key.Key_Delete)
    d.pump()
    assert len(d.types()) == n - 1
    w.undo()
    d.pump()
    assert len(d.types()) == n
    w.redo()
    d.pump()
    assert len(d.types()) == n - 1

    # forms and links on the last page
    last = d.pdf.page_count - 1
    d.c.go_to_page(last)
    page = d.pdf.page(last)
    widgets = {x.field_name: fitz.Rect(x.rect) for x in page.widgets()}
    d.click(d.pos(last, widgets["Requester_name"].x0 + 5, widgets["Requester_name"].y0 + 5))
    d.c.editor.setPlainText("Emil Popov ș")
    d.c.editor.commit()
    d.pump()
    d.click(d.pos(last, widgets["Approved"].x0 + 5, widgets["Approved"].y0 + 5))
    d.click(d.pos(last, widgets["Priority"].x0 + 5, widgets["Priority"].y0 + 5))
    page = d.pdf.page(last)
    values = {x.field_name: x.field_value for x in page.widgets()}
    assert values["Requester_name"] == "Emil Popov ș"
    assert values["Approved"] not in ("Off", "", False)
    assert values["Priority"] == "Medium"
    d.shot("03_forms")
    d.click(d.pos(last, 100, 310))
    assert d.c.current_page == 0

    # search
    d.tab.open_find()
    d.tab.findbar.edit.setText("circuits")
    d.tab.findbar._emit()
    for _ in range(20):
        d.pump(2)
    assert len(d.tab.hit_order) == 1

    # pages
    n = d.pdf.page_count
    d.tab.page_action("rotate_cw", [0])
    assert d.pdf.page(0).rotation == 90
    d.tab.page_action("duplicate", [0])
    assert d.pdf.page_count == n + 1
    d.tab.page_action("delete", [0])
    assert d.pdf.page_count == n
    d.tab.reorder([n - 1], 0)
    assert "Change Request Form" in d.pdf.page(0).get_text()
    w.act["organize"].setChecked(True)
    d.pump(10)
    d.shot("04_organizer")
    w.act["organize"].setChecked(False)

    # rotated page: shapes land where they are drawn
    d.tab.page_action("rotate_cw", [1])
    d.c.go_to_page(1)
    w.set_tool(T.RECT)
    d.drag(d.pos(1, 100, 100), d.pos(1, 200, 160))
    page = d.pdf.page(1)
    vis = d.c.page_rect_to_vis(1, list(page.annots())[-1].rect)
    assert abs(vis.x0 - 100) < 3 and abs(vis.y0 - 100) < 3

    # view options
    d.c.set_zoom(2.0)
    assert abs(d.c.zoom - 2.0) < 0.01
    w.act["layout_facing"].setChecked(True)
    w.act["night"].setChecked(True)
    d.pump(5)
    d.shot("05_night_facing")
    w.act["night"].setChecked(False)
    w.act["layout_single"].setChecked(True)

    # save and reload
    out = str(d.folder / "saved.pdf")
    d.pdf.save(out)
    saved = fitz.open(out)
    kinds = {a.type[1] for p in saved for a in p.annots()}
    assert {"Highlight", "Underline", "StrikeOut", "Text", "FreeText", "Ink", "Circle", "Line", "Stamp"} <= kinds
    assert any(x.field_value == "Emil Popov ș" for p in saved for x in p.widgets())
    assert not d.pdf.dirty
    assert not d.warnings


def test_dialogs_open(driver):
    from pdfdesk import dialogs
    from pdfdesk.config import settings
    from pdfdesk.signature import SignatureDialog
    pdf = driver.pdf
    w = driver.w
    for dlg in (dialogs.ExportDialog(w, pdf, 0, "xlsx"), dialogs.SplitDialog(w, pdf), dialogs.CombineDialog(w),
                dialogs.BlankDialog(w), dialogs.CompressDialog(w, pdf), dialogs.ProtectDialog(w),
                dialogs.WatermarkDialog(w, pdf, 0), dialogs.HeaderFooterDialog(w, pdf, 0),
                dialogs.CropDialog(w, pdf, 0), dialogs.OcrDialog(w, pdf, 0, settings()),
                dialogs.FindRedactDialog(w, pdf, 0), dialogs.PropertiesDialog(w, pdf, 0),
                dialogs.SettingsDialog(w, settings()), dialogs.ShortcutsDialog(w), SignatureDialog(w)):
        dlg.show()
        driver.pump(3)
        dlg.close()


def test_open_converted_file(driver):
    import time
    md = driver.folder / "notes.md"
    md.write_text("# Notes\n\n- one\n- two ș\n", encoding="utf-8")
    n = driver.w.tabs.count()
    driver.w.open_path(str(md))
    t0 = time.time()
    while driver.w.tabs.count() == n and time.time() - t0 < 30:
        driver.pump(1)
    assert driver.w.tabs.count() == n + 1
    assert driver.w.tab().pdf.is_new and "two ș" in driver.w.tab().pdf.page(0).get_text()


def test_untrusted_links_are_blocked(driver, monkeypatch):
    """Links inside a PDF must never open programs, network shares or local files without asking."""
    from PySide6.QtGui import QDesktopServices
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", staticmethod(lambda url: opened.append(url.toString())))
    c = driver.c
    requested = []
    c.open_file_requested.connect(requested.append)
    other = driver.folder / "other.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(str(other))
    cases = [
        ({"kind": fitz.LINK_URI, "uri": "FILE:///C:/Windows/System32/calc.exe"}, "blocked"),
        ({"kind": fitz.LINK_URI, "uri": "smb://attacker/share"}, "blocked"),
        ({"kind": fitz.LINK_URI, "uri": "ms-msdt:/id PCWDiagnostic"}, "blocked"),
        ({"kind": fitz.LINK_URI, "uri": "https://bank.example@evil.example/"}, "blocked"),
        ({"kind": fitz.LINK_URI, "uri": "https://example.com/\u202egpj.exe"}, "blocked"),
        ({"kind": fitz.LINK_GOTOR, "file": "//attacker/share/x.pdf"}, "blocked"),
        ({"kind": fitz.LINK_GOTOR, "file": "\\\\attacker\\share\\x.pdf"}, "blocked"),
        ({"kind": fitz.LINK_GOTOR, "file": "report.docx"}, "blocked"),
        ({"kind": fitz.LINK_LAUNCH, "file": "calc.exe"}, "blocked"),
        ({"kind": fitz.LINK_URI, "uri": "https://example.com/page"}, "asked"),
        ({"kind": fitz.LINK_URI, "uri": "mailto:noc@example.com?subject=Hi&attach=/etc/passwd"}, "asked"),
        ({"kind": fitz.LINK_GOTOR, "file": str(other)}, "asked"),
    ]
    for link, expected in cases:
        driver.questions.clear()
        before = (len(opened), len(requested))
        c._follow_link(link)
        if expected == "blocked":
            assert not driver.questions and (len(opened), len(requested)) == before, link
        else:
            assert driver.questions, link
    assert opened == ["https://example.com/page", "mailto:noc@example.com?subject=Hi"]
    assert requested == [str(other)]


def test_pdf_text_is_never_rich_text(driver):
    """A bookmark title or metadata with HTML must be shown literally, never rendered."""
    from PySide6.QtCore import Qt
    from pdfdesk import dialogs, ui
    evil = "<img src='file:///dev/zero'>"
    assert ui.tip(evil).startswith("<qt>") and "<img" not in ui.tip(evil)
    pdf = driver.pdf
    with pdf.edit("meta", structure=True):
        pdf.doc.set_metadata({"title": "t", "creator": evil, "producer": evil})
        pdf.doc.set_toc([[1, evil, 1]])
    dlg = dialogs.PropertiesDialog(driver.w, pdf, 0)
    from PySide6.QtWidgets import QLabel
    labels = [lab for lab in dlg.findChildren(QLabel) if evil in lab.text()]
    assert labels and all(lab.textFormat() == Qt.TextFormat.PlainText for lab in labels)
    tree = driver.tab.bookmarks.tree
    assert "<img" not in tree.topLevelItem(0).toolTip(0)
