"""Checks for anything that comes from inside a PDF (or another opened file) before PDF Desk acts on it.

A PDF can contain links to web pages, e-mail addresses and other files. Links are only followed
after the user clicks them and confirms, only for web pages, e-mail and other local PDF files,
and never for network shares (\\\\server\\share), which on Windows would send the user's login
hash to that server."""
from __future__ import annotations

import ntpath
import os
import posixpath
import re
import sys
import unicodedata
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

IS_WINDOWS = sys.platform.startswith("win")

WEB_SCHEMES = {"http", "https"}
MAX_DISPLAY = 300


def clean_text(text: str, limit: int | None = MAX_DISPLAY) -> str:
    """Remove control and invisible formatting characters (such as right-to-left overrides that
    can disguise a file name or address) so what is shown is what will be used."""
    out = "".join(ch for ch in str(text) if unicodedata.category(ch) not in ("Cc", "Cf") or ch in "\t")
    if limit is not None and len(out) > limit:
        out = out[: limit - 3] + "..."
    return out


def has_hidden_chars(text: str) -> bool:
    return any(unicodedata.category(ch) in ("Cc", "Cf") for ch in str(text))


def check_web_link(uri: str) -> tuple[str | None, str]:
    """Return (url to open, description) for http/https/mailto links, or (None, reason) when blocked."""
    uri = (uri or "").strip()
    if not uri:
        return None, "The link is empty."
    if has_hidden_chars(uri):
        return None, "The link contains hidden characters, so it was blocked."
    try:
        parts = urlsplit(uri)
    except ValueError:
        return None, "The link isn't a valid address."
    scheme = parts.scheme.lower()
    if scheme in WEB_SCHEMES:
        host = parts.hostname or ""
        if not host:
            return None, "The link has no web address."
        if parts.username or parts.password:
            # http://bank.com@evil.example/ style links hide the real destination
            return None, "The link hides its real destination (it contains a user name), so it was blocked."
        return uri, f"{scheme}://{host}"
    if scheme == "mailto":
        address = parts.path
        if not re.fullmatch(r"[^@\s,;<>]+@[^@\s,;<>]+", address or ""):
            return None, "The e-mail link doesn't contain a single valid address."
        # keep only the subject and body, nothing that could attach files or add recipients
        allowed = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() in ("subject", "body")]
        safe = "mailto:" + quote(address, safe="@.+-_")
        if allowed:
            safe += "?" + urlencode(allowed, quote_via=quote)
        return safe, address
    shown = clean_text(scheme or "unknown", 40)
    return None, f"Links of type “{shown}:” are blocked. Only web and e-mail links can be opened."


def is_network_path(path: str) -> bool:
    """True for UNC paths (\\\\server\\share, //server/share) and any URL-style path (smb://, file://...)."""
    p = (path or "").strip()
    if p.startswith(("\\\\", "//", "\\/", "/\\")):
        return True
    drive, _rest = os.path.splitdrive(p)
    if drive.startswith(("\\\\", "//")):
        return True
    return bool(re.match(r"^[A-Za-z][A-Za-z0-9+.\-]{1,}:[/\\]{2}", p)) or p.lower().startswith("file:")


def _is_remote_drive(path: str) -> bool:
    """On Windows, True when the path is on a mapped network drive (for example Z: -> \\\\server\\share)."""
    if not IS_WINDOWS:
        return False
    try:
        import ctypes
        return ctypes.windll.kernel32.GetDriveTypeW(path[:3]) == 4  # DRIVE_REMOTE
    except Exception:
        return True  # if we can't tell, play it safe


def resolve_pdf_link(target: str, base_pdf: str | None, windows: bool | None = None) -> tuple[str | None, str]:
    """Check a link from inside a PDF that points at another file. Returns (absolute path, "") when it
    is a local PDF that exists, otherwise (None, reason). Only plain local paths are accepted:
    "C:\\folder\\file.pdf" style on Windows and "/folder/file.pdf" style elsewhere, or a name relative
    to the PDF's own folder."""
    windows = IS_WINDOWS if windows is None else windows
    paths = ntpath if windows else posixpath
    target = unquote((target or "").strip())  # MuPDF %-encodes spaces and other characters in file names
    if not target:
        return None, "The link doesn't name a file."
    if has_hidden_chars(target):
        return None, "The link contains hidden characters, so it was blocked."
    if "?" in target or "*" in target or is_network_path(target):
        # \\?\ and \??\ style paths can reach network shares, so anything like that is refused
        return None, "Links to network locations or unusual paths are blocked."
    if windows:
        if re.match(r"^[A-Za-z]:[\\/]", target):
            pass  # full path with a drive letter
        elif re.match(r"^[A-Za-z]:", target) or target[:1] in "\\/":
            return None, "Links to unusual paths are blocked."  # C:file.pdf or \\file.pdf
        else:
            if not base_pdf:
                return None, "Save this PDF first, then the link can be followed."
            target = paths.join(paths.dirname(base_pdf), target)
    elif not target.startswith("/"):
        if not base_pdf:
            return None, "Save this PDF first, then the link can be followed."
        target = paths.join(paths.dirname(base_pdf), target)
    target = paths.normpath(target)
    shape_ok = (re.fullmatch(r'[A-Za-z]:\\[^?*<>|"]*', target) if windows
                else target.startswith("/") and not target.startswith("//"))
    if not shape_ok or is_network_path(target) or (windows and _is_remote_drive(target)):
        return None, "Links to network locations or unusual paths are blocked."
    if not target.lower().endswith(".pdf"):
        return None, "Links can only open other PDF files. Other kinds of files are blocked."
    if not os.path.isfile(target):
        return None, f"The linked file wasn't found:\n{clean_text(target)}"
    return target, ""


def safe_filename_part(text: str, limit: int = 100) -> str:
    """Make text (for example a bookmark title from a PDF) safe to use inside a file name."""
    text = clean_text(text, None)
    text = re.sub(r'[\\/:*?"<>|]+', "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text[:limit].rstrip(" .")
