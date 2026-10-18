@echo off
rem Launch the STM32 init code generator GUI.
rem
rem Why a launcher instead of telling the user to type `stm32-init-gui`:
rem that command lives in the venv and only works in an activated shell.
rem After the GUI was split into its own project, users often do not have
rem such a shell at hand. Double-clicking a file is more reliable.
rem
rem Absolute paths are derived from this file's location, so it works
rem from any working directory.
rem
rem NOTE: this file is deliberately pure ASCII. A .cmd is parsed with the
rem OEM code page before `chcp` takes effect, so non-ASCII bytes here get
rem split into garbage commands.
setlocal enabledelayedexpansion

set "PY=%~dp0..\core\.venv\Scripts\python.exe"
set "PYW=%~dp0..\core\.venv\Scripts\pythonw.exe"

if not exist "%PY%" goto :no_venv

rem Silent self-check first: missing deps or a broken Qt platform plugin
rem show up here instead of a window that flashes and disappears.
"%PY%" -m stm32gen_gui --selfcheck >nul 2>&1
if errorlevel 1 goto :bad_env

if exist "%PYW%" (
    start "" "%PYW%" -m stm32gen_gui %*
) else (
    start "" "%PY%" -m stm32gen_gui %*
)
endlocal
exit /b 0

:no_venv
echo.
echo [ERROR] Python interpreter not found:
echo   %PY%
echo.
echo Create the venv in the core project first, then install this project:
echo   cd ..\core
echo   python -m venv .venv
echo   .venv\Scripts\python.exe -m pip install -r requirements-dev.txt
echo   .venv\Scripts\python.exe -m pip install --no-build-isolation --no-deps -e .
echo   cd ..\gui
echo   ..\core\.venv\Scripts\python.exe -m pip install --no-build-isolation -e .
echo.
pause
endlocal
exit /b 1

:bad_env
echo.
echo [ERROR] Self-check failed. Details:
echo.
"%PY%" -m stm32gen_gui --selfcheck
echo.
pause
endlocal
exit /b 1
