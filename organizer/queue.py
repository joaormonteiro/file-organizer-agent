"""Fila `pendentes`: enfileirar, vencer, adiar, concluir — e o Enum de motivos.

O `Motivo` mora aqui (e não em `ingest`) por uma razão prática: `move.py` e
`stability.py` precisam dele, e `watch.py` importa `move` para a recuperação de
startup. Se o Enum vivesse em `ingest`, o watcher acabaria arrastando `guard`,
`classify` e `extract` para dentro do processo permanente, quebrando a RNF-03.
`ingest` reexporta `Motivo`, de modo que `ingest.Motivo` continua sendo o nome
canônico exigido pela RNF-15.

Requisitos cobertos: RF-37, RF-38, RF-39, RF-41, RF-42, RF-46, RNF-15.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path

from organizer import db, log

_logger = log.get_logger("queue")

#: Teto absoluto do backoff (RF-41).
TETO_BACKOFF_MINUTOS = 24 * 60

#: Fator de crescimento do backoff.
FATOR_BACKOFF = 1.5

#: Atraso curto usado quando o limite de workers é atingido ou o spawn falha.
RETRY_CURTO_MINUTOS = 5

#: Atraso usado quando o arquivo ainda está instável.
RETRY_INSTAVEL_MINUTOS = 10


class Motivo(str, Enum):
    """Todos os valores possíveis da coluna `motivo` — fonte única (RNF-15)."""

    OK = "ok"
    PRONTO = "pronto"
    OCUPADO = "ocupado"
    INSTAVEL = "instavel"
    HANDLE_ABERTO = "handle_aberto"
    SUMIU = "sumiu"
    DIRETORIO = "diretorio"
    PARCIAL = "parcial"
    DENTRO_DO_TARGET = "dentro_do_target"
    BAIXA_CONFIANCA = "baixa_confianca"
    LLM_INDISPONIVEL = "llm_indisponivel"
    LLM_TIMEOUT = "llm_timeout"
    LLM_PARSE_ERROR = "llm_parse_error"
    LLM_SEM_CORROBORACAO = "llm_sem_corroboracao"
    PATH_MUITO_LONGO = "path_muito_longo"
    COLISAO_IRRESOLVIVEL = "colisao_irresolvivel"
    DUPLICADO = "duplicado"
    CROSS_VOLUME = "cross_volume"
    SPAWN_FALHOU = "spawn_falhou"
    LIMITE_WORKERS = "limite_workers"
    PERMISSAO = "permissao"
    MAX_TENTATIVAS = "max_tentativas"
    AGUARDANDO_APROVACAO = "aguardando_aprovacao"
    ERRO_INESPERADO = "erro_inesperado"
    STARTUP = "startup"
    LOCK_OCUPADO = "lock_ocupado"
    RECUPERADO = "recuperado"

    def __str__(self) -> str:  # pragma: no cover - conveniência de log
        return self.value


#: Atraso base por motivo, em minutos. `None` significa "use a config".
BASE_POR_MOTIVO: dict[Motivo, int] = {
    Motivo.INSTAVEL: RETRY_INSTAVEL_MINUTOS,
    Motivo.SPAWN_FALHOU: RETRY_CURTO_MINUTOS,
    Motivo.LIMITE_WORKERS: RETRY_CURTO_MINUTOS,
    Motivo.LOCK_OCUPADO: RETRY_CURTO_MINUTOS,
    Motivo.CROSS_VOLUME: 30,
    Motivo.PERMISSAO: 30,
    Motivo.LLM_TIMEOUT: 30,
    Motivo.ERRO_INESPERADO: 30,
    # recuperado no startup: vence imediatamente, o loop idle pega na primeira passada
    Motivo.RECUPERADO: 0,
}


def base_minutos(motivo: Motivo, cfg) -> int:
    """Atraso base do motivo; `ocupado` e `startup` vêm da configuração."""
    if motivo == Motivo.OCUPADO:
        return cfg.retry_busy_minutes
    if motivo == Motivo.STARTUP:
        return cfg.retry_startup_minutes
    return BASE_POR_MOTIVO.get(motivo, cfg.retry_busy_minutes)


def calcular_backoff(base: float, tentativas: int) -> float:
    """`base * 1.5^(tentativas-1)`, com teto de 24 h (RF-41)."""
    expoente = max(0, int(tentativas) - 1)
    return min(float(base) * (FATOR_BACKOFF**expoente), float(TETO_BACKOFF_MINUTOS))


def excedeu_tentativas(tentativas: int, max_tentativas: int) -> bool:
    """Acima do teto o arquivo sai da fila e vai para o `_Inbox` (RF-42)."""
    return int(tentativas) > int(max_tentativas)


# --------------------------------------------------------------------------- #
# Operações da fila
# --------------------------------------------------------------------------- #


def enfileirar(
    conn: db.Conexao,
    caminho: Path | str,
    motivo: Motivo,
    cfg,
    erro: str | None = None,
    momento: datetime | None = None,
) -> db.Linha:
    """Coloca (ou recoloca) o arquivo na fila aplicando o backoff.

    A primeira tentativa usa exatamente o atraso base — é isso que garante
    `retry_after = now + RETRY_BUSY_MINUTES` para o motivo `ocupado` (RF-37).
    """
    caminho = str(caminho)
    atual = db.pendente_por_path(conn, caminho)
    tentativas = (int(atual["tentativas"]) if atual else 0) + 1
    atraso = calcular_backoff(base_minutos(motivo, cfg), tentativas)
    db.inserir_pendente(
        conn,
        caminho,
        db.ts_mais(atraso, momento),
        motivo.value,
        erro,
    )
    return db.pendente_por_path(conn, caminho)


def vencidos(
    conn: db.Conexao, limite: int = 50, momento: datetime | None = None
) -> list[db.Linha]:
    """Pendentes cujo `retry_after` já passou — a única consulta do loop idle."""
    return db.pendentes_vencidos(conn, limite, momento)


def adiar(
    conn: db.Conexao,
    id_: int,
    motivo: Motivo,
    cfg,
    momento: datetime | None = None,
) -> db.Linha | None:
    """Empurra o pendente para frente e incrementa `tentativas` (RF-39)."""
    linha = db.pendente_por_id(conn, id_)
    if linha is None:
        return None
    tentativas = int(linha["tentativas"]) + 1
    atraso = calcular_backoff(base_minutos(motivo, cfg), tentativas)
    db.adiar_pendente(conn, id_, db.ts_mais(atraso, momento), motivo.value)
    return db.pendente_por_id(conn, id_)


def concluir(conn: db.Conexao, caminho: Path | str) -> None:
    """Tira o arquivo da fila e libera o lock lógico."""
    db.remover_pendente(conn, str(caminho))
    db.liberar_lock(conn, str(caminho))


def descartar(conn: db.Conexao, id_: int, erro: str | None = None) -> None:
    """Remove o pendente sem reprocessar (usado ao estourar `MAX_TENTATIVAS`).

    O erro vai para o log, não para a linha: gravá-lo na `pendentes` que será
    apagada no instante seguinte não deixaria rastro nenhum.
    """
    linha = db.pendente_por_id(conn, id_)
    if linha is None:
        return
    if erro:
        _logger.warning(
            "descartando pendente id=%s path=%s tentativas=%s motivo=%s erro=%s",
            id_,
            linha["path"],
            linha["tentativas"],
            linha["motivo"],
            erro,
        )
    db.remover_pendente_id(conn, id_)


def limpar_locks_orfaos(
    conn: db.Conexao, ttl_minutos: int = db.LOCK_TTL_MINUTOS, momento: datetime | None = None
) -> int:
    """Locks de workers mortos são removidos no startup e a cada loop idle (RF-46)."""
    return db.limpar_locks_orfaos(conn, ttl_minutos, momento)
