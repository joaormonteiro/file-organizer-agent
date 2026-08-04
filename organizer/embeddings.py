"""Backend de embeddings — extra OPCIONAL da Fase 4 (ARQUITETURA §13).

⚠ DIVERGÊNCIA 4: `model2vec` no lugar de `sentence-transformers`. O motivo está
na arquitetura: `sentence-transformers` arrasta `torch` (~500 MB instalados,
2-5 s só para importar), o que contradiz o "carrega em <1 s ... morre" da spec, e
o `all-MiniLM-L6-v2` é treinado só em inglês — enquanto todas as perguntas de
exemplo da spec são em português. `model2vec` usa embeddings estáticos
destilados: wheel pura, sem torch, e o `potion-multilingual-128M` cobre PT-BR.

`numpy`, `model2vec` e `sentence_transformers` são importados **dentro** das
classes, nunca no topo do módulo (RF-67). Numa instalação limpa,
`get_backend()` devolve `None` e nenhum `ImportError` vaza (RF-66).

Requisitos cobertos: RF-66, RF-67, RF-68, RF-69.
"""

from __future__ import annotations

import struct
from typing import Protocol, Sequence

from organizer import log

BACKEND_NENHUM = "none"
BACKEND_AUTO = "auto"
BACKEND_MODEL2VEC = "model2vec"
BACKEND_SENTENCE_TRANSFORMERS = "sentence-transformers"

DICA_INSTALACAO = (
    "Busca semantica desativada - instale: pip install -r requirements-semantic.txt"
)

_logger = log.get_logger("embeddings")


class EmbeddingBackend(Protocol):
    """Protocolo comum a todas as implementações."""

    nome: str
    dim: int

    def encode(self, textos: Sequence[str]): ...


# --------------------------------------------------------------------------- #
# Serialização — float32 little-endian (contrato da spec)
# --------------------------------------------------------------------------- #


def para_blob(vetor) -> bytes:
    """Vetor → `bytes` float32 little-endian, do jeito que a spec pede (RF-69)."""
    valores = [float(x) for x in vetor]
    return struct.pack(f"<{len(valores)}f", *valores)


def de_blob(blob: bytes | None) -> list[float]:
    """`bytes` → lista de floats. Devolve `[]` para `NULL`."""
    if not blob:
        return []
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


# --------------------------------------------------------------------------- #
# Implementações
# --------------------------------------------------------------------------- #


class Model2VecBackend:
    """Embeddings estáticos destilados. Carrega em ~100 ms e não importa torch."""

    def __init__(self, nome_modelo: str):
        from model2vec import StaticModel  # importado só aqui (RF-67)

        self._modelo = StaticModel.from_pretrained(nome_modelo)
        self.nome = f"model2vec:{nome_modelo}"
        self.dim = int(self._modelo.dim)

    def encode(self, textos: Sequence[str]):
        import numpy as np

        if not textos:
            return np.zeros((0, self.dim), dtype="float32")
        return np.asarray(self._modelo.encode(list(textos)), dtype="float32")


class SentenceTransformerBackend:
    """Para quem já tem torch instalado e prefere o modelo da spec original."""

    def __init__(self, nome_modelo: str):
        from sentence_transformers import SentenceTransformer  # importado só aqui

        self._modelo = SentenceTransformer(nome_modelo)
        self.nome = f"sentence-transformers:{nome_modelo}"
        self.dim = int(self._modelo.get_sentence_embedding_dimension())

    def encode(self, textos: Sequence[str]):
        import numpy as np

        if not textos:
            return np.zeros((0, self.dim), dtype="float32")
        return np.asarray(self._modelo.encode(list(textos)), dtype="float32")


def _tentar(construtor, nome_modelo: str, rotulo: str):
    try:
        return construtor(nome_modelo)
    except Exception as exc:
        _logger.debug("backend %s indisponível: %s", rotulo, exc)
        return None


def _tentar_model2vec(cfg):
    return _tentar(Model2VecBackend, cfg.embedding_model, BACKEND_MODEL2VEC)


def _tentar_sentence_transformers(cfg):
    return _tentar(SentenceTransformerBackend, cfg.embedding_model, BACKEND_SENTENCE_TRANSFORMERS)


def get_backend(cfg) -> EmbeddingBackend | None:
    """Backend disponível, ou `None` numa instalação limpa (o caminho padrão)."""
    escolha = (cfg.embedding_backend or BACKEND_AUTO).lower()
    if escolha == BACKEND_NENHUM:
        return None
    if escolha == BACKEND_SENTENCE_TRANSFORMERS:
        return _tentar_sentence_transformers(cfg)
    if escolha == BACKEND_MODEL2VEC:
        return _tentar_model2vec(cfg)
    return _tentar_model2vec(cfg) or _tentar_sentence_transformers(cfg)


def texto_para_indexar(nome_atual, nome_orig, tipo, subtipo, amostra) -> str:
    """O que vira vetor: nome, categoria e o trecho extraído, nesta ordem."""
    partes = [nome_atual, nome_orig, tipo, subtipo, amostra]
    return " ".join(str(p) for p in partes if p).strip()
