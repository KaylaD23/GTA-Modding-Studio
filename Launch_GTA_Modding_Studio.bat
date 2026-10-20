@echo off
setlocal EnableExtensions
cd /d "%~dp0" || (
    echo ERROR: Cannot open the GTA Modding Studio directory.
    pause
    exit /b 1
)
set "PYTHON_CMD="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py -3"
if not defined PYTHON_CMD (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
    echo ERROR: No working Python 3.10+ interpreter found.
    echo Install Python 3.10+ and rerun Install_Dependencies.bat.
    pause
    exit /b 1
)
%PYTHON_CMD% "GTA_Modding_Studio.py"
set "APP_EXIT_CODE=%ERRORLEVEL%"
if not "%APP_EXIT_CODE%"=="0" (
    echo.
    echo ERROR: GTA Modding Studio exited with code %APP_EXIT_CODE%.
    pause
)
exit /b %APP_EXIT_CODE%
