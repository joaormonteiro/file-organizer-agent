# File Organizer Agent

Agente local que vigia a pasta de downloads no Windows, classifica cada arquivo
novo, move para uma árvore organizada e indexa tudo num SQLite pesquisável.

**Princípio-mestre: zero RAM em idle.** Exatamente um processo fica vivo (o
watcher, ~5 MB, 0% de CPU). Tudo que é caro — `psutil`, NVML, `pdfplumber`,
`python-docx`, o Ollama — vive só dentro de processos filhos efêmeros que
nascem, agem e morrem.

Documentos de referência: [`docs/ARQUITETURA.md`](docs/ARQUITETURA.md) (decisões
técnicas) e [`docs/REQUISITOS.md`](docs/REQUISITOS.md) (critério de aceite).

---

## Estado da implementação

| Fase | Escopo | Estado |
|---|---|---|
| 0 | Fundação: config, banco, log, regras, infraestrutura de teste | **pronta** |
| 1 | Watcher + classificação por extensão + move + indexação | **pronta** |
| 2 | Resource Guard + fila de pendentes + varredura de startup + loop idle | **pronta** |
| 3 | Ollama: classificação por conteúdo e renomeação inteligente | **implementada**, prompt a calibrar |
| 4 | Busca: FTS5 pronta; fusão com embeddings pendente | parcial |
| 5 | Notificação, modo interativo e dashboard do `_Inbox` | pendente |

Com as Fases 0-2, arquivo baixado já vai sozinho para a pasta certa. Quando a
classificação por extensão não basta, o arquivo vai para o `_Inbox/` — a rede de
segurança — em vez de ficar apodrecendo na pasta de downloads.

### Fase 3 — o que a medição real mostrou

O encanamento funciona: o `phi3:mini` é chamado como subprocess, responde em
2-8 s, o JSON é parseado mesmo vindo cercado por ``` e o timeout com fallback
para o `_Inbox` funciona. **A qualidade da classificação ainda não.** Num teste
com 10 documentos sintéticos de nome genérico (nota fiscal, contrato, matriz
curricular, extrato, comprovante, RG, horário de aulas, trabalho acadêmico):

- 4 acertos completos, com confiança 0.90-0.95
- 3 acertos parciais — ramo certo, subpasta errada (certificado foi para
  `Comprovantes`, RG foi para `Pessoal/Outros`)
- 3 erros: nota fiscal classificada como `Pessoal/Outros`, trabalho acadêmico
  como `Matrizes-Curriculares`, e um documento em que o modelo falhou nas duas
  tentativas
- um nome sugerido saiu como `nota fiscal-e20260803.json` — com espaço e
  extensão indevida, defeito a investigar na sanitização

Ou seja: seguro (nada se perde, o pior caso é o `_Inbox`), mas o prompt precisa
de calibração antes de valer a pena confiar no modo automático para documentos.

---

## Setup

```bash
git clone <este repositório>
cd file-organizer-agent

python -m venv venv
venv\Scripts\activate

pip install -r requirements.txt          # uso normal
pip install -r requirements-dev.txt      # + pytest, para rodar a suíte
```

Depois copie o modelo de configuração e ajuste as raízes:

```bash
copy .env.example .env
```

As cinco chaves que importam são as primeiras do arquivo:

```ini
DOWNLOADS_DIR=C:\Users\SEU_USUARIO\Downloads
TARGET_ROOT=C:\Users\SEU_USUARIO\Organizado
INBOX_DIRNAME=_Inbox
DB_PATH=C:\Users\SEU_USUARIO\Organizado\.foa\index.db
LOG_DIR=C:\Users\SEU_USUARIO\Organizado\.foa\logs
```

Não há default para `DOWNLOADS_DIR` nem para `TARGET_ROOT`: o agente **recusa
iniciar** sem eles, e recusa também se as duas raízes se sobrepuserem. É o que
impede o loop infinito de reorganizar a própria saída.

### LLM local (opcional — Fase 3)

```bash
ollama pull phi3:mini
```

Sem o Ollama instalado, nada quebra: os arquivos ambíguos vão para o `_Inbox`
com `motivo=llm_indisponivel` e todo o resto continua funcionando.

### Busca semântica (opcional — Fase 4)

```bash
pip install -r requirements-semantic.txt
```

Sem esse extra, a busca usa só o índice léxico FTS5 do próprio SQLite — que já
ignora acentuação (`horario` encontra `horário`). O extra **não** arrasta
`torch`: usa `model2vec`, com embeddings estáticos destilados.

---

## Uso

### Watcher (o processo que fica vivo)

```bash
python watcher.py
```

Ao subir, ele recupera operações interrompidas, limpa locks órfãos, varre a
pasta de downloads atrás do que passou batido e então fica ouvindo eventos.
Ctrl-C encerra limpo. Para rodar junto com o Windows, registre como serviço com
[nssm](https://nssm.cc/).

### Busca

```bash
python query.py "onde esta a matriz curricular"
python query.py "contrato de estagio" --json
```

Sai com 0 quando encontra e 1 quando não encontra.

### Dashboard do `_Inbox`

```bash
python inbox.py
```

Fase 5 — ainda não implementado.

---

## Como o agente decide

1. **Filtro estático** (custo zero): `.crdownload`, `.part`, `~$...`, arquivos
   ocultos e diretórios são descartados sem sequer entrar na fila.
2. **Arquivo pronto?** Probe de handle exclusivo via `CreateFileW` mais três
   leituras consecutivas de tamanho e mtime. Ainda gravando → volta para a fila.
3. **Sistema ocupado?** CPU > 70%, RAM > 80%, GPU > 60% ou VRAM > 70% → o
   arquivo vai para `pendentes` com retry em 2 h e o processo morre.
4. **Classificação por extensão**, com confiança em `[0, 0.95]`:
   `.exe` → 0.95, `.jpg` → 0.85, `.pdf` sem pista → 0.50,
   `nota-fiscal-2026-05.pdf` → 0.85. É a "regra dos 90%".
5. **Abaixo de `CONFIDENCE_MIN` (0.75)** → `_Inbox/`, com o nome original
   preservado e um motivo legível.
6. **Move com journal write-ahead**: a intenção vai para o banco antes de
   qualquer alteração no disco, o destino é reservado com `O_EXCL` e só então
   acontece o `os.replace`. Matar o agente no meio não perde nem duplica nada.

### Regras de segurança de dados

- **Nunca deletar.** Só duas deleções existem no código inteiro, ambas em
  `organizer/move.py` e ambas logadas: a reserva de 0 byte criada pelo próprio
  processo, e a origem de um move entre volumes **depois** de conferir o sha256
  (e só com `ALLOW_CROSS_VOLUME=1`).
- **Nunca sobrescrever.** Destino ocupado por conteúdo idêntico vira duplicata
  em `_Inbox/_Duplicados/`; por conteúdo diferente, ganha sufixo `-2`, `-3`, …
- **Nunca executar** o arquivo classificado. `.exe`, `.msi`, `.bat`, `.cmd` e
  `.ps1` são lidos só por nome, extensão e tamanho.
- `DRY_RUN=1` planeja e loga tudo sem tocar no disco do usuário.

---

## Testes

```bash
venv\Scripts\python -m pytest
venv\Scripts\python -m pytest --cov=organizer --cov-report=term-missing
```

A suíte roda inteira sem Ollama, sem GPU e sem as dependências de embeddings.

Nenhum teste toca em arquivo real. Quatro barreiras independentes garantem isso:
todo caminho vem da configuração; o sandbox vive no `tmp_path` do pytest; um
interlock `autouse` faz `os.replace`, `os.remove`, `os.unlink`, `os.rename`,
`shutil.move`, `shutil.copy2` e `Path.unlink` levantarem `RuntimeError` fora do
sandbox; e `FOA_ENV=test` faz a própria configuração recusar raízes de fora.

O marcador `real_readonly` (desligado por padrão) roda a heurística de nomes
sobre uma pasta real, lendo **apenas** os nomes:

```bash
set FOA_REAL_DOWNLOADS=C:\Users\SEU_USUARIO\Downloads
venv\Scripts\python -m pytest -m real_readonly
```

---

## Estrutura

```
watcher.py  query.py  inbox.py      entrypoints finos
organizer/
  config.py    .env + ambiente, validação fail-fast
  paths.py     regras de filesystem do Windows
  naming.py    heurística de nome genérico
  rules.py     tabela de extensões, categorias, keywords
  log.py       log rotativo + linha única de decisão
  db.py        única porta de acesso ao SQLite
  stability.py "o arquivo terminou de ser gravado?"
  guard.py     Resource Guard (psutil + NVML)
  queue.py     fila de pendentes, backoff e o Enum de motivos
  classify.py  categoria, nome final e confiança
  move.py      único módulo que altera o disco do usuário
  ingest.py    pipeline de um arquivo
  worker.py    processo filho efêmero
  watch.py     processo permanente
  extract.py llm.py embeddings.py search.py notify.py   (Fases 3-5)
tests/         conftest.py (sandbox e interlocks), factories.py, testes
```
