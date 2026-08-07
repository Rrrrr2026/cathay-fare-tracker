@echo off
rem Daily Cathay Pacific fare collection - invoked by Windows Task Scheduler.
rem Retries a failed run up to 3 times, 10 minutes apart; same-day re-runs are
rem idempotent (primary-key overwrite), and run_daily.py exits 0 immediately
rem if another collection is already active.
cd /d "%~dp0"
if not exist logs mkdir logs
type nul > logs\last_run.log
forfiles /p logs /m run_*.log /d -60 /c "cmd /c del @path" >nul 2>&1
for /l %%i in (1,1,3) do (
  "C:\Users\roger.DESKTOP-7Q2P0JS\AppData\Local\Python\pythoncore-3.14-64\python.exe" scripts\run_daily.py >> logs\last_run.log 2>&1
  if not errorlevel 1 exit /b 0
  timeout /t 600 /nobreak >nul
)
exit /b 1
