"""Única porta de acesso ao SQLite. Nenhum SQL existe fora deste módulo (RF-05).

Todo o resto do pacote fala com o banco por estas funções. Isso é o que permite
auditar, num só lugar, idempotência, journal write-ahead e limpeza de locks.

Requisitos cobertos: RF-05, RF-06, RF-07, RF-24, RF-25, RF-43, RF-46, RF-64.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

#: Apelidos de tipo. Existem para que nenhum outro módulo precise importar o
#: driver do banco — a RF-05 exige que `sqlite3` apareça só aqui.
Conexao = sqlite3.Connection
Linha = sqlite3.Row

#: Formato de timestamp idêntico ao `datetime('now')` do SQLite (UTC, sem fuso).
FORMATO_TS = "%Y-%m-%d %H:%M:%S"

#: Milissegundos de espera antes de desistir de um lock de escrita (RF-06).
BUSY_TIMEOUT_MS = 10000

#: Idade máxima de um registro de `em_processamento` antes de ser considerado
#: órfão de um worker morto (RF-46).
LOCK_TTL_MINUTOS = 15


# --------------------------------------------------------------------------- #
# Tempo
# --------------------------------------------------------------------------- #


def agora() -> datetime:
    """Instante atual em UTC — a mesma referência do `datetime('now')` do SQLite."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def ts(momento: datetime | None = None) -> str:
    """Serializa um instante no formato de texto usado por todas as colunas."""
    return (momento or agora()).strftime(FORMATO_TS)


def ts_mais(minutos: float, base: datetime | None = None) -> str:
    """Timestamp deslocado — usado para calcular `retry_after`."""
    return ts((base or agora()) + timedelta(minutes=minutos))


# --------------------------------------------------------------------------- #
# Migrations
# --------------------------------------------------------------------------- #

_V1 = [
    """
    CREATE TABLE IF NOT EXISTS arquivos (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        nome_orig       TEXT    NOT NULL,
        nome_atual      TEXT    NOT NULL,
        path            TEXT    NOT NULL UNIQUE,
        path_orig       TEXT,
        tipo            TEXT,
        subtipo         TEXT,
        ext             TEXT,
        tamanho         INTEGER,
        sha256          TEXT,
        via             TEXT CHECK(via IN ('extensao','llm','manual','fallback')),
        confianca       REAL,
        status          TEXT CHECK(status IN ('organizado','inbox','aguardando','duplicado')),
        motivo          TEXT,
        duplicado_de    TEXT,
        texto_amostra   TEXT,
        embedding       BLOB,
        embedding_model TEXT,
        embedding_dim   INTEGER,
        movido_em       TEXT,
        indexado_em     TEXT DEFAULT (datetime('now'))
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_arquivos_status ON arquivos(status)",
    "CREATE INDEX IF NOT EXISTS ix_arquivos_sha ON arquivos(sha256)",
    """
    CREATE TABLE IF NOT EXISTS pendentes (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        path        TEXT NOT NULL UNIQUE,
        detectado   TEXT DEFAULT (datetime('now')),
        retry_after TEXT NOT NULL,
        tentativas  INTEGER DEFAULT 0,
        motivo      TEXT,
        ultimo_erro TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_pendentes_retry ON pendentes(retry_after)",
    """
    CREATE TABLE IF NOT EXISTS operacoes (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        origem        TEXT NOT NULL,
        destino       TEXT NOT NULL,
        estado        TEXT NOT NULL CHECK(estado IN
                        ('planejado','movido','concluido','aguardando_aprovacao','abortado','falhou')),
        confianca     REAL,
        via           TEXT,
        categoria     TEXT,
        dry_run       INTEGER DEFAULT 0,
        pid           INTEGER,
        iniciado_em   TEXT DEFAULT (datetime('now')),
        finalizado_em TEXT,
        erro          TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_operacoes_estado ON operacoes(estado)",
    """
    CREATE TABLE IF NOT EXISTS reservas (
        op_id  INTEGER PRIMARY KEY REFERENCES operacoes(id),
        path   TEXT NOT NULL UNIQUE,
        criado TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS em_processamento (
        path        TEXT PRIMARY KEY,
        pid         INTEGER,
        iniciado_em TEXT DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS config_kv (
        chave     TEXT PRIMARY KEY,
        valor     TEXT,
        expira_em TEXT
    )
    """,
]

_COLUNAS_FTS = "nome_atual, nome_orig, tipo, subtipo, texto_amostra, path"

_V2 = [
    f"""
    CREATE VIRTUAL TABLE IF NOT EXISTS arquivos_fts USING fts5(
        nome_atual, nome_orig, tipo, subtipo, texto_amostra, path UNINDEXED,
        content='arquivos', content_rowid='id',
        tokenize="unicode61 remove_diacritics 2"
    )
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS arquivos_ai AFTER INSERT ON arquivos BEGIN
        INSERT INTO arquivos_fts(rowid, {_COLUNAS_FTS})
        VALUES (new.id, new.nome_atual, new.nome_orig, new.tipo, new.subtipo,
                new.texto_amostra, new.path);
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS arquivos_ad AFTER DELETE ON arquivos BEGIN
        INSERT INTO arquivos_fts(arquivos_fts, rowid, {_COLUNAS_FTS})
        VALUES ('delete', old.id, old.nome_atual, old.nome_orig, old.tipo,
                old.subtipo, old.texto_amostra, old.path);
    END
    """,
    f"""
    CREATE TRIGGER IF NOT EXISTS arquivos_au AFTER UPDATE ON arquivos BEGIN
        INSERT INTO arquivos_fts(arquivos_fts, rowid, {_COLUNAS_FTS})
        VALUES ('delete', old.id, old.nome_atual, old.nome_orig, old.tipo,
                old.subtipo, old.texto_amostra, old.path);
        INSERT INTO arquivos_fts(rowid, {_COLUNAS_FTS})
        VALUES (new.id, new.nome_atual, new.nome_orig, new.tipo, new.subtipo,
                new.texto_amostra, new.path);
    END
    """,
]

#: Lista ordenada de migrations. O índice + 1 é a versão do schema (RF-07).
MIGRATIONS: list[list[str]] = [_V1, _V2]

CHAVE_VERSAO = "schema_version"


# --------------------------------------------------------------------------- #
# Conexão
# --------------------------------------------------------------------------- #


def conectar(db_path: Path | str) -> sqlite3.Connection:
    """Abre a conexão já com os PRAGMAs obrigatórios aplicados (RF-06)."""
    caminho = Path(db_path)
    caminho.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(caminho),
        timeout=BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,
        # o watcher usa a mesma conexão na thread do observer, na do loop idle e na
        # do reaper; a serialização fica por conta do busy_timeout e do WAL
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def pragmas(conn: sqlite3.Connection) -> dict[str, Any]:
    """Lê de volta os PRAGMAs que RF-06 exige verificar."""
    return {
        "journal_mode": conn.execute("PRAGMA journal_mode").fetchone()[0],
        "busy_timeout": conn.execute("PRAGMA busy_timeout").fetchone()[0],
        "foreign_keys": conn.execute("PRAGMA foreign_keys").fetchone()[0],
        "synchronous": conn.execute("PRAGMA synchronous").fetchone()[0],
    }


def versao_schema(conn: sqlite3.Connection) -> int:
    """Versão atual gravada em `config_kv`; 0 quando o banco ainda está vazio."""
    try:
        linha = conn.execute(
            "SELECT valor FROM config_kv WHERE chave = ?", (CHAVE_VERSAO,)
        ).fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(linha["valor"]) if linha else 0


def migrar(conn: sqlite3.Connection) -> int:
    """Aplica as migrations pendentes em ordem. Idempotente (RF-07)."""
    atual = versao_schema(conn)
    for indice in range(atual, len(MIGRATIONS)):
        conn.execute("BEGIN")
        try:
            for comando in MIGRATIONS[indice]:
                conn.execute(comando)
            conn.execute(
                "INSERT INTO config_kv(chave, valor) VALUES(?, ?) "
                "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor",
                (CHAVE_VERSAO, str(indice + 1)),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return versao_schema(conn)


def abrir(db_path: Path | str) -> sqlite3.Connection:
    """Conecta e migra — o atalho usado por todos os entrypoints."""
    conn = conectar(db_path)
    migrar(conn)
    return conn


def tabelas(conn: sqlite3.Connection) -> list[str]:
    """Nomes de tudo em `sqlite_master` — base do teste de idempotência."""
    linhas = conn.execute(
        "SELECT type, name FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    return [f"{linha['type']}:{linha['name']}" for linha in linhas]


# --------------------------------------------------------------------------- #
# config_kv
# --------------------------------------------------------------------------- #


def kv_set(conn: sqlite3.Connection, chave: str, valor: str, ttl_segundos: float | None = None) -> None:
    """Grava um valor de controle, opcionalmente com validade."""
    expira = ts_mais(ttl_segundos / 60.0) if ttl_segundos else None
    conn.execute(
        "INSERT INTO config_kv(chave, valor, expira_em) VALUES(?, ?, ?) "
        "ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor, expira_em = excluded.expira_em",
        (chave, valor, expira),
    )


def kv_get(conn: sqlite3.Connection, chave: str, momento: datetime | None = None) -> str | None:
    """Lê um valor de controle; devolve `None` se não existir ou tiver expirado."""
    linha = conn.execute(
        "SELECT valor, expira_em FROM config_kv WHERE chave = ?", (chave,)
    ).fetchone()
    if linha is None:
        return None
    if linha["expira_em"] and linha["expira_em"] <= ts(momento):
        return None
    return linha["valor"]


# --------------------------------------------------------------------------- #
# arquivos
# --------------------------------------------------------------------------- #

_CAMPOS_ARQUIVO = (
    "nome_orig",
    "nome_atual",
    "path",
    "path_orig",
    "tipo",
    "subtipo",
    "ext",
    "tamanho",
    "sha256",
    "via",
    "confianca",
    "status",
    "motivo",
    "duplicado_de",
    "texto_amostra",
    "embedding",
    "embedding_model",
    "embedding_dim",
    "movido_em",
)


def inserir_arquivo(conn: sqlite3.Connection, **dados: Any) -> int:
    """UPSERT por `path` — reprocessar o mesmo caminho nunca duplica (RF-25)."""
    campos = [c for c in _CAMPOS_ARQUIVO if c in dados]
    faltando = {"nome_orig", "nome_atual", "path"} - set(campos)
    if faltando:
        raise ValueError(f"campos obrigatórios ausentes: {sorted(faltando)}")
    marcadores = ", ".join("?" for _ in campos)
    atualizacoes = ", ".join(f"{c} = excluded.{c}" for c in campos if c != "path")
    conn.execute(
        f"INSERT INTO arquivos ({', '.join(campos)}) VALUES ({marcadores}) "
        f"ON CONFLICT(path) DO UPDATE SET {atualizacoes}, indexado_em = datetime('now')",
        tuple(dados[c] for c in campos),
    )
    linha = conn.execute("SELECT id FROM arquivos WHERE path = ?", (dados["path"],)).fetchone()
    return int(linha["id"])


def buscar_por_path(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM arquivos WHERE path = ?", (str(path),)).fetchone()


def buscar_por_path_orig(conn: sqlite3.Connection, path_orig: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM arquivos WHERE path_orig = ? ORDER BY id DESC LIMIT 1", (str(path_orig),)
    ).fetchone()


def contar_arquivos(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM arquivos").fetchone()["n"])


def listar_arquivos(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM arquivos ORDER BY id").fetchall()


def remover_arquivo(conn: sqlite3.Connection, path: str) -> None:
    """Remove uma LINHA do índice (nunca um arquivo do disco)."""
    conn.execute("DELETE FROM arquivos WHERE path = ?", (str(path),))


# --------------------------------------------------------------------------- #
# arquivos_fts (Fase 4 — já mantido em sincronia por triggers desde a v2)
# --------------------------------------------------------------------------- #


def buscar_fts(conn: sqlite3.Connection, consulta: str, limite: int = 20) -> list[sqlite3.Row]:
    """Busca léxica BM25 sobre `arquivos_fts`."""
    return conn.execute(
        "SELECT a.*, bm25(arquivos_fts) AS score FROM arquivos_fts "
        "JOIN arquivos a ON a.id = arquivos_fts.rowid "
        "WHERE arquivos_fts MATCH ? ORDER BY score LIMIT ?",
        (consulta, limite),
    ).fetchall()


def linhas_com_embedding(
    conn: sqlite3.Connection, embedding_model: str, limite: int = 50000
) -> list[sqlite3.Row]:
    """Linhas cujo vetor veio do modelo informado — nunca mistura modelos (RF-69)."""
    return conn.execute(
        "SELECT * FROM arquivos WHERE embedding IS NOT NULL AND embedding_model = ? "
        "ORDER BY id LIMIT ?",
        (embedding_model, limite),
    ).fetchall()


def contar_com_embedding(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) AS n FROM arquivos WHERE embedding IS NOT NULL"
        ).fetchone()["n"]
    )


def contar_fts(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM arquivos_fts").fetchone()["n"])


# --------------------------------------------------------------------------- #
# pendentes
# --------------------------------------------------------------------------- #


def pendente_por_path(conn: sqlite3.Connection, path: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM pendentes WHERE path = ?", (str(path),)).fetchone()


def pendente_por_id(conn: sqlite3.Connection, id_: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM pendentes WHERE id = ?", (id_,)).fetchone()


def inserir_pendente(
    conn: sqlite3.Connection, path: str, retry_after: str, motivo: str, ultimo_erro: str | None = None
) -> int:
    """Insere ou atualiza o pendente daquele caminho, incrementando `tentativas`."""
    conn.execute(
        "INSERT INTO pendentes (path, retry_after, motivo, tentativas, ultimo_erro) "
        "VALUES (?, ?, ?, 1, ?) "
        "ON CONFLICT(path) DO UPDATE SET retry_after = excluded.retry_after, "
        "motivo = excluded.motivo, ultimo_erro = excluded.ultimo_erro, "
        "tentativas = pendentes.tentativas + 1",
        (str(path), retry_after, motivo, ultimo_erro),
    )
    linha = conn.execute("SELECT id FROM pendentes WHERE path = ?", (str(path),)).fetchone()
    return int(linha["id"])


def pendentes_vencidos(
    conn: sqlite3.Connection, limite: int = 50, momento: datetime | None = None
) -> list[sqlite3.Row]:
    """A única consulta que o loop idle executa (RF-38)."""
    return conn.execute(
        "SELECT * FROM pendentes WHERE retry_after <= ? ORDER BY retry_after LIMIT ?",
        (ts(momento), limite),
    ).fetchall()


def listar_pendentes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM pendentes ORDER BY id").fetchall()


def contar_pendentes(conn: sqlite3.Connection) -> int:
    return int(conn.execute("SELECT COUNT(*) AS n FROM pendentes").fetchone()["n"])


def adiar_pendente(conn: sqlite3.Connection, id_: int, retry_after: str, motivo: str | None = None) -> None:
    conn.execute(
        "UPDATE pendentes SET retry_after = ?, tentativas = tentativas + 1, "
        "motivo = COALESCE(?, motivo) WHERE id = ?",
        (retry_after, motivo, id_),
    )


def remover_pendente(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM pendentes WHERE path = ?", (str(path),))


def remover_pendente_id(conn: sqlite3.Connection, id_: int) -> None:
    conn.execute("DELETE FROM pendentes WHERE id = ?", (id_,))


# --------------------------------------------------------------------------- #
# em_processamento (lock lógico entre workers)
# --------------------------------------------------------------------------- #


def adquirir_lock(conn: sqlite3.Connection, path: str, pid: int) -> bool:
    """Tenta o lock lógico. `False` quando outro worker já o tem (RF-30)."""
    try:
        conn.execute(
            "INSERT INTO em_processamento (path, pid) VALUES (?, ?)", (str(path), pid)
        )
    except sqlite3.IntegrityError:
        return False
    return True


def liberar_lock(conn: sqlite3.Connection, path: str) -> None:
    conn.execute("DELETE FROM em_processamento WHERE path = ?", (str(path),))


def locks_ativos(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM em_processamento ORDER BY path").fetchall()


def limpar_locks_orfaos(
    conn: sqlite3.Connection, ttl_minutos: int = LOCK_TTL_MINUTOS, momento: datetime | None = None
) -> int:
    """Descarta locks mais velhos que o TTL — worker morto não trava o sistema (RF-46)."""
    corte = ts((momento or agora()) - timedelta(minutes=ttl_minutos))
    cursor = conn.execute("DELETE FROM em_processamento WHERE iniciado_em <= ?", (corte,))
    return cursor.rowcount or 0


# --------------------------------------------------------------------------- #
# operacoes / reservas (journal write-ahead — ARQUITETURA §12)
# --------------------------------------------------------------------------- #


def journal_planejar(
    conn: sqlite3.Connection,
    origem: str,
    destino: str,
    confianca: float | None = None,
    via: str | None = None,
    categoria: str | None = None,
    dry_run: bool = False,
    pid: int | None = None,
) -> int:
    """Registra a intenção ANTES de qualquer alteração no filesystem (RF-43)."""
    cursor = conn.execute(
        "INSERT INTO operacoes (origem, destino, estado, confianca, via, categoria, dry_run, pid) "
        "VALUES (?, ?, 'planejado', ?, ?, ?, ?, ?)",
        (str(origem), str(destino), confianca, via, categoria, 1 if dry_run else 0, pid),
    )
    return int(cursor.lastrowid)


def journal_destino(conn: sqlite3.Connection, op_id: int, destino: str) -> None:
    """Corrige o destino planejado quando a corrida por `O_EXCL` foi perdida."""
    conn.execute("UPDATE operacoes SET destino = ? WHERE id = ?", (str(destino), op_id))


def journal_estado(
    conn: sqlite3.Connection, op_id: int, estado: str, erro: str | None = None
) -> None:
    """Avança o estado da operação; `concluido`/`abortado`/`falhou` fecham o registro."""
    final = estado in ("concluido", "abortado", "falhou")
    conn.execute(
        "UPDATE operacoes SET estado = ?, erro = COALESCE(?, erro), "
        "finalizado_em = CASE WHEN ? THEN datetime('now') ELSE finalizado_em END WHERE id = ?",
        (estado, erro, 1 if final else 0, op_id),
    )


def journal_por_id(conn: sqlite3.Connection, op_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM operacoes WHERE id = ?", (op_id,)).fetchone()


def journal_por_estado(conn: sqlite3.Connection, estados: Sequence[str]) -> list[sqlite3.Row]:
    marcadores = ", ".join("?" for _ in estados)
    return conn.execute(
        f"SELECT * FROM operacoes WHERE estado IN ({marcadores}) ORDER BY id", tuple(estados)
    ).fetchall()


def operacoes_aguardando(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Planos de move parados esperando aprovação manual (RF-75, RF-79)."""
    return conn.execute(
        "SELECT * FROM operacoes WHERE estado = 'aguardando_aprovacao' ORDER BY id"
    ).fetchall()


def listar_operacoes(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM operacoes ORDER BY id").fetchall()


def reserva_criar(conn: sqlite3.Connection, op_id: int, path: str) -> None:
    conn.execute(
        "INSERT INTO reservas (op_id, path) VALUES (?, ?) "
        "ON CONFLICT(op_id) DO UPDATE SET path = excluded.path",
        (op_id, str(path)),
    )


def reserva_remover(conn: sqlite3.Connection, op_id: int) -> None:
    conn.execute("DELETE FROM reservas WHERE op_id = ?", (op_id,))


def reserva_por_op(conn: sqlite3.Connection, op_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM reservas WHERE op_id = ?", (op_id,)).fetchone()


def listar_reservas(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM reservas ORDER BY op_id").fetchall()


def paths_conhecidos(conn: sqlite3.Connection) -> set[str]:
    """Caminhos de origem já vistos — usado pela varredura de startup (RF-40)."""
    conhecidos: set[str] = set()
    for linha in conn.execute("SELECT path, path_orig FROM arquivos").fetchall():
        conhecidos.add(str(linha["path"]))
        if linha["path_orig"]:
            conhecidos.add(str(linha["path_orig"]))
    for linha in conn.execute("SELECT path FROM pendentes").fetchall():
        conhecidos.add(str(linha["path"]))
    for linha in conn.execute("SELECT path FROM em_processamento").fetchall():
        conhecidos.add(str(linha["path"]))
    return conhecidos


def executar_muitos(conn: sqlite3.Connection, comando: str, linhas: Iterable[Sequence[Any]]) -> None:
    """Escape hatch controlado para carga em massa nos testes de desempenho."""
    conn.executemany(comando, linhas)
