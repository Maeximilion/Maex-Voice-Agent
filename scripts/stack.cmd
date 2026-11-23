@echo off
rem On/off switch for the workbench stack in Ubuntu WSL (~/Maex-Voice-Agent, docs/13 section 1).
rem Double-click for a menu, or pass the action: stack.cmd start|pause|resume|stop|status
rem pause freezes the containers and keeps their memory; stop ends them and frees it.
setlocal
set "ACTION=%~1"
set "MENU="
if not "%ACTION%"=="" goto run
set "MENU=1"
echo Workbench stack (~/Maex-Voice-Agent in Ubuntu WSL)
echo   1 start   2 pause   3 resume   4 stop   5 status
choice /c 12345 /n /m "Choose 1-5: "
if errorlevel 5 (set "ACTION=status") else if errorlevel 4 (set "ACTION=stop") else if errorlevel 3 (set "ACTION=resume") else if errorlevel 2 (set "ACTION=pause") else set "ACTION=start"

:run
set "VERB="
if /i "%ACTION%"=="start" set "VERB=start"
if /i "%ACTION%"=="pause" set "VERB=pause"
if /i "%ACTION%"=="resume" set "VERB=unpause"
if /i "%ACTION%"=="stop" set "VERB=stop"
if /i "%ACTION%"=="status" set "VERB=status"
if not defined VERB (
  echo Unknown action "%ACTION%". Use start, pause, resume, stop or status.
  set "RC=2"
  goto end
)
set "STEP=docker compose %VERB% &&"
if "%VERB%"=="status" set "STEP="
wsl.exe -d Ubuntu -- bash -lc "cd ~/Maex-Voice-Agent && %STEP% docker compose ps -a --format '{{.Name}}  {{.Status}}'"
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" echo Failed with exit code %RC%. Is Docker Desktop running?

:end
if defined MENU pause
exit /b %RC%
