"""Dashboard do `_Inbox` e aprovação do modo interativo (Fase 5).

⚠ DIVERGÊNCIA 5: a spec pede "notificação que pede aprovação". O `plyer` no
Windows emite toast **sem callback de botão** — não há como capturar a resposta
do usuário pela notificação. Design honesto (ARQUITETURA §15):

1. o arquivo é movido para `_Inbox/_Aguardando/` (sai do Downloads, não vai ao
   destino final) e o plano fica em `operacoes(estado='aguardando_aprovacao')`;
2. `notify.notificar()` dispara o toast informativo;
3. `python inbox.py` mostra a tabela; `--aprovar <id>` executa o move planejado.

Como o plano vive no banco, ele sobrevive a reboot (RF-79).

Requisitos cobertos: RF-75, RF-76, RF-77, RF-78, RF-79.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from organizer import classify, config, db, log, move, rules
from organizer.queue import Motivo

_logger = log.get_logger("inbox")


def _decisao_de(operacao) -> classify.Decisao:
    """Reconstrói a decisão original a partir do plano gravado no banco."""
    atual = Path(operacao["destino"])
    categoria = rules.categoria_valida(operacao["categoria"]) or rules.CAT_OUTROS
    tipo, subtipo = rules.tipo_subtipo_da_categoria(categoria)
    return classify.Decisao(
        origem=atual,
        categoria=categoria,
        nome_final=atual.name,
        confianca=float(operacao["confianca"] or 0.0),
        via=operacao["via"] or classify.VIA_MANUAL,
        motivo=Motivo.OK.value,
        tipo=tipo,
        subtipo=subtipo,
        generico=False,
        para_inbox=False,
    )


def listar(conn) -> list:
    """Itens aguardando aprovação, na ordem em que entraram."""
    return db.operacoes_aguardando(conn)


def aprovar(conn, cfg, op_id: int) -> move.ResultadoMove | None:
    """Executa o move planejado usando o **mesmo** `move.executar` (RF-76).

    Mesmo journal, mesma reserva `O_EXCL`, mesma política de colisão. Nada aqui
    reimplementa a movimentação.
    """
    operacao = db.journal_por_id(conn, op_id)
    if operacao is None or operacao["estado"] != move.ESTADO_AGUARDANDO:
        return None
    atual = Path(operacao["destino"])
    if not atual.exists():
        db.journal_estado(conn, op_id, move.ESTADO_ABORTADO, Motivo.SUMIU.value)
        return None

    decisao = _decisao_de(operacao)
    rules.criar_arvore(cfg.target_root, cfg.inbox_dirname)
    anterior = db.buscar_por_path(conn, str(atual))
    try:
        # a aprovação já foi dada aqui: este move NÃO pode voltar a exigi-la,
        # senão o destino vira o próprio `_Aguardando` e o arquivo é tratado
        # como duplicata de si mesmo (RF-76)
        resultado = move.executar(conn, cfg, decisao, ignorar_aprovacao=True)
    except OSError as exc:
        # disco cheio, permissão negada, volume removido: o `move` já registrou
        # `falhou` no journal e limpou a reserva. Aqui só não deixamos o
        # traceback vazar para a CLI (o arquivo continua no `_Aguardando`).
        _logger.error("falha ao aprovar op_id=%s: %s", op_id, exc)
        return move.ResultadoMove(False, None, str(exc), move.STATUS_AGUARDANDO, op_id)
    if not resultado.ok:
        return resultado

    db.inserir_arquivo(
        conn,
        nome_orig=anterior["nome_orig"] if anterior else atual.name,
        nome_atual=resultado.destino.name,
        path=str(resultado.destino),
        path_orig=operacao["origem"],
        tipo=decisao.tipo,
        subtipo=decisao.subtipo,
        ext=atual.suffix.lower(),
        tamanho=anterior["tamanho"] if anterior else None,
        sha256=anterior["sha256"] if anterior else None,
        via=decisao.via,
        confianca=decisao.confianca,
        status=move.STATUS_ORGANIZADO,
        motivo=Motivo.OK.value,
        texto_amostra=anterior["texto_amostra"] if anterior else None,
        embedding=anterior["embedding"] if anterior else None,
        embedding_model=anterior["embedding_model"] if anterior else None,
        embedding_dim=anterior["embedding_dim"] if anterior else None,
        movido_em=db.ts(),
    )
    db.remover_arquivo(conn, str(atual))
    move.concluir(conn, resultado.op_id)
    db.journal_estado(conn, op_id, move.ESTADO_CONCLUIDO)
    return resultado


def rejeitar(conn, op_id: int) -> bool:
    """Mantém o arquivo onde está (dentro do `_Inbox`) e aborta o plano (RF-77).

    Nenhum arquivo é deletado nem movido: `_Inbox/_Aguardando/` já é o `_Inbox`.
    """
    operacao = db.journal_por_id(conn, op_id)
    if operacao is None or operacao["estado"] != move.ESTADO_AGUARDANDO:
        return False
    db.journal_estado(conn, op_id, move.ESTADO_ABORTADO, "rejeitado pelo usuario")
    return True


def aprovar_em_lote(conn, cfg, acima: float) -> list[int]:
    """Aprova só os itens com confiança **estritamente acima** do limiar (RF-78)."""
    aprovados = []
    for operacao in listar(conn):
        if float(operacao["confianca"] or 0.0) > acima:
            if aprovar(conn, cfg, int(operacao["id"])) is not None:
                aprovados.append(int(operacao["id"]))
    return aprovados


def _tabela(itens, cfg):
    from rich.table import Table

    tabela = Table(title="_Inbox — aguardando aprovação")
    for coluna in ("id", "nome", "destino proposto", "confiança", "motivo"):
        tabela.add_column(coluna, overflow="fold")
    for operacao in itens:
        tabela.add_row(
            str(operacao["id"]),
            Path(operacao["destino"]).name,
            str(cfg.target_root / Path(operacao["categoria"] or rules.CAT_OUTROS)),
            f"{float(operacao['confianca'] or 0):.2f}",
            operacao["erro"] or Motivo.AGUARDANDO_APROVACAO.value,
        )
    return tabela


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dashboard do _Inbox do File Organizer Agent.")
    parser.add_argument("--aprovar", type=int, metavar="ID", help="executa o move planejado")
    parser.add_argument("--rejeitar", type=int, metavar="ID", help="mantém o arquivo no _Inbox")
    parser.add_argument("--aprovar-todos", action="store_true", help="aprova em lote")
    parser.add_argument("--acima", type=float, default=0.85, help="limiar do lote")
    args = parser.parse_args(argv)

    cfg = config.get_config()
    conn = db.abrir(cfg.db_path)
    from rich.console import Console

    console = Console()
    try:
        if args.aprovar is not None:
            resultado = aprovar(conn, cfg, args.aprovar)
            if resultado is None or not resultado.ok:
                console.print(f"[red]não foi possível aprovar o item {args.aprovar}[/red]")
                return 1
            console.print(f"[green]movido para[/green] {resultado.destino}")
            return 0

        if args.rejeitar is not None:
            if not rejeitar(conn, args.rejeitar):
                console.print(f"[red]item {args.rejeitar} não está aguardando aprovação[/red]")
                return 1
            console.print(f"[yellow]item {args.rejeitar} mantido no _Inbox[/yellow]")
            return 0

        if args.aprovar_todos:
            aprovados = aprovar_em_lote(conn, cfg, args.acima)
            console.print(
                f"[green]{len(aprovados)} item(ns) aprovado(s)[/green] acima de {args.acima}"
            )
            return 0 if aprovados else 1

        itens = listar(conn)
        if not itens:
            console.print("[green]nada aguardando aprovação.[/green]")
            return 0
        console.print(_tabela(itens, cfg))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
