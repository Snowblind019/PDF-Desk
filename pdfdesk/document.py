"""The open-document model: wraps a PyMuPDF document with undo/redo, dirty tracking and saving."""
from __future__ import annotations

import itertools
import os
import re
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
        self.base_bytes: bytes | None = None    # the bytes the in-memory document was read from
        self.signed_bytes: bytes | None = None  # the file as it was digitally signed, if it was

    # ------------------------------------------------------------------ opening
    @classmethod
    def open(cls, path: str, password: str | None = None) -> "PdfDocument":
        data = Path(path).read_bytes()  # read into memory so the file is never locked (Windows)
        doc = fitz.open(stream=data, filetype="pdf")
        if doc.needs_pass:
            if not password or not doc.authenticate(password):
                raise NeedsPassword(path)
        pdf = cls(doc, path=path, password=password)
        pdf.base_bytes = data
        if has_signatures(doc):
            pdf.signed_bytes = data
        return pdf

    @classmethod
    def from_bytes(cls, data: bytes, title: str, suggested_path: str | None = None) -> "PdfDocument":
        doc = fitz.open("pdf", data)
        pdf = cls(doc, path=None, title=title, suggested_path=suggested_path, is_new=True)
        pdf.base_bytes = data
        if has_signatures(doc):
            pdf.signed_bytes = data
        return pdf

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
        if self.keeps_signatures():
            return self.incremental_bytes()  # keeps the signed bytes intact through undo and redo
        return self.doc.tobytes(garbage=0, encryption=fitz.PDF_ENCRYPT_KEEP)

    # ------------------------------------------------------------------ digital signatures
    def keeps_signatures(self) -> bool:
        """True if saving now can keep this PDF's digital signatures valid (the signed file is kept
        byte for byte and the changes are added after it, as PDF signatures require). Not for a
        damaged file that MuPDF had to repair: changes can't be added safely to its end."""
        return bool(self.signed_bytes and self.base_bytes and self.base_bytes.startswith(self.signed_bytes)
                    and not self.doc.is_repaired)

    def will_break_signatures(self) -> bool:
        return bool(self.signed_bytes) and not self.keeps_signatures()

    def incremental_bytes(self) -> bytes:
        """The original bytes with the changes appended (an "incremental update")."""
        m = fitz.mupdf
        pdf = fitz._as_pdf_document(self.doc)
        # MuPDF remembers where the update it just wrote put its cross-reference table, as if the
        # update had been added to the file the document was read from. Here it goes to a separate
        # buffer, so that position is put back afterwards; otherwise the next update would point
        # at a table that isn't there (a broken, or even looping, file).
        startxref = pdf.m_internal.startxref
        opts = m.PdfWriteOptions()
        opts.do_incremental = 1
        buf = m.FzBuffer(0)
        out = m.FzOutput(buf)
        try:
            m.pdf_write_document(pdf, out, opts)
            out.fz_close_output()
        finally:
            pdf.m_internal.startxref = startxref
        data = bytes(buf.fz_buffer_extract())
        if self.base_bytes and not data.startswith(self.base_bytes):
            raise RuntimeError("The changes couldn't be added to the end of the file.")
        return data

    def save_bytes(self) -> bytes:
        """What Save would write: an incremental update for signed PDFs, otherwise a clean rewrite."""
        if self.keeps_signatures():
            return self.incremental_bytes()
        return self.doc.tobytes(garbage=1, deflate=True, encryption=fitz.PDF_ENCRYPT_KEEP)

    def _load(self, data: bytes, password: str | None = None) -> None:
        old = self.doc
        doc = fitz.open("pdf", data)
        if doc.needs_pass:
            doc.authenticate(password or self.password or "")
        self.doc = doc
        self.base_bytes = data
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
        if self.keeps_signatures():
            target = write_file_safely(target, self.incremental_bytes())
        else:
            target = write_file_safely(target, write=lambda tmp: self.doc.save(
                tmp, garbage=1, deflate=True, encryption=fitz.PDF_ENCRYPT_KEEP))
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
        write_file_safely(path, self.save_bytes())

    def plain_bytes(self) -> bytes:
        """Unencrypted bytes, used when exporting or handing the document to background jobs."""
        return self.doc.tobytes(garbage=1, deflate=True, encryption=fitz.PDF_ENCRYPT_NONE)

    def close(self) -> None:
        try:
            self.doc.close()
        except Exception:
            pass


def write_file_safely(target: str, data: bytes | None = None, write=None) -> str:
    """Write `data` (or call write(temp_path)) to an unpredictable temp file in the same folder, then
    swap it in, so a crash or a full disk never leaves a half-written file and nothing planted in a
    shared folder (such as a link with a guessable name) can redirect the write. Returns the path."""
    target = os.path.abspath(target)
    real = target
    if os.path.islink(target) and hasattr(os, "getuid"):
        # Save through a symlink only when both the link and the file it points at belong to this
        # user and it's a PDF. Otherwise the link itself is replaced.
        resolved = os.path.realpath(target)
        try:
            if (os.lstat(target).st_uid == os.getuid() and os.stat(resolved).st_uid == os.getuid()
                    and resolved.lower().endswith(".pdf")):
                real = resolved
        except OSError:
            pass
    fd, tmp = tempfile.mkstemp(prefix=".pdfdesk-", suffix=".tmp", dir=os.path.dirname(real))
    try:
        if write is None:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data or b"")
        else:
            os.close(fd)
            write(tmp)
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
    return target


_REF = re.compile(r"(\d+)\s+(\d+)\s+R")
MAX_FIELDS = 20000


def _ref_list(doc: fitz.Document, xref: int, key: str) -> list[int]:
    """Object numbers in an array that is stored under `key` (directly or as an object of its own)."""
    t, v = doc.xref_get_key(xref, key)
    if t == "xref":
        v = doc.xref_object(int(v.split()[0]), compressed=True)
        t = "array" if v.lstrip().startswith("[") else t
    if t != "array":
        return []
    return [int(n) for n, _g in _REF.findall(v[:4_000_000])]


def _key_ref(doc: fitz.Document, xref: int, key: str) -> int:
    t, v = doc.xref_get_key(xref, key)
    return int(v.split()[0]) if t == "xref" else 0


def signature_fields(doc: fitz.Document) -> list[dict]:
    """Every signature field of the form, found by walking the form's field list (so fields that
    aren't shown on a page, or whose value sits on a parent field, are found too).
    Each item: name, signed, page (None when not shown on a page), rect (PyMuPDF page coordinates,
    or None), xref (the field object)."""
    if not doc.is_pdf:
        return []
    cat = doc.pdf_catalog()
    t, v = doc.xref_get_key(cat, "AcroForm")
    if t == "xref":
        top = _ref_list(doc, int(v.split()[0]), "Fields")
    elif t == "dict":
        top = _ref_list(doc, cat, "AcroForm/Fields")
    else:
        return []
    n_objects = doc.xref_length()
    found, seen = [], set()
    stack = [(x, "", "") for x in reversed(top)]
    while stack and len(seen) < MAX_FIELDS:
        x, parent, ft = stack.pop()
        if x in seen or not 0 < x < n_objects:
            continue
        seen.add(x)
        tt, tv = doc.xref_get_key(x, "T")
        part = tv if tt == "string" else ""
        name = f"{parent}.{part}" if parent else part
        ft_t, ft_v = doc.xref_get_key(x, "FT")
        ft = ft_v if ft_t == "name" else ft
        kids = _ref_list(doc, x, "Kids")
        field_kids = [k for k in kids if 0 < k < n_objects and doc.xref_get_key(k, "T")[0] != "null"]
        for k in reversed(field_kids):
            stack.append((k, name, ft))
        if ft != "/Sig" or field_kids:
            continue
        vt, _vv = doc.xref_get_key(x, "V")
        widgets = [k for k in kids if k not in field_kids] or [x]
        found.append({"name": name, "signed": vt not in ("null", ""), "page": None, "rect": None, "xref": x,
                      "widgets": widgets})
    if found:
        where: dict[int, int] = {}   # annotation object -> page number
        for page in doc:
            for ax, _type, _id in page.annot_xrefs():
                where.setdefault(ax, page.number)
        for f in found:
            for w in f["widgets"]:
                if w in where:
                    f["page"] = where[w]
                    widget = next((wd for wd in doc[f["page"]].widgets() if wd.xref == w), None)
                    f["rect"] = fitz.Rect(widget.rect) if widget is not None else None
                    break
    for f in found:
        del f["widgets"]
    return found


def has_signatures(doc: fitz.Document) -> bool:
    """Does the PDF contain at least one digital signature (a signed signature field)?"""
    try:
        return any(f["signed"] for f in signature_fields(doc))
    except Exception:
        return False
