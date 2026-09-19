@echo off
REM Double-click this to do the whole round trip:
REM   1. download anything new from the CMS
REM   2. rebuild the study checklist
REM   3. sync it with Notion (and pull back ticks made on your phone)
REM Each step skips work it has already done, so running it twice is safe.
REM Every step waits for Enter, so you can read what it did before moving on.

setlocal
cd /d "%~dp0"

set PY=python
where python >nul 2>nul
if errorlevel 1 set PY=py
where %PY% >nul 2>nul
if errorlevel 1 (
    echo Python was not found on your PATH.
    echo Install it from python.org, or run the scripts from a terminal.
    echo.
    pause
    goto :done
)

echo ==========================================
echo  1/3  Downloading new files from the CMS
echo ==========================================
%PY% cms_scraper.py
if errorlevel 1 echo    ^(the scrape failed - carrying on with the files already on disk^)

echo.
echo ==========================================
echo  2/3  Rebuilding the study checklist
echo ==========================================
%PY% make_checklist.py
if errorlevel 1 (
    echo    ^(checklist step failed - skipping the Notion sync^)
    goto :done
)

echo.
echo ==========================================
echo  3/3  Syncing with Notion
echo ==========================================
%PY% notion_sync.py

:done
endlocal
