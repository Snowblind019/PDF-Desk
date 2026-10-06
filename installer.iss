; Inno Setup script for a per-user PDF Desk installer (no administrator rights needed).
; Run build.ps1 first; it compiles this automatically when Inno Setup 6 is installed.

#define AppName "PDF Desk"
#define AppVersion "2.0.1"
#define AppExe "PDF Desk.exe"

[Setup]
AppId={{8C5E2A41-6B0D-4E8A-9F3C-2D7A1B5E9C44}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppName}
DefaultDirName={localappdata}\Programs\{#AppName}
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
OutputDir=dist
OutputBaseFilename=PDF-Desk-Setup
SetupIconFile=pdfdesk\assets\pdfdesk.ico
UninstallDisplayIcon={app}\{#AppExe}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ChangesAssociations=yes

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; Flags: unchecked
Name: "pdfassoc"; Description: "Add PDF Desk to the Open with list for PDF files"

[Files]
Source: "dist\PDF Desk\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#AppName}"; Filename: "{app}\{#AppExe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExe}"; Tasks: desktopicon

[Registry]
Root: HKCU; Subkey: "Software\Classes\PDFDesk.PDF"; ValueType: string; ValueName: ""; ValueData: "PDF Document"; Flags: uninsdeletekey; Tasks: pdfassoc
Root: HKCU; Subkey: "Software\Classes\PDFDesk.PDF\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#AppExe},0"; Tasks: pdfassoc
Root: HKCU; Subkey: "Software\Classes\PDFDesk.PDF\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExe}"" ""%1"""; Tasks: pdfassoc
Root: HKCU; Subkey: "Software\Classes\.pdf\OpenWithProgids"; ValueType: string; ValueName: "PDFDesk.PDF"; ValueData: ""; Flags: uninsdeletevalue; Tasks: pdfassoc

[Run]
Filename: "{app}\{#AppExe}"; Description: "Start {#AppName}"; Flags: nowait postinstall skipifsilent
