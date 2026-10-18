@echo off
rem ASCII only in this file: cmd.exe reads it with the system code page.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0create-shortcut.ps1"
echo.
pause
