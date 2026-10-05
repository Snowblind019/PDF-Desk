"""Light and dark themes built on Qt's Fusion style so the app looks the same on Linux and Windows."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

ACCENT = "#c8323c"

LIGHT = {
    "window": "#f4f4f6", "base": "#ffffff", "alt": "#f7f7f9", "text": "#1d1d1f", "muted": "#6b6b73",
    "button": "#fbfbfc", "border": "#d9d9de", "canvas": "#e4e4e8", "card": "#ffffff",
    "card_hover": "#f0f0f3", "tooltip": "#2b2b30", "highlight": ACCENT, "sidebar": "#ececf0",
}
DARK = {
    "window": "#1e1f22", "base": "#26272b", "alt": "#2b2c31", "text": "#e8e8ea", "muted": "#9a9aa3",
    "button": "#2d2e33", "border": "#3a3b41", "canvas": "#16171a", "card": "#2a2b30",
    "card_hover": "#33343a", "tooltip": "#3a3b41", "highlight": "#d8434c", "sidebar": "#232428",
}

_current = dict(LIGHT)
_dark = False


def colors() -> dict:
    return _current


def is_dark() -> bool:
    return _dark


def system_prefers_dark() -> bool:
    app = QGuiApplication.instance()
    if app is None:
        return False
    try:
        scheme = app.styleHints().colorScheme()
        if scheme == Qt.ColorScheme.Dark:
            return True
        if scheme == Qt.ColorScheme.Light:
            return False
    except Exception:
        pass
    # Fall back to how light the default window color is.
    return app.palette().color(QPalette.ColorRole.Window).lightness() < 110


def apply_theme(app: QApplication, mode: str = "system") -> None:
    global _current, _dark
    dark = system_prefers_dark() if mode == "system" else mode == "dark"
    _dark = dark
    c = dict(DARK if dark else LIGHT)
    _current = c
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(c["window"]))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(c["text"]))
    pal.setColor(QPalette.ColorRole.Base, QColor(c["base"]))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(c["alt"]))
    pal.setColor(QPalette.ColorRole.Text, QColor(c["text"]))
    pal.setColor(QPalette.ColorRole.Button, QColor(c["button"]))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(c["text"]))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(c["tooltip"]))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.Highlight, QColor(c["highlight"]))
    pal.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
    pal.setColor(QPalette.ColorRole.Link, QColor("#3b82f6" if dark else "#1f5fd1"))
    pal.setColor(QPalette.ColorRole.PlaceholderText, QColor(c["muted"]))
    pal.setColor(QPalette.ColorRole.Mid, QColor(c["border"]))
    pal.setColor(QPalette.ColorRole.Midlight, QColor(c["border"]))
    pal.setColor(QPalette.ColorRole.Dark, QColor(c["border"]))
    pal.setColor(QPalette.ColorRole.Light, QColor(c["card"]))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(c["muted"]).darker(120 if not dark else 140))
    app.setPalette(pal)
    app.setStyleSheet(stylesheet(c, dark))


def stylesheet(c: dict, dark: bool) -> str:
    checked_bg = "rgba(216, 67, 76, 0.28)" if dark else "rgba(200, 50, 60, 0.14)"
    hover_bg = "rgba(255,255,255,0.07)" if dark else "rgba(0,0,0,0.06)"
    return f"""
    QMainWindow, QDialog {{ background: {c['window']}; }}
    QToolBar {{ background: {c['window']}; border: none; border-bottom: 1px solid {c['border']};
                spacing: 2px; padding: 3px 6px; }}
    QToolBar::separator {{ background: {c['border']}; width: 1px; margin: 5px 6px; }}
    QToolButton {{ border: 1px solid transparent; border-radius: 6px; padding: 4px; }}
    QToolButton:hover {{ background: {hover_bg}; }}
    QToolButton:checked {{ background: {checked_bg}; border-color: transparent; }}
    QToolButton:pressed {{ background: {checked_bg}; }}
    QToolButton[popupMode="1"] {{ padding-right: 14px; }}
    QTabWidget::pane {{ border: none; }}
    QTabBar#docTabs::tab {{ background: transparent; padding: 7px 14px; margin-right: 2px;
                           border-top-left-radius: 7px; border-top-right-radius: 7px; color: {c['muted']}; }}
    QTabBar#docTabs::tab:selected {{ background: {c['base']}; color: {c['text']}; }}
    QTabBar#docTabs::tab:hover:!selected {{ background: {hover_bg}; }}
    QTabBar#sideTabs::tab {{ padding: 6px 4px; min-width: 30px; border: none; background: transparent;
                            border-radius: 6px; margin: 3px 1px; }}
    QTabBar#sideTabs::tab:selected {{ background: {checked_bg}; }}
    QWidget#sidebar {{ background: {c['sidebar']}; border-right: 1px solid {c['border']}; }}
    QListWidget, QTreeWidget {{ background: transparent; border: none; outline: 0; }}
    QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {c['border']}; border-radius: 4px; min-height: 30px; margin: 2px; }}
    QScrollBar::handle:vertical:hover {{ background: {c['muted']}; }}
    QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
    QScrollBar::handle:horizontal {{ background: {c['border']}; border-radius: 4px; min-width: 30px; margin: 2px; }}
    QScrollBar::handle:horizontal:hover {{ background: {c['muted']}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
    QToolButton#tabClose {{ border: none; border-radius: 4px; padding: 1px; }}
    QToolButton#tabClose:hover {{ background: {hover_bg}; }}
    QListWidget::item:selected, QTreeWidget::item:selected {{ background: {checked_bg}; color: {c['text']};
                                                              border-radius: 6px; }}
    QWidget#findBar {{ background: {c['base']}; border-bottom: 1px solid {c['border']}; }}
    QWidget#home {{ background: {c['window']}; }}
    QLabel#homeTitle {{ font-size: 22px; font-weight: 600; }}
    QLabel#sectionTitle {{ font-size: 15px; font-weight: 600; }}
    QLabel#muted {{ color: {c['muted']}; }}
    QFrame#actionTile {{ border: 1px solid {c['border']}; border-radius: 10px; background: {c['card']}; }}
    QFrame#actionTile:hover {{ background: {c['card_hover']}; border-color: {c['highlight']}; }}
    QFrame#actionTile:focus {{ border-color: {c['highlight']}; }}
    QFrame#actionTile QLabel {{ background: transparent; border: none; }}
    QLabel#tileTitle {{ font-weight: 600; }}
    QPushButton#primary {{ background: {c['highlight']}; color: white; border: none; border-radius: 8px;
                           padding: 8px 16px; font-weight: 600; }}
    QPushButton#primary:hover {{ background: {QColor(c['highlight']).lighter(112).name()}; }}
    QLineEdit, QPlainTextEdit, QTextEdit {{
        border: 1px solid {c['border']}; border-radius: 6px; padding: 3px 6px; background: {c['base']}; }}
    QLineEdit:focus, QPlainTextEdit:focus {{ border-color: {c['highlight']}; }}
    QStatusBar {{ border-top: 1px solid {c['border']}; }}
    QStatusBar QLabel {{ color: {c['muted']}; padding: 0 6px; }}
    QScrollArea#viewer {{ background: {c['canvas']}; border: none; }}
    QMenu::separator {{ height: 1px; background: {c['border']}; margin: 4px 8px; }}
    """
