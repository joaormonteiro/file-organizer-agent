"""RF-17, RF-26, RF-47, RF-60, RF-61 — escala de confiança e decisão."""

from __future__ import annotations

import dataclasses

import pytest

import factories
from organizer import classify, llm, rules
from organizer.queue import Motivo


@pytest.mark.parametrize(
    "nome,esperado,categoria",
    [
        # patamar 0.95 — família e subtipo únicos
        ("x.exe", 0.95, rules.CAT_INSTALADORES),
        ("filme.mp4", 0.95, rules.CAT_VIDEOS),
        ("musica.mp3", 0.95, rules.CAT_MUSICA),
        # patamar 0.85 — subtipo default da família
        ("foto.jpg", 0.85, rules.CAT_FOTOS),
        # patamar 0.60 ambíguo, com o ônus de -0.10 por nome genérico
        ("relatorio.pdf", 0.50, rules.CAT_OUTROS),
        # 0.60 + 0.20 (keyword) + 0.05 (ano) = 0.85
        ("nota-fiscal-2026-05.pdf", 0.85, rules.CAT_NOTAS_FISCAIS),
        # patamar 0.30 — extensão desconhecida
        ("arquivo.v38", 0.30, rules.CAT_OUTROS),
    ],
)
def test_escala_de_confianca_por_extensao(nome, esperado, categoria):
    """RF-17: os cinco casos canônicos da ARQUITETURA §10.1."""
    resultado_categoria, confianca, *_ = classify.classificar_por_extensao(nome)
    assert confianca == pytest.approx(esperado)
    assert resultado_categoria == categoria


def test_keyword_sem_ano():
    """+0.20 sem o bônus de ano: 0.60 + 0.20 = 0.80."""
    _, confianca, *_ = classify.classificar_por_extensao("nota-fiscal.pdf")
    assert confianca == pytest.approx(0.80)


def test_keyword_muda_o_subtipo_dentro_da_familia():
    """`screenshot` leva um `.png` de Fotos para Screenshots e sobe a confiança."""
    categoria, confianca, *_ = classify.classificar_por_extensao("screenshot-2026-01.png")
    assert categoria == rules.CAT_SCREENSHOTS
    assert confianca == pytest.approx(classify.CLAMP_MAX)


def test_keyword_de_outra_familia_e_ignorada():
    """`nota fiscal` num `.exe` é ruído: não muda a categoria nem a confiança."""
    categoria, confianca, *_ = classify.classificar_por_extensao("nota-fiscal-2026.exe")
    assert categoria == rules.CAT_INSTALADORES
    assert confianca == pytest.approx(0.95)


def test_contradicao_entre_keywords():
    """-0.15 quando dois grupos do MESMO eixo apontam para ramos diferentes."""
    nome = "contrato-e-certificado-2026.pdf"  # Profissional vs Academico
    _, confianca, *_ = classify.classificar_por_extensao(nome)
    # 0.60 + 0.20 + 0.05 - 0.15 = 0.70
    assert confianca == pytest.approx(0.70)


# --------------------------------------------------------------------------- #
# B1 — keyword corroborante NUNCA derruba a confiança (regressão)
# --------------------------------------------------------------------------- #

#: As seis linhas levantadas na auditoria. Nenhuma pode acabar no `_Inbox`.
CASOS_CORROBORACAO = [
    ("contrato-estagio-2026.pdf", 0.85, rules.CAT_CONTRATOS),
    ("contrato-estagio-eyeconnect.pdf", 0.80, rules.CAT_EYECONNECT),
    ("certificado-curso-eyeconnect.pdf", 0.80, rules.CAT_EYECONNECT),
    ("nota-fiscal-eyeconnect-2026.pdf", 0.85, rules.CAT_EYECONNECT),
    ("relatorio-de-estagio-eyeconnect-2026.pdf", 0.85, rules.CAT_EYECONNECT),
    ("instalador-portatil.zip", 0.80, rules.CAT_INSTALADORES),
]


@pytest.mark.parametrize("nome,confianca_esperada,categoria", CASOS_CORROBORACAO)
def test_keyword_corroborante_nao_derruba_confianca(
    sandbox, nome, confianca_esperada, categoria
):
    """B1: acrescentar uma keyword verdadeira não pode piorar a decisão.

    `Contratos`, `Eyeconnect` e `EfficienceCo` são irmãs sob `Profissional`;
    `Instaladores` e `Portateis` são irmãs sob `Softwares`. Irmãs se corroboram.
    """
    resultado, confianca, *_ = classify.classificar_por_extensao(nome)
    assert confianca == pytest.approx(confianca_esperada)
    assert confianca >= sandbox.cfg.confidence_min, f"{nome} foi parar no _Inbox"
    assert resultado == categoria


def test_monotonicidade_ao_acrescentar_a_organizacao():
    """B1: o mesmo nome, mais o cliente, nunca vale menos."""
    for base, com_cliente in (
        ("contrato-estagio.pdf", "contrato-estagio-eyeconnect.pdf"),
        ("certificado-curso.pdf", "certificado-curso-eyeconnect.pdf"),
        ("nota-fiscal-2026.pdf", "nota-fiscal-eyeconnect-2026.pdf"),
    ):
        _, sem, *_ = classify.classificar_por_extensao(base)
        _, com, *_ = classify.classificar_por_extensao(com_cliente)
        assert com >= sem, f"{com_cliente} ({com}) ficou abaixo de {base} ({sem})"


def test_ramo_de_topo():
    """`Documentos/` é prefixo estrutural; o ramo é o segundo segmento."""
    assert rules.ramo(rules.CAT_CONTRATOS) == "Profissional"
    assert rules.ramo(rules.CAT_EYECONNECT) == "Profissional"
    assert rules.ramo(rules.CAT_CERTIFICADOS) == "Academico"
    assert rules.ramo(rules.CAT_NOTAS_FISCAIS) == "Financeiro"
    assert rules.ramo(rules.CAT_INSTALADORES) == "Softwares"
    assert rules.ramo(rules.CAT_PORTATEIS) == "Softwares"
    assert rules.ramo(rules.CAT_VIDEOS) == "Videos"


def test_ha_contradicao_so_entre_ramos_do_mesmo_eixo():
    grupos = {g.nome: g for g in rules.KEYWORDS}
    # mesmo eixo, mesmo ramo -> corrobora
    assert classify.ha_contradicao([grupos["instalador"], grupos["portatil"]]) is False
    # eixos diferentes -> nunca contradiz, mesmo com ramos diferentes
    assert classify.ha_contradicao([grupos["certificado"], grupos["eyeconnect"]]) is False
    # mesmo eixo, ramos diferentes -> contradiz
    assert classify.ha_contradicao([grupos["contrato"], grupos["certificado"]]) is True


def test_grupo_principal_prioriza_a_organizacao():
    grupos = {g.nome: g for g in rules.KEYWORDS}
    escolhido = classify.grupo_principal([grupos["contrato"], grupos["eyeconnect"]])
    assert escolhido.nome == "eyeconnect"
    # dentro do mesmo eixo, vence a ordem declarada em KEYWORDS
    assert classify.grupo_principal([grupos["instalador"], grupos["portatil"]]).nome == "instalador"


def test_clamp_maximo():
    assert classify.clamp(9.0) == classify.CLAMP_MAX
    assert classify.clamp(-1.0) == classify.CLAMP_MIN


@pytest.mark.parametrize(
    "nome,escala",
    [
        ("document.pdf", True),  # documento de texto com nome genérico
        ("relatorio.pdf", True),  # confiança 0.50 < 0.75
        ("documento (2).docx", True),
        ("download (3).zip", False),  # compactado: não há texto a extrair
        ("x.exe", False),
        ("foto.jpg", False),
        ("nota-fiscal-2026-05.pdf", False),  # 0.85 já resolve
        ("IMG_20260214.jpg", False),  # genérico, mas não é documento de texto
    ],
)
def test_quando_o_llm_e_acionado(sandbox, nome, escala):
    """RF-47: só documentos de texto genéricos ou de baixa confiança."""
    _, confianca, *_ = classify.classificar_por_extensao(nome)
    assert classify.deve_escalar_para_llm(nome, confianca, sandbox.cfg) is escala


def test_llm_desligado_nunca_escala(sandbox):
    cfg = dataclasses.replace(sandbox.cfg, llm_enabled=False)
    assert classify.deve_escalar_para_llm("document.pdf", 0.5, cfg) is False


@pytest.mark.parametrize(
    "conf_llm,evid,esperado",
    [
        (0.9, 1.0, 0.90),  # teto
        (0.5, 0.5, 0.50),
        (1.0, 0.0, 0.50),
        (0.0, 0.0, 0.00),
        (0.88, 0.80, 0.84),
    ],
)
def test_confianca_do_llm(conf_llm, evid, esperado):
    """RF-60: `clamp(0.5*conf_llm + 0.5*evidencia, 0, 0.90)`."""
    assert classify.confianca_llm(conf_llm, evid) == pytest.approx(esperado)


def test_teto_do_llm():
    """RF-61: o LLM nunca alcança 0.95 — uma extensão inequívoca sempre vence."""
    assert classify.confianca_llm(1.0, 1.0) == classify.TETO_LLM
    assert classify.TETO_LLM < classify.CLAMP_MAX
    _, conf_exe, *_ = classify.classificar_por_extensao("x.exe")
    assert conf_exe > classify.TETO_LLM


def test_evidencia_soma_os_quatro_sinais():
    """ARQUITETURA §10.2: 0.35 + 0.25 + 0.20 + 0.20."""
    texto = "nota fiscal eletronica " * 20
    total = classify.evidencia(True, texto, "nota-fiscal-2026", "doc.pdf", rules.CAT_NOTAS_FISCAIS)
    assert total == pytest.approx(1.0)
    assert classify.evidencia(False, None, "", "doc.pdf", rules.CAT_NOTAS_FISCAIS) == 0.0
    assert classify.evidencia(True, "curto", "ab", "doc.pdf", rules.CAT_OUTROS) == pytest.approx(
        classify.EVID_CATEGORIA
    )


def test_amostras_concordantes_e_discordantes():
    base = classify.confianca_llm(0.6, 0.6)
    assert classify.confianca_llm(0.6, 0.6, True) == pytest.approx(
        base + classify.AJUSTE_AMOSTRAS_CONCORDAM
    )
    assert classify.confianca_llm(0.6, 0.6, False) == pytest.approx(
        base + classify.AJUSTE_AMOSTRAS_DISCORDAM
    )


def test_classificar_alta_confianca(sandbox):
    alvo = sandbox.downloads / "DiscordSetup-2024-11.exe"
    alvo.write_bytes(b"MZ")
    decisao = classify.classificar(alvo, sandbox.cfg)
    assert decisao.categoria == rules.CAT_INSTALADORES
    assert decisao.para_inbox is False
    assert decisao.via == classify.VIA_EXTENSAO
    assert decisao.nome_final == "discordsetup-2024-11.exe"
    assert decisao.ext == ".exe"


def test_classificar_baixa_confianca_vai_para_inbox(sandbox):
    """RF-26: abaixo de CONFIDENCE_MIN vai para o `_Inbox` com o nome preservado."""
    alvo = sandbox.downloads / "relatorio.pdf"
    alvo.write_bytes(b"%PDF-1.4")
    decisao = classify.classificar(alvo, sandbox.cfg)
    assert decisao.para_inbox is True
    assert decisao.nome_final == "relatorio.pdf"
    # documento de texto genérico: o LLM é tentado e responde que está ausente
    assert decisao.motivo == Motivo.LLM_INDISPONIVEL.value


def test_baixa_confianca_sem_llm_usa_motivo_proprio(sandbox):
    """RF-26: sem caminho de LLM, o motivo é `baixa_confianca` — nada de confundir."""
    alvo = sandbox.downloads / "download.7z"
    alvo.write_bytes(b"7z\xbc\xaf")
    decisao = classify.classificar(alvo, sandbox.cfg)
    assert decisao.para_inbox is True
    assert decisao.motivo == Motivo.BAIXA_CONFIANCA.value


def test_sufixo_de_copia_some_do_nome_final(sandbox):
    """O `(n)` do navegador não entra no nome final (a colisão usa `-N`)."""
    alvo = sandbox.downloads / "DiscordSetup (3).exe"
    alvo.write_bytes(b"MZ")
    decisao = classify.classificar(alvo, sandbox.cfg)
    assert decisao.nome_final == "discordsetup.exe"


def test_nome_final_preserva_a_extensao_original(sandbox):
    """RF-57: a extensão nunca vem de fora — sempre a do arquivo."""
    alvo = sandbox.downloads / "relatorio.pdf"
    assert classify.nome_final_para(alvo, "matriz-curricular.docx").endswith(".pdf")


def test_nome_do_llm_e_sanitizado(sandbox):
    """RF-58: qualquer stem sugerido passa por `paths.sanitize_stem`."""
    alvo = sandbox.downloads / "relatorio.pdf"
    resultado = classify.nome_final_para(alvo, 'nota<>:"/\\|?*fiscal 2026')
    assert resultado == "notafiscal-2026.pdf"


def test_llm_indisponivel_nao_quebra(sandbox, monkeypatch):
    """RF-53: sem Ollama, o ambíguo cai no `_Inbox` com motivo específico."""
    assert llm.disponivel(sandbox.cfg) is False
    alvo = sandbox.downloads / "document.pdf"
    alvo.write_bytes(b"%PDF-1.4")
    decisao = classify.classificar(alvo, sandbox.cfg)
    assert decisao.para_inbox is True
    assert decisao.motivo == Motivo.LLM_INDISPONIVEL.value


def test_ramo_do_llm_usa_a_resposta_quando_existe(sandbox, monkeypatch):
    """Contrato da Fase 3: resposta válida vira categoria, nome e confiança."""
    resposta = llm.RespostaLLM(
        categoria=rules.CAT_MATRIZES,
        nome_sugerido="matriz curricular engenharia computacao 2026",
        confianca=0.9,
        motivo="texto cita grade curricular",
    )
    monkeypatch.setattr(llm, "classificar", lambda origem, cfg, texto=None, conn=None: resposta)
    alvo = sandbox.downloads / "document.pdf"
    alvo.write_bytes(b"%PDF-1.4")
    texto = "matriz curricular do curso de engenharia da computacao " * 6

    decisao = classify.classificar(alvo, sandbox.cfg, texto=texto)
    assert decisao.categoria == rules.CAT_MATRIZES
    assert decisao.via == classify.VIA_LLM
    assert decisao.nome_final == "matriz-curricular-engenharia-computacao-2026.pdf"
    assert decisao.confianca <= classify.TETO_LLM


def test_categoria_invalida_do_llm_e_descartada(sandbox, monkeypatch):
    """RF-56: categoria fora do enum descarta a resposta inteira."""
    resposta = llm.RespostaLLM("Documentos/Inventada", "nome-bonito", 0.99, "porque sim")
    monkeypatch.setattr(llm, "classificar", lambda origem, cfg, texto=None, conn=None: resposta)
    alvo = sandbox.downloads / "document.pdf"
    alvo.write_bytes(b"%PDF-1.4")
    decisao = classify.classificar(alvo, sandbox.cfg, texto="x" * 300)
    assert decisao.para_inbox is True
    assert decisao.categoria == rules.CAT_OUTROS


# --------------------------------------------------------------------------- #
# Fase 3 ponta a ponta, com o Ollama falso
# --------------------------------------------------------------------------- #


def test_extensao_preservada(sandbox, ollama_falso):
    """RF-57: o fake sugere outra extensão e o destino mantém a original."""
    from organizer import config

    ollama_falso.modo("nome_sujo")  # nome_sugerido termina em `.docx`
    config.get_config.cache_clear()
    cfg = config.get_config()
    alvo = sandbox.downloads / "document.pdf"
    alvo.write_bytes(factories.pdf_minimo("grade curricular unifesp " * 20))

    decisao = classify.classificar(alvo, cfg)

    assert decisao.via == classify.VIA_LLM
    assert decisao.nome_final.endswith(".pdf")
    assert not decisao.nome_final.endswith(".docx")


def test_nome_do_llm_e_sanitizado(sandbox, ollama_falso):
    """RF-58: o nome sugerido passa por `paths.sanitize_stem` antes de qualquer uso."""
    from organizer import config, paths

    ollama_falso.modo("nome_sujo")
    config.get_config.cache_clear()
    cfg = config.get_config()
    alvo = sandbox.downloads / "document.pdf"
    alvo.write_bytes(factories.pdf_minimo("grade curricular unifesp " * 20))

    decisao = classify.classificar(alvo, cfg)

    for proibido in '<>:"/\\|?*':
        assert proibido not in decisao.nome_final
    assert decisao.nome_final == "matrizcurricular-2026.pdf"
    assert paths.sanitize_stem("matrizcurricular 2026") == "matrizcurricular-2026"


@pytest.mark.parametrize(
    "sugerido,esperado",
    [
        ("matriz_curricular_unifesp_20260802.docx", "matriz_curricular_unifesp_20260802"),
        ("extrato-de-conta-202604.json", "extrato-de-conta-202604"),
        ("relatorio.final.pdf", "relatorio"),
        ("nota-fiscal-2026.05", "nota-fiscal-2026.05"),
        ("matriz-curricular-ec-2026", "matriz-curricular-ec-2026"),
        (".docx", ".docx"),
    ],
)
def test_extensao_sugerida_pelo_llm_e_removida(sugerido, esperado):
    """RF-57: o modelo cola extensão no `nome_sugerido` mesmo mandado não colar.

    Sem a poda, o destino real virava `..._20260802.docx.docx`. Sufixo numérico
    (`.05`) é preservado: pode ser parte do nome.
    """
    assert classify.remover_extensao_sugerida(sugerido) == esperado


def test_nome_sugerido_com_espaco_e_extensao_indevida(sandbox):
    """RF-57/RF-58: caso real medido na auditoria da Fase 3.

    O phi3:mini devolveu `nota fiscal-e20260803.json` para um PDF: espaço no
    meio e extensão inventada. Nada disso pode chegar ao disco.
    """
    origem = sandbox.downloads / "document.pdf"
    final = classify.nome_final_para(origem, "nota fiscal-e20260803.json")

    assert final == "nota-fiscal-e20260803.pdf"
    assert " " not in final
    assert ".json" not in final
    assert final.endswith(".pdf")


def test_nome_final_nunca_duplica_a_extensao(sandbox):
    origem = sandbox.downloads / "documento (2).docx"
    final = classify.nome_final_para(origem, "matriz_curricular_unifesp.docx")
    assert final.count(".docx") == 1
    assert final.endswith(".docx")


def test_ramo_do_llm_ponta_a_ponta(sandbox, ollama_falso):
    """RF-47 + RF-60: documento genérico vai ao LLM e volta classificado."""
    from organizer import config

    config.get_config.cache_clear()
    cfg = config.get_config()
    alvo = sandbox.downloads / "document.pdf"
    alvo.write_bytes(factories.pdf_minimo("matriz curricular da unifesp " * 25))

    decisao = classify.classificar(alvo, cfg)

    assert decisao.via == classify.VIA_LLM
    assert decisao.categoria == rules.CAT_MATRIZES
    assert decisao.para_inbox is False
    # conf_llm=0.88, evidencia=1.0 -> 0.94 -> teto 0.90
    assert decisao.confianca == pytest.approx(classify.TETO_LLM)
    assert decisao.texto_amostra and "matriz curricular" in decisao.texto_amostra


def test_zip_generico_nao_aciona_o_llm(sandbox, ollama_falso):
    """RF-47: `.zip` de nome genérico não vai ao LLM — não há texto a extrair."""
    from organizer import config

    ollama_falso.limpar()
    config.get_config.cache_clear()
    cfg = config.get_config()
    alvo = sandbox.downloads / "download (3).zip"
    alvo.write_bytes(factories.zip_minimo())

    decisao = classify.classificar(alvo, cfg)

    assert decisao.via != classify.VIA_LLM
    assert decisao.para_inbox is True
    assert decisao.motivo == Motivo.BAIXA_CONFIANCA.value
    assert [c for c in ollama_falso.chamadas if c["argv"][:1] != ["list"]] == []


def test_pdf_protegido_segue_por_nome_e_tamanho(sandbox, ollama_falso):
    """RF-49: sem texto, a classificação continua — só com menos evidência."""
    from organizer import config

    config.get_config.cache_clear()
    cfg = config.get_config()
    alvo = sandbox.downloads / "document.pdf"
    alvo.write_bytes(factories.pdf_protegido())

    decisao = classify.classificar(alvo, cfg)

    assert decisao.texto_amostra is None
    # sem os 0.25 de "texto real", a evidência cai e a confiança junto
    assert decisao.confianca < classify.TETO_LLM


def test_categoria_invalida_do_llm_ponta_a_ponta(sandbox, ollama_falso):
    """RF-56 ponta a ponta: resposta descartada manda o arquivo para o `_Inbox`."""
    from organizer import config

    ollama_falso.modo("categoria_invalida")
    config.get_config.cache_clear()
    cfg = config.get_config()
    alvo = sandbox.downloads / "document.pdf"
    alvo.write_bytes(factories.pdf_minimo())

    decisao = classify.classificar(alvo, cfg)

    assert decisao.para_inbox is True
    assert decisao.categoria == rules.CAT_OUTROS
    assert decisao.motivo == Motivo.LLM_PARSE_ERROR.value


def test_decisao_corroborada():
    """Calibração da Fase 3: o texto confirma a categoria escolhida pelo LLM?"""
    assert classify.decisao_corroborada(
        rules.CAT_NOTAS_FISCAIS, "NOTA FISCAL DE SERVICOS numero 4521", "document.pdf"
    )
    assert not classify.decisao_corroborada(
        rules.CAT_CERTIFICADOS, "CARTEIRA DE IDENTIDADE RG 12345", "20260214.pdf"
    )
    # o nome do arquivo também serve de corroboração
    assert classify.decisao_corroborada(rules.CAT_CONTRATOS, None, "contrato-estagio.pdf")
    # categorias sem keywords não têm como ser corroboradas — ficam de fora
    assert classify.decisao_corroborada(rules.CAT_OUTROS, "qualquer coisa", "x.pdf")
    assert classify.decisao_corroborada(rules.CAT_FOTOS, None, "x.jpg")


def test_llm_sem_corroboracao_cai_no_inbox(sandbox, ollama_falso):
    """Mitigação medida: LLM confiante mas sem apoio no texto NÃO move o arquivo.

    O dublê responde `Matrizes-Curriculares` com confiança 0.88, mas o trecho
    fala de identidade civil. Antes desta trava o arquivo ia parar, com 0.90 de
    confiança, na pasta errada.
    """
    from organizer import config

    config.get_config.cache_clear()
    cfg = config.get_config()
    alvo = sandbox.downloads / "document.pdf"
    alvo.write_bytes(factories.pdf_minimo("carteira de identidade rg cpf " * 20))

    decisao = classify.classificar(alvo, cfg)

    assert decisao.confianca <= classify.TETO_LLM_SEM_CORROBORACAO
    assert decisao.para_inbox is True
    assert decisao.motivo == Motivo.LLM_SEM_CORROBORACAO.value
    assert classify.TETO_LLM_SEM_CORROBORACAO < sandbox.cfg.confidence_min


def test_llm_corroborado_move_normalmente(sandbox, ollama_falso):
    """O outro lado: com apoio no texto, a decisão do LLM vale."""
    from organizer import config

    config.get_config.cache_clear()
    cfg = config.get_config()
    alvo = sandbox.downloads / "document.pdf"
    alvo.write_bytes(factories.pdf_minimo("matriz curricular do curso " * 20))

    decisao = classify.classificar(alvo, cfg)

    assert decisao.para_inbox is False
    assert decisao.motivo == Motivo.OK.value
    assert decisao.confianca == pytest.approx(classify.TETO_LLM)


def test_normalizar_para_keyword():
    assert classify.normalizar_para_keyword("Nota_Fiscal-2026.pdf") == "nota fiscal 2026"


def test_tem_keyword_no_texto():
    assert classify.tem_keyword_no_texto("emitimos a NOTA FISCAL hoje", rules.CAT_NOTAS_FISCAIS)
    assert not classify.tem_keyword_no_texto(None, rules.CAT_NOTAS_FISCAIS)
    assert not classify.tem_keyword_no_texto("nada aqui", rules.CAT_NOTAS_FISCAIS)
