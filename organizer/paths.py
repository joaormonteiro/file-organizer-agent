"""Regras de filesystem do Windows: sanitização, limite de caminho, hashes.

Este é o único módulo que conhece os caracteres proibidos, os nomes de dispositivo
reservados e o limite histórico de 260 caracteres do Win32. Praticamente puro:
só toca em disco em `sha256_arquivo` e em `resolver_colisao`.

Requisitos cobertos: RF-20, RF-21, RF-03 (via `assert_disjoint`), RNF-19.
"""

from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from pathlib import Path, PurePath

# --------------------------------------------------------------------------- #
# Constantes
# --------------------------------------------------------------------------- #

#: Teto de caracteres do caminho final. O limite do Win32 é 260; deixamos 10 de
#: folga para o sufixo de colisão (`-999`) que `move.py` pode acrescentar.
MAX_PATH = 250

#: Tamanho máximo do "stem" (nome sem extensão) após a sanitização.
MAX_STEM = 80

#: Caracteres que o Windows recusa em nomes de arquivo, mais os controles.
_PROIBIDOS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Nomes de dispositivo reservados pelo Windows (não podem ser nome de arquivo).
RESERVADOS = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + [f"COM{i}" for i in range(1, 10)]
    + [f"LPT{i}" for i in range(1, 10)]
)

#: Substituto quando a sanitização não deixa nada aproveitável.
FALLBACK_STEM = "arquivo"

_ESPACOS = re.compile(r"\s+")
_HIFENS = re.compile(r"-{2,}")


# --------------------------------------------------------------------------- #
# Sanitização de nomes
# --------------------------------------------------------------------------- #


def fold_ascii(texto: str) -> str:
    """Reduz o texto a ASCII puro preservando a leitura (NFKD + descarte de acentos)."""
    decomposto = unicodedata.normalize("NFKD", texto)
    return decomposto.encode("ascii", "ignore").decode("ascii")


def sanitize_stem(texto: str) -> str:
    """Converte um texto arbitrário num "stem" seguro para o Windows.

    Ordem das transformações (RF-20):
    1. fold NFKD para ASCII;
    2. remoção dos caracteres proibidos e dos controles 0x00-0x1F;
    3. remoção de pontos e espaços à direita;
    4. lowercase;
    5. espaços viram hífen, hífens repetidos colapsam;
    6. corte em ``MAX_STEM`` caracteres;
    7. nomes de dispositivo reservados ganham prefixo ``_``;
    8. resultado vazio vira ``arquivo``.
    """
    s = fold_ascii(texto)
    s = _PROIBIDOS.sub("", s)
    s = s.strip()
    s = s.rstrip(". ")
    s = s.lower()
    s = _ESPACOS.sub("-", s)
    s = _HIFENS.sub("-", s)
    s = s.strip("-")
    s = s[:MAX_STEM]
    s = s.rstrip("-. ")
    if not s:
        return FALLBACK_STEM
    if s.upper() in RESERVADOS:
        return "_" + s
    return s


def sanitize_ext(ext: str) -> str:
    """Normaliza a extensão (mantém o ponto, lowercase, tira lixo)."""
    if not ext:
        return ""
    limpa = _PROIBIDOS.sub("", fold_ascii(ext)).lower().strip()
    if not limpa:
        return ""
    return limpa if limpa.startswith(".") else "." + limpa


# --------------------------------------------------------------------------- #
# Limite de caminho
# --------------------------------------------------------------------------- #


def ensure_max_path(destino: Path) -> Path | None:
    """Garante que ``destino`` cabe em ``MAX_PATH`` caracteres.

    Se não couber, trunca o stem preservando a extensão e anexa ``-`` mais os 8
    primeiros hexadecimais do sha1 do stem original (determinístico e rastreável
    via ``arquivos.nome_orig``). Devolve ``None`` quando nem assim cabe — nesse
    caso o chamador manda o arquivo para o ``_Inbox`` (RF-21).
    """
    destino = Path(destino)
    if len(str(destino)) <= MAX_PATH:
        return destino

    pasta = destino.parent
    ext = destino.suffix
    stem = destino.stem
    marca = "-" + hashlib.sha1(stem.encode("utf-8", "replace")).hexdigest()[:8]

    # +1 do separador de diretório
    disponivel = MAX_PATH - len(str(pasta)) - 1 - len(ext) - len(marca)
    if disponivel < 1:
        return None

    novo = stem[:disponivel].rstrip("-. ")
    if not novo:
        novo = FALLBACK_STEM[:disponivel]
    candidato = pasta / f"{novo}{marca}{ext}"
    if len(str(candidato)) > MAX_PATH:  # pragma: no cover - defensivo, inalcançável
        return None
    return candidato


def long_path(p: Path | str) -> str:
    """Prefixa o caminho com o marcador Win32 de caminho longo, quando aplicável."""
    texto = str(p)
    if os.name != "nt":
        return texto
    if texto.startswith("\\\\?\\"):
        return texto
    if texto.startswith("\\\\"):
        return "\\\\?\\UNC\\" + texto.lstrip("\\")
    return "\\\\?\\" + texto


# --------------------------------------------------------------------------- #
# Relações entre caminhos
# --------------------------------------------------------------------------- #


def normalizar(p: Path | str) -> Path:
    """Forma canônica usada em todas as comparações de caminho."""
    return Path(os.path.normcase(os.path.abspath(str(p))))


def is_subpath(filho: Path | str, pai: Path | str) -> bool:
    """Diz se ``filho`` está dentro de ``pai`` (ou é o próprio ``pai``)."""
    f = normalizar(filho)
    p = normalizar(pai)
    if f == p:
        return True
    return p in f.parents


def volume(p: Path | str) -> str:
    """Devolve o volume (letra de unidade ou share UNC) em forma comparável."""
    return os.path.splitdrive(str(normalizar(p)))[0].lower()


def mesmo_volume(a: Path | str, b: Path | str) -> bool:
    """Diz se dois caminhos vivem no mesmo volume (pré-requisito do os.replace atômico)."""
    return volume(a) == volume(b)


class CaminhosSobrepostosError(ValueError):
    """Duas raízes de configuração se sobrepõem — recusar iniciar (RF-03)."""


def assert_disjoint(*caminhos: Path | str) -> None:
    """Falha se qualquer par de caminhos for igual ou um contiver o outro.

    É a checagem que impede a pasta de destino de ficar dentro da pasta vigiada
    (loop infinito) e o banco de ser gravado dentro da pasta vigiada.
    """
    itens = [Path(c) for c in caminhos if c is not None]
    for i, a in enumerate(itens):
        for b in itens[i + 1 :]:
            if normalizar(a) == normalizar(b):
                raise CaminhosSobrepostosError(f"caminhos idênticos: {a} e {b}")
            if is_subpath(a, b) or is_subpath(b, a):
                raise CaminhosSobrepostosError(f"caminhos sobrepostos: {a} e {b}")


# --------------------------------------------------------------------------- #
# Hash
# --------------------------------------------------------------------------- #

#: Acima disto, o sha256 é calculado só sobre as pontas (ARQUITETURA §11).
LIMITE_HASH_COMPLETO = 256 * 1024 * 1024
_PONTA = 8 * 1024 * 1024


def sha256_arquivo(p: Path | str, completo: bool = False) -> str:
    """sha256 do arquivo.

    Por padrão, acima de `LIMITE_HASH_COMPLETO` usa tamanho + as duas pontas:
    é barato e suficiente para **detectar duplicata**, onde um falso negativo
    apenas cria um arquivo a mais no `_Inbox`.

    `completo=True` força a leitura integral e é **obrigatório** em qualquer
    caminho que preceda a remoção do arquivo original do usuário (move entre
    volumes): ali um falso positivo apagaria o original, e corrupção no miolo
    de um arquivo grande passaria despercebida pelo hash das pontas.
    """
    caminho = Path(p)
    tamanho = caminho.stat().st_size
    h = hashlib.sha256()
    with open(caminho, "rb") as fh:
        if completo or tamanho <= LIMITE_HASH_COMPLETO:
            for bloco in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(bloco)
        else:
            h.update(str(tamanho).encode("ascii"))
            h.update(fh.read(_PONTA))
            fh.seek(-_PONTA, os.SEEK_END)
            h.update(fh.read(_PONTA))
    return h.hexdigest()


def mesmo_conteudo(a: Path | str, b: Path | str) -> bool:
    """Duplicata = mesmo tamanho E mesmo sha256 (ARQUITETURA §11, caso 2)."""
    pa, pb = Path(a), Path(b)
    try:
        if pa.stat().st_size != pb.stat().st_size:
            return False
    except OSError:
        return False
    return sha256_arquivo(pa) == sha256_arquivo(pb)


# --------------------------------------------------------------------------- #
# Candidatos de colisão
# --------------------------------------------------------------------------- #

#: Quantos sufixos ``-N`` tentar antes de desistir (RF-22).
MAX_CANDIDATOS = 999


def candidato_colisao(destino: Path, n: int) -> Path:
    """N-ésimo candidato de destino. ``n=1`` é o próprio destino.

    O sufixo é ``-N`` e nunca `` (N)``: `` (N)`` é justamente o padrão que a
    heurística de nome genérico remove (RF-23).
    """
    if n <= 1:
        return destino
    return destino.with_name(f"{destino.stem}-{n}{destino.suffix}")


def relativo_seguro(p: Path | str, base: Path | str) -> PurePath | None:
    """``p`` relativo a ``base``, ou ``None`` se ``p`` não estiver sob ``base``."""
    try:
        return Path(os.path.abspath(str(p))).relative_to(Path(os.path.abspath(str(base))))
    except ValueError:
        return None
