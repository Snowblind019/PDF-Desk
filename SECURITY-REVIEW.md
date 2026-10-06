# PDF Desk security review

**Latest review:** October 2026, version 2.0.1 (automatic updates). Earlier rounds covered version 1.1.0 (round 2) and 1.0.0 (round 1) and are kept below.

**Result:** no malware, hidden behavior or unexpected network access was found in any round. Every round found real bugs, mostly in how the app handles untrusted content. All of them are fixed and covered by tests.

## Round 3: version 2.0.1 (automatic updates)

Version 2.0.1 adds the first code in PDF Desk that goes online: a check for new releases on GitHub and an automatic update. That code (`pdfdesk/updater.py`, `update_ui.py`, `locks.py`, `tools/make_release.py` and the installer changes) was reviewed line by line, attacked with hand-made inputs, and reviewed again after each round of fixes until nothing was left that could run unsigned code.

### What goes online, and when

- At start-up, at most once a day (can be turned off in Edit > Preferences), and from Help > Check for updates: one HTTPS request to `api.github.com` for the latest release of `Snowblind019/PDF-Desk`. It carries no cookies or IDs, only a User-Agent with the PDF Desk version.
- Only after the user clicks Install now: the release's `.zip` and `.zip.sig` from `github.com` (and the GitHub download hosts it redirects to).
- The installer then runs pip with `requirements.lock` (hash-checked), which contacts PyPI only if a package changed.

### How an update is trusted

- **Signed releases.** The zip must be signed with the PDF Desk release key (Ed25519). The public key ships in `pdfdesk/assets/update-key.pub`; the private key stays on the maintainer's computer, encrypted with AES-256-GCM under a key made from a passphrase with scrypt. The signature covers the version and the zip's SHA-256, so an older signed release can't be passed off as a newer one. Someone who took over the GitHub account still couldn't push an update.
- **Only HTTPS to GitHub.** Every request and every redirect must be `https` to `api.github.com`, `github.com`, `objects.githubusercontent.com` or `release-assets.githubusercontent.com` on port 443, with normal certificate checks. Download links in the release must point at this repo's release downloads. Sizes and total time are capped.
- **Careful unpacking**, after the signature check: no absolute paths, `..`, drive letters, backslashes, links, Windows device names (CON, NUL, COM1...), names ending in a dot or space, or duplicate names; size and file count limits; the version inside must match. Files are unpacked into a private folder in the user's cache that other users can't write to.
- **Text from GitHub** (release notes) is shown as plain text with control and text-direction characters removed.

### How it installs

- The update's own installer (`install.sh` / `install.ps1`) does the work, so the Python packages are still hash-checked. It installs the packages before replacing the program files, so a failed package download leaves the program as it was. Installs made with `--unlocked` (unpinned packages) never update themselves.
- PDF Desk asks to save changes, closes, and a small helper takes over. Every running PDF Desk holds an operating-system lock, and the helper waits until every one of them has closed, then holds an update lock so no PDF Desk can start until the install is done. If a window stays open, nothing is installed and PDF Desk says so. The helper runs the system Python in isolated mode (`-I`), calls bash and PowerShell by full path, and never uses a shell.

### Fixed during this round

- Closing PDF Desk while a check or download was running could crash it. Network work now runs on plain background threads.
- A second update prompt could start a second download that broke the first. Only one check, prompt or download happens at a time.
- Cancel was ignored once the download had finished, and closing the progress window counted as Cancel, which stopped Install now from working at all. Both fixed and covered by a test that goes from Install now to the installer.
- Other PDF Desk windows (started with "new window") weren't waited for, and a crashed copy's leftover file could block updates for good. Replaced by operating-system locks.
- The release script could have packaged stray files: it now packages a fixed list of folders, requires a clean git checkout, refuses anything that looks like a key, and keeps the key outside the repo.

### Tests

`tests/test_updater.py` covers versions, release info from GitHub, the host checks, signatures (changed zip, wrong key, older release with a newer version number), unpacking attacks, the locks, the once-a-day logic, the release script from key creation to a verified zip, and the prompts in the real window. The whole suite has 101 tests. A full update was also run end to end on Linux: installed 2.0.1 with `install.sh`, published a signed 2.0.2, and let PDF Desk update itself while a second window was open.

## Round 2: version 1.1.0

Version 1.1 added a lot of new code: formatted text boxes and fonts, Prepare Form, links, attachments, page labels, Compare, Read Out Loud and, most sensitive of all, Digital IDs and certificate signatures. It also added new outside packages (pyHanko, cryptography, fontTools). The new code was reviewed the same way as in round 1.

### How it was checked

- **Three reviewers, each with one area,** read every new and changed line: text, fonts and comments; document tools and forms; Digital IDs and signatures. Each finding was confirmed with a hand-made hostile file or script before it was fixed, and every proof of concept was run again afterwards.
- **Signature checks were attacked on purpose:** a signed contract whose page content was swapped in a later update, an object that is both a comment and part of the page's drawing settings, an update that only some readers accept, certificates that expired or aren't valid yet, a trusted personal certificate used to "issue" a fake one, signature fields hidden from the page list, certified documents, files with thousands of updates, and a file whose update chain points back at itself.
- **Scanners:** bandit (no high findings; the one medium is the XFDF reader, which refuses any DTD before parsing, see below), pyflakes, ShellCheck (clean), and pip-audit on every pinned package (no known vulnerabilities).
- **Network test:** the signing and signature tests block every network connection and fail if anything tries to connect. Nothing does.
- **Automated tests:** 87 tests, including regression tests for the main fixes in this round (signature attacks, link injection, radio buttons, redaction marks, label numbers, field detection, form filling).

### New outside packages

| What | Source | Why |
|---|---|---|
| pyHanko, pyhanko-certvalidator | PyPI, Matthias Valvekens (the pyHanko project) | Creating and checking PDF signatures |
| cryptography, cffi, pycparser | PyPI, the Python Cryptographic Authority | Digital ID keys and certificates |
| asn1crypto, oscrypto, certifi, uritools, tzlocal, tzdata | PyPI, pulled in by pyHanko | Certificate parsing and time zones |
| aiohttp and its helpers (aiosignal, aiohappyeyeballs, attrs, frozenlist, multidict, propcache, yarl, idna) | PyPI, pulled in by pyHanko | pyHanko's optional online features. **PDF Desk never uses them**: signing doesn't contact a time-stamp server, and checking runs with `allow_fetching=False`, so no certificate or revocation list is downloaded. The tests prove it by blocking the network. |
| fontTools | PyPI, the fontTools project (already used in 1.0 through pdf2docx) | Reading font names and embedding only the letters used |

All of them are pinned with SHA-256 hashes in `requirements.lock`, like the rest.

### Security bugs found and fixed in round 2

**Digital signatures**

| # | Severity | Problem | Fix |
|---|---|---|---|
| S1 | High | A signature showed as valid ("changes were added afterwards") even when a later update had replaced the page content, for example changing "Pay Bob $100" to "Pay Mallory $1,000,000". pyHanko flagged it, but PDF Desk didn't use that verdict. | PDF Desk now compares the signed version with the current one, object by object (`sigdiff.py`). Comments, form filling and more signatures are allowed and listed. Changed page content, page order, form fields, document actions, layers or attachments make the signature NOT VALID. Certified documents are held to the changes their signer allowed. As a second, independent check, the text and pictures MuPDF draws on each page (comments left aside) are compared with the signed version, so a file built to be read differently by pyHanko and by the viewer is caught too, and a signed file that can't be read strictly is reported as not valid instead of as an error. **Open signed version** shows exactly what was signed. |
| S2 | High | Certificates in your trusted list were trusted for everything: an expired one still counted, and a trusted personal certificate could "issue" a certificate in someone else's name that then showed as trusted. | Trusted certificates now only vouch for themselves, within their validity dates. Expired, not-yet-valid and "issued by someone who can't vouch" each get their own plain message. |
| S3 | Medium | Signed PDFs weren't always recognized as signed: when the signature value sat on a parent field, the field wasn't on a page, or the file lacked the "signatures present" flag. Saving such a file rewrote it and broke the signature without warning, and an already signed field could be offered for signing again. | Signature fields are found by walking the whole form tree. |
| S4 | Medium | Edits that are never allowed after signing (changing page text, rotating, deleting pages) were saved without a warning, and on a certified document even comments were. | Before saving, saving a copy or signing again, PDF Desk checks what the save would change and warns in plain words when a signature would show as invalid. |
| S5 | Medium | "Check signatures" checked the file as it was opened, not what was on screen after changes or after saving. | It always checks the current state. |
| S6 | High (found in the final pass) | Saving a signed PDF a second time in the same session wrote a broken file whose update pointed at itself. pyHanko then read that file forever, so a crafted file could freeze signature checks. | The save writes a correct update every time (MuPDF's remembered position is restored after each write), and pyHanko is guarded against update chains that loop. A file with thousands of updates is checked in under a second instead of many seconds. |
| S7 | Low | The signed copy was written through a predictable temp name next to the target, and lost the original file's permissions. | Written through a random temp file in the same folder, keeping permissions. |
| S8 | Low | Text from the signer (reason, location, name) could contain line breaks and pose as PDF Desk's own verdict. | Shown on one line, in quotes, clearly labeled as the signer's text. The verdict is on its own line. |
| S9 | Low | Digital ID files used the cryptography package's default protection, IDs could be imported without a password, and the password stayed in memory in a hidden dialog. | IDs are stored with AES-256 and 600,000 rounds of password hashing, need a password of at least 8 characters (an imported ID is re-locked with a new one if needed), and password fields are cleared and their dialogs deleted after use. |
| S10 | Low | A damaged file that MuPDF had to repair could be saved as an "update" to the signed file. | Damaged files are never saved as updates, and a damaged signed file is reported as such. |
| S11 | Low | Checking signatures couldn't be cancelled, and signing could be cancelled half way. | Checking can be cancelled; signing runs to the end. |

**Text, fonts and comments**

| # | Severity | Problem | Fix |
|---|---|---|---|
| T1 | Medium | Edit Text applied the user's own pending redaction marks as a side effect, removing content they hadn't confirmed yet. | Edit Text sets the user's marks aside, removes only the edited text, and puts the marks back. |
| T2 | Medium | A PDF could contain a font with the same internal name PDF Desk was about to use, so new text was drawn with the PDF's font (wrong or misleading letters). | New fonts get random private names, and existing names are checked before use. |
| T3 | Medium | Crafted formatting data in a text box (very long style strings) could make PDF Desk very slow. | Size limits on all formatting data, and parsing without backtracking patterns. |
| T4 | Low | Text could silently disappear when it didn't fit the box being written. | The text is laid out on a trial page first; if it doesn't fit, nothing is changed and you're told. |
| T5 | Low | Changing the look of one comment could change another that shared the same drawing. | Every comment gets its own drawing. |
| T6 | Low | Text boxes landed in the wrong place on rotated pages whose page box doesn't start at zero. | Coordinates now account for the page box and rotation. |
| T7 | Low | Filling in a form field also changed some of its other settings (such as the border width) and added a font to the page, which counted as changing the page in a signed PDF. | Only the value and its look change. The font for non-Latin letters is kept with the field. |

**Documents and forms**

| # | Severity | Problem | Fix |
|---|---|---|---|
| D1 | High | A web address typed for a new link could contain `)` and add a second action of its own (for example "launch a program"). | The address is stored as a safe hex string after the link is created, so it can't break out. |
| D2 | Medium | Exporting comments or form data to CSV turned text starting with `=`, `+`, `-` or `@` into spreadsheet formulas. | Such cells are saved as text, and turned back on import. |
| D3 | Medium | Importing XFDF form data accepted a DTD, which allows "billion laughs" style memory attacks. | Any DTD is refused before parsing. |
| D4 | Medium | A huge page label start number made the PDF engine build gigantic strings (freezes). | Labels are worked out by PDF Desk with sane limits. |
| D5 | Medium | Adding form fields could drop existing fields when the form's field list was stored as a separate object, and moving radio buttons switched them on. | Both cases handled; button states are kept. |
| D6 | Low | Busy pages (maps, charts) made "Find form fields" and "Highlight all" very slow. | Work limits on both. |
| D7 | Low | "Save all pictures" could overwrite existing files or follow links planted in the folder. | Files are only created new, never through links. |
| D8 | Low | Attachment names and titles from a PDF could hide characters or pose as other file types. | Cleaned before they are shown or used as file names. |
| D9 | Low | Cancel didn't stop Compare, Reader view and some page tools. | All long jobs stop when cancelled. |
| D10 | Low | The Read Out Loud speed was passed to PowerShell as text. | Only a number from -10 to 10 is passed; the text to read goes through standard input. |

### Notes on the signature checks

- **What "valid" means here.** Green: the signed content is intact, nothing that matters changed afterwards (or only more signatures were added), and you trust the signer. Yellow: intact, but comments, form filling or settings were added later, or the signer's identity isn't confirmed. Red: the signed content changed, a later update changed what the page shows, or the signer's rules were broken.
- **Comments can cover content.** Like Acrobat, PDF Desk allows comments after an ordinary signature. A comment can hide part of a page, so the yellow message says so and offers Open signed version.
- **pyHanko is patched in memory, not on disk.** The loop guard wraps two of pyHanko's internal methods when signatures are used. If a future pyHanko renames them, the guard switches itself off; the per-signature limits still apply.
- **Offline means no revocation checks.** A certificate that was revoked after signing still shows as trusted if it is in your list.

## Round 1: version 1.0.0

### How it was checked

- **Line-by-line reading.** Three separate reviewers each read every line of their share of the code: the app, the dialogs and UI, and the conversion code, installers and build files. A fourth pass reviewed the fixes and tried to get around them. Where possible, findings were confirmed by building booby-trapped PDFs and files and running them, not just by reading.
- **Automated scanners:**
  - bandit (Python): no medium or high findings. The 31 low ones are "try/except: pass" blocks and the two LibreOffice/Tesseract program calls, all reviewed.
  - ShellCheck (bash): clean.
  - PSScriptAnalyzer (PowerShell): only style warnings, such as Write-Host.
  - pip-audit: no known vulnerabilities in any installed package.
- **Searches across the whole project** for network code, downloads, `eval`/`exec`, encoded blobs, `pickle`, `subprocess`/`ctypes`, autostart entries, scheduled tasks, Run keys, and reads of SSH keys, browser data or credential files.
- **Network test.** A local web server watched for any request while files were converted. After the fixes it saw none.
- **Automated tests.** 41 tests run on every change, including a full hidden-window session that uses every tool, plus tests that feed in hostile links and file names. All pass.

### Outside code and where it comes from

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

### Malware checks

| Check | Result |
|---|---|
| Network access | None. The only network-style code is a local "single window" channel that only your own user account can open. |
| Programs it runs | LibreOffice (Office conversions, when you ask for one), Tesseract (only to list OCR languages) and, since 1.1, the system speech engine for Read Out Loud. All get fixed arguments as a list, never through a shell. |
| Hidden or encoded code | None. No eval/exec, no base64 blobs, no pickle. |
| Persistence | None. No autostart, cron, systemd units, scheduled tasks or registry Run keys. |
| Files it reads | Your PDFs and the files you pick, its own settings, recent list, thumbnails, signatures and (since 1.1) Digital IDs, the system fonts folders, and OCR language files. Nothing else. |
| Registry (Windows) | Only its own "Open with" entry for PDFs and its uninstall entry, both under your user (HKCU). Both are removed on uninstall. |
| Admin or sudo | Never needed. sudo is only used if you run `./install.sh --extras`, to install LibreOffice, OCR languages and espeak-ng with your package manager. |

### Security bugs found and fixed

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
- **The engines parse untrusted files.** PDF Desk relies on MuPDF (inside PyMuPDF), Qt, pyHanko and LibreOffice to read PDFs and Office files. These are large programs, mostly written in C/C++. They're well maintained, but keep them updated. To update PDF Desk's packages, regenerate `requirements.lock` with `pip-compile --generate-hashes`, or install with `--unlocked`.
- **A booby-trapped PDF can still be slow.** A very complex page can take a long time to draw. That can freeze the window, but it can't do anything else.
- **PDF JavaScript never runs.** PDF Desk doesn't turn on MuPDF's JavaScript support.
- **Your Digital IDs are only as safe as your account.** They're readable only by you and locked with your password, but anyone who can run programs as you could copy the files and try to guess the password. Use a long one.

## Re-checking it yourself

```bash
cd "PDF Desk"
python3 -m pytest -q tests                      # 101 tests, including the hostile-input, signature attack and update ones
pip install bandit pip-audit
bandit -r pdfdesk                               # static security scan
pip-audit -r requirements.lock                  # known vulnerabilities in the pinned packages
grep -rnE "socket|urllib|requests|aiohttp|eval\(|exec\(|base64|pickle" pdfdesk   # only Qt menu .exec() calls and urllib.parse (text parsing, no network)
```
