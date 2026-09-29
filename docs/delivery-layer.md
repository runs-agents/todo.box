# Delivery Layer — pattern documentation

> **Status: pattern only.** The reference implementation is a Hermes Agent plugin (session-first-turn injection). The plugin code is Hermes-specific and not published; this document describes the delivery contract so you can adapt it to your own agent framework.

## The problem

The box is the source of truth, but the agent only sees what's injected into its context. If nothing is injected, the agent forgets the box exists — and so does the user, in any session that doesn't start with "check my todos".

The naive solution — replay the full todo list at the start of every session — fails in three ways: it floods the context, it re-reminds items that were already reminded today, and it produces a *second* computation of "what should be surfaced" that disagrees with the dashboard's numbers.

## The delivery contract

**One line, not a list.** The injection is a single summary line:

```
【todo.box】35 open · 🔔 0 due today (35 not yet due)
```

The full list lives in the dashboard (a derived HTML view the human can open any time). The agent's job on seeing the summary is to *know the box exists and how many accounts are open* — not to recite them.

**Read, never compute.** The summary line is read from `todo_notify_summary.json`, which is written by the dashboard generator (once daily). The plugin never recomputes "what's due" — computation lives in exactly one place (axiom 3). This is what prevents "dashboard says 3, plugin says 5".

**Session dedup.** The injection fires on the *first turn* of a session only, keyed by session id. Mid-session turns get nothing.

## The degrade chain

The delivery layer sits on derived artifacts, so it must survive their failure without lying:

1. **Summary file missing/stale** → degrade to a short fallback list (top items by rhythm priority), prefixed with a visible warning: "reminder digest unavailable (dashboard not generated?)".
2. **Index unreadable** → degrade to `last_good` index.
3. **Everything unreadable** → return a **visible failure banner** — "the todo system is unavailable, check the box folder and the daily scan cron" — and let the conversation continue.

The rule: **the delivery layer may degrade, but it may never fail silently.** A dead injection channel is the one failure mode nobody notices (see pitfall 19), because the symptom is precisely "nothing happens".

## Why first-turn injection (not a tool call)

A tool the agent must *choose* to call competes with the agent's attention; a first-turn injection makes the box part of the agent's ground truth. The tradeoff is context cost — which is why the injection is one line, and why the summary is computed elsewhere.

## What you need to build your own

- A hook in your agent framework that fires on session start (Hermes: a plugin with a session-first-turn injection point)
- Read access to `todo_notify_summary.json` + `todo_box_index.json` (+ `last_good`)
- The three-step degrade chain above, ending in a visible warning — never a silent empty string
