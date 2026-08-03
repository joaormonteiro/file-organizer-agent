"""RF-22, RF-23, RF-43, RNF-06, RNF-07 — o único módulo que altera o disco."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

import factories
from organizer import classify, db, move, paths, rules
from organizer.queue import Motivo


def decisao_para(origem: Path, categoria=rules.CAT_INSTALADORES, nome=None, inbox=False, conf=0.95):
    return classify.Decisao(
        origem=origem,
        categoria=categoria,
        nome_final=nome or origem.name,
        confianca=conf,
        via=classify.VIA_EXTENSAO,
        motivo=Motivo.OK.value,
        tipo=rules.FAM_SOFTWARE,
        subtipo=rules.subtipo_de(categoria),
        generico=False,
        para_inbox=inbox,
    )


def test_move_simples(sandbox, conn):
    origem = factories.criar(sandbox.downloads, "setup.exe")
    resultado = move.executar(conn, sandbox.cfg, decisao_para(origem))

    assert resultado.ok is True
    assert resultado.status == move.STATUS_ORGANIZADO
    assert resultado.destino == sandbox.caminho(rules.CAT_INSTALADORES) / "setup.exe"
    assert resultado.destino.exists()
    assert not origem.exists()
    assert db.journal_por_id(conn, resultado.op_id)["estado"] == move.ESTADO_MOVIDO


def test_journal_precede_o_move(sandbox, conn, monkeypatch):
    """RF-43: nada toca o filesystem antes de `operacoes` receber `planejado`."""
    ordem: list[str] = []

    planejar_original = db.journal_planejar
    replace_original = os.replace
    open_original = os.open

    def espiao_planejar(*args, **kwargs):
        ordem.append("journal")
        return planejar_original(*args, **kwargs)

    def espiao_open(caminho, flags, *args, **kwargs):
        if flags & os.O_EXCL:
            ordem.append("reserva")
        return open_original(caminho, flags, *args, **kwargs)

    def espiao_replace(*args, **kwargs):
        ordem.append("replace")
        return replace_original(*args, **kwargs)

    monkeypatch.setattr(db, "journal_planejar", espiao_planejar)
    monkeypatch.setattr(os, "open", espiao_open)
    monkeypatch.setattr(os, "replace", espiao_replace)

    origem = factories.criar(sandbox.downloads, "setup.exe")
    resultado = move.executar(conn, sandbox.cfg, decisao_para(origem))

    assert resultado.ok
    assert ordem == ["journal", "reserva", "replace"]

    monkeypatch.undo()
    move.concluir(conn, resultado.op_id)
    assert db.journal_por_id(conn, resultado.op_id)["estado"] == move.ESTADO_CONCLUIDO


def test_nunca_deleta_arquivo_do_usuario(sandbox, conn, monkeypatch):
    """RNF-07: um move normal não chama nenhuma primitiva de deleção."""
    delecoes: list[str] = []
    for nome in ("remove", "unlink"):
        original = getattr(os, nome)

        def espiao(caminho, *args, _original=original, _nome=nome, **kwargs):
            delecoes.append(f"{_nome}:{caminho}")
            return _original(caminho, *args, **kwargs)

        monkeypatch.setattr(os, nome, espiao)

    origem = factories.criar(sandbox.downloads, "setup.exe")
    resultado = move.executar(conn, sandbox.cfg, decisao_para(origem))

    assert resultado.ok
    assert delecoes == [], f"houve deleção inesperada: {delecoes}"
    assert resultado.destino.read_bytes() == factories.conteudo_para("setup.exe")


def test_reserva_de_zero_byte_e_substituida_e_nao_apagada(sandbox, conn):
    """A reserva `O_EXCL` é sobrescrita por `os.replace`, nunca deletada."""
    origem = factories.criar(sandbox.downloads, "setup.exe")
    plano = move.resolver_destino(sandbox.cfg, decisao_para(origem))
    op_id = db.journal_planejar(conn, str(origem), str(plano.base))
    alvo = move.reservar(conn, op_id, plano)
    assert alvo.exists() and alvo.stat().st_size == 0
    assert db.reserva_por_op(conn, op_id)["path"] == str(alvo)
    os.replace(str(origem), str(alvo))
    assert alvo.stat().st_size > 0


def test_descartar_reserva_so_apaga_arquivo_de_zero_byte(sandbox, conn):
    """RNF-07 (a): a deleção autorizada exige registro em `reservas` e 0 byte."""
    destino = sandbox.caminho(rules.CAT_INSTALADORES)
    destino.mkdir(parents=True, exist_ok=True)
    reserva = destino / "reserva.exe"
    reserva.write_bytes(b"")
    op_id = db.journal_planejar(conn, "X:/origem.exe", str(reserva))
    db.reserva_criar(conn, op_id, str(reserva))

    move._descartar_reserva(conn, op_id, reserva)
    assert not reserva.exists()
    assert db.reserva_por_op(conn, op_id) is None

    # sem registro em `reservas`, nada é apagado
    outro = destino / "arquivo-do-usuario.exe"
    outro.write_bytes(b"")
    op_id2 = db.journal_planejar(conn, "X:/origem2.exe", str(outro))
    move._descartar_reserva(conn, op_id2, outro)
    assert outro.exists()


def test_colisao_identica(sandbox, conn):
    """RF-22: destino idêntico vira duplicata no `_Inbox/_Duplicados`, nada é apagado."""
    rules.criar_arvore(sandbox.target, sandbox.cfg.inbox_dirname)
    conteudo = factories.bytes_deterministicos(2048)
    ja_existente = factories.criar(sandbox.caminho(rules.CAT_INSTALADORES), "setup.exe", conteudo)
    origem = factories.criar(sandbox.downloads, "setup.exe", conteudo)

    resultado = move.executar(conn, sandbox.cfg, decisao_para(origem))

    assert resultado.ok is True
    assert resultado.status == move.STATUS_DUPLICADO
    assert resultado.motivo == Motivo.DUPLICADO.value
    assert resultado.duplicado_de == str(ja_existente)
    assert resultado.destino.parent == sandbox.duplicados
    assert resultado.destino.name == "setup-dup1.exe"
    assert ja_existente.exists(), "o original nunca é tocado"
    assert not origem.exists()


def test_colisao_diferente(sandbox, conn):
    """RF-22 e RF-23: conteúdo diferente ganha sufixo `-2`, nunca ` (2)`."""
    rules.criar_arvore(sandbox.target, sandbox.cfg.inbox_dirname)
    factories.criar(
        sandbox.caminho(rules.CAT_INSTALADORES), "setup.exe", factories.bytes_deterministicos(64, 1)
    )
    origem = factories.criar(sandbox.downloads, "setup.exe", factories.bytes_deterministicos(64, 2))

    resultado = move.executar(conn, sandbox.cfg, decisao_para(origem))

    assert resultado.ok is True
    assert resultado.destino.name == "setup-2.exe"
    assert " (2)" not in resultado.destino.name
    assert "(" not in resultado.destino.name

    terceiro = factories.criar(
        sandbox.downloads, "setup.exe", factories.bytes_deterministicos(64, 3)
    )
    assert move.executar(conn, sandbox.cfg, decisao_para(terceiro)).destino.name == "setup-3.exe"


def test_colisao_esgotada(sandbox, conn, monkeypatch):
    """RF-22: esgotados os candidatos, o arquivo vai para o `_Inbox`."""
    monkeypatch.setattr(paths, "MAX_CANDIDATOS", 3)
    rules.criar_arvore(sandbox.target, sandbox.cfg.inbox_dirname)
    pasta = sandbox.caminho(rules.CAT_INSTALADORES)
    for indice, nome in enumerate(("setup.exe", "setup-2.exe", "setup-3.exe"), start=10):
        factories.criar(pasta, nome, factories.bytes_deterministicos(64, indice))
    origem = factories.criar(sandbox.downloads, "setup.exe", factories.bytes_deterministicos(64, 99))

    resultado = move.executar(conn, sandbox.cfg, decisao_para(origem))

    assert resultado.ok is True
    assert resultado.motivo == Motivo.COLISAO_IRRESOLVIVEL.value
    assert resultado.destino.parent == sandbox.inbox


def test_path_muito_longo_cai_no_inbox(sandbox, conn, monkeypatch):
    """RF-21: destino impossível pelo tamanho vai para o `_Inbox`."""
    monkeypatch.setattr(paths, "MAX_PATH", len(str(sandbox.target)) + 25)
    origem = factories.criar(sandbox.downloads, "setup.exe")
    decisao = decisao_para(origem, nome="a" * 120 + ".exe")

    resultado = move.executar(conn, sandbox.cfg, decisao)

    assert resultado.ok is True
    assert resultado.destino.parent == sandbox.inbox
    assert resultado.motivo == Motivo.PATH_MUITO_LONGO.value


def test_cross_volume_recusado_por_padrao(sandbox, conn, monkeypatch):
    """RNF-07 (b): sem `ALLOW_CROSS_VOLUME=1`, o move entre volumes é recusado."""
    monkeypatch.setattr(paths, "mesmo_volume", lambda a, b: False)
    origem = factories.criar(sandbox.downloads, "setup.exe")

    resultado = move.executar(conn, sandbox.cfg, decisao_para(origem))

    assert resultado.ok is False
    assert resultado.motivo == Motivo.CROSS_VOLUME.value
    assert origem.exists(), "o arquivo do usuário fica onde está"
    assert db.listar_operacoes(conn) == []


def test_cross_volume_permitido_confere_sha_antes_de_remover(sandbox, conn, monkeypatch):
    """RNF-07 (b): com a flag ligada, copia, confere sha256 e só então remove."""
    monkeypatch.setattr(paths, "mesmo_volume", lambda a, b: False)
    cfg = dataclasses.replace(sandbox.cfg, allow_cross_volume=True)
    conteudo = factories.bytes_deterministicos(4096)
    origem = factories.criar(sandbox.downloads, "setup.exe", conteudo)

    resultado = move.executar(conn, cfg, decisao_para(origem))

    assert resultado.ok is True
    assert resultado.destino.read_bytes() == conteudo
    assert not origem.exists()


def test_cross_volume_com_sha_divergente_nao_remove_origem(sandbox, conn, monkeypatch):
    """RNF-07 (b) e D3: sha divergente preserva a origem E não deixa cópia órfã."""
    monkeypatch.setattr(paths, "mesmo_volume", lambda a, b: False)
    monkeypatch.setattr(
        paths,
        "sha256_arquivo",
        lambda p, completo=False: "a" if "Downloads" in str(p) else "b",
    )
    cfg = dataclasses.replace(sandbox.cfg, allow_cross_volume=True)
    origem = factories.criar(sandbox.downloads, "setup.exe")
    destino = sandbox.caminho(rules.CAT_INSTALADORES) / "setup.exe"

    with pytest.raises(OSError):
        move.executar(conn, cfg, decisao_para(origem))

    assert origem.exists(), "o arquivo do usuário continua onde estava"
    assert not destino.exists(), "a cópia parcial não pode ficar órfã na árvore"
    assert db.listar_reservas(conn) == []
    operacao = db.journal_por_estado(conn, (move.ESTADO_FALHOU,))[0]
    assert "sha256 divergente" in operacao["erro"]


def test_sha_da_verificacao_entre_volumes_e_completo(sandbox, conn, monkeypatch):
    """D2: o hash que precede a deleção da origem nunca é o das pontas."""
    chamadas: list[bool] = []
    original = paths.sha256_arquivo

    monkeypatch.setattr(paths, "mesmo_volume", lambda a, b: False)
    monkeypatch.setattr(
        paths,
        "sha256_arquivo",
        lambda p, completo=False: (chamadas.append(completo), original(p, completo))[1],
    )
    cfg = dataclasses.replace(sandbox.cfg, allow_cross_volume=True)
    origem = factories.criar(sandbox.downloads, "setup.exe")

    resultado = move.executar(conn, cfg, decisao_para(origem))

    assert resultado.ok is True
    assert chamadas[:2] == [True, True], f"hash parcial usado antes de apagar: {chamadas}"


def test_hash_parcial_continua_valendo_para_duplicata(tmp_path, monkeypatch):
    """D2: o hash das pontas segue sendo o padrão barato para detectar duplicata."""
    monkeypatch.setattr(paths, "LIMITE_HASH_COMPLETO", 16)
    monkeypatch.setattr(paths, "_PONTA", 8)
    grande = tmp_path / "grande.bin"
    grande.write_bytes(factories.bytes_deterministicos(64))
    assert paths.sha256_arquivo(grande) != paths.sha256_arquivo(grande, completo=True)


def test_origem_inexistente(sandbox, conn):
    resultado = move.executar(conn, sandbox.cfg, decisao_para(sandbox.downloads / "fantasma.exe"))
    assert resultado.ok is False
    assert resultado.motivo == Motivo.SUMIU.value


def test_dry_run_nao_toca_no_disco(sandbox, conn):
    """RNF-08: `DRY_RUN=1` planeja, registra e não move nada."""
    cfg = dataclasses.replace(sandbox.cfg, dry_run=True)
    origem = factories.criar(sandbox.downloads, "setup.exe")

    resultado = move.executar(conn, cfg, decisao_para(origem))

    assert resultado.ok is True and resultado.dry_run is True
    assert origem.exists()
    assert not resultado.destino.exists()
    operacao = db.journal_por_id(conn, resultado.op_id)
    assert operacao["dry_run"] == 1
    assert operacao["estado"] == move.ESTADO_CONCLUIDO


def test_inbox_recebe_o_nome_preservado(sandbox, conn):
    origem = factories.criar(sandbox.downloads, "relatorio.pdf")
    decisao = decisao_para(origem, categoria=rules.CAT_OUTROS, inbox=True, conf=0.5)
    resultado = move.executar(conn, sandbox.cfg, decisao)
    assert resultado.status == move.STATUS_INBOX
    assert resultado.destino == sandbox.inbox / "relatorio.pdf"


def test_modo_interativo_usa_aguardando(sandbox, conn):
    """RF-74 (base): em `MODE=interactive` o destino é `_Inbox/_Aguardando`."""
    cfg = dataclasses.replace(sandbox.cfg, mode="interactive")
    origem = factories.criar(sandbox.downloads, "relatorio.pdf")
    decisao = decisao_para(origem, categoria=rules.CAT_OUTROS, inbox=True, conf=0.5)
    resultado = move.executar(conn, cfg, decisao)
    assert resultado.destino.parent == cfg.aguardando_dir
    assert resultado.status == move.STATUS_AGUARDANDO


def test_reservar_esgotado_levanta(sandbox, conn, monkeypatch):
    monkeypatch.setattr(paths, "MAX_CANDIDATOS", 1)
    pasta = sandbox.caminho(rules.CAT_INSTALADORES)
    factories.criar(pasta, "setup.exe")
    plano = move.Plano(pasta / "setup.exe", 1, move.STATUS_ORGANIZADO, Motivo.OK.value)
    op_id = db.journal_planejar(conn, "X:/a.exe", str(plano.base))
    with pytest.raises(move.ColisaoIrresolvivelError):
        move.reservar(conn, op_id, plano)


def test_erro_de_replace_limpa_reserva_e_marca_falhou(sandbox, conn, monkeypatch):
    origem = factories.criar(sandbox.downloads, "setup.exe")

    def explode(*args, **kwargs):
        raise OSError("disco cheio")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(OSError):
        move.executar(conn, sandbox.cfg, decisao_para(origem))

    monkeypatch.undo()
    operacao = db.journal_por_estado(conn, (move.ESTADO_FALHOU,))[0]
    assert not Path(operacao["destino"]).exists(), "a reserva de 0 byte foi removida"
    assert origem.exists()


def test_duplicata_incrementa_o_sufixo_dup(sandbox, conn):
    """O segundo duplicado vira `-dup2`, e assim por diante."""
    rules.criar_arvore(sandbox.target, sandbox.cfg.inbox_dirname)
    conteudo = factories.bytes_deterministicos(1024)
    factories.criar(sandbox.caminho(rules.CAT_INSTALADORES), "setup.exe", conteudo)
    factories.criar(sandbox.duplicados, "setup-dup1.exe", conteudo)

    origem = factories.criar(sandbox.downloads, "setup.exe", conteudo)
    resultado = move.executar(conn, sandbox.cfg, decisao_para(origem))

    assert resultado.destino.name == "setup-dup2.exe"


def test_plano_inviavel_quando_nem_o_inbox_cabe(sandbox, conn, monkeypatch):
    """RF-21: destino impossível em qualquer lugar não move nada e reporta o motivo."""
    monkeypatch.setattr(paths, "MAX_PATH", 5)
    origem = factories.criar(sandbox.downloads, "setup.exe")

    resultado = move.executar(conn, sandbox.cfg, decisao_para(origem, nome="x" * 300 + ".exe"))

    assert resultado.ok is False
    assert resultado.motivo == Motivo.PATH_MUITO_LONGO.value
    assert origem.exists()
    assert db.listar_operacoes(conn) == []


def test_corrida_por_reserva_perdida_marca_falhou(sandbox, conn, monkeypatch):
    """Se outro worker toma todos os candidatos entre o plano e a reserva."""

    def sempre_ocupado(conn_, op_id, plano):
        raise move.ColisaoIrresolvivelError(str(plano.base))

    monkeypatch.setattr(move, "reservar", sempre_ocupado)
    origem = factories.criar(sandbox.downloads, "setup.exe")

    resultado = move.executar(conn, sandbox.cfg, decisao_para(origem))

    assert resultado.ok is False
    assert resultado.motivo == Motivo.COLISAO_IRRESOLVIVEL.value
    assert db.journal_por_id(conn, resultado.op_id)["estado"] == move.ESTADO_FALHOU
    assert origem.exists()
