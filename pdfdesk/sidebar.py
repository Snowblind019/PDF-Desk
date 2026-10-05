"""Left sidebar panels: page thumbnails, bookmarks, comments and search results."""
from __future__ import annotations

import pymupdf as fitz
from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QInputDialog, QListView, QListWidget,
                               QListWidgetItem, QMenu, QToolButton, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget, QLabel)

from pdfdesk import ui
from pdfdesk import annots, fonts, icons, jobs
from pdfdesk.document import PdfDocument


def render_thumb(pdf: PdfDocument, pno: int, box: int, dpr: float = 1.0, night: bool = False) -> QPixmap:
    page = pdf.page(pno)
    r = page.rect
    zoom = box * dpr / max(r.width, r.height)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False, annots=True)
    if night:
        pix.invert_irect()
    img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)
    return pm


def framed(pm: QPixmap, box: int, selected_border: bool = False) -> QIcon:
    """Center a thumbnail on a transparent square with a thin border so pages line up."""
    dpr = pm.devicePixelRatio()
    canvas = QPixmap(int(box * dpr), int(box * dpr))
    canvas.setDevicePixelRatio(dpr)
    canvas.fill(Qt.GlobalColor.transparent)
    p = QPainter(canvas)
    w, h = pm.width() / dpr, pm.height() / dpr
    x, y = (box - w) / 2, (box - h) / 2
    p.fillRect(int(x) + 1, int(y) + 2, int(w), int(h), QColor(0, 0, 0, 40))
    p.drawPixmap(int(x), int(y), pm)
    p.setPen(QColor(0, 0, 0, 50))
    p.drawRect(int(x), int(y), int(w) - 1, int(h) - 1)
    p.end()
    icon = QIcon()
    # same picture when selected, so the page isn't tinted by the selection color
    icon.addPixmap(canvas, QIcon.Mode.Normal)
    icon.addPixmap(canvas, QIcon.Mode.Selected)
    icon.addPixmap(canvas, QIcon.Mode.Active)
    return icon


def placeholder(pdf: PdfDocument, pno: int, box: int) -> QIcon:
    r = pdf.page_rect(pno)
    scale = box / max(r.width, r.height)
    pm = QPixmap(max(1, int(r.width * scale)), max(1, int(r.height * scale)))
    pm.fill(QColor("#ffffff"))
    return framed(pm, box)


class ThumbList(QListWidget):
    """A list of page thumbnails rendered lazily as they scroll into view."""
    page_clicked = Signal(int)
    page_action = Signal(str, int)
    pages_dropped = Signal(list, int)  # (moved page numbers, insert-before index)

    def __init__(self, pdf: PdfDocument, box: int = 120, grid: bool = False, parent=None):
        super().__init__(parent)
        self.pdf = pdf
        self.box = box
        self.grid = grid
        self.night = False
        self._rendered: dict[int, tuple] = {}
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setSpacing(6 if not grid else 10)
        self.setWordWrap(False)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        if not grid:
            self.setFlow(QListView.Flow.TopToBottom)
            self.setWrapping(False)
        self._apply_size()
        self.itemClicked.connect(lambda it: self.page_clicked.emit(it.data(Qt.ItemDataRole.UserRole)))
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._render_visible)
        self.verticalScrollBar().valueChanged.connect(lambda _v: self._timer.start())
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        if grid:
            self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
            self.setDragEnabled(True)
            self.setAcceptDrops(True)
            self.setDropIndicatorShown(True)
            self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
            self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.rebuild()
        pdf.structure_changed.connect(self.rebuild)
        pdf.changed.connect(self._pages_changed)

    def _apply_size(self):
        self.setIconSize(QSize(self.box, self.box))
        self.setGridSize(QSize(self.box + 24, self.box + 30))

    def set_box(self, box: int) -> None:
        self.box = box
        self._apply_size()
        self._rendered.clear()
        self.rebuild()

    def rebuild(self) -> None:
        cur = self.currentRow()
        self.blockSignals(True)
        self.clear()
        for i in range(self.pdf.page_count):
            item = QListWidgetItem(placeholder(self.pdf, i, self.box), str(i + 1))
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            item.setSizeHint(QSize(self.box + 20, self.box + 26))
            self.addItem(item)
        self._rendered.clear()
        if 0 <= cur < self.count():
            self.setCurrentRow(cur)
        self.blockSignals(False)
        self._timer.start()

    def _pages_changed(self, pages) -> None:
        if pages is None:
            self._rendered.clear()
        else:
            for p in pages:
                self._rendered.pop(p, None)
        self._timer.start()

    def set_night(self, on: bool) -> None:
        self.night = on
        self._rendered.clear()
        self._timer.start()

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        self._timer.start()

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        self._timer.start()

    def _visible_rows(self) -> range:
        vp = self.viewport().rect()
        first = self.indexAt(QPoint(vp.width() // 2, 4))
        top = first.row() if first.isValid() else 0
        if not first.isValid():
            for x in range(4, vp.width(), 20):
                idx = self.indexAt(QPoint(x, 8))
                if idx.isValid():
                    top = idx.row()
                    break
        per_screen = max(1, (vp.height() // max(1, self.gridSize().height()) + 2))
        cols = max(1, vp.width() // max(1, self.gridSize().width())) if self.grid else 1
        return range(max(0, top - cols), min(self.count(), top + per_screen * cols + cols))

    def _render_visible(self) -> None:
        if not self.isVisible():
            return
        if jobs.busy():
            self._timer.start(400)
            return
        dpr = self.devicePixelRatioF()
        pk_epoch = self.pdf.epoch
        budget = 6
        for row in self._visible_rows():
            key = (pk_epoch, self.pdf.page_key(row), self.box, self.night)
            if self._rendered.get(row) == key:
                continue
            try:
                pm = render_thumb(self.pdf, row, self.box, dpr, self.night)
            except Exception:
                continue
            item = self.item(row)
            if item is not None:
                item.setIcon(framed(pm, self.box))
            self._rendered[row] = key
            budget -= 1
            if budget == 0:
                self._timer.start(10)
                return

    def set_current_page(self, pno: int) -> None:
        if 0 <= pno < self.count() and self.currentRow() != pno:
            self.blockSignals(True)
            self.setCurrentRow(pno)
            self.blockSignals(False)
            self.scrollToItem(self.item(pno), QAbstractItemView.ScrollHint.EnsureVisible)

    def selected_pages(self) -> list[int]:
        return sorted(it.data(Qt.ItemDataRole.UserRole) for it in self.selectedItems())

    def _menu(self, pos) -> None:
        item = self.itemAt(pos)
        if item is None:
            return
        pno = item.data(Qt.ItemDataRole.UserRole)
        if not item.isSelected():
            self.setCurrentItem(item)
        menu = QMenu(self)
        for label, action in (("Rotate clockwise", "rotate_cw"), ("Rotate counterclockwise", "rotate_ccw"),
                              (None, None), ("Insert blank page after", "insert_blank"),
                              ("Insert pages from file...", "insert_file"), ("Duplicate", "duplicate"),
                              ("Extract...", "extract"), (None, None), ("Delete", "delete")):
            if label is None:
                menu.addSeparator()
            else:
                menu.addAction(label, lambda a=action: self.page_action.emit(a, pno))
        menu.exec(self.viewport().mapToGlobal(pos))

    def dropEvent(self, ev) -> None:
        if ev.source() is not self:
            ev.ignore()
            return
        moved = self.selected_pages()
        target = self.indexAt(ev.position().toPoint())
        if target.isValid():
            rect = self.visualRect(target)
            before = target.row()
            if ev.position().x() > rect.center().x():
                before += 1
        else:
            before = self.count()
        ev.setDropAction(Qt.DropAction.IgnoreAction)
        ev.accept()
        if moved:
            QTimer.singleShot(0, lambda: self.pages_dropped.emit(moved, before))


class _BookmarkTree(QTreeWidget):
    dropped = Signal()

    def dropEvent(self, ev) -> None:
        super().dropEvent(ev)
        self.dropped.emit()


class BookmarkPanel(QWidget):
    go_to = Signal(int)

    def __init__(self, pdf: PdfDocument, current_page_fn, parent=None):
        super().__init__(parent)
        self.pdf = pdf
        self.current_page_fn = current_page_fn
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        bar = QHBoxLayout()
        for name, tip, fn in (("bookmark", "Add a bookmark for the current page", self.add),
                              ("square-pen", "Rename the selected bookmark", self.rename),
                              ("trash-2", "Delete the selected bookmark", self.delete)):
            b = QToolButton()
            icons.bind(b, name)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addStretch(1)
        lay.addLayout(bar)
        self.tree = _BookmarkTree()
        self.tree.setHeaderHidden(True)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.itemClicked.connect(self._clicked)
        self.tree.dropped.connect(lambda: QTimer.singleShot(0, self._save))
        self.empty = QLabel("No bookmarks yet.\nUse the bookmark button to\nadd one for the current page.")
        self.empty.setObjectName("muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.tree, 1)
        lay.addWidget(self.empty)
        self._loading = False
        self.reload()
        pdf.structure_changed.connect(self.reload)

    def reload(self) -> None:
        self._loading = True
        self.tree.clear()
        stack: list[QTreeWidgetItem] = []
        for level, title, page, *_ in self.pdf.doc.get_toc(simple=True):
            item = QTreeWidgetItem([title])
            item.setData(0, Qt.ItemDataRole.UserRole, page)
            item.setToolTip(0, ui.tip(f"{title} (page {page})"))
            while len(stack) >= level:
                stack.pop()
            if stack:
                stack[-1].addChild(item)
            else:
                self.tree.addTopLevelItem(item)
            stack.append(item)
        self.tree.expandToDepth(0)
        has = self.tree.topLevelItemCount() > 0
        self.empty.setVisible(not has)
        self._loading = False

    def _clicked(self, item, _col) -> None:
        page = item.data(0, Qt.ItemDataRole.UserRole)
        if page and page > 0:
            self.go_to.emit(page - 1)

    def _toc_from_tree(self) -> list:
        toc = []

        def walk(item, level):
            toc.append([level, item.text(0), max(1, int(item.data(0, Qt.ItemDataRole.UserRole) or 1))])
            for k in range(item.childCount()):
                walk(item.child(k), level + 1)
        for k in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(k), 1)
        return toc

    def _save(self) -> None:
        if self._loading:
            return
        toc = self._toc_from_tree()
        try:
            with self.pdf.edit("Edit bookmarks", structure=True):
                self.pdf.doc.set_toc(toc)
        except Exception as exc:
            ui.warning(self, "Bookmarks", f"Could not save bookmarks:\n{exc}")

    def add(self) -> None:
        page = self.current_page_fn() + 1
        title, ok = QInputDialog.getText(self, "Add bookmark", f"Bookmark name for page {page}:",
                                         text=f"Page {page}")
        if not ok or not title.strip():
            return
        toc = self.pdf.doc.get_toc(simple=True)
        # insert at the top level, keeping page order
        pos = len(toc)
        for k, entry in enumerate(toc):
            if entry[0] == 1 and entry[2] > page:
                pos = k
                break
        toc.insert(pos, [1, title.strip(), page])
        with self.pdf.edit("Add bookmark", structure=True):
            self.pdf.doc.set_toc(toc)

    def rename(self) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        title, ok = QInputDialog.getText(self, "Rename bookmark", "Name:", text=item.text(0))
        if ok and title.strip():
            item.setText(0, title.strip())
            self._save()

    def delete(self) -> None:
        item = self.tree.currentItem()
        if item is None:
            return
        parent = item.parent()
        # keep children by moving them up one level
        children = [item.child(k) for k in range(item.childCount())]
        idx = (parent.indexOfChild(item) if parent else self.tree.indexOfTopLevelItem(item))
        for ch in children:
            item.removeChild(ch)
        if parent:
            parent.removeChild(item)
            for k, ch in enumerate(children):
                parent.insertChild(idx + k, ch)
        else:
            self.tree.takeTopLevelItem(idx)
            for k, ch in enumerate(children):
                self.tree.insertTopLevelItem(idx + k, ch)
        self._save()


class CommentsPanel(QWidget):
    open_annot = Signal(int, int)  # page, xref
    delete_annot = Signal(int, int)

    def __init__(self, pdf: PdfDocument, parent=None):
        super().__init__(parent)
        self.pdf = pdf
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.count_label = QLabel()
        self.count_label.setObjectName("muted")
        lay.addWidget(self.count_label)
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.itemClicked.connect(self._clicked)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._menu)
        lay.addWidget(self.list, 1)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(350)
        self._timer.timeout.connect(self.reload)
        self._stale = True
        pdf.changed.connect(self._mark)

    def _mark(self, *_):
        self._stale = True
        if self.isVisible():
            self._timer.start()

    def showEvent(self, ev) -> None:
        super().showEvent(ev)
        if self._stale:
            self._timer.start(0)

    def reload(self) -> None:
        if jobs.busy():
            self._timer.start(500)
            return
        self._stale = False
        self.list.clear()
        total = 0
        for page in self.pdf.doc:
            for a in page.annots():
                if a.type[0] in annots.SKIP_TYPES:
                    continue
                info = a.info
                content = (info.get("content") or "").strip().replace("\n", " ")
                author = info.get("title") or ""
                head = f"{annots.type_name(a)}  ·  page {page.number + 1}"
                if author:
                    head += f"  ·  {author}"
                text = head + (f"\n{content[:160]}" if content else "")
                item = QListWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, (page.number, a.xref))
                stroke = (a.colors or {}).get("stroke") or (a.colors or {}).get("fill")
                if not stroke and a.type[0] == fitz.PDF_ANNOT_FREE_TEXT:
                    stroke = annots.freetext_style(self.pdf.doc, a)[1]
                pm = QPixmap(10, 10)
                pm.fill(QColor.fromRgbF(*fonts.to_rgb(stroke)) if stroke else QColor("#9a9aa0"))
                item.setIcon(QIcon(pm))
                self.list.addItem(item)
                total += 1
        self.count_label.setText(f"{total} comment{'s' if total != 1 else ''} and markup" if total
                                 else "No comments or markup in this PDF.")

    def _clicked(self, item) -> None:
        pno, xref = item.data(Qt.ItemDataRole.UserRole)
        self.open_annot.emit(pno, xref)

    def _menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is None:
            return
        pno, xref = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        menu.addAction("Go to", lambda: self.open_annot.emit(pno, xref))
        menu.addAction("Delete", lambda: self.delete_annot.emit(pno, xref))
        menu.exec(self.list.viewport().mapToGlobal(pos))


class SearchPanel(QWidget):
    hit_clicked = Signal(int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        self.label = ui.plain_label("Search with Ctrl+F")
        self.label.setObjectName("muted")
        self.label.setWordWrap(True)
        lay.addWidget(self.label)
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.list.itemClicked.connect(lambda it: self.hit_clicked.emit(*it.data(Qt.ItemDataRole.UserRole)))
        lay.addWidget(self.list, 1)

    def clear(self, text: str = "Search with Ctrl+F") -> None:
        self.list.clear()
        self.label.setText(text)

    def add_hits(self, pno: int, snippets: list[str], start_index: int = 0) -> None:
        for k, snip in enumerate(snippets):
            item = QListWidgetItem(f"Page {pno + 1}\n{snip}")
            item.setData(Qt.ItemDataRole.UserRole, (pno, start_index + k))
            self.list.addItem(item)

    def select(self, pno: int, idx: int) -> None:
        for row in range(self.list.count()):
            if self.list.item(row).data(Qt.ItemDataRole.UserRole) == (pno, idx):
                self.list.setCurrentRow(row)
                self.list.scrollToItem(self.list.item(row))
                return
