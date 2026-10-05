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
    REDACT: ("Redact", "eye-off", "Drag across text or draw a box to mark it for redaction"),
    EDIT_TEXT: ("Edit text", "pencil-line", "Click a line of text to change it"),
}

TEXT_TOOLS = {HIGHLIGHT, UNDERLINE, STRIKEOUT, REDACT}
COLOR_TOOLS = {HIGHLIGHT, UNDERLINE, STRIKEOUT, NOTE, TEXTBOX, PEN, RECT, ELLIPSE, LINE, ARROW}
WIDTH_TOOLS = {PEN, RECT, ELLIPSE, LINE, ARROW}
OPACITY_TOOLS = {HIGHLIGHT, UNDERLINE, STRIKEOUT, PEN, RECT, ELLIPSE, LINE, ARROW}
FONT_TOOLS = {TEXTBOX}
FILL_TOOLS = {RECT, ELLIPSE, TEXTBOX}


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
