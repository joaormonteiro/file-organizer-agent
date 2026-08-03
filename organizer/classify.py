"""Decide categoria, nome final e confiança de um arquivo.

Fase 1-2 usa apenas a tabela de extensões (`rules`). O ramo do LLM já está
ligado atrás de `llm.disponivel()`: enquanto a Fase 3 não existir, `llm`
responde que está indisponível e o arquivo cai no `_Inbox` com motivo
`llm_indisponivel` — exatamente a degradação graciosa prevista na ARQUITETURA §9.

Não faz I/O de filesystem além de `stat`.

Requisitos cobertos: RF-17, RF-26, RF-47 (gatilho), RF-57, RF-58, RF-60, RF-61.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

from organizer import extract, llm, naming, paths, rules
from organizer.queue import Motivo

# --------------------------------------------------------------------------- #
# Constantes de confiança (ARQUITETURA §10) — nada de número mágico espalhado
# --------------------------------------------------------------------------- #

#: ≥1 keyword da categoria aparece no nome do arquivo.
AJUSTE_KEYWORD = 0.20
#: keyword acompanhada de um ano plausível (2000-2099).
AJUSTE_ANO = 0.05
#: keywords de duas categorias diferentes casaram — sinal contraditório.
AJUSTE_CONTRADICAO = -0.15
#: o nome não confirma nada (`naming.is_generic`).
AJUSTE_GENERICO = -0.10

#: Limites do clamp da classificação por extensão.
CLAMP_MIN = 0.0
CLAMP_MAX = 0.95

#: Teto da classificação por LLM: sempre abaixo de uma extensão inequívoca (RF-61).
TETO_LLM = 0.90

#: Pesos da evidência calculada por nós (ARQUITETURA §10.2).
EVID_CATEGORIA = 0.35
EVID_TEXTO = 0.25
EVID_KEYWORD = 0.20
EVID_NOME = 0.20
#: Mínimo de caracteres para o trecho extraído contar como evidência.
MIN_CHARS_TEXTO = 200
#: Bônus/ônus quando `LLM_SAMPLES=2`.
AJUSTE_AMOSTRAS_CONCORDAM = 0.10
AJUSTE_AMOSTRAS_DISCORDAM = -0.20

VIA_EXTENSAO = "extensao"
VIA_LLM = "llm"
VIA_FALLBACK = "fallback"
VIA_MANUAL = "manual"

_ANO = re.compile(r"\b20\d{2}\b")
_SEPARADORES = re.compile(r"[_.+\-]|%20")
_ESPACOS = re.compile(r"\s+")


# --------------------------------------------------------------------------- #
# Decisão
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Decisao:
    """O que fazer com um arquivo. Consumida por `move.executar`."""

    origem: Path
    categoria: str
    nome_final: str
    confianca: float
    via: str
    motivo: str
    tipo: str
    subtipo: str
    generico: bool
    motivo_generico: str = ""
    para_inbox: bool = False
    texto_amostra: str | None = None

    @property
    def ext(self) -> str:
        return self.origem.suffix.lower()


def clamp(valor: float, minimo: float = CLAMP_MIN, maximo: float = CLAMP_MAX) -> float:
    return max(minimo, min(maximo, float(valor)))


# --------------------------------------------------------------------------- #
# Keywords
# --------------------------------------------------------------------------- #


def normalizar_para_keyword(nome: str) -> str:
    """Forma comparável do nome: sem acento, minúscula, separadores viram espaço."""
    texto = paths.fold_ascii(Path(nome).stem).lower()
    texto = _SEPARADORES.sub(" ", texto)
    return _ESPACOS.sub(" ", texto).strip()


def _tem_termo(texto: str, termo: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(termo)}(?![a-z0-9])", texto) is not None


def grupos_casados(nome: str, familia: str) -> list[rules.GrupoKeyword]:
    """Grupos de keyword compatíveis com a família da extensão que casam no nome."""
    texto = normalizar_para_keyword(nome)
    achados: list[rules.GrupoKeyword] = []
    for grupo in rules.KEYWORDS:
        if familia not in grupo.familias:
            continue
        if any(_tem_termo(texto, termo) for termo in grupo.termos):
            achados.append(grupo)
    return achados


def grupo_principal(casados: list[rules.GrupoKeyword]) -> rules.GrupoKeyword:
    """Qual grupo decide a categoria quando mais de um casa.

    Precedência por eixo (`rules.PRECEDENCIA_EIXOS`): a organização vence o tipo
    de documento, seguindo o exemplo do Query CLI da spec. Dentro do mesmo eixo,
    vence o primeiro na ordem declarada em `rules.KEYWORDS` — determinístico.
    """
    for eixo in rules.PRECEDENCIA_EIXOS:
        for grupo in casados:
            if grupo.eixo == eixo:
                return grupo
    return casados[0]


def ha_contradicao(casados: list[rules.GrupoKeyword]) -> bool:
    """Sinal contraditório: dois grupos **do mesmo eixo** em ramos de topo diferentes.

    Categorias irmãs (mesmo ramo) se **corroboram**: `instalador` + `portatil`
    concordam que aquilo é software, e `contrato` + `eyeconnect` concordam que
    aquilo é profissional. Só `contrato` + `certificado` (Profissional vs
    Academico) é de fato um conflito.
    """
    ramos_por_eixo: dict[str, set[str]] = {}
    for grupo in casados:
        ramos_por_eixo.setdefault(grupo.eixo, set()).add(rules.ramo(grupo.categoria))
    return any(len(ramos) > 1 for ramos in ramos_por_eixo.values())


def tem_keyword_no_texto(texto: str | None, categoria: str) -> bool:
    """`True` se alguma keyword da categoria aparece no texto informado."""
    if not texto:
        return False
    alvo = _ESPACOS.sub(" ", paths.fold_ascii(texto).lower())
    return any(_tem_termo(alvo, termo) for termo in rules.keywords_da_categoria(categoria))


# --------------------------------------------------------------------------- #
# Classificação por extensão (ARQUITETURA §10.1)
# --------------------------------------------------------------------------- #


def classificar_por_extensao(nome: str) -> tuple[str, float, str, str, bool, str]:
    """`(categoria, confianca, tipo, subtipo, generico, motivo_generico)`.

    Ajustes aplicados sobre a base, com clamp final em `[0.0, 0.95]`:
    `+0.20` keyword, `+0.05` keyword com ano, `-0.15` contradição,
    `-0.10` nome genérico.

    "Contradição" é medida por `ha_contradicao`: dois grupos do **mesmo eixo**
    apontando para **ramos de topo diferentes**. Keywords corroborantes (mesmo
    ramo, ou eixos distintos como organização + tipo de documento) somam o bônus
    uma vez e **nunca** derrubam a confiança — acrescentar informação verdadeira
    ao nome jamais pode piorar a decisão.

    Nota de implementação: o ônus de nome genérico só é aplicado no patamar
    **ambíguo** (0.60), que é onde o nome é a evidência decisiva. Nos demais
    patamares a extensão já resolve sozinha o destino, e aplicar o ônus
    contradiria os valores canônicos fixados na própria ARQUITETURA §10.1
    (`x.exe` = 0.95, `foto.jpg` = 0.85, `arquivo.v38` = 0.30 — todos com stem
    genérico por G1/G4).
    """
    regra = rules.por_extensao(Path(nome).suffix)
    categoria = regra.categoria
    confianca = regra.confianca
    tipo, subtipo = regra.familia, regra.subtipo

    generico, motivo_generico = naming.is_generic(nome)
    casados = grupos_casados(nome, regra.familia)

    if casados:
        principal = grupo_principal(casados)
        categoria = principal.categoria
        subtipo = rules.subtipo_de(categoria)
        confianca += AJUSTE_KEYWORD
        if _ANO.search(normalizar_para_keyword(nome)):
            confianca += AJUSTE_ANO
        if ha_contradicao(casados):
            confianca += AJUSTE_CONTRADICAO

    if generico and regra.confianca == rules.CONF_AMBIGUO:
        confianca += AJUSTE_GENERICO

    return categoria, clamp(confianca), tipo, subtipo, generico, motivo_generico


# --------------------------------------------------------------------------- #
# Gatilho do LLM (ARQUITETURA §8 / RF-47)
# --------------------------------------------------------------------------- #


def deve_escalar_para_llm(nome: str, confianca: float, cfg) -> bool:
    """Só documentos de texto com nome genérico OU confiança abaixo do mínimo.

    Um `.zip` de nome genérico não vai ao LLM: não há texto a extrair.
    """
    if not cfg.llm_enabled:
        return False
    if Path(nome).suffix.lower() not in rules.EXTENSOES_TEXTO:
        return False
    generico, _ = naming.is_generic(nome)
    return generico or confianca < cfg.confidence_min


# --------------------------------------------------------------------------- #
# Confiança do LLM (ARQUITETURA §10.2)
# --------------------------------------------------------------------------- #


def evidencia(
    categoria_valida: bool, texto: str | None, nome_sanitizado: str, nome_original: str, categoria: str
) -> float:
    """Soma dos sinais verificáveis por nós, em `[0, 1]`."""
    total = 0.0
    if categoria_valida:
        total += EVID_CATEGORIA
    if texto and len(texto) >= MIN_CHARS_TEXTO:
        total += EVID_TEXTO
    if tem_keyword_no_texto(texto, categoria) or tem_keyword_no_texto(nome_original, categoria):
        total += EVID_KEYWORD
    if 3 <= len(nome_sanitizado) <= 80:
        total += EVID_NOME
    return min(1.0, total)


def confianca_llm(conf_bruta: float, evid: float, amostras_concordam: bool | None = None) -> float:
    """`clamp(0.5*conf_llm + 0.5*evidencia, 0.0, 0.90)` (RF-60, RF-61)."""
    valor = 0.5 * float(conf_bruta) + 0.5 * float(evid)
    if amostras_concordam is True:
        valor += AJUSTE_AMOSTRAS_CONCORDAM
    elif amostras_concordam is False:
        valor += AJUSTE_AMOSTRAS_DISCORDAM
    return clamp(valor, CLAMP_MIN, TETO_LLM)


# --------------------------------------------------------------------------- #
# Entrada principal
# --------------------------------------------------------------------------- #


#: Quantas extensões parasitas remover de um nome sugerido (`x.json.pdf`).
_MAX_EXTENSOES_PARASITAS = 2


def remover_extensao_sugerida(bruto: str) -> str:
    """Tira a extensão que o modelo insiste em colar no `nome_sugerido`.

    Medido com o phi3:mini real: apesar do "SEM extensao" no prompt, ele devolve
    `matriz_curricular_unifesp.docx` e `extrato-de-conta-202604.json`. Sem esta
    poda o destino vira `...docx.docx`. Só sufixos **alfabéticos** e curtos são
    removidos, para não estragar `nota-fiscal-2026.05` (RF-57).
    """
    resultado = bruto
    for _ in range(_MAX_EXTENSOES_PARASITAS):
        candidato = Path(resultado)
        sufixo = candidato.suffix
        if not sufixo or not (2 <= len(sufixo) <= 6) or not sufixo[1:].isalpha():
            break
        if not candidato.stem:
            break
        resultado = candidato.stem
    return resultado


def nome_final_para(origem: Path, stem_sugerido: str | None = None) -> str:
    """Monta o nome final: stem sanitizado + extensão ORIGINAL.

    A extensão nunca vem do LLM (RF-57) e o stem sempre passa por
    `paths.sanitize_stem` (RF-58).
    """
    ext = paths.sanitize_ext(origem.suffix)
    if stem_sugerido:
        bruto = remover_extensao_sugerida(stem_sugerido)
    else:
        bruto = naming.stem_sem_sufixo_copia(origem.name)
    return paths.sanitize_stem(bruto) + ext


def classificar(caminho: Path | str, cfg, texto: str | None = None, conn=None) -> Decisao:
    """Decide o destino de um arquivo.

    1. tabela de extensões;
    2. se for documento de texto ambíguo/genérico, extrai o trecho e tenta o LLM;
    3. confiança abaixo de `CONFIDENCE_MIN` manda para o `_Inbox` com o nome
       original preservado (RF-26).

    `texto` pode vir pronto (usado nos testes e por quem já extraiu); quando é
    `None` e o LLM vai ser acionado, `extract.trecho` é chamado aqui.
    """
    origem = Path(caminho)
    categoria, confianca, tipo, subtipo, generico, motivo_generico = classificar_por_extensao(
        origem.name
    )
    via = VIA_EXTENSAO if categoria != rules.CAT_OUTROS or confianca > rules.CONF_DESCONHECIDO else VIA_FALLBACK
    motivo = Motivo.OK.value
    stem_sugerido: str | None = None

    if deve_escalar_para_llm(origem.name, confianca, cfg):
        if texto is None:
            extraido = extract.trecho(origem)
            texto = extraido.texto if extraido.ok else None
        resposta = llm.classificar(origem, cfg, texto, conn)
        if resposta is None:
            motivo = llm.motivo_da_falha(origem, cfg).value
        else:
            canonica = rules.categoria_valida(resposta.categoria)
            stem_bruto = paths.sanitize_stem(resposta.nome_sugerido or "")
            evid = evidencia(canonica is not None, texto, stem_bruto, origem.name, canonica or "")
            nova_conf = confianca_llm(resposta.confianca, evid)
            if canonica is None:
                motivo = Motivo.LLM_PARSE_ERROR.value
            elif nova_conf > confianca:
                categoria = canonica
                tipo, subtipo = rules.tipo_subtipo_da_categoria(canonica)
                confianca = nova_conf
                via = VIA_LLM
                motivo = Motivo.OK.value
                if len(stem_bruto) >= 3:
                    stem_sugerido = stem_bruto

    decisao = Decisao(
        origem=origem,
        categoria=categoria,
        nome_final=nome_final_para(origem, stem_sugerido),
        confianca=confianca,
        via=via,
        motivo=motivo,
        tipo=tipo,
        subtipo=subtipo,
        generico=generico,
        motivo_generico=motivo_generico,
        texto_amostra=texto,
    )

    if confianca < cfg.confidence_min:
        motivo_inbox = motivo if motivo != Motivo.OK.value else Motivo.BAIXA_CONFIANCA.value
        decisao = replace(
            decisao,
            para_inbox=True,
            motivo=motivo_inbox,
            nome_final=origem.name,  # nome preservado no _Inbox (RF-26)
        )
    return decisao
