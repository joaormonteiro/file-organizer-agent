@echo off
rem ==========================================================================
rem  FileOrganizerAgent - supervisor externo (watchdog).
rem
rem  Registra uma Tarefa Agendada que roda supervisor.py a cada 5 minutos,
rem  indefinidamente, e sai. Sobe o watcher se ele estiver morto, ou vivo mas
rem  com o log parado ha muito tempo (travado/zumbi).
rem
rem  Este e o UNICO ponto do projeto que usa PowerShell, de proposito:
rem    - `schtasks /create` simples cria a tarefa com
rem      DisallowStartIfOnBatteries=True, e ai ela NAO roda com o notebook
rem      desplugado (descoberto na marra em 2026-09-05: o supervisor parou
rem      quando tiraram da tomada).
rem    - `schtasks /create /xml` conseguiria desligar isso, mas e chato de
rem      gerar um XML que o schtasks aceite (encoding, campos obrigatorios).
rem    - `Register-ScheduledTask` faz isso limpo numa linha.
rem  O autostart principal (o .bat em shell:startup, via instalar.bat) segue
rem  100%% cmd puro - a ressalva historica de "sem PowerShell" era sobre
rem  aquele mecanismo de persistencia, nao sobre registrar tarefa agendada.
rem ==========================================================================

setlocal

set "PROJETO=%~dp0"
set "PROJETO=%PROJETO:~0,-1%"

if not exist "%PROJETO%\.venv\Scripts\pythonw.exe" (
    echo [ERRO] venv nao encontrada em "%PROJETO%\.venv".
    exit /b 1
)
if not exist "%PROJETO%\supervisor.py" (
    echo [ERRO] supervisor.py nao encontrado em "%PROJETO%".
    exit /b 1
)

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$pw = Join-Path '%PROJETO%' '.venv\Scripts\pythonw.exe';" ^
  "$sc = Join-Path '%PROJETO%' 'supervisor.py';" ^
  "$a = New-ScheduledTaskAction -Execute $pw -Argument ('\"' + $sc + '\"') -WorkingDirectory '%PROJETO%';" ^
  "$t = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Minutes 5);" ^
  "$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 5);" ^
  "Register-ScheduledTask -TaskName 'FileOrganizerAgentSupervisor' -Action $a -Trigger $t -Settings $s -Description 'Watchdog do file-organizer: roda a cada 5 min (inclusive na bateria) e religa o watcher se cair.' -Force | Out-Null;" ^
  "Write-Host 'Supervisor instalado: roda a cada 5 minutos, inclusive na bateria.'"

if errorlevel 1 (
    echo.
    echo [ERRO] falha ao registrar a tarefa agendada.
    exit /b 1
)

echo Log do supervisor: "%PROJETO%\supervisor.log"
echo Para desinstalar: desinstalar_supervisor.bat

endlocal
