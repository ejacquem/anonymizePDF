@echo off
rem ---------------------------------------------------------------------------------------------
rem  PDF Anonymizer - double-click to start.
rem  First start: creates the Python environment (.venv). Uses the "wheels" folder if present (no internet
rem  needed, see prepare_offline.bat), otherwise downloads the packages.
rem  The folder that contains the client folders is asked once (folder picker) and remembered in
rem  start_root.txt. To use another folder: drag it onto start.bat, or delete start_root.txt.
rem  No admin rights needed. Optional: set ANON_PORT=5001 if port 5000 is already used.
rem ---------------------------------------------------------------------------------------------
setlocal
cd /d "%~dp0"
title PDF Anonymizer
set "PY=.venv\Scripts\python.exe"

if exist "%PY%" goto :root

echo First start: creating the Python environment (a few minutes)...
py -3.12 -m venv .venv >nul 2>&1
if not exist "%PY%" py -3.13 -m venv .venv >nul 2>&1
if not exist "%PY%" python -m venv .venv >nul 2>&1
if not exist "%PY%" (
    echo ERROR: Python 3.12 or 3.13 is needed: https://www.python.org/downloads/ ^(no admin rights needed:
    echo        untick "install for all users"^).
    goto :fail
)
if exist "wheels\" goto :offline
echo Installing the packages from the internet...
"%PY%" -m pip install --disable-pip-version-check -r requirements.txt || goto :pipfail
"%PY%" -m spacy download fr_core_news_md
goto :root

:offline
echo Installing the packages from the "wheels" folder (offline)...
"%PY%" -m pip install --disable-pip-version-check --no-index --find-links wheels -r requirements.txt || goto :pipfail
for %%W in (wheels\*_core_news_*.whl) do "%PY%" -m pip install --disable-pip-version-check --no-index --find-links wheels "%%W"

:root
set "ROOT=%~1"
if not "%ROOT%"=="" goto :remember
if exist start_root.txt set /p ROOT=<start_root.txt
if not "%ROOT%"=="" if exist "%ROOT%\" goto :run
echo Choose the folder that contains the client folders...
for /f "usebackq delims=" %%D in (`powershell -NoProfile -STA -Command "Add-Type -AssemblyName System.Windows.Forms; $d = New-Object System.Windows.Forms.FolderBrowserDialog; $d.Description = 'Folder that contains the client folders'; if ($d.ShowDialog() -eq 'OK') { $d.SelectedPath }"`) do set "ROOT=%%D"
if "%ROOT%"=="" set /p "ROOT=Folder that contains the client folders: "
if "%ROOT%"=="" goto :fail

:remember
if exist "%ROOT%\" goto :save
echo ERROR: folder not found: "%ROOT%"
goto :fail

:save
> start_root.txt echo %ROOT%

:run
set "OPEN=--open"
if defined ANON_NO_BROWSER set "OPEN="
set "PORT=5000"
if defined ANON_PORT set "PORT=%ANON_PORT%"
echo.
echo Folder: %ROOT%
echo The app opens in your browser. Keep this window open while you work; close it to stop the app.
echo.
"%PY%" app.py --root "%ROOT%" --port %PORT% %OPEN%
goto :end

:pipfail
echo ERROR: the packages could not be installed. Fix the problem below and run start.bat again
echo        (on a PC without internet: run prepare_offline.bat on another PC and copy the "wheels" folder here).
rmdir /s /q .venv >nul 2>&1

:fail
pause
exit /b 1

:end
endlocal
