"""RF-14, RF-15 — arquivo ainda sendo gravado."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import factories
from organizer import db, ingest, stability
from organizer.queue import Motivo

RAIZ_PROJETO = Path(__file__).resolve().parent.parent

_SCRIPT_SEGURA_HANDLE = """
import sys, time
caminho, marcador = sys.argv[1], sys.argv[2]
with open(caminho, "rb") as fh:
    with open(marcador, "w") as m:
        m.write("ok")
    time.sleep(30)
"""


@pytest.mark.skipif(os.name != "nt", reason="probe de handle exclusivo é específico do Windows")
def test_arquivo_aberto_por_outro_handle_nao_esta_pronto(tmp_path):
    """RF-14: `CreateFileW` com `dwShareMode=0` detecta handle de OUTRO processo."""
    alvo = factories.criar(tmp_path, "baixando.pdf")
    marcador = tmp_path / "aberto.flag"
    script = tmp_path / "segura.py"
    script.write_text(_SCRIPT_SEGURA_HANDLE, encoding="utf-8")

    filho = subprocess.Popen([sys.executable, str(script), str(alvo), str(marcador)])
    try:
        limite = time.monotonic() + 15
        while not marcador.exists() and time.monotonic() < limite:
            time.sleep(0.02)
        assert marcador.exists(), "o processo auxiliar não conseguiu abrir o arquivo"

        assert stability.probe_handle(alvo) == stability.EM_USO
        pronto, motivo = stability.esta_pronto(alvo, polls=1, intervalo=0)
        assert pronto is False
        assert motivo == Motivo.HANDLE_ABERTO
    finally:
        filho.kill()
        filho.wait(timeout=10)

    # com o handle fechado, volta a ficar pronto (o Windows leva alguns ms para
    # liberar o handle de um processo morto — por isso o laço de espera)
    limite = time.monotonic() + 15
    while stability.probe_handle(alvo) != stability.LIVRE and time.monotonic() < limite:
        time.sleep(0.05)
    assert stability.probe_handle(alvo) == stability.LIVRE
    assert stability.esta_pronto(alvo, polls=1, intervalo=0)[0] is True


def test_arquivo_crescendo_nao_esta_pronto(tmp_path):
    """RF-14: `STABILITY_POLLS` leituras de `st_size`/`st_mtime_ns` precisam bater."""
    alvo = tmp_path / "crescendo.bin"
    alvo.write_bytes(b"inicio")
    parar = threading.Event()

    def escrever():
        while not parar.is_set():
            with open(alvo, "ab") as fh:
                fh.write(b"x" * 4096)
            time.sleep(0.01)

    thread = threading.Thread(target=escrever, daemon=True)
    thread.start()
    try:
        assert stability.tamanho_estavel(alvo, polls=3, intervalo=0.08) is False
    finally:
        parar.set()
        thread.join(timeout=5)

    assert stability.tamanho_estavel(alvo, polls=3, intervalo=0.01) is True


def test_arquivo_parado_esta_pronto(tmp_path):
    alvo = factories.criar(tmp_path, "pronto.pdf")
    pronto, motivo = stability.esta_pronto(alvo, polls=2, intervalo=0.01)
    assert pronto is True
    assert motivo == Motivo.PRONTO


def test_arquivo_inexistente(tmp_path):
    pronto, motivo = stability.esta_pronto(tmp_path / "nao-existe.pdf", polls=1, intervalo=0)
    assert pronto is False
    assert motivo == Motivo.SUMIU
    assert stability.tamanho_estavel(tmp_path / "nao-existe.pdf", 1, 0) is False


def test_aguardar_estabilidade_devolve_rapido_quando_pronto(tmp_path):
    alvo = factories.criar(tmp_path, "ok.pdf")
    pronto, motivo = stability.aguardar_estabilidade(alvo, polls=1, intervalo=0, timeout=5)
    assert (pronto, motivo) == (True, Motivo.PRONTO)


def test_aguardar_estabilidade_estoura_timeout(tmp_path, monkeypatch):
    """RF-15: estourou `STABILITY_TIMEOUT`, devolve `instavel` sem travar."""
    alvo = factories.criar(tmp_path, "travado.pdf")
    monkeypatch.setattr(stability, "probe_handle", lambda p: stability.EM_USO)
    pronto, motivo = stability.aguardar_estabilidade(alvo, polls=1, intervalo=0, timeout=0.05)
    assert pronto is False
    assert motivo == Motivo.INSTAVEL


def test_timeout_vai_para_pendentes(sandbox, conn, monkeypatch):
    """RF-15: o worker enfileira em `pendentes(motivo='instavel')` e sai com 2."""
    alvo = factories.criar(sandbox.downloads, "travado.pdf")
    monkeypatch.setattr(stability, "probe_handle", lambda p: stability.EM_USO)

    codigo = ingest.processar(alvo, cfg=sandbox.cfg, conn=conn)

    assert codigo == ingest.EXIT_ADIADO
    linha = db.pendente_por_path(conn, str(alvo))
    assert linha is not None
    assert linha["motivo"] == Motivo.INSTAVEL.value
    assert alvo.exists(), "o arquivo não pode ter sido movido"


def test_probe_de_arquivo_inexistente(tmp_path):
    resultado = stability.probe_handle(tmp_path / "fantasma.pdf")
    assert resultado in (stability.SUMIU, stability.INCONCLUSIVO)
