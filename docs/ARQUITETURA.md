# File Organizer Agent — Arquitetura

> Documento de decisões técnicas. Fonte de requisitos: `C:\projects\vault\projetos\file-organizer-agent.md`.
> Este documento **complementa e, em pontos marcados com ⚠ DIVERGÊNCIA, substitui** a spec do vault.
> Escrito em 2026-08-02 para consumo do AGENTE DEV (implementação) e do AGENTE REVISOR (auditoria).
> O checklist de aceite está em `docs/REQUISITOS.md`.

---

## 1. Princípio-mestre

**Zero RAM em idle.** Exatamente um processo fica vivo permanentemente: o `watcher`.
Ele só pode importar `watchdog`, `sqlite3` e stdlib. Tudo que é caro (psutil, pynvml,
pdfplumber, python-docx, ollama, numpy, embeddings) vive **apenas** dentro de processos
filhos efêmeros que nascem, agem e morrem.

Consequência arquitetural direta e não-negociável:

```
organizer/watch.py   --imports-->  config, log, db, queue, spawn        (leve)
organizer/worker.py  --imports-->  config, log, db, guard, ingest, ...  (pesado)
```

`watch.py` **nunca** importa `guard`, `ingest`, `classify`, `extract`, `llm`,
`embeddings` ou `search`. Isso é verificável por teste automatizado
(inspeção de `sys.modules` após `import organizer.watch`) — ver RNF-03.

⚠ **DIVERGÊNCIA 1 (posição do Resource Guard).** A spec sugere o guard antes de
despachar. Aqui o guard roda **dentro do filho**, no topo do `ingest`. Motivo:
`psutil.cpu_percent(interval=1)` bloqueia 1 s e `psutil`/`pynvml` custam ~2 MB
residentes — rodá-los no watcher violaria o princípio-mestre e travaria a thread
de eventos. O filho custa ~50 ms de startup do Python para descobrir que deve adiar,
o que é irrelevante. O loop de 10 min no watcher faz apenas um `SELECT` em SQLite.

---

## 2. Visão geral dos processos

```
+----------------------------------------------------------------------+
| PROCESSO 1 - watcher.py             (sempre vivo, ~5 MB, 0% CPU)     |
|                                                                      |
|  +- ReadDirectoryChangesW via watchdog (nao recursivo)               |
|  |     evento created/modified/moved em DOWNLOADS_DIR                |
|  |        v debounce 2 s + filtro de extensao parcial                |
|  |        v INSERT em em_processamento (UNIQUE path)                 |
|  |        v spawn                                                    |
|  +- Thread do loop idle (a cada IDLE_LOOP_SECONDS = 600)             |
|  |     SELECT * FROM pendentes WHERE retry_after <= now              |
|  |        v spawn (respeitando MAX_WORKERS)                          |
|  +- Thread reaper: Popen.poll() -> libera slot, limpa em_processamento|
|  +- Startup: recover_incomplete() + varredura de DOWNLOADS_DIR       |
+----------------------------------------------------------------------+
                 | subprocess.Popen([sys.executable, "-m",
                 |                   "organizer.worker", "<path>"])
                 v
+----------------------------------------------------------------------+
| PROCESSO 2..N - organizer.worker  (efemero, 1 arquivo, depois morre) |
|                                                                      |
|  1. lock logico (em_processamento) ja garantido pelo pai             |
|  2. stability.aguardar_estabilidade(path)  -> nao pronto? -> pendentes|
|  3. guard.sistema_ocupado()                -> ocupado?    -> pendentes|
|  4. classify.classificar(path)                                       |
|       +- rules.por_extensao()      -> confianca 0.30 ... 0.95        |
|       +- se confianca < CONFIDENCE_MIN e e doc de texto:             |
|             extract.trecho(path,500) -> llm.classificar() -> fusao   |
|  5. move.executar(decisao)  [journal WAL em SQLite -> os.replace]    |
|  6. db.indexar() + FTS5 (+ embedding, se backend disponivel)         |
|  7. DELETE FROM pendentes/em_processamento; exit(0)                  |
+----------------------------------------------------------------------+

+----------------------------------------------------------------------+
| PROCESSO SOB DEMANDA - query.py / inbox.py  (manual, morre no fim)   |
+----------------------------------------------------------------------+
```

---

## 3. Estrutura de módulos

Critério: um módulo por responsabilidade testável isoladamente; nenhum módulo
com mais de ~200 linhas úteis; nada de `utils.py` genérico. 18 módulos.

```
file-organizer-agent/
├── README.md
├── .env.example                  # TODAS as chaves, com defaults e comentários
├── .gitignore                    # venv/, *.db, *.log, .env, sandbox/
├── requirements.txt              # núcleo — Fases 1,2,3,5
├── requirements-semantic.txt     # opcional — Fase 4 (embeddings)
├── requirements-dev.txt          # pytest, pytest-cov
│
├── watcher.py                    # entrypoint fino: organizer.watch.main()
├── query.py                      # entrypoint fino: organizer.search.main()
├── inbox.py                      # entrypoint fino: dashboard do _Inbox (Fase 5)
│
├── organizer/
│   ├── __init__.py               # apenas __version__; SEM imports pesados
│   ├── config.py                 # (A)
│   ├── log.py                    # (B)
│   ├── db.py                     # (C)
│   ├── paths.py                  # (D)
│   ├── naming.py                 # (E)
│   ├── rules.py                  # (F)
│   ├── stability.py              # (G)
│   ├── guard.py                  # (H)
│   ├── queue.py                  # (I)
│   ├── extract.py                # (J)
│   ├── llm.py                    # (K)
│   ├── classify.py               # (L)
│   ├── move.py                   # (M)
│   ├── ingest.py                 # (N)
│   ├── worker.py                 # (O)
│   ├── watch.py                  # (P)
│   ├── embeddings.py             # (Q)
│   ├── search.py                 # (R)
│   └── notify.py                 # (S)
│
├── docs/
│   ├── ARQUITETURA.md            # este arquivo
│   └── REQUISITOS.md             # checklist de aceite
│
└── tests/
    ├── conftest.py               # sandbox + interlocks de segurança
    ├── factories.py              # geradores de arquivos sintéticos
    ├── test_config.py  test_paths.py    test_naming.py  test_rules.py
    ├── test_stability.py test_guard.py  test_queue.py   test_llm.py
    ├── test_classify.py test_move.py    test_ingest.py  test_watch.py
    └── test_search.py  test_recovery.py test_isolation.py
```

### Responsabilidade de cada módulo

**(A) `config.py`** — Carrega `.env` (parser próprio de ~30 linhas, sem dependência
de `python-dotenv`) sobreposto por variáveis de ambiente reais (env vence `.env`).
Expõe um `@dataclass(frozen=True) Config` e `get_config()` com cache.
Faz **validação fail-fast** em duas partes, porque as raízes não são todas do mesmo
tipo (corrigido em 2026-08-02 — a formulação anterior era autocontraditória):
- `paths.assert_disjoint(downloads_dir, target_root)` — a raiz vigiada e a raiz de
  destino têm de ser mundos separados (é o que impede o loop infinito);
- `is_subpath(alvo, downloads_dir)` recusado para `db_path`, `log_dir`, `inbox_dir` e
  `target_root` — nenhum estado do agente pode morar dentro da pasta vigiada.

`DB_PATH` e `LOG_DIR` ficam **dentro** de `TARGET_ROOT` por design (`<TARGET_ROOT>/.foa/`,
seção 4), então incluí-los num `assert_disjoint` único faria a validação falhar sempre,
com qualquer configuração válida.
Nenhum caminho é hardcoded em nenhum outro módulo — regra auditável por grep.
Importa: `os`, `pathlib`, `dataclasses`, `organizer.paths`.

**(B) `log.py`** — `get_logger(nome)`. `RotatingFileHandler` (5 MB × 3) em
`LOG_DIR/organizer.log`, formato
`%(asctime)s %(levelname)s %(process)d %(name)s %(message)s`.
Log estruturado em uma linha por decisão (`decision=` `path=` `dest=` `conf=` `via=` `motivo=`).
`rich` só é importado pelos entrypoints interativos (`query.py`, `inbox.py`), nunca pelo watcher.

**(C) `db.py`** — Única porta de acesso ao SQLite. `connect()` aplica
`PRAGMA journal_mode=WAL`, `busy_timeout=10000`, `foreign_keys=ON`,
`synchronous=NORMAL`. Contém a lista `MIGRATIONS` (SQL aplicado em ordem, versão em
`config_kv['schema_version']`). DAOs: `inserir_arquivo`, `buscar_por_path`,
`upsert_fts`, `journal_*`, `reserva_*`. **Nenhum SQL fora deste módulo.**

**(D) `paths.py`** — Puro, quase sem I/O. Funções: `sanitize_stem(s)`,
`ensure_max_path(dest)`, `resolver_colisao(dest)`, `is_subpath(a, b)`,
`assert_disjoint(*paths)`, `long_path(p)` (prefixo `\?\`), `sha256_arquivo(p)`.
É o único lugar que conhece as regras de filesystem do Windows.

**(E) `naming.py`** — `is_generic(nome) -> (bool, motivo)` e `slugify(texto)`.
Heurística inteiramente determinística (seção 8).

**(F) `rules.py`** — Tabela declarativa `EXTENSOES: dict[str, RegraExt]` mapeando
extensão → (tipo, subtipo, subpasta relativa, confiança base) + `KEYWORDS` por
categoria + `CATEGORIAS` (enum canônico das pastas-alvo, também usado no prompt do
LLM e na validação da resposta). Dados, não lógica.

**(G) `stability.py`** — `esta_pronto(path) -> (bool, motivo)` e
`aguardar_estabilidade(path)`. Combina probe de handle exclusivo (ctypes/CreateFileW)
com estabilidade de tamanho/mtime (seção 7).

**(H) `guard.py`** — `snapshot() -> Recursos(cpu, ram, gpu, vram, fonte_gpu)` e
`sistema_ocupado() -> (bool, Recursos)`. `pynvml` com init/shutdown explícitos e
degradação para 0.0 quando NVML indisponível (seção 6).

**(I) `queue.py`** — Fila `pendentes`: `enfileirar(path, motivo, delay)`,
`vencidos(limite)`, `adiar(id, delay)`, `concluir(path)`, `descartar(id, erro)`.
Encapsula backoff e teto de tentativas.

**(J) `extract.py`** — `trecho(path, max_chars=500) -> Extracao(texto, ok, motivo)`.
`.pdf` via `pdfplumber` (só as 2 primeiras páginas; PDF com senha → `ok=False,
motivo='protegido'`), `.docx` via `python-docx`, `.txt/.md/.csv` via leitura direta
com cascata de encoding (utf-8 → utf-8-sig → cp1252 → latin-1). Nunca executa nada.

**(K) `llm.py`** — `disponivel() -> bool` (cache TTL em `config_kv`),
`classificar(contexto) -> RespostaLLM | None`. Encapsula subprocess, prompt,
parsing tolerante, timeout e retry (seção 9).

**(L) `classify.py`** — `classificar(path) -> Decisao(categoria, nome_final,
confianca, via, motivo)`. Aplica `rules` → decide se escala para `llm` → funde
confianças → decide `_Inbox` vs destino final. Contém as constantes de confiança
(seção 10). Não faz I/O de filesystem além de `stat`.

**(M) `move.py`** — `executar(decisao) -> ResultadoMove`. Journal write-ahead,
reserva `O_EXCL`, `os.replace`, colisão e `recover_incomplete()` (seções 11 e 12).
É o **único** módulo autorizado a alterar o filesystem do usuário.

**(N) `ingest.py`** — Pipeline de um arquivo: estabilidade → guard → classify →
move → índice → limpeza da fila. Converte qualquer exceção em estado persistido
(`pendentes` com erro, ou `_Inbox`); nunca deixa vazar.

**(O) `worker.py`** — `python -m organizer.worker <path> [--motivo=...]`. Parse de
argv, chama `ingest.processar`, define exit code (0 ok, 2 adiado, 3 erro), garante
liberação de `em_processamento` num `finally`.

**(P) `watch.py`** — `main()`: recover → varredura de startup → `Observer` do
watchdog → thread do loop idle → thread reaper → laço de sinal. Contém o debounce
e o controle de `MAX_WORKERS`.

**(Q) `embeddings.py`** — `get_backend() -> EmbeddingBackend | None`. Protocolo com
`nome`, `dim`, `encode(list[str]) -> np.ndarray`. Implementações: `Model2VecBackend`
(padrão do extra), `SentenceTransformerBackend` (opt-in), `None` quando nada instalado.
Importa numpy/model2vec **dentro** das classes, nunca no topo do módulo.

**(R) `search.py`** — `buscar(pergunta, k)` com fusão FTS5 + embeddings (seção 13)
e rerank opcional via `llm`. `main()` é a CLI com `rich`.

**(S) `notify.py`** — `notificar(titulo, msg)` via `plyer`, no-op silencioso se
`plyer` falhar (comum em sessão de serviço). Fase 5.

---

## 4. Configuração (`.env`)

```ini
# --- raizes (TUDO relocavel: e isto que torna o sandbox de teste possivel) ---
DOWNLOADS_DIR=C:\Users\joaor\Downloads
TARGET_ROOT=C:\Users\joaor\Organizado
INBOX_DIRNAME=_Inbox                   # resolvido como TARGET_ROOT\_Inbox
DB_PATH=C:\Users\joaor\Organizado\.foa\index.db
LOG_DIR=C:\Users\joaor\Organizado\.foa\logs

# --- comportamento ---
MODE=auto                 # auto | interactive
DRY_RUN=0                 # 1 = planeja e loga, nao toca em disco do usuario
CONFIDENCE_MIN=0.75
WATCH_RECURSIVE=0
MAX_WORKERS=2
ALLOW_CROSS_VOLUME=0

# --- resource guard ---
THRESHOLD_CPU=70
THRESHOLD_RAM=80
THRESHOLD_GPU=60
THRESHOLD_VRAM=70
GPU_INDEX=0
RETRY_BUSY_MINUTES=120
RETRY_STARTUP_MINUTES=30
IDLE_LOOP_SECONDS=600
MAX_TENTATIVAS=8

# --- estabilidade ---
STABILITY_POLLS=3
STABILITY_INTERVAL=1.0
STABILITY_TIMEOUT=300

# --- LLM ---
LLM_ENABLED=1
OLLAMA_BIN=ollama         # sobrescrito nos testes por um fake script
OLLAMA_MODEL=phi3:mini
LLM_TIMEOUT=90
LLM_SAMPLES=1

# --- busca ---
EMBEDDING_BACKEND=auto    # auto | none | model2vec | sentence-transformers
EMBEDDING_MODEL=minishlab/potion-multilingual-128M
SEARCH_RERANK_LLM=0
```

⚠ **DIVERGÊNCIA 2 (árvore de pastas).** A spec espalha as pastas-alvo pela raiz do
perfil (`C:\Users\joaor\Documentos`, `...\Imagens`, `...\Musica`). Verificado no
ambiente real: essas pastas **não existem** — o perfil tem `Documents`, `Pictures`,
`Music`, `Videos` (nomes em inglês no disco, localizados só na UI). Seguir a spec
literalmente criaria pastas duplicadas e confusas ao lado das reais.

Decisão: **uma única raiz `TARGET_ROOT`** (padrão `C:\Users\joaor\Organizado`) contendo
toda a subárvore da spec, inclusive `Imagens/`, `Videos/`, `Musica/`, `Softwares/` e
`_Inbox/`. Ganhos: (a) uma variável relocaliza tudo para o sandbox de teste;
(b) `assert_disjoint` fica trivial; (c) não polui pastas de sistema.
Quem quiser o layout literal da spec define `TARGET_ROOT=C:\Users\joaor` — a subárvore
relativa é idêntica. A subárvore canônica vive em `rules.CATEGORIAS`.

Árvore criada sob `TARGET_ROOT` (criada sob demanda, nunca no import):

```
<TARGET_ROOT>/
├── Documentos/Academico/UNIFESP/{Matrizes-Curriculares,Comprovantes,Trabalhos,Horarios}
├── Documentos/Academico/Certificados
├── Documentos/Profissional/{Eyeconnect,EfficienceCo,Contratos}
├── Documentos/Financeiro/{Extratos,Notas-Fiscais}
├── Documentos/Pessoal/{Documentos-RG-CPF,Outros}
├── Softwares/{Instaladores,Portateis}
├── Imagens/{Screenshots,Fotos}
├── Videos/
├── Musica/
├── _Inbox/                 <- quarentena (confianca baixa)
├── _Inbox/_Duplicados/     <- colisao com conteudo identico
├── _Inbox/_Aguardando/     <- modo interativo, aguardando aprovacao
└── .foa/{index.db,logs/}   <- estado do agente
```

---

## 5. Segurança de dados (regras invioláveis)

1. **Nunca deletar.** Nenhum módulo além de `move.py` pode chamar `os.remove`,
   `os.unlink`, `shutil.rmtree` ou `Path.unlink`. Em `move.py` o `unlink` é permitido
   em exatamente **dois** casos, ambos logados em nível `INFO`:
   - remoção de um arquivo de reserva de **0 byte** criado por este próprio processo
     e registrado na tabela `reservas`;
   - a remoção da origem num move **entre volumes**, e somente **após** verificar
     `sha256(destino) == sha256(origem)`. Isso só ocorre com `ALLOW_CROSS_VOLUME=1`;
     por padrão o move entre volumes é recusado e o arquivo vai para `pendentes`.
   Verificável por grep — ver RNF-06.
2. **Nunca sobrescrever.** O destino é sempre reservado com `open(dst,'xb')`
   (`O_CREAT|O_EXCL`). `os.replace` só é usado sobre a própria reserva.
3. **Nunca executar** o arquivo classificado. `.exe/.msi/.bat/.ps1/.cmd` são
   classificados só por nome, extensão e tamanho.
4. **Downloads real é intocável em desenvolvimento e teste** (seção 14).
5. `DRY_RUN=1` desliga toda escrita no disco do usuário (o banco continua sendo
   escrito, com `operacoes.dry_run=1`).

---

## 6. Resource Guard — NVML (⚠ DIVERGÊNCIA 3)

A spec oferece "`GPUtil` ou `pynvml`". **Decisão: `nvidia-ml-py==13.610.43`**, a wheel
oficial da NVIDIA, que é quem publica o módulo importável `pynvml`. O código continua
escrevendo `import pynvml`.

Justificativa objetiva (verificada no PyPI em 2026-08-02):
- `GPUtil` está morto: última release `1.4.0` de **2018-12-18**, publicada **só como
  sdist** (nenhuma wheel — em Python 3.14 exigiria build de um `setup.py` legado).
- `GPUtil` funciona fazendo `subprocess` do `nvidia-smi` e **parseando texto**: custa
  ~150 ms e um processo por chamada, e quebra com mudança de locale/formato.
- NVML fala a API C diretamente — sem subprocess, ~2 ms por leitura.

⚠ **Correção de 2026-08-02 (auditoria da Fase 2).** A escolha original era o pacote
`pynvml==13.0.1` do PyPI. Ele é apenas um *shim* de compatibilidade: declara
`Requires-Dist: nvidia-ml-py>=12.0.0`, não contém implementação e emite um
`FutureWarning` no import pedindo para instalar `nvidia-ml-py`. Quem realmente publica
o arquivo `pynvml.py` é a wheel da NVIDIA. Depender do shim significava arrastar um
pacote a mais e poluir a saída de qualquer processo do agente com um aviso de
depreciação. Verificado nesta máquina, com a wheel oficial e sem o shim:
`nvmlDeviceGetName` devolve `NVIDIA GeForce RTX 4060 Laptop GPU`, e o import fica limpo
mesmo com `-W error::FutureWarning`.

**Semântica preservada 1:1 com o snippet da spec:**

| Spec (GPUtil)            | Implementação (NVML)                                            |
|--------------------------|-----------------------------------------------------------------|
| `gpus[0].load * 100`     | `nvmlDeviceGetUtilizationRates(h).gpu` (já em %)                 |
| `gpus[0].memoryUtil*100` | `mem.used / mem.total * 100` de `nvmlDeviceGetMemoryInfo(h)`     |
| `if gpus else 0`         | qualquer falha NVML → `gpu=0.0, vram=0.0, fonte='indisponivel'`  |

Thresholds e o operador (`or` entre as quatro métricas) idênticos à spec.
`nvmlInit()`/`nvmlShutdown()` em `try/finally`; toda a família `NVMLError` é capturada
(driver ausente, GPU em modo exclusivo, Optimus dormindo). Device configurável por
`GPU_INDEX` (default 0), equivalente ao `gpus[0]` da spec.

---

## 7. "Arquivo ainda sendo gravado" — estratégia para Windows

Três camadas, todas obrigatórias, nesta ordem:

**Camada 1 — filtro estático (custo zero, roda no watcher).**
Descartar imediatamente, sem sequer enfileirar:
- extensões: `.crdownload .part .partial .download .opdownload .tmp .temp .!ut .aria2 .filepart .bc! .dctmp`
- nomes começando com `~$` (locks do Office) ou `.`, ou terminando em `~`
- **diretórios** (Fases 1–5 não movem pastas — não-objetivo explícito; o Downloads real
  tem 26 subpastas do usuário que devem permanecer intactas)
- qualquer caminho dentro de `TARGET_ROOT` ou de `.foa/`

**Camada 2 — probe de handle exclusivo (autoritativo).**
`ctypes.windll.kernel32.CreateFileW(long_path(p), GENERIC_READ, dwShareMode=0,
None, OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None)`:
- handle válido → **ninguém** tem o arquivo aberto → `CloseHandle` imediato → pronto;
- `INVALID_HANDLE_VALUE` com `GetLastError()==32` (ERROR_SHARING_VIOLATION) ou `33`
  (ERROR_LOCK_VIOLATION) → ainda em uso;
- erro `2`/`3` (não existe) → arquivo sumiu, aborta o processamento silenciosamente;
- outro erro → inconclusivo, decide pela Camada 3.
Fora do Windows, ou se `ctypes` falhar, retorna `None` (inconclusivo).

Este probe é o que a spec chama de "esperar o handle fechar" — é a única checagem que
realmente responde a essa pergunta no Windows. Abrir com `open(p,'rb')` **não** serve:
o Chrome mantém o `.crdownload` aberto com `FILE_SHARE_READ`, então a leitura teria êxito
com o download ainda em andamento.

**Camada 3 — estabilidade de tamanho + mtime.**
`STABILITY_POLLS=3` leituras de `os.stat` separadas por `STABILITY_INTERVAL=1.0 s`,
exigindo `st_size` e `st_mtime_ns` idênticos nas 3. Necessária porque antivírus e o
Explorer abrem/fecham o arquivo em rajadas (fazendo a Camada 2 oscilar), e porque um
download pode ficar momentaneamente sem handle entre buffers.

**Veredito:** pronto ⇔ (Camada 2 = pronto **ou** inconclusiva) **e** Camada 3 estável.
Se `STABILITY_TIMEOUT=300 s` estourar, o arquivo vai para `pendentes`
(`motivo='instavel'`, retry em 10 min) e o worker morre — nada fica preso.

---

## 8. Heurística de "nome genérico"

`naming.is_generic(nome) -> (bool, motivo)`. Determinística, sem LLM, testável.

**Normalização.** Remove extensão; NFKD + fold para ASCII; lowercase; troca `_`, `.`,
`+`, `%20` por espaço; colapsa espaços; remove o sufixo de cópia `\s*\(\d{1,3}\)$`
guardando `teve_sufixo_copia`.

**Regras (genérico se QUALQUER uma disparar):**

| ID | Regra | Exemplos que casam |
|----|-------|--------------------|
| G1 | stem pertence a `STOPWORDS` (lista literal) | `document`, `documento`, `download`, `arquivo`, `untitled`, `sem titulo`, `novo documento`, `file`, `scan`, `image`, `foto`, `copia`, `final`, `teste`, `output`, `print`, `captura de tela` |
| G2 | stem casa `^(img\|image\|imagem\|foto\|photo\|dsc\|dscn\|pxl\|screenshot\|captura de tela\|scan\|scanned\|doc\|documento\|download\|file\|arquivo\|untitled\|new\|novo)[ -]?\d*$` | `img 20260214`, `screenshot 3`, `doc1` |
| G3 | stem é só dígitos com ≥6 chars, ou hex com ≥16 chars, ou UUID | `20260214093311`, `a3f9c1de77b04e12` |
| G4 | `len(stem) <= 3` | `a`, `zz1` |
| G5 | menos de 2 tokens alfabéticos de ≥3 letras | `x1 2 3`, `-- 7 --` |

**Regra negativa explícita (anti-falso-positivo).** `teve_sufixo_copia` **sozinho
NUNCA torna um nome genérico.** Evidência colhida no Downloads real do usuário:
`holerite_contabil_horizonte_ltda_ana_clara_rodrigues_..._2026-07 (1).pdf` e
`log-events-viewer-result (3).csv` são descritivos apesar do `(n)`. Os exemplos da spec
(`download (3).zip`, `documento (2).docx`) continuam sendo detectados porque o stem já
cai em G1. O sufixo `(n)` é removido do nome final quando não há colisão, mas não
influencia a decisão de "genérico".

**Uso.** Nome genérico é condição **necessária mas não suficiente** para acordar o LLM:
só documentos de texto (`.pdf .docx .doc .txt .md .rtf .odt`) com nome genérico **ou**
com confiança de extensão < `CONFIDENCE_MIN` vão ao LLM. Um `.zip` genérico não vai ao
LLM (não há texto a extrair) — vai para `_Inbox`.

---

## 9. Contrato do Ollama

**Invocação.**
```python
subprocess.run(
  [OLLAMA_BIN, "run", OLLAMA_MODEL, "--format", "json"],
  input=prompt, text=True, encoding="utf-8", errors="replace",
  capture_output=True, timeout=LLM_TIMEOUT,
  creationflags=CREATE_NO_WINDOW)
```
Prompt sempre por **stdin**, nunca por argv: elimina limite de linha de comando e
problemas de quoting no Windows. Se a versão instalada rejeitar `--format json`
(stderr com `unknown flag`), reexecuta sem a flag e usa o parser tolerante — detectado
uma vez e memorizado em `config_kv`.

**Disponibilidade (degradação graciosa — obrigatória).**
`llm.disponivel()` = `shutil.which(OLLAMA_BIN) is not None` **e** `ollama list`
(timeout 10 s) contendo `OLLAMA_MODEL`. Cacheado em `config_kv` com TTL de 1 h.
Se indisponível: um `WARNING` com a instrução `ollama pull phi3:mini`, e **todo** o
caminho LLM é pulado — ambíguos vão para `_Inbox` com `motivo='llm_indisponivel'`.
Fases 1, 2, 4 e 5 continuam funcionando integralmente.

**Entrada do prompt** (PT-BR, ~600 tokens no pior caso):
- lista fechada das categorias válidas (gerada de `rules.CATEGORIAS`, uma por linha);
- `nome_original`, `extensao`, `tamanho_bytes`, `data_download`;
- `trecho`: até 500 chars extraídos, com caracteres de controle removidos e espaços
  colapsados (se `extract` falhou: `"(sem texto extraido)"`);
- instrução final: *"Responda SOMENTE com um objeto JSON, sem markdown, sem explicação,
  exatamente com as chaves: categoria, nome_sugerido, confianca, motivo."*

**Saída esperada (contrato):**
```json
{"categoria":"Documentos/Academico/UNIFESP/Matrizes-Curriculares",
 "nome_sugerido":"matriz-curricular-engenharia-computacao-2026",
 "confianca":0.88,
 "motivo":"texto cita grade curricular e UNIFESP"}
```

**Parsing tolerante, em cascata:**
1. `json.loads(stdout)`;
2. remove cercas de markdown e tenta de novo;
3. extrai o primeiro objeto `{...}` balanceado por varredura de chaves e tenta;
4. **1 retry** com prompt encurtado (trecho de 200 chars) + `"Sua ultima resposta foi
   invalida. Responda SOMENTE o JSON."`;
5. falhou → retorna `None`, `motivo='llm_parse_error'` → `_Inbox`.

**Validação semântica (rejeita, não conserta):**
- `categoria` deve ser **exatamente** um item de `rules.CATEGORIAS` (comparação após
  normalizar separadores). Fora do enum → resposta descartada por inteiro.
- `nome_sugerido` passa por `paths.sanitize_stem`; sobrando <3 chars → descartado.
- A **extensão nunca vem do LLM** — é sempre a do arquivo original.
- `confianca` não numérica ou fora de `[0,1]` → tratada como `0.5`.

**Timeout e retry.** `LLM_TIMEOUT=90 s` (cobre o cold start de ~3 s do phi3:mini com
folga em máquina ocupada). Em `TimeoutExpired`: `Popen.kill()` + `taskkill /T /F /PID`
como reforço, `tentativas += 1`, `pendentes` com retry em 30 min. Após
`MAX_TENTATIVAS_LLM=2`, vai para `_Inbox` com `motivo='llm_timeout'`. Teto de tempo de
parede por arquivo: 3 × `LLM_TIMEOUT`.

---

## 10. Origem do `confidence` (a spec usa o número, não o define)

Escala única `[0.0, 1.0]` comparada contra `CONFIDENCE_MIN=0.75`. Constantes nomeadas
em `classify.py` — nada de números mágicos espalhados.

### 10.1 Classificador por extensão

| Confiança base | Situação | Exemplos |
|---|---|---|
| **0.95** | extensão de família única e subtipo único | `.exe .msi` → `Softwares/Instaladores`; `.mp3 .flac .wav` → `Musica`; `.mp4 .mkv .avi` → `Videos` |
| **0.85** | extensão de família única, subtipo é o *default* da família | `.jpg .png .heic` → `Imagens/Fotos` |
| **0.60** | extensão conhecida, destino intrinsecamente ambíguo | `.pdf .docx .xlsx .csv .pptx .zip .rar .7z` |
| **0.30** | extensão desconhecida ou ausente | `.v38`, `.dlc`, sem extensão |

**Ajustes** (aplicados sobre a base; resultado clampado em `[0.0, 0.95]`):
- `+0.20` se ≥1 keyword da categoria aparece no nome do arquivo
  (`screenshot|captura|print` → `Imagens/Screenshots`; `nota-fiscal|nfe|nfse` →
  `Financeiro/Notas-Fiscais`; `extrato|fatura` → `Financeiro/Extratos`;
  `matriz|curricular|grade` → `UNIFESP/Matrizes-Curriculares`; `contrato` →
  `Profissional/Contratos`; `certificado|diploma` → `Academico/Certificados`; etc.);
- `+0.05` extra se a keyword vier com um ano plausível de 4 dígitos (2000–2099);
- `−0.15` se keywords de **duas famílias diferentes** casarem (sinal contraditório);
- `−0.10` se `naming.is_generic()` for verdadeiro (o nome não confirma nada).

Efeito prático desejado e testável:
`.exe` → 0.95 ≥ 0.75 → move direto.
`.pdf` sem keyword → 0.60 − 0.10 = 0.50 < 0.75 → escala para o LLM.
`.pdf` chamado `nota-fiscal-2026-05.pdf` → 0.60 + 0.20 + 0.05 = 0.85 → move sem LLM.
É isto que materializa a "regra dos 90%" da spec.

### 10.2 Classificador LLM

A confiança auto-reportada por um modelo de 3.8 B é notoriamente mal calibrada; usá-la
crua tornaria o `CONFIDENCE_MIN` decorativo. Portanto:

```
conf_final = clamp(0.5 * conf_llm + 0.5 * evidencia, 0.0, 0.90)
```

`evidencia` ∈ [0,1] é calculada **por nós**, somando sinais verificáveis:

| Peso | Sinal |
|------|-------|
| 0.35 | `categoria` está no enum canônico (se falso, a resposta já foi descartada antes) |
| 0.25 | houve texto real extraído (≥200 chars); PDF protegido/vazio → 0 |
| 0.20 | ≥1 keyword da categoria escolhida aparece no trecho **ou** no nome original |
| 0.20 | `nome_sugerido` sobrevive à sanitização com 3..80 chars |

Teto **0.90** (< 0.95): uma decisão por extensão inequívoca sempre vence uma decisão
por LLM em conflito, e o LLM nunca atinge certeza máxima.
Opcional `LLM_SAMPLES=2` (default 1): duas amostras concordando na categoria dão
`+0.10`; discordando, `−0.20`.

### 10.3 Uso do resultado

- `conf ≥ CONFIDENCE_MIN` e `MODE=auto` → move para a categoria decidida.
- `conf < CONFIDENCE_MIN` → move para `_Inbox/` (nome preservado), registro com
  `status='inbox'` e `motivo` legível. **Não fica em Downloads**: a spec define `_Inbox`
  como a rede de segurança, e deixá-lo em Downloads causaria reprocessamento eterno.
- `MODE=interactive` → move para `_Inbox/_Aguardando/`, dispara notificação e aguarda
  aprovação via `inbox.py` (seção 15).

---

## 11. Colisão de nomes no destino

Ordem de avaliação em `move.resolver_destino`:

1. **Destino livre** → reserva `open(dst,'xb')` e move.
2. **Destino existe e é idêntico** (mesmo `st_size` **e** mesmo `sha256`; para arquivos
   > 256 MB, `st_size` + sha256 dos primeiros e últimos 8 MB) → é duplicata: o arquivo
   de entrada vai para `_Inbox/_Duplicados/<nome>-dupN.<ext>`, registro com
   `status='duplicado'` e `duplicado_de=<path do original>`. **Nada é deletado** (regra
   da spec); o usuário decide depois.
3. **Destino existe e difere** → sufixo incremental `-2`, `-3`, … antes da extensão
   (`contrato-estagio-2.pdf`), tentando `open(...,'xb')` a cada candidato, até 999.
   O `O_EXCL` é o que torna a resolução **livre de TOCTOU** entre workers concorrentes:
   quem perde a corrida recebe `FileExistsError` e tenta o próximo candidato.
4. **999 candidatos esgotados** → `_Inbox` com `motivo='colisao_irresolvivel'`.

Sufixo `-N` (e não ` (N)`) de propósito: ` (N)` é exatamente o padrão que a heurística
de nome genérico remove; reintroduzi-lo criaria interpretação errada em reprocessamentos.

**Volumes.** `os.replace` só é atômico dentro do mesmo volume. Se
`origem.drive != destino.drive` e `ALLOW_CROSS_VOLUME=0` (padrão), o arquivo vai para
`pendentes` com `motivo='cross_volume'` e um `ERROR` explicando a config. Com a flag
ligada: copia → verifica sha256 → só então remove a origem.

---

## 12. Idempotência e sobrevivência a crash

### Journal write-ahead (tabela `operacoes`)

```
1. BEGIN; INSERT operacoes(estado='planejado', origem, destino); COMMIT;
2. reserva: open(destino,'xb') -> INSERT reservas(op_id, path); COMMIT;
3. os.replace(origem, destino)                    <- ponto atomico
4. UPDATE operacoes SET estado='movido'; DELETE FROM reservas WHERE op_id=?; COMMIT;
5. UPSERT arquivos (path UNIQUE) + upsert FTS + embedding
6. UPDATE operacoes SET estado='concluido'; DELETE FROM pendentes/em_processamento;
```

### `recover_incomplete()` — roda no startup do watcher, antes de qualquer evento

| Estado no journal | Realidade no disco | Ação |
|---|---|---|
| `planejado` | origem existe, destino não | nada aconteceu → reenfileira |
| `planejado` | origem existe, destino existe com **0 byte e presente em `reservas`** | é a nossa reserva → refaz `os.replace` e segue do passo 4 |
| `planejado` | origem não existe, destino existe | o `replace` completou antes do commit → avança para `movido` |
| `planejado` | nenhum dos dois existe | usuário mexeu → `abortado` + log |
| `movido` | — | reexecuta a indexação (UPSERT é idempotente) |
| `concluido` | — | nada |

Registros de `em_processamento` com `iniciado_em` mais velho que `LOCK_TTL=15 min` são
órfãos (worker morto) e são removidos no startup e a cada loop idle.

### Chaves de idempotência
- `arquivos.path` **UNIQUE** — reindexar o mesmo path é UPSERT, não duplica.
- `pendentes.path` **UNIQUE** — o par `created`+`modified` do watchdog não cria 2 linhas.
- `em_processamento.path` **UNIQUE** — impede dois workers para o mesmo arquivo; o
  `INSERT` é feito pelo **pai** antes do `Popen`, e o `IntegrityError` é o próprio
  mecanismo de dedupe.
- Debounce em memória no watcher: mapa `{path: ts}` com janela de 2 s, descartando
  eventos repetidos antes mesmo de tocar no banco.

Resultado: matar o agente (Ctrl-C, kill, queda de energia) em qualquer ponto deixa o
sistema num estado recuperável, sem arquivo perdido e sem duplicata no índice.

---

## 13. Fase 4 — busca: FTS5 por padrão, embeddings opcional (⚠ DIVERGÊNCIA 4)

**Custo real de `sentence-transformers`** (verificado no PyPI em 2026-08-02):
`sentence-transformers 5.6.1` arrasta `torch>=1.11` (wheel Windows de **122 MB**,
~500 MB instalados), `transformers`, `scikit-learn`, `scipy`, `huggingface-hub`.
`import torch` custa 2–5 s. Isso contradiz frontalmente dois pontos da própria spec:
"22 MB modelo" e "carrega em <1 s … morre". Pior: `all-MiniLM-L6-v2` é treinado **só em
inglês**, enquanto todas as perguntas de exemplo da spec são em português
("onde está o arquivo da matriz curricular da minha faculdade").

**Decisão — três camadas, com degradação graciosa em cada uma:**

| Camada | Dependência | Status |
|---|---|---|
| **T1 — léxica** | nenhuma (`sqlite3` stdlib; FTS5 confirmado na SQLite 3.50.4 local) | **sempre ativa** |
| **T2 — semântica** | `model2vec` (wheel pura; deps: numpy, tokenizers, safetensors, joblib, jinja2, tqdm — **sem torch**) | opcional (`requirements-semantic.txt`) |
| **T3 — rerank** | `ollama` CLI | opcional (`SEARCH_RERANK_LLM=1`) |

`model2vec` usa embeddings **estáticos destilados**: carrega em ~100 ms, não importa
torch, e `minishlab/potion-multilingual-128M` cobre português — resolvendo de quebra o
problema que o MiniLM inglês teria. `SentenceTransformerBackend` fica implementado atrás
do mesmo protocolo para quem já tenha torch (`EMBEDDING_BACKEND=sentence-transformers`).

**Fase 4 é opcional e lazy:** nenhuma dependência de embeddings entra em
`requirements.txt`. Quando ausente, `get_backend()` retorna `None`, a indexação grava
`embedding=NULL`, e `query.py` usa só FTS5 imprimindo uma dica única
(`Busca semantica desativada - instale: pip install -r requirements-semantic.txt`).
Nenhum `ImportError` vaza. **Este é o caminho padrão de uma instalação limpa.**

**Fusão (quando T2 ativa):** Reciprocal Rank Fusion, `score = soma de 1/(60 + rank_i)`
sobre o ranking BM25 do FTS5 e o ranking de cosseno. O cosseno roda sobre **todas** as
linhas (matmul numpy; 5 000 x 256 floats = ~5 MB, <10 ms), não apenas sobre os
candidatos do FTS — restringir ao FTS anularia o propósito da busca semântica. Acima de
50 000 linhas, degrada para os 2 000 melhores do FTS (marco de escala documentado, não
implementado agora).

**Índice FTS5:** `arquivos_fts(nome_atual, nome_orig, tipo, subtipo, texto_amostra,
path UNINDEXED)` como *external content table* sobre `arquivos`, com
`tokenize="unicode61 remove_diacritics 2"` — essencial para PT-BR (`horario` casa
`horário`). Sincronizado por triggers `AFTER INSERT/UPDATE/DELETE`.

**Fluxo da query** (`python query.py "..."`):
```
pergunta -> normaliza -> T1 FTS5 (BM25, top 20)
                      -> T2 cosseno global (top 20)     [se backend disponivel]
                      -> RRF -> top 5
                      -> T3 rerank ollama               [se SEARCH_RERANK_LLM=1]
                      -> rich.Table: score | path | tipo/subtipo | indexado_em
                      -> exit 0 (achou) / 1 (vazio);  --json para uso programatico
```

---

## 14. Estratégia de teste sem tocar em dados reais (não-negociável)

`C:\Users\joaor\Downloads` tem **126 arquivos e 26 pastas de produção**. Quatro barreiras
independentes, em profundidade:

**Barreira 1 — nenhum caminho hardcoded.** Todo caminho vem de `config.py`. O teste
`test_isolation.py::test_sem_paths_hardcoded` varre `organizer/**/*.py` procurando
`Users\joaor`, `Downloads` e `C:\` e falha se encontrar fora de docstrings.

**Barreira 2 — sandbox por fixture.** `tests/conftest.py` monta, dentro do `tmp_path` do
pytest:
```
sandbox/
├── Downloads/                    -> DOWNLOADS_DIR
├── Organizado/<arvore completa>  -> TARGET_ROOT
└── Organizado/.foa/index.db      -> DB_PATH
```
`monkeypatch.setenv` para todas as chaves + `config.get_config.cache_clear()`.
Nenhum teste toca `%USERPROFILE%`.

**Barreira 3 — interlock ativo (fixture `autouse`).** Um fixture de escopo `session`
monkeypatcha `os.replace`, `os.remove`, `os.unlink`, `os.rename`, `shutil.move`,
`shutil.copy2` e `pathlib.Path.unlink` por wrappers que **levantam `RuntimeError`** se
qualquer argumento estiver fora de `tmp_path`. Se um bug apontar para o Downloads real,
o teste explode em vez de mover o arquivo do usuário.

**Barreira 4 — guarda em produção.** `config.validar()` recusa iniciar se
`DOWNLOADS_DIR == TARGET_ROOT`, se um for subpasta do outro, ou se `DB_PATH`/`LOG_DIR`
estiverem dentro de `DOWNLOADS_DIR`. Adicionalmente, `FOA_ENV=test` (definido pelo
`conftest`) faz o `config` recusar qualquer raiz fora do `tmp_path` da sessão.

**Fixtures sintéticas (`tests/factories.py`)** — nada é copiado do Downloads real:
- PDF mínimo válido: bytes literais de um PDF de 1 página com texto conhecido
  (constante de ~600 bytes) — permite exercitar o `pdfplumber` de verdade;
- PDF protegido: variante com dicionário `/Encrypt`, para o caminho de erro;
- DOCX: gerado em runtime com `python-docx` (já é dependência);
- PNG/JPG: bytes de uma imagem 1x1;
- EXE/ZIP/MP4: bytes aleatórios com a extensão certa (a classificação por extensão não
  lê conteúdo — é exatamente o ponto);
- Nomes: matriz de casos derivada da seção 8 (genéricos e não-genéricos), incluindo os
  casos reais observados como `holerite_..._2026-07 (1).pdf`.

**Fake do Ollama.** `llm.py` executa `[OLLAMA_BIN, "run", ...]` com `OLLAMA_BIN` vindo da
config. Nos testes, aponta para um script Python que imprime respostas roteirizadas
(JSON válido; JSON dentro de cerca markdown; lixo; categoria fora do enum; e um caso que
dorme para exercitar o timeout). **Nenhum teste depende de o Ollama estar instalado.**

**Fake da GPU.** `guard.py` isola as chamadas NVML atrás de `_ler_gpu()`, que os testes
monkeypatcham. Os testes de threshold são tabelas de valores para booleano esperado.

**Teste de fumaça opcional sobre dados reais** (`-m real_readonly`, desligado por padrão
via `addopts = -m "not real_readonly"` no `pytest.ini`): lê `DOWNLOADS_DIR` real
**somente** para rodar `is_generic` e `rules.por_extensao` sobre os nomes — sem abrir,
mover ou escrever nada. Serve para calibrar a heurística contra o corpus verdadeiro.

---

## 15. Modos de operação e Fase 5

`MODE=auto` (padrão): decide e move; incertos vão para `_Inbox/`.

`MODE=interactive`: ⚠ **DIVERGÊNCIA 5.** A spec pede "notificação que pede aprovação".
`plyer` no Windows emite toasts **sem callback de botão** — não há como capturar a
resposta do usuário pela notificação. Design honesto:
1. o arquivo é movido para `_Inbox/_Aguardando/` (sai do Downloads, não vai ao destino final);
2. `notify.notificar()` dispara o toast com o destino proposto;
3. `python inbox.py` mostra uma tabela `rich` com id, nome, destino proposto, confiança e motivo;
4. `python inbox.py --aprovar 12` executa o move planejado; `--rejeitar 12` mantém no
   `_Inbox`; `--aprovar-todos --acima 0.85` faz lote.

O plano fica persistido em `operacoes(estado='aguardando_aprovacao')`, então sobrevive a
reboot. Se `plyer` falhar (comum em sessão de serviço), a fila continua funcionando — a
notificação é conveniência, não mecanismo.

---

## 16. Riscos endereçados

**R1 — Loop infinito (pasta-alvo dentro do Downloads).** Três defesas:
(a) `config.validar()` recusa iniciar se `TARGET_ROOT`, `INBOX_DIR`, `DB_PATH` ou
`LOG_DIR` forem subpasta de `DOWNLOADS_DIR` (ou vice-versa);
(b) `ingest` descarta qualquer path com `is_subpath(TARGET_ROOT)`;
(c) o `_Inbox` **nunca** é observado pelo watchdog — só `DOWNLOADS_DIR` é.
Um path já presente em `arquivos` é ignorado, quebrando qualquer ciclo residual.

**R2 — Arquivos parciais de navegador.** Camada 1 da seção 7 (blocklist) + Camadas 2 e 3.
Nota: o Chrome renomeia `x.crdownload` para `x.pdf` no fim, gerando um evento `moved`;
o handler trata `on_moved` usando `dest_path` — comportamento correto e desejado.

**R3 — MAX_PATH (260) e caracteres inválidos vindos do LLM.** Evidência real medida no
Downloads do usuário: já existe um path de **211 caracteres**
(um PDF de exame começando com `eyeagent-97gnaPFdKu4Z...` e terminando em
`_Macular Thickness OU Analysis_20260513135801.pdf`), a 49 caracteres do limite — movê-lo
para uma subpasta mais profunda **estouraria**. Regras em `paths.py`:
- `sanitize_stem`: remove os caracteres proibidos pelo Windows (menor-que, maior-que,
  dois-pontos, aspas, barra, contrabarra, pipe, interrogação, asterisco e controle
  0x00-0x1F), remove pontos e espaços à direita, aplica fold NFKD para ASCII, lowercase,
  troca espaços por hífen, colapsa hífens e corta em 80 chars; nomes de dispositivo
  reservados (`CON PRN AUX NUL COM1-9 LPT1-9`) recebem prefixo `_`; string vazia após a
  sanitização vira `arquivo`.
- `ensure_max_path(dest)`: se o path final passar de 250 chars, trunca o stem preservando
  a extensão e anexa hífen + os 8 primeiros hex do sha1 do stem original (determinístico
  e reversível via `arquivos.nome_orig`). Se ainda assim não couber, o arquivo vai para
  `_Inbox` com `motivo='path_muito_longo'`.
- `long_path(p)` (prefixo de caminho longo do Win32) é usado como *fallback* em
  `os.replace` e `os.stat` quando vier `OSError` com `winerror` 3 ou 206.
- O nome sugerido pelo LLM passa **obrigatoriamente** por `sanitize_stem` antes de
  qualquer uso, e a extensão nunca vem do LLM.

**R4 — Concorrência (`created` + `modified` no mesmo arquivo).** Debounce de 2 s em
memória, depois `INSERT` em `em_processamento` com `UNIQUE(path)` (o `IntegrityError`
descarta o duplicado), depois `pendentes.path UNIQUE`, depois reserva `O_EXCL` no
destino. Quatro pontos de serialização, nenhum dependendo de lock em memória de um
único processo.

**R5 — Ollama ausente** (verificado: `ollama` ainda não está no PATH desta máquina).
Ver seção 9. Nenhuma fase além da 3 depende dele, e a Fase 3 degrada para `_Inbox`
em vez de falhar.

**R6 — Python 3.14.** Wheels confirmadas em 2026-08-02 para cp314/win_amd64:
`psutil 7.2.2`, `numpy 2.5.1`, `onnxruntime 1.28.0`, `torch 2.13.0`. `watchdog 6.0.0`
publica `py3-none-win_amd64`. `pdfplumber`, `python-docx`, `rich`, `plyer`, `nvidia-ml-py`,
`model2vec` são `py3-none-any`. `tokenizers 0.23.1` é `cp310-abi3` (compatível).
Nenhum bloqueio conhecido.

---

## 17. Schema final do banco

Evolui o schema da spec. Todo campo adicionado é justificado — nada de "metadado por via
das dúvidas".

```sql
-- v1
CREATE TABLE IF NOT EXISTS arquivos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    nome_orig     TEXT    NOT NULL,           -- spec
    nome_atual    TEXT    NOT NULL,           -- spec
    path          TEXT    NOT NULL UNIQUE,    -- spec (chave de idempotencia)
    path_orig     TEXT,                       -- auditoria: de onde veio (undo manual)
    tipo          TEXT,                       -- spec
    subtipo       TEXT,                       -- spec
    ext           TEXT,                       -- filtro barato na query
    tamanho       INTEGER,                    -- desempate de duplicata
    sha256        TEXT,                       -- deteccao de duplicata (secao 11)
    via           TEXT CHECK(via IN ('extensao','llm','manual','fallback')),
    confianca     REAL,                       -- exigido pelo dashboard do _Inbox
    status        TEXT CHECK(status IN ('organizado','inbox','aguardando','duplicado')),
    motivo        TEXT,                       -- por que foi para o _Inbox (legivel)
    duplicado_de  TEXT,                       -- path do original, se status='duplicado'
    texto_amostra TEXT,                       -- 500 chars: alimenta o FTS5 (Fase 4)
    embedding        BLOB,                    -- spec: float32 little-endian
    embedding_model  TEXT,                    -- impede misturar vetores de modelos diferentes
    embedding_dim    INTEGER,
    movido_em     TEXT,
    indexado_em   TEXT DEFAULT (datetime('now'))   -- spec
);
CREATE INDEX IF NOT EXISTS ix_arquivos_status ON arquivos(status);
CREATE INDEX IF NOT EXISTS ix_arquivos_sha    ON arquivos(sha256);

CREATE TABLE IF NOT EXISTS pendentes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    path         TEXT NOT NULL UNIQUE,        -- UNIQUE = dedupe de eventos duplicados
    detectado    TEXT DEFAULT (datetime('now')),
    retry_after  TEXT NOT NULL,               -- nome da spec preservado
    tentativas   INTEGER DEFAULT 0,           -- spec
    motivo       TEXT,                        -- 'ocupado' | 'instavel' | 'llm_timeout' | ...
    ultimo_erro  TEXT
);
CREATE INDEX IF NOT EXISTS ix_pendentes_retry ON pendentes(retry_after);

-- journal write-ahead (secao 12)
CREATE TABLE IF NOT EXISTS operacoes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    origem        TEXT NOT NULL,
    destino       TEXT NOT NULL,
    estado        TEXT NOT NULL CHECK(estado IN
                    ('planejado','movido','concluido','aguardando_aprovacao','abortado','falhou')),
    confianca     REAL,
    via           TEXT,
    dry_run       INTEGER DEFAULT 0,
    pid           INTEGER,
    iniciado_em   TEXT DEFAULT (datetime('now')),
    finalizado_em TEXT,
    erro          TEXT
);
CREATE INDEX IF NOT EXISTS ix_operacoes_estado ON operacoes(estado);

CREATE TABLE IF NOT EXISTS reservas (
    op_id  INTEGER PRIMARY KEY REFERENCES operacoes(id),
    path   TEXT NOT NULL UNIQUE,
    criado TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS em_processamento (
    path        TEXT PRIMARY KEY,             -- lock logico entre workers
    pid         INTEGER,
    iniciado_em TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS config_kv (        -- schema_version, cache do ollama, flags
    chave     TEXT PRIMARY KEY,
    valor     TEXT,
    expira_em TEXT
);

-- v2 (Fase 4)
CREATE VIRTUAL TABLE IF NOT EXISTS arquivos_fts USING fts5(
    nome_atual, nome_orig, tipo, subtipo, texto_amostra, path UNINDEXED,
    content='arquivos', content_rowid='id',
    tokenize="unicode61 remove_diacritics 2"
);
-- + triggers AFTER INSERT / UPDATE / DELETE mantendo arquivos_fts sincronizada
```

**Justificativa do desvio de "apenas isso — sem metadados extras":** `status`,
`confianca` e `motivo` são **exigidos** pelo dashboard do `_Inbox` (Fase 5) e pelo modo
interativo; `sha256`/`tamanho` pela política de colisão (seção 11); `texto_amostra` e
`embedding_model` pela busca (Fase 4); `operacoes`/`reservas`/`em_processamento` pela
sobrevivência a crash (seção 12) — todos requisitos da própria spec que as duas tabelas
originais não conseguem suportar. O princípio "o path já é suficiente para localizar"
continua valendo para a busca.

---

## 18. Tratamento de erros

Política: **nenhuma exceção pode matar o watcher.** O watcher só faz spawn e SQL;
qualquer erro relativo a um arquivo morre dentro do worker.

| Camada | Falha | Resultado |
|---|---|---|
| watcher | erro no handler de evento | log `ERROR`, evento descartado, observer segue vivo |
| watcher | erro ao dar spawn | `pendentes(motivo='spawn_falhou')`, retry em 5 min |
| worker | arquivo sumiu | `exit 0` silencioso (log `DEBUG`) |
| worker | sistema ocupado | `pendentes(retry=+2 h, motivo='ocupado')`, `exit 2` |
| worker | instável além do timeout | `pendentes(retry=+10 min, motivo='instavel')` |
| worker | PDF protegido ou corrompido | segue sem texto; confiança cai; provável `_Inbox` |
| worker | LLM indisponível / timeout / lixo | `_Inbox` com `motivo` específico |
| worker | destino sem permissão ou disco cheio | `pendentes(retry=+30 min)`, journal `falhou` |
| worker | exceção não prevista | journal `falhou` + traceback no log + `pendentes` |
| qualquer | `tentativas > MAX_TENTATIVAS` (8) | move para `_Inbox`, remove da fila, log `WARNING` |

Backoff: `retry = base_do_motivo * 1.5^(tentativas-1)`, com teto de 24 h.
Todos os valores de `motivo` são constantes de um único `Enum` (`ingest.Motivo`), para
que o log seja grepável e os testes possam asserir sobre eles.

---

## 19. Ordem de implementação recomendada

1. **Fundação** (pré-Fase 1): `config`, `paths`, `log`, `db` + `tests/conftest.py` com as
   4 barreiras. Sem isso, qualquer código escrito depois arrisca o Downloads real.
2. **Fase 1**: `naming`, `rules`, `stability`, `classify` (só extensão), `move`, `ingest`,
   `worker`, `watch`.
3. **Fase 2**: `guard`, `queue`, loop idle, varredura de startup, `recover_incomplete`.
4. **Fase 3**: `extract`, `llm`, ramo LLM do `classify`.
5. **Fase 4**: FTS5 + `search` + `embeddings` (opcional) + `query.py`.
6. **Fase 5**: `notify`, `MODE=interactive`, `inbox.py`.

Cada fase termina com os testes da fase anterior ainda verdes e os requisitos
correspondentes de `docs/REQUISITOS.md` marcados.
