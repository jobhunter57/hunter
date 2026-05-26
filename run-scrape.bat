@echo off
REM =====================================================================
REM   RoleDar local run script (Windows)
REM =====================================================================
REM   You normally don't edit this file. Put your email settings in
REM   local-secrets.bat instead. To schedule it, see the README section
REM   "Setting up a local schedule."
REM =====================================================================

REM Always run from the folder this script lives in, so the scraper finds
REM companies.yaml and friends no matter where it was launched from.
cd /d "%~dp0"

REM Load email settings if the user filled them in.
if exist "local-secrets.bat" call "local-secrets.bat"

REM Decide whether email is really configured. A file copied from the template
REM but not yet edited still holds the "re_..." / "you@example.com" placeholders;
REM treat those (and blanks) as "off" so we don't hand junk to Resend.
if "%RESEND_API_KEY%"=="" goto noemail
if "%NOTIFY_EMAIL%"=="" goto noemail
if "%FROM_EMAIL%"=="" goto noemail
if "%RESEND_API_KEY%"=="re_..." goto noemail
if "%NOTIFY_EMAIL%"=="you@example.com" goto noemail
echo [%date% %time%] Email settings found - will email %NOTIFY_EMAIL%.
goto run
:noemail
REM Clear them so scrape.py / notify.py see no email config and skip cleanly.
set RESEND_API_KEY=
set NOTIFY_EMAIL=
set FROM_EMAIL=
echo [%date% %time%] Email is off (settings blank or still the template defaults) - writing CSV reports only.
:run

REM Run the scraper using the virtualenv's Python.
.venv\Scripts\python.exe scrape.py
