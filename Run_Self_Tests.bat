@echo off
setlocal EnableExtensions
cd /d "%~dp0" || exit /b 1
set "PYTHON_CMD="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py -3"
if not defined PYTHON_CMD (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
)
if not defined PYTHON_CMD (
    echo ERROR: Python 3.10+ could not be found.
    pause
    exit /b 1
)
%PYTHON_CMD% GTA_Modding_Studio.py --self-test
if errorlevel 1 goto :failed
%PYTHON_CMD% Texture_Regression_Tests.py
if errorlevel 1 goto :failed
%PYTHON_CMD% Test_PS2_TXD_Raster_Modify.py
if errorlevel 1 goto :failed
%PYTHON_CMD% Test_DTZ_Section_Index.py
if errorlevel 1 goto :failed
%PYTHON_CMD% Test_VCS_Menu_Font_XET.py
if errorlevel 1 goto :failed
%PYTHON_CMD% Test_DTZ_Named_Texture_Boundaries.py
if errorlevel 1 goto :failed
%PYTHON_CMD% Test_DTZ_CHK_Relocation.py
if errorlevel 1 goto :failed
%PYTHON_CMD% Audio_Preparation_Profiles.py
if errorlevel 1 goto :failed
%PYTHON_CMD% Test_Audio_Targets_v0.3.34.py
if errorlevel 1 goto :failed
%PYTHON_CMD% -m unittest -v Test_VCS_Dual_Mono_Regression
if errorlevel 1 goto :failed
%PYTHON_CMD% Test_AT3_Validation_v0.3.26.py
if errorlevel 1 goto :failed
%PYTHON_CMD% Test_AT3_RIFF_Chunk_Bounds.py
if errorlevel 1 goto :failed
%PYTHON_CMD% Test_PSP_Tool_Verification.py
if errorlevel 1 goto :failed
%PYTHON_CMD% Test_Native_ISZ.py
if errorlevel 1 goto :failed
%PYTHON_CMD% Test_Bundled_ISZ.py
if errorlevel 1 goto :failed
%PYTHON_CMD% Test_Windows_Batch_Encoding.py
if errorlevel 1 goto :failed
echo.
echo ALL AVAILABLE AUTOMATED TESTS PASSED.
pause
exit /b 0
:failed
echo.
echo One or more tests FAILED. Inspect the errors above.
pause
exit /b 1
