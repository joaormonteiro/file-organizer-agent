"""Busca no índice: `python query.py "onde está a matriz curricular"`.

Sai com 0 quando encontra e 1 quando não encontra (RF-71). `--json` dá saída
programática. `rich` é importado **só aqui** — nunca no watcher.
"""

from __future__ import annotations

import argparse
import json
import sys

from organizer import config, db, embeddings, search


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Busca no índice do File Organizer Agent.")
    parser.add_argument("pergunta", nargs="+", help="pergunta em linguagem natural")
    parser.add_argument("--k", type=int, default=5, help="quantidade de resultados")
    parser.add_argument("--json", action="store_true", help="saída em JSON")
    parser.add_argument(
        "--sem-semantica", action="store_true", help="força só a busca léxica (FTS5)"
    )
    args = parser.parse_args(argv)

    pergunta = " ".join(args.pergunta)
    cfg = config.get_config()
    backend = None if args.sem_semantica else embeddings.get_backend(cfg)

    conn = db.abrir(cfg.db_path)
    try:
        resultados = search.buscar(conn, pergunta, args.k, cfg=cfg, backend=backend)
    finally:
        conn.close()

    if args.json:
        print(json.dumps([r.como_dicionario() for r in resultados], ensure_ascii=False, indent=2))
        return 0 if resultados else 1

    from rich.console import Console
    from rich.table import Table

    console = Console()
    if backend is None:
        console.print(f"[yellow]{embeddings.DICA_INSTALACAO}[/yellow]")

    if not resultados:
        console.print(f"[red]nada encontrado para:[/red] {pergunta}")
        return 1

    tabela = Table(title=f"Resultados para: {pergunta}")
    for coluna in ("score", "path", "tipo/subtipo", "indexado_em"):
        tabela.add_column(coluna, overflow="fold")
    for resultado in resultados:
        tabela.add_row(
            f"{resultado.score:.4f}",
            str(resultado.path),
            f"{resultado.tipo or '-'}/{resultado.subtipo or '-'}",
            resultado.indexado_em or "",
        )
    console.print(tabela)
    return 0


if __name__ == "__main__":
    sys.exit(main())
