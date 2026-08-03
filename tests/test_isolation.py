"""RF-01, RF-05, RF-27, RNF-01, RNF-02, RNF-04, RNF-06, RNF-10, RNF-19.

As auditorias por leitura de código. Todas ignoram docstrings e comentários:
o que é proibido é a **instrução**, não a menção em prosa.
"""

from __future__ import annotations

import ast
import importlib
import io
import os
import re
import shutil
import tokenize
from pathlib import Path

import pytest

RAIZ_PROJETO = Path(__file__).resolve().parent.parent
PACOTE = RAIZ_PROJETO / "organizer"
MODULOS = sorted(PACOTE.glob("*.py"))
ENTRYPOINTS = sorted(RAIZ_PROJETO.glob("*.py"))


# --------------------------------------------------------------------------- #
# Utilitário: código efetivo (sem docstrings, sem comentários)
# --------------------------------------------------------------------------- #


def linhas_efetivas(caminho: Path) -> list[tuple[int, str]]:
    """Linhas de código de verdade, com docstrings e comentários removidos."""
    fonte = caminho.read_text(encoding="utf-8")
    linhas = fonte.splitlines()

    docstrings: set[int] = set()
    arvore = ast.parse(fonte)
    for no in ast.walk(arvore):
        if not isinstance(no, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        corpo = no.body
        if (
            corpo
            and isinstance(corpo[0], ast.Expr)
            and isinstance(corpo[0].value, ast.Constant)
            and isinstance(corpo[0].value.value, str)
        ):
            docstrings.update(range(corpo[0].lineno, (corpo[0].end_lineno or corpo[0].lineno) + 1))

    cortes: dict[int, int] = {}
    for token in tokenize.generate_tokens(io.StringIO(fonte).readline):
        if token.type == tokenize.COMMENT:
            numero, coluna = token.start
            cortes[numero] = min(cortes.get(numero, 10**9), coluna)

    resultado = []
    for numero, texto in enumerate(linhas, start=1):
        if numero in docstrings:
            continue
        if numero in cortes:
            texto = texto[: cortes[numero]]
        if texto.strip():
            resultado.append((numero, texto))
    return resultado


def ocorrencias(arquivos, padrao: re.Pattern) -> list[str]:
    achados = []
    for arquivo in arquivos:
        for numero, texto in linhas_efetivas(arquivo):
            if padrao.search(texto):
                achados.append(f"{arquivo.name}:{numero}: {texto.strip()}")
    return achados


# --------------------------------------------------------------------------- #
# RF-01 — nenhuma raiz de caminho literal
# --------------------------------------------------------------------------- #

#: Raiz de unidade (`C:\` ou `C:/`). O lookbehind evita casar com o `o:\n` de
#: `"Arquivo:\n"` — numa raiz de verdade a letra vem depois de aspas ou espaço,
#: nunca colada em outra letra.
_RAIZ_DE_UNIDADE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]")
_PASTA_DE_USUARIO = re.compile(r"Downloads|joaor|Users[\\/]")


def test_regex_de_raiz_realmente_pega_um_caminho_hardcoded():
    """O detector precisa ser provado antes de servir de barreira."""
    assert _RAIZ_DE_UNIDADE.search(r'DOWNLOADS = "C:\Users\joaor\Downloads"')
    assert _RAIZ_DE_UNIDADE.search('BASE = "d:/dados"')
    assert _PASTA_DE_USUARIO.search(r'p = Path("C:/Users/alguem/Downloads")')
    # e não pode disparar em sequência de escape dentro de texto comum
    assert not _RAIZ_DE_UNIDADE.search('"Arquivo:\\n"')
    assert not _RAIZ_DE_UNIDADE.search('"copiando o texto igual:\\n"')


def test_sem_paths_hardcoded():
    """RF-01: nenhuma raiz de caminho aparece como literal em `organizer/`."""
    problemas = ocorrencias(MODULOS, _RAIZ_DE_UNIDADE) + ocorrencias(MODULOS, _PASTA_DE_USUARIO)
    assert problemas == [], "caminho hardcoded encontrado:\n" + "\n".join(problemas)


def test_config_e_a_unica_fonte_de_raizes():
    """RF-01: todo módulo que precisa de uma raiz a recebe de `config`."""
    fonte = (PACOTE / "config.py").read_text(encoding="utf-8")
    for chave in ("DOWNLOADS_DIR", "TARGET_ROOT", "DB_PATH", "LOG_DIR", "INBOX_DIRNAME"):
        assert chave in fonte


# --------------------------------------------------------------------------- #
# RF-05 — SQL só em db.py
# --------------------------------------------------------------------------- #

_SQL = re.compile(
    r"\b(SELECT|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|CREATE\s+(TABLE|INDEX|TRIGGER|VIRTUAL)|DROP\s+TABLE|PRAGMA)\b"
)
_DRIVER = re.compile(r"\bsqlite3\b")


def test_sql_apenas_em_db():
    """RF-05: `sqlite3` e comandos SQL não existem fora de `organizer/db.py`."""
    outros = [m for m in MODULOS if m.name != "db.py"]
    problemas = ocorrencias(outros, _SQL) + ocorrencias(outros, _DRIVER)
    assert problemas == [], "SQL fora de db.py:\n" + "\n".join(problemas)
    assert ocorrencias([PACOTE / "db.py"], _SQL), "db.py deveria conter o SQL"


# --------------------------------------------------------------------------- #
# RNF-06 — deleção só em move.py
# --------------------------------------------------------------------------- #

_DELECAO = re.compile(r"os\.remove\(|os\.unlink\(|shutil\.rmtree\(|\.unlink\(|\.rmdir\(")


def test_delete_apenas_em_move():
    """RNF-06: nenhuma primitiva de deleção fora de `organizer/move.py`."""
    outros = [m for m in MODULOS if m.name != "move.py"]
    problemas = ocorrencias(outros, _DELECAO)
    assert problemas == [], "deleção fora de move.py:\n" + "\n".join(problemas)


def test_move_tem_exatamente_duas_delecoes():
    """RNF-07: as duas deleções autorizadas, ambas logadas em INFO."""
    achados = ocorrencias([PACOTE / "move.py"], _DELECAO)
    assert len(achados) == 2, achados
    fonte = (PACOTE / "move.py").read_text(encoding="utf-8")
    assert "removendo destino criado por este processo" in fonte
    assert "removendo origem" in fonte


def test_delecao_de_destino_exige_registro_em_reservas():
    """RNF-07 (a): a pré-condição é o registro em `reservas`, não o tamanho."""
    from organizer import move

    fonte = ocorrencias([PACOTE / "move.py"], re.compile(r"os\.unlink\("))
    assert len(fonte) == 2
    codigo = (PACOTE / "move.py").read_text(encoding="utf-8")
    guarda = codigo.split("def _descartar_reserva")[1].split("def ")[0]
    assert "db.reserva_por_op" in guarda
    assert "registro is None" in guarda
    assert "permitir_parcial" in guarda


def test_hash_completo_antes_de_apagar_a_origem():
    """D2/RNF-07 (b): a conferência que precede a deleção usa o arquivo inteiro."""
    corpo = (PACOTE / "move.py").read_text(encoding="utf-8")
    trecho = corpo.split("def _mover_entre_volumes")[1].split("\ndef ")[0]
    assert "paths.sha256_arquivo(origem, completo=True)" in trecho
    assert "paths.sha256_arquivo(alvo, completo=True)" in trecho
    assert "os.unlink" in trecho


# --------------------------------------------------------------------------- #
# RF-27 — nada é executado
# --------------------------------------------------------------------------- #

_EXECUCAO = re.compile(r"os\.startfile|os\.system|os\.exec|os\.popen|eval\(|exec\(")
_SUBPROCESSO = re.compile(r"\bsubprocess\b")

#: Os únicos módulos autorizados a criar processos: o Ollama e o spawn do worker.
MODULOS_COM_SUBPROCESSO = {"llm.py", "watch.py"}


def test_nunca_executa_arquivo_do_usuario():
    """RF-27: `.exe`, `.msi`, `.bat`, `.cmd` e `.ps1` são só nome, extensão e tamanho."""
    problemas = ocorrencias(MODULOS, _EXECUCAO)
    assert problemas == [], "execução encontrada:\n" + "\n".join(problemas)

    indevidos = ocorrencias(
        [m for m in MODULOS if m.name not in MODULOS_COM_SUBPROCESSO], _SUBPROCESSO
    )
    assert indevidos == [], "subprocess indevido:\n" + "\n".join(indevidos)

    fonte_watch = (PACOTE / "watch.py").read_text(encoding="utf-8")
    assert "organizer.worker" in fonte_watch, "o único Popen é o do worker"


def test_executaveis_sao_classificados_so_por_extensao(sandbox):
    from organizer import classify, rules

    for nome in ("virus.exe", "instala.msi", "roda.bat", "script.ps1", "roda.cmd"):
        alvo = sandbox.downloads / nome
        alvo.write_bytes(b"MZ\x00conteudo que nunca sera executado")
        decisao = classify.classificar(alvo, sandbox.cfg)
        assert decisao.tipo == rules.FAM_SOFTWARE
        assert decisao.texto_amostra is None


# --------------------------------------------------------------------------- #
# RNF-19 — nenhum I/O de rede
# --------------------------------------------------------------------------- #

_REDE = re.compile(r"\b(requests|urllib|httpx|socket|aiohttp)\b")


def test_sem_rede():
    """RNF-19: exceção única é `embeddings.py` (download do modelo, Fase 4)."""
    outros = [m for m in MODULOS if m.name != "embeddings.py"]
    problemas = ocorrencias(outros, _REDE)
    assert problemas == [], "I/O de rede encontrado:\n" + "\n".join(problemas)


# --------------------------------------------------------------------------- #
# RNF-10 — requirements mínimo
# --------------------------------------------------------------------------- #

PESADOS = ("torch", "sentence-transformers", "transformers", "model2vec", "numpy")


def test_requirements_minimo():
    """RNF-10: as dependências pesadas vivem só em `requirements-semantic.txt`."""
    nucleo = (RAIZ_PROJETO / "requirements.txt").read_text(encoding="utf-8").lower()
    linhas = [l.strip() for l in nucleo.splitlines() if l.strip() and not l.startswith("#")]
    for pacote in PESADOS:
        assert not any(l.startswith(pacote) for l in linhas), f"{pacote} não pode estar no núcleo"

    semantico = (RAIZ_PROJETO / "requirements-semantic.txt").read_text(encoding="utf-8").lower()
    assert "model2vec" in semantico and "numpy" in semantico

    for fixado in ("watchdog==6.0.0", "psutil==7.2.2", "nvidia-ml-py==13.610.43", "pdfplumber==0.11.10",
                   "python-docx==1.2.0", "rich==15.0.0", "plyer==2.1.0"):
        assert fixado in nucleo, f"RNF-18: falta {fixado}"


# --------------------------------------------------------------------------- #
# RNF-01 — o interlock funciona
# --------------------------------------------------------------------------- #


def test_interlock_dispara_fora_do_sandbox():
    """RNF-01: qualquer operação destrutiva fora do sandbox explode antes de agir.

    O alvo é um caminho no perfil do usuário que não existe e continua não
    existindo: as funções levantam antes de chegar ao sistema de arquivos.
    """
    alvo = Path.home() / "___foa_interlock_probe___.txt"
    assert not alvo.exists()

    with pytest.raises(RuntimeError):
        os.unlink(str(alvo))
    with pytest.raises(RuntimeError):
        os.remove(str(alvo))
    with pytest.raises(RuntimeError):
        os.rename(str(alvo), str(alvo) + ".bak")
    with pytest.raises(RuntimeError):
        os.replace(str(alvo), str(alvo) + ".bak")
    with pytest.raises(RuntimeError):
        shutil.move(str(alvo), str(alvo) + ".bak")
    with pytest.raises(RuntimeError):
        shutil.copy2(str(alvo), str(alvo) + ".bak")
    with pytest.raises(RuntimeError):
        alvo.unlink()

    assert not alvo.exists()


def test_interlock_permite_dentro_do_sandbox(tmp_path):
    origem = tmp_path / "a.txt"
    origem.write_text("x", encoding="utf-8")
    destino = tmp_path / "b.txt"
    os.replace(str(origem), str(destino))
    assert destino.exists()
    destino.unlink()
    assert not destino.exists()


# --------------------------------------------------------------------------- #
# RNF-02 — as fixtures não leem o disco do usuário
# --------------------------------------------------------------------------- #

_LEITURA_DE_DISCO = re.compile(
    r"read_bytes\(\)|read_text\(|open\(|iterdir\(|glob\(|rglob\(|listdir|shutil|os\.walk"
)


def test_factories_nao_leem_disco_do_usuario():
    """RNF-02: `tests/factories.py` só gera bytes; nunca lê nada do disco."""
    arquivo = Path(__file__).parent / "factories.py"
    problemas = ocorrencias([arquivo], _LEITURA_DE_DISCO)
    assert problemas == [], "factories.py leu disco:\n" + "\n".join(problemas)
    assert ocorrencias([arquivo], _PASTA_DE_USUARIO) == []
    assert ocorrencias([arquivo], _RAIZ_DE_UNIDADE) == []


# --------------------------------------------------------------------------- #
# RNF-15 — motivos vêm de um Enum único
# --------------------------------------------------------------------------- #

#: `duplicado`, `aguardando_aprovacao` e `ok` também são valores das colunas
#: `arquivos.status` / `operacoes.estado` do schema, então aparecem legitimamente
#: como literal em `db.py` e `move.py`. Ficam fora desta checagem.
MOTIVOS_AMBIGUOS = {"ok", "duplicado", "aguardando_aprovacao"}


def test_motivos_nao_aparecem_como_literal():
    """RNF-15: nenhum valor de motivo é escrito à mão fora de `queue.py`."""
    from organizer.queue import Motivo

    valores = [m.value for m in Motivo if m.value not in MOTIVOS_AMBIGUOS]
    padrao = re.compile("|".join(f'"{re.escape(v)}"' for v in valores))
    outros = [m for m in MODULOS if m.name != "queue.py"]
    problemas = ocorrencias(outros, padrao)
    assert problemas == [], "motivo literal fora do Enum:\n" + "\n".join(problemas)


# --------------------------------------------------------------------------- #
# RNF-04 / sanidade geral
# --------------------------------------------------------------------------- #


def test_pytest_ini_desliga_o_marcador_real():
    """RNF-04: `real_readonly` fica desligado por padrão."""
    conteudo = (RAIZ_PROJETO / "pytest.ini").read_text(encoding="utf-8")
    assert 'addopts' in conteudo
    assert '-m "not real_readonly"' in conteudo
    assert "real_readonly:" in conteudo


@pytest.mark.parametrize("modulo", [m.stem for m in MODULOS if m.stem != "__init__"])
def test_todos_os_modulos_importam(modulo):
    """Nenhum `ImportError` — inclusive nos stubs das Fases 3, 4 e 5."""
    importlib.import_module(f"organizer.{modulo}")


@pytest.mark.parametrize("entrypoint", [e.stem for e in ENTRYPOINTS])
def test_entrypoints_importam(entrypoint, sandbox):
    importlib.import_module(entrypoint)


def test_nenhum_modulo_cria_pasta_no_import(tmp_path):
    """RF-08: importar o pacote inteiro não cria nada em disco."""
    import subprocess
    import sys

    script = (
        "import pathlib, sys; "
        f"antes = set(pathlib.Path(r'{tmp_path}').rglob('*')); "
        "import organizer.watch, organizer.ingest, organizer.move, organizer.classify; "
        f"depois = set(pathlib.Path(r'{tmp_path}').rglob('*')); "
        "sys.exit(0 if antes == depois else 1)"
    )
    resultado = subprocess.run(
        [sys.executable, "-c", script], cwd=str(RAIZ_PROJETO), capture_output=True, text=True
    )
    assert resultado.returncode == 0, resultado.stderr


# --------------------------------------------------------------------------- #
# Fumaça opcional sobre o corpus real — DESLIGADA por padrão (RNF-04)
# --------------------------------------------------------------------------- #


@pytest.mark.real_readonly
def test_heuristica_contra_corpus_real():
    """Lê SOMENTE os nomes dos arquivos do Downloads real. Nada é aberto ou movido.

    Rodar com: `pytest -m real_readonly` e `FOA_REAL_DOWNLOADS=<caminho>`.
    """
    from organizer import classify, naming

    bruto = os.environ.get("FOA_REAL_DOWNLOADS")
    if not bruto:
        pytest.skip("defina FOA_REAL_DOWNLOADS para rodar a fumaça sobre o corpus real")
    pasta = Path(bruto)
    nomes = [entrada.name for entrada in pasta.iterdir() if entrada.is_file()]
    for nome in nomes:
        generico, motivo = naming.is_generic(nome)
        assert isinstance(generico, bool) and isinstance(motivo, str)
        categoria, confianca, *_ = classify.classificar_por_extensao(nome)
        assert 0.0 <= confianca <= 0.95
