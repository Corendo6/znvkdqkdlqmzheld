@echo off
setlocal EnableExtensions
cd /d "%~dp0"
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
%PYEXE% %PYOPT% -c "import openpyxl, PIL" >nul 2>&1
if errorlevel 1 %PYEXE% %PYOPT% -m pip install openpyxl pillow
if errorlevel 1 goto failed
%PYEXE% %PYOPT% "coupang_manager.pyw"
exit /b %errorlevel%
:failed
echo Required Python package installation failed.
pause
exit /b 1
:no_python
echo Python was not found in PATH.
pause
exit /b 1
