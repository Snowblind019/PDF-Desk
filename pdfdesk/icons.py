"""Toolbar icons (Lucide, ISC license) tinted to match the current theme."""
from __future__ import annotations

import os
import weakref

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap, QGuiApplication, QPalette
from PySide6.QtSvg import QSvgRenderer

ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icons")
ASSET_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets")

_cache: dict[tuple, QIcon] = {}
_bound: list[tuple[weakref.ref, str, str | None]] = []


def _text_color() -> str:
    app = QGuiApplication.instance()
    if app is None:
        return "#1d1d1f"
    return app.palette().color(QPalette.ColorRole.WindowText).name()


def _disabled_color() -> str:
    app = QGuiApplication.instance()
    if app is None:
        return "#9a9aa0"
    return app.palette().color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText).name()


def _render(svg: bytes, size: int, scale: int = 2) -> QPixmap:
    renderer = QSvgRenderer(QByteArray(svg))
    pm = QPixmap(size * scale, size * scale)
    pm.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter, QRectF(0, 0, size * scale, size * scale))
    painter.end()
    pm.setDevicePixelRatio(scale)
    return pm


def icon(name: str, color: str | None = None) -> QIcon:
    color = color or _text_color()
    key = (name, color)
    if key in _cache:
        return _cache[key]
    path = os.path.join(ICON_DIR, f"{name}.svg")
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError:
        return QIcon()
    normal = raw.replace(b"currentColor", color.encode())
    disabled = raw.replace(b"currentColor", _disabled_color().encode())
    ico = QIcon()
    for size in (16, 20, 24, 32, 48):
        ico.addPixmap(_render(normal, size), QIcon.Mode.Normal, QIcon.State.Off)
        ico.addPixmap(_render(disabled, size), QIcon.Mode.Disabled, QIcon.State.Off)
    _cache[key] = ico
    return ico


def app_icon() -> QIcon:
    ico = QIcon()
    svg_path = os.path.join(ASSET_DIR, "pdfdesk.svg")
    try:
        with open(svg_path, "rb") as fh:
            raw = fh.read()
        for size in (16, 24, 32, 48, 64, 128, 256):
            ico.addPixmap(_render(raw, size, 1))
    except OSError:
        pass
    return ico


def bind(obj, name: str, color: str | None = None):
    """Set an icon on a QAction/QAbstractButton and keep it in sync with theme changes."""
    obj.setIcon(icon(name, color))
    _bound.append((weakref.ref(obj), name, color))
    return obj


def refresh_all() -> None:
    _cache.clear()
    alive = []
    for ref, name, color in _bound:
        obj = ref()
        if obj is None:
            continue
        try:
            obj.setIcon(icon(name, color))
            alive.append((ref, name, color))
        except RuntimeError:  # underlying C++ object deleted
            continue
    _bound[:] = alive
