"""Reader view: the document's text reflowed as simple paragraphs and headings, easy to read on a
small window or with large letters. It shows text only; nothing from the PDF is ever loaded as an
image, link or style."""
from __future__ import annotations

import html
import re
from collections import Counter

import pymupdf as fitz
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QTextBrowser, QVBoxLayout, QComboBox

from pdfdesk import pdfops

MAX_PAGES = 3000


def body_size(doc: fitz.Document, pages: int = 60) -> float:
    sizes: Counter = Counter()
    for pno in range(min(doc.page_count, pages)):
        for block in doc[pno].get_text("dict", flags=pdfops.WORD_FLAGS).get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    sizes[round(span.get("size", 0) * 2) / 2] += len(span.get("text", "").strip())
    return sizes.most_common(1)[0][0] if sizes else 11.0


def _span_html(span: dict) -> str:
    text = html.escape(span.get("text", ""))
    flags = span.get("flags", 0)
    name = (span.get("font") or "").lower()
    if flags & 16 or "bold" in name:
        text = f"<b>{text}</b>"
    if flags & 2 or "italic" in name or "oblique" in name:
        text = f"<i>{text}</i>"
    return text


def document_html(doc: fitz.Document, progress=None) -> str:
    body = body_size(doc)
    parts = []
    n = min(doc.page_count, MAX_PAGES)
    for pno in range(n):
        page = doc[pno]
        parts.append(f'<p class="pg"><a name="p{pno + 1}"></a>Page {pno + 1}</p>')
        for block in page.get_text("dict", flags=pdfops.WORD_FLAGS, sort=True).get("blocks", []):
            if block.get("type") != 0:
                continue
            lines = []
            biggest = 0.0
            for line in block.get("lines", []):
                spans = [s for s in line.get("spans", []) if s.get("text")]
                if not spans:
                    continue
                biggest = max(biggest, max(s.get("size", 0) for s in spans))
                lines.append("".join(_span_html(s) for s in spans))
            if not lines:
                continue
            text = ""
            for ln in lines:
                if text.endswith("-") and ln[:1].islower():
                    text = text[:-1] + ln
                else:
                    text = (text + " " + ln).strip()
            plain = re.sub(r"<[^>]+>", "", text)
            if biggest >= body * 1.6 and len(plain) < 150:
                parts.append(f"<h1>{text}</h1>")
            elif biggest >= body * 1.18 and len(plain) < 200:
                parts.append(f"<h2>{text}</h2>")
            else:
                parts.append(f"<p>{text}</p>")
        pdfops._tick(progress, pno + 1, n, "Preparing the text")
    if doc.page_count > n:
        parts.append(f"<p class='pg'>Only the first {MAX_PAGES} pages are shown.</p>")
    return "".join(parts)


THEMES = {
    "Light": ("#ffffff", "#1d1d1f", "#8a8a90"),
    "Sepia": ("#f6efe0", "#3b2f22", "#8c7a63"),
    "Dark": ("#1e1e20", "#e6e6e8", "#8a8a90"),
}


class _SafeBrowser(QTextBrowser):
    """Never loads anything (images, style sheets, links) from anywhere."""

    def loadResource(self, _type, _url):  # noqa: N802 - Qt name
        return None


class ReaderView(QDialog):
    def __init__(self, parent, title: str, body_html: str):
        super().__init__(parent)
        self.setWindowTitle(f"Reader view - {title}")
        self.setWindowFlag(Qt.WindowType.WindowMaximizeButtonHint, True)
        self.body = body_html
        self.size_px = 18
        self.theme = "Sepia"
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        smaller = QPushButton("A-")
        bigger = QPushButton("A+")
        smaller.clicked.connect(lambda: self._resize_text(-2))
        bigger.clicked.connect(lambda: self._resize_text(2))
        self.theme_box = QComboBox()
        self.theme_box.addItems(list(THEMES))
        self.theme_box.setCurrentText(self.theme)
        self.theme_box.currentTextChanged.connect(self._set_theme)
        bar.addWidget(QLabel("Text size:"))
        bar.addWidget(smaller)
        bar.addWidget(bigger)
        bar.addSpacing(16)
        bar.addWidget(QLabel("Colors:"))
        bar.addWidget(self.theme_box)
        bar.addStretch(1)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        bar.addWidget(close)
        lay.addLayout(bar)
        self.view = _SafeBrowser()
        self.view.setOpenLinks(False)
        self.view.setOpenExternalLinks(False)
        lay.addWidget(self.view, 1)
        self.resize(820, 900)
        self._render()

    def _resize_text(self, step: int) -> None:
        self.size_px = max(10, min(48, self.size_px + step))
        self._render()

    def _set_theme(self, name: str) -> None:
        self.theme = name
        self._render()

    def _render(self) -> None:
        bg, fg, muted = THEMES[self.theme]
        bar = self.view.verticalScrollBar()
        frac = bar.value() / max(1, bar.maximum())
        self.view.document().setDefaultStyleSheet(
            f"body {{ background: {bg}; color: {fg}; }} "
            f"p {{ font-size: {self.size_px}px; line-height: 150%; margin: 0 0 {self.size_px * 0.7:.0f}px 0; }} "
            f"h1 {{ font-size: {self.size_px * 1.6:.0f}px; }} h2 {{ font-size: {self.size_px * 1.25:.0f}px; }} "
            f"p.pg {{ color: {muted}; font-size: {max(10, self.size_px - 6)}px; margin-top: 18px; }}")
        self.view.setStyleSheet(f"QTextBrowser {{ background: {bg}; color: {fg}; border: none; padding: 24px 48px; }}")
        self.view.setHtml(f"<body>{self.body}</body>")
        bar.setValue(int(frac * bar.maximum()))
