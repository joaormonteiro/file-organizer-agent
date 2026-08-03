"""RF-08, RF-12, RF-16 — tabela de extensões, blocklist e árvore sob demanda."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from organizer import rules

RAIZ_PROJETO = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize(
    "ext,categoria,familia",
    [
        (".exe", rules.CAT_INSTALADORES, rules.FAM_SOFTWARE),
        (".msi", rules.CAT_INSTALADORES, rules.FAM_SOFTWARE),
        (".bat", rules.CAT_PORTATEIS, rules.FAM_SOFTWARE),
        (".ps1", rules.CAT_PORTATEIS, rules.FAM_SOFTWARE),
        (".zip", rules.CAT_PORTATEIS, rules.FAM_COMPACTADO),
        (".rar", rules.CAT_PORTATEIS, rules.FAM_COMPACTADO),
        (".7z", rules.CAT_PORTATEIS, rules.FAM_COMPACTADO),
        (".jpg", rules.CAT_FOTOS, rules.FAM_IMAGEM),
        (".png", rules.CAT_FOTOS, rules.FAM_IMAGEM),
        (".heic", rules.CAT_FOTOS, rules.FAM_IMAGEM),
        (".mp4", rules.CAT_VIDEOS, rules.FAM_VIDEO),
        (".mkv", rules.CAT_VIDEOS, rules.FAM_VIDEO),
        (".mp3", rules.CAT_MUSICA, rules.FAM_AUDIO),
        (".flac", rules.CAT_MUSICA, rules.FAM_AUDIO),
        (".wav", rules.CAT_MUSICA, rules.FAM_AUDIO),
        (".pdf", rules.CAT_OUTROS, rules.FAM_DOCUMENTO),
        (".docx", rules.CAT_OUTROS, rules.FAM_DOCUMENTO),
        (".txt", rules.CAT_OUTROS, rules.FAM_DOCUMENTO),
        (".xlsx", rules.CAT_OUTROS, rules.FAM_PLANILHA),
        (".csv", rules.CAT_OUTROS, rules.FAM_PLANILHA),
        (".pptx", rules.CAT_OUTROS, rules.FAM_APRESENTACAO),
        (".v38", rules.CAT_OUTROS, rules.FAM_DESCONHECIDO),
        ("", rules.CAT_OUTROS, rules.FAM_DESCONHECIDO),
    ],
)
def test_mapa_de_extensoes(ext, categoria, familia):
    """RF-16: as oito famílias da spec estão cobertas."""
    regra = rules.por_extensao(ext)
    assert regra.categoria == categoria
    assert regra.familia == familia


def test_extensao_sem_ponto_e_maiuscula():
    assert rules.por_extensao("EXE").categoria == rules.CAT_INSTALADORES
    assert rules.por_extensao(".EXE").categoria == rules.CAT_INSTALADORES


@pytest.mark.parametrize(
    "ext,esperado",
    [
        (".exe", rules.CONF_UNICA),
        (".mp3", rules.CONF_UNICA),
        (".jpg", rules.CONF_DEFAULT_FAMILIA),
        (".pdf", rules.CONF_AMBIGUO),
        (".zip", rules.CONF_AMBIGUO),
        (".v38", rules.CONF_DESCONHECIDO),
    ],
)
def test_confianca_base_por_patamar(ext, esperado):
    """ARQUITETURA §10.1: quatro patamares de confiança base."""
    assert rules.por_extensao(ext).confianca == esperado


def test_categorias_sao_unicas_e_relativas():
    assert len(set(rules.CATEGORIAS)) == len(rules.CATEGORIAS)
    for categoria in rules.CATEGORIAS:
        assert not Path(categoria).is_absolute()
        assert ":" not in categoria


def test_categoria_valida_normaliza_separadores():
    assert rules.categoria_valida("Documentos\\Financeiro\\Extratos") == rules.CAT_EXTRATOS
    assert rules.categoria_valida("/Videos/") == rules.CAT_VIDEOS
    assert rules.categoria_valida("Documentos/Inventado") is None
    assert rules.categoria_valida(None) is None


def test_todas_as_categorias_das_regras_estao_no_enum():
    for regra in rules.EXTENSOES.values():
        assert regra.categoria in rules.CATEGORIAS
    for grupo in rules.KEYWORDS:
        assert grupo.categoria in rules.CATEGORIAS


@pytest.mark.parametrize("ext", sorted(rules.EXTENSOES_PARCIAIS))
def test_extensoes_parciais_reconhecidas(ext):
    """RF-12: toda a blocklist de arquivos em gravação."""
    assert rules.nome_e_parcial(f"instalador{ext}")


@pytest.mark.parametrize(
    "nome", ["~$relatorio.docx", ".oculto", "backup.txt~", ""]
)
def test_prefixos_e_sufixos_ignorados(nome):
    assert rules.nome_e_parcial(nome)


def test_nome_normal_nao_e_parcial():
    assert not rules.nome_e_parcial("contrato-estagio.pdf")


def test_arvore_criada_sob_demanda(tmp_path):
    """RF-08: a árvore vem de CATEGORIAS e só é criada quando pedimos."""
    alvo = tmp_path / "Organizado"
    esperadas = rules.pastas_da_arvore(alvo)
    assert not alvo.exists(), "listar as pastas não pode criar nada"

    criadas = rules.criar_arvore(alvo)
    assert set(criadas) == set(esperadas)
    for categoria in rules.CATEGORIAS:
        assert (alvo / Path(categoria)).is_dir()
    assert (alvo / "_Inbox" / "_Duplicados").is_dir()
    assert (alvo / "_Inbox" / "_Aguardando").is_dir()

    rules.criar_arvore(alvo)  # idempotente


def test_nenhuma_pasta_criada_no_import(tmp_path):
    """RF-08: importar `organizer.rules` não cria nenhuma pasta."""
    script = (
        "import sys, pathlib; "
        f"antes = set(pathlib.Path(r'{tmp_path}').iterdir()); "
        "import organizer.rules; "
        f"depois = set(pathlib.Path(r'{tmp_path}').iterdir()); "
        "sys.exit(0 if antes == depois else 1)"
    )
    resultado = subprocess.run(
        [sys.executable, "-c", script], cwd=str(RAIZ_PROJETO), capture_output=True, text=True
    )
    assert resultado.returncode == 0, resultado.stderr


def test_tipo_subtipo_da_categoria():
    assert rules.tipo_subtipo_da_categoria(rules.CAT_MUSICA) == (rules.FAM_AUDIO, "Musica")
    assert rules.tipo_subtipo_da_categoria(rules.CAT_SCREENSHOTS)[1] == "Screenshots"
    assert rules.tipo_subtipo_da_categoria("inventada")[1] == "Outros"


def test_keywords_da_categoria():
    assert "nota fiscal" in rules.keywords_da_categoria(rules.CAT_NOTAS_FISCAIS)
    assert rules.keywords_da_categoria(rules.CAT_FOTOS) == ()


def test_extensoes_texto_sao_subconjunto_de_documentos():
    for ext in rules.EXTENSOES_TEXTO:
        assert rules.por_extensao(ext).familia == rules.FAM_DOCUMENTO
