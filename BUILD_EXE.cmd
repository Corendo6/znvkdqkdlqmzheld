@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title CoupangManager EXE Builder

echo ========================================
echo CoupangManager - EXE Builder
echo ========================================
echo.

set "PYEXE="
set "PYOPT="

where py.exe >nul 2>&1
if errorlevel 1 goto try_python
set "PYEXE=py.exe"
set "PYOPT=-3"
goto python_found

:try_python
where python.exe >nul 2>&1
if errorlevel 1 goto no_python
set "PYEXE=python.exe"
set "PYOPT="

:python_found
echo [1/6] Python detected
%PYEXE% %PYOPT% --version
if errorlevel 1 goto failed

echo.
echo [2/6] Checking pip
%PYEXE% %PYOPT% -m pip --version >nul 2>&1
if errorlevel 1 %PYEXE% %PYOPT% -m ensurepip --upgrade
if errorlevel 1 goto failed

echo.
echo [3/6] Installing or updating build dependencies
%PYEXE% %PYOPT% -m pip install --upgrade pyinstaller openpyxl pillow
if errorlevel 1 goto failed

echo.
echo [4/6] Cleaning old build files
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "CoupangManager.spec" del /q "CoupangManager.spec"

echo.
echo [5/6] Building standalone EXE
%PYEXE% %PYOPT% -m PyInstaller --noconfirm --clean --windowed --onefile --name CoupangManager --collect-all openpyxl --add-data "data\starter.db;data" --add-data "data\model_master_seed.json;data" "coupang_manager.pyw"
if errorlevel 1 goto failed

if not exist "dist\CoupangManager.exe" goto failed

echo.
echo [6/6] Verifying EXE
for %%F in ("dist\CoupangManager.exe") do if %%~zF LSS 1000000 goto failed

echo.
echo ========================================
echo BUILD COMPLETE
echo ========================================
echo EXE: %CD%\dist\CoupangManager.exe
echo.
echo This EXE can run on another Windows PC without Python.
echo.
start "" explorer.exe "%CD%\dist"
pause
exit /b 0

:no_python
echo.
echo ERROR: Python was not found in PATH.
echo Close this window, finish installing Python, then run BUILD_EXE.cmd again.
echo.
pause
exit /b 1

:failed
echo.
echo ========================================
echo BUILD FAILED
echo ========================================
echo Please send a screenshot of the messages above.
echo.
pause
exit /b 1
