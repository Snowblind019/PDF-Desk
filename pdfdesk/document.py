"""The open-document model: wraps a PyMuPDF document with undo/redo, dirty tracking and saving."""
from __future__ import annotations

import itertools
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pymupdf as fitz
from PySide6.QtCore import QObject, Signal

_ids = itertools.count(1)


class NeedsPassword(Exception):
    pass


class PdfDocument(QObject):
    changed = Signal(object)          # list of page numbers that changed, or None for "everything"
    structure_changed = Signal()      # pages added, removed, moved, rotated or resized
    dirty_changed = Signal(bool)
    history_changed = Signal()
    saved = Signal(str)

    MAX_STEPS = 50
    MAX_BYTES = 450 * 1024 * 1024

    def __init__(self, doc: fitz.Document, path: str | None = None, title: str | None = None,
                 password: str | None = None, suggested_path: str | None = None, is_new: bool = False):
        super().__init__()
        self.doc = doc
        self.path = path
        self.suggested_path = suggested_path or path
        self.title = title or (Path(path).name if path else "Untitled.pdf")
        self.password = password
        self.epoch = 0
        self.page_revs: dict[int, int] = {}
        self._sizes: list[fitz.Rect] | None = None
        self._undo: list[tuple[str, bytes, int]] = []
        self._redo: list[tuple[str, bytes, int]] = []
        self.state_id = next(_ids)
        self.saved_id = -1 if is_new else self.state_id

    # ------------------------------------------------------------------ opening
    @classmethod
    def open(cls, path: str, password: str | None = None) -> "PdfDocument":
        data = Path(path).read_bytes()  # read into memory so the file is never locked (Windows)
        doc = fitz.open(stream=data, filetype="pdf")
        if doc.needs_pass:
            if not password or not doc.authenticate(password):
                raise NeedsPassword(path)
        if doc.is_repaired:
            pass  # MuPDF quietly fixed a damaged file; saving will write a clean copy.
        return cls(doc, path=path, password=password)

    @classmethod
    def from_bytes(cls, data: bytes, title: str, suggested_path: str | None = None) -> "PdfDocument":
        doc = fitz.open("pdf", data)
        return cls(doc, path=None, title=title, suggested_path=suggested_path, is_new=True)

    # ------------------------------------------------------------------ info
    @property
    def page_count(self) -> int:
        return self.doc.page_count

    @property
    def dirty(self) -> bool:
        return self.state_id != self.saved_id

    @property
    def is_new(self) -> bool:
        return self.path is None

    def page(self, pno: int) -> fitz.Page:
        return self.doc.load_page(pno)

    def page_rect(self, pno: int) -> fitz.Rect:
        """Visible (rotated) page rectangle in points."""
        if self._sizes is None:
            self._sizes = [self.doc.load_page(i).rect for i in range(self.doc.page_count)]
        return self._sizes[pno]

    def page_key(self, pno: int) -> tuple[int, int]:
        """Changes whenever the page's appearance may have changed (used by render caches)."""
        return self.epoch, self.page_revs.get(pno, 0)

    def can_undo(self) -> bool:
        return bool(self._undo)

    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo_label(self) -> str:
        return self._undo[-1][0] if self._undo else ""

    def redo_label(self) -> str:
        return self._redo[-1][0] if self._redo else ""

    # ------------------------------------------------------------------ editing
    def _snapshot(self) -> bytes:
        return self.doc.tobytes(garbage=0, encryption=fitz.PDF_ENCRYPT_KEEP)

    def _load(self, data: bytes, password: str | None = None) -> None:
        old = self.doc
        doc = fitz.open("pdf", data)
        if doc.needs_pass:
            doc.authenticate(password or self.password or "")
        self.doc = doc
        try:
            old.close()
        except Exception:
            pass

    def _trim_history(self) -> None:
        while len(self._undo) > self.MAX_STEPS:
            self._undo.pop(0)
        total = sum(len(d) for _l, d, _i in self._undo) + sum(len(d) for _l, d, _i in self._redo)
        while total > self.MAX_BYTES and len(self._undo) > 1:
            total -= len(self._undo.pop(0)[1])

    def begin(self, label: str) -> None:
        self._undo.append((label, self._snapshot(), self.state_id))
        self._redo.clear()
        self._trim_history()

    def commit(self, pages: list[int] | None = None, structure: bool = False) -> None:
        was_dirty = self.dirty
        self.state_id = next(_ids)
        if structure or pages is None:
            self.epoch += 1
            self.page_revs.clear()
            self._sizes = None
        else:
            for p in pages:
                self.page_revs[p] = self.page_revs.get(p, 0) + 1
        if structure:
            self.structure_changed.emit()
        self.changed.emit(None if (structure or pages is None) else list(pages))
        if was_dirty != self.dirty:
            self.dirty_changed.emit(self.dirty)
        self.history_changed.emit()

    def rollback(self) -> None:
        """Throw away a begin() whose edit failed part-way."""
        if not self._undo:
            return
        _label, data, sid = self._undo.pop()
        self._load(data)
        self.state_id = sid
        self.epoch += 1
        self.page_revs.clear()
        self._sizes = None
        self.structure_changed.emit()
        self.changed.emit(None)
        self.history_changed.emit()

    @contextmanager
    def edit(self, label: str, pages: list[int] | None = None, structure: bool = False):
        self.begin(label)
        try:
            yield self.doc
        except Exception:
            self.rollback()
            raise
        self.commit(pages=pages, structure=structure)

    def replace_with_bytes(self, data: bytes, label: str, password: str | None = None) -> None:
        """Swap in a whole new version of the document (OCR, compression, passwords...)."""
        self.begin(label)
        try:
            self._load(data, password)
            if password is not None:
                self.password = password
        except Exception:
            self.rollback()
            raise
        self.commit(structure=True)

    def undo(self) -> None:
        if not self._undo:
            return
        was_dirty = self.dirty
        label, data, sid = self._undo.pop()
        self._redo.append((label, self._snapshot(), self.state_id))
        self._load(data)
        self.state_id = sid
        self._after_history_move(was_dirty)

    def redo(self) -> None:
        if not self._redo:
            return
        was_dirty = self.dirty
        label, data, sid = self._redo.pop()
        self._undo.append((label, self._snapshot(), self.state_id))
        self._load(data)
        self.state_id = sid
        self._after_history_move(was_dirty)

    def _after_history_move(self, was_dirty: bool) -> None:
        self.epoch += 1
        self.page_revs.clear()
        self._sizes = None
        self.structure_changed.emit()
        self.changed.emit(None)
        if was_dirty != self.dirty:
            self.dirty_changed.emit(self.dirty)
        self.history_changed.emit()

    # ------------------------------------------------------------------ saving
    def save(self, path: str | None = None) -> str:
        target = path or self.path
        if not target:
            raise ValueError("No file name given.")
        target = os.path.abspath(target)
        real = target
        if os.path.islink(target) and hasattr(os, "getuid"):
            # Save through a symlink only when both the link and the file it points at belong to
            # this user and it's a PDF. Otherwise the link itself is replaced, so a link planted in
            # a shared folder can't redirect the save onto some other file.
            resolved = os.path.realpath(target)
            try:
                if (os.lstat(target).st_uid == os.getuid() and os.stat(resolved).st_uid == os.getuid()
                        and resolved.lower().endswith(".pdf")):
                    real = resolved
            except OSError:
                pass
        # Write to an unpredictable temp file in the same folder, then swap it in, so a crash or a
        # full disk never leaves a half-written PDF behind.
        fd, tmp = tempfile.mkstemp(prefix=".pdfdesk-", suffix=".tmp", dir=os.path.dirname(real))
        os.close(fd)
        try:
            self.doc.save(tmp, garbage=1, deflate=True, encryption=fitz.PDF_ENCRYPT_KEEP)
            if os.path.exists(real):
                try:
                    shutil.copymode(real, tmp)  # keep the original file's permissions
                except OSError:
                    pass
            else:
                mask = os.umask(0)
                os.umask(mask)
                os.chmod(tmp, 0o666 & ~mask)  # a new file gets the usual permissions
            os.replace(tmp, real)
        finally:
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError:
                    pass
        was_dirty = self.dirty
        self.path = target
        self.suggested_path = target
        self.title = Path(target).name
        self.saved_id = self.state_id
        if was_dirty:
            self.dirty_changed.emit(False)
        self.saved.emit(target)
        return target

    def save_copy(self, path: str) -> None:
        self.doc.save(path, garbage=1, deflate=True, encryption=fitz.PDF_ENCRYPT_KEEP)

    def plain_bytes(self) -> bytes:
        """Unencrypted bytes, used when exporting or handing the document to background jobs."""
        return self.doc.tobytes(garbage=1, deflate=True, encryption=fitz.PDF_ENCRYPT_NONE)

    def close(self) -> None:
        try:
            self.doc.close()
        except Exception:
            pass
