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

import io
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

import pytest

RAIZ_PROJETO = Path(__file__).resolve().parent.parent
if str(RAIZ_PROJETO) not in sys.path:
    sys.path.insert(0, str(RAIZ_PROJETO))

from organizer import config, db, guard, log, rules  # noqa: E402

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
    documents: Path
    pictures: Path
    videos: Path
    music: Path
    desktop: Path
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
        """Caminho completo de destino desta categoria, na raiz certa das 5."""
        return rules.raiz_de(self.cfg, categoria) / Path(categoria)


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
    "GEMINI_API_KEY": "",
    "GEMINI_MODEL": "gemini-3.6-flash",
    "LLM_TIMEOUT": "5",
    "LLM_SAMPLES": "1",
    "EMBEDDING_BACKEND": "none",
    "EMBEDDING_MODEL": "minishlab/potion-multilingual-128M",
    "SEARCH_RERANK_LLM": "0",
}


@pytest.fixture
def sandbox(tmp_path, tmp_path_factory, monkeypatch) -> Sandbox:
    """Monta o sandbox e aponta toda a configuração para dentro dele.

    5 raízes de destino, uma por pasta padrão do Windows (ARQUITETURA §9),
    todas dentro do `tmp_path` do teste — nada toca nas pastas reais do SO.
    """
    raiz = tmp_path / "sandbox"
    downloads = raiz / "Downloads"
    documents = raiz / "Documents"
    pictures = raiz / "Pictures"
    videos = raiz / "Videos"
    music = raiz / "Music"
    desktop = raiz / "Desktop"
    db_path = raiz / ".foa" / "index.db"
    log_dir = raiz / ".foa" / "logs"
    downloads.mkdir(parents=True)
    for pasta in (documents, pictures, videos, music, desktop):
        pasta.mkdir(parents=True)

    monkeypatch.setenv(config.VAR_AMBIENTE, "test")
    monkeypatch.setenv(config.VAR_RAIZ_TESTE, str(Path(tmp_path_factory.getbasetemp()).resolve()))
    monkeypatch.setenv("DOWNLOADS_DIR", str(downloads))
    monkeypatch.setenv("DOCUMENTS_ROOT", str(documents))
    monkeypatch.setenv("PICTURES_ROOT", str(pictures))
    monkeypatch.setenv("VIDEOS_ROOT", str(videos))
    monkeypatch.setenv("MUSIC_ROOT", str(music))
    monkeypatch.setenv("DESKTOP_ROOT", str(desktop))
    monkeypatch.setenv("INBOX_DIRNAME", "_Inbox")
    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("LOG_DIR", str(log_dir))
    for chave, valor in _ENV_PADRAO.items():
        monkeypatch.setenv(chave, valor)

    config.get_config.cache_clear()
    log.resetar()
    cfg = config.get_config()
    log.configurar(cfg.log_dir)

    yield Sandbox(raiz, downloads, documents, pictures, videos, music, desktop, db_path, log_dir, cfg)

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
# Gemini falso — nenhum teste depende de rede ou de uma chave real (RNF-05)
# --------------------------------------------------------------------------- #

RESPOSTA_VALIDA_GEMINI = {
    "categoria": "Documentos/Academico/UNIFESP/Matrizes-Curriculares",
    "nome_sugerido": "matriz-curricular-engenharia-computacao-2026",
    "confianca": 0.88,
    "motivo": "texto cita grade curricular e UNIFESP",
}


def _envelope(texto: str) -> bytes:
    """Empacota `texto` no formato real de resposta da API do Gemini."""
    return json.dumps({"candidates": [{"content": {"parts": [{"text": texto}]}}]}).encode("utf-8")


class _RespostaFalsa:
    """Suficiente do protocolo de `http.client.HTTPResponse` usado por `llm.executar`."""

    def __init__(self, corpo: bytes):
        self._corpo = corpo

    def read(self) -> bytes:
        return self._corpo

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@dataclass
class GeminiFalso:
    """Dublê de `urllib.request.urlopen` roteirizado por modo, substitui a API real."""

    monkeypatch: object
    _modo: str = "json_puro"
    _log: list = field(default_factory=list)

    def modo(self, nome: str) -> None:
        """Escolhe o roteiro da próxima chamada."""
        self._modo = nome

    @property
    def chamadas(self) -> list[dict]:
        return self._log

    def limpar(self) -> None:
        self._log.clear()

    def urlopen(self, requisicao, timeout=None):
        prompt = json.loads(requisicao.data.decode("utf-8"))["contents"][0]["parts"][0]["text"]
        self._log.append(
            {
                "prompt": prompt,
                "url": requisicao.full_url,
                "chave": requisicao.get_header("X-goog-api-key"),
            }
        )
        modo = self._modo

        if modo == "dorme":
            raise TimeoutError("simulado")
        if modo == "erro":
            raise urllib.error.HTTPError(
                requisicao.full_url, 500, "internal error", {}, io.BytesIO(b"erro simulado")
            )
        if modo == "chave_invalida":
            raise urllib.error.HTTPError(
                requisicao.full_url, 401, "unauthorized", {}, io.BytesIO(b"invalid api key")
            )
        if modo == "sem_rede":
            raise urllib.error.URLError("getaddrinfo failed")
        if modo == "bloqueio_safety":
            return _RespostaFalsa(json.dumps({"candidates": []}).encode("utf-8"))
        if modo == "json_puro":
            return _RespostaFalsa(_envelope(json.dumps(RESPOSTA_VALIDA_GEMINI)))
        if modo == "cerca_markdown":
            return _RespostaFalsa(
                _envelope(f"```json\n{json.dumps(RESPOSTA_VALIDA_GEMINI)}\n```")
            )
        if modo == "prosa_com_json":
            return _RespostaFalsa(
                _envelope(
                    f"Claro! Analisei o documento e concluí o seguinte:\n"
                    f"{json.dumps(RESPOSTA_VALIDA_GEMINI)}\nEspero ter ajudado."
                )
            )
        if modo == "lixo":
            return _RespostaFalsa(_envelope("Desculpe, não consegui identificar este arquivo."))
        if modo == "lixo_depois_json":
            # falha na primeira chamada, acerta na segunda: prova o retry (RF-55)
            if len(self._log) == 1:
                return _RespostaFalsa(_envelope("Hmm, deixa eu pensar melhor..."))
            return _RespostaFalsa(_envelope(json.dumps(RESPOSTA_VALIDA_GEMINI)))
        if modo == "categoria_invalida":
            return _RespostaFalsa(
                _envelope(
                    json.dumps({**RESPOSTA_VALIDA_GEMINI, "categoria": "Documentos/Inventada/Nao-Existe"})
                )
            )
        if modo == "confianca_invalida":
            return _RespostaFalsa(
                _envelope(json.dumps({**RESPOSTA_VALIDA_GEMINI, "confianca": "muito alta"}))
            )
        if modo == "confianca_fora_do_intervalo":
            return _RespostaFalsa(
                _envelope(json.dumps({**RESPOSTA_VALIDA_GEMINI, "confianca": 42}))
            )
        if modo == "nome_sujo":
            return _RespostaFalsa(
                _envelope(
                    json.dumps(
                        {
                            **RESPOSTA_VALIDA_GEMINI,
                            "nome_sugerido": 'matriz<>:"/\\|?*curricular 2026.docx',
                        }
                    )
                )
            )
        raise AssertionError(f"modo desconhecido no GeminiFalso: {modo!r}")


@pytest.fixture
def gemini_falso(monkeypatch) -> GeminiFalso:
    """Substitui `urllib.request.urlopen` por um dublê roteirizável e injeta uma chave fake."""
    monkeypatch.setenv("GEMINI_API_KEY", "chave-de-teste-fake")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.6-flash")
    config.get_config.cache_clear()

    falso = GeminiFalso(monkeypatch)
    monkeypatch.setattr(urllib.request, "urlopen", falso.urlopen)

    # o aviso de indisponibilidade é uma vez por processo; zera entre testes
    from organizer import llm

    monkeypatch.setattr(llm, "_avisou", False)
    monkeypatch.setattr(llm, "_motivo_da_falha", {})

    return falso


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
