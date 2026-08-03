"""RF-02, RF-03, RF-04 — configuração, validação fail-fast e cache."""

from __future__ import annotations

import re
from dataclasses import fields
from pathlib import Path

import pytest

from organizer import config

RAIZ_PROJETO = Path(__file__).resolve().parent.parent


def test_env_example_cobre_todas_as_chaves():
    """RF-02: `.env.example` documenta exatamente os campos do dataclass Config."""
    exemplo = RAIZ_PROJETO / ".env.example"
    assert exemplo.is_file(), ".env.example precisa existir"
    chaves_arquivo = set(config.ler_env(exemplo))
    chaves_config = {f.name.upper() for f in fields(config.Config)}
    assert chaves_arquivo == chaves_config, (
        f"faltando no .env.example: {sorted(chaves_config - chaves_arquivo)}; "
        f"sobrando: {sorted(chaves_arquivo - chaves_config)}"
    )


def test_ambiente_vence_env(tmp_path, monkeypatch, sandbox):
    """RF-01: variável de ambiente real sobrepõe o valor do arquivo."""
    arquivo = tmp_path / "custom.env"
    arquivo.write_text("CONFIDENCE_MIN=0.10\nMAX_WORKERS=9\n", encoding="utf-8")
    monkeypatch.delenv("MAX_WORKERS", raising=False)
    monkeypatch.setenv("CONFIDENCE_MIN", "0.42")
    cfg = config.montar(arquivo)
    assert cfg.confidence_min == 0.42  # do ambiente
    assert cfg.max_workers == 9  # do arquivo


@pytest.mark.parametrize("caso", ["iguais", "target_dentro", "db_dentro", "log_dentro"])
def test_recusa_raizes_sobrepostas(tmp_path, monkeypatch, caso):
    """RF-03: quatro configurações perigosas, todas recusadas com ConfigError."""
    downloads = tmp_path / "Downloads"
    target = tmp_path / "Organizado"
    downloads.mkdir()
    target.mkdir()

    valores = {
        "DOWNLOADS_DIR": downloads,
        "TARGET_ROOT": target,
        "DB_PATH": target / ".foa" / "index.db",
        "LOG_DIR": target / ".foa" / "logs",
    }
    if caso == "iguais":
        valores["TARGET_ROOT"] = downloads
        valores["DB_PATH"] = downloads / ".foa" / "index.db"
        valores["LOG_DIR"] = downloads / ".foa" / "logs"
    elif caso == "target_dentro":
        valores["TARGET_ROOT"] = downloads / "Organizado"
        valores["DB_PATH"] = downloads / "Organizado" / ".foa" / "index.db"
        valores["LOG_DIR"] = downloads / "Organizado" / ".foa" / "logs"
    elif caso == "db_dentro":
        valores["DB_PATH"] = downloads / ".foa" / "index.db"
    elif caso == "log_dentro":
        valores["LOG_DIR"] = downloads / ".foa" / "logs"

    monkeypatch.delenv(config.VAR_AMBIENTE, raising=False)
    for chave, valor in valores.items():
        monkeypatch.setenv(chave, str(valor))
    monkeypatch.setenv("INBOX_DIRNAME", "_Inbox")

    with pytest.raises(config.ConfigError):
        config.montar(tmp_path / "inexistente.env")


def test_recusa_raiz_fora_do_sandbox_em_modo_teste(tmp_path, monkeypatch):
    """Barreira 4: com FOA_ENV=test, raiz fora do sandbox é recusada."""
    monkeypatch.setenv(config.VAR_AMBIENTE, "test")
    monkeypatch.setenv(config.VAR_RAIZ_TESTE, str(tmp_path))
    monkeypatch.setenv("DOWNLOADS_DIR", str(tmp_path / "Downloads"))
    monkeypatch.setenv("TARGET_ROOT", str(Path.home() / "Organizado-que-nao-deve-ser-usado"))
    monkeypatch.setenv("DB_PATH", str(tmp_path / "db" / "index.db"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    with pytest.raises(config.ConfigError):
        config.montar(tmp_path / "inexistente.env")


def test_raiz_obrigatoria(tmp_path, monkeypatch):
    """RF-01: sem DOWNLOADS_DIR não há default hardcoded — o agente recusa iniciar."""
    monkeypatch.delenv(config.VAR_AMBIENTE, raising=False)
    monkeypatch.delenv("DOWNLOADS_DIR", raising=False)
    monkeypatch.setenv("TARGET_ROOT", str(tmp_path / "Organizado"))
    with pytest.raises(config.ConfigError):
        config.montar(tmp_path / "inexistente.env")


def test_cache_e_clear(sandbox):
    """RF-04: `get_config()` é cacheado e expõe `cache_clear()`."""
    primeiro = config.get_config()
    assert config.get_config() is primeiro
    config.get_config.cache_clear()
    segundo = config.get_config()
    assert segundo is not primeiro
    assert segundo == primeiro


def test_inbox_dentro_do_target(sandbox):
    assert sandbox.cfg.inbox_dir.parent == sandbox.cfg.target_root
    assert sandbox.cfg.duplicados_dir.parent == sandbox.cfg.inbox_dir


def test_modo_invalido_recusado(sandbox, monkeypatch):
    monkeypatch.setenv("MODE", "turbo")
    config.get_config.cache_clear()
    with pytest.raises(config.ConfigError):
        config.montar(Path("inexistente.env"))


def test_parser_env_tolera_comentario_e_aspas(tmp_path):
    arquivo = tmp_path / "a.env"
    arquivo.write_text(
        '# comentario\nMODE="auto"   # inline\n\nMAX_WORKERS=3\nLIXO\n', encoding="utf-8"
    )
    dados = config.ler_env(arquivo)
    assert dados["MODE"] == "auto"
    assert dados["MAX_WORKERS"] == "3"
    assert "LIXO" not in dados


@pytest.mark.parametrize(
    "chave,valor",
    [
        ("STABILITY_INTERVAL", "0"),
        ("STABILITY_INTERVAL", "-1"),
        ("STABILITY_TIMEOUT", "0"),
        ("STABILITY_POLLS", "0"),
    ],
)
def test_recusa_estabilidade_degenerada(sandbox, monkeypatch, chave, valor):
    """D5: intervalo zero faria `aguardar_estabilidade` girar a 100% de CPU."""
    monkeypatch.setenv(chave, valor)
    config.get_config.cache_clear()
    with pytest.raises(config.ConfigError):
        config.montar(Path("inexistente.env"))


def test_espera_de_estabilidade_nunca_vira_busy_loop(sandbox, monkeypatch):
    """D5 (segunda linha de defesa): mesmo com intervalo 0, o laço dorme.

    Prova por contagem: com timeout de 0,3 s e piso de 0,05 s, o número de
    iterações é limitado. Sem o piso, seriam dezenas de milhares.
    """
    from organizer import stability

    voltas = {"n": 0}

    def sempre_em_uso(caminho):
        voltas["n"] += 1
        return stability.EM_USO

    alvo = sandbox.downloads / "travado.pdf"
    alvo.write_bytes(b"x")
    monkeypatch.setattr(stability, "probe_handle", sempre_em_uso)

    pronto, motivo = stability.aguardar_estabilidade(alvo, polls=1, intervalo=0, timeout=0.3)

    assert pronto is False
    assert voltas["n"] <= 20, f"busy-loop: {voltas['n']} voltas em 0,3 s"
    assert stability.INTERVALO_MINIMO_ESPERA > 0


def test_env_example_nao_tem_chave_desconhecida():
    """Nenhuma chave do exemplo pode ser ignorada silenciosamente pelo parser."""
    exemplo = (RAIZ_PROJETO / ".env.example").read_text(encoding="utf-8")
    for linha in exemplo.splitlines():
        if linha.strip() and not linha.startswith("#"):
            assert re.match(r"^[A-Z0-9_]+=", linha), linha
