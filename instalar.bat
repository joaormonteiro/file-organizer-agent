@echo off
rem ==========================================================================
rem  FileOrganizerAgent - registro de autostart (mesmo padrao do EyeAgent).
rem
rem  Mecanismo: um .bat dentro da pasta Inicializar do usuario. O Windows
rem  executa tudo que esta nessa pasta em todo logon - recurso nativo do SO.
rem  Sem chave de registro Run, sem servico, sem Agendador de Tarefas
rem  (schtasks /sc onlogon ja foi testado no EyeAgent e falhava calado) e
rem  sem PowerShell/COM (bloqueavel por antivirus). So echo do proprio cmd.
rem
rem  O caminho do pythonw.exe e do watcher.py e resolvido AGORA, na hora de
rem  gravar, via %~dp0 - fica fixo dentro do .bat gerado.
rem
rem  Diferenca para o EyeAgent: la e um .exe unico, nao valida nada. Aqui o
rem  watcher e Python, entao antes de gravar o autostart a gente confirma
rem  que a venv existe, o .env existe e a config carrega.
rem ==========================================================================

setlocal

set "PROJETO=%~dp0"
set "PROJETO=%PROJETO:~0,-1%"
cd /d "%PROJETO%"

rem --- 1. limpa mecanismos antigos (tarefa agendada / atalho), se existirem ---
schtasks /delete /tn "FileOrganizerAgent" /f >nul 2>&1
schtasks /delete /tn "FileOrganizer" /f >nul 2>&1
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\FileOrganizerAgent.lnk" >nul 2>&1

rem --- 2. localiza a venv (venv/ ou .venv/) ---
set "VENV=%PROJETO%\venv"
if not exist "%VENV%\Scripts\pythonw.exe" set "VENV=%PROJETO%\.venv"
set "PYTHONW=%VENV%\Scripts\pythonw.exe"
set "PYTHON=%VENV%\Scripts\python.exe"

if not exist "%PYTHONW%" (
    echo [ERRO] venv nao encontrada em "%PROJETO%\venv" nem "%PROJETO%\.venv".
    echo Rode primeiro: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    exit /b 1
)

if not exist "%PROJETO%\.env" (
    echo [ERRO] .env nao encontrado. Copie .env.example para .env e preencha antes de instalar.
    exit /b 1
)

echo Validando configuracao...
"%PYTHON%" -c "from organizer import config; config.montar(); print('config OK')"
if errorlevel 1 (
    echo.
    echo [ERRO] configuracao invalida - veja a mensagem acima e corrija o .env antes de instalar.
    exit /b 1
)

rem --- 3. grava o .bat de autostart com os caminhos absolutos ja resolvidos ---
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "ALVO=%STARTUP%\FileOrganizerAgent_autostart.bat"

> "%ALVO%" echo @echo off
>> "%ALVO%" echo start "" /min "%PYTHONW%" "%PROJETO%\watcher.py"

echo.
echo Instalado: "%ALVO%"
echo O agente sobe sozinho no proximo logon (sem janela, sem console).
echo.
echo Para subir agora sem reiniciar:
echo   start "" /min "%PYTHONW%" "%PROJETO%\watcher.py"
echo.
echo Para desinstalar: desinstalar.bat

endlocal
