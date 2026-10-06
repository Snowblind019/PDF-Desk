"""Presentation mode (F5): one page at a time, full screen, on a black background."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

import pymupdf as fitz

from pdfdesk import fonts, jobs
from pdfdesk.document import PdfDocument


class PresentationWindow(QWidget):
    def __init__(self, pdf: PdfDocument, start: int = 0, night: bool = False):
        super().__init__(None, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle("Presentation")
        self.setCursor(Qt.CursorShape.BlankCursor)
        self.setMouseTracking(True)
        self.pdf = pdf
        self.page = max(0, min(pdf.page_count - 1, start))
        self.night = night
        self._cache: dict[tuple, QPixmap] = {}
        self._show_number = False
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self._hide_extras)
        pdf.changed.connect(self._doc_changed)

    def _doc_changed(self, *_):
        self._cache.clear()
        self.update()

    def closeEvent(self, ev) -> None:
        try:
            self.pdf.changed.disconnect(self._doc_changed)
        except (RuntimeError, TypeError):
            pass
        super().closeEvent(ev)

    def start(self) -> None:
        self.showFullScreen()
        self.activateWindow()
        self.setFocus()
        self._flash_number()

    # ------------------------------------------------------------------ drawing
    def _pixmap(self) -> QPixmap | None:
        dpr = self.devicePixelRatioF()
        key = (self.page, self.width(), self.height(), dpr, self.pdf.page_key(self.page))
        pm = self._cache.get(key)
        if pm is not None:
            return pm
        if jobs.busy():
            return None
        page = self.pdf.page(self.page)
        r = page.rect
        zoom = min(self.width() / r.width, self.height() / r.height) * dpr
        zoom = fonts.safe_zoom(r, zoom)
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False, annots=True)
        if self.night:
            pix.invert_irect()
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(dpr)
        if len(self._cache) > 6:
            self._cache.clear()
        self._cache[key] = pm
        QTimer.singleShot(0, self, self._prefetch)
        return pm

    def _prefetch(self) -> None:
        """Render the next page ahead of time so turning the page is instant."""
        if self.page + 1 < self.pdf.page_count and not jobs.busy():
            cur = self.page
            self.page += 1
            self._pixmap()
            self.page = cur

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#000000"))
        pm = self._pixmap()
        if pm is not None:
            w = pm.width() / pm.devicePixelRatio()
            h = pm.height() / pm.devicePixelRatio()
            p.drawPixmap(QPointF((self.width() - w) / 2, (self.height() - h) / 2), pm)
        else:
            QTimer.singleShot(250, self, self.update)
        if self._show_number:
            text = f"{self.page + 1} / {self.pdf.page_count}"
            f = QFont()
            f.setPixelSize(16)
            p.setFont(f)
            box = QRectF(self.width() - 130, self.height() - 44, 110, 30)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(0, 0, 0, 150))
            p.drawRoundedRect(box, 8, 8)
            p.setPen(QColor("#ffffff"))
            p.drawText(box, Qt.AlignmentFlag.AlignCenter, text)
        p.end()

    def _flash_number(self) -> None:
        self._show_number = True
        self._hide_timer.start(1500)
        self.update()

    def _hide_extras(self) -> None:
        self._show_number = False
        self.setCursor(Qt.CursorShape.BlankCursor)
        self.update()

    # ------------------------------------------------------------------ moving around
    def go(self, page: int) -> None:
        page = max(0, min(self.pdf.page_count - 1, page))
        if page != self.page:
            self.page = page
            self._flash_number()

    def keyPressEvent(self, ev) -> None:
        k = ev.key()
        if k in (Qt.Key.Key_Escape, Qt.Key.Key_F5, Qt.Key.Key_Q):
            self.close()
        elif k in (Qt.Key.Key_Right, Qt.Key.Key_Down, Qt.Key.Key_PageDown, Qt.Key.Key_Space, Qt.Key.Key_Return,
                   Qt.Key.Key_Enter, Qt.Key.Key_N):
            self.go(self.page + 1)
        elif k in (Qt.Key.Key_Left, Qt.Key.Key_Up, Qt.Key.Key_PageUp, Qt.Key.Key_Backspace, Qt.Key.Key_P):
            self.go(self.page - 1)
        elif k == Qt.Key.Key_Home:
            self.go(0)
        elif k == Qt.Key.Key_End:
            self.go(self.pdf.page_count - 1)
        elif k in (Qt.Key.Key_B, Qt.Key.Key_Period):
            self.night = not self.night
            self._cache.clear()
            self.update()
        else:
            super().keyPressEvent(ev)

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self.go(self.page + 1)
        elif ev.button() == Qt.MouseButton.RightButton:
            self.go(self.page - 1)

    def mouseMoveEvent(self, ev) -> None:
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self._hide_timer.start(1500)

    def wheelEvent(self, ev) -> None:
        self.go(self.page + (1 if ev.angleDelta().y() < 0 else -1))
