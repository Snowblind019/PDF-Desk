#!/usr/bin/env python3
"""Make a signed PDF Desk release that installed copies can update to.

One time only, on your own computer:

    python3 tools/make_release.py init

  Creates the release signing key. The private key is saved outside the repo, locked with a
  passphrase you choose (keep a backup of the key file and the passphrase somewhere safe: without
  them you can't sign updates). The public key is written to pdfdesk/assets/update-key.pub:
  commit that file.

For each release (after changing __version__ in pdfdesk/__init__.py and committing):

    python3 tools/make_release.py build

  Writes dist/PDF-Desk-<version>.zip and dist/PDF-Desk-<version>.zip.sig. Publish both as assets
  of a GitHub release tagged v<version>, for example with the GitHub CLI:

    gh release create v2.0.2 dist/PDF-Desk-2.0.2.zip dist/PDF-Desk-2.0.2.zip.sig --title "PDF Desk 2.0.2"

    python3 tools/make_release.py verify dist/PDF-Desk-2.0.2.zip

  Checks a zip and its .sig against the public key in the repo.

Needs the "cryptography" package. PDF Desk's own environment has it, so this works:
    ~/.local/share/pdf-desk/venv/bin/python tools/make_release.py build          (Linux)
    & "$env:LOCALAPPDATA\\Programs\\PDF Desk\\venv\\Scripts\\python.exe" tools\\make_release.py build   (Windows)
"""
from __future__ import annotations

import argparse
import base64
import getpass
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pdfdesk import updater  # noqa: E402  (the same version and message rules the app uses)

PUBLIC_KEY = ROOT / "pdfdesk" / "assets" / "update-key.pub"
# What goes into a release. Anything else in the folder (keys, notes, build output) stays out.
SHIP = ("pdfdesk", "pdfdesk.py", "requirements.txt", "requirements.lock", "install.sh", "install.ps1", "install.bat",
        "uninstall.sh", "uninstall.ps1", "README.md", "SECURITY-REVIEW.md", "docs", "tests", "tools", "build.sh",
        "build.ps1", "pdfdesk.spec", "installer.iss", ".gitignore", ".gitattributes")
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache"}
SECRET_NAMES = re.compile(r"(\.pem|\.key|\.p12|\.pfx|\.kdbx|\.env|signing-key\.json|id_rsa|id_ed25519)$"
                          r"|signing-key|\.old-\d", re.I)
SCRYPT = {"n": 2 ** 17, "r": 8, "p": 1}


def key_path() -> Path:
    if os.environ.get("PDFDESK_SIGNING_KEY"):
        return Path(os.environ["PDFDESK_SIGNING_KEY"])
    if sys.platform.startswith("win"):  # Local, not Roaming, so it doesn't sync anywhere
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "pdf-desk-release" / "update-signing-key.json"


def passphrase(confirm: bool) -> bytes:
    from_env = os.environ.get("PDFDESK_SIGNING_PASSPHRASE")  # for scripted builds; typing it is safer
    first = from_env if from_env else getpass.getpass("Passphrase for the release key: ")
    if confirm:
        if len(first) < 12:
            sys.exit("Use at least 12 characters (a few unrelated words works well).")
        if not from_env and getpass.getpass("Type it again: ") != first:
            sys.exit("The two passphrases don't match.")
    return first.encode()


def app_version() -> str:
    text = (ROOT / "pdfdesk" / "__init__.py").read_text("utf-8")
    m = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    v = updater.parse_version(m.group(1) if m else "")
    if v is None:
        sys.exit("__version__ in pdfdesk/__init__.py isn't a plain version like 2.0.1.")
    return updater.version_text(v)


# ---------------------------------------------------------------------------------- the key file
def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _wrap(raw_private: bytes, raw_public: bytes, secret: bytes) -> dict:
    """The private key, encrypted with AES-256-GCM under a key made from the passphrase with scrypt."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    salt, nonce = os.urandom(16), os.urandom(12)
    kek = Scrypt(salt=salt, length=32, **SCRYPT).derive(secret)
    return {"format": "pdf-desk-release-key-1", "kdf": "scrypt", **SCRYPT, "salt": _b64(salt),
            "nonce": _b64(nonce), "public": _b64(raw_public),
            "private": _b64(AESGCM(kek).encrypt(nonce, raw_private, raw_public))}


def load_private_key():
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
    path = key_path()
    if not path.exists():
        sys.exit(f"No release key at {path}. Run 'init' first (once).")
    try:
        d = json.loads(path.read_text("utf-8"))
        if d.get("format") != "pdf-desk-release-key-1" or d.get("kdf") != "scrypt":
            raise ValueError("unknown format")
        raw = {k: base64.b64decode(d[k], validate=True) for k in ("salt", "nonce", "public", "private")}
        kek = Scrypt(salt=raw["salt"], length=32, n=int(d["n"]), r=int(d["r"]), p=int(d["p"])).derive(
            passphrase(False))
        private = AESGCM(kek).decrypt(raw["nonce"], raw["private"], raw["public"])
    except InvalidTag:
        sys.exit("Wrong passphrase for the release key.")
    except (ValueError, KeyError, TypeError):
        sys.exit(f"The release key file {path} is damaged.")
    return Ed25519PrivateKey.from_private_bytes(private)


def _inside_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(ROOT)
        return True
    except ValueError:
        return False


# ------------------------------------------------------------------------------------- commands
def cmd_init(force: bool) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    path = key_path()
    if _inside_repo(path):
        sys.exit("Keep the release key outside the repo, so it can never be committed or packaged.")
    if path.exists() and not force:
        sys.exit(f"A release key already exists at {path}. Use --force only if you mean to replace it "
                 "(installed copies would then refuse updates signed with the new key).")
    secret = passphrase(confirm=True)
    key = Ed25519PrivateKey.generate()
    raw_priv = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                                 serialization.NoEncryption())
    raw_pub = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    data = json.dumps(_wrap(raw_priv, raw_pub, secret), indent=1).encode()
    if path.exists():
        backup = path.with_name(path.name + time.strftime(".old-%Y%m%d-%H%M%S"))
        path.rename(backup)
        print(f"The old key was moved to {backup}")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    PUBLIC_KEY.write_text("# PDF Desk release key (Ed25519). Updates must be signed with its private half.\n"
                          + _b64(raw_pub) + "\n", encoding="ascii")
    print(f"Private key: {path}  (keep it private and back it up, with its passphrase)")
    print(f"Public key:  {PUBLIC_KEY.relative_to(ROOT).as_posix()}  (commit this file)")


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, check=True).stdout.decode("utf-8")


def _shipped(rel: str) -> bool:
    return any(rel == s or rel.startswith(s + "/") for s in SHIP)


def release_files() -> list[Path]:
    """The files that go into the release: the shipped parts of the project, as committed."""
    if (ROOT / ".git").exists():
        try:
            dirty = [ln for ln in _git("status", "--porcelain").splitlines() if _shipped(ln[3:].strip('"'))]
            tracked = [p for p in _git("ls-files", "-z").split("\0") if p]
        except (OSError, subprocess.CalledProcessError):
            sys.exit("Couldn't ask git which files are committed.")
        if dirty:
            sys.exit("Commit or undo these changes first, so the release matches what's in the repo:\n  "
                     + "\n  ".join(dirty[:20]))
        files = [ROOT / p for p in tracked if _shipped(p)]
    else:
        print("Note: this folder isn't a git checkout, so the files are packaged as they are on disk.")
        files = []
        for top in SHIP:
            start = ROOT / top
            if start.is_symlink():
                continue
            if start.is_file():
                files.append(start)
                continue
            for dirpath, dirnames, filenames in os.walk(start):
                here = Path(dirpath)
                dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not (here / d).is_symlink())
                files += [here / n for n in sorted(filenames) if not n.endswith((".pyc", ".pyo"))]
    files = sorted(f for f in files if f.is_file() and not f.is_symlink())
    secrets = [f.relative_to(ROOT).as_posix() for f in files if SECRET_NAMES.search(f.name)]
    if secrets:
        sys.exit("These look like private keys or secrets, so the release wasn't made:\n  " + "\n  ".join(secrets))
    return files


def build_zip(version: str) -> bytes:
    top = f"PDF-Desk-{version}"
    files = release_files()
    if "pdfdesk/assets/update-key.pub" not in {f.relative_to(ROOT).as_posix() for f in files}:
        sys.exit("pdfdesk/assets/update-key.pub isn't in the release. Run 'init' first and commit the file.")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for f in files:
            info = zipfile.ZipInfo(f"{top}/{f.relative_to(ROOT).as_posix()}", date_time=(2026, 1, 1, 0, 0, 0))
            executable = f.suffix == ".sh" or bool(f.stat().st_mode & stat.S_IXUSR)
            info.external_attr = ((0o755 if executable else 0o644) | stat.S_IFREG) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            data = f.read_bytes()
            if f.suffix == ".sh":  # a Windows checkout may have given them CRLF line ends, which bash can't run
                data = data.replace(b"\r\n", b"\n")
            zf.writestr(info, data)
    return buf.getvalue()


def cmd_build() -> None:
    version = app_version()
    for path, pattern in (("install.ps1", r'^\$Version = "([^"]+)"'), ("installer.iss", r'#define AppVersion "([^"]+)"')):
        m = re.search(pattern, (ROOT / path).read_text("utf-8"), re.M)
        if not m or updater.parse_version(m.group(1)) != updater.parse_version(version):
            print(f"Note: the version in {path} isn't {version}.")
    if _inside_repo(key_path()):
        sys.exit("The release key is inside the repo. Move it out (and make a new one if it was ever committed).")
    if updater.public_key_bytes() is None:
        sys.exit("pdfdesk/assets/update-key.pub is missing. Run 'init' first and commit the file.")
    data = build_zip(version)
    key = load_private_key()
    sig = base64.b64encode(key.sign(updater.signed_message(version, data))) + b"\n"
    # Check it exactly the way an installed copy will before writing anything
    updater.verify(version, data, sig, updater.public_key_bytes())
    with tempfile.TemporaryDirectory() as tmp:
        updater.safe_extract(data, Path(tmp), version)
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    zpath = dist / f"PDF-Desk-{version}.zip"
    spath = zpath.with_name(zpath.name + ".sig")
    zpath.write_bytes(data)
    spath.write_bytes(sig)
    zrel, srel = zpath.relative_to(ROOT).as_posix(), spath.relative_to(ROOT).as_posix()
    print(f"Signed release ready:\n  {zrel}\n  {srel}\n")
    print(f"Publish both on a GitHub release tagged v{version}, for example:")
    print(f'  gh release create v{version} "{zrel}" "{srel}" --title "PDF Desk {version}"')


def cmd_verify(zpath: Path) -> None:
    m = re.match(r"^PDF-Desk-(\d+\.\d+\.\d+)\.zip$", zpath.name)
    if not m:
        sys.exit("Expected a file named like PDF-Desk-2.0.2.zip.")
    data = zpath.read_bytes()
    sig = zpath.with_name(zpath.name + ".sig").read_bytes()
    updater.verify(m.group(1), data, sig, updater.public_key_bytes())
    with tempfile.TemporaryDirectory() as tmp:
        updater.safe_extract(data, Path(tmp), m.group(1))
    print(f"{zpath.name}: signature OK for version {m.group(1)}.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Make a signed PDF Desk release.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_init = sub.add_parser("init", help="create the release signing key (once)")
    p_init.add_argument("--force", action="store_true", help="replace the key (the old one is kept as a backup)")
    sub.add_parser("build", help="build and sign dist/PDF-Desk-<version>.zip")
    p_ver = sub.add_parser("verify", help="check a release zip and its .sig")
    p_ver.add_argument("zip", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "init":
            cmd_init(args.force)
        elif args.cmd == "build":
            cmd_build()
        else:
            cmd_verify(args.zip)
    except updater.UpdateError as exc:
        sys.exit(str(exc))
    except ImportError:
        sys.exit("This needs the 'cryptography' package. Run it with PDF Desk's own Python (see the top of "
                 "this file) or: pip install cryptography")


if __name__ == "__main__":
    main()
