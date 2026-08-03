"""RF-18, RF-19 — heurística determinística de nome genérico."""

from __future__ import annotations

import pytest

import factories
from organizer import naming


@pytest.mark.parametrize("nome,regra", factories.NOMES_GENERICOS)
def test_nomes_genericos(nome, regra):
    """RF-18: todos os casos obrigatoriamente genéricos, com a regra esperada."""
    generico, motivo = naming.is_generic(nome)
    assert generico, f"{nome} deveria ser genérico"
    assert motivo == regra, f"{nome}: esperava {regra}, veio {motivo}"


@pytest.mark.parametrize("nome", factories.NOMES_NAO_GENERICOS)
def test_nomes_nao_genericos(nome):
    """RF-18: a regra negativa do sufixo `(n)` protege nomes descritivos reais."""
    generico, motivo = naming.is_generic(nome)
    assert not generico, f"{nome} NÃO deveria ser genérico (motivo={motivo})"


def test_sufixo_copia_sozinho_nao_torna_generico():
    """RF-18: `(1)` sozinho nunca é evidência de nome genérico."""
    com_sufixo = "holerite-contabil-horizonte (1).pdf"
    sem_sufixo = "holerite-contabil-horizonte.pdf"
    assert naming.is_generic(com_sufixo) == naming.is_generic(sem_sufixo)
    _, teve_sufixo = naming.normalizar(com_sufixo)
    assert teve_sufixo is True


def test_motivo_retornado():
    """RF-19: a assinatura é `-> tuple[bool, str]` e o motivo é G1..G5 ou vazio."""
    resultado = naming.is_generic("document.pdf")
    assert isinstance(resultado, tuple) and len(resultado) == 2
    assert isinstance(resultado[0], bool) and isinstance(resultado[1], str)
    assert resultado[1] in {"G1", "G2", "G3", "G4", "G5"}
    assert naming.is_generic("matriz-curricular-ec-2026.pdf")[1] == naming.MOTIVO_NAO_GENERICO


def test_uuid_e_hex_sao_genericos():
    assert naming.is_generic("550e8400-e29b-41d4-a716-446655440000.dat")[1] == "G3"
    assert naming.is_generic("deadbeefcafebabe0123.bin")[1] == "G3"


def test_normalizar():
    stem, teve = naming.normalizar("Relatório_Final+2026 (2).pdf")
    assert stem == "relatorio final 2026"
    assert teve is True


def test_slugify():
    assert naming.slugify("Matriz Curricular — 2026!") == "matriz-curricular-2026"


def test_stem_sem_sufixo_copia():
    assert naming.stem_sem_sufixo_copia("contrato (3).pdf") == "contrato"
    assert naming.stem_sem_sufixo_copia("contrato.pdf") == "contrato"


def test_nome_vazio_e_generico():
    assert naming.is_generic(".pdf")[0] is True


def test_stem_que_normaliza_para_vazio():
    """Nome só de separadores vira stem vazio — genérico por G4."""
    assert naming.is_generic("___.pdf") == (True, "G4")
    assert naming.normalizar("___.pdf") == ("", False)
