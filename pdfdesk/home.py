"""The Home screen: quick actions and the recent files grid."""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QModelIndex, QRectF, QSize, Qt, Signal, QUrl
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QFontMetrics, QGuiApplication, QPainter, QPainterPath,
                           QPixmap, QPen)
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget,
                               QListWidgetItem, QMenu, QPushButton, QSizePolicy, QStyle, QStyledItemDelegate, QFrame,
                               QVBoxLayout, QWidget, QGridLayout)

from pdfdesk import ui
from pdfdesk import APP_NAME, icons, theme
from pdfdesk.recents import RecentFiles, relative_time

CARD_W, CARD_H, THUMB_H = 188, 246, 160
PATH_ROLE = Qt.ItemDataRole.UserRole
ENTRY_ROLE = Qt.ItemDataRole.UserRole + 1


class RecentDelegate(QStyledItemDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._thumbs: dict[str, tuple[float, QPixmap | None]] = {}

    def sizeHint(self, option, index) -> QSize:
        return QSize(CARD_W, CARD_H)

    def _thumb(self, path: str) -> QPixmap | None:
        tp = RecentFiles.thumb_path(path)
        try:
            mtime = tp.stat().st_mtime
        except OSError:
            return None
        hit = self._thumbs.get(path)
        if hit and hit[0] == mtime:
            return hit[1]
        pm = QPixmap(str(tp))
        self._thumbs[path] = (mtime, pm if not pm.isNull() else None)
        return self._thumbs[path][1]

    def paint(self, p: QPainter, option, index: QModelIndex) -> None:
        c = theme.colors()
        entry = index.data(ENTRY_ROLE) or {}
        path = entry.get("path", "")
        exists = entry.get("_exists", False)  # checked once per reload, not on every repaint
        r = QRectF(option.rect).adjusted(6, 6, -6, -6)
        hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        card = QPainterPath()
        card.addRoundedRect(r, 10, 10)
        p.fillPath(card, QColor(c["card_hover"] if hovered else c["card"]))
        p.setPen(QPen(QColor(c["highlight"]) if selected else QColor(c["border"]), 1.4 if selected else 1))
        p.drawPath(card)
        # thumbnail
        tr = QRectF(r.left() + 12, r.top() + 12, r.width() - 24, THUMB_H - 16)
        pm = self._thumb(path) if exists else None
        if pm:
            pw, ph = pm.width() / pm.devicePixelRatio(), pm.height() / pm.devicePixelRatio()
            s = min(tr.width() / pw, tr.height() / ph)
            w, h = pw * s, ph * s
            target = QRectF(tr.center().x() - w / 2, tr.top() + (tr.height() - h) / 2, w, h)
            p.fillRect(target.adjusted(1, 2, 1, 2), QColor(0, 0, 0, 35))
            p.drawPixmap(target.toRect(), pm)
            p.setPen(QColor(0, 0, 0, 40))
            p.drawRect(target)
        else:
            ico = icons.icon("file-text" if exists else "file-search", c["muted"])
            ico.paint(p, QRectF(tr.center().x() - 24, tr.center().y() - 24, 48, 48).toRect())
        if not exists:
            p.setOpacity(0.55)
        # text
        name_font = QFont(option.font)
        name_font.setBold(True)
        p.setFont(name_font)
        p.setPen(QColor(c["text"]))
        fm = QFontMetrics(name_font)
        text_left = r.left() + 12
        text_w = int(r.width() - 24)
        y = r.top() + THUMB_H + 4
        name = entry.get("name") or Path(path).name
        p.drawText(QRectF(text_left, y, text_w, fm.height()), Qt.AlignmentFlag.AlignLeft,
                   fm.elidedText(name, Qt.TextElideMode.ElideMiddle, text_w))
        small = QFont(option.font)
        small.setPointSizeF(max(7.5, option.font.pointSizeF() - 1))
        p.setFont(small)
        fm2 = QFontMetrics(small)
        p.setPen(QColor(c["muted"]))
        folder = os.path.dirname(path)
        home = str(Path.home())
        if folder.startswith(home):
            folder = "~" + folder[len(home):]
        y += fm.height() + 3
        p.drawText(QRectF(text_left, y, text_w, fm2.height()), Qt.AlignmentFlag.AlignLeft,
                   fm2.elidedText(folder, Qt.TextElideMode.ElideLeft, text_w))
        y += fm2.height() + 2
        if exists:
            bits = [relative_time(entry.get("opened"))]
            if entry.get("pages"):
                pg = int(entry.get("page", 0)) + 1
                total = int(entry["pages"])
                bits.append(f"page {pg} of {total}" if pg > 1 else f"{total} page{'s' if total != 1 else ''}")
            info = "  ·  ".join(b for b in bits if b)
        else:
            info = "File not found"
        p.drawText(QRectF(text_left, y, text_w, fm2.height()), Qt.AlignmentFlag.AlignLeft,
                   fm2.elidedText(info, Qt.TextElideMode.ElideRight, text_w))
        p.setOpacity(1.0)
        if entry.get("pinned"):
            pr = QRectF(r.right() - 30, r.top() + 8, 22, 22)
            badge = QPainterPath()
            badge.addRoundedRect(pr, 11, 11)
            p.fillPath(badge, QColor(c["highlight"]))
            icons.icon("pin", "#ffffff").paint(p, pr.adjusted(4, 4, -4, -4).toRect())
        p.restore()


class RecentGrid(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setGridSize(QSize(CARD_W + 6, CARD_H + 6))
        self.setSpacing(0)
        self.setMouseTracking(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setItemDelegate(RecentDelegate(self))
        self.setFrameShape(QListWidget.Shape.NoFrame)
        self.viewport().setAutoFillBackground(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)


class ActionTile(QFrame):
    """A clickable card with an icon, a title and a short description."""
    clicked = Signal()

    def __init__(self, icon_name: str, title: str, desc: str, parent=None):
        super().__init__(parent)
        self.setObjectName("actionTile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMinimumHeight(64)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(12)
        self.icon = QLabel()
        self.icon_name = icon_name
        self.icon.setFixedSize(28, 28)
        lay.addWidget(self.icon)
        text = QVBoxLayout()
        text.setSpacing(1)
        t = QLabel(title)
        t.setObjectName("tileTitle")
        d = QLabel(desc)
        d.setObjectName("muted")
        text.addWidget(t)
        text.addWidget(d)
        lay.addLayout(text, 1)
        self.refresh_icon()

    def refresh_icon(self) -> None:
        self.icon.setPixmap(icons.icon(self.icon_name, theme.colors()["highlight"]).pixmap(26, 26))

    def mouseReleaseEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton and self.rect().contains(ev.position().toPoint()):
            self.clicked.emit()

    def keyPressEvent(self, ev) -> None:
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Space):
            self.clicked.emit()
            return
        super().keyPressEvent(ev)


class HomePage(QWidget):
    open_requested = Signal(str)
    action_requested = Signal(str)

    def __init__(self, recents: RecentFiles, parent=None):
        super().__init__(parent)
        self.setObjectName("home")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.recents = recents
        outer = QVBoxLayout(self)
        outer.setContentsMargins(36, 28, 36, 16)
        outer.setSpacing(14)

        head = QHBoxLayout()
        logo = QLabel()
        logo.setPixmap(icons.app_icon().pixmap(44, 44))
        head.addWidget(logo)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        t = QLabel(APP_NAME)
        t.setObjectName("homeTitle")
        sub = QLabel("View, edit, convert and sign PDFs. Everything stays on this computer.")
        sub.setObjectName("muted")
        titles.addWidget(t)
        titles.addWidget(sub)
        head.addLayout(titles)
        head.addStretch(1)
        outer.addLayout(head)

        tiles = QGridLayout()
        tiles.setHorizontalSpacing(10)
        tiles.setVerticalSpacing(10)
        actions = [
            ("open", "folder-open", "Open a file", "PDF or any supported file"),
            ("create", "file-plus-2", "Create PDF", "From images, Word, Excel, text..."),
            ("combine", "combine", "Combine files", "Merge several files into one PDF"),
            ("blank", "file", "Blank PDF", "Start with an empty page"),
            ("clipboard", "clipboard", "From clipboard", "Paste an image or text"),
        ]
        for col, (key, ico, title, desc) in enumerate(actions):
            tile = ActionTile(ico, title, desc)
            tile.clicked.connect(lambda k=key: self.action_requested.emit(k))
            tiles.addWidget(tile, 0, col)
        outer.addLayout(tiles)

        bar = QHBoxLayout()
        sec = QLabel("Recent files")
        sec.setObjectName("sectionTitle")
        bar.addWidget(sec)
        bar.addStretch(1)
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter recent files")
        self.filter.setClearButtonEnabled(True)
        self.filter.setFixedWidth(240)
        self.filter.textChanged.connect(self.reload)
        bar.addWidget(self.filter)
        self.clear_btn = QPushButton("Clear list")
        self.clear_btn.clicked.connect(lambda: self.action_requested.emit("clear_recents"))
        bar.addWidget(self.clear_btn)
        outer.addLayout(bar)

        self.grid = RecentGrid()
        self.grid.itemActivated.connect(self._activated)
        self.grid.itemClicked.connect(self._activated)
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self._menu)
        outer.addWidget(self.grid, 1)

        self.empty = QLabel("Files you open will show up here.\n\nDrop any file on this window to open it, "
                            "or to turn it into a PDF.")
        self.empty.setObjectName("muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(self.empty, 1)

        tip = QLabel("Tip: drag files here to open them. Word, Excel, PowerPoint, images, text and more "
                     "are turned into PDFs automatically.")
        tip.setObjectName("muted")
        outer.addWidget(tip)
        self.reload()

    def reload(self, *_args) -> None:
        for tile in self.findChildren(ActionTile):
            tile.refresh_icon()
        needle = self.filter.text().strip().lower() if hasattr(self, "filter") else ""
        self.grid.clear()
        shown = 0
        for entry in self.recents.entries():
            if needle and needle not in entry["path"].lower():
                continue
            item = QListWidgetItem()
            item.setData(PATH_ROLE, entry["path"])
            item.setData(ENTRY_ROLE, dict(entry, _exists=os.path.exists(entry["path"])))
            item.setToolTip(ui.tip(entry["path"]))
            item.setSizeHint(QSize(CARD_W, CARD_H))
            self.grid.addItem(item)
            shown += 1
        has_any = bool(self.recents.items)
        self.grid.setVisible(shown > 0)
        self.empty.setVisible(shown == 0)
        if has_any and shown == 0:
            self.empty.setText("No recent files match the filter.")
        elif not has_any:
            self.empty.setText("Files you open will show up here.\n\nDrop any file on this window to open "
                               "it, or to turn it into a PDF.")
        self.clear_btn.setEnabled(has_any)

    def _activated(self, item) -> None:
        self.open_requested.emit(item.data(PATH_ROLE))

    def _menu(self, pos) -> None:
        item = self.grid.itemAt(pos)
        if item is None:
            return
        path = item.data(PATH_ROLE)
        entry = item.data(ENTRY_ROLE) or {}
        menu = QMenu(self)
        menu.addAction("Open", lambda: self.open_requested.emit(path))
        menu.addAction("Show in folder", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(path))))
        menu.addAction("Copy path", lambda: QGuiApplication.clipboard().setText(path))
        menu.addSeparator()
        menu.addAction("Unpin" if entry.get("pinned") else "Pin to top", lambda: self._pin(path))
        menu.addAction("Remove from recent files", lambda: self._remove(path))
        menu.exec(self.grid.viewport().mapToGlobal(pos))

    def _pin(self, path: str) -> None:
        self.recents.toggle_pin(path)
        self.reload()

    def _remove(self, path: str) -> None:
        self.recents.remove(path)
        self.reload()
