@echo off
setlocal EnableExtensions
cd /d "%~dp0" || (
    echo ERROR: Cannot open the GTA Modding Studio directory.
    pause
    exit /b 1
)
title GTA Modding Studio - Install Dependencies

echo ============================================================
echo GTA Modding Studio - Dependency Installer
echo ============================================================
echo This installs Python packages, FFmpeg, and CSO/ISZ image converters
echo used by Audio Preparation and the Patch ISO workflow.
echo.

set "PYTHON_CMD="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py -3"
if not defined PYTHON_CMD (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
    echo ERROR: Python 3 was not found.
    echo Install Python 3.10 or newer from https://www.python.org/downloads/windows/
    echo During setup, enable the Python launcher and Tcl/Tk support.
    pause
    exit /b 1
)

%PYTHON_CMD% -c "import sys; print('Using Python', sys.version.split()[0]); raise SystemExit(0 if sys.version_info >= (3,10) else 1)"
if errorlevel 1 (
    echo ERROR: Python 3.10 or newer is required.
    pause
    exit /b 1
)

%PYTHON_CMD% -c "import tkinter" >nul 2>&1
if errorlevel 1 (
    echo ERROR: This Python installation has no working Tkinter/Tcl-Tk support.
    echo Rerun the Python installer and enable Tcl/Tk, or install python.org Python with Tcl/Tk.
    pause
    exit /b 1
)

echo.
echo [1/5] Upgrading pip...
%PYTHON_CMD% -m ensurepip --upgrade >nul 2>&1
%PYTHON_CMD% -m pip install --upgrade pip
if errorlevel 1 goto :python_failed

echo.
echo [2/5] Installing Studio, audio encoder, and drag-and-drop Python packages...
%PYTHON_CMD% -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto :python_failed

%PYTHON_CMD% -c "import numpy, numba, tkinterdnd2; print('Python dependencies verified: NumPy', numpy.__version__, '| Numba', numba.__version__, '| tkinterdnd2', tkinterdnd2.__version__ if hasattr(tkinterdnd2,'__version__') else 'installed')"
if errorlevel 1 goto :python_failed

echo.
echo [3/5] Installing/checking FFmpeg for Audio Preparation...
if not exist "%~dp0Install_FFmpeg.ps1" (
    echo ERROR: Install_FFmpeg.ps1 is missing from this folder.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install_FFmpeg.ps1"
if errorlevel 1 (
    echo.
    echo Python dependencies are installed, but FFmpeg setup did not complete.
    echo Fix the network or FFmpeg issue above and rerun Install_Dependencies.bat.
    pause
    exit /b 1
)

echo.
echo [4/5] Installing/checking CSO and ISZ image converters...
if not exist "%~dp0Install_Image_Tools.ps1" (
    echo ERROR: Install_Image_Tools.ps1 is missing from this folder.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0Install_Image_Tools.ps1"
if errorlevel 1 (
    echo.
    echo Python and FFmpeg setup completed, but CSO/ISZ tool setup failed.
    echo Check the error above and rerun Install_Dependencies.bat.
    pause
    exit /b 1
)

echo.
echo [5/5] Smoke-testing bundled PSP tools...
if not exist "%~dp0Verify_PSP_Tools.py" (
    echo ERROR: Verify_PSP_Tools.py missing. Re-extract the complete ZIP.
    goto :psp_failed
)
rem The verifier runs PSPDecrypt --help and performs a real short AT3 encode.
rem It does not decrypt game EBOOTs or claim to establish in-game playback.
%PYTHON_CMD% "%~dp0Verify_PSP_Tools.py"
if errorlevel 1 goto :psp_failed

echo.
echo ============================================================
echo REQUIRED INSTALLATION STEPS COMPLETED.
if exist "%~dp0tools\ffmpeg\bin\ffmpeg.exe" (
    echo FFmpeg: local copy at tools\ffmpeg\bin
) else (
    echo FFmpeg: system PATH installation was verified above; no local copy required.
)
echo CSO/ISZ tools: tools\image_tools
echo PSP tools: PSPDecrypt CLI started and a synthetic ATRAC3plus sample encoded.
echo Actual PSP decryption and in-game radio playback still require game-file testing.
echo You can now run Launch_GTA_Modding_Studio.bat.
echo ============================================================
pause
exit /b 0

:python_failed
echo.
echo ERROR: Python package installation or verification failed.
echo Check the error above, then rerun Install_Dependencies.bat.
pause
exit /b 1

:psp_failed
echo.
echo ERROR: PSP tool startup or ATRAC3plus encoding validation failed.
echo Core Python / FFmpeg / CSO / ISZ dependencies may still work.
echo Inspect the failed tool diagnostics above. Re-extract the bundled PSP tools
echo if they are missing, or check Windows compatibility and msvcr71.dll.
pause
exit /b 1
