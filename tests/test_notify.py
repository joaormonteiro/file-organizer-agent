"""RF-73 — notificação no Windows via `plyer`."""

from __future__ import annotations

import sys
import types

from organizer import notify


def test_falha_de_plyer_nao_propaga(monkeypatch):
    """RF-73: `plyer` quebrado vira no-op silencioso, nunca exceção.

    É comum em sessão de serviço: a notificação é conveniência, não mecanismo.
    """

    class NotificacaoQuebrada:
        @staticmethod
        def notify(**kwargs):
            raise RuntimeError("nenhuma sessão interativa disponível")

    falso = types.ModuleType("plyer")
    falso.notification = NotificacaoQuebrada
    monkeypatch.setitem(sys.modules, "plyer", falso)

    assert notify.notificar("titulo", "mensagem") is False


def test_plyer_ausente_nao_propaga(monkeypatch):
    """Nem `ImportError` escapa."""
    monkeypatch.setitem(sys.modules, "plyer", None)
    assert notify.notificar("titulo", "mensagem") is False


def test_notificacao_bem_sucedida(monkeypatch):
    recebidas = []

    class NotificacaoOk:
        @staticmethod
        def notify(**kwargs):
            recebidas.append(kwargs)

    falso = types.ModuleType("plyer")
    falso.notification = NotificacaoOk
    monkeypatch.setitem(sys.modules, "plyer", falso)

    assert notify.notificar("Arquivo organizado", "setup.exe -> Instaladores") is True
    assert recebidas[0]["title"] == "Arquivo organizado"
    assert "setup.exe" in recebidas[0]["message"]
    assert recebidas[0]["timeout"] == notify.TIMEOUT_SEGUNDOS


def test_titulo_padrao(monkeypatch):
    recebidas = []
    falso = types.ModuleType("plyer")
    falso.notification = type("N", (), {"notify": staticmethod(lambda **k: recebidas.append(k))})
    monkeypatch.setitem(sys.modules, "plyer", falso)

    notify.notificar("", "corpo")
    assert recebidas[0]["title"] == notify.TITULO_PADRAO
