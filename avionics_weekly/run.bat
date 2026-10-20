@echo off
cd /d "%~dp0"
if not exist .venv (
  echo [Setup] Creating virtual environment...
  python -m venv .venv || (echo Python not found. See README. & pause & exit /b 1)
)
echo [Setup] Checking packages...
.venv\Scripts\python -m pip install -q -r requirements.txt || (pause & exit /b 1)
if not exist config.json copy config.example.json config.json >nul
start "" http://127.0.0.1:5000/
.venv\Scripts\python app.py %*
pause
