# amp — spec

**A lead-manager harness for the [&] stack.** One chat window dispatches coding
work to workers across many repositories, tracks them on a board, and escalates
to an architect model when a lane stalls.

This document states what amp *is* and the rules it enforces. The companion
[`docs/HARDENING.md`](../HARDENING.md) is the opposite document: every defect and
gap that was **measured in this tree**, with the work and how to prove it done.
Read that one before changing anything.

## Run it

```
./amp serve                       # browser console on http://127.0.0.1:8787
python3 code/server.py --port 8787
```

Stdlib only, no build step. It **runs locally and cannot be a static site**: it
shells out to the `claude` and `codex` CLIs (which need your local auth), reads
your git worktrees, and holds your OpenRouter key.

## The model

Three layers, and each one is a different kind of thing.

| Layer | What it is |
|---|---|
| **lane** | a named `(repo, path, backend)` triple — one per sub-repo |
| **worker** | `claude -p` in an isolated worktree (default), or `codex cloud exec` billed to a ChatGPT subscription |
| **consult** | an architect model reached with a packet built from a lane |

Three *models* do three different jobs and must not be conflated: a **worker**
writes the code, an **architect** answers when a worker escalates, and a
**supervisor** holds the whole workspace against its mission.

## State

State lives **outside** this repository so the program stays shareable and no
key, board, packet or ruling is ever committed. `AMP_HOME` overrides the
location; the default is `<workspace>/.amp/`.

Two things are deliberately *not* per-workspace: the **secrets** (an OpenRouter
key and a Claude worker token belong to the machine and the person, not to
whichever set of lanes is in front of you) and the **workspace registry**
itself. Everything else — board, chat, queue, findings, obligations, directions,
deploys, handoffs, reports, packets, rulings, worktrees, goals — is bound per
workspace by `_bind_state`. A SQLite mirror under the state *root* holds every
workspace in one file, which is why "back this up" names one thing.

## The rules that make this safe

These are invariants, not preferences. Each exists because the alternative was
tried or is obviously worse.

### 1. A worker never edits the shared checkout

Every claude worker runs in an isolated `amp/<lane>` git worktree. Its output
lands on a real branch.

### 2. `apply` refuses to merge

For a claude lane, `./amp apply` prints the worktree, the branch, and the
`git merge` command — and stops. Merging into the shared checkout is yours to
do, because **other sessions are editing that tree**.

### 3. A merge gate reads GitHub's answer, never the harness's own

`merge_blockers(pr, checks)` consults GitHub's `statusCheckRollup`, `state`,
`isDraft` and `mergeable` — and consults `publish_report` not at all. Our own
gate decides whether work may be *offered*; this one decides whether what the
other side said about it is good enough to act on. Mixing them would let the
harness's opinion of its work count toward accepting it. There is no override,
and `none` / `skipped` checks can never merge. After `gh pr merge` the code
**re-fetches**: exit 0 is the CLI's claim, and only `state == "MERGED"` is a
fact.

`merge_blockers` is one function with three callers (row, preflight, merge),
because a gate living in a request handler is a gate a second client walks
around.

### 4. Lane ownership is containment, deepest lane wins

`lane_owning()` matches by path containment, not exact equality. An exact match
meant a nested directory was owned by nobody, so nearly every lane read as
having nothing to publish.

### 5. Every worker is budget-capped

`DEFAULT_BUDGET_USD = 1.00` per worker turn, `DEFAULT_ORCH_BUDGET_USD = 3.00`
for an orchestrator turn, `DEFAULT_BRIEF_BUDGET_USD = 0.10` for a brief. All
three are config-overridable.

### 6. A prompt never carries a credential

The console mints its own inbound token per run. Prompts that curl the console
are rewritten by `with_console_auth` to add `$(cat <path>)` — the *path*, never
the token — because a prompt is sent to a model provider and written into the
worker transcript on disk, and a local credential should be in neither. Doing
this by rewriting the text rather than by hand is deliberate: the failure mode
of by-hand is one line added later without it, and that line is a 401 the model
will try to reason its way around.

### 7. Inbound auth stops a *page*, not just a remote attacker

Binding to loopback stops a remote attacker. It does not stop a browser page,
and a page is the thing to stop, because downstream of these routes are
`shell=True` call sites — a request that arrives is a command that runs. Three
checks apply in order (`Host`, then `Origin`, then the token), because they stop
three different attacks and the cheapest one stops the worst: `Host` is the only
defence against **DNS rebinding**, where the attacker's page is served from a
name whose DNS re-answers as `127.0.0.1`, making the page same-origin.

### 8. A retraction can never invent a claim

`lane_rungs()` takes the **maximum** rung ever recorded, so a claim recorded at a
rung it did not earn could never be walked back. `retract_rung` returns `None` if
the entry was never recorded, and never edits the review — the review is the only
record of *how* the mistake was made. A verdict whose consequence the harness
cannot actually perform is downgraded to `keep`. **Never clear the contradictions
gate by marking findings read**; it points at a hole in the machinery.

### 9. A trigger proposes, it never starts

Blueprint triggers write an unscored proposal into the ordinary queue, so every
existing bar still applies.

## The console

`server.py` serves a single-page console (`index.html` / `app.js` / `app.css`,
sibling files, no build). Tabs:

**Blockers · Dispatch · Goals · Direction · Specs · Log · Diff · Preview ·
Escalate · History · Rulings · Settings · Pull requests · Publish**, plus a
full-page **Blueprint** — how the lanes stack, what fires between them, and
exactly what each call is sent, block by block, with where each block came from.

Provenance in the Blueprint inspector is **observed, not hand-drawn**:
`traced_block(*reads)` records each block during a real build and the inspector
locates it in the finished bytes, so a block added later appears by itself. The
`empty[]` list is the payload — blocks that were built and produced nothing are
invisible in the prompt itself.

## The CLI

```
amp doctor                 preflight: worker CLIs, keys, lane bindings
amp lanes | board          list lanes; show the board
amp credits                OpenRouter credit remaining
amp serve                  launch the browser console
amp lane …                 manage lanes
amp dispatch <lane> …      send a task to a lane's worker
amp reply <lane> …         answer a claude worker mid-task (resumes its session)
amp poll                   refresh the board from Codex Cloud
amp diff <lane> …          show a task's diff
amp apply <lane> …         apply a task's diff (claude lanes: prints, does not merge)
amp packet <lane>          build an architect packet zip (manual forward)
amp ask <lane>             build a packet AND send it
amp login                  connect a long-lived Claude worker token
amp db …                   the SQLite mirror of everything on disk
```

## Module map

| File | What it holds |
|---|---|
| `amp.py` | the harness: lanes, workers, board, gates, escalation, goals, rungs |
| `server.py` | the local HTTP console — routing, inbound auth, static files |
| `store.py` | the SQLite mirror of everything under the state root |
| `blueprint.py` | how the lanes stack, what fires between them, what is sent |
| `preview.py` | serving what a lane built, so you can look at it |
| `index.html` · `app.js` · `app.css` | the console page |

## Known gaps

Tracked in [`docs/HARDENING.md`](../HARDENING.md), which is authoritative for
this section and re-verified against the tree rather than inferred. As of the
last pass all ten ranked items are closed, and two of them closed by *stating*
that their proof was not met rather than by meeting it: `mypy .` is not clean
(T2), and C3's held-out split has nothing settled in the live record to split,
so the honest reading is "not enough in either half". Read the DONE records,
not this line.
