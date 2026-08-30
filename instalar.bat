@echo off
rem Registra o watcher para subir sozinho no login do Windows.
rem
rem Mesmo mecanismo validado no EyeAgent (vault eyeconnect-vault, sessao
rem 2026-07-22): nada de Agendador de Tarefas (schtasks /sc onlogon existia
rem mas "nunca rodava" silenciosamente) nem PowerShell/COM (bloqueado por
rem antivirus/politica numa das maquinas testadas). So um .bat puro escrito
rem na pasta Inicializar via echo do proprio cmd, sem dependencia externa.

setlocal

set "PROJETO=%~dp0"
set "PROJETO=%PROJETO:~0,-1%"

set "VENV=%PROJETO%\venv"
if not exist "%VENV%\Scripts\pythonw.exe" set "VENV=%PROJETO%\.venv"
set "PYTHONW=%VENV%\Scripts\pythonw.exe"
set "PYTHON=%VENV%\Scripts\python.exe"

if not exist "%PYTHONW%" (
    echo [ERRO] venv nao encontrada em "%PROJETO%\venv" nem "%PROJETO%\.venv".
    echo Rode primeiro: python -m venv venv ^&^& venv\Scripts\pip install -r requirements.txt
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

set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "ALVO=%STARTUP%\FileOrganizerAgent_autostart.bat"

> "%ALVO%" echo @echo off
>> "%ALVO%" echo cd /d "%PROJETO%"
>> "%ALVO%" echo start "" /min "%PYTHONW%" watcher.py

echo.
echo Instalado: "%ALVO%"
echo O agente vai subir sozinho no proximo login (sem janela, sem console).
echo.
echo Para subir agora sem reiniciar a maquina:
echo   start "" /min "%PYTHONW%" "%PROJETO%\watcher.py"
echo.
echo Para desinstalar depois: desinstalar.bat

endlocal
