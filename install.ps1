<#
.SYNOPSIS
  Installs PDF Desk for the current Windows user. No administrator rights needed.

.DESCRIPTION
  Copies PDF Desk to %LOCALAPPDATA%\Programs\PDF Desk, creates a private Python
  environment there, adds a Start menu shortcut, adds PDF Desk to the "Open with"
  list for PDF files, and registers an uninstaller under Settings > Apps.

.PARAMETER Desktop
  Also put a shortcut on the desktop.

.PARAMETER AddToPath
  Add the 'pdfdesk' command to your user PATH.

.PARAMETER Wheels
  Folder with downloaded Python packages, for installing without internet.

.PARAMETER Unlocked
  Use the newest package versions (requirements.txt) instead of the tested ones. By default the
  packages come from requirements.lock: exact versions with SHA-256 hashes, so pip refuses any
  package file that isn't byte-for-byte the one PDF Desk was tested with.

.NOTES
  Needs Python 3.12 or newer (3.10 with -Unlocked). If none is found and winget is available, the
  script asks before installing Python 3.12 for your user account.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File install.ps1 -Desktop
#>
param(
    [switch]$Desktop,
    [switch]$AddToPath,
    [string]$Wheels = "",
    [switch]$Unlocked
)

$ErrorActionPreference = "Stop"
$AppName = "PDF Desk"
$Version = "1.0.0"
$Src = Split-Path -Parent $MyInvocation.MyCommand.Path
$Dest = Join-Path $env:LOCALAPPDATA "Programs\PDF Desk"
$AppDir = Join-Path $Dest "app"
$Venv = Join-Path $Dest "venv"
$MinMinor = 12
if ($Unlocked) { $MinMinor = 10 }

function Say([string]$msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

function Test-Python([string]$exe, [string[]]$pre) {
    try {
        $out = & $exe @pre -c "import sys; print(sys.executable) if (3, $MinMinor) <= sys.version_info < (3, 15) else sys.exit(1)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out) { return ($out | Select-Object -Last 1).Trim() }
    } catch { }
    return $null
}

function Find-Python {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        foreach ($v in @("-3.14", "-3.13", "-3.12", "-3.11", "-3.10", "-3")) {
            $p = Test-Python "py" @($v)
            if ($p) { return $p }
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        $p = Test-Python "python" @()
        if ($p) { return $p }
    }
    foreach ($v in @("314", "313", "312", "311", "310")) {
        $exe = Join-Path $env:LOCALAPPDATA "Programs\Python\Python$v\python.exe"
        if (Test-Path $exe) {
            $p = Test-Python $exe @()
            if ($p) { return $p }
        }
    }
    return $null
}

# ---- Python
$Python = Find-Python
if (-not $Python) {
    Write-Host "Python 3.$MinMinor or newer was not found."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        $answer = Read-Host "Install Python 3.12 for your user account now with winget? (y/N)"
        if ($answer -match "^[Yy]") {
            winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
            $Python = Find-Python
        }
    }
    if (-not $Python) {
        Write-Host "Install Python from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'),"
        Write-Host "then run this installer again."
        exit 1
    }
}
Say "Using Python at $Python"

# ---- program files
Say "Copying program files to $Dest"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
if (Test-Path $AppDir) { Remove-Item -Recurse -Force $AppDir }
New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
Copy-Item -Recurse -Force (Join-Path $Src "pdfdesk") $AppDir
Copy-Item -Force (Join-Path $Src "pdfdesk.py") $AppDir
Copy-Item -Force (Join-Path $Src "requirements.txt") $AppDir
Copy-Item -Force (Join-Path $Src "requirements.lock") $AppDir
Copy-Item -Force (Join-Path $Src "uninstall.ps1") $Dest
Get-ChildItem -Path $AppDir -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force

# ---- private Python environment
$VenvPy = Join-Path $Venv "Scripts\python.exe"
$VenvPyw = Join-Path $Venv "Scripts\pythonw.exe"
if (Test-Path $VenvPy) {
    & $VenvPy -c "import sys; sys.exit(0 if sys.version_info >= (3, $MinMinor) else 1)" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Say "Replacing the old Python environment (it uses an older Python)"
        Remove-Item -Recurse -Force $Venv
    }
}
if (-not (Test-Path $VenvPy)) {
    Say "Creating a private Python environment"
    & $Python -m venv $Venv
    if ($LASTEXITCODE -ne 0) { throw "Could not create the Python environment." }
}
Say "Installing Python packages (first time takes a few minutes)"
$pipArgs = @("-m", "pip", "install", "--disable-pip-version-check")
if ($Wheels) { $pipArgs += @("--no-index", "--find-links", $Wheels) }
if ($Unlocked) {
    $pipArgs += @("--upgrade", "-r", (Join-Path $AppDir "requirements.txt"))
} else {
    $pipArgs += @("--require-hashes", "-r", (Join-Path $AppDir "requirements.lock"))
}
& $VenvPy @pipArgs
if ($LASTEXITCODE -ne 0) { throw "Installing the Python packages failed. See the messages above." }

# ---- launchers
$Script = Join-Path $AppDir "pdfdesk.py"
$Icon = Join-Path $AppDir "pdfdesk\assets\pdfdesk.ico"
$Cmd = Join-Path $Dest "pdfdesk.cmd"
Set-Content -Path $Cmd -Encoding ASCII -Value "@echo off`r`nstart `"`" `"%~dp0venv\Scripts\pythonw.exe`" `"%~dp0app\pdfdesk.py`" %*"

$Shell = New-Object -ComObject WScript.Shell
function New-Shortcut([string]$path) {
    $lnk = $Shell.CreateShortcut($path)
    $lnk.TargetPath = $VenvPyw
    $lnk.Arguments = "`"$Script`""
    $lnk.WorkingDirectory = $AppDir
    $lnk.IconLocation = "$Icon,0"
    $lnk.Description = "View, edit, convert and sign PDF files offline"
    $lnk.Save()
}
$StartMenu = Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"
New-Shortcut (Join-Path $StartMenu "$AppName.lnk")
Say "Added a Start menu shortcut"
if ($Desktop) {
    New-Shortcut (Join-Path ([Environment]::GetFolderPath("Desktop")) "$AppName.lnk")
    Say "Added a desktop shortcut"
}

# ---- "Open with" for PDF files (current user only)
function Set-Default([string]$key, [string]$value) {
    New-Item -Path $key -Force | Out-Null
    Set-ItemProperty -Path $key -Name "(Default)" -Value $value
}
$Classes = "HKCU:\Software\Classes"
Set-Default "$Classes\PDFDesk.PDF" "PDF Document"
Set-Default "$Classes\PDFDesk.PDF\DefaultIcon" "$Icon,0"
Set-Default "$Classes\PDFDesk.PDF\shell\open\command" "`"$VenvPyw`" `"$Script`" `"%1`""
New-Item -Path "$Classes\.pdf\OpenWithProgids" -Force | Out-Null
New-ItemProperty -Path "$Classes\.pdf\OpenWithProgids" -Name "PDFDesk.PDF" -Value "" -PropertyType String -Force | Out-Null
Say "Added PDF Desk to the 'Open with' list for PDF files"

# ---- entry in Settings > Apps
$Uninst = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\PDFDesk"
New-Item -Path $Uninst -Force | Out-Null
$UninstallScript = Join-Path $Dest "uninstall.ps1"
$values = @{
    "DisplayName" = $AppName
    "DisplayVersion" = $Version
    "Publisher" = $AppName
    "DisplayIcon" = $Icon
    "InstallLocation" = $Dest
    "UninstallString" = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File `"$UninstallScript`""
}
foreach ($k in $values.Keys) { Set-ItemProperty -Path $Uninst -Name $k -Value $values[$k] }
New-ItemProperty -Path $Uninst -Name "NoModify" -Value 1 -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path $Uninst -Name "NoRepair" -Value 1 -PropertyType DWord -Force | Out-Null

# ---- PATH (read and written raw, so entries like %USERPROFILE%\bin keep working)
if ($AddToPath) {
    $envKey = Get-Item -Path "HKCU:\Environment"
    $userPath = $envKey.GetValue("Path", "", [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    if (($userPath -split ";") -notcontains $Dest) {
        $newPath = ($userPath.TrimEnd(";") + ";" + $Dest).TrimStart(";")
        Set-ItemProperty -Path "HKCU:\Environment" -Name "Path" -Value $newPath -Type ExpandString
        # Let Windows know the environment changed (setting and clearing a dummy variable sends the notice)
        [Environment]::SetEnvironmentVariable("PDFDESK_ENV_REFRESH", "1", "User")
        [Environment]::SetEnvironmentVariable("PDFDESK_ENV_REFRESH", $null, "User")
        Say "Added $Dest to your PATH (open a new terminal to use 'pdfdesk')"
    }
}

# ---- quick check
& $VenvPy -c "import sys; sys.path.insert(0, sys.argv[1]); import pymupdf, PySide6.QtWidgets, pdfdesk.mainwindow" $AppDir 2>$null
if ($LASTEXITCODE -eq 0) {
    Say "Installed. Start PDF Desk from the Start menu."
} else {
    Write-Host "Installed, but a quick check failed. Run this to see the error:"
    Write-Host "  `"$VenvPy`" `"$Script`""
}

# ---- optional extras
$lo = @("$env:ProgramFiles\LibreOffice\program\soffice.exe", "${env:ProgramFiles(x86)}\LibreOffice\program\soffice.exe")
if (-not ($lo | Where-Object { Test-Path $_ })) {
    Write-Host "Optional: install LibreOffice (libreoffice.org) for .doc/.xls/.ppt/OpenDocument conversions."
}
$tess = @("$env:ProgramFiles\Tesseract-OCR\tessdata", "$env:LOCALAPPDATA\Programs\Tesseract-OCR\tessdata", "$env:APPDATA\PDF Desk\tessdata")
if (-not ($tess | Where-Object { (Test-Path $_) -and (Get-ChildItem -Path $_ -Filter *.traineddata -ErrorAction SilentlyContinue) })) {
    Write-Host "Optional: for Recognize Text (OCR), install Tesseract OCR (UB Mannheim build) or copy"
    Write-Host "          .traineddata files into $env:APPDATA\PDF Desk\tessdata"
}
