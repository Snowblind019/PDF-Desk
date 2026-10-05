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
        f.addRow("Text:", self.text)
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
                    self.opacity.valueChanged, self.angle.valueChanged, self.behind.currentIndexChanged,
                    self.tabs.currentChanged, self.image.changed, self.scale.valueChanged):
            sig.connect(lambda *_: self._timer.start())
        self._timer.start(0)

    def apply(self, doc: fitz.Document, pages: list[int]) -> None:
        if self.tabs.currentIndex() == 0:
            pdfops.add_text_watermark(doc, pages, self.text.text(), self.size.value(), hex_to_rgb(self.color.color()),
                                      self.opacity.value() / 100, self.angle.value(),
                                      behind=self.behind.currentIndex() == 1, bold=self.bold.isChecked())
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
        left.addWidget(muted("You can use {page}, {pages}, {date}, {filename} and {title} in the text."))
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
        self.font = QComboBox()
        self.font.addItems(["Sans serif", "Serif", "Monospace"])
        self.range = PageRangeEdit(pdf.page_count, current)
        form.addRow("Font:", self.font)
        form.addRow("Size:", self.size)
        form.addRow("Color:", self.color)
        form.addRow("Distance from edge:", self.margin)
        form.addRow("First page number:", self.start)
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
                    self.start.valueChanged, self.font.currentIndexChanged):
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
        return ["sans", "serif", "mono"][self.font.currentIndex()]

    def apply(self, doc: fitz.Document, pages: list[int], filename: str) -> None:
        pdfops.add_header_footer(doc, pages, {k: e.text() for k, e in self.slots.items()}, self.size.value(),
                                 hex_to_rgb(self.color.color()), self.margin.value(), self.start.value(),
                                 self.family(), filename)

    def _update_preview(self) -> None:
        try:
            tmp = fitz.open("pdf", pdfops.extract_pages_bytes(self.pdf.doc, [self.current]))
            # number the preview as it will be numbered in the real document
            slots = {k: e.text().replace("{page}", str(self.current + self.start.value()))
                     .replace("{pages}", str(self.pdf.page_count + self.start.value() - 1))
                     for k, e in self.slots.items()}
            pdfops.add_header_footer(tmp, [0], slots, self.size.value(), hex_to_rgb(self.color.color()),
                                     self.margin.value(), self.start.value(), self.family(), self.pdf.title)
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
        s.set("libreoffice_path", self.lo.path())
        s.set("tessdata_path", self.tess.path())


# =========================================================================== help

SHORTCUTS = [
    ("Open", "Ctrl+O"), ("Save", "Ctrl+S"), ("Save as", "Ctrl+Shift+S"), ("Print", "Ctrl+P"),
    ("Close tab", "Ctrl+W"), ("Next / previous tab", "Ctrl+Tab / Ctrl+Shift+Tab"), ("Home screen", "Ctrl+H"),
    ("Undo / redo", "Ctrl+Z / Ctrl+Y"), ("Copy selected text", "Ctrl+C"), ("Find", "Ctrl+F"),
    ("Next / previous match", "F3 / Shift+F3"), ("Zoom in / out", "Ctrl++ / Ctrl+-  (or Ctrl+mouse wheel)"),
    ("Actual size", "Ctrl+0"), ("Fit width / fit page", "Ctrl+1 / Ctrl+2"), ("Go to page", "Ctrl+G"),
    ("First / last page", "Home / End"), ("Rotate page", "Ctrl+R / Ctrl+Shift+R"), ("Add bookmark", "Ctrl+B"),
    ("Sidebar", "F4"), ("Organize pages", "Ctrl+Shift+O"), ("Full screen", "F11"),
    ("Select tool", "Esc"), ("Delete selected comment", "Delete"), ("Nudge selected item", "Arrow keys (Shift = 10x)"),
    ("Finish typing in a text box", "Ctrl+Enter or click outside"), ("Night mode", "Ctrl+Shift+N"),
    ("Document properties", "Ctrl+D"), ("Preferences", "Ctrl+,"), ("PDF from clipboard", "Ctrl+Shift+V"),
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
    lo = externals.libreoffice_command()
    folder, langs = externals.find_tessdata()
    langs = [html.escape(x) for x in langs]
    return (f"<h3>{APP_NAME} {__version__}</h3>"
            f"<p>An offline PDF viewer, editor and converter for Linux and Windows.<br>"
            f"Nothing is uploaded anywhere. Everything runs on this computer.</p>"
            f"<p><b>Built with</b><br>PyMuPDF {fitz.VersionBind} (MuPDF {fitz.VersionFitz})<br>"
            f"PySide6 / Qt {PySide6.__version__}<br>Icons: Lucide (ISC license)</p>"
            f"<p><b>Optional extras</b><br>LibreOffice: {'found' if lo else 'not found'}<br>"
            f"OCR languages: {', '.join(langs) if langs else 'none found'}</p>")


EXTRAS_HELP = """<h3>Optional extras</h3>
<p>PDF Desk works on its own. Two free programs add more conversions and OCR.
Both run offline once installed.</p>
<p><b>LibreOffice</b> (for .doc, .xls, .ppt, .odt, .rtf and best quality .docx/.xlsx/.pptx import)<br>
Fedora: <code>sudo dnf install libreoffice</code><br>
Ubuntu/Debian: <code>sudo apt install libreoffice</code><br>
Windows: install from libreoffice.org, then restart PDF Desk.</p>
<p><b>OCR language files</b> (for Recognize Text)<br>
Fedora: <code>sudo dnf install tesseract-langpack-eng tesseract-langpack-ron</code><br>
Ubuntu/Debian: <code>sudo apt install tesseract-ocr-eng tesseract-ocr-ron</code><br>
Windows: install Tesseract OCR (the UB Mannheim build) and tick the languages you need,
or copy <code>.traineddata</code> files into PDF Desk's tessdata folder (Preferences &gt; Extras).</p>"""
