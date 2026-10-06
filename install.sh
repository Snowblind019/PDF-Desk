#!/usr/bin/env bash
# PDF Desk installer for Linux (Fedora, Ubuntu/Debian, Arch and others). No root needed.
#
#   ./install.sh               install or update for the current user
#   ./install.sh --default     also make PDF Desk the default app for PDFs
#   ./install.sh --extras      also install LibreOffice, OCR language files and a voice for
#                              Read Out Loud (asks for sudo)
#   ./install.sh --wheels DIR  install Python packages from a local folder (no internet)
#   ./install.sh --unlocked    use the newest package versions instead of the tested, hash-checked ones
#
# By default the Python packages come from requirements.lock: exact versions with SHA-256 hashes,
# so pip refuses any package file that isn't byte-for-byte the one PDF Desk was tested with.
#
# Installs to:
#   ~/.local/share/pdf-desk/      program files and a private Python environment
#   ~/.local/bin/pdfdesk          command to start it
#   ~/.local/share/applications/  menu entry (shows up in "Open with" for PDFs)
set -euo pipefail

APP_NAME="PDF Desk"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="${XDG_DATA_HOME:-$HOME/.local/share}/pdf-desk"
BIN="$HOME/.local/bin"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICONS="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"

MAKE_DEFAULT=0
EXTRAS=0
WHEELS=""
UNLOCKED=0
while [ $# -gt 0 ]; do
    case "$1" in
        --default) MAKE_DEFAULT=1 ;;
        --extras) EXTRAS=1 ;;
        --wheels) WHEELS="$(cd "$2" && pwd)"; shift ;;
        --unlocked) UNLOCKED=1 ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
    shift
done

say() { printf '\033[1;31m==>\033[0m %s\n' "$*"; }

# ---- find Python (3.12+ for the tested package set, 3.10+ with --unlocked)
MINOR=12
[ "$UNLOCKED" = 1 ] && MINOR=10
PY=""
for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
        if "$cand" -c "import sys; sys.exit(0 if (3, $MINOR) <= sys.version_info < (3, 15) else 1)" 2>/dev/null; then
            PY="$(command -v "$cand")"
            break
        fi
    fi
done
if [ -z "$PY" ]; then
    echo "Python 3.$MINOR or newer is needed."
    echo "  Fedora: sudo dnf install python3     Ubuntu/Debian: sudo apt install python3 python3-venv"
    exit 1
fi
if ! "$PY" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    echo "Python's venv module is missing."
    echo "  Ubuntu/Debian: sudo apt install python3-venv"
    exit 1
fi
say "Using $("$PY" --version) at $PY"

# ---- optional extras through the system package manager
if [ "$EXTRAS" = 1 ]; then
    say "Installing LibreOffice, OCR language files (English and Romanian) and the espeak-ng voice"
    if command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y libreoffice-writer libreoffice-calc libreoffice-impress \
            tesseract-langpack-eng tesseract-langpack-ron espeak-ng || true
    elif command -v apt-get >/dev/null 2>&1; then
        sudo apt-get install -y libreoffice-writer libreoffice-calc libreoffice-impress \
            tesseract-ocr-eng tesseract-ocr-ron espeak-ng || true
    elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --needed --noconfirm libreoffice-fresh tesseract-data-eng tesseract-data-ron \
            espeak-ng || true
    elif command -v zypper >/dev/null 2>&1; then
        sudo zypper install -y libreoffice-writer libreoffice-calc libreoffice-impress \
            tesseract-ocr-traineddata-english espeak-ng || true
    else
        echo "Unknown package manager. Install LibreOffice, Tesseract language data and espeak-ng yourself."
    fi
fi

# ---- copy the program
say "Copying program files to $DEST"
mkdir -p "$DEST"
rm -rf "$DEST/app.new"
mkdir -p "$DEST/app.new"
cp -r "$SRC/pdfdesk" "$SRC/pdfdesk.py" "$SRC/requirements.txt" "$SRC/requirements.lock" "$DEST/app.new/"
[ -f "$SRC/uninstall.sh" ] && cp "$SRC/uninstall.sh" "$DEST/"
find "$DEST/app.new" -name "__pycache__" -type d -prune -exec rm -rf {} +
rm -rf "$DEST/app"
mv "$DEST/app.new" "$DEST/app"

# ---- private Python environment
if [ -x "$DEST/venv/bin/python" ] && \
   ! "$DEST/venv/bin/python" -c "import sys; sys.exit(0 if sys.version_info >= (3, $MINOR) else 1)"; then
    say "Replacing the old Python environment (it uses an older Python)"
    rm -rf "$DEST/venv"
fi
if [ ! -x "$DEST/venv/bin/python" ]; then
    say "Creating a private Python environment"
    "$PY" -m venv "$DEST/venv"
fi
say "Installing Python packages (first time takes a few minutes)"
PIP=("$DEST/venv/bin/python" -m pip install --disable-pip-version-check)
[ -n "$WHEELS" ] && PIP+=(--no-index --find-links "$WHEELS")
if [ "$UNLOCKED" = 1 ]; then
    "${PIP[@]}" --upgrade -r "$DEST/app/requirements.txt"
else
    "${PIP[@]}" --require-hashes -r "$DEST/app/requirements.lock"
fi

# ---- launcher
mkdir -p "$BIN"
cat > "$BIN/pdfdesk" <<EOF
#!/usr/bin/env bash
exec "$DEST/venv/bin/python" "$DEST/app/pdfdesk.py" "\$@"
EOF
chmod +x "$BIN/pdfdesk"

# ---- icon and menu entry
mkdir -p "$ICONS/scalable/apps" "$ICONS/256x256/apps" "$APPS"
cp "$SRC/pdfdesk/assets/pdfdesk.svg" "$ICONS/scalable/apps/pdfdesk.svg"
cp "$SRC/pdfdesk/assets/pdfdesk.png" "$ICONS/256x256/apps/pdfdesk.png"
cat > "$APPS/pdfdesk.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=$APP_NAME
GenericName=PDF Editor
Comment=View, edit, convert and sign PDF files offline
Exec="$BIN/pdfdesk" %F
Icon=pdfdesk
Terminal=false
Categories=Office;Viewer;Graphics;
MimeType=application/pdf;application/x-pdf;
Keywords=pdf;viewer;editor;convert;sign;merge;split;ocr;
StartupWMClass=pdfdesk
Actions=new-window;

[Desktop Action new-window]
Name=New window
Exec="$BIN/pdfdesk" --new-window
EOF
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q "$ICONS" >/dev/null 2>&1 || true

if [ "$MAKE_DEFAULT" = 1 ] && command -v xdg-mime >/dev/null 2>&1; then
    xdg-mime default pdfdesk.desktop application/pdf
    say "PDF Desk is now the default app for PDF files"
fi

# ---- check that it starts
if "$DEST/venv/bin/python" -c "import sys; sys.path.insert(0, sys.argv[1]); import pymupdf, PySide6.QtWidgets, pdfdesk.mainwindow" "$DEST/app" 2>/dev/null; then
    say "Installed. Start it from your app menu, or run: pdfdesk"
else
    echo "Installed, but a quick check failed. Run '$BIN/pdfdesk' in a terminal to see the error."
fi
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "Note: $BIN is not on your PATH, so use the menu entry or add it to PATH." ;;
esac

# ---- report optional extras
if ! command -v soffice >/dev/null 2>&1 && ! command -v libreoffice >/dev/null 2>&1; then
    echo "Optional: install LibreOffice for .doc/.xls/.ppt/OpenDocument conversions (or rerun with --extras)."
fi
HAVE_OCR=0
for f in /usr/share/tesseract*/tessdata/*.traineddata /usr/share/tesseract-ocr/*/tessdata/*.traineddata \
         /usr/share/tessdata/*.traineddata; do
    if [ -e "$f" ]; then HAVE_OCR=1; break; fi
done
if [ "$HAVE_OCR" = 0 ]; then
    echo "Optional: install OCR language files for Recognize Text (or rerun with --extras)."
fi
if ! command -v espeak-ng >/dev/null 2>&1 && ! command -v espeak >/dev/null 2>&1 && \
   ! command -v spd-say >/dev/null 2>&1; then
    echo "Optional: install espeak-ng for Read Out Loud (or rerun with --extras)."
fi
