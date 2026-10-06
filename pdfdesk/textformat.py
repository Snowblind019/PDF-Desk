"""Word-style text formatting: the font list, the formatting toolbar and the on-page rich text editor."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QSize, QStringListModel, Qt, QTimer, Signal
from PySide6.QtGui import (QActionGroup, QColor, QFont, QKeySequence, QStandardItem, QStandardItemModel, QTextBlockFormat,
                           QTextCharFormat, QTextCursor, QTextFormat, QTextListFormat, QBrush)
from PySide6.QtWidgets import (QApplication, QComboBox, QCompleter, QFrame, QMenu, QStyledItemDelegate,
                               QTextEdit, QToolBar, QToolButton, QWidget)

from pdfdesk import fontcatalog, icons, theme
from pdfdesk import richtext as RT
from pdfdesk.widgets import ColorButton

PROP_FAMILY = QTextFormat.Property.UserProperty + 11
ALIGN_TO_QT = {"left": Qt.AlignmentFlag.AlignLeft, "center": Qt.AlignmentFlag.AlignHCenter,
               "right": Qt.AlignmentFlag.AlignRight, "justify": Qt.AlignmentFlag.AlignJustify}


def qt_align_name(flags) -> str:
    if flags & Qt.AlignmentFlag.AlignHCenter:
        return "center"
    if flags & Qt.AlignmentFlag.AlignRight:
        return "right"
    if flags & Qt.AlignmentFlag.AlignJustify:
        return "justify"
    return "left"


def screen_font(family: str, px: float, bold=False, italic=False) -> QFont:
    f = QFont()
    f.setFamilies(fontcatalog.qt_families(family))
    f.setPixelSize(max(4, min(4000, int(round(px)))))
    f.setBold(bold)
    f.setItalic(italic)
    return f


# =========================================================================== font list

class _FontDelegate(QStyledItemDelegate):
    """Draws each font name in its own font, like Word's font list."""

    def paint(self, painter, option, index):
        name = index.data(Qt.ItemDataRole.DisplayRole) or ""
        if index.data(Qt.ItemDataRole.UserRole + 1):  # section header
            super().paint(painter, option, index)
            return
        opt = option
        self.initStyleOption(opt, index)
        opt.font = screen_font(name, 15)
        super().paint(painter, opt, index)

    def sizeHint(self, option, index):
        s = super().sizeHint(option, index)
        return QSize(s.width(), max(s.height(), 26))


class FontComboBox(QComboBox):
    font_chosen = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMinimumContentsLength(16)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMaxVisibleItems(18)
        self.setToolTip("Font")
        self.setItemDelegate(_FontDelegate(self))
        self._model = QStandardItemModel(self)
        self.setModel(self._model)
        self._names: list[str] = []
        self._recent: list[str] = []
        self._loaded = False
        self._current = ""
        self.activated.connect(self._activated)
        self.lineEdit().editingFinished.connect(self._typed)
        self._completer_model = QStringListModel(self)
        comp = QCompleter(self._completer_model, self)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        comp.setFilterMode(Qt.MatchFlag.MatchContains)
        comp.activated.connect(self._completed)
        self.setCompleter(comp)

    def ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        self._names = fontcatalog.catalog().names()
        self._completer_model.setStringList(self._names)
        self._rebuild()

    def set_recent(self, names: list[str]) -> None:
        self._recent = [n for n in names if n][:6]
        if self._loaded:
            self._rebuild()

    def _rebuild(self) -> None:
        self.blockSignals(True)
        self._model.clear()
        known = set(self._names)
        recent = [n for n in self._recent if n in known]
        if recent:
            self._add_header("Recently used")
            for n in recent:
                self._model.appendRow(QStandardItem(n))
            self._add_header("All fonts")
        for n in self._names:
            self._model.appendRow(QStandardItem(n))
        self.setEditText(self._current)
        self.blockSignals(False)

    def _add_header(self, text: str) -> None:
        item = QStandardItem(text)
        item.setFlags(Qt.ItemFlag.NoItemFlags)
        item.setData(True, Qt.ItemDataRole.UserRole + 1)
        f = QFont()
        f.setBold(True)
        f.setPointSizeF(f.pointSizeF() * 0.9)
        item.setFont(f)
        self._model.appendRow(item)

    def showPopup(self) -> None:
        self.ensure_loaded()
        idx = self.findText(self._current, Qt.MatchFlag.MatchExactly)
        if idx >= 0:
            self.setCurrentIndex(idx)
        # The list is wider than the box so long names (shown in their own font) aren't cut off.
        self.view().setMinimumWidth(max(self.width(), 340))
        super().showPopup()

    def set_family(self, name: str) -> None:
        self._current = name or ""
        self.blockSignals(True)
        self.setEditText(self._current)
        self.lineEdit().setCursorPosition(0)
        self.blockSignals(False)

    def family(self) -> str:
        return self._current

    def _emit(self, name: str) -> None:
        self.ensure_loaded()
        cat = fontcatalog.catalog()
        if name not in cat.families:
            low = name.casefold().strip()
            match = next((n for n in self._names if n.casefold() == low), None)
            if match is None:
                self.set_family(self._current)
                return
            name = match
        self.set_family(name)
        self.font_chosen.emit(name)

    def _activated(self, index: int) -> None:
        name = self.itemText(index)
        if name and not self._model.item(index).data(Qt.ItemDataRole.UserRole + 1):
            self._emit(name)

    def _completed(self, text: str) -> None:
        self._emit(text)

    def _typed(self) -> None:
        text = self.currentText().strip()
        if text and text != self._current:
            self._emit(text)


class SizeComboBox(QComboBox):
    size_chosen = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.addItems([_fmt_size(s) for s in RT.SIZES])
        self.setFixedWidth(66)
        self.setToolTip("Font size")
        self.activated.connect(lambda i: self._emit(self.itemText(i)))
        self.lineEdit().editingFinished.connect(lambda: self._emit(self.currentText()))
        self._current = 12.0

    def set_size(self, size: float | None) -> None:
        self.blockSignals(True)
        if size is None:
            self.setEditText("")
        else:
            self._current = float(size)
            self.setEditText(_fmt_size(size))
        self.blockSignals(False)

    def _emit(self, text: str) -> None:
        try:
            value = float(text.replace(",", ".").replace("pt", "").strip())
        except ValueError:
            self.set_size(self._current)
            return
        value = max(RT.MIN_SIZE, min(RT.MAX_SIZE, value))
        if abs(value - self._current) < 0.01 and text.strip() == _fmt_size(value):
            return
        self.set_size(value)
        self.size_chosen.emit(value)


def _fmt_size(size: float) -> str:
    return str(int(size)) if abs(size - round(size)) < 0.01 else f"{size:.1f}"


# =========================================================================== toolbar

class TextFormatBar(QToolBar):
    """Font, size, bold/italic/underline, colors, alignment, lists and line spacing.
    Emits `changed` with one formatting change, for example {"bold": True} or {"font": "Arial"}."""
    changed = Signal(dict)

    def __init__(self, parent=None):
        super().__init__("Text format", parent)
        self.setObjectName("textFormatBar")
        self.setMovable(False)
        self.setIconSize(QSize(18, 18))
        self.font_box = FontComboBox()
        self.font_box.font_chosen.connect(lambda n: self.changed.emit({"font": n}))
        self.addWidget(self.font_box)
        self.size_box = SizeComboBox()
        self.size_box.size_chosen.connect(lambda s: self.changed.emit({"size": s}))
        self.addWidget(self.size_box)
        self.grow = self._button("a-arrow-up", "Bigger text (Ctrl+])", lambda: self.changed.emit({"grow": 1}))
        self.shrink = self._button("a-arrow-down", "Smaller text (Ctrl+[)", lambda: self.changed.emit({"grow": -1}))
        self.addSeparator()
        self.bold = self._toggle("bold", "Bold (Ctrl+B)", "bold")
        self.italic = self._toggle("italic", "Italic (Ctrl+I)", "italic")
        self.underline = self._toggle("underline", "Underline (Ctrl+U)", "underline")
        self.strike = self._toggle("strikethrough", "Strikethrough", "strike")
        self.sup = self._button("superscript", "Superscript", lambda: self._valign("super"), checkable=True)
        self.sub = self._button("subscript", "Subscript", lambda: self._valign("sub"), checkable=True)
        self.addSeparator()
        self.color = ColorButton("#000000", "Text color", glyph="baseline")
        self.color.color_changed.connect(lambda c: self.changed.emit({"color": c or "#000000"}))
        self.addWidget(self.color)
        self.highlight = ColorButton("", "Highlight color", glyph="highlighter", none_label="No highlight")
        self.highlight.color_changed.connect(lambda c: self.changed.emit({"highlight": c or None}))
        self.w_highlight = self.addWidget(self.highlight)
        self.addSeparator()
        self.align_group = QActionGroup(self)
        self.align_actions = {}
        for key, ico, tip in (("left", "text-align-start", "Align left (Ctrl+L)"),
                              ("center", "text-align-center", "Center (Ctrl+E)"),
                              ("right", "text-align-end", "Align right (Ctrl+R)"),
                              ("justify", "text-align-justify", "Justify (Ctrl+J)")):
            act = self.addAction(tip)
            icons.bind(act, ico)
            act.setToolTip(tip)
            act.setCheckable(True)
            act.triggered.connect(lambda _=False, k=key: self.changed.emit({"align": k}))
            self.align_group.addAction(act)
            self.align_actions[key] = act
        self.addSeparator()
        self.bullets = self._button("list", "Bullets", lambda: self._list("bullet"), checkable=True)
        self.numbers = self._button("list-ordered", "Numbering", lambda: self._list("number"), checkable=True)
        self.spacing = QToolButton()
        icons.bind(self.spacing, "list-chevrons-up-down")
        self.spacing.setToolTip("Line spacing")
        self.spacing.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.spacing)
        self.spacing_actions = {}
        for val in RT.SPACINGS:
            act = menu.addAction(f"{val:g}")
            act.setCheckable(True)
            act.triggered.connect(lambda _=False, v=val: self.changed.emit({"spacing": v}))
            self.spacing_actions[val] = act
        self.spacing.setMenu(menu)
        self.w_spacing = self.addWidget(self.spacing)
        self.clear = self._button("remove-formatting", "Clear formatting", lambda: self.changed.emit({"clear": True}))
        self._rich_only = [self.sup, self.sub, self.w_highlight, self.bullets, self.numbers, self.w_spacing,
                           self.clear, *self.align_actions.values()]

    def _button(self, ico: str, tip: str, slot, checkable: bool = False):
        act = self.addAction(tip)
        icons.bind(act, ico)
        act.setToolTip(tip)
        act.setCheckable(checkable)
        act.triggered.connect(lambda _=False: slot())
        return act

    def _toggle(self, ico: str, tip: str, attr: str):
        act = self._button(ico, tip, lambda: None, checkable=True)
        act.triggered.disconnect()
        act.triggered.connect(lambda on, a=attr: self.changed.emit({a: bool(on)}))
        return act

    def _valign(self, kind: str) -> None:
        act = self.sup if kind == "super" else self.sub
        self.changed.emit({"valign": kind if act.isChecked() else ""})

    def _list(self, kind: str) -> None:
        act = self.bullets if kind == "bullet" else self.numbers
        self.changed.emit({"list": kind if act.isChecked() else ""})

    def set_rich(self, rich: bool) -> None:
        """Rich mode has every control; plain mode (editing existing page text) only font controls."""
        for w in self._rich_only:
            w.setVisible(rich)

    def set_state(self, st: dict) -> None:
        """Show the formatting of the text at the cursor (None means mixed)."""
        self.font_box.set_family(st.get("font") or "")
        self.size_box.set_size(st.get("size"))
        for act, key in ((self.bold, "bold"), (self.italic, "italic"), (self.underline, "underline"),
                         (self.strike, "strike")):
            act.setChecked(bool(st.get(key)))
        self.sup.setChecked(st.get("valign") == "super")
        self.sub.setChecked(st.get("valign") == "sub")
        self.color.set_color(st.get("color") or "#000000")
        self.highlight.set_color(st.get("highlight") or "")
        align = st.get("align") or "left"
        for key, act in self.align_actions.items():
            act.setChecked(key == align)
        self.bullets.setChecked(st.get("list") == "bullet")
        self.numbers.setChecked(st.get("list") == "number")
        for val, act in self.spacing_actions.items():
            act.setChecked(abs(val - float(st.get("spacing") or 1.0)) < 0.01)

    def owns(self, widget) -> bool:
        """Is this widget part of the bar (or one of its popups)?"""
        w = widget
        while w is not None:
            if w is self:
                return True
            w = w.parentWidget()
        return False


# =========================================================================== on-page editor

class RichEditor(QTextEdit):
    """Edits a text box right on the page, with full formatting. In "plain" mode (editing text that is
    part of the page) every formatting change applies to all of the text."""
    committed = Signal(object)    # richtext.Box
    cancelled = Signal()
    format_changed = Signal(dict)

    def __init__(self, parent: QWidget, box: RT.Box, scale: float, auto_width: bool, plain: bool = False,
                 max_width_px: float = 4000, bar: TextFormatBar | None = None, single_line: bool = False):
        super().__init__(parent)
        self.scale = scale
        self.plain = plain
        self.single_line = single_line
        self.auto_width = auto_width
        self.max_width_px = max_width_px
        self.bar = bar
        self._done = False
        self.box_template = box
        self.default_run = RT.Run("", box.first_run().font, box.first_run().size, color=box.first_run().color)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap if auto_width else QTextEdit.LineWrapMode.WidgetWidth)
        self.setAcceptRichText(False)
        bg = box.fill or "#ffffff"
        c = QColor(bg)
        self.setStyleSheet(f"QTextEdit {{ background: rgba({c.red()},{c.green()},{c.blue()},235); "
                           f"border: 1px dashed {theme.ACCENT}; border-radius: 0; padding: 0; }}")
        inset = (box.padding + (box.border_width if box.border else 0)) * scale
        self.document().setDocumentMargin(max(1.0, inset))
        self._dpi = max(1, self.logicalDpiY())
        self.load_box(box)
        self.moveCursor(QTextCursor.MoveOperation.End)
        self.textChanged.connect(self._grow)
        self.cursorPositionChanged.connect(self._report)
        self.currentCharFormatChanged.connect(lambda _f: self._report())

    # ------------------------------------------------------------------ sizes
    def _pt_to_editor(self, size_pt: float) -> float:
        return size_pt * self.scale * 72.0 / self._dpi

    def _editor_to_pt(self, editor_pt: float) -> float:
        return round(editor_pt * self._dpi / 72.0 / max(0.01, self.scale) * 2) / 2

    # ------------------------------------------------------------------ box <-> document
    def char_format(self, run: RT.Run) -> QTextCharFormat:
        f = QTextCharFormat()
        f.setFontFamilies(fontcatalog.qt_families(run.font))
        f.setProperty(PROP_FAMILY, run.font)
        f.setFontPointSize(self._pt_to_editor(run.size))
        f.setFontWeight(QFont.Weight.Bold if run.bold else QFont.Weight.Normal)
        f.setFontItalic(run.italic)
        f.setFontUnderline(run.underline)
        f.setFontStrikeOut(run.strike)
        f.setForeground(QBrush(QColor(run.color)))
        if run.highlight:
            f.setBackground(QBrush(QColor(run.highlight)))
        else:
            f.clearBackground()
        f.setVerticalAlignment({"super": QTextCharFormat.VerticalAlignment.AlignSuperScript,
                                "sub": QTextCharFormat.VerticalAlignment.AlignSubScript}.get(
            run.valign, QTextCharFormat.VerticalAlignment.AlignNormal))
        return f

    def run_from_format(self, text: str, f: QTextCharFormat) -> RT.Run:
        fam = f.property(PROP_FAMILY)
        if not isinstance(fam, str) or not fam:
            fams = f.fontFamilies() or []
            fam = None
            for name in (fams if isinstance(fams, list) else [fams]):
                fam = fontcatalog.family_from_qt(str(name))
                if fam:
                    break
            fam = fam or self.default_run.font
        size = self._editor_to_pt(f.fontPointSize()) if f.fontPointSize() > 0 else self.default_run.size
        fg = f.foreground()
        color = fg.color().name() if fg.style() != Qt.BrushStyle.NoBrush else self.default_run.color
        bg = f.background()
        highlight = bg.color().name() if bg.style() != Qt.BrushStyle.NoBrush and bg.color().alpha() > 0 else None
        va = f.verticalAlignment()
        valign = ("super" if va == QTextCharFormat.VerticalAlignment.AlignSuperScript else
                  "sub" if va == QTextCharFormat.VerticalAlignment.AlignSubScript else "")
        return RT.Run(text, fam, max(RT.MIN_SIZE, min(RT.MAX_SIZE, size)), f.fontWeight() >= 600, f.fontItalic(),
                      f.fontUnderline(), f.fontStrikeOut(), color, highlight, valign)

    def load_box(self, box: RT.Box) -> None:
        doc = self.document()
        doc.clear()
        cur = QTextCursor(doc)
        cur.beginEditBlock()
        current_list = None
        last_kind = ""
        for k, para in enumerate(box.paras or [RT.Para([self.default_run])]):
            bf = QTextBlockFormat()
            bf.setAlignment(ALIGN_TO_QT.get(para.align, Qt.AlignmentFlag.AlignLeft))
            if para.spacing != 1.0:
                bf.setLineHeight(para.spacing * 100, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            first = para.runs[0] if para.runs else self.default_run
            if k == 0:
                cur.setBlockFormat(bf)
                cur.setBlockCharFormat(self.char_format(first))
            else:
                cur.insertBlock(bf, self.char_format(first))
            if para.list:
                if para.list == last_kind and current_list is not None:
                    current_list.add(cur.block())
                else:
                    lf = QTextListFormat()
                    lf.setStyle(QTextListFormat.Style.ListDisc if para.list == "bullet"
                                else QTextListFormat.Style.ListDecimal)
                    current_list = cur.createList(lf)
            else:
                current_list = None
            last_kind = para.list
            for run in para.runs:
                cur.insertText(run.text.replace("\n", " "), self.char_format(run))
            cur.setCharFormat(self.char_format(para.runs[-1] if para.runs else first))
        cur.endEditBlock()
        self.setTextCursor(cur)

    def to_box(self) -> RT.Box:
        doc = self.document()
        paras = []
        block = doc.begin()
        while block.isValid():
            bf = block.blockFormat()
            spacing = 1.0
            if bf.lineHeightType() == QTextBlockFormat.LineHeightTypes.ProportionalHeight.value and bf.lineHeight() > 0:
                spacing = round(bf.lineHeight() / 100.0, 2)
            lst = block.textList()
            kind = ""
            if lst is not None:
                style = lst.format().style()
                kind = "number" if style in (QTextListFormat.Style.ListDecimal, QTextListFormat.Style.ListLowerAlpha,
                                             QTextListFormat.Style.ListUpperAlpha) else "bullet"
            runs = []
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag.isValid():
                    text = frag.text().replace(" ", " ").replace("￼", "")
                    if text:
                        runs.append(self.run_from_format(text, frag.charFormat()))
                it += 1
            if not runs:
                runs = [self.run_from_format("", block.charFormat())]
            paras.append(RT.Para(runs, align=qt_align_name(bf.alignment()), spacing=spacing, list=kind))
            block = block.next()
        t = self.box_template
        box = RT.Box(paras=paras, fill=t.fill, border=t.border, border_width=t.border_width, opacity=t.opacity,
                     padding=t.padding, auto_width=self.auto_width, min_height=t.min_height)
        return box.normalize()

    # ------------------------------------------------------------------ formatting
    def state(self) -> dict:
        cur = self.textCursor()
        f = cur.charFormat()
        run = self.run_from_format("", f)
        bf = cur.blockFormat()
        lst = cur.currentList()
        kind = ""
        if lst is not None:
            kind = "number" if lst.format().style() == QTextListFormat.Style.ListDecimal else "bullet"
        spacing = 1.0
        if bf.lineHeightType() == QTextBlockFormat.LineHeightTypes.ProportionalHeight.value and bf.lineHeight() > 0:
            spacing = bf.lineHeight() / 100.0
        return {"font": run.font, "size": run.size, "bold": run.bold, "italic": run.italic,
                "underline": run.underline, "strike": run.strike, "color": run.color, "highlight": run.highlight,
                "valign": run.valign, "align": qt_align_name(bf.alignment()), "list": kind, "spacing": spacing}

    def _report(self) -> None:
        self.format_changed.emit(self.state())

    def apply(self, change: dict) -> None:
        """Apply one change from the formatting toolbar to the selection (or the word at the cursor)."""
        cur = self.textCursor()
        whole = self.plain
        if whole:
            pos, anchor = cur.position(), cur.anchor()
            cur.select(QTextCursor.SelectionType.Document)
        key, value = next(iter(change.items()))
        f = QTextCharFormat()
        if key == "font":
            f.setFontFamilies(fontcatalog.qt_families(value))
            f.setProperty(PROP_FAMILY, value)
        elif key == "size":
            f.setFontPointSize(self._pt_to_editor(value))
        elif key == "grow":
            self._grow_sizes(cur, value)
            f = None
        elif key == "bold":
            f.setFontWeight(QFont.Weight.Bold if value else QFont.Weight.Normal)
        elif key == "italic":
            f.setFontItalic(bool(value))
        elif key == "underline":
            f.setFontUnderline(bool(value))
        elif key == "strike":
            f.setFontStrikeOut(bool(value))
        elif key == "color":
            f.setForeground(QBrush(QColor(value)))
        elif key == "highlight":
            if value:
                f.setBackground(QBrush(QColor(value)))
            else:
                f.setBackground(QBrush(Qt.BrushStyle.NoBrush))
        elif key == "valign":
            f.setVerticalAlignment({"super": QTextCharFormat.VerticalAlignment.AlignSuperScript,
                                    "sub": QTextCharFormat.VerticalAlignment.AlignSubScript}.get(
                value, QTextCharFormat.VerticalAlignment.AlignNormal))
        elif key in ("align", "spacing"):
            bf = QTextBlockFormat()
            if key == "align":
                bf.setAlignment(ALIGN_TO_QT.get(value, Qt.AlignmentFlag.AlignLeft))
            else:
                bf.setLineHeight(float(value) * 100, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
            cur.mergeBlockFormat(bf)
            f = None
        elif key == "list":
            self._set_list(cur, value)
            f = None
        elif key == "clear":
            base = self.char_format(RT.Run("", self.default_run.font, self.default_run.size,
                                           color=self.default_run.color))
            if not cur.hasSelection():
                cur.select(QTextCursor.SelectionType.WordUnderCursor)
            cur.setCharFormat(base)
            f = None
        if f is not None:
            if not cur.hasSelection():
                word = QTextCursor(cur)
                word.select(QTextCursor.SelectionType.WordUnderCursor)
                if word.hasSelection() and cur.position() not in (word.selectionStart(), word.selectionEnd()):
                    word.mergeCharFormat(f)
                self.mergeCurrentCharFormat(f)
            else:
                cur.mergeCharFormat(f)
        if whole:
            cur.setPosition(anchor)
            cur.setPosition(pos, QTextCursor.MoveMode.KeepAnchor)
            self.setTextCursor(cur)
            if f is not None:
                self.mergeCurrentCharFormat(f)
        self._grow()
        self._report()
        self.setFocus()

    def _grow_sizes(self, cur: QTextCursor, step: int) -> None:
        def bump(size: float) -> float:
            if step > 0:
                return next((s for s in RT.SIZES if s > size + 0.01), min(RT.MAX_SIZE, size + 12))
            return next((s for s in reversed(RT.SIZES) if s < size - 0.01), max(RT.MIN_SIZE, size - 1))

        if not cur.hasSelection():
            run = self.run_from_format("", cur.charFormat())
            f = QTextCharFormat()
            f.setFontPointSize(self._pt_to_editor(bump(run.size)))
            self.mergeCurrentCharFormat(f)
            return
        start, end = cur.selectionStart(), cur.selectionEnd()
        pos = start
        while pos < end:
            c = QTextCursor(self.document())
            c.setPosition(pos)
            c.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
            run = self.run_from_format("", c.charFormat())
            f = QTextCharFormat()
            f.setFontPointSize(self._pt_to_editor(bump(run.size)))
            c.mergeCharFormat(f)
            pos += 1

    def _set_list(self, cur: QTextCursor, kind: str) -> None:
        cur.beginEditBlock()
        start, end = cur.selectionStart(), cur.selectionEnd()
        c = QTextCursor(self.document())
        c.setPosition(start)
        first_block = c.block()
        c.setPosition(end)
        last_block = c.block()
        blocks = []
        b = first_block
        while b.isValid():
            blocks.append(b)
            if b == last_block:
                break
            b = b.next()
        for b in blocks:
            lst = b.textList()
            if lst is not None:
                lst.remove(b)
                bc = QTextCursor(b)
                bf = bc.blockFormat()
                bf.setIndent(0)
                bc.setBlockFormat(bf)
        if kind:
            lf = QTextListFormat()
            lf.setStyle(QTextListFormat.Style.ListDisc if kind == "bullet" else QTextListFormat.Style.ListDecimal)
            c = QTextCursor(blocks[0])
            new_list = c.createList(lf)
            for b in blocks[1:]:
                new_list.add(b)
        cur.endEditBlock()

    # ------------------------------------------------------------------ size of the box on screen
    def _grow(self) -> None:
        doc = self.document()
        if self.auto_width:
            doc.setTextWidth(-1)
            need_w = min(self.max_width_px, doc.idealWidth() + 6)
            if need_w > self.width():
                self.resize(int(need_w), self.height())
            if self.width() >= self.max_width_px - 1 and self.lineWrapMode() == QTextEdit.LineWrapMode.NoWrap:
                self.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        need_h = int(doc.size().height() + 4)
        if need_h > self.height():
            self.resize(self.width(), need_h)

    # ------------------------------------------------------------------ keys & focus
    _OWN_SHORTCUTS = {"Ctrl+B", "Ctrl+I", "Ctrl+U", "Ctrl+E", "Ctrl+L", "Ctrl+R", "Ctrl+J", "Ctrl+]", "Ctrl+[",
                      "Ctrl+Shift+=", "Ctrl+="}

    def event(self, ev):
        if ev.type() == QEvent.Type.ShortcutOverride:
            seq = QKeySequence(ev.keyCombination()).toString()
            if seq in self._OWN_SHORTCUTS:
                ev.accept()
                return True
        return super().event(ev)

    def keyPressEvent(self, ev):
        key = ev.key()
        ctrl = bool(ev.modifiers() & Qt.KeyboardModifier.ControlModifier)
        if key == Qt.Key.Key_Escape:
            self.cancel()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (ctrl or self.single_line):
            self.commit()
            return
        if ctrl and not (ev.modifiers() & Qt.KeyboardModifier.AltModifier):
            seq = QKeySequence(ev.keyCombination()).toString()
            st = self.state()
            mapping = {"Ctrl+B": {"bold": not st["bold"]}, "Ctrl+I": {"italic": not st["italic"]},
                       "Ctrl+U": {"underline": not st["underline"]}, "Ctrl+]": {"grow": 1}, "Ctrl+[": {"grow": -1}}
            if not self.plain:
                mapping.update({"Ctrl+E": {"align": "center"}, "Ctrl+L": {"align": "left"},
                                "Ctrl+R": {"align": "right"}, "Ctrl+J": {"align": "justify"}})
            if seq in mapping:
                self.apply(mapping[seq])
                return
        super().keyPressEvent(ev)

    def insertFromMimeData(self, source) -> None:
        # Paste plain text only, in the current formatting (never HTML from the clipboard).
        if source.hasText():
            text = source.text()
            if self.single_line:
                text = text.replace("\r", " ").replace("\n", " ")
            self.textCursor().insertText(text[:RT.MAX_CHARS])

    def focusOutEvent(self, ev):
        super().focusOutEvent(ev)
        if ev.reason() != Qt.FocusReason.PopupFocusReason:
            QTimer.singleShot(0, self._check_focus)

    def _check_focus(self) -> None:
        if self._done:
            return
        fw = QApplication.focusWidget()
        if fw is None or fw is self:
            return
        if fw.window() is not self.window():
            return  # a dialog (color picker...) or another window: keep editing
        if self.bar is not None and self.bar.owns(fw):
            return
        self.commit()

    def commit(self) -> None:
        if not self._done:
            self._done = True
            self.committed.emit(self.to_box())

    def cancel(self) -> None:
        if not self._done:
            self._done = True
            self.cancelled.emit()
