@echo off
chcp 65001 >nul
setlocal
echo ====================================================
echo   T3 Code Codex 会话一键同步并重启刷新
echo ====================================================
echo.
call "%~dp0t3-codex-sync.cmd" sync --restart
echo.
pause
