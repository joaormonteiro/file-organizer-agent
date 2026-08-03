"""RF-09, RF-10, RF-12, RF-13, RF-28 a RF-32, RF-40, RNF-03, RNF-09."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from watchdog.events import FileCreatedEvent, FileModifiedEvent, FileMovedEvent

import factories
from organizer import db, rules, watch
from organizer.queue import Motivo

RAIZ_PROJETO = Path(__file__).resolve().parent.parent


class SpawnFalso:
    """Registra os despachos em vez de criar processos."""

    def __init__(self):
        self.chamadas: list[tuple[str, str | None]] = []

    def __call__(self, caminho, motivo=None):
        self.chamadas.append((str(caminho), motivo.value if motivo else None))
        return None

    @property
    def paths(self) -> list[str]:
        return [c[0] for c in self.chamadas]


@pytest.fixture
def spawn():
    return SpawnFalso()


@pytest.fixture
def watcher(sandbox, conn, spawn):
    relogio = {"t": 1000.0}
    w = watch.Watcher(sandbox.cfg, conn=conn, spawn=spawn, relogio=lambda: relogio["t"])
    w.relogio_estado = relogio
    return w


# --------------------------------------------------------------------------- #
# RNF-03 — o watcher não importa dependências pesadas
# --------------------------------------------------------------------------- #

PROIBIDOS = [
    "psutil",
    "pynvml",
    "pdfplumber",
    "docx",
    "numpy",
    "torch",
    "model2vec",
    "rich",
    "organizer.guard",
    "organizer.ingest",
    "organizer.classify",
    "organizer.extract",
    "organizer.llm",
    "organizer.embeddings",
    "organizer.search",
]


def test_watcher_nao_importa_pesado():
    """RNF-03: verificado num subprocess limpo, olhando `sys.modules` de verdade."""
    script = (
        "import json, sys; import organizer.watch; "
        f"print(json.dumps([m for m in {PROIBIDOS!r} if m in sys.modules]))"
    )
    resultado = subprocess.run(
        [sys.executable, "-c", script],
        cwd=str(RAIZ_PROJETO),
        capture_output=True,
        text=True,
    )
    assert resultado.returncode == 0, resultado.stderr
    encontrados = json.loads(resultado.stdout.strip().splitlines()[-1])
    assert encontrados == [], f"o watcher arrastou dependências pesadas: {encontrados}"


def test_watcher_importa_o_essencial():
    script = (
        "import sys; import organizer.watch; "
        "assert 'watchdog.observers' in sys.modules; "
        "assert 'sqlite3' in sys.modules; "
        "assert 'organizer.queue' in sys.modules"
    )
    resultado = subprocess.run(
        [sys.executable, "-c", script], cwd=str(RAIZ_PROJETO), capture_output=True, text=True
    )
    assert resultado.returncode == 0, resultado.stderr


# --------------------------------------------------------------------------- #
# Filtros e despacho
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("ext", sorted(rules.EXTENSOES_PARCIAIS))
def test_extensoes_parciais_ignoradas(watcher, sandbox, conn, spawn, ext):
    """RF-12: nem `pendentes` nem `em_processamento` recebem linha."""
    alvo = factories.criar(sandbox.downloads, f"filme.mp4{ext}")
    assert watcher.despachar(alvo) is False
    assert spawn.chamadas == []
    assert db.contar_pendentes(conn) == 0
    assert db.locks_ativos(conn) == []


@pytest.mark.parametrize("nome", ["~$doc.docx", ".oculto.pdf", "backup.txt~"])
def test_nomes_de_controle_ignorados(watcher, sandbox, spawn, nome):
    alvo = factories.criar(sandbox.downloads, nome)
    assert watcher.despachar(alvo) is False
    assert spawn.chamadas == []


def test_nao_recursivo_por_padrao(watcher, sandbox, spawn):
    """RF-10: arquivo em subpasta do Downloads não dispara worker."""
    subpasta = sandbox.downloads / "Cursos"
    alvo = factories.criar(subpasta, "aula.pdf")
    assert watcher.despachar(alvo) is False
    assert spawn.chamadas == []
    assert watcher.cfg.watch_recursive is False


def test_observer_e_agendado_apenas_em_downloads(watcher, sandbox, monkeypatch):
    """RF-10 e RF-32: só `DOWNLOADS_DIR` é observado; o `_Inbox` nunca."""
    agendamentos: list[tuple[str, bool]] = []

    class ObserverFalso:
        def schedule(self, handler, caminho, recursive=False):
            agendamentos.append((caminho, recursive))

        def start(self):
            pass

        def stop(self):
            pass

        def join(self, timeout=None):
            pass

    monkeypatch.setattr(watch, "Observer", ObserverFalso)
    watcher.iniciar()
    try:
        assert agendamentos == [(str(sandbox.downloads), False)]
    finally:
        watcher.parar()


def test_inbox_nao_e_observado(watcher, sandbox, spawn):
    """RF-32: mesmo forçando um evento vindo do `_Inbox`, nada é despachado."""
    rules.criar_arvore(sandbox.target, sandbox.cfg.inbox_dirname)
    alvo = factories.criar(sandbox.inbox, "relatorio.pdf")
    assert watcher.despachar(alvo) is False
    assert spawn.chamadas == []


def test_diretorio_nao_e_despachado(watcher, sandbox, spawn):
    pasta = sandbox.downloads / "Cursos"
    pasta.mkdir()
    assert watcher.despachar(pasta) is False
    assert spawn.chamadas == []


def test_spawn_de_worker(watcher, sandbox, conn, spawn):
    """RF-28: o despacho cria um worker e registra o lock lógico."""
    alvo = factories.criar(sandbox.downloads, "setup.exe")
    assert watcher.despachar(alvo) is True
    assert spawn.paths == [str(alvo)]
    assert [linha["path"] for linha in db.locks_ativos(conn)] == [str(alvo)]


def test_evento_duplicado_gera_um_worker(watcher, sandbox, conn, spawn):
    """RF-30: `created` + `modified` na janela de debounce viram um só despacho."""
    alvo = factories.criar(sandbox.downloads, "setup.exe")
    manipulador = watch.ManipuladorDeEventos(watcher)
    manipulador.on_created(FileCreatedEvent(str(alvo)))
    manipulador.on_modified(FileModifiedEvent(str(alvo)))
    assert spawn.paths == [str(alvo)]

    # passada a janela de debounce, o lock lógico ainda barra o segundo worker
    watcher.relogio_estado["t"] += watch.DEBOUNCE_SEGUNDOS + 1
    manipulador.on_modified(FileModifiedEvent(str(alvo)))
    assert spawn.paths == [str(alvo)]
    assert len(db.locks_ativos(conn)) == 1


def test_on_moved_usa_dest_path(watcher, sandbox, spawn):
    """RF-13: o Chrome renomeia `.crdownload` para `.pdf` — vale o destino."""
    parcial = sandbox.downloads / "relatorio.pdf.crdownload"
    final = factories.criar(sandbox.downloads, "relatorio-2026.pdf")
    manipulador = watch.ManipuladorDeEventos(watcher)
    manipulador.on_moved(FileMovedEvent(str(parcial), str(final)))
    assert spawn.paths == [str(final)]


def test_limite_de_workers(watcher, sandbox, conn, spawn):
    """RF-29: acima de `MAX_WORKERS` o excedente vai para `pendentes` com retry curto."""

    class ProcessoFalso:
        def poll(self):
            return None

    watcher.spawn = lambda caminho, motivo=None: ProcessoFalso()
    agora = db.agora()
    alvos = [factories.criar(sandbox.downloads, f"setup{i}.exe") for i in range(4)]
    resultados = [watcher.despachar(alvo) for alvo in alvos]

    assert resultados == [True, True, False, False]
    assert watcher.vagas_livres() == 0
    for alvo in alvos[2:]:
        linha = db.pendente_por_path(conn, str(alvo))
        assert linha["motivo"] == Motivo.LIMITE_WORKERS.value
        assert linha["retry_after"] <= db.ts_mais(queue_retry_curto() + 1, agora)


def queue_retry_curto() -> int:
    from organizer import queue

    return queue.RETRY_CURTO_MINUTOS


def test_reaper_libera_slots(watcher, sandbox):
    class ProcessoFalso:
        def __init__(self):
            self.morto = False

        def poll(self):
            return 0 if self.morto else None

    processos: list[ProcessoFalso] = []

    def spawn(caminho, motivo=None):
        p = ProcessoFalso()
        processos.append(p)
        return p

    watcher.spawn = spawn
    for i in range(2):
        assert watcher.despachar(factories.criar(sandbox.downloads, f"setup{i}.exe")) is True
    assert watcher.vagas_livres() == 0

    for p in processos:
        p.morto = True
    assert watcher.colher() == 2
    assert watcher.vagas_livres() == 2


def test_falha_de_spawn_vira_pendente(watcher, sandbox, conn):
    def spawn_quebrado(caminho, motivo=None):
        raise OSError("sem recursos para criar processo")

    watcher.spawn = spawn_quebrado
    alvo = factories.criar(sandbox.downloads, "setup.exe")
    assert watcher.despachar(alvo) is False
    assert db.pendente_por_path(conn, str(alvo))["motivo"] == Motivo.SPAWN_FALHOU.value
    assert db.locks_ativos(conn) == []


def test_erro_em_evento_nao_mata_observer(watcher, sandbox, spawn, monkeypatch):
    """RNF-09: exceção num evento é logada e o próximo evento ainda é atendido."""
    manipulador = watch.ManipuladorDeEventos(watcher)
    primeiro = factories.criar(sandbox.downloads, "explode.exe")
    segundo = factories.criar(sandbox.downloads, "ok.exe")

    original = watcher.despachar
    chamou = {"n": 0}

    def instavel(caminho, motivo=None):
        chamou["n"] += 1
        if chamou["n"] == 1:
            raise RuntimeError("falha injetada no handler")
        return original(caminho, motivo)

    monkeypatch.setattr(watcher, "despachar", instavel)
    manipulador.on_created(FileCreatedEvent(str(primeiro)))  # não pode levantar
    manipulador.on_created(FileCreatedEvent(str(segundo)))
    assert spawn.paths == [str(segundo)]


# --------------------------------------------------------------------------- #
# Varredura de startup e loop idle
# --------------------------------------------------------------------------- #


def test_varredura_de_startup(watcher, sandbox, conn, spawn):
    """RF-40: só o que passou batido é despachado, com `--motivo=startup`."""
    novo = factories.criar(sandbox.downloads, "novo.exe")
    ja_indexado = factories.criar(sandbox.downloads, "indexado.exe")
    ja_pendente = factories.criar(sandbox.downloads, "pendente.exe")
    parcial = factories.criar(sandbox.downloads, "meio.crdownload")

    db.inserir_arquivo(
        conn,
        nome_orig="indexado.exe",
        nome_atual="indexado.exe",
        path=str(sandbox.target / "Softwares" / "Instaladores" / "indexado.exe"),
        path_orig=str(ja_indexado),
    )
    db.inserir_pendente(conn, str(ja_pendente), db.ts(), Motivo.OCUPADO.value)

    assert watcher.varredura_de_startup() == 1
    assert spawn.chamadas == [(str(novo), Motivo.STARTUP.value)]
    assert str(parcial) not in spawn.paths


def test_varredura_sem_pasta(watcher, sandbox):
    import shutil as _shutil

    _shutil.rmtree(sandbox.downloads)
    assert watcher.varredura_de_startup() == 0


def test_passada_idle_pega_vencidos(watcher, sandbox, conn, spawn):
    """RF-38: o loop idle só faz o SELECT dos vencidos e despacha."""
    agora = db.agora()
    vencido = factories.criar(sandbox.downloads, "vencido.exe")
    futuro = factories.criar(sandbox.downloads, "futuro.exe")
    db.inserir_pendente(conn, str(vencido), db.ts_mais(-5, agora), Motivo.OCUPADO.value)
    db.inserir_pendente(conn, str(futuro), db.ts_mais(120, agora), Motivo.OCUPADO.value)

    assert watcher.passada_idle(momento=agora) == 1
    assert spawn.paths == [str(vencido)]


def test_passada_idle_limpa_locks_orfaos(watcher, conn):
    """RF-46: locks órfãos são limpos a cada passada do loop idle."""
    agora = db.agora()
    db.adquirir_lock(conn, "X:/dl/velho.exe", 1)
    conn.execute(
        "UPDATE em_processamento SET iniciado_em = ?", (db.ts_mais(-60, agora),)
    )
    watcher.passada_idle(momento=agora)
    assert db.locks_ativos(conn) == []


def test_intervalo_do_loop_idle(sandbox):
    """RF-38: o padrão é 600 s (10 min)."""
    assert sandbox.cfg.idle_loop_seconds == 600


# --------------------------------------------------------------------------- #
# Ciclo de vida
# --------------------------------------------------------------------------- #


def test_ciclo_de_vida(sandbox, conn, spawn):
    """RF-09: sobe o observer de verdade e encerra limpo."""
    watcher = watch.Watcher(sandbox.cfg, conn=conn, spawn=spawn)
    watcher.iniciar()
    try:
        assert watcher._observer is not None
        assert watcher._observer.is_alive()
        assert all(t.is_alive() for t in watcher._threads)
    finally:
        watcher.parar()
    assert watcher._observer is None
    assert watcher._threads == []


def test_evento_real_do_watchdog_dispara_worker(sandbox, conn, spawn):
    """RF-09 e RF-28: o caminho real do watchdog até o despacho."""
    watcher = watch.Watcher(sandbox.cfg, conn=conn, spawn=spawn)
    watcher.iniciar()
    try:
        factories.criar(sandbox.downloads, "novo-instalador.exe")
        limite = time.monotonic() + 15
        while not spawn.chamadas and time.monotonic() < limite:
            time.sleep(0.05)
    finally:
        watcher.parar()
    assert spawn.paths, "o watchdog não entregou o evento"
    assert spawn.paths[0].endswith("novo-instalador.exe")


def test_worker_real_em_subprocesso(sandbox, conn):
    """RF-28 ponta a ponta: `python -m organizer.worker` move e indexa de verdade."""
    alvo = factories.criar(sandbox.downloads, "setup-real.exe")
    processo = watch.spawn_worker(Path(alvo))
    assert processo.wait(timeout=120) == 0
    assert not alvo.exists()
    destino = sandbox.caminho(rules.CAT_INSTALADORES) / "setup-real.exe"
    assert destino.is_file()
    assert db.buscar_por_path(conn, str(destino)) is not None


def test_spawn_worker_monta_o_comando_certo(sandbox, monkeypatch):
    """RF-28: `Popen([sys.executable, '-m', 'organizer.worker', path])`, sem bloquear."""
    capturado: dict = {}

    class PopenFalso:
        def __init__(self, args, **kwargs):
            capturado["args"] = args
            capturado["kwargs"] = kwargs

        def poll(self):
            return None

    monkeypatch.setattr(watch.subprocess, "Popen", PopenFalso)
    watch.spawn_worker(Path("X:/dl/a.pdf"), Motivo.STARTUP)

    assert capturado["args"][:3] == [sys.executable, "-m", "organizer.worker"]
    assert capturado["args"][3] == str(Path("X:/dl/a.pdf"))
    assert capturado["args"][4] == "--motivo=startup"
    assert os.name != "nt" or capturado["kwargs"]["creationflags"] == watch._CREATE_NO_WINDOW


def test_preparar_roda_recuperacao(watcher, sandbox, conn):
    """RF-44: a recuperação acontece antes de qualquer evento."""
    origem = factories.criar(sandbox.downloads, "orfao.exe")
    destino = sandbox.caminho(rules.CAT_INSTALADORES) / "orfao.exe"
    db.journal_planejar(conn, str(origem), str(destino))

    watcher.preparar()

    assert db.pendente_por_path(conn, str(origem)) is not None
    assert db.journal_por_estado(conn, ("planejado",)) == []


# --------------------------------------------------------------------------- #
# worker.py — o entrypoint do processo filho
# --------------------------------------------------------------------------- #


def test_worker_parse_argv():
    from organizer import worker

    assert worker.parse_argv(["X:/a.pdf"]) == ("X:/a.pdf", None)
    assert worker.parse_argv(["X:/a.pdf", "--motivo=startup"]) == ("X:/a.pdf", Motivo.STARTUP)
    assert worker.parse_argv(["X:/a.pdf", "--motivo=inventado"]) == ("X:/a.pdf", None)
    assert worker.parse_argv([]) == (None, None)


def test_worker_sem_argumento_falha(capsys):
    from organizer import ingest, worker

    assert worker.main([]) == ingest.EXIT_ERRO
    assert "uso:" in capsys.readouterr().err


def test_worker_main_processa_e_libera_o_lock(sandbox, conn):
    """RF-28: o worker processa um arquivo e sempre solta o lock no `finally`."""
    from organizer import ingest, worker

    alvo = factories.criar(sandbox.downloads, "setup.exe")
    db.adquirir_lock(conn, str(alvo), 1)

    assert worker.main([str(alvo)]) == ingest.EXIT_OK

    assert (sandbox.caminho(rules.CAT_INSTALADORES) / "setup.exe").is_file()
    assert db.locks_ativos(conn) == []


# --------------------------------------------------------------------------- #
# D1, D4, D7 — robustez do processo permanente
# --------------------------------------------------------------------------- #


def test_startup_com_falha_ainda_sobe_o_observer(sandbox, conn, spawn, monkeypatch):
    """D1: `OSError` na recuperação ou na varredura não pode matar o watcher."""

    def explode(*args, **kwargs):
        raise OSError("volume de rede indisponivel")

    monkeypatch.setattr(watch.move, "recover_incomplete", explode)
    monkeypatch.setattr(watch.Watcher, "varredura_de_startup", explode)

    watcher_local = watch.Watcher(sandbox.cfg, conn=conn, spawn=spawn)
    watcher_local.iniciar()
    try:
        assert watcher_local._observer is not None
        assert watcher_local._observer.is_alive()
    finally:
        watcher_local.parar()


def test_main_fecha_a_conexao_mesmo_se_iniciar_falhar(sandbox, monkeypatch):
    """D1: `main()` não vaza a conexão SQLite quando o startup explode."""
    fechadas: list[bool] = []

    def iniciar_quebrado(self):
        raise OSError("observer não pôde subir")

    class ConexaoEspia:
        """`sqlite3.Connection` não aceita monkeypatch de `close` (read-only)."""

        def __init__(self, real):
            self._real = real

        def __getattr__(self, nome):
            return getattr(self._real, nome)

        def close(self):
            fechadas.append(True)
            return self._real.close()

    espia = ConexaoEspia(db.abrir(sandbox.db_path))
    monkeypatch.setattr(watch.db, "abrir", lambda caminho: espia)
    monkeypatch.setattr(watch.Watcher, "iniciar", iniciar_quebrado)

    assert watch.main([]) == 1
    assert fechadas == [True], "a conexão SQLite ficou aberta"


def test_varredura_ignora_arquivo_problematico(watcher, sandbox, conn, spawn, monkeypatch):
    """D1: um arquivo que explode não aborta a varredura dos demais."""
    factories.criar(sandbox.downloads, "a-quebra.exe")
    bom = factories.criar(sandbox.downloads, "b-ok.exe")
    original = watcher.despachar

    def instavel(caminho, motivo=None):
        if "a-quebra" in str(caminho):
            raise OSError("stat falhou")
        return original(caminho, motivo)

    monkeypatch.setattr(watcher, "despachar", instavel)
    assert watcher.varredura_de_startup() == 1
    assert spawn.paths == [str(bom)]


def test_recuperacao_isola_falha_por_operacao(sandbox, conn, monkeypatch):
    """D1: uma operação podre não impede as outras de serem recuperadas."""
    from organizer import move

    boa = factories.criar(sandbox.downloads, "boa.exe")
    ruim = factories.criar(sandbox.downloads, "ruim.exe")
    pasta = sandbox.caminho(rules.CAT_INSTALADORES)
    pasta.mkdir(parents=True, exist_ok=True)
    op_ruim = db.journal_planejar(conn, str(ruim), str(pasta / "ruim.exe"))
    db.journal_planejar(conn, str(boa), str(pasta / "boa.exe"))

    original = move._recuperar_planejada

    def instavel(conexao, cfg, operacao, contadores):
        if int(operacao["id"]) == op_ruim:
            raise OSError("unidade removida")
        return original(conexao, cfg, operacao, contadores)

    monkeypatch.setattr(move, "_recuperar_planejada", instavel)
    contadores = move.recover_incomplete(conn, sandbox.cfg)

    assert contadores["falhas"] == 1
    assert contadores["reenfileirados"] == 1
    assert db.journal_por_id(conn, op_ruim)["estado"] == move.ESTADO_FALHOU
    assert db.pendente_por_path(conn, str(boa)) is not None


def test_debounce_nao_cresce_indefinidamente(watcher, sandbox, spawn):
    """D4: entradas mais velhas que a janela são podadas — o RSS não escala com o uso."""
    for i in range(50):
        watcher.despachar(factories.criar(sandbox.downloads, f"arq{i}.exe"))
        watcher._processos.clear()
        watcher._reservados = 0
    assert len(watcher._debounce) == 50

    watcher.relogio_estado["t"] += watch.DEBOUNCE_SEGUNDOS + 1
    watcher.despachar(factories.criar(sandbox.downloads, "novo.exe"))
    assert len(watcher._debounce) == 1, "o mapa de debounce não foi podado"


def test_reserva_de_vaga_e_atomica(watcher, sandbox, conn):
    """D7: checar e reservar acontecem sob um único lock."""
    assert watcher.vagas_livres() == watcher.cfg.max_workers
    assert watcher._reservar_vaga() is True
    assert watcher._reservar_vaga() is True
    assert watcher.vagas_livres() == 0
    assert watcher._reservar_vaga() is False, "estourou MAX_WORKERS"

    watcher._confirmar_vaga("X:/a.exe", None)
    assert watcher.vagas_livres() == 1


def test_lock_ja_tomado_devolve_a_vaga(watcher, sandbox, conn, spawn):
    """D7: perder a corrida pelo lock lógico não pode vazar a vaga reservada."""
    alvo = factories.criar(sandbox.downloads, "setup.exe")
    db.adquirir_lock(conn, str(alvo), 999)

    assert watcher.despachar(alvo) is False
    assert watcher.vagas_livres() == watcher.cfg.max_workers
    assert spawn.chamadas == []


def test_falha_de_spawn_devolve_a_vaga(watcher, sandbox, conn):
    """D7: erro no spawn também devolve a vaga."""

    def quebrado(caminho, motivo=None):
        raise OSError("sem recursos")

    watcher.spawn = quebrado
    watcher.despachar(factories.criar(sandbox.downloads, "setup.exe"))
    assert watcher.vagas_livres() == watcher.cfg.max_workers
