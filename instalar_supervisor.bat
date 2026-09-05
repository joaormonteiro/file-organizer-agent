@echo off
rem ==========================================================================
rem  FileOrganizerAgent - supervisor externo (watchdog).
rem
rem  Registra uma Tarefa Agendada que roda supervisor.py a cada 5 minutos,
rem  indefinidamente. Nao usa "ao logon" (schtasks /sc onlogon ja falhou
rem  silencioso no EyeAgent) - usa repeticao continua (/sc minute), tipo de
rem  gatilho mais confiavel no Agendador.
rem
rem  O supervisor.py roda e sai em menos de 2 segundos, entao nao fica tempo
rem  suficiente vivo pra ser pego pelo detector de "Application Hang" do
rem  Windows (o mesmo que matou o watcher.py em 2026-09-04): ele so ataca
rem  processo com janela que fica de pe sem responder por varios segundos.
rem ==========================================================================

setlocal

set "PROJETO=%~dp0"
set "PROJETO=%PROJETO:~0,-1%"
cd /d "%PROJETO%"

set "VENV=%PROJETO%\venv"
if not exist "%VENV%\Scripts\pythonw.exe" set "VENV=%PROJETO%\.venv"
set "PYTHONW=%VENV%\Scripts\pythonw.exe"

if not exist "%PYTHONW%" (
    echo [ERRO] venv nao encontrada em "%PROJETO%\venv" nem "%PROJETO%\.venv".
    exit /b 1
)

if not exist "%PROJETO%\supervisor.py" (
    echo [ERRO] supervisor.py nao encontrado em "%PROJETO%".
    exit /b 1
)

schtasks /delete /tn "FileOrganizerAgentSupervisor" /f >nul 2>&1

schtasks /create /tn "FileOrganizerAgentSupervisor" ^
    /tr "\"%PYTHONW%\" \"%PROJETO%\supervisor.py\"" ^
    /sc minute /mo 5 /f

if errorlevel 1 (
    echo.
    echo [ERRO] falha ao criar a tarefa agendada.
    exit /b 1
)

echo.
echo Supervisor instalado: roda a cada 5 minutos.
echo Sobe o watcher se ele estiver morto, ou se estiver vivo mas o log nao
echo mexer ha mais de 30 minutos (travado/zumbi).
echo.
echo Log do supervisor: "%PROJETO%\supervisor.log"
echo Para desinstalar: desinstalar_supervisor.bat

endlocal
