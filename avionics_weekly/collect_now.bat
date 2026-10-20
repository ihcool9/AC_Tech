@echo off
cd /d "%~dp0"
if not exist .venv (echo Run run.bat once first. & pause & exit /b 1)
.venv\Scripts\python app.py --collect
pause
