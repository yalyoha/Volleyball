@echo off
REM Build a standalone Windows .exe with embedded Python + pygame.
REM Output: dist\BeachVolleyball.exe (single file, no console window).

setlocal
cd /d "%~dp0"

echo === Cleaning previous build ===
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist BeachVolleyball.spec del /q BeachVolleyball.spec

echo === Generating icon.ico from ball drawing ===
python make_icon.py
if errorlevel 1 (
    echo === ICON GENERATION FAILED ===
    exit /b 1
)

echo === Building with PyInstaller ===
python -m PyInstaller ^
    --onefile ^
    --noconsole ^
    --name BeachVolleyball ^
    --icon icon.ico ^
    --add-data "icon.png;." ^
    --add-data "Ball.svg;." ^
    --add-data "ball.png;." ^
    --clean ^
    volleyball.py

if errorlevel 1 (
    echo === BUILD FAILED ===
    exit /b 1
)

echo.
echo === Build finished ===
echo Output: %cd%\dist\BeachVolleyball.exe
dir /b dist\BeachVolleyball.exe
endlocal
