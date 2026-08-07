@echo off
rem Start the local dashboard server and open it in the default browser.
cd /d "%~dp0"
start "" http://127.0.0.1:8737/
"C:\Users\roger.DESKTOP-7Q2P0JS\AppData\Local\Python\pythoncore-3.14-64\python.exe" scripts\api_server.py
