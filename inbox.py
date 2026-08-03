"""Dashboard do `_Inbox` — Fase 5.

**Estado: não implementado nesta rodada.** A entrega atual cobre as Fases 0, 1
e 2. O plano de aprovação já sobrevive no banco (`operacoes` com estado
`aguardando_aprovacao`), que é o pré-requisito desta CLI.
"""

from __future__ import annotations

import sys

MENSAGEM = (
    "inbox.py faz parte da Fase 5 (notificacao, modo interativo e dashboard) "
    "e ainda nao foi implementado. Entrega atual: Fases 0, 1 e 2."
)


def main(argv: list[str] | None = None) -> int:
    print(MENSAGEM, file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
