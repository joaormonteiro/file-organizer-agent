@echo off
rem Remove a Tarefa Agendada do supervisor (nao mexe no watcher em si).

setlocal

schtasks /delete /tn "FileOrganizerAgentSupervisor" /f
if errorlevel 1 (
    echo Nada instalado, ou falha ao remover.
) else (
    echo Supervisor removido. O watcher, se estiver rodando, continua normal.
)

endlocal
