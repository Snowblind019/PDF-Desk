"""Signature manager: draw, type or import a signature, saved as a transparent PNG on this computer."""
from __future__ import annotations

import io
import os
import time

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QImage, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QPushButton, QTabWidget,
                               QVBoxLayout, QWidget, QListView)

from pdfdesk import ui
from pdfdesk.config import signatures_dir
from pdfdesk.widgets import ColorButton


def list_signatures() -> list[str]:
    folder = signatures_dir()
    return sorted((str(p) for p in folder.glob("*.png")), key=os.path.getmtime, reverse=True)


def _to_pil(img: QImage):
    from PIL import Image
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    ba = QByteArray()
    buf = QBuffer(ba)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return Image.open(io.BytesIO(bytes(ba.data()))).convert("RGBA")


def _from_pil(pil) -> QImage:
    out = io.BytesIO()
    pil.save(out, "PNG")
    result = QImage()
    result.loadFromData(out.getvalue(), "PNG")
    return result


def _trim(img: QImage, pad: int = 6) -> QImage:
    """Crop transparent edges."""
    pil = _to_pil(img)
    box = pil.getchannel("A").point(lambda a: 255 if a > 10 else 0).getbbox()
    if not box:
        return img
    left, top = max(0, box[0] - pad), max(0, box[1] - pad)
    right, bottom = min(pil.width, box[2] + pad), min(pil.height, box[3] + pad)
    return _from_pil(pil.crop((left, top, right, bottom)))


def _remove_white(img: QImage) -> QImage:
    """Make white paper transparent: light pixels fade out, ink stays."""
    from PIL import ImageChops
    pil = _to_pil(img)
    lum = pil.convert("L")
    fade = lum.point(lambda v: 0 if v > 200 else 255 if v <= 150 else int(255 * (200 - v) / 50))
    pil.putalpha(ImageChops.multiply(fade, pil.getchannel("A")))
    return _from_pil(pil)


class SignaturePad(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(520, 200)
        self.strokes: list[list[QPointF]] = []
        self.color = QColor("#0b1f66")
        self.pen_width = 3.0
        self.setCursor(Qt.CursorShape.CrossCursor)

    def clear(self) -> None:
        self.strokes.clear()
        self.update()

    def is_empty(self) -> bool:
        return not any(len(s) > 1 for s in self.strokes)

    def mousePressEvent(self, ev) -> None:
        self.strokes.append([ev.position()])
        self.update()

    def mouseMoveEvent(self, ev) -> None:
        if self.strokes and ev.buttons() & Qt.MouseButton.LeftButton:
            self.strokes[-1].append(ev.position())
            self.update()

    def _path(self, scale: float = 1.0) -> QPainterPath:
        path = QPainterPath()
        for stroke in self.strokes:
            if len(stroke) < 2:
                continue
            pts = [QPointF(p.x() * scale, p.y() * scale) for p in stroke]
            path.moveTo(pts[0])
            for a, b in zip(pts[1:], pts[2:]):
                path.quadTo(a, QPointF((a.x() + b.x()) / 2, (a.y() + b.y()) / 2))
            path.lineTo(pts[-1])
        return path

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), QColor("#ffffff"))
        p.setPen(QPen(QColor(0, 0, 0, 60), 1, Qt.PenStyle.DashLine))
        y = self.height() * 0.72
        p.drawLine(QPointF(30, y), QPointF(self.width_px() - 30, y))
        p.setPen(QColor(0, 0, 0, 90))
        p.drawText(QRectF(30, y + 4, 200, 20), "Sign above the line")
        p.setPen(QPen(self.color, self.pen_width, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        p.drawPath(self._path())

    def width_px(self) -> int:
        return self.width()

    def to_image(self) -> QImage:
        scale = 3.0
        img = QImage(int(self.width_px() * scale), int(self.height() * scale), QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(self.color, self.pen_width * scale, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap,
                      Qt.PenJoinStyle.RoundJoin))
        p.drawPath(self._path(scale))
        p.end()
        return _trim(img)


class SignatureDialog(QDialog):
    """Pick a saved signature or make a new one. self.chosen holds the PNG path afterwards."""

    def __init__(self, parent=None, current: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Signatures")
        self.resize(620, 520)
        self.chosen = current
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Saved signatures (stored only on this computer):"))
        self.saved = QListWidget()
        self.saved.setViewMode(QListView.ViewMode.IconMode)
        self.saved.setIconSize(QSize(180, 70))
        self.saved.setGridSize(QSize(200, 90))
        self.saved.setMovement(QListView.Movement.Static)
        self.saved.setResizeMode(QListView.ResizeMode.Adjust)
        self.saved.setMaximumHeight(120)
        self.saved.itemDoubleClicked.connect(lambda _it: self._use_selected())
        lay.addWidget(self.saved)
        row = QHBoxLayout()
        use = QPushButton("Use selected")
        use.clicked.connect(self._use_selected)
        delete = QPushButton("Delete selected")
        delete.clicked.connect(self._delete)
        row.addWidget(use)
        row.addWidget(delete)
        row.addStretch(1)
        lay.addLayout(row)

        lay.addWidget(QLabel("Make a new signature:"))
        self.tabs = QTabWidget()
        # draw
        draw = QWidget()
        d = QVBoxLayout(draw)
        self.pad = SignaturePad()
        d.addWidget(self.pad, 1)
        dr = QHBoxLayout()
        clear = QPushButton("Clear")
        clear.clicked.connect(self.pad.clear)
        self.ink = ColorButton("#0b1f66", "Ink color")
        self.ink.color_changed.connect(self._ink)
        dr.addWidget(QLabel("Ink:"))
        dr.addWidget(self.ink)
        dr.addStretch(1)
        dr.addWidget(clear)
        d.addLayout(dr)
        self.tabs.addTab(draw, "Draw")
        # type
        typ = QWidget()
        t = QVBoxLayout(typ)
        self.name = QLineEdit()
        self.name.setPlaceholderText("Type your name")
        self.font_box = QComboBox()
        families = QFontDatabase.families()
        preferred = [f for f in families if any(k in f.lower() for k in
                     ("script", "hand", "brush", "signature", "cursive", "dancing", "pacifico", "satisfy",
                      "comic", "chancery", "z003", "segoe print", "lucida handwriting", "italic"))]
        for f in preferred + [f for f in ("DejaVu Serif", "Liberation Serif", "Times New Roman", "Georgia")
                              if f in families and f not in preferred]:
            self.font_box.addItem(f)
        if self.font_box.count() == 0:
            self.font_box.addItem(QFont().family())
        self.preview = QLabel()
        self.preview.setMinimumHeight(120)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("QLabel { background: white; border-radius: 6px; }")
        self.name.textChanged.connect(self._update_typed)
        self.font_box.currentIndexChanged.connect(self._update_typed)
        t.addWidget(self.name)
        t.addWidget(self.font_box)
        t.addWidget(self.preview, 1)
        self.tabs.addTab(typ, "Type")
        # image
        img = QWidget()
        im = QVBoxLayout(img)
        pick = QPushButton("Choose an image of your signature...")
        pick.clicked.connect(self._pick_image)
        self.remove_bg = QCheckBox("Make the white background transparent")
        self.remove_bg.setChecked(True)
        self.img_preview = QLabel("A photo or scan of your signature on white paper works best.")
        self.img_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.img_preview.setMinimumHeight(120)
        self._img: QImage | None = None
        im.addWidget(pick)
        im.addWidget(self.remove_bg)
        im.addWidget(self.img_preview, 1)
        self.tabs.addTab(img, "Image")
        lay.addWidget(self.tabs, 1)
        box = QDialogButtonBox()
        save = box.addButton("Save and use", QDialogButtonBox.ButtonRole.AcceptRole)
        box.addButton(QDialogButtonBox.StandardButton.Cancel)
        save.clicked.connect(self._save_new)
        box.rejected.connect(self.reject)
        lay.addWidget(box)
        self._reload()
        if self.saved.count() and not current:
            self.saved.setCurrentRow(0)

    def _ink(self, color: str) -> None:
        self.pad.color = QColor(color)
        self.pad.update()
        self._update_typed()

    def _reload(self) -> None:
        self.saved.clear()
        for path in list_signatures():
            item = QListWidgetItem(QIcon(QPixmap(path)), "")
            item.setData(Qt.ItemDataRole.UserRole, path)
            self.saved.addItem(item)
            if path == self.chosen:
                self.saved.setCurrentItem(item)

    def _typed_image(self) -> QImage | None:
        text = self.name.text().strip()
        if not text:
            return None
        font = QFont(self.font_box.currentText())
        font.setPixelSize(140)
        family = self.font_box.currentText().lower()
        script_like = any(k in family for k in ("script", "hand", "brush", "signature", "cursive", "chancery",
                                                 "dancing", "pacifico", "satisfy", "print", "z003"))
        font.setItalic(not script_like)
        from PySide6.QtGui import QFontMetrics
        fm = QFontMetrics(font)
        w = fm.horizontalAdvance(text) + 80
        h = fm.height() + 60
        img = QImage(w, h, QImage.Format.Format_ARGB32)
        img.fill(Qt.GlobalColor.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        p.setFont(font)
        p.setPen(QColor(self.ink.color()))
        p.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, text)
        p.end()
        return _trim(img)

    def _update_typed(self) -> None:
        img = self._typed_image()
        if img is None:
            self.preview.clear()
            return
        pm = QPixmap.fromImage(img).scaled(self.preview.width() - 20, self.preview.height() - 20,
                                           Qt.AspectRatioMode.KeepAspectRatio,
                                           Qt.TransformationMode.SmoothTransformation)
        self.preview.setPixmap(pm)

    def _pick_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Signature image", "",
                                              "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff)")
        if not path:
            return
        img = QImage(path)
        if img.isNull():
            ui.warning(self, "Signature", "That image couldn't be read.")
            return
        img = img.convertToFormat(QImage.Format.Format_ARGB32)
        if img.width() > 1600 or img.height() > 1600:
            img = img.scaled(1600, 1600, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
        self._img = img
        self.img_preview.setPixmap(QPixmap.fromImage(img).scaled(400, 120, Qt.AspectRatioMode.KeepAspectRatio,
                                                                 Qt.TransformationMode.SmoothTransformation))

    def _image_from_file(self) -> QImage | None:
        if self._img is None:
            return None
        img = self._img.copy()
        if self.remove_bg.isChecked():
            img = _remove_white(img)
        return _trim(img)

    def _use_selected(self) -> None:
        item = self.saved.currentItem()
        if item is None:
            ui.information(self, "Signatures", "Select a saved signature first, or make a new one below.")
            return
        self.chosen = item.data(Qt.ItemDataRole.UserRole)
        self.accept()

    def _delete(self) -> None:
        item = self.saved.currentItem()
        if item is None:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        try:
            os.remove(path)
        except OSError:
            pass
        if self.chosen == path:
            self.chosen = ""
        self._reload()

    def _save_new(self) -> None:
        tab = self.tabs.currentIndex()
        if tab == 0:
            img = None if self.pad.is_empty() else self.pad.to_image()
        elif tab == 1:
            img = self._typed_image()
        else:
            img = self._image_from_file()
        if img is None or img.isNull():
            ui.information(self, "Signatures", "Draw, type or choose a signature first.")
            return
        path = str(signatures_dir() / f"signature-{int(time.time() * 1000)}.png")
        img.save(path, "PNG")
        self.chosen = path
        self.accept()
