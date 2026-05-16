@echo off
REM Build TimeChecker.exe from the local tracker (no Flask, no DB code paths
REM exercised in remote mode, but still bundled so LOCAL mode also works).
REM
REM Requirements:
REM   pip install pyinstaller
REM   (the rest of requirements.txt installed)
REM
REM Output: ..\dist\TimeChecker.exe

setlocal
cd /d "%~dp0\.."

if exist build rmdir /s /q build
if exist dist  rmdir /s /q dist

pyinstaller ^
  --onefile ^
  --windowed ^
  --name TimeChecker ^
  --add-data "config.json;." ^
  --add-data "templates;templates" ^
  --hidden-import win32timezone ^
  main.py

if errorlevel 1 (
  echo.
  echo PyInstaller failed.
  exit /b 1
)

echo.
echo Built: %CD%\dist\TimeChecker.exe
endlocal
