"""Entrypoint do processo permanente: `python watcher.py`.

Fino de propósito: toda a lógica vive em `organizer.watch`, que é o módulo
auditado pela RNF-03 (não pode importar dependências pesadas).
"""

from __future__ import annotations

import sys

from organizer.watch import main

if __name__ == "__main__":
    sys.exit(main())
