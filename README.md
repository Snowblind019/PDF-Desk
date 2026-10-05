# PDF Desk

PDF Desk is a PDF viewer, editor and converter for Linux and Windows. It covers the everyday things Adobe Acrobat is used for: reading, highlighting and commenting, filling and signing forms, editing text, moving pages around, redacting, compressing, password protecting, OCR, and turning other files into PDFs or PDFs into other files.

Everything runs on your own computer. PDF Desk never connects to the internet, so it works the same with no Wi-Fi. The only time a link opens your browser is when you click a web link inside a PDF, and it asks first. See [Security](#security) for details.

![Home screen with recent files](docs/screenshots/home.png)

## Features

**Home screen and recent files**
- Every PDF you open shows up on the Home screen with a thumbnail, its folder, when you last opened it and the page count
- Reopens files at the page where you left off
- Pin files to keep them at the top, filter the list, remove entries, or show the file in its folder
- Quick buttons for Open, Create PDF, Combine files, Blank PDF and From clipboard
- Drag any file onto the window to open it (or convert it)

**Viewing**
- Tabs for several documents at once
- Smooth scrolling, one page or two pages side by side (with or without a cover page)
- Zoom with Ctrl + mouse wheel, touchpad pinch, fit width, fit page or a typed percentage
- Sidebar with page thumbnails, bookmarks, a list of all comments, and search results
- Search the whole document with every match highlighted
- Clickable links, night mode for reading in the dark, full screen, light and dark themes
- Print through your normal system print dialog

![Editing and commenting](docs/screenshots/editing.png)

**Comment and mark up**
- Highlight, underline and strike through text (highlight also works as a box on scanned pages)
- Sticky notes, text boxes, freehand drawing, rectangles, ellipses, lines and arrows
- 14 standard stamps (Approved, Draft, Confidential...) plus text stamps with today's date
- Select any comment to move it, resize it, change its color, line width or opacity, or delete it
- Undo and redo for every change

**Edit**
- Edit text: click a line of text and type the new wording
- Add images and signatures. Draw, type or import your signature once and reuse it
- Fill in forms: text fields, check boxes, radio buttons and drop-down lists (Romanian letters like ș and ț work)
- Redact: mark text or areas, or use Find and Redact with ready-made patterns (emails, phone numbers, SSNs, card numbers, IP addresses, AWS account IDs, access keys and ARNs), then apply to remove the content for good
- Flatten comments and forms into the page, remove hidden information (metadata, scripts, attachments)

![Organize pages](docs/screenshots/organize.png)

**Pages**
- Organize Pages view: drag pages to reorder, rotate, duplicate, delete, insert blank pages or pages from any file
- Extract pages to a new PDF (as one file or one file per page)
- Split by every N pages, by page ranges, or by bookmarks
- Crop margins, add headers, footers and page numbers, add text or image watermarks (with a live preview)

**Create PDFs from other files**
- Images: PNG, JPEG, WebP, BMP, GIF, TIFF (including multi-page TIFF) and more
- Word, Excel, PowerPoint, OpenDocument, RTF and more (through LibreOffice when it is installed; .docx, .xlsx and .pptx also work without it, with a simpler layout)
- Text and code files, Markdown, HTML, CSV
- E-books and other documents: EPUB, MOBI, FB2, XPS, CBZ, SVG
- Combine many files of mixed types into one PDF, or paste an image or text from the clipboard

**Export PDFs to other formats**
- Word (.docx), Excel (.xlsx, tables become sheets), PowerPoint (.pptx)
- OpenDocument text (.odt) and Rich Text (.rtf) when LibreOffice is installed
- PNG, JPEG, WebP, multi-page TIFF and SVG images
- Plain text, HTML and Markdown

**Document tools**
- Reduce file size (clean up only, balanced, smallest, or custom image resolution and quality)
- Protect with a password (AES-256) and choose what people may do: print, copy, edit, comment, fill forms
- Remove a password
- Recognize text (OCR) in scanned PDFs, so they can be searched and copied. The pages keep looking exactly the same
- Edit the title, author, subject and keywords, and see page size, PDF version, fonts and security details

![Dark theme](docs/screenshots/dark.png)

## Install on Linux

You need Python 3.12 or newer (Fedora 39+ and Ubuntu 24.04+ already have it).

```bash
cd "PDF Desk"
./install.sh
```

That installs PDF Desk for your user only (no sudo), adds it to your app menu and to the "Open with" list for PDF files, and adds a `pdfdesk` command. The first install downloads the Python packages it needs, after that no internet is used.

Options:

| Option | What it does |
|---|---|
| `./install.sh --default` | Also make PDF Desk the default app for PDF files |
| `./install.sh --extras` | Also install LibreOffice and the English and Romanian OCR language files (asks for your sudo password) |
| `./install.sh --wheels DIR` | Install the Python packages from a folder instead of downloading them |
| `./install.sh --unlocked` | Use the newest package versions instead of the tested, hash-checked ones (also works with Python 3.10 and 3.11) |

Run `./install.sh` again any time to update. To remove it, run `~/.local/share/pdf-desk/uninstall.sh` (add `--purge` to also delete your settings and signatures).

## Install on Windows

1. Double-click `install.bat`. No administrator rights are needed.
2. If Python 3.12 or newer isn't installed yet, the installer asks whether to install it for your account with winget (type y to agree). You can also install it yourself from python.org (tick "Add python.exe to PATH").

PDF Desk is installed to `%LOCALAPPDATA%\Programs\PDF Desk`, gets a Start menu shortcut, shows up in the "Open with" list for PDF files, and can be removed from Settings > Apps like any other program.

Options (run from PowerShell in the PDF Desk folder):

```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -Desktop -AddToPath
```

| Option | What it does |
|---|---|
| `-Desktop` | Also put a shortcut on the desktop |
| `-AddToPath` | Add the `pdfdesk` command to your PATH |
| `-Wheels DIR` | Install the Python packages from a folder instead of downloading them |
| `-Unlocked` | Use the newest package versions instead of the tested, hash-checked ones |

To make PDF Desk the default PDF app on Windows, right-click any PDF, choose Open with > Choose another app, pick PDF Desk and tick "Always".

## Optional extras

PDF Desk works on its own. These two free extras add more:

**LibreOffice** gives full-quality conversion of Word, Excel, PowerPoint and OpenDocument files, and exporting to .odt and .rtf.
- Fedora: `sudo dnf install libreoffice`
- Ubuntu/Debian: `sudo apt install libreoffice`
- Windows: install it from libreoffice.org, then restart PDF Desk

**OCR language files** are needed for Recognize Text. PDF Desk has the OCR engine built in, it only needs the language data.
- Fedora: `sudo dnf install tesseract-langpack-eng tesseract-langpack-ron`
- Ubuntu/Debian: `sudo apt install tesseract-ocr-eng tesseract-ocr-ron`
- Windows: install Tesseract OCR (the UB Mannheim build) and tick the languages you want, or copy `.traineddata` files into `%APPDATA%\PDF Desk\tessdata`

You can point PDF Desk at a custom LibreOffice or language folder in Preferences > Extras.

## Standalone build (no Python on the target computer)

To get a folder you can copy to another computer and run straight away, even one without internet:

- Linux: `./build.sh` makes `dist/PDF Desk/pdfdesk` and `dist/PDF-Desk-linux-x86_64.tar.gz`. After extracting it somewhere, run `install-desktop-entry.sh` inside the folder to add it to your app menu.
- Windows: `powershell -ExecutionPolicy Bypass -File build.ps1` makes `dist\PDF Desk\PDF Desk.exe` and a zip. If Inno Setup 6 is installed it also makes `dist\PDF-Desk-Setup.exe`, a normal installer that doesn't need admin rights. Use either that Setup.exe or `install.bat` on a computer, not both, since they install to the same folder.

The build itself needs internet once to download the packages. The result is around 450 MB unpacked because it carries its own Python, Qt and the conversion libraries.

To install on a computer that has Python but no internet, download the packages on another computer first and copy the `wheels` folder over:

```bash
pip download --require-hashes -r requirements.lock -d wheels       # same OS and Python version as the target
./install.sh --wheels wheels                                       # Linux
powershell -ExecutionPolicy Bypass -File install.ps1 -Wheels wheels  # Windows
```

## Tips

- **Esc** goes back to the Select tool. The tool hint next to the toolbar says what the current tool does.
- **Text boxes:** click to start typing, or drag a box first. Click outside the box or press **Ctrl+Enter** to finish. Double-click a text box or note later to change it.
- **Selected comments:** drag to move, drag a corner handle to resize, use the color, width and opacity controls in the toolbar to restyle, arrow keys to nudge (Shift for bigger steps), Delete to remove.
- **Right-click** on the page for quick actions: copy, highlight, add a note, rotate, insert or delete the page, copy the page as an image.
- **Redaction** is two steps on purpose. Marked areas show a red outline. Nothing is removed until you click Apply redactions, and the file only changes when you save.
- **Edit text** works one line at a time and writes the new text with a standard font (Helvetica, Times or Courier style, matched to the original). Text that's part of an image needs OCR first and can't be edited this way.
- **Images and signatures** become part of the page. To move one, undo and place it again.
- Changes stay in PDF Desk until you save, and the tab title shows an asterisk while there are unsaved changes.

## Keyboard shortcuts

| Action | Keys |
|---|---|
| Open / Save / Save as | Ctrl+O / Ctrl+S / Ctrl+Shift+S |
| Print | Ctrl+P |
| Close tab / switch tabs | Ctrl+W / Ctrl+Tab |
| Home screen | Ctrl+H |
| Undo / Redo | Ctrl+Z / Ctrl+Y |
| Find / next / previous | Ctrl+F / F3 / Shift+F3 |
| Copy selected text | Ctrl+C |
| Zoom in / out / actual size | Ctrl++ / Ctrl+- / Ctrl+0 |
| Fit width / fit page | Ctrl+1 / Ctrl+2 |
| Go to page | Ctrl+G |
| Rotate page | Ctrl+R / Ctrl+Shift+R |
| Organize pages | Ctrl+Shift+O |
| Add bookmark | Ctrl+B |
| Sidebar | F4 |
| Full screen | F11 |
| Night mode | Ctrl+Shift+N |
| Document properties | Ctrl+D |
| Create PDF from clipboard | Ctrl+Shift+V |
| All shortcuts | F1 |

## Where things are stored

| | Linux | Windows |
|---|---|---|
| Settings, recent files list, signatures | `~/.config/pdfdesk/` | `%APPDATA%\PDF Desk\` |
| Thumbnails and temporary files | `~/.cache/pdfdesk/` | `%LOCALAPPDATA%\PDF Desk\cache\` |
| Program | `~/.local/share/pdf-desk/` | `%LOCALAPPDATA%\Programs\PDF Desk\` |

Signatures are saved as PNG files in the `signatures` folder and never leave your computer.

## Troubleshooting

- Run `pdfdesk --check` (Linux) to test every part: libraries, fonts, conversions, LibreOffice and OCR. On Windows run `"%LOCALAPPDATA%\Programs\PDF Desk\venv\Scripts\python.exe" "%LOCALAPPDATA%\Programs\PDF Desk\app\pdfdesk.py" --check`.
- Opening a PDF while PDF Desk is already running opens it as a new tab in the same window. Use `pdfdesk --new-window` for a separate window.
- If an Office file won't convert, check that LibreOffice is found in Preferences > Extras. PDF Desk uses its own LibreOffice profile, so conversions work even while LibreOffice is open.
- A scanned PDF can't be searched or copied from until you run Tools > Recognize text (OCR).

## Project layout

| Path | What's in it |
|---|---|
| `pdfdesk/app.py` | Start-up, theme, single window handling, `--check` |
| `pdfdesk/mainwindow.py` | Menus, toolbars, tabs and every command |
| `pdfdesk/home.py`, `recents.py` | Home screen and the recent files list |
| `pdfdesk/canvas.py` | The page view: rendering, zoom, selection and all the mouse tools |
| `pdfdesk/doctab.py`, `sidebar.py`, `organizer.py` | Document tab, sidebar panels, Organize Pages |
| `pdfdesk/annots.py` | Creating and changing comments and form fields |
| `pdfdesk/pdfops.py` | Page tools, split, watermark, headers, compression, passwords, OCR, redaction |
| `pdfdesk/convert.py` | Importing to PDF and exporting to other formats |
| `pdfdesk/dialogs.py`, `signature.py` | Dialog windows and the signature manager |
| `tests/` | Automated tests (`python -m pytest -q tests`), including a full headless GUI run |

## Security

PDF Desk was checked line by line for anything harmful and for security bugs (see `SECURITY-REVIEW.md`). In short:

- **No network access.** The program has no code that talks to the internet. The only outside programs it runs are LibreOffice (for Office conversions) and Tesseract (only to list OCR languages), always with fixed arguments and never through a shell.
- **Links inside PDFs are treated as untrusted.** Web links only open after you confirm, and only `http`, `https` and `mailto` links are allowed. Links to other files only open other local PDFs, after you confirm. Links to network shares (`\\server\share`, mapped network drives), `file://` addresses, unusual Windows device paths and "launch" actions are blocked. Before you confirm a web link, PDF Desk shows the real site name, and warns when it uses look-alike letters. PDF JavaScript never runs.
- **Text from inside a PDF is always shown as plain text**, so a booby-trapped title or bookmark can't make Qt load files or network shares.
- **LibreOffice runs with a locked-down private profile**: macros, embedded objects (OLE) and DDE links are disabled, linked images and other linked content are never fetched from the internet or network shares, links aren't updated and spreadsheet formulas aren't recalculated when a file is converted. CSV files are converted with PDF Desk's own code, so formulas in them never run.
- **Exports are defused**: text that starts with `=` becomes plain text in Excel exports, never a live formula.
- **Python packages are pinned and hash-checked.** `requirements.lock` lists the exact versions PDF Desk was tested with and the SHA-256 hash of every file. pip refuses to install anything that doesn't match.
- **Single-window hand-off is private to your user.** Only your own account can talk to a running PDF Desk, and it proves itself with a random secret before a second launch hands over file names.

## License notes

PDF Desk is built on PyMuPDF/MuPDF, which is licensed under the GNU AGPL 3.0. If you publish PDF Desk or share builds of it, publish its source code under the AGPL 3.0 (or another compatible license) as well. Toolbar icons are from Lucide (ISC license, see `pdfdesk/icons/LICENSE-lucide.txt`). Qt for Python is LGPL 3.0.
