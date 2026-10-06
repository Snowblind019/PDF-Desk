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
        import time
        for _ in range(n):
            self.app.processEvents()
            QTest.qWait(3)
            time.sleep(0.002)  # let background jobs run (qWait may keep Python's lock)

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

    # image and signature on page 2: movable items until flattened
    from pdfdesk import appearance

    def kinds(pno):
        return [appearance.kind_of(d.pdf.doc, a.xref) for a in d.pdf.page(pno).annots()]

    before = len(d.pdf.page(1).get_images())
    d.c.go_to_page(1)
    w.set_tool(T.IMAGE)
    d.click(d.pos(1, 300, 300))
    assert kinds(1).count("Image") == 1
    from pdfdesk.signature import SignaturePad
    pad = SignaturePad()
    pad.resize(520, 200)
    pad.strokes = [[QPointF(30, 120), QPointF(80, 60), QPointF(140, 130), QPointF(220, 70)]]
    sig = str(d.folder / "sig.png")
    pad.to_image().save(sig)
    w.opt.set_signature(sig)
    w.set_tool(T.SIGNATURE)
    d.click(d.pos(1, 300, 450))
    assert kinds(1).count("Signature") == 1
    # flatten the signature: it becomes part of the page
    page = d.pdf.page(1)
    sig_xref = next(a.xref for a in page.annots() if appearance.kind_of(d.pdf.doc, a.xref) == "Signature")
    w.set_tool(T.SELECT)
    d.c.select_annot(1, sig_xref)
    d.c.flatten_selected_annot()
    assert "Signature" not in kinds(1) and len(d.pdf.page(1).get_images()) == before + 1
    # Fill & Sign marks, date and initials
    for mode, x in (("check", 100), ("cross", 140), ("dot", 180), ("date", 220), ("initials", 320)):
        w._choose_fillsign(mode)
        d.click(d.pos(1, x, 600))
    k = kinds(1)
    assert {"Check", "Cross", "Dot"} <= set(k) and k.count("RichText") == 2
    import datetime as _dt
    assert _dt.date.today().strftime("%m/%d/%Y") in d.pdf.page(1).get_text()
    # the text formatting bar comes with the Fill & Sign text tool, not with the first click
    w._choose_fillsign("text")
    assert not w.format_bar.isHidden()
    w._choose_fillsign("check")
    assert w.format_bar.isHidden()

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
    grid = d.tab.organizer.grid
    assert grid.viewport().acceptDrops()  # drag-to-reorder drops land on the viewport
    r1 = grid.visualItemRect(grid.item(1))
    assert grid._drop_spot(QPoint(r1.left() + 3, r1.center().y()))[0] == 1
    assert grid._drop_spot(QPoint(r1.right() - 3, r1.center().y()))[0] == 2
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
                dialogs.SettingsDialog(w, settings()), dialogs.ShortcutsDialog(w), SignatureDialog(w),
                dialogs.LinkDialog(w, pdf.page_count, {"uri": "https://example.com"}, can_delete=True),
                dialogs.PageLabelsDialog(w, pdf.page_count, [{"startpage": 0, "style": "r", "firstpagenum": 1}]),
                dialogs.BackgroundDialog(w, pdf, 0), dialogs.PageSizeDialog(w, pdf, 0),
                dialogs.PrintLayoutDialog(w, True), dialogs.RadioButtonDialog(w, ["Size"], "Size", "Small"),
                dialogs.FieldPropertiesDialog(w, {"kind": "combo", "name": "x", "choices": ["a", "b"]}),
                dialogs.DetectedFieldsDialog(w, [(0, "text", fitz.Rect(0, 0, 10, 10), "Name")])):
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


def test_word_style_text_boxes(driver):
    """Type a formatted text box, restyle it from the toolbar, move/resize it, edit page text."""
    from pdfdesk import appearance
    from pdfdesk import richtext as RT
    from pdfdesk import tools as T
    from pdfdesk.textformat import RichEditor
    d, w = driver, driver.w
    bar = w.format_bar
    w.set_tool(T.TEXTBOX)
    d.pump()
    assert bar.isVisible()
    bar.changed.emit({"font": "Noto Sans"})
    bar.changed.emit({"size": 16})
    d.click(d.pos(0, 80, 640))
    ed = d.c.editor
    assert isinstance(ed, RichEditor) and ed.state()["font"] == "Noto Sans" and ed.state()["size"] == 16
    QTest.keyClicks(ed, "Plain ")
    QTest.keyClick(ed, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier)
    QTest.keyClicks(ed, "bold")
    QTest.keyClick(ed, Qt.Key.Key_B, Qt.KeyboardModifier.ControlModifier)
    # picking from the toolbar must not close the editor
    bar.font_box.setFocus()
    d.pump()
    assert d.c.editor is ed
    bar.changed.emit({"font": "Helvetica"})
    bar.changed.emit({"color": "#c00000"})
    QTest.keyClicks(ed, " red")
    QTest.keyClick(ed, Qt.Key.Key_Return)
    bar.changed.emit({"align": "center"})
    QTest.keyClicks(ed, "middle")
    ed.commit()
    d.pump()
    page = d.pdf.page(0)
    rich = [a for a in page.annots() if RT.is_rich(d.pdf.doc, a.xref)]
    assert len(rich) == 1
    xref = rich[0].xref
    box = RT.read_box(d.pdf.doc, xref)
    runs = [(r.text, r.font, r.bold, r.color) for r in box.runs()]
    assert runs[0][:3] == ("Plain ", "Noto Sans", False) and runs[1][:3] == ("bold", "Noto Sans", True)
    assert ("Helvetica", "#c00000") in {(r[1], r[3]) for r in runs}
    assert [p.align for p in box.paras] == ["left", "center"]
    assert "Plain bold red" in d.pdf.page(0).get_text()

    # select it and change the whole box from the toolbar
    w.set_tool(T.SELECT)
    d.c.select_annot(0, xref)
    d.pump()
    assert bar.isVisible() and d.c.sel_annot["rich"]
    bar.changed.emit({"size": 20})
    assert {r.size for r in RT.read_box(d.pdf.doc, xref).runs()} == {20}
    w._fill_toggled(True)
    assert RT.read_box(d.pdf.doc, xref).fill
    # move and resize: the box keeps its own look (the engine never redraws it)
    vis = d.c.sel_annot["vis"]
    d.c._apply_transform(0, xref, vis + (20, 30, 20, 30), "Move")
    assert appearance.kind_of(d.pdf.doc, xref) == RT.KIND
    assert abs(d.c.sel_annot["vis"].x0 - vis.x0 - 20) < 0.5
    v = d.c.sel_annot["vis"]
    d.c._apply_transform(0, xref, fitz.Rect(v.x0, v.y0, v.x0 + 90, v.y1), "Resize")
    assert abs(d.c.sel_annot["vis"].width - 90) < 0.5 and not RT.read_box(d.pdf.doc, xref).auto_width
    w.undo()
    d.pump()
    assert RT.read_box(d.pdf.doc, xref).auto_width

    # double-click edits it again with its formatting
    d.c._edit_freetext(0, xref)
    assert isinstance(d.c.editor, RichEditor) and "middle" in d.c.editor.toPlainText()
    QTest.keyClick(d.c.editor, Qt.Key.Key_Escape)
    d.pump()
    assert d.c.editor is None

    # edit text that is part of the page, with a different font
    w.set_tool(T.EDIT_TEXT)
    d.pump()
    r = d.word(0, "regional")
    d.click(d.pos(0, r.x0 + 2, (r.y0 + r.y1) / 2))
    ed = d.c.editor
    assert isinstance(ed, RichEditor) and "regional transport" in ed.toPlainText()
    bar.changed.emit({"font": "Times"})  # nothing selected: applies to the word at the cursor
    cur = ed.textCursor()
    cur.movePosition(cur.MoveOperation.End)
    ed.setTextCursor(cur)
    ed.insertPlainText(" Extra words ș.")  # QTest can only type ASCII
    ed.commit()
    d.pump()
    text = d.pdf.page(0).get_text().replace("\n", " ")
    assert "Extra words ș." in text and "regional" in text
    assert not d.warnings


def test_prepare_form(driver, monkeypatch):
    """Create fields by hand and automatically, move them, set options, and move data in and out."""
    from pdfdesk import dialogs, forms
    from pdfdesk import tools as T
    d, w = driver, driver.w
    pdf = d.pdf
    with pdf.edit("setup", structure=True):
        page = pdf.doc.new_page(width=612, height=792)
        page.insert_text((72, 100), "Full name: ______________________", fontsize=11)
        page.draw_rect(fitz.Rect(72, 170, 84, 182))
        page.insert_text((90, 180), "I agree", fontsize=11)
    pno = pdf.page_count - 1
    d.c.go_to_page(pno)
    w.act["prepare_form"].setChecked(True)
    d.pump()
    assert w.form_bar.isVisible() and d.c.form_edit

    # automatic detection
    monkeypatch.setattr(dialogs.DetectedFieldsDialog, "exec", lambda self: True)
    w.detect_fields()
    names = {x.field_name for x in pdf.page(pno).widgets()}
    assert {"Full_name", "I_agree"} <= names

    # drawing fields by hand
    w.set_tool(T.FIELD_TEXT)
    d.drag(d.pos(pno, 72, 300), d.pos(pno, 260, 320))
    monkeypatch.setattr(dialogs.RadioButtonDialog, "exec", lambda self: True)
    w.set_tool(T.FIELD_RADIO)
    d.click(d.pos(pno, 80, 400))
    d.click(d.pos(pno, 140, 400))
    w.set_tool(T.FIELD_COMBO)
    d.click(d.pos(pno, 300, 400))
    kinds = [forms.kind_of(x.field_type) for x in pdf.page(pno).widgets()]
    assert kinds.count("text") == 2 and kinds.count("radio") == 2 and kinds.count("combo") == 1
    assert len(forms.radio_groups(pdf.doc)) == 1

    # select, move and change options of the drawn text field
    w.set_tool(T.SELECT)
    field = next(x for x in pdf.page(pno).widgets() if x.field_name == "Text1")
    r0 = fitz.Rect(field.rect)
    start = d.pos(pno, r0.x0 + 5, r0.y0 + 5)
    d.drag(start, QPoint(start.x() + 40, start.y() + 30))
    moved = next(x for x in pdf.page(pno).widgets() if x.field_name == "Text1")
    assert moved.rect.x0 > r0.x0 + 10 and d.c.sel_annot.get("widget")

    def fake_props(self):
        self.name.setText("Phone")
        self.required.setChecked(True)
        return True
    monkeypatch.setattr(dialogs.FieldPropertiesDialog, "exec", fake_props)
    d.c.field_properties()
    props = forms.properties(pdf.page(pno), d.c.sel_annot["xref"])
    assert props["name"] == "Phone" and props["required"]

    # data out and back in
    w.act["prepare_form"].setChecked(False)
    assert not d.c.form_edit
    with pdf.edit("fill"):
        forms.apply_values(pdf.doc, {"Full_name": "Ana Pop ș", "Phone": "555", "I_agree": "Yes"})
    out = str(d.folder / "data.csv")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (out, "CSV")))
    w.export_form_data()
    assert "Ana Pop ș" in open(out, encoding="utf-8-sig").read()
    w.clear_form()
    assert forms.collect_values(pdf.doc)["Full_name"] == ""
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (out, "")))
    w.import_form_data()
    values = forms.collect_values(pdf.doc)
    assert values["Full_name"] == "Ana Pop ș" and values["I_agree"] not in ("", "Off")
    assert not d.warnings


def test_links_attachments_and_layouts(driver, monkeypatch):
    import time
    from pdfdesk import dialogs, docfeatures
    from pdfdesk import tools as T
    d, w = driver, driver.w
    pdf = d.pdf
    # link tool: drag a box, point it at page 3; click it again and delete it
    monkeypatch.setattr(dialogs.LinkDialog, "exec", lambda self: (self.page.setValue(3), True)[1])
    w.set_tool(T.LINK)
    d.drag(d.pos(0, 72, 700), d.pos(0, 220, 720))
    links = d.c.links(0)
    assert any(lk.get("page") == 2 for lk in links)

    def delete(self):
        self._delete()
        return True
    monkeypatch.setattr(dialogs.LinkDialog, "exec", delete)
    target = next(lk for lk in links if lk.get("page") == 2)
    r = fitz.Rect(target["from"])
    d.click(d.pos(0, r.x0 + 4, r.y0 + 4))
    assert not any(lk.get("page") == 2 for lk in d.c.links(0))
    w.set_tool(T.SELECT)

    # attachments: add, list, save; PDFs open in a new tab
    f = d.folder / "minutes.pdf"
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Attached minutes")
    doc.save(str(f))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(f), "")))
    w.attach_file()
    d.pump(5)
    assert d.tab.attachments.list.count() == 1
    item = d.tab.attachments.list.item(0).data(Qt.ItemDataRole.UserRole)
    out = d.folder / "saved-attachment.pdf"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", staticmethod(lambda *a, **k: (str(out), "")))
    w.save_attachment(item)
    assert out.read_bytes()[:4] == b"%PDF"
    n = w.tabs.count()
    w.open_attachment(item)
    assert w.tabs.count() == n + 1 and "Attached minutes" in w.tab().pdf.page(0).get_text()
    w.tab().pdf.saved_id = w.tab().pdf.state_id
    w.close_tab(w.tabs.currentIndex())
    d.pump()

    # find and highlight every match
    d.tab.open_find()
    d.tab.findbar.edit.setText("verified")
    d.tab.findbar._emit()
    for _ in range(30):
        d.pump(2)
    hits = len(d.tab.hit_order)
    assert hits > 3
    before = sum(1 for p in pdf.doc for a in p.annots() if a.type[1] == "Highlight")
    d.tab.highlight_all_hits()
    after = sum(1 for p in pdf.doc for a in p.annots() if a.type[1] == "Highlight")
    assert after == before + hits

    # a booklet is made as a new document in the background
    monkeypatch.setattr(dialogs.PrintLayoutDialog, "exec", lambda self: True)
    n = w.tabs.count()
    w.print_layout(True)
    t0 = time.time()
    while w.tabs.count() == n and time.time() - t0 < 30:
        d.pump(2)
    assert w.tabs.count() == n + 1 and "booklet" in w.tab().pdf.title
    assert docfeatures is not None
    assert not d.warnings


def test_measure_present_read_compare(driver, monkeypatch):
    import time
    from pdfdesk import appearance, speech
    from pdfdesk import tools as T
    d, w = driver, driver.w
    pdf = d.pdf

    def kinds(pno):
        return [appearance.kind_of(pdf.doc, a.xref) for a in pdf.page(pno).annots()]

    # distance by dragging, area by clicking corners and double-clicking
    w.opt.set_measure_scale(__import__("pdfdesk.measure", fromlist=["Scale"]).Scale(1, "in", 10, "ft"))
    w._choose_measure("distance")
    d.drag(d.pos(0, 100, 650), d.pos(0, 244, 650))
    assert kinds(0).count("Measure") == 1
    dist = [a.info["content"] for a in pdf.page(0).annots() if a.type[1] == "Line"][-1]
    assert dist.endswith(" ft") and abs(float(dist.split()[0]) - 20) < 0.3
    w._choose_measure("area")
    for x, y in ((100, 680), (172, 680), (172, 752)):
        d.click(d.pos(0, x, y))
    QTest.mouseDClick(d.c, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, d.pos(0, 100, 752))
    d.pump()
    area = [a.info["content"] for a in pdf.page(0).annots() if a.type[1] == "Polygon"]
    assert area and area[-1].endswith("ft\u00b2") and abs(float(area[-1].split()[0]) - 100) < 2

    # snapshot copies a picture
    w.set_tool(T.SNAPSHOT)
    d.drag(d.pos(0, 72, 72), d.pos(0, 300, 200))
    img = QApplication.clipboard().image()
    assert not img.isNull() and img.width() > 300
    w.set_tool(T.SELECT)

    # presentation mode turns pages with the keyboard
    w.present()
    d.pump(5)
    pres = w._presentation
    QTest.keyClick(pres, Qt.Key.Key_Right)
    assert pres.page == d.c.current_page + 1
    QTest.keyClick(pres, Qt.Key.Key_Escape)
    d.pump(5)

    # reader view shows the text without loading anything
    w.reader_view()
    t0 = time.time()
    from pdfdesk.reader import ReaderView
    while not [x for x in w.findChildren(ReaderView) if x.isVisible()] and time.time() - t0 < 20:
        d.pump(2)
    rv = [x for x in w.findChildren(ReaderView) if x.isVisible()][0]
    assert "Network Maintenance Report" in rv.view.toPlainText()
    rv.close()

    # bookmarks from headings
    w.auto_bookmarks()
    t0 = time.time()
    while not pdf.doc.get_toc() and time.time() - t0 < 20:
        d.pump(2)
    assert any("Summary" in t[1] for t in pdf.doc.get_toc())

    # read out loud uses the speech engine when there is one
    if speech.available():
        w.read_aloud("page")
        d.pump(5)
        w.stop_reading()

    # compare with an edited copy
    other = d.folder / "older.pdf"
    old = fitz.open("pdf", pdf.plain_bytes())
    old[0].add_redact_annot(old[0].search_for("regional")[0])
    old[0].apply_redactions()
    old.save(str(other))
    monkeypatch.setattr(QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: (str(other), "")))
    n = w.tabs.count()
    w.compare_files()
    t0 = time.time()
    while w.tabs.count() == n and time.time() - t0 < 60:
        d.pump(2)
    assert w.tabs.count() == n + 1 and w.tab().pdf.title.startswith("Compare")
    assert "Comparison report" in w.tab().pdf.page(0).get_text()
    assert not d.warnings


def test_digital_signature(driver, monkeypatch):
    import time
    from pdfdesk import dialogs, digitalid
    from pdfdesk import tools as T
    if not digitalid.available():
        pytest.skip("pyHanko is not installed")
    d, w = driver, driver.w
    digitalid.create_id("Ana Pop", "ana@example.com", "", "secret12")
    out = str(d.folder / "signed.pdf")

    def fill(self):
        self.password.setText("secret12")
        self.target.set_path(out)
        self.validate()
        return True
    monkeypatch.setattr(dialogs.SignDialog, "exec", fill)
    w.start_digital_sign()
    assert w.opt.tool == T.DIGISIGN
    n = w.tabs.count()
    d.drag(d.pos(0, 72, 640), d.pos(0, 300, 700))
    t0 = time.time()
    while w.tabs.count() == n and time.time() - t0 < 60:
        d.pump(2)
    assert w.tabs.count() == n + 1 and w.tab().pdf.path == out
    signed = w.tab()
    assert signed.pdf.signed_bytes and signed.banner.isVisible()
    shown = []
    monkeypatch.setattr(dialogs.SignaturesDialog, "exec", lambda self: shown.append(self.results) or True)
    w.check_signatures()
    t0 = time.time()
    while not shown and time.time() - t0 < 60:
        d.pump(2)
    assert shown and shown[0][0]["intact"] and shown[0][0]["valid"] and shown[0][0]["name"] == "Ana Pop"
    # adding a comment and saving keeps the signature intact (changes are appended after it)
    w.set_tool(T.NOTE)
    d.click(d.pos(0, 500, 100))
    assert signed.pdf.keeps_signatures()
    w.save()
    results = digitalid.validate(open(out, "rb").read())
    assert results[0]["intact"] and results[0]["valid"] and results[0]["changed_after"]
    assert results[0]["modification"] == "ANNOTATIONS" and digitalid.describe(results[0])[0] == "warn"
    # "Open signed version" shows the document exactly as signed (without the later comment)

    def open_first(self):
        self.list.setCurrentRow(0)
        self._open_version()
        return True
    monkeypatch.setattr(dialogs.SignaturesDialog, "exec", open_first)
    n = w.tabs.count()
    w.check_signatures()
    t0 = time.time()
    while w.tabs.count() == n and time.time() - t0 < 60:
        d.pump(2)
    version = w.tab()
    assert w.tabs.count() == n + 1 and "signed version" in version.pdf.title
    assert not list(version.pdf.page(0).annots(types=[fitz.PDF_ANNOT_TEXT]))
    assert any(a.type[0] == fitz.PDF_ANNOT_TEXT for a in signed.pdf.page(0).annots())
    version.pdf.saved_id = version.pdf.state_id
    signed.pdf.saved_id = signed.pdf.state_id
    assert not d.warnings
