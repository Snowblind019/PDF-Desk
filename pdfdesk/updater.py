"""Update checks and updates from the PDF Desk GitHub releases.

This is the only part of PDF Desk that goes online, and only like this:

- It asks GitHub for the latest release (one HTTPS request to api.github.com). Nothing about the
  user or their files is sent, only a User-Agent with the PDF Desk version.
- Downloads come only from the PDF Desk release on github.com (and the GitHub hosts it redirects
  to). Every request is HTTPS with certificate checks, and any redirect to another host is refused.
- An update is installed only if its zip is signed with the PDF Desk release key (Ed25519). The
  public half of that key ships with PDF Desk in assets/update-key.pub; the private half never
  leaves the developer's computer. The signature covers the version number too, so an old signed
  release can't be passed off as a new one.
- The zip is checked before anything is unpacked (no absolute paths, no "..", no links, size
  limits), and the version inside it must match.
- Installing runs the update's own installer (install.sh / install.ps1), the same one used for a
  normal install, after PDF Desk has closed. Python packages still come from requirements.lock,
  hash-checked.

There's no Qt in here, so all of it can be tested without a display.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from pdfdesk import APP_NAME, __version__
from pdfdesk.locks import others_running, try_lock, unlock

REPO = "Snowblind019/PDF-Desk"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
# GitHub sends release downloads through these hosts. Anything else is refused.
ALLOWED_HOSTS = {"api.github.com", "github.com", "objects.githubusercontent.com",
                 "release-assets.githubusercontent.com"}

KEY_FILE = Path(__file__).resolve().parent / "assets" / "update-key.pub"
SIGNED_PREFIX = b"PDF-Desk update v1\n"

TIMEOUT = 20                     # seconds per network read
CHECK_DEADLINE = 60              # seconds for the whole update check
DOWNLOAD_DEADLINE = 15 * 60      # seconds for the whole download
MAX_JSON = 2 * 1024 * 1024       # the release description from GitHub
MAX_SIG = 4096
MAX_ZIP = 100 * 1024 * 1024      # the release zip
MAX_FILES = 5000
MAX_UNPACKED = 400 * 1024 * 1024
MAX_NOTES = 3000                 # characters of release notes shown to the user

_VERSION_RE = re.compile(r"^v?(\d{1,4})(?:\.(\d{1,4}))?(?:\.(\d{1,4}))?$", re.ASCII)
_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", "conin$", "conout$",
                     *(f"{d}{i}" for d in ("com", "lpt") for i in (*"123456789", "\u00b9", "\u00b2", "\u00b3"))}


class UpdateError(Exception):
    """Something went wrong; the message is meant for the user."""


# ------------------------------------------------------------------------------------ versions
def parse_version(text: str) -> tuple[int, int, int] | None:
    """'v2', '2.0' and 'v2.0.1' are versions; anything else (betas, odd tags) isn't."""
    m = _VERSION_RE.match((text or "").strip())
    if not m:
        return None
    return tuple(int(x or 0) for x in m.groups())  # type: ignore[return-value]


def version_text(v: tuple[int, int, int]) -> str:
    return ".".join(str(x) for x in v)


def current_version() -> tuple[int, int, int]:
    return parse_version(__version__) or (0, 0, 0)


# ------------------------------------------------------------------------------------- network
class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _check_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _check_url(url: str) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in ALLOWED_HOSTS or parts.port not in (None, 443):
        raise UpdateError("The update server sent PDF Desk somewhere unexpected, so the download was stopped.")


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - fall back to the system's certificates
        return ssl.create_default_context()


def _opener():
    return urllib.request.build_opener(_SafeRedirect(), urllib.request.HTTPSHandler(context=_ssl_context()))


def fetch(url: str, limit: int, accept: str = "*/*", progress=None, cancelled=None,
          deadline: float = CHECK_DEADLINE) -> bytes:
    """GET an allowed HTTPS URL and return at most `limit` bytes (more is an error), within `deadline`
    seconds overall."""
    _check_url(url)
    give_up = time.monotonic() + deadline
    req = urllib.request.Request(url, headers={"User-Agent": f"PDF-Desk/{__version__} (update check)",
                                               "Accept": accept})
    try:
        with _opener().open(req, timeout=TIMEOUT) as resp:
            _check_url(resp.geturl())
            total = int(resp.headers.get("Content-Length") or 0)
            if total > limit:
                raise UpdateError("The download is much bigger than expected, so it was stopped.")
            chunks, size = [], 0
            while True:
                if cancelled and cancelled():
                    raise UpdateError("Cancelled.")
                if time.monotonic() > give_up:
                    raise UpdateError("The download from GitHub is taking far too long, so it was stopped.")
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise UpdateError("The download is much bigger than expected, so it was stopped.")
                chunks.append(chunk)
                if progress:
                    progress(size, total)
            return b"".join(chunks)
    except UpdateError:
        raise
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise UpdateError("No published release was found on GitHub.") from None
        if exc.code in (403, 429):
            raise UpdateError("GitHub is limiting requests right now. Try again in an hour.") from None
        raise UpdateError(f"GitHub answered with an error ({exc.code}).") from None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        raise UpdateError(f"Couldn't reach GitHub ({_plain(str(reason), 120)}). Check the internet "
                          "connection and try again.") from None


def _plain(text: str, limit: int) -> str:
    """Release text from GitHub, made safe to show: no control or direction-changing characters,
    limited length."""
    text = "".join(ch for ch in str(text) if ch in "\n\t" or unicodedata.category(ch) not in ("Cc", "Cf", "Cs"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "..."


# ------------------------------------------------------------------------------------ releases
def latest_release(data: bytes | None = None) -> dict:
    """What GitHub says the latest release is. `data` is for tests (the API's JSON)."""
    if data is None:
        data = fetch(LATEST_URL, MAX_JSON, accept="application/vnd.github+json")
    try:
        info = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise UpdateError("GitHub sent an answer PDF Desk couldn't read.") from None
    if not isinstance(info, dict):
        raise UpdateError("GitHub sent an answer PDF Desk couldn't read.")
    tag = str(info.get("tag_name") or "")
    version = parse_version(tag)
    if version is None or info.get("draft") or info.get("prerelease"):
        raise UpdateError("The latest release on GitHub doesn't have a version number PDF Desk understands.")
    vtext = version_text(version)
    want = {f"PDF-Desk-{vtext}.zip": "zip", f"PDF-Desk-{vtext}.zip.sig": "sig"}
    found = {}
    for asset in info.get("assets") or []:
        if not isinstance(asset, dict):
            continue
        kind = want.get(str(asset.get("name") or ""))
        url = str(asset.get("browser_download_url") or "")
        if kind and url.startswith(DOWNLOAD_PREFIX) and "/../" not in url and "?" not in url:
            found[kind] = url
    page = f"{RELEASES_PAGE}/tag/{urllib.parse.quote(tag)}"
    return {"version": version, "version_text": vtext, "tag": tag, "page": page,
            "notes": _plain(info.get("body") or "", MAX_NOTES), "zip_url": found.get("zip"),
            "sig_url": found.get("sig")}


def is_newer(release: dict) -> bool:
    return release["version"] > current_version()


# --------------------------------------------------------------------------------- signatures
def public_key_bytes() -> bytes | None:
    """The release key PDF Desk trusts, or None when this copy has none."""
    try:
        lines = [ln.strip() for ln in KEY_FILE.read_text(encoding="ascii").splitlines()]
        raw = base64.b64decode("".join(ln for ln in lines if ln and not ln.startswith("#")), validate=True)
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return raw if len(raw) == 32 else None


def signed_message(version: str, zip_bytes: bytes) -> bytes:
    """What the release key signs: the version and the zip's SHA-256."""
    digest = hashlib.sha256(zip_bytes).hexdigest()
    return SIGNED_PREFIX + f"{version}\n{digest}\n".encode("ascii")


def verify(version: str, zip_bytes: bytes, sig_text: bytes, key: bytes | None = None) -> None:
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    key = key if key is not None else public_key_bytes()
    if key is None:
        raise UpdateError("This copy of PDF Desk has no update key, so it can't check where an update came from.")
    try:
        sig = base64.b64decode(sig_text.strip(), validate=True)
    except ValueError:
        raise UpdateError("The update's signature file is damaged, so it wasn't installed.") from None
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(sig, signed_message(version, zip_bytes))
    except (InvalidSignature, ValueError):
        raise UpdateError("The update isn't signed with the PDF Desk release key, so it wasn't installed. "
                          "Nothing was changed.") from None


# ---------------------------------------------------------------------------------- unpacking
def safe_extract(zip_bytes: bytes, dest: Path, version: str) -> Path:
    """Unpack a verified release zip into dest and return its top folder."""
    import io
    top = f"PDF-Desk-{version}"
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise UpdateError("The update file is damaged.") from None
    with zf:
        infos = zf.infolist()
        if not infos or len(infos) > MAX_FILES:
            raise UpdateError("The update file has an unexpected layout.")
        total, seen = 0, set()
        for info in infos:
            name = info.filename
            parts = name.rstrip("/").split("/")
            mode = (info.external_attr >> 16) & 0o170000
            if ("\\" in name or name.startswith("/") or ":" in name or parts[0] != top
                    or any(p in ("", ".", "..") for p in parts) or mode == stat.S_IFLNK
                    or any(p[-1] in ". " or p.split(".")[0].lower() in _WINDOWS_RESERVED for p in parts)):
                raise UpdateError("The update file has an unexpected layout.")
            key = name.rstrip("/").lower()
            if key in seen:
                raise UpdateError("The update file has an unexpected layout.")
            seen.add(key)
            total += info.file_size
            if total > MAX_UNPACKED:
                raise UpdateError("The update file is much bigger than expected.")
        dest.mkdir(parents=True, exist_ok=True)
        for info in infos:
            target = dest.joinpath(*info.filename.rstrip("/").split("/"))
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            executable = (info.external_attr >> 16) & 0o111
            os.chmod(target, 0o755 if executable else 0o644)
    root = dest / top
    for need in ("pdfdesk/__init__.py", "pdfdesk.py", "install.sh", "install.ps1", "requirements.lock"):
        if not (root / need).is_file():
            raise UpdateError("The update file is missing parts of PDF Desk.")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', (root / "pdfdesk" / "__init__.py").read_text("utf-8"), re.M)
    if not m or parse_version(m.group(1)) != parse_version(version):
        raise UpdateError("The version inside the update doesn't match the release.")
    return root


# -------------------------------------------------------------------------------- installation
def install_location() -> tuple[str, Path | None, str]:
    """(kind, install folder, reason). kind is 'linux' or 'windows' for copies installed with
    install.sh / install.ps1, which can update themselves, and 'other' for everything else."""
    if getattr(sys, "frozen", False):
        return "other", None, "This is a standalone build, so updates are installed by downloading the new version."
    app_dir = Path(__file__).resolve().parent.parent
    dest = app_dir.parent
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        expected = Path(local) / "Programs" / APP_NAME
        same = os.path.normcase(str(dest)) == os.path.normcase(str(expected.resolve()))
        venv_ok = (dest / "venv" / "Scripts" / "python.exe").is_file()
        kind = "windows"
    else:
        data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        expected = Path(data) / "pdf-desk"
        same = dest == expected.resolve()
        venv_ok = (dest / "venv" / "bin" / "python").is_file()
        kind = "linux"
    if app_dir.name == "app" and same and venv_ok:
        return kind, dest, ""
    return "other", None, "This copy of PDF Desk wasn't installed with the installer, so it can't update itself."


def _install_options(dest: Path) -> dict:
    opts = {}
    try:
        for line in (dest / "install-options").read_text("utf-8").splitlines():
            key, _, value = line.partition("=")
            opts[key.strip()] = value.strip()
    except OSError:
        pass
    return opts


def why_not_automatic(release: dict | None = None) -> str:
    """Empty when this copy can install `release` by itself, otherwise the reason it can't."""
    kind, dest, reason = install_location()
    if dest is None:
        return reason
    if not _locking_works:
        return ("File locking doesn't work in this computer's settings folder, so PDF Desk can't make sure "
                "every window is closed during an update. Download the new version and run its installer.")
    if _install_options(dest).get("unlocked") == "1":
        return ("This copy was installed with the newest, unpinned Python packages (--unlocked), so updates "
                "aren't installed automatically. Download the new version and run its installer the same way.")
    if public_key_bytes() is None:
        return "This copy of PDF Desk has no update key, so it can't check where an update came from."
    if release is not None and (not release.get("zip_url") or not release.get("sig_url")):
        return "The latest release on GitHub doesn't include a signed PDF Desk download."
    return ""


def installer_command(kind: str, dest: Path, root: Path) -> list[str]:
    if kind == "windows":
        system = os.environ.get("SystemRoot") or r"C:\Windows"
        ps = os.path.join(system, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
        return [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(root / "install.ps1")]
    bash = "/bin/bash" if os.path.exists("/bin/bash") else "/usr/bin/bash"
    return [bash, str(root / "install.sh")]


def relaunch_command(kind: str, dest: Path) -> list[str]:
    if kind == "windows":
        return [str(dest / "venv" / "Scripts" / "pythonw.exe"), str(dest / "app" / "pdfdesk.py")]
    return [str(dest / "venv" / "bin" / "python"), str(dest / "app" / "pdfdesk.py")]


# ------------------------------------------------------------- other copies of PDF Desk running
# Each running PDF Desk holds a lock on its own file in cache/running/. The operating system drops
# the lock when the process ends in any way (closed, killed, crashed, logged out), so a lock that can
# be taken belongs to a copy that isn't running any more. While an update installs, the helper holds
# cache/update.lock, and PDF Desk won't start until it's done.
_registration = None
_locking_works = True


def pid_alive(pid: int) -> bool:
    if sys.platform.startswith("win"):  # never os.kill here: on Windows that ends the process
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
        if not handle:
            return False
        try:
            return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: running
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _running_dir() -> Path:
    from pdfdesk.config import cache_dir
    path = cache_dir() / "running"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _update_lock_path() -> Path:
    return _running_dir().parent / "update.lock"


def register_process(wait: float = 3.0) -> bool:
    """Note that this PDF Desk is running, so an update waits until every window is closed. Returns
    False while an update is being installed (PDF Desk should not start then)."""
    global _registration, _locking_works
    if _registration is not None:
        return True
    try:
        gate = _take_gate(wait)
    except OSError:
        _locking_works = False  # e.g. a network home folder without file locking: no automatic installs
        return True
    if gate is None:
        return False
    try:
        folder = _running_dir()
        others_running(str(folder))  # also tidies up after copies that ended
        path = folder / f"{os.getpid()}.lock"
        for _ in range(5):
            fh = open(path, "a+b")
            if try_lock(fh) and _same_file(fh, path):
                _registration = fh  # held (and so locked) until this process ends
                break
            fh.close()
    except OSError:
        _locking_works = False
    finally:
        unlock(gate)
        gate.close()
    return True


def _take_gate(wait: float):
    """Take the update lock, waiting up to `wait` seconds. None if it stays taken."""
    gate = open(_update_lock_path(), "a+b")
    give_up = time.monotonic() + wait
    try:
        while not try_lock(gate):  # the helper takes it briefly while waiting, and for good while installing
            if time.monotonic() > give_up:
                gate.close()
                return None
            time.sleep(0.1)
    except OSError:
        gate.close()
        raise
    return gate


def _same_file(fh, path: Path) -> bool:
    """The lock is on the file that's really at `path` (not one removed in the meantime)."""
    try:
        a, b = os.fstat(fh.fileno()), os.stat(path)
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def other_copies_running() -> list[str]:
    try:
        gate = _take_gate(3.0)
    except OSError:
        return ["(file locking isn't available)"]
    if gate is None:
        return ["(an update is being installed)"]
    try:
        return others_running(str(_running_dir()), skip=f"{os.getpid()}.lock")
    finally:
        unlock(gate)
        gate.close()


def _private_folder(path: Path) -> None:
    """The update is unpacked here and then run: make sure nobody else can change it."""
    path.mkdir(parents=True, exist_ok=True)
    if sys.platform.startswith("win"):
        return  # inside the user's own profile, which other users can't write to
    for folder in (path.parent, path):
        st = folder.lstat()
        if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o002:
            raise UpdateError(f"The folder {folder} can be changed by other users, so the update was stopped.")
    os.chmod(path, 0o700)


# --------------------------------------------------------------------------------- installing
_HELPER = r"""
import json, os, subprocess, sys, time
cfg = json.load(open(sys.argv[1], encoding="utf-8"))
WIN = sys.platform.startswith("win")
"""  # + the code of locks.py, added in start_install
_HELPER_BODY = r"""
def relaunch(result):
    env = dict(os.environ)
    env["PDFDESK_UPDATE_RESULT"] = result
    flags = 0x00000008 | 0x00000200 if WIN else 0  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    with open(cfg["relaunch_log"], "w", encoding="utf-8") as out:
        subprocess.Popen(cfg["relaunch"], env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                         close_fds=True, creationflags=flags, start_new_session=not WIN)

# Wait until every PDF Desk has closed, then hold the update lock so none can start during the install
gate = open(cfg["update_lock"], "a+b")
deadline = time.time() + 180
while True:
    try:
        if try_lock(gate):
            if not others_running(cfg["running"]):
                break
            unlock(gate)
    except OSError:
        pass
    if time.time() > deadline:
        with open(cfg["log"], "w", encoding="utf-8") as log:
            log.write("PDF Desk was still open, so the update to %s wasn't installed.\n" % cfg["version"])
        relaunch("skipped:" + cfg["version"])
        sys.exit(0)
    time.sleep(0.5)
with open(cfg["log"], "w", encoding="utf-8") as log:
    log.write("Updating to PDF Desk %s\n" % cfg["version"])
    log.flush()
    try:
        code = subprocess.call(cfg["installer"], cwd=cfg["cwd"], stdin=subprocess.DEVNULL, stdout=log,
                               stderr=subprocess.STDOUT, creationflags=0x08000000 if WIN else 0)
    except OSError as exc:
        log.write("Couldn't start the installer: %s\n" % exc)
        code = 1
    log.write("Installer finished with code %s\n" % code)
unlock(gate)
gate.close()
relaunch(("ok:" + cfg["version"]) if code == 0 else ("failed:" + cfg["log"]))
"""


def prepare(release: dict, progress=None, cancelled=None) -> dict:
    """Download, verify and unpack an update. Returns what start_install needs. Nothing on the
    computer changes here except a new private folder in the cache."""
    reason = why_not_automatic(release)
    if reason:
        raise UpdateError(reason)
    kind, dest, _reason = install_location()
    vtext = release["version_text"]
    sig = fetch(release["sig_url"], MAX_SIG, cancelled=cancelled)
    data = fetch(release["zip_url"], MAX_ZIP, progress=progress, cancelled=cancelled, deadline=DOWNLOAD_DEADLINE)
    verify(vtext, data, sig)
    if cancelled and cancelled():
        raise UpdateError("Cancelled.")
    from pdfdesk.config import cache_dir
    base = cache_dir() / "updates"
    _private_folder(base)
    for old in base.glob("update-*"):  # leftovers from earlier updates
        shutil.rmtree(old, ignore_errors=True)
    work = Path(tempfile.mkdtemp(prefix="update-", dir=base))
    try:
        root = safe_extract(data, work / "files", vtext)
        if cancelled and cancelled():
            raise UpdateError("Cancelled.")
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return {"kind": kind, "dest": dest, "root": root, "work": work, "version": vtext}


def discard(prepared: dict | None) -> None:
    if prepared:
        shutil.rmtree(prepared["work"], ignore_errors=True)


def start_install(prepared: dict) -> None:
    """Start the helper that waits for every PDF Desk window to close, runs the installer and opens
    PDF Desk again. Call it right before quitting."""
    work = prepared["work"]
    helper = work / "update_helper.py"
    locks = (Path(__file__).resolve().parent / "locks.py").read_text(encoding="utf-8")
    helper.write_text(_HELPER + locks + _HELPER_BODY, encoding="utf-8")
    if _registration is None:
        raise OSError("PDF Desk couldn't register itself as running, so it can't update safely.")
    cfg = {"version": prepared["version"], "cwd": str(prepared["root"]),
           "installer": installer_command(prepared["kind"], prepared["dest"], prepared["root"]),
           "relaunch": relaunch_command(prepared["kind"], prepared["dest"]),
           "running": str(_running_dir()), "update_lock": str(_update_lock_path()),
           "log": str(work / "update.log"), "relaunch_log": str(work.parent / "restart.log")}
    config = work / "update.json"
    config.write_text(json.dumps(cfg), encoding="utf-8")
    # The helper runs with the base Python, not the one inside PDF Desk's own environment, so the
    # installer is free to replace that environment.
    python = getattr(sys, "_base_executable", None) or sys.executable
    args = [python, "-I", str(helper), str(config)]
    if sys.platform.startswith("win"):
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED, NEW_PROCESS_GROUP, NO_WINDOW
        subprocess.Popen(args, cwd=str(work), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
    else:
        subprocess.Popen(args, cwd=str(work), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True, start_new_session=True)


def clean_up_after_update() -> None:
    """After a successful update, remove the downloaded files."""
    try:
        from pdfdesk.config import cache_dir
        for old in (cache_dir() / "updates").glob("update-*"):
            shutil.rmtree(old, ignore_errors=True)
    except OSError:
        pass


def due(settings, now: float | None = None) -> bool:
    """True when the once-a-day automatic check should run."""
    if not settings.get("check_updates", True) or os.environ.get("PDFDESK_NO_UPDATE_CHECK"):
        return False
    try:
        last = float(settings.get("last_update_check") or 0)
    except (TypeError, ValueError):
        last = 0.0
    now = time.time() if now is None else now
    return now - last >= 20 * 3600 or now < last
