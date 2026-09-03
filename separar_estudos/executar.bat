@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" separar_estudos.py
) else (
  python separar_estudos.py
)
pause
