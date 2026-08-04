"""RF-63 a RF-72, RNF-10, RNF-11 — busca léxica, semântica e fusão."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from organizer import config, db, embeddings, rules, search

RAIZ_PROJETO = Path(__file__).resolve().parent.parent

#: Índice de exemplo — os três casos da spec do vault.
CORPUS = [
    (
        "matriz-curricular-ec-2026.pdf",
        rules.CAT_MATRIZES,
        "grade curricular do curso de engenharia da computacao da unifesp",
    ),
    (
        "contrato-estagio-2025-03.pdf",
        rules.CAT_CONTRATOS,
        "termo de compromisso de estagio celebrado com a eyeconnect",
    ),
    (
        "DiscordSetup-2024-11.exe",
        rules.CAT_INSTALADORES,
        "instalador do discord para windows",
    ),
    ("horario-das-aulas.pdf", rules.CAT_HORARIOS, "horário das aulas do primeiro semestre"),
    ("nota-fiscal-2026-05.pdf", rules.CAT_NOTAS_FISCAIS, "nota fiscal de servicos"),
]


@pytest.fixture
def indice(conn, sandbox):
    """Popula o índice do sandbox com o corpus acima."""
    for nome, categoria, amostra in CORPUS:
        tipo, subtipo = rules.tipo_subtipo_da_categoria(categoria)
        db.inserir_arquivo(
            conn,
            nome_orig=nome,
            nome_atual=nome,
            path=str(sandbox.caminho(categoria) / nome),
            tipo=tipo,
            subtipo=subtipo,
            texto_amostra=amostra,
            status="organizado",
            via="extensao",
            confianca=0.9,
        )
    return conn


# --------------------------------------------------------------------------- #
# T1 — léxica
# --------------------------------------------------------------------------- #


def test_busca_sem_backend_de_embeddings(indice, sandbox):
    """RF-63: a busca responde numa instalação que só tem `requirements.txt`."""
    assert embeddings.get_backend(sandbox.cfg) is None, "o sandbox usa EMBEDDING_BACKEND=none"

    resultados = search.buscar(indice, "matriz curricular da minha faculdade", k=5)

    assert resultados
    assert resultados[0].path.name == "matriz-curricular-ec-2026.pdf"
    assert resultados[0].via == search.VIA_FTS


def test_fts_sincronizada(indice, sandbox):
    """RF-64: insert, update e delete em `arquivos` refletem em `arquivos_fts`."""
    assert db.contar_fts(indice) == len(CORPUS)

    caminho = str(sandbox.caminho(rules.CAT_MATRIZES) / "matriz-curricular-ec-2026.pdf")
    db.inserir_arquivo(
        indice, nome_orig="x.pdf", nome_atual="renomeado-zebra.pdf", path=caminho, tipo="documento"
    )
    assert search.buscar_lexico(indice, "zebra")
    db.remover_arquivo(indice, caminho)
    assert db.contar_fts(indice) == len(CORPUS) - 1
    assert not search.buscar_lexico(indice, "zebra")


def test_busca_ignora_acento(indice):
    """RF-64: `unicode61 remove_diacritics 2` — `horario` encontra `horário`."""
    resultados = search.buscar_lexico(indice, "horario")
    assert resultados and resultados[0].path.name == "horario-das-aulas.pdf"
    assert search.buscar_lexico(indice, "horário")


def test_ranking_bm25(indice):
    """RF-65: ordena por BM25 e devolve no máximo 20 candidatos."""
    resultados = search.buscar_lexico(indice, "estagio eyeconnect contrato")
    assert resultados[0].path.name == "contrato-estagio-2025-03.pdf"
    assert [r.score for r in resultados] == sorted((r.score for r in resultados), reverse=True)
    assert len(search.buscar_lexico(indice, "de")) <= search.TOP_CANDIDATOS
    assert search.TOP_CANDIDATOS == 20


def test_consulta_vazia_nao_quebra(indice):
    assert search.buscar_lexico(indice, "") == []
    assert search.buscar(indice, "   ") == []
    assert search.preparar_consulta("matriz curricular!") == '"matriz" OR "curricular"'


def test_pergunta_com_sintaxe_do_fts_nao_quebra(indice):
    """Aspas e operadores digitados pelo usuário não podem virar sintaxe."""
    for pergunta in ['matriz "curricular', "NEAR/2 alguma", "a OR OR b", "*", "^inicio"]:
        assert isinstance(search.buscar(indice, pergunta), list)


# --------------------------------------------------------------------------- #
# T2 — embeddings (opcional)
# --------------------------------------------------------------------------- #


def test_backend_ausente_retorna_none(sandbox):
    """RF-66: sem dependência opcional, `get_backend()` devolve `None`, sem ImportError."""
    cfg = dataclasses.replace(sandbox.cfg, embedding_backend="none")
    assert embeddings.get_backend(cfg) is None

    inexistente = dataclasses.replace(
        sandbox.cfg, embedding_backend="model2vec", embedding_model="modelo/que-nao-existe-12345"
    )
    assert embeddings.get_backend(inexistente) is None


def test_import_lazy():
    """RF-67: `import organizer.embeddings` não traz numpy para `sys.modules`."""
    script = (
        "import sys, json; import organizer.embeddings; "
        "print(json.dumps([m for m in ('numpy','model2vec','sentence_transformers','torch') "
        "if m in sys.modules]))"
    )
    resultado = subprocess.run(
        [sys.executable, "-c", script], cwd=str(RAIZ_PROJETO), capture_output=True, text=True
    )
    assert resultado.returncode == 0, resultado.stderr
    assert json.loads(resultado.stdout.strip().splitlines()[-1]) == []


def test_serializacao_float32_little_endian():
    """RF-69: o vetor é gravado como float32 little-endian."""
    blob = embeddings.para_blob([1.0, -2.5, 0.0])
    assert len(blob) == 12
    assert blob[:4] == b"\x00\x00\x80\x3f"  # 1.0 em float32 LE
    assert embeddings.de_blob(blob) == [1.0, -2.5, 0.0]
    assert embeddings.de_blob(None) == []
    assert embeddings.de_blob(b"") == []


class _BackendFalso:
    """Dublê determinístico: não precisa de model2vec instalado."""

    nome = "falso:v1"
    dim = 3

    def __init__(self, mapa=None):
        self.mapa = mapa or {}

    def encode(self, textos):
        import numpy as np

        return np.array(
            [self.mapa.get(t.strip(), [0.0, 0.0, 1.0]) for t in textos], dtype="float32"
        )


def _numpy_disponivel() -> bool:
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


requer_numpy = pytest.mark.skipif(
    not _numpy_disponivel(), reason="numpy só existe com o extra semântico"
)


@requer_numpy
def test_nao_mistura_modelos(indice, sandbox):
    """RF-69: vetores de modelos diferentes nunca são comparados entre si."""
    caminho = str(sandbox.caminho(rules.CAT_MATRIZES) / "matriz-curricular-ec-2026.pdf")
    db.inserir_arquivo(
        indice,
        nome_orig="m.pdf",
        nome_atual="m.pdf",
        path=caminho,
        embedding=embeddings.para_blob([1.0, 0.0, 0.0]),
        embedding_model="outro-modelo:v9",
        embedding_dim=3,
    )
    backend = _BackendFalso()
    assert db.contar_com_embedding(indice) == 1
    assert db.linhas_com_embedding(indice, backend.nome) == []
    assert search.buscar_semantico(indice, "qualquer coisa", backend) == []
    assert len(db.linhas_com_embedding(indice, "outro-modelo:v9")) == 1


@requer_numpy
def test_busca_semantica_encontra_por_similaridade(indice, sandbox):
    """T2: o cosseno roda sobre todas as linhas do mesmo modelo."""
    alvo = str(sandbox.caminho(rules.CAT_INSTALADORES) / "DiscordSetup-2024-11.exe")
    outro = str(sandbox.caminho(rules.CAT_HORARIOS) / "horario-das-aulas.pdf")
    backend = _BackendFalso({"programa de bate papo": [1.0, 0.0, 0.0]})
    for caminho, vetor in ((alvo, [1.0, 0.0, 0.0]), (outro, [0.0, 1.0, 0.0])):
        linha = db.buscar_por_path(indice, caminho)
        db.inserir_arquivo(
            indice,
            nome_orig=linha["nome_orig"],
            nome_atual=linha["nome_atual"],
            path=caminho,
            embedding=embeddings.para_blob(vetor),
            embedding_model=backend.nome,
            embedding_dim=3,
        )

    resultados = search.buscar_semantico(indice, "programa de bate papo", backend)

    assert resultados[0].path.name == "DiscordSetup-2024-11.exe"
    assert resultados[0].via == search.VIA_COSSENO
    assert resultados[0].score == pytest.approx(1.0, abs=1e-5)


def test_busca_semantica_sem_backend(indice):
    assert search.buscar_semantico(indice, "qualquer", None) == []


# --------------------------------------------------------------------------- #
# Fusão
# --------------------------------------------------------------------------- #


def test_rrf():
    """RF-70: `score = soma de 1/(60 + rank)`."""
    fundido = search.rrf([["a", "b"], ["b", "a"]])
    assert fundido["a"] == pytest.approx(1 / 61 + 1 / 62)
    assert fundido["b"] == pytest.approx(1 / 61 + 1 / 62)
    assert search.rrf([["a"], ["a"]])["a"] == pytest.approx(2 / 61)
    assert search.K_RRF == 60

    desempate = search.rrf([["x", "y"], ["x", "z"]])
    assert desempate["x"] > desempate["y"] > 0


def test_fusao_promove_quem_aparece_nos_dois_rankings():
    def r(nome, score, via):
        return search.Resultado(Path(nome), score, "documento", "Outros", None, via)

    lexicos = [r("a.pdf", 3, search.VIA_FTS), r("b.pdf", 2, search.VIA_FTS)]
    semanticos = [r("b.pdf", 0.9, search.VIA_COSSENO), r("c.pdf", 0.8, search.VIA_COSSENO)]

    fundidos = search.fundir(lexicos, semanticos)

    assert [f.path.name for f in fundidos][0] == "b.pdf"
    assert all(f.via == search.VIA_RRF for f in fundidos)
    assert search.fundir(lexicos, []) == lexicos
    assert search.fundir([], semanticos) == semanticos


# --------------------------------------------------------------------------- #
# T3 — rerank opcional
# --------------------------------------------------------------------------- #


def test_rerank_opcional(indice, sandbox):
    """RF-72: desligado por padrão e pulado em silêncio sem Ollama."""
    resultados = search.buscar_lexico(indice, "matriz curricular")
    assert sandbox.cfg.search_rerank_llm is False
    assert search.rerankear(resultados, "x", sandbox.cfg) == resultados

    ligado = dataclasses.replace(sandbox.cfg, search_rerank_llm=True)
    # `OLLAMA_BIN` do sandbox não existe: o rerank some sem levantar nada
    assert search.rerankear(resultados, "x", ligado) == resultados


def test_rerank_promove_o_escolhido(indice, sandbox, ollama_falso, monkeypatch):
    from organizer import llm

    config.get_config.cache_clear()
    cfg = dataclasses.replace(config.get_config(), search_rerank_llm=True)
    resultados = search.buscar_lexico(indice, "matriz curricular estagio")
    assert len(resultados) >= 2
    monkeypatch.setattr(llm, "executar", lambda *a, **k: '{"melhor": 2}')

    rerankeados = search.rerankear(resultados, "qual o contrato?", cfg)

    assert rerankeados[0] is resultados[1]
    assert len(rerankeados) == len(resultados)


def test_rerank_ignora_resposta_absurda(indice, sandbox, ollama_falso, monkeypatch):
    from organizer import llm

    config.get_config.cache_clear()
    cfg = dataclasses.replace(config.get_config(), search_rerank_llm=True)
    resultados = search.buscar_lexico(indice, "matriz curricular estagio")
    for resposta in ('{"melhor": 999}', '{"melhor": "abc"}', "lixo", '{"outra": 1}'):
        monkeypatch.setattr(llm, "executar", lambda *a, **k: resposta)
        assert search.rerankear(resultados, "x", cfg) == resultados


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _rodar_query(sandbox, *args):
    import query

    return query.main(list(args))


def test_cli_exit_codes(indice, sandbox, capsys):
    """RF-71: 0 quando encontra, 1 quando não encontra."""
    assert _rodar_query(sandbox, "matriz", "curricular") == 0
    # a tabela do `rich` quebra o caminho em várias linhas; o que importa aqui é
    # o código de saída — o conteúdo exato é conferido em `test_saida_json`
    assert "Resultados para" in capsys.readouterr().out

    assert _rodar_query(sandbox, "xyzabc123inexistente") == 1
    assert "nada encontrado" in capsys.readouterr().out


def test_saida_json(indice, sandbox, capsys):
    """RF-71: `--json` para uso programático, com o path absoluto."""
    assert _rodar_query(sandbox, "matriz curricular", "--json") == 0
    dados = json.loads(capsys.readouterr().out)
    assert dados and Path(dados[0]["path"]).is_absolute()
    assert {"path", "score", "tipo", "subtipo", "indexado_em", "via"} <= set(dados[0])


def test_dica_de_instalacao(indice, sandbox, capsys):
    """RF-68: sem backend, a busca funciona e a dica aparece uma vez."""
    assert _rodar_query(sandbox, "matriz curricular") == 0
    saida = capsys.readouterr().out
    assert "requirements-semantic.txt" in saida
    assert saida.count("requirements-semantic.txt") == 1


def test_indexacao_sem_backend_grava_embedding_nulo(sandbox, conn):
    """RF-68: sem o extra, `embedding` fica NULL e nada quebra."""
    import factories
    from organizer import ingest

    origem = factories.criar(sandbox.downloads, "setup.exe")
    ingest.processar(origem, cfg=sandbox.cfg, conn=conn)
    linha = db.listar_arquivos(conn)[0]
    assert linha["embedding"] is None
    assert linha["embedding_model"] is None
    assert db.contar_com_embedding(conn) == 0


@pytest.mark.lento
def test_desempenho(sandbox, conn):
    """RNF-11: menos de 2 s num índice de 1 000 arquivos, sem embeddings."""
    for i in range(1000):
        db.inserir_arquivo(
            conn,
            nome_orig=f"documento-{i}.pdf",
            nome_atual=f"documento-{i}.pdf",
            path=str(sandbox.caminho(rules.CAT_OUTROS) / f"documento-{i}.pdf"),
            tipo="documento",
            subtipo="Outros",
            texto_amostra=f"conteudo do relatorio numero {i} sobre matriz curricular",
        )
    inicio = time.monotonic()
    resultados = search.buscar(conn, "matriz curricular da faculdade", k=5)
    decorrido = time.monotonic() - inicio
    assert resultados
    assert decorrido < 2.0, f"levou {decorrido:.2f}s"
