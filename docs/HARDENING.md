# amp — hardening work list

A hand-off document. Every item below is a defect or gap that was **measured in
this tree**, not inferred. Each carries: where it is, what is wrong, the work,
how to prove the work is done, and what it is expected to move on the ratings.

Read the two warnings first. They are the reason two earlier reviews of this
codebase produced wrong findings.

---

## 0. Before you start

### 0.1 Two claims that are FALSE. Do not repeat them.

- **"amp has no calibration loop."** False. `calibration()` at `amp.py:11726`
  is a four-way table — `confidence`→goal reached `done` in four reliability
  bands, `need`→`_moved_a_rung()`, `cost_usd`→`goal_spend()`,
  `headroom`→`_refine_record()` — deliberately kept apart so that
  over-confidence and under-costing cannot cancel into one flattering number.
  There is also full cost accounting (four budget tiers + `notional_spend_today()`)
  and worktree staleness handling (`worktree_refresh()`). **The real gap is much
  narrower and is item C1 below.**
- **"store.py has no lock."** False. `store.py:60` is
  `_LOCK = threading.RLock()` and every connection use is under it
  (`sqlite3.connect(..., check_same_thread=False, timeout=15)` at
  `store.py:104`). A grep for `threading.Lock()` returns 0 and means nothing.

### 0.2 State is bound per workspace

`.amp/.direction.json` **exists and is not the file the console reads.** The
live store is `.amp/ws/<current>/.direction.json`; the core file still holds
113 stale proposals. Same trap: `.amp/.chat.json` vs
`.amp/ws/<current>/.orchestrator.json`.

Seeding the wrong one produces "0 proposals" and `no proposal '<id>'`, which
looks exactly like a broken route. Also: `/api/lanes` reports `stage: None` for
a lane whose stage IS set — read `direction_view`'s `hold`, not the lanes payload.

### 0.3 Verification pattern that works here

- Copy the modules to `~/.cache/<name>/` and drive them with `AMP_HOME` pointed
  at a temp dir. **Never import from the live tree** — it writes `.pyc` and
  other sessions share it.
- Syntax-check with `ast.parse`, not `py_compile` (which writes `__pycache__`).
- `/tmp` is a tmpfs that fills to 0 bytes. Use `~/.cache`.
- **Prove each guard is load-bearing by reverting it.** The F3 guard was proved
  by patching its condition to `if False:` and watching the same test open a
  goal in a *frozen* lane. A test that passes both with and without the fix is
  not evidence.
- Prove ordering by **spying the calls**, not by asserting the file exists. The
  F2 fix was proved by asserting `os.fsync`/`os.replace` fired in the order
  `['fsync', 'replace', 'fsync']`.

---

## 1. Measured baseline (2026-07-31, post F1/F2/F3)

| | |
|---|---|
| lines | **37,281** over 8 files: `amp.py` 19,302 · `app.js` 7,651 · `server.py` 3,734 · `blueprint.py` 2,497 · `app.css` 2,374 · `index.html` 820 · `store.py` 546 · `preview.py` 357 |
| functions | **911** (`amp.py` 592 · `server.py` 175 · `blueprint.py` 86 · `store.py` 26 · `preview.py` 22 · `tools/` 9 · `checks/` 1) |
| type hints | already near-complete — params annotated: `blueprint.py` 98% · `server.py` 97% · `amp.py` 91% · `preview.py` 89% · `store.py` 87%. **Nothing checks them.** |
| tests | **1 file**, `checks/trvm_bench_published.py`, and it is a *lane bar* about a TRVM page, not a test of the harness |
| CI / lint / type config | **none** — no `pyproject.toml`, no `requirements.txt`, no `Makefile`, no `.github/` |
| dead code | ~0.9%. **Zero `except: pass` in 37K lines.** |
| inbound auth | **zero.** `HOST = "127.0.0.1"` (`server.py:43`) is the entire access control |
| shell execution | `shell=True` at `amp.py:1542`, `amp.py:6905`, `preview.py:223` |
| XSS discipline | `app.js` 205 `innerHTML` against 684 `esc(` calls — escaping is the habit |
| lane dependencies | **0 fields.** `depends_on` / `blocked_by` / `prereq` = 0 hits tree-wide |

### Ratings baseline

| axis | score |
|---|---|
| refusal discipline | 9.5 |
| context provenance | 9.5 |
| derived state | 9 |
| code hygiene | 9 |
| calibration | 7 |
| security | 3 |
| durability | 2 |
| concurrency | 2 |
| testing | 1 |
| **overall** | **~6.4 / 10** |

**Rating deltas below are judgment, not measurement.** The evidence in each
item is measured; the number attached to it is an estimate of how much a
reviewer would move that axis. Treat the ordering as more reliable than the
magnitudes.

---

## 2. The work, ranked

Ranked by rating movement per unit of effort. Items are independent unless a
dependency is stated.

---

### S1 — There is no inbound auth, and no Origin check, on a server that runs shell commands

**Rating: security 3 → 6.** Effort: small (~120 lines). **Do this first.**

**Where.** `server.py:43` (`HOST = "127.0.0.1"`), `server.py:3638`
(`ThreadingHTTPServer((a.host, port), Handler)`). Zero occurrences of an
`Origin`, `Referer` or `Host` header check anywhere in `server.py`. The only
`Authorization` header in the tree is **outbound** to OpenRouter.

**What is wrong.** Binding to loopback stops a *remote* attacker. It does not
stop a *page*. Any website the operator has open in the same browser can issue
a cross-origin `POST` to `http://127.0.0.1:8787/api/...`. A simple form post
needs no CORS preflight at all, and even a blocked-response `fetch(..., {mode:
'no-cors'})` still **executes on the server**; the attacker not reading the
reply is irrelevant when the request itself starts a worker. Downstream of
those routes are three `shell=True` call sites — `amp.py:1542` (running a
lane's `check` command), `amp.py:6905` (spawning a worker process group),
`preview.py:223` (starting a lane's dev server).

The harness is otherwise extremely careful about who is allowed to start work —
that is what the whole admission gate is for. This is the one door with no lock
on it.

**The work.**
1. Mint a random token at console start into `<STATE_ROOT>/.console.token`
   (mode `0600`, `.token` added to `store.SKIP_SUFFIX` so it never lands in the
   mirror — same trick the console lock uses).
2. Inject it into `index.html` on serve; have `app.js` send it as a header on
   every `/api` call.
3. Reject any `/api` request without the header, **and** any request whose
   `Origin` is present and not the console's own. Both, not either: the header
   defeats the form post, the Origin check defeats a token leak.
4. Keep `GET /` and static assets open — they are the delivery path for the
   token.
5. The CLI reads the token from the file, so nothing about `amp <cmd>` changes.

**Proof.** `curl -X POST` to any `/api` route without the header → `401`, and
the board is unchanged. A cross-origin form post → `403`. The browser console
still works end to end. Revert the check → the same curl succeeds, which is
what shows the guard was load-bearing.

**Do not** add a password, a login page, or user accounts. This is a
single-operator local tool; the requirement is "a web page cannot drive it",
not "identify the human".

---

### T1 — There is no test suite

**Rating: testing 1 → 5.** Effort: large, but splits cleanly. Highest absolute
point gain on the board.

**Where.** `checks/` holds exactly one file and it is a lane bar about a TRVM
page. 911 functions, no test that exercises any of them.

**What is wrong.** The ratings above say the same thing twice: the judgment
encoded in this code is excellent and there is no mechanism that keeps it true.
Relevant published result: **SpecBench (arXiv 2605.21384)** measures the gap
between what an agent-written codebase is specified to do and what it does,
and finds it grows roughly 27 percentage points per 10× LOC, reaching 100pp
above 25K LOC. This tree is 37K.

**The work.** Add `tests/` driven by pytest, each test pointing `AMP_HOME` at a
temp dir (see §0.3). Build it in this order — the order matters, because the
first three already have working ad-hoc batteries that just need to move into
the suite:

1. **The three fixed defects.** F1 (a second console on one state root exits 1
   naming the live pid/port; two consoles on *different* `AMP_HOME`s both run),
   F2 (spy `os.fsync`/`os.replace`, assert the order), F3 (the six-part battery
   already written — policy holds split from bar holds, frozen lane refused,
   bar hold adopted by hand without override, override writes `OVERRODE`,
   auto-adopt still filtered, `hold_is_policy` reaches the payload).
2. **The admission gate as a table.** `proposal_hold` has 8 refusals and
   `proposal_policy_hold` has 2. One row per refusal, each asserting the exact
   sentence, plus a row for the pass. This is the highest-value block in the
   suite: it is the function that decides whether work starts.
3. **`store.py` round-trip.** record → read back → history → the four verdicts
   of `verify()` (five, after F4). Include a file over `MAX_BYTES`.
4. **Derived state.** `lane_rungs()`, `worth()`, `calibration()` — given a
   fixed board, assert the exact derived numbers. These are pure functions of
   state and are cheap to pin.
5. **The HTTP surface.** One test per `/api` route: shape of the payload, and
   that a refusal is a refusal.

Target ~40 tests. Add `pyproject.toml` with the pytest config while you are
there (T2 needs it too).

**Proof.** `pytest -q` green from a clean checkout. And for each of the gate
tests, the revert check from §0.3 — a test of a guard that passes with the
guard removed is not testing the guard.

---

### D1 — `save_doctrine` still has the exact defect F2 fixed everywhere else

**Rating: durability 2 → 4** (with D2, → 5). Effort: tiny (~15 lines).

**Where.** `amp.py:14727-14729`:

```python
tmp = DOCTRINE_PATH.with_suffix(DOCTRINE_PATH.suffix + ".tmp")
tmp.write_text(text, encoding="utf-8")
tmp.replace(DOCTRINE_PATH)
```

Also `amp.py:18388` and `amp.py:18394` (report HTML + its JSON sidecar).

**What is wrong.** This is byte-for-byte the pattern F2 removed from
`save_json`: write, then rename, with no flush and no fsync. `os.replace` is
atomic *against another reader*; it is not a claim about power loss. The rename
is metadata that can land while the bytes have not, which yields a zero-length
file.

And of all the files in this tree, this is DOCTRINE.md — the harness's
constitution, injected into prompts. A truncated doctrine is not a lost file,
it is a silently different set of rules.

Note `_fsync_dir()` already exists at `amp.py:303` and `save_json` at
`amp.py:323` already does the right thing. This is not new machinery, it is
three call sites that were missed.

**The work.** Add `save_text(path, text)` next to `save_json` doing
open/write/flush/`os.fsync` → `os.replace` → `_fsync_dir` (best-effort;
directory fsync is POSIX-only and must never fail a write). Route
`save_doctrine` and both report writers through it. Then grep
`\.write_text\(|open\([^)]*["']w["']` and confirm the only survivors are inside
`save_json`/`save_text` and the console lock's `O_EXCL` create.

**Proof.** The fsync-order spy. And the grep, as a test — a new raw writer
added later should fail the suite.

---

### D2 — There is a complete history and no way to get back to it

**Rating: durability +1 (→ 5 with D1).** Effort: small (~80 lines).

**Where.** `store.py` keeps every prior version in `revisions` with a monotonic
`seq`. `load_json`'s die message now names `amp db history <rel>` and
`amp db show <rel> --seq <n>` (a rider that shipped with F2). There is no
`restore`.

**What is wrong.** The operator can *look at* the good version and must then
copy it out of a terminal by hand into a dotfile. Recovery exists but is not an
operation, so under the one condition it is for — a corrupt state file blocking
startup — it is a manual editing job against live state.

**The work.** `amp db restore <rel> [--seq N]` — defaults to the newest
revision whose body differs from what is on disk now, writes through
`save_json`/`save_text` (so the restore is itself durable), and **records the
restore as a note**, because a state file that changed underneath the board with
no trace is the thing this whole subsystem exists to prevent.

**Proof.** Truncate `board.json` to zero bytes, confirm the console dies with
the message naming the command, run the command, confirm it starts. Then a
crash-consistency loop: `kill -9` the console at a random point during a write,
in a loop, asserting every `load_json` on restart succeeds.

---

### C1 — Read-modify-write with no compare-and-swap

**Rating: concurrency 2 → 5.** Effort: medium (~150 lines).

**Where.** `save_json(` has 55 call sites: `amp.py` 47, `server.py` 6,
`blueprint.py` 2. The universal pattern is `load_json` → mutate the dict →
`save_json`. Nothing between the read and the write establishes that the file
did not change in between.

**What is wrong.** The F1 console lock stops a second *console*. It does not
stop the *CLI*: `amp <cmd>` invocations write the same state documents while a
console is running, and two writers to one document lose an update silently —
the later `save_json` writes a whole object built from a stale read. The
symptom is not corruption, it is a proposal or a note that quietly disappears,
which is exactly the failure that leaves no evidence.

**The author already solved this, in the substrate.**
`TRVM/forge/wrl_project.py` carries a per-project **exact-CAS** revision:
`save` refuses unless the on-disk revision equals the expected one, raising
`WRL_PROJECT_STALE`, then increments. The discipline exists; the harness
governing it did not get it. Same shape as F2, where `TRVM/forge/wrl_store.py`
already did temp→flush→fsync→replace→dir-fsync as a stated persistence law.

**The work.**
1. Add a `rev` integer to each state document, written by `save_json`.
2. `save_json(path, data, expect_rev=None)` — when `expect_rev` is given and
   the on-disk `rev` differs, **refuse** (do not merge; an auto-merge here would
   invent a state neither writer asked for).
3. Give the read side a `load_for_update(path)` returning `(data, rev)`, and
   convert call sites in order of blast radius: `board.json`,
   `.direction.json`, `.orchestrator.json` first.
4. On refusal, re-read and re-apply once, then surface the conflict. Do not
   loop forever.

**Proof.** Two processes each doing load → sleep → append → save against one
document: without CAS one append vanishes; with CAS the second is refused and
the retry lands both. Assert the *count*, not the absence of an exception.

---

### C2 — Wire `calibration()` into the scorer's context

**Rating: calibration 7 → 9.** Effort: small (~30 lines). **Best
value-per-line item on this list.**

**Where.** `calibration()` at `amp.py:11726`. It reaches `app.js:404`,
`app.js:2295`, `server.py:2542` — the UI, and nothing else. `grep calibration
blueprint.py` returns **0**.

**What is wrong.** The measurement was built and the actuator was never wired.
The scorer is never shown how its own past scores turned out. It produces a
`confidence` for every proposal and has no idea that (say) its 0.8-band
proposals reach `done` 40% of the time. Separately, `adopt_bar()`
(`amp.py:10896`) reads a hand-set config value — the bar that decides what runs
unattended has no connection to the measured reliability of the number it is
comparing against.

**The work.**
1. A `_calibration_block()` in `blueprint.py` rendering the four-way table as
   context: for each band, how many were scored there and how many reached
   `done`. Feed it into the scoring context builder.
2. Register it with `traced_block(...)` so it appears in the Blueprint
   inspector like every other block — provenance is observed here, not
   hand-drawn.
3. Say the sample size in the block. "3 of 4 in this band" and "300 of 400" are
   not the same evidence and the scorer must be able to tell them apart.

**Deliberately NOT in scope:** having `adopt_bar()` move on its own. A bar that
drifts from measurements the scorer is also reading is a feedback loop with no
damping, and the operator loses the one number they set by hand. Show the
measured reliability *next to* the configured bar in the UI and let the human
move it.

**Proof.** The Blueprint inspector shows the block in the real assembled bytes
for a scoring action. Then a before/after: score the same proposal set with the
block on and off and report the shift. If the shift is zero, say so — that is a
result, not a failure.

---

### T2 — 91–98% of parameters are annotated and nothing checks them

**Rating: code hygiene 9 → 9.5, testing +0.5.** Effort: small to set up, medium
to clear the fallout.

**Where.** Measured coverage: `blueprint.py` 98% of params / 93% of returns ·
`server.py` 97% / 89% · `amp.py` 91% / 85% · `preview.py` 89% / 31% ·
`store.py` 87% / 96%. No `pyproject.toml`, no mypy or pyright config anywhere.

**What is wrong.** The expensive half is already done. The annotations were
written by hand across 911 functions and are currently documentation only.

**The work.** `pyproject.toml` with mypy (or pyright) in non-strict mode:
`warn_unused_ignores`, `warn_redundant_casts`, `no_implicit_optional`. Fix the
fallout. **Do not** turn on `disallow_untyped_defs` in the first pass — it will
produce hundreds of findings about `preview.py` return types and bury the real
ones. Add ruff at the same time with a narrow rule set (`F`, `E9`) — the aim is
finding bugs, not restyling a codebase whose style is deliberate.

**Proof.** `mypy .` clean, wired into the same command as `pytest`.

---

### F4 — A verdict that promises a sweep will fix it, when no sweep ever will

**Rating: durability +0.5, derived state +0.5.** Effort: tiny (~10 lines).

**Where.** `store.py:287` and `store.py:292` — `_record` returns `False` for
anything over `MAX_BYTES` (16MB, `store.py:58`). `verify()` at `store.py:399`
reads every file **uncapped** (`store.py:412`) and files the result under
`missing`, whose docstring says "on disk, never mirrored".

**What is wrong.** A file over the cap is reported as `missing` forever. The
docstring for the neighbouring `stale` verdict says "a sweep fixes it", and the
operator will reasonably read the whole block that way and sweep repeatedly.
Nothing is broken; the report is unfalsifiable.

**The work.** A fifth verdict, `too_large`, routed through the same
`skipped()`-style path, naming the cap and the actual size. Cap the read in
`verify()` too — it currently reads a file into memory that `_record` refused
to read.

**Proof.** A 20MB file in the state dir appears under `too_large` with its
size, and `missing` is empty. Latent today; nothing is near the cap.

---

### F5 — The one field meant to detect a version mismatch always agrees

**Rating: derived state +0.5.** Effort: tiny (~10 lines).

**Where.** `store.py:152` writes `meta.schema` with `INSERT OR IGNORE` and it
is never read back. `status()` at `store.py:367` reports `"schema": SCHEMA` —
the **code's** constant.

**What is wrong.** The check compares the running code against itself, so it
can never disagree. Real schema evolution is done ad hoc via `PRAGMA
table_info` probes, which work — this field is the one that looks like it is
doing the job and is not.

**The work.** Read `meta.schema` back in `status()`, report both
(`schema_code`, `schema_db`), and refuse to open a database whose stored schema
is *newer* than the code's — an older one is a migration, a newer one is a
different program's file.

**Proof.** Hand-edit `meta.schema` to `SCHEMA + 1`, confirm the refusal names
both numbers.

---

### C3 — Held-out split per lane, reporting Δ

**Rating: calibration 9 → 9.5, testing +0.5.** Effort: medium. **Depends on
C2.**

**What is wrong.** After C2, the scorer sees its own past outcomes — which
means every calibration number afterwards is measured on data the scorer was
trained on. Without a held-out split, C2's improvement cannot be distinguished
from the scorer learning to reproduce the table it was shown.

**The work.** Per lane, hold out a fraction of scored-and-resolved proposals
from the context block. Report calibration on both halves and the Δ between
them. A large Δ is the finding.

**Proof.** The Δ is reported on the Blueprint or Specs tab with both sample
sizes. **If the held-out set is too small to say anything, report that instead
of a number** — this is the same discipline `calibration()` already applies by
counting open goals nowhere ("not evidence yet").

---

## 3. Optional — features, not ratings

These do not move any axis on the table. Listed because they came out of the
same sweep.

- **Lane dependencies do not exist.** `depends_on` / `blocked_by` / `prereq` =
  **0 hits tree-wide**. Every lane is schedulable the moment its bars clear.
  Whether that is a gap depends on whether lanes here are ever genuinely
  ordered; the Blueprint tab's layers are a rung judgment, not an ordering.
- **`preview.py` return-type annotations are 31%**, well below every other
  file. Worth a pass when T2 lands.

---

## 4. Suggested order, and where it lands

| # | item | axis | from → to | effort |
|---|---|---|---|---|
| 1 | S1 auth + Origin | security | 3 → 6 | small |
| 2 | D1 `save_doctrine` fsync | durability | 2 → 4 | tiny |
| 3 | C2 wire calibration | calibration | 7 → 9 | small |
| 4 | T1 test suite | testing | 1 → 5 | large |
| 5 | C1 exact-CAS | concurrency | 2 → 5 | medium |
| 6 | D2 `db restore` | durability | 4 → 5 | small |
| 7 | T2 mypy + ruff | code hygiene | 9 → 9.5 | small/med |
| 8 | F4 fifth verdict | durability/derived | +0.5 | tiny |
| 9 | F5 read schema back | derived state | 9 → 9.5 | tiny |
| 10 | C3 held-out split | calibration | 9 → 9.5 | medium |

**Projected overall: ~6.4 → ~7.9.** Items 1–3 alone are ~6.4 → ~7.1 for a
fraction of the effort of item 4.

The three axes that stay high without work — refusal discipline 9.5, context
provenance 9.5, derived state 9 — are the ones the existing design is actually
about. Nothing on this list should be allowed to lower them. In particular:
**do not add a second copy of a gate in order to make it easier to test.** The
F3 fix went the other way on purpose — the browser holds no copy of the lane
policy and arms its override only after the *server* has refused, because a
copy of the rule in JS is a second gate that can disagree with the one that
actually stops things.
