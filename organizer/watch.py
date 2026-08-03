"""O único processo permanente. ~5 MB residentes, 0% de CPU em idle.

Regra não-negociável (ARQUITETURA §1, RNF-03): este módulo só pode importar
`watchdog`, `sqlite3` (via `db`) e stdlib. Ele **nunca** importa `guard`,
`ingest`, `classify`, `extract`, `llm`, `embeddings`, `search` nem `rich` —
tudo que é caro vive dentro dos processos filhos efêmeros.

`move` é importado apenas por causa de `recover_incomplete()`, e `move` também
só depende de módulos leves (`db`, `paths`, `rules`, `log`, `queue`).

Requisitos cobertos: RF-09, RF-10, RF-12, RF-13, RF-28, RF-29, RF-30, RF-32,
RF-38, RF-40, RF-44, RF-46, RNF-03, RNF-09.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from organizer import config, db, log, move, paths, queue, rules
from organizer.queue import Motivo

#: Janela de debounce em memória: `created` + `modified` do mesmo arquivo viram
#: um único despacho (RF-30).
DEBOUNCE_SEGUNDOS = 2.0

#: Intervalo do reaper que colhe processos filhos terminados.
REAPER_SEGUNDOS = 1.0

#: Raiz do projeto — usada para dar `-m organizer.worker` no filho.
RAIZ_PROJETO = Path(__file__).resolve().parent.parent

_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

_logger = log.get_logger("watch")


# --------------------------------------------------------------------------- #
# Spawn do worker
# --------------------------------------------------------------------------- #


def spawn_worker(caminho: Path, motivo: Motivo | None = None):
    """`Popen` do processo filho. Não bloqueia — o reaper colhe depois (RF-28)."""
    argumentos = [sys.executable, "-m", "organizer.worker", str(caminho)]
    if motivo is not None:
        argumentos.append(f"--motivo={motivo.value}")
    ambiente = dict(os.environ)
    anterior = ambiente.get("PYTHONPATH", "")
    ambiente["PYTHONPATH"] = (
        f"{RAIZ_PROJETO}{os.pathsep}{anterior}" if anterior else str(RAIZ_PROJETO)
    )
    return subprocess.Popen(
        argumentos,
        cwd=str(RAIZ_PROJETO),
        env=ambiente,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_CREATE_NO_WINDOW,
    )


# --------------------------------------------------------------------------- #
# Watcher
# --------------------------------------------------------------------------- #


class Watcher:
    """Estado e comportamento do processo permanente."""

    def __init__(self, cfg, conn=None, spawn=spawn_worker, relogio=time.monotonic):
        self.cfg = cfg
        self.conn = conn if conn is not None else db.abrir(cfg.db_path)
        self.spawn = spawn
        self.relogio = relogio
        self._debounce: dict[str, float] = {}
        self._processos: list[tuple[str, object]] = []
        #: vagas já prometidas a um spawn que ainda não retornou — o que torna
        #: "checar e reservar" uma operação única sob o mesmo lock
        self._reservados = 0
        self._lock = threading.Lock()
        self._parar = threading.Event()
        self._observer = None
        self._threads: list[threading.Thread] = []

    # -- filtros -------------------------------------------------------- #

    def motivo_de_descarte(self, caminho: Path) -> str | None:
        """Camada 1 da ARQUITETURA §7: descarte sem custo, sem tocar no banco."""
        nome = caminho.name
        if rules.nome_e_parcial(nome):
            return Motivo.PARCIAL.value
        for raiz in (self.cfg.target_root, self.cfg.db_path.parent, self.cfg.log_dir):
            if paths.is_subpath(caminho, raiz):
                return Motivo.DENTRO_DO_TARGET.value
        if not self.cfg.watch_recursive and caminho.parent != Path(self.cfg.downloads_dir):
            return Motivo.DENTRO_DO_TARGET.value
        if caminho.is_dir():
            return Motivo.DIRETORIO.value
        return None

    def _debounced(self, caminho: Path) -> bool:
        """Janela de 2 s por caminho. O mapa é podado a cada consulta.

        Sem a poda, este seria o único estado ilimitado de um processo que deve
        ficar em ~4 MB por semanas (RNF-12): uma entrada por arquivo já visto,
        para sempre.
        """
        agora = self.relogio()
        chave = os.path.normcase(str(caminho))
        with self._lock:
            corte = agora - DEBOUNCE_SEGUNDOS
            self._debounce = {k: t for k, t in self._debounce.items() if t >= corte}
            anterior = self._debounce.get(chave)
            if anterior is not None:
                return True
            self._debounce[chave] = agora
        return False

    # -- despacho ------------------------------------------------------- #

    def vagas_livres(self) -> int:
        with self._lock:
            return self.cfg.max_workers - len(self._processos) - self._reservados

    def _reservar_vaga(self) -> bool:
        """Checa e reserva numa só seção crítica (senão dois eventos simultâneos
        passam pela checagem antes de qualquer um contabilizar o seu processo)."""
        with self._lock:
            if len(self._processos) + self._reservados >= self.cfg.max_workers:
                return False
            self._reservados += 1
            return True

    def _confirmar_vaga(self, caminho: str, processo) -> None:
        with self._lock:
            self._reservados = max(0, self._reservados - 1)
            if processo is not None and hasattr(processo, "poll"):
                self._processos.append((caminho, processo))

    def despachar(self, caminho: Path | str, motivo: Motivo | None = None) -> bool:
        """Ponto único de entrada. Devolve `True` se um worker foi disparado."""
        alvo = Path(caminho)
        descarte = self.motivo_de_descarte(alvo)
        if descarte:
            _logger.debug("descartado %s motivo=%s", alvo, descarte)
            return False
        if self._debounced(alvo):
            _logger.debug("debounce descartou evento repetido de %s", alvo)
            return False
        if not self._reservar_vaga():
            queue.enfileirar(self.conn, alvo, Motivo.LIMITE_WORKERS, self.cfg)
            _logger.info("limite de workers atingido, adiando %s", alvo)
            return False
        if not db.adquirir_lock(self.conn, str(alvo), os.getpid()):
            self._confirmar_vaga(str(alvo), None)
            _logger.debug("outro worker já processa %s", alvo)
            return False
        try:
            processo = self.spawn(alvo, motivo)
        except Exception as exc:
            self._confirmar_vaga(str(alvo), None)
            db.liberar_lock(self.conn, str(alvo))
            queue.enfileirar(self.conn, alvo, Motivo.SPAWN_FALHOU, self.cfg, erro=str(exc))
            _logger.error("falha ao dar spawn para %s: %s", alvo, exc)
            return False
        self._confirmar_vaga(str(alvo), processo)
        return True

    def colher(self) -> int:
        """Reaper: libera slots de filhos que já morreram."""
        colhidos = 0
        with self._lock:
            vivos = []
            for caminho, processo in self._processos:
                if processo.poll() is None:
                    vivos.append((caminho, processo))
                else:
                    colhidos += 1
            self._processos = vivos
        return colhidos

    # -- rotinas periódicas --------------------------------------------- #

    def varredura_de_startup(self) -> int:
        """Arquivos que passaram batido enquanto o agente estava desligado (RF-40).

        O guard não roda aqui (ele vive no filho, DIVERGÊNCIA 1): o worker é
        disparado com `--motivo=startup` e, se encontrar o sistema ocupado, usa
        `RETRY_STARTUP_MINUTES` em vez de `RETRY_BUSY_MINUTES`.
        """
        pasta = Path(self.cfg.downloads_dir)
        if not pasta.is_dir():
            return 0
        conhecidos = {os.path.normcase(p) for p in db.paths_conhecidos(self.conn)}
        despachados = 0
        for item in sorted(pasta.iterdir()):
            if os.path.normcase(str(item)) in conhecidos:
                continue
            try:
                if self.despachar(item, Motivo.STARTUP):
                    despachados += 1
            except Exception as exc:
                # um arquivo problemático não pode abortar a varredura inteira
                _logger.error("varredura de startup falhou em %s: %s", item, exc)
        _logger.info("varredura de startup despachou %s arquivo(s)", despachados)
        return despachados

    def passada_idle(self, momento: datetime | None = None) -> int:
        """Uma passada do loop idle: limpa locks órfãos e pega os vencidos (RF-38)."""
        queue.limpar_locks_orfaos(self.conn, momento=momento)
        despachados = 0
        for linha in queue.vencidos(self.conn, momento=momento):
            if self.vagas_livres() <= 0:
                break
            if self.despachar(linha["path"]):
                despachados += 1
        return despachados

    # -- ciclo de vida --------------------------------------------------- #

    def preparar(self) -> None:
        """Recuperação e limpeza que precedem qualquer evento (RF-44)."""
        rules.criar_arvore(self.cfg.target_root, self.cfg.inbox_dirname)
        contadores = move.recover_incomplete(self.conn, self.cfg)
        queue.limpar_locks_orfaos(self.conn)
        _logger.info("recuperação de startup: %s", contadores)

    def iniciar(self) -> None:
        """Sobe o observer e as threads auxiliares. Não bloqueia.

        A recuperação e a varredura são melhores esforços: se qualquer uma
        falhar, o watcher **ainda assim** sobe e passa a atender eventos novos.
        Um `OSError` num volume de rede caído não pode deixar o usuário sem
        agente (ARQUITETURA §18).
        """
        for etapa, funcao in (("recuperação", self.preparar), ("varredura", self.varredura_de_startup)):
            try:
                funcao()
            except Exception as exc:
                _logger.error("%s de startup falhou, seguindo mesmo assim: %s", etapa, exc)

        self._observer = Observer()
        self._observer.schedule(
            ManipuladorDeEventos(self),
            str(self.cfg.downloads_dir),
            recursive=self.cfg.watch_recursive,
        )
        self._observer.start()

        self._threads = [
            threading.Thread(target=self._laco_idle, name="foa-idle", daemon=True),
            threading.Thread(target=self._laco_reaper, name="foa-reaper", daemon=True),
        ]
        for thread in self._threads:
            thread.start()
        _logger.info("watcher ativo em %s", self.cfg.downloads_dir)

    def _laco_idle(self) -> None:
        while not self._parar.wait(self.cfg.idle_loop_seconds):
            try:
                self.passada_idle()
            except Exception as exc:  # pragma: no cover - defesa do processo permanente
                _logger.error("erro no loop idle: %s", exc)

    def _laco_reaper(self) -> None:
        while not self._parar.wait(REAPER_SEGUNDOS):
            try:
                self.colher()
            except Exception as exc:  # pragma: no cover - defesa do processo permanente
                _logger.error("erro no reaper: %s", exc)

    def parar(self) -> None:
        """Encerramento limpo (Ctrl-C): para o observer e as threads."""
        self._parar.set()
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        for thread in self._threads:
            thread.join(timeout=5)
        self._threads = []
        _logger.info("watcher encerrado")

    def aguardar(self) -> None:
        """Bloqueia até alguém chamar `parar()`."""
        while not self._parar.is_set():
            self._parar.wait(1.0)


# --------------------------------------------------------------------------- #
# Handler do watchdog
# --------------------------------------------------------------------------- #


class ManipuladorDeEventos(FileSystemEventHandler):
    """Traduz eventos do watchdog em despachos. Nenhuma exceção sobe daqui (RNF-09)."""

    def __init__(self, watcher: Watcher):
        self.watcher = watcher

    def _tratar(self, caminho) -> None:
        try:
            self.watcher.despachar(Path(caminho))
        except Exception as exc:
            _logger.error("erro tratando evento de %s: %s", caminho, exc)

    def on_created(self, evento) -> None:
        if not evento.is_directory:
            self._tratar(evento.src_path)

    def on_modified(self, evento) -> None:
        if not evento.is_directory:
            self._tratar(evento.src_path)

    def on_moved(self, evento) -> None:
        # o Chrome renomeia `x.crdownload` para `x.pdf` no fim do download:
        # o que interessa é sempre o `dest_path` (RF-13)
        if not evento.is_directory:
            self._tratar(evento.dest_path)


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    cfg = config.get_config()
    log.configurar(cfg.log_dir, console=True)
    watcher = Watcher(cfg)

    def _encerrar(_sinal, _quadro):
        watcher.parar()

    for nome in ("SIGINT", "SIGTERM"):
        sinal = getattr(signal, nome, None)
        if sinal is not None:
            try:
                signal.signal(sinal, _encerrar)
            except ValueError:  # pragma: no cover - fora da thread principal
                pass

    try:
        watcher.iniciar()
        watcher.aguardar()
    except KeyboardInterrupt:  # pragma: no cover - Ctrl-C fora do handler
        pass
    except Exception as exc:  # pragma: no cover - falha fatal ao subir
        _logger.exception("watcher não conseguiu iniciar: %s", exc)
        return 1
    finally:
        # a conexão fecha aconteça o que acontecer, inclusive se `iniciar` levantar
        try:
            watcher.parar()
        finally:
            watcher.conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover - entrypoint
    sys.exit(main())
