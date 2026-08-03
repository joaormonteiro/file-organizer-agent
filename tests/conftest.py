"""Sandbox e interlocks de segurança da suíte.

As quatro barreiras da ARQUITETURA §14, em profundidade:

1. nenhum caminho hardcoded nos módulos (verificado em `test_isolation.py`);
2. sandbox por fixture, inteiramente dentro do `tmp_path` do pytest;
3. **interlock ativo**: `os.replace`, `os.rename`, `os.remove`, `os.unlink`,
   `shutil.move`, `shutil.copy2` e `Path.unlink` levantam `RuntimeError` se
   receberem qualquer caminho fora do sandbox — um bug que apontasse para o
   Downloads real faria o teste explodir em vez de mover o arquivo do usuário;
4. `FOA_ENV=test` faz `config.validar()` recusar raízes fora do sandbox.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

RAIZ_PROJETO = Path(__file__).resolve().parent.parent
if str(RAIZ_PROJETO) not in sys.path:
    sys.path.insert(0, str(RAIZ_PROJETO))

from organizer import config, db, guard, log  # noqa: E402

# --------------------------------------------------------------------------- #
# Barreira 3 — interlock ativo
# --------------------------------------------------------------------------- #

#: Funções envolvidas pelo interlock e o índice dos argumentos que são caminhos.
_ALVOS = (
    (os, "replace", (0, 1)),
    (os, "rename", (0, 1)),
    (os, "remove", (0,)),
    (os, "unlink", (0,)),
    (shutil, "move", (0, 1)),
    (shutil, "copy2", (0, 1)),
)

class InterlockError(RuntimeError):
    """Uma operação destrutiva tentou sair do sandbox de teste."""


def _dentro(caminho, permitidos: list[Path]) -> bool:
    try:
        alvo = Path(os.path.abspath(os.fspath(caminho)))
    except (TypeError, ValueError):
        return False
    return any(alvo == raiz or raiz in alvo.parents for raiz in permitidos)


@pytest.fixture(scope="session", autouse=True)
def interlock(tmp_path_factory):
    """Instala o interlock por toda a sessão. `autouse`: ninguém escapa.

    A lista de permitidos é deliberadamente estreita: **só** o `basetemp` da
    sessão e o diretório `pytest-of-<usuário>` que o contém (é lá que o próprio
    pytest recicla os diretórios numerados das execuções anteriores). Nem o
    `%TEMP%` inteiro, nem exceções por nome de arquivo.
    """
    basetemp = Path(tmp_path_factory.getbasetemp()).resolve()
    permitidos = [basetemp, basetemp.parent]
    originais = []

    def envolver(modulo, nome, indices):
        original = getattr(modulo, nome)

        def protegida(*args, **kwargs):
            for i in indices:
                if i < len(args) and not _dentro(args[i], permitidos):
                    raise InterlockError(
                        f"{modulo.__name__}.{nome} tentou operar fora do sandbox: {args[i]!r}"
                    )
            return original(*args, **kwargs)

        originais.append((modulo, nome, original))
        setattr(modulo, nome, protegida)

    for modulo, nome, indices in _ALVOS:
        envolver(modulo, nome, indices)

    unlink_original = Path.unlink

    def unlink_protegido(self, *args, **kwargs):
        if not _dentro(self, permitidos):
            raise InterlockError(f"Path.unlink tentou operar fora do sandbox: {self!r}")
        return unlink_original(self, *args, **kwargs)

    Path.unlink = unlink_protegido

    yield permitidos

    for modulo, nome, original in originais:
        setattr(modulo, nome, original)
    Path.unlink = unlink_original


# --------------------------------------------------------------------------- #
# Barreira 2 — sandbox
# --------------------------------------------------------------------------- #


@dataclass
class Sandbox:
    """Uma árvore completa e descartável, isolada em `tmp_path`."""

    raiz: Path
    downloads: Path
    target: Path
    db_path: Path
    log_dir: Path
    cfg: config.Config

    @property
    def inbox(self) -> Path:
        return self.cfg.inbox_dir

    @property
    def duplicados(self) -> Path:
        return self.cfg.duplicados_dir

    def caminho(self, categoria: str) -> Path:
        return self.target / Path(categoria)


#: Valores padrão do sandbox. Thresholds em 100 para que o guard nunca considere
#: a máquina ocupada por acidente (os testes do guard definem os seus próprios).
_ENV_PADRAO = {
    "MODE": "auto",
    "DRY_RUN": "0",
    "CONFIDENCE_MIN": "0.75",
    "WATCH_RECURSIVE": "0",
    "MAX_WORKERS": "2",
    "ALLOW_CROSS_VOLUME": "0",
    "THRESHOLD_CPU": "100",
    "THRESHOLD_RAM": "100",
    "THRESHOLD_GPU": "100",
    "THRESHOLD_VRAM": "100",
    "GPU_INDEX": "0",
    "RETRY_BUSY_MINUTES": "120",
    "RETRY_STARTUP_MINUTES": "30",
    "IDLE_LOOP_SECONDS": "600",
    "MAX_TENTATIVAS": "8",
    "STABILITY_POLLS": "1",
    "STABILITY_INTERVAL": "0.01",
    "STABILITY_TIMEOUT": "2",
    "LLM_ENABLED": "1",
    "OLLAMA_BIN": "ollama-inexistente-para-testes",
    "OLLAMA_MODEL": "phi3:mini",
    "LLM_TIMEOUT": "5",
    "LLM_SAMPLES": "1",
    "EMBEDDING_BACKEND": "none",
    "EMBEDDING_MODEL": "minishlab/potion-multilingual-128M",
    "SEARCH_RERANK_LLM": "0",
}


@pytest.fixture
def sandbox(tmp_path, tmp_path_factory, monkeypatch) -> Sandbox:
    """Monta o sandbox e aponta toda a configuração para dentro dele."""
    raiz = tmp_path / "sandbox"
    downloads = raiz / "Downloads"
    target = raiz / "Organizado"
    db_path = target / ".foa" / "index.db"
    log_dir = target / ".foa" / "logs"
    downloads.mkdir(parents=True)
    target.mkdir(parents=True)

    monkeypatch.setenv(config.VAR_AMBIENTE, "test")
    monkeypatch.setenv(config.VAR_RAIZ_TESTE, str(Path(tmp_path_factory.getbasetemp()).resolve()))
    monkeypatch.setenv("DOWNLOADS_DIR", str(downloads))
    monkeypatch.setenv("TARGET_ROOT", str(target))
    monkeypatch.setenv("INBOX_DIRNAME", "_Inbox")
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("LOG_DIR", str(log_dir))
    for chave, valor in _ENV_PADRAO.items():
        monkeypatch.setenv(chave, valor)

    config.get_config.cache_clear()
    log.resetar()
    cfg = config.get_config()
    log.configurar(cfg.log_dir)

    yield Sandbox(raiz, downloads, target, db_path, log_dir, cfg)

    log.resetar()
    config.get_config.cache_clear()


@pytest.fixture
def conn(sandbox):
    """Conexão já migrada com o banco do sandbox."""
    conexao = db.abrir(sandbox.db_path)
    yield conexao
    conexao.close()


# --------------------------------------------------------------------------- #
# Guard neutro por padrão
# --------------------------------------------------------------------------- #


# --------------------------------------------------------------------------- #
# Ollama falso — nenhum teste depende do Ollama real (RNF-05)
# --------------------------------------------------------------------------- #


@dataclass
class OllamaFalso:
    """Binário roteirizado que substitui o `ollama` nos testes."""

    binario: Path
    log: Path
    monkeypatch: object

    def modo(self, nome: str, **ambiente: str) -> None:
        """Escolhe o roteiro da próxima chamada."""
        self.monkeypatch.setenv("FAKE_OLLAMA_MODO", nome)
        for chave, valor in ambiente.items():
            self.monkeypatch.setenv(chave, str(valor))

    @property
    def chamadas(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [
            json.loads(linha)
            for linha in self.log.read_text(encoding="utf-8").splitlines()
            if linha.strip()
        ]

    def limpar(self) -> None:
        if self.log.exists():
            self.log.write_text("", encoding="utf-8")


@pytest.fixture
def ollama_falso(tmp_path, monkeypatch) -> OllamaFalso:
    """Gera um `.cmd` que encaminha para `tests/fake_ollama.py` e aponta `OLLAMA_BIN`."""
    script = Path(__file__).parent / "fake_ollama.py"
    binario = tmp_path / "ollama-falso.cmd"
    binario.write_text(
        f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
    )
    log = tmp_path / "chamadas-ollama.jsonl"

    monkeypatch.setenv("OLLAMA_BIN", str(binario))
    monkeypatch.setenv("OLLAMA_MODEL", "phi3:mini")
    monkeypatch.setenv("FAKE_OLLAMA_LOG", str(log))
    monkeypatch.setenv("FAKE_OLLAMA_MODO", "json_puro")
    config.get_config.cache_clear()

    # o aviso de indisponibilidade é uma vez por processo; zera entre testes
    from organizer import llm

    monkeypatch.setattr(llm, "_avisou", False)
    monkeypatch.setattr(llm, "_motivo_da_falha", {})

    return OllamaFalso(binario, log, monkeypatch)


#: Módulo que testa o próprio guard e por isso precisa dos leitores reais.
_MODULO_DO_GUARD = "test_guard.py"


@pytest.fixture(autouse=True)
def guard_neutro(request, monkeypatch):
    """Nenhum teste paga 1 s de `psutil.cpu_percent` nem depende da GPU real.

    `tests/test_guard.py` fica de fora: é ele que exercita os leitores de verdade.
    """
    if Path(str(request.node.fspath)).name == _MODULO_DO_GUARD:
        return
    monkeypatch.setattr(guard, "_ler_cpu_ram", lambda intervalo=1.0: (1.0, 1.0))
    monkeypatch.setattr(guard, "_ler_gpu", lambda indice=0: (0.0, 0.0))
