@echo off
setlocal
set "SCRIPT_DIR=%~dp0"
python "%SCRIPT_DIR%t3_codex_sync.py" %*
exit /b %ERRORLEVEL%
