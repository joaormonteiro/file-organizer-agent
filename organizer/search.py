"""Busca híbrida: FTS5 sempre, embeddings quando houver, rerank opcional.

Três camadas (ARQUITETURA §13), cada uma degradando sozinha:

- **T1 léxica** — `sqlite3` stdlib + FTS5: sempre ativa;
- **T2 semântica** — `model2vec` (extra opcional), via `EMBEDDING_BACKEND`;
- **T3 rerank** — `ollama` CLI, via `SEARCH_RERANK_LLM=1`.

Sem o extra de embeddings a busca continua respondendo por BM25 e `query.py`
imprime uma dica de instalação — nenhum `ImportError` vaza (RF-63, RF-68).

Requisitos cobertos: RF-63, RF-64, RF-65, RF-68, RF-69, RF-70, RF-71, RF-72.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from organizer import db, embeddings

#: Constante da Reciprocal Rank Fusion (ARQUITETURA §13).
K_RRF = 60
#: Candidatos considerados em cada ranking antes da fusão (RF-65).
TOP_CANDIDATOS = 20
#: Acima disto o cosseno global degrada para os melhores do FTS (marco de escala).
LIMITE_COSSENO_GLOBAL = 50_000

VIA_FTS = "fts"
VIA_COSSENO = "cosseno"
VIA_RRF = "rrf"

_TOKEN = re.compile(r"[0-9A-Za-zÀ-ÿ]+")


@dataclass(frozen=True)
class Resultado:
    """Uma linha do resultado da busca."""

    path: Path
    score: float
    tipo: str | None
    subtipo: str | None
    indexado_em: str | None
    via: str = VIA_FTS

    def como_dicionario(self) -> dict:
        return {
            "path": str(self.path),
            "score": round(self.score, 6),
            "tipo": self.tipo,
            "subtipo": self.subtipo,
            "indexado_em": self.indexado_em,
            "via": self.via,
        }


# --------------------------------------------------------------------------- #
# T1 — léxica (BM25)
# --------------------------------------------------------------------------- #


def preparar_consulta(pergunta: str) -> str:
    """Transforma a pergunta em uma expressão MATCH segura para o FTS5.

    Cada token vira uma frase entre aspas, unidas por `OR`: nada do que o
    usuário digitar pode virar sintaxe do FTS5 por acidente.
    """
    termos = _TOKEN.findall(pergunta or "")
    return " OR ".join(f'"{t}"' for t in termos)


def _resultado(linha, score: float, via: str) -> Resultado:
    return Resultado(
        path=Path(linha["path"]),
        score=score,
        tipo=linha["tipo"],
        subtipo=linha["subtipo"],
        indexado_em=linha["indexado_em"],
        via=via,
    )


def buscar_lexico(conn, pergunta: str, k: int = TOP_CANDIDATOS) -> list[Resultado]:
    """Camada T1: BM25 puro, sempre disponível (RF-63, RF-65).

    O `bm25()` do SQLite devolve valores negativos, mais negativo = melhor;
    invertemos o sinal para que "maior é melhor" valha em todo o módulo.
    """
    consulta = preparar_consulta(pergunta)
    if not consulta:
        return []
    return [_resultado(l, -float(l["score"]), VIA_FTS) for l in db.buscar_fts(conn, consulta, k)]


# --------------------------------------------------------------------------- #
# T2 — semântica (cosseno global)
# --------------------------------------------------------------------------- #


def buscar_semantico(conn, pergunta: str, backend, k: int = TOP_CANDIDATOS) -> list[Resultado]:
    """Cosseno da pergunta contra **todas** as linhas do mesmo modelo (RF-69).

    Restringir aos candidatos do FTS anularia o propósito da busca semântica,
    então o produto é feito sobre a matriz inteira. Vetores de outro modelo
    ficam de fora: nunca se compara embedding de modelos diferentes.
    """
    if backend is None or not (pergunta or "").strip():
        return []
    import numpy as np

    linhas = db.linhas_com_embedding(conn, backend.nome, LIMITE_COSSENO_GLOBAL)
    if not linhas:
        return []

    matriz = np.array([embeddings.de_blob(l["embedding"]) for l in linhas], dtype="float32")
    if matriz.ndim != 2 or matriz.shape[1] != backend.dim:
        return []

    vetor = np.asarray(backend.encode([pergunta])[0], dtype="float32")
    normas = np.linalg.norm(matriz, axis=1) * float(np.linalg.norm(vetor))
    normas[normas == 0] = 1e-9
    similaridades = (matriz @ vetor) / normas

    melhores = np.argsort(-similaridades)[:k]
    return [_resultado(linhas[i], float(similaridades[i]), VIA_COSSENO) for i in melhores]


# --------------------------------------------------------------------------- #
# Fusão
# --------------------------------------------------------------------------- #


def rrf(rankings: list[list[str]], k: int = K_RRF) -> dict[str, float]:
    """Reciprocal Rank Fusion: `score = soma de 1/(k + rank)` (RF-70)."""
    fundido: dict[str, float] = {}
    for ranking in rankings:
        for posicao, chave in enumerate(ranking, start=1):
            fundido[chave] = fundido.get(chave, 0.0) + 1.0 / (k + posicao)
    return fundido


def fundir(lexicos: list[Resultado], semanticos: list[Resultado]) -> list[Resultado]:
    """Aplica RRF sobre os dois rankings e devolve a lista reordenada."""
    if not semanticos:
        return list(lexicos)
    if not lexicos:
        return list(semanticos)

    por_path: dict[str, Resultado] = {}
    for resultado in list(semanticos) + list(lexicos):
        por_path.setdefault(str(resultado.path), resultado)

    scores = rrf([[str(r.path) for r in lexicos], [str(r.path) for r in semanticos]])
    ordenados = sorted(scores.items(), key=lambda par: -par[1])
    return [replace(por_path[chave], score=valor, via=VIA_RRF) for chave, valor in ordenados]


# --------------------------------------------------------------------------- #
# T3 — rerank opcional por LLM
# --------------------------------------------------------------------------- #


def rerankear(resultados: list[Resultado], pergunta: str, cfg, conn=None) -> list[Resultado]:
    """Pede ao Ollama qual candidato responde melhor. Falhou? Segue igual (RF-72)."""
    if not cfg.search_rerank_llm or len(resultados) < 2:
        return resultados
    from organizer import llm

    if not llm.disponivel(cfg, conn):
        return resultados

    candidatos = "\n".join(
        f"{i}. {r.path.name} ({r.tipo}/{r.subtipo}) em {r.path.parent}"
        for i, r in enumerate(resultados, start=1)
    )
    prompt = (
        "Escolha qual arquivo responde melhor a pergunta do usuario.\n\n"
        f"pergunta: {pergunta}\n\ncandidatos:\n{candidatos}\n\n"
        'Responda SOMENTE com o JSON {"melhor": <numero do candidato>}.'
    )
    try:
        dados = llm.parsear(llm.executar(cfg, prompt, conn))
    except Exception:
        return resultados
    if not dados:
        return resultados
    try:
        escolhido = int(dados.get("melhor", 0))
    except (TypeError, ValueError):
        return resultados
    if not 1 <= escolhido <= len(resultados):
        return resultados
    promovido = resultados[escolhido - 1]
    return [promovido] + [r for r in resultados if r is not promovido]


# --------------------------------------------------------------------------- #
# Entrada
# --------------------------------------------------------------------------- #


def buscar(conn, pergunta: str, k: int = 5, cfg=None, backend=None) -> list[Resultado]:
    """Busca completa: T1 sempre, T2 se houver backend, T3 se ligado."""
    lexicos = buscar_lexico(conn, pergunta, TOP_CANDIDATOS)
    semanticos = buscar_semantico(conn, pergunta, backend, TOP_CANDIDATOS) if backend else []
    fundidos = fundir(lexicos, semanticos)[:k]
    if cfg is not None:
        fundidos = rerankear(fundidos, pergunta, cfg, conn)
    return fundidos
