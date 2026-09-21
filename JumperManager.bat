@echo off
setlocal
cd /d "%~dp0"
where python.exe >nul 2>&1
if errorlevel 1 (
    echo Python 3.10 or later is required. Install Python and add it to PATH.
    pause
    exit /b 1
)
python.exe "%~dp0app.py" %*
if errorlevel 1 pause
