"""RF-51 a RF-56, RF-59, RF-62 — fronteira com o Ollama.

Todos os testes usam o dublê de `tests/fake_ollama.py`: a suíte tem de passar
numa máquina sem Ollama nenhum (RNF-05).
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path

import pytest

import factories
from organizer import config, db, llm, rules
from organizer.queue import Motivo

RAIZ_PROJETO = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg(sandbox, ollama_falso):
    """Config já apontando para o Ollama falso."""
    config.get_config.cache_clear()
    return config.get_config()


@pytest.fixture
def alvo(sandbox):
    return factories.criar(sandbox.downloads, "document.pdf", factories.pdf_minimo())


# --------------------------------------------------------------------------- #
# RF-51 / RF-52 — processo efêmero, prompt por stdin
# --------------------------------------------------------------------------- #


def test_processo_encerra(cfg, alvo, ollama_falso):
    """RF-51: o Ollama é chamado como subprocess que morre ao terminar."""
    import psutil

    antes = {p.pid for p in psutil.process_iter()}
    resposta = llm.classificar(alvo, cfg)
    depois = {p.pid for p in psutil.process_iter()}

    assert resposta is not None
    novos_vivos = [
        p for p in psutil.process_iter(["name"]) if p.pid in (depois - antes)
    ]
    nomes = {(p.info["name"] or "").lower() for p in novos_vivos}
    assert "ollama-falso.cmd" not in nomes
    assert not any("fake_ollama" in n for n in nomes)


def test_nenhum_servidor_e_mantido_vivo():
    """RF-51: `llm.py` não sobe `ollama serve` nem guarda um Popen persistente."""
    fonte = (RAIZ_PROJETO / "organizer" / "llm.py").read_text(encoding="utf-8")
    assert "subprocess.run(" in fonte
    assert "serve" not in fonte
    assert "Popen" not in fonte


def test_prompt_por_stdin(cfg, alvo, ollama_falso):
    """RF-52: o texto do prompt não aparece em argv — só em stdin."""
    ollama_falso.limpar()
    llm.classificar(alvo, cfg, texto="matriz curricular da unifesp 2026")

    chamadas = [c for c in ollama_falso.chamadas if c["argv"][:1] != ["list"]]
    assert chamadas, "o dublê não foi chamado"
    chamada = chamadas[-1]

    assert chamada["argv"] == ["run", "phi3:mini", *llm.FLAGS_EXTRAS]
    argv_inteiro = " ".join(chamada["argv"])
    assert "matriz curricular" not in argv_inteiro
    assert "document.pdf" not in argv_inteiro
    # e o prompt chegou inteiro por stdin
    assert "matriz curricular da unifesp 2026" in chamada["prompt"]
    assert "document.pdf" in chamada["prompt"]


def test_argumentos_nao_carregam_o_prompt(cfg):
    """RF-52: a montagem de argv é auditável e não tem espaço para o prompt."""
    assert llm.argumentos(cfg) == [cfg.ollama_bin, "run", cfg.ollama_model, *llm.FLAGS_EXTRAS]
    assert llm.argumentos(cfg, com_extras=False) == [cfg.ollama_bin, "run", cfg.ollama_model]
    assert "--nowordwrap" in llm.FLAGS_EXTRAS


def test_prompt_lista_as_categorias_canonicas(cfg, alvo):
    """O enum fechado vai no prompt, uma categoria por linha (ARQUITETURA §9)."""
    prompt = llm.montar_prompt(alvo, "texto qualquer")
    for categoria in rules.CATEGORIAS:
        assert categoria in prompt
    assert "document.pdf" in prompt
    assert ".pdf" in prompt
    assert "JSON" in prompt


def test_prompt_sem_texto_extraido(cfg, alvo):
    assert llm.SEM_TEXTO in llm.montar_prompt(alvo, None)
    assert llm.SEM_TEXTO in llm.montar_prompt(alvo, "   ")


def test_prompt_de_retry_encurta_o_trecho(cfg, alvo):
    prompt = llm.montar_prompt(alvo, "x" * 500)
    retry = llm.prompt_de_retry(prompt)
    linha = [l for l in retry.splitlines() if l.startswith("trecho: ")][0]
    assert len(linha) <= len("trecho: ") + llm.CHARS_RETRY
    assert "Responda SOMENTE o JSON" in retry


# --------------------------------------------------------------------------- #
# RF-53 / RF-54 — disponibilidade e cache
# --------------------------------------------------------------------------- #


def test_ollama_ausente(sandbox, monkeypatch, caplog):
    """RF-53: binário ausente → `disponivel()` False, WARNING único, nada quebra."""
    monkeypatch.setenv("OLLAMA_BIN", "ollama-que-nao-existe-em-lugar-nenhum")
    monkeypatch.setattr(llm, "_avisou", False)
    config.get_config.cache_clear()
    cfg_local = config.get_config()

    with caplog.at_level(logging.WARNING, logger="organizer.llm"):
        assert llm.disponivel(cfg_local) is False
        assert llm.disponivel(cfg_local) is False

    assert llm.binario_presente(cfg_local) is False
    assert caplog.text.count("ollama pull") == 1, "o aviso tem de ser único"
    assert "phi3:mini" in caplog.text

    alvo_local = factories.criar(sandbox.downloads, "document.pdf", factories.pdf_minimo())
    assert llm.classificar(alvo_local, cfg_local) is None
    assert llm.motivo_da_falha(alvo_local, cfg_local) == Motivo.LLM_INDISPONIVEL


def test_modelo_ausente(cfg, alvo, ollama_falso):
    """RF-53: binário presente mas modelo não baixado → indisponível."""
    ollama_falso.modo("modelo_ausente")
    assert llm.binario_presente(cfg) is True
    assert llm.disponivel(cfg) is False
    assert llm.classificar(alvo, cfg) is None


def test_list_com_erro_e_tratado(cfg, ollama_falso):
    ollama_falso.modo("list_quebrado")
    assert llm.disponivel(cfg) is False


def test_llm_desligado_na_config(sandbox, ollama_falso):
    cfg_local = dataclasses.replace(config.get_config(), llm_enabled=False)
    assert llm.disponivel(cfg_local) is False


def test_cache_de_disponibilidade(cfg, conn, ollama_falso):
    """RF-54: com `conn`, a segunda chamada não executa subprocess (TTL de 1 h)."""
    ollama_falso.limpar()
    assert llm.disponivel(cfg, conn) is True
    assert db.kv_get(conn, llm.CHAVE_CACHE) == "1"

    # se o cache não valesse, `disponivel` chamaria `ollama list` de novo e
    # passaria a responder False neste modo
    ollama_falso.modo("modelo_ausente")
    assert llm.disponivel(cfg, conn) is True, "a segunda chamada não usou o cache"

    assert llm.TTL_CACHE_SEGUNDOS == 3600


def test_cache_expira(cfg, conn, ollama_falso):
    from datetime import timedelta

    llm.disponivel(cfg, conn)
    depois = db.agora() + timedelta(hours=2)
    assert db.kv_get(conn, llm.CHAVE_CACHE, depois) is None


# --------------------------------------------------------------------------- #
# RF-55 — parser em cascata
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "modo,esperado",
    [
        ("json_puro", True),
        ("cerca_markdown", True),
        ("prosa_com_json", True),
        ("lixo_depois_json", True),
        ("lixo", False),
    ],
)
def test_parsing(cfg, alvo, ollama_falso, modo, esperado):
    """RF-55: JSON puro, cerca markdown, objeto balanceado, retry e falha final."""
    ollama_falso.modo(modo)
    ollama_falso.limpar()

    resposta = llm.classificar(alvo, cfg)

    if esperado:
        assert resposta is not None, f"modo {modo} deveria ter sido parseado"
        assert resposta.categoria == rules.CAT_MATRIZES
    else:
        assert resposta is None
        assert llm.motivo_da_falha(alvo, cfg) == Motivo.LLM_PARSE_ERROR


def test_retry_acontece_uma_unica_vez(cfg, alvo, ollama_falso):
    """RF-55 nível 4: exatamente um retry, com prompt encurtado."""
    ollama_falso.modo("lixo")
    ollama_falso.limpar()

    llm.classificar(alvo, cfg, texto="x" * 500)

    chamadas = [c for c in ollama_falso.chamadas if c["argv"][:1] != ["list"]]
    assert len(chamadas) == llm.MAX_TENTATIVAS_LLM == 2
    assert "Responda SOMENTE o JSON" in chamadas[1]["prompt"]
    assert len(chamadas[1]["prompt"]) < len(chamadas[0]["prompt"])


@pytest.mark.parametrize(
    "bruto,esperado",
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ('bla bla {"a": 1} tchau', {"a": 1}),
        ('{"a": {"b": 2}}', {"a": {"b": 2}}),
        ('{"a": "chave } falsa"}', {"a": "chave } falsa"}),
        ("nada aqui", None),
        ("", None),
        (None, None),
        ("[1, 2, 3]", None),
    ],
)
def test_parsear_unitario(bruto, esperado):
    assert llm.parsear(bruto) == esperado


def test_limpar_terminal_desfaz_o_redesenho_do_ollama():
    """Defeito real medido com ollama 0.32.5: CSI no meio da string JSON.

    O CLI recua o cursor e reimprime o pedaço ao quebrar a linha. Sem desfazer
    isso, `json.loads` recusa a resposta inteira — e todo documento cairia no
    `_Inbox` com `llm_parse_error`.
    """
    bruto = '{"motivo": "servicos eletroni\x1b[8D\x1b[K\neletronicos"}'
    limpo = llm.limpar_terminal(bruto)
    assert "\x1b" not in limpo
    assert "eletronieletronicos" not in limpo, "o recuo do cursor não foi aplicado"
    assert llm.parsear(bruto) == {"motivo": "servicos  eletronicos"}


def test_parser_sobrevive_a_saida_real_do_ollama(cfg, alvo, ollama_falso):
    """Ponta a ponta com o dublê emitindo as mesmas sequências do ollama real."""
    ollama_falso.modo("ansi_do_terminal")
    resposta = llm.classificar(alvo, cfg)
    assert resposta is not None
    assert resposta.categoria == rules.CAT_MATRIZES


def test_normalizar_quebras_so_dentro_de_strings():
    assert llm.normalizar_quebras_em_strings('{"a": "x\ny"}') == '{"a": "x y"}'
    # fora de string, a formatação é preservada
    assert llm.normalizar_quebras_em_strings('{\n"a": 1\n}') == '{\n"a": 1\n}'


def test_primeiro_objeto_ignora_chave_dentro_de_string():
    assert llm.primeiro_objeto('{"nome": "a{b"}') == '{"nome": "a{b"}'
    assert llm.primeiro_objeto('{"nome": "a\\"}b"}') == '{"nome": "a\\"}b"}'
    assert llm.primeiro_objeto("sem objeto") is None
    assert llm.primeiro_objeto("{incompleto") is None


# --------------------------------------------------------------------------- #
# RF-56 / RF-62 — validação semântica
# --------------------------------------------------------------------------- #


def test_categoria_invalida_descarta_resposta(cfg, alvo, ollama_falso):
    """RF-56: categoria fora do enum invalida a resposta inteira."""
    ollama_falso.modo("categoria_invalida")
    assert llm.classificar(alvo, cfg) is None
    assert llm.motivo_da_falha(alvo, cfg) == Motivo.LLM_PARSE_ERROR


def test_validar_descarta_tudo_e_nao_conserta():
    """RF-56: nem o `nome_sugerido` de uma resposta com categoria errada é usado."""
    assert llm.validar({"categoria": "Nao/Existe", "nome_sugerido": "otimo-nome"}) is None
    assert llm.validar({"nome_sugerido": "sem categoria"}) is None
    assert llm.validar(None) is None
    assert llm.validar("string") is None

    boa = llm.validar({"categoria": "Documentos\\Financeiro\\Extratos", "confianca": 0.7})
    assert boa is not None and boa.categoria == rules.CAT_EXTRATOS


@pytest.mark.parametrize(
    "bruta,esperada",
    [
        (0.88, 0.88),
        (0, 0.0),
        (1, 1.0),
        ("0.5", 0.5),
        ("muito alta", 0.5),
        (None, 0.5),
        (42, 0.5),
        (-1, 0.5),
        (float("nan"), 0.5),
    ],
)
def test_confianca_invalida(bruta, esperada):
    """RF-62: não numérica ou fora de `[0,1]` vira 0.5."""
    assert llm.normalizar_confianca(bruta) == pytest.approx(esperada)


def test_confianca_invalida_ponta_a_ponta(cfg, alvo, ollama_falso):
    ollama_falso.modo("confianca_invalida")
    resposta = llm.classificar(alvo, cfg)
    assert resposta is not None
    assert resposta.confianca == llm.CONFIANCA_PADRAO

    ollama_falso.modo("confianca_fora_do_intervalo")
    assert llm.classificar(alvo, cfg).confianca == llm.CONFIANCA_PADRAO


# --------------------------------------------------------------------------- #
# RF-59 — timeout
# --------------------------------------------------------------------------- #


def test_timeout(sandbox, ollama_falso, monkeypatch, caplog):
    """RF-59: `LLM_TIMEOUT` é aplicado e o timeout não deixa processo para trás."""
    monkeypatch.setenv("LLM_TIMEOUT", "1")
    config.get_config.cache_clear()
    cfg_local = config.get_config()
    ollama_falso.modo("dorme", FAKE_OLLAMA_SONO="20")
    alvo_local = factories.criar(sandbox.downloads, "document.pdf", factories.pdf_minimo())

    with caplog.at_level(logging.WARNING, logger="organizer.llm"):
        resposta = llm.classificar(alvo_local, cfg_local)

    assert resposta is None
    assert llm.motivo_da_falha(alvo_local, cfg_local) == Motivo.LLM_TIMEOUT
    assert "timeout do LLM" in caplog.text


def test_timeout_tenta_duas_vezes(sandbox, ollama_falso, monkeypatch):
    """RF-59: `MAX_TENTATIVAS_LLM` chamadas antes de desistir."""
    monkeypatch.setenv("LLM_TIMEOUT", "1")
    config.get_config.cache_clear()
    cfg_local = config.get_config()
    ollama_falso.modo("dorme", FAKE_OLLAMA_SONO="20")
    ollama_falso.limpar()
    alvo_local = factories.criar(sandbox.downloads, "document.pdf", factories.pdf_minimo())

    llm.classificar(alvo_local, cfg_local)

    chamadas = [c for c in ollama_falso.chamadas if c["argv"][:1] != ["list"]]
    assert len(chamadas) == llm.MAX_TENTATIVAS_LLM


def test_executar_levanta_timeout(cfg, ollama_falso, monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT", "1")
    config.get_config.cache_clear()
    cfg_local = config.get_config()
    ollama_falso.modo("dorme", FAKE_OLLAMA_SONO="20")
    with pytest.raises(llm.TimeoutLLM):
        llm.executar(cfg_local, "prompt qualquer")


# --------------------------------------------------------------------------- #
# Robustez da invocação
# --------------------------------------------------------------------------- #


def test_erro_do_binario_vira_indisponivel(cfg, alvo, ollama_falso):
    ollama_falso.modo("erro")
    assert llm.classificar(alvo, cfg) is None
    assert llm.motivo_da_falha(alvo, cfg) == Motivo.LLM_INDISPONIVEL


def test_flag_format_rejeitada_e_memorizada(cfg, conn, alvo, ollama_falso):
    """ARQUITETURA §9: instalação sem `--format json` é detectada uma vez e memorizada."""
    ollama_falso.modo("flag_desconhecida")
    ollama_falso.limpar()

    resposta = llm.classificar(alvo, cfg, conn=conn)

    assert resposta is not None, "o parser tolerante devia ter salvado a chamada"
    assert db.kv_get(conn, llm.CHAVE_FORMATO) == "0"
    chamadas = [c for c in ollama_falso.chamadas if c["argv"][:1] != ["list"]]
    assert "--format" in chamadas[0]["argv"]
    assert "--format" not in chamadas[1]["argv"]


def test_sem_cercas():
    assert llm.sem_cercas('{"a":1}') == '{"a":1}'
    assert llm.sem_cercas('```json\n{"a":1}\n```').strip() == '{"a":1}'
    assert llm.sem_cercas("```\nsem chaves\n```") == "```\nsem chaves\n```"


def test_binario_inexistente_em_executar(sandbox, monkeypatch):
    monkeypatch.setenv("OLLAMA_BIN", "nao-existe-mesmo-12345")
    config.get_config.cache_clear()
    cfg_local = config.get_config()
    with pytest.raises(llm.Indisponivel):
        llm.executar(cfg_local, "prompt")


def test_resposta_valida_completa(cfg, alvo, ollama_falso):
    """O contrato de saída inteiro, campo a campo."""
    resposta = llm.classificar(alvo, cfg, texto="grade curricular da unifesp")
    assert resposta.categoria == rules.CAT_MATRIZES
    assert resposta.nome_sugerido == "matriz-curricular-engenharia-computacao-2026"
    assert resposta.confianca == pytest.approx(0.88)
    assert "curricular" in resposta.motivo
    assert json.dumps(dataclasses.asdict(resposta))  # serializável


def test_motivo_da_falha_e_consumido_uma_vez(cfg, alvo, ollama_falso):
    ollama_falso.modo("lixo")
    llm.classificar(alvo, cfg)
    assert llm.motivo_da_falha(alvo, cfg) == Motivo.LLM_PARSE_ERROR
    # já consumido: volta ao motivo genérico
    assert llm.motivo_da_falha(alvo, cfg) == Motivo.LLM_INDISPONIVEL
