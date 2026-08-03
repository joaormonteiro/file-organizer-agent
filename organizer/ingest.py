"""Pipeline de um arquivo: estabilidade → guard → classify → move → índice.

Converte qualquer exceção em estado persistido (`pendentes` com erro, ou
`_Inbox`); nunca deixa vazar. É o corpo do processo filho efêmero.

Requisitos cobertos: RF-11, RF-24, RF-25, RF-26, RF-31, RF-37, RF-42,
RNF-08, RNF-13, RNF-14, RNF-15.
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import replace
from pathlib import Path

from organizer import classify, config, db, guard, log, move, paths, queue, rules, stability
from organizer.queue import Motivo  # reexport: `ingest.Motivo` é o nome canônico (RNF-15)

__all__ = ["Motivo", "processar", "EXIT_OK", "EXIT_ADIADO", "EXIT_ERRO"]

EXIT_OK = 0
EXIT_ADIADO = 2
EXIT_ERRO = 3

_logger = log.get_logger("ingest")


# --------------------------------------------------------------------------- #
# Filtros baratos (camada 1 da ARQUITETURA §7)
# --------------------------------------------------------------------------- #


def deve_ignorar(caminho: Path, cfg) -> str | None:
    """Motivo para descartar o arquivo sem enfileirar, ou `None` para seguir."""
    if caminho.is_dir():
        return Motivo.DIRETORIO.value
    if rules.nome_e_parcial(caminho.name):
        return Motivo.PARCIAL.value
    for raiz in (cfg.target_root, cfg.db_path.parent, cfg.log_dir):
        if paths.is_subpath(caminho, raiz):
            return Motivo.DENTRO_DO_TARGET.value
    return None


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


def _indexar(conn, cfg, decisao, resultado) -> None:
    destino = resultado.destino
    try:
        tamanho = destino.stat().st_size
        sha = paths.sha256_arquivo(destino)
    except OSError:  # pragma: no cover - destino sumiu logo após o move
        tamanho, sha = None, None
    db.inserir_arquivo(
        conn,
        nome_orig=decisao.origem.name,
        nome_atual=destino.name,
        path=str(destino),
        path_orig=str(decisao.origem),
        tipo=decisao.tipo,
        subtipo=decisao.subtipo,
        ext=decisao.origem.suffix.lower(),
        tamanho=tamanho,
        sha256=sha,
        via=decisao.via,
        confianca=decisao.confianca,
        status=resultado.status,
        motivo=resultado.motivo,
        duplicado_de=resultado.duplicado_de,
        texto_amostra=decisao.texto_amostra,
        movido_em=db.ts(),
    )


def processar(
    caminho: Path | str,
    cfg=None,
    conn=None,
    motivo_origem: Motivo | None = None,
    intervalo_cpu: float = 1.0,
) -> int:
    """Processa um único arquivo. Devolve o código de saída do worker."""
    cfg = cfg or config.get_config()
    origem = Path(caminho)
    fechar_conn = conn is None
    conn = conn or db.abrir(cfg.db_path)

    try:
        return _processar(conn, cfg, origem, motivo_origem, intervalo_cpu)
    except Exception as exc:  # nenhuma exceção pode escapar do worker
        _logger.error("erro inesperado em %s: %s\n%s", origem, exc, traceback.format_exc())
        try:
            queue.enfileirar(conn, origem, Motivo.ERRO_INESPERADO, cfg, erro=str(exc))
            db.liberar_lock(conn, str(origem))
        except Exception:  # pragma: no cover - banco indisponível
            pass
        return EXIT_ERRO
    finally:
        if fechar_conn:
            conn.close()


def _processar(conn, cfg, origem: Path, motivo_origem, intervalo_cpu: float) -> int:
    if not origem.exists():
        _logger.debug("arquivo sumiu antes do processamento: %s", origem)
        queue.concluir(conn, origem)
        return EXIT_OK

    descarte = deve_ignorar(origem, cfg)
    if descarte:
        log.logar_decisao(_logger, "ignorado", origem, motivo=descarte, nivel=logging.DEBUG)
        queue.concluir(conn, origem)
        return EXIT_OK

    # ---- camada 2 e 3: o arquivo ainda está sendo gravado? ----
    pronto, motivo_estabilidade = stability.aguardar_estabilidade(
        origem, cfg.stability_polls, cfg.stability_interval, cfg.stability_timeout
    )
    if not pronto:
        if motivo_estabilidade == Motivo.SUMIU:
            queue.concluir(conn, origem)
            return EXIT_OK
        queue.enfileirar(conn, origem, Motivo.INSTAVEL, cfg)
        db.liberar_lock(conn, str(origem))
        log.logar_decisao(_logger, "adiado", origem, motivo=Motivo.INSTAVEL.value)
        return EXIT_ADIADO

    # ---- resource guard (DIVERGÊNCIA 1: roda aqui, no filho) ----
    ocupado, recursos = guard.sistema_ocupado(cfg, intervalo_cpu)
    if ocupado:
        motivo_fila = (
            Motivo.STARTUP if motivo_origem == Motivo.STARTUP else Motivo.OCUPADO
        )
        queue.enfileirar(conn, origem, motivo_fila, cfg, erro=recursos.resumo())
        db.liberar_lock(conn, str(origem))
        log.logar_decisao(_logger, "adiado", origem, motivo=Motivo.OCUPADO.value)
        return EXIT_ADIADO

    # ---- classificação (o ramo do LLM extrai o trecho e usa o cache em config_kv) ----
    decisao = classify.classificar(origem, cfg, conn=conn)

    pendente = db.pendente_por_path(conn, str(origem))
    if pendente is not None and queue.excedeu_tentativas(pendente["tentativas"], cfg.max_tentativas):
        _logger.warning(
            "tentativas esgotadas para %s (%s) — indo para o _Inbox",
            origem,
            pendente["tentativas"],
        )
        decisao = replace(
            decisao,
            para_inbox=True,
            motivo=Motivo.MAX_TENTATIVAS.value,
            nome_final=origem.name,
        )

    if not cfg.dry_run:
        rules.criar_arvore(cfg.target_root, cfg.inbox_dirname)

    resultado = move.executar(conn, cfg, decisao)

    if not resultado.ok:
        if resultado.motivo == Motivo.SUMIU.value:
            queue.concluir(conn, origem)
            return EXIT_OK
        motivo = Motivo(resultado.motivo)
        queue.enfileirar(conn, origem, motivo, cfg)
        db.liberar_lock(conn, str(origem))
        log.logar_decisao(_logger, "adiado", origem, motivo=resultado.motivo)
        return EXIT_ADIADO

    if resultado.dry_run:
        log.logar_decisao(
            _logger,
            "planejado",
            origem,
            dest=resultado.destino,
            conf=decisao.confianca,
            via=decisao.via,
            motivo=resultado.motivo,
        )
        queue.concluir(conn, origem)
        return EXIT_OK

    _indexar(conn, cfg, decisao, resultado)
    move.concluir(conn, resultado.op_id)
    queue.concluir(conn, origem)

    log.logar_decisao(
        _logger,
        "movido",
        origem,
        dest=resultado.destino,
        conf=decisao.confianca,
        via=decisao.via,
        motivo=resultado.motivo,
    )
    return EXIT_OK
