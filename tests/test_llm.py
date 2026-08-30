"""RF-51 a RF-56, RF-59, RF-62 — fronteira com a API do Gemini.

Todos os testes usam o dublê `gemini_falso` (conftest.py, substitui
`urllib.request.urlopen`): a suíte tem de passar sem rede e sem uma
`GEMINI_API_KEY` real (RNF-05).
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path

import pytest

import factories
from organizer import config, llm, rules
from organizer.queue import Motivo

RAIZ_PROJETO = Path(__file__).resolve().parent.parent


@pytest.fixture
def cfg(sandbox, gemini_falso):
    """Config já apontando para o Gemini falso."""
    config.get_config.cache_clear()
    return config.get_config()


@pytest.fixture
def alvo(sandbox):
    return factories.criar(sandbox.downloads, "document.pdf", factories.pdf_minimo())


# --------------------------------------------------------------------------- #
# RF-51 / RF-52 — chamada efêmera, prompt no corpo (não na URL)
# --------------------------------------------------------------------------- #


def test_cada_chamada_e_independente(cfg, alvo, gemini_falso):
    """RF-51 equivalente: nenhuma conexão/sessão é reaproveitada entre chamadas."""
    gemini_falso.limpar()
    llm.classificar(alvo, cfg)
    llm.classificar(alvo, cfg)

    chamadas = gemini_falso.chamadas
    assert len(chamadas) == 2, "cada classificação deveria disparar sua própria requisição"


def test_nenhum_servidor_e_mantido_vivo():
    """RF-51: `llm.py` fala HTTP direto, sem abrir socket de servidor nem Popen."""
    from test_isolation import linhas_efetivas

    codigo = "\n".join(t for _, t in linhas_efetivas(RAIZ_PROJETO / "organizer" / "llm.py"))
    assert "urllib.request.urlopen(" in codigo
    assert "Popen" not in codigo
    assert "socket.socket(" not in codigo


def test_prompt_no_corpo_nao_na_url(cfg, alvo, gemini_falso):
    """RF-52 equivalente: o texto do prompt vai no corpo POST, nunca na URL."""
    gemini_falso.limpar()
    llm.classificar(alvo, cfg, texto="matriz curricular da unifesp 2026")

    chamadas = gemini_falso.chamadas
    assert chamadas, "o dublê não foi chamado"
    chamada = chamadas[-1]

    assert "matriz curricular" not in chamada["url"]
    assert "document.pdf" not in chamada["url"]
    # e o prompt chegou inteiro no corpo
    assert "matriz curricular da unifesp 2026" in chamada["prompt"]
    assert "document.pdf" in chamada["prompt"]


def test_chave_vai_no_header_nao_na_url(cfg, alvo, gemini_falso):
    """A API key nunca aparece na URL (evita vazar em logs de acesso)."""
    gemini_falso.limpar()
    llm.classificar(alvo, cfg)

    chamada = gemini_falso.chamadas[-1]
    assert chamada["chave"] == cfg.gemini_api_key
    assert cfg.gemini_api_key not in chamada["url"]
    assert cfg.gemini_model in chamada["url"]


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


def test_schema_restringe_categoria_ao_enum():
    """`responseSchema` fecha `categoria` no mesmo enum que `rules.categoria_valida` aceita."""
    schema = llm._schema_resposta()
    assert schema["properties"]["categoria"]["enum"] == list(rules.CATEGORIAS)
    assert set(schema["required"]) == {"categoria", "nome_sugerido", "confianca", "motivo"}


# --------------------------------------------------------------------------- #
# RF-53 — disponibilidade
# --------------------------------------------------------------------------- #


def test_chave_ausente(sandbox, monkeypatch, caplog):
    """RF-53: sem `GEMINI_API_KEY` → `disponivel()` False, WARNING único, nada quebra."""
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setattr(llm, "_avisou", False)
    config.get_config.cache_clear()
    cfg_local = config.get_config()

    with caplog.at_level(logging.WARNING, logger="organizer.llm"):
        assert llm.disponivel(cfg_local) is False
        assert llm.disponivel(cfg_local) is False

    assert caplog.text.count("aistudio.google.com") == 1, "o aviso tem de ser único"
    assert "GEMINI_API_KEY" in caplog.text

    alvo_local = factories.criar(sandbox.downloads, "document.pdf", factories.pdf_minimo())
    assert llm.classificar(alvo_local, cfg_local) is None
    assert llm.motivo_da_falha(alvo_local, cfg_local) == Motivo.LLM_INDISPONIVEL


def test_llm_desligado_na_config(sandbox, gemini_falso):
    cfg_local = dataclasses.replace(config.get_config(), llm_enabled=False)
    assert llm.disponivel(cfg_local) is False


def test_disponivel_reflete_configuracao_imediatamente(sandbox, monkeypatch):
    """Diferente do Ollama, checar a chave é O(1) — não há cache para ficar obsoleto."""
    monkeypatch.setenv("GEMINI_API_KEY", "")
    config.get_config.cache_clear()
    assert llm.disponivel(config.get_config()) is False

    monkeypatch.setenv("GEMINI_API_KEY", "agora-tem-chave")
    config.get_config.cache_clear()
    assert llm.disponivel(config.get_config()) is True


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
        ("bloqueio_safety", False),
    ],
)
def test_parsing(cfg, alvo, gemini_falso, modo, esperado):
    """RF-55: JSON puro, cerca markdown, objeto balanceado, retry e falha final."""
    gemini_falso.modo(modo)
    gemini_falso.limpar()

    resposta = llm.classificar(alvo, cfg)

    if esperado:
        assert resposta is not None, f"modo {modo} deveria ter sido parseado"
        assert resposta.categoria == rules.CAT_MATRIZES
    else:
        assert resposta is None
        assert llm.motivo_da_falha(alvo, cfg) == Motivo.LLM_PARSE_ERROR


def test_retry_acontece_uma_unica_vez(cfg, alvo, gemini_falso):
    """RF-55: exatamente um retry, com prompt encurtado."""
    gemini_falso.modo("lixo")
    gemini_falso.limpar()

    llm.classificar(alvo, cfg, texto="x" * 500)

    chamadas = gemini_falso.chamadas
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


def test_normalizar_quebras_so_dentro_de_strings():
    assert llm.normalizar_quebras_em_strings('{"a": "x\ny"}') == '{"a": "x y"}'
    # fora de string, a formatação é preservada
    assert llm.normalizar_quebras_em_strings('{\n"a": 1\n}') == '{\n"a": 1\n}'


def test_primeiro_objeto_ignora_chave_dentro_de_string():
    assert llm.primeiro_objeto('{"nome": "a{b"}') == '{"nome": "a{b"}'
    assert llm.primeiro_objeto('{"nome": "a\\"}b"}') == '{"nome": "a\\"}b"}'
    assert llm.primeiro_objeto("sem objeto") is None
    assert llm.primeiro_objeto("{incompleto") is None


def test_sem_cercas():
    assert llm.sem_cercas('{"a":1}') == '{"a":1}'
    assert llm.sem_cercas('```json\n{"a":1}\n```').strip() == '{"a":1}'
    assert llm.sem_cercas("```\nsem chaves\n```") == "```\nsem chaves\n```"


def test_extrair_texto_de_resposta_bloqueada_nao_quebra():
    """Bloqueio de safety devolve `candidates: []` — vira string vazia, não exceção."""
    assert llm._extrair_texto(json.dumps({"candidates": []})) == ""
    assert llm._extrair_texto("nao e json") == ""


# --------------------------------------------------------------------------- #
# RF-56 / RF-62 — validação semântica
# --------------------------------------------------------------------------- #


def test_categoria_invalida_descarta_resposta(cfg, alvo, gemini_falso):
    """RF-56: categoria fora do enum invalida a resposta inteira (rede de segurança:
    o `responseSchema` já deveria ter barrado isso na origem)."""
    gemini_falso.modo("categoria_invalida")
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


@pytest.mark.parametrize(
    "chave,valor",
    [
        ("confiança", 0.82),
        ("confidence", 0.82),
        ("Confianca", 0.82),
    ],
)
def test_chave_acentuada_ou_em_ingles_e_aceita(chave, valor):
    """O modelo às vezes devolve `"confiança"` de vez em quando, mesmo com schema.

    Antes, a leitura crua achava `None` e caía em silêncio para 0.5, jogando
    fora um número perfeitamente bom.
    """
    resposta = llm.validar({"categoria": rules.CAT_EXTRATOS, chave: valor})
    assert resposta is not None
    assert resposta.confianca == pytest.approx(valor)


def test_nome_e_motivo_tambem_aceitam_sinonimos():
    resposta = llm.validar(
        {
            "categoria": rules.CAT_EXTRATOS,
            "nome sugerido": "extrato-2026-04",
            "razão": "extrato bancario",
            "confianca": 0.7,
        }
    )
    assert resposta.nome_sugerido == "extrato-2026-04"
    assert resposta.motivo == "extrato bancario"


def test_confianca_ausente_gera_warning(caplog):
    """Cair no default de 0.5 deixou de ser silencioso."""
    with caplog.at_level(logging.WARNING, logger="organizer.llm"):
        resposta = llm.validar({"categoria": rules.CAT_EXTRATOS})
    assert resposta.confianca == llm.CONFIANCA_PADRAO
    assert "sem confianca utilizável" in caplog.text


def test_confianca_boa_nao_gera_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="organizer.llm"):
        llm.validar({"categoria": rules.CAT_EXTRATOS, "confianca": 0.9})
    assert "sem confianca" not in caplog.text


def test_normalizar_chaves_preserva_o_canonico():
    """Se as duas grafias vierem, a canônica vence."""
    normalizado = llm.normalizar_chaves({"confianca": 0.9, "confiança": 0.1})
    assert normalizado["confianca"] == 0.9


def test_confianca_invalida_ponta_a_ponta(cfg, alvo, gemini_falso):
    gemini_falso.modo("confianca_invalida")
    resposta = llm.classificar(alvo, cfg)
    assert resposta is not None
    assert resposta.confianca == llm.CONFIANCA_PADRAO

    gemini_falso.modo("confianca_fora_do_intervalo")
    assert llm.classificar(alvo, cfg).confianca == llm.CONFIANCA_PADRAO


# --------------------------------------------------------------------------- #
# RF-59 — timeout
# --------------------------------------------------------------------------- #


def test_timeout(sandbox, gemini_falso, caplog):
    """RF-59: timeout vira `TimeoutLLM`, tratado sem exceção subir para o chamador."""
    config.get_config.cache_clear()
    cfg_local = config.get_config()
    gemini_falso.modo("dorme")
    alvo_local = factories.criar(sandbox.downloads, "document.pdf", factories.pdf_minimo())

    with caplog.at_level(logging.WARNING, logger="organizer.llm"):
        resposta = llm.classificar(alvo_local, cfg_local)

    assert resposta is None
    assert llm.motivo_da_falha(alvo_local, cfg_local) == Motivo.LLM_TIMEOUT
    assert "timeout do LLM" in caplog.text


def test_timeout_tenta_duas_vezes(sandbox, gemini_falso):
    """RF-59: `MAX_TENTATIVAS_LLM` chamadas antes de desistir."""
    config.get_config.cache_clear()
    cfg_local = config.get_config()
    gemini_falso.modo("dorme")
    gemini_falso.limpar()
    alvo_local = factories.criar(sandbox.downloads, "document.pdf", factories.pdf_minimo())

    llm.classificar(alvo_local, cfg_local)

    assert len(gemini_falso.chamadas) == llm.MAX_TENTATIVAS_LLM


def test_executar_levanta_timeout(cfg, gemini_falso):
    gemini_falso.modo("dorme")
    with pytest.raises(llm.TimeoutLLM):
        llm.executar(cfg, "prompt qualquer")


# --------------------------------------------------------------------------- #
# Robustez da invocação
# --------------------------------------------------------------------------- #


def test_erro_5xx_vira_temporariamente_indisponivel(cfg, alvo, gemini_falso):
    """Erro do lado do servidor (500/503/...) nao e definitivo: motivo distinto
    de llm_indisponivel, para o chamador (ingest) saber que vale a pena
    devolver para a fila em vez de mandar direto para o _Inbox."""
    gemini_falso.modo("erro")
    with pytest.raises(llm.IndisponivelTemporario):
        llm.executar(cfg, "prompt qualquer")
    assert llm.classificar(alvo, cfg) is None
    assert llm.motivo_da_falha(alvo, cfg) == Motivo.LLM_TEMPORARIAMENTE_INDISPONIVEL


def test_chave_invalida_vira_indisponivel(cfg, alvo, gemini_falso):
    """401 (chave errada) e configuracao, nao rede/servidor: esperar nao
    resolve, entao continua como llm_indisponivel (nao temporario)."""
    gemini_falso.modo("chave_invalida")
    with pytest.raises(llm.Indisponivel) as excinfo:
        llm.executar(cfg, "prompt qualquer")
    assert not isinstance(excinfo.value, llm.IndisponivelTemporario)
    assert llm.classificar(alvo, cfg) is None
    assert llm.motivo_da_falha(alvo, cfg) == Motivo.LLM_INDISPONIVEL


def test_sem_rede_vira_temporariamente_indisponivel(cfg, gemini_falso):
    """Internet fora do ar (DNS, sem rota) e passageira, nao configuracao —
    vira `IndisponivelTemporario`, não exceção crua."""
    gemini_falso.modo("sem_rede")
    with pytest.raises(llm.IndisponivelTemporario):
        llm.executar(cfg, "prompt qualquer")


def test_resposta_valida_completa(cfg, alvo, gemini_falso):
    """O contrato de saída inteiro, campo a campo."""
    resposta = llm.classificar(alvo, cfg, texto="grade curricular da unifesp")
    assert resposta.categoria == rules.CAT_MATRIZES
    assert resposta.nome_sugerido == "matriz-curricular-engenharia-computacao-2026"
    assert resposta.confianca == pytest.approx(0.88)
    assert "curricular" in resposta.motivo
    assert json.dumps(dataclasses.asdict(resposta))  # serializável


def test_motivo_da_falha_e_consumido_uma_vez(cfg, alvo, gemini_falso):
    gemini_falso.modo("lixo")
    llm.classificar(alvo, cfg)
    assert llm.motivo_da_falha(alvo, cfg) == Motivo.LLM_PARSE_ERROR
    # já consumido: volta ao motivo genérico
    assert llm.motivo_da_falha(alvo, cfg) == Motivo.LLM_INDISPONIVEL
