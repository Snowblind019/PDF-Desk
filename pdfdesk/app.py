"""Application entry point. Starts the window, applies the theme and makes sure only one copy runs:
opening a PDF while PDF Desk is already open sends it to the running window as a new tab."""
from __future__ import annotations

import getpass
import hmac
import json
import logging
import os
import secrets
import sys

from pdfdesk import APP_ID, APP_NAME, __version__


MAX_MESSAGE = 1_000_000
MAX_PATHS = 200


def _server_name() -> str:
    """Per-user name for the single-instance channel. On Linux it lives in the user's private
    runtime folder; on Windows the pipe only accepts connections from the same user."""
    try:
        user = getpass.getuser()
    except Exception:
        user = "user"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not sys.platform.startswith("win") and runtime and os.path.isdir(runtime):
        return os.path.join(runtime, f"{APP_ID}.sock")
    return f"{APP_ID}-{user}"


def _token_file():
    from pdfdesk.config import config_dir
    return config_dir() / "instance.token"


def _read_token() -> str:
    try:
        return _token_file().read_text(encoding="ascii").strip()
    except OSError:
        return ""


def _new_token() -> str:
    """A random secret that only this user can read. The running window sends it first, so a
    second launch only hands over file names after it knows it's talking to the real PDF Desk."""
    token = secrets.token_hex(16)
    path = _token_file()
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(token)
    except OSError:
        return ""
    return token


def _send_to_running(paths: list[str]) -> bool:
    import time
    from PySide6.QtNetwork import QLocalSocket
    sock = QLocalSocket()
    sock.connectToServer(_server_name())
    if not sock.waitForConnected(400):
        return False
    data = b""
    deadline = time.monotonic() + 1.5
    while b"\n" not in data and time.monotonic() < deadline and len(data) < 200:
        if sock.waitForReadyRead(200):
            data += bytes(sock.readAll())
    expected = _read_token()
    if not expected or not hmac.compare_digest(data.split(b"\n")[0].strip(), expected.encode("ascii")):
        sock.abort()  # not our PDF Desk, so don't tell it anything
        return False
    sock.write(json.dumps({"token": expected, "paths": paths}).encode("utf-8"))
    sock.flush()
    sock.waitForBytesWritten(1000)
    sock.disconnectFromServer()
    return True


def _start_server(window):
    from PySide6.QtNetwork import QLocalServer
    server = QLocalServer(window)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    name = _server_name()
    QLocalServer.removeServer(name)
    if not server.listen(name):
        return None
    token = _new_token()
    if not token:
        server.close()
        return None

    def on_connection():
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            buf = bytearray()
            conn.write((token + "\n").encode("ascii"))
            conn.flush()

            def read(c=conn, b=buf):
                b.extend(bytes(c.readAll()))
                if len(b) > MAX_MESSAGE:
                    c.abort()
                    return
                try:
                    msg = json.loads(b.decode("utf-8"))
                except ValueError:
                    return  # the rest of the message hasn't arrived yet
                c.disconnectFromServer()
                if not isinstance(msg, dict) or not hmac.compare_digest(str(msg.get("token", "")), token):
                    return
                raw = msg.get("paths")
                paths = [p for p in raw[:MAX_PATHS] if isinstance(p, str) and 0 < len(p) < 4096
                         and os.path.isabs(p)] if isinstance(raw, list) else []
                if paths:
                    window.open_paths(paths)
                window.setWindowState(window.windowState() & ~window.windowState().WindowMinimized)
                window.show()
                window.raise_()
                window.activateWindow()
            conn.readyRead.connect(read)
            conn.disconnected.connect(conn.deleteLater)
    server.newConnection.connect(on_connection)
    return server


def self_check() -> tuple[bool, str]:
    """Check that every part works: libraries, fonts, conversions and the optional extras."""
    import tempfile
    lines, ok = [], True

    def step(name, fn):
        nonlocal ok
        try:
            detail = fn()
            lines.append(f"OK    {name}" + (f": {detail}" if detail else ""))
        except Exception as exc:  # report and keep going
            ok = False
            lines.append(f"FAIL  {name}: {exc}")

    import pymupdf as fitz
    from pdfdesk import convert, externals, fonts
    step("PyMuPDF", lambda: fitz.VersionBind)
    import PySide6
    step("Qt", lambda: PySide6.__version__)
    step("Unicode fonts", lambda: "bundled" if fonts.HAVE_FONT_PACK else "using system fonts")
    tmp = tempfile.mkdtemp(prefix="pdfdesk-check-")
    sample = {}

    def make():
        sample["pdf"] = convert.markdown_to_pdf_bytes("# Check\n\nText ăîșț\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
        return f"{fitz.open('pdf', sample['pdf']).page_count} page"
    step("Markdown to PDF", make)
    for fmt in ("docx", "xlsx", "pptx", "png", "md"):
        key, label, ext, kind = convert.export_format(fmt)
        target = os.path.join(tmp, "out" if kind == "folder" else f"out{ext}")
        step(f"Export {label}", lambda f=fmt, t=target: (convert.export_pdf(sample["pdf"], f, t), "")[1])
    def font_list():
        from pdfdesk import fontcatalog
        from pdfdesk import richtext as RT
        cat = fontcatalog.catalog()
        cat.start()
        cat.wait(60)
        box = RT.Box(paras=[RT.Para([RT.Run("Text ăîșț", cat.default_family(), 12, bold=True)])])
        RT.render(box, 200)
        return f"{len(cat.names())} fonts"
    step("Font list and text boxes", font_list)

    def signatures():
        from pdfdesk import digitalid
        if not digitalid.available():
            raise RuntimeError("pyHanko isn't installed, so Digital ID signing won't work")
        import datetime as dt
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives.serialization import pkcs12
        from cryptography.x509.oid import NameOID
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "PDF Desk check")])
        now = dt.datetime.now(dt.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(minutes=5))
                .not_valid_after(now + dt.timedelta(days=1)).sign(key, hashes.SHA256()))
        p12 = os.path.join(tmp, "check.p12")
        with open(p12, "wb") as fh:
            fh.write(pkcs12.serialize_key_and_certificates(b"x", key, cert, None,
                                                           serialization.BestAvailableEncryption(b"checkpass")))
        result = digitalid.validate(digitalid.sign(sample["pdf"], p12, "checkpass"))[0]
        if not (result["intact"] and result["valid"]):
            raise RuntimeError("a test signature didn't check out")
        return "signing and checking work"
    step("Digital signatures", signatures)
    lo = externals.libreoffice_command()
    lines.append(("OK    LibreOffice: " + " ".join(lo)) if lo else "--    LibreOffice: not found (optional)")
    from pdfdesk import speech
    lines.append("OK    Read Out Loud voice: found" if speech.available()
                 else "--    Read Out Loud voice: not found (optional)")
    folder, langs = externals.find_tessdata()
    lines.append(f"OK    OCR languages: {', '.join(langs)}" if langs else "--    OCR languages: none found (optional)")
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return ok, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    if any(a in ("-v", "--version") for a in argv[1:]):
        print(f"{APP_NAME} {__version__}")
        return 0
    if any(a in ("-h", "--help") for a in argv[1:]):
        print(f"Usage: pdfdesk [--new-window] [--check] [files...]\n\n{APP_NAME} {__version__}: offline PDF "
              "viewer, editor and converter.\nFiles can be PDFs or anything PDF Desk can convert (Word, images, "
              "text...).\n--check tests that every part of PDF Desk works on this computer.")
        return 0
    # Windowed builds (pythonw / the .exe) have no console. Send output to a log file so libraries
    # that print messages can't fail, and so there's something to look at if a problem comes up.
    no_console = sys.stdout is None or sys.stderr is None
    if no_console:
        try:
            from pdfdesk.config import cache_dir
            log = open(cache_dir() / "pdfdesk.log", "a", encoding="utf-8", buffering=1)
        except Exception:
            log = open(os.devnull, "w")
        if sys.stdout is None:
            sys.stdout = log
        if sys.stderr is None:
            sys.stderr = log
    if "--check" in argv[1:]:
        ok, report = self_check()
        if not no_console:
            print(report)
        else:  # windowed build on Windows has no console, so show the result in a window
            from PySide6.QtWidgets import QApplication
            from pdfdesk import ui
            app = QApplication(argv)
            ui.information(None, f"{APP_NAME} check", report)
        return 0 if ok else 1
    new_window = "--new-window" in argv
    files = [os.path.abspath(a) for a in argv[1:] if not a.startswith("-")]
    # Work from the home folder, not the folder of the file being opened. On Windows this also stops
    # program lookups from finding programs that happen to sit next to a downloaded PDF.
    if sys.platform.startswith("win"):
        os.environ["NoDefaultCurrentDirectoryInExePath"] = "1"
    try:
        os.chdir(os.path.expanduser("~"))
    except OSError:
        pass
    logging.getLogger().setLevel(logging.WARNING)

    if sys.platform.startswith("win"):
        try:  # show our own icon in the Windows taskbar
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PDFDesk.PDFDesk")
        except Exception:
            pass

    from PySide6.QtCore import Qt, QCoreApplication
    from PySide6.QtWidgets import QApplication
    QCoreApplication.setApplicationName(APP_NAME)
    QCoreApplication.setOrganizationName(APP_NAME)
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(argv)
    app.setApplicationDisplayName(APP_NAME)
    app.setDesktopFileName("pdfdesk")

    if not new_window and _send_to_running(files):
        return 0

    from pdfdesk import icons, theme
    from pdfdesk.config import settings
    theme.apply_theme(app, settings().get("theme") or "system")
    app.setWindowIcon(icons.app_icon())

    from pdfdesk.mainwindow import MainWindow
    window = MainWindow()
    window._server = _start_server(window) if not new_window else None

    def follow_system_theme(*_):
        if (settings().get("theme") or "system") == "system":
            window.set_theme("system")
    try:
        app.styleHints().colorSchemeChanged.connect(follow_system_theme)
    except Exception:
        pass

    window.show()
    if files:
        from PySide6.QtCore import QTimer
        QTimer.singleShot(0, lambda: window.open_paths(files))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
