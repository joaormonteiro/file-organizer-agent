"""RF-44, RF-45 — recuperação de crash (uma linha da tabela da ARQUITETURA §12 por teste)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import factories
from organizer import db, move, rules
from organizer.queue import Motivo

RAIZ_PROJETO = Path(__file__).resolve().parent.parent


def _destino(sandbox, nome="setup.exe") -> Path:
    pasta = sandbox.caminho(rules.CAT_INSTALADORES)
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta / nome


def test_planejado_nada_aconteceu(sandbox, conn):
    """Linha 1: origem existe, destino não → reenfileira."""
    origem = factories.criar(sandbox.downloads, "setup.exe")
    op_id = db.journal_planejar(conn, str(origem), str(_destino(sandbox)))

    contadores = move.recover_incomplete(conn, sandbox.cfg)

    assert contadores["reenfileirados"] == 1
    assert db.journal_por_id(conn, op_id)["estado"] == move.ESTADO_ABORTADO
    pendente = db.pendente_por_path(conn, str(origem))
    assert pendente["motivo"] == Motivo.RECUPERADO.value
    assert origem.exists()


def test_planejado_com_reserva(sandbox, conn):
    """Linha 2: destino é a nossa reserva de 0 byte → refaz o replace."""
    origem = factories.criar(sandbox.downloads, "setup.exe")
    conteudo = origem.read_bytes()
    destino = _destino(sandbox)
    op_id = db.journal_planejar(conn, str(origem), str(destino))
    destino.write_bytes(b"")
    db.reserva_criar(conn, op_id, str(destino))

    contadores = move.recover_incomplete(conn, sandbox.cfg)

    assert contadores["reserva_refeita"] == 1
    assert not origem.exists()
    assert destino.read_bytes() == conteudo
    assert db.journal_por_id(conn, op_id)["estado"] == move.ESTADO_CONCLUIDO
    assert db.reserva_por_op(conn, op_id) is None
    assert db.contar_arquivos(conn) == 1


def test_planejado_replace_ja_ocorreu(sandbox, conn):
    """Linha 3: origem sumiu, destino existe → o replace completou antes do commit."""
    origem = sandbox.downloads / "setup.exe"
    destino = _destino(sandbox)
    destino.write_bytes(factories.bytes_deterministicos(256))
    op_id = db.journal_planejar(conn, str(origem), str(destino))

    contadores = move.recover_incomplete(conn, sandbox.cfg)

    assert contadores["replace_ja_ocorrido"] == 1
    assert db.journal_por_id(conn, op_id)["estado"] == move.ESTADO_CONCLUIDO
    assert db.buscar_por_path(conn, str(destino)) is not None


def test_planejado_ambos_sumiram(sandbox, conn):
    """Linha 4: usuário mexeu → abortado, com log."""
    origem = sandbox.downloads / "sumido.exe"
    destino = _destino(sandbox, "sumido.exe")
    op_id = db.journal_planejar(conn, str(origem), str(destino))

    contadores = move.recover_incomplete(conn, sandbox.cfg)

    assert contadores["abortados"] == 1
    linha = db.journal_por_id(conn, op_id)
    assert linha["estado"] == move.ESTADO_ABORTADO
    assert linha["erro"] == Motivo.SUMIU.value
    assert db.contar_pendentes(conn) == 0


def test_movido_reindexado(sandbox, conn):
    """Linha 5: estado `movido` → a indexação é reexecutada (UPSERT idempotente)."""
    origem = sandbox.downloads / "setup.exe"
    destino = _destino(sandbox)
    destino.write_bytes(factories.bytes_deterministicos(512))
    op_id = db.journal_planejar(
        conn, str(origem), str(destino), 0.95, "extensao", rules.CAT_INSTALADORES
    )
    db.journal_estado(conn, op_id, move.ESTADO_MOVIDO)

    assert move.recover_incomplete(conn, sandbox.cfg)["reindexados"] == 1
    linha = db.buscar_por_path(conn, str(destino))
    assert linha["tipo"] == rules.FAM_SOFTWARE
    assert linha["subtipo"] == "Instaladores"
    assert linha["via"] == "extensao"
    assert db.journal_por_id(conn, op_id)["estado"] == move.ESTADO_CONCLUIDO

    # rodar de novo não duplica
    move.recover_incomplete(conn, sandbox.cfg)
    assert db.contar_arquivos(conn) == 1


def test_concluido_noop(sandbox, conn):
    """Linha 6: `concluido` não é tocado."""
    destino = _destino(sandbox)
    destino.write_bytes(b"x")
    op_id = db.journal_planejar(conn, "X:/dl/setup.exe", str(destino))
    db.journal_estado(conn, op_id, move.ESTADO_CONCLUIDO)

    contadores = move.recover_incomplete(conn, sandbox.cfg)

    assert contadores == {
        "reenfileirados": 0,
        "reserva_refeita": 0,
        "replace_ja_ocorrido": 0,
        "abortados": 0,
        "reindexados": 0,
        "falhas": 0,
    }
    assert db.contar_arquivos(conn) == 0


def test_planejado_destino_ocupado_por_terceiro(sandbox, conn):
    """Fora da tabela: destino existe mas não é a nossa reserva → reenfileira sem tocar."""
    origem = factories.criar(sandbox.downloads, "setup.exe")
    destino = _destino(sandbox)
    destino.write_bytes(b"conteudo de outra pessoa")
    db.journal_planejar(conn, str(origem), str(destino))

    assert move.recover_incomplete(conn, sandbox.cfg)["reenfileirados"] == 1
    assert destino.read_bytes() == b"conteudo de outra pessoa"
    assert origem.exists()


def test_dry_run_e_abortado(sandbox, conn):
    origem = factories.criar(sandbox.downloads, "setup.exe")
    op_id = db.journal_planejar(conn, str(origem), str(_destino(sandbox)), dry_run=True)
    assert move.recover_incomplete(conn, sandbox.cfg)["abortados"] == 1
    assert db.journal_por_id(conn, op_id)["estado"] == move.ESTADO_ABORTADO


_SCRIPT_CRASH = """
import os, sys
sys.path.insert(0, sys.argv[3])
from organizer import config, db

origem, destino = sys.argv[1], sys.argv[2]
cfg = config.get_config()
conn = db.abrir(cfg.db_path)
op_id = db.journal_planejar(conn, origem, destino, 0.95, "extensao", "Softwares/Instaladores")
os.makedirs(os.path.dirname(destino), exist_ok=True)
descritor = os.open(destino, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
os.close(descritor)
db.reserva_criar(conn, op_id, destino)
# morre exatamente entre o passo 2 (reserva) e o passo 4 (commit do move)
os._exit(1)
"""


def test_kill_no_meio(sandbox, conn, tmp_path):
    """RF-45: matar o worker entre a reserva e o commit não perde nem duplica arquivo."""
    origem = factories.criar(sandbox.downloads, "setup.exe")
    conteudo = origem.read_bytes()
    destino = _destino(sandbox)
    script = tmp_path / "crash.py"
    script.write_text(_SCRIPT_CRASH, encoding="utf-8")

    processo = subprocess.run(
        [sys.executable, str(script), str(origem), str(destino), str(RAIZ_PROJETO)],
        cwd=str(RAIZ_PROJETO),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert processo.returncode == 1, processo.stderr
    assert origem.exists(), "a origem ainda está lá — o replace não chegou a acontecer"
    assert destino.exists() and destino.stat().st_size == 0, "a reserva ficou no disco"

    contadores = move.recover_incomplete(conn, sandbox.cfg)

    assert contadores["reserva_refeita"] == 1
    assert not origem.exists()
    assert destino.read_bytes() == conteudo
    assert db.contar_arquivos(conn) == 1
    assert db.listar_reservas(conn) == []
    assert db.journal_por_estado(conn, (move.ESTADO_PLANEJADO, move.ESTADO_MOVIDO)) == []

    # o arquivo está em exatamente um lugar
    encontrados = [p for p in sandbox.raiz.rglob("setup.exe") if p.is_file()]
    assert len(encontrados) == 1 and encontrados[0] == destino


def test_recuperacao_nao_deleta_nada(sandbox, conn, monkeypatch):
    """A recuperação nunca chama primitivas de deleção de arquivo do usuário."""
    delecoes: list[str] = []
    for nome in ("remove", "unlink"):
        original = getattr(os, nome)
        monkeypatch.setattr(
            os,
            nome,
            lambda caminho, *a, _o=original, _n=nome, **k: (
                delecoes.append(f"{_n}:{caminho}"),
                _o(caminho, *a, **k),
            )[1],
        )
    origem = factories.criar(sandbox.downloads, "setup.exe")
    destino = _destino(sandbox)
    op_id = db.journal_planejar(conn, str(origem), str(destino))
    destino.write_bytes(b"")
    db.reserva_criar(conn, op_id, str(destino))

    move.recover_incomplete(conn, sandbox.cfg)
    assert delecoes == []


def test_falha_na_reindexacao_e_isolada(sandbox, conn, monkeypatch):
    """D1: erro ao reindexar uma operação `movido` não derruba as outras."""
    destino_a = _destino(sandbox, "a.exe")
    destino_b = _destino(sandbox, "b.exe")
    destino_a.write_bytes(b"aaa")
    destino_b.write_bytes(b"bbb")
    op_a = db.journal_planejar(conn, str(sandbox.downloads / "a.exe"), str(destino_a))
    op_b = db.journal_planejar(conn, str(sandbox.downloads / "b.exe"), str(destino_b))
    db.journal_estado(conn, op_a, move.ESTADO_MOVIDO)
    db.journal_estado(conn, op_b, move.ESTADO_MOVIDO)

    original = move._indexar_de_operacao

    def instavel(conexao, operacao):
        if int(operacao["id"]) == op_a:
            raise OSError("disco sumiu no meio da reindexação")
        return original(conexao, operacao)

    monkeypatch.setattr(move, "_indexar_de_operacao", instavel)
    contadores = move.recover_incomplete(conn, sandbox.cfg)

    assert contadores["falhas"] == 1
    assert contadores["reindexados"] == 1
    assert db.buscar_por_path(conn, str(destino_b)) is not None
    assert db.journal_por_id(conn, op_b)["estado"] == move.ESTADO_CONCLUIDO


def test_reserva_entre_volumes_nao_refaz_replace(sandbox, conn, monkeypatch):
    """D10: a linha 2 da tabela §12 não tenta `os.replace` cruzando volumes."""
    from organizer import paths

    origem = factories.criar(sandbox.downloads, "setup.exe")
    destino = _destino(sandbox)
    op_id = db.journal_planejar(conn, str(origem), str(destino))
    destino.write_bytes(b"")
    db.reserva_criar(conn, op_id, str(destino))

    monkeypatch.setattr(paths, "mesmo_volume", lambda a, b: False)
    contadores = move.recover_incomplete(conn, sandbox.cfg)

    assert contadores["reserva_refeita"] == 0
    assert contadores["reenfileirados"] == 1
    assert contadores["falhas"] == 0, "não pode virar OSError não tratado"
    assert origem.exists(), "o arquivo do usuário fica intacto"
    assert not destino.exists(), "a reserva de 0 byte foi descartada"
    assert db.listar_reservas(conn) == []
    assert db.pendente_por_path(conn, str(origem))["motivo"] == Motivo.RECUPERADO.value
