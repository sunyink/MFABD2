@echo off
setlocal
title Restore BrownDust II
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0restore_game_window.ps1" %*
if errorlevel 1 (
    echo.
    pause
    exit /b 1
)
exit /b 0
