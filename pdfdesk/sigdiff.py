"""What changed in a PDF after one of its earlier versions. Used to judge digital signatures.

A signed PDF is never changed in place: later edits (comments, form filling, more signatures) are
added after the signed bytes as "incremental updates". The signature only proves the signed bytes
are intact, so a checker must also look at what the updates did. An update can add a comment
(normally allowed) or swap a page's content (never allowed), and both leave the signature itself
intact.

pyHanko's own check (used first) accepts the changes it knows: signatures, form filling and a few
technical additions. It treats every other change as suspicious, including comments, which Acrobat
and PDF Desk allow on signed documents. When pyHanko can't explain a change, this module compares
the signed version with the current one object by object and sorts every difference into one of
the levels below. Anything it can't place is "other", which makes the signature show as invalid.

The levels use the same numbers as pyHanko's ModificationLevel.
"""
from __future__ import annotations

import io

NONE, TECH, FORM, COMMENTS, OTHER = 0, 1, 2, 3, 4

MAX_OBJECTS = 400_000
MAX_STREAM_BYTES = 1024 * 1024 * 1024
MAX_PAGES = 100_000
MAX_FIELDS = 50_000
MAX_DEPTH = 200
MAX_REVISIONS = 100   # later versions pyHanko checks one by one

INHERITABLE = ("/Resources", "/MediaBox", "/CropBox", "/Rotate")
LINK_KEYS = frozenset({"/P", "/Popup", "/Parent", "/IRT"})   # links between annotations, fields and pages
VALUE_KEYS = frozenset({"/V", "/AP", "/AS"})                 # what filling in or signing a field changes

CATALOG_TECH = frozenset({"/DSS", "/Version", "/Extensions"})
CATALOG_MINOR = {"/Outlines": "bookmarks", "/PageLabels": "page numbers", "/PageMode": "how the document opens",
                 "/PageLayout": "how the document opens", "/ViewerPreferences": "viewer settings",
                 "/Lang": "language", "/MarkInfo": "accessibility tags", "/StructTreeRoot": "accessibility tags",
                 "/Metadata": "document properties", "/Threads": "article threads", "/PieceInfo": "private data"}
CATALOG_NAMES = {"/Names": "named items (attachments, scripts or link targets)", "/OpenAction": "opening action",
                 "/AA": "automatic actions", "/OCProperties": "layers", "/Perms": "permissions",
                 "/Dests": "link targets", "/URI": "web link settings", "/Collection": "portfolio settings"}

ANNOT_TYPES = frozenset("/" + n for n in (
    "Text Link FreeText Line Square Circle Polygon PolyLine Highlight Underline Squiggly StrikeOut Stamp "
    "Caret Ink Popup FileAttachment Sound Movie Widget Screen PrinterMark TrapNet Watermark 3D Redact "
    "Projection RichMedia").split())


class TooBig(Exception):
    """The document is too large or too tangled to compare completely."""


class Malformed(Exception):
    """The document's structure is broken in a way that hides what it shows."""


class Changes:
    """The differences found, as a level, kinds and plain-language notes."""

    def __init__(self):
        self.level = NONE
        self.kinds: set[str] = set()      # tech, signature, form, comment, settings, content
        self.notes: list[str] = []
        self.form_fields: set[str] = set()

    def add(self, level: int, kind: str, note: str = "") -> None:
        self.level = max(self.level, level)
        self.kinds.add(kind)
        if note and note not in self.notes and len(self.notes) < 40:
            self.notes.append(note)

    @property
    def only_signatures(self) -> bool:
        """Only signatures (and technical data) were added: the signed content is untouched."""
        return self.kinds <= {"tech", "signature"}

    def summary(self, limit: int = 4, inline: bool = False) -> str:
        """The first notes as sentences, or (inline) as one phrase to put in brackets."""
        return summarize(self.notes, limit, inline)


def summarize(notes: list[str], limit: int = 4, inline: bool = False) -> str:
    shown = list(notes[:limit])
    more = len(notes) - len(shown)
    if inline:
        text = "; ".join(n[:1].lower() + n[1:].rstrip(".") for n in shown)
        return text + (f"; and {more} more change(s)" if more > 0 else "")
    return " ".join(shown) + (f" There are {more} more change(s)." if more > 0 else "")


# --------------------------------------------------------------------------- comparing

class _Compare:
    def __init__(self, old, new, changed: set[int]):
        from pyhanko.pdf_utils import generic
        self.g = generic
        self.old, self.new = old, new          # pyHanko HistoricalResolver views
        self.changed = changed                 # object numbers written or freed after the old version
        self.done_true: set[tuple] = set()    # pairs known to be equal
        self.done_false: set[tuple] = set()   # pairs known to differ
        self.tentative: set[tuple] = set()    # assumed equal during the current comparison
        self.in_top = False
        self.objects = 0
        self.stream_bytes = 0
        self.identity: set[int] = set()        # objects fully compared by a pass of their own
        self.ch = Changes()

    # ------------------------------------------------------------------ helpers
    def _tick(self) -> None:
        self.objects += 1
        if self.objects > MAX_OBJECTS:
            raise TooBig()

    def deref(self, obj):
        return obj.get_object() if isinstance(obj, self.g.IndirectObject) else obj

    def is_ref(self, obj) -> bool:
        return isinstance(obj, self.g.IndirectObject)

    def is_dict(self, obj) -> bool:
        return isinstance(obj, self.g.DictionaryObject)

    def raw(self, container, key):
        """A value without following references. Strings are decrypted when the file is encrypted;
        a few (such as a signature's contents) never are, and those are compared as stored."""
        try:
            return container.raw_get(key)
        except Exception:
            store = dict if isinstance(container, self.g.DictionaryObject) else list
            value = store.__getitem__(container, key)
            return getattr(value, "raw_object", value)

    def get(self, d, key):
        """Raw (not dereferenced) value of a dictionary key, or None."""
        if self.is_dict(d) and key in d:
            return self.raw(d, key)
        return None

    def name(self, d, key) -> str:
        v = self.deref(self.get(d, key))
        return str(v) if isinstance(v, self.g.NameObject) else ""

    def text(self, d, key) -> str:
        v = self.deref(self.get(d, key))
        if isinstance(v, (self.g.TextStringObject, self.g.ByteStringObject)):
            try:
                return str(v) if isinstance(v, self.g.TextStringObject) else bytes(v).decode("latin-1")
            except Exception:
                return ""
        return ""

    def items(self, arr):
        arr = self.deref(arr)
        if isinstance(arr, self.g.ArrayObject):
            return [self.raw(arr, i) for i in range(len(arr))]
        return []

    # ------------------------------------------------------------------ deep equality
    # Objects can point at each other in loops, so a pair being compared is assumed equal until
    # shown otherwise. Those assumptions only become final when the whole comparison they belong
    # to comes out equal; a difference found along the way is always final.
    def eq(self, a, b) -> bool:
        return self._top(self.same, a, b)

    def eq_dict(self, a, b, skip=frozenset(), ident=frozenset()) -> bool:
        return self._top(self.same_dict, a, b, 0, skip, ident)

    def eq_opt(self, a, b) -> bool:
        if a is None or b is None:
            return a is None and b is None
        return self.eq(a, b)

    def eq_identity(self, a, b) -> bool:
        return self._top(self.same_identity, a, b)

    def _top(self, fn, *args):
        if self.in_top:
            return fn(*args)
        self.in_top, self.tentative = True, set()
        try:
            result = fn(*args)
        except RecursionError:
            raise TooBig()
        finally:
            self.in_top = False
        if result:
            self.done_true |= self.tentative
        self.tentative = set()
        return result

    def same(self, a, b, depth: int = 0) -> bool:
        g = self.g
        if depth > MAX_DEPTH:
            raise TooBig()
        ra, rb = self.is_ref(a), self.is_ref(b)
        if ra and rb:
            return self.same_ref(a, b, depth)
        if ra or rb:
            return self.same(self.deref(a), self.deref(b), depth + 1)
        if isinstance(a, g.StreamObject) or isinstance(b, g.StreamObject):
            if not (isinstance(a, g.StreamObject) and isinstance(b, g.StreamObject)):
                return False
            if not self.same_dict(a, b, depth):
                return False
            da, db = a.encoded_data, b.encoded_data
            self.stream_bytes += len(da) + len(db)
            if self.stream_bytes > MAX_STREAM_BYTES:
                raise TooBig()
            return da == db
        if isinstance(a, g.DictionaryObject) or isinstance(b, g.DictionaryObject):
            return isinstance(a, g.DictionaryObject) and isinstance(b, g.DictionaryObject) and \
                self.same_dict(a, b, depth)
        if isinstance(a, g.ArrayObject) or isinstance(b, g.ArrayObject):
            if not (isinstance(a, g.ArrayObject) and isinstance(b, g.ArrayObject)) or len(a) != len(b):
                return False
            return all(self.same(self.raw(a, i), self.raw(b, i), depth + 1) for i in range(len(a)))
        return self.same_value(a, b)

    def same_value(self, a, b) -> bool:
        g = self.g
        if isinstance(a, g.NameObject) or isinstance(b, g.NameObject):
            return isinstance(a, g.NameObject) and isinstance(b, g.NameObject) and str(a) == str(b)
        nums = (g.NumberObject, g.FloatObject)
        if isinstance(a, nums) or isinstance(b, nums):
            return isinstance(a, nums) and isinstance(b, nums) and a == b
        strings = (g.TextStringObject, g.ByteStringObject)
        if isinstance(a, strings) or isinstance(b, strings):
            if not (isinstance(a, strings) and isinstance(b, strings)):
                return False
            try:
                return bytes(a.original_bytes) == bytes(b.original_bytes)
            except Exception:
                return str(a) == str(b)
        if isinstance(a, g.BooleanObject) or isinstance(b, g.BooleanObject):
            return isinstance(a, g.BooleanObject) and isinstance(b, g.BooleanObject) and bool(a) == bool(b)
        if isinstance(a, g.NullObject) or isinstance(b, g.NullObject):
            return isinstance(a, g.NullObject) and isinstance(b, g.NullObject)
        return False

    def same_dict(self, a, b, depth: int = 0, skip=frozenset(), ident=frozenset()) -> bool:
        keys = set(a.keys()) - skip
        if keys != set(b.keys()) - skip:
            return False
        for k in keys:
            va, vb = self.raw(a, k), self.raw(b, k)
            if k in ident:
                if not self.same_identity(va, vb):
                    return False
            elif not self.same(va, vb, depth + 1):
                return False
        return True

    def same_identity(self, a, b) -> bool:
        """For links between objects that are compared on their own (an annotation's page, its popup)."""
        if self.is_ref(a) and self.is_ref(b):
            return a.idnum == b.idnum and a.generation == b.generation
        return self.same(a, b)

    def same_ref(self, a, b, depth: int) -> bool:
        key = (a.idnum, a.generation, b.idnum, b.generation)
        if key in self.done_false:
            return False
        if key in self.done_true or key in self.tentative:
            return True
        self.tentative.add(key)
        self._tick()
        same_number = a.idnum == b.idnum and a.generation == b.generation
        if same_number and a.idnum in self.identity:
            return True       # a page, form field or the catalog: compared by its own pass
        if same_number and self.unchanged(a):
            # Not rewritten since the old version, so the object itself is identical; only what it
            # points to can differ.
            result = self.children_same(b.get_object(), depth)
        else:
            result = self.same(a.get_object(), b.get_object(), depth + 1)
        if not result:
            self.done_false.add(key)
        return result

    def unchanged(self, ref) -> bool:
        """True when the object was last written at or before the old version, which is exactly when
        both views read the very same object (pyHanko decides it the same way)."""
        if ref.idnum in self.changed:
            return False
        try:
            return self.new.reader.xrefs.get_last_change(ref.reference) <= self.old.revision
        except Exception:
            return False

    def children_same(self, obj, depth: int) -> bool:
        """Compare what an unchanged object points to. Only references matter here, so nothing is
        decrypted."""
        g = self.g
        stack = [obj]
        while stack:
            cur = stack.pop()
            cur = getattr(cur, "raw_object", cur)
            if isinstance(cur, g.DictionaryObject):
                values = list(dict.values(cur))
            elif isinstance(cur, g.ArrayObject):
                values = list(list.__iter__(cur))
            else:
                continue
            for v in values:
                v = getattr(v, "raw_object", v)
                if isinstance(v, g.IndirectObject):
                    a = g.IndirectObject(v.idnum, v.generation, self.old)
                    b = g.IndirectObject(v.idnum, v.generation, self.new)
                    if not self.same_ref(a, b, depth + 1):
                        return False
                elif isinstance(v, (g.DictionaryObject, g.ArrayObject)):
                    stack.append(v)
        return True

    # ------------------------------------------------------------------ structure
    def pages(self, root) -> list:
        """The page objects in order (as references), walking the page tree."""
        out, seen = [], set()

        def walk(node, depth):
            if depth > 64:
                raise Malformed("The page tree is too deep.")
            if not self.is_ref(node):
                raise Malformed("The page tree has a page that isn't a separate object.")
            if node.idnum in seen:
                raise Malformed("The page tree loops.")
            seen.add(node.idnum)
            obj = node.get_object()
            if not self.is_dict(obj):
                raise Malformed("The page tree is broken.")
            kids = self.get(obj, "/Kids")
            if self.name(obj, "/Type") != "/Page" and kids is not None:
                for kid in self.items(kids):
                    walk(kid, depth + 1)
            else:
                out.append(node)
                if len(out) > MAX_PAGES:
                    raise TooBig()

        top = self.get(root, "/Pages")
        if top is None:
            raise Malformed("The document has no pages.")
        walk(top, 0)
        return out

    def inherited(self, page, key):
        node, depth = page, 0
        while self.is_dict(node) and depth < 64:
            v = self.get(node, key)
            if v is not None:
                return v
            node = self.deref(self.get(node, "/Parent"))
            depth += 1
        return None

    def field_map(self, root) -> dict:
        """Fully qualified field name -> (reference or None, field dictionary, field type)."""
        af = self.deref(self.get(root, "/AcroForm"))
        out: dict = {}
        if not self.is_dict(af):
            return out
        seen: set[int] = set()
        stack = [(item, "", "", 0) for item in reversed(self.items(self.get(af, "/Fields")))]
        while stack:
            item, parent, ft, depth = stack.pop()
            if depth > 32:
                raise Malformed("The form's fields are nested too deeply.")
            ref = item if self.is_ref(item) else None
            if ref is not None:
                if ref.idnum in seen:
                    continue
                seen.add(ref.idnum)
            obj = self.deref(item)
            if not self.is_dict(obj):
                continue
            if len(seen) > MAX_FIELDS:
                raise TooBig()
            part = self.text(obj, "/T")
            name = f"{parent}.{part}" if parent else part
            ft = self.name(obj, "/FT") or ft
            key, k = name, 1
            while key in out:
                k += 1
                key = f"{name}#{k}"
            out[key] = (ref, obj, ft)
            for kid in reversed(self.items(self.get(obj, "/Kids"))):
                kid_obj = self.deref(kid)
                if self.is_dict(kid_obj) and self.get(kid_obj, "/T") is not None:
                    stack.append((kid, name, ft, depth + 1))
        return out

    def widget_field(self, widget):
        """(field name, field type, field dictionary) for a widget annotation."""
        names, ft, node, field, depth = [], "", widget, None, 0
        while self.is_dict(node) and depth < 32:
            if self.get(node, "/T") is not None:
                names.append(self.text(node, "/T"))
                field = field if field is not None else node
            ft = ft or self.name(node, "/FT")
            node = self.deref(self.get(node, "/Parent"))
            depth += 1
        return ".".join(reversed(names)), ft, field if field is not None else widget

    def field_value(self, field):
        """The value of a field (inherited from its parents when it has none of its own)."""
        node, depth = field, 0
        while self.is_dict(node) and depth < 32:
            v = self.get(node, "/V")
            if v is not None:
                return None if isinstance(self.deref(v), self.g.NullObject) else v
            node = self.deref(self.get(node, "/Parent"))
            depth += 1
        return None

    # ------------------------------------------------------------------ the passes
    def run(self) -> Changes:
        old_root, new_root = self.old.root, self.new.root
        old_pages, new_pages = self.pages(old_root), self.pages(new_root)
        same_pages = [(p.idnum, p.generation) for p in old_pages] == [(p.idnum, p.generation) for p in new_pages]
        old_fields, new_fields = self.field_map(old_root), self.field_map(new_root)
        # Objects that a pass below compares in full may be skipped wherever else they're linked from.
        if same_pages:
            self.identity |= {p.idnum for p in old_pages}
        ro, rn = self.old.root_ref, self.new.root_ref
        if ro.idnum == rn.idnum:
            self.identity.add(ro.idnum)
        af_o, af_n = self.get(old_root, "/AcroForm"), self.get(new_root, "/AcroForm")
        if self.is_ref(af_o) and self.is_ref(af_n) and af_o.idnum == af_n.idnum:
            self.identity.add(af_o.idnum)
        for name, (ref, _obj, _ft) in old_fields.items():
            other = new_fields.get(name)
            if ref is not None and other is not None and other[0] is not None and other[0].idnum == ref.idnum:
                self.identity.add(ref.idnum)

        self.trailer_pass()
        self.catalog_pass(old_root, new_root)
        if not same_pages:
            self.ch.add(OTHER, "content", "Pages were added, removed or put in a different order.")
        else:
            for pno, ref in enumerate(old_pages):
                self.page_pass(pno, ref.get_object(), new_pages[pno].get_object())
        self.form_pass(old_root, new_root, old_fields, new_fields)
        return self.ch

    def trailer_pass(self) -> None:
        to, tn = self.old.trailer_view, self.new.trailer_view
        if not self.eq_opt(self.get(to, "/Encrypt"), self.get(tn, "/Encrypt")):
            self.ch.add(OTHER, "content", "The document's encryption settings changed.")
        if not self.eq_opt(self.get(to, "/Info"), self.get(tn, "/Info")):
            self.ch.add(TECH, "tech", "The document properties (such as title or author) changed.")

    def catalog_pass(self, old_root, new_root) -> None:
        for key in sorted(set(old_root.keys()) | set(new_root.keys())):
            if key in ("/Pages", "/AcroForm", "/Type"):
                continue
            if self.eq_opt(self.get(old_root, key), self.get(new_root, key)):
                continue
            if key in CATALOG_TECH:
                self.ch.add(TECH, "tech")
            elif key in CATALOG_MINOR:
                self.ch.add(COMMENTS, "settings", f"Document settings changed: {CATALOG_MINOR[key]}.")
            else:
                what = CATALOG_NAMES.get(key, f"internal data ({key[1:60]})")
                self.ch.add(OTHER, "content", f"The document's {what} changed.")
        if self.name(old_root, "/Type") != self.name(new_root, "/Type"):
            self.ch.add(OTHER, "content", "The document's structure changed.")

    def page_pass(self, pno: int, po, pn) -> None:
        if not (self.is_dict(po) and self.is_dict(pn)):
            self.ch.add(OTHER, "content", f"Page {pno + 1} is damaged.")
            return
        ok = self.eq_dict(po, pn, skip=frozenset({"/Annots", "/Parent"}))
        if ok:
            for key in INHERITABLE:
                if self.get(po, key) is None and self.get(pn, key) is None:
                    if not self.eq_opt(self.inherited(po, key), self.inherited(pn, key)):
                        ok = False
                        break
        if not ok:
            self.ch.add(OTHER, "content", f"The content of page {pno + 1} changed.")
        self.annot_pass(pno, self.items(self.get(po, "/Annots")), self.items(self.get(pn, "/Annots")))

    def annot_pass(self, pno: int, old_items: list, new_items: list) -> None:
        page = f"page {pno + 1}"
        old_direct = [a for a in old_items if not self.is_ref(a)]
        new_direct = [a for a in new_items if not self.is_ref(a)]
        if len(old_direct) != len(new_direct) or not all(self.eq(a, b) for a, b in zip(old_direct, new_direct)):
            self.ch.add(OTHER, "content", f"Annotations on {page} changed in an unusual way.")
        old_refs = {a.idnum: a for a in old_items if self.is_ref(a)}
        new_refs = {a.idnum: a for a in new_items if self.is_ref(a)}
        counts = {"added": 0, "changed": 0, "removed": 0}
        popups = False

        def count(what: str, obj) -> None:
            nonlocal popups
            if self.name(obj, "/Subtype") == "/Popup":
                popups = True   # the pop-up window of a note; counted with its note
            else:
                counts[what] += 1

        for num in old_refs.keys() - new_refs.keys():
            obj = old_refs[num].get_object()
            if self.name(obj, "/Subtype") == "/Widget":
                self.ch.add(OTHER, "content", f"A form field was removed from {page}.")
            else:
                count("removed", obj)
        for num in new_refs.keys() - old_refs.keys():
            obj = new_refs[num].get_object()
            if not self.is_dict(obj):
                self.ch.add(OTHER, "content", f"Something unusual was added to {page}.")
            elif self.name(obj, "/Subtype") == "/Widget":
                name, ft, _field = self.widget_field(obj)
                if ft == "/Sig":
                    self.ch.add(FORM, "signature", "A signature was added.")
                else:
                    self.ch.add(OTHER, "content", f"A form field ({name or 'unnamed'}) was added to {page}.")
            else:
                count("added", obj)
        for num in sorted(old_refs.keys() & new_refs.keys()):
            o, n = old_refs[num].get_object(), new_refs[num].get_object()
            if self.annot_changed(pno, o, n):
                count("changed", n)
        for what, k in counts.items():
            if k:
                where = "from" if what == "removed" else "on"
                self.ch.add(COMMENTS, "comment", f"{k} comment(s) or markup(s) were {what} {where} {page}.")
        if popups and not any(counts.values()):
            self.ch.add(COMMENTS, "comment", f"Comments were changed on {page}.")

    def annot_changed(self, pno: int, o, n) -> bool:
        """Compare one annotation that is on the page in both versions. True for a changed comment;
        changes to form fields are recorded directly."""
        if not (self.is_dict(o) and self.is_dict(n)):
            self.ch.add(OTHER, "content", f"Something unusual changed on page {pno + 1}.")
            return False
        wo, wn = self.name(o, "/Subtype") == "/Widget", self.name(n, "/Subtype") == "/Widget"
        if wo != wn:
            self.ch.add(OTHER, "content", f"A form field on page {pno + 1} changed into something else.")
            return False
        if not wo:
            return not self.eq_dict(o, n, ident=LINK_KEYS)
        if not self.eq_dict(o, n, skip=VALUE_KEYS, ident=LINK_KEYS):
            self.ch.add(OTHER, "content", f"The settings of a form field on page {pno + 1} changed.")
            return False
        if all(self.eq_opt(self.get(o, k), self.get(n, k)) for k in VALUE_KEYS):
            return False
        name, ft, field_n = self.widget_field(n)
        _name, _ft, field_o = self.widget_field(o)
        vo, vn = self.field_value(field_o), self.field_value(field_n)
        label = f"\u201c{name}\u201d" if name else "a form field"
        if ft == "/Sig":
            if vo is None and vn is not None:
                self.ch.add(FORM, "signature", "A signature field was signed.")
            else:
                self.ch.add(OTHER, "content", f"The signature field {label} was changed.")
        else:
            self.ch.add(FORM, "form", f"Form field {label} was filled in or changed.")
            self.ch.form_fields.add(name)
        return False

    def form_pass(self, old_root, new_root, old_fields: dict, new_fields: dict) -> None:
        af_o, af_n = self.deref(self.get(old_root, "/AcroForm")), self.deref(self.get(new_root, "/AcroForm"))
        if self.is_dict(af_o) and self.is_dict(af_n):
            loose = frozenset({"/Fields", "/SigFlags", "/DR", "/DA", "/NeedAppearances"})
            if not self.eq_dict(af_o, af_n, skip=loose):
                self.ch.add(OTHER, "content", "The form's settings changed.")
            if not self.eq_opt(self.get(af_o, "/SigFlags"), self.get(af_n, "/SigFlags")):
                self.ch.add(TECH, "tech")
            if not all(self.eq_opt(self.get(af_o, k), self.get(af_n, k)) for k in ("/DR", "/DA", "/NeedAppearances")):
                self.ch.add(FORM, "form", "The form's default text settings changed.")
        elif self.is_dict(af_o) != self.is_dict(af_n):
            if self.is_dict(af_o):
                self.ch.add(OTHER, "content", "The form was removed.")
            elif any(ft != "/Sig" for _r, _o, ft in new_fields.values()):
                self.ch.add(OTHER, "content", "A form was added.")
        for name in sorted(old_fields.keys() - new_fields.keys()):
            self.ch.add(OTHER, "content", f"Form field \u201c{name}\u201d was removed.")
        for name in sorted(new_fields.keys() - old_fields.keys()):
            if new_fields[name][2] == "/Sig":
                self.ch.add(FORM, "signature", "A signature field was added.")
            else:
                self.ch.add(OTHER, "content", f"Form field \u201c{name}\u201d was added.")
        for name in sorted(old_fields.keys() & new_fields.keys()):
            _r, fo, ft_o = old_fields[name]
            _r, fn, ft = new_fields[name]
            label = f"\u201c{name}\u201d"
            if ft_o != ft or not self.eq_dict(fo, fn, skip=VALUE_KEYS | {"/Kids"}, ident=LINK_KEYS):
                self.ch.add(OTHER, "content", f"The settings of form field {label} changed.")
                continue
            kids_o = {k.idnum for k in self.items(self.get(fo, "/Kids")) if self.is_ref(k)}
            kids_n = {k.idnum for k in self.items(self.get(fn, "/Kids")) if self.is_ref(k)}
            if kids_o != kids_n:
                if ft == "/Sig" and kids_o <= kids_n:
                    self.ch.add(FORM, "signature", "A signature field was added.")
                else:
                    self.ch.add(OTHER, "content", f"The parts of form field {label} changed.")
            vo, vn = self.get(fo, "/V"), self.get(fn, "/V")
            vo = None if vo is None or isinstance(self.deref(vo), self.g.NullObject) else vo
            vn = None if vn is None or isinstance(self.deref(vn), self.g.NullObject) else vn
            if vo is None and vn is None:
                continue
            if ft == "/Sig":
                if vo is None:
                    self.ch.add(FORM, "signature", "A signature field was signed.")
                elif vn is None or not self.eq(vo, vn):
                    self.ch.add(OTHER, "content", f"The signature in {label} was replaced or removed.")
            elif vo is None or vn is None or not self.eq(vo, vn):
                self.ch.add(FORM, "form", f"Form field {label} was filled in or changed.")
                self.ch.form_fields.add(name)


# --------------------------------------------------------------------------- what MuPDF shows

MAX_SHOWN_PAGES = 1000


def shown_pages_differ(signed: bytes, current: bytes, password: str | None = None) -> str:
    """A second, independent check: compare what MuPDF (which draws the pages in PDF Desk) shows on
    each page in the signed version and now, leaving comments and form fields aside. pyHanko and
    MuPDF read files separately, so a file built to be read differently by the two is caught here.
    Returns "" when the pages match, otherwise a note."""
    import pymupdf as fitz
    a, b = fitz.open("pdf", signed), fitz.open("pdf", current)
    for d in (a, b):
        if d.needs_pass:
            d.authenticate(password or "")
    if a.page_count != b.page_count:
        return "The number of pages shown differs from the signed version."
    for d in (a, b):   # these are private copies: take the comments and fields off their pages
        for pno in range(min(d.page_count, MAX_SHOWN_PAGES)):
            d.xref_set_key(d.page_xref(pno), "Annots", "null")
    for pno in range(min(a.page_count, MAX_SHOWN_PAGES)):
        pa, pb = a[pno], b[pno]
        if pa.rect != pb.rect or pa.rotation != pb.rotation:
            return f"Page {pno + 1} has a different size or rotation than when it was signed."
        if pa.get_text("text") != pb.get_text("text"):
            return f"The text shown on page {pno + 1} differs from the signed version."

        def pictures(page):
            return [(tuple(round(v, 1) for v in info["bbox"]), info.get("width"), info.get("height"))
                    for info in page.get_image_info()]
        if pictures(pa) != pictures(pb):
            return f"The pictures on page {pno + 1} differ from the signed version."
    return ""


# --------------------------------------------------------------------------- pyHanko safety net

MAX_SECTIONS = 20_000


def harden_pyhanko() -> None:
    """pyHanko follows a PDF's chain of cross-reference tables without checking for loops, so a file
    whose table points back at itself would make it read forever. This adds that check (once per
    process) by wrapping the two methods that read one table each."""
    try:
        from pyhanko.pdf_utils import misc, xref
    except Exception:
        return
    builder = getattr(xref, "XRefBuilder", None)
    if builder is None or getattr(builder, "_pdfdesk_guarded", False):
        return

    def guarded(method):
        def read(self, *args, **kwargs):
            seen = self.__dict__.setdefault("_pdfdesk_seen", set())
            here = self.stream.tell()
            if here in seen or len(seen) >= MAX_SECTIONS:
                raise misc.PdfReadError("The file's list of versions loops or is far too long.")
            seen.add(here)
            return method(self, *args, **kwargs)
        return read

    for name in ("_read_xref_table", "_read_xref_stream"):
        method = getattr(builder, name, None)
        if method is not None:
            setattr(builder, name, guarded(method))
    builder._pdfdesk_guarded = True


# --------------------------------------------------------------------------- entry points

def compare(reader, base_revision: int) -> Changes:
    """Changes between revision `base_revision` of a pyHanko PdfFileReader and its latest revision."""
    last = reader.xrefs.total_revisions - 1
    ch = Changes()
    if base_revision >= last:
        return ch
    changed: set[int] = set()
    for rev in range(base_revision + 1, last + 1):
        changed |= {r.idnum for r in reader.xrefs.explicit_refs_in_revision(rev)}
        changed |= {r.idnum for r in reader.xrefs.refs_freed_in_revision(rev)}
    old = reader.get_historical_resolver(base_revision)
    new = reader.get_historical_resolver(last)
    try:
        return _Compare(old, new, changed).run()
    except Malformed as exc:
        ch.add(OTHER, "content", str(exc))
        return ch


def _reader(data: bytes, password: str | None):
    harden_pyhanko()
    from pyhanko.pdf_utils.reader import PdfFileReader
    reader = PdfFileReader(io.BytesIO(data))
    if reader.encrypted:
        reader.decrypt((password or "").encode("utf-8"))
    return reader


def changes_between(old_data: bytes, new_data: bytes, password: str | None = None) -> Changes | None:
    """What an update appended to old_data changes (None when it can't be worked out)."""
    if not new_data.startswith(old_data):
        return None
    try:
        base = _reader(old_data, password).xrefs.total_revisions - 1
        return compare(_reader(new_data, password), base)
    except Exception:
        return None


PERM_TEXT = {1: "no changes", 2: "only filling in forms and signing", 3: "only filling in forms, signing and comments"}


def update_problem(old_data: bytes, new_data: bytes, password: str | None = None) -> str:
    """Why saving new_data (old_data plus an update) would make a signature show as invalid, in
    plain words, or "" if it wouldn't (or that can't be worked out)."""
    ch = changes_between(old_data, new_data, password)
    if ch is None or ch.level == NONE:
        return ""
    if ch.level >= OTHER:
        return ("Changes like these aren't allowed after signing: " + ch.summary(3)
                + " The signatures will show as INVALID when they are checked.")
    try:
        reader = _reader(new_data, password)
        sigs = list(reader.embedded_signatures)
    except Exception:
        return ""
    for sig in sigs:
        try:
            perm = sig.docmdp_level
            allowed = int(perm.value) if perm is not None else None
            if allowed is not None and ch.level > max(allowed, TECH):
                return (f"The person who signed or certified this document allowed {PERM_TEXT.get(allowed, 'few changes')} "
                        f"afterwards. Your changes go further ({ch.summary(2, inline=True)}), so their signature "
                        "will show as INVALID when it is checked.")
            lock = sig.fieldmdp
            if lock is not None and any(lock.is_locked(n) for n in ch.form_fields):
                return ("One of the form fields you changed was locked by a signature, so that signature will "
                        "show as INVALID when it is checked.")
        except Exception:
            continue
    return ""


def make_policy():
    """A pyHanko difference policy: pyHanko's own rules first, then the comparison above for the
    changes they don't cover (such as comments). The comparison results are kept in .changes,
    keyed by the signed revision."""
    harden_pyhanko()
    from pyhanko.sign.diff_analysis import (DEFAULT_DIFF_POLICY, DiffPolicy, DiffResult, ModificationLevel,
                                            SuspiciousModification)

    class PdfDeskPolicy(DiffPolicy):
        def __init__(self):
            self.changes: dict[int, Changes | None] = {}

        def apply(self, old, new, field_mdp_spec=None, doc_mdp=None):
            return DEFAULT_DIFF_POLICY.apply(old, new, field_mdp_spec, doc_mdp)

        def review_file(self, reader, base_revision, field_mdp_spec=None, doc_mdp=None):
            rev = base_revision if isinstance(base_revision, int) else base_revision.revision
            if reader.xrefs.total_revisions - 1 - rev > MAX_REVISIONS:
                # pyHanko checks every later version one by one, which a file with thousands of tiny
                # updates turns into minutes of work. The comparison below looks only at the result.
                std = SuspiciousModification("Too many updates after signing to check one by one.")
            else:
                std = DEFAULT_DIFF_POLICY.review_file(reader, base_revision, field_mdp_spec=field_mdp_spec,
                                                      doc_mdp=doc_mdp)
            try:
                ch = compare(reader, rev)
            except Exception:
                ch = None
            self.changes[rev] = ch
            if isinstance(std, DiffResult) or ch is None or ch.level >= OTHER:
                return std
            if field_mdp_spec is not None and any(field_mdp_spec.is_locked(n) for n in ch.form_fields):
                return SuspiciousModification("A form field locked by the signature was changed.")
            return DiffResult(ModificationLevel(ch.level), set(ch.form_fields))

    return PdfDeskPolicy()
