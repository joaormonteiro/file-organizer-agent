"""RF-06, RF-07, RF-24, RF-25 — PRAGMAs, migrations e DAOs."""

from __future__ import annotations

from datetime import timedelta

import pytest

from organizer import db


def test_pragmas(conn):
    """RF-06: WAL, busy_timeout >= 10000 e foreign_keys ligadas."""
    valores = db.pragmas(conn)
    assert str(valores["journal_mode"]).lower() == "wal"
    assert int(valores["busy_timeout"]) >= 10000
    assert int(valores["foreign_keys"]) == 1


def test_migrar_e_idempotente(sandbox):
    """RF-07: rodar duas vezes não muda `sqlite_master` nem a versão."""
    conexao = db.conectar(sandbox.db_path)
    try:
        versao_um = db.migrar(conexao)
        estrutura_um = db.tabelas(conexao)
        versao_dois = db.migrar(conexao)
        estrutura_dois = db.tabelas(conexao)
        assert versao_um == versao_dois == len(db.MIGRATIONS)
        assert estrutura_um == estrutura_dois
        assert "table:arquivos" in estrutura_um
        assert "table:pendentes" in estrutura_um
        assert "table:operacoes" in estrutura_um
        assert "table:reservas" in estrutura_um
        assert "table:em_processamento" in estrutura_um
    finally:
        conexao.close()


def test_versao_em_banco_vazio(sandbox):
    conexao = db.conectar(sandbox.db_path)
    try:
        assert db.versao_schema(conexao) == 0
    finally:
        conexao.close()


def test_inserir_arquivo_e_upsert(conn):
    """RF-25: reindexar o mesmo path é UPSERT, nunca duplica."""
    dados = dict(nome_orig="a.pdf", nome_atual="a.pdf", path="X:/alvo/a.pdf", tipo="documento")
    primeiro = db.inserir_arquivo(conn, **dados)
    segundo = db.inserir_arquivo(conn, **{**dados, "tipo": "planilha"})
    assert primeiro == segundo
    assert db.contar_arquivos(conn) == 1
    assert db.buscar_por_path(conn, "X:/alvo/a.pdf")["tipo"] == "planilha"


def test_inserir_arquivo_exige_campos(conn):
    with pytest.raises(ValueError):
        db.inserir_arquivo(conn, nome_orig="a.pdf")


def test_fts_sincronizada_por_triggers(conn):
    """RF-64 (base): a v2 já mantém `arquivos_fts` em sincronia."""
    db.inserir_arquivo(
        conn,
        nome_orig="matriz.pdf",
        nome_atual="matriz-curricular-ec-2026.pdf",
        path="X:/alvo/matriz.pdf",
        tipo="documento",
        subtipo="Matrizes-Curriculares",
        texto_amostra="grade curricular do curso",
    )
    assert db.contar_fts(conn) == 1
    achados = db.buscar_fts(conn, '"curricular"')
    assert achados and achados[0]["path"] == "X:/alvo/matriz.pdf"

    db.inserir_arquivo(
        conn,
        nome_orig="matriz.pdf",
        nome_atual="renomeado.pdf",
        path="X:/alvo/matriz.pdf",
        tipo="documento",
    )
    assert db.contar_fts(conn) == 1
    assert db.buscar_fts(conn, '"renomeado"')

    db.remover_arquivo(conn, "X:/alvo/matriz.pdf")
    assert db.contar_fts(conn) == 0


def test_busca_fts_ignora_acento(conn):
    """RF-64: `remove_diacritics 2` — `horario` encontra `horário`."""
    db.inserir_arquivo(
        conn,
        nome_orig="h.pdf",
        nome_atual="horário-das-aulas.pdf",
        path="X:/alvo/h.pdf",
        tipo="documento",
    )
    assert db.buscar_fts(conn, '"horario"')


def test_kv_com_ttl(conn):
    agora = db.agora()
    db.kv_set(conn, "chave", "valor", ttl_segundos=60)
    assert db.kv_get(conn, "chave", agora) == "valor"
    assert db.kv_get(conn, "chave", agora + timedelta(minutes=5)) is None
    assert db.kv_get(conn, "inexistente") is None


def test_lock_logico(conn):
    """RF-30: `em_processamento.path` UNIQUE é o mecanismo de dedupe."""
    assert db.adquirir_lock(conn, "X:/a.pdf", 1) is True
    assert db.adquirir_lock(conn, "X:/a.pdf", 2) is False
    db.liberar_lock(conn, "X:/a.pdf")
    assert db.adquirir_lock(conn, "X:/a.pdf", 3) is True


def test_pendentes_dedupe_e_tentativas(conn):
    db.inserir_pendente(conn, "X:/a.pdf", db.ts(), "ocupado")
    db.inserir_pendente(conn, "X:/a.pdf", db.ts(), "ocupado")
    assert db.contar_pendentes(conn) == 1
    assert db.pendente_por_path(conn, "X:/a.pdf")["tentativas"] == 2


def test_pendentes_vencidos(conn):
    agora = db.agora()
    db.inserir_pendente(conn, "X:/vencido.pdf", db.ts_mais(-10, agora), "ocupado")
    db.inserir_pendente(conn, "X:/futuro.pdf", db.ts_mais(120, agora), "ocupado")
    vencidos = db.pendentes_vencidos(conn, momento=agora)
    assert [linha["path"] for linha in vencidos] == ["X:/vencido.pdf"]


def test_journal_ciclo(conn):
    op_id = db.journal_planejar(conn, "X:/origem.pdf", "X:/destino.pdf", 0.9, "extensao")
    assert db.journal_por_id(conn, op_id)["estado"] == "planejado"
    db.reserva_criar(conn, op_id, "X:/destino.pdf")
    assert db.reserva_por_op(conn, op_id)["path"] == "X:/destino.pdf"
    db.journal_estado(conn, op_id, "movido")
    db.reserva_remover(conn, op_id)
    assert db.reserva_por_op(conn, op_id) is None
    db.journal_estado(conn, op_id, "concluido")
    linha = db.journal_por_id(conn, op_id)
    assert linha["estado"] == "concluido"
    assert linha["finalizado_em"] is not None
    assert [l["id"] for l in db.journal_por_estado(conn, ("concluido",))] == [op_id]


def test_paths_conhecidos(conn):
    db.inserir_arquivo(
        conn, nome_orig="a.pdf", nome_atual="a.pdf", path="X:/alvo/a.pdf", path_orig="X:/dl/a.pdf"
    )
    db.inserir_pendente(conn, "X:/dl/b.pdf", db.ts(), "ocupado")
    db.adquirir_lock(conn, "X:/dl/c.pdf", 1)
    assert db.paths_conhecidos(conn) == {
        "X:/alvo/a.pdf",
        "X:/dl/a.pdf",
        "X:/dl/b.pdf",
        "X:/dl/c.pdf",
    }


def test_ts_e_ts_mais():
    base = db.agora()
    assert db.ts(base) < db.ts_mais(10, base)
    assert len(db.ts(base)) == 19
