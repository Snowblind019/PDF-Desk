"""Dialogs for the document tools (export, split, watermark, OCR, passwords, settings...)."""
from __future__ import annotations

import os
from pathlib import Path

import pymupdf as fitz
from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QImage, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QRadioButton, QSlider, QSpinBox, QTabWidget,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from pdfdesk import ui
from pdfdesk import APP_NAME, __version__, convert, externals, pdfops
from pdfdesk.config import Settings, user_tessdata_dir
from pdfdesk.document import PdfDocument
from pdfdesk.fonts import hex_to_rgb
from pdfdesk.widgets import ColorButton, PathPicker


def _font_picker(initial: str = "Helvetica"):
    """A font list like Word's, starting on `initial`."""
    from pdfdesk import fontcatalog
    from pdfdesk.textformat import FontComboBox
    box = FontComboBox()
    box.set_family(fontcatalog.catalog().resolve(initial))
    return box


def _buttons(dlg: QDialog, ok_text: str = "OK") -> QDialogButtonBox:
    box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    box.button(QDialogButtonBox.StandardButton.Ok).setText(ok_text)
    box.button(QDialogButtonBox.StandardButton.Ok).setDefault(True)
    box.accepted.connect(dlg.accept)
    box.rejected.connect(dlg.reject)
    return box


def muted(text: str) -> QLabel:
    lab = ui.plain_label(text)
    lab.setObjectName("muted")
    lab.setWordWrap(True)
    return lab


class PageRangeEdit(QWidget):
    """'All pages' / 'Current page' / custom range picker."""

    def __init__(self, page_count: int, current: int = 0, default: str = "all", parent=None):
        super().__init__(parent)
        self.page_count = page_count
        self.current = current
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.combo = QComboBox()
        self.combo.addItems(["All pages", "Current page", "Pages:"])
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(f"e.g. 1-3, 5, 8-{page_count}")
        lay.addWidget(self.combo)
        lay.addWidget(self.edit, 1)
        self.combo.currentIndexChanged.connect(lambda i: self.edit.setEnabled(i == 2))
        if default == "current":
            self.combo.setCurrentIndex(1)
        elif default not in ("all", ""):
            self.combo.setCurrentIndex(2)
            self.edit.setText(default)
        self.edit.setEnabled(self.combo.currentIndex() == 2)

    def pages(self) -> list[int]:
        idx = self.combo.currentIndex()
        if idx == 0:
            return list(range(self.page_count))
        if idx == 1:
            return [self.current]
        return pdfops.parse_page_range(self.edit.text(), self.page_count)

    def is_all(self) -> bool:
        return self.combo.currentIndex() == 0


class _Validated(QDialog):
    """Dialog base that shows ValueError messages instead of closing."""

    def validate(self) -> None:  # raise ValueError to block
        pass

    def accept(self) -> None:
        try:
            self.validate()
        except ValueError as exc:
            ui.warning(self, self.windowTitle(), str(exc))
            return
        super().accept()


def _preview_label() -> QLabel:
    lab = QLabel()
    lab.setFixedSize(230, 300)
    lab.setAlignment(Qt.AlignmentFlag.AlignCenter)
    lab.setStyleSheet("QLabel { background: rgba(127,127,127,0.12); border-radius: 8px; }")
    return lab


def _render_preview(doc: fitz.Document, label: QLabel, overlay_rect: fitz.Rect | None = None) -> None:
    page = doc[0]
    r = page.rect
    z = min((label.width() - 16) / r.width, (label.height() - 16) / r.height)
    dpr = label.devicePixelRatioF()
    pix = page.get_pixmap(matrix=fitz.Matrix(z * dpr, z * dpr), alpha=False)
    img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)
    if overlay_rect is not None:
        from PySide6.QtGui import QPainter, QPen, QColor
        p = QPainter(pm)
        p.setPen(QPen(QColor("#c8323c"), 2, Qt.PenStyle.DashLine))
        p.drawRect(int(overlay_rect.x0 * z), int(overlay_rect.y0 * z), int(overlay_rect.width * z),
                   int(overlay_rect.height * z))
        p.end()
    label.setPixmap(pm)


# =========================================================================== page ranges

class PagesDialog(_Validated):
    def __init__(self, parent, title: str, prompt: str, page_count: int, current: int, default: str = "current",
                 options: list[tuple[str, str, bool]] | None = None, ok_text: str = "OK"):
        super().__init__(parent)
        self.setWindowTitle(title)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel(prompt))
        self.range = PageRangeEdit(page_count, current, default)
        lay.addWidget(self.range)
        self.checks = {}
        for key, label, checked in options or []:
            cb = QCheckBox(label)
            cb.setChecked(checked)
            self.checks[key] = cb
            lay.addWidget(cb)
        lay.addWidget(_buttons(self, ok_text))
        self._pages: list[int] = []

    def validate(self) -> None:
        self._pages = self.range.pages()

    def result_pages(self) -> list[int]:
        return self._pages

    def option(self, key: str) -> bool:
        return self.checks[key].isChecked()


class InsertPagesDialog(_Validated):
    def __init__(self, parent, page_count: int, current: int, path: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Insert pages")
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.picker = PathPicker("open", "Choose a file to insert", convert.import_filter())
        self.picker.set_path(path)
        form.addRow("File:", self.picker)
        self.where = QComboBox()
        self.where.addItems(["After the current page", "Before the current page", "At the beginning", "At the end",
                             "After page:"])
        self.page = QSpinBox()
        self.page.setRange(1, page_count)
        self.page.setValue(current + 1)
        row = QHBoxLayout()
        row.addWidget(self.where, 1)
        row.addWidget(self.page)
        form.addRow("Insert:", row)
        lay.addLayout(form)
        lay.addWidget(muted("Any supported file works: other PDFs, images, Word or text files..."))
        lay.addWidget(_buttons(self, "Insert"))
        self.current = current
        self.page_count = page_count
        self.where.currentIndexChanged.connect(lambda i: self.page.setEnabled(i == 4))
        self.page.setEnabled(False)

    def validate(self) -> None:
        if not self.picker.path() or not os.path.exists(self.picker.path()):
            raise ValueError("Choose a file to insert.")

    def index(self) -> int:
        return {0: self.current + 1, 1: self.current, 2: 0, 3: self.page_count}.get(
            self.where.currentIndex(), self.page.value())


# =========================================================================== split

class SplitDialog(_Validated):
    def __init__(self, parent, pdf: PdfDocument):
        super().__init__(parent)
        self.setWindowTitle("Split PDF")
        self.pdf = pdf
        lay = QVBoxLayout(self)
        self.group = QButtonGroup(self)
        self.r_every = QRadioButton("Every")
        self.every = QSpinBox()
        self.every.setRange(1, max(1, pdf.page_count))
        self.every.setValue(1 if pdf.page_count < 3 else 2)
        self.r_ranges = QRadioButton("Custom ranges, one file each:")
        self.ranges = QLineEdit()
        self.ranges.setPlaceholderText("e.g. 1-3; 4-10; 11-")
        self.r_marks = QRadioButton("By top-level bookmarks")
        has_toc = any(t[0] == 1 for t in pdf.doc.get_toc())
        self.r_marks.setEnabled(has_toc)
        for b in (self.r_every, self.r_ranges, self.r_marks):
            self.group.addButton(b)
        self.r_every.setChecked(True)
        row = QHBoxLayout()
        row.addWidget(self.r_every)
        row.addWidget(self.every)
        row.addWidget(QLabel("pages per file"))
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(self.r_ranges)
        lay.addWidget(self.ranges)
        lay.addWidget(self.r_marks)
        if not has_toc:
            lay.addWidget(muted("This PDF has no bookmarks."))
        form = QFormLayout()
        self.folder = PathPicker("folder", "Save the parts in")
        base_dir = os.path.dirname(pdf.path) if pdf.path else str(Path.home())
        self.folder.set_path(base_dir)
        self.base = QLineEdit(Path(pdf.title).stem)
        form.addRow("Save to folder:", self.folder)
        form.addRow("File name start:", self.base)
        lay.addLayout(form)
        lay.addWidget(_buttons(self, "Split"))

    def validate(self) -> None:
        if not self.folder.path():
            raise ValueError("Choose a folder for the new files.")
        if self.r_ranges.isChecked():
            parts = [p.strip() for p in self.ranges.text().replace(",", ";").split(";") if p.strip()]
            if not parts:
                raise ValueError("Type at least one page range.")
            for p in parts:
                pdfops.parse_page_range(p, self.pdf.page_count)

    def params(self) -> tuple[str, object]:
        if self.r_ranges.isChecked():
            # allow "1-3; 4-10" or one range per comma when semicolons aren't used
            text = self.ranges.text()
            parts = [p.strip() for p in (text.split(";") if ";" in text else text.split(",")) if p.strip()]
            return "ranges", parts
        if self.r_marks.isChecked():
            return "bookmarks", None
        n = self.every.value()
        return ("single", None) if n == 1 else ("every", n)


# =========================================================================== combine / create

class CombineDialog(_Validated):
    def __init__(self, parent, title: str = "Combine files", files: list[str] | None = None,
                 last_dir: str = "", ok_text: str = "Combine"):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(560, 440)
        self.last_dir = last_dir
        self.setAcceptDrops(True)
        lay = QVBoxLayout(self)
        lay.addWidget(muted("Add PDFs, images, Office documents, text files and more. They are put together "
                            "in this order. Drag files in from your file manager or reorder them below."))
        body = QHBoxLayout()
        self.list = QListWidget()
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        body.addWidget(self.list, 1)
        side = QVBoxLayout()
        for text, fn in (("Add files...", self.add_files), ("Remove", self.remove), ("Move up", lambda: self.move(-1)),
                         ("Move down", lambda: self.move(1)), ("Sort by name", self.sort)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            side.addWidget(b)
        side.addStretch(1)
        body.addLayout(side)
        lay.addLayout(body, 1)
        form = QFormLayout()
        self.paper = QComboBox()
        self.paper.addItems(["Same size as the image", "Letter", "A4"])
        form.addRow("Image pages:", self.paper)
        lay.addLayout(form)
        if not externals.libreoffice_available():
            lay.addWidget(muted("LibreOffice was not found, so .doc, .xls, .ppt and OpenDocument files can't be "
                                "converted. .docx, .xlsx and .pptx still work with a simpler layout."))
        lay.addWidget(_buttons(self, ok_text))
        for f in files or []:
            self._add(f)

    def _add(self, path: str) -> None:
        item = QListWidgetItem(Path(path).name)
        item.setToolTip(ui.tip(path))
        item.setData(Qt.ItemDataRole.UserRole, path)
        self.list.addItem(item)

    def add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Add files", self.last_dir, convert.import_filter())
        for p in paths:
            self._add(p)
        if paths:
            self.last_dir = os.path.dirname(paths[0])

    def remove(self) -> None:
        for it in self.list.selectedItems():
            self.list.takeItem(self.list.row(it))

    def move(self, step: int) -> None:
        row = self.list.currentRow()
        new = row + step
        if row < 0 or not (0 <= new < self.list.count()):
            return
        item = self.list.takeItem(row)
        self.list.insertItem(new, item)
        self.list.setCurrentRow(new)

    def sort(self) -> None:
        items = sorted((self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())),
                       key=lambda p: Path(p).name.lower())
        self.list.clear()
        for p in items:
            self._add(p)

    def dragEnterEvent(self, ev) -> None:
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev) -> None:
        for url in ev.mimeData().urls():
            if url.isLocalFile() and os.path.isfile(url.toLocalFile()):
                self._add(url.toLocalFile())

    def files(self) -> list[str]:
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())]

    def paper_key(self) -> str:
        return ["image", "letter", "a4"][self.paper.currentIndex()]

    def validate(self) -> None:
        files = self.files()
        if not files:
            raise ValueError("Add at least one file.")
        bad = [Path(f).name for f in files if not (convert.can_import(f) or f.lower().endswith(".pdf"))]
        if bad:
            raise ValueError("These files can't be converted:\n" + "\n".join(bad))


class BlankDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("New blank PDF")
        form = QFormLayout(self)
        self.size = QComboBox()
        self.size.addItems(["Letter (8.5 x 11 in)", "A4 (210 x 297 mm)", "Legal (8.5 x 14 in)", "A5", "Tabloid"])
        self.orient = QComboBox()
        self.orient.addItems(["Portrait", "Landscape"])
        self.count = QSpinBox()
        self.count.setRange(1, 500)
        self.count.setValue(1)
        form.addRow("Page size:", self.size)
        form.addRow("Orientation:", self.orient)
        form.addRow("Pages:", self.count)
        form.addRow(_buttons(self, "Create"))

    def make(self) -> bytes:
        name = ["letter", "a4", "legal", "a5", "tabloid"][self.size.currentIndex()]
        r = fitz.paper_rect(name)
        w, h = (r.height, r.width) if self.orient.currentIndex() == 1 else (r.width, r.height)
        doc = fitz.open()
        for _ in range(self.count.value()):
            doc.new_page(width=w, height=h)
        return doc.tobytes()


# =========================================================================== export

class ExportDialog(_Validated):
    def __init__(self, parent, pdf: PdfDocument, current: int, fmt: str = "docx"):
        super().__init__(parent)
        self.setWindowTitle("Export PDF")
        self.pdf = pdf
        self.resize(520, 0)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.fmt = QComboBox()
        lo = externals.libreoffice_available()
        for key, label, ext, kind in convert.EXPORT_FORMATS:
            text = label + ("" if lo or key not in convert.NEEDS_LIBREOFFICE else "  (needs LibreOffice)")
            self.fmt.addItem(text, key)
            if key in convert.NEEDS_LIBREOFFICE and not lo:
                self.fmt.model().item(self.fmt.count() - 1).setEnabled(False)
        form.addRow("Format:", self.fmt)
        self.range = PageRangeEdit(pdf.page_count, current)
        form.addRow("Pages:", self.range)
        self.dpi = QComboBox()
        for d in (72, 96, 150, 200, 300, 600):
            self.dpi.addItem(f"{d} dpi", d)
        self.dpi.setCurrentIndex(2)
        self.dpi_label = QLabel("Resolution:")
        form.addRow(self.dpi_label, self.dpi)
        self.quality = QSpinBox()
        self.quality.setRange(10, 100)
        self.quality.setValue(90)
        self.quality_label = QLabel("Quality:")
        form.addRow(self.quality_label, self.quality)
        self.markers = QCheckBox("Add a '===== Page N =====' line before each page")
        form.addRow("", self.markers)
        self.target = PathPicker("save", "Save as")
        self.target_label = QLabel("Save as:")
        form.addRow(self.target_label, self.target)
        lay.addLayout(form)
        self.note = muted("")
        lay.addWidget(self.note)
        lay.addWidget(_buttons(self, "Export"))
        idx = self.fmt.findData(fmt)
        self.fmt.setCurrentIndex(max(0, idx))
        self.fmt.currentIndexChanged.connect(self._update)
        self._update()

    def key(self) -> str:
        return self.fmt.currentData()

    def _update(self) -> None:
        key, label, ext, kind = convert.export_format(self.key())
        images = key in ("png", "jpg", "webp", "tiff", "pptx")
        self.dpi.setVisible(images)
        self.dpi_label.setVisible(images)
        q = key in ("jpg", "webp")
        self.quality.setVisible(q)
        self.quality_label.setVisible(q)
        self.markers.setVisible(key == "txt")
        base = self.pdf.path or self.pdf.suggested_path or str(Path.home() / self.pdf.title)
        folder, stem = os.path.dirname(base), Path(base).stem
        if kind == "folder":
            self.target.mode = "folder"
            self.target_label.setText("Save into folder:")
            self.target.set_path(os.path.join(folder, f"{stem} pages"))
        else:
            self.target.mode = "save"
            self.target.file_filter = f"{label} (*{ext})"
            self.target_label.setText("Save as:")
            self.target.set_path(os.path.join(folder, stem + ext))
        notes = {
            "docx": "Text, images and tables are rebuilt as an editable Word document. Complex layouts may "
                    "need touching up.",
            "xlsx": "Tables found in the PDF become sheets. Pages without tables are split into rows and columns.",
            "pptx": "Each page becomes a slide (as a picture). The page text is added to the slide notes.",
            "md": "Headings, bold/italic text, lists and tables are kept.",
            "html": "Text and images are kept, one section per page.",
            "svg": "Each page becomes a scalable vector image.",
            "tiff": "All pages are saved in a single multi-page TIFF file.",
        }
        self.note.setText(notes.get(key, ""))

    def validate(self) -> None:
        self._pages = self.range.pages()
        if not self.target.path():
            raise ValueError("Choose where to save the export.")

    def params(self) -> dict:
        return {"fmt": self.key(), "target": self.target.path(), "pages": self._pages,
                "dpi": self.dpi.currentData(), "quality": self.quality.value(),
                "page_markers": self.markers.isChecked()}


# =========================================================================== compress

class CompressDialog(QDialog):
    def __init__(self, parent, pdf: PdfDocument):
        super().__init__(parent)
        self.setWindowTitle("Reduce file size")
        lay = QVBoxLayout(self)
        size = len(pdf.doc.tobytes(garbage=0)) if pdf.page_count < 3000 else 0
        if pdf.path and os.path.exists(pdf.path):
            size = os.path.getsize(pdf.path)
        lay.addWidget(QLabel(f"Current size: {pdfops.file_size_text(size)}"))
        self.group = QButtonGroup(self)
        self.opts = []
        for key, title, desc in (
                ("lossless", "Clean up only", "Removes unused data and compresses streams. No quality loss."),
                ("medium", "Balanced (recommended)", "Images above 150 dpi are downsampled. Good for sharing."),
                ("high", "Smallest file", "Images are reduced to 96 dpi with stronger compression."),
                ("custom", "Custom", "")):
            rb = QRadioButton(title)
            self.group.addButton(rb)
            self.opts.append((key, rb))
            lay.addWidget(rb)
            if desc:
                d = muted(desc)
                d.setContentsMargins(24, 0, 0, 4)
                lay.addWidget(d)
        self.opts[1][1].setChecked(True)
        row = QHBoxLayout()
        row.setContentsMargins(24, 0, 0, 0)
        self.dpi = QSpinBox()
        self.dpi.setRange(36, 600)
        self.dpi.setValue(120)
        self.dpi.setSuffix(" dpi")
        self.quality = QSpinBox()
        self.quality.setRange(10, 100)
        self.quality.setValue(70)
        self.quality.setPrefix("JPEG quality ")
        row.addWidget(self.dpi)
        row.addWidget(self.quality)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(_buttons(self, "Reduce"))

    def params(self) -> dict:
        level = next(k for k, rb in self.opts if rb.isChecked())
        return {"level": level, "dpi": self.dpi.value(), "quality": self.quality.value()}


# =========================================================================== protect

class ProtectDialog(_Validated):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Protect with password")
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.user = QLineEdit()
        self.user.setEchoMode(QLineEdit.EchoMode.Password)
        self.user2 = QLineEdit()
        self.user2.setEchoMode(QLineEdit.EchoMode.Password)
        self.owner = QLineEdit()
        self.owner.setEchoMode(QLineEdit.EchoMode.Password)
        self.show_pw = QCheckBox("Show passwords")
        self.show_pw.toggled.connect(self._toggle)
        form.addRow("Password to open:", self.user)
        form.addRow("Repeat it:", self.user2)
        form.addRow("Permissions password:", self.owner)
        form.addRow("", self.show_pw)
        lay.addLayout(form)
        lay.addWidget(muted("The permissions password (optional) lets you limit what people can do once the file "
                            "is open. If you leave the first password empty, anyone can open the file but the "
                            "limits below still apply."))
        box = QGroupBox("Allow people to")
        g = QVBoxLayout(box)
        self.allow = {}
        for key, label in (("print", "Print"), ("copy", "Copy text and images"), ("modify", "Change the document"),
                           ("annotate", "Add comments"), ("forms", "Fill in forms"),
                           ("assemble", "Insert, delete and rotate pages")):
            cb = QCheckBox(label)
            cb.setChecked(True)
            g.addWidget(cb)
            self.allow[key] = cb
        lay.addWidget(box)
        lay.addWidget(muted("Uses AES-256 encryption. Don't lose the password, it can't be recovered."))
        lay.addWidget(_buttons(self, "Protect"))

    def _toggle(self, on: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password
        for w in (self.user, self.user2, self.owner):
            w.setEchoMode(mode)

    def validate(self) -> None:
        if self.user.text() != self.user2.text():
            raise ValueError("The two passwords don't match.")
        if not self.user.text() and not self.owner.text():
            raise ValueError("Enter a password to open the file, a permissions password, or both.")

    def params(self) -> dict:
        return {"user_pw": self.user.text(), "owner_pw": self.owner.text(),
                **{f"allow_{k}": cb.isChecked() for k, cb in self.allow.items()}}


# =========================================================================== watermark

class WatermarkDialog(_Validated):
    def __init__(self, parent, pdf: PdfDocument, current: int):
        super().__init__(parent)
        self.setWindowTitle("Add watermark")
        self.pdf = pdf
        self.current = current
        outer = QHBoxLayout(self)
        left = QVBoxLayout()
        self.tabs = QTabWidget()
        text_tab = QWidget()
        f = QFormLayout(text_tab)
        self.text = QLineEdit("CONFIDENTIAL")
        self.size = QSpinBox()
        self.size.setRange(6, 300)
        self.size.setValue(60)
        self.color = ColorButton("#c8323c")
        self.bold = QCheckBox("Bold")
        self.bold.setChecked(True)
        self.font = _font_picker("Helvetica")
        f.addRow("Text:", self.text)
        f.addRow("Font:", self.font)
        f.addRow("Font size:", self.size)
        f.addRow("Color:", self.color)
        f.addRow("", self.bold)
        img_tab = QWidget()
        g = QFormLayout(img_tab)
        self.image = PathPicker("open", "Choose an image", "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff)")
        self.scale = QSpinBox()
        self.scale.setRange(5, 100)
        self.scale.setValue(50)
        self.scale.setSuffix(" % of page width")
        g.addRow("Image:", self.image)
        g.addRow("Size:", self.scale)
        self.tabs.addTab(text_tab, "Text")
        self.tabs.addTab(img_tab, "Image")
        left.addWidget(self.tabs)
        common = QFormLayout()
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(5, 100)
        self.opacity.setValue(25)
        self.angle = QSpinBox()
        self.angle.setRange(-180, 180)
        self.angle.setValue(45)
        self.angle.setSuffix(" degrees")
        self.behind = QComboBox()
        self.behind.addItems(["On top of the page", "Behind the page content"])
        self.range = PageRangeEdit(pdf.page_count, current)
        common.addRow("Opacity:", self.opacity)
        common.addRow("Rotation:", self.angle)
        common.addRow("Place:", self.behind)
        common.addRow("Pages:", self.range)
        left.addLayout(common)
        left.addStretch(1)
        left.addWidget(_buttons(self, "Add watermark"))
        outer.addLayout(left, 1)
        self.preview = _preview_label()
        outer.addWidget(self.preview)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._update_preview)
        for sig in (self.text.textChanged, self.size.valueChanged, self.color.color_changed, self.bold.toggled,
                    self.font.font_chosen,
                    self.opacity.valueChanged, self.angle.valueChanged, self.behind.currentIndexChanged,
                    self.tabs.currentChanged, self.image.changed, self.scale.valueChanged):
            sig.connect(lambda *_: self._timer.start())
        self._timer.start(0)

    def apply(self, doc: fitz.Document, pages: list[int]) -> None:
        if self.tabs.currentIndex() == 0:
            pdfops.add_text_watermark(doc, pages, self.text.text(), self.size.value(), hex_to_rgb(self.color.color()),
                                      self.opacity.value() / 100, self.angle.value(),
                                      behind=self.behind.currentIndex() == 1, bold=self.bold.isChecked(),
                                      family=self.font.family() or "Helvetica")
        else:
            pdfops.add_image_watermark(doc, pages, self.image.path(), self.scale.value() / 100,
                                       self.opacity.value() / 100, behind=self.behind.currentIndex() == 1)

    def _update_preview(self) -> None:
        try:
            tmp = fitz.open("pdf", pdfops.extract_pages_bytes(self.pdf.doc, [self.current]))
            if self.tabs.currentIndex() == 0 and self.text.text().strip() or \
                    self.tabs.currentIndex() == 1 and os.path.exists(self.image.path()):
                self.apply(tmp, [0])
            _render_preview(tmp, self.preview)
        except Exception:
            pass

    def validate(self) -> None:
        self._pages = self.range.pages()
        if self.tabs.currentIndex() == 0 and not self.text.text().strip():
            raise ValueError("Type the watermark text.")
        if self.tabs.currentIndex() == 1 and not os.path.exists(self.image.path()):
            raise ValueError("Choose an image for the watermark.")


# =========================================================================== header & footer

HF_PRESETS = [
    ("Custom", {}),
    ("Page X of Y (bottom center)", {"footer_center": "Page {page} of {pages}"}),
    ("Page number (bottom right)", {"footer_right": "{page}"}),
    ("Page number (bottom center)", {"footer_center": "{page}"}),
    ("File name and date (top)", {"header_left": "{filename}", "header_right": "{date}"}),
    ("Bates number (bottom right)", {"footer_right": "{bates}"}),
]


class HeaderFooterDialog(_Validated):
    def __init__(self, parent, pdf: PdfDocument, current: int):
        super().__init__(parent)
        self.setWindowTitle("Header, footer and page numbers")
        self.pdf = pdf
        self.current = current
        outer = QHBoxLayout(self)
        left = QVBoxLayout()
        self.preset = QComboBox()
        for name, _ in HF_PRESETS:
            self.preset.addItem(name)
        self.preset.currentIndexChanged.connect(self._preset)
        row = QHBoxLayout()
        row.addWidget(QLabel("Quick setup:"))
        row.addWidget(self.preset, 1)
        left.addLayout(row)
        grid = QGridLayout()
        self.slots = {}
        for c, name in enumerate(("Left", "Center", "Right")):
            grid.addWidget(QLabel(name), 0, c + 1)
        for r, part in enumerate(("header", "footer")):
            grid.addWidget(QLabel(part.capitalize() + ":"), r + 1, 0)
            for c, side in enumerate(("left", "center", "right")):
                e = QLineEdit()
                grid.addWidget(e, r + 1, c + 1)
                self.slots[f"{part}_{side}"] = e
        left.addLayout(grid)
        left.addWidget(muted("You can use {page}, {pages}, {date}, {filename}, {title} and {bates} in the text."))
        form = QFormLayout()
        self.size = QSpinBox()
        self.size.setRange(5, 48)
        self.size.setValue(10)
        self.color = ColorButton("#1d1d1f")
        self.margin = QSpinBox()
        self.margin.setRange(4, 200)
        self.margin.setValue(28)
        self.margin.setSuffix(" pt")
        self.start = QSpinBox()
        self.start.setRange(0, 100000)
        self.start.setValue(1)
        self.font = _font_picker("Helvetica")
        self.range = PageRangeEdit(pdf.page_count, current)
        form.addRow("Font:", self.font)
        form.addRow("Size:", self.size)
        form.addRow("Color:", self.color)
        form.addRow("Distance from edge:", self.margin)
        form.addRow("First page number:", self.start)
        bates = QHBoxLayout()
        self.bates_prefix = QLineEdit("ABC")
        self.bates_prefix.setMaximumWidth(90)
        self.bates_start = QSpinBox()
        self.bates_start.setRange(0, 999999999)
        self.bates_start.setValue(1)
        self.bates_digits = QSpinBox()
        self.bates_digits.setRange(1, 12)
        self.bates_digits.setValue(6)
        for label, w in (("Prefix", self.bates_prefix), ("Start", self.bates_start), ("Digits", self.bates_digits)):
            bates.addWidget(QLabel(label))
            bates.addWidget(w)
        bates.addStretch(1)
        bates_host = QWidget()
        bates.setContentsMargins(0, 0, 0, 0)
        bates_host.setLayout(bates)
        form.addRow("Bates number:", bates_host)
        form.addRow("Pages:", self.range)
        left.addLayout(form)
        left.addStretch(1)
        left.addWidget(_buttons(self, "Add"))
        outer.addLayout(left, 1)
        self.preview = _preview_label()
        outer.addWidget(self.preview)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._update_preview)
        for e in self.slots.values():
            e.textChanged.connect(lambda *_: self._timer.start())
        for sig in (self.size.valueChanged, self.color.color_changed, self.margin.valueChanged,
                    self.start.valueChanged, self.font.font_chosen, self.bates_prefix.textChanged,
                    self.bates_start.valueChanged, self.bates_digits.valueChanged):
            sig.connect(lambda *_: self._timer.start())
        self.preset.setCurrentIndex(1)

    def _preset(self, idx: int) -> None:
        values = HF_PRESETS[idx][1]
        if idx == 0:
            return
        for k, e in self.slots.items():
            e.blockSignals(True)
            e.setText(values.get(k, ""))
            e.blockSignals(False)
        self._timer.start()

    def family(self) -> str:
        return self.font.family() or "Helvetica"

    def bates(self) -> dict:
        return {"bates_prefix": self.bates_prefix.text()[:40], "bates_start": self.bates_start.value(),
                "bates_digits": self.bates_digits.value()}

    def apply(self, doc: fitz.Document, pages: list[int], filename: str) -> None:
        pdfops.add_header_footer(doc, pages, {k: e.text() for k, e in self.slots.items()}, self.size.value(),
                                 hex_to_rgb(self.color.color()), self.margin.value(), self.start.value(),
                                 self.family(), filename, **self.bates())

    def _update_preview(self) -> None:
        try:
            tmp = fitz.open("pdf", pdfops.extract_pages_bytes(self.pdf.doc, [self.current]))
            # number the preview as it will be numbered in the real document
            slots = {k: e.text().replace("{page}", str(self.current + self.start.value()))
                     .replace("{pages}", str(self.pdf.page_count + self.start.value() - 1))
                     for k, e in self.slots.items()}
            pdfops.add_header_footer(tmp, [0], slots, self.size.value(), hex_to_rgb(self.color.color()),
                                     self.margin.value(), self.start.value(), self.family(), self.pdf.title,
                                     **self.bates())
            _render_preview(tmp, self.preview)
        except Exception:
            pass

    def validate(self) -> None:
        self._pages = self.range.pages()
        if not any(e.text().strip() for e in self.slots.values()):
            raise ValueError("Type something for the header or footer.")


# =========================================================================== crop

class CropDialog(_Validated):
    UNITS = {"mm": 72 / 25.4, "in": 72.0, "pt": 1.0}

    def __init__(self, parent, pdf: PdfDocument, current: int):
        super().__init__(parent)
        self.setWindowTitle("Crop pages")
        self.pdf = pdf
        self.current = current
        self.reset_requested = False
        outer = QHBoxLayout(self)
        left = QVBoxLayout()
        form = QFormLayout()
        self.unit = QComboBox()
        self.unit.addItems(["mm", "in", "pt"])
        self.unit.currentIndexChanged.connect(self._unit_changed)
        form.addRow("Units:", self.unit)
        self.m = {}
        for side in ("Left", "Top", "Right", "Bottom"):
            sp = QDoubleSpinBox()
            sp.setRange(0, 2000)
            sp.setDecimals(1)
            sp.setValue(10)
            sp.valueChanged.connect(lambda *_: self._timer.start())
            self.m[side.lower()] = sp
            form.addRow(f"{side}:", sp)
        self.range = PageRangeEdit(pdf.page_count, current, "all")
        form.addRow("Pages:", self.range)
        left.addLayout(form)
        left.addWidget(muted("Cropping hides the edges of the page. The content is still in the file and can be "
                             "brought back with Remove crop."))
        reset = QPushButton("Remove crop from these pages")
        reset.clicked.connect(self._reset)
        left.addWidget(reset)
        left.addStretch(1)
        left.addWidget(_buttons(self, "Crop"))
        outer.addLayout(left, 1)
        self.preview = _preview_label()
        outer.addWidget(self.preview)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._update_preview)
        self._last_unit = "mm"
        self._timer.start(0)

    def _unit_changed(self) -> None:
        new = self.unit.currentText()
        f = self.UNITS[self._last_unit] / self.UNITS[new]
        for sp in self.m.values():
            sp.setValue(sp.value() * f)
        self._last_unit = new

    def margins_pt(self) -> tuple[float, float, float, float]:
        f = self.UNITS[self.unit.currentText()]
        return tuple(self.m[s].value() * f for s in ("left", "top", "right", "bottom"))

    def _update_preview(self) -> None:
        try:
            tmp = fitz.open("pdf", pdfops.extract_pages_bytes(self.pdf.doc, [self.current]))
            l, t, r, b = self.margins_pt()
            pr = tmp[0].rect
            _render_preview(tmp, self.preview, fitz.Rect(l, t, pr.width - r, pr.height - b))
        except Exception:
            pass

    def _reset(self) -> None:
        try:
            self._pages = self.range.pages()
        except ValueError as exc:
            ui.warning(self, self.windowTitle(), str(exc))
            return
        self.reset_requested = True
        super().accept()

    def validate(self) -> None:
        self._pages = self.range.pages()


# =========================================================================== OCR

class OcrDialog(_Validated):
    def __init__(self, parent, pdf: PdfDocument, current: int, settings: Settings):
        super().__init__(parent)
        self.setWindowTitle("Recognize text (OCR)")
        self.settings = settings
        lay = QVBoxLayout(self)
        folder, langs = externals.find_tessdata()
        self.folder = folder
        lay.addWidget(muted("Finds the words in scanned pages and adds an invisible text layer, so you can "
                            "search, select and copy text. The pages look exactly the same."))
        form = QFormLayout()
        self.lang = QComboBox()
        for code in langs:
            self.lang.addItem(externals.language_label(code), code)
        want = settings.get("ocr_language") or "eng"
        if "+" in want:
            want = want.split("+")[0]
        idx = self.lang.findData(want)
        self.lang.setCurrentIndex(max(0, idx))
        self.lang2 = QComboBox()
        self.lang2.addItem("(none)", "")
        for code in langs:
            self.lang2.addItem(externals.language_label(code), code)
        form.addRow("Language:", self.lang)
        form.addRow("Second language:", self.lang2)
        self.dpi = QComboBox()
        for d, label in ((200, "200 dpi (faster)"), (300, "300 dpi (recommended)"), (400, "400 dpi (small print)")):
            self.dpi.addItem(label, d)
        self.dpi.setCurrentIndex(1)
        form.addRow("Detail:", self.dpi)
        self.range = PageRangeEdit(pdf.page_count, current, "all")
        form.addRow("Pages:", self.range)
        self.skip = QCheckBox("Skip pages that already have text")
        self.skip.setChecked(True)
        form.addRow("", self.skip)
        lay.addLayout(form)
        if not langs:
            lay.addWidget(muted(
                "No OCR language files were found.\n\n"
                "Fedora:  sudo dnf install tesseract-langpack-eng tesseract-langpack-ron\n"
                "Ubuntu/Debian:  sudo apt install tesseract-ocr-eng tesseract-ocr-ron\n"
                "Windows: install Tesseract OCR (UB Mannheim build), or copy .traineddata files into "
                "the folder below. Then reopen this window."))
            b = QPushButton("Open the language folder")
            b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(user_tessdata_dir()))))
            lay.addWidget(b)
        else:
            lay.addWidget(muted(f"Language files: {folder}"))
        self.ok_box = _buttons(self, "Recognize text")
        self.ok_box.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(langs))
        lay.addWidget(self.ok_box)

    def validate(self) -> None:
        self._pages = self.range.pages()

    def params(self) -> dict:
        lang = self.lang.currentData()
        second = self.lang2.currentData()
        if second and second != lang:
            lang = f"{lang}+{second}"
        self.settings.set("ocr_language", lang)
        return {"language": lang, "tessdata": self.folder, "dpi": self.dpi.currentData(), "pages": self._pages,
                "skip_text_pages": self.skip.isChecked()}


# =========================================================================== find & redact

class FindRedactDialog(_Validated):
    def __init__(self, parent, pdf: PdfDocument, current: int):
        super().__init__(parent)
        self.setWindowTitle("Find and redact")
        lay = QVBoxLayout(self)
        lay.addWidget(muted("Marks every match for redaction. Marked areas show with a red outline. Nothing is "
                            "removed until you apply the redactions."))
        form = QFormLayout()
        self.text = QLineEdit()
        self.text.setPlaceholderText("Word or phrase")
        self.whole = QCheckBox("Whole words only")
        form.addRow("Find:", self.text)
        form.addRow("", self.whole)
        lay.addLayout(form)
        box = QGroupBox("Also find")
        g = QVBoxLayout(box)
        self.presets = {}
        for name in pdfops.REDACT_PRESETS:
            cb = QCheckBox(name)
            g.addWidget(cb)
            self.presets[name] = cb
        lay.addWidget(box)
        form2 = QFormLayout()
        self.regex = QLineEdit()
        self.regex.setPlaceholderText("Optional regular expression")
        form2.addRow("Pattern:", self.regex)
        self.range = PageRangeEdit(pdf.page_count, current, "all")
        form2.addRow("Pages:", self.range)
        self.apply_now = QCheckBox("Apply the redactions right away")
        form2.addRow("", self.apply_now)
        lay.addLayout(form2)
        lay.addWidget(_buttons(self, "Mark matches"))

    def validate(self) -> None:
        import re
        self._pages = self.range.pages()
        if self.regex.text().strip():
            try:
                re.compile(self.regex.text())
            except re.error as exc:
                raise ValueError(f"The pattern isn't valid: {exc}")
        if not (self.text.text().strip() or self.regex.text().strip() or any(cb.isChecked() for cb in self.presets.values())):
            raise ValueError("Type something to find or tick one of the options.")

    def searches(self) -> list[dict]:
        out = []
        if self.text.text().strip():
            out.append({"needle": self.text.text().strip(), "whole_word": self.whole.isChecked()})
        for name, cb in self.presets.items():
            if cb.isChecked():
                out.append({"regex": pdfops.REDACT_PRESETS[name]})
        if self.regex.text().strip():
            out.append({"regex": self.regex.text().strip()})
        return out


# =========================================================================== properties

class PropertiesDialog(QDialog):
    def __init__(self, parent, pdf: PdfDocument, current: int):
        super().__init__(parent)
        self.setWindowTitle("Document properties")
        self.pdf = pdf
        self.resize(520, 0)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        desc = QWidget()
        f = QFormLayout(desc)
        meta = pdf.doc.metadata or {}
        self.fields = {}
        for key, label in (("title", "Title"), ("author", "Author"), ("subject", "Subject"), ("keywords", "Keywords")):
            e = QLineEdit(meta.get(key) or "")
            f.addRow(f"{label}:", e)
            self.fields[key] = e
        for key, label in (("creator", "Created with"), ("producer", "PDF producer")):
            f.addRow(f"{label}:", ui.plain_label(meta.get(key) or "-"))
        tabs.addTab(desc, "Description")
        info = QWidget()
        g = QFormLayout(info)
        size = os.path.getsize(pdf.path) if pdf.path and os.path.exists(pdf.path) else None

        def fmt_date(s):
            if not s:
                return "-"
            s = s.replace("D:", "")
            try:
                return f"{s[0:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}"
            except Exception:
                return s
        rows = [
            ("File", pdf.path or "(not saved yet)"),
            ("File size", pdfops.file_size_text(size) if size is not None else "-"),
            ("Pages", str(pdf.page_count)),
            ("Page size", pdfops.page_size_text(pdf.page_rect(current)) + f" (page {current + 1})"),
            ("PDF version", meta.get("format") or "-"),
            ("Created", fmt_date(meta.get("creationDate"))),
            ("Modified", fmt_date(meta.get("modDate"))),
            ("Security", meta.get("encryption") or "None"),
            ("Form fields", "Yes" if pdf.doc.is_form_pdf else "No"),
            ("Tagged", "Yes" if (pdf.doc.markinfo or {}).get("Marked") else "No"),
        ]
        for label, value in rows:
            lab = ui.plain_label(value)
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            lab.setWordWrap(True)
            g.addRow(f"{label}:", lab)
        tabs.addTab(info, "Details")
        fonts_tab = QWidget()
        fl = QVBoxLayout(fonts_tab)
        self.font_list = QListWidget()
        btn = QPushButton("List fonts")
        btn.clicked.connect(self._fonts)
        fl.addWidget(btn)
        fl.addWidget(self.font_list, 1)
        tabs.addTab(fonts_tab, "Fonts")
        lay.addWidget(tabs)
        lay.addWidget(_buttons(self, "Save"))

    def _fonts(self) -> None:
        self.font_list.clear()
        seen = set()
        for pno in range(min(self.pdf.page_count, 500)):
            for xref, ext, ftype, base, name, enc, *_ in self.pdf.doc.get_page_fonts(pno):
                key = (base, ftype)
                if key in seen:
                    continue
                seen.add(key)
                clean = base.split("+", 1)[-1] if "+" in base else base
                embedded = "embedded" if ext and ext != "n/a" else "not embedded"
                self.font_list.addItem(f"{clean}  ({ftype}, {embedded})")
        if not seen:
            self.font_list.addItem("No fonts (this may be a scanned PDF)")

    def metadata(self) -> dict:
        meta = dict(self.pdf.doc.metadata or {})
        for k, e in self.fields.items():
            meta[k] = e.text()
        return {k: v for k, v in meta.items() if k in ("title", "author", "subject", "keywords", "creator",
                                                        "producer", "creationDate", "modDate")}


# =========================================================================== settings

class SettingsDialog(QDialog):
    def __init__(self, parent, settings: Settings):
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.settings = settings
        self.resize(560, 0)
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        gen = QWidget()
        f = QFormLayout(gen)
        self.theme = QComboBox()
        self.theme.addItems(["Follow system", "Light", "Dark"])
        self.theme.setCurrentIndex(["system", "light", "dark"].index(settings.get("theme") or "system"))
        f.addRow("Theme:", self.theme)
        self.zoom = QComboBox()
        for label, val in (("Fit width", "fit_width"), ("Fit page", "fit_page"), ("75%", "75"), ("100%", "100"),
                           ("125%", "125"), ("150%", "150")):
            self.zoom.addItem(label, val)
        self.zoom.setCurrentIndex(max(0, self.zoom.findData(str(settings.get("default_zoom")))))
        f.addRow("Open documents at:", self.zoom)
        self.layout_mode = QComboBox()
        for label, val in (("One page, scrolling", "single"), ("Two pages side by side", "facing"),
                           ("Two pages with cover page", "cover")):
            self.layout_mode.addItem(label, val)
        self.layout_mode.setCurrentIndex(max(0, self.layout_mode.findData(settings.get("layout"))))
        f.addRow("Page layout:", self.layout_mode)
        self.restore = QCheckBox("Reopen files at the page where I left off")
        self.restore.setChecked(bool(settings.get("restore_page")))
        f.addRow("", self.restore)
        self.forms = QCheckBox("Highlight form fields")
        self.forms.setChecked(bool(settings.get("highlight_forms")))
        f.addRow("", self.forms)
        self.author = QLineEdit(settings.get("author") or "")
        f.addRow("Name on comments:", self.author)
        self.limit = QSpinBox()
        self.limit.setRange(5, 200)
        self.limit.setValue(int(settings.get("recents_limit") or 40))
        f.addRow("Recent files to keep:", self.limit)
        self.updates = QCheckBox("Check for updates when PDF Desk starts (once a day)")
        self.updates.setChecked(bool(settings.get("check_updates")))
        self.updates.setToolTip("Asks GitHub whether there's a newer PDF Desk. Nothing about you or your files "
                                "is sent. Help > Check for updates always works.")
        f.addRow("", self.updates)
        tabs.addTab(gen, "General")

        ext = QWidget()
        e = QFormLayout(ext)
        self.lo = PathPicker("open", "Find LibreOffice (soffice)")
        self.lo.set_path(settings.get("libreoffice_path") or "")
        self.lo.edit.setPlaceholderText("Found automatically")
        e.addRow("LibreOffice:", self.lo)
        lo_cmd = externals.libreoffice_command()
        e.addRow("", muted(("Found: " + " ".join(lo_cmd)) if lo_cmd else
                           "Not found. Needed for .doc/.xls/.ppt/OpenDocument files and exporting to .odt/.rtf."))
        self.tess = PathPicker("folder", "Folder with .traineddata files")
        self.tess.set_path(settings.get("tessdata_path") or "")
        self.tess.edit.setPlaceholderText("Found automatically")
        e.addRow("OCR languages:", self.tess)
        folder, langs = externals.find_tessdata()
        e.addRow("", muted((f"Found {len(langs)} language(s) in {folder}: " + ", ".join(langs)) if langs else
                           "No OCR language files found. See Help > Optional extras."))
        tabs.addTab(ext, "Extras")
        lay.addWidget(tabs)
        lay.addWidget(_buttons(self, "Save"))

    def save(self) -> None:
        s = self.settings
        s.set("theme", ["system", "light", "dark"][self.theme.currentIndex()])
        s.set("default_zoom", self.zoom.currentData())
        s.set("layout", self.layout_mode.currentData())
        s.set("restore_page", self.restore.isChecked())
        s.set("highlight_forms", self.forms.isChecked())
        s.set("author", self.author.text().strip())
        s.set("recents_limit", self.limit.value())
        s.set("check_updates", self.updates.isChecked())
        s.set("libreoffice_path", self.lo.path())
        s.set("tessdata_path", self.tess.path())


# =========================================================================== help

SHORTCUTS = [
    ("Open", "Ctrl+O"), ("New blank PDF", "Ctrl+N"), ("Save", "Ctrl+S"), ("Save as", "Ctrl+Shift+S"),
    ("Print", "Ctrl+P"), ("Close tab", "Ctrl+W"), ("Next / previous tab", "Ctrl+Tab / Ctrl+Shift+Tab"),
    ("Home screen", "Ctrl+H"), ("Undo / redo", "Ctrl+Z / Ctrl+Y"), ("Copy selected text", "Ctrl+C"),
    ("Select all text on the page", "Ctrl+A"), ("Find", "Ctrl+F"), ("Next / previous match", "F3 / Shift+F3"),
    ("Zoom in / out", "Ctrl++ / Ctrl+-  (or Ctrl+mouse wheel)"), ("Actual size", "Ctrl+0"),
    ("Fit width / fit page", "Ctrl+1 / Ctrl+2"), ("Go to page", "Ctrl+G"), ("First / last page", "Home / End"),
    ("Rotate page", "Ctrl+R / Ctrl+Shift+R"), ("Add bookmark", "Ctrl+B"), ("Sidebar", "F4"),
    ("Organize pages", "Ctrl+Shift+O"), ("Full screen", "F11"), ("Presentation", "F5 (Esc to leave)"),
    ("Presentation: next / previous page", "Space, arrows or click / Backspace or right-click"),
    ("Presentation: dark pages on / off", "B"), ("Reader view", "Ctrl+4"),
    ("Read this page out loud", "Ctrl+Shift+Y"), ("Read to the end", "Ctrl+Shift+B"),
    ("Stop reading", "Ctrl+Shift+E"), ("Select tool", "Esc"), ("Delete selected comment", "Delete"),
    ("Nudge selected item", "Arrow keys (Shift = 10x)"),
    ("Finish typing in a text box", "Ctrl+Enter or click outside"),
    ("Text box: bold / italic / underline", "Ctrl+B / Ctrl+I / Ctrl+U (while typing)"),
    ("Text box: bigger / smaller text", "Ctrl+] / Ctrl+[ (while typing)"),
    ("Text box: align left / center / right / justify", "Ctrl+L / Ctrl+E / Ctrl+R / Ctrl+J (while typing)"),
    ("Finish a measurement (distance, perimeter, area)", "Double-click or Enter"),
    ("Night mode", "Ctrl+Shift+N"), ("Document properties", "Ctrl+D"), ("Preferences", "Ctrl+,"),
    ("PDF from clipboard", "Ctrl+Shift+V"), ("Keyboard shortcuts", "F1"), ("Quit", "Ctrl+Q"),
]


class ShortcutsDialog(QDialog):
    def __init__(self, parent):
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        self.resize(520, 600)
        lay = QVBoxLayout(self)
        table = QTableWidget(len(SHORTCUTS), 2)
        table.setHorizontalHeaderLabels(["Action", "Keys"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        for r, (a, k) in enumerate(SHORTCUTS):
            table.setItem(r, 0, QTableWidgetItem(a))
            table.setItem(r, 1, QTableWidgetItem(k))
        lay.addWidget(table)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        lay.addWidget(box)


def about_text() -> str:
    import PySide6
    import html
    from pdfdesk import digitalid, speech
    lo = externals.libreoffice_command()
    folder, langs = externals.find_tessdata()
    if digitalid.available():
        import pyhanko
        signing = f"pyHanko {html.escape(getattr(pyhanko, '__version__', ''))} (digital signatures)"
    else:
        signing = "Digital signatures: pyHanko not installed"
    langs = [html.escape(x) for x in langs]
    return (f"<h3>{APP_NAME} {__version__}</h3>"
            f"<p>An offline PDF viewer, editor and converter for Linux and Windows.<br>"
            f"Nothing is uploaded anywhere. Everything runs on this computer.</p>"
            f"<p><b>Built with</b><br>PyMuPDF {fitz.VersionBind} (MuPDF {fitz.VersionFitz})<br>"
            f"PySide6 / Qt {PySide6.__version__}<br>{signing}<br>fontTools (font subsetting)<br>"
            f"Fonts: FiraGO, Fira Mono, Noto Sans, Ubuntu, Space Mono, Cascadia Mono (open font licenses)<br>"
            f"Icons: Lucide (ISC license)</p>"
            f"<p><b>Optional extras</b><br>LibreOffice: {'found' if lo else 'not found'}<br>"
            f"OCR languages: {', '.join(langs) if langs else 'none found'}<br>"
            f"Read Out Loud voice: {'found' if speech.available() else 'not found'}</p>")


EXTRAS_HELP = """<h3>Optional extras</h3>
<p>PDF Desk works on its own. A few free programs add more conversions, OCR and a voice for
Read Out Loud. All of them run offline once installed.</p>
<p><b>LibreOffice</b> (for .doc, .xls, .ppt, .odt, .rtf and best quality .docx/.xlsx/.pptx import)<br>
Fedora: <code>sudo dnf install libreoffice</code><br>
Ubuntu/Debian: <code>sudo apt install libreoffice</code><br>
Windows: install from libreoffice.org, then restart PDF Desk.</p>
<p><b>OCR language files</b> (for Recognize Text)<br>
Fedora: <code>sudo dnf install tesseract-langpack-eng tesseract-langpack-ron</code><br>
Ubuntu/Debian: <code>sudo apt install tesseract-ocr-eng tesseract-ocr-ron</code><br>
Windows: install Tesseract OCR (the UB Mannheim build) and tick the languages you need,
or copy <code>.traineddata</code> files into PDF Desk's tessdata folder (Preferences &gt; Extras).</p>
<p><b>A voice for Read Out Loud</b> (Linux only; Windows has one built in)<br>
Fedora: <code>sudo dnf install espeak-ng</code><br>
Ubuntu/Debian: <code>sudo apt install espeak-ng</code></p>"""


# =========================================================================== form fields

class FieldPropertiesDialog(_Validated):
    """Name, tooltip, required/read-only, look and (for lists) the choices of one form field."""

    def __init__(self, parent, props: dict):
        super().__init__(parent)
        from pdfdesk import forms
        from PySide6.QtWidgets import QPlainTextEdit
        kind = props.get("kind", "text")
        self.kind = kind
        self.props = dict(props)
        self.setWindowTitle(f"{forms.KIND_LABELS.get(kind, 'Field')} properties")
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit(props.get("name", ""))
        self.name.setEnabled(kind != "radio")
        self.tooltip = QLineEdit(props.get("tooltip", ""))
        self.tooltip.setPlaceholderText("Shown when someone points at the field")
        self.required = QCheckBox("Required")
        self.required.setChecked(bool(props.get("required")))
        self.read_only = QCheckBox("Read-only")
        self.read_only.setChecked(bool(props.get("read_only")))
        flags = QHBoxLayout()
        flags.addWidget(self.required)
        flags.addWidget(self.read_only)
        flags.addStretch(1)
        form.addRow("Name:", self.name)
        if kind == "radio":
            form.addRow("", muted("Radio buttons share the name of their group."))
        form.addRow("Tooltip:", self.tooltip)
        form.addRow("", self._wrap(flags))
        self.multiline = self.max_length = self.align = self.choices = self.editable = None
        if kind == "text":
            self.multiline = QCheckBox("Allow several lines")
            self.multiline.setChecked(bool(props.get("multiline")))
            self.max_length = QSpinBox()
            self.max_length.setRange(0, 10000)
            self.max_length.setSpecialValueText("No limit")
            self.max_length.setValue(int(props.get("max_length") or 0))
            self.align = QComboBox()
            self.align.addItems(["Left", "Center", "Right"])
            self.align.setCurrentIndex(int(props.get("align") or 0))
            form.addRow("", self.multiline)
            form.addRow("Maximum characters:", self.max_length)
            form.addRow("Text alignment:", self.align)
        if kind in ("combo", "list"):
            self.choices = QPlainTextEdit("\n".join(props.get("choices") or []))
            self.choices.setPlaceholderText("One choice per line")
            self.choices.setFixedHeight(110)
            form.addRow("Choices:", self.choices)
            if kind == "combo":
                self.editable = QCheckBox("Let people type their own answer")
                self.editable.setChecked(bool(props.get("editable")))
                form.addRow("", self.editable)
        self.font_size = QDoubleSpinBox()
        self.font_size.setRange(0, 72)
        self.font_size.setDecimals(1)
        self.font_size.setSpecialValueText("Auto")
        self.font_size.setValue(float(props.get("font_size") or 0))
        self.font_size.setSuffix(" pt")
        if kind not in ("signature",):
            form.addRow("Text size:", self.font_size)
        from pdfdesk.fonts import rgb_to_hex
        self.border = ColorButton(rgb_to_hex(props.get("border_color")) if props.get("border_color") else "",
                                  "Border color", none_label="No border")
        self.fill = ColorButton(rgb_to_hex(props.get("fill_color")) if props.get("fill_color") else "",
                                "Background color", none_label="No background")
        self.text_color = ColorButton(rgb_to_hex(props.get("text_color") or (0, 0, 0)), "Text color")
        colors = QHBoxLayout()
        for label, btn in (("Border", self.border), ("Background", self.fill), ("Text", self.text_color)):
            colors.addWidget(QLabel(label))
            colors.addWidget(btn)
        colors.addStretch(1)
        form.addRow("Colors:", self._wrap(colors))
        lay.addLayout(form)
        lay.addWidget(_buttons(self, "OK"))

    @staticmethod
    def _wrap(layout) -> QWidget:
        w = QWidget()
        layout.setContentsMargins(0, 0, 0, 0)
        w.setLayout(layout)
        return w

    def validate(self) -> None:
        if self.kind != "radio" and not self.name.text().strip():
            raise ValueError("Give the field a name.")
        if self.choices is not None and not self.choices.toPlainText().strip():
            raise ValueError("Add at least one choice.")

    def result_props(self) -> dict:
        from pdfdesk.fonts import hex_to_rgb
        p = dict(self.props)
        p.update(name=self.name.text().strip(), tooltip=self.tooltip.text().strip(),
                 required=self.required.isChecked(), read_only=self.read_only.isChecked(),
                 font_size=self.font_size.value(),
                 border_color=hex_to_rgb(self.border.color()) if self.border.color() else None,
                 fill_color=hex_to_rgb(self.fill.color()) if self.fill.color() else None,
                 text_color=hex_to_rgb(self.text_color.color()))
        if p.get("border_color") is None:
            p["border_width"] = 0
        elif not p.get("border_width"):
            p["border_width"] = 1
        if self.multiline is not None:
            p.update(multiline=self.multiline.isChecked(), max_length=self.max_length.value(),
                     align=self.align.currentIndex())
        if self.choices is not None:
            p["choices"] = [c for c in self.choices.toPlainText().splitlines() if c.strip()]
        if self.editable is not None:
            p["editable"] = self.editable.isChecked()
        return p


class RadioButtonDialog(_Validated):
    """Which group a new radio button belongs to, and the value it stands for."""

    def __init__(self, parent, groups: list[str], group: str, value: str):
        super().__init__(parent)
        self.setWindowTitle("Radio button")
        lay = QVBoxLayout(self)
        lay.addWidget(muted("Buttons in the same group allow only one choice. Pick a group or type a new name."))
        form = QFormLayout()
        self.group = QComboBox()
        self.group.setEditable(True)
        self.group.addItems(groups)
        self.group.setEditText(group)
        self.value = QLineEdit(value)
        form.addRow("Group:", self.group)
        form.addRow("Value of this button:", self.value)
        lay.addLayout(form)
        lay.addWidget(_buttons(self, "Add"))

    def validate(self) -> None:
        if not self.group.currentText().strip() or not self.value.text().strip():
            raise ValueError("Type a group name and a value.")


class DetectedFieldsDialog(QDialog):
    """Shows the fields PDF Desk found and lets the person untick any before adding them."""

    def __init__(self, parent, found: list[tuple[int, str, object, str]]):
        super().__init__(parent)
        from pdfdesk import forms
        self.setWindowTitle("Fields found")
        self.found = found
        lay = QVBoxLayout(self)
        lay.addWidget(muted(f"PDF Desk found {len(found)} place(s) that look like they need an answer. "
                            "Untick any you don't want, then click Add."))
        self.list = QListWidget()
        for pno, kind, _rect, name in found:
            item = QListWidgetItem(f"Page {pno + 1}: {forms.KIND_LABELS[kind]}  {name}")
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked)
            self.list.addItem(item)
        self.list.setMinimumSize(420, 260)
        lay.addWidget(self.list)
        lay.addWidget(_buttons(self, "Add fields"))

    def chosen(self) -> list[tuple[int, str, object, str]]:
        return [f for k, f in enumerate(self.found) if self.list.item(k).checkState() == Qt.CheckState.Checked]


# =========================================================================== document features

class LinkDialog(_Validated):
    """Where a link goes: a page in this PDF or a web/e-mail address."""

    def __init__(self, parent, page_count: int, current: dict | None = None, can_delete: bool = False):
        super().__init__(parent)
        self.setWindowTitle("Edit link" if current else "Add link")
        self.page_count = page_count
        self.deleted = False
        lay = QVBoxLayout(self)
        self.to_page = QRadioButton("Go to a page in this PDF:")
        self.page = QSpinBox()
        self.page.setRange(1, max(1, page_count))
        self.to_web = QRadioButton("Open a web page or e-mail:")
        self.url = QLineEdit()
        self.url.setPlaceholderText("https://example.com  or  mailto:name@example.com")
        grid = QGridLayout()
        grid.addWidget(self.to_page, 0, 0)
        grid.addWidget(self.page, 0, 1)
        grid.addWidget(self.to_web, 1, 0)
        grid.addWidget(self.url, 1, 1)
        lay.addLayout(grid)
        lay.addWidget(muted("Only web (http, https) and e-mail links can be added. Links to files or programs "
                            "aren't allowed."))
        current = current or {}
        if "uri" in current:
            self.to_web.setChecked(True)
            self.url.setText(current["uri"])
        else:
            self.to_page.setChecked(True)
            self.page.setValue(int(current.get("page", 0)) + 1)
        self.url.textEdited.connect(lambda _t: self.to_web.setChecked(True))
        self.page.valueChanged.connect(lambda _v: self.to_page.setChecked(True))
        buttons = _buttons(self, "OK")
        if can_delete:
            delete = buttons.addButton("Delete link", QDialogButtonBox.ButtonRole.DestructiveRole)
            delete.clicked.connect(self._delete)
        lay.addWidget(buttons)
        self.resize(460, 0)

    def _delete(self) -> None:
        self.deleted = True
        QDialog.accept(self)

    def validate(self) -> None:
        if self.to_web.isChecked():
            from pdfdesk import safety
            text = self.url.text().strip()
            if text and "://" not in text and not text.lower().startswith("mailto:"):
                text = ("mailto:" + text) if "@" in text and "/" not in text else "https://" + text
                self.url.setText(text)
            url, info = safety.check_web_link(text)
            if url is None:
                raise ValueError(info)

    def target(self) -> dict:
        if self.to_web.isChecked():
            return {"uri": self.url.text().strip()}
        return {"page": self.page.value() - 1}


class PageLabelsDialog(_Validated):
    """Page numbering as shown in viewers (i, ii, iii for a preface, then 1, 2, 3...)."""

    def __init__(self, parent, page_count: int, rules: list[dict]):
        super().__init__(parent)
        from pdfdesk.docfeatures import LABEL_STYLES
        self.setWindowTitle("Page labels")
        self.page_count = page_count
        self.styles = LABEL_STYLES
        lay = QVBoxLayout(self)
        lay.addWidget(muted("Each row starts a new numbering style from its first page. For example: pages 1-4 "
                            "as i, ii, iii, iv and from page 5 on as 1, 2, 3."))
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["From page", "Style", "Prefix", "Start at"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        add = QPushButton("Add range")
        add.clicked.connect(lambda: self._add_row({"startpage": self._next_start(), "style": "D", "firstpagenum": 1}))
        remove = QPushButton("Remove range")
        remove.clicked.connect(lambda: self.table.removeRow(self.table.currentRow())
                               if self.table.currentRow() >= 0 else None)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        lay.addLayout(row)
        for r in rules or [{"startpage": 0, "style": "D", "firstpagenum": 1}]:
            self._add_row(r)
        lay.addWidget(_buttons(self, "Apply"))
        self.resize(560, 360)

    def _next_start(self) -> int:
        starts = [self.table.cellWidget(r, 0).value() for r in range(self.table.rowCount())]
        return min(self.page_count - 1, max(starts)) if starts else 0  # the page after the last range start

    def _add_row(self, rule: dict) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        start = QSpinBox()
        start.setRange(1, max(1, self.page_count))
        start.setValue(max(1, min(self.page_count, int(rule.get("startpage", 0)) + 1)))
        style = QComboBox()
        for code, label in self.styles:
            style.addItem(label, code)
        idx = style.findData(rule.get("style", "D"))
        style.setCurrentIndex(idx if idx >= 0 else 0)
        prefix = QLineEdit(str(rule.get("prefix", "")))
        first = QSpinBox()
        first.setRange(1, 100000)
        try:
            first.setValue(max(1, min(100000, int(rule.get("firstpagenum", 1)))))
        except (TypeError, ValueError, OverflowError):
            first.setValue(1)
        for c, w in enumerate((start, style, prefix, first)):
            self.table.setCellWidget(r, c, w)

    def rules(self) -> list[dict]:
        out = []
        for r in range(self.table.rowCount()):
            out.append({"startpage": self.table.cellWidget(r, 0).value() - 1,
                        "style": self.table.cellWidget(r, 1).currentData(),
                        "prefix": self.table.cellWidget(r, 2).text(),
                        "firstpagenum": self.table.cellWidget(r, 3).value()})
        return out

    def validate(self) -> None:
        starts = [r["startpage"] for r in self.rules()]
        if len(set(starts)) != len(starts):
            raise ValueError("Two ranges start on the same page.")


class BackgroundDialog(_Validated):
    def __init__(self, parent, pdf: PdfDocument, current: int):
        super().__init__(parent)
        self.setWindowTitle("Page background")
        self.pdf = pdf
        self.current = current
        outer = QHBoxLayout(self)
        left = QVBoxLayout()
        form = QFormLayout()
        self.use_color = QRadioButton("Color:")
        self.use_color.setChecked(True)
        self.color = ColorButton("#fff6d5", "Background color")
        self.use_image = QRadioButton("Picture:")
        self.image = PathPicker("open", "Choose a picture", "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff)")
        self.stretch = QCheckBox("Stretch to fill the page")
        self.opacity = QSlider(Qt.Orientation.Horizontal)
        self.opacity.setRange(5, 100)
        self.opacity.setValue(100)
        self.range = PageRangeEdit(pdf.page_count, current)
        form.addRow(self.use_color, self.color)
        form.addRow(self.use_image, self.image)
        form.addRow("", self.stretch)
        form.addRow("Opacity:", self.opacity)
        form.addRow("Pages:", self.range)
        left.addLayout(form)
        left.addWidget(muted("The background goes behind everything on the page. Undo removes it."))
        left.addStretch(1)
        left.addWidget(_buttons(self, "Add background"))
        outer.addLayout(left, 1)
        self.preview = _preview_label()
        outer.addWidget(self.preview)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._update_preview)
        for sig in (self.color.color_changed, self.use_color.toggled, self.image.changed, self.stretch.toggled,
                    self.opacity.valueChanged):
            sig.connect(lambda *_: self._timer.start())
        self.image.changed.connect(lambda _p: self.use_image.setChecked(True))
        self._timer.start(0)

    def _image_bytes(self) -> bytes | None:
        path = self.image.path()
        if self.use_image.isChecked() and os.path.isfile(path):
            from pdfdesk.convert import _image_bytes_for_mupdf
            data = _image_bytes_for_mupdf(Path(path).read_bytes())
            if self.opacity.value() < 100:
                from PIL import Image
                import io
                img = Image.open(io.BytesIO(data)).convert("RGBA")
                alpha = img.getchannel("A").point(lambda a: int(a * self.opacity.value() / 100))
                img.putalpha(alpha)
                buf = io.BytesIO()
                img.save(buf, "PNG")
                data = buf.getvalue()
            return data
        return None

    def apply(self, doc: fitz.Document, pages: list[int]) -> None:
        from pdfdesk import docfeatures
        if self.use_color.isChecked():
            docfeatures.add_background(doc, pages, color=hex_to_rgb(self.color.color()),
                                       opacity=self.opacity.value() / 100)
        else:
            docfeatures.add_background(doc, pages, image=self._image_bytes(),
                                       fit="stretch" if self.stretch.isChecked() else "fit")

    def _update_preview(self) -> None:
        try:
            tmp = fitz.open("pdf", pdfops.extract_pages_bytes(self.pdf.doc, [self.current]))
            if self.use_color.isChecked() or self._image_bytes():
                self.apply(tmp, [0])
            _render_preview(tmp, self.preview)
        except Exception:
            pass

    def validate(self) -> None:
        self._pages = self.range.pages()
        if self.use_image.isChecked() and not os.path.isfile(self.image.path()):
            raise ValueError("Choose a picture for the background.")


class PageSizeDialog(_Validated):
    def __init__(self, parent, pdf: PdfDocument, current: int):
        super().__init__(parent)
        from pdfdesk.docfeatures import PAPER_SIZES
        self.setWindowTitle("Page size")
        self.sizes = PAPER_SIZES
        lay = QVBoxLayout(self)
        form = QFormLayout()
        cur = pdf.page_rect(current)
        form.addRow("Current page:", ui.plain_label(pdfops.page_size_text(cur)))
        self.paper = QComboBox()
        for name, w, h in PAPER_SIZES:
            self.paper.addItem(f"{name} ({w / 72:.2f} x {h / 72:.2f} in)", (w, h))
        self.paper.addItem("Custom size", None)
        self.paper.setCurrentIndex(0 if cur.width < 600 or cur.width > 620 else 4)
        self.width = QDoubleSpinBox()
        self.height = QDoubleSpinBox()
        for spin in (self.width, self.height):
            spin.setRange(1, 200)
            spin.setDecimals(2)
            spin.setSuffix(" in")
        self.landscape = QCheckBox("Landscape")
        self.mode = QComboBox()
        self.mode.addItems(["Shrink or enlarge the content to fit", "Keep the content size (change the margins)"])
        self.range = PageRangeEdit(pdf.page_count, current)
        custom = QHBoxLayout()
        custom.addWidget(self.width)
        custom.addWidget(QLabel("x"))
        custom.addWidget(self.height)
        custom.addWidget(self.landscape)
        form.addRow("New size:", self.paper)
        form.addRow("", self._wrap(custom))
        form.addRow("Content:", self.mode)
        form.addRow("Pages:", self.range)
        lay.addLayout(form)
        lay.addWidget(_buttons(self, "Change size"))
        self.paper.currentIndexChanged.connect(self._paper_changed)
        self._paper_changed()

    @staticmethod
    def _wrap(layout) -> QWidget:
        w = QWidget()
        layout.setContentsMargins(0, 0, 0, 0)
        w.setLayout(layout)
        return w

    def _paper_changed(self, *_):
        data = self.paper.currentData()
        custom = data is None
        self.width.setEnabled(custom)
        self.height.setEnabled(custom)
        if data:
            self.width.setValue(data[0] / 72)
            self.height.setValue(data[1] / 72)

    def size_pt(self) -> tuple[float, float]:
        w, h = self.width.value() * 72, self.height.value() * 72
        if self.landscape.isChecked():
            w, h = max(w, h), min(w, h)
        else:
            w, h = min(w, h), max(w, h)
        return w, h

    def validate(self) -> None:
        self._pages = self.range.pages()

    def mode_key(self) -> str:
        return "scale" if self.mode.currentIndex() == 0 else "margins"


class PrintLayoutDialog(_Validated):
    """Pages per sheet (N-up) or a folded booklet, made as a new PDF."""

    def __init__(self, parent, booklet: bool = False):
        super().__init__(parent)
        from pdfdesk.docfeatures import PAPER_SIZES
        self.setWindowTitle("Booklet" if booklet else "Pages per sheet")
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.kind = QComboBox()
        for n in (2, 4, 6, 9, 16):
            self.kind.addItem(f"{n} pages per sheet", n)
        self.kind.addItem("Booklet (fold in half, staple in the middle)", "booklet")
        self.kind.setCurrentIndex(self.kind.count() - 1 if booklet else 0)
        self.paper = QComboBox()
        for name, w, h in PAPER_SIZES:
            self.paper.addItem(name, (w, h))
        self.paper.setCurrentIndex(0)
        self.landscape = QCheckBox("Landscape sheets")
        self.landscape.setChecked(True)
        self.borders = QCheckBox("Draw a thin border around each page")
        form.addRow("Layout:", self.kind)
        form.addRow("Sheet size:", self.paper)
        form.addRow("", self.landscape)
        form.addRow("", self.borders)
        lay.addLayout(form)
        lay.addWidget(muted("A new PDF is made; this one isn't changed. Comments and form entries are printed "
                            "into the pages. For a booklet, print both sides and flip on the short edge."))
        lay.addWidget(_buttons(self, "Make PDF"))
        self.kind.currentIndexChanged.connect(lambda _i: self.landscape.setEnabled(self.kind.currentData() != "booklet"))

    def params(self) -> dict:
        w, h = self.paper.currentData()
        if self.landscape.isChecked() or self.kind.currentData() == "booklet":
            w, h = max(w, h), min(w, h)
        return {"kind": self.kind.currentData(), "paper": (w, h), "borders": self.borders.isChecked()}



class MeasureScaleDialog(QDialog):
    """The drawing scale used by the Measure tool, like "1 in = 10 ft" on a floor plan."""

    def __init__(self, parent, scale):
        super().__init__(parent)
        from pdfdesk import measure
        self.setWindowTitle("Measuring scale")
        lay = QVBoxLayout(self)
        lay.addWidget(muted("How long is something on the page compared to the real thing? For a plan drawn at "
                            "1:100, use 1 cm = 1 m. For a normal document, use 1 in = 1 in."))
        row = QHBoxLayout()
        self.page_value = QDoubleSpinBox()
        self.page_value.setRange(0.001, 100000)
        self.page_value.setDecimals(3)
        self.page_value.setValue(scale.page_value)
        self.page_unit = QComboBox()
        self.page_unit.addItems(list(measure.PAGE_UNITS))
        self.page_unit.setCurrentText(scale.page_unit)
        self.real_value = QDoubleSpinBox()
        self.real_value.setRange(0.000001, 1e9)
        self.real_value.setDecimals(3)
        self.real_value.setValue(scale.real_value)
        self.real_unit = QComboBox()
        self.real_unit.addItems(measure.REAL_UNITS)
        self.real_unit.setCurrentText(scale.real_unit)
        for w in (self.page_value, self.page_unit, QLabel("on the page  ="), self.real_value, self.real_unit):
            row.addWidget(w)
        lay.addLayout(row)
        form = QFormLayout()
        self.decimals = QSpinBox()
        self.decimals.setRange(0, 4)
        self.decimals.setValue(scale.decimals)
        form.addRow("Decimal places:", self.decimals)
        lay.addLayout(form)
        lay.addWidget(_buttons(self, "OK"))

    def scale(self):
        from pdfdesk.measure import Scale
        return Scale(self.page_value.value(), self.page_unit.currentText(), self.real_value.value(),
                     self.real_unit.currentText(), self.decimals.value())


# =========================================================================== digital IDs

class CreateIdDialog(_Validated):
    def __init__(self, parent, author: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Create a Digital ID")
        lay = QVBoxLayout(self)
        lay.addWidget(muted("A Digital ID lets you sign PDFs so others can see who signed and that nothing changed "
                            "afterwards. This one is made on your computer (self-signed): people who receive your "
                            "documents can choose to trust it. It is protected by the password below."))
        form = QFormLayout()
        self.name = QLineEdit(author)
        self.email = QLineEdit()
        self.org = QLineEdit()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password2 = QLineEdit()
        self.password2.setEchoMode(QLineEdit.EchoMode.Password)
        self.years = QSpinBox()
        self.years.setRange(1, 20)
        self.years.setValue(5)
        self.years.setSuffix(" years")
        form.addRow("Your name:", self.name)
        form.addRow("E-mail:", self.email)
        form.addRow("Organization:", self.org)
        form.addRow("Password:", self.password)
        form.addRow("Password again:", self.password2)
        form.addRow("Valid for:", self.years)
        lay.addLayout(form)
        lay.addWidget(_buttons(self, "Create"))
        self.resize(460, 0)

    def validate(self) -> None:
        if not self.name.text().strip():
            raise ValueError("Type your name.")
        if len(self.password.text()) < 8:
            raise ValueError("Use a password of at least 8 characters.")
        if self.password.text() != self.password2.text():
            raise ValueError("The two passwords are different.")

    def clear_passwords(self) -> None:
        self.password.clear()
        self.password2.clear()


class NewPasswordDialog(_Validated):
    """Choose a new password for a Digital ID being imported."""

    def __init__(self, parent, message: str):
        super().__init__(parent)
        self.setWindowTitle("New password")
        lay = QVBoxLayout(self)
        lay.addWidget(muted(message))
        form = QFormLayout()
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password2 = QLineEdit()
        self.password2.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("New password:", self.password)
        form.addRow("Password again:", self.password2)
        lay.addLayout(form)
        lay.addWidget(_buttons(self, "OK"))
        self.resize(420, 0)

    def validate(self) -> None:
        if len(self.password.text()) < 8:
            raise ValueError("Use a password of at least 8 characters.")
        if self.password.text() != self.password2.text():
            raise ValueError("The two passwords are different.")


class DigitalIdsDialog(QDialog):
    """Your Digital IDs, and the certificates of people you trust."""

    def __init__(self, parent, author: str = ""):
        super().__init__(parent)
        from pdfdesk import digitalid
        self.di = digitalid
        self.author = author
        self.setWindowTitle("Digital IDs")
        lay = QVBoxLayout(self)
        lay.addWidget(muted("Your Digital IDs are kept in PDF Desk's settings folder, readable only by you, and "
                            "each is locked with its own password. Nothing is sent anywhere."))
        self.list = QListWidget()
        self.list.setMinimumSize(520, 200)
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        for text, fn in (("Create new ID...", self._create), ("Import .p12 / .pfx...", self._import),
                         ("Export certificate...", self._export), ("Delete", self._delete)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        lay.addLayout(row)
        self.trust_label = muted("")
        lay.addWidget(self.trust_label)
        trust_row = QHBoxLayout()
        add_trust = QPushButton("Trust a certificate from a file...")
        add_trust.clicked.connect(self._add_trust)
        trust_row.addWidget(add_trust)
        trust_row.addStretch(1)
        lay.addLayout(trust_row)
        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        box.rejected.connect(self.reject)
        box.accepted.connect(self.accept)
        lay.addWidget(box)
        self.reload()

    def reload(self) -> None:
        self.list.clear()
        for info in self.di.list_ids():
            kind = "self-made" if info["self_signed"] else f"issued by {info['issuer']}"
            text = (f"{info['name']}" + (f"  <{info['email']}>" if info["email"] else "")
                    + f"\n{kind}, valid until {info['not_after'].date().isoformat()}")
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, info)
            self.list.addItem(item)
        if not self.list.count():
            item = QListWidgetItem("No Digital IDs yet. Create one or import one.")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(item)
        n = len(list(self.di.trusted_dir().glob("*.crt")))
        self.trust_label.setText(f"You trust {n} certificate(s). Signatures made with them show as confirmed.")

    def current(self) -> dict | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _create(self) -> None:
        dlg = CreateIdDialog(self, self.author)
        try:
            if dlg.exec():
                try:
                    self.di.create_id(dlg.name.text(), dlg.email.text(), dlg.org.text(), dlg.password.text(),
                                      dlg.years.value())
                except Exception as exc:
                    ui.warning(self, "Create a Digital ID", str(exc))
        finally:
            dlg.clear_passwords()
            dlg.deleteLater()
        self.reload()

    def _import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import a Digital ID", "", "Digital IDs (*.p12 *.pfx)")
        if not path:
            return
        from PySide6.QtWidgets import QInputDialog
        pw, ok = QInputDialog.getText(self, "Import a Digital ID", "Password of this Digital ID:",
                                      QLineEdit.EchoMode.Password)
        if not ok:
            return
        try:
            try:
                self.di.import_id(path, pw)
            except self.di.ShortPassword as short:
                dlg = NewPasswordDialog(self, f"{short} PDF Desk keeps it locked with the new password; the "
                                              "original file is not changed.")
                try:
                    if not dlg.exec():
                        return
                    self.di.import_id(path, pw, dlg.password.text())
                finally:
                    dlg.password.clear()
                    dlg.password2.clear()
                    dlg.deleteLater()
        except Exception as exc:
            ui.warning(self, "Import a Digital ID", str(exc))
        finally:
            self.reload()

    def _export(self) -> None:
        info = self.current()
        if not info:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export certificate (safe to share)",
                                              f"{info['name']} certificate.cer", "Certificates (*.cer *.crt *.pem)")
        if path:
            try:
                self.di.export_certificate(info["cert_path"], path)
            except Exception as exc:
                ui.warning(self, "Export certificate", str(exc))

    def _delete(self) -> None:
        info = self.current()
        if not info:
            return
        if ui.question(self, "Delete Digital ID", f"Delete the Digital ID of {info['name']}? You won't be able to "
                                                  "sign with it again. Signatures already made stay valid.",
                       default=ui.No) == ui.Yes:
            try:
                self.di.delete_id(info["path"])
            except Exception as exc:
                ui.warning(self, "Delete Digital ID", str(exc))
            self.reload()

    def _add_trust(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Trust a certificate", "", "Certificates (*.cer *.crt *.pem *.der)")
        if not path:
            return
        try:
            data = Path(path).read_bytes()[:200000]
            self.di.trust_certificate(data)
        except Exception as exc:
            ui.warning(self, "Trust a certificate", f"That file isn't a certificate:\n\n{exc}")
        self.reload()


class SignDialog(_Validated):
    def __init__(self, parent, ids: list[dict], default_path: str, has_image: bool, last_reason: str = "",
                 last_location: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Sign with Digital ID")
        self.ids = ids
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.id_box = QComboBox()
        for info in ids:
            self.id_box.addItem(f"{info['name']}" + (f" <{info['email']}>" if info["email"] else ""), info["path"])
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.reason = QComboBox()
        self.reason.setEditable(True)
        self.reason.addItems(["I approve this document", "I am the author of this document",
                              "I have reviewed this document", "I agree to the terms"])
        self.reason.setEditText(last_reason or "I approve this document")
        self.location = QLineEdit(last_location)
        self.with_image = QCheckBox("Include my handwritten signature in the box")
        self.with_image.setChecked(has_image)
        self.with_image.setEnabled(has_image)
        self.target = PathPicker("save", "Save the signed PDF as", "PDF files (*.pdf)")
        self.target.set_path(default_path)
        form.addRow("Digital ID:", self.id_box)
        form.addRow("Password:", self.password)
        form.addRow("Reason:", self.reason)
        form.addRow("Location:", self.location)
        form.addRow("", self.with_image)
        form.addRow("Save as:", self.target)
        lay.addLayout(form)
        lay.addWidget(muted("Signing saves a new copy of the PDF. Any later change to the signed copy shows up "
                            "when the signature is checked. The signing time is taken from this computer's clock."))
        lay.addWidget(_buttons(self, "Sign"))
        self.resize(520, 0)

    def validate(self) -> None:
        from pdfdesk import digitalid
        if not self.password.text():
            raise ValueError("Type the password of your Digital ID.")
        if not digitalid.check_password(self.id_box.currentData(), self.password.text()):
            raise ValueError("That password doesn't open this Digital ID.")
        path = self.target.path()
        if not path:
            raise ValueError("Choose where to save the signed PDF.")
        if not path.lower().endswith(".pdf"):
            self.target.set_path(path + ".pdf")


class SignaturesDialog(QDialog):
    """Results of checking the digital signatures in a PDF."""

    def __init__(self, parent, results: list[dict], on_open_version=None):
        super().__init__(parent)
        from pdfdesk import digitalid
        self.di = digitalid
        self.results = results
        self.on_open_version = on_open_version
        self.setWindowTitle("Digital signatures")
        lay = QVBoxLayout(self)
        lay.addWidget(muted("Checked on this computer only: certificates are compared with the ones you trust, "
                            "and nothing is downloaded. Text in quotes was written by the signer."))
        self.list = QListWidget()
        self.list.setWordWrap(True)
        self.list.setMinimumSize(600, 260)
        colors = {"good": "#2a9d4a", "warn": "#d08a00", "bad": "#c8323c"}
        verdicts = {"good": "VALID", "warn": "VALID, WITH NOTES", "bad": "NOT VALID"}
        from PySide6.QtGui import QColor, QIcon, QPixmap
        for item in results:
            level, text = digitalid.describe(item)
            extra = []
            if item.get("reason"):
                extra.append(f"Signer's reason: \u201c{item['reason']}\u201d")
            if item.get("location"):
                extra.append(f"Signer's location: \u201c{item['location']}\u201d")
            if item.get("email"):
                extra.append(f"E-mail in the certificate: {item['email']}")
            head = f"{item.get('field') or 'Signature'}: {verdicts[level]}"
            row = QListWidgetItem(f"{head}\n{text}" + ("\n" + "\n".join(extra) if extra else ""))
            pm = QPixmap(14, 14)
            pm.fill(QColor(colors[level]))
            row.setIcon(QIcon(pm))
            row.setData(Qt.ItemDataRole.UserRole, item)
            self.list.addItem(row)
        lay.addWidget(self.list, 1)
        row = QHBoxLayout()
        self.trust_btn = QPushButton("Trust this signer's certificate")
        self.trust_btn.clicked.connect(self._trust)
        row.addWidget(self.trust_btn)
        self.version_btn = QPushButton("Open signed version")
        self.version_btn.setToolTip("Open the document exactly as it was when the selected signature was made")
        self.version_btn.clicked.connect(self._open_version)
        self.version_btn.setVisible(on_open_version is not None)
        row.addWidget(self.version_btn)
        row.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        lay.addLayout(row)
        self.trusted_any = False
        self.list.currentItemChanged.connect(self._selection_changed)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _selected(self) -> dict | None:
        item = self.list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _selection_changed(self, *_a) -> None:
        data = self._selected() or {}
        self.version_btn.setEnabled(bool(data.get("changed_after") and data.get("signed_end")))
        self.trust_btn.setEnabled(bool(data.get("certificate")) and not data.get("trusted"))

    def _open_version(self) -> None:
        data = self._selected()
        if data and self.on_open_version is not None:
            try:
                self.on_open_version(data)
            except Exception as exc:
                ui.warning(self, "Open signed version", str(exc))
                return
            self.accept()

    def _trust(self) -> None:
        data = self._selected()
        if not data or not data.get("certificate"):
            ui.information(self, "Trust certificate", "Select a signature first.")
            return
        if ui.question(self, "Trust certificate",
                       f"Trust signatures made by {data['name']} from now on?\n\nOnly do this if you know the "
                       "certificate really belongs to them (for example, they sent it to you directly).",
                       default=ui.No) != ui.Yes:
            return
        try:
            self.di.trust_certificate(data["certificate"])
        except Exception as exc:
            ui.warning(self, "Trust certificate", str(exc))
            return
        self.trusted_any = True
        self.accept()
