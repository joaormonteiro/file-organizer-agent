"""Tabelas declarativas: extensões, categorias canônicas, keywords e blocklist.

Dados, não lógica. É o único lugar onde a subárvore de destino está escrita, e
serve tanto para `classify` quanto para o prompt e a validação da resposta do LLM.

Requisitos cobertos: RF-08, RF-12, RF-16, RF-17 (parte da tabela base).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

# --------------------------------------------------------------------------- #
# Escala de confiança base (ARQUITETURA §10.1)
# --------------------------------------------------------------------------- #

#: Extensão de família única e subtipo único (`.exe`, `.mp3`, `.mp4`).
CONF_UNICA = 0.95
#: Extensão de família única cujo subtipo é o *default* da família (`.jpg`).
CONF_DEFAULT_FAMILIA = 0.85
#: Extensão conhecida mas de destino intrinsecamente ambíguo (`.pdf`, `.zip`).
CONF_AMBIGUO = 0.60
#: Extensão desconhecida ou ausente.
CONF_DESCONHECIDO = 0.30


# --------------------------------------------------------------------------- #
# Categorias canônicas (subárvore relativa a TARGET_ROOT)
# --------------------------------------------------------------------------- #

CAT_MATRIZES = "Documentos/Academico/UNIFESP/Matrizes-Curriculares"
CAT_COMPROVANTES = "Documentos/Academico/UNIFESP/Comprovantes"
CAT_TRABALHOS = "Documentos/Academico/UNIFESP/Trabalhos"
CAT_HORARIOS = "Documentos/Academico/UNIFESP/Horarios"
CAT_CERTIFICADOS = "Documentos/Academico/Certificados"
CAT_EYECONNECT = "Documentos/Profissional/Eyeconnect"
CAT_EFFICIENCECO = "Documentos/Profissional/EfficienceCo"
CAT_CONTRATOS = "Documentos/Profissional/Contratos"
CAT_EXTRATOS = "Documentos/Financeiro/Extratos"
CAT_NOTAS_FISCAIS = "Documentos/Financeiro/Notas-Fiscais"
CAT_RG_CPF = "Documentos/Pessoal/Documentos-RG-CPF"
CAT_OUTROS = "Documentos/Pessoal/Outros"
CAT_INSTALADORES = "Softwares/Instaladores"
CAT_PORTATEIS = "Softwares/Portateis"
CAT_SCREENSHOTS = "Imagens/Screenshots"
CAT_FOTOS = "Imagens/Fotos"
CAT_VIDEOS = "Videos"
CAT_MUSICA = "Musica"

#: Enum canônico das pastas-alvo. Usado no prompt do LLM e na validação da
#: resposta: categoria fora desta tupla faz a resposta inteira ser descartada.
CATEGORIAS: tuple[str, ...] = (
    CAT_MATRIZES,
    CAT_COMPROVANTES,
    CAT_TRABALHOS,
    CAT_HORARIOS,
    CAT_CERTIFICADOS,
    CAT_EYECONNECT,
    CAT_EFFICIENCECO,
    CAT_CONTRATOS,
    CAT_EXTRATOS,
    CAT_NOTAS_FISCAIS,
    CAT_RG_CPF,
    CAT_OUTROS,
    CAT_INSTALADORES,
    CAT_PORTATEIS,
    CAT_SCREENSHOTS,
    CAT_FOTOS,
    CAT_VIDEOS,
    CAT_MUSICA,
)

#: Glosa de uma linha por categoria. Serve só para o prompt do LLM: medido com o
#: phi3:mini, a lista crua produzia forte viés pelo primeiro item (tudo caía em
#: Matrizes-Curriculares). Dado, não lógica.
DESCRICOES: dict[str, str] = {
    CAT_MATRIZES: "grade curricular do curso, lista de disciplinas por semestre",
    CAT_COMPROVANTES: "comprovante de matricula, declaracao de vinculo com a faculdade",
    CAT_TRABALHOS: "TCC, monografia, relatorio ou trabalho entregue na faculdade",
    CAT_HORARIOS: "horario de aulas, dias e salas das disciplinas",
    CAT_CERTIFICADOS: "certificado ou diploma de conclusao de curso",
    CAT_EYECONNECT: "documento do cliente Eyeconnect ou do produto EyeAgent",
    CAT_EFFICIENCECO: "documento do cliente EfficienceCo",
    CAT_CONTRATOS: "contrato, termo de compromisso, aditivo contratual",
    CAT_EXTRATOS: "extrato bancario, fatura de cartao, boleto",
    CAT_NOTAS_FISCAIS: "nota fiscal, NF-e, NFS-e, DANFE",
    CAT_RG_CPF: "RG, CPF, CNH, passaporte, documento de identidade",
    CAT_OUTROS: "qualquer outro documento pessoal que nao se encaixe acima",
    CAT_INSTALADORES: "instalador de programa",
    CAT_PORTATEIS: "programa portatil ou compactado",
    CAT_SCREENSHOTS: "captura de tela",
    CAT_FOTOS: "foto ou imagem",
    CAT_VIDEOS: "video",
    CAT_MUSICA: "audio ou musica",
}

_CATEGORIAS_NORMALIZADAS = {c.replace("\\", "/").strip("/").lower(): c for c in CATEGORIAS}


def categoria_valida(bruta: str | None) -> str | None:
    """Normaliza separadores e devolve a categoria canônica, ou `None`.

    Comparação estrita: nada de "consertar" categoria inventada pelo LLM (RF-56).
    """
    if not bruta:
        return None
    return _CATEGORIAS_NORMALIZADAS.get(str(bruta).replace("\\", "/").strip("/").lower())


def subtipo_de(categoria: str) -> str:
    """Último segmento da categoria — vira a coluna `subtipo`."""
    return categoria.replace("\\", "/").strip("/").split("/")[-1]


def ramo(categoria: str) -> str:
    """Ramo de topo da categoria: `Academico`, `Profissional`, `Financeiro`,
    `Pessoal`, `Softwares`, `Imagens`, `Videos` ou `Musica`.

    `Documentos/` é um prefixo estrutural, não um ramo: o que separa mundos é o
    segundo segmento. Duas categorias do mesmo ramo são **irmãs** e se
    corroboram; só categorias de ramos diferentes são sinal contraditório.
    """
    partes = categoria.replace("\\", "/").strip("/").split("/")
    if partes[0] == "Documentos" and len(partes) > 1:
        return partes[1]
    return partes[0]


# --------------------------------------------------------------------------- #
# Famílias e tabela de extensões
# --------------------------------------------------------------------------- #

FAM_SOFTWARE = "software"
FAM_COMPACTADO = "compactado"
FAM_IMAGEM = "imagem"
FAM_VIDEO = "video"
FAM_AUDIO = "audio"
FAM_DOCUMENTO = "documento"
FAM_PLANILHA = "planilha"
FAM_APRESENTACAO = "apresentacao"
FAM_DESCONHECIDO = "desconhecido"


@dataclass(frozen=True)
class RegraExt:
    """Uma linha da tabela de extensões."""

    familia: str
    categoria: str
    confianca: float

    @property
    def tipo(self) -> str:
        return self.familia

    @property
    def subtipo(self) -> str:
        return subtipo_de(self.categoria)


def _bloco(exts: tuple[str, ...], familia: str, categoria: str, confianca: float) -> dict[str, RegraExt]:
    return {e: RegraExt(familia, categoria, confianca) for e in exts}


#: Extensão (com ponto, minúscula) para a regra correspondente.
EXTENSOES: dict[str, RegraExt] = {
    # executáveis / instaladores: família única, subtipo único
    **_bloco((".exe", ".msi", ".msix", ".appx", ".apk"), FAM_SOFTWARE, CAT_INSTALADORES, CONF_UNICA),
    # scripts: família única, subtipo default da família
    **_bloco(
        (".bat", ".cmd", ".ps1", ".sh", ".vbs", ".jar"),
        FAM_SOFTWARE,
        CAT_PORTATEIS,
        CONF_DEFAULT_FAMILIA,
    ),
    # áudio e vídeo: família única, subtipo único
    **_bloco(
        (".mp3", ".flac", ".wav", ".m4a", ".aac", ".ogg", ".opus", ".wma"),
        FAM_AUDIO,
        CAT_MUSICA,
        CONF_UNICA,
    ),
    **_bloco(
        (".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v", ".flv", ".mpeg", ".mpg"),
        FAM_VIDEO,
        CAT_VIDEOS,
        CONF_UNICA,
    ),
    # imagens: família única, subtipo default (Fotos); keyword move para Screenshots
    **_bloco(
        (".jpg", ".jpeg", ".png", ".heic", ".heif", ".gif", ".bmp", ".webp", ".tiff", ".tif", ".avif"),
        FAM_IMAGEM,
        CAT_FOTOS,
        CONF_DEFAULT_FAMILIA,
    ),
    # documentos de texto: destino intrinsecamente ambíguo
    **_bloco(
        (".pdf", ".docx", ".doc", ".txt", ".md", ".rtf", ".odt", ".epub"),
        FAM_DOCUMENTO,
        CAT_OUTROS,
        CONF_AMBIGUO,
    ),
    **_bloco((".xlsx", ".xls", ".csv", ".ods", ".tsv"), FAM_PLANILHA, CAT_OUTROS, CONF_AMBIGUO),
    **_bloco((".pptx", ".ppt", ".odp"), FAM_APRESENTACAO, CAT_OUTROS, CONF_AMBIGUO),
    # compactados: podem ser software portátil ou qualquer outra coisa
    **_bloco(
        (".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso"),
        FAM_COMPACTADO,
        CAT_PORTATEIS,
        CONF_AMBIGUO,
    ),
}

#: Regra usada quando a extensão não está na tabela (ou não existe).
REGRA_DESCONHECIDA = RegraExt(FAM_DESCONHECIDO, CAT_OUTROS, CONF_DESCONHECIDO)

#: Extensões cujo conteúdo textual pode ser extraído — é o que autoriza acordar
#: o LLM (ARQUITETURA §8: um `.zip` genérico NÃO vai ao LLM).
EXTENSOES_TEXTO: frozenset[str] = frozenset(
    {".pdf", ".docx", ".doc", ".txt", ".md", ".rtf", ".odt"}
)


def por_extensao(ext: str) -> RegraExt:
    """Consulta a tabela; devolve `REGRA_DESCONHECIDA` para o que não conhece."""
    if not ext:
        return REGRA_DESCONHECIDA
    chave = ext.lower()
    if not chave.startswith("."):
        chave = "." + chave
    return EXTENSOES.get(chave, REGRA_DESCONHECIDA)


def tipo_subtipo_da_categoria(categoria: str) -> tuple[str, str]:
    """Melhor par (tipo, subtipo) para uma categoria — usado na recuperação."""
    canonica = categoria_valida(categoria) or CAT_OUTROS
    for regra in EXTENSOES.values():
        if regra.categoria == canonica:
            return regra.familia, subtipo_de(canonica)
    return FAM_DOCUMENTO, subtipo_de(canonica)


# --------------------------------------------------------------------------- #
# Keywords (ARQUITETURA §10.1)
# --------------------------------------------------------------------------- #


#: Eixo semântico de um grupo de keywords. Dois grupos de eixos **diferentes**
#: nunca se contradizem: "contrato" responde *que tipo de documento é* e
#: "eyeconnect" responde *de quem é* — juntos informam mais, não menos.
EIXO_TIPO = "tipo_de_documento"
EIXO_ORGANIZACAO = "organizacao"

#: Quem decide a categoria quando os dois eixos casam no mesmo nome.
#: A organização vence porque é o que a própria spec do vault faz no exemplo do
#: Query CLI: `contrato de estágio eyeconnect` mora em
#: `Documentos/Profissional/Eyeconnect/contrato-estagio-2025-03.pdf`.
PRECEDENCIA_EIXOS: tuple[str, ...] = (EIXO_ORGANIZACAO, EIXO_TIPO)


@dataclass(frozen=True)
class GrupoKeyword:
    """Um conjunto de termos que aponta para uma categoria.

    `familias` limita onde o grupo pode agir: `nota fiscal` num `.exe` é ruído,
    não sinal, e por isso não altera a categoria nem soma confiança.

    `eixo` diz *que pergunta o grupo responde*. Só grupos do mesmo eixo podem se
    contradizer — e ainda assim apenas quando apontam para ramos de topo
    diferentes (ver `ramo`).
    """

    nome: str
    termos: tuple[str, ...]
    categoria: str
    familias: frozenset[str]
    eixo: str = EIXO_TIPO


_DOCS = frozenset({FAM_DOCUMENTO, FAM_PLANILHA, FAM_APRESENTACAO})
_SOFT = frozenset({FAM_SOFTWARE, FAM_COMPACTADO})
_ORG = EIXO_ORGANIZACAO

KEYWORDS: tuple[GrupoKeyword, ...] = (
    GrupoKeyword("eyeconnect", ("eyeconnect", "eyeagent"), CAT_EYECONNECT, _DOCS, _ORG),
    GrupoKeyword("efficienceco", ("efficienceco", "efficience"), CAT_EFFICIENCECO, _DOCS, _ORG),
    GrupoKeyword("screenshot", ("screenshot", "captura de tela", "captura", "print"), CAT_SCREENSHOTS, frozenset({FAM_IMAGEM})),
    GrupoKeyword("nota-fiscal", ("nota fiscal", "nota-fiscal", "notafiscal", "nfe", "nfse", "danfe"), CAT_NOTAS_FISCAIS, _DOCS),
    GrupoKeyword("extrato", ("extrato", "fatura", "boleto"), CAT_EXTRATOS, _DOCS),
    GrupoKeyword("matriz-curricular", ("matriz curricular", "matriz", "curricular", "grade curricular"), CAT_MATRIZES, _DOCS),
    GrupoKeyword("horario", ("horario", "horarios", "grade de horarios"), CAT_HORARIOS, _DOCS),
    GrupoKeyword("comprovante", ("comprovante", "matricula"), CAT_COMPROVANTES, _DOCS),
    GrupoKeyword("trabalho", ("tcc", "monografia", "dissertacao", "relatorio de estagio"), CAT_TRABALHOS, _DOCS),
    GrupoKeyword("certificado", ("certificado", "diploma", "certificate"), CAT_CERTIFICADOS, _DOCS),
    GrupoKeyword("contrato", ("contrato", "aditivo contratual"), CAT_CONTRATOS, _DOCS),
    GrupoKeyword("rg-cpf", ("rg", "cpf", "identidade", "passaporte", "cnh"), CAT_RG_CPF, _DOCS),
    GrupoKeyword("instalador", ("instalador", "installer", "setup"), CAT_INSTALADORES, _SOFT),
    GrupoKeyword("portatil", ("portatil", "portable"), CAT_PORTATEIS, _SOFT),
)


def keywords_da_categoria(categoria: str) -> tuple[str, ...]:
    """Todos os termos que apontam para uma categoria (usado no cálculo de evidência)."""
    termos: list[str] = []
    for grupo in KEYWORDS:
        if grupo.categoria == categoria:
            termos.extend(grupo.termos)
    return tuple(termos)


# --------------------------------------------------------------------------- #
# Blocklist estática (camada 1 da ARQUITETURA §7)
# --------------------------------------------------------------------------- #

#: Extensões de download/gravação em andamento — descartadas sem enfileirar (RF-12).
EXTENSOES_PARCIAIS: frozenset[str] = frozenset(
    {
        ".crdownload",
        ".part",
        ".partial",
        ".download",
        ".opdownload",
        ".tmp",
        ".temp",
        ".!ut",
        ".aria2",
        ".filepart",
        ".bc!",
        ".dctmp",
    }
)

#: Prefixos de nome que indicam arquivo de controle (locks do Office, ocultos).
PREFIXOS_IGNORADOS: tuple[str, ...] = ("~$", ".")

#: Sufixos de nome que indicam backup de editor.
SUFIXOS_IGNORADOS: tuple[str, ...] = ("~",)


def nome_e_parcial(nome: str) -> bool:
    """`True` se o nome é de arquivo parcial/temporário e deve ser descartado."""
    if not nome:
        return True
    if nome.startswith(PREFIXOS_IGNORADOS):
        return True
    if nome.endswith(SUFIXOS_IGNORADOS):
        return True
    return Path(nome).suffix.lower() in EXTENSOES_PARCIAIS


# --------------------------------------------------------------------------- #
# Criação da árvore (sob demanda — nunca no import, RF-08)
# --------------------------------------------------------------------------- #


def pastas_da_arvore(target_root: Path | str, inbox_dirname: str = "_Inbox") -> list[Path]:
    """Lista, sem criar nada, todas as pastas que compõem a árvore de destino."""
    raiz = Path(target_root)
    pastas = [raiz / Path(c) for c in CATEGORIAS]
    inbox = raiz / inbox_dirname
    pastas.extend([inbox, inbox / "_Duplicados", inbox / "_Aguardando"])
    return pastas


def criar_arvore(target_root: Path | str, inbox_dirname: str = "_Inbox") -> list[Path]:
    """Cria a árvore de destino. Idempotente e chamada só em runtime."""
    criadas = pastas_da_arvore(target_root, inbox_dirname)
    for pasta in criadas:
        pasta.mkdir(parents=True, exist_ok=True)
    return criadas
