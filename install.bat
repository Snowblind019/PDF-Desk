@echo off
rem Double-click to install PDF Desk for your Windows user account (no admin needed).
rem Add -Desktop to also create a desktop shortcut, e.g.:  install.bat -Desktop
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
echo.
pause
