@echo off
REM Double-click to open the Furniture Player Intelligence portal.
REM Keep this window open while you use it; close it to stop the portal.

cd /d "%~dp0"
title Furniture Player Intelligence

set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"

"%PY%" --version >nul 2>&1
if errorlevel 1 (
  echo.
  echo   Python was not found. Install Python 3.12, then run this again.
  echo.
  pause
  exit /b 1
)

REM Free port 8501 if an earlier copy of the portal is still running.
for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:":8501 .*LISTENING"') do taskkill /f /pid %%p >nul 2>&1

echo.
echo   Starting the portal. Your browser will open in a few seconds.
echo   If it does not, open:  http://localhost:8501
echo.
echo   Keep this window open. Close it to stop the portal.
echo.

REM Open the browser ourselves once the server has had time to start.
start "" /min powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep -Seconds 6; Start-Process 'http://localhost:8501'"

"%PY%" -m streamlit run app.py --server.port 8501 --server.headless true

echo.
echo   The portal has stopped.
pause
