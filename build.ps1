<#
.SYNOPSIS
  Builds a standalone PDF Desk for Windows (no Python needed on the target computer).

.DESCRIPTION
  Output:
    dist\PDF Desk\PDF Desk.exe         the program folder (can be copied anywhere)
    dist\PDF-Desk-windows-x64.zip      the same folder zipped
    dist\PDF-Desk-Setup.exe            a per-user installer, if Inno Setup 6 is installed
  Needs internet once, for the build.
#>
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)

$py = "python"
if (Get-Command py -ErrorAction SilentlyContinue) { $py = "py" }
& $py -m venv .build-venv
& .\.build-venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null
& .\.build-venv\Scripts\python.exe -m pip install --require-hashes -r requirements.lock
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
& .\.build-venv\Scripts\python.exe -m pip install pyinstaller
if ($LASTEXITCODE -ne 0) { throw "pip install failed" }
& .\.build-venv\Scripts\pyinstaller.exe --noconfirm --clean pdfdesk.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

$zip = "dist\PDF-Desk-windows-x64.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path "dist\PDF Desk" -DestinationPath $zip
Write-Host "Built: dist\PDF Desk\PDF Desk.exe and $zip"

$iscc = @("${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe", "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
          "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($iscc) {
    & $iscc installer.iss
    Write-Host "Built: dist\PDF-Desk-Setup.exe"
} else {
    Write-Host "Inno Setup 6 not found, so no Setup.exe was made (the zip works on its own)."
}
