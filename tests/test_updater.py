"""Update checks: versions, the release key, signatures, safe unpacking and the release script.
None of these tests use the network (any attempt fails the test)."""
import base64
import io
import json
import os
import socket
import stat
import sys
import tempfile
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("PDFDESK_HOME", tempfile.mkdtemp(prefix="pdfdesk-update-home-"))

from pathlib import Path  # noqa: E402

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from pdfdesk import updater  # noqa: E402


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("the updater tests must not use the network")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


@pytest.fixture
def release_key(tmp_path, monkeypatch):
    """A throwaway release key, installed as the key this copy of PDF Desk trusts."""
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    pub = tmp_path / "update-key.pub"
    pub.write_text("# test key\n" + base64.b64encode(raw).decode() + "\n", encoding="ascii")
    monkeypatch.setattr(updater, "KEY_FILE", pub)
    return key


def make_zip(version="2.0.2", extra=None, version_inside=None):
    top = f"PDF-Desk-{version}"
    files = {
        "pdfdesk/__init__.py": f'__version__ = "{version_inside or version}"\n',
        "pdfdesk.py": "print('hi')\n", "install.sh": "#!/bin/bash\n", "install.ps1": "# ps\n",
        "requirements.lock": "x==1\n",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in files.items():
            info = zipfile.ZipInfo(f"{top}/{name}")
            info.external_attr = ((0o755 if name.endswith(".sh") else 0o644) | stat.S_IFREG) << 16
            zf.writestr(info, text)
        for info, data in (extra or []):
            zf.writestr(info, data)
    return buf.getvalue()


def sign(key, version, data):
    return base64.b64encode(key.sign(updater.signed_message(version, data)))


# ---------------------------------------------------------------------------------- versions
def test_versions():
    assert updater.parse_version("v2") == (2, 0, 0)
    assert updater.parse_version("2.0.1") == (2, 0, 1)
    assert updater.parse_version("v10.2") == (10, 2, 0)
    for bad in ("", "v2.0.1-beta", "latest", "v2.0.1.4", "2..1", "v-1", " v2 x", "v\u0663.0.0"):
        assert updater.parse_version(bad) is None
    assert updater.current_version() == (2, 0, 1)
    assert updater.is_newer({"version": (2, 0, 2)}) and not updater.is_newer({"version": (2, 0, 1)})
    assert not updater.is_newer({"version": (2, 0, 0)})


def test_release_info_only_trusts_its_own_downloads():
    base = updater.DOWNLOAD_PREFIX + "v2.0.2/"
    good = {"tag_name": "v2.0.2", "body": "Fixes.\x1b[31m\x00 more", "assets": [
        {"name": "PDF-Desk-2.0.2.zip", "browser_download_url": base + "PDF-Desk-2.0.2.zip"},
        {"name": "PDF-Desk-2.0.2.zip.sig", "browser_download_url": base + "PDF-Desk-2.0.2.zip.sig"},
        {"name": "other.zip", "browser_download_url": base + "other.zip"}]}
    rel = updater.latest_release(json.dumps(good).encode())
    assert rel["version"] == (2, 0, 2) and rel["zip_url"].endswith("PDF-Desk-2.0.2.zip")
    assert rel["sig_url"].endswith(".sig") and "\x1b" not in rel["notes"] and "\x00" not in rel["notes"]
    assert updater._plain("abc\u202edef\u0085", 100) == "abcdef"  # no direction tricks or C1 controls
    assert rel["page"] == updater.RELEASES_PAGE + "/tag/v2.0.2"
    # assets pointing anywhere else are ignored
    evil = dict(good, assets=[{"name": "PDF-Desk-2.0.2.zip", "browser_download_url": "https://evil.example/x.zip"},
                              {"name": "PDF-Desk-2.0.2.zip.sig",
                               "browser_download_url": "https://github.com/someone-else/x/releases/download/a"}])
    rel = updater.latest_release(json.dumps(evil).encode())
    assert rel["zip_url"] is None and rel["sig_url"] is None
    for bad in ({"tag_name": "nightly"}, {"tag_name": "v3", "prerelease": True}, [], {"tag_name": "v3", "draft": 1}):
        with pytest.raises(updater.UpdateError):
            updater.latest_release(json.dumps(bad).encode())
    with pytest.raises(updater.UpdateError):
        updater.latest_release(b"\xff not json")


def test_only_https_to_github():
    for ok in (updater.LATEST_URL, "https://objects.githubusercontent.com/x",
               "https://release-assets.githubusercontent.com/y"):
        updater._check_url(ok)
    for bad in ("http://api.github.com/x", "https://github.com.evil.example/x", "https://evil.example/",
                "file:///etc/passwd", "https://github.com:8443/x", "ftp://github.com/x"):
        with pytest.raises(updater.UpdateError):
            updater._check_url(bad)
    with pytest.raises(updater.UpdateError):  # refused before any connection is made
        updater.fetch("https://evil.example/x", 10)


# -------------------------------------------------------------------------------- signatures
def test_signature_checks(release_key):
    data = make_zip()
    sig = sign(release_key, "2.0.2", data)
    updater.verify("2.0.2", data, sig)
    with pytest.raises(updater.UpdateError):  # a changed zip
        updater.verify("2.0.2", data + b"x", sig)
    with pytest.raises(updater.UpdateError):  # an old signed release passed off as a newer version
        updater.verify("2.0.3", data, sig)
    with pytest.raises(updater.UpdateError):  # signed by someone else
        other = Ed25519PrivateKey.generate()
        updater.verify("2.0.2", data, sign(other, "2.0.2", data))
    with pytest.raises(updater.UpdateError):
        updater.verify("2.0.2", data, b"not base64!!")


def test_no_key_means_no_automatic_install(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "KEY_FILE", tmp_path / "missing.pub")
    assert updater.public_key_bytes() is None
    with pytest.raises(updater.UpdateError):
        updater.verify("2.0.2", b"x", b"AAAA")
    (tmp_path / "bad.pub").write_text("c2hvcnQ=\n")  # valid base64 but not a 32-byte key
    monkeypatch.setattr(updater, "KEY_FILE", tmp_path / "bad.pub")
    assert updater.public_key_bytes() is None


# --------------------------------------------------------------------------------- unpacking
@pytest.mark.filterwarnings("ignore:Duplicate name")
def test_safe_extract(tmp_path):
    root = updater.safe_extract(make_zip(), tmp_path / "a", "2.0.2")
    assert (root / "install.sh").stat().st_mode & 0o111 and not (root / "pdfdesk.py").stat().st_mode & 0o111

    def bad(name, data=b"x", link=False):
        info = zipfile.ZipInfo(name)
        if link:
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        return make_zip(extra=[(info, data)])

    for data in (bad("PDF-Desk-2.0.2/../escape.txt"), bad("/etc/x"), bad("other-folder/x"),
                 bad("PDF-Desk-2.0.2/C:/x"), bad("PDF-Desk-2.0.2/a\\..\\..\\b"),
                 bad("PDF-Desk-2.0.2/link", b"/etc/passwd", link=True), bad("PDF-Desk-2.0.2/CON.txt"),
                 bad("PDF-Desk-2.0.2/nul"), bad("PDF-Desk-2.0.2/trailing."), bad("PDF-Desk-2.0.2/space "),
                 bad("PDF-Desk-2.0.2/INSTALL.SH"), bad("PDF-Desk-2.0.2/install.sh")):
        with pytest.raises(updater.UpdateError):
            updater.safe_extract(data, tmp_path / "b", "2.0.2")
    assert not (tmp_path / "escape.txt").exists()
    with pytest.raises(updater.UpdateError):  # the version inside must match the release
        updater.safe_extract(make_zip(version_inside="2.0.1"), tmp_path / "c", "2.0.2")
    with pytest.raises(updater.UpdateError):
        updater.safe_extract(b"not a zip", tmp_path / "d", "2.0.2")


def test_prepare_downloads_checks_and_unpacks(tmp_path, monkeypatch, release_key):
    data = make_zip()
    sig = sign(release_key, "2.0.2", data)
    files = {"https://github.com/z.zip": data, "https://github.com/z.sig": sig}
    monkeypatch.setattr(updater, "fetch", lambda url, limit, **_k: files[url])
    monkeypatch.setattr(updater, "install_location", lambda: ("linux", tmp_path / "dest", ""))
    rel = {"version": (2, 0, 2), "version_text": "2.0.2", "zip_url": "https://github.com/z.zip",
           "sig_url": "https://github.com/z.sig"}
    prepared = updater.prepare(rel)
    assert (prepared["root"] / "install.sh").is_file() and prepared["version"] == "2.0.2"
    files["https://github.com/z.zip"] = data[:100] + bytes([data[100] ^ 1]) + data[101:]  # damaged
    with pytest.raises(updater.UpdateError):
        updater.prepare(rel)
    monkeypatch.setattr(updater, "install_location", lambda: ("other", None, "not installed"))
    with pytest.raises(updater.UpdateError, match="not installed"):
        updater.prepare(rel)


def test_install_commands(tmp_path, monkeypatch, release_key):
    dest, root = tmp_path / "dest", tmp_path / "root"
    dest.mkdir()
    assert updater.installer_command("linux", dest, root)[-1].endswith("install.sh")
    win = updater.installer_command("windows", dest, root)
    assert win[0].lower().endswith("powershell.exe") and "-NonInteractive" in win and win[-1].endswith("install.ps1")
    assert updater.relaunch_command("linux", dest)[1].endswith("pdfdesk.py")
    assert updater.install_location()[0] == "other"  # running from the source folder
    assert "wasn't installed with the installer" in updater.why_not_automatic()
    monkeypatch.setattr(updater, "install_location", lambda: ("linux", dest, ""))
    rel = {"zip_url": "https://github.com/a", "sig_url": "https://github.com/b"}
    assert updater.why_not_automatic(rel) == ""
    assert "signed" in updater.why_not_automatic({"zip_url": None, "sig_url": None})
    # installs with unpinned packages never update themselves (that would install unchecked packages)
    (dest / "install-options").write_text("unlocked=1\n")
    assert "unlocked" in updater.why_not_automatic(rel)


def test_other_copies_and_private_folder(tmp_path, monkeypatch):
    import subprocess
    running = tmp_path / "running"
    running.mkdir()
    monkeypatch.setattr(updater, "_running_dir", lambda: running)
    monkeypatch.setattr(updater, "_update_lock_path", lambda: tmp_path / "update.lock")
    monkeypatch.setattr(updater, "_registration", None)
    (running / "999999.lock").write_text("")   # a copy that ended without cleaning up
    (running / "\u00b2.lock").write_text("")   # not a process number: ignored, never a crash
    assert updater.register_process()
    assert (running / f"{os.getpid()}.lock").exists() and not (running / "999999.lock").exists()
    assert updater.other_copies_running() == []
    # another copy that's really running holds its lock; once it ends, its file no longer counts
    holder = (f"import sys, time; sys.path.insert(0, {ROOT!r}); from pdfdesk.locks import try_lock; "
              f"fh = open({str(running / '424242.lock')!r}, 'a+b'); assert try_lock(fh); print('ok', flush=True); "
              "time.sleep(30)")
    child = subprocess.Popen([sys.executable, "-c", holder], stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "ok"
        assert updater.other_copies_running() == ["424242.lock"]
    finally:
        child.kill()
        child.wait()
    assert updater.other_copies_running() == []
    # while an update installs (the update lock is held), PDF Desk doesn't start
    from pdfdesk.locks import try_lock, unlock
    monkeypatch.setattr(updater, "_registration", None)
    gate = open(tmp_path / "update.lock", "a+b")
    assert try_lock(gate)
    assert updater.register_process(wait=0.3) is False
    unlock(gate)
    gate.close()
    assert updater.register_process(wait=0.3) is True
    assert updater.other_copies_running() == []
    # a file system without locking: PDF Desk still starts, but won't install updates by itself
    import errno
    monkeypatch.setattr(updater, "_registration", None)
    monkeypatch.setattr(updater, "_locking_works", True)  # put back after the test

    def no_locking(_fh):
        raise OSError(errno.ENOLCK, "No locks available")
    monkeypatch.setattr(updater, "try_lock", no_locking)
    assert updater.register_process(wait=0.3) is True and updater._locking_works is False
    monkeypatch.setattr(updater, "install_location", lambda: ("linux", tmp_path, ""))
    assert "locking" in updater.why_not_automatic()
    assert updater.other_copies_running()  # unknown counts as "maybe running"
    if not sys.platform.startswith("win"):
        shared = tmp_path / "shared"
        shared.mkdir()
        os.chmod(shared, 0o777)
        with pytest.raises(updater.UpdateError):
            updater._private_folder(shared / "updates")
        ok = tmp_path / "mine" / "updates"
        updater._private_folder(ok)
        assert stat.S_IMODE(ok.stat().st_mode) == 0o700


def test_cancel_after_download(tmp_path, monkeypatch, release_key):
    data = make_zip()
    files = {"https://github.com/z.zip": data, "https://github.com/z.sig": sign(release_key, "2.0.2", data)}
    monkeypatch.setattr(updater, "fetch", lambda url, limit, **_k: files[url])
    monkeypatch.setattr(updater, "install_location", lambda: ("linux", tmp_path / "dest", ""))
    rel = {"version": (2, 0, 2), "version_text": "2.0.2", "zip_url": "https://github.com/z.zip",
           "sig_url": "https://github.com/z.sig"}
    with pytest.raises(updater.UpdateError, match="Cancelled"):
        updater.prepare(rel, cancelled=lambda: True)


def test_once_a_day():
    class S(dict):
        def get(self, k, d=None):
            return super().get(k, d)
    os.environ.pop("PDFDESK_NO_UPDATE_CHECK", None)
    assert updater.due(S(check_updates=True, last_update_check=0), now=100000)
    assert not updater.due(S(check_updates=True, last_update_check=99000), now=100000)
    assert not updater.due(S(check_updates=False, last_update_check=0), now=100000)
    assert updater.due(S(check_updates=True, last_update_check=200000), now=100000)  # clock went back


# ---------------------------------------------------------------------------- release script
def test_release_script_round_trip(tmp_path, monkeypatch):
    sys.path.insert(0, os.path.join(ROOT, "tools"))
    import make_release as mr
    repo = tmp_path / "repo"
    (repo / "pdfdesk" / "assets").mkdir(parents=True)
    (repo / "pdfdesk" / "__init__.py").write_text('__version__ = "2.0.2"\n')
    for name in ("pdfdesk.py", "install.sh", "install.ps1", "requirements.lock"):
        (repo / name).write_text("x\n")
    (repo / "installer.iss").write_text('#define AppVersion "2.0.2"\n')
    (repo / "__pycache__").mkdir()
    (repo / "__pycache__" / "junk.pyc").write_bytes(b"x")
    monkeypatch.setattr(mr, "ROOT", repo)
    monkeypatch.setattr(mr, "PUBLIC_KEY", repo / "pdfdesk" / "assets" / "update-key.pub")
    monkeypatch.setattr(updater, "KEY_FILE", repo / "pdfdesk" / "assets" / "update-key.pub")
    monkeypatch.setenv("PDFDESK_SIGNING_KEY", str(tmp_path / "keys" / "k.pem"))
    monkeypatch.setenv("PDFDESK_SIGNING_PASSPHRASE", "a long test passphrase")
    mr.main(["init"])
    keyfile = tmp_path / "keys" / "k.pem"
    if not sys.platform.startswith("win"):
        assert stat.S_IMODE(keyfile.stat().st_mode) == 0o600
    stored = json.loads(keyfile.read_text())
    assert stored["kdf"] == "scrypt" and stored["n"] >= 2 ** 17
    with pytest.raises(SystemExit):  # never silently replaces the key
        mr.main(["init"])
    monkeypatch.setenv("PDFDESK_SIGNING_KEY", str(repo / "inside.json"))
    with pytest.raises(SystemExit):  # the key may not live inside the repo
        mr.main(["init"])
    monkeypatch.setenv("PDFDESK_SIGNING_KEY", str(keyfile))
    mr.main(["build"])
    zpath = repo / "dist" / "PDF-Desk-2.0.2.zip"
    mr.main(["verify", str(zpath)])
    names = zipfile.ZipFile(zpath).namelist()
    assert "PDF-Desk-2.0.2/pdfdesk/assets/update-key.pub" in names and not any("pycache" in n for n in names)
    data, sig = zpath.read_bytes(), Path(str(zpath) + ".sig").read_bytes()
    updater.verify("2.0.2", data, sig)
    root = updater.safe_extract(data, tmp_path / "out", "2.0.2")
    assert (root / "install.sh").stat().st_mode & 0o111
    monkeypatch.setenv("PDFDESK_SIGNING_PASSPHRASE", "the wrong passphrase")
    with pytest.raises(SystemExit):
        mr.main(["build"])
    monkeypatch.setenv("PDFDESK_SIGNING_PASSPHRASE", "a long test passphrase")
    (repo / "notes.txt").write_text("not shipped")
    (repo / "pdfdesk" / "assets" / "oops.pem").write_text("secret")
    with pytest.raises(SystemExit):  # something that looks like a key is never packaged
        mr.main(["build"])
    (repo / "pdfdesk" / "assets" / "oops.pem").unlink()
    mr.main(["build"])
    assert not any(n.endswith("notes.txt") for n in zipfile.ZipFile(zpath).namelist())


# ------------------------------------------------------------------------------------- the GUI
def test_update_prompts(monkeypatch, tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QMessageBox
    app = QApplication.instance() or QApplication([])
    from pdfdesk import update_ui, ui
    from pdfdesk.mainwindow import MainWindow
    w = MainWindow()
    assert w.act["check_updates"].text() == "Check for updates..."
    shown = []

    def settle():
        import time
        for _ in range(400):
            app.processEvents()
            if w.updates._job is None:
                break
            time.sleep(0.01)
        app.processEvents()
    monkeypatch.setattr(ui, "information", lambda _p, title, text: shown.append(text))
    monkeypatch.setattr(ui, "warning", lambda _p, title, text: shown.append(text))

    def run(release=None, error=None):
        def fake():
            if error:
                raise updater.UpdateError(error)
            return release
        monkeypatch.setattr(updater, "latest_release", fake)
        w.updates.check_now()
        settle()

    run({"version": (2, 0, 1), "version_text": "2.0.1"})
    assert "latest version" in shown[-1]
    run(error="Couldn't reach GitHub (test).")
    assert "Couldn't reach GitHub" in shown[-1]

    asked = []

    def fake_exec(box):
        asked.append((box.text(), [b.text() for b in box.buttons()], box.detailedText()))
        return 0
    monkeypatch.setattr(QMessageBox, "exec", fake_exec)
    rel = {"version": (2, 0, 2), "version_text": "2.0.2", "page": updater.RELEASES_PAGE, "notes": "<b>notes</b>",
           "zip_url": None, "sig_url": None}
    run(rel)
    text, buttons, details = asked[-1]
    assert "2.0.2 is available" in text and "Open download page" in buttons and details == "<b>notes</b>"
    # a copy that can update itself offers to install
    monkeypatch.setattr(updater, "why_not_automatic", lambda _r=None: "")
    run(dict(rel, zip_url="https://github.com/a.zip", sig_url="https://github.com/a.sig"))
    assert "Install now" in asked[-1][1] and "close, update itself" in asked[-1][0]
    # a skipped version isn't offered again by the start-up check, but Help > Check for updates still does
    w.settings.set("skipped_version", "2.0.2")
    monkeypatch.setattr(updater, "due", lambda _s: True)
    before = len(asked)
    w.updates.check_in_background()
    settle()
    assert len(asked) == before
    # after an update, the restarted app says how it went
    monkeypatch.setenv("PDFDESK_UPDATE_RESULT", "failed:/tmp/update.log")
    update_ui.report_last_update(w)
    assert "/tmp/update.log" in shown[-1] and "PDFDESK_UPDATE_RESULT" not in os.environ
    w.settings.set("skipped_version", "")
    w.close()


def test_install_now_reaches_the_installer(monkeypatch, tmp_path):
    """Install now -> download -> close -> installer, and the ways it stops instead."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pytest.importorskip("PySide6")
    import time
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from pdfdesk import ui
    from pdfdesk.mainwindow import MainWindow
    calls = []
    monkeypatch.setattr(updater, "prepare", lambda rel, progress=None, cancelled=None: {"work": tmp_path, "v": 1})
    monkeypatch.setattr(updater, "start_install", lambda prepared: calls.append(("install", prepared)))
    monkeypatch.setattr(updater, "discard", lambda prepared: calls.append(("discard", prepared)))
    monkeypatch.setattr(QApplication, "quit", staticmethod(lambda: calls.append(("quit", None))))
    monkeypatch.setattr(ui, "information", lambda _p, _t, text: calls.append(("info", text)))
    monkeypatch.setattr(ui, "warning", lambda _p, _t, text: calls.append(("warn", text)))
    rel = {"version": (2, 0, 2), "version_text": "2.0.2"}

    def run(w):
        w.updates._download(rel)
        for _ in range(400):
            app.processEvents()
            if w.updates._job is None:
                break
            time.sleep(0.01)
        app.processEvents()

    w = MainWindow()
    monkeypatch.setattr(updater, "other_copies_running", lambda: ["123.lock"])
    run(w)
    assert calls[-2][0] == "discard" and "Another PDF Desk window" in calls[-1][1]
    monkeypatch.setattr(updater, "other_copies_running", lambda: [])
    calls.clear()
    run(w)
    assert [c[0] for c in calls] == ["install", "quit"] and not w.isVisible()
