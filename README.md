# todo.box — a cross-session todo system for AI agents and humans

> **A physical folder as the single source of truth. One todo = one file. The filename is the protocol; the folder is the database. No database service, no daemon — `ls` is your query interface.**
>
> Built and battle-tested by an AI agent (Hermes) and its user working together daily. This repo documents the **architecture, the data flow, and 32 real pitfalls we stepped into** — so you don't have to.

---

## Why

LLM agents forget. Session memory is lossy, retrieval misses items, and "I'll remember that" is a lie the model tells both itself and the user. The fix is not a smarter database — it is a **physical, human-inspectable source of truth** that survives every session, every model swap, and every context compression.

Design constitution (five axioms):

1. **A file exists = the work exists. The file leaves = the account is settled.** Completion is a rename (`OPEN_`→`DONE_`) plus a move to the archive folder — never a verbal claim.
2. **One source of truth.** The box folder. Everything else (index, dashboard, summary) is derived and rebuildable from it. Never hand-repair a derived artifact.
3. **Computation lives in exactly one place.** Reminder cadence is computed once per day at dashboard-generation time; every other component only *reads* the result. This kills the "dashboard says 3, plugin says 5" multi-source disease.
4. **Failures must be loud.** Any silent failure is a design bug. Dashboard generation failure writes an alert file *inside the box* (so it syncs to the user's phone); a corrupted index degrades to `last_good` and returns a **visible warning** instead of crashing the conversation.
5. **Never auto-delete; only mark.** Machines tag `suspected` fields; final judgment always belongs to a human.

---

## Architecture

```
Source layer     <box>/            a plain folder, human-browsable
                 └─ one todo = one file: OPEN_20260917_T0000ff_watch_example-title.txt
                 (filename protocol segments are configurable; defaults ship in Chinese — see "Chinese by default" below)
Tools layer      box_add (writer) · todo_box_scan (scanner) · box_tidy (maintainer) · _make_dashboard (view)
Derived layer    todo_box_index.json (machine index) · _看板.html (human view, "kanban board") · todo_notify_summary.json (reminder digest)
Delivery layer   session-first-turn injection plugin (reads derived layer; one-line summary, never a full replay)
```

### What counts as "one todo" — the ledger rules

- **Filename protocol (five segments)**: `STATUS_DATE_TID_RHYTHM_TITLE.txt`. If the title itself contains `_`, take the first four segments from the left and give the rest to the title.
- **TID**: `T` + 6 hex digits, minted from a registry, **stable across renames** — this is the reconciliation key (stored in the file's first line as `tid:`, not as file:line, which would break on moves).
- **State machine**: `OPEN_` (active) → `DONE_` (done + archived to `_done\YYYY-MM\`). One transition, no intermediate states.
- **Rhythm enum**: watch / do-now / awaiting-owner / long-term / never-remind. Reminder cadence per rhythm (1d / 3d / 10d), configurable in a single source.
- **Single write entry point**: the writer mints the TID, rejects duplicates (same zone + normalized title → refuse, rc=3), writes atomically, reads back. If the tool is unavailable: **delay the write, never hand-bypass it**.
- **Human write protocol**: the human may create files directly (this freedom is by design). Wrong prefix / missing TID → the scanner flags it into the "📥 待整理" (needs-triage) section and **says so out loud** — never silently drops it. Format errors are visible at the filename level, by eye.

### The gray-band gate — the best story in this repo

The machine cannot judge *content* truthfulness, but it can judge *objective form*. A three-band gate (borrowed from voiceprint verification):

- form matches protocol → counts as a todo
- form suspicious (e.g. numbered+bold document formatting) → tag `suspected`, human eyes decide
- clearly not a todo → not collected

**Measured**: the first version guessed "suspicious content" via keywords — caught 3 of 6, missed 3. Switched to judging *form* — caught all 6 with zero false positives. **The machine manages objective form; truth goes to human eyes.**

### Settlement sentinel (two layers)

- **On-duty layer**: in-box self-report regex — a todo whose title contains completion semantics (and passes a negation window) gets flagged "probably done, pending settlement". Mark, never delete.
- **Shadow layer**: session-evidence matching (strong verbs + keyword-cluster matching), runs silently, writes only to its own file, never speaks. It must **prove incremental value over the on-duty layer via ablation** before being promoted — this prevents building the same wall twice.
- **Reconciliation uses strong signals only**: ① does the file path mentioned by a todo exist / was it modified ② exact external-archive name match ③ exact session-title containment. Semantic similarity is used **only to rank candidates, never to decide** — a false alarm costs more trust than a miss.

---

## Data flow

```
Write:    box_add (mint TID → create file → atomic write → read back)
          → triggers scan→dashboard refresh (fresh ledger, fresh board; refresh failure never blocks the write)
Scan:     todo_box_scan enumerates filenames → index JSON (.tmp → os.replace atomic write + file lock) → last_good fallback
Tidy:     box_tidy --auto (cron) → DONE archived to _done\month\ → whole-tree snapshot to _snapshots\ (9 kept) → weekly report
Dashboard:_make_dashboard (once daily) → _看板.html + todo_notify_summary.json (reminders computed exactly once)
Delivery: plugin injects a one-line summary on each session's first turn (reads the digest);
          index broken → degrade to last_good → still broken → visible warning, conversation continues
```

**Key invariants**:

- Index header carries `source_hash` (proves "which version of the ledger I read")
- Reminder timestamps are recorded **only after** a successful disk write (better to remind twice than to silently swallow one)
- Reminder state lives in its **own state file** (the scanner's state file is wholesale-replaced on write and would erase keys it doesn't own)
- Tools never live inside the box (double-clicking a file in the box always just opens text; the maintainer also patrols for stray executables and reports them — see pitfall 26)

---

## Module map — what's in this repo, what isn't

| Module | Status | Notes |
|---|---|---|
| `box_lock.py` | ✅ code | pure-generic file lock |
| `write_safe.py` | ✅ code | encoding-probe chain + backup-before-write |
| `todo_id.py` | ✅ code | TID minting (write-side `mint_fresh`, scan-side read-only) |
| `todo_notify.py` | ✅ code | cadence math, natural-day comparison |
| `box_add.py` | ✅ code | the single write entry point |
| `todo_box_scan.py` | ✅ code | scanner; keyword tables at module top, classification targets config-defined rhythms |
| `box_tidy.py` | ✅ code | maintainer; executable-inspection patrol |
| `box_config.py` | ✅ code (parameterized) | all paths via env/CLI; rhythm enum configurable |
| `_make_dashboard.py` | ✅ code | dashboard; skin attribution kept (MIT) |
| `box_selftest.py` | ✅ code | 190+ assertions (~80 run in the open-source config; the rest skip without private modules) — the self-test culture is a feature, not an afterthought |
| Settlement shadow layer | 📄 **pattern only** | depends on a **local semantic memory store** (vector/embedding retrieval over private session data) and the agent's session database — **private infrastructure, code not published**. The matching pipeline (path-stripping, quoted-verb stripping, cluster-count thresholds) is documented in `docs/settlement-shadow.md`. |
| Delivery plugin | 📄 **pattern only** | a **Hermes Agent plugin** (session-first-turn injection) — **Hermes-specific, code not published**. The delivery contract (one-line summary, degrade chain, visible-warning-on-total-failure) is documented in `docs/delivery-layer.md`. |
| Desktop kanban window | 📄 **pattern only** | Windows/pywebview/VBS launcher for the dashboard; a reference design, not shipped code (the dashboard HTML itself is generated by `_make_dashboard.py`) |

**Never published (data plane)**: the box folder itself, index/state/registry/snapshot files, logs, archives. **Patterns are publishable, code is reviewable, data never leaves home.**

---

## Pitfalls — 32 real ones, each one line

> Each row: the trap → the defense that guards it. This is the index that turns a 5,000-line review from needle-in-haystack into seat-number lookup.

| # | Trap | Defense |
|---|---|---|
| 1 | Line-protocol era: checkbox in the wrong position → whole line **silently dropped**; looked recorded, wasn't | Filename protocol: format errors visible by eye + flagged into "needs triage", out loud |
| 2 | Mixed GBK/UTF-8 ledgers read as utf-8/replace → Chinese text destroyed | `read_text_strict()` encoding-probe chain; unresolvable files **forbidden to write back** + backup-before-write |
| 3 | SKIP set written as relative paths → never matches → garbage enters the index | SKIP matches **bare names** (name dimension, not path dimension) |
| 4 | English sentences in review reports collide with skill names (`signed artifacts`→`artifacts`) → daily false positives | Reconciliation only processes "detail" entries + word-boundary matching for pure-ASCII names |
| 5 | Reminders compared by timestamp (19.5h < 24h) → "daily" becomes "every other day" | Compare by **natural day** |
| 6 | First launch: 29 legacy todos all "never reminded" → all marked as reminded (semantically wrong) | Initialize from ledger file dates |
| 7 | Reminder silently swallowed (timestamp recorded before the write; write crashed) | Order discipline: **write the HTML first, record the timestamp after** |
| 8 | Reminders computed in-conversation → multiple sessions per day = multiple reminders + inconsistent numbers | Computation in exactly one place (dashboard, once daily); plugins only read |
| 9 | Assertion passes on an empty set (0 == 0 = false positive) | Self-test assertions must guard against the empty set |
| 10 | Assertion criteria not updated with the protocol (entry-uniqueness check outdated) | Assertion criteria follow the protocol (one file = one entry) |
| 11 | Migration data source was a derived index → overwritten by the scanner → rerun reports unknown zones | Freeze an **immutable snapshot** as the migration source |
| 12 | Write side reused the scanner's similarity-based TID → new todo collides with an old TID | Write side has its own entry `mint_fresh` (mint-only, never reuse) |
| 13 | `.py`/`.pyw` double copies; only one edited → "works on CLI, broken on double-click" | Double-copy trap documented (or generate from a single source) |
| 14 | VBS `Run cmd, 0` hide-state inherited by the first window → ghost window (created, rendered, invisible) | Run's second argument = 1; EnumWindows diagnosis |
| 15 | Under pythonw, stdout is None — any print crashes | Force redirect at script start |
| 16 | venv uv-stub pythons spawn console subprocesses → an unkillable black window | System pythonw + PYTHONPATH pointing into the venv |
| 17 | Cron only accepts relative wrapper paths → the real script can't live on the data drive | Wrapper pattern: cron entry calls the real script; edit scripts without touching cron |
| 18 | Index half-written on power loss → JSONDecodeError → dashboard crashes | Atomic write (.tmp→os.replace) + last_good degradation |
| 19 | Silent cron failure nobody notices | Failures must be loud: dashboard failure writes an alert file inside the box (syncs to phone) |
| 20 | Keyword false positives (contains "wait" ≠ awaiting-owner) | Section/form signals take priority over keywords |
| 21 | The machine can't judge content truthfulness (keyword version missed half) | Gray-band gate: judge **form**, not content; truth goes to human eyes |
| 22 | Settlement evidence mis-attached: directory names in paths collide with title keywords (1 evidence → 3 todos) | Evidence triple-strip: strip file paths + strip quoted verbs + weak verbs gated by cluster count |
| 23 | Dedup-by-deleting-words killed partial matches ("board popup construction" vs "board popup landed") | The matching surface never deletes words; dedup happens only on the counting surface (interval clustering) |
| 24 | Claiming completion without an artifact (treating "the box is reminding me" as "delivered") | Settlement discipline: **done = artifact + receipt, not an impression in memory** |
| 25 | Trusting only the box reports stale accounts (source finished, box entry not cleared) | Scanning must reconcile two books: in-box reality + external sources; on conflict, check the source first |
| 26 | Executables sneaking into the box (accidental double-click risk) | Maintainer patrols for executables, reports only; tools never live inside the box |
| 27 | Reusing the scanner's state file → wholesale replacement erases keys it doesn't own | Reminder state in its **own state file** |
| 28 | "Never-remind" category entries getting nagged | The rhythm enum's special class → never enters reminders (hard rule) |
| 29 | Old box deleted/edited during migration (it's the archive of record) | Old box read-only sealed + conservation gate (TID bidirectional set-difference must be empty) |
| 30 | Re-minting TIDs during migration → one todo becomes two | TIDs follow the frozen manifest, never re-minted + migration report with bidirectional diff |
| 31 | Document prose and ledgers cohabiting (formatting read as todos: 9 false + 6 true buried) | `_resources\` zone for non-ledger documents; the test: "is this a *task* or *reference material*?" |
| 32 | Dashboard reads only the index; after a write, regenerating the dashboard alone is a no-op | Refresh order: **scan to rebuild the index first, then generate the dashboard** |

---

## License

Code: MIT. Docs: CC BY-SA 4.0. Dashboard skin: derived from dsh-priestess-skin (MIT, FriksD) — attribution preserved.

---

## Quick start

```bash
git clone https://github.com/runs-agents/todo.box.git
cd todo.box
python box_selftest.py        # bootstraps the box skeleton, runs ~80 assertions
```

The first run creates `box/` (the skeleton: five zones + inbox + `_done` + `_registry` + `_snapshots`) next to the code and runs the self-test. All green means the toolchain works on your machine.

Daily use:

```bash
python box_add.py "ship the launch article" --zone 03 --cls 现在就做   # write one todo (one file)
python todo_box_scan.py --check                                          # scan + reconcile
python _make_dashboard.py                                                # generate the kanban board (box/_看板.html)
python box_tidy.py --auto                                                # archive DONE, snapshot, patrol
```

Paths, the rhythm enum (5 items), and external reconciliation sources are all configurable via `TODOBOX_*` environment variables — see `box_config.py`.

> The self-test's git assertion expects at least one commit: if you copied the files without cloning, run `git init && git commit --allow-empty -m init` first.

---

## How this differs from Taskmaster / todo.txt

| | todo.txt | Taskmaster | **todo.box** |
|---|---|---|---|
| Storage | one text file, human-edited | database + MCP server + API keys | **a folder; one todo = one file** |
| Who writes | the human | the AI (editor-integrated) | **both — human may hand-write files, agent must use the writer** |
| Truth | the file | the database | **the folder — `ls` is the query** |
| Reminders | none (client apps add it) | AI-driven, in-editor | **computed once daily, one dashboard, everything else reads** |
| Failure mode | silent drops (line protocol) | service down = nothing works | **loud by design: alert files, visible warnings, last_good degrade** |

todo.box borrows todo.txt's philosophy (plain files, human-first, zero dependencies) and adds the agent-era layer: a stable TID per todo, a single writer entry point, a gray-band gate for what counts as a todo, and a delivery contract for injecting the box into the agent's context.

---

## Chinese by default

The rhythm enum, zone names, and code comments ship in Chinese by default. This is deliberate: the first audience is Chinese-speaking developers (the launch article will be in Chinese), and every Chinese token is configurable — `TODOBOX_RHYTHMS` (comma-separated, 5 items) swaps the whole vocabulary, and zone names derive from config. English localization is a docs-level task if demand appears.

---

*v2.2 — revised 2026-09-29. Reviewed by the human owner; second review by the external code reviewer. Data plane: zero bytes published.*
