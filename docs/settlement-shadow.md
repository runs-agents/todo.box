# Settlement Shadow Layer — pattern documentation

> **Status: pattern only.** The production implementation depends on private infrastructure (a local semantic memory store with embedding retrieval, and the agent's session database). The code is not published; this document describes the architecture so you can build your own.

## The problem

Todos get done, but the files stay `OPEN_`. The box never lies about what's *recorded* — but nobody renames the file when the work actually completes in a conversation. You need a way to detect "this todo is probably already done" without a human re-reading every entry.

## Two layers, one promotion rule

**On-duty layer (in-box, published as part of the scanner):**
A self-report regex over todo *titles*: if the title contains completion semantics ("built", "landed", "wrapped up") **and** passes a negation window ("pending", "scheduled for later", "waiting until X stabilizes"), the scanner flags it as "probably done, pending settlement" in the index. It never deletes, never renames — marking only, human settles.

**Shadow layer (session-evidence, private):**
The on-duty layer only sees the box. The shadow layer looks at *conversation evidence*: did someone actually say this work is done? It runs silently (cron), writes only to its own JSON file, and **never speaks** — because its false-positive rate is unknown until measured.

**Promotion rule (the important part):** the shadow layer must prove **incremental value over the on-duty layer via ablation** before any of its output is allowed to reach a human. In the reference deployment, the first ablation showed the two layers catching exactly the same two items — zero increment — so the shadow layer stayed silent. This rule exists because building the same wall twice is invisible without a measurement.

## Evidence matching pipeline

For each candidate todo, evidence is gathered from session transcripts (in the reference deployment: an embedding-retrieval semantic store + the session database). Each candidate must survive:

1. **Path stripping** — file paths in evidence sentences are not topic content. A path like `D:/docs/a.jpg` contains the token `docs`, which collides with any todo whose title mentions "docs". Strip all path-shaped spans *before* keyword extraction. (Measured: one evidence line mis-attached to 3 todos before this strip existed.)
2. **Quoted-verb stripping** — 「已修」/『销账』 in *quotes* is a mention of the word, not a statement of completion. Diagnostic sentences like "『已修』 is a strong verb" were being read as completion evidence. Strip quoted verbs before matching.
3. **Weak verbs need clusters** — a single weak verb ("handled", "looked at") is not evidence. Require ≥2 *distinct keyword clusters* (overlapping sliding windows are grouped by interval clustering into clusters; the threshold counts clusters, not windows).
4. **The matching surface never deletes words** — dedup happens only on the counting surface. An early version deleted overlapping keyword windows from the match set, which killed legitimate partial matches ("board popup construction" vs "board popup landed"). Keep every window for matching; cluster only for counting.
5. **30-day window anchored on `first_seen`** — not `created`. Back-filled todos (migrated in) have old `created` dates but recent `first_seen`; anchoring on `created` silently excludes them from evidence eligibility.

## Ignoring false positives

Each shadow-layer suspect carries an evidence fingerprint. A human can mark a tid as "ignored" with its fingerprint; if the evidence *changes* (new line added), the fingerprint no longer matches and the suspect is re-reported. Ignoring is evidence-scoped, not todo-scoped — "ignore this exact report", not "ignore this todo forever".

## What you need to build your own

- A transcript/evidence source (anything queryable by time window)
- The five-step pipeline above (all pure regex + counting, no ML required)
- An ablation harness: run both layers over the same period, diff the hits, and only surface the delta
- A place to write suspects that nobody reads until the ablation justifies it

## Related pitfalls

See the main README, pitfalls 22–24: evidence mis-attachment, dedup killing partial matches, and "done = artifact + receipt, not an impression in memory".
