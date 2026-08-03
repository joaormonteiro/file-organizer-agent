"""RF-38, RF-39, RF-41, RF-42, RF-46 — fila de pendentes e backoff."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

import pytest

from organizer import db, queue
from organizer.queue import Motivo


def test_enfileirar_ocupado_usa_retry_busy(sandbox, conn):
    """RF-37: primeira tentativa com `ocupado` vence em `RETRY_BUSY_MINUTES`."""
    agora = db.agora()
    linha = queue.enfileirar(conn, "X:/dl/a.pdf", Motivo.OCUPADO, sandbox.cfg, momento=agora)
    assert linha["motivo"] == Motivo.OCUPADO.value
    assert linha["tentativas"] == 1
    assert linha["retry_after"] == db.ts_mais(sandbox.cfg.retry_busy_minutes, agora)


def test_enfileirar_startup_usa_retry_startup(sandbox, conn):
    """RF-40: a varredura de startup usa `RETRY_STARTUP_MINUTES` (30)."""
    agora = db.agora()
    linha = queue.enfileirar(conn, "X:/dl/a.pdf", Motivo.STARTUP, sandbox.cfg, momento=agora)
    assert linha["retry_after"] == db.ts_mais(sandbox.cfg.retry_startup_minutes, agora)


@pytest.mark.parametrize(
    "tentativas,esperado",
    [(1, 120.0), (2, 180.0), (3, 270.0), (4, 405.0), (20, float(queue.TETO_BACKOFF_MINUTOS))],
)
def test_backoff(tentativas, esperado):
    """RF-41: `base * 1.5^(tentativas-1)` com teto de 24 h."""
    assert queue.calcular_backoff(120, tentativas) == pytest.approx(esperado)


def test_backoff_zero():
    assert queue.calcular_backoff(0, 5) == 0.0


def test_loop_idle_seleciona_vencidos(sandbox, conn):
    """RF-38: a única consulta do loop idle é pelos `retry_after` vencidos."""
    agora = datetime(2026, 8, 2, 12, 0, 0)
    db.inserir_pendente(conn, "X:/dl/vencido.pdf", db.ts_mais(-1, agora), Motivo.OCUPADO.value)
    db.inserir_pendente(conn, "X:/dl/agora.pdf", db.ts(agora), Motivo.OCUPADO.value)
    db.inserir_pendente(conn, "X:/dl/futuro.pdf", db.ts_mais(120, agora), Motivo.OCUPADO.value)

    achados = {linha["path"] for linha in queue.vencidos(conn, momento=agora)}
    assert achados == {"X:/dl/vencido.pdf", "X:/dl/agora.pdf"}

    depois = {linha["path"] for linha in queue.vencidos(conn, momento=agora + timedelta(hours=3))}
    assert "X:/dl/futuro.pdf" in depois


def test_adia_quando_ainda_ocupado(sandbox, conn):
    """RF-39: pendente vencido com sistema ocupado é adiado e conta a tentativa."""
    agora = datetime(2026, 8, 2, 12, 0, 0)
    primeiro = queue.enfileirar(conn, "X:/dl/a.pdf", Motivo.OCUPADO, sandbox.cfg, momento=agora)
    assert primeiro["tentativas"] == 1

    depois = agora + timedelta(minutes=sandbox.cfg.retry_busy_minutes)
    segundo = queue.adiar(conn, primeiro["id"], Motivo.OCUPADO, sandbox.cfg, momento=depois)
    assert segundo["tentativas"] == 2
    # adiado ao menos outras RETRY_BUSY_MINUTES (o backoff cresce 1.5x a partir daí)
    assert segundo["retry_after"] >= db.ts_mais(sandbox.cfg.retry_busy_minutes, depois)
    assert queue.adiar(conn, 9999, Motivo.OCUPADO, sandbox.cfg) is None


def test_teto_de_tentativas(sandbox, conn):
    """RF-42: acima de `MAX_TENTATIVAS` o arquivo não fica em loop de retry."""
    maximo = sandbox.cfg.max_tentativas
    assert queue.excedeu_tentativas(maximo, maximo) is False
    assert queue.excedeu_tentativas(maximo + 1, maximo) is True

    for _ in range(maximo + 1):
        queue.enfileirar(conn, "X:/dl/a.pdf", Motivo.OCUPADO, sandbox.cfg)
    linha = db.pendente_por_path(conn, "X:/dl/a.pdf")
    assert queue.excedeu_tentativas(linha["tentativas"], maximo) is True

    queue.descartar(conn, linha["id"], erro="teto atingido")
    assert db.pendente_por_path(conn, "X:/dl/a.pdf") is None


def test_descartar_inexistente_nao_explode(conn):
    queue.descartar(conn, 12345)


def test_concluir_remove_da_fila_e_solta_lock(sandbox, conn):
    queue.enfileirar(conn, "X:/dl/a.pdf", Motivo.OCUPADO, sandbox.cfg)
    db.adquirir_lock(conn, "X:/dl/a.pdf", 1)
    queue.concluir(conn, "X:/dl/a.pdf")
    assert db.pendente_por_path(conn, "X:/dl/a.pdf") is None
    assert db.locks_ativos(conn) == []


def test_locks_orfaos_sao_limpos(conn):
    """RF-46: locks mais velhos que `LOCK_TTL` (15 min) somem."""
    agora = db.agora()
    db.adquirir_lock(conn, "X:/dl/novo.pdf", 1)
    db.adquirir_lock(conn, "X:/dl/velho.pdf", 2)
    conn.execute(
        "UPDATE em_processamento SET iniciado_em = ? WHERE path = ?",
        (db.ts_mais(-60, agora), "X:/dl/velho.pdf"),
    )

    removidos = queue.limpar_locks_orfaos(conn, momento=agora)
    assert removidos == 1
    assert [linha["path"] for linha in db.locks_ativos(conn)] == ["X:/dl/novo.pdf"]


def test_base_por_motivo(sandbox):
    assert queue.base_minutos(Motivo.OCUPADO, sandbox.cfg) == sandbox.cfg.retry_busy_minutes
    assert queue.base_minutos(Motivo.STARTUP, sandbox.cfg) == sandbox.cfg.retry_startup_minutes
    assert queue.base_minutos(Motivo.INSTAVEL, sandbox.cfg) == queue.RETRY_INSTAVEL_MINUTOS
    assert queue.base_minutos(Motivo.LIMITE_WORKERS, sandbox.cfg) == queue.RETRY_CURTO_MINUTOS
    assert queue.base_minutos(Motivo.RECUPERADO, sandbox.cfg) == 0


def test_motivo_e_string_utilizavel():
    """RNF-15: `Motivo` é um `str` Enum — o valor grava direto no banco."""
    assert Motivo.OCUPADO == "ocupado"
    assert Motivo("instavel") is Motivo.INSTAVEL


def test_descartar_registra_o_erro_no_log(sandbox, conn, caplog):
    """D9: o erro vai para o log, não para uma linha que some no instante seguinte."""
    linha = queue.enfileirar(conn, "X:/dl/a.pdf", Motivo.OCUPADO, sandbox.cfg)
    with caplog.at_level(logging.WARNING, logger="organizer.queue"):
        queue.descartar(conn, linha["id"], erro="teto de tentativas atingido")

    assert db.pendente_por_path(conn, "X:/dl/a.pdf") is None
    assert "teto de tentativas atingido" in caplog.text
    assert "X:/dl/a.pdf" in caplog.text


def test_descartar_sem_erro_nao_loga(sandbox, conn, caplog):
    linha = queue.enfileirar(conn, "X:/dl/a.pdf", Motivo.OCUPADO, sandbox.cfg)
    with caplog.at_level(logging.WARNING, logger="organizer.queue"):
        queue.descartar(conn, linha["id"])
    assert caplog.text == ""
