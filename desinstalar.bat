@echo off
rem Remove o auto-start instalado por instalar.bat.
rem Nao mexe em Agendador de Tarefas - nunca foi esse o mecanismo usado aqui.

setlocal

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "ALVO=%STARTUP%\FileOrganizerAgent_autostart.bat"

if exist "%ALVO%" (
    del "%ALVO%"
    echo Removido: "%ALVO%"
) else (
    echo Nada instalado em "%ALVO%".
)

echo.
echo Isso so impede o proximo login de subir o agente sozinho. Se ele ja
echo estiver rodando agora, continua ate voce fechar a sessao ou encerrar
echo o processo manualmente pelo Gerenciador de Tarefas (procure "pythonw.exe"
echo - se houver mais de um na lista, confirme pela coluna "Caminho da imagem"
echo antes de encerrar, para nao derrubar outro programa Python seu por engano).

endlocal
