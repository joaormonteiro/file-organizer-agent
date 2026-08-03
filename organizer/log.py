"""Logging do agente: arquivo rotativo + linha única por decisão.

`rich` NUNCA é importado aqui — ele pertence só aos entrypoints interativos
(`query.py`, `inbox.py`), porque o watcher não pode carregá-lo (RNF-03).

Requisitos cobertos: RNF-14, RNF-16.
"""

from __future__ import annotations

import io
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

#: Teto de 5 MB por arquivo, 3 backups (RNF-16).
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3

NOME_ARQUIVO = "organizer.log"
FORMATO = "%(asctime)s %(levelname)s %(process)d %(name)s %(message)s"

_RAIZ = "organizer"
_configurado = False


def _stderr_utf8():
    """`sys.stderr` capaz de escrever qualquer nome de arquivo.

    O console do Windows entrega um stream em cp1252: um nome com CJK,
    cirílico ou emoji levanta `UnicodeEncodeError` dentro do handler, o módulo
    `logging` engole a exceção e a linha de decisão **some**. Reembrulhamos o
    buffer binário em UTF-8 com `backslashreplace` — pior caso, escapa; nunca
    perde a linha.
    """
    try:
        return io.TextIOWrapper(
            sys.stderr.buffer,
            encoding="utf-8",
            errors="backslashreplace",
            line_buffering=True,
        )
    except (AttributeError, ValueError, io.UnsupportedOperation):
        return sys.stderr


def configurar(log_dir: Path | str, nivel: int = logging.INFO, console: bool = False) -> logging.Logger:
    """Instala o handler rotativo no logger raiz do pacote. Idempotente."""
    global _configurado
    raiz = logging.getLogger(_RAIZ)
    raiz.setLevel(nivel)
    # `propagate` fica ligado de propósito: é o que permite a um harness externo
    # (pytest/caplog, um supervisor) capturar as linhas de decisão sem gambiarra.
    raiz.propagate = True

    if _configurado:
        return raiz

    destino = Path(log_dir)
    destino.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        destino / NOME_ARQUIVO,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(FORMATO))
    raiz.addHandler(handler)

    if console:
        eco = logging.StreamHandler(_stderr_utf8())
        eco.setFormatter(logging.Formatter(FORMATO))
        raiz.addHandler(eco)

    _configurado = True
    return raiz


def resetar() -> None:
    """Desfaz `configurar()` — usado entre testes para não vazar handlers."""
    global _configurado
    raiz = logging.getLogger(_RAIZ)
    for handler in list(raiz.handlers):
        raiz.removeHandler(handler)
        handler.close()
    _configurado = False


def get_logger(nome: str) -> logging.Logger:
    """Logger nomeado sob a hierarquia do pacote."""
    if nome.startswith(_RAIZ):
        return logging.getLogger(nome)
    return logging.getLogger(f"{_RAIZ}.{nome}")


def _campo(valor) -> str:
    texto = "" if valor is None else str(valor)
    texto = texto.replace("\r", " ").replace("\n", " ")
    return f'"{texto}"' if " " in texto else texto


def linha_decisao(
    decision: str,
    path: str | Path,
    dest: str | Path | None,
    conf: float | None,
    via: str | None,
    motivo: str | None,
) -> str:
    """Formata a linha única de decisão exigida por RNF-14.

    Sempre nesta ordem: `decision= path= dest= conf= via= motivo=`.
    """
    conf_txt = "" if conf is None else f"{float(conf):.2f}"
    return (
        f"decision={_campo(decision)} "
        f"path={_campo(path)} "
        f"dest={_campo(dest)} "
        f"conf={_campo(conf_txt)} "
        f"via={_campo(via)} "
        f"motivo={_campo(motivo)}"
    )


def logar_decisao(
    logger: logging.Logger,
    decision: str,
    path: str | Path,
    dest: str | Path | None = None,
    conf: float | None = None,
    via: str | None = None,
    motivo: str | None = None,
    nivel: int = logging.INFO,
) -> str:
    """Emite (e devolve) a linha de decisão."""
    linha = linha_decisao(decision, path, dest, conf, via, motivo)
    logger.log(nivel, linha)
    return linha
