"""Measuring tools: distance, perimeter and area, with a drawing scale (for example 1 in = 10 ft).

Each measurement becomes a Line, PolyLine or Polygon annotation that shows its result, so other
PDF viewers show it too. Points are in visible (rotated) page coordinates unless noted.
"""
from __future__ import annotations

import json
import math

import pymupdf as fitz

from pdfdesk import appearance

KIND = "Measure"
KEY = "PDFDeskMeasure"
PAGE_UNITS = {"in": 72.0, "cm": 72 / 2.54, "mm": 72 / 25.4, "pt": 1.0}
REAL_UNITS = ["in", "ft", "yd", "mi", "mm", "cm", "m", "km", "pt"]
MODES = [("distance", "Distance", "ruler"), ("perimeter", "Perimeter", "ruler"), ("area", "Area", "pentagon")]


class Scale:
    """page_value page_unit on paper = real_value real_unit in the real world."""

    def __init__(self, page_value=1.0, page_unit="in", real_value=1.0, real_unit="in", decimals=2):
        self.page_value = max(1e-6, float(page_value))
        self.page_unit = page_unit if page_unit in PAGE_UNITS else "in"
        self.real_value = max(1e-9, float(real_value))
        self.real_unit = real_unit if real_unit in REAL_UNITS else "in"
        self.decimals = max(0, min(4, int(decimals)))

    @classmethod
    def from_settings(cls, value) -> "Scale":
        if isinstance(value, dict):
            try:
                return cls(value.get("page_value", 1), value.get("page_unit", "in"), value.get("real_value", 1),
                           value.get("real_unit", "in"), value.get("decimals", 2))
            except (TypeError, ValueError):
                pass
        return cls()

    def to_dict(self) -> dict:
        return {"page_value": self.page_value, "page_unit": self.page_unit, "real_value": self.real_value,
                "real_unit": self.real_unit, "decimals": self.decimals}

    def label(self) -> str:
        return f"{self.page_value:g} {self.page_unit} = {self.real_value:g} {self.real_unit}"

    def length(self, points: float) -> float:
        return points / PAGE_UNITS[self.page_unit] / self.page_value * self.real_value

    def fmt_length(self, points: float) -> str:
        return f"{self.length(points):,.{self.decimals}f} {self.real_unit}"

    def fmt_area(self, square_points: float) -> str:
        f = 1 / PAGE_UNITS[self.page_unit] / self.page_value * self.real_value
        return f"{square_points * f * f:,.{self.decimals}f} {self.real_unit}²"


def polyline_length(points: list[fitz.Point], closed: bool = False) -> float:
    pts = list(points) + ([points[0]] if closed and points else [])
    return sum(math.hypot(b.x - a.x, b.y - a.y) for a, b in zip(pts, pts[1:]))


def polygon_area(points: list[fitz.Point]) -> float:
    if len(points) < 3:
        return 0.0
    s = 0.0
    for a, b in zip(points, points[1:] + points[:1]):
        s += a.x * b.y - b.x * a.y
    return abs(s) / 2


def result_text(mode: str, points: list[fitz.Point], scale: Scale) -> str:
    if mode == "distance":
        return scale.fmt_length(polyline_length(points[:2]))
    if mode == "perimeter":
        return scale.fmt_length(polyline_length(points))
    return scale.fmt_area(polygon_area(points))


def _draw(mode: str, points: list[fitz.Point], label: str, color, box: fitz.Rect) -> fitz.Document:
    """Draw the measurement on a page the size of `box` (visible coordinates)."""
    tmp = fitz.open()
    page = tmp.new_page(width=max(1.0, box.width), height=max(1.0, box.height))
    pts = [fitz.Point(p.x - box.x0, p.y - box.y0) for p in points]
    shape = page.new_shape()
    if mode == "area" and len(pts) >= 3:
        shape.draw_polyline(pts + [pts[0]])
        shape.finish(color=color, fill=color, fill_opacity=0.15, width=1.2, closePath=True)
    else:
        shape.draw_polyline(pts)
        shape.finish(color=color, width=1.2, closePath=False)
    # small ticks at the ends of a distance, dots at the corners of the others
    if mode == "distance" and len(pts) >= 2:
        a, b = pts[0], pts[1]
        ang = math.atan2(b.y - a.y, b.x - a.x) + math.pi / 2
        dx, dy = 5 * math.cos(ang), 5 * math.sin(ang)
        for p in (a, b):
            shape.draw_line(fitz.Point(p.x - dx, p.y - dy), fitz.Point(p.x + dx, p.y + dy))
        shape.finish(color=color, width=1.2)
    else:
        for p in pts:
            shape.draw_circle(p, 1.6)
        shape.finish(color=None, fill=color)
    shape.commit()
    # the result, on a white label in the middle
    if mode == "distance" and len(pts) >= 2:
        center = fitz.Point((pts[0].x + pts[1].x) / 2, (pts[0].y + pts[1].y) / 2)
    else:
        center = fitz.Point(sum(p.x for p in pts) / len(pts), sum(p.y for p in pts) / len(pts))
    size = 9
    width = fitz.get_text_length(label, "helv", size) + 6
    lab = fitz.Rect(center.x - width / 2, center.y - size * 0.8 - 2, center.x + width / 2, center.y + size * 0.45 + 2)
    lab = _keep_inside(lab, fitz.Rect(0, 0, box.width, box.height))
    page.draw_rect(lab, color=color, fill=(1, 1, 1), width=0.6, fill_opacity=0.92)
    page.insert_text(fitz.Point(lab.x0 + 3, lab.y1 - 3), label, fontname="helv", fontsize=size, color=color)
    return tmp


def _keep_inside(r: fitz.Rect, outer: fitz.Rect) -> fitz.Rect:
    dx = max(0, outer.x0 - r.x0) - max(0, r.x1 - outer.x1)
    dy = max(0, outer.y0 - r.y0) - max(0, r.y1 - outer.y1)
    return r + (dx, dy, dx, dy)


def add(page: fitz.Page, mode: str, vis_points: list[fitz.Point], scale: Scale, color=(0.85, 0.2, 0.2),
        author: str = "") -> int:
    """Create a measurement annotation and return its xref."""
    doc = page.parent
    vis_points = [fitz.Point(p) for p in vis_points]
    label = result_text(mode, vis_points, scale)
    derot = page.derotation_matrix
    unrot = [p * derot for p in vis_points]
    if mode == "distance":
        annot = page.add_line_annot(unrot[0], unrot[1])
        it = "LineDimension"
    elif mode == "perimeter":
        annot = page.add_polyline_annot(unrot)
        it = "PolyLineDimension"
    else:
        annot = page.add_polygon_annot(unrot)
        it = "PolygonDimension"
    r, g, b = color
    annot.set_colors(stroke=color)
    annot.set_info(title=author or "", subject="Measurement", content=label)
    annot.update()
    xref = annot.xref
    # a box around the points with room for the label
    box = fitz.Rect(vis_points[0], vis_points[0])
    for p in vis_points[1:]:
        box |= p
    pad = 6
    width = fitz.get_text_length(label, "helv", 9) + 10
    box = fitz.Rect(box.x0 - pad, box.y0 - pad, box.x1 + pad, box.y1 + pad)
    if box.width < width:
        extra = (width - box.width) / 2
        box = fitz.Rect(box.x0 - extra, box.y0, box.x1 + extra, box.y1)
    if box.height < 20:
        extra = (20 - box.height) / 2
        box = fitz.Rect(box.x0, box.y0 - extra, box.x1, box.y1 + extra)
    appearance.move_annot(page, xref, (box * derot).normalize())
    appearance.apply(doc, xref, _draw(mode, vis_points, label, color, box), page.rotation)
    doc.xref_set_key(xref, appearance.KIND_KEY, "/" + KIND)
    doc.xref_set_key(xref, "IT", "/" + it)
    doc.xref_set_key(xref, KEY, appearance.pdf_string(json.dumps({"mode": mode, "label": label,
                                                                   "scale": scale.label()}, ensure_ascii=True)))
    return xref


def describe(doc: fitz.Document, xref: int) -> str:
    t, v = appearance.get_key(doc, xref, KEY)
    if t != "string":
        return ""
    try:
        data = json.loads(v)
        return f"{data.get('label', '')} (scale {data.get('scale', '')})"[:200]
    except Exception:
        return ""
