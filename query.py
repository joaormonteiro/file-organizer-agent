"""Busca no índice: `python query.py "onde está a matriz curricular"`.

Fase 4 parcial: a camada léxica (FTS5) já responde, porque a tabela
`arquivos_fts` é criada e mantida em sincronia por triggers desde a migration
v2. A fusão com embeddings e o rerank por LLM são o restante da Fase 4.

`rich` só é importado aqui — nunca no watcher.
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
    args = parser.parse_args(argv)

    pergunta = " ".join(args.pergunta)
    cfg = config.get_config()
    conn = db.abrir(cfg.db_path)
    try:
        resultados = search.buscar(conn, pergunta, args.k)
    finally:
        conn.close()

    if args.json:
        print(
            json.dumps(
                [
                    {
                        "path": str(r.path),
                        "score": r.score,
                        "tipo": r.tipo,
                        "subtipo": r.subtipo,
                        "indexado_em": r.indexado_em,
                    }
                    for r in resultados
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        from rich.console import Console
        from rich.table import Table

        console = Console()
        if embeddings.get_backend(cfg) is None:
            console.print(f"[yellow]{embeddings.DICA_INSTALACAO}[/yellow]")
        tabela = Table(title=f"Resultados para: {pergunta}")
        for coluna in ("score", "path", "tipo/subtipo", "indexado_em"):
            tabela.add_column(coluna)
        for r in resultados:
            tabela.add_row(f"{r.score:.3f}", str(r.path), f"{r.tipo}/{r.subtipo}", r.indexado_em or "")
        console.print(tabela)

    return 0 if resultados else 1


if __name__ == "__main__":
    sys.exit(main())
