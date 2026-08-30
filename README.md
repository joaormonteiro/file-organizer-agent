# File Organizer Agent

A local agent that watches the Downloads folder on Windows, classifies every new
file, moves it into an organized tree and indexes everything in a searchable
SQLite database.

**Guiding principle: zero RAM at idle.** Exactly one process stays alive (the
watcher, ~5 MB, 0% CPU). Everything expensive (`psutil`, NVML, `pdfplumber`,
`python-docx`, the call to Gemini) only exists for the lifetime of a single
event.

Reference documents: [`docs/ARQUITETURA.md`](docs/ARQUITETURA.md) (technical
decisions) and [`docs/REQUISITOS.md`](docs/REQUISITOS.md) (acceptance criteria).
Both are in Portuguese.

---

## Implementation status

| Phase | Scope | Status |
|---|---|---|
| 0 | Foundation: config, database, logging, rules, test infrastructure | **done** |
| 1 | Watcher + classification by extension + move + indexing | **done** |
| 2 | Resource Guard + pending queue + startup scan + idle loop | **done** |
| 3 | Gemini: classification by content and smart renaming | **done** |
| 4 | Search: FTS5 + optional embeddings (`model2vec`) | **done** |
| 5 | Notifications, interactive mode and `_Inbox` dashboard | **done** |

**Project complete and approved in final audit.** All 5 phases implemented,
562 tests, 96% coverage (100% in `paths.py`, `naming.py`, `move.py`, `guard.py`).
No blocking items open.

With Phases 0 to 2, a downloaded file already makes its own way to the right
folder. When classification by extension isn't enough, the file goes to
`_Inbox/`, the safety net, instead of rotting in the Downloads folder.

### Phase 3: Ollama to Gemini

Until 08/2026, Phase 3 ran `phi3:mini` (3.8B) locally via `ollama run` as a
subprocess. It worked, but coverage was low: the model tended to collapse onto a
dominant category from the few-shot prompt whenever the text had no obvious
keyword, a limitation of the model rather than the prompt. In a validation run
with 8 real ambiguous documents, only 2 were classified (both correct); the other
6 fell into `_Inbox` for lack of lexical corroboration
(`classify.decisao_corroborada`).

The LLM boundary (`organizer/llm.py`) was switched to call the Gemini API over
HTTP (`urllib`, no new dependency) instead of a local binary. Direct gains from
the switch:

- **`responseSchema` with a closed `enum`**: the API is instructed to return
  only one of the canonical categories, so the whole class of "invented category"
  errors, previously caught only later in validation, is now much rarer at the
  source.
- No local VRAM contention: the Resource Guard thresholds went back to the
  original spec values (`THRESHOLD_GPU=60`, `THRESHOLD_VRAM=70`). A loaded
  `phi3:mini` fought for VRAM with games and editing on a laptop GPU; the API
  doesn't.
- No install, no model download: just a `GEMINI_API_KEY` in `.env`.

The safety lock (`decisao_corroborada`) is still active and unchanged: an LLM
decision is only accepted if a keyword from the chosen category appears in the
text or the file name. Without that, confidence stays pinned at 0.60 and the file
goes to `_Inbox`. **Always safe** (nothing is moved to the wrong place, the worst
case is `_Inbox`); coverage is expected to rise with a larger model and the
closed `enum`, but this hasn't yet been validated against real traffic, so it is
worth watching the first days of use before trusting it blindly.

---

## Setup

```bash
git clone <this repository>
cd file-organizer-agent

python -m venv venv
venv\Scripts\activate

pip install -r requirements.txt          # normal use
pip install -r requirements-dev.txt      # + pytest, to run the suite
```

Then copy the config template and adjust the roots:

```bash
copy .env.example .env
```

The keys that matter are the first ones in the file: 5 destination roots, one
per standard Windows folder, instead of a separate "Organized" tree.

```ini
DOWNLOADS_DIR=C:\Users\YOUR_USER\Downloads
DOCUMENTS_ROOT=C:\Users\YOUR_USER\Documents
PICTURES_ROOT=C:\Users\YOUR_USER\Pictures
VIDEOS_ROOT=C:\Users\YOUR_USER\Videos
MUSIC_ROOT=C:\Users\YOUR_USER\Music
DESKTOP_ROOT=C:\Users\YOUR_USER\Desktop
INBOX_DIRNAME=_Inbox
```

There is no default for `DOWNLOADS_DIR` or the 5 roots: the agent **refuses to
start** without them, and also refuses if any of them overlaps `DOWNLOADS_DIR`.
That's what prevents the infinite loop of reorganizing its own output. `DB_PATH`
and `LOG_DIR` don't need a value. Without them, the database and log live in
`%LOCALAPPDATA%\FileOrganizerAgent`, outside the 5 roots.

### LLM (optional, Phase 3)

Generate a free key at <https://aistudio.google.com/apikey> and set it in
`.env`:

```ini
GEMINI_API_KEY=your-key-here
GEMINI_MODEL=gemini-3.6-flash
```

Without `GEMINI_API_KEY`, nothing breaks: ambiguous files go to `_Inbox` with
`motivo=llm_indisponivel` and everything else keeps working.

### Semantic search (optional, Phase 4)

```bash
pip install -r requirements-semantic.txt
```

Without this extra, search uses only SQLite's own FTS5 lexical index, which
already ignores accents (`horario` finds `horário`). The extra does **not** pull
in `torch`: it uses `model2vec` (`potion-multilingual-128M`), with distilled
static embeddings. Tested on the 3 example queries from the original spec against
a small index: plain FTS5 already gets all 3 on its own. The semantic gain from
`model2vec` should show up with divergent vocabulary and a larger index, but
that's not what this project had to demonstrate. The real value of the extra is
avoiding the cost of `torch` (~122 MB) while keeping the option open.

### Interactive mode (optional, Phase 5)

```ini
MODE=interactive
```

In this mode, **every** file passes through `_Inbox/_Aguardando/` before going to
its final destination. The agent never moves anything on its own. Approval is
manual, via `inbox.py` (below).

---

## Usage

### Watcher (the process that stays alive)

```bash
python watcher.py
```

On startup it recovers interrupted operations, clears orphan locks, scans the
Downloads folder for anything it missed, and then listens for events. Ctrl-C
shuts down cleanly.

### Start automatically at login

```bash
instalar.bat
```

Writes a plain `.bat` into the user's Startup folder (`shell:startup`), which
starts the watcher via `pythonw.exe` (no window, no console) on the next login.
**No Task Scheduler**: the same mechanism validated on EyeAgent after
`schtasks /sc onlogon` failed silently on a real machine (the task existed, but
Windows reported "never ran at logon"). Also no PowerShell/COM, which another
attempt showed can be blocked by antivirus. Just `cmd`'s own `echo`, no external
dependency.

`instalar.bat` validates `.env` before installing (it actually runs
`config.montar()`) and refuses if the configuration is invalid. To remove it
from login:

```bash
desinstalar.bat
```

This only affects the next login. If the watcher is already running, it keeps
going until you log out or kill the `pythonw.exe` process manually.

### Search

```bash
python query.py "where is the course curriculum"
python query.py "internship contract" --json
```

Exits with 0 when it finds something and 1 when it doesn't.

### `_Inbox` dashboard

```bash
python inbox.py                          # list items pending approval
python inbox.py --aprovar 3              # approve item 3, move to destination
python inbox.py --rejeitar 3             # leave as is, drop from the list
python inbox.py --aprovar-todos --acima 0.85   # batch-approve by confidence
```

Approving reuses the same collision policy as the automatic flow: if the
destination already exists, it becomes a duplicate or gets a suffix, never
overwrites.

---

## How the agent decides

1. **Static filter** (zero cost): `.crdownload`, `.part`, `~$...`, hidden files
   and directories are dropped without even entering the queue.
2. **File ready?** Exclusive-handle probe via `CreateFileW` plus three
   consecutive reads of size and mtime. Still being written means it goes back
   to the queue.
3. **System busy?** CPU > 70%, RAM > 80%, GPU > 35% or VRAM > 45%, then the file
   goes to `pendentes` with a retry in 2h and the process dies. GPU/VRAM are
   deliberately more conservative than CPU/RAM: on a laptop GPU with shared VRAM,
   a loaded `phi3:mini` used about 2 to 3 GB, and a high threshold leaves little
   headroom, so the LLM ends up competing for VRAM with games or editing instead
   of simply waiting its turn.
4. **Classification by extension**, with confidence in `[0, 0.95]`: `.exe` gives
   0.95, `.jpg` 0.85, `.pdf` with no hint 0.50, `nota-fiscal-2026-05.pdf` 0.85.
   This is the "90% rule".
5. **Below `CONFIDENCE_MIN` (0.75)** the file goes to `_Inbox/`, with the
   original name preserved and a human-readable reason.
6. **Move with a write-ahead journal**: the intent goes to the database before
   any change on disk, the destination is reserved with `O_EXCL`, and only then
   does `os.replace` happen. Killing the agent mid-operation loses nothing and
   duplicates nothing.

### Data-safety rules

- **Never delete.** Only two deletions exist in the entire codebase, both in
  `organizer/move.py` and both logged: the 0-byte reservation created by the
  process itself, and the source of a cross-volume move **after** verifying the
  sha256 (and only with `ALLOW_CROSS_VOLUME=1`).
- **Never overwrite.** A destination occupied by identical content becomes a
  duplicate in `_Inbox/_Duplicados/`; by different content, it gets a suffix
  `-2`, `-3`, and so on.
- **Never execute** the classified file. `.exe`, `.msi`, `.bat`, `.cmd` and
  `.ps1` are read only by name, extension and size.
- `DRY_RUN=1` plans and logs everything without touching the user's disk.

---

## Tests

```bash
venv\Scripts\python -m pytest
venv\Scripts\python -m pytest --cov=organizer --cov-report=term-missing
```

The suite runs end to end with no network, no real `GEMINI_API_KEY`, no GPU and
none of the embedding dependencies.

No test touches a real file. Four independent barriers guarantee this: every
path comes from configuration; the sandbox lives in pytest's `tmp_path`; an
`autouse` interlock makes `os.replace`, `os.remove`, `os.unlink`, `os.rename`,
`shutil.move`, `shutil.copy2` and `Path.unlink` raise `RuntimeError` outside the
sandbox; and `FOA_ENV=test` makes the configuration itself refuse roots from
outside.

The `real_readonly` marker (off by default) runs the name heuristic over a real
folder, reading **only** the names:

```bash
set FOA_REAL_DOWNLOADS=C:\Users\YOUR_USER\Downloads
venv\Scripts\python -m pytest -m real_readonly
```

---

## Structure

```
watcher.py  query.py  inbox.py      thin entrypoints
organizer/
  config.py    .env + environment, fail-fast validation
  paths.py     Windows filesystem rules
  naming.py    generic-name heuristic
  rules.py     table of extensions, categories, keywords
  log.py       rotating log + single decision line
  db.py        the only door to SQLite
  stability.py "has the file finished being written?"
  guard.py     Resource Guard (psutil + NVML)
  queue.py     pending queue, backoff and the reason Enum
  classify.py  category, final name and confidence
  move.py      the only module that changes the user's disk
  ingest.py    single-file pipeline
  worker.py    ephemeral child process
  watch.py     permanent process
  extract.py   pdfplumber + python-docx, snippet of up to 500 chars
  llm.py       Gemini API over HTTP, tolerant JSON parser
  embeddings.py optional model2vec backend, no torch
  search.py    FTS5 + optional fusion with embeddings
  notify.py    Windows toast via plyer
tests/         conftest.py (sandbox and interlocks), factories.py, tests
```
