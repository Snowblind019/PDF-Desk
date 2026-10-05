#!/usr/bin/env bash
# Removes PDF Desk for the current user.
#   ./uninstall.sh          keep your settings, recent files list and saved signatures
#   ./uninstall.sh --purge  remove those too
set -euo pipefail

DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
rm -rf "$DATA/pdf-desk"
rm -f "$HOME/.local/bin/pdfdesk"
rm -f "$DATA/applications/pdfdesk.desktop"
rm -f "$DATA/icons/hicolor/scalable/apps/pdfdesk.svg" "$DATA/icons/hicolor/256x256/apps/pdfdesk.png"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DATA/applications" >/dev/null 2>&1 || true

if [ "${1:-}" = "--purge" ]; then
    rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/pdfdesk" "${XDG_CACHE_HOME:-$HOME/.cache}/pdfdesk"
    echo "PDF Desk and its settings were removed."
else
    echo "PDF Desk was removed. Settings and signatures are still in ${XDG_CONFIG_HOME:-$HOME/.config}/pdfdesk"
    echo "(run with --purge to delete them)."
fi
