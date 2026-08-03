"""Busca híbrida FTS5 + embeddings — Fase 4.

**Estado: camada léxica funcional, fusão e CLI pendentes.** A tabela
`arquivos_fts` já é criada e mantida em sincronia por triggers desde a migration
v2 (ver `db.MIGRATIONS`), então a busca léxica já responde. A fusão RRF, o
rerank por LLM e a saída `rich` são Fase 4.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from organizer import db

#: Constante da Reciprocal Rank Fusion (ARQUITETURA §13).
K_RRF = 60
#: Candidatos considerados em cada ranking antes da fusão.
TOP_CANDIDATOS = 20

_TOKEN = re.compile(r"[0-9A-Za-zÀ-ÿ]+")


@dataclass(frozen=True)
class Resultado:
    """Uma linha do resultado da busca."""

    path: Path
    score: float
    tipo: str | None
    subtipo: str | None
    indexado_em: str | None


def preparar_consulta(pergunta: str) -> str:
    """Transforma a pergunta em uma expressão MATCH segura para o FTS5."""
    termos = _TOKEN.findall(pergunta or "")
    return " OR ".join(f'"{t}"' for t in termos)


def buscar_lexico(conn, pergunta: str, k: int = TOP_CANDIDATOS) -> list[Resultado]:
    """Camada T1: BM25 puro, sempre disponível (RF-63, RF-65)."""
    consulta = preparar_consulta(pergunta)
    if not consulta:
        return []
    linhas = db.buscar_fts(conn, consulta, k)
    return [
        Resultado(
            path=Path(linha["path"]),
            score=float(linha["score"]),
            tipo=linha["tipo"],
            subtipo=linha["subtipo"],
            indexado_em=linha["indexado_em"],
        )
        for linha in linhas
    ]


def rrf(rankings: list[list[str]], k: int = K_RRF) -> dict[str, float]:
    """Reciprocal Rank Fusion: `score = soma de 1/(k + rank)` (RF-70)."""
    fundido: dict[str, float] = {}
    for ranking in rankings:
        for posicao, chave in enumerate(ranking, start=1):
            fundido[chave] = fundido.get(chave, 0.0) + 1.0 / (k + posicao)
    return fundido


def buscar(conn, pergunta: str, k: int = 5) -> list[Resultado]:
    """Busca completa. Hoje é só T1; T2/T3 entram na Fase 4."""
    return buscar_lexico(conn, pergunta, TOP_CANDIDATOS)[:k]
