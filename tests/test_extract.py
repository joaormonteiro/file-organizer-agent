"""RF-48, RF-49, RF-50 — extração de trecho para alimentar o LLM."""

from __future__ import annotations

import pytest

import factories
from organizer import extract

TEXTO_PDF = "matriz curricular engenharia de computacao unifesp 2026"


def test_le_pdf_sintetico(tmp_path):
    """O PDF gerado por `factories` é válido de verdade e o `pdfplumber` o lê."""
    alvo = factories.criar(tmp_path, "doc.pdf", factories.pdf_minimo(TEXTO_PDF))
    resultado = extract.trecho(alvo)
    assert resultado.ok is True
    assert resultado.motivo == extract.MOTIVO_OK
    assert TEXTO_PDF in resultado.texto


def test_limite_de_500_chars(tmp_path):
    """RF-48: no máximo 500 caracteres, sempre."""
    longo = "palavra " * 400
    alvo = factories.criar(tmp_path, "longo.pdf", factories.pdf_minimo(longo))
    assert len(extract.trecho(alvo).texto) <= extract.MAX_CHARS

    txt = factories.criar(tmp_path, "longo.txt", longo.encode("utf-8"))
    assert len(extract.trecho(txt).texto) <= extract.MAX_CHARS

    docx = factories.criar(tmp_path, "longo.docx", factories.docx_minimo(longo))
    assert len(extract.trecho(docx).texto) <= extract.MAX_CHARS

    assert len(extract.trecho(txt, max_chars=42).texto) <= 42


def test_limite_de_paginas(tmp_path):
    """RF-48: só as 2 primeiras páginas do PDF são lidas."""
    paginas = ["PRIMEIRA pagina", "SEGUNDA pagina", "TERCEIRA pagina", "QUARTA pagina"]
    alvo = factories.criar(tmp_path, "muitas.pdf", factories.pdf_multipagina(paginas))

    resultado = extract.trecho(alvo)

    assert resultado.ok is True
    assert "PRIMEIRA" in resultado.texto
    assert "SEGUNDA" in resultado.texto
    assert "TERCEIRA" not in resultado.texto
    assert "QUARTA" not in resultado.texto


def test_pdf_protegido(tmp_path):
    """RF-49: PDF com senha devolve `ok=False, motivo='protegido'` sem levantar."""
    alvo = factories.criar(tmp_path, "cofre.pdf", factories.pdf_protegido())

    resultado = extract.trecho(alvo)

    assert resultado.ok is False
    assert resultado.motivo == extract.MOTIVO_PROTEGIDO
    assert resultado.texto == ""
    assert extract.pdf_esta_protegido(alvo) is True


def test_pdf_normal_nao_e_confundido_com_protegido(tmp_path):
    alvo = factories.criar(tmp_path, "aberto.pdf", factories.pdf_minimo())
    assert extract.pdf_esta_protegido(alvo) is False


def test_pdf_corrompido_nao_levanta(tmp_path):
    alvo = factories.criar(tmp_path, "quebrado.pdf", b"%PDF-1.4\nlixo binario\n")
    resultado = extract.trecho(alvo)
    assert resultado.ok is False
    assert resultado.motivo in (extract.MOTIVO_FALHOU, extract.MOTIVO_VAZIO)


def test_pdf_sem_texto(tmp_path):
    """PDF válido mas só com página em branco → `sem_texto`, não exceção."""
    alvo = factories.criar(tmp_path, "branco.pdf", factories.pdf_multipagina([" "]))
    resultado = extract.trecho(alvo)
    assert resultado.ok is False
    assert resultado.motivo in (extract.MOTIVO_VAZIO, extract.MOTIVO_FALHOU)


def test_le_docx(tmp_path):
    texto = "contrato de estagio eyeconnect 2026"
    alvo = factories.criar(tmp_path, "doc.docx", factories.docx_minimo(texto))
    resultado = extract.trecho(alvo)
    assert resultado.ok is True
    assert texto in resultado.texto


def test_docx_invalido_nao_levanta(tmp_path):
    alvo = factories.criar(tmp_path, "falso.docx", b"isto nao e um docx")
    resultado = extract.trecho(alvo)
    assert resultado.ok is False
    assert resultado.motivo == extract.MOTIVO_FALHOU


@pytest.mark.parametrize(
    "codec,texto",
    [
        ("utf-8", "relatório de gestão — ação e coração"),
        ("utf-8-sig", "relatório com BOM"),
        ("cp1252", "acentuação em cp1252: ç ã é"),
        ("latin-1", "latin um: à ê õ"),
    ],
)
def test_encodings(tmp_path, codec, texto):
    """RF-50: cascata utf-8 → utf-8-sig → cp1252 → latin-1, sem UnicodeDecodeError."""
    alvo = factories.criar(tmp_path, f"texto-{codec}.txt", texto.encode(codec))
    resultado = extract.trecho(alvo)
    assert resultado.ok is True
    assert resultado.texto, f"{codec} não produziu texto"


def test_bytes_invalidos_em_qualquer_encoding(tmp_path):
    """RF-50: nem sequência de bytes arbitrária levanta exceção."""
    alvo = factories.criar(tmp_path, "binario.txt", bytes(range(256)))
    resultado = extract.trecho(alvo)
    assert resultado.ok is True


def test_csv_e_md(tmp_path):
    csv = factories.criar(tmp_path, "planilha.csv", b"col1;col2\nvalor;outro\n")
    md = factories.criar(tmp_path, "leiame.md", b"# titulo\n\ncorpo do texto\n")
    assert extract.trecho(csv).ok is True
    assert "titulo" in extract.trecho(md).texto


def test_extensao_nao_suportada(tmp_path):
    """`.zip`, `.exe` e afins não têm texto a extrair — e não são abertos."""
    alvo = factories.criar(tmp_path, "app.exe")
    resultado = extract.trecho(alvo)
    assert resultado.ok is False
    assert resultado.motivo == extract.MOTIVO_NAO_SUPORTADO
    assert extract.suportado(alvo) is False
    assert extract.suportado(tmp_path / "x.pdf") is True


def test_arquivo_inexistente(tmp_path):
    resultado = extract.trecho(tmp_path / "fantasma.pdf")
    assert resultado.ok is False
    assert resultado.motivo == extract.MOTIVO_FALHOU


def test_controles_sao_removidos(tmp_path):
    alvo = factories.criar(tmp_path, "sujo.txt", b"linha\x00um\x07  com   controles\x1f")
    texto = extract.trecho(alvo).texto
    assert "\x00" not in texto and "\x07" not in texto
    assert "  " not in texto


def test_extracao_e_falsy_quando_falha(tmp_path):
    assert not extract.trecho(tmp_path / "nada.pdf")
    assert extract.trecho(factories.criar(tmp_path, "a.txt", b"conteudo"))
