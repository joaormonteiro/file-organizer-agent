"""RF-20, RF-21 — sanitização de nome e limite de caminho do Windows."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

import factories
from organizer import paths, rules


@pytest.mark.parametrize("proibido", list('<>:"/\\|?*') + ["\x00", "\x07", "\x1f"])
def test_sanitize_stem_remove_proibidos(proibido):
    """RF-20: cada caractere proibido do Windows desaparece."""
    resultado = paths.sanitize_stem(f"nota{proibido}fiscal")
    assert proibido not in resultado
    assert resultado == "notafiscal"


@pytest.mark.parametrize("reservado", sorted(paths.RESERVADOS))
def test_sanitize_stem_nomes_reservados(reservado):
    """RF-20: nomes de dispositivo reservados ganham prefixo underscore."""
    resultado = paths.sanitize_stem(reservado)
    assert resultado == "_" + reservado.lower()


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("Matriz Curricular 2026", "matriz-curricular-2026"),
        ("relatório  final .", "relatorio-final"),
        ("   ", "arquivo"),
        ("...", "arquivo"),
        ("a" * 200, "a" * 80),
        ("ação--de--graças", "acao-de-gracas"),
        ("nome com   espaços", "nome-com-espacos"),
    ],
)
def test_sanitize_stem(entrada, esperado):
    """RF-20: fold ASCII, lowercase, espaços viram hífen, corte em 80, vazio vira `arquivo`."""
    assert paths.sanitize_stem(entrada) == esperado


def test_sanitize_stem_corta_em_80():
    assert len(paths.sanitize_stem("x" * 500)) == paths.MAX_STEM


def test_sanitize_ext():
    assert paths.sanitize_ext(".PDF") == ".pdf"
    assert paths.sanitize_ext("pdf") == ".pdf"
    assert paths.sanitize_ext("") == ""
    assert paths.sanitize_ext(':"') == ""


def test_max_path_curto_passa_intacto(tmp_path):
    destino = tmp_path / "curto.pdf"
    assert paths.ensure_max_path(destino) == destino


def test_max_path(tmp_path):
    """RF-21: o caso real de 211 caracteres, movido para a subpasta mais profunda."""
    mais_profunda = max(rules.CATEGORIAS, key=lambda c: len(c))
    pasta = tmp_path / "Organizado" / Path(mais_profunda)
    destino = pasta / factories.NOME_LONGO
    assert len(str(destino)) > paths.MAX_PATH, "o caso precisa realmente estourar o limite"

    ajustado = paths.ensure_max_path(destino)
    assert ajustado is not None
    assert len(str(ajustado)) <= paths.MAX_PATH
    assert ajustado.suffix == ".pdf"
    marca = hashlib.sha1(destino.stem.encode("utf-8", "replace")).hexdigest()[:8]
    assert ajustado.stem.endswith("-" + marca)
    # determinístico: rodar de novo dá exatamente o mesmo nome
    assert paths.ensure_max_path(destino) == ajustado


def test_max_path_impossivel_devolve_none():
    """RF-21: quando nem truncando cabe, devolve None e o chamador manda para o _Inbox."""
    pasta = Path("C:/" + "p" * (paths.MAX_PATH + 20))
    assert paths.ensure_max_path(pasta / "x.pdf") is None


def test_is_subpath(tmp_path):
    assert paths.is_subpath(tmp_path / "a" / "b", tmp_path)
    assert paths.is_subpath(tmp_path, tmp_path)
    assert not paths.is_subpath(tmp_path, tmp_path / "a")


def test_assert_disjoint(tmp_path):
    paths.assert_disjoint(tmp_path / "a", tmp_path / "b")
    with pytest.raises(paths.CaminhosSobrepostosError):
        paths.assert_disjoint(tmp_path / "a", tmp_path / "a")
    with pytest.raises(paths.CaminhosSobrepostosError):
        paths.assert_disjoint(tmp_path / "a", tmp_path / "a" / "b")


def test_long_path():
    resultado = paths.long_path("C:/pasta/arquivo.txt")
    assert resultado.startswith("\\\\?\\") or resultado == "C:/pasta/arquivo.txt"
    assert paths.long_path("\\\\?\\C:\\ja\\prefixado") == "\\\\?\\C:\\ja\\prefixado"


def test_sha256_e_mesmo_conteudo(tmp_path):
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    c = tmp_path / "c.bin"
    a.write_bytes(b"conteudo identico")
    b.write_bytes(b"conteudo identico")
    c.write_bytes(b"outro conteudo")
    assert paths.sha256_arquivo(a) == paths.sha256_arquivo(b)
    assert paths.mesmo_conteudo(a, b)
    assert not paths.mesmo_conteudo(a, c)
    assert not paths.mesmo_conteudo(a, tmp_path / "inexistente.bin")


def test_sha256_arquivo_gigante_usa_pontas(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "LIMITE_HASH_COMPLETO", 16)
    monkeypatch.setattr(paths, "_PONTA", 8)
    grande = tmp_path / "grande.bin"
    grande.write_bytes(factories.bytes_deterministicos(64))
    assert len(paths.sha256_arquivo(grande)) == 64


def test_candidato_colisao_usa_hifen(tmp_path):
    """RF-23: o sufixo é `-N`, nunca ` (N)`."""
    base = tmp_path / "contrato-estagio.pdf"
    assert paths.candidato_colisao(base, 1) == base
    segundo = paths.candidato_colisao(base, 2)
    assert segundo.name == "contrato-estagio-2.pdf"
    assert "(" not in segundo.name


def test_volume_e_mesmo_volume(tmp_path):
    assert paths.mesmo_volume(tmp_path / "a", tmp_path / "b")
    assert paths.volume(tmp_path) == paths.volume(tmp_path / "sub")


def test_relativo_seguro(tmp_path):
    assert str(paths.relativo_seguro(tmp_path / "a" / "b", tmp_path)) == str(Path("a/b"))
    assert paths.relativo_seguro(Path("Z:/fora"), tmp_path) is None


def test_fold_ascii():
    assert paths.fold_ascii("ação") == "acao"


def test_long_path_fora_do_windows(monkeypatch):
    monkeypatch.setattr(paths.os, "name", "posix")
    assert paths.long_path("/home/joao/a.txt") == "/home/joao/a.txt"


def test_long_path_unc():
    entrada = r"\\servidor\share\a.txt"
    assert paths.long_path(entrada) == r"\\?\UNC\servidor\share\a.txt"


def test_max_path_stem_truncado_fica_vazio(monkeypatch):
    """Se o corte deixa só separadores, o stem vira o fallback `arquivo`."""
    monkeypatch.setattr(paths, "MAX_PATH", 60)
    pasta = Path("C:/p" + "a" * 30)  # sobra pouquíssimo espaço para o stem
    destino = pasta / ("-" * 40 + "z" * 200 + ".pdf")
    ajustado = paths.ensure_max_path(destino)
    assert ajustado is not None
    assert len(str(ajustado)) <= 60
    assert ajustado.stem.startswith(paths.FALLBACK_STEM[:3])
