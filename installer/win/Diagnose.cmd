@echo off
rem Runs the pdf2md right-click diagnostic on this machine.
rem Shows what is registered, whether the program is there, whether it is
rem blocked, and whether it starts.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0pdf2md-doctor.ps1" %*
echo.
pause
