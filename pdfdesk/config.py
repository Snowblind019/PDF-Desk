"""Paths and user settings, stored locally as JSON."""
from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path

from pdfdesk import APP_ID, APP_NAME

IS_WINDOWS = sys.platform.startswith("win")


def _env_path(name: str, fallback: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else fallback


def config_dir() -> Path:
    if os.environ.get("PDFDESK_HOME"):
        path = Path(os.environ["PDFDESK_HOME"]) / "config"
    elif IS_WINDOWS:
        path = _env_path("APPDATA", Path.home() / "AppData" / "Roaming") / APP_NAME
    else:
        path = _env_path("XDG_CONFIG_HOME", Path.home() / ".config") / APP_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    if os.environ.get("PDFDESK_HOME"):
        path = Path(os.environ["PDFDESK_HOME"]) / "cache"
    elif IS_WINDOWS:
        path = _env_path("LOCALAPPDATA", Path.home() / "AppData" / "Local") / APP_NAME / "cache"
    else:
        path = _env_path("XDG_CACHE_HOME", Path.home() / ".cache") / APP_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def signatures_dir() -> Path:
    path = config_dir() / "signatures"
    path.mkdir(parents=True, exist_ok=True)
    return path


def user_tessdata_dir() -> Path:
    """A folder where .traineddata OCR language files can be dropped by hand."""
    path = config_dir() / "tessdata"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _default_author() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "User"


DEFAULTS = {
    "theme": "system",             # system | light | dark
    "night_mode": False,           # invert page colors for reading in the dark
    "default_zoom": "fit_width",   # fit_width | fit_page | 100
    "layout": "single",            # single | facing | cover
    "restore_page": True,
    "author": _default_author(),
    "recents_limit": 40,
    "libreoffice_path": "",
    "tessdata_path": "",
    "ocr_language": "eng",
    "highlight_forms": True,
    "sidebar_visible": True,
    "last_dir": str(Path.home()),
    "window_geometry": "",
    "window_state": "",
    "tool_colors": {
        "highlight": "#ffd60a",
        "underline": "#2f80ed",
        "strikeout": "#e5383b",
        "note": "#ffd60a",
        "textbox": "#1d1d1f",
        "pen": "#e5383b",
        "rect": "#e5383b",
        "ellipse": "#e5383b",
        "line": "#e5383b",
        "arrow": "#e5383b",
        "fillsign": "#1d1d1f",
        "measure": "#d33f49",
    },
    "stroke_width": 2.0,
    "opacity": 1.0,
    "font_size": 12,
    "fill_shapes": False,
    "fill_color": "#ffffff",
    "stamp": "Approved",
    "signature": "",
    "fillsign_mode": "text",
    "initials": "",
    "date_format": "%m/%d/%Y",
    "measure_mode": "distance",
    "measure_scale": {"page_value": 1, "page_unit": "in", "real_value": 1, "real_unit": "in", "decimals": 2},
    "speech_rate": 0,
    "check_updates": True,         # look for a new release on GitHub at start-up, at most once a day
    "last_update_check": 0,
    "skipped_version": "",
}


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = path or (config_dir() / "settings.json")
        self.data: dict = json.loads(json.dumps(DEFAULTS))
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                stored = json.load(fh)
            if isinstance(stored, dict):
                for key, value in stored.items():
                    if key == "tool_colors" and isinstance(value, dict):
                        self.data["tool_colors"].update(value)
                    else:
                        self.data[key] = value
        except FileNotFoundError:
            pass
        except Exception:
            # A broken settings file should never stop the app from starting.
            pass

    def save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.data, fh, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def get(self, key, default=None):
        return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key, value) -> None:
        self.data[key] = value
        self.save()

    def tool_color(self, tool: str) -> str:
        return self.data.get("tool_colors", {}).get(tool, "#e5383b")

    def set_tool_color(self, tool: str, color: str) -> None:
        self.data.setdefault("tool_colors", {})[tool] = color
        self.save()


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
