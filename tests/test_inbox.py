"""RF-74 a RF-79 — modo interativo e dashboard do `_Inbox`."""

from __future__ import annotations

import dataclasses

import pytest

import factories
import inbox
from organizer import db, ingest, move, rules
from organizer.queue import Motivo


@pytest.fixture
def cfg_interativo(sandbox, monkeypatch):
    """Config em `MODE=interactive`, com o `plyer` neutralizado.

    A variável de ambiente também é ajustada: em produção, `inbox.py` chama
    `config.get_config()`, então os testes de CLI precisam ver o **mesmo** modo
    que criou a linha `aguardando_aprovacao`. Produzir o estado com uma cfg e
    consumi-lo com outra é uma combinação que não existe em produção — foi
    exatamente essa lacuna que escondeu a regressão da RF-76.
    """
    from organizer import config, notify

    monkeypatch.setattr(notify, "notificar", lambda titulo, msg: True)
    monkeypatch.setenv("MODE", "interactive")
    config.get_config.cache_clear()
    cfg = config.get_config()
    assert cfg.mode == "interactive"
    return cfg


def _aguardando(sandbox, conn, cfg, nome="relatorio.pdf", conteudo=None):
    """Processa um arquivo em modo interativo e devolve a operação parada."""
    origem = factories.criar(sandbox.downloads, nome, conteudo)
    assert ingest.processar(origem, cfg=cfg, conn=conn) == ingest.EXIT_OK
    pendentes = db.operacoes_aguardando(conn)
    return pendentes[-1] if pendentes else None


def test_modo_interativo_aguarda_aprovacao(sandbox, conn, cfg_interativo):
    """RF-74: nada vai direto ao destino final; o plano fica no banco."""
    origem = factories.criar(sandbox.downloads, "relatorio.pdf")

    assert ingest.processar(origem, cfg=cfg_interativo, conn=conn) == ingest.EXIT_OK

    assert not origem.exists(), "o arquivo tem de sair do Downloads"
    aguardando = cfg_interativo.aguardando_dir / "relatorio.pdf"
    assert aguardando.is_file()
    assert not (sandbox.caminho(rules.CAT_OUTROS) / "relatorio.pdf").exists()

    operacoes = db.operacoes_aguardando(conn)
    assert len(operacoes) == 1
    assert operacoes[0]["estado"] == move.ESTADO_AGUARDANDO
    assert db.buscar_por_path(conn, str(aguardando))["status"] == "aguardando"


#: Cenário da auditoria: os dois casos de **alta** confiança, que antes iam
#: direto ao destino final por `para_inbox` ser falso.
ALTA_CONFIANCA = [
    ("InstaladorApp.exe", rules.CAT_INSTALADORES, 0.95, "instaladorapp.exe"),
    ("nota-fiscal-2026-05.pdf", rules.CAT_NOTAS_FISCAIS, 0.85, "nota-fiscal-2026-05.pdf"),
]


@pytest.mark.parametrize("nome,categoria,confianca,nome_final", ALTA_CONFIANCA)
def test_alta_confianca_tambem_aguarda_aprovacao(
    sandbox, conn, cfg_interativo, nome, categoria, confianca, nome_final
):
    """RF-74: em `MODE=interactive`, **nenhum** arquivo vai direto ao destino.

    Regressão do bug da auditoria: o desvio para `_Aguardando` dependia de
    `decisao.para_inbox`, que só é verdadeiro abaixo de `CONFIDENCE_MIN` — ou
    seja, a maioria dos arquivos (a "regra dos 90%") escapava do modo interativo.
    """
    from organizer import classify

    origem = factories.criar(sandbox.downloads, nome)
    decisao = classify.classificar(origem, cfg_interativo)
    assert decisao.confianca == pytest.approx(confianca), "o caso precisa ser de alta confiança"
    assert decisao.para_inbox is False, "e NÃO pode ser um caso de quarentena"

    assert ingest.processar(origem, cfg=cfg_interativo, conn=conn) == ingest.EXIT_OK

    destino_final = sandbox.caminho(categoria) / nome_final
    assert not destino_final.exists(), "foi direto ao destino final — RF-74 violada"
    assert not origem.exists(), "mas também não pode ficar no Downloads"

    aguardando = cfg_interativo.aguardando_dir / nome_final
    assert aguardando.is_file()

    operacoes = db.operacoes_aguardando(conn)
    assert len(operacoes) == 1
    assert operacoes[0]["estado"] == move.ESTADO_AGUARDANDO
    assert operacoes[0]["categoria"] == categoria
    assert db.buscar_por_path(conn, str(aguardando))["status"] == "aguardando"


@pytest.mark.parametrize("nome,categoria,confianca,nome_final", ALTA_CONFIANCA)
def test_alta_confianca_so_sai_do_aguardando_por_aprovacao(
    sandbox, conn, cfg_interativo, nome, categoria, confianca, nome_final
):
    """RF-74 + RF-76: aprovar leva ao destino certo; o comportamento não mudou."""
    origem = factories.criar(sandbox.downloads, nome)
    ingest.processar(origem, cfg=cfg_interativo, conn=conn)
    operacao = db.operacoes_aguardando(conn)[0]

    resultado = inbox.aprovar(conn, cfg_interativo, int(operacao["id"]))

    assert resultado is not None and resultado.ok
    assert resultado.destino == sandbox.caminho(categoria) / nome_final
    assert resultado.destino.is_file()
    assert not (cfg_interativo.aguardando_dir / nome_final).exists()
    assert db.buscar_por_path(conn, str(resultado.destino))["status"] == "organizado"
    assert db.operacoes_aguardando(conn) == []


def test_modo_auto_nao_pede_aprovacao(sandbox, conn):
    """O outro lado: em `MODE=auto` nada muda — alta confiança vai direto."""
    origem = factories.criar(sandbox.downloads, "InstaladorApp.exe")

    assert ingest.processar(origem, cfg=sandbox.cfg, conn=conn) == ingest.EXIT_OK

    assert (sandbox.caminho(rules.CAT_INSTALADORES) / "instaladorapp.exe").is_file()
    assert db.operacoes_aguardando(conn) == []


def test_notificacao_e_disparada(sandbox, conn, monkeypatch):
    """RF-73/RF-74: o toast avisa, mas a fila funciona mesmo sem ele."""
    from organizer import notify

    disparadas = []
    monkeypatch.setattr(notify, "notificar", lambda t, m: disparadas.append((t, m)) or True)
    cfg = dataclasses.replace(sandbox.cfg, mode="interactive")

    ingest.processar(
        factories.criar(sandbox.downloads, "relatorio.pdf"), cfg=cfg, conn=conn
    )

    assert disparadas and "relatorio.pdf" in disparadas[0][1]


def test_listagem(sandbox, conn, cfg_interativo):
    """RF-75: id, nome, destino proposto, confiança e motivo."""
    _aguardando(sandbox, conn, cfg_interativo)

    itens = inbox.listar(conn)

    assert len(itens) == 1
    item = itens[0]
    assert item["id"] and item["categoria"] and item["confianca"] is not None
    tabela = inbox._tabela(itens, cfg_interativo)
    assert [c.header for c in tabela.columns] == [
        "id",
        "nome",
        "destino proposto",
        "confiança",
        "motivo",
    ]
    assert tabela.row_count == 1


def test_aprovar_move(sandbox, conn, cfg_interativo):
    """RF-76: usa o mesmo `move.executar` — mesmo journal, mesma colisão."""
    operacao = _aguardando(sandbox, conn, cfg_interativo)
    aguardando = cfg_interativo.aguardando_dir / "relatorio.pdf"
    assert aguardando.is_file()

    resultado = inbox.aprovar(conn, cfg_interativo, int(operacao["id"]))

    assert resultado is not None and resultado.ok
    assert resultado.destino == sandbox.caminho(rules.CAT_OUTROS) / "relatorio.pdf"
    assert resultado.destino.is_file()
    assert not aguardando.exists()

    assert db.journal_por_id(conn, int(operacao["id"]))["estado"] == move.ESTADO_CONCLUIDO
    assert db.journal_por_id(conn, resultado.op_id)["estado"] == move.ESTADO_CONCLUIDO
    linha = db.buscar_por_path(conn, str(resultado.destino))
    assert linha["status"] == "organizado"
    assert db.buscar_por_path(conn, str(aguardando)) is None
    assert db.operacoes_aguardando(conn) == []


def test_aprovar_aplica_politica_de_colisao(sandbox, conn, cfg_interativo):
    """RF-76: aprovar não sobrescreve nada — o sufixo `-2` continua valendo."""
    rules.criar_arvore(sandbox.target, cfg_interativo.inbox_dirname)
    factories.criar(
        sandbox.caminho(rules.CAT_OUTROS), "relatorio.pdf", factories.bytes_deterministicos(64, 1)
    )
    operacao = _aguardando(
        sandbox, conn, cfg_interativo, conteudo=factories.bytes_deterministicos(64, 2)
    )

    resultado = inbox.aprovar(conn, cfg_interativo, int(operacao["id"]))

    assert resultado.destino.name == "relatorio-2.pdf"
    assert (sandbox.caminho(rules.CAT_OUTROS) / "relatorio.pdf").read_bytes() == (
        factories.bytes_deterministicos(64, 1)
    )


def test_rejeitar(sandbox, conn, cfg_interativo):
    """RF-77: mantém o arquivo no `_Inbox` e marca `abortado`. Nada é deletado."""
    operacao = _aguardando(sandbox, conn, cfg_interativo)
    aguardando = cfg_interativo.aguardando_dir / "relatorio.pdf"

    assert inbox.rejeitar(conn, int(operacao["id"])) is True

    assert aguardando.is_file(), "o arquivo continua no _Inbox"
    assert db.journal_por_id(conn, int(operacao["id"]))["estado"] == move.ESTADO_ABORTADO
    assert db.operacoes_aguardando(conn) == []
    assert not (sandbox.caminho(rules.CAT_OUTROS) / "relatorio.pdf").exists()
    assert inbox.rejeitar(conn, 99999) is False


def test_aprovar_em_lote(sandbox, conn, cfg_interativo, monkeypatch):
    """RF-78: `--aprovar-todos --acima 0.85` só pega o que passa do limiar."""
    ops = []
    for nome, confianca in (("alta.pdf", 0.92), ("baixa.pdf", 0.55)):
        operacao = _aguardando(sandbox, conn, cfg_interativo, nome=nome)
        conn.execute(
            "UPDATE operacoes SET confianca = ? WHERE id = ?", (confianca, operacao["id"])
        )
        ops.append(int(operacao["id"]))

    aprovados = inbox.aprovar_em_lote(conn, cfg_interativo, acima=0.85)

    assert aprovados == [ops[0]]
    assert (sandbox.caminho(rules.CAT_OUTROS) / "alta.pdf").is_file()
    assert (cfg_interativo.aguardando_dir / "baixa.pdf").is_file()
    assert [int(o["id"]) for o in db.operacoes_aguardando(conn)] == [ops[1]]


def test_plano_persistente(sandbox, conn, cfg_interativo):
    """RF-79: o plano está no banco, não em memória — sobrevive a reinício."""
    operacao = _aguardando(sandbox, conn, cfg_interativo)
    antes = [dict(o) for o in inbox.listar(conn)]
    conn.close()

    nova = db.abrir(sandbox.db_path)
    try:
        depois = [dict(o) for o in inbox.listar(nova)]
        assert depois == antes
        assert depois[0]["id"] == operacao["id"]
        # e ainda dá para aprovar depois do "reinício"
        assert inbox.aprovar(nova, cfg_interativo, int(operacao["id"])).ok
    finally:
        nova.close()


def test_aprovar_item_inexistente_ou_ja_resolvido(sandbox, conn, cfg_interativo):
    assert inbox.aprovar(conn, cfg_interativo, 99999) is None
    operacao = _aguardando(sandbox, conn, cfg_interativo)
    inbox.rejeitar(conn, int(operacao["id"]))
    assert inbox.aprovar(conn, cfg_interativo, int(operacao["id"])) is None


def test_aprovar_arquivo_que_sumiu(sandbox, conn, cfg_interativo):
    """O usuário mexeu no arquivo: aborta o plano em vez de explodir."""
    operacao = _aguardando(sandbox, conn, cfg_interativo)
    (cfg_interativo.aguardando_dir / "relatorio.pdf").unlink()

    assert inbox.aprovar(conn, cfg_interativo, int(operacao["id"])) is None
    linha = db.journal_por_id(conn, int(operacao["id"]))
    assert linha["estado"] == move.ESTADO_ABORTADO
    assert linha["erro"] == Motivo.SUMIU.value


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_lista_vazia(sandbox, capsys):
    assert inbox.main([]) == 0
    assert "nada aguardando" in capsys.readouterr().out


def test_cli_aprova_e_rejeita(sandbox, conn, cfg_interativo, capsys):
    operacao = _aguardando(sandbox, conn, cfg_interativo)
    conn.close()

    assert inbox.main([]) == 0
    assert "aguardando aprova" in capsys.readouterr().out

    assert inbox.main(["--aprovar", str(operacao["id"])]) == 0
    assert "movido para" in capsys.readouterr().out

    assert inbox.main(["--aprovar", str(operacao["id"])]) == 1
    assert inbox.main(["--rejeitar", "99999"]) == 1


def test_cli_lote(sandbox, conn, cfg_interativo, capsys):
    _aguardando(sandbox, conn, cfg_interativo, nome="alta.pdf")
    conn.execute("UPDATE operacoes SET confianca = 0.95")
    conn.close()

    assert inbox.main(["--aprovar-todos", "--acima", "0.85"]) == 0
    assert "1 item(ns) aprovado" in capsys.readouterr().out
    assert inbox.main(["--aprovar-todos"]) == 1
