"""Prepare Form: create, change and remove form fields, find fields automatically, and save or load
the values people typed (JSON, CSV or XFDF, which Acrobat also reads).

Coordinates are unrotated page coordinates (the ones PyMuPDF uses for widgets).
"""
from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET

import pymupdf as fitz

from pdfdesk import appearance, safety

KINDS = {
    "text": fitz.PDF_WIDGET_TYPE_TEXT, "check": fitz.PDF_WIDGET_TYPE_CHECKBOX,
    "radio": fitz.PDF_WIDGET_TYPE_RADIOBUTTON, "combo": fitz.PDF_WIDGET_TYPE_COMBOBOX,
    "list": fitz.PDF_WIDGET_TYPE_LISTBOX, "signature": fitz.PDF_WIDGET_TYPE_SIGNATURE,
}
KIND_LABELS = {"text": "Text field", "check": "Check box", "radio": "Radio button", "combo": "Drop-down",
               "list": "List box", "signature": "Signature field"}
DEFAULT_SIZE = {"text": (160, 20), "check": (14, 14), "radio": (14, 14), "combo": (140, 20), "list": (140, 60),
                "signature": (180, 44)}
NAME_PREFIX = {"text": "Text", "check": "Check", "radio": "Choice", "combo": "Dropdown", "list": "List",
               "signature": "Signature"}

FLAG_READ_ONLY = 1
FLAG_REQUIRED = 2
MAX_IMPORT_BYTES = 5 * 1024 * 1024


def kind_of(field_type: int) -> str:
    for k, v in KINDS.items():
        if v == field_type:
            return k
    return "text"


def clean_name(text: str, fallback: str = "Field") -> str:
    """A safe, readable field name from a label: letters, digits and underscores."""
    name = re.sub(r"[^\w]+", "_", text or "", flags=re.UNICODE).strip("_")
    return (name or fallback)[:60]


def all_names(doc: fitz.Document) -> set[str]:
    names = set()
    for page in doc:
        for w in page.widgets():
            if w.field_name:
                names.add(w.field_name)
    return names


def unique_name(doc: fitz.Document, base: str, taken: set[str] | None = None) -> str:
    taken = taken if taken is not None else all_names(doc)
    base = clean_name(base)
    if base not in taken:
        return base
    k = 2
    while f"{base}_{k}" in taken:
        k += 1
    return f"{base}_{k}"


def _new_widget(kind: str, rect: fitz.Rect, name: str) -> fitz.Widget:
    w = fitz.Widget()
    w.field_type = KINDS[kind]
    w.field_name = name
    w.rect = fitz.Rect(rect)
    w.border_color = (0.35, 0.35, 0.4)
    w.border_width = 1
    w.text_font = "Helv"
    w.text_fontsize = 0  # auto size
    if kind in ("check", "radio"):
        w.field_value = False
    if kind in ("combo", "list"):
        w.choice_values = ["Option 1", "Option 2", "Option 3"]
    return w


def add_field(page: fitz.Page, rect: fitz.Rect, kind: str, name: str | None = None) -> int:
    doc = page.parent
    name = unique_name(doc, name or NAME_PREFIX[kind] + "1")
    widget = page.add_widget(_new_widget(kind, rect, name))
    return widget.xref


def add_radio_group(page: fitz.Page, rects: list[fitz.Rect], name: str | None = None,
                    values: list[str] | None = None) -> list[int]:
    """Radio buttons where only one can be chosen: one field with several buttons."""
    doc = page.parent
    name = unique_name(doc, name or "Choice1")
    values = values or [f"Option{k + 1}" for k in range(len(rects))]
    xrefs = []
    for k, rect in enumerate(rects):
        w = _new_widget("radio", rect, f"{name}__tmp{k}")
        xrefs.append(page.add_widget(w).xref)
    _group_radios(doc, xrefs, name, [clean_name(v, f"Option{k + 1}") for k, v in enumerate(values)])
    return xrefs


REF = re.compile(r"(\d+)\s+(\d+)\s+R")


def _fields_array(doc: fitz.Document) -> tuple[tuple, list[tuple[str, str]]]:
    """Where the form's /Fields list lives, and its (number, generation) entries. The list can be
    inside the AcroForm dictionary or an object of its own."""
    cat = doc.pdf_catalog()
    t, v = doc.xref_get_key(cat, "AcroForm")
    holder, key = (int(v.split()[0]), "Fields") if t == "xref" else (cat, "AcroForm/Fields")
    t, arr = doc.xref_get_key(holder, key)
    if t == "xref":
        obj = int(arr.split()[0])
        return ("obj", obj), REF.findall(doc.xref_object(obj, compressed=True)[:5_000_000])
    return ("key", holder, key), REF.findall(arr[:5_000_000]) if t == "array" else []


def _write_fields(doc: fitz.Document, where: tuple, refs: list[tuple[str, str]]) -> None:
    text = "[" + " ".join(f"{n} {g} R" for n, g in refs) + "]"
    if where[0] == "obj":
        doc.update_object(where[1], text)
    else:
        doc.xref_set_key(where[1], where[2], text)


def _make_kid(doc: fitz.Document, x: int, parent: int, value: str) -> None:
    on = doc.xref_get_key(x, "AP/N/Yes")[1]
    off = doc.xref_get_key(x, "AP/N/Off")[1]
    for key in ("T", "FT", "Ff", "V"):
        doc.xref_set_key(x, key, "null")
    doc.xref_set_key(x, "Parent", f"{parent} 0 R")
    if on != "null" and off != "null":
        doc.xref_set_key(x, "AP/N", f"<</Off {off}/{value} {on}>>")
    doc.xref_set_key(x, "AP/D", "null")
    doc.xref_set_key(x, "AS", "/Off")


def _drop_from_fields(doc: fitz.Document, xrefs: list[int], add: int | None = None) -> None:
    where, refs = _fields_array(doc)
    drop = {(str(x), "0") for x in xrefs}
    keep = [r for r in refs if r not in drop] + ([(str(add), "0")] if add else [])
    _write_fields(doc, where, keep)


def _group_radios(doc: fitz.Document, xrefs: list[int], name: str, values: list[str]) -> int:
    parent = doc.get_new_xref()
    kids = " ".join(f"{x} 0 R" for x in xrefs)
    doc.update_object(parent, f"<</FT/Btn/Ff 49152/T{appearance.pdf_string(name)}/V/Off/Kids[{kids}]>>")
    for x, value in zip(xrefs, values):
        _make_kid(doc, x, parent, value)
    _drop_from_fields(doc, xrefs, add=parent)
    return parent


def radio_groups(doc: fitz.Document) -> dict[str, int]:
    """Radio button groups: {group name: xref of the group's field}."""
    groups = {}
    for page in doc:
        for w in page.widgets(types=[fitz.PDF_WIDGET_TYPE_RADIOBUTTON]):
            t, v = doc.xref_get_key(w.xref, "Parent")
            if t == "xref" and w.field_name:
                groups.setdefault(w.field_name, int(v.split()[0]))
    return groups


def add_radio_button(page: fitz.Page, rect: fitz.Rect, group: str, value: str) -> int:
    """Add one radio button to `group` (created if needed). `value` is what the group is set to when
    this button is chosen."""
    doc = page.parent
    group = clean_name(group, "Choice")
    value = clean_name(value, "Option1")
    groups = radio_groups(doc)
    xref = page.add_widget(_new_widget("radio", rect, unique_name(doc, "PDFDeskRadioTmp"))).xref
    if group in groups:
        parent = groups[group]
        t, kids = doc.xref_get_key(parent, "Kids")
        refs = REF.findall(kids) if t == "array" else []
        used = set()
        for n, _g in refs:
            t2, states = doc.xref_get_key(int(n), "AP/N")
            used |= set(re.findall(r"/([^\s/<>\[\]()]+)\s", states[:2000])) - {"Off"}
        base, k = value, 2
        while value in used:  # two buttons with the same value would switch on and off together
            value, k = f"{base}_{k}", k + 1
        doc.xref_set_key(parent, "Kids", "[" + " ".join(f"{n} {g} R" for n, g in refs + [(str(xref), "0")]) + "]")
        _make_kid(doc, xref, parent, value)
        _drop_from_fields(doc, [xref])
    else:
        if group in all_names(doc):
            group = unique_name(doc, group)
        _group_radios(doc, [xref], group, [value])
    return xref


def find_widget(page: fitz.Page, xref: int):
    for w in page.widgets():
        if w.xref == xref:
            return w
    return None


def _button_states(doc: fitz.Document, w) -> list[tuple[int, str, str]]:
    """The on/off state of a check box or radio button (and the rest of its group), so redrawing a
    button can't switch it on by accident."""
    saved = []
    t, parent = doc.xref_get_key(w.xref, "Parent")
    group = [w.xref]
    if t == "xref":
        pxref = int(parent.split()[0])
        saved.append((pxref, "V", doc.xref_get_key(pxref, "V")[1]))
        t2, kids = doc.xref_get_key(pxref, "Kids")
        group = [int(n) for n, _g in REF.findall(kids)] if t2 == "array" else group
    saved.append((w.xref, "V", doc.xref_get_key(w.xref, "V")[1]))
    for x in group:
        saved.append((x, "AS", doc.xref_get_key(x, "AS")[1]))
    return saved


def _restore(doc: fitz.Document, saved: list[tuple[int, str, str]]) -> None:
    for xref, key, value in saved:
        doc.xref_set_key(xref, key, value if value else "null")


def move_field(page: fitz.Page, xref: int, rect: fitz.Rect) -> None:
    w = find_widget(page, xref)
    if w is None:
        return
    if w.field_type in (fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.PDF_WIDGET_TYPE_RADIOBUTTON):
        appearance.move_annot(page, xref, rect)  # just move it: redrawing would switch it on
        return
    w.rect = fitz.Rect(rect)
    w.update()


def delete_field(page: fitz.Page, xref: int) -> None:
    w = find_widget(page, xref)
    if w is not None:
        page.delete_widget(w)


# --------------------------------------------------------------------------- properties

def properties(page: fitz.Page, xref: int) -> dict:
    w = find_widget(page, xref)
    if w is None:
        return {}
    kind = kind_of(w.field_type)
    props = {
        "kind": kind, "name": w.field_name or "", "tooltip": w.field_label or "",
        "required": bool(w.field_flags & FLAG_REQUIRED), "read_only": bool(w.field_flags & FLAG_READ_ONLY),
        "font_size": float(w.text_fontsize or 0), "border_color": w.border_color, "fill_color": w.fill_color,
        "text_color": w.text_color, "border_width": float(w.border_width or 0),
    }
    if kind == "text":
        props["multiline"] = bool(w.field_flags & fitz.PDF_TX_FIELD_IS_MULTILINE)
        props["max_length"] = int(w.text_maxlen or 0)
        props["value"] = w.field_value or ""
        props["align"] = _quadding(page.parent, xref)
    if kind in ("combo", "list"):
        props["choices"] = [str(c[1] if isinstance(c, (list, tuple)) else c) for c in (w.choice_values or [])]
        props["value"] = w.field_value or ""
        props["editable"] = bool(w.field_flags & fitz.PDF_CH_FIELD_IS_EDIT)
    if kind in ("check", "radio"):
        props["export"] = w.on_state() or "Yes"
    return props


def _quadding(doc, xref) -> int:
    t, v = doc.xref_get_key(xref, "Q")
    return int(v) if t == "int" and v in ("0", "1", "2") else 0


def set_properties(page: fitz.Page, xref: int, props: dict) -> None:
    doc = page.parent
    w = find_widget(page, xref)
    if w is None:
        return
    kind = kind_of(w.field_type)
    saved = _button_states(doc, w) if kind in ("check", "radio") else []
    new_name = clean_name(props.get("name") or w.field_name)
    if new_name != w.field_name and kind != "radio":
        w.field_name = unique_name(doc, new_name, all_names(doc) - {w.field_name})
    w.field_label = (props.get("tooltip") or "")[:200]
    flags = w.field_flags or 0
    flags = (flags | FLAG_REQUIRED) if props.get("required") else (flags & ~FLAG_REQUIRED)
    flags = (flags | FLAG_READ_ONLY) if props.get("read_only") else (flags & ~FLAG_READ_ONLY)
    if kind == "text":
        multi = fitz.PDF_TX_FIELD_IS_MULTILINE
        flags = (flags | multi) if props.get("multiline") else (flags & ~multi)
        w.text_maxlen = max(0, min(10000, int(props.get("max_length") or 0)))
    if kind in ("combo", "list"):
        choices = [c.strip()[:200] for c in props.get("choices") or [] if c.strip()][:500]
        w.choice_values = choices or ["Option 1"]
        edit = fitz.PDF_CH_FIELD_IS_EDIT
        if kind == "combo":
            flags = (flags | edit) if props.get("editable") else (flags & ~edit)
    w.field_flags = flags
    if props.get("font_size") is not None:
        w.text_fontsize = max(0.0, min(72.0, float(props["font_size"])))
    for key in ("border_color", "fill_color", "text_color"):
        if key in props:
            setattr(w, key, props[key])
    if props.get("border_width") is not None:
        w.border_width = max(0.0, min(12.0, float(props["border_width"])))
    w.update()
    if kind == "text" and "align" in props:
        doc.xref_set_key(xref, "Q", str(max(0, min(2, int(props["align"])))))
        w = find_widget(page, xref)
        w.update()
    if saved:
        _restore(doc, saved)
    if kind == "radio":
        # required / read-only belong to the group, so other viewers honour them too
        t, parent = doc.xref_get_key(xref, "Parent")
        if t == "xref":
            pxref = int(parent.split()[0])
            t2, ff = doc.xref_get_key(pxref, "Ff")
            pflags = int(ff) if t2 == "int" else 49152
            for bit, on in ((FLAG_REQUIRED, props.get("required")), (FLAG_READ_ONLY, props.get("read_only"))):
                pflags = (pflags | bit) if on else (pflags & ~bit)
            doc.xref_set_key(pxref, "Ff", str(pflags))


# --------------------------------------------------------------------------- finding fields automatically

def _words_left_of(words: list, rect: fitz.Rect) -> str:
    cy = (rect.y0 + rect.y1) / 2
    row = [w for w in words if w[1] - 3 <= cy <= w[3] + 3 and w[2] <= rect.x0 + 2 and rect.x0 - w[2] < 220]
    row.sort(key=lambda w: w[0])
    text = " ".join(w[4] for w in row[-6:])
    return text.strip(" :_.")


def _words_above(words: list, rect: fitz.Rect) -> str:
    row = [w for w in words if 0 <= rect.y0 - w[3] < 16 and w[0] < rect.x1 and w[2] > rect.x0]
    row.sort(key=lambda w: w[0])
    return " ".join(w[4] for w in row[:6]).strip(" :_.")


MAX_DETECTED = 400      # fields suggested per page
MAX_CANDIDATES = 1500   # lines and boxes looked at per page


def detect_fields(page: fitz.Page) -> list[tuple[str, fitz.Rect, str]]:
    """Guess where a form wants answers: runs of underscores, blank lines to write on, and little
    boxes to tick. Returns [(kind, rect, name)]. Places that already have a field are skipped."""
    found: list[tuple[str, fitz.Rect, str]] = []
    existing = [fitz.Rect(w.rect) for w in page.widgets()]
    words = page.get_text("words", flags=fitz.TEXT_PRESERVE_WHITESPACE)
    text_words = [w for w in words if not re.fullmatch(r"_+", w[4])]

    def free(rect: fitz.Rect) -> bool:
        if any((rect & e).get_area() > 0.3 * min(rect.get_area(), e.get_area()) for e in existing):
            return False
        if any((rect & f[1]).get_area() > 0.3 * rect.get_area() for f in found):
            return False
        return True

    def add(kind: str, rect: fitz.Rect, label: str) -> None:
        if len(found) >= MAX_DETECTED or rect.width < 6 or rect.height < 6 or not free(rect):
            return
        found.append((kind, rect, clean_name(label, NAME_PREFIX[kind])))

    # 1. runs of underscores in the text: "Name: ________"
    for blk in page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]:
        for line in blk.get("lines", []):
            for span in line.get("spans", []):
                chars = span.get("chars", [])
                k = 0
                while k < len(chars):
                    if chars[k]["c"] == "_":
                        j = k
                        while j < len(chars) and chars[j]["c"] == "_":
                            j += 1
                        if j - k >= 4:
                            r = fitz.Rect(chars[k]["bbox"]) | fitz.Rect(chars[j - 1]["bbox"])
                            h = max(14.0, span["size"] * 1.35)
                            rect = fitz.Rect(r.x0, r.y1 - h, r.x1, r.y1 + 1)
                            add("text", rect, _words_left_of(text_words, rect) or _words_above(text_words, rect))
                        k = j
                    else:
                        if chars[k]["c"] in "☐□❏❑":  # ballot box characters
                            r = fitz.Rect(chars[k]["bbox"])
                            side = max(8.0, min(r.width, r.height))
                            rect = fitz.Rect(r.x0, r.y1 - side, r.x0 + side, r.y1)
                            label = _words_after(text_words, rect)
                            add("check", rect, label)
                        k += 1

    # 2. lines drawn to write on, and small square boxes to tick
    try:
        drawings = page.get_drawings()
    except Exception:
        drawings = []
    budget = 5000  # very busy pages (maps, charts) are not forms: look at a limited number of pieces
    kept = []
    for d in drawings:
        budget -= len(d.get("items", []))
        if budget < 0:
            break
        kept.append(d)
    drawings = kept
    lines, squares = [], []
    for d in drawings:
        for item in d.get("items", []):
            if item[0] == "l":
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 0.6 and abs(p1.x - p2.x) >= 50:
                    lines.append(fitz.Rect(min(p1.x, p2.x), p1.y, max(p1.x, p2.x), p1.y))
            elif item[0] == "re":
                r = fitz.Rect(item[1])
                if 6 <= r.width <= 22 and 6 <= r.height <= 22 and abs(r.width - r.height) < 2.5:
                    squares.append(r)
                elif r.height <= 1.2 and r.width >= 50:
                    lines.append(fitz.Rect(r.x0, r.y1, r.x1, r.y1))
    lines, squares = lines[:MAX_CANDIDATES], squares[:MAX_CANDIDATES]
    verticals: dict[int, list] = {}   # vertical lines by their x position (rounded)
    for d in drawings:
        for item in d.get("items", []):
            if item[0] == "l" and abs(item[1].x - item[2].x) < 0.6 and abs(item[1].y - item[2].y) > 6:
                a, b = item[1], item[2]
                verticals.setdefault(round(a.x), []).append((a.x, min(a.y, b.y), max(a.y, b.y)))

    def touching(x: float, y: float) -> int:
        return sum(1 for k in range(round(x) - 3, round(x) + 4) for vx, y0, y1 in verticals.get(k, ())
                   if abs(vx - x) < 2 and y0 - 2 <= y <= y1 + 2)

    for ln in lines:
        # lines that are part of a table grid have vertical lines touching them
        if touching(ln.x0, ln.y0) + touching(ln.x1, ln.y0) >= 2:
            continue
        rect = fitz.Rect(ln.x0, ln.y0 - 16, ln.x1, ln.y0 - 0.5)
        if any(fitz.Rect(w[:4]).intersects(rect) for w in text_words):
            continue  # something is already written on it
        add("text", rect, _words_left_of(text_words, rect) or _words_above(text_words, rect))
    for sq in squares:
        if any(fitz.Rect(w[:4]).intersects(sq) for w in text_words):
            continue
        add("check", fitz.Rect(sq), _words_after(text_words, sq))
    found.sort(key=lambda f: (round(f[1].y0 / 4), f[1].x0))
    return found


def _words_after(words: list, rect: fitz.Rect) -> str:
    cy = (rect.y0 + rect.y1) / 2
    row = [w for w in words if w[1] - 3 <= cy <= w[3] + 3 and w[0] >= rect.x1 - 1 and w[0] - rect.x1 < 120]
    row.sort(key=lambda w: w[0])
    return " ".join(w[4] for w in row[:5]).strip(" :.")


def add_detected(page: fitz.Page, fields: list[tuple[str, fitz.Rect, str]]) -> int:
    doc = page.parent
    taken = all_names(doc)
    count = 0
    for kind, rect, name in fields:
        name = unique_name(doc, name, taken)
        taken.add(name)
        page.add_widget(_new_widget(kind, rect, name))
        count += 1
    return count


# --------------------------------------------------------------------------- form data

def _value_text(w) -> str:
    v = w.field_value
    if v is None or v is False:
        return "Off" if w.field_type in (fitz.PDF_WIDGET_TYPE_CHECKBOX, fitz.PDF_WIDGET_TYPE_RADIOBUTTON) else ""
    if v is True:
        return w.on_state() or "Yes"
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return str(v)


def collect_values(doc: fitz.Document) -> dict[str, str]:
    values: dict[str, str] = {}
    for page in doc:
        for w in page.widgets():
            if not w.field_name or w.field_type in (fitz.PDF_WIDGET_TYPE_BUTTON, fitz.PDF_WIDGET_TYPE_SIGNATURE):
                continue
            text = _value_text(w)
            if w.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON:
                if text != "Off" or w.field_name not in values:
                    values[w.field_name] = text
            else:
                values.setdefault(w.field_name, text)
    return values


def export_data(doc: fitz.Document, path: str, pdf_name: str = "") -> int:
    values = collect_values(doc)
    low = path.lower()
    if low.endswith(".csv"):
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["Field", "Value"])
        for k, v in values.items():
            writer.writerow([safety.csv_cell(k), safety.csv_cell(v)])  # never live spreadsheet formulas
        data = buf.getvalue().encode("utf-8-sig")
    elif low.endswith(".xfdf"):
        root = ET.Element("xfdf", {"xmlns": "http://ns.adobe.com/xfdf/", "xml:space": "preserve"})
        fields = ET.SubElement(root, "fields")
        for k, v in values.items():
            f = ET.SubElement(fields, "field", {"name": k})
            ET.SubElement(f, "value").text = v
        if pdf_name:
            ET.SubElement(root, "f", {"href": pdf_name})
        data = b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")
    else:
        data = json.dumps(values, ensure_ascii=False, indent=2).encode("utf-8")
    with open(path, "wb") as fh:
        fh.write(data)
    return len(values)


def read_data(path: str) -> dict[str, str]:
    """Read field values from JSON, CSV or XFDF. Untrusted input: size-limited, plain values only."""
    with open(path, "rb") as fh:
        raw = fh.read(MAX_IMPORT_BYTES + 1)
    if len(raw) > MAX_IMPORT_BYTES:
        raise ValueError("The file is too large to be form data.")
    low = path.lower()
    out: dict[str, str] = {}
    if low.endswith(".csv"):
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig", errors="replace"))))
        for row in rows[1:] if rows and [c.lower() for c in rows[0][:2]] == ["field", "value"] else rows:
            if len(row) >= 2 and row[0]:
                out[safety.csv_uncell(row[0])[:200]] = safety.csv_uncell(row[1])[:10000]
    elif low.endswith((".xfdf", ".xml")):
        _reject_dtd(raw)
        root = ET.fromstring(raw)
        for f in root.iter():
            if f.tag.endswith("field") and f.get("name"):
                val = next((c for c in f if c.tag.endswith("value")), None)
                if val is not None:
                    out[f.get("name")[:200]] = (val.text or "")[:10000]
    else:
        data = json.loads(raw.decode("utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("The JSON file should contain field names and values.")
        for k, v in data.items():
            if isinstance(v, (str, int, float, bool)):
                out[str(k)[:200]] = str(v)[:10000] if not isinstance(v, bool) else ("Yes" if v else "Off")
    return out


def _reject_dtd(raw: bytes) -> None:
    """XFDF never needs a document type definition, and one can make an XML file expand to gigabytes.
    The XML parser itself (which understands every text encoding) stops at the first one."""
    import xml.parsers.expat as expat

    def refuse(*_args):
        raise ValueError("This XFDF file contains document type definitions, which are not allowed.")
    parser = expat.ParserCreate()
    parser.StartDoctypeDeclHandler = refuse
    parser.EntityDeclHandler = refuse
    try:
        parser.Parse(raw, True)
    except expat.ExpatError as exc:
        raise ValueError(f"This isn't a valid XFDF file ({exc}).")


def apply_values(doc: fitz.Document, values: dict[str, str]) -> int:
    from pdfdesk import annots
    changed = 0
    for page in doc:
        for w in list(page.widgets()):
            name = w.field_name
            if name not in values or w.field_flags & FLAG_READ_ONLY:
                continue
            value = values[name]
            t = w.field_type
            try:
                if t == fitz.PDF_WIDGET_TYPE_CHECKBOX:
                    on = value not in ("", "Off", "off", "false", "False", "0", "No")
                    w.field_value = (w.on_state() or "Yes") if on else "Off"
                    annots.update_value(w)
                elif t == fitz.PDF_WIDGET_TYPE_RADIOBUTTON:
                    if value == (w.on_state() or ""):
                        annots.select_radio(page, w)
                    else:
                        continue
                elif t == fitz.PDF_WIDGET_TYPE_TEXT:
                    annots.set_text_field(doc, page, w, value)
                elif t in (fitz.PDF_WIDGET_TYPE_COMBOBOX, fitz.PDF_WIDGET_TYPE_LISTBOX):
                    w.field_value = value
                    annots.update_value(w)
                else:
                    continue
                changed += 1
            except Exception:
                continue
    return changed


def reset_form(doc: fitz.Document) -> int:
    """Clear every field back to its default (empty, unticked)."""
    count = 0
    for page in doc:
        for w in list(page.widgets()):
            if w.field_type in (fitz.PDF_WIDGET_TYPE_BUTTON, fitz.PDF_WIDGET_TYPE_SIGNATURE):
                continue
            before = _value_text(w)
            try:
                w.reset()
            except Exception:
                continue
            if before not in ("", "Off"):
                count += 1
    return count
