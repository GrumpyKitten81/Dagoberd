@echo off
setlocal
cd /d "%~dp0.."
poetry run dagoberd gui %*
exit /b %ERRORLEVEL%
