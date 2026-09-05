"""Supervisor externo do watcher, disparado por Tarefa Agendada a cada N min.

Deliberadamente nao importa nada de `organizer`: se o bug que matou o watcher
for no proprio pacote, o supervisor nao pode herdar o mesmo problema. So usa
`subprocess`/`Path` da stdlib pra checar processos do Windows e a idade do
log.

Curto de proposito (roda e sai em menos de 2s): e isso que o protege do
"Application Hang" do Windows que matou o watcher em 2026-09-04 - esse
detector so pega processo com janela que fica *vivo* sem responder por
varios segundos seguidos; um processo que termina sozinho rapido nunca fica
tempo suficiente no ar pra ser amostrado como travado.

Casos cobertos:
  1. processo do watcher nao existe -> sobe.
  2. processo existe e o log parado ha um tempo plausivel (entre
     TRAVADO_SEGUNDOS e IDADE_SUSPEITA_SEGUNDOS) -> trata como travado/zumbi
     (deadlock, ou o watcher perdeu o watchdog sem cair): mata e sobe de novo.
  3. processo existe mas o log parece parado ha um tempo absurdo (mais que
     IDADE_SUSPEITA_SEGUNDOS) -> so avisa, nunca mata. Ambiente sob Tarefa
     Agendada as vezes enxerga um caminho ou timestamp diferente do que um
     processo interativo ve para o mesmo `%LOCALAPPDATA%\\FileOrganizerAgent`
     (reproduzido em 2026-09-04); tratar isso como hang real derrubaria um
     watcher saudavel.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
PYTHONW = RAIZ / ".venv" / "Scripts" / "pythonw.exe"
WATCHER = RAIZ / "watcher.py"
PID_FILE = RAIZ / "watcher.pid"
#: Mesmo fallback de organizer/config.py: sob Tarefa Agendada, %LOCALAPPDATA%
#: pode nao vir no bloco de ambiente do processo (a Task Scheduler monta o
#: ambiente da conta, e essa variavel e "especial", resolvida por API de
#: pastas conhecidas, nem sempre presente como var literal). Sem isso,
#: `Path("")` vira `.` e o log resolvia pra um caminho relativo qualquer -
#: foi assim que uma versao anterior deste script leu idade de log errada
#: (18086 min) e matou um watcher saudavel.
_LOCALAPPDATA = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
LOG = Path(_LOCALAPPDATA) / "FileOrganizerAgent" / "logs" / "organizer.log"
SUPERVISOR_LOG = RAIZ / "supervisor.log"

#: Se o log nao muda ha mais que isso com o processo vivo, trata como travado.
#: Folga generosa de propósito: o loop idle roda a cada IDLE_LOOP_SECONDS (600s
#: por padrao) e pode ficar silencioso por ciclos inteiros se nao houver
#: arquivo novo nem pendente vencido - isso e normal, nao e trava.
TRAVADO_SEGUNDOS = 30 * 60

#: Teto de sanidade: uma idade maior que isso e sinal mais forte de que o
#: caminho do log foi resolvido errado (ambiente distinto sob Task Scheduler)
#: do que de um hang real. Acima disso so avisa, nunca mata - matar por
#: engano um watcher saudavel e pior que deixar um hang real esperar mais
#: um ciclo de 5 min.
IDADE_SUSPEITA_SEGUNDOS = 6 * 60 * 60

#: Poda o supervisor.log pra nao crescer pra sempre (ele nao usa RotatingFileHandler).
LIMITE_LINHAS_LOG = 2000


def _log(msg: str) -> None:
    linha = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n"
    try:
        with SUPERVISOR_LOG.open("a", encoding="utf-8") as f:
            f.write(linha)
    except OSError:
        pass


def _podar_log() -> None:
    try:
        linhas = SUPERVISOR_LOG.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError:
        return
    if len(linhas) > LIMITE_LINHAS_LOG:
        SUPERVISOR_LOG.write_text("".join(linhas[-LIMITE_LINHAS_LOG:]), encoding="utf-8")


def pid_do_watcher() -> int | None:
    """Le `watcher.pid` (gravado pelo proprio watcher.py no boot) e confirma
    com `tasklist` que esse PID esta vivo e e mesmo um python*.exe - cobre o
    caso raro de reaproveitamento de PID por outro processo depois que o
    watcher morreu. `wmic` foi removido em builds recentes do Windows (e foi
    exatamente essa ausencia que fez uma versao anterior deste script subir
    um watcher duplicado por engano); `tasklist`/`taskkill` continuam nativos.
    """
    try:
        pid = int(PID_FILE.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None

    try:
        saida = subprocess.check_output(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
    except Exception as exc:
        _log(f"falha ao consultar tasklist (assumindo vivo por seguranca): {exc}")
        return pid  # nao arrisca duplicar o watcher por causa de uma falha de I/O

    primeira_coluna = saida.split(",", 1)[0].strip('"').lower()
    if "python" in primeira_coluna:
        return pid
    return None


def matar(pid: int) -> None:
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/F"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )


def subir() -> None:
    if not PYTHONW.exists():
        _log(f"ERRO: venv nao encontrada em {PYTHONW}")
        return
    subprocess.Popen(
        [str(PYTHONW), str(WATCHER)],
        cwd=str(RAIZ),
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    _log("watcher iniciado")


def main() -> int:
    _podar_log()
    pid = pid_do_watcher()

    if pid is None:
        _log("watcher nao esta rodando, subindo")
        subir()
        return 0

    if not LOG.exists():
        _log(f"ok, watcher vivo (pid={pid}); log ainda nao existe em {LOG}")
        return 0

    idade = time.time() - LOG.stat().st_mtime
    if idade > IDADE_SUSPEITA_SEGUNDOS:
        _log(
            f"AVISO: log em {LOG} parece parado ha {idade / 3600:.1f}h, o que e "
            "suspeito demais pra ser um hang real - mais provavel e o caminho "
            "ter resolvido errado neste ambiente. Nao vou matar o watcher por "
            "isso; so avisando."
        )
    elif idade > TRAVADO_SEGUNDOS:
        _log(
            f"watcher vivo (pid={pid}) mas log parado ha {int(idade / 60)} min, "
            "tratando como travado: reiniciando"
        )
        matar(pid)
        time.sleep(5)  # da tempo do Windows liberar os arquivos WAL do processo morto
        subir()
        return 0
    else:
        _log(f"ok, watcher vivo (pid={pid}), log ha {int(idade)}s")

    return 0


if __name__ == "__main__":
    sys.exit(main())
