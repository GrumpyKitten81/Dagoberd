@echo off
setlocal
set "ROOT=%~dp0.."
if exist "%ROOT%\.dagoberd\mcp.cmd" (
  "%ROOT%\.dagoberd\mcp.cmd" %*
  exit /b %ERRORLEVEL%
)
cd /d "%ROOT%"
poetry run python -m dagoberd.mcp_server %*
