"""Main window: tabs, menus, toolbars, opening/saving, and every document command."""
from __future__ import annotations

import os
from pathlib import Path

import pymupdf as fitz
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import (QAction, QActionGroup, QDesktopServices, QGuiApplication, QIcon, QKeySequence,
                           QPixmap)
from PySide6.QtWidgets import (QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QInputDialog, QLabel,
                               QLineEdit, QMainWindow, QMenu, QMessageBox, QSpinBox, QTabBar, QTabWidget, QToolBar,
                               QToolButton, QWidget, QSizePolicy, QCheckBox)

from pdfdesk import ui
from pdfdesk import (APP_NAME, annots, compare, convert, dialogs, docfeatures, forms, icons, jobs, measure, pdfops,
                     printing, safety, speech, theme)
from pdfdesk import tools as T
from pdfdesk.config import settings as get_settings
from pdfdesk.doctab import DocumentTab
from pdfdesk.document import NeedsPassword, PdfDocument, write_file_safely
from pdfdesk.fonts import hex_to_rgb, rgb_to_hex
from pdfdesk.home import HomePage
from pdfdesk.recents import RecentFiles
from pdfdesk.signature import SignatureDialog, list_signatures
from pdfdesk.textformat import RichEditor, TextFormatBar
from pdfdesk.widgets import ColorButton

ZOOM_PRESETS = ["Fit width", "Fit page", "50%", "75%", "100%", "125%", "150%", "200%", "300%", "400%"]


class _NoChange(Exception):
    pass


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = get_settings()
        self.recents = RecentFiles(int(self.settings.get("recents_limit") or 40))
        self.opt = T.ToolOptions(self.settings)
        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(icons.app_icon())
        self.setAcceptDrops(True)
        self.resize(1320, 880)
        self.act: dict[str, QAction] = {}
        self.doc_actions: list[QAction] = []
        self.tool_buttons: dict[str, QToolButton] = {}

        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabBar().setObjectName("docTabs")
        self.tabs.tabBar().setExpanding(False)
        self.tabs.tabBar().setElideMode(Qt.TextElideMode.ElideMiddle)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.setCentralWidget(self.tabs)

        self.home = HomePage(self.recents)
        self.home.open_requested.connect(self.open_path)
        self.home.action_requested.connect(self._home_action)
        self.tabs.addTab(self.home, icons.icon("house"), "Home")
        self.tabs.tabBar().setTabButton(0, QTabBar.ButtonPosition.RightSide, None)
        self.tabs.tabBar().setTabButton(0, QTabBar.ButtonPosition.LeftSide, None)

        self.reader = speech.Reader(self)
        self.reader.started_piece.connect(self._reading_piece)
        self.reader.finished.connect(lambda: self.statusBar().showMessage("Finished reading.", 3000))
        self.reader.failed.connect(lambda m: ui.information(self, "Read out loud", m))
        self._build_actions()
        self._build_menus()
        self._build_toolbars()
        self._build_statusbar()
        self.opt.tool_changed.connect(self._tool_changed)
        self.opt.changed.connect(self._sync_tool_widgets)
        self._restore_window()
        self.tabs.currentChanged.connect(self._tab_changed)
        self._tab_changed(0)

    # ================================================================== helpers
    def tab(self) -> DocumentTab | None:
        w = self.tabs.currentWidget()
        return w if isinstance(w, DocumentTab) else None

    def doc_tabs(self) -> list[DocumentTab]:
        return [self.tabs.widget(i) for i in range(self.tabs.count()) if isinstance(self.tabs.widget(i), DocumentTab)]

    def _a(self, key: str, text: str, slot, shortcut=None, icon: str | None = None, doc: bool = True,
           checkable: bool = False, tip: str | None = None) -> QAction:
        a = QAction(text, self)
        if icon:
            icons.bind(a, icon)
        if shortcut:
            if isinstance(shortcut, (list, tuple)):
                a.setShortcuts([QKeySequence(s) for s in shortcut])
            else:
                a.setShortcut(QKeySequence(shortcut))
        a.setCheckable(checkable)
        if tip:
            a.setToolTip(tip)
        elif shortcut:
            first = shortcut[0] if isinstance(shortcut, (list, tuple)) else shortcut
            ks = QKeySequence(first).toString(QKeySequence.SequenceFormat.NativeText)
            a.setToolTip(f"{text.replace('...', '')} ({ks})")
        if checkable:
            a.toggled.connect(slot)
        else:
            a.triggered.connect(lambda _=False: slot())
        self.act[key] = a
        if doc:
            self.doc_actions.append(a)
        self.addAction(a)
        return a

    def doc_base(self, pdf) -> Path:
        """Folder and safe file-name stem to suggest when saving something made from this document."""
        from pdfdesk import safety
        stem = safety.safe_filename_part(Path(pdf.path or pdf.title or "").stem) or "Document"
        folder = os.path.dirname(pdf.path or pdf.suggested_path or "") or self.settings.get("last_dir")
        return Path(folder) / stem

    def msg(self, text: str, ms: int = 5000) -> None:
        self.statusBar().showMessage(text, ms)

    def _with_tab(fn):  # noqa: N805 - decorator for "needs an open document"
        def wrapper(self, *args, **kwargs):
            tab = self.tab()
            if tab is None:
                return None
            if jobs.busy():
                self.msg("Wait for the running task to finish first.")
                return None
            tab.canvas.commit_editor()
            return fn(self, tab, *args, **kwargs)
        wrapper.__name__ = fn.__name__
        return wrapper

    # ================================================================== actions
    def _build_actions(self) -> None:
        a = self._a
        a("new_blank", "New blank PDF...", self.new_blank, "Ctrl+N", "file", doc=False)
        a("open", "Open...", self.open_dialog, "Ctrl+O", "folder-open", doc=False)
        a("create", "Create PDF from files...", self.create_pdf, None, "file-plus-2", doc=False)
        a("clipboard", "Create PDF from clipboard", self.from_clipboard, "Ctrl+Shift+V", "clipboard", doc=False)
        a("combine", "Combine files...", self.combine, None, "combine", doc=False)
        a("save", "Save", self.save, "Ctrl+S", "save")
        a("save_as", "Save as...", self.save_as, "Ctrl+Shift+S")
        a("save_copy", "Save a copy...", self.save_copy)
        a("print", "Print...", self.print_doc, "Ctrl+P", "printer")
        a("properties", "Document properties...", self.properties, "Ctrl+D", "info")
        a("show_folder", "Show in folder", self.show_in_folder, None, "folder")
        a("close_tab", "Close tab", lambda: self.close_tab(self.tabs.currentIndex()), "Ctrl+W", doc=False)
        a("quit", "Quit", self.close, "Ctrl+Q", doc=False)

        a("undo", "Undo", self.undo, QKeySequence.StandardKey.Undo, "undo-2")
        a("redo", "Redo", self.redo, ["Ctrl+Y", "Ctrl+Shift+Z"], "redo-2")
        a("copy", "Copy", self.copy, QKeySequence.StandardKey.Copy, "copy")
        a("select_all", "Select all text on page", self.select_all, "Ctrl+A")
        a("find", "Find...", self.find, "Ctrl+F", "search")
        a("find_next", "Find next", lambda: self.tab() and self.tab().step_hit(1), "F3")
        a("find_prev", "Find previous", lambda: self.tab() and self.tab().step_hit(-1), "Shift+F3")
        a("delete_sel", "Delete selected comment", self.delete_selected, None, "trash-2")
        a("prefs", "Preferences...", self.preferences, "Ctrl+,", "settings", doc=False)

        a("home", "Home", lambda: self.tabs.setCurrentIndex(0), "Ctrl+H", "house", doc=False)
        a("zoom_in", "Zoom in", lambda: self.tab() and self.tab().canvas.zoom_by(1.2), ["Ctrl+=", "Ctrl++"], "zoom-in")
        a("zoom_out", "Zoom out", lambda: self.tab() and self.tab().canvas.zoom_by(1 / 1.2), "Ctrl+-", "zoom-out")
        a("actual", "Actual size", lambda: self.tab() and self.tab().canvas.set_zoom(1.0), "Ctrl+0")
        a("fit_width", "Fit width", lambda: self.tab() and self.tab().canvas.set_zoom(1, mode="fit_width"), "Ctrl+1",
          "move-horizontal")
        a("fit_page", "Fit page", lambda: self.tab() and self.tab().canvas.set_zoom(1, mode="fit_page"), "Ctrl+2",
          "maximize")
        a("goto", "Go to page...", self.goto_page, "Ctrl+G")
        a("first", "First page", lambda: self.tab() and self.tab().canvas.go_to_page(0), None, None)
        a("prev", "Previous page", self.prev_page, None, "chevron-up")
        a("next", "Next page", self.next_page, None, "chevron-down")
        a("last", "Last page", lambda: self.tab() and self.tab().canvas.go_to_page(self.tab().pdf.page_count - 1))
        a("sidebar", "Sidebar", self.toggle_sidebar, "F4", "panel-left", checkable=True)
        a("night", "Night mode", self.toggle_night, "Ctrl+Shift+N", "moon", doc=False, checkable=True,
          tip="Night mode: dark pages for reading in the dark (Ctrl+Shift+N)")
        a("forms", "Highlight form fields", self.toggle_forms, None, "text-cursor-input", doc=False, checkable=True)
        a("fullscreen", "Full screen", self.toggle_fullscreen, "F11", "maximize-2", doc=False, checkable=True)
        a("present", "Presentation", self.present, "F5", "presentation", tip="Show the pages full screen (F5)")
        a("reader", "Reader view", self.reader_view, "Ctrl+4", "book-open-text",
          tip="Read the text reflowed, with large letters (Ctrl+4)")
        a("read_page", "Read this page out loud", lambda: self.read_aloud("page"), "Ctrl+Shift+Y", "volume-2")
        a("read_end", "Read to the end", lambda: self.read_aloud("end"), "Ctrl+Shift+B")
        a("read_selection", "Read the selected text", lambda: self.read_aloud("selection"))
        a("read_stop", "Stop reading", self.stop_reading, "Ctrl+Shift+E", "volume-x", doc=False)
        a("read_speed", "Reading speed...", self.reading_speed, None, None, doc=False)
        self.layout_group = QActionGroup(self)
        for key, label in (("single", "One page, scrolling"), ("facing", "Two pages"), ("cover", "Two pages with cover")):
            act = a(f"layout_{key}", label, lambda on, k=key: on and self.set_layout(k), None, None, checkable=True)
            self.layout_group.addAction(act)
        self.theme_group = QActionGroup(self)
        for key, label in (("system", "Follow system"), ("light", "Light"), ("dark", "Dark")):
            act = a(f"theme_{key}", label, lambda on, k=key: on and self.set_theme(k), None, None, doc=False,
                    checkable=True)
            self.theme_group.addAction(act)

        a("organize", "Organize pages", self.toggle_organizer, "Ctrl+Shift+O", "layout-grid", checkable=True)
        a("rotate_cw", "Rotate page clockwise", lambda: self.page_action("rotate_cw"), "Ctrl+R", "rotate-cw")
        a("rotate_ccw", "Rotate page counterclockwise", lambda: self.page_action("rotate_ccw"), "Ctrl+Shift+R",
          "rotate-ccw")
        a("rotate_pages", "Rotate pages...", self.rotate_pages)
        a("insert_blank", "Insert blank page", lambda: self.page_action("insert_blank"), None, "file-plus")
        a("insert_file", "Insert pages from file...", lambda: self.page_action("insert_file"), None, "file-input")
        a("duplicate", "Duplicate page", lambda: self.page_action("duplicate"), None, "copy")
        a("delete_pages", "Delete pages...", self.delete_pages, None, "trash-2")
        a("extract", "Extract pages...", lambda: self.tab() and self.tab().extract_pages(), None, "file-output")
        a("split", "Split PDF...", lambda: self.tab() and self.tab().split(), None, "scissors")
        a("crop", "Crop pages...", self.crop, None, "crop")
        a("header_footer", "Header, footer and page numbers...", self.header_footer, None, "panel-top")
        a("watermark", "Watermark...", self.watermark, None, "droplet")
        a("bookmark", "Add bookmark for this page...", self.add_bookmark, "Ctrl+B", "bookmark")
        a("page_labels", "Page labels...", self.page_labels, None, "tag")
        a("background", "Background...", self.background, None, "paint-bucket")
        a("page_size", "Page size...", self.page_size, None, "scaling")
        a("blank_pages", "Remove blank pages...", self.remove_blank_pages, None, "file-x")
        a("nup", "Pages per sheet...", lambda: self.print_layout(False), None, "grid-2x2")
        a("booklet", "Booklet...", lambda: self.print_layout(True), None, "book-open")

        a("compress", "Reduce file size...", self.compress, None, "shrink")
        a("protect", "Protect with password...", self.protect, None, "lock")
        a("unprotect", "Remove password...", self.unprotect, None, "lock-open")
        a("ocr", "Recognize text (OCR)...", self.ocr, None, "scan-text")
        a("find_redact", "Find and redact...", self.find_redact, None, "file-search")
        a("apply_redact", "Apply redactions", self.apply_redactions, None, "shield-check",
          tip="Permanently remove everything marked for redaction")
        a("flatten", "Flatten comments and forms", self.flatten, None, "layers")
        a("hidden", "Remove hidden information...", self.remove_hidden, None, "eraser")
        a("signatures", "Manage signatures...", self.manage_signatures, None, "signature", doc=False)
        a("grayscale", "Convert to grayscale", self.grayscale, None, "contrast")
        a("compare", "Compare files...", self.compare_files, None, "git-compare")
        a("auto_bookmarks", "Make bookmarks from headings", self.auto_bookmarks, None, "bookmark-plus")
        a("measure_scale", "Measuring scale...", self.measure_scale, None, "ruler", doc=False)
        a("digital_sign", "Sign with Digital ID...", self.start_digital_sign, None, "badge-check")
        a("check_signatures", "Check signatures...", self.check_signatures, None, "shield-check")
        a("digital_ids", "Digital IDs...", self.manage_digital_ids, None, "key-round", doc=False)
        a("extract_images", "Save all pictures...", self.extract_images, None, "images")
        a("export_comments", "Comment summary...", self.export_comments, None, "messages-square")
        a("attach_file", "Attach a file...", self.attach_file, None, "paperclip")
        a("prepare_form", "Prepare form", self.toggle_prepare_form, None, "file-check", checkable=True,
          tip="Prepare form: add, move and change form fields")
        a("detect_fields", "Find form fields automatically", self.detect_fields, None, "wand-sparkles")
        a("clear_form", "Clear form", self.clear_form, None, "eraser")
        a("export_form", "Export form data...", self.export_form_data, None, "file-output")
        a("import_form", "Import form data...", self.import_form_data, None, "file-input")

        a("shortcuts", "Keyboard shortcuts", lambda: dialogs.ShortcutsDialog(self).exec(), "F1", doc=False)
        a("extras", "Optional extras (LibreOffice, OCR)", self.extras_help, None, doc=False)
        a("about", f"About {APP_NAME}", self.about, None, doc=False)

        self.act["night"].setChecked(bool(self.settings.get("night_mode")))
        self.act["forms"].setChecked(bool(self.settings.get("highlight_forms")))
        self.act["sidebar"].setChecked(bool(self.settings.get("sidebar_visible")))
        self.act.get(f"layout_{self.settings.get('layout') or 'single'}", self.act["layout_single"]).setChecked(True)
        self.act.get(f"theme_{self.settings.get('theme') or 'system'}", self.act["theme_system"]).setChecked(True)
        nxt = QAction(self)
        nxt.setShortcut(QKeySequence("Ctrl+Tab"))
        nxt.triggered.connect(lambda: self.tabs.setCurrentIndex((self.tabs.currentIndex() + 1) % self.tabs.count()))
        prv = QAction(self)
        prv.setShortcut(QKeySequence("Ctrl+Shift+Tab"))
        prv.triggered.connect(lambda: self.tabs.setCurrentIndex((self.tabs.currentIndex() - 1) % self.tabs.count()))
        self.addAction(nxt)
        self.addAction(prv)

    def _build_menus(self) -> None:
        mb = self.menuBar()
        m = mb.addMenu("&File")
        for k in ("new_blank", "open"):
            m.addAction(self.act[k])
        self.recent_menu = m.addMenu("Open recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        m.addSeparator()
        for k in ("create", "combine", "clipboard"):
            m.addAction(self.act[k])
        m.addSeparator()
        for k in ("save", "save_as", "save_copy"):
            m.addAction(self.act[k])
        self.export_menu = m.addMenu(icons.icon("share"), "Export to")
        for key, label, ext, kind in convert.EXPORT_FORMATS:
            act = self.export_menu.addAction(label, lambda k=key: self.export(k))
            self.doc_actions.append(act)
        self.export_menu.addSeparator()
        self.export_menu.addAction(self.act["extract_images"])
        self.export_menu.addAction(self.act["export_comments"])
        self.export_menu.addAction(self.act["export_form"])
        m.addSeparator()
        for k in ("print", "properties", "show_folder"):
            m.addAction(self.act[k])
        m.addSeparator()
        m.addAction(self.act["close_tab"])
        m.addAction(self.act["quit"])

        m = mb.addMenu("&Edit")
        for k in ("undo", "redo", None, "copy", "select_all", "delete_sel", None, "find", "find_next", "find_prev",
                  None, "prefs"):
            m.addSeparator() if k is None else m.addAction(self.act[k])

        m = mb.addMenu("&View")
        for k in ("home", None, "zoom_in", "zoom_out", "actual", "fit_width", "fit_page", None):
            m.addSeparator() if k is None else m.addAction(self.act[k])
        lm = m.addMenu("Page layout")
        for k in ("single", "facing", "cover"):
            lm.addAction(self.act[f"layout_{k}"])
        tm = m.addMenu("Theme")
        for k in ("system", "light", "dark"):
            tm.addAction(self.act[f"theme_{k}"])
        for k in ("night", "forms", "sidebar", "fullscreen", None, "present", "reader", None, "goto", "first", "prev",
                  "next", "last"):
            m.addSeparator() if k is None else m.addAction(self.act[k])
        rm = m.addMenu(icons.icon("volume-2"), "Read out loud")
        for k in ("read_page", "read_end", "read_selection", None, "read_stop", "read_speed"):
            rm.addSeparator() if k is None else rm.addAction(self.act[k])

        m = mb.addMenu("&Pages")
        for k in ("organize", None, "rotate_cw", "rotate_ccw", "rotate_pages", None, "insert_blank", "insert_file",
                  "duplicate", "delete_pages", "blank_pages", None, "extract", "split", None, "crop", "page_size",
                  "header_footer", "watermark", "background", "page_labels", None, "nup", "booklet", None, "bookmark",
                  "auto_bookmarks"):
            m.addSeparator() if k is None else m.addAction(self.act[k])

        m = mb.addMenu("&Tools")
        for k in ("compare", None, "compress", "ocr", "grayscale", None, "protect", "unprotect", None, "find_redact",
                  "apply_redact", "hidden", None, "flatten", "signatures", None, "digital_sign", "check_signatures",
                  "digital_ids", None, "attach_file", "extract_images", "export_comments", "measure_scale"):
            m.addSeparator() if k is None else m.addAction(self.act[k])
        fm = m.addMenu(icons.icon("file-check"), "Forms")
        for k in ("prepare_form", "detect_fields", None, "clear_form", "export_form", "import_form"):
            fm.addSeparator() if k is None else fm.addAction(self.act[k])

        m = mb.addMenu("&Help")
        for k in ("shortcuts", "extras", None, "about"):
            m.addSeparator() if k is None else m.addAction(self.act[k])

    def _build_toolbars(self) -> None:
        tb = QToolBar("Main")
        tb.setObjectName("mainToolbar")
        tb.setMovable(False)
        tb.setIconSize(QSize(20, 20))
        self.addToolBar(tb)
        self.main_tb = tb
        for k in ("home", None, "open", "save", "print", None, "undo", "redo", None, "prev"):
            tb.addSeparator() if k is None else tb.addAction(self.act[k])
        self.page_edit = QLineEdit()
        self.page_edit.setFixedWidth(48)
        self.page_edit.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.page_edit.setToolTip("Current page (Ctrl+G)")
        self.page_edit.returnPressed.connect(self._page_entered)
        self.page_total = QLabel(" / 0 ")
        self.page_total.setObjectName("muted")
        self.page_widgets = [tb.addWidget(self.page_edit), tb.addWidget(self.page_total)]
        tb.addAction(self.act["next"])
        tb.addSeparator()
        tb.addAction(self.act["zoom_out"])
        self.zoom_box = QComboBox()
        self.zoom_box.setEditable(True)
        self.zoom_box.addItems(ZOOM_PRESETS)
        self.zoom_box.setFixedWidth(104)
        self.zoom_box.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.zoom_box.activated.connect(lambda _i: self._zoom_entered())
        self.zoom_box.lineEdit().returnPressed.connect(self._zoom_entered)
        self.zoom_widget = tb.addWidget(self.zoom_box)
        tb.addAction(self.act["zoom_in"])
        tb.addAction(self.act["fit_width"])
        tb.addAction(self.act["fit_page"])
        tb.addSeparator()
        for k in ("sidebar", "organize", "find", "night", "reader", "present"):
            tb.addAction(self.act[k])
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        tb.addAction(self.act["prefs"])

        # ---- editing tools
        et = QToolBar("Edit and comment")
        et.setObjectName("toolsToolbar")
        et.setMovable(False)
        et.setIconSize(QSize(20, 20))
        self.addToolBarBreak()
        self.addToolBar(et)
        self.tools_tb = et
        groups = [[T.SELECT, T.HAND], [T.HIGHLIGHT, T.UNDERLINE, T.STRIKEOUT],
                  [T.NOTE, T.TEXTBOX, T.PEN, T.RECT, T.ELLIPSE, T.LINE, T.ARROW],
                  [T.STAMP, T.IMAGE, T.SIGNATURE, T.FILLSIGN], [T.EDIT_TEXT, T.LINK, T.REDACT],
                  [T.MEASURE, T.SNAPSHOT]]
        for gi, group in enumerate(groups):
            if gi:
                et.addSeparator()
            for tool in group:
                label, ico, helptext = T.TOOLS[tool]
                b = QToolButton()
                icons.bind(b, ico)
                b.setCheckable(True)
                b.setToolTip(f"{label}: {helptext}")
                b.setAutoRaise(True)
                b.clicked.connect(lambda _=False, t=tool: self.set_tool(t))
                if tool == T.STAMP:
                    b.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
                    b.setMenu(self._stamp_menu())
                if tool == T.FILLSIGN:
                    b.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
                    b.setMenu(self._fillsign_menu())
                if tool == T.MEASURE:
                    b.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
                    b.setMenu(self._measure_menu())
                if tool == T.SIGNATURE:
                    b.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
                    menu = QMenu(b)
                    menu.aboutToShow.connect(lambda m=menu: self._fill_signature_menu(m))
                    b.setMenu(menu)
                et.addWidget(b)
                self.tool_buttons[tool] = b
        et.addAction(self.act["apply_redact"])
        et.addSeparator()
        # ---- tool options
        self.color_btn = ColorButton(self.opt.color_hex(T.HIGHLIGHT), "Color")
        self.color_btn.color_changed.connect(self._color_picked)
        self.w_color = et.addWidget(self.color_btn)
        self.fill_check = QCheckBox("Fill")
        self.fill_check.setToolTip("Fill shapes and text boxes")
        self.fill_check.setChecked(self.opt.fill)
        self.fill_check.toggled.connect(self._fill_toggled)
        self.w_fill = et.addWidget(self.fill_check)
        self.fill_btn = ColorButton(self.opt.fill_hex, "Fill color")
        self.fill_btn.color_changed.connect(self._fill_color_picked)
        self.w_fill_color = et.addWidget(self.fill_btn)
        self.width_box = QDoubleSpinBox()
        self.width_box.setRange(0.5, 20)
        self.width_box.setSingleStep(0.5)
        self.width_box.setDecimals(1)
        self.width_box.setSuffix(" pt")
        self.width_box.setToolTip("Line width")
        self.width_box.setValue(self.opt.width)
        self.width_box.valueChanged.connect(self._width_picked)
        self.w_width = et.addWidget(self.width_box)
        self.opacity_box = QSpinBox()
        self.opacity_box.setRange(10, 100)
        self.opacity_box.setSuffix(" %")
        self.opacity_box.setToolTip("Opacity")
        self.opacity_box.setValue(int(self.opt.opacity * 100))
        self.opacity_box.valueChanged.connect(self._opacity_picked)
        self.w_opacity = et.addWidget(self.opacity_box)
        et.addAction(self.act["delete_sel"])
        self.tool_hint = QLabel()
        self.tool_hint.setTextFormat(Qt.TextFormat.PlainText)  # can include field names from the PDF
        self.tool_hint.setObjectName("muted")
        self.tool_hint.setContentsMargins(10, 0, 4, 0)
        et.addWidget(self.tool_hint)

        # ---- Prepare Form (shown while editing form fields)
        self.addToolBarBreak()
        fb = QToolBar("Prepare form")
        fb.setObjectName("formToolbar")
        fb.setMovable(False)
        fb.setIconSize(QSize(20, 20))
        self.addToolBar(fb)
        self.form_bar = fb
        title = QLabel("  Prepare form:  ")
        title.setObjectName("muted")
        fb.addWidget(title)
        fb.addAction(self.act["prepare_form"])
        for tool in (T.SELECT, T.FIELD_TEXT, T.FIELD_CHECK, T.FIELD_RADIO, T.FIELD_COMBO, T.FIELD_LIST,
                     T.FIELD_SIGNATURE):
            label, ico, helptext = T.TOOLS[tool]
            b = QToolButton()
            icons.bind(b, ico)
            b.setCheckable(True)
            b.setToolTip(f"{label}: {helptext}")
            b.setAutoRaise(True)
            b.clicked.connect(lambda _=False, t=tool: self.set_tool(t))
            fb.addWidget(b)
            if tool != T.SELECT:
                self.tool_buttons[tool] = b
            else:
                self._form_select_btn = b
        fb.addSeparator()
        for k in ("detect_fields", "clear_form", "export_form", "import_form"):
            fb.addAction(self.act[k])
        done = QToolButton()
        done.setText("Done")
        done.setToolTip("Leave Prepare form")
        done.clicked.connect(lambda: self.act["prepare_form"].setChecked(False))
        spacer2 = QWidget()
        spacer2.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        fb.addWidget(spacer2)
        fb.addWidget(done)
        fb.hide()

        # ---- Word-style text formatting (shown while adding or editing text)
        self.addToolBarBreak()
        self.format_bar = TextFormatBar(self)
        self.addToolBar(self.format_bar)
        self.format_bar.changed.connect(self._format_changed)
        self.format_bar.font_box.set_recent(self.opt.recent_fonts())
        self.format_bar.hide()

    def _stamp_menu(self) -> QMenu:
        menu = QMenu(self)
        for name, _code in annots.STAMPS:
            menu.addAction(name, lambda n=name: self._choose_stamp(n))
        menu.addSeparator()
        for name in annots.CUSTOM_STAMPS:
            menu.addAction(name.replace("{date}", "+ date"), lambda n=name: self._choose_stamp(n))
        menu.addSeparator()
        menu.addAction("Custom text...", self._custom_stamp)
        return menu

    def _fillsign_menu(self) -> QMenu:
        menu = QMenu(self)
        group = QActionGroup(menu)
        for mode, label, ico in T.FILLSIGN_MODES:
            act = menu.addAction(icons.icon(ico), label, lambda m=mode: self._choose_fillsign(m))
            act.setCheckable(True)
            act.setChecked(mode == self.opt.fillsign_mode)
            group.addAction(act)
        menu.addSeparator()
        menu.addAction("Set my initials...", self._set_initials)
        menu.addAction("Date format...", self._set_date_format)
        return menu

    def _measure_menu(self) -> QMenu:
        menu = QMenu(self)
        group = QActionGroup(menu)
        for mode, label, ico in measure.MODES:
            act = menu.addAction(icons.icon(ico), label, lambda m=mode: self._choose_measure(m))
            act.setCheckable(True)
            act.setChecked(mode == self.opt.measure_mode)
            group.addAction(act)
        menu.addSeparator()
        menu.addAction(self.act["measure_scale"])
        return menu

    def _choose_measure(self, mode: str) -> None:
        self.opt.set_measure_mode(mode)
        self.set_tool(T.MEASURE)
        tip = ("Drag from one point to another." if mode == "distance" else
               "Click each corner, then double-click the last one.")
        self.msg(f"Measure {mode} (scale {self.opt.measure_scale().label()}). {tip}", 8000)

    def measure_scale(self) -> None:
        dlg = dialogs.MeasureScaleDialog(self, self.opt.measure_scale())
        if dlg.exec():
            self.opt.set_measure_scale(dlg.scale())
            self.msg(f"Measuring scale: {self.opt.measure_scale().label()}")

    def _choose_fillsign(self, mode: str) -> None:
        self.opt.set_fillsign_mode(mode)
        self.set_tool(T.FILLSIGN)
        label = next(lbl for m, lbl, _i in T.FILLSIGN_MODES if m == mode)
        self.msg(f"Fill & Sign: {label}. Click on the page to place it.")

    def _set_initials(self) -> None:
        text, ok = QInputDialog.getText(self, "Initials", "Your initials:", text=self.opt.initials)
        if ok and text.strip():
            self.settings.set("initials", text.strip()[:12])

    def _set_date_format(self) -> None:
        import datetime as _dt
        formats = ["%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%d", "%d.%m.%Y", "%b %d, %Y", "%d %B %Y"]
        today = _dt.date.today()
        labels = [today.strftime(f) for f in formats]
        cur = self.settings.get("date_format") or formats[0]
        idx = formats.index(cur) if cur in formats else 0
        choice, ok = QInputDialog.getItem(self, "Date format", "Dates look like:", labels, idx, False)
        if ok and choice in labels:
            self.settings.set("date_format", formats[labels.index(choice)])

    def _choose_stamp(self, name: str) -> None:
        self.opt.set_stamp(name)
        self.set_tool(T.STAMP)
        self.msg(f"Stamp: {name.replace('{date}', 'with today’s date')}. Click on the page to place it.")

    def _custom_stamp(self) -> None:
        text, ok = QInputDialog.getText(self, "Custom stamp", "Stamp text (use {date} for today's date):",
                                        text="Received {date}")
        if ok and text.strip():
            self._choose_stamp(text.strip())

    def _fill_signature_menu(self, menu: QMenu) -> None:
        menu.clear()
        for path in list_signatures()[:8]:
            act = menu.addAction(QIcon(QPixmap(path)), "", lambda p=path: self._use_signature(p))
            act.setCheckable(True)
            act.setChecked(path == self.opt.signature)
        if menu.actions():
            menu.addSeparator()
        menu.addAction("Manage signatures...", self.manage_signatures)

    def _use_signature(self, path: str) -> None:
        self.opt.set_signature(path)
        self.set_tool(T.SIGNATURE)
        self.msg("Click or drag on the page to place your signature.")

    def _build_statusbar(self) -> None:
        sb = self.statusBar()
        self.status_page = QLabel()
        self.status_zoom = QLabel()
        sb.addPermanentWidget(self.status_page)
        sb.addPermanentWidget(self.status_zoom)

    # ================================================================== state sync
    def _tab_changed(self, _index: int) -> None:
        tab = self.tab()
        is_doc = tab is not None
        for a in self.doc_actions:
            a.setEnabled(is_doc)
        self.export_menu.setEnabled(is_doc)
        self.tools_tb.setVisible(is_doc)
        self.form_bar.setVisible(is_doc and self.act["prepare_form"].isChecked())
        for w in self.page_widgets + [self.zoom_widget]:
            w.setVisible(is_doc)
        if not is_doc:
            self.home.reload()
        self._update_title()
        self._update_status()
        self._update_history()
        if tab:
            self.act["organize"].blockSignals(True)
            self.act["organize"].setChecked(tab.organizer_visible())
            self.act["organize"].blockSignals(False)
            self.act["sidebar"].blockSignals(True)
            self.act["sidebar"].setChecked(tab.sidebar.isVisible())
            self.act["sidebar"].blockSignals(False)
            self.act[f"layout_{tab.canvas.layout_mode}"].setChecked(True)
            tab.canvas.setFocus()
            self._annot_selected(tab.canvas.sel_annot)
        self._sync_tool_widgets()

    def _update_title(self) -> None:
        tab = self.tab()
        self.setWindowTitle(f"{tab.display_title()} - {APP_NAME}" if tab else APP_NAME)
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, DocumentTab):
                self.tabs.setTabText(i, w.display_title())
                self.tabs.setTabToolTip(i, ui.tip(w.pdf.path or "Not saved yet"))

    def _update_status(self) -> None:
        tab = self.tab()
        if not tab:
            self.status_page.setText("")
            self.status_zoom.setText("")
            return
        c = tab.canvas
        n = tab.pdf.page_count
        self.page_edit.setText(str(c.current_page + 1))
        self.page_total.setText(f" / {n} ")
        self.status_page.setText(f"Page {c.current_page + 1} of {n}")
        pct = f"{round(c.zoom * 100)}%"
        self.status_zoom.setText(pct)
        if not self.zoom_box.lineEdit().hasFocus():
            label = {"fit_width": "Fit width", "fit_page": "Fit page"}.get(c.zoom_mode or "", pct)
            self.zoom_box.setEditText(label)
        self.act["prev"].setEnabled(c.current_page > 0)
        self.act["next"].setEnabled(c.current_page < n - 1)
        if self.act["organize"].isChecked() != tab.organizer_visible():
            self.act["organize"].blockSignals(True)
            self.act["organize"].setChecked(tab.organizer_visible())
            self.act["organize"].blockSignals(False)

    def _update_history(self) -> None:
        tab = self.tab()
        can_u = bool(tab and tab.pdf.can_undo())
        can_r = bool(tab and tab.pdf.can_redo())
        self.act["undo"].setEnabled(can_u)
        self.act["redo"].setEnabled(can_r)
        self.act["undo"].setText(f"Undo {tab.pdf.undo_label().lower()}" if can_u else "Undo")
        self.act["redo"].setText(f"Redo {tab.pdf.redo_label().lower()}" if can_r else "Redo")
        self.act["undo"].setToolTip(self.act["undo"].text() + " (Ctrl+Z)")
        self.act["redo"].setToolTip(self.act["redo"].text() + " (Ctrl+Y)")
        self.act["delete_sel"].setEnabled(bool(tab and tab.canvas.sel_annot))

    def set_tool(self, tool: str) -> None:
        self.opt.set_tool(tool)
        self._tool_changed(tool)

    def _tool_changed(self, tool: str) -> None:
        for t, b in self.tool_buttons.items():
            b.setChecked(t == tool)
        if hasattr(self, "_form_select_btn"):
            self._form_select_btn.setChecked(tool == T.SELECT)
        if tool == T.SIGNATURE and not (self.opt.signature and os.path.exists(self.opt.signature)):
            QTimer.singleShot(0, self.manage_signatures)
        label, _ico, helptext = T.TOOLS[tool]
        self.tool_hint.setText(helptext if tool != T.SELECT else "")
        self._sync_tool_widgets()

    def _sync_tool_widgets(self) -> None:
        tool = self.opt.tool
        tab = self.tab()
        sa = tab.canvas.sel_annot if tab else None
        if tool == T.SELECT and sa and sa.get("widget"):
            vis = {"color": False, "fill": False, "width": False, "opacity": False, "font": False}
        elif tool == T.SELECT and sa:
            stroke = (sa.get("colors") or {}).get("stroke") or (sa.get("colors") or {}).get("fill")
            is_text = sa["type"] == fitz.PDF_ANNOT_FREE_TEXT
            if is_text and not sa.get("rich"):
                try:
                    page = tab.pdf.page(sa["pno"])
                    _size, col = annots.freetext_style(tab.pdf.doc, page.load_annot(sa["xref"]))
                    stroke = col
                except Exception:
                    pass
            show_color = (sa["type"] not in (fitz.PDF_ANNOT_STAMP, fitz.PDF_ANNOT_REDACT) and not sa.get("kind")
                          and not is_text) or sa.get("kind") in annots.MARK_KINDS
            if stroke:
                self.color_btn.set_color(rgb_to_hex(stroke))
            show_width = sa["type"] in (fitz.PDF_ANNOT_SQUARE, fitz.PDF_ANNOT_CIRCLE, fitz.PDF_ANNOT_LINE,
                                        fitz.PDF_ANNOT_INK, fitz.PDF_ANNOT_POLYGON, fitz.PDF_ANNOT_POLY_LINE) \
                and not sa.get("kind")
            if show_width:
                self.width_box.blockSignals(True)
                self.width_box.setValue(float((sa.get("border") or {}).get("width") or 1))
                self.width_box.blockSignals(False)
            self.opacity_box.blockSignals(True)
            op = sa.get("opacity")
            self.opacity_box.setValue(int((op if op is not None and op >= 0 else 1.0) * 100))
            self.opacity_box.blockSignals(False)
            show_fill = bool(sa.get("rich"))
            if show_fill:
                fill = (sa.get("colors") or {}).get("fill")
                self.fill_check.blockSignals(True)
                self.fill_check.setChecked(bool(fill))
                self.fill_check.blockSignals(False)
                if fill:
                    self.fill_btn.set_color(rgb_to_hex(fill))
            vis = {"color": show_color, "fill": show_fill, "width": show_width,
                   "opacity": not is_text or bool(sa.get("rich")), "font": False}
        else:
            self.color_btn.set_color(self.opt.color_hex(tool))
            vis = {"color": tool in T.COLOR_TOOLS, "fill": tool in T.FILL_TOOLS, "width": tool in T.WIDTH_TOOLS,
                   "opacity": tool in T.OPACITY_TOOLS, "font": tool in T.FONT_TOOLS}
            self.fill_check.blockSignals(True)
            self.fill_check.setChecked(self.opt.fill)
            self.fill_check.blockSignals(False)
            self.fill_btn.set_color(self.opt.fill_hex)
            self.width_box.blockSignals(True)
            self.width_box.setValue(self.opt.width)
            self.width_box.blockSignals(False)
            self.opacity_box.blockSignals(True)
            self.opacity_box.setValue(int(self.opt.opacity * 100))
            self.opacity_box.blockSignals(False)
        self.w_color.setVisible(vis["color"])
        self.w_fill.setVisible(vis["fill"])
        self.w_fill_color.setVisible(vis["fill"])
        self.fill_btn.setEnabled(self.fill_check.isChecked())
        self.w_width.setVisible(vis["width"])
        self.w_opacity.setVisible(vis["opacity"])
        self._sync_format_bar()
        self._update_history()

    def _sync_format_bar(self) -> None:
        """Show the text formatting toolbar while text is being added, edited or is selected."""
        tab = self.tab()
        bar = self.format_bar
        if tab is None:
            bar.hide()
            return
        canvas = tab.canvas
        ed = canvas.editor if isinstance(canvas.editor, RichEditor) else None
        sa = canvas.sel_annot
        tool = self.opt.tool
        if ed is not None:
            bar.set_rich(not ed.plain)
            bar.set_state(ed.state())
            bar.show()
        elif tool == T.SELECT and sa and sa["type"] == fitz.PDF_ANNOT_FREE_TEXT \
                and (sa.get("info") or {}).get("subject") != "Stamp":
            box = sa.get("box")
            if box is not None:
                first = box.first_run()
                para = box.paras[0] if box.paras else None
                bar.set_state({"font": first.font, "size": first.size, "bold": box.all_have("bold"),
                               "italic": box.all_have("italic"), "underline": box.all_have("underline"),
                               "strike": box.all_have("strike"), "color": first.color, "highlight": first.highlight,
                               "valign": first.valign, "align": para.align if para else "left",
                               "list": para.list if para else "", "spacing": para.spacing if para else 1.0})
            else:
                size, col = 12.0, (0, 0, 0)
                try:
                    page = tab.pdf.page(sa["pno"])
                    size, col = annots.freetext_style(tab.pdf.doc, page.load_annot(sa["xref"]))
                except Exception:
                    pass
                bar.set_state({"font": "Helvetica", "size": size, "color": rgb_to_hex(col)})
            bar.set_rich(True)
            bar.show()
        elif tool == T.TEXTBOX or (tool == T.FILLSIGN and self.opt.fillsign_mode == "text"):
            # shown as soon as the tool is picked, so the page doesn't jump down on the first click
            bar.set_rich(True)
            bar.set_state(self.opt.text_style())
            bar.show()
        elif tool == T.EDIT_TEXT:
            bar.set_rich(True)
            bar.set_state({})
            bar.show()
        else:
            bar.hide()

    def _rich_editor_changed(self, ed) -> None:
        if ed is not None:
            ed.format_changed.connect(self.format_bar.set_state)
        self._sync_tool_widgets()

    def _format_changed(self, change: dict) -> None:
        tab = self.tab()
        if "font" in change:
            self.opt.add_recent_font(change["font"])
            self.format_bar.font_box.set_recent(self.opt.recent_fonts())
        if tab is None:
            return
        canvas = tab.canvas
        if isinstance(canvas.editor, RichEditor):
            canvas.editor.apply(change)
            return
        if jobs.busy():
            return
        sa = canvas.sel_annot
        if self.opt.tool == T.SELECT and sa and sa["type"] == fitz.PDF_ANNOT_FREE_TEXT:
            canvas.format_selected(change)
            return
        if self.opt.tool == T.TEXTBOX:
            self.opt.set_text_style(change)
        elif self.opt.tool == T.EDIT_TEXT:
            self.msg("Click on the text you want to change first, then pick the formatting.")

    def _fill_toggled(self, on: bool) -> None:
        tab = self.tab()
        if tab and self.opt.tool == T.SELECT and tab.canvas.sel_annot and tab.canvas.sel_annot.get("rich"):
            tab.canvas.restyle_selected(fill=hex_to_rgb(self.fill_btn.color()) if on else None)
        else:
            self.opt.set_fill(on)

    def _fill_color_picked(self, color: str) -> None:
        tab = self.tab()
        if tab and self.opt.tool == T.SELECT and tab.canvas.sel_annot and tab.canvas.sel_annot.get("rich"):
            tab.canvas.restyle_selected(fill=hex_to_rgb(color))
        else:
            self.opt.set_fill_color(color)

    def _annot_selected(self, info) -> None:
        tab = self.tab()
        if info and tab and info.get("widget"):
            self.tool_hint.setText(f"{info['name']} \u201c{info.get('field_name', '')}\u201d selected. Drag to move, "
                                   "Delete to remove, double-click for options.")
        elif info and tab:
            self.tool_hint.setText(f"{info['name']} selected. Drag to move, Delete to remove"
                                   + (", double-click to edit" if info["type"] in (fitz.PDF_ANNOT_FREE_TEXT,
                                                                                   fitz.PDF_ANNOT_TEXT) else "") + ".")
        elif self.opt.tool == T.SELECT:
            self.tool_hint.setText("")
        self._sync_tool_widgets()

    def _color_picked(self, color: str) -> None:
        tab = self.tab()
        if tab and self.opt.tool == T.SELECT and tab.canvas.sel_annot:
            tab.canvas.restyle_selected(color=hex_to_rgb(color))
        else:
            self.opt.set_color(color)

    def _width_picked(self, value: float) -> None:
        tab = self.tab()
        if tab and self.opt.tool == T.SELECT and tab.canvas.sel_annot:
            tab.canvas.restyle_selected(width=value)
        else:
            self.opt.set_width(value)

    def _opacity_picked(self, value: int) -> None:
        tab = self.tab()
        if tab and self.opt.tool == T.SELECT and tab.canvas.sel_annot:
            tab.canvas.restyle_selected(opacity=value / 100)
        else:
            self.opt.set_opacity(value / 100)

    def _page_entered(self) -> None:
        tab = self.tab()
        if not tab:
            return
        try:
            n = int(self.page_edit.text())
        except ValueError:
            self._update_status()
            return
        tab.canvas.go_to_page(max(1, min(tab.pdf.page_count, n)) - 1)
        tab.canvas.setFocus()

    def _zoom_entered(self) -> None:
        tab = self.tab()
        if not tab:
            return
        text = self.zoom_box.currentText().strip().lower()
        if text.startswith("fit w"):
            tab.canvas.set_zoom(1, mode="fit_width")
        elif text.startswith("fit p"):
            tab.canvas.set_zoom(1, mode="fit_page")
        else:
            try:
                tab.canvas.set_zoom(float(text.rstrip("%").strip()) / 100)
            except ValueError:
                pass
        tab.canvas.setFocus()
        self._update_status()

    # ================================================================== opening
    def open_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Open", self.settings.get("last_dir"), convert.import_filter())
        if paths:
            self.settings.set("last_dir", os.path.dirname(paths[0]))
            self.open_paths(paths)

    def open_paths(self, paths: list[str]) -> None:
        for p in paths:
            self.open_path(p)

    @staticmethod
    def _looks_like_pdf(path: str) -> bool:
        try:
            with open(path, "rb") as fh:
                return b"%PDF" in fh.read(1024)
        except OSError:
            return False

    def open_path(self, path: str) -> None:
        if not path:
            return
        path = os.path.abspath(path)
        if not os.path.exists(path):
            if self.recents.get(path) and ui.question(
                    self, "File not found", f"{path}\n\nwas not found. Remove it from recent files?") \
                    == ui.Yes:
                self.recents.remove(path)
                self.home.reload()
            elif not self.recents.get(path):
                ui.warning(self, "Open", f"File not found:\n{path}")
            return
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, DocumentTab) and w.pdf.path and os.path.normcase(w.pdf.path) == os.path.normcase(path):
                self.tabs.setCurrentIndex(i)
                return
        if path.lower().endswith(".pdf") or self._looks_like_pdf(path):
            self._open_pdf(path)
        elif convert.can_import(path):
            stem = Path(path).stem
            suggested = os.path.join(os.path.dirname(path), stem + ".pdf")

            def done(data: bytes) -> None:
                pdf = PdfDocument.from_bytes(data, f"{stem}.pdf", suggested)
                self.add_document(pdf)
                self.msg(f"Converted {Path(path).name} to PDF. Save it to keep it.", 8000)

            jobs.run_job(self, f"Converting {Path(path).name}", convert.to_pdf_bytes, path, on_done=done)
        else:
            ui.warning(self, "Open", f"{APP_NAME} can't open {Path(path).suffix or 'this kind of'} files.")

    def _open_pdf(self, path: str) -> None:
        password = None
        for attempt in range(4):
            try:
                pdf = PdfDocument.open(path, password)
                break
            except NeedsPassword:
                label = (f"This file is password protected:\n{Path(path).name}\n\nEnter the password to open it:"
                         if attempt == 0 else f"That password didn't work. Try again:\n{Path(path).name}")
                password, ok = QInputDialog.getText(self, "Password needed", label, QLineEdit.EchoMode.Password)
                if not ok:
                    return
            except Exception as exc:
                ui.warning(self, "Open", f"Could not open {Path(path).name}:\n\n{exc}")
                return
        else:
            return
        self.add_document(pdf)
        entry = self.recents.get(path)
        tab = self.tab()
        if entry and self.settings.get("restore_page") and entry.get("page") and tab:
            page = min(int(entry["page"]), pdf.page_count - 1)
            QTimer.singleShot(50, lambda: tab.canvas.go_to_page(page))
        self._remember(pdf)

    def _thumb_png(self, pdf: PdfDocument) -> bytes | None:
        try:
            page = pdf.page(0)
            z = 300 / max(page.rect.width, page.rect.height)
            return page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False).tobytes("png")
        except Exception:
            return None

    def _remember(self, pdf: PdfDocument, page: int | None = None) -> None:
        if not pdf.path:
            return
        tab = next((t for t in self.doc_tabs() if t.pdf is pdf), None)
        cur = page if page is not None else (tab.canvas.current_page if tab else 0)
        self.recents.limit = int(self.settings.get("recents_limit") or 40)
        self.recents.add(pdf.path, cur, pdf.page_count, self._thumb_png(pdf))
        self.settings.set("last_dir", os.path.dirname(pdf.path))

    def add_document(self, pdf: PdfDocument) -> DocumentTab:
        tab = DocumentTab(pdf, self.opt, self.settings)
        c = tab.canvas
        c.message.connect(lambda m: self.msg(m))
        c.tool_reset_requested.connect(lambda: self.set_tool(T.SELECT))
        c.open_file_requested.connect(self.open_path)
        c.signature_needed.connect(self.manage_signatures)
        c.format_bar = self.format_bar
        c.digital_sign_requested.connect(lambda pno, rect, field, t=tab: self.tab() is t and
                                         self.sign_with_digital_id(pno, rect, field))
        tab.banner_button.clicked.connect(self.check_signatures)
        c.form_edit = self.act["prepare_form"].isChecked()
        c.rich_editor_changed.connect(lambda ed, t=tab: self.tab() is t and self._rich_editor_changed(ed))
        c.annot_selected.connect(lambda info, t=tab: self.tab() is t and self._annot_selected(info))
        c.selection_changed.connect(lambda _s: None)
        tab.status_changed.connect(lambda t=tab: self.tab() is t and self._update_status())
        tab.title_changed.connect(self._update_title)
        tab.comments.export_requested.connect(self.export_comments)
        tab.bookmarks.auto_requested.connect(self.auto_bookmarks)
        tab.attachments.add_requested.connect(self.attach_file)
        tab.attachments.save_requested.connect(self.save_attachment)
        tab.attachments.delete_requested.connect(self.delete_attachment)
        tab.attachments.open_pdf_requested.connect(self.open_attachment)
        pdf.history_changed.connect(lambda t=tab: self.tab() is t and self._update_history())
        idx = self.tabs.addTab(tab, icons.icon("file-text"), tab.display_title())
        close = QToolButton()
        close.setObjectName("tabClose")
        icons.bind(close, "x")
        close.setIconSize(QSize(14, 14))
        close.setToolTip("Close (Ctrl+W)")
        close.clicked.connect(lambda _=False, t=tab: self.close_tab(self.tabs.indexOf(t)))
        self.tabs.tabBar().setTabButton(idx, QTabBar.ButtonPosition.RightSide, close)
        self.tabs.setCurrentIndex(idx)
        tab.canvas.setFocus()
        return tab

    def _fill_recent_menu(self) -> None:
        self.recent_menu.clear()
        entries = self.recents.entries()[:15]
        for e in entries:
            act = self.recent_menu.addAction(e.get("name") or Path(e["path"]).name, lambda p=e["path"]: self.open_path(p))
            act.setToolTip(ui.tip(e["path"]))
            act.setEnabled(os.path.exists(e["path"]))
        if not entries:
            act = self.recent_menu.addAction("No recent files")
            act.setEnabled(False)
        else:
            self.recent_menu.addSeparator()
            self.recent_menu.addAction("Clear list", self.clear_recents)

    def clear_recents(self) -> None:
        if ui.question(self, "Recent files", "Clear the recent files list? Pinned files are kept.") \
                == ui.Yes:
            self.recents.clear(keep_pinned=True)
            self.home.reload()

    def _home_action(self, key: str) -> None:
        {"open": self.open_dialog, "create": self.create_pdf, "combine": self.combine, "blank": self.new_blank,
         "clipboard": self.from_clipboard, "clear_recents": self.clear_recents}[key]()

    # ================================================================== creating
    def new_blank(self) -> None:
        dlg = dialogs.BlankDialog(self)
        if dlg.exec():
            self.add_document(PdfDocument.from_bytes(dlg.make(), "Untitled.pdf"))

    def _convert_files(self, title: str, files: list[str], image_page: str, name: str) -> None:
        if len(files) == 1 and files[0].lower().endswith(".pdf"):
            self.open_path(files[0])
            return
        folder = os.path.dirname(files[0])

        def done(data: bytes) -> None:
            pdf = PdfDocument.from_bytes(data, name, os.path.join(folder, name))
            self.add_document(pdf)
            self.msg(f"Created a {pdf.page_count}-page PDF. Save it to keep it.", 8000)

        jobs.run_job(self, title, convert.files_to_pdf_bytes, files, image_page=image_page, on_done=done)

    def create_pdf(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Create PDF from", self.settings.get("last_dir"),
                                                convert.import_filter())
        if not paths:
            return
        self.settings.set("last_dir", os.path.dirname(paths[0]))
        if len(paths) == 1:
            self.open_path(paths[0])
            return
        dlg = dialogs.CombineDialog(self, "Create PDF", paths, self.settings.get("last_dir"), "Create")
        if dlg.exec():
            files = dlg.files()
            self._convert_files("Creating PDF", files, dlg.paper_key(), Path(files[0]).stem + ".pdf")

    def combine(self) -> None:
        dlg = dialogs.CombineDialog(self, "Combine files", [], self.settings.get("last_dir"))
        if dlg.exec():
            self.settings.set("last_dir", dlg.last_dir or self.settings.get("last_dir"))
            self._convert_files("Combining files", dlg.files(), dlg.paper_key(), "Combined.pdf")

    def from_clipboard(self) -> None:
        cb = QGuiApplication.clipboard()
        md = cb.mimeData()
        if md is None:
            return
        if md.hasUrls() and any(u.isLocalFile() for u in md.urls()):
            self.open_paths([u.toLocalFile() for u in md.urls() if u.isLocalFile()])
            return
        if md.hasImage():
            img = cb.image()
            if not img.isNull():
                ba = QByteArray()
                buf = QBuffer(ba)
                buf.open(QIODevice.OpenModeFlag.WriteOnly)
                img.save(buf, "PNG")
                data = convert.images_to_pdf_bytes([bytes(ba.data())])
                self.add_document(PdfDocument.from_bytes(data, "Clipboard.pdf"))
                return
        text = cb.text()
        if text.strip():
            self.add_document(PdfDocument.from_bytes(convert.text_to_pdf_bytes(text, mono=False), "Clipboard.pdf"))
            return
        ui.information(self, "Clipboard", "The clipboard is empty. Copy an image or some text first.")

    # ================================================================== saving & closing
    @_with_tab
    def save(self, tab: DocumentTab) -> bool:
        return self._save_tab(tab)

    def _save_tab(self, tab: DocumentTab, as_new: bool = False) -> bool:
        tab.canvas.commit_editor()
        pdf = tab.pdf
        problem = self._signature_problem(pdf) if pdf.dirty else ""
        if problem:
            box = QMessageBox(QMessageBox.Icon.Warning, "Digital signatures",
                              f"This PDF is digitally signed. {problem}\n\nSave it as a new file to keep the "
                              "signed original?", parent=self)
            box.setTextFormat(Qt.TextFormat.PlainText)
            new_btn = box.addButton("Save as new file", QMessageBox.ButtonRole.AcceptRole)
            anyway = box.addButton("Save anyway", QMessageBox.ButtonRole.DestructiveRole)
            box.addButton(QMessageBox.StandardButton.Cancel)
            box.setDefaultButton(new_btn)
            box.exec()
            if box.clickedButton() is new_btn:
                as_new = True
            elif box.clickedButton() is not anyway:
                return False
        target = pdf.path
        if as_new or pdf.is_new or not target:
            suggestion = pdf.suggested_path or os.path.join(self.settings.get("last_dir"), pdf.title)
            if as_new and pdf.path:
                suggestion = pdf.path
            target, _ = QFileDialog.getSaveFileName(self, "Save as", suggestion, "PDF files (*.pdf)")
            if not target:
                return False
            if not target.lower().endswith(".pdf"):
                target += ".pdf"
        try:
            pdf.save(target)
        except PermissionError:
            ui.warning(self, "Save", f"Can't write to\n{target}\n\nThe file may be open in another program "
                                              "or the folder may be read-only. Try Save as with a different name.")
            return False
        except Exception as exc:
            ui.warning(self, "Save", f"Saving failed:\n\n{exc}")
            return False
        self._remember(pdf)
        self._update_title()
        self.msg(f"Saved {Path(target).name}")
        return True

    @_with_tab
    def save_as(self, tab: DocumentTab) -> bool:
        return self._save_tab(tab, as_new=True)

    @_with_tab
    def save_copy(self, tab: DocumentTab) -> None:
        base = self.doc_base(tab.pdf)
        problem = self._signature_problem(tab.pdf)
        if problem and ui.question(self, "Save a copy", f"This PDF is digitally signed. {problem}\n\nSave the copy "
                                                        "anyway?", default=ui.No) != ui.Yes:
            return
        target, _ = QFileDialog.getSaveFileName(self, "Save a copy", f"{base} copy.pdf", "PDF files (*.pdf)")
        if target:
            if not target.lower().endswith(".pdf"):
                target += ".pdf"
            try:
                tab.pdf.save_copy(target)
                self.msg(f"Saved a copy as {Path(target).name}")
            except Exception as exc:
                ui.warning(self, "Save a copy", str(exc))

    def _signature_problem(self, pdf: PdfDocument) -> str:
        """Why saving the document as it is now would make its digital signatures show as invalid,
        in plain words ("" when it wouldn't)."""
        if not pdf.signed_bytes:
            return ""
        if pdf.will_break_signatures():
            return ("One of the changes you made (such as reducing the file size, recognizing text or converting "
                    "colors) rewrites the whole file, so the signatures will no longer be valid.")
        from pdfdesk import digitalid
        if not digitalid.available() or not pdf.base_bytes:
            return ""
        from pdfdesk import sigdiff
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            return sigdiff.update_problem(pdf.base_bytes, pdf.save_bytes(), pdf.password)
        except Exception:
            return ""
        finally:
            QApplication.restoreOverrideCursor()

    def _confirm_close(self, tab: DocumentTab) -> bool:
        tab.canvas.commit_editor()
        if not tab.pdf.dirty:
            return True
        self.tabs.setCurrentWidget(tab)
        box = QMessageBox(QMessageBox.Icon.Question, "Unsaved changes",
                          f"Save the changes to {tab.pdf.title} before closing?", parent=self)
        box.setTextFormat(Qt.TextFormat.PlainText)
        save = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("Don't save", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(save)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save:
            return self._save_tab(tab)
        return clicked is discard

    def close_tab(self, index: int) -> None:
        w = self.tabs.widget(index)
        if not isinstance(w, DocumentTab):
            return
        if jobs.busy():
            self.msg("Wait for the running task to finish before closing this tab.")
            return
        if not self._confirm_close(w):
            return
        if w.pdf.path:
            self.recents.set_page(w.pdf.path, w.canvas.current_page)
        self.tabs.removeTab(index)
        w.pdf.close()
        w.deleteLater()

    def closeEvent(self, ev) -> None:
        if jobs.busy():
            ui.information(self, APP_NAME, "A task is still running. Wait for it to finish or cancel "
                                                    "it, then close the window.")
            ev.ignore()
            return
        for tab in self.doc_tabs():
            if not self._confirm_close(tab):
                ev.ignore()
                return
        for tab in self.doc_tabs():
            if tab.pdf.path:
                self.recents.set_page(tab.pdf.path, tab.canvas.current_page)
        self.settings.data["window_geometry"] = bytes(self.saveGeometry().toBase64()).decode()
        self.settings.data["window_state"] = bytes(self.saveState().toBase64()).decode()
        self.settings.save()
        ev.accept()

    def _restore_window(self) -> None:
        geo = self.settings.get("window_geometry")
        if geo:
            try:
                self.restoreGeometry(QByteArray.fromBase64(geo.encode()))
            except Exception:
                pass

    def dragEnterEvent(self, ev) -> None:
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:
        paths = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.open_paths(paths)

    # ================================================================== edit & view
    @_with_tab
    def undo(self, tab: DocumentTab) -> None:
        tab.pdf.undo()

    @_with_tab
    def redo(self, tab: DocumentTab) -> None:
        tab.pdf.redo()

    def copy(self) -> None:
        focus = QApplication.focusWidget()
        if isinstance(focus, QLineEdit) or (focus is not None and (focus.inherits("QPlainTextEdit")
                                                                     or focus.inherits("QTextEdit"))):
            focus.copy()
            return
        tab = self.tab()
        if tab and not tab.canvas.copy_selection():
            self.msg("Select some text first.")

    @_with_tab
    def select_all(self, tab: DocumentTab) -> None:
        focus = QApplication.focusWidget()
        if isinstance(focus, QLineEdit):
            focus.selectAll()
            return
        tab.canvas.select_all_on_page()

    @_with_tab
    def find(self, tab: DocumentTab) -> None:
        tab.open_find()

    @_with_tab
    def delete_selected(self, tab: DocumentTab) -> None:
        tab.canvas.delete_selected_annot()

    @_with_tab
    def goto_page(self, tab: DocumentTab) -> None:
        n, ok = QInputDialog.getInt(self, "Go to page", f"Page (1-{tab.pdf.page_count}):",
                                    tab.canvas.current_page + 1, 1, tab.pdf.page_count)
        if ok:
            tab.canvas.go_to_page(n - 1)

    @_with_tab
    def prev_page(self, tab: DocumentTab) -> None:
        tab.canvas.go_to_page(max(0, tab.canvas.current_page - (2 if tab.canvas.layout_mode != "single" else 1)))

    @_with_tab
    def next_page(self, tab: DocumentTab) -> None:
        step = 2 if tab.canvas.layout_mode != "single" else 1
        tab.canvas.go_to_page(min(tab.pdf.page_count - 1, tab.canvas.current_page + step))

    def toggle_sidebar(self, on: bool) -> None:
        tab = self.tab()
        if tab:
            tab.toggle_sidebar(on)

    def toggle_night(self, on: bool) -> None:
        self.settings.set("night_mode", on)
        for tab in self.doc_tabs():
            tab.canvas.set_night(on)
            tab.thumbs.set_night(on)
            if tab.organizer is not None:
                tab.organizer.grid.set_night(on)

    def toggle_forms(self, on: bool) -> None:
        self.settings.set("highlight_forms", on)
        for tab in self.doc_tabs():
            tab.canvas.set_highlight_forms(on)

    def toggle_fullscreen(self, on: bool) -> None:
        if on:
            self.showFullScreen()
        else:
            self.showNormal()

    def set_layout(self, mode: str) -> None:
        tab = self.tab()
        if tab:
            tab.canvas.set_layout(mode)

    def set_theme(self, mode: str) -> None:
        self.settings.set("theme", mode)
        theme.apply_theme(QApplication.instance(), mode)
        icons.refresh_all()
        if hasattr(self, "format_bar"):
            self.format_bar.color._refresh()
            self.format_bar.highlight._refresh()
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            self.tabs.setTabIcon(i, icons.icon("house" if w is self.home else "file-text"))
            if isinstance(w, DocumentTab):
                for k, ico in enumerate(("gallery-vertical", "bookmark", "message-square-text", "paperclip",
                                         "layers", "search")):
                    w.side_tabs.setTabIcon(k, icons.icon(ico))
                w.canvas.update()
        self.home.reload()
        self.update()

    def toggle_organizer(self, on: bool) -> None:
        tab = self.tab()
        if tab:
            tab.show_organizer(on)

    def preferences(self) -> None:
        dlg = dialogs.SettingsDialog(self, self.settings)
        if dlg.exec():
            old_theme = self.settings.get("theme")
            dlg.save()
            if self.settings.get("theme") != old_theme:
                self.act[f"theme_{self.settings.get('theme')}"].setChecked(True)
            for tab in self.doc_tabs():
                tab.canvas.set_highlight_forms(bool(self.settings.get("highlight_forms")))
            self.act["forms"].setChecked(bool(self.settings.get("highlight_forms")))
            self.recents.limit = int(self.settings.get("recents_limit") or 40)

    # ================================================================== pages
    @_with_tab
    def page_action(self, tab: DocumentTab, action: str) -> None:
        pages = tab.organizer.grid.selected_pages() if tab.organizer_visible() else [tab.canvas.current_page]
        tab.page_action(action, pages)

    @_with_tab
    def rotate_pages(self, tab: DocumentTab) -> None:
        dlg = dialogs.PagesDialog(self, "Rotate pages", "Pages to rotate:", tab.pdf.page_count,
                                  tab.canvas.current_page, "all",
                                  options=[("ccw", "Counterclockwise (instead of clockwise)", False)],
                                  ok_text="Rotate")
        if dlg.exec():
            tab.page_action("rotate_ccw" if dlg.option("ccw") else "rotate_cw", dlg.result_pages())

    @_with_tab
    def delete_pages(self, tab: DocumentTab) -> None:
        pages = tab.ask_pages("Delete pages", "Pages to delete:", "current", "Delete")
        if pages:
            tab.page_action("delete", pages)

    @_with_tab
    def crop(self, tab: DocumentTab) -> None:
        dlg = dialogs.CropDialog(self, tab.pdf, tab.canvas.current_page)
        if not dlg.exec():
            return
        try:
            with tab.pdf.edit("Crop pages", structure=True):
                if dlg.reset_requested:
                    pdfops.reset_crop(tab.pdf.doc, dlg._pages)
                else:
                    pdfops.crop_pages(tab.pdf.doc, dlg._pages, *dlg.margins_pt())
        except Exception as exc:
            ui.warning(self, "Crop pages", str(exc))

    @_with_tab
    def header_footer(self, tab: DocumentTab) -> None:
        dlg = dialogs.HeaderFooterDialog(self, tab.pdf, tab.canvas.current_page)
        if dlg.exec():
            try:
                with tab.pdf.edit("Add header and footer", pages=dlg._pages):
                    dlg.apply(tab.pdf.doc, dlg._pages, tab.pdf.title)
            except Exception as exc:
                ui.warning(self, "Header and footer", str(exc))

    @_with_tab
    def watermark(self, tab: DocumentTab) -> None:
        dlg = dialogs.WatermarkDialog(self, tab.pdf, tab.canvas.current_page)
        if dlg.exec():
            try:
                with tab.pdf.edit("Add watermark", pages=dlg._pages):
                    dlg.apply(tab.pdf.doc, dlg._pages)
            except Exception as exc:
                ui.warning(self, "Watermark", str(exc))

    @_with_tab
    def add_bookmark(self, tab: DocumentTab) -> None:
        tab.toggle_sidebar(True)
        self.act["sidebar"].setChecked(True)
        tab.side_tabs.setCurrentWidget(tab.bookmarks)
        tab.bookmarks.add()

    # ================================================================== document tools
    @_with_tab
    def properties(self, tab: DocumentTab) -> None:
        dlg = dialogs.PropertiesDialog(self, tab.pdf, tab.canvas.current_page)
        if dlg.exec():
            meta = dlg.metadata()
            if any((tab.pdf.doc.metadata or {}).get(k, "") != v for k, v in meta.items()):
                with tab.pdf.edit("Edit properties", pages=[]):
                    tab.pdf.doc.set_metadata(meta)

    @_with_tab
    def show_in_folder(self, tab: DocumentTab) -> None:
        if tab.pdf.path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(tab.pdf.path)))
        else:
            self.msg("This PDF hasn't been saved yet.")

    @_with_tab
    def print_doc(self, tab: DocumentTab) -> None:
        printing.print_document(self, tab.pdf, tab.canvas.current_page)

    @_with_tab
    def export(self, tab: DocumentTab, fmt: str = "docx") -> None:
        dlg = dialogs.ExportDialog(self, tab.pdf, tab.canvas.current_page, fmt)
        if not dlg.exec():
            return
        p = dlg.params()
        data = tab.pdf.plain_bytes()

        def done(paths: list[str]) -> None:
            target = paths[0] if len(paths) == 1 else os.path.dirname(paths[0]) if paths else p["target"]
            box = QMessageBox(QMessageBox.Icon.Information, "Export",
                              f"Exported {len(paths)} file(s).\n\n{target}", parent=self)
            box.setTextFormat(Qt.TextFormat.PlainText)
            open_btn = box.addButton("Open", QMessageBox.ButtonRole.ActionRole)
            folder_btn = box.addButton("Show folder", QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Close)
            box.exec()
            if box.clickedButton() is open_btn:
                QDesktopServices.openUrl(QUrl.fromLocalFile(target))
            elif box.clickedButton() is folder_btn:
                QDesktopServices.openUrl(QUrl.fromLocalFile(target if os.path.isdir(target) else os.path.dirname(target)))

        jobs.run_job(self, "Exporting", convert.export_pdf, data, p["fmt"], p["target"], p["pages"], dpi=p["dpi"],
                     quality=p["quality"], page_markers=p["page_markers"], on_done=done)

    @_with_tab
    def compress(self, tab: DocumentTab) -> None:
        dlg = dialogs.CompressDialog(self, tab.pdf)
        if not dlg.exec():
            return
        p = dlg.params()
        before = len(tab.pdf.doc.tobytes(garbage=0))

        def done(data: bytes) -> None:
            if len(data) >= before:
                ui.information(self, "Reduce file size", "This PDF is already about as small as it gets, "
                                                                  "nothing was changed.")
                return
            tab.pdf.replace_with_bytes(data, "Reduce file size")
            self.msg(f"Reduced from {pdfops.file_size_text(before)} to {pdfops.file_size_text(len(data))} "
                     f"({100 - len(data) * 100 // max(1, before)}% smaller). Save to keep it.", 10000)

        jobs.run_job(self, "Reducing file size", pdfops.compress_bytes, tab.pdf.doc.tobytes(), tab.pdf.password,
                     p["level"], p["dpi"], p["quality"], on_done=done)

    @_with_tab
    def protect(self, tab: DocumentTab) -> None:
        dlg = dialogs.ProtectDialog(self)
        if not dlg.exec():
            return
        p = dlg.params()
        try:
            data = pdfops.protect_bytes(tab.pdf.doc, **p)
            tab.pdf.replace_with_bytes(data, "Add password", password=p["owner_pw"] or p["user_pw"])
        except Exception as exc:
            ui.warning(self, "Protect", str(exc))
            return
        if self._save_tab(tab):
            self.msg("Password protection added and saved.", 8000)

    @_with_tab
    def unprotect(self, tab: DocumentTab) -> None:
        if not tab.pdf.doc.is_encrypted and not (tab.pdf.doc.metadata or {}).get("encryption"):
            ui.information(self, "Remove password", "This PDF isn't password protected.")
            return
        if ui.question(self, "Remove password", "Remove the password and restrictions from this PDF?") \
                != ui.Yes:
            return
        data = tab.pdf.doc.tobytes(garbage=1, deflate=True, encryption=fitz.PDF_ENCRYPT_NONE)
        tab.pdf.replace_with_bytes(data, "Remove password")
        tab.pdf.password = None
        self.msg("Password removed. Save to keep the change.", 8000)

    @_with_tab
    def ocr(self, tab: DocumentTab) -> None:
        dlg = dialogs.OcrDialog(self, tab.pdf, tab.canvas.current_page, self.settings)
        if not dlg.exec():
            return
        p = dlg.params()

        def done(result) -> None:
            data, count = result
            if not count:
                ui.information(self, "Recognize text", "No pages needed text recognition (they already "
                                                                "have text, or no words were found).")
                return
            tab.pdf.replace_with_bytes(data, "Recognize text")
            self.msg(f"Recognized text on {count} page(s). You can now search and select it.", 10000)

        jobs.run_job(self, "Recognizing text", pdfops.ocr_bytes, tab.pdf.doc.tobytes(), tab.pdf.password,
                     p["language"], p["tessdata"], p["dpi"], p["pages"], p["skip_text_pages"], on_done=done)

    @_with_tab
    def find_redact(self, tab: DocumentTab) -> None:
        dlg = dialogs.FindRedactDialog(self, tab.pdf, tab.canvas.current_page)
        if not dlg.exec():
            return
        count = 0
        try:
            with tab.pdf.edit("Mark redactions", pages=dlg._pages):
                for search in dlg.searches():
                    count += pdfops.mark_redactions(tab.pdf.doc, dlg._pages, needle=search.get("needle", ""),
                                                    regex=search.get("regex"), whole_word=search.get("whole_word", False))
                if count == 0:
                    raise _NoChange()
                if dlg.apply_now.isChecked():
                    pdfops.apply_redactions(tab.pdf.doc)
        except _NoChange:
            ui.information(self, "Find and redact", "Nothing matched.")
            return
        except Exception as exc:
            ui.warning(self, "Find and redact", str(exc))
            return
        if dlg.apply_now.isChecked():
            self.msg(f"Redacted {count} match(es). Save to keep the change.", 10000)
        else:
            self.msg(f"Marked {count} match(es). Check them, then use Apply redactions.", 10000)

    @_with_tab
    def apply_redactions(self, tab: DocumentTab) -> None:
        pages = pdfops.redaction_pages(tab.pdf.doc)
        if not pages:
            ui.information(self, "Apply redactions", "Nothing is marked for redaction. Use the Redact tool "
                                                              "or Find and redact first.")
            return
        if ui.question(self, "Apply redactions",
                                f"Permanently remove everything marked for redaction on {len(pages)} page(s)?\n\n"
                                "Text, images and drawings under the marks are deleted from the file.") \
                != ui.Yes:
            return
        with tab.pdf.edit("Apply redactions", pages=pages):
            pdfops.apply_redactions(tab.pdf.doc)
        self.msg("Redactions applied. Save to keep the change.", 8000)

    @_with_tab
    def flatten(self, tab: DocumentTab) -> None:
        if ui.question(self, "Flatten", "Turn all comments, markup and form fields into regular page "
                                                 "content? They can't be edited afterwards.") \
                != ui.Yes:
            return
        with tab.pdf.edit("Flatten", structure=True):
            pdfops.flatten(tab.pdf.doc)

    @_with_tab
    def remove_hidden(self, tab: DocumentTab) -> None:
        if ui.question(self, "Remove hidden information",
                                "Remove metadata (author, title...), JavaScript, attached files, hidden text and "
                                "thumbnails from this PDF?") != ui.Yes:
            return
        with tab.pdf.edit("Remove hidden information", structure=True):
            pdfops.remove_hidden_data(tab.pdf.doc)
        self.msg("Hidden information removed. Save to keep the change.", 8000)

    # ================================================================== document features
    @_with_tab
    def page_labels(self, tab: DocumentTab) -> None:
        dlg = dialogs.PageLabelsDialog(self, tab.pdf.page_count, docfeatures.get_labels(tab.pdf.doc))
        if dlg.exec():
            try:
                with tab.pdf.edit("Page labels", structure=True):
                    docfeatures.set_labels(tab.pdf.doc, dlg.rules())
            except Exception as exc:
                ui.warning(self, "Page labels", str(exc))

    @_with_tab
    def background(self, tab: DocumentTab) -> None:
        dlg = dialogs.BackgroundDialog(self, tab.pdf, tab.canvas.current_page)
        if dlg.exec():
            try:
                with tab.pdf.edit("Add background", pages=dlg._pages):
                    dlg.apply(tab.pdf.doc, dlg._pages)
            except Exception as exc:
                ui.warning(self, "Background", str(exc))

    @_with_tab
    def page_size(self, tab: DocumentTab) -> None:
        dlg = dialogs.PageSizeDialog(self, tab.pdf, tab.canvas.current_page)
        if dlg.exec():
            w, h = dlg.size_pt()
            try:
                with tab.pdf.edit("Change page size", structure=True):
                    docfeatures.resize_pages(tab.pdf.doc, dlg._pages, w, h, dlg.mode_key())
            except Exception as exc:
                ui.warning(self, "Page size", str(exc))

    @_with_tab
    def remove_blank_pages(self, tab: DocumentTab) -> None:
        def done(blank: list[int]) -> None:
            if not blank:
                ui.information(self, "Remove blank pages", "No blank pages were found.")
                return
            if len(blank) >= tab.pdf.page_count:
                ui.information(self, "Remove blank pages", "Every page looks blank, so nothing was removed.")
                return
            if ui.question(self, "Remove blank pages",
                           f"Found {len(blank)} blank page(s): {pdfops.format_page_list(blank)}.\n\nRemove them?") \
                    != ui.Yes:
                return
            with tab.pdf.edit("Remove blank pages", structure=True):
                pdfops.delete_pages(tab.pdf.doc, blank)
            self.msg(f"Removed {len(blank)} blank page(s).")

        jobs.run_job(self, "Looking for blank pages", docfeatures.find_blank_pages, tab.pdf.doc.tobytes(),
                     tab.pdf.password, on_done=done)

    @_with_tab
    def print_layout(self, tab: DocumentTab, booklet: bool = False) -> None:
        dlg = dialogs.PrintLayoutDialog(self, booklet)
        if not dlg.exec():
            return
        p = dlg.params()
        base = self.doc_base(tab.pdf)
        stem = base.name
        name = f"{stem} - booklet.pdf" if p["kind"] == "booklet" else f"{stem} - {p['kind']} per sheet.pdf"
        folder = str(base.parent)

        def done(data: bytes) -> None:
            self.add_document(PdfDocument.from_bytes(data, name, os.path.join(folder, name)))
            self.msg("Made a new PDF for printing. Save it to keep it.", 8000)

        data = tab.pdf.plain_bytes()
        if p["kind"] == "booklet":
            jobs.run_job(self, "Making a booklet", docfeatures.booklet_bytes, data, p["paper"], on_done=done)
        else:
            jobs.run_job(self, "Arranging pages", docfeatures.nup_bytes, data, p["kind"], p["paper"],
                         borders=p["borders"], on_done=done)

    @_with_tab
    def grayscale(self, tab: DocumentTab) -> None:
        if ui.question(self, "Convert to grayscale", "Turn every color in this PDF (text, drawings and pictures) "
                                                     "into shades of gray?") != ui.Yes:
            return

        def done(data: bytes) -> None:
            tab.pdf.replace_with_bytes(data, "Convert to grayscale")
            self.msg("Converted to grayscale. Save to keep the change.", 8000)

        jobs.run_job(self, "Converting to grayscale", docfeatures.grayscale_bytes, tab.pdf.doc.tobytes(),
                     tab.pdf.password, on_done=done)

    @_with_tab
    def extract_images(self, tab: DocumentTab) -> None:
        base = tab.pdf.path or tab.pdf.suggested_path or ""
        folder = QFileDialog.getExistingDirectory(self, "Save the pictures in", os.path.dirname(base) or
                                                  self.settings.get("last_dir"))
        if not folder:
            return

        def done(paths: list[str]) -> None:
            if not paths:
                ui.information(self, "Save all pictures", "This PDF has no pictures to save.")
                return
            box = QMessageBox(QMessageBox.Icon.Information, "Save all pictures",
                              f"Saved {len(paths)} picture(s) in\n{folder}", parent=self)
            box.setTextFormat(Qt.TextFormat.PlainText)
            open_btn = box.addButton("Show folder", QMessageBox.ButtonRole.ActionRole)
            box.addButton(QMessageBox.StandardButton.Ok)
            box.exec()
            if box.clickedButton() is open_btn:
                QDesktopServices.openUrl(QUrl.fromLocalFile(folder))

        jobs.run_job(self, "Saving pictures", docfeatures.extract_images, tab.pdf.plain_bytes(), folder,
                     self.doc_base(tab.pdf).name, on_done=done)

    @_with_tab
    def export_comments(self, tab: DocumentTab) -> None:
        base = self.doc_base(tab.pdf)
        path, chosen = QFileDialog.getSaveFileName(self, "Comment summary", str(base) + " comments.md",
                                                   "Markdown (*.md);;CSV for spreadsheets (*.csv)")
        if not path:
            return
        if not path.lower().endswith((".md", ".csv")):
            path += ".csv" if chosen.startswith("CSV") else ".md"
        try:
            count = docfeatures.export_comments(tab.pdf.doc, path, tab.pdf.title)
        except Exception as exc:
            ui.warning(self, "Comment summary", str(exc))
            return
        self.msg(f"Saved {count} comment(s) to {Path(path).name}")

    @_with_tab
    def attach_file(self, tab: DocumentTab) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Attach a file", self.settings.get("last_dir"), "All files (*)")
        if not path:
            return
        try:
            with tab.pdf.edit("Attach file", pages=[]):
                name = docfeatures.add_attachment(tab.pdf.doc, path)
        except Exception as exc:
            ui.warning(self, "Attach a file", str(exc))
            return
        tab.toggle_sidebar(True)
        self.act["sidebar"].setChecked(True)
        tab.side_tabs.setCurrentWidget(tab.attachments)
        tab.attachments.reload()
        self.msg(f"Attached {name}. Save the PDF to keep it.")

    def save_attachment(self, item: dict) -> None:
        tab = self.tab()
        if tab is None or jobs.busy():
            return
        from pdfdesk import safety
        name = safety.safe_filename_part(item["name"]) or "attachment"
        path, _ = QFileDialog.getSaveFileName(self, "Save attachment", os.path.join(self.settings.get("last_dir"), name),
                                              "All files (*)")
        if not path:
            return
        try:
            data = docfeatures.attachment_bytes(tab.pdf.doc, item)
            with open(path, "wb") as fh:
                fh.write(data)
        except Exception as exc:
            ui.warning(self, "Save attachment", str(exc))
            return
        self.msg(f"Saved {Path(path).name}. PDF Desk never opens attached files for you; check them before use.")

    def delete_attachment(self, item: dict) -> None:
        tab = self.tab()
        if tab is None or jobs.busy():
            return
        if ui.question(self, "Remove attachment", f"Remove the attached file \u201c{item['name']}\u201d from this PDF?") \
                != ui.Yes:
            return
        try:
            with tab.pdf.edit("Remove attachment", pages=[] if item["page"] is None else [item["page"]]):
                docfeatures.delete_attachment(tab.pdf.doc, item)
        except Exception as exc:
            ui.warning(self, "Remove attachment", str(exc))
            return
        tab.attachments.reload()

    def open_attachment(self, item: dict) -> None:
        """Attached PDFs open in a new tab (from memory). Other kinds of files are never opened."""
        tab = self.tab()
        if tab is None or jobs.busy():
            return
        try:
            data = docfeatures.attachment_bytes(tab.pdf.doc, item)
        except Exception as exc:
            ui.warning(self, "Open attachment", str(exc))
            return
        if not data.lstrip()[:5].startswith(b"%PDF") and b"%PDF" not in data[:1024]:
            ui.information(self, "Open attachment", "That attachment isn't a PDF, so it can only be saved.")
            return
        try:
            from pdfdesk import safety
            title = safety.safe_filename_part(Path(item["name"]).name) or "attachment"
            if not title.lower().endswith(".pdf"):
                title += ".pdf"
            pdf = PdfDocument.from_bytes(data, title)
        except Exception as exc:
            ui.warning(self, "Open attachment", f"The attached PDF couldn't be opened:\n\n{exc}")
            return
        self.add_document(pdf)

    # ================================================================== reading & comparing
    @_with_tab
    def present(self, tab: DocumentTab) -> None:
        from pdfdesk.present import PresentationWindow
        self._presentation = PresentationWindow(tab.pdf, tab.canvas.current_page, bool(self.settings.get("night_mode")))
        self._presentation.destroyed.connect(lambda *_: setattr(self, "_presentation", None))
        self._presentation.start()

    @_with_tab
    def reader_view(self, tab: DocumentTab) -> None:
        from pdfdesk import reader

        def done(body: str) -> None:
            dlg = reader.ReaderView(self, tab.pdf.title, body)
            dlg.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            dlg.show()
            QTimer.singleShot(0, lambda: dlg.view.scrollToAnchor(f"p{tab.canvas.current_page + 1}"))

        jobs.run_job(self, "Preparing reader view", lambda data, pw, progress=None:
                     reader.document_html(pdfops.open_bytes(data, pw), progress),
                     tab.pdf.doc.tobytes(), tab.pdf.password, on_done=done)

    @_with_tab
    def auto_bookmarks(self, tab: DocumentTab) -> None:
        jobs.run_job(self, "Looking for headings", docfeatures.headings_toc_bytes, tab.pdf.doc.tobytes(),
                     tab.pdf.password, on_done=lambda toc: self._apply_headings(tab, toc))

    def _apply_headings(self, tab: DocumentTab, toc: list) -> None:
        if tab not in self.doc_tabs():
            return
        if not toc:
            ui.information(self, "Bookmarks from headings", "No headings were found. They are found by their size: "
                                                            "text clearly bigger than the body text.")
            return
        if tab.pdf.doc.get_toc() and ui.question(
                self, "Bookmarks from headings",
                f"Found {len(toc)} heading(s). Replace the bookmarks this PDF already has?") != ui.Yes:
            return
        with tab.pdf.edit("Bookmarks from headings", structure=True):
            tab.pdf.doc.set_toc(toc)
        tab.toggle_sidebar(True)
        self.act["sidebar"].setChecked(True)
        tab.side_tabs.setCurrentWidget(tab.bookmarks)
        self.msg(f"Made {len(toc)} bookmark(s) from the headings.")

    @_with_tab
    def compare_files(self, tab: DocumentTab) -> None:
        path, _ = QFileDialog.getOpenFileName(self, f"Compare {tab.pdf.title} with", self.settings.get("last_dir"),
                                              "PDF files (*.pdf)")
        if not path:
            return
        try:
            other = Path(path).read_bytes()
            probe = fitz.open("pdf", other)
            needs = probe.needs_pass
            probe.close()
        except Exception as exc:
            ui.warning(self, "Compare files", f"That file couldn't be read as a PDF:\n\n{exc}")
            return
        other_pw = None
        if needs:
            other_pw, ok = QInputDialog.getText(self, "Password", f"This file needs a password:\n{Path(path).name}",
                                                QLineEdit.EchoMode.Password)
            if not ok:
                return
        new_name, old_name = tab.pdf.title, Path(path).name
        mine = tab.pdf.plain_bytes()

        def done(result) -> None:
            data, info = result
            name = f"Compare - {Path(old_name).stem} vs {Path(new_name).stem}.pdf"
            self.add_document(PdfDocument.from_bytes(data, name))
            n = len(info["changes"])
            self.msg(f"{n} difference(s) found. Red = only in {old_name}, green = only in {new_name}.", 12000)

        # the chosen file is the "old" version, the open document the "new" one
        jobs.run_job(self, "Comparing", compare.report_bytes, other, mine, old_name, new_name, other_pw, None,
                     on_done=done)

    def read_aloud(self, what: str) -> None:
        tab = self.tab()
        if tab is None or jobs.busy():
            return
        if not speech.available():
            ui.information(self, "Read out loud", speech.missing_help())
            return
        cur = tab.canvas.current_page
        if what == "selection":
            text = tab.canvas.selected_text()
            if not text.strip():
                self.msg("Select some text first.")
                return
            self._reading = (tab, [cur])
            texts = [text]
        else:
            pages = [cur] if what == "page" else list(range(cur, min(tab.pdf.page_count, cur + 300)))
            texts = [tab.pdf.page(p).get_text("text", sort=True, flags=pdfops.WORD_FLAGS) for p in pages]
            self._reading = (tab, pages)
        self.reader.read(texts, int(self.settings.get("speech_rate") or 0))

    def _reading_piece(self, idx: int) -> None:
        tab, pages = getattr(self, "_reading", (None, []))
        if tab is not None and 0 <= idx < len(pages) and tab in self.doc_tabs():
            if len(pages) > 1:
                tab.canvas.go_to_page(pages[idx])
            self.statusBar().showMessage(f"Reading page {pages[idx] + 1} out loud. Ctrl+Shift+E stops.")

    def stop_reading(self) -> None:
        self.reader.stop()

    def reading_speed(self) -> None:
        value, ok = QInputDialog.getInt(self, "Reading speed", "Speed (-10 slow, 0 normal, 10 fast):",
                                        int(self.settings.get("speech_rate") or 0), -10, 10)
        if ok:
            self.settings.set("speech_rate", value)

    # ================================================================== digital IDs
    def manage_digital_ids(self) -> None:
        from pdfdesk import digitalid
        if not digitalid.available():
            ui.information(self, "Digital IDs", "Digital signatures need the pyHanko package. Run the PDF Desk "
                                                "installer again to add it.")
            return
        dialogs.DigitalIdsDialog(self, self.opt.author).exec()

    @_with_tab
    def start_digital_sign(self, tab: DocumentTab) -> None:
        from pdfdesk import digitalid
        if not digitalid.available():
            self.manage_digital_ids()
            return
        empty = [f for f in digitalid.existing_signature_fields(tab.pdf.doc) if not f["signed"]]
        if empty:
            f = empty[0]
            where = f"on page {f['page'] + 1}" if f["page"] is not None else "not shown on any page"
            name = safety.clean_text(f["name"], 80)
            if ui.question(self, "Sign with Digital ID",
                           f"This PDF has an empty signature field (\u201c{name}\u201d, {where}). "
                           "Sign that field?") == ui.Yes:
                self.sign_with_digital_id(f["page"] or 0, None, f["name"])
                return
        self.set_tool(T.DIGISIGN)
        self.msg("Drag a box where your digital signature should appear.", 10000)

    def sign_with_digital_id(self, pno: int, vis_rect, field: str = "") -> None:
        from pdfdesk import digitalid
        tab = self.tab()
        if tab is None or jobs.busy():
            return
        self.set_tool(T.SELECT)
        if not digitalid.available():
            self.manage_digital_ids()
            return
        if not digitalid.list_ids():
            if ui.question(self, "Sign with Digital ID", "You don't have a Digital ID yet. Create one now?") \
                    != ui.Yes:
                return
            dialogs.DigitalIdsDialog(self, self.opt.author).exec()
            if not digitalid.list_ids():
                return
        pdf = tab.pdf
        problem = self._signature_problem(pdf) if (pdf.dirty or pdf.will_break_signatures()) else ""
        if problem and ui.question(self, "Sign with Digital ID",
                                   f"This PDF already has digital signatures. {problem} Signing it now will "
                                   "show the earlier signatures as invalid in the signed copy.\n\nSign anyway?",
                                   default=ui.No) != ui.Yes:
            return
        base = self.doc_base(pdf)
        default = f"{base} (signed).pdf"
        sig_image = self.opt.signature if self.opt.signature and os.path.exists(self.opt.signature) else ""
        dlg = dialogs.SignDialog(self, digitalid.list_ids(), default, bool(sig_image) and vis_rect is not None
                                 or bool(sig_image and field), self.settings.get("sign_reason") or "",
                                 self.settings.get("sign_location") or "")
        accepted = dlg.exec()
        target = dlg.target.path()
        reason = digitalid.one_line(dlg.reason.currentText(), 200)
        location = digitalid.one_line(dlg.location.text(), 200)
        id_path, password = dlg.id_box.currentData(), dlg.password.text()
        with_image = dlg.with_image.isChecked()
        dlg.password.clear()
        dlg.deleteLater()
        if not accepted:
            return
        self.settings.set("sign_reason", reason)
        self.settings.set("sign_location", location)
        image = Path(sig_image).read_bytes() if sig_image and with_image else None
        try:
            data = pdf.save_bytes()
        except Exception as exc:
            ui.warning(self, "Sign with Digital ID", str(exc))
            return

        def done(signed: bytes) -> None:
            digitalid.cleanup()
            try:
                write_file_safely(target, signed)
            except Exception as exc:
                ui.warning(self, "Sign with Digital ID", f"The signed PDF couldn't be saved:\n\n{exc}")
                return
            same = pdf.path and os.path.normcase(os.path.abspath(pdf.path)) == os.path.normcase(os.path.abspath(target))
            if same:
                pdf.saved_id = pdf.state_id
                self.close_tab(self.tabs.indexOf(tab))
            self.open_path(target)
            self.msg(f"Signed and saved as {Path(target).name}.", 10000)

        def failed(exc) -> None:
            digitalid.cleanup()
            ui.warning(self, "Sign with Digital ID", str(exc))

        digitalid.preload()
        jobs.run_job(self, "Signing", digitalid.sign, data, id_path, password, page=pno, vis_rect=vis_rect,
                     field_name=field or None, reason=reason, location=location, contact="", image=image,
                     doc_password=pdf.password, on_done=done, on_error=failed, cancellable=False)

    @_with_tab
    def check_signatures(self, tab: DocumentTab) -> None:
        from pdfdesk import digitalid
        if not digitalid.available():
            self.manage_digital_ids()
            return
        pdf = tab.pdf
        try:
            data = pdf.save_bytes()   # what you see now, including changes not saved yet
        except Exception:
            data = pdf.base_bytes or pdf.doc.tobytes()
        base = self.doc_base(pdf)

        def done(results: list[dict]) -> None:
            if not results:
                tab.set_banner_level("info", "No digital signatures were found in this PDF.")
                ui.information(self, "Digital signatures", "This PDF has no digital signatures.")
                return
            levels = [digitalid.describe(r)[0] for r in results]
            worst = "bad" if "bad" in levels else "warn" if "warn" in levels else "good"
            summary = {"good": "All signatures are valid and the document hasn't been changed in a way that "
                               "matters.",
                       "warn": "The signatures are intact, but see the details (changes after signing or an "
                               "unconfirmed identity).",
                       "bad": "At least one signature is NOT valid. Don't rely on this document without "
                              "checking the details."}[worst]
            tab.set_banner_level(worst, f"{len(results)} digital signature(s). {summary}")
            tab.banner.show()
            dlg = dialogs.SignaturesDialog(self, results, lambda item: self._open_signed_version(
                data, item, base, pdf.password))
            dlg.exec()
            trusted = dlg.trusted_any
            dlg.deleteLater()
            if trusted:
                QTimer.singleShot(0, self.check_signatures)

        digitalid.preload()
        jobs.run_job(self, "Checking signatures", digitalid.validate, data, pdf.password, on_done=done)

    def _open_signed_version(self, data: bytes, item: dict, base: str, password: str | None) -> None:
        from pdfdesk import digitalid
        version = digitalid.signed_version(data, item)
        if version is None:
            raise ValueError("The signed version of this signature can't be told apart from the rest of the file.")
        field = safety.safe_filename_part(item.get("field") or "Signature", 40) or "Signature"
        base = Path(base)
        name = f"{base.name} (signed version, {field}).pdf"
        pdf = PdfDocument.from_bytes(version, name, str(base.parent / name))
        if pdf.doc.needs_pass and password:
            pdf.doc.authenticate(password)
            pdf.password = password
        self.add_document(pdf)
        self.msg("This is the document exactly as it was when the signature was made. Tools > Compare files "
                 "shows what changed since.", 12000)

    # ================================================================== forms
    def toggle_prepare_form(self, on: bool) -> None:
        for tab in self.doc_tabs():
            tab.canvas.set_form_edit(on)
        self.form_bar.setVisible(on and self.tab() is not None)
        if on:
            self.set_tool(T.SELECT)
            self.msg("Prepare form: pick a field type and drag on the page. Drag fields to move them, "
                     "double-click for their options.", 10000)
        elif self.opt.tool in T.FIELD_TOOLS:
            self.set_tool(T.SELECT)

    @_with_tab
    def detect_fields(self, tab: DocumentTab) -> None:
        pdf = tab.pdf
        pages = range(pdf.page_count) if pdf.page_count <= 60 else [tab.canvas.current_page]
        found = []
        for pno in pages:
            try:
                found += [(pno, k, r, n) for k, r, n in forms.detect_fields(pdf.page(pno))]
            except Exception:
                continue
        if not found:
            ui.information(self, "Find form fields", "No blank lines, underscores or boxes that look like form "
                                                     "fields were found." + ("" if pdf.page_count <= 60 else
                                                                            " (Only the current page was checked.)"))
            return
        dlg = dialogs.DetectedFieldsDialog(self, found)
        if not dlg.exec():
            return
        chosen = dlg.chosen()
        if not chosen:
            return
        by_page: dict[int, list] = {}
        for pno, kind, rect, name in chosen:
            by_page.setdefault(pno, []).append((kind, rect, name))
        try:
            with pdf.edit("Add form fields", pages=sorted(by_page)):
                total = sum(forms.add_detected(pdf.page(pno), items) for pno, items in by_page.items())
        except Exception as exc:
            ui.warning(self, "Find form fields", str(exc))
            return
        if not self.act["prepare_form"].isChecked():
            self.act["prepare_form"].setChecked(True)
        self.msg(f"Added {total} form field(s). Double-click a field to rename it or change its options.", 10000)

    @_with_tab
    def clear_form(self, tab: DocumentTab) -> None:
        if ui.question(self, "Clear form", "Empty every field and untick every box in this form?") != ui.Yes:
            return
        with tab.pdf.edit("Clear form"):
            count = forms.reset_form(tab.pdf.doc)
        self.msg(f"Cleared {count} field(s).")

    @_with_tab
    def export_form_data(self, tab: DocumentTab) -> None:
        base = self.doc_base(tab.pdf)
        path, chosen = QFileDialog.getSaveFileName(self, "Export form data", str(base) + " data.json",
                                                   "JSON (*.json);;CSV for spreadsheets (*.csv);;XFDF for Acrobat (*.xfdf)")
        if not path:
            return
        ext = {"CSV": ".csv", "XFDF": ".xfdf"}.get(chosen.split()[0], ".json")
        if not path.lower().endswith((".json", ".csv", ".xfdf")):
            path += ext
        try:
            count = forms.export_data(tab.pdf.doc, path, Path(tab.pdf.path or tab.pdf.title).name)
        except Exception as exc:
            ui.warning(self, "Export form data", str(exc))
            return
        self.msg(f"Saved {count} field value(s) to {Path(path).name}")

    @_with_tab
    def import_form_data(self, tab: DocumentTab) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import form data", self.settings.get("last_dir"),
                                              "Form data (*.json *.csv *.xfdf)")
        if not path:
            return
        try:
            values = forms.read_data(path)
        except Exception as exc:
            ui.warning(self, "Import form data", f"That file couldn't be read as form data:\n\n{exc}")
            return
        try:
            with tab.pdf.edit("Import form data"):
                count = forms.apply_values(tab.pdf.doc, values)
                if not count:
                    raise _NoChange()
        except _NoChange:
            ui.information(self, "Import form data", "None of the names in that file match a field in this form.")
            return
        except Exception as exc:
            ui.warning(self, "Import form data", str(exc))
            return
        self.msg(f"Filled in {count} field(s) from {Path(path).name}.")

    def manage_signatures(self) -> None:
        dlg = SignatureDialog(self, self.opt.signature)
        if dlg.exec() and dlg.chosen:
            self.opt.set_signature(dlg.chosen)
            if self.tab():
                self.set_tool(T.SIGNATURE)
                self.msg("Click or drag on the page to place your signature.", 8000)
        elif self.opt.tool == T.SIGNATURE and not (self.opt.signature and os.path.exists(self.opt.signature)):
            self.set_tool(T.SELECT)

    # ================================================================== help
    def extras_help(self) -> None:
        ui.rich_information(self, "Optional extras", dialogs.EXTRAS_HELP)

    def about(self) -> None:
        box = QMessageBox(self)
        box.setWindowTitle(f"About {APP_NAME}")
        box.setIconPixmap(icons.app_icon().pixmap(64, 64))
        box.setTextFormat(Qt.TextFormat.RichText)
        box.setText(dialogs.about_text())
        box.exec()
