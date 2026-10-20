@echo off
cd /d "%~dp0"
if "%~1"=="" (
    echo Provide a folder containing the four original GAME.DTZ files:
    echo   GTA LCS PS2 GAME.DTZ
    echo   GTA LCS PSP GAME.DTZ
    echo   GTA VCS PS2 GAME.DTZ
    echo   GTA VCS PSP GAME.DTZ
    echo Example: Run_DTZ_Four_Game_Audit.bat "C:\GTA References"
    pause
    exit /b 2
)
py -3 Test_DTZ_Indexes_Quick.py "%~1"
if errorlevel 1 (
    echo Four-game resource-index audit FAILED.
    pause
    exit /b 1
)
py -3 Test_DTZ_Decoders_v0.3.25.py "%~1"
if errorlevel 1 (
    echo Native raster-decoder audit FAILED.
    pause
    exit /b 1
)
echo All four original DTZ indexing and decoder checks passed.
pause
