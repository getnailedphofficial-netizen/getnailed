@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    echo Starting Get Nailed Nail Bar and Spa Development Server...
    echo Open http://127.0.0.1:8000/ in your browser.
    ".venv\Scripts\python.exe" manage.py runserver 127.0.0.1:8000
) else (
    echo Error: Virtual environment .venv not found.
    pause
)
