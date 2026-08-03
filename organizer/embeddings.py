"""Backend de embeddings — extra OPCIONAL da Fase 4.

**Estado: interface publicada, implementação pendente.** O contrato já é o final
(ARQUITETURA §13): `get_backend()` devolve `None` quando nenhuma dependência
opcional está instalada, sem nunca deixar vazar `ImportError` (RF-66).

`numpy`, `model2vec` e `sentence-transformers` são importados **dentro** das
classes, nunca no topo do módulo (RF-67).
"""

from __future__ import annotations

from typing import Protocol, Sequence

BACKEND_NENHUM = "none"
BACKEND_AUTO = "auto"
BACKEND_MODEL2VEC = "model2vec"
BACKEND_SENTENCE_TRANSFORMERS = "sentence-transformers"

DICA_INSTALACAO = (
    "Busca semantica desativada - instale: pip install -r requirements-semantic.txt"
)


class EmbeddingBackend(Protocol):
    """Protocolo comum a todas as implementações."""

    nome: str
    dim: int

    def encode(self, textos: Sequence[str]): ...


def _tentar_model2vec(cfg):
    """Fase 4. Importa `model2vec` só aqui dentro."""
    return None


def _tentar_sentence_transformers(cfg):
    """Fase 4. Importa `sentence_transformers` só aqui dentro."""
    return None


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
