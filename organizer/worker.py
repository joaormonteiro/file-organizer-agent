"""Processo filho efêmero: `python -m organizer.worker <path> [--motivo=...]`.

Nasce, processa exatamente um arquivo, e morre. Nenhuma biblioteca pesada fica
residente: quando este processo termina, a RAM volta ao zero (RF-28).

Códigos de saída: 0 ok, 2 adiado, 3 erro.
"""

from __future__ import annotations

import sys
from pathlib import Path

from organizer import config, db, ingest, log
from organizer.queue import Motivo

USO = "uso: python -m organizer.worker <caminho> [--motivo=<motivo>]"


def parse_argv(argv: list[str]) -> tuple[str | None, Motivo | None]:
    """Extrai o caminho e o motivo de origem da linha de comando."""
    caminho: str | None = None
    motivo: Motivo | None = None
    for arg in argv:
        if arg.startswith("--motivo="):
            bruto = arg.split("=", 1)[1]
            try:
                motivo = Motivo(bruto)
            except ValueError:
                motivo = None
        elif not arg.startswith("--") and caminho is None:
            caminho = arg
    return caminho, motivo


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    caminho, motivo = parse_argv(argv)
    if not caminho:
        print(USO, file=sys.stderr)
        return ingest.EXIT_ERRO

    cfg = config.get_config()
    log.configurar(cfg.log_dir)
    conn = db.abrir(cfg.db_path)
    try:
        return ingest.processar(Path(caminho), cfg=cfg, conn=conn, motivo_origem=motivo)
    finally:
        # o lock lógico é sempre liberado, aconteça o que acontecer
        try:
            db.liberar_lock(conn, str(Path(caminho)))
        finally:
            conn.close()


if __name__ == "__main__":  # pragma: no cover - entrypoint
    sys.exit(main())
