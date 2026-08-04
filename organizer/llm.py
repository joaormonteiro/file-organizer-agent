"""Fronteira com o Ollama: classificação por conteúdo (Fase 3).

Contrato inteiro em ARQUITETURA §9. Pontos não-negociáveis, todos verificáveis:

- o prompt vai por **stdin**, nunca por argv (RF-52) — elimina limite de linha de
  comando e problema de quoting no Windows;
- o processo é criado, aguardado e morre dentro de uma chamada de função; nenhum
  servidor é mantido vivo pelo agente (RF-51);
- categoria fora de `rules.CATEGORIAS` **descarta a resposta inteira** (RF-56);
- a extensão nunca vem do LLM (RF-57) — isto aqui só devolve `nome_sugerido`;
- Ollama ausente degrada graciosamente, sem quebrar nada (RF-53).

Requisitos cobertos: RF-51 a RF-56, RF-59, RF-62.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from organizer import db, log, rules
from organizer.queue import Motivo

#: Chave do cache de disponibilidade em `config_kv` e seu TTL de 1 h (RF-54).
CHAVE_CACHE = "llm_disponivel"
TTL_CACHE_SEGUNDOS = 3600

#: Chave que memoriza se esta instalação aceita as flags extras (ARQUITETURA §9).
CHAVE_FORMATO = "llm_aceita_flags_extras"

#: Tentativas de chamada ao LLM antes de mandar o arquivo para o `_Inbox` (RF-59).
MAX_TENTATIVAS_LLM = 2

#: Timeout do `ollama list` da checagem de disponibilidade.
TIMEOUT_LISTAGEM = 10

#: Tamanho do trecho na segunda tentativa do parser em cascata.
CHARS_RETRY = 200

#: Confiança usada quando o modelo devolve um valor inútil (RF-62).
CONFIANCA_PADRAO = 0.5

SEM_TEXTO = "(sem texto extraido)"

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_logger = log.get_logger("llm")

#: Um único WARNING por processo quando o Ollama não está disponível (RF-53).
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
    """O binário, o modelo ou a chamada falharam."""


class TimeoutLLM(RuntimeError):
    """O modelo estourou `LLM_TIMEOUT`."""


# --------------------------------------------------------------------------- #
# Disponibilidade
# --------------------------------------------------------------------------- #


def binario_presente(cfg) -> bool:
    """`True` se `OLLAMA_BIN` existe (aceita nome no PATH ou caminho absoluto)."""
    return shutil.which(cfg.ollama_bin) is not None


def _modelo_listado(cfg) -> bool:
    """Roda `ollama list` e procura o modelo configurado."""
    try:
        processo = subprocess.run(
            [cfg.ollama_bin, "list"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=TIMEOUT_LISTAGEM,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if processo.returncode != 0:
        return False
    saida = (processo.stdout or "").lower()
    nome = cfg.ollama_model.lower()
    # `ollama list` mostra `phi3:mini`; aceitamos também o nome sem a tag
    return nome in saida or nome.split(":", 1)[0] in saida


def avisar_indisponivel(cfg) -> Motivo:
    """Emite (uma única vez por processo) a instrução de instalação (RF-53)."""
    global _avisou
    if not _avisou:
        _avisou = True
        _logger.warning(
            "LLM indisponível — arquivos ambíguos vão para o _Inbox. "
            "Para habilitar: ollama pull %s",
            cfg.ollama_model,
        )
    return Motivo.LLM_INDISPONIVEL


def disponivel(cfg, conn=None) -> bool:
    """Ollama utilizável? Resultado cacheado em `config_kv` por 1 h (RF-54)."""
    if not cfg.llm_enabled:
        return False

    if conn is not None:
        cacheado = db.kv_get(conn, CHAVE_CACHE)
        if cacheado is not None:
            return cacheado == "1"

    resultado = binario_presente(cfg) and _modelo_listado(cfg)
    if conn is not None:
        db.kv_set(conn, CHAVE_CACHE, "1" if resultado else "0", TTL_CACHE_SEGUNDOS)
    if not resultado:
        avisar_indisponivel(cfg)
    return resultado


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
# Parser em cascata (RF-55)
# --------------------------------------------------------------------------- #


#: Sequência CSI (`ESC [ ... letra`) — é o que o `ollama run` injeta no stdout.
_CSI = re.compile(r"\x1b\[([0-9;?]*)([ -/]*)([@-~])")


def limpar_terminal(texto: str) -> str:
    """Desfaz o redesenho de terminal que o `ollama run` deixa vazar no stdout.

    Medido nesta máquina com ollama 0.32.5: ao quebrar linha, o CLI emite
    `ESC[8D` (recua 8 colunas) + `ESC[K` (apaga até o fim da linha) e reimprime
    o pedaço — **no meio de uma string JSON**, o que invalida o `json.loads`.
    `--nowordwrap` evita isso na origem; esta função é a segunda linha de
    defesa, para instalações que não conheçam a flag.

    `ESC[<n>D` é interpretado de verdade (apaga os `n` últimos caracteres já
    emitidos); as demais sequências são só cosméticas e somem.
    """
    if "\x1b" not in texto:
        return texto
    saida: list[str] = []
    posicao = 0
    for achado in _CSI.finditer(texto):
        saida.append(texto[posicao : achado.start()])
        posicao = achado.end()
        parametros, _, final = achado.groups()
        if final == "D":
            recuo = int(parametros) if parametros.isdigit() else 1
            acumulado = "".join(saida)
            saida = [acumulado[:-recuo] if recuo else acumulado]
    saida.append(texto[posicao:])
    limpo = "".join(saida).replace("\x1b", "")
    return "".join(ch for ch in limpo if ch >= " " or ch in "\n\r\t")


def normalizar_quebras_em_strings(texto: str) -> str:
    """Troca quebras de linha **dentro** de strings JSON por espaço.

    JSON não aceita caractere de controle cru dentro de string; o modelo às vezes quebra a
    linha no meio do campo `motivo`. Fora das strings, a formatação é preservada.
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


def _candidatos(bruto: str):
    """Formas sucessivamente mais agressivas de achar o objeto na saída."""
    vistos: set[str] = set()
    for base in (bruto.strip(), limpar_terminal(bruto).strip()):
        if not base:
            continue
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
    """Níveis 1 a 3 da cascata: JSON puro, sem cercas, objeto balanceado (RF-55).

    Cada nível é tentado duas vezes: sobre a saída crua e sobre a saída livre de
    sequências de terminal.
    """
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
#: phi3:mini às vezes devolve `"confiança"` / `"categoría"` — e a leitura crua
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
# Invocação
# --------------------------------------------------------------------------- #


def _matar_orfaos(cfg, desde: float) -> int:
    """Reforço do kill: derruba processos do Ollama nascidos durante esta chamada.

    `subprocess.run` já mata o filho direto ao estourar o timeout, mas não
    alcança netos — e no Windows um neto órfão não é reparentado, então não dá
    para achá-lo por `children()` depois que o intermediário morre.

    Dois critérios, sempre combinados com o horário de criação (só processos
    nascidos **durante esta chamada**), para nunca tocar num Ollama que já
    estava de pé — o servidor do aplicativo de desktop, por exemplo:

    1. o nome do executável é o de `OLLAMA_BIN` (o caso normal);
    2. a linha de comando cita ao mesmo tempo o binário configurado e o modelo
       — é o que pega um *wrapper* (`.cmd`, `.bat`, um interpretador) entre nós
       e o Ollama, que o critério 1 sozinho não enxerga.

    Limitação conhecida: um wrapper que não repasse nem o nome do binário nem o
    do modelo na linha de comando continua invisível. Não há como identificá-lo
    sem capturar o PID, o que exigiria trocar `subprocess.run` por `Popen`.
    """
    caminho = Path(shutil.which(cfg.ollama_bin) or cfg.ollama_bin)
    alvo = caminho.name.lower()
    marca = caminho.stem.lower()
    modelo = (cfg.ollama_model or "").lower()
    mortos = 0
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil é dependência do núcleo
        return 0

    def _e_nosso(info) -> bool:
        if (info.get("name") or "").lower() == alvo:
            return True
        linha = " ".join(info.get("cmdline") or []).lower()
        return bool(marca and modelo and marca in linha and modelo in linha)

    for processo in psutil.process_iter(["name", "create_time", "cmdline"]):
        try:
            if (processo.info["create_time"] or 0) < desde:
                continue
            if not _e_nosso(processo.info):
                continue
            for filho in processo.children(recursive=True):
                try:
                    filho.kill()
                    mortos += 1
                except Exception:
                    continue
            processo.kill()
            mortos += 1
        except Exception:
            continue
    if mortos:
        _logger.warning("timeout do LLM: %s processo(s) órfão(s) do Ollama derrubado(s)", mortos)
    return mortos


#: Flags opcionais. `--format json` pede JSON ao modelo; `--nowordwrap` impede o
#: CLI de reposicionar o cursor no meio da resposta (ver `limpar_terminal`).
#: Instalações antigas podem não conhecê-las — daí serem opcionais.
FLAGS_EXTRAS = ("--format", "json", "--nowordwrap")


def argumentos(cfg, com_extras: bool = True) -> list[str]:
    """Linha de comando. O prompt **não** aparece aqui — ele vai por stdin (RF-52)."""
    argv = [cfg.ollama_bin, "run", cfg.ollama_model]
    if com_extras:
        argv += list(FLAGS_EXTRAS)
    return argv


def _flag_rejeitada(stderr: str | None) -> bool:
    texto = (stderr or "").lower()
    return "unknown flag" in texto or "unknown shorthand" in texto


def executar(cfg, prompt: str, conn=None, _com_extras: bool | None = None) -> str:
    """Roda o modelo uma vez e devolve o stdout cru.

    O processo nasce, é aguardado e morre dentro desta função: nenhum servidor
    fica de pé por nossa conta (RF-51).
    """
    if _com_extras is None:
        _com_extras = db.kv_get(conn, CHAVE_FORMATO) != "0" if conn is not None else True

    inicio = time.time()
    try:
        processo = subprocess.run(
            argumentos(cfg, _com_extras),
            input=prompt,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=cfg.llm_timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        _matar_orfaos(cfg, inicio)
        raise TimeoutLLM(f"ollama excedeu {cfg.llm_timeout}s") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise Indisponivel(str(exc)) from exc

    if _com_extras and processo.returncode != 0 and _flag_rejeitada(processo.stderr):
        # esta instalação não conhece as flags: memoriza e refaz sem elas
        _logger.info("ollama rejeitou as flags extras; usando só o parser tolerante")
        if conn is not None:
            db.kv_set(conn, CHAVE_FORMATO, "0")
        return executar(cfg, prompt, conn, _com_extras=False)

    if processo.returncode != 0:
        raise Indisponivel(
            f"ollama saiu com {processo.returncode}: {(processo.stderr or '')[:200]}"
        )
    return processo.stdout or ""


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
            _logger.warning("ollama indisponível durante a chamada: %s", exc)
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
