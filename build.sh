#!/usr/bin/env bash
# Builds a standalone PDF Desk for Linux into dist/PDF Desk/ plus a .tar.gz you can copy to
# another computer and run without installing Python. Needs internet once, for the build.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

python3 -m venv .build-venv
.build-venv/bin/python -m pip install --upgrade pip >/dev/null
.build-venv/bin/python -m pip install --require-hashes -r requirements.lock
.build-venv/bin/python -m pip install pyinstaller
.build-venv/bin/pyinstaller --noconfirm --clean pdfdesk.spec

cp pdfdesk/assets/pdfdesk.svg "dist/PDF Desk/"
cat > "dist/PDF Desk/install-desktop-entry.sh" <<'EOF'
#!/usr/bin/env bash
# Adds this standalone PDF Desk to your app menu (run it from inside the extracted folder).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS" "$HOME/.local/bin"
ln -sf "$HERE/pdfdesk" "$HOME/.local/bin/pdfdesk"
cat > "$APPS/pdfdesk.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=PDF Desk
Comment=View, edit, convert and sign PDF files offline
Exec="$HERE/pdfdesk" %F
Icon=$HERE/pdfdesk.svg
Terminal=false
Categories=Office;Viewer;
MimeType=application/pdf;
StartupWMClass=pdfdesk
DESK
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" || true
echo "Added PDF Desk to your app menu."
EOF
chmod +x "dist/PDF Desk/install-desktop-entry.sh"

tar -C dist -czf "dist/PDF-Desk-linux-$(uname -m).tar.gz" "PDF Desk"
echo "Built: dist/PDF Desk/pdfdesk  and  dist/PDF-Desk-linux-$(uname -m).tar.gz"
