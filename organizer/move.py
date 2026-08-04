"""Único módulo autorizado a alterar o filesystem do usuário.

Regras invioláveis (ARQUITETURA §5), todas verificáveis por grep:
- **Nunca sobrescrever**: o destino é sempre reservado com `O_CREAT|O_EXCL`, e
  `os.replace` só é usado sobre a própria reserva.
- **Nunca deletar**, com exatamente duas exceções, ambas logadas em `INFO`:
  (a) a reserva de 0 byte criada por este processo e registrada em `reservas`;
  (b) a origem de um move entre volumes, e somente depois de conferir o sha256,
  e somente com `ALLOW_CROSS_VOLUME=1`.
- **Journal write-ahead**: a intenção vai para `operacoes` antes de qualquer
  alteração no disco.

Requisitos cobertos: RF-22, RF-23, RF-43, RF-44, RF-45, RNF-06, RNF-07, RNF-08.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from organizer import db, log, paths, rules
from organizer.queue import Motivo

STATUS_ORGANIZADO = "organizado"
STATUS_INBOX = "inbox"
STATUS_AGUARDANDO = "aguardando"
STATUS_DUPLICADO = "duplicado"

ESTADO_PLANEJADO = "planejado"
ESTADO_MOVIDO = "movido"
ESTADO_CONCLUIDO = "concluido"
ESTADO_ABORTADO = "abortado"
ESTADO_FALHOU = "falhou"
ESTADO_AGUARDANDO = "aguardando_aprovacao"

#: Sufixo do nome usado quando o destino já contém um arquivo idêntico.
SUFIXO_DUPLICADO = "-dup"

_logger = log.get_logger("move")

_FLAGS_RESERVA = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)


# --------------------------------------------------------------------------- #
# Estruturas
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Plano:
    """Destino calculado *sem* tocar em disco (só `stat`/`sha256` de leitura)."""

    base: Path
    indice: int
    status: str
    motivo: str
    duplicado_de: str | None = None
    viavel: bool = True


@dataclass(frozen=True)
class ResultadoMove:
    """O que aconteceu de fato."""

    ok: bool
    destino: Path | None
    motivo: str
    status: str
    op_id: int | None = None
    duplicado_de: str | None = None
    dry_run: bool = False


# --------------------------------------------------------------------------- #
# Cálculo do destino (leitura pura)
# --------------------------------------------------------------------------- #


def exige_aprovacao(cfg, ignorar: bool = False) -> bool:
    """Em `MODE=interactive`, **todo** arquivo espera aprovação (RF-74).

    Não depende de `decisao.para_inbox`: condicionar a aprovação à baixa
    confiança deixaria passar direto justamente a maioria dos arquivos — a
    "regra dos 90%" —, que é exatamente o que o modo interativo existe para
    revisar nos primeiros dias de uso.

    `ignorar=True` existe para **um** chamador: o move disparado por
    `inbox.aprovar()`. A aprovação já foi dada ali; exigi-la de novo mandaria o
    arquivo de volta para `_Aguardando`, que é onde ele já está — e a política
    de colisão o classificaria como duplicata de si mesmo, enterrando em
    `_Duplicados` justamente o arquivo que o usuário aprovou (RF-76).
    """
    return cfg.mode == "interactive" and not ignorar


def pasta_destino(cfg, decisao, ignorar_aprovacao: bool = False) -> Path:
    """`_Aguardando` no modo interativo; `_Inbox` na quarentena; categoria no resto."""
    if exige_aprovacao(cfg, ignorar_aprovacao):
        return cfg.aguardando_dir
    if decisao.para_inbox:
        return cfg.inbox_dir
    return cfg.target_root / Path(decisao.categoria)


def _nome_duplicado(destino: Path, cfg) -> Path:
    alvo = cfg.duplicados_dir
    n = 1
    while True:
        candidato = alvo / f"{destino.stem}{SUFIXO_DUPLICADO}{n}{destino.suffix}"
        if not candidato.exists():
            return candidato
        n += 1


def resolver_destino(cfg, decisao, ignorar_aprovacao: bool = False) -> Plano:
    """Aplica, nesta ordem, limite de caminho, duplicata e sufixo `-N` (RF-22)."""
    bruto = pasta_destino(cfg, decisao, ignorar_aprovacao) / decisao.nome_final
    if exige_aprovacao(cfg, ignorar_aprovacao):
        status = STATUS_AGUARDANDO
    else:
        status = STATUS_INBOX if decisao.para_inbox else STATUS_ORGANIZADO

    ajustado = paths.ensure_max_path(bruto)
    if ajustado is None:
        alternativo = paths.ensure_max_path(cfg.inbox_dir / decisao.origem.name)
        if alternativo is None:
            return Plano(bruto, 1, STATUS_INBOX, Motivo.PATH_MUITO_LONGO.value, viavel=False)
        return Plano(alternativo, 1, STATUS_INBOX, Motivo.PATH_MUITO_LONGO.value)

    for n in range(1, paths.MAX_CANDIDATOS + 1):
        candidato = paths.candidato_colisao(ajustado, n)
        if not candidato.exists():
            return Plano(ajustado, n, status, decisao.motivo)
        if paths.mesmo_conteudo(decisao.origem, candidato):
            return Plano(
                _nome_duplicado(ajustado, cfg),
                1,
                STATUS_DUPLICADO,
                Motivo.DUPLICADO.value,
                duplicado_de=str(candidato),
            )

    alternativo = paths.ensure_max_path(cfg.inbox_dir / decisao.origem.name)
    return Plano(
        alternativo or bruto,
        1,
        STATUS_INBOX,
        Motivo.COLISAO_IRRESOLVIVEL.value,
        viavel=alternativo is not None,
    )


# --------------------------------------------------------------------------- #
# Reserva (O_EXCL — livre de TOCTOU entre workers concorrentes)
# --------------------------------------------------------------------------- #


class ColisaoIrresolvivelError(RuntimeError):
    """999 candidatos tentados sem sucesso."""


def reservar(conn: db.Conexao, op_id: int, plano: Plano) -> Path:
    """Cria o arquivo de 0 byte que garante o destino. Quem perde a corrida
    recebe `FileExistsError` e tenta o próximo candidato."""
    for n in range(plano.indice, paths.MAX_CANDIDATOS + 1):
        alvo = paths.candidato_colisao(plano.base, n)
        alvo.parent.mkdir(parents=True, exist_ok=True)
        try:
            descritor = os.open(str(alvo), _FLAGS_RESERVA)
        except FileExistsError:
            continue
        os.close(descritor)
        db.reserva_criar(conn, op_id, str(alvo))
        db.journal_destino(conn, op_id, str(alvo))
        return alvo
    raise ColisaoIrresolvivelError(str(plano.base))


def _descartar_reserva(
    conn: db.Conexao, op_id: int, alvo: Path, permitir_parcial: bool = False
) -> None:
    """DELEÇÃO AUTORIZADA (a): remove o arquivo de destino que ESTE processo criou.

    Pré-condição inegociável, checada aqui: o caminho tem de estar registrado em
    `reservas` para esta operação. Sem esse registro, nada é apagado — nem que o
    arquivo esteja vazio.

    Normalmente o alvo é a reserva de 0 byte. `permitir_parcial=True` cobre o
    único caso em que a reserva deixa de ser vazia sem virar o arquivo final: a
    cópia entre volumes que falhou na conferência de sha256. Sem isso, a cópia
    parcial ficaria órfã na árvore de destino, fora do journal e fora de
    `reservas`, e a recuperação nunca a limparia.
    """
    registro = db.reserva_por_op(conn, op_id)
    if registro is None or Path(registro["path"]) != alvo:
        return
    try:
        if alvo.exists() and (alvo.stat().st_size == 0 or permitir_parcial):
            _logger.info(
                "removendo destino criado por este processo op_id=%s path=%s bytes=%s",
                op_id,
                alvo,
                alvo.stat().st_size,
            )
            os.unlink(str(alvo))
    except OSError as exc:  # pragma: no cover - disco/permissão
        _logger.warning("falha ao remover reserva %s: %s", alvo, exc)
    db.reserva_remover(conn, op_id)


# --------------------------------------------------------------------------- #
# Execução
# --------------------------------------------------------------------------- #


def _mover_entre_volumes(conn: db.Conexao, op_id: int, origem: Path, alvo: Path) -> None:
    """DELEÇÃO AUTORIZADA (b): copia, confere sha256 e só então remove a origem.

    A conferência usa `completo=True`: este é o único ponto do sistema que apaga
    um arquivo original do usuário, então o hash das pontas não serve — ele
    deixaria passar corrupção no miolo de um arquivo grande.
    """
    shutil.copy2(str(origem), str(alvo))
    if paths.sha256_arquivo(origem, completo=True) != paths.sha256_arquivo(alvo, completo=True):
        raise OSError(f"sha256 divergente ao copiar entre volumes: {origem} -> {alvo}")
    _logger.info(
        "move entre volumes concluído, removendo origem op_id=%s origem=%s destino=%s",
        op_id,
        origem,
        alvo,
    )
    os.unlink(str(origem))


def executar(
    conn: db.Conexao, cfg, decisao, ignorar_aprovacao: bool = False
) -> ResultadoMove:
    """Move o arquivo seguindo o journal write-ahead (ARQUITETURA §12).

    `ignorar_aprovacao=True` é exclusivo de `inbox.aprovar()`, que está
    executando o plano que o usuário acabou de aprovar — ver `exige_aprovacao`.
    """
    origem = Path(decisao.origem)
    if not origem.exists():
        return ResultadoMove(False, None, Motivo.SUMIU.value, STATUS_INBOX)

    plano = resolver_destino(cfg, decisao, ignorar_aprovacao)
    if not plano.viavel:
        return ResultadoMove(False, None, plano.motivo, STATUS_INBOX)

    cruzando_volume = not paths.mesmo_volume(origem, plano.base)
    if cruzando_volume and not cfg.allow_cross_volume:
        _logger.error(
            "move entre volumes recusado (%s -> %s). Ligue ALLOW_CROSS_VOLUME=1 para permitir.",
            origem,
            plano.base,
        )
        return ResultadoMove(False, None, Motivo.CROSS_VOLUME.value, STATUS_INBOX)

    # ---- journal ANTES de qualquer alteração no filesystem (RF-43) ----
    op_id = db.journal_planejar(
        conn,
        str(origem),
        str(paths.candidato_colisao(plano.base, plano.indice)),
        confianca=decisao.confianca,
        via=decisao.via,
        categoria=decisao.categoria,
        dry_run=cfg.dry_run,
        pid=os.getpid(),
    )

    if cfg.dry_run:
        db.journal_estado(conn, op_id, ESTADO_CONCLUIDO)
        return ResultadoMove(
            True,
            paths.candidato_colisao(plano.base, plano.indice),
            plano.motivo,
            plano.status,
            op_id,
            plano.duplicado_de,
            dry_run=True,
        )

    aguardando_aprovacao = exige_aprovacao(cfg, ignorar_aprovacao)

    try:
        alvo = reservar(conn, op_id, plano)
    except ColisaoIrresolvivelError:
        db.journal_estado(conn, op_id, ESTADO_FALHOU, Motivo.COLISAO_IRRESOLVIVEL.value)
        return ResultadoMove(False, None, Motivo.COLISAO_IRRESOLVIVEL.value, STATUS_INBOX, op_id)

    try:
        if cruzando_volume:
            _mover_entre_volumes(conn, op_id, origem, alvo)
        else:
            os.replace(str(origem), str(alvo))
    except OSError as exc:
        # num move entre volumes a reserva pode ter virado uma cópia parcial;
        # ela é nossa e precisa sair, senão fica órfã fora do journal
        _descartar_reserva(conn, op_id, alvo, permitir_parcial=cruzando_volume)
        db.journal_estado(conn, op_id, ESTADO_FALHOU, str(exc))
        raise

    # em MODE=interactive a operação para aqui, esperando o `inbox.py --aprovar`.
    # O plano fica no banco e por isso sobrevive a reboot (RF-74, RF-79).
    db.journal_estado(conn, op_id, ESTADO_AGUARDANDO if aguardando_aprovacao else ESTADO_MOVIDO)
    db.reserva_remover(conn, op_id)
    return ResultadoMove(True, alvo, plano.motivo, plano.status, op_id, plano.duplicado_de)


def concluir(conn: db.Conexao, op_id: int) -> None:
    """Fecha a operação depois que a indexação já foi gravada."""
    db.journal_estado(conn, op_id, ESTADO_CONCLUIDO)


# --------------------------------------------------------------------------- #
# Recuperação (ARQUITETURA §12) — roda no startup, antes de qualquer evento
# --------------------------------------------------------------------------- #


def _indexar_de_operacao(conn: db.Conexao, operacao: db.Linha) -> None:
    """Reexecuta a indexação de uma operação já movida (UPSERT idempotente)."""
    destino = Path(operacao["destino"])
    origem = Path(operacao["origem"])
    categoria = operacao["categoria"] or rules.CAT_OUTROS
    tipo, subtipo = rules.tipo_subtipo_da_categoria(categoria)
    try:
        tamanho = destino.stat().st_size
        sha = paths.sha256_arquivo(destino)
    except OSError:  # pragma: no cover - destino sumiu entre a checagem e o stat
        tamanho, sha = None, None
    db.inserir_arquivo(
        conn,
        nome_orig=origem.name,
        nome_atual=destino.name,
        path=str(destino),
        path_orig=str(origem),
        tipo=tipo,
        subtipo=subtipo,
        ext=destino.suffix.lower(),
        tamanho=tamanho,
        sha256=sha,
        via=operacao["via"] or "fallback",
        confianca=operacao["confianca"],
        status=STATUS_ORGANIZADO,
        motivo=Motivo.RECUPERADO.value,
        movido_em=db.ts(),
    )


def recover_incomplete(conn: db.Conexao, cfg) -> dict[str, int]:
    """Cobre as seis linhas da tabela da ARQUITETURA §12 (RF-44, RF-45).

    Cada operação é recuperada dentro do seu próprio `try`: uma única falha de
    I/O (volume removido, unidade de rede caída, permissão negada) não pode
    derrubar a recuperação inteira nem, por tabela, o watcher — ARQUITETURA §18
    diz que nenhuma exceção mata o processo permanente.
    """
    contadores = {
        "reenfileirados": 0,
        "reserva_refeita": 0,
        "replace_ja_ocorrido": 0,
        "abortados": 0,
        "reindexados": 0,
        "falhas": 0,
    }

    for operacao in db.journal_por_estado(conn, (ESTADO_PLANEJADO,)):
        try:
            _recuperar_planejada(conn, cfg, operacao, contadores)
        except Exception as exc:
            contadores["falhas"] += 1
            _logger.error("recuperação falhou para op_id=%s: %s", operacao["id"], exc)
            try:
                db.journal_estado(conn, int(operacao["id"]), ESTADO_FALHOU, str(exc))
            except Exception:  # pragma: no cover - banco indisponível
                pass

    for operacao in db.journal_por_estado(conn, (ESTADO_MOVIDO,)):
        try:
            _indexar_de_operacao(conn, operacao)
            db.remover_pendente(conn, str(operacao["origem"]))
            db.liberar_lock(conn, str(operacao["origem"]))
            db.journal_estado(conn, int(operacao["id"]), ESTADO_CONCLUIDO)
            contadores["reindexados"] += 1
        except Exception as exc:
            contadores["falhas"] += 1
            _logger.error("reindexação falhou para op_id=%s: %s", operacao["id"], exc)

    return contadores


def _reenfileirar(conn: db.Conexao, op_id: int, origem: Path, contadores: dict[str, int]) -> None:
    db.inserir_pendente(conn, str(origem), db.ts(), Motivo.RECUPERADO.value)
    db.journal_estado(conn, op_id, ESTADO_ABORTADO, Motivo.RECUPERADO.value)
    contadores["reenfileirados"] += 1


def _recuperar_planejada(conn: db.Conexao, cfg, operacao, contadores: dict[str, int]) -> None:
    """Uma linha `planejado` do journal, segundo a tabela da ARQUITETURA §12."""
    op_id = int(operacao["id"])
    origem = Path(operacao["origem"])
    destino = Path(operacao["destino"])
    reserva = db.reserva_por_op(conn, op_id)
    tem_origem = origem.exists()
    tem_destino = destino.exists()

    if operacao["dry_run"]:
        db.journal_estado(conn, op_id, ESTADO_ABORTADO, Motivo.RECUPERADO.value)
        contadores["abortados"] += 1
        return

    if tem_origem and not tem_destino:
        _logger.info("recuperação: nada aconteceu, reenfileirando %s", origem)
        _reenfileirar(conn, op_id, origem, contadores)
        return

    nossa_reserva = (
        tem_origem
        and tem_destino
        and reserva is not None
        and Path(reserva["path"]) == destino
        and destino.stat().st_size == 0
    )
    if nossa_reserva:
        # o `os.replace` só é atômico dentro do mesmo volume; se a operação
        # original cruzava volumes, refazê-la aqui levantaria OSError
        if not paths.mesmo_volume(origem, destino):
            _logger.warning(
                "recuperação: operação entre volumes não é refeita aqui, reenfileirando %s",
                origem,
            )
            _descartar_reserva(conn, op_id, destino)
            _reenfileirar(conn, op_id, origem, contadores)
            return
        _logger.info("recuperação: refazendo replace sobre a própria reserva %s", destino)
        os.replace(str(origem), str(destino))
        db.reserva_remover(conn, op_id)
        db.journal_estado(conn, op_id, ESTADO_MOVIDO)
        contadores["reserva_refeita"] += 1
        return

    if not tem_origem and tem_destino:
        _logger.info("recuperação: replace já havia ocorrido para %s", destino)
        db.reserva_remover(conn, op_id)
        db.journal_estado(conn, op_id, ESTADO_MOVIDO)
        contadores["replace_ja_ocorrido"] += 1
        return

    if not tem_origem and not tem_destino:
        _logger.warning("recuperação: origem e destino sumiram, abortando op_id=%s", op_id)
        db.reserva_remover(conn, op_id)
        db.journal_estado(conn, op_id, ESTADO_ABORTADO, Motivo.SUMIU.value)
        contadores["abortados"] += 1
        return

    # origem e destino existem, mas o destino não é a nossa reserva:
    # outro processo (ou o usuário) criou algo ali. Reenfileira sem tocar.
    _logger.warning("recuperação: destino ocupado por terceiro, reenfileirando %s", origem)
    _reenfileirar(conn, op_id, origem, contadores)
