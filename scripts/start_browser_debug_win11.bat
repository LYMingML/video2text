@echo off
setlocal

set "SCRIPT_DIR=%~dp0"
set "POWERSHELL_SCRIPT=%SCRIPT_DIR%start_browser_debug_win11.ps1"

if not exist "%POWERSHELL_SCRIPT%" (
  echo [ERROR] 未找到脚本: %POWERSHELL_SCRIPT%
  exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%POWERSHELL_SCRIPT%" %*
set "EXIT_CODE=%ERRORLEVEL%"

if not "%EXIT_CODE%"=="0" (
  echo [ERROR] 调试浏览器启动失败，退出码 %EXIT_CODE%
)

exit /b %EXIT_CODE%