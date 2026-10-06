"""Digital IDs, signing and signature checks (all offline). Run with:  python -m pytest -q tests"""
import os
import socket
import sys
import tempfile

import pymupdf as fitz
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("PDFDESK_HOME", tempfile.mkdtemp(prefix="pdfdesk-test-home-"))

from pdfdesk import digitalid as DI  # noqa: E402

pytestmark = pytest.mark.skipif(not DI.available(), reason="pyHanko is not installed")


@pytest.fixture()
def no_network(monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("PDF Desk tried to use the network")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


def pdf_bytes(rotated=False) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Contract between A and B ș")
    if rotated:
        page.set_rotation(90)
    return doc.tobytes()


def test_create_sign_and_check(tmp_path, no_network):
    p12 = DI.create_id("Ana Pop ș", "ana@example.com", "Acme", "secret12")
    if os.name != "nt":
        assert oct(os.stat(p12).st_mode & 0o777) == "0o600"
        assert oct(os.stat(os.path.dirname(p12)).st_mode & 0o777) == "0o700"
    ids = DI.list_ids()
    assert any(i["name"] == "Ana Pop ș" and i["self_signed"] for i in ids)
    with pytest.raises(ValueError):
        DI.sign(pdf_bytes(), p12, "wrong")
    data = pdf_bytes(rotated=True)
    signed = DI.sign(data, p12, "secret12", page=0, vis_rect=fitz.Rect(72, 300, 300, 360), reason="I approve")
    assert signed.startswith(data)  # signing only appends to the file
    twice = DI.sign(signed, p12, "secret12")  # a second, invisible signature
    results = DI.validate(twice)
    assert [r["field"] for r in results] == ["Signature1", "Signature2"]
    assert all(r["intact"] and r["valid"] and r["trusted"] for r in results)
    assert results[0]["changed_after"] and not results[1]["changed_after"]
    assert DI.describe(results[1])[0] == "good"
    assert results[0]["reason"] == "I approve" and results[0]["name"] == "Ana Pop ș"
    DI.cleanup()


def test_tampering_is_detected(no_network):
    p12 = DI.create_id("Bob", "", "", "secret12")
    signed = DI.sign(pdf_bytes(), p12, "secret12")
    k = signed.find(b"Contract between")
    if k < 0:  # the page text is compressed; change a byte inside the signed range instead
        k = len(signed) // 3
    broken = signed[:k] + bytes([signed[k] ^ 1]) + signed[k + 1:]
    results = DI.validate(broken)
    assert results and DI.describe(results[0])[0] == "bad"


def test_untrusted_and_imported_ids(tmp_path, no_network):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
    p12 = DI.create_id("Carla", "", "", "secret12")
    # someone else's view: the certificate isn't in their trusted list
    for f in DI.trusted_dir().glob("*.crt"):
        f.unlink()
    result = DI.validate(DI.sign(pdf_bytes(), p12, "secret12"))[0]
    assert result["intact"] and result["valid"] and not result["trusted"]
    assert DI.describe(result)[0] == "warn"
    DI.trust_certificate(result["certificate"])
    assert DI.validate(DI.sign(pdf_bytes(), p12, "secret12"))[0]["trusted"]
    # import a .p12 made elsewhere
    key, cert, _ = pkcs12.load_key_and_certificates(open(p12, "rb").read(), b"secret12")
    other = tmp_path / "other.pfx"
    other.write_bytes(pkcs12.serialize_key_and_certificates(b"x", key, cert, None,
                                                            serialization.BestAvailableEncryption(b"pw123456")))
    with pytest.raises(ValueError):
        DI.import_id(str(other), "nope")
    stored = DI.import_id(str(other), "pw123456")
    assert DI.check_password(stored, "pw123456") and not DI.check_password(stored, "nope")
    DI.delete_id(stored)
    assert not os.path.exists(stored)
    with pytest.raises(ValueError):
        DI.delete_id(str(other))  # only IDs in PDF Desk's own folder can be deleted


def test_saving_signed_documents_keeps_signatures(tmp_path, no_network):
    from pdfdesk.document import PdfDocument
    p12 = DI.create_id("Dan", "", "", "secret12")
    path = tmp_path / "signed.pdf"
    path.write_bytes(DI.sign(pdf_bytes(), p12, "secret12"))
    pdf = PdfDocument.open(str(path))
    assert pdf.signed_bytes and pdf.keeps_signatures()
    with pdf.edit("note", pages=[0]):
        pdf.page(0).add_text_annot((200, 200), "a comment")
    pdf.undo()
    pdf.redo()
    assert pdf.keeps_signatures()
    pdf.save()
    result = DI.validate(path.read_bytes())[0]
    assert result["intact"] and result["valid"] and result["changed_after"]
    # rewriting the whole file (for example "Reduce file size") would break them: the app warns first
    pdf.replace_with_bytes(pdf.doc.tobytes(garbage=3), "rewrite")
    assert pdf.will_break_signatures()


# --------------------------------------------------------------------------- review round 2 (signatures)

def _update(signed: bytes, change) -> bytes:
    """Append an update made with pyHanko (how someone else's tool would change a signed file)."""
    import io
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    w = IncrementalPdfFileWriter(io.BytesIO(signed))
    change(w)
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def test_replaced_page_content_is_not_valid(no_network):
    from pyhanko.pdf_utils import generic
    p12 = DI.create_id("Ana Pop", "", "", "secret12")
    signed = DI.sign(pdf_bytes(), p12, "secret12")

    def swap(w):
        page = w.root["/Pages"]["/Kids"][0].get_object()
        page["/Contents"] = w.add_object(generic.StreamObject(stream_data=b"BT /F1 20 Tf 72 720 Td (Pay Mallory) Tj ET"))
        w.update_container(page)
    tampered = _update(signed, swap)
    item = DI.validate(tampered)[0]
    assert item["intact"] and item["modification"] == "OTHER"
    level, text = DI.describe(item)
    assert level == "bad" and "page 1" in text
    original = DI.signed_version(tampered, item)
    assert original == signed and "Contract" in fitz.open("pdf", original)[0].get_text()


def test_comments_after_signing_are_reported(tmp_path, no_network):
    from pdfdesk import sigdiff
    from pdfdesk.document import PdfDocument
    p12 = DI.create_id("Ana Pop", "", "", "secret12")
    path = tmp_path / "s.pdf"
    path.write_bytes(DI.sign(pdf_bytes(), p12, "secret12"))
    pdf = PdfDocument.open(str(path))
    with pdf.edit("note", pages=[0]):
        pdf.page(0).add_text_annot((200, 200), "a comment")
        pdf.page(0).add_highlight_annot(fitz.Rect(70, 55, 200, 80))
    assert sigdiff.update_problem(pdf.base_bytes, pdf.save_bytes()) == ""
    item = DI.validate(pdf.save_bytes())[0]
    assert item["modification"] == "ANNOTATIONS"
    assert DI.describe(item)[0] == "warn" and any("comment" in n for n in item["notes"])
    with pdf.edit("text", pages=[0]):
        pdf.page(0).insert_text((72, 300), "extra line")
    assert "INVALID" in sigdiff.update_problem(pdf.base_bytes, pdf.save_bytes())
    assert DI.describe(DI.validate(pdf.save_bytes())[0])[0] == "bad"


def test_repeated_updates_stay_well_formed(tmp_path, no_network):
    import re
    from pdfdesk.document import PdfDocument
    p12 = DI.create_id("Dan", "", "", "secret12")
    path = tmp_path / "s.pdf"
    path.write_bytes(DI.sign(pdf_bytes(), p12, "secret12"))
    pdf = PdfDocument.open(str(path))
    for k in range(3):
        with pdf.edit("note", pages=[0]):
            pdf.page(0).add_text_annot((100 + 60 * k, 200), f"c{k}")
        first, again = pdf.incremental_bytes(), pdf.incremental_bytes()
        for data in (first, again):
            assert not fitz.open("pdf", data).is_repaired
            prev = int(re.findall(rb"/Prev (\d+)", data)[-1])
            last = int(re.findall(rb"startxref\s+(\d+)", data)[-1])
            assert prev != last
        pdf.save()
    item = DI.validate(path.read_bytes())[0]
    assert item["intact"] and DI.describe(item)[0] in ("good", "warn") and item["modification"] == "ANNOTATIONS"


def test_form_filling_after_signing(tmp_path, no_network):
    from pdfdesk import annots, sigdiff
    from pdfdesk.document import PdfDocument
    p12 = DI.create_id("Eve", "", "", "secret12")
    doc = fitz.open()
    page = doc.new_page()
    for k, name in enumerate(("amount", "name")):
        w = fitz.Widget()
        w.field_type, w.field_name, w.rect = fitz.PDF_WIDGET_TYPE_TEXT, name, fitz.Rect(72, 100 + 50 * k, 300, 130 + 50 * k)
        page.add_widget(w)
    path = tmp_path / "form.pdf"
    path.write_bytes(DI.sign(doc.tobytes(), p12, "secret12", page=0, vis_rect=fitz.Rect(72, 400, 300, 460)))
    pdf = PdfDocument.open(str(path))
    with pdf.edit("fill", pages=[0]):
        pg = pdf.page(0)
        for w in pg.widgets(types=[fitz.PDF_WIDGET_TYPE_TEXT]):
            annots.set_text_field(pdf.doc, pg, w, "1000" if w.field_name == "amount" else "Ana ș")
    assert sigdiff.update_problem(pdf.base_bytes, pdf.save_bytes()) == ""
    item = DI.validate(pdf.save_bytes())[0]
    assert item["modification"] == "FORM_FILLING" and DI.describe(item)[0] == "warn"


def test_certified_document_allows_no_changes(tmp_path, no_network):
    import io
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields, signers
    from pdfdesk import sigdiff
    from pdfdesk.document import PdfDocument
    p12 = DI.create_id("Fay", "", "", "secret12")
    signer = signers.SimpleSigner.load_pkcs12(pfx_file=p12, passphrase=b"secret12")
    out = io.BytesIO()
    meta = signers.PdfSignatureMetadata(field_name="Cert1", certify=True, docmdp_permissions=fields.MDPPerm.NO_CHANGES)
    signers.PdfSigner(meta, signer=signer).sign_pdf(IncrementalPdfFileWriter(io.BytesIO(pdf_bytes())), output=out)
    path = tmp_path / "certified.pdf"
    path.write_bytes(out.getvalue())
    pdf = PdfDocument.open(str(path))
    with pdf.edit("note", pages=[0]):
        pdf.page(0).add_text_annot((200, 200), "a comment")
    assert "allowed no changes" in sigdiff.update_problem(pdf.base_bytes, pdf.save_bytes())
    item = DI.validate(pdf.save_bytes())[0]
    assert item["docmdp_ok"] is False and DI.describe(item)[0] == "bad"


def _cert(name, key, issuer_name, issuer_key, start, end):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    from cryptography.x509.oid import NameOID
    return (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
            .issuer_name(issuer_name).public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(start).not_valid_after(end)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                         crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
            .sign(issuer_key, hashes.SHA256()))


def test_trust_rules(tmp_path, no_network):
    import datetime as dt
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import NameOID
    now = dt.datetime.now(dt.timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def p12(name, k, cert, chain=None):
        path = tmp_path / f"{name}.p12"
        path.write_bytes(pkcs12.serialize_key_and_certificates(b"x", k, cert, chain,
                                                               serialization.BestAvailableEncryption(b"pw123456")))
        return str(path)
    old_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Old Otto")])
    old = _cert("Old Otto", key, old_name, key, now - dt.timedelta(days=800), now - dt.timedelta(days=400))
    DI.trust_certificate(old.public_bytes(serialization.Encoding.PEM))
    item = DI.validate(DI.sign(pdf_bytes(), p12("old", key, old), "pw123456"))[0]
    assert not item["trusted"] and item["cert_problem"] == "expired"
    assert "expired" in DI.describe(item)[1]
    # a trusted personal certificate can't vouch for someone else
    m_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Mallory")])
    mallory = _cert("Mallory", key, m_name, key, now - dt.timedelta(days=1), now + dt.timedelta(days=300))
    DI.trust_certificate(mallory.public_bytes(serialization.Encoding.DER))
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    fake = _cert("The Boss", other, m_name, key, now - dt.timedelta(days=1), now + dt.timedelta(days=300))
    item = DI.validate(DI.sign(pdf_bytes(), p12("fake", other, fake, [mallory]), "pw123456"))[0]
    assert not item["trusted"] and item["cert_problem"] == "issuer_cannot_vouch"
    assert DI.describe(item)[0] == "warn"


def test_signature_fields_anywhere_in_the_form(no_network):
    import io
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import signers
    from pdfdesk.document import has_signatures, signature_fields
    p12 = DI.create_id("Gus", "", "", "secret12")
    doc = fitz.open()
    page = doc.new_page()
    field, widget = doc.get_new_xref(), doc.get_new_xref()
    doc.update_object(widget, f"<</Type/Annot/Subtype/Widget/Rect[100 600 300 650]/F 4/Parent {field} 0 R"
                              f"/P {page.xref} 0 R>>")
    doc.update_object(field, f"<</FT/Sig/T(Sig1)/Kids[{widget} 0 R]>>")
    doc.xref_set_key(page.xref, "Annots", f"[{widget} 0 R]")
    form = doc.get_new_xref()
    doc.update_object(form, f"<</Fields[{field} 0 R]>>")   # no SigFlags
    doc.xref_set_key(doc.pdf_catalog(), "AcroForm", f"{form} 0 R")
    base = doc.tobytes()
    fields_before = signature_fields(fitz.open("pdf", base))
    assert [(f["name"], f["signed"], f["page"]) for f in fields_before] == [("Sig1", False, 0)]
    signer = signers.SimpleSigner.load_pkcs12(pfx_file=p12, passphrase=b"secret12")
    out = io.BytesIO()
    signers.PdfSigner(signers.PdfSignatureMetadata(field_name="Sig1"), signer=signer).sign_pdf(
        IncrementalPdfFileWriter(io.BytesIO(base)), output=out)
    signed = fitz.open("pdf", out.getvalue())
    assert has_signatures(signed) and signature_fields(signed)[0]["signed"]


def test_looping_file_is_refused_quickly(no_network):
    import re
    import threading
    p12 = DI.create_id("Hal", "", "", "secret12")
    base = DI.sign(pdf_bytes(), p12, "secret12")
    size = int(re.findall(rb"/Size (\d+)", base)[-1])
    root = re.findall(rb"/Root (\d+) 0 R", base)[-1]
    at = len(base) + 1
    looped = base + (b"\nxref\n0 1\n0000000000 65535 f \ntrailer\n<</Size %d/Root %s 0 R/Prev %d>>\nstartxref\n%d\n"
                     b"%%%%EOF\n" % (size, root, at, at))
    outcome = []

    def run():
        try:
            outcome.append(DI.validate(looped))
        except Exception as exc:
            outcome.append(exc)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(20)
    assert not t.is_alive(), "checking a looping file never finished"
    assert outcome and (isinstance(outcome[0], Exception) or all(DI.describe(r)[0] == "bad" for r in outcome[0]))


def test_imported_ids_are_rewrapped(tmp_path, no_network):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
    p12 = DI.create_id("Ida", "", "", "secret12")
    key, cert, _ = pkcs12.load_key_and_certificates(open(p12, "rb").read(), b"secret12")
    weak = tmp_path / "weak.p12"
    weak.write_bytes(pkcs12.serialize_key_and_certificates(b"x", key, cert, None, serialization.NoEncryption()))
    with pytest.raises(DI.ShortPassword):
        DI.import_id(str(weak), "")
    stored = DI.import_id(str(weak), "", "a long password")
    assert DI.check_password(stored, "a long password") and not DI.check_password(stored, "")
    with pytest.raises(ValueError):
        DI.create_id("Short", "", "", "1234567")


def test_signer_text_is_shown_on_one_line():
    assert DI.one_line("I agree\nSigned by Boss. You trust‮ them\t!") == \
        "I agree Signed by Boss. You trust them !"


def test_hidden_content_swaps_are_not_valid(no_network):
    """An object that is both a comment and part of the page, and an update that only another reader
    would see, must both make the signature not valid."""
    import re
    from pyhanko.pdf_utils import generic
    p12 = DI.create_id("Joe", "", "", "secret12")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Pay Bob $100", fontsize=20)
    annot = page.add_rect_annot(fitz.Rect(300, 300, 400, 400))
    ax = annot.xref
    doc.xref_set_key(ax, "ca", "1")
    contents = page.get_contents()[0]
    doc.update_stream(contents, b"/GSX gs\n" + doc.xref_stream(contents))
    kind, res = doc.xref_get_key(page.xref, "Resources")
    if kind == "xref":
        doc.xref_set_key(int(res.split()[0]), "ExtGState", f"<</GSX {ax} 0 R>>")
    else:
        doc.xref_set_key(page.xref, "Resources/ExtGState", f"<</GSX {ax} 0 R>>")
    signed = DI.sign(doc.tobytes(), p12, "secret12")

    def fade(w):
        shared = generic.Reference(ax, 0, w).get_object()
        shared["/ca"] = generic.FloatObject(0)   # the page text becomes invisible
        w.update_container(shared)
    item = DI.validate(_update(signed, fade))[0]
    assert DI.describe(item)[0] == "bad"
    # an update that pyHanko refuses to read (but a viewer would show) is not valid either
    cx = fitz.open("pdf", signed)[0].get_contents()[0]
    size = int(re.findall(rb"/Size (\d+)", signed)[-1])
    root = re.findall(rb"/Root (\d+) 0 R", signed)[-1]
    prev = int(re.findall(rb"startxref\s+(\d+)", signed)[-1])
    body = b"BT /F1 20 Tf 72 742 Td (Pay Mallory) Tj ET"
    obj = b"\n%d 1 obj\n<</Length %d>>\nstream\n%s\nendstream\nendobj\n" % (cx, len(body), body)
    data = bytearray(signed)
    at = len(data) + 1
    data += obj
    xref = len(data)
    data += (b"xref\n%d 1\n%010d 00001 n \ntrailer\n<</Size %d/Root %s 0 R/Prev %d>>\nstartxref\n%d\n%%%%EOF\n"
             % (cx, at, size, root, prev, xref))
    assert "Mallory" in fitz.open("pdf", bytes(data))[0].get_text()
    assert all(DI.describe(r)[0] == "bad" for r in DI.validate(bytes(data)))
