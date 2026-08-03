"""Geradores de artefatos sintéticos.

**Nada aqui lê o disco do usuário.** Todo byte é literal, gerado por código ou
produzido por `python-docx`. É isso que a RNF-02 exige: nenhuma fixture copia
arquivo do Downloads real.
"""

from __future__ import annotations

import io
import zlib
from pathlib import Path

# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #


def _objetos_pdf(texto: str, extra_catalogo: str = "") -> list[bytes]:
    fluxo = f"BT /F1 12 Tf 72 720 Td ({texto}) Tj ET".encode("latin-1", "replace")
    return [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(fluxo)).encode() + b" >>\nstream\n" + fluxo + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]


def pdf_minimo(texto: str = "matriz curricular engenharia de computacao unifesp 2026") -> bytes:
    """PDF de 1 página, válido, com texto conhecido. Offsets calculados em runtime."""
    saida = io.BytesIO()
    saida.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    for numero, corpo in enumerate(_objetos_pdf(texto), start=1):
        offsets.append(saida.tell())
        saida.write(f"{numero} 0 obj\n".encode())
        saida.write(corpo)
        saida.write(b"\nendobj\n")
    inicio_xref = saida.tell()
    total = len(offsets) + 1
    saida.write(f"xref\n0 {total}\n".encode())
    saida.write(b"0000000000 65535 f \n")
    for posicao in offsets:
        saida.write(f"{posicao:010d} 00000 n \n".encode())
    saida.write(f"trailer\n<< /Size {total} /Root 1 0 R >>\n".encode())
    saida.write(f"startxref\n{inicio_xref}\n%%EOF\n".encode())
    return saida.getvalue()


def pdf_multipagina(textos: list[str]) -> bytes:
    """PDF com uma página por texto — usado para provar o teto de 2 páginas (RF-48)."""
    saida = io.BytesIO()
    saida.write(b"%PDF-1.4\n")
    offsets: list[int] = []
    total_paginas = len(textos)
    numero_pagina_base = 3
    numero_conteudo_base = numero_pagina_base + total_paginas
    numero_fonte = numero_conteudo_base + total_paginas

    kids = " ".join(f"{numero_pagina_base + i} 0 R" for i in range(total_paginas))
    objetos: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        f"<< /Type /Pages /Kids [{kids}] /Count {total_paginas} >>".encode(),
    ]
    for i in range(total_paginas):
        objetos.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {numero_conteudo_base + i} 0 R "
            f"/Resources << /Font << /F1 {numero_fonte} 0 R >> >> >>".encode()
        )
    for texto in textos:
        fluxo = f"BT /F1 12 Tf 72 720 Td ({texto}) Tj ET".encode("latin-1", "replace")
        objetos.append(
            b"<< /Length " + str(len(fluxo)).encode() + b" >>\nstream\n" + fluxo + b"\nendstream"
        )
    objetos.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for numero, corpo in enumerate(objetos, start=1):
        offsets.append(saida.tell())
        saida.write(f"{numero} 0 obj\n".encode())
        saida.write(corpo)
        saida.write(b"\nendobj\n")
    inicio_xref = saida.tell()
    total = len(offsets) + 1
    saida.write(f"xref\n0 {total}\n".encode())
    saida.write(b"0000000000 65535 f \n")
    for posicao in offsets:
        saida.write(f"{posicao:010d} 00000 n \n".encode())
    saida.write(f"trailer\n<< /Size {total} /Root 1 0 R >>\n".encode())
    saida.write(f"startxref\n{inicio_xref}\n%%EOF\n".encode())
    return saida.getvalue()


def pdf_protegido() -> bytes:
    """PDF com dicionário `/Encrypt` no trailer — o caminho "protegido" da RF-49.

    Não precisa ser criptograficamente válido: o agente **nunca** decifra nada.
    O que importa é que o arquivo se declare cifrado exatamente como um PDF real
    com senha faria, para exercitar a detecção.
    """
    base = pdf_minimo("conteudo protegido")
    encrypt = (
        b"6 0 obj\n<< /Filter /Standard /V 1 /R 2 /Length 40 "
        b"/O <0102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f20> "
        b"/U <202122232425262728292a2b2c2d2e2f303132333435363738393a3b3c3d3e3f40> "
        b"/P -1 >>\nendobj\n"
    )
    marca = b"trailer\n"
    posicao = base.rindex(marca)
    corpo = base[:posicao] + encrypt + base[posicao:]
    return corpo.replace(b"/Root 1 0 R", b"/Root 1 0 R /Encrypt 6 0 R", 1)


# --------------------------------------------------------------------------- #
# DOCX
# --------------------------------------------------------------------------- #


def docx_minimo(texto: str = "contrato de estagio eyeconnect 2026") -> bytes:
    """Gerado em runtime com `python-docx`, que já é dependência do projeto."""
    from docx import Document

    documento = Document()
    documento.add_paragraph(texto)
    buffer = io.BytesIO()
    documento.save(buffer)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# Imagens
# --------------------------------------------------------------------------- #

#: PNG 1x1 transparente, bytes literais.
PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05"
    b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)

#: JPEG 1x1 cinza, bytes literais (cabeçalho mínimo JFIF).
JPG_1X1 = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    b"\xff\xdb\x00C\x00" + bytes([16] * 64) + b"\xff\xc0\x00\x0b\x08\x00\x01\x00\x01"
    b"\x01\x01\x11\x00\xff\xc4\x00\x14\x00\x01" + bytes(15) + b"\x08"
    b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00\xd2\xcf \xff\xd9"
)


# --------------------------------------------------------------------------- #
# Binários genéricos
# --------------------------------------------------------------------------- #


def bytes_deterministicos(tamanho: int = 1024, semente: int = 7) -> bytes:
    """Conteúdo reproduzível sem depender de `random` global."""
    dados = bytearray()
    valor = semente & 0xFFFFFFFF
    while len(dados) < tamanho:
        valor = (1103515245 * valor + 12345) & 0xFFFFFFFF
        dados.extend(valor.to_bytes(4, "little"))
    return bytes(dados[:tamanho])


def zip_minimo(nome_interno: str = "leiame.txt", conteudo: bytes = b"conteudo") -> bytes:
    """ZIP válido montado à mão (sem tocar em disco)."""
    crc = zlib.crc32(conteudo) & 0xFFFFFFFF
    nome = nome_interno.encode("ascii")
    local = (
        b"PK\x03\x04\x14\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        + crc.to_bytes(4, "little")
        + len(conteudo).to_bytes(4, "little")
        + len(conteudo).to_bytes(4, "little")
        + len(nome).to_bytes(2, "little")
        + b"\x00\x00"
        + nome
        + conteudo
    )
    central = (
        b"PK\x01\x02\x14\x00\x14\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        + crc.to_bytes(4, "little")
        + len(conteudo).to_bytes(4, "little")
        + len(conteudo).to_bytes(4, "little")
        + len(nome).to_bytes(2, "little")
        + b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        + nome
    )
    fim = (
        b"PK\x05\x06\x00\x00\x00\x00\x01\x00\x01\x00"
        + len(central).to_bytes(4, "little")
        + len(local).to_bytes(4, "little")
        + b"\x00\x00"
    )
    return local + central + fim


#: Conteúdo padrão por extensão, para os testes que só precisam de "algo válido".
def conteudo_para(nome: str) -> bytes:
    ext = Path(nome).suffix.lower()
    if ext == ".pdf":
        return pdf_minimo()
    if ext == ".docx":
        return docx_minimo()
    if ext == ".png":
        return PNG_1X1
    if ext in (".jpg", ".jpeg"):
        return JPG_1X1
    if ext == ".zip":
        return zip_minimo()
    if ext in (".txt", ".md", ".csv"):
        return b"linha um\nlinha dois\n"
    return bytes_deterministicos(512)


def criar(pasta: Path, nome: str, conteudo: bytes | None = None) -> Path:
    """Escreve um arquivo sintético dentro do sandbox e devolve o caminho."""
    pasta.mkdir(parents=True, exist_ok=True)
    destino = pasta / nome
    destino.write_bytes(conteudo if conteudo is not None else conteudo_para(nome))
    return destino


def criar_muitos(pasta: Path, nomes: list[str]) -> list[Path]:
    return [criar(pasta, nome) for nome in nomes]


# --------------------------------------------------------------------------- #
# Matriz de nomes derivada da ARQUITETURA §8
# --------------------------------------------------------------------------- #

#: Casos obrigatoriamente genéricos (RF-18).
NOMES_GENERICOS: tuple[tuple[str, str], ...] = (
    ("document.pdf", "G1"),
    ("documento (2).docx", "G1"),
    ("download (3).zip", "G1"),
    ("untitled.txt", "G1"),
    ("IMG_20260214.jpg", "G2"),
    ("screenshot 3.png", "G2"),
    ("doc1.pdf", "G2"),
    ("20260214093311.pdf", "G3"),
    ("a3f9c1de77b04e12.bin", "G3"),
    ("aa.pdf", "G4"),
    ("zz1.txt", "G4"),
    ("relatorio.pdf", "G5"),
)

#: Casos obrigatoriamente NÃO genéricos (RF-18) — inclui os padrões reais que
#: motivaram a regra negativa do sufixo de cópia.
NOMES_NAO_GENERICOS: tuple[str, ...] = (
    "holerite_contabil_horizonte_ana_clara_2026-07 (1).pdf",
    "log-events-viewer-result (3).csv",
    "matriz-curricular-ec-2026.pdf",
    "contrato-estagio-eyeconnect.pdf",
    "nota-fiscal-2026-05.pdf",
)

#: Caso real de caminho longo observado em produção (211 caracteres), reproduzido
#: aqui só como *nome sintético* — nenhum arquivo real foi lido.
NOME_LONGO = (
    "eyeagent-97gnaPFdKu4ZQwErTyUiOpAsDfGhJkLzXcVbNmQwErTyUiOpAsDfGhJkLz"
    "XcVbNmQwErTyUiOpAsDfGhJkLzXcVbNm_Macular Thickness OU Analysis_20260513135801.pdf"
)
