"""Heurística determinística de "nome genérico" (ARQUITETURA §8).

Nome genérico é condição *necessária mas não suficiente* para acordar o LLM, e
é um dos ajustes de confiança da classificação por extensão.

Requisitos cobertos: RF-18, RF-19.
"""

from __future__ import annotations

import re
from pathlib import Path

from organizer.paths import fold_ascii

# --------------------------------------------------------------------------- #
# Normalização
# --------------------------------------------------------------------------- #

_SEPARADORES = re.compile(r"[_.+]|%20")
_ESPACOS = re.compile(r"\s+")
_SUFIXO_COPIA = re.compile(r"\s*\(\d{1,3}\)$")
_TOKENS_ALFA = re.compile(r"[a-z]{3,}")
_HEX = re.compile(r"^[0-9a-f]{16,}$")
_UUID = re.compile(
    r"^[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}$"
)


def normalizar(nome: str) -> tuple[str, bool]:
    """Devolve `(stem normalizado, teve_sufixo_copia)`.

    Remove a extensão, faz fold para ASCII, minúsculas, troca `_ . + %20` por
    espaço, colapsa espaços e retira o sufixo de cópia `(n)`.
    """
    stem = Path(nome).stem
    texto = fold_ascii(stem).lower()
    texto = _SEPARADORES.sub(" ", texto)
    texto = _ESPACOS.sub(" ", texto).strip()
    sem_copia = _SUFIXO_COPIA.sub("", texto).strip()
    return sem_copia, sem_copia != texto


# --------------------------------------------------------------------------- #
# Regras G1..G5
# --------------------------------------------------------------------------- #

#: G1 — o stem inteiro é uma dessas palavras.
STOPWORDS: frozenset[str] = frozenset(
    {
        "document",
        "documento",
        "documents",
        "documentos",
        "download",
        "downloads",
        "arquivo",
        "arquivos",
        "untitled",
        "sem titulo",
        "novo documento",
        "new document",
        "file",
        "scan",
        "scanned",
        "image",
        "imagem",
        "foto",
        "photo",
        "copia",
        "copy",
        "final",
        "teste",
        "test",
        "output",
        "print",
        "captura de tela",
        "screenshot",
        "novo",
        "new",
        "temp",
        "tmp",
    }
)

#: G2 — prefixo típico de câmera/scanner/navegador seguido de número opcional.
_G2 = re.compile(
    r"^(img|image|imagem|foto|photo|dsc|dscn|pxl|screenshot|captura de tela|"
    r"scan|scanned|doc|documento|download|file|arquivo|untitled|new|novo)"
    r"[ -]?\d*$"
)

MOTIVO_NAO_GENERICO = ""


def is_generic(nome: str) -> tuple[bool, str]:
    """Diz se o nome é genérico e por qual regra (`G1`..`G5`).

    Regra negativa explícita: o sufixo de cópia `(n)` **sozinho nunca** torna um
    nome genérico — `holerite_..._2026-07 (1).pdf` é descritivo.
    """
    stem, _teve_sufixo_copia = normalizar(nome)

    if not stem:
        return True, "G4"
    if stem in STOPWORDS:
        return True, "G1"
    if _G2.match(stem):
        return True, "G2"

    compacto = stem.replace(" ", "").replace("-", "")
    if compacto.isdigit() and len(compacto) >= 6:
        return True, "G3"
    if _HEX.match(compacto) or _UUID.match(compacto):
        return True, "G3"
    if len(stem) <= 3:
        return True, "G4"
    if len(_TOKENS_ALFA.findall(stem)) < 2:
        return True, "G5"
    return False, MOTIVO_NAO_GENERICO


def slugify(texto: str) -> str:
    """Versão amigável de um texto livre para uso em nome de arquivo."""
    base = fold_ascii(texto).lower()
    base = re.sub(r"[^a-z0-9]+", "-", base)
    return base.strip("-")


def stem_sem_sufixo_copia(nome: str) -> str:
    """Remove o `(n)` do nome preservando maiúsculas e acentos do original.

    Usado para montar o nome final: o sufixo de cópia é ruído do navegador, e a
    política de colisão do `move` usa `-N`, nunca ` (N)` (RF-23).
    """
    stem = Path(nome).stem
    return _SUFIXO_COPIA.sub("", stem).strip() or stem
