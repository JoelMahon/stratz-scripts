@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
title STRATZ Dota tools

if not exist ".venv\Scripts\python.exe" (
    echo Creating a private Python environment...
    py -3 -m venv .venv
    if errorlevel 1 (
        color 0C
        echo.
        echo ERROR: Python could not create .venv.
        echo Install Python 3.10 or newer from https://www.python.org/downloads/
        echo During setup, enable "Add Python to PATH".
        pause
        exit /b 1
    )
)

call ".venv\Scripts\activate.bat"

echo Checking pinned dependencies...
python -m pip install --require-hashes -r requirements.lock
if errorlevel 1 (
    color 0E
    echo WARNING: pinned timezone data could not be installed.
    echo Internet access may be unavailable. You can retry later with:
    echo   python -m pip install --require-hashes -r requirements.lock
)

if not exist "config.json" (
    color 0E
    echo.
    echo WARNING: config.json is missing.
    echo.
    echo 1. Copy config.example.json
    echo 2. Rename the copy to config.json
    echo 3. Get a token from https://stratz.com/api
    echo 4. Replace PASTE_YOUR_STRATZ_API_TOKEN_HERE in config.json
    echo.
    echo config.json is private and ignored by Git.
    pause
    exit /b 2
)

python -c "import json,sys; d=json.load(open('config.json',encoding='utf-8')); t=str(d.get('api_key','')).strip(); n=''.join(c for c in t.lower() if c.isalnum()); sys.exit(0 if t and n not in {'pasteyourstratzapitokenhere','yourtokenhere','changeme'} else 2)" >nul 2>&1
if errorlevel 1 (
    color 0C
    echo.
    echo ERROR: config.json does not contain a real STRATZ API token.
    echo Get one from https://stratz.com/api and replace the placeholder.
    echo The token belongs at the top level, in the api_key setting.
    pause
    exit /b 2
)

color 0A
echo.
echo =============================================
echo  Ready - personal config and token detected
echo =============================================
echo.
echo Try:
echo   python .\lane_gold.py
echo   python .\item_winrate.py
echo   python .\match_diary.py
echo.
echo Add -D to run without the settings review.
echo The match diary opens its GUI directly and does not need -D.
echo Use --help for all options.
echo Closing this window ends the prepared environment.
echo.
color 07

cmd /k
