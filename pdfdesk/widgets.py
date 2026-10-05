"""Small reusable widgets."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QColorDialog, QFileDialog, QHBoxLayout, QLineEdit, QMenu, QPushButton, QToolButton,
                               QWidget, QWidgetAction, QGridLayout)

SWATCHES = ["#1d1d1f", "#5f6368", "#ffffff", "#e5383b", "#f77f00", "#ffd60a", "#2a9d8f", "#38b000",
            "#2f80ed", "#1f4e9c", "#7b2cbf", "#e05297", "#8d5524", "#00b4d8", "#90e0ef", "#ffadad"]


def swatch_icon(color: str, size: int = 16, ring: bool = True) -> QIcon:
    pm = QPixmap(size * 2, size * 2)
    pm.setDevicePixelRatio(2)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(QColor(0, 0, 0, 90) if ring else Qt.PenStyle.NoPen)
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pm)


class ColorButton(QToolButton):
    """A button showing the current color with a quick palette and a full color picker."""
    color_changed = Signal(str)

    def __init__(self, color: str = "#e5383b", tooltip: str = "Color", parent=None):
        super().__init__(parent)
        self._color = color
        self.setToolTip(tooltip)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setIconSize(QSize(18, 18))
        menu = QMenu(self)
        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setContentsMargins(8, 8, 8, 4)
        grid.setSpacing(4)
        for k, c in enumerate(SWATCHES):
            b = QToolButton()
            b.setIcon(swatch_icon(c, 20))
            b.setIconSize(QSize(20, 20))
            b.setAutoRaise(True)
            b.setToolTip(c)
            b.clicked.connect(lambda _=False, col=c: (self.set_color(col, emit=True), menu.close()))
            grid.addWidget(b, k // 8, k % 8)
        wa = QWidgetAction(menu)
        wa.setDefaultWidget(grid_host)
        menu.addAction(wa)
        menu.addSeparator()
        menu.addAction("More colors...", self._pick)
        self.setMenu(menu)
        self._refresh()

    def color(self) -> str:
        return self._color

    def set_color(self, color: str, emit: bool = False) -> None:
        self._color = color
        self._refresh()
        if emit:
            self.color_changed.emit(color)

    def _refresh(self) -> None:
        self.setIcon(swatch_icon(self._color, 18))

    def _pick(self) -> None:
        c = QColorDialog.getColor(QColor(self._color), self, "Choose a color")
        if c.isValid():
            self.set_color(c.name(), emit=True)


class PathPicker(QWidget):
    """Line edit + Browse button for a file or folder."""
    changed = Signal(str)

    def __init__(self, mode: str = "save", caption: str = "Choose", file_filter: str = "", parent=None):
        super().__init__(parent)
        self.mode = mode  # save | open | folder
        self.caption = caption
        self.file_filter = file_filter
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit()
        self.edit.textChanged.connect(self.changed.emit)
        btn = QPushButton("Browse...")
        btn.clicked.connect(self._browse)
        lay.addWidget(self.edit, 1)
        lay.addWidget(btn)

    def path(self) -> str:
        return self.edit.text().strip()

    def set_path(self, path: str) -> None:
        self.edit.setText(path)

    def _browse(self) -> None:
        cur = self.path()
        if self.mode == "folder":
            p = QFileDialog.getExistingDirectory(self, self.caption, cur)
        elif self.mode == "open":
            p, _ = QFileDialog.getOpenFileName(self, self.caption, cur, self.file_filter)
        else:
            p, _ = QFileDialog.getSaveFileName(self, self.caption, cur, self.file_filter)
        if p:
            self.set_path(p)
