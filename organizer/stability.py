"""Detecção de "arquivo ainda sendo gravado" (ARQUITETURA §7).

Três camadas: blocklist estática (em `rules`), probe de handle exclusivo via
`CreateFileW` e estabilidade de tamanho/mtime. Só usa `ctypes` e `os` — nada
pesado, porque roda no início de todo worker.

Requisitos cobertos: RF-14, RF-15.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from organizer import paths
from organizer.queue import Motivo

# Resultados do probe de handle (estados internos, não são motivos de fila)
LIVRE = "livre"
EM_USO = "em_uso"
INCONCLUSIVO = "inconclusivo"

# Motivos devolvidos por `esta_pronto` — todos vindos do Enum único (RNF-15)
PRONTO = Motivo.PRONTO
HANDLE_ABERTO = Motivo.HANDLE_ABERTO
INSTAVEL = Motivo.INSTAVEL
SUMIU = Motivo.SUMIU

#: Piso do intervalo entre tentativas de `aguardar_estabilidade`, em segundos.
INTERVALO_MINIMO_ESPERA = 0.05

# Códigos do Win32 relevantes
ERROR_FILE_NOT_FOUND = 2
ERROR_PATH_NOT_FOUND = 3
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33

_GENERIC_READ = 0x80000000
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_NORMAL = 0x80
_INVALID_HANDLE_VALUE = -1


def probe_handle(caminho: Path | str) -> str:
    """Tenta abrir o arquivo em modo exclusivo (`dwShareMode=0`).

    Se o handle vier válido, ninguém mais tem o arquivo aberto. Um
    `open(p, 'rb')` comum NÃO serve para essa pergunta: o Chrome mantém o
    `.crdownload` aberto permitindo leitura, e a leitura teria êxito com o
    download em andamento.

    Fora do Windows (ou se `ctypes` falhar) devolve `INCONCLUSIVO`, deixando o
    veredito para a camada de estabilidade.
    """
    if os.name != "nt":
        return INCONCLUSIVO
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        handle = kernel32.CreateFileW(
            paths.long_path(caminho),
            _GENERIC_READ,
            0,  # dwShareMode = 0 -> exclusivo
            None,
            _OPEN_EXISTING,
            _FILE_ATTRIBUTE_NORMAL,
            None,
        )
        if handle and handle != ctypes.c_void_p(_INVALID_HANDLE_VALUE).value:
            kernel32.CloseHandle(handle)
            return LIVRE
        erro = ctypes.get_last_error()
        if erro in (ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION):
            return EM_USO
        if erro in (ERROR_FILE_NOT_FOUND, ERROR_PATH_NOT_FOUND):
            return SUMIU
        return INCONCLUSIVO
    except Exception:  # pragma: no cover - ambiente sem ctypes/Win32
        return INCONCLUSIVO


def _assinatura(caminho: Path) -> tuple[int, int] | None:
    try:
        st = caminho.stat()
    except OSError:
        return None
    return st.st_size, st.st_mtime_ns


def tamanho_estavel(caminho: Path | str, polls: int, intervalo: float) -> bool:
    """`polls` leituras consecutivas de `st_size` e `st_mtime_ns` idênticas."""
    alvo = Path(caminho)
    referencia = _assinatura(alvo)
    if referencia is None:
        return False
    for _ in range(max(0, polls - 1)):
        time.sleep(intervalo)
        atual = _assinatura(alvo)
        if atual is None or atual != referencia:
            return False
    return True


def esta_pronto(caminho: Path | str, polls: int = 3, intervalo: float = 1.0) -> tuple[bool, str]:
    """Veredito único: pronto ⇔ (handle livre ou inconclusivo) E tamanho estável."""
    alvo = Path(caminho)
    resultado = probe_handle(alvo)
    if resultado == SUMIU or not alvo.exists():
        return False, SUMIU
    if resultado == EM_USO:
        return False, HANDLE_ABERTO
    if not tamanho_estavel(alvo, polls, intervalo):
        return False, INSTAVEL
    return True, PRONTO


def aguardar_estabilidade(
    caminho: Path | str,
    polls: int = 3,
    intervalo: float = 1.0,
    timeout: float = 300.0,
    relogio=time.monotonic,
) -> tuple[bool, str]:
    """Insiste até `timeout`. Estourou, devolve `(False, 'instavel')` (RF-15).

    Nunca trava o worker: quem chama enfileira em `pendentes` e morre.
    """
    inicio = relogio()
    while True:
        pronto, motivo = esta_pronto(caminho, polls, intervalo)
        if pronto or motivo == SUMIU:
            return pronto, motivo
        if relogio() - inicio >= timeout:
            return False, INSTAVEL
        # piso de espera: mesmo com intervalo mal configurado, este laço nunca
        # vira busy-loop de CPU (a config já recusa <= 0, isto é a segunda linha)
        time.sleep(max(intervalo, INTERVALO_MINIMO_ESPERA))
