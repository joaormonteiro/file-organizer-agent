"""Entrypoint do processo permanente: `python watcher.py`.

Fino de propósito: toda a lógica vive em `organizer.watch`, que é o módulo
auditado pela RNF-03 (não pode importar dependências pesadas).

Autostart real (pasta Inicializar) roda `pythonw.exe` sem console: uma
exceção antes de `organizer.log` estar configurado (import, `config.get_config()`,
`Watcher.__init__`) hoje desaparecia sem deixar rastro. `_registrar_crash` grava
ao lado deste arquivo, sem depender de `.env`, `%LOCALAPPDATA%` nem de nenhuma
raiz redirecionada pro OneDrive, então funciona mesmo se a própria config for a
causa da falha.
"""

from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime
from pathlib import Path

_RAIZ = Path(__file__).resolve().parent
_CRASH_LOG = _RAIZ / "crash.log"
_PID_FILE = _RAIZ / "watcher.pid"


def _registrar_crash(exc: BaseException) -> None:
    try:
        with _CRASH_LOG.open("a", encoding="utf-8") as arquivo:
            arquivo.write(f"\n--- {datetime.now().isoformat(timespec='seconds')} ---\n")
            traceback.print_exception(type(exc), exc, exc.__traceback__, file=arquivo)
    except Exception:
        pass  # nem isso pode derrubar o processo


def _gravar_pid() -> None:
    """`watcher.pid` ao lado deste arquivo: e como `supervisor.py` confirma que
    o processo vivo com este PID e mesmo este watcher, sem depender do `wmic`
    (removido em builds recentes do Windows) nem da linha de comando do processo.
    """
    try:
        _PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    except OSError:
        pass


if __name__ == "__main__":
    _gravar_pid()
    try:
        from organizer.watch import main

        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as exc:  # pragma: no cover - rede de segurança do autostart
        _registrar_crash(exc)
        raise
