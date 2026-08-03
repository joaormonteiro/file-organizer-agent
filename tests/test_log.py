"""RNF-14, RNF-16 — linha única de decisão e rotação do arquivo de log."""

from __future__ import annotations

import io
import logging
import re

from organizer import log

_VALOR = r'(?:"[^"]*"|\S*)'
REGEX_DECISAO = re.compile(
    rf"^decision={_VALOR} path={_VALOR} dest={_VALOR} "
    rf"conf={_VALOR} via={_VALOR} motivo={_VALOR}$"
)


def test_linha_decisao_tem_todos_os_campos():
    linha = log.linha_decisao("movido", "X:/dl/a.pdf", "X:/alvo/a.pdf", 0.95, "extensao", "ok")
    assert "decision=movido" in linha
    assert "conf=0.95" in linha
    assert REGEX_DECISAO.match(linha), linha


def test_linha_decisao_com_espacos_usa_aspas():
    linha = log.linha_decisao("movido", "X:/com espaco/a.pdf", None, None, None, None)
    assert 'path="X:/com espaco/a.pdf"' in linha
    assert REGEX_DECISAO.match(linha), linha


def test_rotacao(sandbox):
    """RNF-16: RotatingFileHandler com teto de 5 MB e 3 backups."""
    from logging.handlers import RotatingFileHandler

    raiz = log.configurar(sandbox.log_dir)
    handlers = [h for h in raiz.handlers if isinstance(h, RotatingFileHandler)]
    assert handlers, "faltou o RotatingFileHandler"
    handler = handlers[0]
    assert handler.maxBytes == log.MAX_BYTES == 5 * 1024 * 1024
    assert handler.backupCount == log.BACKUP_COUNT == 3


def test_arquivo_de_log_recebe_a_linha(sandbox):
    logger = log.get_logger("teste")
    log.logar_decisao(logger, "movido", "X:/dl/a.pdf", "X:/alvo/a.pdf", 0.9, "extensao", "ok")
    for handler in log.get_logger("teste").parent.handlers:
        handler.flush()
    conteudo = (sandbox.log_dir / log.NOME_ARQUIVO).read_text(encoding="utf-8")
    assert "decision=movido" in conteudo


def test_configurar_e_idempotente(sandbox):
    raiz = log.configurar(sandbox.log_dir)
    quantidade = len(raiz.handlers)
    log.configurar(sandbox.log_dir)
    assert len(raiz.handlers) == quantidade


def test_get_logger_fica_sob_a_hierarquia():
    assert log.get_logger("x").name == "organizer.x"
    assert log.get_logger("organizer.x").name == "organizer.x"


class _StderrCp1252(io.TextIOWrapper):
    """Dublê do console do Windows: só sabe escrever cp1252."""

    def __init__(self):
        self._bruto = io.BytesIO()
        super().__init__(self._bruto, encoding="cp1252", errors="strict", newline="")


def test_console_nao_perde_linha_com_caractere_fora_do_cp1252(sandbox, monkeypatch):
    """D6: nome com CJK/cirílico/emoji não pode sumir do console nem quebrar.

    Sem o reembrulho em UTF-8, o `StreamHandler` levanta `UnicodeEncodeError`,
    o módulo `logging` engole a exceção e a linha de decisão é **perdida**.
    """
    console = _StderrCp1252()
    monkeypatch.setattr(log.sys, "stderr", console)

    log.resetar()
    raiz = log.configurar(sandbox.log_dir, console=True)
    fluxos = [
        h
        for h in raiz.handlers
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
    ]
    assert fluxos, "faltou o handler de console"
    assert fluxos[0].stream.encoding.lower().replace("_", "-").startswith("utf-8")

    nome = "相談-договор-📄.pdf"
    log.logar_decisao(
        log.get_logger("teste"), "movido", nome, "X:/alvo/a.pdf", 0.9, "extensao", "ok"
    )
    for handler in raiz.handlers:
        handler.flush()

    escrito = console._bruto.getvalue().decode("utf-8", "replace")
    assert "decision=movido" in escrito, "a linha de decisão sumiu do console"
    assert nome in escrito
    assert nome in (sandbox.log_dir / log.NOME_ARQUIVO).read_text(encoding="utf-8")


def test_stderr_utf8_faz_fallback_sem_buffer(monkeypatch):
    """D6: stream sem `.buffer` (pytest, sessão de serviço) usa o `sys.stderr` original."""

    class SemBuffer:
        encoding = "cp1252"

        def write(self, texto):
            return len(texto)

        def flush(self):
            pass

    monkeypatch.setattr(log.sys, "stderr", SemBuffer())
    assert log._stderr_utf8() is log.sys.stderr
