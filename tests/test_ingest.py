"""RF-11, RF-24 a RF-26, RF-31, RF-37, RF-42, RNF-08, RNF-13, RNF-14, RNF-15.

Inclui o teste end-to-end da Fase 1: arquivos sintéticos numa pasta Downloads
falsa entram, saem organizados na árvore falsa e aparecem no SQLite.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from enum import Enum
from pathlib import Path

import pytest

import factories
from organizer import classify, db, guard, ingest, llm, rules
from organizer.queue import Motivo

RAIZ_PROJETO = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# End-to-end da Fase 1
# --------------------------------------------------------------------------- #

CENARIO_E2E = [
    ("DiscordSetup-2024-11.exe", rules.CAT_INSTALADORES, "discordsetup-2024-11.exe"),
    ("Instalador-Firefox.zip", rules.CAT_INSTALADORES, "instalador-firefox.zip"),
    ("ferias.jpg", rules.CAT_FOTOS, "ferias.jpg"),
    ("Screenshot 2026-02-14.png", rules.CAT_SCREENSHOTS, "screenshot-2026-02-14.png"),
    ("aula-magna.mp4", rules.CAT_VIDEOS, "aula-magna.mp4"),
    ("bach-suite.mp3", rules.CAT_MUSICA, "bach-suite.mp3"),
    ("nota-fiscal-2026-05.pdf", rules.CAT_NOTAS_FISCAIS, "nota-fiscal-2026-05.pdf"),
    ("matriz-curricular-ec-2026.pdf", rules.CAT_MATRIZES, "matriz-curricular-ec-2026.pdf"),
    ("contrato-estagio-2025-03.pdf", rules.CAT_CONTRATOS, "contrato-estagio-2025-03.pdf"),
]


def test_pipeline_completo_da_fase1(sandbox, conn):
    """Ciclo completo: detectar, classificar por extensão, mover e indexar."""
    origens = {nome: factories.criar(sandbox.downloads, nome) for nome, _, _ in CENARIO_E2E}

    for nome in origens:
        assert ingest.processar(origens[nome], cfg=sandbox.cfg, conn=conn) == ingest.EXIT_OK

    for nome, categoria, nome_final in CENARIO_E2E:
        destino = sandbox.caminho(categoria) / nome_final
        assert destino.is_file(), f"{nome} deveria estar em {destino}"
        assert not origens[nome].exists(), f"{nome} continuou no Downloads falso"

        linha = db.buscar_por_path(conn, str(destino))
        assert linha is not None, f"{nome} não foi indexado"
        assert linha["nome_orig"] == nome
        assert linha["nome_atual"] == nome_final
        assert linha["status"] == "organizado"
        assert linha["via"] == classify.VIA_EXTENSAO
        assert linha["confianca"] >= sandbox.cfg.confidence_min
        assert linha["tipo"] and linha["subtipo"]
        assert linha["sha256"] and linha["tamanho"] > 0

    assert db.contar_arquivos(conn) == len(CENARIO_E2E)
    assert db.contar_pendentes(conn) == 0
    assert db.locks_ativos(conn) == []
    assert list(sandbox.downloads.iterdir()) == []


def test_indexacao_completa(sandbox, conn):
    """RF-24: todos os campos exigidos são gravados."""
    origem = factories.criar(sandbox.downloads, "setup.exe")
    ingest.processar(origem, cfg=sandbox.cfg, conn=conn)
    linha = db.listar_arquivos(conn)[0]
    for campo in ("nome_orig", "nome_atual", "path", "tipo", "subtipo", "via", "confianca", "status"):
        assert linha[campo] is not None, f"campo {campo} vazio"
    assert linha["path_orig"] == str(origem)


def test_indexacao_idempotente(sandbox, conn):
    """RF-25: `ingest.processar` rodando duas vezes no mesmo path conta 1 linha.

    A segunda execução é um `ingest.processar` de verdade (não um `inserir_arquivo`
    manual): o `V:` do requisito pede "roda duas vezes".
    """
    origem = factories.criar(sandbox.downloads, "setup.exe")
    conteudo = origem.read_bytes()

    assert ingest.processar(origem, cfg=sandbox.cfg, conn=conn) == ingest.EXIT_OK
    destino = sandbox.caminho(rules.CAT_INSTALADORES) / "setup.exe"
    assert db.contar_arquivos(conn) == 1
    primeiro_id = db.buscar_por_path(conn, str(destino))["id"]

    # o mesmo arquivo cai de novo no Downloads, byte a byte idêntico
    origem_de_novo = factories.criar(sandbox.downloads, "setup.exe", conteudo)
    assert ingest.processar(origem_de_novo, cfg=sandbox.cfg, conn=conn) == ingest.EXIT_OK

    assert db.buscar_por_path(conn, str(destino))["id"] == primeiro_id
    assert destino.read_bytes() == conteudo, "o original nunca é sobrescrito"

    # a segunda cópia foi reconhecida como duplicata, não duplicou a linha do destino
    duplicado = sandbox.duplicados / "setup-dup1.exe"
    assert duplicado.is_file()
    assert db.contar_arquivos(conn) == 2
    assert db.buscar_por_path(conn, str(duplicado))["status"] == "duplicado"

    # e reindexar o mesmo path uma terceira vez continua não duplicando
    ingest.processar(destino, cfg=sandbox.cfg, conn=conn)
    assert db.contar_arquivos(conn) == 2


def test_baixa_confianca_vai_para_inbox(sandbox, conn, caplog):
    """RF-26: abaixo do mínimo vai para o `_Inbox`, com nome preservado, e sai do Downloads."""
    origem = factories.criar(sandbox.downloads, "relatorio.pdf")
    assert ingest.processar(origem, cfg=sandbox.cfg, conn=conn) == ingest.EXIT_OK

    destino = sandbox.inbox / "relatorio.pdf"
    assert destino.is_file()
    assert not origem.exists()
    linha = db.buscar_por_path(conn, str(destino))
    assert linha["status"] == "inbox"
    # `relatorio.pdf` é documento de texto com nome genérico: o caminho do LLM é
    # tentado e, sem Ollama, o motivo é exatamente `llm_indisponivel`
    assert linha["motivo"] == Motivo.LLM_INDISPONIVEL.value
    assert linha["confianca"] < sandbox.cfg.confidence_min


def test_baixa_confianca_sem_caminho_de_llm(sandbox, conn):
    """RF-26: quando o LLM nem chega a ser cogitado, o motivo é `baixa_confianca`.

    Um `.7z` de nome genérico não tem texto a extrair, então não aciona o LLM —
    e precisa registrar um motivo diferente de `llm_indisponivel`.
    """
    origem = factories.criar(sandbox.downloads, "download.7z")
    assert ingest.processar(origem, cfg=sandbox.cfg, conn=conn) == ingest.EXIT_OK

    destino = sandbox.inbox / "download.7z"
    assert destino.is_file()
    assert db.buscar_por_path(conn, str(destino))["motivo"] == Motivo.BAIXA_CONFIANCA.value


def test_diretorio_e_ignorado(sandbox, conn):
    """RF-11: diretórios nunca são movidos nem renomeados."""
    pasta = sandbox.downloads / "Cursos"
    pasta.mkdir()
    interno = factories.criar(pasta, "aula.pdf")

    assert ingest.processar(pasta, cfg=sandbox.cfg, conn=conn) == ingest.EXIT_OK

    assert pasta.is_dir()
    assert interno.is_file()
    assert db.contar_arquivos(conn) == 0
    assert db.contar_pendentes(conn) == 0


def test_ignora_path_dentro_do_target(sandbox, conn):
    """RF-31: nada dentro de `TARGET_ROOT` é processado (anti-loop)."""
    rules.criar_arvore(sandbox.target, sandbox.cfg.inbox_dirname)
    dentro = factories.criar(sandbox.inbox, "relatorio.pdf")

    assert ingest.processar(dentro, cfg=sandbox.cfg, conn=conn) == ingest.EXIT_OK

    assert dentro.exists()
    assert db.contar_arquivos(conn) == 0
    assert db.contar_pendentes(conn) == 0


def test_arquivo_parcial_e_ignorado(sandbox, conn):
    """RF-12: parciais não entram em nenhuma fila."""
    parcial = factories.criar(sandbox.downloads, "filme.mp4.crdownload")
    assert ingest.processar(parcial, cfg=sandbox.cfg, conn=conn) == ingest.EXIT_OK
    assert parcial.exists()
    assert db.contar_pendentes(conn) == 0
    assert db.contar_arquivos(conn) == 0


def test_arquivo_sumiu_sai_em_silencio(sandbox, conn):
    assert ingest.processar(sandbox.downloads / "fantasma.pdf", cfg=sandbox.cfg, conn=conn) == 0


def test_ocupado_vai_para_pendentes(sandbox, conn, monkeypatch):
    """RF-37: sistema ocupado enfileira com `retry_after = now + RETRY_BUSY_MINUTES`."""
    monkeypatch.setattr(guard, "_ler_cpu_ram", lambda intervalo=1.0: (99.0, 10.0))
    cfg = dataclasses.replace(sandbox.cfg, threshold_cpu=70.0)
    origem = factories.criar(sandbox.downloads, "setup.exe")
    agora = db.agora()

    codigo = ingest.processar(origem, cfg=cfg, conn=conn, intervalo_cpu=0)

    assert codigo == ingest.EXIT_ADIADO
    linha = db.pendente_por_path(conn, str(origem))
    assert linha["motivo"] == Motivo.OCUPADO.value
    assert linha["tentativas"] == 1
    assert (
        db.ts_mais(cfg.retry_busy_minutes - 1, agora)
        <= linha["retry_after"]
        <= db.ts_mais(cfg.retry_busy_minutes + 1, agora)
    )
    assert origem.exists(), "arquivo não pode ser movido quando o sistema está ocupado"
    assert db.contar_arquivos(conn) == 0


def test_sistema_livre_processa(sandbox, conn, monkeypatch):
    """RF-37 (o outro lado): com o sistema livre, o arquivo é processado na hora."""
    monkeypatch.setattr(guard, "_ler_cpu_ram", lambda intervalo=1.0: (5.0, 5.0))
    cfg = dataclasses.replace(sandbox.cfg, threshold_cpu=70.0)
    origem = factories.criar(sandbox.downloads, "setup.exe")

    assert ingest.processar(origem, cfg=cfg, conn=conn, intervalo_cpu=0) == ingest.EXIT_OK
    assert (sandbox.caminho(rules.CAT_INSTALADORES) / "setup.exe").is_file()
    assert db.contar_pendentes(conn) == 0


def test_startup_ocupado_usa_retry_curto(sandbox, conn, monkeypatch):
    """RF-40: na varredura de startup o atraso é `RETRY_STARTUP_MINUTES` (30)."""
    monkeypatch.setattr(guard, "_ler_cpu_ram", lambda intervalo=1.0: (99.0, 10.0))
    cfg = dataclasses.replace(sandbox.cfg, threshold_cpu=70.0)
    origem = factories.criar(sandbox.downloads, "setup.exe")
    agora = db.agora()

    ingest.processar(origem, cfg=cfg, conn=conn, motivo_origem=Motivo.STARTUP, intervalo_cpu=0)

    linha = db.pendente_por_path(conn, str(origem))
    assert linha["motivo"] == Motivo.STARTUP.value
    assert (
        db.ts_mais(cfg.retry_startup_minutes - 1, agora)
        <= linha["retry_after"]
        <= db.ts_mais(cfg.retry_startup_minutes + 1, agora)
    )


def test_teto_de_tentativas_manda_para_inbox(sandbox, conn):
    """RF-42: passado o teto, o arquivo sai da fila e vai para o `_Inbox`."""
    origem = factories.criar(sandbox.downloads, "setup.exe")
    db.inserir_pendente(conn, str(origem), db.ts(), Motivo.OCUPADO.value)
    conn.execute(
        "UPDATE pendentes SET tentativas = ? WHERE path = ?",
        (sandbox.cfg.max_tentativas + 1, str(origem)),
    )

    assert ingest.processar(origem, cfg=sandbox.cfg, conn=conn) == ingest.EXIT_OK

    destino = sandbox.inbox / "setup.exe"
    assert destino.is_file()
    assert db.pendente_por_path(conn, str(origem)) is None
    assert db.buscar_por_path(conn, str(destino))["motivo"] == Motivo.MAX_TENTATIVAS.value


def test_dry_run_nao_move(sandbox, conn, caplog):
    """RNF-08: com `DRY_RUN=1` o arquivo fica onde está e o plano é logado."""
    cfg = dataclasses.replace(sandbox.cfg, dry_run=True)
    origem = factories.criar(sandbox.downloads, "setup.exe")

    with caplog.at_level(logging.INFO, logger="organizer.ingest"):
        assert ingest.processar(origem, cfg=cfg, conn=conn) == ingest.EXIT_OK

    assert origem.exists()
    assert not (sandbox.caminho(rules.CAT_INSTALADORES) / "setup.exe").exists()
    assert db.contar_arquivos(conn) == 0
    operacao = db.listar_operacoes(conn)[0]
    assert operacao["dry_run"] == 1
    assert "decision=planejado" in caplog.text
    assert "Instaladores" in caplog.text


def test_caminho_rapido_sem_llm(sandbox, conn, monkeypatch):
    """RNF-13: extensão inequívoca é organizada sem invocar o Ollama.

    Contar zero chamadas ao LLM não basta: um arquivo mandado para o `_Inbox`
    também não chama o LLM. O teste exige o destino final correto.
    """
    chamadas: list[str] = []
    monkeypatch.setattr(llm, "classificar", lambda *a, **k: chamadas.append("llm") or None)

    esperado = [
        ("setup.exe", rules.CAT_INSTALADORES),
        ("ferias.jpg", rules.CAT_FOTOS),
        ("aula.mp4", rules.CAT_VIDEOS),
        ("instalador-portatil.zip", rules.CAT_INSTALADORES),
    ]
    for nome, categoria in esperado:
        origem = factories.criar(sandbox.downloads, nome)
        assert ingest.processar(origem, cfg=sandbox.cfg, conn=conn) == ingest.EXIT_OK

        destino = sandbox.caminho(categoria) / nome
        assert destino.is_file(), f"{nome} não foi para {categoria}"
        linha = db.buscar_por_path(conn, str(destino))
        assert linha["status"] == "organizado", f"{nome} caiu no _Inbox"
        assert linha["via"] == classify.VIA_EXTENSAO
        assert linha["confianca"] >= sandbox.cfg.confidence_min

    assert chamadas == []
    assert not any(sandbox.inbox.glob("*.*")), "nada deveria ter ido para o _Inbox"


def test_formato_do_log(sandbox, conn, caplog):
    """RNF-14: uma linha, com `decision= path= dest= conf= via= motivo=`."""
    origem = factories.criar(sandbox.downloads, "setup.exe")
    with caplog.at_level(logging.INFO, logger="organizer.ingest"):
        ingest.processar(origem, cfg=sandbox.cfg, conn=conn)

    linhas = [m for m in caplog.messages if m.startswith("decision=")]
    assert linhas, caplog.text
    padrao = re.compile(r"^decision=\S+ path=\S+ dest=\S+ conf=\S+ via=\S+ motivo=\S+$")
    assert padrao.match(linhas[-1]), linhas[-1]
    assert "\n" not in linhas[-1]


def test_motivos_sao_enum():
    """RNF-15: `ingest.Motivo` é o Enum único e todo valor é string."""
    assert issubclass(ingest.Motivo, Enum)
    from organizer import queue as fila

    # o Enum é declarado em queue.py e reexportado por ingest.py (RNF-15)
    assert ingest.Motivo is Motivo is fila.Motivo
    valores = [m.value for m in ingest.Motivo]
    assert len(valores) == len(set(valores))
    assert all(isinstance(v, str) and v == v.lower() for v in valores)


def test_excecao_inesperada_vira_pendente(sandbox, conn, monkeypatch):
    """ARQUITETURA §18: nenhuma exceção escapa do worker."""

    def explode(*args, **kwargs):
        raise RuntimeError("falha simulada")

    monkeypatch.setattr(classify, "classificar", explode)
    origem = factories.criar(sandbox.downloads, "setup.exe")

    assert ingest.processar(origem, cfg=sandbox.cfg, conn=conn) == ingest.EXIT_ERRO

    linha = db.pendente_por_path(conn, str(origem))
    assert linha["motivo"] == Motivo.ERRO_INESPERADO.value
    assert "falha simulada" in linha["ultimo_erro"]
    assert origem.exists()


def test_cross_volume_vira_pendente(sandbox, conn, monkeypatch):
    from organizer import paths

    monkeypatch.setattr(paths, "mesmo_volume", lambda a, b: False)
    origem = factories.criar(sandbox.downloads, "setup.exe")

    assert ingest.processar(origem, cfg=sandbox.cfg, conn=conn) == ingest.EXIT_ADIADO
    assert db.pendente_por_path(conn, str(origem))["motivo"] == Motivo.CROSS_VOLUME.value
    assert origem.exists()


def test_duplicata_registra_duplicado_de(sandbox, conn):
    """RF-22: duplicata exata vai para `_Duplicados` com `duplicado_de` preenchido."""
    conteudo = factories.bytes_deterministicos(2048)
    primeiro = factories.criar(sandbox.downloads, "setup.exe", conteudo)
    ingest.processar(primeiro, cfg=sandbox.cfg, conn=conn)
    segundo = factories.criar(sandbox.downloads, "setup.exe", conteudo)
    ingest.processar(segundo, cfg=sandbox.cfg, conn=conn)

    duplicado = sandbox.duplicados / "setup-dup1.exe"
    assert duplicado.is_file()
    linha = db.buscar_por_path(conn, str(duplicado))
    assert linha["status"] == "duplicado"
    assert linha["duplicado_de"] == str(sandbox.caminho(rules.CAT_INSTALADORES) / "setup.exe")


# --------------------------------------------------------------------------- #
# Fase 3 — o pipeline inteiro com o Ollama falso
# --------------------------------------------------------------------------- #


def test_documento_generico_e_organizado_pelo_llm(sandbox, conn, ollama_falso):
    """RF-47 + RF-57 + RF-58: `document.pdf` sai do Downloads renomeado e no lugar certo."""
    from organizer import config

    config.get_config.cache_clear()
    cfg = config.get_config()
    origem = factories.criar(
        sandbox.downloads, "document.pdf", factories.pdf_minimo("matriz curricular unifesp " * 25)
    )

    assert ingest.processar(origem, cfg=cfg, conn=conn) == ingest.EXIT_OK

    destino = (
        sandbox.caminho(rules.CAT_MATRIZES)
        / "matriz-curricular-engenharia-computacao-2026.pdf"
    )
    assert destino.is_file()
    assert not origem.exists()
    linha = db.buscar_por_path(conn, str(destino))
    assert linha["via"] == classify.VIA_LLM
    assert linha["status"] == "organizado"
    assert linha["nome_orig"] == "document.pdf"
    assert linha["texto_amostra"] and "matriz curricular" in linha["texto_amostra"]


def test_llm_timeout_esgotado_vai_para_inbox(sandbox, conn, ollama_falso, monkeypatch):
    """RF-59: esgotadas as tentativas, o arquivo vai para o `_Inbox` com o motivo certo."""
    from organizer import config

    monkeypatch.setenv("LLM_TIMEOUT", "1")
    config.get_config.cache_clear()
    cfg = config.get_config()
    ollama_falso.modo("dorme", FAKE_OLLAMA_SONO="20")
    origem = factories.criar(sandbox.downloads, "document.pdf", factories.pdf_minimo())

    assert ingest.processar(origem, cfg=cfg, conn=conn) == ingest.EXIT_OK

    destino = sandbox.inbox / "document.pdf"
    assert destino.is_file(), "o arquivo tem de sair do Downloads mesmo assim"
    assert db.buscar_por_path(conn, str(destino))["motivo"] == Motivo.LLM_TIMEOUT.value


def test_llm_com_lixo_vai_para_inbox(sandbox, conn, ollama_falso):
    """RF-55 nível 5: parser esgotado → `_Inbox` com `llm_parse_error`."""
    from organizer import config

    ollama_falso.modo("lixo")
    config.get_config.cache_clear()
    cfg = config.get_config()
    origem = factories.criar(sandbox.downloads, "document.pdf", factories.pdf_minimo())

    assert ingest.processar(origem, cfg=cfg, conn=conn) == ingest.EXIT_OK

    destino = sandbox.inbox / "document.pdf"
    assert destino.is_file()
    assert db.buscar_por_path(conn, str(destino))["motivo"] == Motivo.LLM_PARSE_ERROR.value


def test_deve_ignorar(sandbox):
    assert ingest.deve_ignorar(sandbox.downloads, sandbox.cfg) == Motivo.DIRETORIO.value
    assert (
        ingest.deve_ignorar(sandbox.downloads / "a.crdownload", sandbox.cfg)
        == Motivo.PARCIAL.value
    )
    assert (
        ingest.deve_ignorar(sandbox.target / "x.pdf", sandbox.cfg)
        == Motivo.DENTRO_DO_TARGET.value
    )
    assert ingest.deve_ignorar(sandbox.downloads / "ok.pdf", sandbox.cfg) is None
