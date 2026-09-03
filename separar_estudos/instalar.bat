@echo off
setlocal
cd /d "%~dp0"
python -m venv .venv
if errorlevel 1 goto :erro
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 goto :erro
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :erro
echo.
echo Instalacao concluida. Agora execute executar.bat.
pause
exit /b 0

:erro
echo.
echo Falha na instalacao. Confirme se o Python esta instalado e acessivel pelo comando python.
pause
exit /b 1
