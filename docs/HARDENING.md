# amp — hardening work list

A hand-off document. Every item below is a defect or gap that was **measured in
this tree**, not inferred. Each carries: where it is, what is wrong, the work,
how to prove the work is done, and what it is expected to move on the ratings.

Read the two warnings first. They are the reason two earlier reviews of this
codebase produced wrong findings.

---

## 0. Before you start

### 0.0 Re-verification, 2026-07-31 (second pass, nothing implemented yet)

Every one of the ten items was re-checked against the tree at `06da38b`. **All
ten are real and none has been started.** The baseline table in §1 reproduces
exactly (37,281 lines over the same 8 files). What did not survive the check:

| | |
|---|---|
| **Line numbers are stale** | The §1 baseline is post-F1/F2/F3 but several citations are pre-fix. `calibration()` is at **`amp.py:11791`**, not 11726. `app.js:2302`, not 2295. `server.py:2548`, not 2542. Grepping the printed numbers lands you ~65 lines short. Corrected in place below. |
| **SpecBench is misquoted, and cited for the wrong item** | See T1 and C3. The figure is 28pp, not 27; the "100pp above 25K LOC" sentence is **not in the paper**; and the paper is about reward hacking, which makes it an argument for **C3** and a *warning attached to* T1. |
| **D1's inventory is incomplete and ranked backwards** | There is a fourth raw writer, and the two report writers are a worse class than `save_doctrine`, not the same one. See D1. |
| **C2's "reaches the UI only" needs a footnote** | `calibration()` has three non-UI callers. What it does not reach is a *prompt*. The work is smaller than written. See C2. |
| **S1's remedy is missing the check that matters most** | The document's own grep noted `Host` had zero checks and then the remedy dropped it. Against DNS rebinding the token and the Origin check both come along for the ride. See S1. |

Confirmed unchanged and still true, so they do not need re-measuring: the
`server.py:43` / `:3638` bind and the **zero** `Origin`/`Referer` occurrences in
`server.py`; all three `shell=True` sites at the printed lines; the 55
`save_json` call sites split 47/6/2; `grep calibration blueprint.py` = 0;
`store.py:287`/`:292` (F4) and `store.py:152`/`:367` (F5); `amp db` having
`status backup verify prune export history show` and **no** `restore` (D2); no
`pyproject.toml`, `Makefile` or `.github/` (T2). Both claims in §0.1 are still
false claims — `store.py:60` is still `threading.RLock()`.

Three things were checked *because* they looked like findings and turned out not
to be. Do not open them:

- **`store.py` is already safe across processes.** `PRAGMA journal_mode=WAL`
  (`store.py:105`) plus `timeout=15` (`store.py:104`) is cross-process
  interlocking; the `threading.RLock` is only the in-process half. C1 is about
  the **JSON state documents**, not the mirror. Do not "fix" `store.py` under C1.
- **`PRAGMA synchronous=NORMAL` (`store.py:106`) is deliberate and correct.**
  It means a power cut can lose the newest mirror rows without corrupting the
  database — the mirror is *supposed* to be less durable than the state file it
  copies. Raising it to `FULL` would make every board write wait on the history.
- **No `GET` route mutates.** `do_GET` (`server.py:3139`) is reads only —
  `/api/spec/run` loads a recorded run, it does not start one; `do_spec_start`
  is `POST`. `/api/ruling` already defends traversal with `Path(name).name`.
  This narrows S1, but see S1 for why the checks must still cover `GET`.

### 0.1 Two claims that are FALSE. Do not repeat them.

- **"amp has no calibration loop."** False. `calibration()` at `amp.py:11791`
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

**Three additions, each of which cost a wrong result first.**

- **`AMP_HOME` does not isolate everything.** `DOCTRINE_PATH` is `ROOT`-relative,
  not `STATE_ROOT`-relative, so `set_doctrine()` under a temp `AMP_HOME` writes
  the **repository's real `DOCTRINE.md`**. It did, during D1; it was restored
  from git (it was clean, so `git checkout --` was exact) and the check re-run
  with `amp.DOCTRINE_PATH` reassigned. Before driving a function in a temp home,
  check whether the paths it touches actually hang off `STATE_ROOT`. `ROOT`-
  relative names are the exception and they are the ones outside the mirror,
  so git is the only way back.
- **Ask the process which port it bound; never ask the port which process it is.**
  `ss -ltnpH | grep "pid=$SPID,"`. Two S1 runs reported "no refusal at all"
  because a *different* server already held the port that was asked for — and
  `amp` deliberately walks to the next free port when one is taken (F1), so the
  port you asked for and the port you got are routinely different.
- **Assert that the revert patch matched.** `assert s != a` before writing.
  A `str.replace` that matches nothing is a silent no-op, and a no-op revert
  reports the guard as *not* load-bearing — which is the same broken-fixture
  failure that has now been hit in three separate projects in this repo.

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

### S1 — No inbound auth and no `Host` check, on a server that runs shell commands

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

**The second attack, which the first draft of this item missed.** A token and
an `Origin` check stop the cross-origin form post. Neither stops **DNS
rebinding**, and rebinding is the attack that developer tooling on loopback
actually loses to. The attacker serves a page from `evil.com` with a one-second
DNS TTL, then re-answers with `127.0.0.1`. The browser now believes `evil.com`
*is* this server, so the page is **same-origin**: it can read every response,
it can read the token straight out of the `index.html` it is allowed to fetch,
and it sends whatever `Origin` and `Sec-Fetch-Site` a legitimate console would.
Every defence in the original list is carried along by the attack.

The one header the attacker cannot change is `Host` — the browser sets it from
the name in the address bar, so it says `evil.com`. That is why a **`Host`
allow-list is the load-bearing check here**, not the token. The original grep
in §1 recorded that `Host` was unchecked and the remedy then only spent
`Origin`; that was the error.

This also promotes `GET`. Read-only routes are safe under the same-origin
policy — a cross-origin page cannot read `/api/state`. Under rebinding it can,
and `/api/state`, `/api/session` and `/api/ruling` are the whole board, the
worker transcripts and the architect's rulings. **The checks must cover `GET`,
not just the mutating verbs.**

**The work,** in the order the checks should be applied:

1. **`Host` allow-list, first and unconditional.** Reject any request whose
   `Host` is not `127.0.0.1:<port>` or `localhost:<port>`. This is ~6 lines and
   it is the only thing here that stops rebinding. Apply it to every request,
   including `GET /` — serving the page at all to a rebound origin is what
   hands over the token.
2. **`Sec-Fetch-Site`.** Browsers send fetch-metadata to loopback (it counts as
   a trustworthy origin) and page JavaScript cannot forge it. Accept
   `same-origin` and `none`; reject `cross-site` and `same-site`. This alone
   defeats the form post, with no token to mint, distribute or leak — Datasette,
   which is the same shape of tool, replaced its CSRF token with exactly this
   check. Fall back to the `Origin` comparison when the header is absent, for
   pre-2023 browsers and for `curl`.
3. **Then the token**, as written before: minted at console start into
   `<STATE_ROOT>/.console.token` (mode `0600`, `.token` in `store.SKIP_SUFFIX`
   so it never lands in the mirror — the trick the console lock already uses),
   injected into `index.html` on serve, sent by `app.js` as a header on every
   `/api` call. It is now the *third* line, not the first: it is what covers a
   non-browser client and an unknown user agent, and it is the layer rebinding
   defeats.
4. The CLI reads the token from the file, so nothing about `amp <cmd>` changes.

**Do not** add a password, a login page, or user accounts. This is a
single-operator local tool; the requirement is "a web page cannot drive it and
cannot read it", not "identify the human".

**Do not** bind to `0.0.0.0` to make any of this testable.

---

#### S1 — DONE, 2026-07-31. Measured.

Built as written, in the ruled order. `server.py` gained `_refuse(path)` — one
function, called first by both `do_GET` and `do_POST`, for the same reason
`merge_blockers` is one function with three callers: a gate each handler
re-implements is a gate a handler forgets. `POST` reads its body *before*
refusing, so a kept-alive connection is not left mid-JSON.

The token's file name and header name are `amp.py`'s
(`CONSOLE_TOKEN_FILE`, `TOKEN_HEADER`, `console_token_path()`), not
`server.py`'s: the server writes the file and the *prompts* tell a worker to
read it, and two spellings of one name is the arrangement where the reader and
the writer disagree about where the credential is and nothing says so.

Worker and orchestrator prompts get the header from `amp.with_console_auth()`,
a mechanical rewrite of `curl -s ` across the whole prompt rather than an edit
to each of the twenty-odd curl lines — the failure mode of the second is one
line added later without it, and that line is a 401 the model tries to reason
around. The header carries `$(cat <path>)`, **never the token**: a prompt goes
to a model provider and is written into the worker transcript on disk, and a
local credential belongs in neither.

**Measured** (isolated copy in `~/.cache/amp-s1`, `AMP_HOME` at a temp dir,
free port confirmed to belong to *our* PID — see the trap below):

| case | `Host: evil.com` | cross-site + token | `POST`, no token | console `GET` |
|---|---|---|---|---|
| all guards on | **403** | **403** | **401** | 200 |
| Host check reverted | 401 | 403 | 401 | 200 |
| fetch-metadata reverted | 403 | **200** | 401 | 200 |
| token check reverted | 403 | 403 | **404** | 200 |

Each guard moves exactly its own column and nothing else, which is what makes
each one load-bearing rather than decorative.

The last row of the table is not the proof that matters for `Host`, because a
`curl` with no token is refused either way. The proof is the **rebound
same-origin `GET`**: `Host: evil.com`, `Sec-Fetch-Site: same-origin`, no
`Origin` (browsers omit it on same-origin GET), and a **valid token** read out
of the `index.html` the page was entitled to fetch — exactly what DNS rebinding
buys an attacker.

| | rebound `/api/state` | rebound `/` |
|---|---|---|
| guards on | **403** | **403** |
| Host check reverted | **200** | **200** |

Also verified: the served page carries the real token and no `__AMP_TOKEN__`
placeholder; `index.html` opened from `file://` keeps the placeholder and every
`/api` call then correctly refuses.

**The preview server was closed too**, rather than left as a documented second
door. `preview.py`'s `_StaticHandler.send_head` — the one place `GET` and `HEAD`
both pass through — runs the same allow-list, built in `_start_static` *before*
the socket opens so there is no window with no list to consult. Measured: good
Host `200`, `Host: evil.com` `403`. A `command` preview is a foreign dev server
on its own socket; this process never sees its requests and cannot speak for it,
and the code says so.

**A verification trap worth the lines it costs.** The first three runs of this
proof reported "no refusal at all", twice, from two different causes: a
*different* server was already holding the port that was asked for, and then the
copy under test silently drifted because a restore did not take. Both present
identically — a clean-looking run of numbers that describe code that never ran.
Two rules came out of it, and both are now in the harness script:

- **Ask the process which port it bound, never ask the port which process it
  is.** `ss -ltnpH | grep "pid=$SPID,"`. A free-port helper is not enough,
  because `amp` deliberately walks to the next port when one is taken (F1), so
  the port you asked for and the port you got are routinely different.
- **Assert the patch matched.** Every revert is `assert s != a` before the write.
  A `str.replace` that matches nothing is a silent no-op, and a no-op revert
  reports the guard as not load-bearing — the same broken-fixture failure that
  has now been hit in three separate projects in this repo.

---

### T1 — There is no test suite

**Rating: testing 1 → 5.** Effort: large, but splits cleanly. Highest absolute
point gain on the board.

**Where.** `checks/` holds exactly one file and it is a lane bar about a TRVM
page. 911 functions, no test that exercises any of them.

**What is wrong.** The ratings above say the same thing twice: the judgment
encoded in this code is excellent and there is no mechanism that keeps it true.

**Correction to the citation this item used to carry.** The first draft cited
**SpecBench (arXiv 2605.21384)** as measuring "the gap between what an
agent-written codebase is specified to do and what it does … roughly 27
percentage points per 10× LOC, reaching 100pp above 25K LOC." Three errors:

- The figure is **28** points per tenfold increase in code size, not 27.
- **"Reaching 100pp above 25K LOC" is not in the paper.** It was an
  extrapolation off the slope, written down as if it were a measurement. In a
  document whose whole argument is that this codebase states things it has not
  checked, that is the wrong mistake to make.
- The paper does not measure specification drift. It measures **reward
  hacking**: the gap between an agent's pass rate on the *visible* validation
  suite and its pass rate on *held-out* tests that compose the same features.

Read correctly, the paper is not an argument for this item. It is an argument
for **C3**, and it is a **caution attached to this item**: a visible test suite
is exactly the proxy the paper watches agents saturate while the held-out gap
widens. The reason to build T1 is the ordinary one — 911 functions and no
mechanism that keeps the judgment true — and it does not need a citation. What
the citation does say is: **do not let the suite become the definition of
correct.** Every gate test below carries the §0.3 revert check for that reason.

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

#### T1 — DONE, 2026-07-31. Measured. **232 tests, not 40.**

`code/tests/`, eleven files, `232 passed in 59.13s`. The estimate was off by
almost six times, and the reason is worth writing down: the plan counted
*subjects* and the suite counts *refusals*. "The admission gate as a table" is
one line of plan and 47 collected tests, because `proposal_hold` has ten
distinct refusals and each of them has an exact sentence that a reader of the
console depends on.

| file | tests | block |
|---|---|---|
| `test_admission.py` | 47 | 2 — the gate, one row per refusal |
| `test_store.py` | 49 | 3 — round trip, history, the five verdicts, the failure paths |
| `test_http.py` | 44 | 5 — the S1 gate, then payload shape per route |
| `test_derived.py` | 26 | 4 — `worth()`, `lane_rungs()`, retraction |
| `test_state_locking.py` | 15 | 1 (C1) — cross-process, counted not caught |
| `test_console_lock.py` | 14 | 1 (F1) — one console per state directory |
| `test_db_restore.py` | 12 | D2 |
| `test_store_schema.py` | 10 | F5 |
| `test_calibration_block.py` | 5 | C2 |
| `test_durability.py` | 5 | 1 (F2) — `fsync` before `replace`, spied |
| `test_store_verify.py` | 5 | F4 |

**Six guards proved load-bearing**, each reverted in a fresh `copytree` under
`~/.cache/amp-revert/`, each with `assert patched != original`, and each
required to go red *for its stated reason* rather than merely to go red:

| guard reverted to | test that caught it | required marker |
|---|---|---|
| `_BOARD_LOCK = threading.Lock()` | `test_three_processes_all_land` | `an append is missing` |
| one shared `.tmp` name | `..._never_a_mixture_of_two_writers` | `the temp name is not per-writer` |
| `str(target).startswith(str(HERE))` | `test_confinement_is_a_path_test...` | `a sibling directory was served out of the tree` |
| a refusal naming no pid or port | `..._refuses_and_says_where_to_go` | `the refusal does not name the live console` |
| `path.unlink()` with no pid check | `..._does_not_remove_somebody_elses` | `a lock belonging to another console was deleted` |
| `os.open` without `O_EXCL` | `..._race_and_only_one_starts` | `both consoles started on one state root` |

**A defect found by writing block 5, and fixed:** `_static` confined static
serving with `str(target).startswith(str(HERE))`, which is true of any sibling
directory whose *name* merely begins with this one — `code-backup/` is served
out of `code/`. Now `target.is_relative_to(HERE)`. Worth noting how close this
came to being missed: the traversal tests already written (`/../amp.py`) fail
under both spellings, so none of them discriminated the bug. The test that
does had to be written on purpose, and it moves `server.HERE` into a tmpdir
rather than creating a probe directory beside the real one — a test that
writes into the repository to prove a point leaves the point lying there when
it dies.

**Two things the harness got wrong, both of them the same shape.**

- `test_store.py` first asserted `_FAILS["n"] == before + 1` after one failed
  `record`. It is 2: a cold `record` asks the database twice — once through
  `enabled()` for the settings, once to write — and each refusal is its own
  note. The guarantee is that a failure is never *silent*, not that it is
  counted once. Asserting the stronger thing was asserting something the code
  never promised.
- **`pyproject.toml` broke the revert prover on its first run.** `addopts`
  held `-q --no-header -p no:cacheprovider` — exactly what the §0.3 harnesses
  already pass — and pytest **adds** the two rather than noticing they agree.
  Two `-q` is `-qq`, which still runs every test and still prints the progress
  dots but **drops the final `232 passed in 59s` line**. The prover reads that
  line; it indexed `[-1]` into an empty list and took the whole proof down.
  Nothing about it looked like a test problem — the suite was green, the
  config was three obvious flags. Two corrections: `addopts` is now empty and
  says why, and the prover reports *"pytest reported no counts — it did not
  run"* instead of crashing. That second one is the §0.3 broken-fixture rule
  again, and this is the fourth time in this repo: **a harness that dies has
  said nothing about the guard it was pointed at.**

**The caution in this item still stands and is not discharged.** 232 visible
tests is exactly the proxy SpecBench watches agents saturate. Nothing here
measures the held-out gap; that is C3, and it is still open.

---

### D1 — Four writers never got the F2 discipline, and three of them are worse than F2

**Rating: durability 2 → 4** (with D2, → 5). Effort: tiny (~15 lines).

**Where.** `amp.py:14727-14729`:

```python
tmp = DOCTRINE_PATH.with_suffix(DOCTRINE_PATH.suffix + ".tmp")
tmp.write_text(text, encoding="utf-8")
tmp.replace(DOCTRINE_PATH)
```

**What is wrong.** This is byte-for-byte the pattern F2 removed from
`save_json`: write, then rename, with no flush and no fsync. `os.replace` is
atomic *against another reader*; it is not a claim about power loss. The rename
is metadata that can land while the bytes have not, which yields a zero-length
file.

And of all the files in this tree, this is DOCTRINE.md — the harness's
constitution, injected into prompts. A truncated doctrine is not a lost file,
it is a silently different set of rules.

Note `_fsync_dir()` already exists at `amp.py:303` and `save_json` at
`amp.py:323` already does the right thing. This is not new machinery, it is a
handful of call sites that were missed.

**The full inventory, and it is not three sites.** Grepping
`\.write_text\(|open\([^)]*["']w["']` across all five Python files returns six
hits. Two are correct (`amp.py:334` is inside `save_json`; `server.py:3600` is
the console lock's `O_EXCL` create). The other four are **two different
defects**, and the original ordering had them backwards:

| site | writes | pattern | exposure |
|---|---|---|---|
| `amp.py:14728` | `DOCTRINE.md` | tmp → rename, no fsync | **not durable** |
| `amp.py:6119` | `<lane>-<id>.md` in `RULING_DIR` | **direct write, no rename** | **not atomic** |
| `amp.py:18388` | report HTML | **direct write, no rename** | **not atomic** |
| `amp.py:18394` | report JSON sidecar | **direct write, no rename** | **not atomic** |

`amp.py:6119` was missed entirely. It writes the architect's rulings — the
record of what was decided and why, which is not reconstructible from anything
else in the tree.

**And the ranking inverts.** This box is `ext4` (checked). ext4 and btrfs both
carry the `auto_da_alloc` heuristic, which writes back the source file before
committing a rename when a program forgot the fsyncs. That heuristic covers
`save_doctrine` — it is a rename — so DOCTRINE.md is *partially* protected by
luck. It does **nothing** for the three sites that never rename at all: those
are a plain overwrite in place, so a crash mid-write leaves a reader a
truncated file *where a whole one used to be*, and there is no window in which
the old bytes are still there. That is worse than losing a write.

The sharpest case is `amp.py:18394`, the report's JSON sidecar. It is not a
by-product: `report_data_for` (`amp.py:18605`) `json.loads` it, `_report_facts`
renders it, and `_solver_context` (`amp.py:18567`) makes it the body of an
architect prompt. The comment above it says the solver reads *this* and not a
fresh survey, precisely so a report cannot be answered against a different day.
A half-written sidecar takes that guarantee out — and because the file is
overwritten in place, the failure is "report six can no longer be solved", with
nothing to fall back to. Fix the three no-rename sites **first**.

**Do not fold the heuristic into the argument.** `auto_da_alloc` is a
filesystem being helpful, not a property this code is allowed to rely on; it is
worth knowing only so the work is ordered right.

**The work.** Add `save_text(path, text)` next to `save_json` doing
open/write/flush/`os.fsync` → `os.replace` → `_fsync_dir` (best-effort;
directory fsync is POSIX-only and must never fail a write). The temp file must
be a sibling of the target, as `save_json` already does — a temp on another
filesystem turns `os.replace` into a non-atomic copy-and-delete and quietly
removes the guarantee. Route all four sites through it, the three no-rename
ones first. Then grep `\.write_text\(|open\([^)]*["']w["']` and confirm the
only survivors are inside `save_json`/`save_text` and the console lock's
`O_EXCL` create.

**Proof.** The fsync-order spy. And the grep, as a test — a new raw writer
added later should fail the suite. The grep test is the durable half: the
defect here is not that four sites are wrong, it is that nothing noticed three
of them for the length of the project.

---

#### D1 — DONE, 2026-07-31. Measured.

`save_text(path, text)` added at `amp.py:353`, and **`save_json` is now three
lines: a serializer and a call to it**. That was not in the plan and is the
better shape — "the durable way to write a file" had been a property of
`save_json` and therefore a property of *JSON*, which is not a distinction a
filesystem makes. There is now one write path, not two that have to be kept in
step.

All four sites routed through it, the three no-rename ones first:
`write_ruling` (`amp.py:6155`), the report HTML and its JSON sidecar
(`make_report`), then `set_doctrine`. The sidecar keeps `indent=1` and so goes
through `save_text` rather than `save_json`: re-indenting it would be a
formatting change arriving as a diff in every mirrored revision of a file the
solver reads.

The grep now returns **three** hits and all three are correct: `save_text`
itself, and the two `os.fdopen(fd, "w")` writes in `server.py` — the console
lock's `O_EXCL` create and the console token's `0600` create. Neither can be
`save_text`: the first must *fail* when the file exists, which is the whole
mechanism, and the second must be mode-restricted from the instant it exists.
Both already flush, fsync and fsync the directory.

**Tests: `tests/test_durability.py`, 5 passing.** This is the first test file in
the project.

| test | proven load-bearing by |
|---|---|
| `test_save_text_fsyncs_before_it_renames` | dropping the fsyncs in a copy → **fails**, on the ordering assertion |
| `test_no_new_raw_writers` | reintroducing `DOCTRINE_PATH.write_text(text)` in a copy → **fails**, naming `amp.py:14781` |
| `test_the_grep_can_fail` | the regex asserted against known-bad and known-good lines, so the guard cannot quietly stop matching |
| `test_save_text_temp_file_is_a_sibling` | a cross-filesystem temp turns `replace` into copy-and-delete |
| `test_save_json_is_save_text` | keeps the two paths from forking again |

The exemption list is keyed on **line content, not line number**. Line numbers
move, and an exemption that drifts onto a different line is an exemption granted
to a write nobody approved.

End to end against a temp state directory: ruling written and mirrored, doctrine
written, read back byte-identical and mirrored, the empty/marker-less refusals
still refuse, no `.tmp` left behind.

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

#### D2 — DONE, 2026-07-31. Measured, and one design decision was reversed mid-build.

`store.restorable(path, seq=None)` chooses and explains; `amp._db_restore`
writes. The split is deliberate: the write belongs to `save_text`, so the
restore is atomic, fsynced and mirrored like every other write in the program. A
recovery less durable than an ordinary write can leave you worse off than the
corruption did.

**The default is the newest revision that DIFFERS from disk**, not simply the
newest. The newest is usually the corruption — the mirror copied it faithfully.
"Give me back the last version that was not this" is the request an operator
actually has, and making them work out a seq for it at the exact moment their
console will not start is the gap being closed.

`load_json`'s die message now names `amp db restore <rel>` first.

**The reversal.** The first build refused to overwrite bytes the mirror had
never seen, behind a `--force`. Two tests failed and they were right to: a
truncated `board.json` nobody has swept yet *is* unseen bytes, so the one case
the command exists for was the one case that needed `--force` — which is the
same as not having the command. The rule is "do not lose what is on disk", and
the honest way to obey it is to **keep** it: the restore now mirrors the current
bytes first, then writes. `--force` is gone. The restore is its own undo, and
the run says so on stdout. It still refuses in one case — bytes the mirror
*cannot* hold (`.secrets.json`, over the cap, mirror off) — because there the
refusal is the only thing standing between the operator and a real loss.

`tests/test_db_restore.py`, 12 tests. The one that carries the file is
`a_truncated_board_is_recoverable`: write a board, mirror it, truncate it,
assert `load_json` dies, restore, assert it loads and the contents are the ones
that were there.

Both defaults proved load-bearing by reverting each in `~/.cache/amp-d2/`:

| revert | fails |
|---|---|
| default = `rows[0]` (newest, not newest-that-differs) | 4 of 12 |
| do not `store.record` before overwriting | 4 of 12, including both truncated-board tests |

**Live, on the real CLI**, against a temp `AMP_HOME`:

```
$ python -c "import amp; amp.board()"
amp: .board.json is not valid JSON: Expecting value: line 1 column 1 (char 0)
      every earlier version of it is in the mirror:
        amp db restore .board.json      # the newest one that differs
$ python amp.py db restore .board.json
restored .board.json from revision 1 (newest revision that differs from disk)
  written 2026-07-31T18:58:23+00:00, 106 bytes, replacing 0 bytes on disk
  the bytes that were on disk were not in the mirror; they are now, and can be restored back
$ python -c "import amp,json; print(json.dumps(amp.board()))"
{"tasks": {"code": [{"state": "done", "task_id": "t1"}]}}
```

**The crash-consistency loop was run, and it is the strongest number here.** A
child writes the board in a loop; the parent `SIGKILL`s it at a random point
inside the write window; the parent then parses the file. 120 rounds:

| build | result |
|---|---|
| `save_text` as shipped (D1) | **120 kills, 0 unreadable**, 7 distinct sizes, 1 leftover `.tmp` |
| `save_text` reverted to `open(path,"w")` | **4 of 40 unreadable** — two 0-byte files and two torn at exactly 16384 bytes, the buffer boundary |

The leftover `.tmp` matters: it is proof the kill landed between the write and
the rename at least once, which is the case that is *supposed* to leave the real
file untouched. Without it the run would only have shown that the timing never
hit the window. The 16384-byte tears in the reverted build are what an
un-fsynced buffered write looks like from the outside.

Also closed here: `amp db verify` was still printing four verdicts after F4
added a fifth. It now prints `too_large` last and in its own words, because the
two lines above it mean "sweep again" and this one means "sweeping will not
help".

---

### C1 — Read-modify-write with no compare-and-swap

**Rating: concurrency 2 → 5.** Effort: medium (~150 lines).

**Where.** `save_json(` has 55 call sites: `amp.py` 47, `server.py` 6,
`blueprint.py` 2 (all three counts re-checked). The universal pattern is
`load_json` → mutate the dict → `save_json`. Nothing between the read and the
write establishes that the file did not change in between.

**Scope, so this does not get spent in the wrong file.** This item is about the
**JSON state documents**. `store.py` is already correct across processes:
`PRAGMA journal_mode=WAL` (`store.py:105`) with `timeout=15` (`store.py:104`)
is SQLite's own cross-process interlock, and the `threading.RLock` at
`store.py:60` is the in-process half of the same thing. The mirror is not the
problem. The problem is that the mirror faithfully records both halves of a
lost update.

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

#### C1 — DONE, 2026-07-31. Measured, and **the plan above was denied.**

**The diagnosis was right and the prescription was wrong.** The plan asked for a
`rev` integer, `save_json(..., expect_rev=)`, `load_for_update`, and a
retry-once loop across 55 call sites. It was not built. What was built is
cross-process **exclusion**: `_file_lock` / `_DocLock` in `amp.py`, and four
one-line changes.

The reason is in the code the plan was written about. Every read-modify-write on
these four documents is *already inside* a `with _BOARD_LOCK:` — the file
already declares them critical sections, and the only defect was that
`threading.Lock` is half an interlock: real against the console's own worker
threads, absent against `amp <cmd>` on a terminal. CAS answers "did this change
under me", which is an editor's question and needs a retry loop and a merge
policy before a caller can act on it. Exclusion answers "nobody else is in
here", which is stronger, needs no retry, and cannot be forgotten at a call
site. And CAS *without* a lock still has a window between the compare and the
`os.replace`, so it could not honestly have been called exact-CAS — the
substrate's `wrl_project.py` gets away with it because a project document has
one writer by construction, which is not true here.

`_DocLock` holds the **name** of the path, not the path. `_bind_state` rebinds
every state path when the workspace switches; a lock that captured the `Path` at
import would go on excluding writers to the workspace you left while the writes
went to the one you are in — which is worse than no lock, because it looks like
it is working. `test_the_lock_follows_a_workspace_switch` is that.

`_file_lock` is re-entrant on purpose. `flock` is per open file description, so
a second `flock` on a second descriptor blocks **inside one process**; the
`RLock` plus an fd/depth refcount is what makes nesting safe and what stops an
inner frame unlocking the outer one's file on the way past. The wait is polled
at 20 Hz rather than blocking, because a blocking `flock` cannot be given a
deadline without signals, and it ends in `Busy` naming the file rather than in a
hang.

**Four writers a lock alone would not have fixed.** `cmd_poll` and
`server.do_poll` each held `b = board()` *across* the codex network calls and
wrote that whole board back — so every record the console wrote while the poll
waited was deleted by a poll that succeeded. The read now happens after the
network, under the lock, touching only the polled lanes (`record_remote`).
`cmd_codex_submit` and `server._codex_submit` each carried a private, unlocked
copy of `record_task`'s body; both now call the one definition. Two unlocked
`.direction.json` writers were also found — the explore stamp, and the
`settle_tried` check-then-act, where "have we already asked this" and "write
down that we asked" have to be one step or the paid call happens twice.

**A second defect, found by the revert and not by reading.** With the lock put
back to `threading.Lock`, the run went red — but on `FileNotFoundError`, not on
the count. `save_text` named its temp file `<name>.tmp`: **one name shared by
every writer of a document**, which is safe for exactly as long as there is only
ever one. Two of them open that file, interleave bytes into it, and both rename
it. The crash is the *lucky* ordering; the quiet one publishes a mixture of two
writes that is neither of them and may still parse. D1 made the write durable,
and durability is a property of one writer. The temp name is now per pid and
thread, with an unlink on the failure path because a unique name is litter where
a shared one was self-cleaning. **`config.json` has 12 `save_json` writers
(`amp.py` 9, `server.py` 3) and no lock**, so this one was live in the tree
today.

`tests/test_state_locking.py`, 15 tests; the suite is **52 passed**. The test
that carries the file is `test_three_processes_all_land` — three processes, 40
appends each, and the file has to hold 120. The 4 ms sleep sits *inside* the
critical section, so a working lock hides it entirely and a broken one makes the
loss certain: a test that fails sometimes is a test nobody believes.

Both guards proved load-bearing by reverting each in `~/.cache/amp-revert/`,
each from a fresh copy, each with `assert patched != original` so a typo cannot
produce a false green:

| revert | result |
|---|---|
| `_BOARD_LOCK = threading.Lock()` | red **on the count** — `{'l1': 40, 'l2': 1}`: 79 of 120 appends gone, nothing raised |
| `tmp = path.with_suffix(path.suffix + ".tmp")` | 2 red — the document comes back a mixture of two writers |

The lost-update line is the whole argument for this item: 79 records vanished,
exit status 0 on all three processes, no exception, no bad bytes, nothing in any
log. That is why the assertion is on the count.

**Not done, named.** `config.json` is out of scope here and is the next
conversion — it has no `_CONFIG_LOCK`, 12 writers, and is the highest-frequency
CLI-vs-console collision in the program. It is now safe against *torn* writes
(above) and still exposed to *lost* ones.

---

### C2 — Wire `calibration()` into the scorer's context

**Rating: calibration 7 → 9.** Effort: small (~30 lines). **Best
value-per-line item on this list.**

**Where.** `calibration()` at **`amp.py:11791`** (not 11726 — that number was
taken before the F1/F2/F3 fixes shifted the file). `grep calibration
blueprint.py` returns **0**, and that is the finding.

**Correction: "the UI and nothing else" is not true.** `calibration()` has five
callers, three of them outside the UI:

- `amp.py:16168` — `direction_view`, the console panel. UI.
- `server.py:2548` — `state_payload`. UI.
- `amp.py:16856` — **`report_data`**. The table is written into every report.
- `amp.py:17148` — **`report_actions`**, which *reasons over it*: it divides
  actual spend by stated spend and, past 1.5× or under 0.67×, raises
  "re-anchor the cost estimates" with the sample size attached.
- `app.js:404` / `app.js:2302` — rendering.

So the measurement is not inert. It already reaches the operator, and it
already drives generated advice.

**What is actually wrong, stated precisely.** Calibration reaches every surface
*except a prompt*. `report_data` puts it in the report; `_report_facts`
(`amp.py:18482`) renders that report into the architect's solver context; and
`_report_facts` **drops it**. Meanwhile the same prompt, forty lines later in
`_solver_context` (`amp.py:18567`), prints a block headed *"The dials that
currently decide what gets started"* listing `adopt_bar()`, `need_bar()`, the
sharpen floors and the escalation limits — with the comment that a model asked
to change a bar without being told what it is will invent one.

That is the defect in one sentence: **the prompt states the bar and withholds
the measured reliability of the number being compared against it.** The model
is told the threshold is 0.60 and is not told that the 0.8 band reaches `done`
40% of the time. By this document's own comment, that is the condition under
which a model invents the missing half.

**The work is therefore a render, not a pipeline.** The number is already in
the dict the prompt is built from.

1. Render the four-way table in `_report_facts`, next to where the dials block
   is assembled — not a new data path, a section that stops being skipped.
2. A `_calibration_block()` in `blueprint.py` for the **scoring** context,
   which is the one that never sees a report at all. This is the genuinely new
   half.
3. Register it with `traced_block(...)` so it appears in the Blueprint
   inspector like every other block — provenance is observed here, not
   hand-drawn. A block that produces nothing shows up in the inspector's
   `empty[]` list, which is how you will find out the table was empty rather
   than unwired.
4. Say the sample size in the block. "3 of 4 in this band" and "300 of 400" are
   not the same evidence and the scorer must be able to tell them apart —
   `report_actions` already does this, and the block should match it rather
   than invent a second format.

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

#### C2 — DONE, 2026-07-31. Measured, with one deviation and one thing not run.

`calibration_block(cal=None)` added next to `calibration()` (`amp.py:11908`),
decorated `@traced_block(...)` naming the three files it derives from. Two
callers:

- `_report_facts` — renders it from **the report's own `d["calibration"]`**, not
  a fresh `calibration()` call. That is the whole point of the sidecar: a report
  is solved against the day it was taken, and re-deriving here would answer
  about today under an older report's heading.
- `_explore_context` — the genuinely new half. This is the call that *produces*
  `confidence`, `need` and `cost_usd`, and it was the one surface the
  measurement of those three had never reached.

**Deviation from the plan, deliberate.** The plan said put the block in
`blueprint.py`. It is in `amp.py` instead. `blueprint.py` builds the Blueprint
tab's own drafting contexts; the scoring prompt is `_explore_context` and it
lives in `amp.py`. Putting the function beside the data it renders, and one
import away from both callers, is the version with one definition. The
substance of the plan — "the scoring context, which never sees a report" — is
what was built.

**Measured**, against a temp state directory holding one adopted proposal, its
finished goal, its review and its billed board row, so `calibration()` returns
real numbers rather than a fixture:

- `calibration_block` is built while `_explore_context` runs, and its exact bytes
  are present in the assembled prompt (char 5117 of 10256).
- With nothing measured it returns `""` and is recorded by `traced_block` with
  `len 0` — which is precisely what the inspector's `empty[]` list keys on, so
  "the table was empty" stays distinguishable from "the block was unwired".
- Every line carries its `n`, in `report_actions`' wording rather than a second
  format for the same numbers: *"over 7 judged"*, *"over 3 finished
  objective(s)"*, *"2 of 4 finished"*. A band nobody scored into is not printed.
- The block ends by saying to read the numbers **as a correction, not as a
  target** — a scorer handed "43% actually finished" will otherwise start
  answering 0.43.

**Tests: `tests/test_calibration_block.py`, 5 passing.** Proven load-bearing:
replacing the two lines that append the block in `_explore_context` with
`cal = ""` in a copy → `test_it_reaches_the_scoring_prompt` **fails**, on the
assertion that the block was built at all.

**Not run: the before/after shift.** Scoring the same proposal set twice
requires real architect calls against OpenRouter, i.e. money, and this is a
zero-budget project. What is proven is the deterministic half — the bytes are in
the prompt. The behavioural half is unmeasured and is not claimed. Do not write
it up as an improvement to scoring until somebody has actually run it.

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

#### T2 — DONE, 2026-07-31. Measured. **ruff is clean; `mypy .` is NOT, and that is the honest result.**

**ruff: 137 → 0.** "All checks passed!" `select = ["F", "E9"]`, `ignore = ["F541"]`,
`"amp.py" = ["F821"]`. Every one of those three decisions is argued in
`pyproject.toml` next to the line that makes it, because each is a rule being
given up and the reason has to survive the person who turned it off.

**mypy: 472 → 169.** The item's stated proof is `mypy .` clean. It is not clean,
and no amount of further work in this item would have made it clean without
`# type: ignore` comments that assert rather than check. What the number means is
below.

**Four edits did all 303.**

| edit | mypy | why it is that large |
|---|---|---|
| 28-line declaration block above `_bind_state` | 472 → 354 | 106 `name-defined`. `_bind_state` binds 28 workspace paths by writing into `globals()`, so no tool and no `grep` could see they exist |
| `def die(...) -> NoReturn` | 354 → 301 | a single annotation. Without it `if not c: die(...)` **narrows nothing**, so every line after every guard in the file was checked as though the guard had not run |
| the `require_*` family (below) | 301 → 170 | 131 — one defect, thirty-four sites |
| the `ON_GOAL_DISPATCH` guard (below) | 170 → 169 | 1 |

The `NoReturn` line is the one worth remembering. `die` is how nearly every
refusal in `amp.py` ends; one word of annotation on it was worth more than any
other change made under this item, and until it was there the tool's output was
mostly noise **generated by its own ignorance of the guards that were already
correct**.

**What T2 actually found, which was not a typing problem.** The 114 `[index]`
errors were not dynamic-typing noise. They were one defect, replicated across
three document stores:

`load_consult`, `load_goal` and `load_specrun` are each `-> dict | None`. Each is
read at the start of a long operation, and read **again** at the end to write the
answer down. Between the two reads is an architect call, a shell check or a whole
worker — minutes, not milliseconds. All three resolve through a module global
(`CONSULT_DIR`, `GOAL_DIR`, `SPECRUN_DIR`) that `_bind_state` **rebinds when the
operator switches workspace**. So the second read can miss a file the first read
found, and every second read was unguarded.

What that cost was `TypeError: 'NoneType' object is not subscriptable`, thrown
from whichever line happened to index the result — a line with nothing to do with
the cause. For a consult it also cost the architect's answer, which at that moment
existed nowhere but in a local variable and had already been paid for.

**The fix is two halves and both are needed.**

- `require_consult` / `require_goal` / `require_specrun` — `load_*` for the reader
  that is about to index it. **34 call sites converted**, each one chosen because
  it indexes unguarded *and* straddles a long call. The refusal names the id and
  the directory, because the directory is the thing that moved and a message with
  only the id sends the operator to look in a folder the process is no longer
  reading.
- `switch_blocked` now calls `consults_in_flight()`. The `_CONSULT_INFLIGHT`
  registry and its `try/finally` already existed **and had no consumer** — the
  guard whose entire job is to refuse while something is in flight counted live
  workers and a busy orchestrator and stopped there, and a consult is neither. The
  first half is now a backstop rather than the plan.

`load_*` is untouched, deliberately. Plenty of callers say `load_goal(gid) or {}`
and mean it — a missing document is an ordinary answer there. Converting `load_*`
itself would have turned every one of those into a refusal.

**A consequence of `Died` that had to be chased down.** `Died` subclasses
`SystemExit`, which is a `BaseException`, so `except Exception` does not catch it
— and `threading.excepthook` **discards a `SystemExit` without printing anything**.
At the converted sites a `TypeError` used to be caught by a broad handler in a
background thread; a `Died` is not. In `_run_claude_bg` that costs two things: the
record the handler exists to rescue stays on `running` forever, and `_drain_queue`
never runs, so the slot the worker just freed is never handed to whoever was
queued for it. All three handlers there now name `(Exception, SystemExit)` — the
same spelling already used at `server.py:479`.

**A second defect of the same shape, found by the same tool and fixed:**
`ON_GOAL_DISPATCH` is `None` until the console sets it, and `goal_dispatch` called
it without asking. Run `amp` as a command rather than under the console and that
is `TypeError: 'NoneType' object is not callable`, reported to the operator as a
fault in the goal. It now says *"nothing is wired to start workers"* and goes down
the existing failure path, because the task above it is already marked `running`
and that path is the one that puts it back.

**Tests: 232 → 250.** `test_consult_switch.py` (4) pins both halves of the switch
fix — the in-flight check is made **from inside** the architect call, since a
`try/finally` wrapping the wrong statement would leave the registry correct before
and after and that is the only moment the answer can be wrong.
`test_require_documents.py` (12, parametrised over all three stores) pins the
guard, and its third case performs a **real `use_workspace` switch between the two
reads** rather than rebinding the globals by hand — a test that pokes `globals()`
would still pass if the switch stopped calling `_bind_state` at all.

**What those tests do not prove**, said plainly because the count looks stronger
than it is: `test_require_documents.py` tests the *guard*, not the 34 *call sites*.
Reverting `require_goal` to `load_goal` would take it red, but tautologically — it
calls the function directly. Only `test_consult_switch.py` reaches a converted
site through the operation that contains it (`advance_consult`). The other 33 are
argued from reading, not from a red test, and no revert scenario was added to
`~/.cache/amp-revert/proof.py` because a scenario that cannot fail for the right
reason is not a proof.

**What the remaining 169 are.** Read, not counted. They are not one thing, and
none of them is a second F6:

- `[index]` 51 — the same `dict | None` shape at sites where a missing document
  **is** handled, by `or {}` or by an `if not g: return`. mypy cannot see the
  guard through the truthiness test. These are correct code.
- `[arg-type]` 34, `[operator]` 20, `[union-attr]` 16 — mostly `Any` arriving from
  `json.loads` and being narrowed by a runtime check mypy does not follow.
- `[var-annotated]` 12 — empty-collection literals, each fixable by one annotation.
- `[annotation-unchecked]` 8 — notes, not errors: bodies of untyped functions.

Two are worth a look by whoever picks this up: `store.py:282-284` (an `int | None`
counter that is added to and then assigned a `str`) and `amp.py:10761` (iterating
a `Popen.stdout` that is typed `IO[Any] | None`). Neither has produced a report.

**A new finding, not fixed, because it is durability and not typing.**
`orch_busy()` is derived from a persisted `status == "running"` on the turn record,
and `_run_orchestrator_bg` catches, prints, and **never clears it**. Any exception
out of `orchestrator_ask` — not just a `Died` — leaves the orchestrator busy
forever, with nothing on screen saying why. Same shape as F1 and F2. It predates
everything in this item and belongs with them, not here.

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

#### F4 — DONE, 2026-07-31. Measured.

`store.verify()` now asks `stat` for the size **before** reading, appends
`{"path", "bytes", "cap"}` to a fifth list and `continue`s. Oversized paths are
excluded from `held` as well (`over = {t["path"] for t in too_large}`) — a path
in two lists reads as two problems when it is one.

`clean` is deliberately **unchanged**: `not stale and not missing`. Clean means
"a sweep has nothing left to do", and a sweep has nothing it *can* do about
these. Reporting the mirror as permanently dirty over a file it was designed to
refuse is the same defect wearing the other colour — it teaches the operator to
ignore the flag.

Surfaced in `app.js:dbVerify` as its own row, `too large to copy`, with the size
next to the cap in MB, and the success sentence switches from *The copy is
current* to *Everything the copy can hold is current* when the list is
non-empty. Two facts in one sentence, because the first sentence alone reads as
"everything is copied" and these files are not.

`tests/test_store_verify.py`, 5 tests:

| test | asserts |
|---|---|
| `a_file_over_the_cap_is_named_with_its_size` | `bytes` and `cap` are both there — "too large" with no number is a verdict the reader has to go and check |
| `it_is_not_reported_as_missing` | `missing == []`, the defect itself |
| `it_is_not_reported_as_held` | it is on disk; it is the mirror that cannot hold it |
| `a_file_under_the_cap_is_untouched_by_any_of_this` | no behaviour change below the cap |
| `sweeping_does_not_change_the_report` | **the one that carries the file**: sweep, sweep again, `after == before` |

Load-bearing, proved by reverting the `continue` in a copy under
`~/.cache/amp-f4/` (with `assert s != a` on the patch): **3 of 5 fail**, and the
last one fails as `clean is True` → `False` — i.e. the reverted code reports the
mirror dirty forever over a file it will never copy, which is exactly the
unfalsifiable report this item was written about. The two that still pass are
right to: reverting only misfiles the row into `missing`, it does not put it in
`held`.

Two verification notes worth keeping:

- **`/tmp` is a 16G tmpfs and was at 100%** from other sessions' artifacts, so
  `pytest`'s `tmp_path` could not take a 16MB write. Tests run with
  `TMPDIR=~/.cache/amp-tests`. Do not clear `/tmp` to make a test pass; the
  files there belong to work in progress.
- The oversized file in the test is **sparse** (`f.truncate(size)`), not 16MB of
  `a`. That is not a shortcut around the check — the code under test asks
  `stat` and never reads the body, which is the property being relied on. A
  test that writes 16MB to prove a size check mostly proves the tmpdir had 16MB.

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

#### F5 — DONE, 2026-07-31. Measured, and it was bigger than ten lines.

Three parts, only the first of which was in the plan.

**The refusal.** `SchemaTooNew(RuntimeError)` carrying `.found` and `.known`.
`_stored_schema(conn)` reads `meta.schema`; `_connect` checks it **before the
first `executescript`**, and closes the connection and raises if it is greater.
Placement is the whole thing: every line below that point creates, alters or
writes, and one of them is `ALTER TABLE docs DROP COLUMN body`. An older reader
that opens a newer file does not fail — it migrates it backwards and writes a
worse version back. That is the loss this module exists to prevent, and the only
moment it can be stopped is before the first write.

`None` covers both "no `meta` table yet" and "value is not a number", and both
proceed. Older is a migration; only *newer* is a refusal. Refusing to open the
harness's only backup on the strength of an unparseable string would be a guess
with a real cost.

**The field made able to move.** `INSERT OR IGNORE` became an upsert guarded by
`found is None or found < SCHEMA`. The old form meant the number never changed
once the file existed, so a file migrated by a newer build still claimed the old
schema — a field that cannot change is a field that cannot disagree, which is
the same as not having it.

**`status()` reports two numbers**, `schema_code` and `schema_db`, from two
places. The old `"schema": SCHEMA` is gone. `stored_schema()` reads the file
**read-only and outside `_connect`**, because `_connect` is the thing that
refuses — asking it would produce the number only in the cases where nobody
needs it. Shown in Settings always, including when they agree: a field displayed
only on disagreement is a field nobody can check is working.

**The part that was not in the plan, and is half the diff.** Adding a failure
mode means finding everyone who assumed there wasn't one. Two callers read
`status()["docs"]` unconditionally:

- `server.py:3869`, at console startup — this would have **KeyError'd the
  console to death on a database it had just correctly refused**. Now it says so
  and carries on, and leaves the sweep thread unstarted, because a sweep that
  fails every ten minutes is noise and the fix is not on this machine's hot path.
- `amp.py:cmd_db("status")` — now prints the error and returns **1**, so a
  script can tell "there is no usable backup" from "here it is".

`tests/test_store_schema.py`, 10 tests. The one that carries the file is
`a_newer_file_is_refused_before_it_is_touched`: it is not enough that opening
raises. It compares the database **and its `-wal` and `-shm`** byte for byte
across the refusal — `_reopen` closes the connection rather than just forgetting
it, because WAL means an open handle can hold committed bytes the main file has
not got, and a comparison of the main file alone would pass by not looking.

Both guards proved load-bearing by reverting each separately in
`~/.cache/amp-f5/` (each patch asserted to have matched):

| revert | fails |
|---|---|
| drop the `raise SchemaTooNew` | 4 of 10 — including `record_still_refuses_to_raise_over_it`, which then *succeeds* in writing to a file it should not have opened |
| put back `INSERT OR IGNORE` | 2 of 10 — the migrated file goes on claiming the old number |

**One verification trap, and it nearly hit the live state directory.** `amp`
calls `store.bind(STATE_ROOT)` **at import**, so a test that binds `store` by
hand and *then* imports `amp` is silently re-pointed at the real `.amp/`. The
symptom was benign and misleading — a test asserting `rc == 1` got `rc == 0`,
because `status()` was reporting a database that existed and was fine. Set
`AMP_HOME` **and** `bind()`, to the same path, and assert `amp.store is st`.
This is the same family as the `DOCTRINE.md` clobber in §0.3: `AMP_HOME` is not
by itself an isolation boundary.

---

### C3 — Held-out split per lane, reporting Δ

**Rating: calibration 9 → 9.5, testing +0.5.** Effort: medium. **Depends on
C2.**

**What is wrong.** After C2, the scorer sees its own past outcomes — which
means every calibration number afterwards is measured on data the scorer was
trained on. Without a held-out split, C2's improvement cannot be distinguished
from the scorer learning to reproduce the table it was shown.

**This is the item SpecBench actually supports** (arXiv 2605.21384, moved here
from T1 where it was cited for the wrong thing). The paper's whole construction
is a visible validation suite next to held-out tests that compose the same
features, and its headline is that frontier models **saturate the visible suite
while the held-out gap persists and widens** — 28 points per tenfold increase
in code size. The gap between the two numbers is the measurement; either number
alone is the thing the paper says gets gamed.

That maps onto this harness exactly. C2 shows the scorer its own calibration
table; calibration measured afterwards on the same proposals is the visible
suite. **The Δ between the two halves is the only number here that means
anything**, and reporting the in-sample half alone after C2 would be this
codebase doing the thing the paper measures.

**The work.** Per lane, hold out a fraction of scored-and-resolved proposals
from the context block. Report calibration on both halves and the Δ between
them. A large Δ is the finding.

**Proof.** The Δ is reported on the Blueprint or Specs tab with both sample
sizes. **If the held-out set is too small to say anything, report that instead
of a number** — this is the same discipline `calibration()` already applies by
counting open goals nowhere ("not evidence yet").

---

#### C3 — DONE, 2026-07-31. Measured. **The live record has nothing settled in it, so the honest answer today is "not enough in either half" — which is the item's own stated proof, not a shortfall.**

**The split.** `_held_out(pid)` — `sha256` of the proposal's **own id**, mod 4.
One in four never enters the block. The rule is the whole design, and the two
obvious alternatives both break the claim it exists to support:

- **A random draw is not a split.** A proposal shown last week and held back
  today was never held out, and a number computed from it measures nothing.
- **Every fourth by position** is stable right up until a proposal is added,
  which this record does all day. An insertion moves everything after it across
  the line and retroactively rewrites which cases were in-sample.

**Not stratified by lane, deliberately** — despite the item being written "per
lane". Stratifying buys balance at small `n` by making one proposal's membership
depend on **how many other proposals its lane has**, which is the insertion
problem again in a different coat. Each lane gets a quarter in expectation; where
it does not, the honest output is a refusal, and the per-lane counts are reported
so a Δ that is really a statement about lane mix can be seen for what it is.

**`_calibration_cases(half)` is one definition of "judged"**, used by the table
and by the split. It started as two copies of the same four lines, which is how
two numbers that are supposed to be the same number quietly stop being it. An
open goal is on **neither** side — the split inherits `calibration`'s existing
refusal to count them, or the held-out half would be measured by a different rule
than the shown one.

**What is reported**, per measure, `odds` / `need` / `cost`:

> Δ = |gap on the half it never saw| − |gap on the half it was shown|

Positive says the shown half is flattering. Near zero says the scoring
generalises. **Negative is not better than zero** and is not drawn as though it
were: it means the scorer does worse on what it was shown, which is not a shape
improvement takes, and is a reason to go and look at the two samples.

`refine` is **deliberately absent from the Δ**. It is measured from the sharpen
log rather than from adopted proposals, so there is no proposal id to hash and
nothing to hold out; a Δ for it would be the same number printed twice.

**`CALIBRATION_MIN_N = 5`, and below it there is no rate at all.** Four judged
cases cannot support a percentage — one of them is 25 points — and a Δ built from
two such numbers is noise wearing the clothes of a finding. The row says which
side is short and what it would need.

**The gate is inside `calibration_block`, not in its callers.** A table that does
not say `half == "shown"` is refused with `die`. Three keys now leave
`report_data`, and they are not interchangeable: `calibration` (the whole table,
what the **operator** reads), `calibration_shown` (the only thing that may reach
a **prompt**), `calibration_split` (the Δ, which is the only one of the three
that says whether the first two mean anything). A split that fails open is worse
than no split — it keeps reporting a Δ measured on nothing — and a held-out
proposal that reaches a prompt through any route has stopped being held out
**permanently**, which no later run can undo.

One backwards-compatibility trap was caught before it bit: reports are
**persisted**, so a report taken before today has no `calibration_shown` key.
`_report_facts` **omits the block** in that case rather than falling back to the
whole table. The solver loses one block, which is recoverable; the alternative is
not.

**Measured live** (`http://127.0.0.1:8899`, Direction tab, real browser):

| state | what the screen drew |
|---|---|
| real record, 0 settled | `odds/need/cost not enough in either half — shown 0, held 0, need 5 each` |
| seeded, 81 settled | `odds shown +1% off, held out +55% off · Δ +54% · 64/17`, flagged, plus `only one side has anything from: docs` |

The seeded corpus was built with the held-out half **finishing far less often**
than the shown one on purpose — a Δ of zero is indistinguishable from a Δ that
never got computed — and was **deleted afterwards** (81 proposals, 81 goal files;
the split went back to "either half"). Leaving it would have put a fabricated
calibration record exactly where a real one goes, which is the thing this item is
about not doing.

The gate was also exercised against the live record: the shown half is accepted,
and **both** the whole table and the held half are refused by name.

**A defect found on the way in, and fixed.** Starting the console with a
**relative** `AMP_HOME` crashed it at startup with `ValueError: relative paths
can't be expressed as file URIs` — from `store.stored_schema`, which builds a
`file:` URI to open the mirror read-only. Nothing in that sentence names the
setting that caused it. `STATE_ROOT` is now `.resolve()`d at `amp.py:55`. The
second reason matters more than the crash: workers each run in their **own
worktree**, so a relative state root would have meant several state directories
rather than one.

**Tests: 250 → 262.** `test_calibration_split.py` (12). The load-bearing one is
`test_inserting_a_proposal_moves_nobody` — it is the property that rules out
positional membership, and it is the only one of the twelve that a plausible
wrong implementation passes everything else while failing. The rest pin: the
halves partition the whole and do not overlap; an unsettled goal is on neither
side; each table names its own half; too few reports `short` instead of dividing;
the Δ equals the difference of the two gaps **computed from the halves it
publishes** rather than from a number written into the test; `refine` has no Δ; a
lane on one side only is named; and the prompt boundary refuses both wrong tables
**with the right sentence**.

`test_calibration_block.py`'s two fixtures gained `"half": "shown"` and its stub
`calibration` gained the parameter — a stub with the narrower signature passes
while the real caller cannot make the call.

**ruff still clean, mypy still 169** — C3 added nothing to either. It briefly
added two: `lanes` carried the lane name inside a counter row, making it
`dict[str, object]`, and `object + int` is not an addition; and `row` was already
bound in the function above. Both were fixed rather than annotated away.

**What this does not prove.** Nothing has been through the split yet on real
data. The arithmetic is proven; the *finding* — whether C2 made the scorer better
or taught it to agree with itself — needs roughly 20 settled proposals before
either half clears `min_n`, and cannot be reported before then. That is the
correct state for it to be in, and the screen says so in those words.

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
- **The lane dev servers are a second unauthenticated door.** `preview.py:46`
  binds `127.0.0.1` and `preview.py:198` starts a `ThreadingHTTPServer` per
  lane; discovered `npm run dev` ports are the lane's own server and outside
  this harness's control entirely. Nothing there reaches the board, which is
  why it is not S1 — but if S1's `Host` allow-list lands, the same six lines
  belong in `preview.py`'s static handler, and a note belongs in the docs for
  anyone who points a lane's dev server at `0.0.0.0`.

---

## 4. Suggested order, and where it lands

| # | item | axis | from → to | effort |
|---|---|---|---|---|
| 1 | S1 `Host` + fetch-metadata + token | security | 3 → 6 | small |
| 2 | D1 durable + atomic writes (4 sites) | durability | 2 → 4 | tiny |
| 3 | C2 wire calibration into the prompt | calibration | 7 → 9 | small |
| 4 | T1 test suite | testing | 1 → 5 | large |
| 5 | ~~C1 exact-CAS~~ → cross-process locks | concurrency | 2 → 5 | medium |
| 6 | D2 `db restore` | durability | 4 → 5 | small |
| 7 | T2 mypy + ruff | code hygiene | 9 → 9.5 | small/med |
| 8 | F4 fifth verdict | durability/derived | +0.5 | tiny |
| 9 | F5 read schema back | derived state | 9 → 9.5 | tiny |
| 10 | C3 held-out split | calibration | 9 → 9.5 | medium |

**All ten are done as of 2026-07-31.** Each has a `#### … — DONE` record above
it saying what was actually built and what was not. Two of those records say the
item's own stated proof was **not** met — T2 (`mypy .` is not clean, 169 remain
and are characterised) and C3 (the Δ exists and is drawn, but no real data has
been through it yet) — because a list that only records the parts that worked is
the same failure mode C3 was written to catch.

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
