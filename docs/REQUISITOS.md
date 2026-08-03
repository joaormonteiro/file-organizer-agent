# File Organizer Agent — Requisitos verificáveis (critério de aceite)

> Este arquivo é o **critério de aceite** usado pelo AGENTE REVISOR.
> Cada item é observável: dá para confirmar lendo um trecho específico de código ou
> rodando um comando. Nada de "código limpo" ou "bem estruturado".
> Projeto: `docs/ARQUITETURA.md`. Fonte original: `C:\projects\vault\projetos\file-organizer-agent.md`.

**Convenção**
- `RF-nn` = requisito funcional. `RNF-nn` = requisito não-funcional.
- Cada requisito traz **V:** = como verificar (comando ou local exato no código).
- Um requisito só pode ser marcado como atendido se a verificação **foi executada**.
- Prefixo `[BLOQUEANTE]` = reprova a fase se falhar.

---

## Fase 0 — Fundação, segurança de dados e infraestrutura de teste

*Deve estar pronta e verde antes de qualquer código que toque no filesystem.*

### RF-01 [BLOQUEANTE]: Toda raiz de caminho vem de configuração, nunca de literal no código.
`DOWNLOADS_DIR`, `TARGET_ROOT`, `INBOX_DIR`, `DB_PATH` e `LOG_DIR` são lidos por
`organizer/config.py` a partir de `.env` + variáveis de ambiente (ambiente vence `.env`).
**V:** `pytest tests/test_isolation.py::test_sem_paths_hardcoded` — varre `organizer/**/*.py`
e falha se encontrar `Downloads`, `Users` + `joaor`, ou raiz de unidade fora de docstring.

### RF-02: Existe `.env.example` com todas as chaves documentadas e seus defaults.
**V:** `pytest tests/test_config.py::test_env_example_cobre_todas_as_chaves` compara as
chaves do arquivo com os campos do dataclass `Config`.

### RF-03 [BLOQUEANTE]: `config.validar()` recusa iniciar com raízes sobrepostas.
Erro fatal se `DOWNLOADS_DIR == TARGET_ROOT`, se um for subpasta do outro, ou se
`DB_PATH`, `LOG_DIR` ou `INBOX_DIR` estiverem dentro de `DOWNLOADS_DIR`.
**V:** `pytest tests/test_config.py::test_recusa_raizes_sobrepostas` (4 casos, cada um
esperando `ConfigError`).

### RF-04: `config.get_config()` é cacheado e expõe `cache_clear()` para os testes.
**V:** `pytest tests/test_config.py::test_cache_e_clear`.

### RF-05: `organizer/db.py` é a única porta de acesso ao SQLite.
**V:** `pytest tests/test_isolation.py::test_sql_apenas_em_db` — grep por `sqlite3` e por
comandos SQL nos demais módulos deve retornar zero ocorrências.

### RF-06: A conexão aplica `journal_mode=WAL`, `busy_timeout>=10000`, `foreign_keys=ON`.
**V:** `pytest tests/test_db.py::test_pragmas` lê os PRAGMAs de volta da conexão.

### RF-07: Migrations versionadas e idempotentes.
`db.migrar()` aplica `MIGRATIONS` em ordem e grava a versão em `config_kv['schema_version']`.
**V:** `pytest tests/test_db.py::test_migrar_e_idempotente` — roda duas vezes e compara
`sqlite_master` e a versão.

### RF-08: A árvore de destino é criada sob demanda a partir de `rules.CATEGORIAS`.
Nenhuma pasta é criada durante o import de um módulo.
**V:** `pytest tests/test_rules.py::test_arvore_criada_sob_demanda`.

### RNF-01 [BLOQUEANTE]: Nenhum teste toca o Downloads real nem qualquer caminho fora do
`tmp_path` do pytest.
**V:** fixture `autouse` de escopo `session` em `tests/conftest.py` monkeypatcha
`os.replace`, `os.rename`, `os.remove`, `os.unlink`, `shutil.move`, `shutil.copy2` e
`Path.unlink` para levantar `RuntimeError` fora do sandbox;
`pytest tests/test_isolation.py::test_interlock_dispara_fora_do_sandbox` prova o interlock.

### RNF-02 [BLOQUEANTE]: Nenhuma fixture copia arquivo do Downloads real.
Todos os artefatos vêm de `tests/factories.py` (bytes literais ou `python-docx`).
**V:** inspeção de `tests/factories.py` +
`pytest tests/test_isolation.py::test_factories_nao_leem_disco_do_usuario`.

### RNF-03 [BLOQUEANTE]: O processo watcher não importa dependências pesadas.
Após `import organizer.watch`, `sys.modules` não contém `psutil`, `pynvml`, `pdfplumber`,
`docx`, `numpy`, `torch`, `model2vec`, `rich`, nem
`organizer.guard/ingest/classify/extract/llm/embeddings/search`.
**V:** `pytest tests/test_watch.py::test_watcher_nao_importa_pesado` (subprocess limpo).

### RNF-04: Existe `pytest.ini` com `addopts = -m "not real_readonly"`.
O marcador `real_readonly` fica desligado por padrão e, quando ligado, só lê nomes.
**V:** ler `pytest.ini` e inspecionar os testes marcados.

### RNF-05: A suíte roda sem Ollama, sem GPU e sem as dependências de embeddings.
**V:** `pytest` numa venv com apenas `requirements.txt` + `requirements-dev.txt` e sem
`ollama` no PATH deve ficar 100% verde.

### RNF-06 [BLOQUEANTE]: Chamadas de deleção existem apenas em `organizer/move.py`.
**V:** `pytest tests/test_isolation.py::test_delete_apenas_em_move` — grep por `os.remove`,
`os.unlink`, `shutil.rmtree` e `.unlink(` em `organizer/` fora de `move.py` deve dar zero.

### RNF-07 [BLOQUEANTE]: Em `move.py` a deleção só ocorre em dois casos, ambos logados.
(a) reserva de 0 byte criada por este processo e registrada em `reservas`;
(b) origem de move entre volumes, após conferência de `sha256`, e só com `ALLOW_CROSS_VOLUME=1`.
**V:** `pytest tests/test_move.py::test_nunca_deleta_arquivo_do_usuario` e
`::test_cross_volume_recusado_por_padrao`.

### RNF-08: `DRY_RUN=1` não altera o filesystem do usuário.
**V:** `pytest tests/test_ingest.py::test_dry_run_nao_move` — o arquivo permanece na origem,
`operacoes` registra `dry_run=1` e o log mostra o destino planejado.

---

## Fase 1 — Watcher + classificação por extensão + mover + indexar

### RF-09: `python watcher.py` inicia, observa `DOWNLOADS_DIR` e encerra limpo com Ctrl-C.
**V:** execução manual no sandbox + `pytest tests/test_watch.py::test_ciclo_de_vida`.

### RF-10: O observer é não recursivo por padrão (`WATCH_RECURSIVE=0`).
Subpastas do Downloads (o real tem 26) são ignoradas.
**V:** `pytest tests/test_watch.py::test_nao_recursivo_por_padrao` — cria arquivo em
subpasta do sandbox e verifica que nenhum worker é disparado.

### RF-11 [BLOQUEANTE]: Diretórios nunca são movidos nem renomeados.
**V:** `pytest tests/test_ingest.py::test_diretorio_e_ignorado`.

### RF-12 [BLOQUEANTE]: Arquivos parciais são ignorados sem entrar em nenhuma fila.
Extensões `.crdownload`, `.part`, `.partial`, `.download`, `.opdownload`, `.tmp`, `.temp`,
`.aria2`, `.filepart`, `.dctmp`; nomes iniciados por til-cifrão ou ponto; nomes terminados em til.
**V:** `pytest tests/test_watch.py::test_extensoes_parciais_ignoradas` (parametrizado com a
lista completa) — nem `pendentes` nem `em_processamento` recebem linha.

### RF-13: `on_moved` é tratado usando `dest_path`.
Cobre o caso real do Chrome renomeando `x.crdownload` para `x.pdf`.
**V:** `pytest tests/test_watch.py::test_on_moved_usa_dest_path`.

### RF-14 [BLOQUEANTE]: A detecção de arquivo em gravação combina probe de handle exclusivo
com estabilidade de tamanho e mtime.
`stability.esta_pronto` usa `CreateFileW` com `dwShareMode=0`, tratando os erros 32
(ERROR_SHARING_VIOLATION) e 33 (ERROR_LOCK_VIOLATION) como "em uso", e exige
`STABILITY_POLLS` leituras consecutivas de `st_size` e `st_mtime_ns` idênticas.
**V:** `pytest tests/test_stability.py::test_arquivo_aberto_por_outro_handle_nao_esta_pronto`
(mantém um handle aberto em processo separado) e `::test_arquivo_crescendo_nao_esta_pronto`.

### RF-15: Estabilidade que estoura `STABILITY_TIMEOUT` vira `pendentes(motivo='instavel')`,
sem processar e sem travar o worker.
**V:** `pytest tests/test_stability.py::test_timeout_vai_para_pendentes`.

### RF-16: A classificação por extensão cobre as famílias da spec.
Executáveis/instaladores, compactados, imagens, vídeo, áudio, documentos de texto,
planilhas e apresentações.
**V:** `pytest tests/test_rules.py::test_mapa_de_extensoes` (tabela parametrizada
extensão para categoria esperada).

### RF-17 [BLOQUEANTE]: A confiança por extensão segue a escala de ARQUITETURA §10.1.
0.95 família e subtipo únicos; 0.85 subtipo default; 0.60 ambíguo; 0.30 desconhecido;
ajustes de +0.20 (keyword), +0.05 (keyword com ano), -0.15 (contradição), -0.10 (nome
genérico); clamp final em `[0.0, 0.95]`.
**V:** `pytest tests/test_classify.py::test_escala_de_confianca_por_extensao` — casos
canônicos: `x.exe` 0.95, `foto.jpg` 0.85, `relatorio.pdf` 0.50,
`nota-fiscal-2026-05.pdf` 0.85, `arquivo.v38` 0.30.

### RF-18 [BLOQUEANTE]: `naming.is_generic` implementa as regras G1 a G5 e a regra negativa
do sufixo de cópia.
**V:** `pytest tests/test_naming.py::test_nomes_genericos` e `::test_nomes_nao_genericos`.
Casos obrigatoriamente **genéricos**: `document.pdf`, `documento (2).docx`,
`download (3).zip`, `IMG_20260214.jpg`, `20260214093311.pdf`, `aa.pdf`.
Casos obrigatoriamente **não genéricos**: `holerite_contabil_horizonte_ana_clara_2026-07 (1).pdf`,
`log-events-viewer-result (3).csv`, `matriz-curricular-ec-2026.pdf`.

### RF-19: `is_generic` retorna também o motivo (`G1` a `G5`), e o motivo aparece no log.
**V:** assinatura `-> tuple[bool, str]` + `pytest tests/test_naming.py::test_motivo_retornado`.

### RF-20 [BLOQUEANTE]: A sanitização de nome remove todos os caracteres proibidos do Windows.
Remove menor-que, maior-que, dois-pontos, aspas, barra, contrabarra, pipe, interrogação,
asterisco e controles 0x00 a 0x1F; remove pontos e espaços à direita; faz fold NFKD para
ASCII; lowercase; espaços viram hífen; corta em 80 chars; nomes de dispositivo reservados
(`CON`, `PRN`, `AUX`, `NUL`, `COM1` a `COM9`, `LPT1` a `LPT9`) ganham prefixo underscore;
resultado vazio vira `arquivo`.
**V:** `pytest tests/test_paths.py::test_sanitize_stem`, parametrizado com cada caractere
proibido e com cada nome reservado.

### RF-21 [BLOQUEANTE]: O path final nunca excede 250 caracteres.
`ensure_max_path` trunca o stem preservando a extensão e anexa hífen mais 8 hex do sha1 do
stem original; se ainda não couber, o destino vira `_Inbox` com `motivo='path_muito_longo'`.
**V:** `pytest tests/test_paths.py::test_max_path` — inclui o caso real de 211 caracteres
observado no Downloads do usuário, movido para a subpasta mais profunda da árvore.

### RF-22 [BLOQUEANTE]: Colisão no destino nunca sobrescreve.
Destino idêntico (tamanho + sha256) vai para `_Inbox/_Duplicados/` com `status='duplicado'`
e `duplicado_de` preenchido; destino diferente recebe sufixo `-2`, `-3` e assim por diante,
obtido via `open(dst,'xb')`; após 999 tentativas vai para `_Inbox` com
`motivo='colisao_irresolvivel'`.
**V:** `pytest tests/test_move.py::test_colisao_identica`, `::test_colisao_diferente`,
`::test_colisao_esgotada`.

### RF-23: O sufixo de colisão é `-N`, nunca ` (N)`.
**V:** asserção explícita em `tests/test_move.py::test_colisao_diferente`.

### RF-24 [BLOQUEANTE]: Todo arquivo processado é indexado em `arquivos` com `nome_orig`,
`nome_atual`, `path` único, `tipo`, `subtipo`, `via`, `confianca` e `status`.
**V:** `pytest tests/test_ingest.py::test_indexacao_completa`.

### RF-25: Reprocessar o mesmo path não duplica linhas em `arquivos`.
**V:** `pytest tests/test_ingest.py::test_indexacao_idempotente` (roda duas vezes, conta 1).

### RF-26 [BLOQUEANTE]: Arquivo com confiança abaixo de `CONFIDENCE_MIN` vai para `_Inbox/`
com o nome preservado e um `motivo` legível — e sai do Downloads.
**V:** `pytest tests/test_ingest.py::test_baixa_confianca_vai_para_inbox`.

### RF-27 [BLOQUEANTE]: Nenhum arquivo classificado é executado.
`.exe`, `.msi`, `.bat`, `.cmd`, `.ps1` são classificados só por nome, extensão e tamanho.
**V:** `pytest tests/test_isolation.py::test_nunca_executa_arquivo_do_usuario` — grep por
`os.startfile`, `os.system` e por `subprocess` recebendo o path do arquivo classificado;
os únicos `subprocess` permitidos são o do `ollama` em `llm.py` e o `Popen` do worker em
`watch.py`.

### RF-28 [BLOQUEANTE]: O worker é um processo separado que morre ao terminar.
`watch.py` faz `Popen([sys.executable, "-m", "organizer.worker", path])` e não bloqueia.
**V:** `pytest tests/test_watch.py::test_spawn_de_worker` + inspeção do código.

### RF-29: A concorrência é limitada por `MAX_WORKERS`; o excedente vai para `pendentes`
com retry curto.
**V:** `pytest tests/test_watch.py::test_limite_de_workers`.

### RF-30 [BLOQUEANTE]: Eventos duplicados (`created` mais `modified`) geram um único
processamento.
Debounce de 2 s, mais `em_processamento.path UNIQUE`, mais `pendentes.path UNIQUE`.
**V:** `pytest tests/test_watch.py::test_evento_duplicado_gera_um_worker`.

### RF-31 [BLOQUEANTE]: Nenhum arquivo dentro de `TARGET_ROOT` é processado (anti-loop).
**V:** `pytest tests/test_ingest.py::test_ignora_path_dentro_do_target`.

### RF-32 [BLOQUEANTE]: O `_Inbox` nunca é observado pelo watchdog.
**V:** inspeção do `Observer.schedule` em `watch.py` (só `DOWNLOADS_DIR`) +
`pytest tests/test_watch.py::test_inbox_nao_e_observado`.

### RNF-09: Nenhuma exceção em um arquivo derruba o watcher.
**V:** `pytest tests/test_watch.py::test_erro_em_evento_nao_mata_observer` — injeta exceção
no handler e confirma que o próximo evento ainda é atendido.

---

## Fase 2 — Resource Guard + fila de pendentes

### RF-33 [BLOQUEANTE]: `guard.sistema_ocupado()` usa `psutil` para CPU e RAM e `pynvml`
para GPU e VRAM, com os thresholds da spec vindos do `.env`.
CPU 70, RAM 80, GPU 60, VRAM 70; combinação por `or`, exatamente como no snippet da spec.
**V:** `pytest tests/test_guard.py::test_thresholds` — tabela de valores para booleano
esperado, cobrindo cada métrica isoladamente no limite e acima dele.

### RF-34 [BLOQUEANTE]: A ausência de GPU ou de driver NVML não quebra nada.
Qualquer `NVMLError` resulta em `gpu=0.0`, `vram=0.0` e `fonte='indisponivel'`.
**V:** `pytest tests/test_guard.py::test_sem_gpu_degrada_para_zero` (monkeypatch de
`_ler_gpu` levantando exceção).

### RF-35: `nvmlInit()` e `nvmlShutdown()` são sempre pareados em `try/finally`.
**V:** inspeção de `organizer/guard.py` + `pytest tests/test_guard.py::test_shutdown_sempre_chamado`.

### RF-36 [BLOQUEANTE]: `GPUtil` não é dependência do projeto.
**V:** `grep -ri gputil` no repositório retorna zero ocorrências fora de `docs/`;
`requirements.txt` lista `nvidia-ml-py` (a wheel oficial da NVIDIA, que publica o
módulo importável `pynvml`) e **não** lista o shim `pynvml` do PyPI.

### RF-37 [BLOQUEANTE]: Sistema ocupado enfileira em `pendentes` com
`retry_after = now + RETRY_BUSY_MINUTES` (padrão 120) e o worker sai com código 2.
**V:** `pytest tests/test_ingest.py::test_ocupado_vai_para_pendentes`.

### RF-38 [BLOQUEANTE]: O loop idle roda a cada `IDLE_LOOP_SECONDS` (padrão 600) e só
executa `SELECT ... FROM pendentes WHERE retry_after <= now`.
**V:** `pytest tests/test_queue.py::test_loop_idle_seleciona_vencidos` (com relógio
injetado) + inspeção de `watch.py`.

### RF-39: Pendente vencido com sistema ainda ocupado é adiado outras `RETRY_BUSY_MINUTES`
e tem `tentativas` incrementado.
**V:** `pytest tests/test_queue.py::test_adia_quando_ainda_ocupado`.

### RF-40 [BLOQUEANTE]: A varredura de startup encontra arquivos que passaram batido.
Para cada arquivo de `DOWNLOADS_DIR`: se já estiver em `arquivos` ou `pendentes`, ignora;
senão enfileira com `retry_after = now + RETRY_STARTUP_MINUTES` (padrão 30) quando o
sistema está ocupado, ou processa imediatamente quando não está.
**V:** `pytest tests/test_watch.py::test_varredura_de_startup`.

### RF-41: O backoff é `base * 1.5^(tentativas-1)` com teto de 24 h.
**V:** `pytest tests/test_queue.py::test_backoff`.

### RF-42 [BLOQUEANTE]: Acima de `MAX_TENTATIVAS` (padrão 8) o arquivo vai para `_Inbox`
e sai da fila — nunca fica em loop infinito de retry.
**V:** `pytest tests/test_queue.py::test_teto_de_tentativas`.

### RF-43 [BLOQUEANTE]: O journal `operacoes` é escrito **antes** de qualquer alteração no
filesystem, e o estado avança `planejado` → `movido` → `concluido`.
**V:** `pytest tests/test_move.py::test_journal_precede_o_move` (asserção de ordem via
espião nas chamadas).

### RF-44 [BLOQUEANTE]: `recover_incomplete()` roda no startup, antes de qualquer evento, e
cobre os 6 casos da tabela de ARQUITETURA §12.
**V:** `pytest tests/test_recovery.py` com um teste por linha da tabela:
`test_planejado_nada_aconteceu`, `test_planejado_com_reserva`,
`test_planejado_replace_ja_ocorreu`, `test_planejado_ambos_sumiram`,
`test_movido_reindexado`, `test_concluido_noop`.

### RF-45 [BLOQUEANTE]: Matar o worker no meio da operação não perde nem duplica arquivo.
**V:** `pytest tests/test_recovery.py::test_kill_no_meio` — mata o subprocess entre os
passos 2 e 4, roda `recover_incomplete()` e confirma que o arquivo está exatamente num
lugar e com exatamente uma linha em `arquivos`.

### RF-46: Registros de `em_processamento` mais velhos que `LOCK_TTL` (15 min) são limpos
no startup e a cada loop idle.
**V:** `pytest tests/test_queue.py::test_locks_orfaos_sao_limpos`.

---

## Fase 3 — Ollama: classificação por conteúdo e renomeação inteligente

### RF-47 [BLOQUEANTE]: O LLM só é acionado para documentos de texto
(`.pdf .docx .doc .txt .md .rtf .odt`) que tenham nome genérico **ou** confiança de
extensão abaixo de `CONFIDENCE_MIN`.
Um `.zip` de nome genérico **não** aciona o LLM.
**V:** `pytest tests/test_classify.py::test_quando_o_llm_e_acionado` (tabela de casos com
espião no `llm.classificar`).

### RF-48 [BLOQUEANTE]: `extract.trecho` devolve no máximo 500 caracteres e lê no máximo as
2 primeiras páginas de um PDF.
**V:** `pytest tests/test_extract.py::test_limite_de_500_chars` e `::test_limite_de_paginas`.

### RF-49: PDF protegido por senha retorna `ok=False, motivo='protegido'` sem levantar
exceção, e a classificação segue por nome e tamanho.
**V:** `pytest tests/test_extract.py::test_pdf_protegido` (fixture sintética com `/Encrypt`).

### RF-50: `.txt`, `.md` e `.csv` são lidos com cascata de encoding
(utf-8, utf-8-sig, cp1252, latin-1) sem levantar `UnicodeDecodeError`.
**V:** `pytest tests/test_extract.py::test_encodings`.

### RF-51 [BLOQUEANTE]: O Ollama é chamado como subprocess que morre ao terminar; nenhum
servidor é mantido vivo pelo agente.
**V:** inspeção de `organizer/llm.py` (`subprocess.run`, não `Popen` persistente, e nenhum
`ollama serve`) + `pytest tests/test_llm.py::test_processo_encerra`.

### RF-52 [BLOQUEANTE]: O prompt é enviado por **stdin**, nunca por argv.
**V:** `pytest tests/test_llm.py::test_prompt_por_stdin` — o fake do ollama ecoa o argv e o
teste confirma que o texto do prompt não está lá.

### RF-53 [BLOQUEANTE]: Ollama ausente ou modelo ausente degrada graciosamente.
`llm.disponivel()` retorna `False`, um `WARNING` único instrui `ollama pull phi3:mini`, e
os arquivos ambíguos vão para `_Inbox` com `motivo='llm_indisponivel'`. Nada quebra.
**V:** `pytest tests/test_llm.py::test_ollama_ausente` (PATH sem `ollama`) e
`::test_modelo_ausente` (fake retornando lista vazia).

### RF-54: O resultado de `disponivel()` é cacheado em `config_kv` com TTL de 1 h.
**V:** `pytest tests/test_llm.py::test_cache_de_disponibilidade` — a segunda chamada não
executa subprocess.

### RF-55 [BLOQUEANTE]: O parser de resposta cobre a cascata de 5 níveis.
JSON puro; JSON dentro de cerca markdown; primeiro objeto balanceado; 1 retry com prompt
encurtado; falha final devolve `None` com `motivo='llm_parse_error'`.
**V:** `pytest tests/test_llm.py::test_parsing` parametrizado com as 5 respostas do fake.

### RF-56 [BLOQUEANTE]: Categoria fora do enum `rules.CATEGORIAS` faz a resposta inteira ser
descartada — nunca é "consertada" nem usada parcialmente.
**V:** `pytest tests/test_llm.py::test_categoria_invalida_descarta_resposta`.

### RF-57 [BLOQUEANTE]: A extensão do arquivo nunca vem do LLM.
**V:** `pytest tests/test_classify.py::test_extensao_preservada` — o fake sugere
`nome_sugerido` com outra extensão e o destino final mantém a original.

### RF-58 [BLOQUEANTE]: O nome sugerido pelo LLM passa por `paths.sanitize_stem` antes de
qualquer uso.
**V:** `pytest tests/test_classify.py::test_nome_do_llm_e_sanitizado` — o fake devolve um
nome contendo caracteres proibidos do Windows e o destino final sai limpo.

### RF-59 [BLOQUEANTE]: `LLM_TIMEOUT` (padrão 90 s) é aplicado, e o timeout mata a árvore de
processos e enfileira em `pendentes` com `motivo='llm_timeout'`.
Após `MAX_TENTATIVAS_LLM` (2), o arquivo vai para `_Inbox`.
**V:** `pytest tests/test_llm.py::test_timeout` (fake que dorme, `LLM_TIMEOUT=1`) e
`tests/test_ingest.py::test_llm_timeout_esgotado_vai_para_inbox`.

### RF-60 [BLOQUEANTE]: A confiança do LLM segue a fórmula de ARQUITETURA §10.2.
`conf_final = clamp(0.5 * conf_llm + 0.5 * evidencia, 0.0, 0.90)`, com `evidencia`
somando 0.35 (categoria no enum), 0.25 (texto com 200+ chars), 0.20 (keyword presente) e
0.20 (nome sanitizado válido).
**V:** `pytest tests/test_classify.py::test_confianca_do_llm` — tabela com pelo menos 4
combinações e o valor exato esperado.

### RF-61: A confiança do LLM nunca alcança 0.95; uma decisão por extensão inequívoca sempre
vence em conflito.
**V:** `pytest tests/test_classify.py::test_teto_do_llm`.

### RF-62: `confianca` não numérica ou fora de `[0,1]` na resposta é tratada como 0.5.
**V:** `pytest tests/test_llm.py::test_confianca_invalida`.

---

## Fase 4 — Busca (FTS5 obrigatório, embeddings opcional)

### RF-63 [BLOQUEANTE]: `python query.py "pergunta"` funciona numa instalação que tem
apenas `requirements.txt` — sem `torch`, sem `sentence-transformers`, sem `model2vec`.
**V:** `pytest tests/test_search.py::test_busca_sem_backend_de_embeddings` +
execução manual na venv mínima.

### RF-64 [BLOQUEANTE]: A tabela `arquivos_fts` é FTS5 com
`tokenize="unicode61 remove_diacritics 2"` e é mantida em sincronia por triggers.
**V:** `pytest tests/test_search.py::test_fts_sincronizada` (insert, update e delete em
`arquivos` refletem em `arquivos_fts`) e `::test_busca_ignora_acento`
(`horario` encontra `horário`).

### RF-65: A busca léxica ordena por BM25 e devolve no máximo 20 candidatos.
**V:** `pytest tests/test_search.py::test_ranking_bm25`.

### RF-66 [BLOQUEANTE]: `embeddings.get_backend()` devolve `None` quando nenhuma dependência
opcional está instalada, sem levantar `ImportError`.
**V:** `pytest tests/test_search.py::test_backend_ausente_retorna_none`.

### RF-67 [BLOQUEANTE]: `organizer/embeddings.py` não importa numpy, model2vec nem
sentence-transformers no topo do módulo.
**V:** inspeção do arquivo + `pytest tests/test_search.py::test_import_lazy`
(`import organizer.embeddings` não traz numpy para `sys.modules`).

### RF-68: Com backend ausente, a indexação grava `embedding=NULL` e a busca continua
funcionando; `query.py` imprime uma dica única sobre `requirements-semantic.txt`.
**V:** `pytest tests/test_search.py::test_dica_de_instalacao`.

### RF-69: O vetor é gravado como float32 little-endian junto com `embedding_model` e
`embedding_dim`; vetores de modelos diferentes nunca são comparados entre si.
**V:** `pytest tests/test_search.py::test_nao_mistura_modelos`.

### RF-70: Com backend presente, a fusão usa Reciprocal Rank Fusion com k=60 entre o ranking
BM25 e o ranking de cosseno.
**V:** `pytest tests/test_search.py::test_rrf` com rankings sintéticos e score esperado.

### RF-71: `query.py` imprime o path absoluto do resultado, sai com 0 quando encontra e com 1
quando não encontra, e suporta `--json`.
**V:** `pytest tests/test_search.py::test_cli_exit_codes` e `::test_saida_json`.

### RF-72: O rerank por LLM é opcional (`SEARCH_RERANK_LLM=1`) e é pulado silenciosamente se
o Ollama não estiver disponível.
**V:** `pytest tests/test_search.py::test_rerank_opcional`.

### RNF-10 [BLOQUEANTE]: `requirements.txt` não contém `torch`, `sentence-transformers`,
`transformers`, `model2vec` nem `numpy`.
Essas dependências vivem apenas em `requirements-semantic.txt`.
**V:** `pytest tests/test_isolation.py::test_requirements_minimo` lê os dois arquivos.

### RNF-11: `query.py` responde em menos de 2 s num índice de 1 000 arquivos, sem backend
de embeddings.
**V:** `pytest tests/test_search.py::test_desempenho -m lento` com 1 000 linhas geradas.

---

## Fase 5 — Notificação, modo interativo e dashboard do `_Inbox`

### RF-73: `notify.notificar()` usa `plyer` e vira no-op silencioso se o `plyer` falhar.
**V:** `pytest tests/test_notify.py::test_falha_de_plyer_nao_propaga` (monkeypatch
levantando exceção).

### RF-74 [BLOQUEANTE]: Em `MODE=interactive`, nenhum arquivo vai direto para o destino final.
O arquivo é movido para `_Inbox/_Aguardando/`, e o plano fica em
`operacoes(estado='aguardando_aprovacao')`.
**V:** `pytest tests/test_ingest.py::test_modo_interativo_aguarda_aprovacao`.

### RF-75: `python inbox.py` lista os itens do `_Inbox` numa tabela com id, nome, destino
proposto, confiança e motivo.
**V:** `pytest tests/test_inbox.py::test_listagem`.

### RF-76 [BLOQUEANTE]: `python inbox.py --aprovar <id>` executa o move planejado usando o
mesmo `move.executar` (mesmo journal, mesma política de colisão).
**V:** `pytest tests/test_inbox.py::test_aprovar_move` — confirma que o journal recebeu o
estado `concluido` e que a política de colisão foi aplicada.

### RF-77: `python inbox.py --rejeitar <id>` mantém o arquivo no `_Inbox` e marca a operação
como `abortado`; nada é deletado.
**V:** `pytest tests/test_inbox.py::test_rejeitar`.

### RF-78: `python inbox.py --aprovar-todos --acima 0.85` aprova em lote apenas os itens
acima do limiar informado.
**V:** `pytest tests/test_inbox.py::test_aprovar_em_lote`.

### RF-79: O plano de aprovação sobrevive a reinício do agente (está no banco, não em memória).
**V:** `pytest tests/test_inbox.py::test_plano_persistente` — recria a conexão e a lista
continua igual.

---

## Requisitos não-funcionais transversais

### RNF-12 [BLOQUEANTE]: Idle real do watcher abaixo de 15 MB de RSS e 0% de CPU sustentado.
**V:** rodar `python watcher.py` no sandbox, esperar 60 s sem eventos e medir com
`Get-Process python | Select WorkingSet64` ou `psutil` num processo separado.

### RNF-13 [BLOQUEANTE]: Um arquivo classificado só por extensão é processado sem invocar
o Ollama.
**V:** `pytest tests/test_ingest.py::test_caminho_rapido_sem_llm` — espião em
`llm.classificar` com contagem zero para `.exe`, `.jpg`, `.mp4`, `.zip` com keyword.

### RNF-14: Todo log de decisão sai em uma única linha com os campos `decision=`, `path=`,
`dest=`, `conf=`, `via=`, `motivo=`.
**V:** `pytest tests/test_ingest.py::test_formato_do_log` com regex sobre o caplog.

### RNF-15: Todos os valores de `motivo` vêm de um único `Enum`, acessível como `ingest.Motivo`.
O Enum é **declarado em `organizer/queue.py`** (`queue.Motivo`) e reexportado por
`ingest.py`, de modo que `ingest.Motivo` continua sendo o nome canônico de uso.
Motivo da localização: `move.py` e `stability.py` também precisam do Enum, e `watch.py`
importa `move` para o `recover_incomplete()` do startup (RF-44). Se o Enum morasse em
`ingest.py`, o processo permanente acabaria arrastando `guard`, `classify` e `extract`
para dentro de si, violando a RNF-03. `queue.py` é leve (só `db` + `config`) e o
`motivo` é literalmente a coluna que ele gerencia.
**V:** inspeção + `pytest tests/test_ingest.py::test_motivos_sao_enum` (que assere
`ingest.Motivo is queue.Motivo`) + `pytest tests/test_isolation.py::test_motivos_nao_aparecem_como_literal`
(nenhuma string literal de motivo fora de `queue.py`) + `pytest tests/test_watch.py::test_watcher_nao_importa_pesado`.

### RNF-16: O log usa `RotatingFileHandler` com teto de 5 MB e 3 backups — o disco não
enche sozinho.
**V:** inspeção de `organizer/log.py` + `pytest tests/test_log.py::test_rotacao`.

### RNF-17: `README.md` documenta setup em venv, `ollama pull phi3:mini`, execução do
watcher, execução da query e a instalação opcional do extra semântico.
**V:** leitura do `README.md` e execução literal dos comandos numa venv limpa.

### RNF-18: `requirements.txt` fixa versões compatíveis com Python 3.14 no Windows.
`watchdog==6.0.0`, `psutil==7.2.2`, `nvidia-ml-py==13.610.43`, `pdfplumber==0.11.10`,
`python-docx==1.2.0`, `rich==15.0.0`, `plyer==2.1.0`.
**V:** `pip install -r requirements.txt` numa venv de Python 3.14 sem erro de build.

### RNF-19: Nenhum módulo de `organizer/` faz I/O de rede.
Exceção única: o download do modelo de embeddings, e apenas no primeiro uso do extra
opcional da Fase 4.
**V:** `pytest tests/test_isolation.py::test_sem_rede` — grep por `requests`, `urllib`,
`httpx` e `socket` em `organizer/` fora de `embeddings.py`.

### RNF-20: Cobertura de testes de no mínimo 80% em `organizer/`, com 100% em
`paths.py`, `naming.py`, `move.py` e `guard.py`.
**V:** `pytest --cov=organizer --cov-report=term-missing`.

---

## Matriz de rastreabilidade — spec do vault para requisito

| Item da spec | Requisitos |
|---|---|
| Monitora Downloads em tempo real | RF-09, RF-10, RF-13 |
| Zero RAM em idle / subprocessos efêmeros | RNF-03, RNF-12, RF-28, RF-51 |
| Regra dos 90% (maioria só por extensão) | RF-16, RF-17, RNF-13 |
| Renomeia se o nome for genérico | RF-18, RF-19, RF-57, RF-58 |
| Move para a pasta correta | RF-22, RF-24, RF-26 |
| Indexa em SQLite | RF-06, RF-07, RF-24, RF-25 |
| Resource Guard com os 4 thresholds | RF-33, RF-34, RF-36 |
| Fila `pendentes` com retry de 2 h | RF-37, RF-38, RF-39, RF-41 |
| Varredura de startup com retry de 30 min | RF-40 |
| Loop idle a cada 10 min | RF-38 |
| Ollama `phi3:mini` como subprocess | RF-51, RF-52, RF-53 |
| `CONFIDENCE_MIN=0.75` e `_Inbox` | RF-17, RF-26, RF-60, RF-61 |
| Busca semântica por linguagem natural | RF-63 a RF-72 |
| Modo interativo com notificação | RF-73 a RF-79 |
| Nunca deletar, só mover | RNF-06, RNF-07, RF-22, RF-77 |
| Esperar o handle fechar | RF-14, RF-15 |
| PDF protegido: só metadados | RF-49 |
| EXE: nunca executar | RF-27 |

---

## Divergências aprovadas em relação à spec do vault

Estas divergências estão justificadas em `docs/ARQUITETURA.md` e **não** devem ser
tratadas como defeito pelo revisor:

| # | Spec dizia | Implementação | Onde está justificado |
|---|---|---|---|
| 1 | Resource Guard antes do dispatch | Guard roda dentro do worker filho | ARQUITETURA §1 |
| 2 | Pastas-alvo espalhadas na raiz do perfil | Raiz única `TARGET_ROOT` | ARQUITETURA §4 |
| 3 | "`GPUtil` ou `pynvml`" | `nvidia-ml-py` (wheel oficial; publica o módulo `pynvml`). GPUtil abandonado em 2018, só sdist | ARQUITETURA §6 |
| 4 | `sentence-transformers` + `all-MiniLM-L6-v2` | FTS5 por padrão; embeddings opcionais via `model2vec` | ARQUITETURA §13 |
| 5 | Notificação que "pede aprovação" | Toast informativo + fila em `_Inbox/_Aguardando/` + `inbox.py` | ARQUITETURA §15 |
| 6 | Schema com 2 tabelas e sem metadados extras | 2 tabelas da spec estendidas + 4 tabelas de controle | ARQUITETURA §17 |
| 7 | §10.1: "−0,10 se `is_generic()` for verdadeiro" (sem qualificar o patamar) | O ônus é aplicado **apenas no patamar ambíguo (0,60)**, que é onde o nome é a evidência decisiva | ARQUITETURA §10.1 e `classify.classificar_por_extensao` |
| 8 | §10.1: "−0,15 se keywords de duas famílias diferentes casarem" | Penaliza só quando dois grupos do **mesmo eixo** apontam para **ramos de topo diferentes**; keywords corroborantes nunca derrubam a confiança | ARQUITETURA §10.1 e `classify.ha_contradicao` |
| 9 | RNF-15: Enum em `ingest.Motivo` | Declarado em `queue.Motivo`, reexportado como `ingest.Motivo` | RNF-15 acima |

**Divergência 7 — por que o ônus de nome genérico não vale em todos os patamares.**
Aplicá-lo em todo lugar contradiz os cinco valores canônicos que a própria RF-17 fixa:
`x.exe` (stem `x`, genérico por G4) daria 0,85 em vez de 0,95; `arquivo.v38` (G1) daria
0,20 em vez de 0,30; `foto.jpg` (G1) daria 0,75 em vez de 0,85. Nesses patamares a
extensão já resolve o destino sozinha e o nome não é evidência. Restringir o ônus ao
patamar 0,60 reproduz exatamente os cinco valores canônicos **e** os três exemplos de
"efeito prático" da ARQUITETURA §10.1 (`.pdf` sem keyword = 0,60 − 0,10 = 0,50).

**Divergência 8 — por que "famílias" virou "eixo + ramo".** Implementar a regra como
"duas categorias diferentes" quarentenava todo arquivo que nomeasse o cliente *e* o tipo
de documento: `contrato-estagio-eyeconnect.pdf` caía para 0,65 e ia para o `_Inbox`,
enquanto `contrato-estagio-2026.pdf` ia direto para `Profissional/Contratos` com 0,85 —
acrescentar informação verdadeira ao nome **piorava** a decisão. `Contratos`,
`Eyeconnect` e `EfficienceCo` são irmãs sob `Profissional`, e irmãs se corroboram.
Cada grupo de keywords passou a declarar um **eixo** (`tipo_de_documento` ou
`organizacao`); só há contradição quando dois grupos do mesmo eixo apontam para ramos de
topo diferentes (`contrato` + `certificado` = Profissional vs Academico → −0,15). Quando
os dois eixos casam, a organização decide a categoria, seguindo o exemplo do Query CLI
da própria spec do vault (`contrato de estágio eyeconnect` →
`Documentos/Profissional/Eyeconnect/contrato-estagio-2025-03.pdf`).
**V:** `pytest tests/test_classify.py::test_keyword_corroborante_nao_derruba_confianca`
(as 6 linhas levantadas na auditoria) e `::test_monotonicidade_ao_acrescentar_a_organizacao`.
