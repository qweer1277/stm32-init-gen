@echo off
rem Build a standalone Windows exe with PyInstaller.
rem ASCII only: cmd.exe reads this file with the system code page.
setlocal
cd /d "%~dp0.."

set "PY=..\core\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [ERROR] venv python not found: %PY%
    echo         Create it under core and install the GUI project first.
    pause
    exit /b 1
)

"%PY%" -m pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo [INFO] installing PyInstaller from the Tsinghua mirror...
    "%PY%" -m pip install --disable-pip-version-check ^
        -i https://pypi.tuna.tsinghua.edu.cn/simple ^
        --timeout 8 --retries 1 pyinstaller
    if errorlevel 1 (
        echo [ERROR] PyInstaller install failed.
        pause
        exit /b 1
    )
)

rem console=False in the spec means --selfcheck prints nowhere. For debugging,
rem flip console to True in packaging\stm32initgen.spec and rebuild.
"%PY%" -m PyInstaller --noconfirm --clean --distpath dist --workpath build packaging\stm32initgen.spec
if errorlevel 1 (
    echo [ERROR] build failed.
    pause
    exit /b 1
)

echo.
echo [ OK ] dist\STM32InitGen\STM32InitGen.exe
echo        Move the whole STM32InitGen folder; the exe needs its _internal directory.
pause
