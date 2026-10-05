"""The list of recently opened files shown on the Home screen (stored locally as JSON)."""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from pathlib import Path

from pdfdesk.config import cache_dir, config_dir


def _key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


class RecentFiles:
    def __init__(self, limit: int = 40, path: Path | None = None):
        self.path = path or (config_dir() / "recents.json")
        self.limit = limit
        self.items: list[dict] = []
        self.load()

    def load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            self.items = [d for d in data if isinstance(d, dict) and d.get("path")]
        except Exception:
            self.items = []

    def save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self.items, fh, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def entries(self) -> list[dict]:
        return sorted(self.items, key=lambda d: (not d.get("pinned"), -(d.get("opened_ts") or 0)))

    def get(self, path: str) -> dict | None:
        k = _key(path)
        for d in self.items:
            if _key(d["path"]) == k:
                return d
        return None

    @staticmethod
    def thumb_path(path: str) -> Path:
        folder = cache_dir() / "thumbs"
        folder.mkdir(parents=True, exist_ok=True)
        return folder / (hashlib.sha256(_key(path).encode("utf-8")).hexdigest()[:40] + ".png")

    def add(self, path: str, page: int = 0, pages: int | None = None, thumb_png: bytes | None = None) -> None:
        path = os.path.abspath(path)
        entry = self.get(path)
        now = _dt.datetime.now()
        if entry is None:
            entry = {"path": path, "pinned": False, "page": page}
            self.items.append(entry)
        entry["path"] = path
        entry["name"] = Path(path).name
        entry["opened"] = now.isoformat(timespec="seconds")
        entry["opened_ts"] = now.timestamp()
        if pages is not None:
            entry["pages"] = pages
        try:
            entry["size"] = os.path.getsize(path)
        except OSError:
            pass
        if thumb_png:
            try:
                self.thumb_path(path).write_bytes(thumb_png)
            except OSError:
                pass
        self._trim()
        self.save()

    def set_page(self, path: str, page: int) -> None:
        entry = self.get(path)
        if entry is not None:
            entry["page"] = page
            self.save()

    def update_thumb(self, path: str, thumb_png: bytes) -> None:
        if self.get(path) is not None:
            try:
                self.thumb_path(path).write_bytes(thumb_png)
            except OSError:
                pass

    def remove(self, path: str) -> None:
        k = _key(path)
        self.items = [d for d in self.items if _key(d["path"]) != k]
        try:
            self.thumb_path(path).unlink()
        except OSError:
            pass
        self.save()

    def toggle_pin(self, path: str) -> None:
        entry = self.get(path)
        if entry is not None:
            entry["pinned"] = not entry.get("pinned")
            self.save()

    def clear(self, keep_pinned: bool = True) -> None:
        keep = [d for d in self.items if keep_pinned and d.get("pinned")]
        for d in self.items:
            if d not in keep:
                try:
                    self.thumb_path(d["path"]).unlink()
                except OSError:
                    pass
        self.items = keep
        self.save()

    def _trim(self) -> None:
        unpinned = sorted((d for d in self.items if not d.get("pinned")),
                          key=lambda d: -(d.get("opened_ts") or 0))
        for d in unpinned[self.limit:]:
            self.items.remove(d)
            try:
                self.thumb_path(d["path"]).unlink()
            except OSError:
                pass


def relative_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        then = _dt.datetime.fromisoformat(iso)
    except ValueError:
        return ""
    delta = _dt.datetime.now() - then
    secs = delta.total_seconds()
    if secs < 60:
        return "just now"
    if secs < 3600:
        m = int(secs // 60)
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if secs < 86400 and then.date() == _dt.date.today():
        h = int(secs // 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    if then.date() == _dt.date.today() - _dt.timedelta(days=1):
        return "yesterday"
    if delta.days < 7:
        return then.strftime("%A")
    if then.year == _dt.date.today().year:
        return then.strftime("%b %d").replace(" 0", " ")
    return then.strftime("%b %d, %Y").replace(" 0", " ")
