"""Editing tools and their shared options (color, line width, opacity, font size...)."""
from __future__ import annotations

from PySide6.QtCore import QObject, Signal

from pdfdesk.config import Settings
from pdfdesk.fonts import hex_to_rgb

SELECT = "select"
HAND = "hand"
HIGHLIGHT = "highlight"
UNDERLINE = "underline"
STRIKEOUT = "strikeout"
NOTE = "note"
TEXTBOX = "textbox"
PEN = "pen"
RECT = "rect"
ELLIPSE = "ellipse"
LINE = "line"
ARROW = "arrow"
STAMP = "stamp"
IMAGE = "image"
SIGNATURE = "signature"
REDACT = "redact"
EDIT_TEXT = "edit_text"
FILLSIGN = "fillsign"
LINK = "link"
MEASURE = "measure"
DIGISIGN = "digisign"
SNAPSHOT = "snapshot"
FIELD_TEXT = "field_text"
FIELD_CHECK = "field_check"
FIELD_RADIO = "field_radio"
FIELD_COMBO = "field_combo"
FIELD_LIST = "field_list"
FIELD_SIGNATURE = "field_signature"

# tool: (label, icon, help text)
TOOLS = {
    SELECT: ("Select", "mouse-pointer-2", "Select text, annotations, links and form fields"),
    HAND: ("Hand", "hand", "Drag to scroll the page"),
    HIGHLIGHT: ("Highlight", "highlighter", "Drag across text to highlight it (or draw a box on images)"),
    UNDERLINE: ("Underline", "underline", "Drag across text to underline it"),
    STRIKEOUT: ("Strikethrough", "strikethrough", "Drag across text to strike it out"),
    NOTE: ("Sticky note", "sticky-note", "Click to add a comment note"),
    TEXTBOX: ("Add text", "type", "Click or drag a box to type text on the page"),
    PEN: ("Draw", "pen-line", "Draw freehand"),
    RECT: ("Rectangle", "square", "Drag to draw a rectangle (hold Shift for a square)"),
    ELLIPSE: ("Ellipse", "circle", "Drag to draw an ellipse (hold Shift for a circle)"),
    LINE: ("Line", "minus", "Drag to draw a line (hold Shift to snap to 45 degrees)"),
    ARROW: ("Arrow", "move-up-right", "Drag to draw an arrow (hold Shift to snap to 45 degrees)"),
    STAMP: ("Stamp", "stamp", "Click or drag to place a stamp"),
    IMAGE: ("Image", "image", "Click or drag a box, then pick an image to place"),
    SIGNATURE: ("Signature", "signature", "Click or drag to place your signature"),
    FILLSIGN: ("Fill & Sign", "file-pen-line", "Click to fill in a form that has no fields: text, check marks, dates..."),
    FIELD_TEXT: ("Text field", "text-cursor-input", "Drag a box (or click) to add a text field"),
    FIELD_CHECK: ("Check box", "square-check", "Click to add a check box"),
    FIELD_RADIO: ("Radio button", "circle-dot", "Click to add a radio button (buttons in a group allow one choice)"),
    FIELD_COMBO: ("Drop-down", "square-chevron-down", "Drag a box (or click) to add a drop-down list"),
    FIELD_LIST: ("List box", "list", "Drag a box (or click) to add a list box"),
    FIELD_SIGNATURE: ("Signature field", "signature", "Drag a box (or click) to add a place for a digital signature"),
    REDACT: ("Redact", "eye-off", "Drag across text or draw a box to mark it for redaction"),
    LINK: ("Link", "link", "Drag a box to make a link; click a link to change or delete it"),
    MEASURE: ("Measure", "ruler", "Drag to measure a distance; for areas click each corner and double-click to finish"),
    SNAPSHOT: ("Snapshot", "camera", "Drag a box to copy that part of the page as a picture"),
    DIGISIGN: ("Digital signature", "badge-check", "Drag a box where your digital signature should appear"),
    EDIT_TEXT: ("Edit text", "pencil-line", "Click a paragraph to change its words, font, size or color"),
}

TEXT_TOOLS = {HIGHLIGHT, UNDERLINE, STRIKEOUT, REDACT}
COLOR_TOOLS = {HIGHLIGHT, UNDERLINE, STRIKEOUT, NOTE, PEN, RECT, ELLIPSE, LINE, ARROW, FILLSIGN, MEASURE}
WIDTH_TOOLS = {PEN, RECT, ELLIPSE, LINE, ARROW}
OPACITY_TOOLS = {HIGHLIGHT, UNDERLINE, STRIKEOUT, PEN, RECT, ELLIPSE, LINE, ARROW}
FONT_TOOLS = {TEXTBOX, EDIT_TEXT}
FILL_TOOLS = {RECT, ELLIPSE, TEXTBOX}

FIELD_TOOLS = {FIELD_TEXT: "text", FIELD_CHECK: "check", FIELD_RADIO: "radio", FIELD_COMBO: "combo",
               FIELD_LIST: "list", FIELD_SIGNATURE: "signature"}

FILLSIGN_MODES = [("text", "Text", "type"), ("check", "Check mark", "check"), ("cross", "Cross", "x"),
                  ("dot", "Dot", "circle"), ("date", "Today's date", "calendar"), ("initials", "Initials", "case-sensitive")]

TEXT_STYLE_DEFAULTS = {"font": "", "bold": False, "italic": False, "underline": False, "strike": False,
                       "align": "left", "highlight": None, "spacing": 1.0}


class ToolOptions(QObject):
    changed = Signal()
    tool_changed = Signal(str)

    def __init__(self, settings: Settings):
        super().__init__()
        self.settings = settings
        self.tool = SELECT

    def set_tool(self, tool: str) -> None:
        if tool != self.tool:
            self.tool = tool
            self.tool_changed.emit(tool)
            self.changed.emit()

    # colors are remembered per tool
    def color_hex(self, tool: str | None = None) -> str:
        return self.settings.tool_color(tool or self.tool)

    def color(self, tool: str | None = None):
        return hex_to_rgb(self.color_hex(tool))

    def set_color(self, hex_color: str, tool: str | None = None) -> None:
        self.settings.set_tool_color(tool or self.tool, hex_color)
        self.changed.emit()

    @property
    def width(self) -> float:
        return float(self.settings.get("stroke_width") or 2)

    def set_width(self, value: float) -> None:
        self.settings.set("stroke_width", float(value))
        self.changed.emit()

    @property
    def opacity(self) -> float:
        return float(self.settings.get("opacity") or 1.0)

    def set_opacity(self, value: float) -> None:
        self.settings.set("opacity", max(0.1, min(1.0, float(value))))
        self.changed.emit()

    def opacity_for(self, tool: str) -> float:
        # Highlights look best slightly see-through, the PDF viewer blends them with "Multiply".
        return self.opacity

    @property
    def font_size(self) -> float:
        return float(self.settings.get("font_size") or 12)

    def set_font_size(self, value: float) -> None:
        self.settings.set("font_size", float(value))
        self.changed.emit()

    @property
    def fill(self) -> bool:
        return bool(self.settings.get("fill_shapes"))

    def set_fill(self, value: bool) -> None:
        self.settings.set("fill_shapes", bool(value))
        self.changed.emit()

    @property
    def fill_hex(self) -> str:
        return self.settings.get("fill_color") or "#ffffff"

    def fill_rgb(self):
        return hex_to_rgb(self.fill_hex) if self.fill else None

    def set_fill_color(self, hex_color: str) -> None:
        self.settings.set("fill_color", hex_color)
        self.changed.emit()

    @property
    def stamp(self) -> str:
        return self.settings.get("stamp") or "Approved"

    def set_stamp(self, name: str) -> None:
        self.settings.set("stamp", name)
        self.changed.emit()

    @property
    def signature(self) -> str:
        return self.settings.get("signature") or ""

    def set_signature(self, path: str) -> None:
        self.settings.set("signature", path)
        self.changed.emit()

    @property
    def author(self) -> str:
        return self.settings.get("author") or ""

    # ---- text formatting for new text boxes (like the defaults in Word)
    def text_style(self) -> dict:
        stored = self.settings.get("text_style")
        style = dict(TEXT_STYLE_DEFAULTS)
        if isinstance(stored, dict):
            for key in TEXT_STYLE_DEFAULTS:
                if key in stored:
                    style[key] = stored[key]
        from pdfdesk import fontcatalog
        style["font"] = fontcatalog.catalog().resolve(style.get("font") or None)
        style["size"] = self.font_size
        style["color"] = self.color_hex(TEXTBOX)
        return style

    def set_text_style(self, change: dict) -> None:
        stored = self.settings.get("text_style")
        stored = dict(stored) if isinstance(stored, dict) else {}
        for key, value in change.items():
            if key == "size":
                self.settings.set("font_size", float(value))
            elif key == "grow":
                from pdfdesk.richtext import SIZES
                size = self.font_size
                new = (next((s for s in SIZES if s > size + 0.01), size) if value > 0
                       else next((s for s in reversed(SIZES) if s < size - 0.01), size))
                self.settings.set("font_size", float(new))
            elif key == "color":
                self.settings.set_tool_color(TEXTBOX, value)
            elif key == "clear":
                stored = {"font": stored.get("font", "")}
            elif key in TEXT_STYLE_DEFAULTS:
                stored[key] = value
        self.settings.set("text_style", stored)
        self.changed.emit()

    @property
    def measure_mode(self) -> str:
        mode = self.settings.get("measure_mode") or "distance"
        return mode if mode in ("distance", "perimeter", "area") else "distance"

    def set_measure_mode(self, mode: str) -> None:
        self.settings.set("measure_mode", mode)
        self.changed.emit()

    def measure_scale(self):
        from pdfdesk.measure import Scale
        return Scale.from_settings(self.settings.get("measure_scale"))

    def set_measure_scale(self, scale) -> None:
        self.settings.set("measure_scale", scale.to_dict())
        self.changed.emit()

    @property
    def fillsign_mode(self) -> str:
        mode = self.settings.get("fillsign_mode") or "text"
        return mode if mode in {m for m, _l, _i in FILLSIGN_MODES} else "text"

    def set_fillsign_mode(self, mode: str) -> None:
        self.settings.set("fillsign_mode", mode)
        self.changed.emit()

    @property
    def initials(self) -> str:
        value = self.settings.get("initials")
        if isinstance(value, str) and value.strip():
            return value.strip()[:12]
        parts = [p for p in self.author.replace(".", " ").split() if p]
        return "".join(p[0].upper() for p in parts[:3]) or "AB"

    def recent_fonts(self) -> list[str]:
        value = self.settings.get("recent_fonts")
        return [v for v in value if isinstance(v, str)][:6] if isinstance(value, list) else []

    def add_recent_font(self, name: str) -> None:
        fonts = [name] + [f for f in self.recent_fonts() if f != name]
        self.settings.set("recent_fonts", fonts[:6])
