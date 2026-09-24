@echo off
rem PDF Editor - double-click to start (Windows). First run sets everything up.
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe (
  echo First run: setting up PDF Editor...
  py -3 -m venv .venv 2>nul || python -m venv .venv
  .venv\Scripts\python -m pip install --upgrade pip >nul
  .venv\Scripts\python -m pip install -r requirements.txt
)
.venv\Scripts\python run.py %*
