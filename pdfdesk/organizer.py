"""Organize Pages view: a big thumbnail grid for reordering, rotating, inserting and deleting pages."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSlider, QToolButton, QVBoxLayout, QWidget, QFrame

from pdfdesk import icons
from pdfdesk.document import PdfDocument
from pdfdesk.sidebar import ThumbList


class PageOrganizer(QWidget):
    action_requested = Signal(str, list)   # action, selected pages
    reorder_requested = Signal(list, int)  # pages, insert before
    close_requested = Signal()
    page_opened = Signal(int)

    def __init__(self, pdf: PdfDocument, parent=None):
        super().__init__(parent)
        self.pdf = pdf
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        bar = QFrame()
        bar.setObjectName("findBar")
        row = QHBoxLayout(bar)
        row.setContentsMargins(10, 6, 10, 6)
        title = QLabel("Organize pages")
        title.setObjectName("sectionTitle")
        row.addWidget(title)
        row.addSpacing(12)
        self.buttons = {}
        for key, ico, label in (("rotate_ccw", "rotate-ccw", "Rotate left"), ("rotate_cw", "rotate-cw", "Rotate right"),
                                ("duplicate", "copy", "Duplicate"), ("insert_blank", "file-plus", "Insert blank"),
                                ("insert_file", "file-input", "Insert from file"),
                                ("extract", "file-output", "Extract"), ("split", "scissors", "Split"),
                                ("delete", "trash-2", "Delete")):
            b = QToolButton()
            icons.bind(b, ico)
            b.setText(label)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            b.clicked.connect(lambda _=False, k=key: self._act(k))
            row.addWidget(b)
            self.buttons[key] = b
        row.addStretch(1)
        row.addWidget(QLabel("Size"))
        self.size = QSlider(Qt.Orientation.Horizontal)
        self.size.setRange(110, 320)
        self.size.setValue(170)
        self.size.setFixedWidth(120)
        self.size.sliderReleased.connect(lambda: self.grid.set_box(self.size.value()))
        row.addWidget(self.size)
        done = QToolButton()
        done.setText("Done")
        icons.bind(done, "x")
        done.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        done.clicked.connect(self.close_requested.emit)
        row.addWidget(done)
        lay.addWidget(bar)
        hint = QLabel("  Drag pages to reorder them. Ctrl or Shift click to select several. "
                      "Double-click a page to open it.")
        hint.setObjectName("muted")
        hint.setContentsMargins(10, 6, 10, 4)
        lay.addWidget(hint)
        self.grid = ThumbList(pdf, box=170, grid=True)
        self.grid.pages_dropped.connect(self.reorder_requested.emit)
        self.grid.page_action.connect(lambda a, p: self._act(a, fallback=p))
        self.grid.itemDoubleClicked.connect(lambda it: self.page_opened.emit(it.data(Qt.ItemDataRole.UserRole)))
        lay.addWidget(self.grid, 1)

    def _act(self, key: str, fallback: int | None = None) -> None:
        pages = self.grid.selected_pages()
        if not pages and fallback is not None:
            pages = [fallback]
        if not pages and key not in ("insert_file", "insert_blank", "split"):
            return
        self.action_requested.emit(key, pages)

    def keyPressEvent(self, ev) -> None:
        if ev.key() == Qt.Key.Key_Delete:
            self._act("delete")
            return
        if ev.key() == Qt.Key.Key_Escape:
            self.close_requested.emit()
            return
        super().keyPressEvent(ev)
