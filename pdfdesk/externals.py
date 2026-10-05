"""Optional helpers that live outside Python: LibreOffice (Office conversions)
and Tesseract language data (OCR). Both are found locally, nothing is downloaded."""
from __future__ import annotations

import glob
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from pdfdesk.config import cache_dir, settings, user_tessdata_dir

IS_WINDOWS = sys.platform.startswith("win")
NO_WINDOW = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW


class ExternalToolMissing(RuntimeError):
    pass


def find_program(name: str) -> str | None:
    """Find a program on PATH. Unlike shutil.which on Windows, this never looks in the current
    folder, so a program planted next to a PDF can't be picked up by mistake."""
    exts = [""]
    if IS_WINDOWS:
        exts = [e.lower() for e in os.environ.get("PATHEXT", ".EXE;.BAT;.CMD").split(";") if e]
        if os.path.splitext(name)[1]:
            exts = [""]
    try:
        cwd = os.path.normcase(os.path.abspath(os.getcwd()))
    except OSError:
        cwd = ""
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        folder = folder.strip().strip('"')
        if not folder or folder == "." or not os.path.isabs(folder):
            continue
        if os.path.normcase(os.path.abspath(folder)) == cwd:
            continue
        for ext in exts:
            candidate = os.path.join(folder, name + ext)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


# --------------------------------------------------------------------------- LibreOffice

def _windows_office_candidates() -> list[str]:
    roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
             os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")]
    found = []
    for root in roots:
        if not root:
            continue
        found += glob.glob(os.path.join(root, "LibreOffice*", "program", "soffice.exe"))
    return found


def libreoffice_command() -> list[str] | None:
    """Return the command prefix used to run LibreOffice headless, or None."""
    custom = (settings().get("libreoffice_path") or "").strip()
    if custom and os.path.exists(custom):
        return [custom]
    for name in ("soffice", "libreoffice"):
        path = find_program(name)
        if path:
            return [path]
    if IS_WINDOWS:
        for path in _windows_office_candidates():
            if os.path.exists(path):
                return [path]
    else:
        for path in ("/usr/bin/soffice", "/usr/lib/libreoffice/program/soffice",
                     "/usr/lib64/libreoffice/program/soffice", "/opt/libreoffice/program/soffice",
                     "/snap/bin/libreoffice"):
            if os.path.exists(path):
                return [path]
        flatpak = find_program("flatpak")
        if flatpak:
            try:
                res = subprocess.run([flatpak, "info", "org.libreoffice.LibreOffice"],
                                     capture_output=True, timeout=10)
                if res.returncode == 0:
                    return [flatpak, "run", "org.libreoffice.LibreOffice"]
            except Exception:
                pass
    return None


def libreoffice_available() -> bool:
    return libreoffice_command() is not None


# Settings for PDF Desk's private LibreOffice profile: never fetch linked images or other content
# from the internet or network shares, never run macros, never activate embedded objects or DDE
# links, never update links in Writer or Calc, and never recalculate spreadsheet formulas on load.
_LO_SETTINGS = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" \
xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="BlockUntrustedRefererLinks" \
oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableMacrosExecution" \
oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" \
oor:op="fuse"><value>3</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="DisableActiveContent" \
oor:op="fuse"><value>true</value></prop></item>
<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse">\
<value>0</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse">\
<value>1</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="OOXMLRecalcMode" oor:op="fuse">\
<value>1</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="ODFRecalcMode" oor:op="fuse">\
<value>1</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Misc"><prop oor:name="ShowTipOfTheDay" oor:op="fuse">\
<value>false</value></prop></item>
</oor:items>
"""


def _harden_libreoffice_profile(profile: Path) -> None:
    user = profile / "user"
    user.mkdir(parents=True, exist_ok=True)
    (user / "registrymodifications.xcu").write_text(_LO_SETTINGS, encoding="utf-8")


def libreoffice_convert(src: str, target_ext: str, timeout: int = 300) -> bytes:
    """Convert a file with LibreOffice and return the bytes of the result.

    target_ext is e.g. 'pdf', 'docx', 'odt'. A private LibreOffice profile is used
    so this still works while LibreOffice itself is open."""
    cmd = libreoffice_command()
    if not cmd:
        raise ExternalToolMissing(
            "LibreOffice is needed for this conversion but was not found.\n"
            "Install LibreOffice (free) or set its path in Preferences.")
    work = Path(tempfile.mkdtemp(prefix="convert-", dir=cache_dir()))
    profile = cache_dir() / "lo-profile"
    _harden_libreoffice_profile(profile)
    try:
        # Copy the source into the work folder so odd paths and locked files are not a problem.
        local_src = work / ("input" + Path(src).suffix.lower())
        shutil.copyfile(src, local_src)
        out_dir = work / "out"
        out_dir.mkdir()
        filter_arg = target_ext
        if target_ext == "pdf":
            filter_arg = "pdf"
        args = cmd + [f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore",
                      "--nolockcheck", "--convert-to", filter_arg, "--outdir", str(out_dir), str(local_src)]
        res = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                             creationflags=NO_WINDOW)
        outputs = list(out_dir.glob("*"))
        if not outputs:
            detail = (res.stderr or res.stdout or "").strip()
            raise RuntimeError("LibreOffice could not convert this file." + (f"\n\n{detail[-800:]}" if detail else ""))
        return outputs[0].read_bytes()
    except subprocess.TimeoutExpired:
        raise RuntimeError("LibreOffice took too long and was stopped.")
    finally:
        shutil.rmtree(work, ignore_errors=True)


# --------------------------------------------------------------------------- Tesseract data

def _tessdata_candidates() -> list[str]:
    paths = []
    custom = (settings().get("tessdata_path") or "").strip()
    if custom:
        paths.append(custom)
    if os.environ.get("TESSDATA_PREFIX"):
        paths.append(os.environ["TESSDATA_PREFIX"])
    paths.append(str(user_tessdata_dir()))
    if IS_WINDOWS:
        for root in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Tesseract-OCR")):
            if root:
                paths.append(os.path.join(root, "Tesseract-OCR", "tessdata"))
                paths.append(os.path.join(root, "tessdata"))
    else:
        paths += ["/usr/share/tesseract/tessdata", "/usr/share/tessdata", "/usr/local/share/tessdata",
                  "/usr/share/tesseract-ocr/tessdata"]
        paths += sorted(glob.glob("/usr/share/tesseract-ocr/*/tessdata"), reverse=True)
        paths += sorted(glob.glob("/usr/share/tesseract*/tessdata"), reverse=True)
    exe = find_program("tesseract")
    if exe:
        try:
            res = subprocess.run([exe, "--list-langs"], capture_output=True, text=True, timeout=10,
                                 creationflags=NO_WINDOW)
            first = (res.stdout or res.stderr).splitlines()[0] if (res.stdout or res.stderr) else ""
            if '"' in first:
                paths.append(first.split('"')[1])
        except Exception:
            pass
    return paths


def _langs_in(folder: str) -> list[str]:
    try:
        return sorted(p.stem for p in Path(folder).glob("*.traineddata") if p.stem != "osd")
    except Exception:
        return []


def find_tessdata() -> tuple[str | None, list[str]]:
    """Return (tessdata folder, available language codes)."""
    for folder in _tessdata_candidates():
        if folder and os.path.isdir(folder):
            langs = _langs_in(folder)
            if langs:
                return folder.rstrip("/\\"), langs
    return None, []


def ocr_available() -> bool:
    folder, langs = find_tessdata()
    return bool(folder and langs)


LANGUAGE_NAMES = {
    "eng": "English", "ron": "Romanian", "rus": "Russian", "ukr": "Ukrainian", "deu": "German",
    "fra": "French", "spa": "Spanish", "ita": "Italian", "por": "Portuguese", "pol": "Polish",
    "nld": "Dutch", "hun": "Hungarian", "ces": "Czech", "bul": "Bulgarian", "ell": "Greek",
    "tur": "Turkish", "chi_sim": "Chinese (Simplified)", "chi_tra": "Chinese (Traditional)",
    "jpn": "Japanese", "kor": "Korean", "ara": "Arabic", "heb": "Hebrew", "hin": "Hindi",
    "swe": "Swedish", "nor": "Norwegian", "dan": "Danish", "fin": "Finnish", "srp": "Serbian",
    "hrv": "Croatian", "slk": "Slovak", "slv": "Slovenian", "lit": "Lithuanian", "lav": "Latvian",
    "est": "Estonian", "vie": "Vietnamese", "tha": "Thai", "ind": "Indonesian",
}


def language_label(code: str) -> str:
    return f"{LANGUAGE_NAMES.get(code, code)} ({code})"
