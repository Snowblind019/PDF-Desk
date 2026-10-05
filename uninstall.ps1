<#
.SYNOPSIS
  Removes PDF Desk for the current Windows user.

.PARAMETER Purge
  Also delete settings, the recent files list and saved signatures.
#>
param([switch]$Purge)

$ErrorActionPreference = "SilentlyContinue"
$AppName = "PDF Desk"
$Dest = Join-Path $env:LOCALAPPDATA "Programs\PDF Desk"

$running = Get-Process -Name pythonw, python -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($Dest, [StringComparison]::OrdinalIgnoreCase) }
if ($running) {
    Write-Host "Close PDF Desk first, then run the uninstaller again."
    Read-Host "Press Enter to exit"
    exit 1
}

Remove-Item -Force (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs\$AppName.lnk")
Remove-Item -Force (Join-Path ([Environment]::GetFolderPath("Desktop")) "$AppName.lnk")

$Classes = "HKCU:\Software\Classes"
Remove-Item -Recurse -Force "$Classes\PDFDesk.PDF"
Remove-ItemProperty -Path "$Classes\.pdf\OpenWithProgids" -Name "PDFDesk.PDF"
Remove-Item -Recurse -Force "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\PDFDesk"

# Only touch PATH if the installer added PDF Desk to it, and keep %VARIABLES% unexpanded.
$envKey = Get-Item -Path "HKCU:\Environment"
$userPath = $envKey.GetValue("Path", "", [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
if ($userPath -and (($userPath -split ";") -contains $Dest)) {
    $parts = ($userPath -split ";") | Where-Object { $_ -and ($_ -ne $Dest) }
    Set-ItemProperty -Path "HKCU:\Environment" -Name "Path" -Value ($parts -join ";") -Type ExpandString
    # Let Windows know the environment changed (setting and clearing a dummy variable sends the notice)
    [Environment]::SetEnvironmentVariable("PDFDESK_ENV_REFRESH", "1", "User")
    [Environment]::SetEnvironmentVariable("PDFDESK_ENV_REFRESH", $null, "User")
}

Remove-Item -Recurse -Force $Dest

if ($Purge) {
    Remove-Item -Recurse -Force (Join-Path $env:APPDATA $AppName)
    Remove-Item -Recurse -Force (Join-Path $env:LOCALAPPDATA $AppName)
    Write-Host "PDF Desk and its settings were removed."
} else {
    Write-Host "PDF Desk was removed. Settings and signatures are kept in $env:APPDATA\$AppName"
    Write-Host "(run uninstall.ps1 -Purge to delete them)."
}
