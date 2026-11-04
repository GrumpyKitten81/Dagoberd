@echo off
setlocal
set "ROOT=%~dp0.."
if exist "%ROOT%\.account-eval\mcp.cmd" (
  "%ROOT%\.account-eval\mcp.cmd" %*
  exit /b %ERRORLEVEL%
)
cd /d "%ROOT%"
poetry run python -m dagoberd.mcp_server %*
