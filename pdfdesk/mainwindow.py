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
from pdfdesk import APP_NAME, annots, convert, dialogs, icons, jobs, pdfops, printing, theme
from pdfdesk import tools as T
from pdfdesk.config import settings as get_settings
from pdfdesk.doctab import DocumentTab
from pdfdesk.document import NeedsPassword, PdfDocument
from pdfdesk.fonts import hex_to_rgb, rgb_to_hex
from pdfdesk.home import HomePage
from pdfdesk.recents import RecentFiles
from pdfdesk.signature import SignatureDialog, list_signatures
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
        for k in ("night", "forms", "sidebar", "fullscreen", None, "goto", "first", "prev", "next", "last"):
            m.addSeparator() if k is None else m.addAction(self.act[k])

        m = mb.addMenu("&Pages")
        for k in ("organize", None, "rotate_cw", "rotate_ccw", "rotate_pages", None, "insert_blank", "insert_file",
                  "duplicate", "delete_pages", None, "extract", "split", None, "crop", "header_footer", "watermark",
                  None, "bookmark"):
            m.addSeparator() if k is None else m.addAction(self.act[k])

        m = mb.addMenu("&Tools")
        for k in ("compress", "ocr", None, "protect", "unprotect", None, "find_redact", "apply_redact", "hidden",
                  None, "flatten", "signatures"):
            m.addSeparator() if k is None else m.addAction(self.act[k])

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
        for k in ("sidebar", "organize", "find", "night"):
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
                  [T.STAMP, T.IMAGE, T.SIGNATURE], [T.EDIT_TEXT, T.REDACT]]
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
        self.fill_check.toggled.connect(self.opt.set_fill)
        self.w_fill = et.addWidget(self.fill_check)
        self.fill_btn = ColorButton(self.opt.fill_hex, "Fill color")
        self.fill_btn.color_changed.connect(self.opt.set_fill_color)
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
        self.font_box = QSpinBox()
        self.font_box.setRange(4, 144)
        self.font_box.setSuffix(" pt")
        self.font_box.setToolTip("Text size")
        self.font_box.setValue(int(self.opt.font_size))
        self.font_box.valueChanged.connect(lambda v: self.opt.set_font_size(v))
        self.w_font = et.addWidget(self.font_box)
        et.addAction(self.act["delete_sel"])
        self.tool_hint = QLabel()
        self.tool_hint.setObjectName("muted")
        self.tool_hint.setContentsMargins(10, 0, 4, 0)
        et.addWidget(self.tool_hint)

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
        if tool == T.SIGNATURE and not (self.opt.signature and os.path.exists(self.opt.signature)):
            QTimer.singleShot(0, self.manage_signatures)
        label, _ico, helptext = T.TOOLS[tool]
        self.tool_hint.setText(helptext if tool != T.SELECT else "")
        self._sync_tool_widgets()

    def _sync_tool_widgets(self) -> None:
        tool = self.opt.tool
        tab = self.tab()
        sa = tab.canvas.sel_annot if tab else None
        if tool == T.SELECT and sa:
            stroke = (sa.get("colors") or {}).get("stroke") or (sa.get("colors") or {}).get("fill")
            if sa["type"] == fitz.PDF_ANNOT_FREE_TEXT:
                try:
                    page = tab.pdf.page(sa["pno"])
                    _size, col = annots.freetext_style(tab.pdf.doc, page.load_annot(sa["xref"]))
                    stroke = col
                except Exception:
                    pass
            show_color = sa["type"] not in (fitz.PDF_ANNOT_STAMP, fitz.PDF_ANNOT_REDACT)
            if stroke:
                self.color_btn.set_color(rgb_to_hex(stroke))
            show_width = sa["type"] in (fitz.PDF_ANNOT_SQUARE, fitz.PDF_ANNOT_CIRCLE, fitz.PDF_ANNOT_LINE,
                                        fitz.PDF_ANNOT_INK, fitz.PDF_ANNOT_POLYGON, fitz.PDF_ANNOT_POLY_LINE)
            if show_width:
                self.width_box.blockSignals(True)
                self.width_box.setValue(float((sa.get("border") or {}).get("width") or 1))
                self.width_box.blockSignals(False)
            self.opacity_box.blockSignals(True)
            op = sa.get("opacity")
            self.opacity_box.setValue(int((op if op is not None and op >= 0 else 1.0) * 100))
            self.opacity_box.blockSignals(False)
            vis = {"color": show_color, "fill": False, "width": show_width, "opacity": True, "font": False}
        else:
            self.color_btn.set_color(self.opt.color_hex(tool))
            vis = {"color": tool in T.COLOR_TOOLS, "fill": tool in T.FILL_TOOLS, "width": tool in T.WIDTH_TOOLS,
                   "opacity": tool in T.OPACITY_TOOLS, "font": tool in T.FONT_TOOLS}
            self.width_box.blockSignals(True)
            self.width_box.setValue(self.opt.width)
            self.width_box.blockSignals(False)
            self.opacity_box.blockSignals(True)
            self.opacity_box.setValue(int(self.opt.opacity * 100))
            self.opacity_box.blockSignals(False)
        self.w_color.setVisible(vis["color"])
        self.w_fill.setVisible(vis["fill"])
        self.w_fill_color.setVisible(vis["fill"])
        self.fill_btn.setEnabled(self.opt.fill)
        self.w_width.setVisible(vis["width"])
        self.w_opacity.setVisible(vis["opacity"])
        self.w_font.setVisible(vis["font"])
        self._update_history()

    def _annot_selected(self, info) -> None:
        tab = self.tab()
        if info and tab:
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
        c.annot_selected.connect(lambda info, t=tab: self.tab() is t and self._annot_selected(info))
        c.selection_changed.connect(lambda _s: None)
        tab.status_changed.connect(lambda t=tab: self.tab() is t and self._update_status())
        tab.title_changed.connect(self._update_title)
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
        base = tab.pdf.path or tab.pdf.suggested_path or os.path.join(self.settings.get("last_dir"), tab.pdf.title)
        target, _ = QFileDialog.getSaveFileName(self, "Save a copy", str(Path(base).with_name(Path(base).stem + " copy.pdf")),
                                                "PDF files (*.pdf)")
        if target:
            if not target.lower().endswith(".pdf"):
                target += ".pdf"
            try:
                tab.pdf.save_copy(target)
                self.msg(f"Saved a copy as {Path(target).name}")
            except Exception as exc:
                ui.warning(self, "Save a copy", str(exc))

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
        if isinstance(focus, QLineEdit) or (focus is not None and focus.inherits("QPlainTextEdit")):
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
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            self.tabs.setTabIcon(i, icons.icon("house" if w is self.home else "file-text"))
            if isinstance(w, DocumentTab):
                for k, ico in enumerate(("gallery-vertical", "bookmark", "message-square-text", "search")):
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
