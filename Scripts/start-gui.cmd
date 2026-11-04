@echo off
setlocal
cd /d "%~dp0.."
poetry run account-eval gui %*
exit /b %ERRORLEVEL%
