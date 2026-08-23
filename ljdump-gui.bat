@echo off
rem Launch the ljdump graphical front end.
rem
rem Run this from anywhere - it switches to its own folder first, so ljdump finds
rem its config and support files no matter where the shortcut lives.
cd /d "%~dp0"

rem Prefer the Windows Python launcher, and fall back to python on the PATH.
where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "ljdump-gui.py"
) else (
    python "ljdump-gui.py"
)

rem If anything went wrong, keep the window open so the message can be read.
rem A vanishing console with no explanation is the exact problem this replaces.
if errorlevel 1 (
    echo.
    echo ljdump could not start. The error above explains why.
    echo If it says Python is not recognised, install Python from python.org
    echo and tick "Add python.exe to PATH" during setup.
    echo.
    pause
)
