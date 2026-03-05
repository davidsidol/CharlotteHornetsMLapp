@echo off
title Stop Hornets Dashboard
echo Stopping Hornets Dashboard on port 8080...
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":8080"') do (
    echo Killing PID %%a
    taskkill /F /PID %%a 2>nul
)
echo Done.
pause
