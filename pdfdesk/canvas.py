"""The page view: renders pages in tiles, handles zoom and scrolling, and runs every mouse tool."""
from __future__ import annotations

import math
import os
from collections import OrderedDict
from pathlib import Path

import pymupdf as fitz
from PySide6.QtCore import QPointF, QRectF, QTimer, Qt, QUrl, Signal, QEvent
from PySide6.QtGui import (QColor, QDesktopServices, QFont, QFontMetricsF, QGuiApplication, QImage, QPainter, QPen,
                           QPixmap, QPolygonF, QTextOption)
from PySide6.QtWidgets import (QFileDialog, QInputDialog, QMenu, QPlainTextEdit,
                               QScrollArea, QWidget, QFrame)

from pdfdesk import annots, fonts, jobs, pdfops, safety, theme, ui
from pdfdesk import tools as T
from pdfdesk.document import PdfDocument

PT_TO_PX = 96 / 72
MARGIN = 18
GAP = 14
TILE = 512
MIN_ZOOM = 0.1
MAX_ZOOM = 8.0
TILE_BUDGET = 220 * 1024 * 1024
HANDLE = 7

SEL_COLOR = QColor(40, 110, 255, 70)
HIT_COLOR = QColor(255, 214, 10, 110)
HIT_CURRENT = QColor(255, 140, 0, 150)
FORM_COLOR = QColor(70, 130, 255, 34)
FORM_BORDER = QColor(70, 130, 255, 120)


class TextSelection:
    def __init__(self, pno: int, words: list):
        self.pno = pno
        self.words = words
        self.rects: list[fitz.Rect] = []
        lines: dict[tuple, fitz.Rect] = {}
        order = []
        for w in words:
            key = (w[5], w[6])
            r = fitz.Rect(w[:4])
            if key in lines:
                lines[key] |= r
            else:
                lines[key] = fitz.Rect(r)
                order.append(key)
        self.rects = [lines[k] for k in order]
        parts, last = [], None
        for w in words:
            key = (w[5], w[6])
            if last is None:
                parts.append(w[4])
            elif key == last:
                parts.append(" " + w[4])
            else:
                parts.append("\n" + w[4])
            last = key
        self.text = "".join(parts)


class InlineEditor(QPlainTextEdit):
    """A borderless text box drawn right on the page for typing or editing text."""
    committed = Signal(str)
    cancelled = Signal()

    def __init__(self, parent: QWidget, text: str, font: QFont, color: QColor, single_line: bool = False):
        super().__init__(parent)
        self.single_line = single_line
        self._done = False
        self.setFont(font)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QTextOption.WrapMode.NoWrap if single_line else QTextOption.WrapMode.WordWrap)
        self.setStyleSheet(f"QPlainTextEdit {{ background: rgba(255,255,255,235); color: {color.name()};"
                           f" border: 1px dashed {theme.ACCENT}; border-radius: 0; padding: 0; }}")
        self.document().setDocumentMargin(2)
        self.setPlainText(text)
        self.moveCursor(self.textCursor().MoveOperation.End)
        self.textChanged.connect(self._grow)

    def _grow(self):
        fm = QFontMetricsF(self.font())
        lines = max(1, self.document().blockCount())
        need_h = int(lines * fm.lineSpacing() + 8)
        longest = max((fm.horizontalAdvance(b) for b in self.toPlainText().splitlines() or [""]), default=0)
        need_w = int(longest + fm.averageCharWidth() * 3)
        if need_h > self.height():
            self.resize(self.width(), need_h)
        if (self.single_line or self.lineWrapMode() == QPlainTextEdit.LineWrapMode.NoWrap) and need_w > self.width():
            self.resize(need_w, self.height())

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key.Key_Escape:
            self.cancel()
            return
        if ev.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
                self.single_line or ev.modifiers() & Qt.KeyboardModifier.ControlModifier):
            self.commit()
            return
        super().keyPressEvent(ev)

    def focusOutEvent(self, ev):
        super().focusOutEvent(ev)
        if ev.reason() != Qt.FocusReason.PopupFocusReason:
            self.commit()

    def commit(self):
        if not self._done:
            self._done = True
            self.committed.emit(self.toPlainText())

    def cancel(self):
        if not self._done:
            self._done = True
            self.cancelled.emit()


class PageCanvas(QWidget):
    current_page_changed = Signal(int)
    zoom_changed = Signal(float)
    selection_changed = Signal(bool)
    annot_selected = Signal(object)          # dict with pno, xref, type, colors... or None
    message = Signal(str)
    tool_reset_requested = Signal()
    open_file_requested = Signal(str)
    signature_needed = Signal()
    page_action_requested = Signal(str, int)  # (action, page) for the main window: rotate_cw, delete...

    def __init__(self, view: "DocumentView", pdf: PdfDocument, options: T.ToolOptions, settings):
        super().__init__()
        self.view = view
        self.pdf = pdf
        self.opt = options
        self.settings = settings
        self.zoom = 1.0
        self.zoom_mode = settings.get("default_zoom") if settings.get("default_zoom") in ("fit_width", "fit_page") else None
        if self.zoom_mode is None:
            try:
                self.zoom = float(settings.get("default_zoom")) / 100.0
            except (TypeError, ValueError):
                self.zoom_mode = "fit_width"
        self.layout_mode = settings.get("layout") or "single"
        self.night = bool(settings.get("night_mode"))
        self.highlight_forms = bool(settings.get("highlight_forms"))
        self.page_rects: list[QRectF] = []
        self.current_page = 0
        self._tiles: OrderedDict = OrderedDict()
        self._tile_bytes = 0
        self._dlists: OrderedDict = OrderedDict()
        self._pinfo: dict[int, tuple] = {}
        self._cache: dict[tuple, tuple] = {}
        self.sel: TextSelection | None = None
        self.sel_annot: dict | None = None
        self.search_hits: dict[int, list[fitz.Rect]] = {}
        self.current_hit: tuple[int, int] | None = None
        self.drag: dict | None = None
        self.hover_line: dict | None = None
        self.editor: InlineEditor | None = None
        self._last_click_link = None
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.grabGesture(Qt.GestureType.PinchGesture)
        pdf.changed.connect(self._on_doc_changed)
        pdf.structure_changed.connect(self._on_structure_changed)
        options.tool_changed.connect(self._on_tool_changed)

    # ================================================================== geometry
    @property
    def scale(self) -> float:
        return self.zoom * PT_TO_PX

    def _rows(self) -> list[list[int]]:
        n = self.pdf.page_count
        if self.layout_mode == "facing":
            return [list(range(i, min(n, i + 2))) for i in range(0, n, 2)]
        if self.layout_mode == "cover":
            rows = [[0]] if n else []
            rows += [list(range(i, min(n, i + 2))) for i in range(1, n, 2)]
            return rows
        return [[i] for i in range(n)]

    def fit_zoom(self, mode: str) -> float:
        vp = self.view.viewport()
        vw = max(50, vp.width() - 2 * MARGIN)
        vh = max(50, vp.height() - 2 * MARGIN)
        rows = self._rows()
        if not rows:
            return 1.0
        widest = 1.0
        cols = 1
        for row in rows:
            w = sum(self.pdf.page_rect(i).width for i in row)
            if w > widest:
                widest, cols = w, len(row)
        zw = (vw - GAP * (cols - 1)) / (widest * PT_TO_PX)
        if mode == "fit_page":
            row = next((r for r in rows if self.current_page in r), rows[0])
            tallest = max(self.pdf.page_rect(i).height for i in row)
            row_w = sum(self.pdf.page_rect(i).width for i in row)
            zw = (vw - GAP * (len(row) - 1)) / (row_w * PT_TO_PX)
            zh = vh / (tallest * PT_TO_PX)
            return max(MIN_ZOOM, min(MAX_ZOOM, min(zw, zh)))
        return max(MIN_ZOOM, min(MAX_ZOOM, zw))

    def relayout(self) -> None:
        old_zoom = self.zoom
        if self.zoom_mode:
            self.zoom = self.fit_zoom(self.zoom_mode)
        s = self.scale
        vp = self.view.viewport()
        rows = self._rows()
        n = self.pdf.page_count
        rects: list[QRectF] = [QRectF() for _ in range(n)]
        content_w = 0.0
        for row in rows:
            rw = sum(self.pdf.page_rect(i).width * s for i in row) + GAP * (len(row) - 1)
            content_w = max(content_w, rw)
        width = max(float(vp.width()), content_w + 2 * MARGIN)
        y = float(MARGIN)
        for row in rows:
            rw = sum(self.pdf.page_rect(i).width * s for i in row) + GAP * (len(row) - 1)
            rh = max(self.pdf.page_rect(i).height * s for i in row)
            x = (width - rw) / 2
            if len(row) == 1 and self.layout_mode == "cover" and row[0] == 0 and content_w > rw:
                x = width / 2 + GAP / 2  # cover page sits on the right like a book
            for i in row:
                pr = self.pdf.page_rect(i)
                w, h = pr.width * s, pr.height * s
                rects[i] = QRectF(round(x), round(y + (rh - h) / 2), round(w), round(h))
                x += w + GAP
            y += rh + GAP
        height = max(float(vp.height()), y - GAP + MARGIN)
        self.page_rects = rects
        self.resize(int(math.ceil(width)), int(math.ceil(height)))
        self.update()
        if abs(old_zoom - self.zoom) > 1e-6:
            self.commit_editor()
            self.zoom_changed.emit(self.zoom)

    def visible_pages(self, area: QRectF | None = None) -> list[int]:
        if area is None:
            area = self.visible_area()
        out = []
        for i, r in enumerate(self.page_rects):
            if r.bottom() < area.top():
                continue
            if r.top() > area.bottom():
                if self.layout_mode == "single":
                    break
                continue
            if r.intersects(area):
                out.append(i)
        return out

    def visible_area(self) -> QRectF:
        vp = self.view.viewport()
        return QRectF(self.view.horizontalScrollBar().value(), self.view.verticalScrollBar().value(),
                      vp.width(), vp.height())

    def page_at(self, pos: QPointF) -> int | None:
        for i in self.visible_pages(QRectF(pos.x() - 1, pos.y() - 1, 2, 2)):
            if self.page_rects[i].contains(pos):
                return i
        return None

    def _info(self, i: int):
        info = self._pinfo.get(i)
        if info is None or info[0] != self.pdf.epoch:
            page = self.pdf.page(i)
            info = (self.pdf.epoch, page.rotation_matrix, page.derotation_matrix, page.rotation)
            self._pinfo[i] = info
        return info

    def to_vis(self, i: int, pos: QPointF) -> fitz.Point:
        r = self.page_rects[i]
        s = self.scale
        return fitz.Point((pos.x() - r.left()) / s, (pos.y() - r.top()) / s)

    def to_page(self, i: int, pos: QPointF) -> fitz.Point:
        return self.to_vis(i, pos) * self._info(i)[2]

    def vis_to_canvas(self, i: int, pt: fitz.Point) -> QPointF:
        r = self.page_rects[i]
        return QPointF(r.left() + pt.x * self.scale, r.top() + pt.y * self.scale)

    def vis_rect_to_canvas(self, i: int, vr: fitz.Rect) -> QRectF:
        r = self.page_rects[i]
        s = self.scale
        return QRectF(r.left() + vr.x0 * s, r.top() + vr.y0 * s, vr.width * s, vr.height * s)

    def page_rect_to_vis(self, i: int, rect: fitz.Rect) -> fitz.Rect:
        return (fitz.Rect(rect) * self._info(i)[1]).normalize()

    def vis_rect_to_page(self, i: int, vr: fitz.Rect) -> fitz.Rect:
        return (fitz.Rect(vr) * self._info(i)[2]).normalize()

    def rect_to_canvas(self, i: int, rect: fitz.Rect) -> QRectF:
        return self.vis_rect_to_canvas(i, self.page_rect_to_vis(i, rect))

    def clamp_vis(self, i: int, pt: fitz.Point) -> fitz.Point:
        pr = self.pdf.page_rect(i)
        return fitz.Point(min(max(pt.x, 0), pr.width), min(max(pt.y, 0), pr.height))

    # ================================================================== zoom & navigation
    def set_zoom(self, zoom: float, anchor=None, mode: str | None = None) -> None:
        self.commit_editor()
        vp = self.view.viewport()
        hbar, vbar = self.view.horizontalScrollBar(), self.view.verticalScrollBar()
        if anchor is None:
            anchor = QPointF(vp.width() / 2, vp.height() / 3)
        canvas_pt = QPointF(anchor.x() + hbar.value(), anchor.y() + vbar.value())
        pno = self.page_at(canvas_pt)
        if pno is None:
            pno = self.current_page
            ref = self.page_rects[pno] if self.page_rects else QRectF()
            canvas_pt = QPointF(ref.center().x(), ref.top())
            anchor = QPointF(canvas_pt.x() - hbar.value(), max(0.0, canvas_pt.y() - vbar.value()))
        vis = self.to_vis(pno, canvas_pt) if self.page_rects else fitz.Point()
        self.zoom_mode = mode
        if mode:
            self.zoom = self.fit_zoom(mode)
        else:
            self.zoom = max(MIN_ZOOM, min(MAX_ZOOM, zoom))
        self.relayout()
        if self.page_rects:
            new_pt = self.vis_to_canvas(pno, vis)
            hbar.setValue(int(new_pt.x() - anchor.x()))
            vbar.setValue(int(new_pt.y() - anchor.y()))
            if mode == "fit_page":
                vbar.setValue(int(self.page_rects[pno].top() - MARGIN))
        self.zoom_changed.emit(self.zoom)

    def zoom_by(self, factor: float, anchor=None) -> None:
        self.set_zoom(self.zoom * factor, anchor)

    def go_to_page(self, pno: int, vis_point: fitz.Point | None = None, center_rect: QRectF | None = None) -> None:
        if not (0 <= pno < len(self.page_rects)):
            return
        r = self.page_rects[pno]
        vbar, hbar = self.view.verticalScrollBar(), self.view.horizontalScrollBar()
        vp = self.view.viewport()
        if center_rect is not None:
            vbar.setValue(int(center_rect.center().y() - vp.height() / 2))
            if center_rect.left() < hbar.value() or center_rect.right() > hbar.value() + vp.width():
                hbar.setValue(int(center_rect.center().x() - vp.width() / 2))
        elif vis_point is not None:
            vbar.setValue(int(r.top() + vis_point.y * self.scale - 40))
        else:
            vbar.setValue(int(r.top() - MARGIN / 2))
        self._set_current(pno)

    def update_current_page(self) -> None:
        if not self.page_rects:
            return
        area = self.visible_area()
        probe_y = area.top() + area.height() * 0.35
        best, best_d = self.current_page, None
        for i in self.visible_pages(area):
            r = self.page_rects[i]
            d = 0 if r.top() <= probe_y <= r.bottom() else min(abs(r.top() - probe_y), abs(r.bottom() - probe_y))
            if best_d is None or d < best_d or (d == best_d and i < best):
                best, best_d = i, d
        self._set_current(best)

    def _set_current(self, pno: int) -> None:
        if pno != self.current_page:
            self.current_page = pno
            self.current_page_changed.emit(pno)

    def set_layout(self, mode: str) -> None:
        cur = self.current_page
        self.layout_mode = mode
        self.relayout()
        self.go_to_page(cur)

    def set_night(self, on: bool) -> None:
        self.night = on
        self.update()

    def set_highlight_forms(self, on: bool) -> None:
        self.highlight_forms = on
        self.update()

    # ================================================================== caches
    def _cached(self, kind: str, i: int, producer):
        key = (kind, i)
        pk = self.pdf.page_key(i)
        hit = self._cache.get(key)
        if hit and hit[0] == pk:
            return hit[1]
        value = producer(self.pdf.page(i))
        self._cache[key] = (pk, value)
        return value

    def words(self, i: int) -> list:
        return self._cached("words", i, lambda p: p.get_text("words", sort=False, flags=pdfops.WORD_FLAGS))

    def text_dict(self, i: int) -> dict:
        flags = pdfops.WORD_FLAGS
        return self._cached("dict", i, lambda p: p.get_text("dict", flags=flags))

    def links(self, i: int) -> list:
        return self._cached("links", i, lambda p: p.get_links())

    def widgets(self, i: int) -> list:
        def produce(p):
            return [(fitz.Rect(w.rect), w.field_type, w.xref) for w in p.widgets()]
        return self._cached("widgets", i, produce)

    def annot_boxes(self, i: int) -> list:
        def produce(p):
            return [(fitz.Rect(a.rect), a.type[0], a.xref) for a in p.annots()
                    if a.type[0] not in annots.SKIP_TYPES]
        return self._cached("annots", i, produce)

    def _display_list(self, i: int):
        pk = self.pdf.page_key(i)
        hit = self._dlists.get(i)
        if hit and hit[0] == pk:
            self._dlists.move_to_end(i)
            return hit[1]
        dl = self.pdf.page(i).get_displaylist(annots=True)
        self._dlists[i] = (pk, dl)
        while len(self._dlists) > 10:
            self._dlists.popitem(last=False)
        return dl

    def clear_caches(self, pages=None) -> None:
        if pages is None:
            self._tiles.clear()
            self._tile_bytes = 0
            self._dlists.clear()
            self._cache.clear()
            self._pinfo.clear()
            return
        pages = set(pages)
        for key in [k for k in self._tiles if k[0] in pages]:
            pm = self._tiles.pop(key)[0]
            self._tile_bytes -= pm.width() * pm.height() * 4
        for p in pages:
            self._dlists.pop(p, None)

    def _on_doc_changed(self, pages) -> None:
        if pages is None:
            self.clear_caches()
            self.sel = None
            self.sel_annot = None
            self.hover_line = None
            self.annot_selected.emit(None)
            self.selection_changed.emit(False)
        else:
            self.clear_caches(pages)
        self.update()

    def _on_structure_changed(self) -> None:
        self.clear_caches()
        if self.current_page >= self.pdf.page_count:
            self.current_page = max(0, self.pdf.page_count - 1)
        self.relayout()
        self.update_current_page()

    # ================================================================== painting
    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        colors = theme.colors()
        exposed = QRectF(ev.rect())
        p.fillRect(exposed, QColor(colors["canvas"]))
        dpr = self.devicePixelRatioF()
        busy = jobs.busy()
        retry = False
        paper = QColor("#141414") if self.night else QColor("#ffffff")
        for i in self.visible_pages(exposed):
            r = self.page_rects[i]
            p.fillRect(r.adjusted(-1, 0, 1, 3), QColor(0, 0, 0, 28))
            p.fillRect(r.adjusted(0, 0, 0, 1), QColor(0, 0, 0, 22))
            p.fillRect(r, paper)
            if not self._paint_tiles(p, i, r, exposed, dpr, busy):
                retry = True
            info = self._pinfo.get(i)
            if busy and (info is None or info[0] != self.pdf.epoch):
                continue  # overlays need page details that can't be loaded while a job runs
            self._paint_overlays(p, i, r)
        self._paint_drag(p)
        p.end()
        if retry:
            QTimer.singleShot(250, self.update)

    def _paint_tiles(self, p: QPainter, i: int, r: QRectF, exposed: QRectF, dpr: float, busy: bool) -> bool:
        total = self.scale * dpr
        inter = exposed & r
        if inter.isEmpty():
            return True
        tx0 = int(max(0.0, (inter.left() - r.left()) * dpr) // TILE)
        tx1 = int(max(0.0, (inter.right() - r.left()) * dpr - 1) // TILE)
        ty0 = int(max(0.0, (inter.top() - r.top()) * dpr) // TILE)
        ty1 = int(max(0.0, (inter.bottom() - r.top()) * dpr - 1) // TILE)
        pk = self.pdf.page_key(i)
        complete = True
        dl = None
        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                key = (i, pk, round(total, 4), self.night, tx, ty)
                entry = self._tiles.get(key)
                if entry is None:
                    if busy:
                        complete = False
                        continue
                    try:
                        if dl is None:
                            dl = self._display_list(i)
                        entry = self._render_tile(dl, total, tx, ty, dpr)
                    except Exception:
                        entry = None
                    if entry is None:
                        continue
                    self._tiles[key] = entry
                    self._tile_bytes += entry[0].width() * entry[0].height() * 4
                    while self._tile_bytes > TILE_BUDGET and len(self._tiles) > 8:
                        _k, old = self._tiles.popitem(last=False)
                        self._tile_bytes -= old[0].width() * old[0].height() * 4
                else:
                    self._tiles.move_to_end(key)
                pm, ox, oy = entry
                p.drawPixmap(QPointF(r.left() + ox / dpr, r.top() + oy / dpr), pm)
        return complete

    def _render_tile(self, dl, total: float, tx: int, ty: int, dpr: float):
        clip = fitz.Rect(tx * TILE / total, ty * TILE / total, (tx + 1) * TILE / total, (ty + 1) * TILE / total)
        clip &= dl.rect
        if clip.is_empty:
            return None
        pix = dl.get_pixmap(matrix=fitz.Matrix(total, total), clip=clip, alpha=False)
        if self.night:
            pix.invert_irect()
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(dpr)
        return pm, pix.x, pix.y

    def _paint_overlays(self, p: QPainter, i: int, r: QRectF) -> None:
        p.save()
        p.setClipRect(r.adjusted(-HANDLE, -HANDLE, HANDLE, HANDLE))
        if self.highlight_forms and not (jobs.busy() and ("widgets", i) not in self._cache):
            try:
                for rect, ftype, _xref in self.widgets(i):
                    if ftype in (fitz.PDF_WIDGET_TYPE_BUTTON,):
                        continue
                    cr = self.rect_to_canvas(i, rect)
                    p.fillRect(cr, FORM_COLOR)
                    p.setPen(QPen(FORM_BORDER, 1))
                    p.drawRect(cr)
            except Exception:
                pass
        hits = self.search_hits.get(i)
        if hits:
            p.setPen(Qt.PenStyle.NoPen)
            for k, rect in enumerate(hits):
                current = self.current_hit == (i, k)
                p.fillRect(self.rect_to_canvas(i, rect).adjusted(-1, -1, 1, 1), HIT_CURRENT if current else HIT_COLOR)
        if self.sel and self.sel.pno == i:
            for rect in self.sel.rects:
                p.fillRect(self.rect_to_canvas(i, rect), SEL_COLOR)
        if self.hover_line and self.hover_line["pno"] == i and self.opt.tool == T.EDIT_TEXT and not self.editor:
            p.setPen(QPen(QColor(theme.ACCENT), 1.2, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(self.rect_to_canvas(i, self.hover_line["bbox"]).adjusted(-2, -1, 2, 1))
        if self.sel_annot and self.sel_annot["pno"] == i and not (self.drag and self.drag.get("kind") in ("move", "resize")):
            cr = self.vis_rect_to_canvas(i, self.sel_annot["vis"])
            self._paint_selection_box(p, cr, self.sel_annot.get("resizable", False))
        p.restore()

    def _paint_selection_box(self, p: QPainter, cr: QRectF, handles: bool) -> None:
        accent = QColor(theme.ACCENT)
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(accent, 1.2, Qt.PenStyle.DashLine))
        p.drawRect(cr.adjusted(-2, -2, 2, 2))
        if handles:
            p.setPen(QPen(accent, 1.2))
            p.setBrush(QColor("#ffffff"))
            for hp in self._handle_points(cr.adjusted(-2, -2, 2, 2)):
                p.drawRect(QRectF(hp.x() - HANDLE / 2, hp.y() - HANDLE / 2, HANDLE, HANDLE))

    @staticmethod
    def _handle_points(cr: QRectF) -> list[QPointF]:
        cx, cy = cr.center().x(), cr.center().y()
        return [QPointF(cr.left(), cr.top()), QPointF(cx, cr.top()), QPointF(cr.right(), cr.top()),
                QPointF(cr.right(), cy), QPointF(cr.right(), cr.bottom()), QPointF(cx, cr.bottom()),
                QPointF(cr.left(), cr.bottom()), QPointF(cr.left(), cy)]

    def _paint_drag(self, p: QPainter) -> None:
        d = self.drag
        if not d or "pno" not in d:
            return
        i = d["pno"]
        if i >= len(self.page_rects):
            return
        kind = d["kind"]
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = QColor(self.opt.color_hex()) if self.opt.tool in T.COLOR_TOOLS else QColor(theme.ACCENT)
        if kind == "shape":
            a = self.vis_to_canvas(i, d["start"])
            b = self.vis_to_canvas(i, d["cur"])
            tool = d["tool"]
            pen = QPen(color, max(1.0, self.opt.width * self.scale))
            if tool in (T.RECT, T.ELLIPSE, T.LINE, T.ARROW):
                p.setPen(pen)
                p.setOpacity(self.opt.opacity)
                if tool in (T.RECT, T.ELLIPSE):
                    if self.opt.fill:
                        p.setBrush(QColor(self.opt.fill_hex))
                    rr = QRectF(a, b).normalized()
                    p.drawRect(rr) if tool == T.RECT else p.drawEllipse(rr)
                else:
                    p.drawLine(a, b)
                    if tool == T.ARROW:
                        self._draw_arrow_head(p, a, b, color, max(6.0, self.opt.width * self.scale * 3.5))
            else:
                p.setPen(QPen(QColor(theme.ACCENT), 1.2, Qt.PenStyle.DashLine))
                p.setBrush(QColor(200, 50, 60, 18))
                p.drawRect(QRectF(a, b).normalized())
        elif kind == "ink":
            pts = [self.vis_to_canvas(i, q) for q in d["points"]]
            if len(pts) > 1:
                pen = QPen(color, max(1.0, self.opt.width * self.scale), Qt.PenStyle.SolidLine,
                           Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
                p.setPen(pen)
                p.setOpacity(self.opt.opacity)
                p.drawPolyline(QPolygonF(pts))
        elif kind == "area":
            rr = QRectF(self.vis_to_canvas(i, d["start"]), self.vis_to_canvas(i, d["cur"])).normalized()
            if self.opt.tool == T.REDACT:
                p.setPen(QPen(QColor(220, 30, 40), 1.5))
                p.setBrush(QColor(0, 0, 0, 60))
            else:
                p.setPen(QPen(color, 1, Qt.PenStyle.DashLine))
                c2 = QColor(color)
                c2.setAlpha(70)
                p.setBrush(c2)
            p.drawRect(rr)
        elif kind in ("move", "resize") and d.get("preview") is not None:
            self._paint_selection_box(p, self.vis_rect_to_canvas(i, d["preview"]), kind == "resize")
        p.restore()

    @staticmethod
    def _draw_arrow_head(p: QPainter, a: QPointF, b: QPointF, color: QColor, size: float) -> None:
        ang = math.atan2(b.y() - a.y(), b.x() - a.x())
        left = QPointF(b.x() - size * math.cos(ang - 0.45), b.y() - size * math.sin(ang - 0.45))
        right = QPointF(b.x() - size * math.cos(ang + 0.45), b.y() - size * math.sin(ang + 0.45))
        p.setBrush(color)
        p.drawPolygon(QPolygonF([b, left, right]))

    # ================================================================== selection helpers
    def _word_index(self, words: list, pt: fitz.Point) -> int | None:
        best, best_d = None, None
        for k, w in enumerate(words):
            x0, y0, x1, y1 = w[:4]
            if x0 <= pt.x <= x1 and y0 <= pt.y <= y1:
                return k
            dx = max(x0 - pt.x, 0, pt.x - x1)
            dy = max(y0 - pt.y, 0, pt.y - y1)
            d = dx + dy * 3
            if best_d is None or d < best_d:
                best, best_d = k, d
        return best

    def _word_under(self, i: int, pt: fitz.Point) -> bool:
        for w in self.words(i):
            if w[0] - 1 <= pt.x <= w[2] + 1 and w[1] - 1 <= pt.y <= w[3] + 1:
                return True
        return False

    def _selection_between(self, i: int, a: fitz.Point, b: fitz.Point) -> TextSelection | None:
        words = self.words(i)
        if not words:
            return None
        ia = self._word_index(words, a)
        ib = self._word_index(words, b)
        if ia is None or ib is None:
            return None
        lo, hi = min(ia, ib), max(ia, ib)
        return TextSelection(i, words[lo:hi + 1])

    def clear_selection(self) -> None:
        had = self.sel is not None
        self.sel = None
        if had:
            self.selection_changed.emit(False)
        self.update()

    def has_selection(self) -> bool:
        return bool(self.sel and self.sel.text)

    def selected_text(self) -> str:
        return self.sel.text if self.sel else ""

    def copy_selection(self) -> bool:
        if self.sel and self.sel.text:
            QGuiApplication.clipboard().setText(self.sel.text)
            self.message.emit("Copied text")
            return True
        return False

    def select_all_on_page(self) -> None:
        i = self.current_page
        words = self.words(i)
        if words:
            self.sel = TextSelection(i, list(words))
            self.selection_changed.emit(True)
            self.update()

    def select_annot(self, pno: int | None, xref: int | None = None) -> None:
        if pno is None or xref is None:
            if self.sel_annot is not None:
                self.sel_annot = None
                self.annot_selected.emit(None)
                self.update()
            return
        page = self.pdf.page(pno)  # keep the page alive while the annotation is used
        try:
            annot = page.load_annot(xref)
        except Exception:
            annot = None
        if annot is None:
            self.select_annot(None)
            return
        self.sel_annot = {
            "pno": pno, "xref": xref, "type": annot.type[0], "name": annots.type_name(annot),
            "vis": self.page_rect_to_vis(pno, annot.rect), "movable": annots.can_move(annot),
            "resizable": annots.can_resize(annot), "colors": annot.colors, "border": annot.border,
            "opacity": annot.opacity, "info": annot.info,
        }
        self.annot_selected.emit(self.sel_annot)
        self.update()

    # ================================================================== search
    def set_search_hits(self, hits: dict, current=None) -> None:
        self.search_hits = hits
        self.current_hit = current
        self.update()

    def show_hit(self, pno: int, idx: int) -> None:
        self.current_hit = (pno, idx)
        rects = self.search_hits.get(pno) or []
        if 0 <= idx < len(rects):
            self.go_to_page(pno, center_rect=self.rect_to_canvas(pno, rects[idx]))
        self.update()

    # ================================================================== editing helpers
    def _edit(self, label: str, pages=None, structure=False):
        return self.pdf.edit(label, pages=pages, structure=structure)

    def _author(self) -> str:
        return self.opt.author

    def _fail(self, title: str, exc: Exception) -> None:
        ui.warning(self, title, f"{title} failed:\n\n{exc}")

    def delete_selected_annot(self) -> None:
        sa = self.sel_annot
        if not sa:
            return
        try:
            with self._edit(f"Delete {sa['name'].lower()}", pages=[sa["pno"]]):
                page = self.pdf.page(sa["pno"])
                annot = page.load_annot(sa["xref"])
                if annot:
                    page.delete_annot(annot)
        except Exception as exc:
            self._fail("Delete", exc)
        self.select_annot(None)

    def restyle_selected(self, color=None, width=None, opacity=None) -> None:
        sa = self.sel_annot
        if not sa:
            return
        try:
            with self._edit("Change appearance", pages=[sa["pno"]]):
                page = self.pdf.page(sa["pno"])
                annot = page.load_annot(sa["xref"])
                annots.restyle(self.pdf.doc, annot, color=color, width=width, opacity=opacity)
        except Exception as exc:
            self._fail("Change appearance", exc)
            return
        self.select_annot(sa["pno"], sa["xref"])

    def _apply_transform(self, pno: int, xref: int, new_vis: fitz.Rect, label: str) -> None:
        try:
            with self._edit(label, pages=[pno]):
                page = self.pdf.page(pno)
                annot = page.load_annot(xref)
                new = annots.transform_annot(page, annot, self.vis_rect_to_page(pno, new_vis))
                xref = new.xref
        except Exception as exc:
            self._fail(label, exc)
            return
        self.select_annot(pno, xref)

    # ================================================================== inline editor
    def _qfont(self, size_pt: float, family: str = "sans", bold=False, italic=False) -> QFont:
        f = QFont()
        if family == "serif":
            f.setStyleHint(QFont.StyleHint.Serif)
            f.setFamilies(["Times New Roman", "Liberation Serif", "DejaVu Serif", "Serif"])
        elif family == "mono":
            f.setStyleHint(QFont.StyleHint.Monospace)
            f.setFamilies(["Courier New", "Liberation Mono", "DejaVu Sans Mono", "Monospace"])
        else:
            f.setStyleHint(QFont.StyleHint.SansSerif)
            f.setFamilies(["Arial", "Liberation Sans", "Helvetica", "DejaVu Sans", "Sans Serif"])
        size_pt = size_pt if 0 < size_pt < 1000 else 12.0
        f.setPixelSize(max(6, min(2000, int(round(size_pt * self.scale)))))
        f.setBold(bold)
        f.setItalic(italic)
        return f

    def open_editor(self, cr: QRectF, text: str, font: QFont, color: QColor, on_commit, single_line=False,
                    on_cancel=None) -> None:
        self.commit_editor()
        ed = InlineEditor(self, text, font, color, single_line)
        ed.setGeometry(cr.toAlignedRect())
        ed._grow()
        self.editor = ed

        def done(value):
            if self.editor is ed:
                self.editor = None
            ed.hide()
            ed.deleteLater()
            self.setFocus()
            on_commit(value)

        def cancelled():
            if self.editor is ed:
                self.editor = None
            ed.hide()
            ed.deleteLater()
            self.setFocus()
            if on_cancel:
                on_cancel()
            self.update()

        ed.committed.connect(done)
        ed.cancelled.connect(cancelled)
        ed.show()
        ed.setFocus()
        self.update()

    def commit_editor(self) -> None:
        if self.editor is not None:
            self.editor.commit()

    def _new_textbox(self, i: int, vis_rect: fitz.Rect | None, at: fitz.Point) -> None:
        fs = self.opt.font_size
        dragged = vis_rect is not None and vis_rect.width > 8 and vis_rect.height > 6
        if not dragged:
            vis_rect = fitz.Rect(at.x, at.y - fs * 0.7, at.x + max(120, fs * 12), at.y + fs * 0.9)
        color = QColor(self.opt.color_hex(T.TEXTBOX))
        cr = self.vis_rect_to_canvas(i, vis_rect)
        cr.setHeight(max(cr.height(), fs * self.scale * 1.6))

        def commit(text: str):
            if not text.strip():
                return
            w, h = annots.freetext_size(text, fs)
            pr = self.pdf.page_rect(i)
            if dragged:
                width = max(vis_rect.width, 30)
                lines = 0
                for ln in text.splitlines() or [""]:
                    lines += max(1, math.ceil(fonts.text_width(ln, fs) / max(10, width - fs)))
                height = max(vis_rect.height, lines * fs * 1.25 + fs * 0.5 + 4)
                final = fitz.Rect(vis_rect.x0, vis_rect.y0, vis_rect.x0 + width, vis_rect.y0 + height)
            else:
                final = fitz.Rect(vis_rect.x0, vis_rect.y0, min(pr.width, vis_rect.x0 + w), vis_rect.y0 + h)
            try:
                with self._edit("Add text", pages=[i]):
                    page = self.pdf.page(i)
                    annots.add_textbox(page, self.vis_rect_to_page(i, final), text, fs, self.opt.color(T.TEXTBOX),
                                       fill=self.opt.fill_rgb(), author=self._author())
            except Exception as exc:
                self._fail("Add text", exc)

        self.open_editor(cr, "", self._qfont(fs), color, commit)

    def _edit_freetext(self, pno: int, xref: int) -> None:
        page = self.pdf.page(pno)
        annot = page.load_annot(xref)
        if annot is None:
            return
        size, color = annots.freetext_style(self.pdf.doc, annot)
        vis = self.page_rect_to_vis(pno, annot.rect)
        text = annot.info.get("content", "")
        qcolor = QColor.fromRgbF(*color[:3])

        def commit(new_text: str):
            if new_text == text:
                return
            try:
                with self._edit("Edit text box", pages=[pno]):
                    pg = self.pdf.page(pno)
                    a = pg.load_annot(xref)
                    if not new_text.strip():
                        pg.delete_annot(a)
                        return
                    w, h = annots.freetext_size(new_text, size)
                    cur = self.page_rect_to_vis(pno, a.rect)
                    if h > cur.height or w > cur.width:
                        grown = fitz.Rect(cur.x0, cur.y0, max(cur.x1, cur.x0 + w), max(cur.y1, cur.y0 + h))
                        a.set_rect(self.vis_rect_to_page(pno, grown))
                    annots.set_textbox_text(self.pdf.doc, a, new_text)
            except Exception as exc:
                self._fail("Edit text box", exc)
            self.select_annot(None)

        self.open_editor(self.vis_rect_to_canvas(pno, vis), text, self._qfont(size), qcolor, commit)

    def _edit_note(self, pno: int, xref: int) -> None:
        page = self.pdf.page(pno)
        annot = page.load_annot(xref)
        if annot is None:
            return
        old = annot.info.get("content", "")
        text, ok = QInputDialog.getMultiLineText(self, "Note", "Comment:", old)
        if not ok or text == old:
            return
        try:
            with self._edit("Edit note", pages=[pno]):
                pg = self.pdf.page(pno)
                a = pg.load_annot(xref)
                a.set_info(content=text)
                a.update()
        except Exception as exc:
            self._fail("Edit note", exc)

    def edit_annot_content(self) -> None:
        sa = self.sel_annot
        if not sa:
            return
        if sa["type"] == fitz.PDF_ANNOT_FREE_TEXT:
            self._edit_freetext(sa["pno"], sa["xref"])
        else:
            self._edit_note(sa["pno"], sa["xref"])

    def _line_at(self, i: int, pt: fitz.Point) -> dict | None:
        derot = self._info(i)[2]
        expect = fitz.Point(derot.a, derot.b)
        for block in self.text_dict(i).get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                bbox = fitz.Rect(line["bbox"])
                if not bbox.contains(pt):
                    continue
                spans = [s for s in line["spans"] if s["text"]]
                if not spans:
                    continue
                d = line.get("dir", (1, 0))
                horizontal = abs(d[0] * expect.x + d[1] * expect.y) > 0.98
                main = max(spans, key=lambda s: len(s["text"].strip()))
                family, bold, italic = fonts.span_style(main)
                return {"pno": i, "bbox": bbox, "origin": fitz.Point(spans[0]["origin"]),
                        "text": "".join(s["text"] for s in spans), "size": main["size"],
                        "color": fonts.int_to_rgb(main.get("color", 0)), "family": family,
                        "bold": bold, "italic": italic, "editable": horizontal}
        return None

    def _edit_line(self, line: dict) -> None:
        if not line["editable"]:
            self.message.emit("Only text that runs left to right on the page can be edited.")
            return
        i = line["pno"]
        vis = self.page_rect_to_vis(i, line["bbox"])
        cr = self.vis_rect_to_canvas(i, vis).adjusted(-3, -2, 30, 2)
        font = self._qfont(line["size"], line["family"], line["bold"], line["italic"])
        color = QColor.fromRgbF(*line["color"])
        old = line["text"]

        def commit(new_text: str):
            new_text = new_text.replace("\n", " ")
            if new_text == old:
                return
            try:
                with self._edit("Edit text", pages=[i]):
                    page = self.pdf.page(i)
                    pdfops.replace_text_line(page, line["bbox"], line["origin"], new_text, line["size"],
                                             line["color"], line["family"], line["bold"], line["italic"])
            except Exception as exc:
                self._fail("Edit text", exc)

        self.hover_line = None
        self.open_editor(cr, old, font, color, commit, single_line=True)

    # ================================================================== creating things
    def _make_markup(self, kind: str, sel: TextSelection) -> None:
        tool = {"highlight": T.HIGHLIGHT, "underline": T.UNDERLINE, "strikeout": T.STRIKEOUT}[kind]
        try:
            with self._edit(kind.capitalize(), pages=[sel.pno]):
                page = self.pdf.page(sel.pno)
                annots.add_text_markup(page, kind, sel.rects, self.opt.color(tool),
                                       self.opt.opacity_for(tool), self._author(), sel.text)
        except Exception as exc:
            self._fail(kind.capitalize(), exc)

    def _make_redaction_from_selection(self, sel: TextSelection) -> None:
        try:
            with self._edit("Mark for redaction", pages=[sel.pno]):
                page = self.pdf.page(sel.pno)
                for r in sel.rects:
                    annots.add_redaction(page, r, self._author())
        except Exception as exc:
            self._fail("Mark for redaction", exc)
        self.message.emit("Marked for redaction. Use Apply Redactions to remove it for good.")

    def markup_selection(self, kind: str) -> None:
        if not self.sel:
            return
        sel = self.sel
        self.clear_selection()
        if kind == "redact":
            self._make_redaction_from_selection(sel)
        else:
            self._make_markup(kind, sel)

    def _place_rect(self, i: int, start: fitz.Point, end: fitz.Point, default_w: float, default_h: float,
                    ratio: float | None = None) -> fitz.Rect:
        r = fitz.Rect(start, end).normalize()
        if r.width < 6 or r.height < 6:
            w, h = default_w, default_h
            if ratio:
                h = w * ratio
            r = fitz.Rect(start.x - w / 2, start.y - h / 2, start.x + w / 2, start.y + h / 2)
        pr = self.pdf.page_rect(i)
        dx = max(0, -r.x0) - max(0, r.x1 - pr.width)
        dy = max(0, -r.y0) - max(0, r.y1 - pr.height)
        return r + (dx, dy, dx, dy)

    def _finish_shape(self, d: dict) -> None:
        i, tool = d["pno"], d["tool"]
        a, b = d["start"], d["cur"]
        tiny = abs(a.x - b.x) * self.scale < 4 and abs(a.y - b.y) * self.scale < 4
        if tool in (T.RECT, T.ELLIPSE, T.LINE, T.ARROW):
            if tiny:
                return
            derot = self._info(i)[2]
            try:
                with self._edit(T.TOOLS[tool][0], pages=[i]):
                    page = self.pdf.page(i)
                    annots.add_shape(page, tool, a * derot, b * derot, self.opt.color(tool), self.opt.width,
                                     self.opt.opacity, self.opt.fill_rgb() if tool in (T.RECT, T.ELLIPSE) else None,
                                     self._author())
            except Exception as exc:
                self._fail(T.TOOLS[tool][0], exc)
        elif tool == T.TEXTBOX:
            self._new_textbox(i, None if tiny else fitz.Rect(a, b).normalize(), a)
        elif tool == T.STAMP:
            name = self.opt.stamp
            custom = annots.stamp_code(name) is None
            rect = self._place_rect(i, a, b, 200 if custom else 180, 40 if custom else 50)
            try:
                with self._edit("Add stamp", pages=[i]):
                    annots.add_stamp(self.pdf.page(i), self.vis_rect_to_page(i, rect), name, self._author())
            except Exception as exc:
                self._fail("Add stamp", exc)
        elif tool == T.IMAGE:
            path, _ = QFileDialog.getOpenFileName(self, "Choose an image", self.settings.get("last_dir"),
                                                  "Images (*.png *.jpg *.jpeg *.bmp *.gif *.tif *.tiff *.webp)")
            if path:
                self._place_image_file(i, a, b, path, "Add image")
        elif tool == T.SIGNATURE:
            sig = self.opt.signature
            if not sig or not os.path.exists(sig):
                self.signature_needed.emit()
                return
            self._place_image_file(i, a, b, sig, "Add signature", default_w=160)

    def _place_image_file(self, i, a, b, path, label, default_w=200) -> None:
        try:
            data = Path(path).read_bytes()
            from pdfdesk.convert import _image_bytes_for_mupdf
            data = _image_bytes_for_mupdf(data)
            pix = fitz.Pixmap(data)
            ratio = pix.height / max(1, pix.width)
        except Exception as exc:
            self._fail(label, exc)
            return
        pr = self.pdf.page_rect(i)
        rect = self._place_rect(i, a, b, min(default_w, pr.width * 0.6), 0, ratio)
        try:
            with self._edit(label, pages=[i]):
                annots.add_image(self.pdf.page(i), self.vis_rect_to_page(i, rect), data)
        except Exception as exc:
            self._fail(label, exc)

    def _finish_ink(self, d: dict) -> None:
        pts = d["points"]
        if len(pts) < 2:
            return
        i = d["pno"]
        derot = self._info(i)[2]
        try:
            with self._edit("Draw", pages=[i]):
                annots.add_ink(self.pdf.page(i), [[q * derot for q in pts]], self.opt.color(T.PEN),
                               self.opt.width, self.opt.opacity, self._author())
        except Exception as exc:
            self._fail("Draw", exc)

    def _finish_area(self, d: dict) -> None:
        i = d["pno"]
        vr = fitz.Rect(d["start"], d["cur"]).normalize()
        if vr.width * self.scale < 4 or vr.height * self.scale < 4:
            return
        rect = self.vis_rect_to_page(i, vr)
        tool = self.opt.tool
        try:
            if tool == T.REDACT:
                with self._edit("Mark for redaction", pages=[i]):
                    annots.add_redaction(self.pdf.page(i), rect, self._author())
                self.message.emit("Marked for redaction. Use Apply Redactions to remove it for good.")
            elif tool == T.HIGHLIGHT:
                with self._edit("Highlight", pages=[i]):
                    annots.add_text_markup(self.pdf.page(i), "highlight", [rect], self.opt.color(T.HIGHLIGHT),
                                           self.opt.opacity, self._author())
            else:
                self.message.emit("Drag across text to underline or strike it out.")
        except Exception as exc:
            self._fail(T.TOOLS[tool][0], exc)

    def _add_note(self, i: int, vis: fitz.Point) -> None:
        text, ok = QInputDialog.getMultiLineText(self, "Sticky note", "Comment:")
        if not ok or not text.strip():
            return
        try:
            with self._edit("Add note", pages=[i]):
                annots.add_note(self.pdf.page(i), vis * self._info(i)[2], text, self.opt.color(T.NOTE),
                                self._author())
        except Exception as exc:
            self._fail("Add note", exc)

    # ================================================================== forms & links
    def _click_widget(self, i: int, pt: fitz.Point) -> bool:
        page = self.pdf.page(i)
        w = annots.widget_at(page, pt)
        if w is None:
            return False
        ftype, xref, name = w.field_type, w.xref, w.field_name
        if w.field_flags & 1:  # read-only
            self.message.emit(f"The field '{name}' is read-only.")
            return True

        def run(label, fn):
            try:
                with self._edit(label, pages=None if ftype == fitz.PDF_WIDGET_TYPE_RADIOBUTTON else [i]):
                    pg = self.pdf.page(i)
                    target = next((x for x in pg.widgets() if x.xref == xref), None)
                    if target is not None:
                        fn(pg, target)
            except Exception as exc:
                self._fail(label, exc)

        if ftype == fitz.PDF_WIDGET_TYPE_CHECKBOX:
            run("Fill form", lambda pg, t: annots.toggle_checkbox(t))
        elif ftype == fitz.PDF_WIDGET_TYPE_RADIOBUTTON:
            run("Fill form", lambda pg, t: annots.select_radio(pg, t))
        elif ftype in (fitz.PDF_WIDGET_TYPE_COMBOBOX, fitz.PDF_WIDGET_TYPE_LISTBOX):
            menu = QMenu(self)
            for value, label in annots.choice_options(w):
                act = menu.addAction(label)
                act.setCheckable(True)
                act.setChecked(str(w.field_value) == value)
                act.triggered.connect(lambda _=False, v=value: run("Fill form", lambda pg, t: annots.set_choice(t, v)))
            cr = self.rect_to_canvas(i, w.rect)
            menu.exec(self.mapToGlobal(cr.bottomLeft().toPoint()))
        elif ftype == fitz.PDF_WIDGET_TYPE_TEXT:
            multiline = bool(w.field_flags & fitz.PDF_TX_FIELD_IS_MULTILINE)
            size = w.text_fontsize or (10 if multiline else min(12.0, w.rect.height * 0.65))
            cr = self.rect_to_canvas(i, w.rect)
            old = w.field_value or ""

            def commit(text: str):
                if text != old:
                    run("Fill form", lambda pg, t: annots.set_text_field(self.pdf.doc, pg, t, text))

            self.open_editor(cr, old, self._qfont(size), QColor("#000000"), commit, single_line=not multiline)
        elif ftype == fitz.PDF_WIDGET_TYPE_SIGNATURE:
            self.message.emit("Digital signature fields aren't supported. Use the Signature tool to place your signature.")
        return True

    def _link_at(self, i: int, vis: fitz.Point):
        for link in self.links(i):
            if fitz.Rect(link["from"]).contains(vis):
                return link
        return None

    def _follow_link(self, link: dict) -> None:
        kind = link.get("kind")
        if kind in (fitz.LINK_GOTO, fitz.LINK_NAMED) and link.get("page", -1) is not None and link.get("page", -1) >= 0:
            to = link.get("to")
            self.go_to_page(link["page"], fitz.Point(to) if isinstance(to, fitz.Point) and to.y > 0 else None)
        elif kind == fitz.LINK_URI:
            url, info = safety.check_web_link(link.get("uri", ""))
            if url is None:
                ui.information(self, "Link blocked", info)
                return
            # Check the address exactly as Qt will hand it to the browser, and show that host.
            qurl = QUrl(url, QUrl.ParsingMode.StrictMode)
            scheme = qurl.scheme().lower()
            if not qurl.isValid() or scheme not in ("http", "https", "mailto") or qurl.userInfo():
                ui.information(self, "Link blocked", "The link isn't a valid web or e-mail address, so it was blocked.")
                return
            if scheme == "mailto":
                prompt = f"Write an e-mail to this address?\n\n{info}"
            else:
                real_host = qurl.host(QUrl.ComponentFormattingOption.FullyEncoded)
                shown_host = qurl.host()
                if not real_host:
                    ui.information(self, "Link blocked", "The link has no web address, so it was blocked.")
                    return
                warning = ""
                if real_host != shown_host or "xn--" in real_host:
                    warning = ("\n\nCareful: this address uses letters that can look like other letters. "
                               f"It is really:\n{real_host}")
                prompt = (f"Open this web page in your browser?\n\nSite: {real_host}{warning}\n\n"
                          f"Full address:\n{safety.clean_text(qurl.toString(QUrl.ComponentFormattingOption.FullyEncoded))}")
            if ui.question(self, "Open link", prompt, default=ui.No) != ui.Yes:
                return
            QDesktopServices.openUrl(qurl)
        elif kind == fitz.LINK_GOTOR:
            # Links to other files (PyMuPDF also reports "launch" actions this way). Only existing
            # local PDFs are allowed, never network paths, and only after the user says yes.
            path, reason = safety.resolve_pdf_link(link.get("file", ""), self.pdf.path)
            if path is None:
                ui.information(self, "Link blocked", reason)
                return
            if ui.question(self, "Open linked file",
                           f"This link opens another PDF:\n\n{safety.clean_text(path)}\n\nOpen it?",
                           default=ui.No) == ui.Yes:
                self.open_file_requested.emit(path)
        else:
            self.message.emit("This kind of link is blocked.")

    # ================================================================== mouse
    def _handle_hit(self, pos: QPointF) -> int | None:
        sa = self.sel_annot
        if not sa or not sa.get("resizable"):
            return None
        cr = self.vis_rect_to_canvas(sa["pno"], sa["vis"]).adjusted(-2, -2, 2, 2)
        for k, hp in enumerate(self._handle_points(cr)):
            if abs(hp.x() - pos.x()) <= HANDLE and abs(hp.y() - pos.y()) <= HANDLE:
                return k
        return None

    def mousePressEvent(self, ev) -> None:
        if jobs.busy():  # never touch the document while a background job is running
            return
        self.setFocus()
        pos = ev.position()
        if ev.button() == Qt.MouseButton.MiddleButton or (
                ev.button() == Qt.MouseButton.LeftButton and self.opt.tool == T.HAND):
            self.drag = {"kind": "pan", "start": ev.globalPosition(), "h": self.view.horizontalScrollBar().value(),
                         "v": self.view.verticalScrollBar().value()}
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        if self.editor is not None:
            self.commit_editor()
            return
        i = self.page_at(pos)
        tool = self.opt.tool
        if i is None:
            self.clear_selection()
            self.select_annot(None)
            return
        vis = self.to_vis(i, pos)
        pt = vis * self._info(i)[2]
        shift = bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)

        if tool == T.SELECT:
            handle = self._handle_hit(pos)
            if handle is not None and self.sel_annot:
                self.drag = {"kind": "resize", "pno": self.sel_annot["pno"], "xref": self.sel_annot["xref"],
                             "handle": handle, "orig": fitz.Rect(self.sel_annot["vis"]), "start": vis,
                             "preview": None}
                return
            if self._click_widget(i, pt):
                return
            page = self.pdf.page(i)
            annot = annots.annot_at(page, pt)
            if annot is not None:
                xref = annot.xref
                self.clear_selection()
                self.select_annot(i, xref)
                if self.sel_annot and self.sel_annot["movable"]:
                    self.drag = {"kind": "move", "pno": i, "xref": xref, "orig": fitz.Rect(self.sel_annot["vis"]),
                                 "start": vis, "preview": None}
                return
            self.select_annot(None)
            link = self._link_at(i, vis)
            if link is not None:
                self.drag = {"kind": "link", "pno": i, "link": link, "start": vis}
                return
            self.clear_selection()
            self.drag = {"kind": "text", "pno": i, "anchor": pt}
            return

        self.select_annot(None)
        if tool in T.TEXT_TOOLS:
            self.clear_selection()
            if self._word_under(i, pt):
                self.drag = {"kind": "text", "pno": i, "anchor": pt}
            else:
                self.drag = {"kind": "area", "pno": i, "start": vis, "cur": vis}
        elif tool in (T.RECT, T.ELLIPSE, T.LINE, T.ARROW, T.TEXTBOX, T.STAMP, T.IMAGE, T.SIGNATURE):
            self.drag = {"kind": "shape", "pno": i, "tool": tool, "start": vis, "cur": vis, "shift": shift}
        elif tool == T.PEN:
            self.drag = {"kind": "ink", "pno": i, "points": [vis]}
        elif tool == T.NOTE:
            self._add_note(i, vis)
        elif tool == T.EDIT_TEXT:
            line = self._line_at(i, pt)
            if line:
                self._edit_line(line)
            else:
                self.message.emit("Click directly on a line of text to edit it.")

    def mouseMoveEvent(self, ev) -> None:
        if jobs.busy():  # never touch the document while a background job is running
            return
        pos = ev.position()
        d = self.drag
        if d is None:
            self._update_hover(pos)
            return
        kind = d["kind"]
        if kind == "pan":
            delta = ev.globalPosition() - d["start"]
            self.view.horizontalScrollBar().setValue(int(d["h"] - delta.x()))
            self.view.verticalScrollBar().setValue(int(d["v"] - delta.y()))
            return
        i = d.get("pno")
        if i is None:
            return
        vis = self.clamp_vis(i, self.to_vis(i, pos))
        shift = bool(ev.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if kind == "text":
            sel = self._selection_between(i, d["anchor"], vis * self._info(i)[2])
            self.sel = sel
            self.update()
        elif kind in ("area",):
            d["cur"] = vis
            self.update()
        elif kind == "shape":
            if shift:
                vis = self._constrain(d["tool"], d["start"], vis)
            d["cur"] = vis
            self.update()
        elif kind == "ink":
            last = d["points"][-1]
            if abs(last.x - vis.x) * self.scale + abs(last.y - vis.y) * self.scale >= 1.5:
                d["points"].append(vis)
                self.update()
        elif kind == "move":
            dx, dy = vis.x - d["start"].x, vis.y - d["start"].y
            d["preview"] = d["orig"] + (dx, dy, dx, dy)
            self.update()
        elif kind == "resize":
            d["preview"] = self._resized(d["orig"], d["handle"], vis)
            self.update()
        elif kind == "link":
            if abs(vis.x - d["start"].x) + abs(vis.y - d["start"].y) > 4:
                self.drag = {"kind": "text", "pno": i, "anchor": d["start"] * self._info(i)[2]}

    @staticmethod
    def _constrain(tool: str, a: fitz.Point, b: fitz.Point) -> fitz.Point:
        dx, dy = b.x - a.x, b.y - a.y
        if tool in (T.RECT, T.ELLIPSE):
            side = max(abs(dx), abs(dy))
            return fitz.Point(a.x + math.copysign(side, dx or 1), a.y + math.copysign(side, dy or 1))
        if tool in (T.LINE, T.ARROW):
            ang = round(math.atan2(dy, dx) / (math.pi / 4)) * (math.pi / 4)
            length = math.hypot(dx, dy)
            return fitz.Point(a.x + length * math.cos(ang), a.y + length * math.sin(ang))
        return b

    @staticmethod
    def _resized(orig: fitz.Rect, handle: int, p: fitz.Point) -> fitz.Rect:
        x0, y0, x1, y1 = orig.x0, orig.y0, orig.x1, orig.y1
        if handle in (0, 6, 7):
            x0 = min(p.x, x1 - 4)
        if handle in (2, 3, 4):
            x1 = max(p.x, x0 + 4)
        if handle in (0, 1, 2):
            y0 = min(p.y, y1 - 4)
        if handle in (4, 5, 6):
            y1 = max(p.y, y0 + 4)
        return fitz.Rect(x0, y0, x1, y1)

    def mouseReleaseEvent(self, ev) -> None:
        if jobs.busy():  # never touch the document while a background job is running
            return
        d = self.drag
        self.drag = None
        if d is None:
            return
        kind = d["kind"]
        if kind == "pan":
            self._update_hover(ev.position())
            return
        if kind == "text":
            sel = self.sel if self.sel and self.sel.pno == d["pno"] else None
            tool = self.opt.tool
            if tool in (T.HIGHLIGHT, T.UNDERLINE, T.STRIKEOUT) and sel:
                self.sel = None
                self._make_markup(tool, sel)
            elif tool == T.REDACT and sel:
                self.sel = None
                self._make_redaction_from_selection(sel)
            self.selection_changed.emit(bool(self.sel))
            self.update()
        elif kind == "area":
            self._finish_area(d)
        elif kind == "shape":
            self._finish_shape(d)
        elif kind == "ink":
            self._finish_ink(d)
        elif kind == "link":
            self._follow_link(d["link"])
        elif kind in ("move", "resize"):
            prev = d.get("preview")
            if prev is not None and (abs(prev.x0 - d["orig"].x0) > 0.5 or abs(prev.y0 - d["orig"].y0) > 0.5
                                     or abs(prev.x1 - d["orig"].x1) > 0.5 or abs(prev.y1 - d["orig"].y1) > 0.5):
                self._apply_transform(d["pno"], d["xref"], prev, "Move" if kind == "move" else "Resize")
        self.update()

    def mouseDoubleClickEvent(self, ev) -> None:
        if jobs.busy():  # never touch the document while a background job is running
            return
        pos = ev.position()
        i = self.page_at(pos)
        if i is None or self.opt.tool != T.SELECT:
            return
        pt = self.to_page(i, pos)
        page = self.pdf.page(i)
        annot = annots.annot_at(page, pt)
        if annot is not None:
            t, xref = annot.type[0], annot.xref
            self.drag = None
            if t == fitz.PDF_ANNOT_FREE_TEXT:
                self._edit_freetext(i, xref)
            elif t in (fitz.PDF_ANNOT_TEXT,) or t in annots.MARKUP_TYPES or t in (
                    fitz.PDF_ANNOT_SQUARE, fitz.PDF_ANNOT_CIRCLE, fitz.PDF_ANNOT_INK, fitz.PDF_ANNOT_LINE):
                self._edit_note(i, xref)
            return
        words = self.words(i)
        k = self._word_index(words, pt)
        if k is not None:
            w = words[k]
            if fitz.Rect(w[:4]).contains(pt):
                self.sel = TextSelection(i, [w])
                self.selection_changed.emit(True)
                self.drag = None
                self.update()

    def _update_hover(self, pos: QPointF) -> None:
        i = self.page_at(pos)
        tool = self.opt.tool
        cursor = Qt.CursorShape.ArrowCursor
        if tool == T.HAND:
            cursor = Qt.CursorShape.OpenHandCursor
        elif i is not None:
            vis = self.to_vis(i, pos)
            pt = vis * self._info(i)[2]
            if tool == T.SELECT:
                if self._handle_hit(pos) is not None:
                    cursor = Qt.CursorShape.SizeAllCursor
                elif any(r.contains(pt) for r, ft, _x in self.widgets(i)):
                    cursor = Qt.CursorShape.PointingHandCursor
                elif any((r + (-3, -3, 3, 3)).contains(pt) for r, t, _x in self.annot_boxes(i)
                         if t not in annots.MARKUP_TYPES):
                    cursor = Qt.CursorShape.SizeAllCursor
                elif self._link_at(i, vis) is not None:
                    cursor = Qt.CursorShape.PointingHandCursor
                elif self._word_under(i, pt):
                    cursor = Qt.CursorShape.IBeamCursor
            elif tool in T.TEXT_TOOLS:
                cursor = Qt.CursorShape.IBeamCursor if self._word_under(i, pt) else Qt.CursorShape.CrossCursor
            elif tool == T.EDIT_TEXT:
                line = self._line_at(i, pt)
                if (line and self.hover_line and line["bbox"] == self.hover_line["bbox"]
                        and line["pno"] == self.hover_line["pno"]):
                    pass
                else:
                    self.hover_line = line
                    self.update()
                cursor = Qt.CursorShape.IBeamCursor if line else Qt.CursorShape.ArrowCursor
            else:
                cursor = Qt.CursorShape.CrossCursor
        self.setCursor(cursor)

    def wheelEvent(self, ev) -> None:
        if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
            steps = ev.angleDelta().y() / 120.0
            if steps:
                anchor = ev.position() - QPointF(self.view.horizontalScrollBar().value(),
                                                 self.view.verticalScrollBar().value())
                self.zoom_by(1.1 ** steps, anchor)
            ev.accept()
            return
        ev.ignore()

    def event(self, ev):
        if ev.type() == QEvent.Type.Gesture:
            pinch = ev.gesture(Qt.GestureType.PinchGesture)
            if pinch is not None:
                factor = pinch.scaleFactor()
                if factor and abs(factor - 1.0) > 0.001:
                    self.zoom_by(factor)
                return True
        if ev.type() == QEvent.Type.NativeGesture and ev.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
            self.zoom_by(1.0 + ev.value())
            return True
        return super().event(ev)

    def keyPressEvent(self, ev) -> None:
        if jobs.busy():
            return
        key = ev.key()
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace) and self.sel_annot:
            self.delete_selected_annot()
            return
        if key == Qt.Key.Key_Escape:
            if self.drag:
                self.drag = None
                self.update()
            elif self.sel or self.sel_annot:
                self.clear_selection()
                self.select_annot(None)
            elif self.opt.tool != T.SELECT:
                self.tool_reset_requested.emit()
            return
        if self.sel_annot and self.sel_annot.get("movable") and key in (
                Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down):
            step = 10 if ev.modifiers() & Qt.KeyboardModifier.ShiftModifier else 1
            dx = {Qt.Key.Key_Left: -step, Qt.Key.Key_Right: step}.get(key, 0)
            dy = {Qt.Key.Key_Up: -step, Qt.Key.Key_Down: step}.get(key, 0)
            sa = self.sel_annot
            self._apply_transform(sa["pno"], sa["xref"], sa["vis"] + (dx, dy, dx, dy), "Move")
            return
        super().keyPressEvent(ev)

    def leaveEvent(self, ev) -> None:
        if self.hover_line:
            self.hover_line = None
            self.update()
        super().leaveEvent(ev)

    def _on_tool_changed(self, tool: str) -> None:
        self.commit_editor()
        self.drag = None
        self.hover_line = None
        if tool != T.SELECT:
            self.select_annot(None)
        self.setCursor(Qt.CursorShape.OpenHandCursor if tool == T.HAND else Qt.CursorShape.ArrowCursor)
        self.update()

    # ================================================================== context menu
    def contextMenuEvent(self, ev) -> None:
        if jobs.busy():  # never touch the document while a background job is running
            return
        pos = QPointF(ev.pos())
        i = self.page_at(pos)
        menu = QMenu(self)
        if i is not None:
            pt = self.to_page(i, pos)
            page = self.pdf.page(i)
            annot = annots.annot_at(page, pt)
            if annot is not None:
                self.select_annot(i, annot.xref)
        if self.sel and self.sel.text:
            menu.addAction("Copy", self.copy_selection)
            menu.addSeparator()
            menu.addAction("Highlight", lambda: self.markup_selection("highlight"))
            menu.addAction("Underline", lambda: self.markup_selection("underline"))
            menu.addAction("Strikethrough", lambda: self.markup_selection("strikeout"))
            menu.addAction("Mark for redaction", lambda: self.markup_selection("redact"))
            menu.addSeparator()
        if self.sel_annot:
            sa = self.sel_annot
            if sa["type"] == fitz.PDF_ANNOT_FREE_TEXT:
                menu.addAction("Edit text", self.edit_annot_content)
            else:
                menu.addAction("Edit comment...", self.edit_annot_content)
            menu.addAction(f"Delete {sa['name'].lower()}", self.delete_selected_annot)
            menu.addSeparator()
        if i is not None:
            vis = self.to_vis(i, pos)
            menu.addAction("Add sticky note here", lambda: self._add_note(i, vis))
            menu.addAction("Select all text on this page", lambda: (self._set_current(i), self.select_all_on_page()))
            menu.addSeparator()
            menu.addAction("Rotate page clockwise", lambda: self.page_action_requested.emit("rotate_cw", i))
            menu.addAction("Rotate page counterclockwise", lambda: self.page_action_requested.emit("rotate_ccw", i))
            menu.addAction("Insert blank page after", lambda: self.page_action_requested.emit("insert_blank", i))
            menu.addAction("Extract this page...", lambda: self.page_action_requested.emit("extract", i))
            menu.addAction("Copy page as image", lambda: self.copy_page_image(i))
            menu.addAction("Delete page", lambda: self.page_action_requested.emit("delete", i))
        if not menu.isEmpty():
            menu.exec(ev.globalPos())

    def copy_page_image(self, i: int) -> None:
        page = self.pdf.page(i)
        z = fonts.safe_zoom(page.rect, 150 / 72)
        pix = page.get_pixmap(matrix=fitz.Matrix(z, z), alpha=False)
        img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
        QGuiApplication.clipboard().setImage(img)
        self.message.emit(f"Copied page {i + 1} as an image")


class DocumentView(QScrollArea):
    """Scroll area that hosts the page canvas."""

    def __init__(self, pdf: PdfDocument, options: T.ToolOptions, settings, parent=None):
        super().__init__(parent)
        self.setObjectName("viewer")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWidgetResizable(False)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.canvas = PageCanvas(self, pdf, options, settings)
        self.setWidget(self.canvas)
        self.verticalScrollBar().valueChanged.connect(lambda _v: self.canvas.update_current_page())
        self.verticalScrollBar().setSingleStep(40)
        self.horizontalScrollBar().setSingleStep(40)
        self._first_layout = True

    def resizeEvent(self, ev) -> None:
        super().resizeEvent(ev)
        if self.canvas.zoom_mode or self._first_layout:
            cur = self.canvas.current_page
            frac = 0.0
            if self.canvas.page_rects and not self._first_layout:
                r = self.canvas.page_rects[cur]
                frac = (self.verticalScrollBar().value() - r.top()) / max(1.0, r.height())
            self.canvas.relayout()
            if self.canvas.page_rects and not self._first_layout:
                r = self.canvas.page_rects[cur]
                self.verticalScrollBar().setValue(int(r.top() + frac * r.height()))
            self._first_layout = False
        else:
            self.canvas.relayout()

    def keyPressEvent(self, ev) -> None:
        canvas = self.canvas
        if ev.key() in (Qt.Key.Key_PageDown, Qt.Key.Key_Space) and canvas.zoom_mode == "fit_page":
            canvas.go_to_page(min(canvas.pdf.page_count - 1, canvas.current_page + 1))
            return
        if ev.key() == Qt.Key.Key_PageUp and canvas.zoom_mode == "fit_page":
            canvas.go_to_page(max(0, canvas.current_page - 1))
            return
        if ev.key() == Qt.Key.Key_Home and not ev.modifiers():
            canvas.go_to_page(0)
            return
        if ev.key() == Qt.Key.Key_End and not ev.modifiers():
            canvas.go_to_page(canvas.pdf.page_count - 1)
            return
        super().keyPressEvent(ev)
