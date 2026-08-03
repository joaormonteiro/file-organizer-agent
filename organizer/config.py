"""Configuração do agente: `.env` + variáveis de ambiente reais (ambiente vence).

Nenhum outro módulo do pacote pode conter uma raiz de caminho literal — todas
saem daqui (RF-01). A validação é *fail-fast*: `get_config()` levanta
`ConfigError` antes de qualquer código tocar em disco (RF-03).

Requisitos cobertos: RF-01, RF-02, RF-03, RF-04.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields
from functools import lru_cache
from pathlib import Path

from organizer import paths

#: Nome do arquivo de configuração procurado na raiz do projeto.
NOME_ENV = ".env"

#: Raiz do projeto (dois níveis acima deste arquivo: organizer/config.py).
RAIZ_PROJETO = Path(__file__).resolve().parent.parent

#: Variável que marca a suíte de testes. Quando vale "test", `validar()` recusa
#: qualquer raiz fora de `FOA_TEST_ROOT` (barreira 4 da ARQUITETURA §14).
VAR_AMBIENTE = "FOA_ENV"
VAR_RAIZ_TESTE = "FOA_TEST_ROOT"


class ConfigError(RuntimeError):
    """Configuração inválida — o agente não pode iniciar."""


# --------------------------------------------------------------------------- #
# Dataclass
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Config:
    """Instantâneo imutável da configuração.

    O nome de cada campo em maiúsculas é exatamente a chave do `.env`, e é isso
    que `test_env_example_cobre_todas_as_chaves` verifica (RF-02).
    """

    # raízes
    downloads_dir: Path
    target_root: Path
    inbox_dirname: str
    db_path: Path
    log_dir: Path
    # comportamento
    mode: str
    dry_run: bool
    confidence_min: float
    watch_recursive: bool
    max_workers: int
    allow_cross_volume: bool
    # resource guard
    threshold_cpu: float
    threshold_ram: float
    threshold_gpu: float
    threshold_vram: float
    gpu_index: int
    retry_busy_minutes: int
    retry_startup_minutes: int
    idle_loop_seconds: int
    max_tentativas: int
    # estabilidade
    stability_polls: int
    stability_interval: float
    stability_timeout: float
    # LLM
    llm_enabled: bool
    ollama_bin: str
    ollama_model: str
    llm_timeout: float
    llm_samples: int
    # busca
    embedding_backend: str
    embedding_model: str
    search_rerank_llm: bool

    @property
    def inbox_dir(self) -> Path:
        """Pasta de quarentena, sempre dentro de `TARGET_ROOT`."""
        return self.target_root / self.inbox_dirname

    @property
    def duplicados_dir(self) -> Path:
        return self.inbox_dir / "_Duplicados"

    @property
    def aguardando_dir(self) -> Path:
        return self.inbox_dir / "_Aguardando"


#: Chaves do `.env`, derivadas do dataclass — fonte única para RF-02.
CHAVES = tuple(f.name.upper() for f in fields(Config))


# --------------------------------------------------------------------------- #
# Parser de .env (30 linhas, sem python-dotenv)
# --------------------------------------------------------------------------- #


def ler_env(arquivo: Path) -> dict[str, str]:
    """Lê um `.env` simples: `CHAVE=valor`, `#` comenta, aspas opcionais."""
    dados: dict[str, str] = {}
    if not arquivo.is_file():
        return dados
    for linha in arquivo.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#"):
            continue
        if linha.lower().startswith("export "):
            linha = linha[7:].lstrip()
        if "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        chave = chave.strip()
        valor = valor.split(" #", 1)[0].strip()
        if len(valor) >= 2 and valor[0] == valor[-1] and valor[0] in "\"'":
            valor = valor[1:-1]
        if chave:
            dados[chave] = valor
    return dados


def _fonte(arquivo: Path | None = None) -> dict[str, str]:
    """Mescla `.env` com o ambiente real — o ambiente sempre vence (RF-01)."""
    alvo = arquivo if arquivo is not None else RAIZ_PROJETO / NOME_ENV
    dados = ler_env(alvo)
    dados.update({k: v for k, v in os.environ.items() if k in CHAVES})
    return dados


# --------------------------------------------------------------------------- #
# Conversores
# --------------------------------------------------------------------------- #

_VERDADEIROS = {"1", "true", "yes", "on", "sim"}


def _texto(d: dict[str, str], chave: str, padrao: str) -> str:
    valor = d.get(chave, padrao)
    return valor if valor != "" else padrao


def _bool(d: dict[str, str], chave: str, padrao: bool) -> bool:
    bruto = d.get(chave)
    if bruto is None or bruto == "":
        return padrao
    return bruto.strip().lower() in _VERDADEIROS


def _num(d: dict[str, str], chave: str, padrao, conv):
    bruto = d.get(chave)
    if bruto is None or bruto == "":
        return padrao
    try:
        return conv(bruto.strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{chave}: valor numérico inválido ({bruto!r})") from exc


def _caminho_obrigatorio(d: dict[str, str], chave: str) -> Path:
    bruto = d.get(chave, "").strip()
    if not bruto:
        raise ConfigError(
            f"{chave} não definida. Copie .env.example para .env e ajuste as raízes."
        )
    return Path(os.path.expandvars(os.path.expanduser(bruto)))


# --------------------------------------------------------------------------- #
# Montagem e validação
# --------------------------------------------------------------------------- #


def montar(arquivo: Path | None = None) -> Config:
    """Constrói o `Config` sem cache (usado pelos testes e por `get_config`)."""
    d = _fonte(arquivo)
    target_root = _caminho_obrigatorio(d, "TARGET_ROOT")
    inbox_dirname = _texto(d, "INBOX_DIRNAME", "_Inbox")

    bruto_db = _texto(d, "DB_PATH", "")
    bruto_log = _texto(d, "LOG_DIR", "")
    db_path = Path(os.path.expandvars(bruto_db)) if bruto_db else target_root / ".foa" / "index.db"
    log_dir = Path(os.path.expandvars(bruto_log)) if bruto_log else target_root / ".foa" / "logs"

    cfg = Config(
        downloads_dir=_caminho_obrigatorio(d, "DOWNLOADS_DIR"),
        target_root=target_root,
        inbox_dirname=inbox_dirname,
        db_path=db_path,
        log_dir=log_dir,
        mode=_texto(d, "MODE", "auto").lower(),
        dry_run=_bool(d, "DRY_RUN", False),
        confidence_min=_num(d, "CONFIDENCE_MIN", 0.75, float),
        watch_recursive=_bool(d, "WATCH_RECURSIVE", False),
        max_workers=_num(d, "MAX_WORKERS", 2, int),
        allow_cross_volume=_bool(d, "ALLOW_CROSS_VOLUME", False),
        threshold_cpu=_num(d, "THRESHOLD_CPU", 70.0, float),
        threshold_ram=_num(d, "THRESHOLD_RAM", 80.0, float),
        threshold_gpu=_num(d, "THRESHOLD_GPU", 60.0, float),
        threshold_vram=_num(d, "THRESHOLD_VRAM", 70.0, float),
        gpu_index=_num(d, "GPU_INDEX", 0, int),
        retry_busy_minutes=_num(d, "RETRY_BUSY_MINUTES", 120, int),
        retry_startup_minutes=_num(d, "RETRY_STARTUP_MINUTES", 30, int),
        idle_loop_seconds=_num(d, "IDLE_LOOP_SECONDS", 600, int),
        max_tentativas=_num(d, "MAX_TENTATIVAS", 8, int),
        stability_polls=_num(d, "STABILITY_POLLS", 3, int),
        stability_interval=_num(d, "STABILITY_INTERVAL", 1.0, float),
        stability_timeout=_num(d, "STABILITY_TIMEOUT", 300.0, float),
        llm_enabled=_bool(d, "LLM_ENABLED", True),
        ollama_bin=_texto(d, "OLLAMA_BIN", "ollama"),
        ollama_model=_texto(d, "OLLAMA_MODEL", "phi3:mini"),
        llm_timeout=_num(d, "LLM_TIMEOUT", 90.0, float),
        llm_samples=_num(d, "LLM_SAMPLES", 1, int),
        embedding_backend=_texto(d, "EMBEDDING_BACKEND", "auto").lower(),
        embedding_model=_texto(d, "EMBEDDING_MODEL", "minishlab/potion-multilingual-128M"),
        search_rerank_llm=_bool(d, "SEARCH_RERANK_LLM", False),
    )
    validar(cfg)
    return cfg


def validar(cfg: Config) -> Config:
    """Recusa configurações que poriam dados do usuário em risco (RF-03).

    Regras:
    - `DOWNLOADS_DIR`, `TARGET_ROOT`, `DB_PATH` e `LOG_DIR` são dois a dois
      disjuntos (nenhum é igual ou subpasta do outro);
    - `INBOX_DIR` fica dentro de `TARGET_ROOT` e, portanto, fora do `DOWNLOADS_DIR`;
    - em `FOA_ENV=test`, toda raiz precisa estar dentro de `FOA_TEST_ROOT`.
    """
    if cfg.mode not in ("auto", "interactive"):
        raise ConfigError(f"MODE inválido: {cfg.mode!r} (use auto ou interactive)")
    if not (0.0 <= cfg.confidence_min <= 1.0):
        raise ConfigError(f"CONFIDENCE_MIN fora de [0,1]: {cfg.confidence_min}")
    if cfg.max_workers < 1:
        raise ConfigError("MAX_WORKERS precisa ser >= 1")
    if cfg.stability_polls < 1:
        raise ConfigError("STABILITY_POLLS precisa ser >= 1")
    if cfg.stability_interval <= 0:
        # com intervalo zero, `aguardar_estabilidade` giraria a 100% de CPU por
        # STABILITY_TIMEOUT segundos em cada worker — exatamente o que o
        # Resource Guard existe para evitar
        raise ConfigError("STABILITY_INTERVAL precisa ser > 0")
    if cfg.stability_timeout <= 0:
        raise ConfigError("STABILITY_TIMEOUT precisa ser > 0")
    if "/" in cfg.inbox_dirname or "\\" in cfg.inbox_dirname:
        raise ConfigError("INBOX_DIRNAME é um nome de pasta, não um caminho")

    # a raiz vigiada e a raiz de destino precisam ser mundos separados;
    # `DB_PATH` e `LOG_DIR` moram de propósito dentro de `TARGET_ROOT` (em `.foa`)
    try:
        paths.assert_disjoint(cfg.downloads_dir, cfg.target_root)
    except paths.CaminhosSobrepostosError as exc:
        raise ConfigError(str(exc)) from exc

    for nome, alvo in (
        ("DB_PATH", cfg.db_path),
        ("LOG_DIR", cfg.log_dir),
        ("INBOX_DIR", cfg.inbox_dir),
        ("TARGET_ROOT", cfg.target_root),
    ):
        if paths.is_subpath(alvo, cfg.downloads_dir):
            raise ConfigError(f"{nome} não pode ficar dentro de DOWNLOADS_DIR ({alvo})")

    if os.environ.get(VAR_AMBIENTE, "").lower() == "test":
        raiz_teste = os.environ.get(VAR_RAIZ_TESTE, "")
        if not raiz_teste:
            raise ConfigError(f"{VAR_AMBIENTE}=test exige {VAR_RAIZ_TESTE}")
        for nome, alvo in (
            ("DOWNLOADS_DIR", cfg.downloads_dir),
            ("TARGET_ROOT", cfg.target_root),
            ("DB_PATH", cfg.db_path),
            ("LOG_DIR", cfg.log_dir),
        ):
            if not paths.is_subpath(alvo, raiz_teste):
                raise ConfigError(
                    f"modo de teste: {nome}={alvo} está fora do sandbox {raiz_teste}"
                )
    return cfg


@lru_cache(maxsize=1)
def get_config() -> Config:
    """Configuração do processo, montada uma vez (RF-04: tem `cache_clear`)."""
    return montar()
