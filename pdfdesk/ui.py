"""Message boxes and tooltips that always show text as plain text.

Qt guesses whether text is HTML. A PDF's metadata, bookmark titles or a file name could contain HTML
that makes Qt load an image from a local file or (on Windows) a network share. These helpers make
sure outside text is never treated as HTML."""
from __future__ import annotations

import html

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QMessageBox

Yes = QMessageBox.StandardButton.Yes
No = QMessageBox.StandardButton.No


def _box(icon, parent, title: str, text: str, buttons=QMessageBox.StandardButton.Ok, default=None) -> QMessageBox:
    box = QMessageBox(icon, title, text, buttons, parent)
    box.setTextFormat(Qt.TextFormat.PlainText)
    if default is not None:
        box.setDefaultButton(default)
    return box


def information(parent, title: str, text: str) -> None:
    _box(QMessageBox.Icon.Information, parent, title, text).exec()


def warning(parent, title: str, text: str) -> None:
    _box(QMessageBox.Icon.Warning, parent, title, text).exec()


def question(parent, title: str, text: str, default=Yes) -> QMessageBox.StandardButton:
    box = _box(QMessageBox.Icon.Question, parent, title, text, Yes | No, default)
    return QMessageBox.StandardButton(box.exec())


def rich_information(parent, title: str, html_text: str) -> None:
    """For PDF Desk's own help text only. Escape anything dynamic with html.escape first."""
    box = QMessageBox(QMessageBox.Icon.Information, title, html_text, QMessageBox.StandardButton.Ok, parent)
    box.setTextFormat(Qt.TextFormat.RichText)
    box.exec()


def tip(text: str) -> str:
    """A tooltip that shows outside text literally (wrapped as escaped rich text)."""
    return "<qt>" + html.escape(str(text)).replace("\n", "<br>") + "</qt>"


def plain_label(text: str = "", object_name: str | None = None) -> QLabel:
    lab = QLabel(text)
    lab.setTextFormat(Qt.TextFormat.PlainText)
    if object_name:
        lab.setObjectName(object_name)
    return lab
