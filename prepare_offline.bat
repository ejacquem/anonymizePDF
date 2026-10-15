@echo off
rem ---------------------------------------------------------------------------------------------
rem  Run this on a PC WITH internet access and the SAME Python version as the production PC (3.12).
rem  It downloads everything the app needs into the "wheels" folder (packages + the French names model).
rem  Then copy the whole project folder, including "wheels", to the production PC and run start.bat there:
rem  it installs from "wheels" without internet.
rem  Optional: copy your secret key too (%%APPDATA%%\anonymizePDF\secret.key) if the fake names must be the same on
rem  both PCs.
rem ---------------------------------------------------------------------------------------------
setlocal
cd /d "%~dp0"
set "PY="
py -3.12 -c "import sys" >nul 2>&1 && set "PY=py -3.12"
if not defined PY py -3.13 -c "import sys" >nul 2>&1 && set "PY=py -3.13"
if not defined PY set "PY=python"
%PY% tools\prepare_offline.py wheels
pause
