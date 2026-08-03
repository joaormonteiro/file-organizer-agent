"""Notificação no Windows via `plyer` — Fase 5.

**Estado: implementado como no-op resistente.** A regra de ouro já vale: se o
`plyer` falhar (comum em sessão de serviço), nada propaga — a notificação é
conveniência, não mecanismo (RF-73).
"""

from __future__ import annotations

from organizer import log

TITULO_PADRAO = "File Organizer Agent"
TIMEOUT_SEGUNDOS = 8

_logger = log.get_logger("notify")


def notificar(titulo: str, mensagem: str) -> bool:
    """Dispara o toast. Devolve `False` (silenciosamente) se não foi possível."""
    try:
        from plyer import notification

        notification.notify(
            title=titulo or TITULO_PADRAO,
            message=mensagem,
            app_name=TITULO_PADRAO,
            timeout=TIMEOUT_SEGUNDOS,
        )
        return True
    except Exception as exc:
        _logger.debug("notificação suprimida: %s", exc)
        return False
