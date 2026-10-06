# PyInstaller recipe for a standalone PDF Desk (no Python needed on the target computer).
# Use build.sh (Linux) or build.ps1 (Windows) instead of calling this directly.
import sys

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None
is_windows = sys.platform.startswith("win")

datas = [("pdfdesk/icons", "pdfdesk/icons"), ("pdfdesk/assets", "pdfdesk/assets")]
binaries = []
hiddenimports = ["fitz", "pdfdesk.mainwindow"]
for pkg in ("pymupdf", "pymupdf_fonts", "docx", "pptx", "openpyxl", "pdf2docx", "fontTools", "pyhanko",
            "pyhanko_certvalidator", "asn1crypto", "cryptography", "tzlocal", "tzdata"):
    try:
        d, b, h = collect_all(pkg)
    except Exception:  # an optional package that isn't installed on this system (such as tzdata on Linux)
        continue
    datas += d
    binaries += b
    hiddenimports += h
hiddenimports += collect_submodules("markdown")

a = Analysis(
    ["pdfdesk.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "matplotlib", "IPython", "PySide6.QtWebEngineCore", "PySide6.Qt3DCore",
              "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia"],
    noarchive=False,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PDF Desk" if is_windows else "pdfdesk",
    icon="pdfdesk/assets/pdfdesk.ico",
    console=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, upx=False, name="PDF Desk")
