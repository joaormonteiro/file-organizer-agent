@echo off
rem Remove o autostart do FileOrganizerAgent (mesmo padrao do EyeAgent).
rem Cobre os tres mecanismos possiveis: .bat na pasta Inicializar, atalho
rem .lnk e tarefa agendada - mesmo que so o .bat seja usado hoje.

setlocal

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "ALVO=%STARTUP%\FileOrganizerAgent_autostart.bat"

schtasks /delete /tn "FileOrganizerAgent" /f >nul 2>&1
schtasks /delete /tn "FileOrganizer" /f >nul 2>&1
del "%STARTUP%\FileOrganizerAgent.lnk" >nul 2>&1

if exist "%ALVO%" (
    del "%ALVO%"
    echo Removido: "%ALVO%"
) else (
    echo Nada instalado em "%ALVO%".
)

echo.
echo Isso so impede o proximo logon de subir o agente sozinho. Se ele ja
echo estiver rodando agora, continua ate voce fechar a sessao ou encerrar
echo o processo manualmente pelo Gerenciador de Tarefas (procure "pythonw.exe"
echo - se houver mais de um na lista, confirme pela coluna "Linha de comando"
echo antes de encerrar, para nao derrubar outro programa Python seu por engano).

endlocal
