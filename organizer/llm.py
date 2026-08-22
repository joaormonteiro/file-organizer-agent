"""Fronteira com a API do Gemini: classificação por conteúdo (Fase 3).

Contrato de saída inalterado desde a versão Ollama (ARQUITETURA §9). Pontos
não-negociáveis, todos verificáveis:

- cada classificação é **uma requisição HTTP síncrona**; nenhuma conexão fica
  aberta por nossa conta entre chamadas (equivalente a RF-51);
- categoria fora de `rules.CATEGORIAS` **descarta a resposta inteira** (RF-56)
  — reforçado na origem via `responseSchema` com `enum`, mas validado de novo
  aqui porque a API pode ignorar o schema em respostas bloqueadas por safety;
- a extensão nunca vem do LLM (RF-57) — isto aqui só devolve `nome_sugerido`;
- API indisponível (sem chave, rede fora, erro HTTP) degrada graciosamente,
  sem quebrar nada (equivalente a RF-53).

Requisitos cobertos: RF-51 a RF-56, RF-59, RF-62 (adaptados de Ollama→Gemini).
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from organizer import log, rules
from organizer.queue import Motivo

#: Base da API REST do Gemini (v1beta — `responseSchema` ainda não é GA em v1).
ENDPOINT_BASE = "https://generativelanguage.googleapis.com/v1beta/models"

#: Tentativas de chamada ao LLM antes de mandar o arquivo para o `_Inbox` (RF-59).
MAX_TENTATIVAS_LLM = 2

#: Tamanho do trecho na segunda tentativa do parser em cascata.
CHARS_RETRY = 200

#: Confiança usada quando o modelo devolve um valor inútil (RF-62).
CONFIANCA_PADRAO = 0.5

#: Temperatura baixa: queremos a classificação mais provável, não criatividade.
TEMPERATURA = 0.1

SEM_TEXTO = "(sem texto extraido)"

_logger = log.get_logger("llm")

#: Um único WARNING por processo quando a API não está disponível (RF-53).
_avisou = False

#: Por que `classificar` devolveu `None` para cada arquivo — lido por `classify`.
_motivo_da_falha: dict[Path, Motivo] = {}


@dataclass(frozen=True)
class RespostaLLM:
    """Contrato de saída do modelo (ARQUITETURA §9)."""

    categoria: str
    nome_sugerido: str
    confianca: float
    motivo: str


class Indisponivel(RuntimeError):
    """A chave, a rede ou a chamada falharam."""


class TimeoutLLM(RuntimeError):
    """O modelo estourou `LLM_TIMEOUT`."""


# --------------------------------------------------------------------------- #
# Disponibilidade
# --------------------------------------------------------------------------- #


def avisar_indisponivel(cfg) -> Motivo:
    """Emite (uma única vez por processo) a instrução de configuração (RF-53)."""
    global _avisou
    if not _avisou:
        _avisou = True
        _logger.warning(
            "Gemini indisponível — arquivos ambíguos vão para o _Inbox. "
            "Gere uma chave em https://aistudio.google.com/apikey e defina "
            "GEMINI_API_KEY no .env"
        )
    return Motivo.LLM_INDISPONIVEL


def disponivel(cfg, conn=None) -> bool:
    """Gemini utilizável? Só depende de `LLM_ENABLED` e da chave estar presente.

    Diferente do Ollama, não há um "binário instalado" para checar — a
    verificação é O(1) e não precisa de cache (RF-54 deixou de fazer sentido
    aqui: cachear uma comparação de string não economiza nada).
    """
    if not cfg.llm_enabled:
        return False
    if not cfg.gemini_api_key:
        avisar_indisponivel(cfg)
        return False
    return True


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #

_INSTRUCAO = (
    "Responda SOMENTE com um objeto JSON, sem markdown, sem explicacao, "
    "exatamente com as chaves: categoria, nome_sugerido, confianca, motivo. "
    "O campo motivo tem no maximo 10 palavras, numa unica linha."
)

#: Few-shot. Cobre justamente as quatro categorias que o phi3:mini errava sem
#: exemplo (medido na auditoria: Notas-Fiscais, Certificados, RG-CPF, Trabalhos).
#: Mantido com o Gemini: o few-shot ajuda tanto quanto o `responseSchema` ajuda
#: a forma — um restringe a sintaxe, o outro guia o julgamento.
#: São exemplos inventados; nenhum arquivo real do usuário foi usado.
EXEMPLOS = """Exemplos de resposta correta:

trecho: "NOTA FISCAL DE SERVICOS ELETRONICA NFS-e numero 004521 Prestador ..."
{"categoria": "Documentos/Financeiro/Notas-Fiscais", "nome_sugerido": "nfse-004521-horizonte-2026-05", "confianca": 0.9, "motivo": "cabecalho declara NFS-e"}

trecho: "CERTIFICADO Certificamos que Fulano concluiu o curso de ... 120 horas"
{"categoria": "Documentos/Academico/Certificados", "nome_sugerido": "certificado-curso-machine-learning-2026", "confianca": 0.9, "motivo": "certifica conclusao de curso"}

trecho: "CARTEIRA DE IDENTIDADE Registro Geral 12.345.678-9 CPF 123.456.789-00"
{"categoria": "Documentos/Pessoal/Documentos-RG-CPF", "nome_sugerido": "rg-cpf-fulano", "confianca": 0.9, "motivo": "documento de identidade civil"}

trecho: "TRABALHO DE CONCLUSAO DE CURSO Titulo: ... Resumo: ... Orientador: ..."
{"categoria": "Documentos/Academico/UNIFESP/Trabalhos", "nome_sugerido": "tcc-deteccao-anomalias-series-temporais", "confianca": 0.9, "motivo": "monografia com orientador"}

trecho: "MATRIZ CURRICULAR Grade do curso. Primeiro semestre: Calculo I, Algoritmos I ..."
{"categoria": "Documentos/Academico/UNIFESP/Matrizes-Curriculares", "nome_sugerido": "matriz-curricular-engenharia-computacao", "confianca": 0.9, "motivo": "lista disciplinas por semestre"}

trecho: "COMPROVANTE DE MATRICULA Aluno: ... RA ... Situacao: MATRICULADO 2026/1"
{"categoria": "Documentos/Academico/UNIFESP/Comprovantes", "nome_sugerido": "comprovante-matricula-2026-1", "confianca": 0.9, "motivo": "comprova vinculo com a faculdade"}

trecho: "HORARIO DE AULAS Segunda 08:00 Calculo III sala B12; Terca 08:00 Redes ..."
{"categoria": "Documentos/Academico/UNIFESP/Horarios", "nome_sugerido": "horario-aulas-2026-1", "confianca": 0.9, "motivo": "dias horas e salas das aulas"}

trecho: "EXTRATO DE CONTA CORRENTE Banco ... Saldo anterior ... Lancamentos ..."
{"categoria": "Documentos/Financeiro/Extratos", "nome_sugerido": "extrato-conta-2026-04", "confianca": 0.9, "motivo": "extrato bancario com lancamentos"}

Os exemplos acima sao apenas ilustrativos: NAO copie a categoria do ultimo
exemplo. Leia o trecho do arquivo em questao e escolha pelo titulo dele.
Agora classifique o arquivo acima."""


def montar_prompt(origem: Path, texto: str | None, max_chars: int = 500) -> str:
    """Prompt em PT-BR com a lista fechada de categorias (ARQUITETURA §9)."""
    alvo = Path(origem)
    try:
        st = alvo.stat()
        tamanho = st.st_size
        data = time.strftime("%Y-%m-%d", time.localtime(st.st_mtime))
    except OSError:
        tamanho, data = 0, "desconhecida"

    amostra = " ".join((texto or "").split())[:max_chars] or SEM_TEXTO
    categorias = "\n".join(
        f"- {categoria}  ({rules.DESCRICOES.get(categoria, '')})" for categoria in rules.CATEGORIAS
    )
    return (
        "Voce classifica arquivos baixados em uma arvore de pastas fixa.\n\n"
        "Categorias possiveis (a glosa entre parenteses e so explicacao):\n"
        f"{categorias}\n\n"
        "Arquivo:\n"
        f"nome_original: {alvo.name}\n"
        f"extensao: {alvo.suffix.lower()}\n"
        f"tamanho_bytes: {tamanho}\n"
        f"data_download: {data}\n"
        f"trecho: {amostra}\n\n"
        "Como decidir:\n"
        "1. Olhe o TITULO do documento no inicio do trecho (costuma vir em caixa "
        "alta: NOTA FISCAL, CONTRATO, COMPROVANTE DE MATRICULA, CERTIFICADO, "
        "EXTRATO, HORARIO DE AULAS, TRABALHO DE CONCLUSAO, CARTEIRA DE IDENTIDADE). "
        "Esse titulo e o sinal mais forte e quase sempre decide sozinho.\n"
        "2. Ignore o nome do arquivo: ele costuma ser generico e inutil.\n"
        "3. Um documento que so CERTIFICA conclusao de curso e Certificados, nao "
        "Trabalhos. Um que comprova vinculo com a faculdade e Comprovantes, nao "
        "Contratos. Uma lista de disciplinas por semestre e Matrizes-Curriculares; "
        "uma lista de dias e horas de aula e Horarios.\n"
        "4. Copie a categoria exatamente como esta na lista, sem a glosa. "
        f"Se o trecho nao permitir decidir, use {rules.CAT_OUTROS} com confianca baixa.\n\n"
        f"{EXEMPLOS}\n"
        "Sugira tambem um nome curto e descritivo, em minusculas, sem acento, "
        "palavras separadas por hifen, SEM extensao e SEM ponto.\n"
        "confianca e um numero entre 0 e 1.\n\n"
        f"{_INSTRUCAO}"
    )


def prompt_de_retry(prompt: str) -> str:
    """Prompt encurtado da 4ª etapa da cascata (ARQUITETURA §9)."""
    linhas = []
    for linha in prompt.splitlines():
        if linha.startswith("trecho: "):
            corpo = linha[len("trecho: ") :][:CHARS_RETRY]
            linha = "trecho: " + corpo
        linhas.append(linha)
    return "\n".join(linhas) + "\n\nSua ultima resposta foi invalida. Responda SOMENTE o JSON."


# --------------------------------------------------------------------------- #
# Parser em cascata (RF-55) — rede de segurança mesmo com responseSchema
# --------------------------------------------------------------------------- #


def sem_cercas(texto: str) -> str:
    """Remove cercas de markdown (``` ou ```json) em volta do JSON."""
    limpo = texto.strip()
    if "```" not in limpo:
        return limpo
    for parte in limpo.split("```")[1:]:
        corpo = parte
        primeira, _, resto = parte.partition("\n")
        if primeira.strip().isalpha():
            corpo = resto
        if "{" in corpo:
            return corpo.strip()
    return limpo


def primeiro_objeto(texto: str) -> str | None:
    """Primeiro objeto `{...}` balanceado, por varredura de chaves.

    Ignora chaves dentro de strings: um `nome_sugerido` contendo `{` quebraria a
    contagem ingênua.
    """
    profundidade = 0
    inicio = -1
    em_string = False
    escapado = False
    for posicao, ch in enumerate(texto):
        if em_string:
            if escapado:
                escapado = False
            elif ch == "\\":
                escapado = True
            elif ch == '"':
                em_string = False
            continue
        if ch == '"':
            em_string = True
        elif ch == "{":
            if profundidade == 0:
                inicio = posicao
            profundidade += 1
        elif ch == "}":
            profundidade -= 1
            if profundidade == 0 and inicio >= 0:
                return texto[inicio : posicao + 1]
    return None


def normalizar_quebras_em_strings(texto: str) -> str:
    """Troca quebras de linha **dentro** de strings JSON por espaço.

    JSON não aceita caractere de controle cru dentro de string; mantido como
    rede de segurança caso a API devolva um `motivo` com quebra de linha crua.
    """
    saida: list[str] = []
    em_string = False
    escapado = False
    for ch in texto:
        if em_string:
            if escapado:
                escapado = False
            elif ch == "\\":
                escapado = True
            elif ch == '"':
                em_string = False
            if ch in "\n\r\t" and not escapado:
                saida.append(" ")
                continue
        elif ch == '"':
            em_string = True
        saida.append(ch)
    return "".join(saida)


def _candidatos(bruto: str):
    """Formas sucessivamente mais agressivas de achar o objeto na saída."""
    vistos: set[str] = set()
    base = bruto.strip()
    if not base:
        return
    objeto = primeiro_objeto(base)
    for candidato in (
        base,
        sem_cercas(base),
        objeto,
        normalizar_quebras_em_strings(objeto) if objeto else None,
    ):
        if candidato and candidato not in vistos:
            vistos.add(candidato)
            yield candidato


def parsear(bruto: str | None) -> dict | None:
    """Níveis 1 a 3 da cascata: JSON puro, sem cercas, objeto balanceado (RF-55)."""
    if not bruto or not bruto.strip():
        return None
    for candidato in _candidatos(bruto):
        try:
            dados = json.loads(candidato)
        except (ValueError, TypeError):
            continue
        if isinstance(dados, dict):
            return dados
    return None


# --------------------------------------------------------------------------- #
# Validação semântica — rejeita, não conserta (RF-56, RF-62)
# --------------------------------------------------------------------------- #


def normalizar_confianca(bruta) -> float:
    """Não numérica ou fora de `[0,1]` vira 0.5 (RF-62)."""
    try:
        valor = float(bruta)
    except (TypeError, ValueError):
        return CONFIANCA_PADRAO
    if valor != valor or not (0.0 <= valor <= 1.0):  # NaN cai aqui também
        return CONFIANCA_PADRAO
    return valor


#: Grafias que o modelo usa para cada chave. O prompt pede sem acento, mas o
#: modelo às vezes devolve `"confiança"` / `"categoría"` — e a leitura crua
#: caía em silêncio no default de 0.5, jogando fora informação boa.
_SINONIMOS_DE_CHAVE = {
    "categoria": ("categoria", "categoría", "category"),
    "nome_sugerido": ("nome_sugerido", "nome sugerido", "nome", "suggested_name", "name"),
    "confianca": ("confianca", "confiança", "confidence", "confidencia"),
    "motivo": ("motivo", "razao", "razão", "reason", "justificativa"),
}


def normalizar_chaves(dados: dict) -> dict:
    """Mapeia as grafias aceitas para os nomes canônicos do contrato."""
    minusculas = {str(k).strip().lower(): v for k, v in dados.items()}
    normalizado = dict(dados)
    for canonica, grafias in _SINONIMOS_DE_CHAVE.items():
        if canonica in dados:
            continue
        for grafia in grafias:
            if grafia in minusculas:
                normalizado[canonica] = minusculas[grafia]
                break
    return normalizado


def validar(dados: dict | None) -> RespostaLLM | None:
    """Converte o dicionário cru em `RespostaLLM`, ou descarta tudo.

    Categoria fora do enum canônico invalida a resposta **inteira** (RF-56):
    nada de aproveitar o `nome_sugerido` de quem já errou o mais fácil.
    """
    if not isinstance(dados, dict):
        return None
    dados = normalizar_chaves(dados)
    canonica = rules.categoria_valida(dados.get("categoria"))
    if canonica is None:
        return None

    bruta = dados.get("confianca")
    confianca = normalizar_confianca(bruta)
    if bruta is None or confianca != _como_float(bruta):
        _logger.warning(
            "resposta do LLM sem confianca utilizável (%r) — usando %.2f. "
            "Chaves recebidas: %s",
            bruta,
            confianca,
            sorted(str(k) for k in dados),
        )

    return RespostaLLM(
        categoria=canonica,
        nome_sugerido=str(dados.get("nome_sugerido") or ""),
        confianca=confianca,
        motivo=str(dados.get("motivo") or ""),
    )


def _como_float(bruta):
    try:
        return float(bruta)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Invocação HTTP
# --------------------------------------------------------------------------- #


def _schema_resposta() -> dict:
    """`responseSchema` que restringe `categoria` ao enum fechado na origem.

    Não substitui `validar()` — uma resposta bloqueada por safety ou cortada
    por `MAX_TOKENS` ainda pode chegar fora do schema — mas reduz bastante a
    taxa de categoria inválida que o phi3:mini produzia sem essa amarra.
    """
    return {
        "type": "OBJECT",
        "properties": {
            "categoria": {"type": "STRING", "enum": list(rules.CATEGORIAS)},
            "nome_sugerido": {"type": "STRING"},
            "confianca": {"type": "NUMBER"},
            "motivo": {"type": "STRING"},
        },
        "required": ["categoria", "nome_sugerido", "confianca", "motivo"],
    }


def _extrair_texto(corpo_resposta: str) -> str:
    """Puxa o texto gerado do envelope JSON da API do Gemini.

    Resposta vazia (bloqueio de safety, MAX_TOKENS antes do JSON fechar) não
    quebra — vira string vazia e cai no parser em cascata como qualquer saída
    inválida (RF-55), que manda o arquivo para o `_Inbox`.
    """
    try:
        dados = json.loads(corpo_resposta)
    except (ValueError, TypeError):
        return ""
    candidatos = dados.get("candidates") or []
    if not candidatos:
        return ""
    partes = (candidatos[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in partes if isinstance(p, dict))


def executar(cfg, prompt: str, conn=None) -> str:
    """Chama a API do Gemini uma vez e devolve o texto cru da resposta.

    Uma requisição HTTP síncrona por chamada: nenhuma conexão fica de pé por
    nossa conta (equivalente a RF-51). `conn` é aceito por compatibilidade com
    `search.rerankear`, que não precisa mais dele.
    """
    corpo = json.dumps(
        {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": TEMPERATURA,
                "responseMimeType": "application/json",
                "responseSchema": _schema_resposta(),
            },
        }
    ).encode("utf-8")

    requisicao = urllib.request.Request(
        f"{ENDPOINT_BASE}/{cfg.gemini_model}:generateContent",
        data=corpo,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": cfg.gemini_api_key,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(requisicao, timeout=cfg.llm_timeout) as resposta:
            bruto = resposta.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detalhe = exc.read().decode("utf-8", errors="replace")[:200]
        raise Indisponivel(f"gemini respondeu {exc.code}: {detalhe}") from exc
    except TimeoutError as exc:
        raise TimeoutLLM(f"gemini excedeu {cfg.llm_timeout}s") from exc
    except urllib.error.URLError as exc:
        raise Indisponivel(str(exc.reason)) from exc
    except OSError as exc:
        raise Indisponivel(str(exc)) from exc

    return _extrair_texto(bruto)


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


def classificar(origem: Path, cfg, texto: str | None = None, conn=None) -> RespostaLLM | None:
    """Classifica um arquivo por conteúdo. `None` quando não deu — nunca levanta.

    Cascata completa: chamada, parsing tolerante e **um** retry com prompt
    encurtado (RF-55). Falhou tudo → `None`, e o chamador manda para o `_Inbox`.
    """
    alvo = Path(origem)
    if not disponivel(cfg, conn):
        _motivo_da_falha[alvo] = Motivo.LLM_INDISPONIVEL
        return None

    prompt = montar_prompt(alvo, texto)
    for tentativa in range(1, MAX_TENTATIVAS_LLM + 1):
        try:
            bruto = executar(cfg, prompt, conn)
        except TimeoutLLM:
            _logger.warning("timeout do LLM em %s (tentativa %s)", alvo.name, tentativa)
            if tentativa >= MAX_TENTATIVAS_LLM:
                _motivo_da_falha[alvo] = Motivo.LLM_TIMEOUT
                return None
            prompt = prompt_de_retry(prompt)
            continue
        except Indisponivel as exc:
            _logger.warning("gemini indisponível durante a chamada: %s", exc)
            _motivo_da_falha[alvo] = Motivo.LLM_INDISPONIVEL
            return None

        resposta = validar(parsear(bruto))
        if resposta is not None:
            _motivo_da_falha.pop(alvo, None)
            return resposta

        _logger.info(
            "resposta inválida do LLM para %s (tentativa %s): %r",
            alvo.name,
            tentativa,
            (bruto or "")[:200],
        )
        prompt = prompt_de_retry(prompt)

    _motivo_da_falha[alvo] = Motivo.LLM_PARSE_ERROR
    return None


def motivo_da_falha(origem: Path, cfg) -> Motivo:
    """Por que `classificar` devolveu `None` para este arquivo."""
    motivo = _motivo_da_falha.pop(Path(origem), None)
    if motivo is None or motivo == Motivo.LLM_INDISPONIVEL:
        return avisar_indisponivel(cfg)
    return motivo


def ultimo_motivo(cfg) -> Motivo:
    """Compatibilidade com a Fase 2: motivo quando não houve arquivo específico."""
    return avisar_indisponivel(cfg)
