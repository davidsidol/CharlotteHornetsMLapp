@echo off
title Charlotte Hornets Dashboard
set PYTHON=C:\Users\Administrator.DAVIDSIDOL\AppData\Local\Programs\Python\Python315\python.exe
cd /d %~dp0
echo Installing dependencies...
"%PYTHON%" install.py
echo.
echo ==========================================
echo  Charlotte Hornets Dashboard
echo  http://localhost:8080
echo  Press Ctrl+C to stop
echo ==========================================
echo.
"%PYTHON%" serve.py
pause
