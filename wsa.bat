@echo off
setlocal
chcp 65001 >nul 2>&1
set "AGENT_DIR=%~dp0"
set "PYTHONPATH=%AGENT_DIR%"
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
if exist "%AGENT_DIR%.venv\Scripts\python.exe" (
    "%AGENT_DIR%.venv\Scripts\python.exe" -m wsa_agent %*
) else (
    python -m wsa_agent %*
)
endlocal
