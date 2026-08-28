@echo off
REM Build a standalone Windows .exe with embedded Python + pygame.
REM Output: dist\BeachVolleyball-<version>.exe (single file, no console window).
REM Version is derived from `git describe --tags` (e.g., v0.3 or v0.3-2-gabc123).

setlocal
cd /d "%~dp0"

echo === Detecting version ===
REM Use the nearest tag name only (no commits-ahead / dirty suffix) so release
REM artifacts stay named cleanly (e.g., BeachVolleyball-v0.3.exe).
for /f "delims=" %%v in ('git describe --tags --abbrev^=0 2^>nul') do set VER=%%v
if not defined VER set VER=dev
echo Version: %VER%

echo === Cleaning previous build ===
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
del /q BeachVolleyball*.spec 2>nul

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
    --name BeachVolleyball-%VER% ^
    --icon icon.ico ^
    --add-data "icon.png;." ^
    --add-data "Ball.svg;." ^
    --clean ^
    volleyball.py

if errorlevel 1 (
    echo === BUILD FAILED ===
    exit /b 1
)

echo.
echo === Build finished ===
echo Output: %cd%\dist\BeachVolleyball-%VER%.exe
dir /b dist\BeachVolleyball-%VER%.exe
endlocal
