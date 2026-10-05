# PDF Desk security review

**Date:** October 2026, version 1.0.0

**Result:** no malware, hidden behavior or network access was found. The review did find real security bugs, mostly in how the app handled untrusted content inside PDFs. All of them are fixed and covered by tests.

## How it was checked

- **Line-by-line reading.** Three separate reviewers each read every line of their share of the code: the app, the dialogs and UI, and the conversion code, installers and build files. A fourth pass reviewed the fixes and tried to get around them. Where possible, findings were confirmed by building booby-trapped PDFs and files and running them, not just by reading.
- **Automated scanners:**
  - bandit (Python): no medium or high findings. The 31 low ones are "try/except: pass" blocks and the two LibreOffice/Tesseract program calls, all reviewed.
  - ShellCheck (bash): clean.
  - PSScriptAnalyzer (PowerShell): only style warnings, such as Write-Host.
  - pip-audit: no known vulnerabilities in any installed package.
- **Searches across the whole project** for network code, downloads, `eval`/`exec`, encoded blobs, `pickle`, `subprocess`/`ctypes`, autostart entries, scheduled tasks, Run keys, and reads of SSH keys, browser data or credential files.
- **Network test.** A local web server watched for any request while files were converted. After the fixes it saw none.
- **Automated tests.** 41 tests run on every change, including a full hidden-window session that uses every tool, plus tests that feed in hostile links and file names. All pass.

## Outside code and where it comes from

No GitHub repositories or other code are downloaded or referenced by the program. Everything outside PDF Desk's own code is one of the following.

| What | Source | Checked |
|---|---|---|
| PyMuPDF, pymupdf-fonts, pdf2docx | PyPI, published by Artifex (makers of MuPDF) | Exact names, no look-alikes |
| PySide6-Essentials, shiboken6 | PyPI, the Qt Company's Qt for Python team | Same |
| python-docx, python-pptx | PyPI, python-openxml / scanny | Same |
| openpyxl, et-xmlfile | PyPI, openpyxl project | Same |
| Markdown, Pillow, numpy, lxml, fonttools | PyPI, their official projects | Same |
| opencv-python-headless, fire, termcolor, XlsxWriter, typing_extensions | PyPI, pulled in by pdf2docx and friends | Same |
| Toolbar icons | npm `lucide-static` 1.52.0 (Lucide, ISC license) | The package's SHA-512 matches the npm registry. All 88 icons are byte-for-byte the official files. They contain only shapes: no scripts, links or external references |
| PyInstaller, Inno Setup | Only used if you make a standalone build | Not part of the app |

**Pinned versions.** `requirements.lock` pins every package to the exact version that was tested, with the SHA-256 hash of every file PyPI has for it. The installers use `--require-hashes`, so pip refuses any file that doesn't match byte for byte. This was tested by changing one hash: pip refused to install.

## Malware checks

| Check | Result |
|---|---|
| Network access | None. The only network-style code is a local "single window" channel that only your own user account can open. |
| Programs it runs | LibreOffice (Office conversions, when you ask for one) and Tesseract (only to list OCR languages). Both get fixed arguments as a list, never through a shell. |
| Hidden or encoded code | None. No eval/exec, no base64 blobs, no pickle. |
| Persistence | None. No autostart, cron, systemd units, scheduled tasks or registry Run keys. |
| Files it reads | Your PDFs and the files you pick, its own settings, recent list, thumbnails and signatures, the system fonts folder, and OCR language files. Nothing else. |
| Registry (Windows) | Only its own "Open with" entry for PDFs and its uninstall entry, both under your user (HKCU). Both are removed on uninstall. |
| Admin or sudo | Never needed. sudo is only used if you run `./install.sh --extras`, to install LibreOffice and OCR languages with your package manager. |

## Security bugs found and fixed

| # | Severity | Problem | Fix |
|---|---|---|---|
| 1 | High | Clicking a link inside a PDF could open any local file without asking. On Windows it could also contact a network share, which can leak your Windows login hash. | File links only open other local PDFs, after you confirm. Network shares, mapped network drives, `\\?\` and `\??\` style paths, and "launch" actions are refused. |
| 2 | High | Web links weren't limited to web addresses. A `FILE:///...exe` link could start a program on Windows, and `smb:`, `ms-msdt:` and similar links went to the system. | Only `http`, `https` and `mailto` links are allowed, always after you confirm. The prompt shows the real site name and warns about look-alike letters. |
| 3 | Medium | Text from a PDF (bookmark titles, document info, file names) could be read by Qt as HTML. That could crash the app or, on Windows, load an image from a network share. | All outside text is shown as plain text: message boxes, tooltips, labels and progress windows. |
| 4 | Medium | Excel export turned PDF text that starts with `=` into live formulas, which could phish or leak data when the file is opened. | Those cells are always saved as plain text. |
| 5 | Medium | LibreOffice fetched images linked from inside Word files from the internet. That can be used for tracking, or on Windows to leak your login hash. | PDF Desk's private LibreOffice profile blocks linked content, macros, embedded objects, DDE, link updates and formula recalculation. Confirmed with a test server: zero requests. |
| 6 | Medium | On Windows, a program placed next to a downloaded PDF (for example a fake `soffice.exe`) could be run by mistake. | Program lookup never checks the current folder, and the app starts in your home folder. |
| 7 | Low | `mailto:` links opened without asking and could carry extra parameters such as file attachments. | You're asked first. Only the address, subject and body are kept. |
| 8 | Low | The "single window" channel lived in a shared temp folder, wasn't restricted to your user, and didn't check what it received. | It moved to your private runtime folder, is restricted to your user, uses a secret handshake, and checks everything it receives. |
| 9 | Low | Saving changed a private file's permissions to readable by everyone, and used a predictable temp name. | The original permissions are kept, temp names are random, and symlinks that aren't yours are never followed. |
| 10 | Low | Background tasks and the page view could use the PDF engine at the same time. | The page view waits while a task runs. |
| 11 | Low | Some inputs could make the app very slow: two text patterns, and very large pages when printing, exporting, copying or running OCR. | The patterns are bounded, and page images are capped at about 40 megapixels. |
| 12 | Low | Split file names taken from bookmark titles could hide their real type with special characters. | Hidden and special characters are removed, and names are kept to 100 characters. |
| 13 | Low | Packages weren't pinned, so a bad future release could have been installed. | Packages are now hash-locked (see above). |
| 14 | Low | Installer details: paths with spaces or apostrophes, PATH entries being expanded or rewritten, the winget prompt defaulting to Yes. | Fixed. PATH is only touched when needed, and winget now needs an explicit "y". |
| 15 | Low | Crashes from odd PDF values: gray or CMYK colors, absurd font sizes, a corrupt file inserted as pages. | All handled. |
| 16 | Low | Markdown export copied raw HTML from the PDF. CSV import went through LibreOffice, which ran formulas. | HTML characters are escaped. CSV files use PDF Desk's own table layout. |

## Things to keep in mind

- **No review can promise zero bugs.** What can be said is that every line was read and the risky paths were attacked on purpose, and nothing harmful turned up.
- **Windows wasn't tested on a real machine.** The Windows scripts passed the PowerShell parser and PSScriptAnalyzer, and the Windows path rules were tested by simulating them. A real Windows run is still worth doing once.
- **The engines parse untrusted files.** PDF Desk relies on MuPDF (inside PyMuPDF), Qt and LibreOffice to read PDFs and Office files. These are large programs written in C/C++. They're well maintained, but keep them updated. To update PDF Desk's packages, regenerate `requirements.lock` with `pip-compile --generate-hashes`, or install with `--unlocked`.
- **A booby-trapped PDF can still be slow.** A very complex page can take a long time to draw. That can freeze the window, but it can't do anything else.
- **PDF JavaScript never runs.** PDF Desk doesn't turn on MuPDF's JavaScript support.

## Re-checking it yourself

```bash
cd "PDF Desk"
python3 -m pytest -q tests                      # 41 tests, including the hostile-input ones
pip install bandit pip-audit
bandit -r pdfdesk                               # static security scan
pip-audit -r requirements.lock                  # known vulnerabilities in the pinned packages
grep -rnE "socket|urllib|requests|eval\(|exec\(|base64|pickle" pdfdesk   # only Qt menu .exec() calls and urllib.parse (text parsing, no network)
```
