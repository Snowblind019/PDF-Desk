"""One open document: sidebar + page view (or the page organizer) + find bar."""
from __future__ import annotations

import os
from pathlib import Path

import pymupdf as fitz
from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
                               QMessageBox, QSplitter, QStackedWidget, QTabWidget, QToolButton, QVBoxLayout, QWidget)

from pdfdesk import ui
from pdfdesk import convert, icons, jobs, pdfops
from pdfdesk.canvas import DocumentView
from pdfdesk.dialogs import InsertPagesDialog, PagesDialog, SplitDialog
from pdfdesk.document import PdfDocument
from pdfdesk.organizer import PageOrganizer
from pdfdesk.sidebar import AttachmentsPanel, BookmarkPanel, CommentsPanel, LayersPanel, SearchPanel, ThumbList
from pdfdesk.tools import ToolOptions


class FindBar(QFrame):
    query_changed = Signal(str, bool)
    next_requested = Signal()
    prev_requested = Signal()
    closed = Signal()
    highlight_all = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("findBar")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 5, 10, 5)
        ico = QLabel()
        ico.setPixmap(icons.icon("search").pixmap(16, 16))
        lay.addWidget(ico)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Find in document")
        self.edit.setClearButtonEnabled(True)
        self.edit.setMinimumWidth(260)
        lay.addWidget(self.edit)
        self.whole = QCheckBox("Whole words")
        lay.addWidget(self.whole)
        self.count = QLabel("")
        self.count.setObjectName("muted")
        lay.addWidget(self.count)
        lay.addStretch(1)
        mark = QToolButton()
        icons.bind(mark, "highlighter")
        mark.setToolTip("Highlight every match")
        mark.clicked.connect(self.highlight_all.emit)
        lay.addWidget(mark)
        for name, tip, sig in (("chevron-up", "Previous match (Shift+F3)", self.prev_requested),
                               ("chevron-down", "Next match (F3)", self.next_requested)):
            b = QToolButton()
            icons.bind(b, name)
            b.setToolTip(tip)
            b.clicked.connect(sig.emit)
            lay.addWidget(b)
        close = QToolButton()
        icons.bind(close, "x")
        close.setToolTip("Close (Esc)")
        close.clicked.connect(self.close_bar)
        lay.addWidget(close)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(280)
        self._timer.timeout.connect(self._emit)
        self.edit.textChanged.connect(lambda _t: self._timer.start())
        self.whole.toggled.connect(lambda _c: self._emit())
        self._last = None

    def _emit(self) -> None:
        q = (self.edit.text().strip(), self.whole.isChecked())
        if q != self._last:
            self._last = q
            self.query_changed.emit(*q)

    def open_bar(self, text: str = "") -> None:
        self.show()
        if text:
            self.edit.setText(text)
        self.edit.setFocus()
        self.edit.selectAll()

    def close_bar(self) -> None:
        self.hide()
        self.closed.emit()

    def keyPressEvent(self, ev) -> None:
        if ev.key() == Qt.Key.Key_Escape:
            self.close_bar()
            return
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._timer.stop()
            if (self.edit.text().strip(), self.whole.isChecked()) != self._last:
                self._emit()
            elif ev.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                self.prev_requested.emit()
            else:
                self.next_requested.emit()
            return
        super().keyPressEvent(ev)


class DocumentTab(QWidget):
    title_changed = Signal()
    status_changed = Signal()

    def __init__(self, pdf: PdfDocument, options: ToolOptions, settings, parent=None):
        super().__init__(parent)
        self.pdf = pdf
        self.settings = settings
        self.opt = options
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        lay.addWidget(self.splitter)

        # ---- sidebar
        self.sidebar = QWidget()
        self.sidebar.setObjectName("sidebar")
        self.sidebar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        sb = QVBoxLayout(self.sidebar)
        sb.setContentsMargins(0, 0, 0, 0)
        self.side_tabs = QTabWidget()
        self.side_tabs.tabBar().setObjectName("sideTabs")
        self.side_tabs.setDocumentMode(True)
        self.side_tabs.setIconSize(QSize(18, 18))
        self.side_tabs.tabBar().setUsesScrollButtons(False)
        self.side_tabs.tabBar().setExpanding(True)
        self.thumbs = ThumbList(pdf, box=112)
        self.bookmarks = BookmarkPanel(pdf, lambda: self.canvas.current_page)
        self.comments = CommentsPanel(pdf)
        self.attachments = AttachmentsPanel(pdf)
        self.layers = LayersPanel(pdf)
        self.search_panel = SearchPanel()
        for widget, ico, tip in ((self.thumbs, "gallery-vertical", "Pages"), (self.bookmarks, "bookmark", "Bookmarks"),
                                 (self.comments, "message-square-text", "Comments"),
                                 (self.attachments, "paperclip", "Attached files"), (self.layers, "layers", "Layers"),
                                 (self.search_panel, "search", "Search results")):
            idx = self.side_tabs.addTab(widget, "")
            self.side_tabs.setTabIcon(idx, icons.icon(ico))
            self.side_tabs.setTabToolTip(idx, tip)
        sb.addWidget(self.side_tabs)
        self.sidebar.setMinimumWidth(150)
        self.splitter.addWidget(self.sidebar)

        # ---- main area
        main = QWidget()
        ml = QVBoxLayout(main)
        ml.setContentsMargins(0, 0, 0, 0)
        ml.setSpacing(0)
        self.banner = QFrame()
        self.banner.setObjectName("sigBanner")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(12, 6, 10, 6)
        self.banner_icon = QLabel()
        self.banner_icon.setPixmap(icons.icon("file-check").pixmap(18, 18))
        bl.addWidget(self.banner_icon)
        self.banner_text = QLabel("This PDF contains digital signatures. Check them to see who signed it and "
                                  "whether it changed afterwards.")
        self.banner_text.setTextFormat(Qt.TextFormat.PlainText)
        self.banner_text.setWordWrap(True)
        bl.addWidget(self.banner_text, 1)
        self.banner_button = QToolButton()
        self.banner_button.setText("Check signatures")
        bl.addWidget(self.banner_button)
        self.banner.setVisible(bool(pdf.signed_bytes))
        self.set_banner_level("info")
        ml.addWidget(self.banner)
        self.findbar = FindBar()
        self.findbar.hide()
        ml.addWidget(self.findbar)
        self.stack = QStackedWidget()
        self.view = DocumentView(pdf, options, settings)
        self.canvas = self.view.canvas
        self.stack.addWidget(self.view)
        self.organizer: PageOrganizer | None = None
        ml.addWidget(self.stack, 1)
        self.splitter.addWidget(main)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([190, 1000])
        self.sidebar.setVisible(bool(settings.get("sidebar_visible")))

        # ---- wiring
        self.thumbs.page_clicked.connect(lambda p: self.canvas.go_to_page(p))
        self.thumbs.page_action.connect(lambda a, p: self.page_action(a, [p]))
        self.canvas.current_page_changed.connect(self.thumbs.set_current_page)
        self.canvas.current_page_changed.connect(lambda _p: self.status_changed.emit())
        self.canvas.zoom_changed.connect(lambda _z: self.status_changed.emit())
        self.canvas.page_action_requested.connect(lambda a, p: self.page_action(a, [p]))
        self.bookmarks.go_to.connect(lambda p: self.canvas.go_to_page(p))
        self.comments.open_annot.connect(self._open_annot)
        self.comments.delete_annot.connect(self._delete_annot)
        self.search_panel.hit_clicked.connect(self._show_hit)
        self.findbar.query_changed.connect(self.start_search)
        self.findbar.next_requested.connect(lambda: self.step_hit(1))
        self.findbar.prev_requested.connect(lambda: self.step_hit(-1))
        self.findbar.closed.connect(self._search_closed)
        self.findbar.highlight_all.connect(self.highlight_all_hits)
        pdf.dirty_changed.connect(lambda _d: self.title_changed.emit())
        pdf.saved.connect(lambda _p: self.title_changed.emit())
        pdf.structure_changed.connect(self._structure_changed)

        self.hits: dict[int, list] = {}
        self.hit_order: list[tuple[int, int]] = []
        self._search_gen = 0
        self._search_state: dict | None = None

    # ------------------------------------------------------------------ basics
    def set_banner_level(self, level: str, text: str | None = None) -> None:
        colors = {"info": ("#e8f0fe", "#1f4e9c"), "good": ("#e6f4ea", "#1e6b34"), "warn": ("#fff4d6", "#7a5300"),
                  "bad": ("#fde7e9", "#9b1c26")}
        bg, fg = colors.get(level, colors["info"])
        icon = {"good": "badge-check", "warn": "triangle-alert", "bad": "octagon-x"}.get(level, "file-check")
        self.banner_icon.setPixmap(icons.icon(icon).pixmap(18, 18))
        self.banner.setStyleSheet(f"QFrame#sigBanner {{ background: {bg}; border-bottom: 1px solid {fg}33; }}"
                                  f" QFrame#sigBanner QLabel {{ color: {fg}; }}")
        if text:
            self.banner_text.setText(text)

    def display_title(self) -> str:
        return ("* " if self.pdf.dirty else "") + self.pdf.title

    def toggle_sidebar(self, show: bool | None = None) -> None:
        show = not self.sidebar.isVisible() if show is None else show
        self.sidebar.setVisible(show)
        self.settings.set("sidebar_visible", show)

    def show_organizer(self, on: bool) -> None:
        if on:
            if self.organizer is None:
                self.organizer = PageOrganizer(self.pdf)
                self.organizer.action_requested.connect(self.page_action)
                self.organizer.reorder_requested.connect(self.reorder)
                self.organizer.close_requested.connect(lambda: self.show_organizer(False))
                self.organizer.page_opened.connect(self._open_from_organizer)
                self.stack.addWidget(self.organizer)
            self.stack.setCurrentWidget(self.organizer)
            self.organizer.grid.set_current_page(self.canvas.current_page)
            self.organizer.grid.setFocus()
        else:
            self.stack.setCurrentWidget(self.view)
            self.canvas.setFocus()
        self.status_changed.emit()

    def organizer_visible(self) -> bool:
        return self.organizer is not None and self.stack.currentWidget() is self.organizer

    def _open_from_organizer(self, pno: int) -> None:
        self.show_organizer(False)
        self.canvas.go_to_page(pno)

    def _structure_changed(self) -> None:
        if self._search_state or self.hits:
            q = self.findbar.edit.text().strip()
            if q:
                QTimer.singleShot(0, lambda: self.start_search(q, self.findbar.whole.isChecked()))
        self.status_changed.emit()

    def _open_annot(self, pno: int, xref: int) -> None:
        if jobs.busy():
            return
        page = self.pdf.page(pno)
        try:
            annot = page.load_annot(xref)
        except Exception:
            annot = None
        if annot is None:
            return
        self.show_organizer(False)
        self.canvas.go_to_page(pno, center_rect=self.canvas.rect_to_canvas(pno, annot.rect))
        if self.opt.tool != "select":
            self.opt.set_tool("select")
        self.canvas.select_annot(pno, xref)

    def _delete_annot(self, pno: int, xref: int) -> None:
        if jobs.busy():
            return
        self.canvas.select_annot(pno, xref)
        self.canvas.delete_selected_annot()

    # ------------------------------------------------------------------ search
    def open_find(self) -> None:
        text = self.canvas.selected_text() if self.canvas.has_selection() and "\n" not in self.canvas.selected_text() else ""
        self.findbar.open_bar(text)

    def _search_closed(self) -> None:
        self._search_gen += 1
        self._search_state = None
        self.hits = {}
        self.hit_order = []
        self.canvas.set_search_hits({})
        self.search_panel.clear()
        self.canvas.setFocus()

    def start_search(self, text: str, whole: bool = False) -> None:
        self._search_gen += 1
        self.hits = {}
        self.hit_order = []
        self.canvas.set_search_hits({})
        if not text:
            self._search_state = None
            self.findbar.count.setText("")
            self.search_panel.clear()
            return
        self.search_panel.clear(f"Searching for “{text}”...")
        self._search_state = {"text": text, "whole": whole, "page": 0, "gen": self._search_gen, "jumped": False}
        QTimer.singleShot(0, self._search_step)

    def _snippet(self, pno: int, rect) -> str:
        words = self.canvas.words(pno)
        line_key = None
        for w in words:
            if fitz.Rect(w[:4]).intersects(rect):
                line_key = (w[5], w[6])
                break
        if line_key is None:
            return ""
        text = " ".join(w[4] for w in words if (w[5], w[6]) == line_key)
        return text if len(text) <= 110 else text[:107] + "..."

    def _search_step(self) -> None:
        st = self._search_state
        if not st or st["gen"] != self._search_gen:
            return
        if jobs.busy():
            QTimer.singleShot(400, self._search_step)
            return
        n = self.pdf.page_count
        end = min(n, st["page"] + 20)
        for pno in range(st["page"], end):
            try:
                page = self.pdf.page(pno)
                if st["whole"]:
                    found = pdfops.find_matches(page, st["text"], whole_word=True)
                    rects = found[0][1] if found else []
                else:
                    rects = page.search_for(st["text"], flags=pdfops.SEARCH_FLAGS)
            except Exception:
                rects = []
            if rects:
                self.hits[pno] = rects
                self.search_panel.add_hits(pno, [self._snippet(pno, r) for r in rects])
        st["page"] = end
        self.hit_order = [(p, k) for p in sorted(self.hits) for k in range(len(self.hits[p]))]
        self.canvas.set_search_hits(self.hits, self.canvas.current_hit)
        total = len(self.hit_order)
        if total and not st["jumped"]:
            after = [h for h in self.hit_order if h[0] >= self.canvas.current_page]
            if after or end >= n:
                st["jumped"] = True
                self._show_hit(*(after[0] if after else self.hit_order[0]))
        if end < n:
            self.findbar.count.setText(f"{total} so far...")
            QTimer.singleShot(0, self._search_step)
        else:
            self._search_state = None
            self.findbar.count.setText("No matches" if not total else f"{total} match{'es' if total != 1 else ''}")
            self.search_panel.label.setText(
                f"No matches for “{st['text']}”" if not total else
                f"{total} match{'es' if total != 1 else ''} for “{st['text']}”")
            if total and not self.sidebar.isVisible():
                pass

    def highlight_all_hits(self) -> None:
        """Turn every search match into a highlight comment."""
        if jobs.busy() or not self.hits:
            if not self.hits:
                self.window().statusBar().showMessage("Search for something first.", 4000)
            return
        from pdfdesk import annots
        color = self.opt.color("highlight")
        total = sum(len(r) for r in self.hits.values())
        if total > 5000:
            self.window().statusBar().showMessage(f"{total} matches is too many to highlight; narrow the search.",
                                                  6000)
            return
        count = 0
        with self.pdf.edit("Highlight matches", pages=sorted(self.hits)):
            for pno, rects in self.hits.items():
                page = self.pdf.page(pno)
                for r in rects:
                    annots.add_text_markup(page, "highlight", [r], color, self.opt.opacity_for("highlight"),
                                           self.opt.author, self.findbar.edit.text().strip())
                    count += 1
        self.window().statusBar().showMessage(f"Highlighted {count} match(es).", 5000)

    def _show_hit(self, pno: int, idx: int) -> None:
        self.canvas.show_hit(pno, idx)
        self.search_panel.select(pno, idx)
        if (pno, idx) in self.hit_order:
            k = self.hit_order.index((pno, idx)) + 1
            self.findbar.count.setText(f"{k} of {len(self.hit_order)}")

    def step_hit(self, step: int) -> None:
        if not self.hit_order:
            if not self.findbar.isVisible():
                self.open_find()
            return
        cur = self.canvas.current_hit
        if cur in self.hit_order:
            k = (self.hit_order.index(cur) + step) % len(self.hit_order)
        else:
            page = self.canvas.current_page
            cands = [i for i, h in enumerate(self.hit_order) if (h[0] >= page if step > 0 else h[0] <= page)]
            k = (cands[0] if step > 0 else cands[-1]) if cands else (0 if step > 0 else len(self.hit_order) - 1)
        self._show_hit(*self.hit_order[k])

    # ------------------------------------------------------------------ page actions
    def reorder(self, moved: list[int], before: int) -> None:
        if jobs.busy():
            return
        n = self.pdf.page_count
        rest = [p for p in range(n) if p not in moved]
        pos = len([p for p in rest if p < before])
        order = rest[:pos] + moved + rest[pos:]
        if order == list(range(n)):
            return
        with self.pdf.edit("Move pages", structure=True):
            pdfops.reorder_pages(self.pdf.doc, order)
        if self.organizer:
            grid = self.organizer.grid
            grid.clearSelection()
            for k in range(pos, pos + len(moved)):
                if grid.item(k):
                    grid.item(k).setSelected(True)

    def page_action(self, action: str, pages: list[int]) -> None:
        if jobs.busy():
            return
        pdf = self.pdf
        cur = self.canvas.current_page
        pages = sorted(set(pages)) if pages else [cur]
        try:
            if action in ("rotate_cw", "rotate_ccw"):
                with pdf.edit("Rotate pages", structure=True):
                    pdfops.rotate_pages(pdf.doc, pages, 90 if action == "rotate_cw" else -90)
            elif action == "delete":
                if len(pages) >= pdf.page_count:
                    ui.information(self, "Delete pages", "A PDF needs at least one page.")
                    return
                if len(pages) > 1 and ui.question(
                        self, "Delete pages", f"Delete {len(pages)} pages ({pdfops.format_page_list(pages)})?") \
                        != ui.Yes:
                    return
                with pdf.edit("Delete pages", structure=True):
                    pdfops.delete_pages(pdf.doc, pages)
            elif action == "insert_blank":
                at = max(pages) + 1
                with pdf.edit("Insert blank page", structure=True):
                    pdfops.insert_blank_page(pdf.doc, at, like_page=max(pages))
                QTimer.singleShot(0, lambda: self.canvas.go_to_page(at))
            elif action == "duplicate":
                with pdf.edit("Duplicate pages", structure=True):
                    for p in reversed(pages):
                        pdfops.duplicate_page(pdf.doc, p)
            elif action == "insert_file":
                self.insert_file(pages[-1] if pages else cur)
            elif action == "extract":
                self.extract_pages(pages)
            elif action == "split":
                self.split()
        except Exception as exc:
            ui.warning(self, "Pages", str(exc))

    def ask_pages(self, title: str, prompt: str, default: str = "current", ok: str = "OK"):
        dlg = PagesDialog(self, title, prompt, self.pdf.page_count, self.canvas.current_page, default, ok_text=ok)
        if dlg.exec():
            return dlg.result_pages()
        return None

    def insert_file(self, after: int | None = None, path: str = "") -> None:
        if jobs.busy():
            return
        cur = self.canvas.current_page if after is None else after
        dlg = InsertPagesDialog(self, self.pdf.page_count, cur, path)
        if not dlg.exec():
            return
        src = dlg.picker.path()
        index = dlg.index()

        def insert(data: bytes) -> None:
            password = None
            try:
                probe = fitz.open("pdf", data)
                needs_pass = probe.needs_pass
                probe.close()
            except Exception as exc:
                ui.warning(self, "Insert pages", f"That file couldn't be read as a PDF:\n\n{exc}")
                return
            if needs_pass:
                password, ok = QInputDialog.getText(self, "Password", f"This file needs a password:\n{Path(src).name}",
                                                    QLineEdit.EchoMode.Password)
                if not ok:
                    return
            try:
                with self.pdf.edit("Insert pages", structure=True):
                    count = pdfops.insert_pdf_bytes(self.pdf.doc, data, index, password)
            except Exception as exc:
                ui.warning(self, "Insert pages", str(exc))
                return
            QTimer.singleShot(0, lambda: self.canvas.go_to_page(index))
            self.window().statusBar().showMessage(f"Inserted {count} page(s)", 4000)

        if src.lower().endswith(".pdf"):
            insert(Path(src).read_bytes())
        else:
            jobs.run_job(self, "Converting file", convert.to_pdf_bytes, src, on_done=insert)

    def extract_pages(self, pages: list[int] | None = None) -> None:
        if jobs.busy():
            return
        dlg = PagesDialog(self, "Extract pages", "Pages to extract:", self.pdf.page_count, self.canvas.current_page,
                          pdfops.format_page_list(pages) if pages and len(pages) > 1 else "current",
                          options=[("separate", "Save each page as its own file", False),
                                   ("delete", "Delete these pages from this document afterwards", False)],
                          ok_text="Extract")
        if not dlg.exec():
            return
        pages = dlg.result_pages()
        base = self.pdf.path or self.pdf.suggested_path or str(Path(self.settings.get("last_dir")) / self.pdf.title)
        stem = Path(base).stem
        if dlg.option("separate"):
            folder = QFileDialog.getExistingDirectory(self, "Save pages in", os.path.dirname(base))
            if not folder:
                return
            for p in pages:
                data = pdfops.extract_pages_bytes(self.pdf.doc, [p])
                path = pdfops._unique_path(os.path.join(folder, f"{stem} - page {p + 1}.pdf"))
                Path(path).write_bytes(data)
            msg = f"Saved {len(pages)} file(s) in {folder}"
        else:
            suggestion = os.path.join(os.path.dirname(base), f"{stem} - pages {pdfops.format_page_list(pages).replace(', ', '_')}.pdf")
            path, _ = QFileDialog.getSaveFileName(self, "Save extracted pages", suggestion, "PDF files (*.pdf)")
            if not path:
                return
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
            Path(path).write_bytes(pdfops.extract_pages_bytes(self.pdf.doc, pages))
            msg = f"Saved {Path(path).name}"
        if dlg.option("delete"):
            if len(pages) >= self.pdf.page_count:
                ui.information(self, "Extract pages", "All pages were extracted, so none were deleted.")
            else:
                with self.pdf.edit("Delete extracted pages", structure=True):
                    pdfops.delete_pages(self.pdf.doc, pages)
        self.window().statusBar().showMessage(msg, 6000)

    def split(self) -> None:
        if jobs.busy():
            return
        dlg = SplitDialog(self, self.pdf)
        if not dlg.exec():
            return
        mode, value = dlg.params()
        copy = fitz.open("pdf", self.pdf.plain_bytes())

        def done(paths):
            box = QMessageBox(QMessageBox.Icon.Information, "Split PDF",
                              f"Created {len(paths)} file(s) in\n{dlg.folder.path()}", parent=self)
            box.setTextFormat(Qt.TextFormat.PlainText)
            open_btn = box.addButton("Open folder", QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Ok)
            box.exec()
            if box.clickedButton() is open_btn:
                from PySide6.QtCore import QUrl
                from PySide6.QtGui import QDesktopServices
                QDesktopServices.openUrl(QUrl.fromLocalFile(dlg.folder.path()))

        jobs.run_job(self, "Splitting PDF", pdfops.split_document, copy, mode, value, dlg.folder.path(),
                     dlg.base.text().strip() or Path(self.pdf.title).stem, on_done=done)
