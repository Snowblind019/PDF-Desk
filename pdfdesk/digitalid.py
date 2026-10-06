"""Digital IDs and digital signatures (certificate-based, like Acrobat's "Sign with Digital ID").

Everything is offline:
  - A Digital ID is created on this computer (or imported from a .p12/.pfx file) and kept in PDF Desk's
    settings folder, readable only by you and protected by the password you choose.
  - Signing never contacts a time-stamp server or any other service.
  - Checking signatures never downloads anything: certificates you trust are added by you, and
    revocation lists are not fetched.

Signing uses pyHanko (https://github.com/MatthiasValvekens/pyHanko) and the "cryptography" package.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import io
import logging
import os
import re
import secrets
import sys
import tempfile
import unicodedata
from pathlib import Path

import pymupdf as fitz

from pdfdesk import appearance, fontcatalog, pdfops
from pdfdesk import richtext as RT
from pdfdesk.config import config_dir
from pdfdesk.document import signature_fields

IS_WINDOWS = sys.platform.startswith("win")
DOC_SIGNING_OID = "1.3.6.1.4.1.311.10.3.12"   # "Document Signing" (recognised by Acrobat and Windows)
for _name in ("pyhanko", "pyhanko_certvalidator", "pyhanko.sign", "pyhanko.sign.validation"):
    logging.getLogger(_name).setLevel(logging.CRITICAL)  # results are shown in the window, not the log


class NotAvailable(RuntimeError):
    pass


def available() -> bool:
    try:
        import pyhanko  # noqa: F401
        import cryptography  # noqa: F401
        return True
    except Exception:
        return False


def preload() -> None:
    """Import the signing libraries on the main thread before a background job uses them."""
    if available():
        from pdfdesk import sigdiff
        sigdiff.harden_pyhanko()
        import pyhanko.pdf_utils.incremental_writer  # noqa: F401
        import pyhanko.pdf_utils.reader  # noqa: F401
        import pyhanko.sign  # noqa: F401
        import pyhanko.sign.validation  # noqa: F401
        import pyhanko_certvalidator  # noqa: F401
        from pyhanko import stamp  # noqa: F401


def _require() -> None:
    if not available():
        raise NotAvailable("Digital signatures need the pyHanko package. Run the PDF Desk installer again to add it.")
    from pdfdesk import sigdiff
    sigdiff.harden_pyhanko()


# --------------------------------------------------------------------------- storage

def _private_dir(name: str) -> Path:
    path = config_dir() / name
    path.mkdir(parents=True, exist_ok=True)
    if not IS_WINDOWS:
        try:
            os.chmod(path, 0o700)
        except OSError:
            pass
    return path


def ids_dir() -> Path:
    return _private_dir("digital-ids")


def trusted_dir() -> Path:
    return _private_dir("trusted-certificates")


def _write_private(path: Path, data: bytes) -> None:
    """Create a file only this user can read (never follows an existing link)."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _safe_stem(text: str) -> str:
    stem = re.sub(r"[^\w\-]+", "_", text, flags=re.UNICODE).strip("_")[:40]
    return stem or "id"


def one_line(text, limit: int = 200) -> str:
    """Text from a certificate or a signature (written by the signer), made safe to show: control
    and invisible characters and line breaks removed, so it can't pose as PDF Desk's own verdict."""
    out = []
    for ch in str(text or ""):
        cat = unicodedata.category(ch)
        if cat in ("Zl", "Zp") or ch in "\t\r\n":
            out.append(" ")
        elif cat not in ("Cc", "Cf", "Co", "Cs"):
            out.append(ch)
    text = re.sub(r"\s{2,}", " ", "".join(out)).strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


MIN_PASSWORD = 8
KDF_ROUNDS = 600_000


def _protect(password: str):
    """PKCS#12 encryption with AES-256 and a slow (600,000 round) password hash, so a stolen ID file
    is very hard to unlock by guessing."""
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
    return (serialization.PrivateFormat.PKCS12.encryption_builder().kdf_rounds(KDF_ROUNDS)
            .key_cert_algorithm(pkcs12.PBES.PBESv2SHA256AndAES256CBC).hmac_hash(hashes.SHA256())
            .build(password.encode("utf-8")))


def _check_new_password(password: str) -> None:
    if len(password) < MIN_PASSWORD:
        raise ValueError(f"Use a password of at least {MIN_PASSWORD} characters.")


# --------------------------------------------------------------------------- certificates

def _cert_info(cert) -> dict:
    from cryptography.x509.oid import NameOID

    def attr(name, oid):
        try:
            vals = name.get_attributes_for_oid(oid)
            return vals[0].value if vals else ""
        except Exception:
            return ""
    subject, issuer = cert.subject, cert.issuer
    try:
        not_after = cert.not_valid_after_utc
    except AttributeError:
        not_after = cert.not_valid_after.replace(tzinfo=_dt.timezone.utc)
    from cryptography.hazmat.primitives import serialization
    der = cert.public_bytes(serialization.Encoding.DER)
    return {"name": str(attr(subject, NameOID.COMMON_NAME) or subject.rfc4514_string())[:120],
            "email": str(attr(subject, NameOID.EMAIL_ADDRESS))[:120],
            "org": str(attr(subject, NameOID.ORGANIZATION_NAME))[:120],
            "issuer": str(attr(issuer, NameOID.COMMON_NAME) or issuer.rfc4514_string())[:120],
            "self_signed": subject == issuer, "not_after": not_after,
            "fingerprint": hashlib.sha256(der).hexdigest()}


def list_ids() -> list[dict]:
    out = []
    if not available():
        return out
    from cryptography import x509
    for crt in sorted(ids_dir().glob("*.crt")):
        p12 = crt.with_suffix(".p12")
        if not p12.exists():
            continue
        try:
            cert = x509.load_pem_x509_certificate(crt.read_bytes())
        except Exception:
            continue
        info = _cert_info(cert)
        info["path"] = str(p12)
        info["cert_path"] = str(crt)
        out.append(info)
    return out


def create_id(name: str, email: str, org: str, password: str, years: int = 5) -> str:
    """Make a new self-signed Digital ID. Returns the path of the stored .p12 file."""
    _require()
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs12
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    name = name.strip()[:64]
    if not name:
        raise ValueError("Type your name.")
    _check_new_password(password)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, name)]
    if org.strip():
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_NAME, org.strip()[:64]))
    if email.strip():
        attrs.append(x509.NameAttribute(NameOID.EMAIL_ADDRESS, email.strip()[:120]))
    subject = x509.Name(attrs)
    now = _dt.datetime.now(_dt.timezone.utc)
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(now - _dt.timedelta(minutes=5))
               .not_valid_after(now + _dt.timedelta(days=365 * max(1, min(20, int(years)))))
               .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                                            data_encipherment=False, key_agreement=False, key_cert_sign=False,
                                            crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
               .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION,
                                                     x509.ObjectIdentifier(DOC_SIGNING_OID)]), critical=False)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False))
    if email.strip():
        builder = builder.add_extension(x509.SubjectAlternativeName([x509.RFC822Name(email.strip()[:120])]),
                                        critical=False)
    cert = builder.sign(key, hashes.SHA256())
    p12 = pkcs12.serialize_key_and_certificates(name.encode("utf-8"), key, cert, None, _protect(password))
    stem = f"{_safe_stem(name)}-{secrets.token_hex(3)}"
    folder = ids_dir()
    _write_private(folder / f"{stem}.p12", p12)
    _write_private(folder / f"{stem}.crt", cert.public_bytes(serialization.Encoding.PEM))
    trust_certificate(cert.public_bytes(serialization.Encoding.PEM))  # you trust your own ID
    return str(folder / f"{stem}.p12")


def import_id(path: str, password: str, new_password: str | None = None) -> str:
    """Copy a .p12/.pfx Digital ID into PDF Desk (after checking the password opens it). The key is
    stored again under new_password (or the same password), always with strong protection."""
    _require()
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.serialization import pkcs12
    data = Path(path).read_bytes()
    if len(data) > 1024 * 1024:
        raise ValueError("That file is too large to be a Digital ID.")
    try:
        key, cert, extra = pkcs12.load_key_and_certificates(data, password.encode("utf-8") if password else None)
    except Exception:
        raise ValueError("The password is wrong, or the file isn't a Digital ID (.p12 or .pfx).")
    if key is None or cert is None:
        raise ValueError("That file has no private key or certificate, so it can't be used to sign.")
    keep = new_password if new_password is not None else password
    if len(keep) < MIN_PASSWORD:
        raise ShortPassword(f"This Digital ID needs a new password of at least {MIN_PASSWORD} characters "
                            "to be kept in PDF Desk.")
    info = _cert_info(cert)
    stored = pkcs12.serialize_key_and_certificates(info["name"].encode("utf-8"), key, cert, extra or None,
                                                   _protect(keep))
    stem = f"{_safe_stem(info['name'])}-{secrets.token_hex(3)}"
    folder = ids_dir()
    _write_private(folder / f"{stem}.p12", stored)
    _write_private(folder / f"{stem}.crt", cert.public_bytes(serialization.Encoding.PEM))
    return str(folder / f"{stem}.p12")


class ShortPassword(ValueError):
    pass


def delete_id(p12_path: str) -> None:
    p = Path(p12_path)
    if p.parent.resolve() != ids_dir().resolve():
        raise ValueError("Not a PDF Desk Digital ID.")
    for f in (p, p.with_suffix(".crt")):
        try:
            f.unlink()
        except FileNotFoundError:
            pass


def check_password(p12_path: str, password: str) -> bool:
    _require()
    from cryptography.hazmat.primitives.serialization import pkcs12
    try:
        key, cert, _extra = pkcs12.load_key_and_certificates(Path(p12_path).read_bytes(), password.encode("utf-8"))
        return key is not None and cert is not None
    except Exception:
        return False


# --------------------------------------------------------------------------- trusted certificates

def trust_certificate(pem_or_der: bytes) -> str:
    """Add a certificate to the people you trust (used when checking signatures)."""
    _require()
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    try:
        cert = x509.load_pem_x509_certificate(pem_or_der)
    except Exception:
        cert = x509.load_der_x509_certificate(pem_or_der)
    pem = cert.public_bytes(serialization.Encoding.PEM)
    fp = hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()
    path = trusted_dir() / f"{fp[:32]}.crt"
    if not path.exists():
        _write_private(path, pem)
    return str(path)


def trusted_certificates() -> list:
    from asn1crypto import pem, x509 as a509
    out = []
    for f in sorted(trusted_dir().glob("*.crt")):
        try:
            data = f.read_bytes()
            if pem.detect(data):
                _t, _h, data = pem.unarmor(data)
            out.append(a509.Certificate.load(data))
        except Exception:
            continue
    return out


def export_certificate(p12_cert_path: str, target: str) -> None:
    """Save the public part of an ID (safe to share) so others can trust your signatures."""
    data = Path(p12_cert_path).read_bytes()
    with open(target, "wb") as fh:
        fh.write(data)


# --------------------------------------------------------------------------- signing

def signature_look(name: str, reason: str, location: str, when: _dt.datetime, width: float, height: float,
                   image: bytes | None = None, rotation: int = 0) -> bytes:
    """A one-page PDF with the visible part of a signature, drawn upright for a page turned by
    `rotation` degrees. Fonts are subset so the signature stays small."""
    font = fontcatalog.catalog().resolve("Noto Sans")
    vw, vh = (height, width) if rotation % 180 == 90 else (width, height)
    lines = [RT.Para([RT.Run("Digitally signed by", font, 7, color="#555555")]),
             RT.Para([RT.Run(name, font, 11, bold=True)]),
             RT.Para([RT.Run(when.strftime("%Y-%m-%d %H:%M %z").strip(), font, 7.5, color="#333333")])]
    if reason:
        lines.append(RT.Para([RT.Run(f"Reason: {reason}", font, 7.5, color="#333333")]))
    if location:
        lines.append(RT.Para([RT.Run(f"Location: {location}", font, 7.5, color="#333333")]))
    box = RT.Box(paras=lines, padding=3, auto_width=False)
    text_w = vw * (0.55 if image else 1.0)
    tmp, h = RT.render(box, max(30.0, text_w), vh)
    upright = fitz.open()
    page = upright.new_page(width=vw, height=vh)
    if image:
        try:
            page.insert_image(fitz.Rect(2, 2, vw * 0.45 - 2, vh - 2), stream=image, keep_proportion=True)
        except Exception:
            pass
    x0 = vw - text_w if image else 0
    page.show_pdf_page(fitz.Rect(x0, 0, vw, min(vh, h)), tmp, 0, keep_proportion=True)
    if rotation % 360 == 0:
        return upright.tobytes()
    final = fitz.open()
    fpage = final.new_page(width=width, height=height)
    fpage.show_pdf_page(fpage.rect, upright, 0, keep_proportion=False, rotate=rotation % 360)
    return final.tobytes()


def existing_signature_fields(doc: fitz.Document) -> list[dict]:
    """The document's signature fields (signed or empty), including ones not shown on any page."""
    return signature_fields(doc)


def sign(data: bytes, p12_path: str, password: str, *, page: int = 0, vis_rect: fitz.Rect | None = None,
         field_name: str | None = None, reason: str = "", location: str = "", contact: str = "",
         image: bytes | None = None, doc_password: str | None = None, progress=None) -> bytes:
    """Return the signed PDF (the original bytes plus a signed update).
    vis_rect (visible page coordinates) shows the signature on `page`; without it the signature is
    invisible. field_name signs an existing empty signature field instead."""
    _require()
    from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
    from pyhanko.sign import fields, signers
    reason, location, contact = one_line(reason, 200), one_line(location, 200), one_line(contact, 200)
    if progress:
        progress(0, 2, "Signing")
    try:
        signer = signers.SimpleSigner.load_pkcs12(pfx_file=p12_path, passphrase=password.encode("utf-8"))
    except Exception:
        signer = None
    if signer is None:
        raise ValueError("The Digital ID password is wrong.")
    name = _signer_name(signer)
    check = fitz.open("pdf", data)
    if check.needs_pass and not (doc_password and check.authenticate(doc_password)):
        raise ValueError("This PDF is locked with a password. Open it with its password, then sign it.")
    try:
        writer = IncrementalPdfFileWriter(io.BytesIO(data))
        if writer.prev.encrypted:
            writer.encrypt((doc_password or "").encode("utf-8"))
    except Exception:
        raise ValueError("PDF Desk can't sign this PDF while it is password protected. Use Tools > Remove "
                         "password, sign it, and then use Tools > Protect with password again.")
    existing = signature_fields(check)
    stamp_style = None
    if field_name:
        target = next((f for f in existing if f["name"] == field_name), None)
        if target is None:
            raise ValueError("That signature field wasn't found.")
        if target["signed"]:
            raise ValueError("That field is already signed.")
        rect = target["rect"]
        if target["page"] is not None and rect is not None and rect.width >= 4 and rect.height >= 4:
            pg = check[target["page"]]
            look = signature_look(name, reason, location, _dt.datetime.now().astimezone(), rect.width,
                                  rect.height, image, pg.rotation)
            stamp_style = _static_style(look)
    else:
        taken = {f["name"] for f in existing}
        field_name = next(f"Signature{k}" for k in range(1, 100000) if f"Signature{k}" not in taken)
        box = None
        if vis_rect is not None:
            pg = check[page]
            unrot = (fitz.Rect(vis_rect) * pg.derotation_matrix).normalize()
            pdf_box = appearance.page_rect_to_pdf(pg, unrot)
            box = (pdf_box.x0, pdf_box.y0, pdf_box.x1, pdf_box.y1)
            look = signature_look(name, reason, location, _dt.datetime.now().astimezone(), unrot.width,
                                  unrot.height, image, pg.rotation)
            stamp_style = _static_style(look)
        fields.append_signature_field(writer, fields.SigFieldSpec(sig_field_name=field_name, on_page=page, box=box))
    meta = signers.PdfSignatureMetadata(field_name=field_name, reason=reason or None,
                                        location=location or None, contact_info=contact or None,
                                        md_algorithm="sha256")
    out = io.BytesIO()
    pdf_signer = signers.PdfSigner(meta, signer=signer, stamp_style=stamp_style) if stamp_style else \
        signers.PdfSigner(meta, signer=signer)
    pdf_signer.sign_pdf(writer, output=out)
    if progress:
        progress(2, 2, "Signing")
    signed = out.getvalue()
    if not signed.startswith(data):
        raise RuntimeError("Signing changed the original file instead of adding to it.")
    return signed


_look_files: list[str] = []


def _static_style(look_pdf: bytes):
    from pyhanko import stamp
    fd, path = tempfile.mkstemp(prefix="pdfdesk-sig-", suffix=".pdf")
    with os.fdopen(fd, "wb") as fh:
        fh.write(look_pdf)
    _look_files.append(path)
    return stamp.StaticStampStyle.from_pdf_file(path, border_width=0)


def cleanup() -> None:
    while _look_files:
        try:
            os.remove(_look_files.pop())
        except OSError:
            pass


def _signer_name(signer) -> str:
    try:
        subject = signer.signing_cert.subject
        native = subject.native
        return str(native.get("common_name") or subject.human_friendly)[:120]
    except Exception:
        return "Unknown"


# --------------------------------------------------------------------------- checking signatures

def _trust_anchors() -> list:
    """The certificates you trust, each only for what it is: a personal certificate vouches for
    itself, not for certificates it might have "issued" to others, and only while it is valid."""
    from pyhanko_certvalidator.authority import CertTrustAnchor
    return [CertTrustAnchor(c, derive_default_quals_from_cert=True) for c in trusted_certificates()]


def in_trusted_list(cert_der: bytes) -> bool:
    return (trusted_dir() / f"{hashlib.sha256(cert_der).hexdigest()[:32]}.crt").exists()


def _when(dt_value) -> str:
    try:
        return dt_value.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _cert_dates(cert) -> tuple:
    try:
        validity = cert["tbs_certificate"]["validity"]
        return validity["not_before"].native, validity["not_after"].native
    except Exception:
        return None, None


COVERAGE_OK = ("ENTIRE_FILE", "ENTIRE_REVISION")


def validate(data: bytes, password: str | None = None, progress=None) -> list[dict]:
    """Check every signature in the PDF, offline. Returns one dict per signature."""
    _require()
    from pyhanko.pdf_utils.reader import PdfFileReader
    from pyhanko.sign.validation import validate_pdf_signature
    from pyhanko_certvalidator import ValidationContext
    from pdfdesk import sigdiff
    try:
        reader = PdfFileReader(io.BytesIO(data))
        if reader.encrypted:
            reader.decrypt((password or "").encode("utf-8"))
        sigs = list(reader.embedded_signatures)
    except Exception as exc:
        # The file can't be read strictly. If it has signatures, that alone means they can't be
        # trusted: report it as a failed check instead of an error.
        try:
            signed = [f for f in signature_fields(fitz.open("pdf", data)) if f["signed"]]
        except Exception:
            signed = [{"name": ""}]
        if not signed:
            raise
        return [_unreadable(f["name"], exc) for f in signed]
    anchors = _trust_anchors()
    trusted_names = set()
    for c in trusted_certificates():
        try:
            trusted_names.add(c.subject.dump())
        except Exception:
            pass
    damaged = False
    try:
        damaged = fitz.open("pdf", data).is_repaired
    except Exception:
        damaged = True
    results = []
    pdfops._tick(progress, 0, max(1, len(sigs)), "Checking signatures")
    for k, sig in enumerate(sigs):
        item = {"field": one_line(sig.field_name, 120), "name": "", "email": "", "when": "", "intact": False,
                "valid": False, "trusted": False, "in_list": False, "changed_after": False, "coverage": "",
                "reason": "", "location": "", "modification": "", "docmdp_ok": None, "certify_level": 0,
                "notes": [], "only_signatures": False, "problem": "", "cert_problem": "", "certificate": b"",
                "issuer": "", "self_signed": False, "not_after": "", "not_before": "", "signed_end": 0,
                "damaged": damaged}
        try:
            sig_obj = sig.sig_object
            item["reason"] = one_line(sig_obj.get("/Reason", "") or "", 200)
            item["location"] = one_line(sig_obj.get("/Location", "") or "", 200)
        except Exception:
            pass
        try:
            br = list(sig.byte_range)
            if len(br) == 4 and br[0] == 0:
                item["signed_end"] = int(br[2]) + int(br[3])
        except Exception:
            pass
        try:
            policy = sigdiff.make_policy()
            vc = ValidationContext(trust_roots=anchors, allow_fetching=False)
            st = validate_pdf_signature(sig, vc, diff_policy=policy)
            cert = st.signing_cert
            native = cert.subject.native
            not_before, not_after = _cert_dates(cert)
            item.update(
                name=one_line(native.get("common_name") or cert.subject.human_friendly, 120),
                email=one_line(native.get("email_address", ""), 120),
                issuer=one_line(cert.issuer.native.get("common_name") or cert.issuer.human_friendly, 120),
                self_signed=cert.self_signed in ("yes", "maybe"),
                when=_when(st.signer_reported_dt) if st.signer_reported_dt else "",
                intact=bool(st.intact), valid=bool(st.valid), trusted=bool(st.trusted),
                in_list=in_trusted_list(cert.dump()),
                coverage=str(getattr(st.coverage, "name", st.coverage)),
                modification=str(getattr(st.modification_level, "name", st.modification_level) or ""),
                docmdp_ok=st.docmdp_ok, certificate=cert.dump(),
                not_before=not_before.date().isoformat() if not_before else "",
                not_after=not_after.date().isoformat() if not_after else "")
            try:
                perm = sig.docmdp_level
                item["certify_level"] = int(perm.value) if perm is not None else 0
            except Exception:
                pass
            item["changed_after"] = item["coverage"] != "ENTIRE_FILE"
            changes = policy.changes.get(sig.signed_revision)
            if changes is not None and changes.level <= int(getattr(st.modification_level, "value", 4)):
                item["notes"] = list(changes.notes)
                item["only_signatures"] = changes.only_signatures
            if item["coverage"] == "ENTIRE_REVISION" and item["modification"] != "OTHER" and item["signed_end"]:
                try:
                    differ = sigdiff.shown_pages_differ(data[:item["signed_end"]], data, password)
                except Exception:
                    differ = "PDF Desk couldn't compare the pages with the signed version."
                if differ:
                    item["modification"] = "OTHER"
                    item["notes"] = [differ] + item["notes"]
            now = _dt.datetime.now(_dt.timezone.utc)
            if not st.trusted:
                if not_after and now > not_after:
                    item["cert_problem"] = "expired"
                elif not_before and now < not_before:
                    item["cert_problem"] = "not_yet_valid"
                elif item["in_list"]:
                    item["cert_problem"] = "unusable"
                elif not item["self_signed"] and cert.issuer.dump() in trusted_names:
                    item["cert_problem"] = "issuer_cannot_vouch"
        except Exception as exc:
            item["problem"] = one_line(exc, 300)
        results.append(item)
        pdfops._tick(progress, k + 1, len(sigs), "Checking signatures")
    return results


def _unreadable(field: str, exc) -> dict:
    return {"field": one_line(field, 120), "name": "", "email": "", "when": "", "intact": False, "valid": False,
            "trusted": False, "in_list": False, "changed_after": True, "coverage": "", "reason": "", "location": "",
            "modification": "", "docmdp_ok": None, "certify_level": 0, "notes": [], "only_signatures": False,
            "problem": one_line(f"the file is put together in an unusual or damaged way ({exc})", 300),
            "cert_problem": "", "certificate": b"", "issuer": "", "self_signed": False, "not_after": "",
            "not_before": "", "signed_end": 0, "damaged": True}


CERTIFY_TEXT = {1: "no changes at all", 2: "only filling in forms and signing",
                3: "only filling in forms, signing and comments"}


def describe(item: dict) -> tuple[str, str]:
    """(level, text) for one signature: level is "good", "warn" or "bad"."""
    who = item["name"] or "Unknown signer"
    when = f" on {item['when']}" if item["when"] else ""
    if item["problem"] and not item["name"]:
        return "bad", f"A signature couldn't be checked: {item['problem']}"
    if not (item["intact"] and item["valid"]):
        return "bad", (f"Signed by {who}{when}, but the signed content has been CHANGED or the signature is "
                       "damaged. Don't rely on this document.")
    if item["coverage"] not in COVERAGE_OK:
        return "bad", (f"Signed by {who}{when}, but the signature doesn't cover its version of the file in the "
                       "usual way, so PDF Desk can't confirm what was signed. Don't rely on this document.")
    from pdfdesk.sigdiff import summarize
    notes = summarize(item.get("notes") or [], 4)
    brief = summarize(item.get("notes") or [], 3, inline=True)
    if item["modification"] == "OTHER":
        return "bad", (f"Signed by {who}{when}. The signed version is intact, but the document was CHANGED "
                       "after signing in a way signatures don't allow" + (f": {notes}" if notes else ".")
                       + " What you see may not be what was signed. Use Open signed version to see exactly "
                       "what was signed.")
    if item.get("docmdp_ok") is False:
        allowed = CERTIFY_TEXT.get(item.get("certify_level") or 0, "only some changes")
        return "bad", (f"Signed by {who}{when}. The signer allowed {allowed} afterwards, but later changes go "
                       "further" + (f": {notes}" if notes else ".") + " Use Open signed version to see what "
                       "was signed.")
    if item.get("damaged"):
        return "bad", (f"Signed by {who}{when}, but the file is damaged, so what PDF Desk shows may differ from "
                       "what was signed. Use Open signed version, or ask the sender for a new copy.")
    parts = [f"Signed by {who}{when} (the time comes from the signer's computer clock)."]
    level = "good"
    mod = item["modification"]
    if not item["changed_after"] or mod == "NONE":
        parts.append("The document hasn't been changed since it was signed.")
    elif mod == "":
        level = "warn"
        parts.append("The signed version is intact, but PDF Desk couldn't check the changes made after signing. "
                     "Use Open signed version to see exactly what was signed.")
    elif mod == "LTA_UPDATES" or item.get("only_signatures"):
        parts.append("The signed content is unchanged. Only more signatures or technical data were added "
                     "afterwards.")
    elif mod == "FORM_FILLING":
        level = "warn"
        parts.append("The signed version is intact. Afterwards, form fields were filled in or more signatures "
                     "were added" + (f" ({brief})" if brief else "") + ".")
    else:
        level = "warn"
        parts.append("The signed version is intact. Afterwards, comments, markups or document settings were "
                     "added or changed" + (f" ({brief})" if brief else "") + ". Comments can cover parts of the "
                     "page, so use Open signed version to see exactly what was signed.")
    if item["trusted"]:
        parts.append("You trust this person's certificate.")
        return level, " ".join(parts)
    problem = item.get("cert_problem")
    if problem == "expired":
        parts.append(f"Their certificate expired on {item['not_after']}, so their identity can't be confirmed "
                     "any more.")
    elif problem == "not_yet_valid":
        parts.append(f"Their certificate only becomes valid on {item['not_before']}, so their identity can't "
                     "be confirmed yet.")
    elif problem == "issuer_cannot_vouch":
        parts.append(f"Their certificate was issued by {item['issuer']}. That certificate is in your trusted list, "
                     "but only for its own signatures, not to vouch for other people, so this signer's identity "
                     "isn't confirmed.")
    elif problem == "unusable":
        parts.append("Their certificate is in your trusted list, but it can't be used for signing, so their "
                     "identity isn't confirmed.")
    else:
        parts.append("Their identity isn't confirmed: their certificate isn't in your trusted list"
                     + (" (it is self-made)." if item["self_signed"] else ", and wasn't issued by anyone in it."))
    return "warn", " ".join(parts)


def signed_version(data: bytes, item: dict) -> bytes | None:
    """The file exactly as it was when this signature was made (None when that can't be told)."""
    end = int(item.get("signed_end") or 0)
    if item.get("coverage") not in COVERAGE_OK or not 0 < end <= len(data):
        return None
    return data[:end]
