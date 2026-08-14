#!/usr/bin/env python3
"""code - browser console for the amp orchestration harness.

Runs LOCALLY. It shells out to the `claude` and `codex` CLIs (which need your
local auth), reads your git worktrees, and holds your OpenRouter key, so it
cannot be served as a static site from code.traaviis.com. Run it here, open
localhost.

    ./amp serve            # or: python3 code/server.py --port 8787

Follows the spinner_bench.py convention: stdlib ThreadingHTTPServer, sibling
index.html / app.js / app.css, no build step.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
import secrets
import socket
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import amp  # noqa: E402  the harness itself
import blueprint  # noqa: E402  how the lanes stack, what fires between them, what is sent
import preview  # noqa: E402  serving what a lane built, so you can look at it
import store  # noqa: E402  the SQLite mirror of everything under .amp/

ROOT = amp.ROOT  # workspace holding the lane worktrees

HOST = "127.0.0.1"
PORT = 8787

# --------------------------------------------------------------- inbound auth
#
# Binding to loopback stops a REMOTE attacker. It does not stop a PAGE, and a
# page is the thing to stop: downstream of these routes are three `shell=True`
# call sites, so a request that arrives is a command that runs. Three checks,
# applied in this order, because they stop three different attacks and the
# cheapest one stops the worst:
#
#   1. `Host`. The only defence against DNS REBINDING, where the attacker's
#      page is served from a name whose DNS re-answers as 127.0.0.1. The
#      browser then believes that name IS this server, so the page is
#      SAME-ORIGIN: it reads every response, it reads the token straight out of
#      the index.html it is entitled to fetch, and it sends exactly the
#      `Origin` and `Sec-Fetch-Site` a real console sends. Every other check
#      here is carried along by that attack. `Host` is not, because the browser
#      writes it from the name in the address bar, so it says the attacker's
#      name. This is why the check also covers GET and covers `/`: serving the
#      page at all to a rebound origin is what hands over everything else.
#   2. `Sec-Fetch-Site`. Sent by browsers to loopback (it counts as a
#      trustworthy origin) and NOT settable by page JavaScript, which is what
#      makes it worth more than the token against a browser. `Origin` is the
#      fallback for a client that sends neither.
#   3. The token. Last, not first: it is what covers a client that is not a
#      browser and sends no fetch metadata at all, and it is the layer
#      rebinding defeats.
#
# What is deliberately NOT here: a password, a login, or accounts. The
# requirement is "a web page cannot drive it and cannot read it", not "identify
# the human". A local process running as this user can already read the token,
# the OpenRouter key and the whole state directory, so nothing here is aimed at
# one.
# The file name and the header name are `amp`'s, not this module's. The server
# writes the file and the prompts in `amp` tell a worker to read it; two
# spellings of one name is the arrangement where the reader and the writer
# disagree about where the credential is and nothing says so.
#
# `none` is a user typing the URL or opening a bookmark - no initiator. A page
# cannot manufacture it. `same-site` is excluded on purpose: nothing here is
# served from a sibling subdomain, so a `same-site` request is not us.
_FETCH_SITE_OK = ("same-origin", "none")

# Set once the socket is bound, because the port is not known until then and
# `Host` includes it.
_ALLOWED_HOSTS: set[str] = set()
_TOKEN = ""


def read_console_token() -> str:
    """The running console's token, for a client in another process.

    Absent is not an error here: the CLI and the harness's own callers ask for
    this before they know whether a console is up, and "" simply fails the
    header check with the ordinary refusal.
    """
    try:
        return amp.console_token_path().read_text().strip()
    except OSError:
        return ""


def mint_console_token() -> str:
    """A fresh token per console, written 0600 before the socket opens.

    Fresh rather than persistent so that a token seen once cannot drive the
    next console, and because there is nothing to migrate: the page is served
    the current one on load. The `.token` suffix is what keeps it out of the
    mirror (`store.SKIP_SUFFIX`) - a secret with a full revision history is a
    secret with a full revision history.
    """
    global _TOKEN
    _TOKEN = secrets.token_urlsafe(32)
    p = amp.console_token_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Written the way every other durable file here is written, and 0600 from
    # the moment it exists rather than chmod-ed after: a world-readable window,
    # however short, is the whole of the exposure this file has.
    tmp = p.with_suffix(p.suffix + ".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(_TOKEN + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)
    amp._fsync_dir(p.parent)
    return _TOKEN


def set_allowed_hosts(host: str, port: int) -> None:
    """The names this console will answer to, and no others.

    Both spellings of loopback plus the bare name, since a browser omits the
    port only for 80. A console explicitly bound to some other address is
    added as given - binding elsewhere is the operator saying so, and refusing
    the address they chose would be a refusal they cannot act on.
    """
    _ALLOWED_HOSTS.clear()
    for h in {"127.0.0.1", "localhost", "[::1]", host}:
        _ALLOWED_HOSTS.add(f"{h}:{port}")
        if port == 80:
            _ALLOWED_HOSTS.add(h)

# Where the orchestrator reaches this console. It drives the board over the
# same HTTP API the browser uses, so every guard behind those endpoints - the
# per-lane lock, the worker cap, budgets, timeouts - applies to it too.
BASE_URL = f"http://{HOST}:{PORT}"

# codex cloud exec can block for a while; serialize dispatches so two clicks
# do not race the same lane.
_DISPATCH_LOCK = threading.Lock()

# One claude worker per lane at a time - two workers in the same worktree would
# overwrite each other's edits.
_LANE_LOCKS: dict[str, threading.Lock] = {}
_LANE_LOCKS_GUARD = threading.Lock()


def lane_lock(name: str) -> threading.Lock:
    with _LANE_LOCKS_GUARD:
        return _LANE_LOCKS.setdefault(name, threading.Lock())


# A dispatch that arrives with no slot free used to be refused outright, which
# put the whole burden of coming back later on whoever asked - and the
# orchestrator, whose turn ends the moment it replies, structurally cannot come
# back later. So a blocked dispatch waits here instead, and is started by the
# worker whose finishing freed the slot.
#
# It is written to disk on every change, because a queue that only exists in
# this process is dropped by every restart - and this file is restarted for
# every change to it. That would be the same bug the queue was built to fix,
# arriving by a different road: work reported as queued, then silently gone.
_QUEUE: list[dict] = []
_QUEUE_LOCK = threading.Lock()


def _save_queue():
    """Call with _QUEUE_LOCK held."""
    amp.save_json(amp.QUEUE_PATH, {"queued": _QUEUE})


def _load_queue():
    with _QUEUE_LOCK:
        _QUEUE[:] = amp.load_json(amp.QUEUE_PATH, {}).get("queued") or []
        return len(_QUEUE)


# The queue is the one piece of workspace state this process holds in memory, so
# it is the one thing a workspace switch could carry across a boundary it must
# not cross: tasks queued against one set of lanes, started against another.
# `amp.use_workspace` refuses while anything is running, so this only ever swaps
# a list nobody is draining.
amp.WORKSPACE_HOOKS.append(lambda _slug: _load_queue())


def queued_view() -> list[dict]:
    with _QUEUE_LOCK:
        return [{"lane": b.get("lane"), "queued_at": b.get("queued_at"),
                 "prompt": (b.get("prompt") or "")[:160]} for b in _QUEUE]


def _enqueue(body: dict, why: str) -> dict:
    with _QUEUE_LOCK:
        body = {**body, "queued_at": amp.now()}
        _QUEUE.append(body)
        _save_queue()
        pos = len(_QUEUE)
    return {"ok": True, "queued": True, "position": pos, "lane": body.get("lane"),
            "reason": why}


def _hold_lane_for_adopted(lane_name: str, task_id: str, lk: threading.Lock):
    """Keep an adopted worker's lane held until it actually finishes.

    The lane lock is what stops two workers sharing one worktree, and it lives
    in this process - so a console restart releases it while the worker it was
    protecting is still running. The queue then sees a free lane and starts a
    second agent in the same directory as the first. The lock is re-taken
    synchronously at boot, before any drain; this thread only gives it back.
    """
    try:
        while task_id in amp.adopted_task_ids():
            time.sleep(2.0)
    finally:
        lk.release()
        _drain_queue()


def _drain_queue():
    """Start whatever now fits. Called whenever a worker settles.

    Head-of-line blocking is deliberate at the cap but not per lane: one busy
    lane must not hold up a queued task for a different, idle one.
    """
    while True:
        with _QUEUE_LOCK:
            if not _QUEUE or amp.live_workers() >= amp.limits()["max_workers"]:
                return
            i = next((i for i, b in enumerate(_QUEUE)
                      if not lane_lock(b.get("lane") or "").locked()), None)
            if i is None:
                return
            body = _QUEUE.pop(i)
            _save_queue()
        out = do_dispatch(body, queue=False)
        if not out.get("ok"):
            # Dropping it silently would repeat the bug this queue exists to
            # fix, so the reason lands in the thread where it can be seen.
            amp.add_note(f"queued task for {body.get('lane')} could not start: "
                         f"{out.get('error')}", lane=body.get("lane"))


# ---------------------------------------------------------------- payloads


def state_payload() -> dict:
    cfg = amp.config()
    b = amp.board()
    key = amp.find_openrouter_key()
    codex_installed = amp.codex_available()

    lanes = []
    for name in sorted(cfg["lanes"]):
        lane = cfg["lanes"][name]
        backend = amp.lane_backend(lane)
        history = b.get("tasks", {}).get(name, [])
        if backend == "claude":
            tasks = [
                {
                    "task_id": t.get("task_id"),
                    "status": t.get("status"),
                    "dispatched_at": t.get("dispatched_at"),
                    "title": t.get("result") or t.get("error") or t.get("prompt"),
                    "cost_usd": t.get("cost_usd"),
                    "resumed": bool(t.get("resume_of")),
                }
                for t in history
                if t.get("backend") == "claude"
            ][:5]
        else:
            tasks = b.get("remote", {}).get(name, [])[:5]
        # Both of these are read off disk, so they cost nothing per poll. The
        # ratings are what the sidebar draws instead of a list of the last five
        # dispatches: five task titles say what happened here recently, which is
        # the one question the Log tab already answers, and say nothing at all
        # about whether this lane is anywhere worth spending the next worker.
        try:
            ratings = amp.lane_ratings(name)
        except Exception:
            traceback.print_exc()
            ratings = {}
        lanes.append(
            {
                "name": name,
                "repo": lane.get("repo"),
                "path": lane.get("path"),
                "branch": lane.get("branch", "main"),
                "backend": backend,
                "env_id": lane.get("env_id"),
                "bound": backend == "claude" or bool(lane.get("env_id")),
                "running": any(t.get("status") == "running" for t in tasks),
                "tasks": tasks,
                "ratings": ratings,
                "stage": amp.lane_stage(name),
                "mode": amp.lane_mode(name),
                "dispatch_count": len(history),
                "last_dispatch": history[0]["dispatched_at"] if history else None,
            }
        )

    uses_codex = any(l["backend"] == "codex" for l in lanes)
    # The whole board in four numbers, so the dock can say where things stand
    # without you opening a single lane.
    threads = [c for c in amp.consults() if c["status"] == "open"]
    goals = amp.goals()
    live_goals = [g for g in goals if g["state"] in ("planning", "running", "blocked")]
    try:
        seen = amp.observations()
    except Exception:
        # A diagnostic that can take the console down with it is worse than no
        # diagnostic - it shells out to git in trees it does not control.
        traceback.print_exc()
        seen = []
    try:
        found = amp.findings_summary()
    except Exception:
        traceback.print_exc()
        found = {"unread": 0, "contradicted": 0, "top": None}
    try:
        standing = amp.obligations_summary()
    except Exception:
        traceback.print_exc()
        standing = {"total": 0, "drifted": 0, "broken": 0, "unchecked": 0}
    try:
        ws = amp.workspace_view()
        # Cheap: the last reading off disk, not a new one. Taking a reading
        # costs an architect call and happens only when it is asked for.
        sup = amp.supervisor_view()
    except Exception:
        traceback.print_exc()
        ws = {"current": None, "mission": "", "blocked": None, "list": []}
        sup = {"mission": "", "last": None, "stale": False, "since": 0, "history": []}
    return {
        "lanes": lanes,
        # Which set of lanes, goals and history this whole payload is about.
        # Every number below is scoped to it, so it is not an aside.
        "workspace": ws,
        "supervisor": sup,
        "goals": goals[:12],
        "observations": seen,
        "findings": found,
        "obligations": standing,
        "summary": {
            "goals": len(live_goals),
            # Things that have to keep being true and currently are not. Not
            # counted as `problems`: drift in a published artifact is expected
            # after work lands, and is a queue rather than an alarm.
            "drifted": standing["drifted"] + standing["broken"],
            # What the work has said about the doctrine and nobody has been told
            # yet. A contradiction is counted separately because it means
            # something we believed and acted on is false.
            "findings": found["unread"],
            "contradicted": found["contradicted"],
            "goals_stuck": sum(1 for g in live_goals if g["state"] == "blocked"),
            # Of those, the ones stopped because a budget ran out rather than
            # because they need a decision. Both say `blocked`, but only one of
            # them is anybody waiting: a goal out of rounds is a lane that has
            # quietly retired, and it reads as healthy next to the ones that
            # correctly asked you something.
            "goals_spent": len(amp.budget_stopped()),
            # A goal that is running with nobody on it. The heartbeat restarts
            # these, so a number here that does not fall within a minute means
            # the restart itself is failing - which is worth seeing, and used to
            # be invisible because a quiet goal counted as a healthy one.
            "goals_idle": len(amp.idle_goals()),
            # Goals deliberately waiting out a capacity limit rather than being
            # judged for it. Counted separately from idle because a held goal is
            # working as intended and an idle one is not.
            "goals_held": sum(1 for g in live_goals
                              if (amp.load_goal(g["id"]) or {}).get("hold_until", "") > amp.now()),
            # What is currently stopping the pipeline from feeding itself. Empty
            # is the normal, running state; anything here is waiting on you.
            "escalations": amp.escalations(),
            "problems": sum(1 for o in seen if o.get("severity") == "high"),
            # Workers, not lanes. This is shown against the cap, and the cap
            # counts workers - a lane can hold more than one (a restart, a
            # resume), so counting lanes reads as spare capacity that is not
            # there.
            "running": amp.live_workers(),
            "cap": amp.limits()["max_workers"],
            "failed": sum(1 for l in lanes
                          if l["tasks"] and l["tasks"][0].get("status") in ("failed", "error")),
            "threads": len(threads),
            # A thread with a worker out fetching what it asked for is not
            # waiting on you, and counting it as though it were is how the dock
            # ends up asking for attention that nothing needs.
            "waiting": sum(1 for c in threads if _needs_you(c)),
            "lanes": [c["lane"] for c in threads if _needs_you(c)],
            "queued": queued_view(),
        },
        "polled_at": b.get("polled_at"),
        "health": {
            "claude_installed": amp.claude_available(),
            "claude_auth": amp.claude_auth_problem(),
            "codex_installed": codex_installed,
            "codex_logged_in": codex_installed and amp.codex_logged_in(),
            "codex_needed": uses_codex,
            "openrouter_key": bool(key),
            "openrouter_enabled": amp.openrouter_enabled(),
            # Which backend answers as the architect, and whether it can. The
            # banner needs both: "the architect is unavailable" is only useful
            # next to which architect it is talking about.
            "architect_backend": amp.architect_backend(),
            "architect_ready": amp.architect_available(),
            "unbound_lanes": [l["name"] for l in lanes if not l["bound"]],
            # The three lights in the header. Each one names the role, the model
            # answering as it, whether it may run and why not - the header shows
            # all four, because a dark dot with no reason is just a worry.
            "roles": amp.role_view(),
        },
        # What the stage dropdown on every lane card is choosing between, sent
        # once rather than per lane: it is the same ladder everywhere, and
        # repeating it eleven times a poll is bytes for nothing.
        "stages": [{"key": s, "means": amp.STAGE_MEANS[s],
                    "automated": amp.STAGE_AUTOMATED[s], "who": amp.STAGE_WHO[s]}
                   for s in amp.LANE_STAGES],
        "consult_models": amp.CONSULT_MODELS,
        "default_model": cfg.get("consult_model", amp.DEFAULT_CONSULT),
        "backends": list(amp.BACKENDS),
        "claude_budget_usd": cfg.get("claude_budget_usd", amp.DEFAULT_BUDGET_USD),
        "claude_model": cfg.get("claude_model", amp.DEFAULT_CLAUDE_MODEL),
        "limits": amp.limits(),
        "workers": amp.worker_stats(),
    }


def _needs_you(c: dict) -> bool:
    """Is this thread actually stuck on the operator, or just mid-round?"""
    return bool(c.get("needs")) and c.get("blocked_on") != "gathering"


def rulings_payload() -> list[dict]:
    if not amp.RULING_DIR.exists():
        return []
    out = []
    for p in sorted(amp.RULING_DIR.glob("*.md"), reverse=True)[:40]:
        out.append(
            {
                "name": p.name,
                "lane": p.name.split("-")[0],
                "size": p.stat().st_size,
                "mtime": p.stat().st_mtime,
            }
        )
    return out


def do_poll(lane_filter: str | None) -> dict:
    """Refresh codex lanes. Claude lanes settle themselves as their threads finish."""
    cfg = amp.config()
    names = [lane_filter] if lane_filter else sorted(cfg["lanes"])
    names = [n for n in names if amp.lane_backend(cfg["lanes"].get(n, {})) == "codex"]
    if names and not amp.codex_logged_in():
        return {"ok": False, "error": "codex is not logged in. Run: codex login"}
    seen, errors, remote = 0, {}, {}
    for name in names:
        lane = cfg["lanes"].get(name)
        if not lane or not lane.get("env_id"):
            continue
        try:
            tasks = amp.codex_list(env_id=lane["env_id"])
            remote[name] = tasks
            seen += len(tasks)
        except SystemExit as e:
            errors[name] = str(e) or "codex cloud list failed"
    # The board is read AFTER the network, under the lock. Held across the
    # poll it would write back a board from before every worker that finished
    # while this ran - see `amp.record_remote`.
    polled_at = amp.record_remote(remote)
    return {"ok": True, "tasks_seen": seen, "errors": errors, "polled_at": polled_at}


def _run_claude_bg(name: str, rec: dict):
    """Workers take minutes; the browser polls /api/state for the settled record.

    `SystemExit` is named alongside `Exception` in all three handlers because
    `amp.die` raises one - `amp.Died` subclasses it to keep the reason attached -
    and a refusal from anywhere down these call paths is an ordinary outcome
    here, not a reason to stop. It would not be caught otherwise: `SystemExit`
    is a `BaseException`, and `threading.excepthook` discards it without a word,
    so the record below would sit on `running` forever and the slot this worker
    just freed would never be handed to whoever was queued for it.
    """
    lock = lane_lock(name)
    with lock:
        try:
            amp.run_claude_task(name, rec)
        except (Exception, SystemExit) as e:  # never leave a record stuck on `running`
            traceback.print_exc()
            amp.update_task(name, rec["task_id"], {"status": "failed", "error": str(e)})
    try:
        _settle(name, rec)
    except (Exception, SystemExit):
        # An escalation that fails must not rewrite a worker's real outcome.
        traceback.print_exc()
    # The slot is free now. Whoever was waiting on it is started here, by the
    # worker that freed it - there is no other moment when anyone is looking.
    try:
        _drain_queue()
    except (Exception, SystemExit):
        traceback.print_exc()


def _settle(name: str, rec: dict):
    """What happens once a worker stops: report to the architect that sent it,
    or escalate on its own if it came back blocked.

    A worker's report goes back to the architect automatically, but the next
    build order does not go back to a worker automatically - a loop that both
    ends drive is a loop that spends your money while you are not looking.
    """
    fresh = (do_task(name, rec["task_id"]).get("task")) or rec
    # Every worker passes through here exactly once, whoever sent it - a goal, a
    # consult, or the operator - so this is the one place a DOCTRINE: line cannot
    # be missed. It is filed before anything below can return.
    try:
        amp.record_worker_finding(name, fresh)
        # Same place, same reason: whatever it noticed in passing is in that one
        # message and nowhere else once the worktree goes.
        amp.record_worker_ideas(name, fresh)
    except Exception:
        traceback.print_exc()
    rid = rec.get("spec_run_id")
    if rid:
        # A spec run owns what happens next to its writer, the same way a goal
        # does: it reads the file back, records whether it moved, and sends the
        # reviewer at it again. Nothing below applies.
        amp.spec_worker_done(rid, name, fresh)
        return
    gid = rec.get("goal_id")
    if gid:
        # A goal's worker reports to the goal, which judges it against the
        # definition of done and sends the next one. Nothing else here applies:
        # the goal owns what happens next by construction.
        amp.goal_worker_done(gid, name, {**fresh, "goal_task": rec.get("goal_task")})
        return
    cid = rec.get("consult_id")
    if cid and rec.get("report_back", True):
        # The report is recorded either way - it is free, and losing a worker's
        # findings because the architect was switched off would be throwing away
        # the expensive half to save the cheap half. Only the round is skipped.
        amp.add_turn(cid, "worker", amp.worker_report(name, fresh), task_id=rec["task_id"])
        if amp.architect_available():
            amp.advance_consult(cid)
        else:
            amp.add_note(f"{name}'s report is on consult {cid}, but "
                         f"{amp.architect_off_reason().lower()} - continue the thread "
                         f"to send it.", lane=name)
        return
    if not amp.config().get("auto_escalate", True) or not amp.architect_available():
        return
    why = amp.needs_escalation(fresh)
    if not why:
        return
    amp.open_consult(
        name,
        f"A worker in this lane stopped and needs a ruling.\n\n{why}\n\n"
        "The packet below is its full context. Rule on what should happen next.",
        model_key=amp.config().get("consult_model", amp.DEFAULT_CONSULT),
        trigger="auto",
    )


def _gather_task_for(cid: str) -> dict | None:
    """The most recent worker sent to gather evidence for this thread."""
    for tasks in (amp.board().get("tasks") or {}).values():
        for rec in tasks:
            if rec.get("consult_id") == cid and rec.get("report_back"):
                return rec
    return None


def _recover_stuck_consults() -> list[str]:
    """Relay any worker that finished while nobody was listening.

    A thread waiting on evidence is waiting on one specific worker, and the only
    thing that moves it is that worker's report. If the report is missed - a
    restart, a crash in the settle path - the thread does not fail, it simply
    stops, and stopping is indistinguishable from working. So the fact that
    settles it is not `did we relay?` but `has its worker finished, and is its
    report in the thread?`, both of which are on disk and can be re-checked.
    """
    moved = []
    for row in amp.consults():
        if row.get("blocked_on") != "gathering":
            continue
        cid = row["id"]
        c = amp.load_consult(cid)
        rec = _gather_task_for(cid)
        if not c or not rec or rec.get("status") == "running":
            continue
        if any(t.get("task_id") == rec["task_id"] and t.get("role") == "worker"
               for t in c.get("turns") or []):
            continue          # it did report; the thread is stopped for another reason
        try:
            _settle(rec.get("lane") or c["lane"], rec)
            moved.append(cid)
        except Exception:
            traceback.print_exc()
    return moved


def _recover_stuck_goals() -> list[str]:
    """A goal whose worker finished while nobody was listening, restarted.

    Same failure as a stopped thread and the same test for it: the goal says a
    task is running, and the board says that task is not.
    """
    moved = []
    for row in amp.goals():
        if row.get("state") != "running":
            continue
        g = amp.load_goal(row["id"])
        task = next((t for t in g.get("tasks") or [] if t.get("state") == "running"), None)
        if not task:
            continue
        rec = next((r for recs in (amp.board().get("tasks") or {}).values() for r in recs
                    if r.get("goal_id") == g["id"] and r.get("goal_task") == task["id"]), None)
        if rec and rec.get("status") == "running":
            continue
        try:
            if rec:
                amp.goal_worker_done(g["id"], g["lane"], {**rec, "goal_task": task["id"]})
            else:
                # It never started. Put the task back and let the goal send it.
                task["state"] = "todo"
                amp.save_goal(g)
                amp.goal_dispatch(g["id"])
            moved.append(g["id"])
        except Exception:
            traceback.print_exc()
    return moved + _restart_idle_goals()


def _restart_idle_goals() -> list[str]:
    """Send the next task out for every goal that has nobody on it.

    The companion to the sweep above, for the case it cannot see: that one looks
    for a goal whose task says `running` when the board says otherwise, which
    only catches a worker lost mid-flight. A review lost mid-flight leaves no
    running task at all, so the goal is simply quiet, and quiet was indexed as
    healthy. Three goals sat like this for two hours with five lanes idle.
    """
    moved = []
    for g in amp.idle_goals():
        try:
            amp.goal_log(g["id"], "picked back up: it had work left and nobody on it")
            amp.goal_dispatch(g["id"])
            moved.append(g["id"])
        except Exception:
            traceback.print_exc()
    return moved


def do_dispatch(body: dict, *, queue: bool = True) -> dict:
    name = body.get("lane")
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "prompt is empty"}
    cfg = amp.config()
    lane = cfg["lanes"].get(name)
    if not lane:
        return {"ok": False, "error": f"unknown lane {name!r}"}

    # Checked before either backend, because a worker is a worker wherever it
    # runs, and before the queue, because queueing work against a role that is
    # switched off just builds a backlog that starts the moment it comes back.
    if not amp.role_on("worker"):
        return {"ok": False, "error": "workers are switched off in Settings"}

    backend = body.get("backend") or amp.lane_backend(lane)
    branch = body.get("branch") or lane.get("branch") or "main"

    if backend == "claude":
        if not amp.claude_available():
            return {"ok": False, "error": "claude CLI not found"}
        if lane_lock(name).locked():
            if queue:
                return _enqueue(body, f"a worker is already running in {name!r}")
            return {"ok": False, "error": f"a worker is already running in {name!r}"}
        cap = amp.limits()["max_workers"]
        live = amp.live_workers()
        if live >= cap:
            if queue:
                return _enqueue(body, f"{live} of {cap} slots in use")
            return {"ok": False, "error":
                    f"{live} workers already running - the board is capped at {cap}. "
                    "Wait for one to finish, stop one, or raise max_workers in config.json."}
        budget = float(body.get("budget") or cfg.get("claude_budget_usd", amp.DEFAULT_BUDGET_USD))
        model = body.get("model") or cfg.get("claude_model", amp.DEFAULT_CLAUDE_MODEL)
        resume_of = None
        want = body.get("resume")
        if isinstance(want, str) and want not in ("", "1", "true", "last"):
            # A named session, because the one worth continuing is usually not
            # the newest - a worker killed by a limit is often followed by
            # others, and those are what "reply to last" was reaching.
            resume_of = amp.resumable_session(name, want)
            if not resume_of:
                return {"ok": False,
                        "error": f"no resumable claude session matching {want!r} in {name!r}"}
        elif want:
            prior = amp.latest_claude_task(name)
            if not prior:
                return {"ok": False, "error": "no prior claude session to resume"}
            resume_of = prior.get("session_id") or prior.get("task_id")
        try:
            rec = amp.start_claude_task(
                name, lane, prompt,
                branch=branch, model=model, budget=budget, resume_of=resume_of,
            )
        except SystemExit as e:
            return {"ok": False, "error": str(e) or "could not start the worker"}
        # Carried on the record so the settle step knows which architect, if any,
        # is waiting on this worker.
        extra = {k: body[k] for k in ("consult_id", "report_back", "goal_id", "goal_task",
                                      "spec_run_id", "spec_draft")
                 if body.get(k) is not None}
        if extra:
            rec.update(extra)
            amp.update_task(name, rec["task_id"], extra)
        threading.Thread(target=_run_claude_bg, args=(name, rec), daemon=True).start()
        return {
            "ok": True,
            "running": True,
            "task_id": rec["task_id"],
            "cwd": rec["cwd"],
            "budget_usd": budget,
            "model": model,
            "resumed": bool(resume_of),
        }

    env_id = lane.get("env_id")
    if not env_id:
        return {"ok": False, "error": f"lane {name!r} has no codex env id (bind it first)"}
    if not amp.codex_logged_in():
        return {"ok": False, "error": "codex is not logged in. Run: codex login"}

    attempts = int(body.get("attempts") or 1)

    cmd = ["codex", "cloud", "exec", "--env", env_id, "--branch", branch]
    if attempts > 1:
        cmd += ["--attempts", str(attempts)]
    cmd += [prompt]

    with _DISPATCH_LOCK:
        p = amp.run(cmd)
        out = (p.stdout + p.stderr).strip()
        if p.returncode != 0:
            return {"ok": False, "error": out or f"codex exited {p.returncode}"}
        # `record_task`, not a second copy of it: this one read and wrote the
        # whole board without the board lock, under a lock that only excludes
        # other dispatches.
        amp.record_task(name, {
            "backend": "codex",
            "dispatched_at": amp.now(),
            "prompt": prompt,
            "branch": branch,
            "attempts": attempts,
            "env_id": env_id,
            "submit_output": out,
            "status": "submitted",
        })
    return {"ok": True, "output": out}


# ---------------------------------------------------------------- consult threads


def _consult_view(c: dict) -> dict:
    """A thread as the console shows it. The packet turn is a document, not a
    chat line, so it is summarised rather than shipped in full."""
    turns = []
    for t in c["turns"]:
        text = t["text"]
        turns.append({
            "role": t["role"],
            "at": t["at"],
            "text": (f"packet built ({len(text)} chars): {c.get('question','')}"
                     if t["role"] == "packet" else text),
            "packet": t.get("packet"),
            "task_id": t.get("task_id"),
            "usage": t.get("usage"),
        })
    return {
        "id": c["id"], "lane": c["lane"], "model": c["model"], "status": c["status"],
        "trigger": c.get("trigger"), "question": c.get("question"),
        "opened_at": c["opened_at"], "cost_tokens": c.get("cost_tokens", 0),
        "needs": c.get("needs") or [], "turns": turns,
        # Why it is not moving. A thread that is gathering is not stuck, and a
        # thread that is stuck should say which kind of stuck.
        "blocked_on": c.get("blocked_on"),
        "blocked_why": amp.BLOCK_REASONS.get(c.get("blocked_on") or ""),
        "auto_rounds": c.get("auto_rounds", 0),
    }


def _gather_for_consult(cid: str, needs: list[str]) -> bool:
    """Send a worker to fetch the evidence an architect stopped without.

    This is the relay a build order already uses - dispatched with the consult id
    on it, so when the worker finishes, `_settle` hands its report straight back
    and the thread advances on its own. The only difference is the prompt: this
    one is told to look and not touch.
    """
    c = amp.load_consult(cid)
    if not c:
        return False
    out = do_dispatch({
        "lane": c["lane"],
        "prompt": amp.gather_prompt(needs, c["lane"]),
        "backend": "claude",
        "consult_id": cid,
        "report_back": True,
    })
    if not out.get("ok"):
        amp.add_note(f"could not send a worker to gather what the {c['lane']} architect "
                     f"asked for: {out.get('error')}", lane=c["lane"])
        return False
    # The architect is told a worker went, so the report that arrives next round
    # arrives as the answer to something rather than out of nowhere.
    amp.add_turn(cid, "note",
                 ("queued" if out.get("queued") else "sent") +
                 " a worker to gather: " + "; ".join(needs)[:400],
                 task_id=out.get("task_id"))
    return True


def do_ask(body: dict) -> dict:
    """Open a consult, or add a round to one that is already open."""
    cfg = amp.config()
    if not amp.architect_available():
        return {"ok": False, "error": amp.architect_off_reason()}

    cid = body.get("consult_id")
    text = (body.get("question") or "").strip()
    if not text:
        return {"ok": False, "error": "question is empty"}
    try:
        if cid:
            if not amp.load_consult(cid):
                return {"ok": False, "error": f"no consult {cid!r}"}
            amp.add_turn(cid, "you", text)
            c = amp.advance_consult(cid)
        else:
            name = body.get("lane")
            if name not in cfg["lanes"]:
                return {"ok": False, "error": f"unknown lane {name!r}"}
            model_key = body.get("model") or cfg.get("consult_model", amp.DEFAULT_CONSULT)
            c = amp.open_consult(name, text, model_key=model_key,
                                 extra_files=body.get("files") or [])
    except SystemExit as e:
        return {"ok": False, "error": str(e) or "packet/consult failed"}
    return {"ok": True, "consult": _consult_view(c), "ruling": amp.last_ruling(c)}


def do_consults(lane: str | None, cid: str | None) -> dict:
    if cid:
        c = amp.load_consult(cid)
        return {"ok": True, "consult": _consult_view(c)} if c else {
            "ok": False, "error": f"no consult {cid!r}"}
    return {"ok": True, "consults": amp.consults(lane or None)}


def do_relay(body: dict) -> dict:
    """Hand the architect's latest ruling to the lane's worker, and bring the
    worker's report back to the architect when it finishes."""
    cid = body.get("consult_id")
    c = amp.load_consult(cid or "")
    if not c:
        return {"ok": False, "error": f"no consult {cid!r}"}
    if not amp.last_ruling(c):
        return {"ok": False, "error": "this consult has no ruling to relay yet"}
    out = do_dispatch({
        "lane": c["lane"],
        "prompt": amp.ruling_prompt(c),
        "backend": "claude",
        "budget": body.get("budget"),
        "model": body.get("model"),
        "consult_id": cid,
        "report_back": body.get("report_back", True),
    })
    if out.get("ok"):
        amp.add_turn(cid, "note", f"relayed to the {c['lane']} worker as task "
                                  f"{out.get('task_id')}", task_id=out.get("task_id"))
    return out


def _dispatch_for_goal(goal: dict, task: dict) -> dict:
    """One task of a goal, out to one worker in that goal's lane."""
    return do_dispatch({
        "lane": goal["lane"],
        "prompt": amp.goal_worker_prompt(goal, task),
        "backend": "claude",
        "goal_id": goal["id"],
        "goal_task": task["id"],
        # It reports to the goal, not to a consult thread.
        "report_back": False,
    })


def do_open_goal(body: dict) -> dict:
    lane = body.get("lane") or ""
    objective = (body.get("objective") or "").strip()
    if not objective:
        return {"ok": False, "error": "say what the goal is"}
    if lane not in amp.config()["lanes"]:
        return {"ok": False, "error": f"unknown lane {lane!r}"}
    if not amp.architect_available():
        return {"ok": False, "error": amp.architect_off_reason()
                + " - a goal needs an architect to plan it"}
    try:
        g = amp.open_goal(lane, objective, model_key=body.get("model"))
    except SystemExit as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "goal": amp.load_goal(g["id"])}


def do_goal(body: dict) -> dict:
    gid = body.get("goal_id") or ""
    g = amp.load_goal(gid)
    if not g:
        return {"ok": False, "error": f"no goal {gid!r}"}
    return {"ok": True, "goal": g}


def do_answer_goal(body: dict) -> dict:
    gid = body.get("goal_id") or ""
    text = (body.get("text") or "").strip()
    if not amp.load_goal(gid):
        return {"ok": False, "error": f"no goal {gid!r}"}
    if not text:
        return {"ok": False, "error": "say something"}
    return {"ok": True, "goal": amp.answer_goal(gid, text)}


def _triage_goal_bg(gid: str):
    try:
        amp.triage_goal(gid, BASE_URL)
    except Exception:
        traceback.print_exc()


def do_triage_goal(body: dict) -> dict:
    """Ask the orchestrator to have a go at a stopped goal's questions, now.

    The same seam the heartbeat uses, reached by a click. The heartbeat only
    re-reads a goal when the doctrine or a recorded decision has moved, which
    is right for a background sweep and useless to an operator looking at a
    question on screen and wanting it tried. This is the "read it again now"
    button; it grants no new authority, so a question whose honest answer is
    the operator's preference comes straight back as his.

    Refused synchronously, run in the background - a turn takes as long as a
    model takes, and holding the request open for that would leave the button
    dead and the feed, which is already showing the turn, the only place
    anything was happening.
    """
    gid = body.get("goal_id") or ""
    why = amp.triage_blocker(gid)
    if why:
        return {"ok": False, "error": why}
    threading.Thread(target=_triage_goal_bg, args=(gid,), daemon=True).start()
    return {"ok": True, "triaging": True}


def do_push_goal(body: dict) -> dict:
    """Extend a budget-stopped goal's budget and let it carry on.

    This used to zero `rounds` in place and, for a token stop, refuse outright
    and tell the operator to raise a module constant. Both were wrong in the
    same way: the first erased the only record of what the goal had cost, and
    the second offered the operator a decision they could not actually take.
    `amp.extend_goal_budget` moves the ceiling instead, so the spend survives
    and a second extension is visible as one.
    """
    gid = body.get("goal_id") or ""
    if not amp.load_goal(gid):
        return {"ok": False, "error": f"no goal {gid!r}"}
    out = amp.extend_goal_budget(gid)
    if out.get("error"):
        return {"ok": False, "error": out["error"]}
    return {"ok": True, "goal": out}


def do_publish_report(body: dict) -> dict:
    """What would happen, and what is standing in the way. Pushes nothing."""
    gid = body.get("goal_id") or ""
    return {"ok": True, "report": amp.publish_report(gid)}


def do_publish_goal(body: dict) -> dict:
    """Push the lane branch and open a pull request.

    `confirm: true` is required in the body. Not as ceremony - this is the only
    call in the harness whose effect leaves this machine and lands somewhere
    other people can see, and it is worth its own deliberate word.
    """
    gid = body.get("goal_id") or ""
    if not body.get("confirm"):
        rep = amp.publish_report(gid)
        rep["dry_run"] = True
        return {"ok": True, "report": rep,
                "note": "dry run - send confirm:true to push the branch and open the PR"}
    return {"ok": True, "report": amp.publish_goal(gid, dry_run=False)}


def do_close_goal(body: dict) -> dict:
    gid = body.get("goal_id") or ""
    if not amp.load_goal(gid):
        return {"ok": False, "error": f"no goal {gid!r}"}
    return {"ok": True, "goal": amp.close_goal(gid, body.get("state") or "abandoned")}


def do_findings(lane: str | None, unread_only: bool) -> dict:
    """What the work has said about the doctrine."""
    return {"ok": True, "findings": amp.findings(lane=lane, unread_only=unread_only)[:80],
            "summary": amp.findings_summary(),
            "doctrine_path": str(amp.DOCTRINE_PATH),
            "doctrine_state": amp.doctrine_state(),
            "doctrine": amp.doctrine()}


def do_ack_findings(body: dict) -> dict:
    """Mark findings as told to the operator.

    Deliberately not automatic. A finding is unread until someone says it was
    put in front of Travis, because the whole point of the channel is that a
    contradiction cannot be quietly aged out of the board.
    """
    ids = body.get("ids") or ([body["id"]] if body.get("id") else [])
    if not ids:
        return {"ok": False, "error": "no ids"}
    return {"ok": True, "acked": amp.ack_findings([str(i) for i in ids]),
            "summary": amp.findings_summary()}


def do_settle_findings(body: dict) -> dict:
    """Work out what the open contradictions imply, and perform it.

    The other half of `do_ack_findings`, and the reason that one stayed manual.
    An ack says Travis has seen it. This says the harness DID something - took a
    rung back, filed a proposal, matched it to a later finding that already
    answered it - and closes the finding against that act. Anything it cannot
    perform stays unread, so the gate only falls as far as the work justifies.
    """
    return amp.settle_findings(body.get("lane") or None)


def do_ideas(lane: str | None) -> dict:
    """Leads workers left behind. Nothing here has been judged or acted on."""
    return {"ok": True, "ideas": amp.ideas(lane=lane)[:80]}


def do_close_ideas(body: dict) -> dict:
    """Take an idea off the list. `picked` means it became work; `dropped` means no.

    Both are answers, and both are the operator's - a list nobody can clear is a
    list that stops being read.
    """
    ids = body.get("ids") or ([body["id"]] if body.get("id") else [])
    state = body.get("state") or "dropped"
    if not ids:
        return {"ok": False, "error": "no ids"}
    if state not in ("picked", "dropped"):
        return {"ok": False, "error": "state must be picked or dropped"}
    return {"ok": True, "closed": amp.close_ideas([str(i) for i in ids], state)}


# --------------------------------------------------------------- direction


def do_direction(lane: str | None) -> dict:
    """The thesis, the values, what came back, and where there is left to go."""
    return {"ok": True, "direction": amp.direction_view(lane)}


# ------------------------------------------------------------------ spec review


def _dispatch_for_spec(run: dict, by_rel: dict) -> dict:
    """One spec run's next writer, out to a worker in that run's lane.

    ONE writer for every document the reviewers named this round, which is what
    `by_rel` is: `{document: [items]}`. A writer per document would be N workers
    in a lane that runs one at a time, so N-1 of them would be refused.

    `report_back: False` because it reports to the run, not to a consult thread.
    Not queued: a spec run is a synchronous back-and-forth, and a writer that
    sits in a queue leaves the run saying `waiting on worker` for an hour with
    nothing running, which is indistinguishable from being stuck.
    """
    return do_dispatch({
        "lane": run["lane"],
        "prompt": amp.spec_worker_prompt(run, by_rel),
        "backend": "claude",
        "spec_run_id": run["id"],
        "report_back": False,
    }, queue=False)


def do_spec(lane: str | None) -> dict:
    """One lane's `docs/spec/` documents, and every run against them."""
    lane = lane or ""
    if lane not in amp.config()["lanes"]:
        return {"ok": False, "error": f"unknown lane {lane!r}"}
    return {"ok": True, "spec": amp.spec_view(lane)}


def do_spec_run(rid: str | None) -> dict:
    r = amp.load_specrun(rid or "")
    # Normalised, like the ones `spec_view` sends. A run recorded before a run
    # covered a set carries one `rel` and a round-level verdict, and the tab
    # would have to know both shapes to draw either.
    return ({"ok": True, "run": amp.specrun_view(r)} if r
            else {"ok": False, "error": f"no run {rid!r}"})


def do_spec_start(body: dict) -> dict:
    """Open a run. Slow on purpose - the first architect turn happens inline, so
    the button either comes back with a verdict or comes back with the reason
    there is not one."""
    lane, rel = body.get("lane") or "", (body.get("rel") or "").strip()
    if not rel:
        return {"ok": False, "error": "say which document"}
    if not amp.architect_available():
        return {"ok": False, "error": amp.architect_off_reason()
                + " - a spec run needs an architect to review it"}
    if not amp.role_on("worker"):
        return {"ok": False, "error": "workers are switched off in Settings"}
    try:
        r = amp.spec_review_open(lane, [rel])
    except SystemExit as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "run": r}


def do_spec_close(body: dict) -> dict:
    try:
        return {"ok": True, "run": amp.close_specrun(body.get("id") or "")}
    except SystemExit as e:
        return {"ok": False, "error": str(e)}


def do_spec_rate(body: dict) -> dict:
    """Rate one document, or every unrated one in the lane.

    Both on one route because they are the same call in a loop, and because the
    difference the caller cares about - one architect turn or eleven - is
    already visible in whether a `rel` was named.
    """
    lane, rel = body.get("lane") or "", (body.get("rel") or "").strip()
    if lane not in amp.config()["lanes"]:
        return {"ok": False, "error": f"unknown lane {lane!r}"}
    if not amp.architect_available():
        return {"ok": False, "error": amp.architect_off_reason()
                + " - rating a document is an architect call"}
    try:
        if rel:
            return {"ok": True, "rating": amp.spec_rate(lane, rel)}
        return {"ok": True, "audit": amp.spec_audit(lane)}
    except SystemExit as e:
        return {"ok": False, "error": str(e)}


def do_spec_campaign(body: dict) -> dict:
    """Sharpen every document in the lane, in one run, with one writer.

    Slower than the single-document button by one architect call per document,
    for the same reason that one is slow: the reviews happen inline, so the
    button comes back with what every document was told, or with the reason it
    was told nothing.
    """
    lane = body.get("lane") or ""
    if lane not in amp.config()["lanes"]:
        return {"ok": False, "error": f"unknown lane {lane!r}"}
    if not amp.architect_available():
        return {"ok": False, "error": amp.architect_off_reason()
                + " - a spec run needs an architect to review it"}
    if not amp.role_on("worker"):
        return {"ok": False, "error": "workers are switched off in Settings"}
    try:
        return {"ok": True, "plan": amp.spec_campaign_open(lane, rels=body.get("rels"))}
    except SystemExit as e:
        return {"ok": False, "error": str(e)}


def do_spec_campaign_stop(body: dict) -> dict:
    """Stop the run this selection opened. There is nothing else to stop.

    The old queue could be stopped without touching the run it had started,
    because that was a decision about what to start NEXT. A selection starts
    everything at once, so this closes the run - the same thing the run's own
    button does, reached from the strip that started it.
    """
    lane = body.get("lane") or ""
    plan = amp.spec_campaign_close(lane)
    if not plan:
        return {"ok": False, "error": f"nothing has been sharpened on {lane!r}"}
    return {"ok": True, "plan": plan}


def do_lane_refresh(body: dict) -> dict:
    """Bring a lane's worker tree forward to its branch.

    Under the lane lock, because it rewrites the same tree a worker would be
    editing. Merging the branch in underneath a running writer would change the
    file it is halfway through, and it has no way to notice.
    """
    lane = body.get("lane") or ""
    if lane not in amp.config()["lanes"]:
        return {"ok": False, "error": f"unknown lane {lane!r}"}
    lock = lane_lock(lane)
    if not lock.acquire(blocking=False):
        return {"ok": False, "error": f"a worker is running in {lane} - "
                                      f"refresh once it has finished"}
    try:
        return amp.lane_refresh(lane)
    except SystemExit as e:
        return {"ok": False, "error": str(e)}
    finally:
        lock.release()


def do_spec_draft(body: dict) -> dict:
    """Send a worker to write this lane's first design document from its code."""
    lane = body.get("lane") or ""
    if lane not in amp.config()["lanes"]:
        return {"ok": False, "error": f"unknown lane {lane!r}"}
    if amp.lane_spec_files(lane):
        return {"ok": False, "error":
                f"{lane} already has documents under docs/spec/ - sharpen those instead"}
    if amp.spec_draft_status(lane):
        # The button is disabled while one is out, so reaching here means two
        # tabs, or a stale page. Refused rather than dispatched: the second
        # worker would write the same file in the same worktree as the first.
        return {"ok": False, "error": f"a draft worker is already out for {lane}"}
    out = do_dispatch({
        "lane": lane,
        "prompt": amp.spec_draft_prompt(lane, amp.spec_candidates(lane)),
        "backend": "claude",
        "report_back": False,
        # What makes the worker findable afterwards. Without it the only record
        # that this was a draft is the prompt text.
        "spec_draft": True,
    })
    return out


def do_spec_candidates(lane: str | None) -> dict:
    lane = lane or ""
    if lane not in amp.config()["lanes"]:
        return {"ok": False, "error": f"unknown lane {lane!r}"}
    return {"ok": True, "candidates": amp.spec_candidates(lane)}


def _recover_stuck_specruns() -> list[str]:
    """A spec run whose writer finished while nobody was listening, picked back up.

    Same failure and same test as a stopped consult: the run says it is waiting
    on a worker, and the board says that worker is not running. Stopping is
    indistinguishable from working, so the fact checked is on disk rather than
    remembered.
    """
    moved = []
    for row in amp.specruns():
        if row.get("state") != "running" or row.get("waiting_on") != "worker":
            continue
        r = amp.load_specrun(row["id"])
        rd = ((r or {}).get("rounds") or [{}])[-1]
        tid = rd.get("task_id")
        if not tid or rd.get("worker"):
            continue
        rec = (do_task(r["lane"], tid) or {}).get("task")
        if not rec or rec.get("status") == "running":
            continue
        try:
            amp.spec_worker_done(r["id"], r["lane"], rec)
            moved.append(r["id"])
        except Exception:
            traceback.print_exc()
    return moved


def do_direction_review(body: dict) -> dict:
    """Look back at one finished goal and ask what it was for.

    Slow - it is a full architect call against the repository as it now is - and
    it spends tokens, so it is a request rather than something the tab does on
    open. It fires by itself when a goal finishes; this is for the ones that
    finished before there was anything to fire.
    """
    gid = body.get("goal_id") or ""
    if not gid:
        return {"ok": False, "error": "no goal_id"}
    rev = amp.direction_review(gid)
    return {"ok": bool(rev.get("ok")), "review": rev, "error": rev.get("error")}


def do_direction_explore(body: dict) -> dict:
    """Go looking for direction across every lane instead of waiting for one.

    The other door into the same list. A review needs a goal to have finished,
    so when the fleet is held there is nothing to review and nothing new can
    appear - which is the moment you most want somewhere to go. Slower and
    dearer than a review because it can search the web.
    """
    lane = body.get("lane") or None
    web = body.get("web")
    rev = amp.explore_direction(lane, web=True if web is None else bool(web))
    return {"ok": bool(rev.get("ok")), "review": rev, "error": rev.get("error")}


def do_direction_case(body: dict) -> dict:
    """Write up why the stack is stuck and put it in the orchestrator thread.

    Offered when there is nowhere left to go, because "nowhere" is nearly always
    a decision nobody has made rather than a shortage of ideas, and the harness
    cannot make it. Produces no objective on purpose.
    """
    rev = amp.direction_case(body.get("lane") or None)
    return {"ok": bool(rev.get("ok")), "case": rev, "error": rev.get("error")}


def do_direction_proposal(body: dict) -> dict:
    """Adopt a proposed objective as a real goal, or turn it down.

    Adopting is the operator deciding what gets built, which is why it is a click
    and not a consequence of the review that proposed it.

    The lane's own mode and stage are enforced inside `adopt_proposal`, not
    here. A gate written in a handler is a gate the next client walks around,
    and this handler is not the only way in. What this passes on is the
    operator's answer to it.
    """
    pid = body.get("id") or ""
    action = body.get("action") or ""
    if action == "adopt":
        return amp.adopt_proposal(pid, override=bool(body.get("override")))
    if action == "dismiss":
        p = amp.set_proposal(pid, "dismissed", reason=str(body.get("reason") or "")[:400])
        return {"ok": bool(p), "proposal": p} if p else {"ok": False, "error": "no such proposal"}
    if action == "sharpen":
        return amp.sharpen_proposal(pid)
    return {"ok": False, "error": "action must be adopt, dismiss or sharpen"}


def do_goal_reopen(body: dict) -> dict:
    """Recalculate a live goal's plan, or improve the objective it is aiming at.

    Both are architect calls that rewrite work in flight, so both are a click
    and neither is on the heartbeat. `improve` answers first and applies only
    when asked twice - changing what a goal is for is not a one-click action.
    """
    gid = body.get("goal_id") or ""
    action = body.get("action") or ""
    if not gid:
        return {"ok": False, "error": "no goal_id"}
    if action == "recalculate":
        return amp.recalculate_goal(gid)
    if action == "improve":
        return amp.improve_goal(gid, apply=bool(body.get("apply")))
    return {"ok": False, "error": "action must be recalculate or improve"}


def do_direction_auto(body: dict) -> dict:
    """Let a review open its own goals, or stop letting it.

    Off by default. On, the stack keeps choosing its own next objective inside a
    lane until it says the lane is exhausted - which is the automation Travis
    asked for, and is also exactly the thing rule 6 says is his to switch on.
    """
    return {"ok": True, "auto_adopt": amp.set_auto_adopt(bool(body.get("on")))}


def do_auto_spec(body: dict) -> dict:
    """Let the harness draft, rate and sharpen ONE lane's specs unasked.

    `all` is offered because eleven lanes is eleven clicks, but it is an
    explicit request rather than what one lane's checkbox quietly did before.
    """
    if body.get("all"):
        lanes = sorted(amp.config().get("lanes") or {})
        for name in lanes:
            amp.set_auto_spec(name, bool(body.get("on")))
        return {"ok": True, "lanes": lanes, "on": bool(body.get("on"))}
    lane = body.get("lane") or ""
    if lane not in amp.config()["lanes"]:
        return {"ok": False, "error": f"unknown lane {lane!r}"}
    return {"ok": True, "lane": lane, "on": amp.set_auto_spec(lane, bool(body.get("on")))}


def do_spec_explore(body: dict) -> dict:
    """Ask Direction what to build from this lane's documents, now."""
    lane = body.get("lane") or ""
    if lane not in amp.config()["lanes"]:
        return {"ok": False, "error": f"unknown lane {lane!r}"}
    try:
        return amp.spec_explore(lane)
    except SystemExit as e:
        return {"ok": False, "error": str(e)}


def do_ratify_doctrine() -> dict:
    """The operator, and only the operator, saying the current core stands.

    There is no counterpart for an agent. An amendment is proposed as a finding
    and adopted here, by hand, or it is not adopted.
    """
    return {"ok": True, "doctrine": {**amp.ratify_doctrine(), **amp.doctrine_state()}}


def do_doctrine() -> dict:
    """The workspace's own direction: the file, and what is measurable about it."""
    try:
        raw = amp.DOCTRINE_PATH.read_text()
    except OSError:
        raw = ""
    return {"ok": True, "text": raw, "core": amp.doctrine(),
            "begin": amp.DOCTRINE_BEGIN, "end": amp.DOCTRINE_END,
            "mission": amp.mission(), "workspace": amp.current_workspace(),
            "architect": amp.architect_available(),
            "architect_off": "" if amp.architect_available() else amp.architect_off_reason(),
            "stats": amp.doctrine_stats()}


def do_set_doctrine(body: dict) -> dict:
    return amp.set_doctrine(body.get("text") or "")


def do_draft_doctrine() -> dict:
    """Redraft the doctrine against the evidence. Saves nothing."""
    return amp.draft_doctrine()


def do_review_doctrine() -> dict:
    """Judge the doctrine against the evidence. Saves nothing."""
    return amp.review_doctrine()


# ------------------------------------------------- workspaces, mission, supervisor


def do_workspaces() -> dict:
    return {"ok": True, "workspace": amp.workspace_view(),
            "supervisor": amp.supervisor_view()}


def do_workspace_use(body: dict) -> dict:
    """Point the whole console at another workspace.

    This rebinds every path the harness writes to, so it refuses outright while
    anything is in flight rather than trying to be clever about it. The refusal
    says what is in flight, because "not now" without a reason is the same as a
    bug from where the operator is sitting.
    """
    slug = str(body.get("slug") or "").strip()
    if not slug:
        return {"ok": False, "error": "which workspace?"}
    try:
        ws = amp.use_workspace(slug)
    except amp.WorkspaceError as e:
        return {"ok": False, "error": str(e), "blocked": amp.switch_blocked()}
    amp.add_note(f"switched to the {ws.get('name')!r} workspace")
    return {"ok": True, "workspace": amp.workspace_view()}


def do_workspace_add(body: dict) -> dict:
    """Open a new workspace: no lanes, no board, no goals, its own mission."""
    try:
        ws = amp.add_workspace(str(body.get("name") or ""),
                               mission=str(body.get("mission") or ""))
    except amp.WorkspaceError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "created": ws, "workspace": amp.workspace_view()}


def do_workspace_rename(body: dict) -> dict:
    try:
        amp.rename_workspace(str(body.get("slug") or ""), str(body.get("name") or ""))
    except amp.WorkspaceError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "workspace": amp.workspace_view()}


def do_workspace_remove(body: dict) -> dict:
    """Drop a workspace from the list. Its state stays on disk."""
    try:
        gone = amp.remove_workspace(str(body.get("slug") or ""))
    except amp.WorkspaceError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "removed": gone, "workspace": amp.workspace_view(),
            "note": "the state directory was left on disk; putting the entry back "
                    "restores the workspace"}


def do_lane_move(body: dict) -> dict:
    """Move a lane, its goals and its worktree into another workspace.

    Defaults to a DRY RUN, and the caller has to say `apply: true` to make it
    happen. This is the only call in the console that deletes state as part of
    succeeding - a lane leaves one config and some goal files leave one
    directory - so the shape of it is: ask what would happen, read the answer,
    then say do it.
    """
    lane = str(body.get("lane") or "").strip()
    to = str(body.get("to") or "").strip()
    if not lane or not to:
        return {"ok": False, "error": "which lane, and to which workspace?"}
    apply_ = bool(body.get("apply"))
    # Which workspace the caller was looking at when it decided. Another console
    # against this same state directory can move `current` between the dry run
    # and the apply, and then "the lane I am looking at" names a different lane.
    frm = str(body.get("from") or "").strip() or None
    try:
        out = amp.move_lane(lane, to, dry_run=not apply_, from_slug=frm)
    except amp.WorkspaceError as e:
        return {"ok": False, "error": str(e)}
    if apply_:
        amp.add_note(f"moved the {lane!r} lane to the {to!r} workspace")
    return dict(out, workspace=amp.workspace_view())


def do_mission(body: dict) -> dict:
    """Write what this workspace is for.

    The only text in the harness that no agent may author. It goes out with the
    doctrine to every planner, worker and review from the next prompt onward.
    """
    if "text" not in body:
        return {"ok": False, "error": "nothing to write"}
    try:
        amp.set_mission(str(body.get("text") or ""), body.get("slug") or None)
    except amp.WorkspaceError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "workspace": amp.workspace_view()}


def do_supervise() -> dict:
    """Take a reading: is what is happening still the mission?

    Slow and costs an architect call, like a direction review, and for the same
    reason it is a request rather than something a timer does. Nothing it returns
    is carried out - it names drift, it does not stop anything.
    """
    return amp.supervise_workspace()


def do_supervisor() -> dict:
    return {"ok": True, "supervisor": amp.supervisor_view(),
            "reports": amp.supervisor_store().get("reports", [])[-12:][::-1]}


# ---------------------------------------------------------------- reports


def do_reports() -> dict:
    """Every report taken, and what has moved since the last one.

    `pending` is the same idea the supervisor strip uses: a report is not out of
    date because it is old, it is out of date because the workspace moved after
    it was taken. A quiet week leaves the last report perfectly current.
    """
    rows = amp.reports()
    last = rows[0] if rows else None
    w = amp.report_window((last or {}).get("at"))
    return {"ok": True, "reports": rows[:20], "last": last,
            "pending": sum(w["counts"].values()), "counts": w["counts"],
            "can_web": amp.web_search_backend()}


def do_report(body: dict) -> dict:
    """Take a report. The web search is opt-in: it costs an architect turn."""
    try:
        rec = amp.make_report(web=bool(body.get("web")))
    except Exception:
        traceback.print_exc()
        return {"ok": False, "error": traceback.format_exc(limit=3)}
    return {**rec, "url": "/reports/" + rec["file"]}


def do_solve_report(body: dict) -> dict:
    """Read a report back and propose what to do about it.

    A click, never the heartbeat. It writes proposals into the same store the
    architect writes to, held by the same bars and the same gates, and it adopts
    nothing - so a bad reading costs a dismissal, not a worker.
    """
    try:
        return amp.solve_report(body.get("report_id") or None)
    except Exception:
        traceback.print_exc()
        return {"ok": False, "error": traceback.format_exc(limit=3)}


# ------------------------------------------------------------- obligations

_OBLIGATION_TICK = 300.0   # how often the ticker wakes, not how often a check runs


def do_obligations() -> dict:
    return {"ok": True, "obligations": amp.obligations(),
            "summary": amp.obligations_summary()}


def do_add_obligation(body: dict) -> dict:
    try:
        ob = amp.add_obligation(
            body.get("name") or "", body.get("check") or "",
            why=body.get("why") or "", fix=body.get("fix") or "",
            every_hours=float(body.get("every_hours") or 24),
            lane=body.get("lane") or None, auto_fix=bool(body.get("auto_fix")))
    except SystemExit as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "obligation": ob}


def do_check_obligation(body: dict) -> dict:
    """Run one obligation's check now, on demand."""
    oid = body.get("id") or ""
    ob = next((o for o in amp.obligations() if o["id"] == oid), None)
    if not ob:
        return {"ok": False, "error": f"no obligation {oid!r}"}
    return {"ok": True, "obligation": amp.run_obligation_check(ob)}


def do_set_obligation(body: dict) -> dict:
    oid = body.get("id") or ""
    patch = {k: body[k] for k in ("enabled", "auto_fix", "every_hours", "fix", "check", "why")
             if k in body}
    if not patch:
        return {"ok": False, "error": "nothing to change"}
    ob = amp.update_obligation(oid, patch)
    return {"ok": True, "obligation": ob} if ob else {"ok": False, "error": f"no obligation {oid!r}"}


def do_remove_obligation(body: dict) -> dict:
    oid = body.get("id") or ""
    return ({"ok": True} if amp.remove_obligation(oid)
            else {"ok": False, "error": f"no obligation {oid!r}"})


_PIPELINE_TICK = 60.0


# ---------------------------------------------------------------- blueprint
#
# Three questions, one screen: how the lanes stack, what fires between them,
# and what any given call is actually sent. Every route here is a thin wrapper -
# the module holds the rules, so a second client cannot get a different answer
# by asking a different endpoint.


def _bp(fn, *a, **kw) -> dict:
    """Run a blueprint call and turn its refusals into an answer, not a 500.

    A refusal here is nearly always the operator being told something true -
    "there is no open proposal to sharpen" - and a stack trace would bury it.
    """
    try:
        out = fn(*a, **kw)
    except (ValueError, LookupError) as e:
        return {"ok": False, "error": str(e)}
    return out if isinstance(out, dict) and "ok" in out else {"ok": True, **(out or {})}


def do_blueprint_map() -> dict:
    return {"ok": True, **blueprint.map_view()}


def do_blueprint_triggers() -> dict:
    return {"ok": True, **blueprint.triggers_view()}


def do_blueprint_actions() -> dict:
    return {"ok": True, **blueprint.actions_view()}


def do_blueprint_flow(lens: str, action: str, lane: str, level: str) -> dict:
    return blueprint.flow_view(lens or "agents",
                               {"action": action, "lane": lane, "level": level})


def do_blueprint_ports() -> dict:
    return {"ok": True, **blueprint.ports_view()}


def do_blueprint_context(action: str, lane: str) -> dict:
    return blueprint.context_view(action, lane)


def do_blueprint_stack(body: dict) -> dict:
    op = (body.get("op") or "").strip()
    if op == "add":
        return _bp(blueprint.add_stack, body.get("name") or "", body.get("note") or "")
    if op == "rename":
        return _bp(blueprint.rename_stack, body.get("id") or "",
                   body.get("name") or "", body.get("note"))
    if op == "remove":
        return _bp(blueprint.remove_stack, body.get("id") or "")
    if op == "draft":
        # Reads the lanes and proposes a layering. Writes nothing - `apply` is a
        # separate call precisely so that accepting one is an act the operator
        # performed rather than a side effect of asking.
        return _bp(blueprint.draft_stacks)
    if op == "apply":
        return _bp(blueprint.apply_stacks, body.get("layers") or [])
    return {"ok": False, "error": f"unknown layer operation {op!r}"}


def do_blueprint_place(body: dict) -> dict:
    return _bp(blueprint.place, body.get("lane") or "", body.get("stack") or None)


def do_blueprint_trigger(body: dict) -> dict:
    op = (body.get("op") or "").strip()
    if op == "add":
        return _bp(blueprint.add_trigger, body)
    if op == "set":
        return _bp(blueprint.set_trigger, body.get("id") or "", body.get("patch") or {})
    if op == "remove":
        return _bp(blueprint.remove_trigger, body.get("id") or "")
    if op == "draft":
        # Proposes wiring. Writes nothing - `apply` is a separate call, and even
        # that writes triggers that are OFF.
        return _bp(blueprint.draft_triggers)
    if op == "apply":
        return _bp(blueprint.apply_triggers, body.get("triggers") or [])
    if op == "step":
        # The same pass the heartbeat makes, on demand. It writes proposals and
        # starts nothing, so there is no reason to make the operator wait a tick
        # to see whether what they just wired up actually matches anything.
        return {"ok": True, "fired": blueprint.step()}
    return {"ok": False, "error": f"unknown trigger operation {op!r}"}


def _pipeline_ticker():
    """The fleet's heartbeat: keep every running goal actually running.

    Recovery used to happen once, at boot. That is enough only if the thing that
    interrupts a goal is a restart - and it is not. A dispatch hook that throws,
    a review that dies, a worker settled while the hooks were being rebound: any
    of them leaves a goal quiet, and until now quiet lasted until someone looked.

    Restarting an idle goal costs one worker's budget on work the goal already
    said it wanted, so it is safe to do unattended. Restarting a *stopped* goal
    is not - it stopped for a reason - and this does not touch those.
    """
    while True:
        time.sleep(_PIPELINE_TICK)
        try:
            for gid in _restart_idle_goals():
                print(f"amp: restarted idle goal {gid}")
        except Exception:
            traceback.print_exc()
        # The same halt one state earlier. `_restart_idle_goals` only ever looks
        # at goals that say `running`, so a goal interrupted while it was still
        # being PLANNED is invisible to it and to every gate - and it holds a
        # lane's worktree for good. Measured: `wrl` lost a lane that way for an
        # hour and twenty minutes, with four proposals waiting in it.
        try:
            for what in amp.reap_stranded_plans():
                print(f"amp: abandoned a plan that never arrived — {what}")
        except Exception:
            traceback.print_exc()
        # The queue is otherwise drained only by a settling worker, which is
        # sound only while every enqueue has a live worker behind it - true
        # today, since you can only be queued for a busy lane or a full cap, but
        # enforced nowhere. If that ever stops holding, the queue holds work,
        # nothing is running, and nothing is looking: the exact shape of the
        # halt this ticker was written for. Not a diagnosed bug; a no-op when
        # there is nothing to start.
        try:
            _drain_queue()
        except Exception:
            traceback.print_exc()
        # A goal that stopped to ask something is the one halt the harness is
        # not allowed to clear on its own - so it is handed to the orchestrator
        # instead, which reads what can be read and puts what cannot in front of
        # the operator as one decision rather than a wall of prose. Blocking,
        # deliberately: it runs on this thread so two triages cannot overlap on
        # a single orchestrator session, and the tick is 60s against a turn of
        # perhaps 30.
        try:
            for gid in amp.triage_blocked_goals(BASE_URL):
                print(f"amp: triaged blocked goal {gid}")
        except Exception:
            traceback.print_exc()
        # Before adoption, because it is the thing most likely to be stopping
        # it. The contradictions gate holds every lane at once and was cleared
        # only by hand, so an unattended fleet does not converge on running - it
        # converges on stopped, three contradictions deep, with a full worker
        # cap and nothing using it.
        try:
            r = amp.settle_contradictions()
            if r:
                print(f"amp: settled {r['settled']} of {r['considered']} contradiction(s)"
                      + (f", {r['kept']} left for you" if r.get("kept") else "")
                      + ("" if r.get("ok") else f" — failed: {r.get('error')}"))
        except Exception:
            traceback.print_exc()
        # An escalation gate is meant to pause the pipeline, not to end a lane -
        # but auto-adopt is judged once, as a goal closes, so a gate that was up
        # at that moment left the lane's proposals waiting on a reader that was
        # never going to come back. This is the reader. It re-tests the same
        # gates and adopts nothing they still cover.
        try:
            for r in amp.resume_adoption():
                if r.get("ok"):
                    print(f"amp: adopted waiting proposal {r['proposal_id']}")
        except Exception:
            traceback.print_exc()
        # Adoption above can only start what has already been proposed, and a
        # lane with no goal has nothing left to close - so nothing will ever
        # propose into it again. Measured: five of eleven lanes were in exactly
        # that state and the fleet sat at three workers against a cap of ten,
        # held by no gate, no bar and no budget. This is the only thing that
        # goes looking for a lane nobody is going to mention again. Runs AFTER
        # adoption so a tick that has real work to start spends itself starting
        # it rather than inventing more.
        try:
            for r in amp.explore_idle_lanes():
                print(f"amp: explored idle lane {r['lane']} -> "
                      + (f"{r['proposed']} proposal(s)" if r.get("ok")
                         else f"failed: {r.get('error')}"))
        except Exception:
            traceback.print_exc()
        # A spec run that nobody is coming back to holds its lane's one writer,
        # so every later selection is refused over work that has already stopped.
        # There is no queue to step any more - a selection opens one run over the
        # whole set - but a stranded run still has to be picked up or stopped, and
        # this is the only thing that does it.
        try:
            for r in amp.spec_reap():
                print(f"amp: spec run {r['id']} ({', '.join(r['rels']) or '?'}) "
                      f"-> {r.get('state')}: {r.get('why')}")
        except Exception:
            traceback.print_exc()
        # Draft what is missing, rate what exists, sharpen what is under the bar.
        # One step for the whole stack per tick, and only with `auto_spec` on.
        # After the reaper, so a lane whose run has just been picked back up is
        # seen as busy here rather than being handed a second thing to do.
        try:
            for r in amp.spec_auto_run():
                if r.get("ok"):
                    print(f"amp: spec loop {r['lane']} -> {r['did']}"
                          + (f" {r.get('rel')} {r.get('solidity')}" if r["did"] == "rate"
                             else f" proposed {r.get('proposed')} goal(s)" if r["did"] == "explore"
                             else f" — {r.get('thin')} thin document(s), none worth a round"
                             if r["did"] == "settled"
                             else f" ({r.get('chose', r.get('attempt'))} document(s))"
                             if r["did"] == "campaign"
                             else f" ({r.get('attempt')})"))
                else:
                    print(f"amp: spec loop {r['lane']} {r['did']}: {r.get('error')}")
        except Exception:
            traceback.print_exc()
        # Write the missing checks, waive what the architect ruled uncheckable,
        # open the pull request, merge it once GitHub's own rollup passes, and
        # send a worker at a conflict. One step for the whole stack per tick,
        # and only on lanes with `auto_handoff` on - see `handoff_auto_on`,
        # where the lane's mode and stage both outrank that switch.
        try:
            for r in amp.handoff_auto_run():
                if r.get("ok"):
                    print(f"amp: handoff loop {r['lane']} -> {r['did']}"
                          + (f" {r.get('goal_id')}" if r.get("goal_id") else "")
                          + (f" {r.get('url')}" if r.get("url") else ""))
                else:
                    print(f"amp: handoff loop {r['lane']} {r['did']}: "
                          f"{r.get('error') or r.get('result', {}).get('blocked')}")
        except Exception:
            traceback.print_exc()
        # What the operator drew on the Blueprint. A trigger writes an UNSCORED
        # proposal and stops - it dispatches nothing and spends nothing - so it
        # runs here, ahead of the sharpener, and everything downstream treats
        # what it wrote exactly like any other proposal: scored first, then held
        # or adopted by the same bars. A diagram cannot start work.
        try:
            for r in blueprint.step():
                print(f"amp: blueprint trigger {r['trigger']} fired on {r['lane']}"
                      f" -> proposed {r['proposal']} in {r['into']} ({r['note']})")
        except Exception:
            traceback.print_exc()
        # Anything still held back is held for one of two reasons - no odds on
        # it, or odds under the bar - and both are answerable by looking harder
        # at the objective. Runs after adoption so a proposal that is already
        # good enough is started rather than re-examined.
        try:
            for r in amp.auto_sharpen():
                if r.get("ok"):
                    print(f"amp: sharpened {r['proposal_id']} -> {amp.pct(r['confidence'])}"
                          + (" (superseded)" if r.get("superseded") else ""))
                else:
                    print(f"amp: sharpen failed: {r.get('error')}")
        except Exception:
            traceback.print_exc()


def _obligation_ticker():
    """Wake periodically, run whatever check is due, and report drift.

    The checks run here rather than on a browser poll because an obligation that
    is only evaluated when someone is looking at the page is not a standing
    obligation - it is a manual task with extra steps.

    This thread never writes to a repository. It runs read-only checks and files
    what they said; repairing drift is a dispatch, and a dispatch into the shared
    checkout is the operator's decision (see the note on auto_fix in amp.py).
    """
    while True:
        try:
            for ob in amp.obligations():
                if not amp.obligation_due(ob):
                    continue
                was = ob.get("state")
                done = amp.run_obligation_check(ob)
                if done.get("state") == "drifted" and was != "drifted":
                    amp.add_note(f"obligation {done['name']!r} is no longer current: "
                                 f"its check exited {done.get('last_rc')}.",
                                 lane=done.get("lane"))
        except Exception:
            traceback.print_exc()
        time.sleep(_OBLIGATION_TICK)


def do_continue_consult(body: dict) -> dict:
    """Push a halted thread one more time.

    Threads carry themselves now, but three kinds stop: ones that ran out of
    automatic rounds, ones that went round in circles, and every thread that was
    already sitting there before any of this existed. This is the kick. It clears
    the two limits that are about the loop rather than about the money, so the
    same button cannot be leaned on to spend without end.
    """
    cid = body.get("consult_id") or ""
    c = amp.load_consult(cid)
    if not c:
        return {"ok": False, "error": f"no consult {cid!r}"}
    if c.get("cost_tokens", 0) >= amp.AUTO_TOKEN_CEILING:
        return {"ok": False, "error":
                f"this thread has spent {c['cost_tokens']} tokens, past the "
                f"{amp.AUTO_TOKEN_CEILING} it is allowed to continue itself on. "
                "Answer it, or open a fresh thread with what you have learned."}
    c["auto_rounds"] = 0
    c["need_trail"] = []
    amp.save_consult(c)
    c = amp.auto_continue(cid)
    return {"ok": True, "consult": _consult_view(c), "ruling": amp.last_ruling(c)}


def do_close_consult(body: dict) -> dict:
    c = amp.load_consult(body.get("consult_id") or "")
    if not c:
        return {"ok": False, "error": "no such consult"}
    c["status"] = "closed"
    amp.save_consult(c)
    return {"ok": True}


def do_cancel(body: dict) -> dict:
    """Stop a runaway worker and everything it spawned."""
    lane = body.get("lane")
    task_id = body.get("task_id")
    if not task_id:
        recs = amp.board().get("tasks", {}).get(lane, [])
        rec = next((r for r in recs if r.get("status") == "running"), None)
        if not rec:
            return {"ok": False, "error": f"nothing is running in {lane!r}"}
        task_id = rec["task_id"]
    if not amp.cancel_worker(task_id):
        # The record can say `running` after a server restart, when the process
        # it names is long gone. Settle it rather than leaving the lane stuck.
        amp.update_task(lane, task_id, {"status": "cancelled", "finished_at": amp.now(),
                                        "error": "no live process - the record was stale"})
        return {"ok": True, "stale": True, "task_id": task_id}
    return {"ok": True, "stale": False, "task_id": task_id}


def do_restart(body: dict) -> dict:
    """Stop a worker and dispatch its own prompt again, fresh.

    Deliberately a button and not a rule. A restart is a second full budget on
    a task that has already spent one, and a worker that hung once on the same
    prompt can hang on it twice - so the harness stops the hung one by itself
    and leaves the decision to spend again here.
    """
    lane = (body.get("lane") or "").strip()
    task_id = (body.get("task_id") or "").strip()
    recs = amp.board().get("tasks", {}).get(lane, [])
    rec = (next((r for r in recs if r.get("task_id") == task_id), None) if task_id
           else next((r for r in recs if r.get("status") == "running"), None))
    if not rec:
        return {"ok": False, "error": f"no such task in {lane!r}"}
    if not rec.get("prompt"):
        return {"ok": False, "error": "that task has no prompt to run again"}
    if rec.get("status") == "running":
        do_cancel({"lane": lane, "task_id": rec["task_id"]})
        # The kill usually beats the dispatch to the lane lock, but not always.
        # It does not matter which wins: a dispatch that arrives while the lock
        # is still held is queued, and the dying worker's own settle starts it.
    return do_dispatch({"lane": lane, "prompt": rec["prompt"],
                        "branch": rec.get("branch"), "model": rec.get("model"),
                        "budget": rec.get("budget_usd")})


def do_diff(lane: str, attempt: int | None, task_id: str | None) -> dict:
    cfg = amp.config()
    l = cfg["lanes"].get(lane)
    if not l:
        return {"ok": False, "error": f"unknown lane {lane!r}"}
    if amp.lane_backend(l) == "claude":
        diff = amp.claude_diff(lane, l)
        if not diff:
            note = ("no worker has run in this lane yet"
                    if not (amp.WORKTREE_DIR / lane).exists()
                    else "the worker ran but changed nothing")
            return {"ok": True, "branch": f"amp/{lane}", "diff": "", "note": note}
        return {"ok": True, "branch": f"amp/{lane}", "diff": diff}

    b = amp.board()
    if not task_id:
        tasks = b.get("remote", {}).get(lane, [])
        if not tasks:
            return {"ok": False, "error": f"no polled tasks for {lane!r} - poll first"}
        task_id = tasks[0].get("task_id")
    if not task_id:
        return {"ok": False, "error": "task has no id in the codex payload"}
    cmd = ["codex", "cloud", "diff", str(task_id)]
    if attempt:
        cmd += ["--attempt", str(attempt)]
    p = amp.run(cmd)
    return {
        "ok": p.returncode == 0,
        "task_id": task_id,
        "diff": p.stdout,
        "error": p.stderr.strip() if p.returncode != 0 else None,
    }


def _preview_root(lane: str, source: str, subdir: str) -> tuple[Path | None, str]:
    """The directory a preview serves. Returns (path, error).

    Two sources, and the default is the worktree: the whole point is to see
    what the worker just did, and the worker never touches the live checkout.
    """
    l = amp.config()["lanes"].get(lane)
    if not l:
        return None, f"unknown lane {lane!r}"
    base = (amp.WORKTREE_DIR / lane if source == "worktree"
            else (ROOT / l["path"]).resolve())
    if not base.is_dir():
        return None, ("no worker has run in this lane yet, so it has no worktree"
                      if source == "worktree" else f"{base} is missing")
    target = (base / (subdir or "")).resolve()
    # A subdirectory is a place inside the tree, not a way out of it.
    if not str(target).startswith(str(base.resolve())):
        return None, "that subdirectory is outside the lane"
    if not target.is_dir():
        return None, f"{subdir!r} is not a directory in this lane"
    return target, ""


def do_preview(lane: str, source: str, subdir: str) -> dict:
    """What is running, and what this tree looks like it wants to be served as."""
    p = preview.get(lane)
    out = {"ok": True, "preview": p.view() if p else None,
           "sources": [s for s in ("worktree", "repo")
                       if _preview_root(lane, s, "")[0] is not None]}
    root, err = _preview_root(lane, source or "worktree", subdir or "")
    if root is None:
        return {**out, "root": None, "detected": None, "note": err}
    return {**out, "root": str(root), "detected": preview.detect(root)}


def do_preview_start(body: dict) -> dict:
    lane = (body.get("lane") or "").strip()
    root, err = _preview_root(lane, body.get("source") or "worktree",
                              (body.get("dir") or "").strip())
    if root is None:
        return {"ok": False, "error": err}
    return preview.start(lane, root, body.get("mode") or "static",
                         body.get("cmd") or "")


def do_preview_stop(body: dict) -> dict:
    return preview.stop((body.get("lane") or "").strip())


def do_preview_stamp(lane: str) -> dict:
    """A fingerprint of the served tree, so the page can reload itself when a
    worker changes it. Cheap enough to poll, which is the only requirement."""
    p = preview.get(lane)
    if p is None:
        return {"ok": False, "error": "nothing is previewing this lane"}
    return {"ok": True, "stamp": preview.stamp(p.root), "state": p.state, "url": p.url}


def do_apply(body: dict) -> dict:
    lane = body.get("lane")
    cfg = amp.config()
    l = cfg["lanes"].get(lane)
    if not l:
        return {"ok": False, "error": f"unknown lane {lane!r}"}
    if amp.lane_backend(l) == "claude":
        # The work is already a real branch. Merging it into the shared checkout
        # is the user's call - other sessions are editing that tree right now.
        return {
            "ok": False,
            "error": (
                f"claude work lives on branch amp/{lane} in {amp.WORKTREE_DIR / lane}. "
                f"Merge it yourself: git -C {ROOT / l['path']} merge amp/{lane}"
            ),
        }
    task_id = body.get("task_id")
    if not task_id:
        tasks = amp.board().get("remote", {}).get(lane, [])
        if not tasks:
            return {"ok": False, "error": "no polled tasks - poll first"}
        task_id = tasks[0].get("task_id")
    cmd = ["codex", "cloud", "apply", str(task_id)]
    if body.get("attempt"):
        cmd += ["--attempt", str(body["attempt"])]
    p = amp.run(cmd, cwd=ROOT / l["path"])
    return {
        "ok": p.returncode == 0,
        "output": (p.stdout + p.stderr).strip(),
        "task_id": task_id,
        "path": l["path"],
    }


def do_credits() -> dict:
    import urllib.request

    key = amp.find_openrouter_key()
    if not key:
        return {"ok": False, "error": "no OpenRouter key"}
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read().decode())["data"]
        return {"ok": True, "remaining": round(d["total_credits"] - d["total_usage"], 2)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def do_lane_add(body: dict) -> dict:
    """Register a new lane. The orchestrator can reach this, and should.

    Deciding that a body of work deserves its own lane is exactly the judgement
    the orchestrator is for, and until this existed it could see the gap, say so,
    and then be stuck - `lane add` was terminal-only, so noticing was as far as
    it could get.
    """
    try:
        lane = amp.add_lane(
            (body.get("lane") or body.get("name") or "").strip(),
            repo=(body.get("repo") or "").strip() or None,
            path=(body.get("path") or "").strip() or None,
            branch=(body.get("branch") or "main").strip(),
            backend=(body.get("backend") or amp.DEFAULT_BACKEND).strip(),
            env_id=(body.get("env_id") or "").strip() or None,
            replace=bool(body.get("replace")),
        )
    except amp.LaneError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "lane": lane}


def do_lanes() -> dict:
    """Every lane, its mode, and what that mode is currently holding.

    A read, and deliberately a whole-stack one rather than per-lane: the question
    Settings answers is "what is this workspace allowed to do", and that is not
    answerable a lane at a time - a lane sitting at build looks identical whether
    the rest of the stack is running or entirely frozen.
    """
    return amp.lanes_view()


def do_deploy(lane: str = "") -> dict:
    """What could be deployed, what credential it needs, and whether that works.

    Slow enough to be worth saying so: it runs each provider's own `whoami`, and
    those reach the network. Asked on opening the tab rather than on the
    heartbeat, because a credential does not change on its own - it changes when
    the operator signs in, which is the moment they are looking at this.

    `lane` narrows it to that lane's targets. The narrowing happens in
    `amp.deploy_view`, not here, so the counts it returns are counts of what it
    returned - a filter applied at the edge would leave every total behind
    describing a list the operator cannot see.
    """
    return {"ok": True, **amp.deploy_view(lane or None)}


def do_deploy_pages(lane: str = "") -> dict:
    """Every Cloudflare Pages site, and which commit each one is serving.

    Its own call rather than part of `do_deploy`, because it is a wrangler
    start-up per project - thirty-eight of them - and the answer to "which
    credential is missing" should not wait behind the answer to "which commit is
    live". The tab paints the first, then fills in the second.
    """
    return {"ok": True, **amp.cf_pages_view(lane or None)}


def do_deploy_preflight(body: dict) -> dict:
    """Everything that would stop one publish. The same code that gates it.

    Deliberately the same call `start_deploy` makes rather than a second
    description of the rules, so the sentence the operator reads before pressing
    Publish and the sentence that refuses the publish cannot disagree.
    """
    key = (body.get("key") or "").strip()
    t = next((x for x in amp.deploy_targets() if amp.deploy_key(x) == key), None)
    if not t:
        return {"ok": False, "error": f"nothing here is called {key!r}"}
    return {"ok": True, "target": t, "preflight": amp.deploy_preflight(t)}


def do_deploy_run(body: dict) -> dict:
    """Start one check, or one real publish.

    `publish` has to arrive as exactly `true` and is false by default: the
    difference between these two is money and production, and a default that
    reached either because a field was omitted is the wrong default.
    """
    return amp.start_deploy((body.get("key") or "").strip(),
                            publish=body.get("publish") is True)


def do_deploy_status(body: dict) -> dict:
    """How the running one is doing, or how the last one went."""
    key = (body.get("key") or "").strip()
    live = amp.deploy_status(key)
    if live:
        return {"ok": True, "run": live, "running": True}
    past = [r for r in amp.deploy_runs() if r.get("key") == key]
    return {"ok": True, "run": past[-1] if past else None, "running": False}


def do_deploy_cancel(body: dict) -> dict:
    return amp.cancel_deploy((body.get("key") or "").strip())


def do_deploy_history(body: dict) -> dict:
    """Every publish recorded, newest first - the evidence for the rung."""
    rows = amp.deploy_runs()
    lane = (body.get("lane") or "").strip()
    if lane:
        rows = [r for r in rows if r.get("lane") == lane]
    return {"ok": True, "runs": list(reversed(rows))[:100]}


def do_prs(lane: str = "") -> dict:
    """The handoff across every lane: what is finished, and what is being tested.

    The slowest read in the console and worth it once, on opening the tab. It
    asks GitHub about every lane repository and builds a publish report for every
    finished goal. Neither is on the heartbeat: a pull request appears when
    somebody opens one, and the checks behind these reports are read from disk
    rather than re-run, which is exactly why they are marked stale.

    `lane` narrows it, and the narrowing happens in `amp.pr_view` rather than
    here - the repositories the lane does not point at are then never asked
    about at all, which is most of why the narrowed pane is the quick one.
    """
    return {"ok": True, **amp.pr_view(lane or None)}


def do_pr_report(body: dict) -> dict:
    """One goal's gate, in full - every condition and what decided it."""
    gid = (body.get("goal_id") or "").strip()
    g = amp.load_goal(gid)
    if not g:
        return {"ok": False, "error": f"no goal {gid!r}"}
    rep = amp.publish_report(gid, rerun=bool(body.get("rerun")))
    return {"ok": True, "report": rep, "tally": amp.condition_tally(g)}


def do_pr_write_checks(body: dict) -> dict:
    """Ask the architect for the commands a goal never got, and keep them or not.

    `apply` is false by default for the same reason `publish` is over on deploy:
    a check outranks every judgement in this harness, so one arriving from a
    model gets read by a person before it starts deciding whether work may be
    handed over.
    """
    return amp.write_checks((body.get("goal_id") or "").strip(),
                            apply=body.get("apply") is True)


def do_pr_waive(body: dict) -> dict:
    """Record a person taking responsibility for an unproven condition.

    Reachable only from here. Nothing on the ticker calls into it, and that is
    deliberate - the value of a waiver is entirely that somebody accepted a
    consequence, and a program cannot accept one on its own behalf.
    """
    gid = (body.get("goal_id") or "").strip()
    text = body.get("text") or ""
    if body.get("undo") is True:
        return amp.unwaive_condition(gid, text)
    return amp.waive_condition(gid, text, body.get("why") or "")


def do_pr_merge(body: dict) -> dict:
    """Read GitHub's verdict on a pull request, and merge it if it passed.

    `confirm: true` is required, and without it this is a preflight that touches
    nothing - the same call, so what the operator is shown before accepting and
    what refuses the merge are one piece of code. The gate itself lives in
    `amp.merge_blockers` and is enforced there, not here: a gate that lives in a
    request handler is a gate a second client walks around.
    """
    repo = (body.get("repo") or "").strip()
    number = body.get("number")
    if not repo or not isinstance(number, int):
        return {"ok": False, "error": "a repository and a pull request number are needed"}
    return amp.merge_pr(repo, number, confirm=body.get("confirm") is True,
                        method=(body.get("method") or "squash"))


def do_pr_resolve(body: dict) -> dict:
    """Try the cheapest tier that has not been tried on a stuck pull request.

    Left as one route with a `tier` rather than three, because `auto` choosing
    the tier IS the feature - the operator asking for a re-run of something that
    has already been re-run twice is the loop this replaces.
    """
    repo = (body.get("repo") or "").strip()
    number = body.get("number")
    if not repo or not isinstance(number, int):
        return {"ok": False, "error": "a repository and a pull request number are needed"}
    return amp.resolve_pr(repo, number, tier=(body.get("tier") or "auto"))


def do_pr_auto(body: dict) -> dict:
    """Switch the unattended handoff on or off for one lane.

    Per lane and not per stack, for the same reason `do_auto_spec` is: the
    lanes are not equally ready to have their work merged without being asked,
    and the one thing an operator needs is to exclude the ones that are not.

    The reply carries `running` as well as `on`, because the switch is not the
    only thing that decides: the lane's mode and its stage ceiling both outrank
    it, and a checkbox that ticked next to a lane whose stage stops at `code`
    would be reporting a thing that is not going to happen.
    """
    lane = (body.get("lane") or "").strip()
    if lane not in (amp.config().get("lanes") or {}):
        return {"ok": False, "error": f"unknown lane {lane!r}"}
    on = amp.set_auto_handoff(lane, bool(body.get("on")))
    return {"ok": True, "lane": lane, "on": on,
            "running": amp.handoff_auto_on(lane),
            "why": amp.lane_admits(lane, "development")
                   or amp.stage_admits(lane, "review")}


def do_pr_auto_step(body: dict) -> dict:
    """Run one step of the handoff on one lane, now.

    The same call the heartbeat makes, reachable without waiting for it - but
    NOT without the switch, which is checked inside `handoff_auto_step` rather
    than here. A button that ran the loop on a lane the operator had switched
    off would be a second way in past the only control there is.
    """
    lane = (body.get("lane") or "").strip()
    if lane not in (amp.config().get("lanes") or {}):
        return {"ok": False, "error": f"unknown lane {lane!r}"}
    return amp.handoff_auto_step(lane)


def do_set_lane_mode(body: dict) -> dict:
    """Change what a lane may START. Never stops what it is already doing.

    The reply carries `in_flight` straight back to the caller, because the one
    way this switch can lie is by looking instantaneous: an operator freezes a
    lane, sees the row go grey, and then finds a worker still committing to it an
    hour later. Naming the goals here means the console can say so at the moment
    of the click rather than the operator discovering it from the board.
    """
    try:
        return {"ok": True, **amp.set_lane_mode(body.get("lane") or "",
                                                body.get("mode") or "")}
    except amp.LaneError as e:
        return {"ok": False, "error": str(e)}


def do_set_lane_direction(body: dict) -> dict:
    """Write what a lane is FOR - the other half of what `mode` says it MAY do.

    Sent as a whole object rather than one field at a time, because a direction
    is read as one statement: a `bar` written against an older `claim` reads as
    confident and is wrong, and the cheapest way to make that impossible is to
    make partial writes impossible. Sending `{}` clears it, which is a real
    answer - a lane whose purpose nobody can state is better shown as having
    none than as having a stale one.
    """
    try:
        return {"ok": True, "lane": body.get("lane") or "",
                "direction": amp.set_lane_direction(body.get("lane") or "",
                                                    body.get("direction") or {})}
    except amp.LaneError as e:
        return {"ok": False, "error": str(e)}


def do_lane_directions() -> dict:
    """Every lane's direction, with the mission it sits under."""
    rungs = amp.lane_rungs()
    return {"ok": True, "mission": amp.mission(),
            "workspace": amp.current_workspace(),
            "fields": list(amp.DIRECTION_FIELDS),
            "after_bar": amp.AFTER_BAR,
            "default_after_bar": amp.DEFAULT_AFTER_BAR,
            "architect": amp.architect_available(),
            "architect_off": "" if amp.architect_available() else amp.architect_off_reason(),
            # The judged rung beside the written claim, because the one failure
            # this screen exists to catch is a direction that says more than the
            # lane's reviews ever did, and that is invisible while the two facts
            # live on different pages.
            "lanes": [{"name": n, "mode": amp.lane_mode(n),
                       "rung": rungs.get(n, ""),
                       "direction": amp.lane_direction(n)}
                      for n in sorted(amp.config().get("lanes") or {})]}


def do_draft_lane_direction(body: dict) -> dict:
    """Redraft one lane's direction against its own evidence. Saves nothing."""
    return amp.draft_lane_direction(body.get("lane") or "")


def do_set_lane_stage(body: dict) -> dict:
    """Change how far the harness may carry a lane by itself.

    The other axis from `mode`, and deliberately its own call: a mode says what
    kind of work may start, a stage says how far it may be taken, and folding
    them into one control would make choosing either of them a choice about
    both.
    """
    try:
        return {"ok": True, **amp.set_lane_stage(body.get("lane") or "",
                                                 body.get("stage") or "")}
    except amp.LaneError as e:
        return {"ok": False, "error": str(e)}


def do_flow(lane: str | None) -> dict:
    """Every rung of the pipeline for one lane, and what is stopping each.

    Read-only and off disk. It takes no network reading, so it opens instantly -
    the two rungs whose real state lives behind a `gh` or a `wrangler` call name
    the tab that takes that reading rather than taking it here.
    """
    if not lane:
        return {"ok": False, "error": "pick a lane first — the flow is a lane's flow"}
    if lane not in (amp.config().get("lanes") or {}):
        return {"ok": False, "error": f"unknown lane {lane!r}"}
    return {"ok": True, "flow": amp.lane_flow(lane)}


def do_bind_env(body: dict) -> dict:
    cfg = amp.config()
    name, env_id = body.get("lane"), (body.get("env_id") or "").strip()
    if name not in cfg["lanes"]:
        return {"ok": False, "error": f"unknown lane {name!r}"}
    if not env_id:
        return {"ok": False, "error": "env_id is empty"}
    cfg["lanes"][name]["env_id"] = env_id
    amp.save_json(amp.CONFIG_PATH, cfg)
    return {"ok": True, "lane": name, "env_id": env_id}


def do_set_backend(body: dict) -> dict:
    cfg = amp.config()
    name, backend = body.get("lane"), body.get("backend")
    if name not in cfg["lanes"]:
        return {"ok": False, "error": f"unknown lane {name!r}"}
    if backend not in amp.BACKENDS:
        return {"ok": False, "error": f"backend must be one of {', '.join(amp.BACKENDS)}"}
    cfg["lanes"][name]["backend"] = backend
    amp.save_json(amp.CONFIG_PATH, cfg)
    return {"ok": True, "lane": name, "backend": backend}


# Sign-in is a two-call conversation with one live pty, so the pending flow is
# held here between /api/auth/start and /api/auth/code.
_LOGIN: amp.ClaudeLogin | None = None
_LOGIN_LOCK = threading.Lock()


def do_auth_start() -> dict:
    global _LOGIN
    with _LOGIN_LOCK:
        if _LOGIN:
            _LOGIN.close()
        lg = amp.ClaudeLogin()
        try:
            url = lg.start()
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
        _LOGIN = lg
        amp.open_url(url)          # Chrome, per browser_cmd()
        return {"ok": True, "url": url}


def do_auth_code(body: dict) -> dict:
    global _LOGIN
    code = (body.get("code") or "").strip()
    if not code:
        return {"ok": False, "error": "paste the code from the sign-in page"}
    with _LOGIN_LOCK:
        lg = _LOGIN
        if not lg:
            return {"ok": False, "error": "sign-in expired - click Connect Claude again"}
        try:
            token = lg.submit(code)
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
        finally:
            _LOGIN = None
        amp.store_claude_token(token)
    return {"ok": True, "connected": True}


# The deploy providers sign in the same way the ChatGPT flow does - a URL, a
# browser, and a CLI that polls and exits - so this is the same two-call shape.
# One slot, not one per provider: two sign-ins at once would each hold a pty
# waiting on the same operator and the same browser, and the second one would be
# approving a page the first one opened.
_PROVIDER_LOGIN: amp.ProviderLogin | None = None
_PROVIDER_LOCK = threading.Lock()


def do_provider_login(body: dict) -> dict:
    global _PROVIDER_LOGIN
    provider = (body.get("provider") or "").strip()
    with _PROVIDER_LOCK:
        if _PROVIDER_LOGIN:
            _PROVIDER_LOGIN.close()
        lg = amp.ProviderLogin(provider)
        try:
            out = lg.start()
        except (RuntimeError, OSError) as e:
            return {"ok": False, "error": amp.redact(str(e))}
        _PROVIDER_LOGIN = lg
        amp.open_url(out["url"])
        return {"ok": True, "provider": provider, **out}


def do_provider_poll() -> dict:
    """Has the operator approved it yet? Answered by the provider, not the CLI."""
    global _PROVIDER_LOGIN
    with _PROVIDER_LOCK:
        lg = _PROVIDER_LOGIN
        if not lg:
            return {"ok": False, "state": "failed", "error": "no sign-in is running"}
        try:
            r = lg.poll()
        except OSError as e:
            r = {"state": "failed", "error": amp.redact(str(e))}
        if r.get("state") != "pending":
            _PROVIDER_LOGIN = None
        return {"ok": r.get("state") == "connected", "provider": lg.provider, **r}


# The ChatGPT flow is also two calls over one live pty, but the second one asks
# "did they approve it yet?" rather than carrying anything back - see CodexLogin.
_CODEX_LOGIN: amp.CodexLogin | None = None
_CODEX_LOCK = threading.Lock()


def do_codex_auth_start() -> dict:
    global _CODEX_LOGIN
    with _CODEX_LOCK:
        if _CODEX_LOGIN:
            _CODEX_LOGIN.close()
            _CODEX_LOGIN = None
        # `fresh`: this decides whether to run a sign-in at all, so a cached
        # answer could either start a flow the CLI then refuses, or skip one
        # the operator actually needs.
        if amp.codex_logged_in(fresh=True):
            # Starting a flow that the CLI will refuse, and then reporting its
            # refusal, would be a confusing way to say "already done".
            return {"ok": True, "connected": True}
        lg = amp.CodexLogin()
        try:
            out = lg.start()
        except RuntimeError as e:
            return {"ok": False, "error": str(e)}
        _CODEX_LOGIN = lg
        amp.open_url(out["url"])        # Chrome, per browser_cmd()
        return {"ok": True, **out}


def do_codex_auth_poll() -> dict:
    global _CODEX_LOGIN
    with _CODEX_LOCK:
        lg = _CODEX_LOGIN
        if not lg:
            # A poll with nothing running is not necessarily an error: the flow
            # may have finished and been cleared. Answer from the CLI, and
            # `fresh`, because "may have just finished" is exactly the window a
            # cached answer would still be reporting the state before.
            if amp.codex_logged_in(fresh=True):
                return {"ok": True, "connected": True}
            return {"ok": False, "error": "sign-in is not running - click Connect ChatGPT"}
        out = lg.poll()
        if out["state"] != "pending":
            _CODEX_LOGIN = None
        if out["state"] == "failed":
            return {"ok": False, "error": out["error"]}
        return {"ok": True, "connected": out["state"] == "connected",
                "url": out.get("url"), "code": out.get("code")}


def do_settings() -> dict:
    """What is switched on, and what each switch actually stops."""
    cfg = amp.config()
    return {
        "ok": True,
        "architect_backend": amp.architect_backend(),
        "architect_backends": list(amp.ARCHITECT_BACKENDS),
        "architect_ready": amp.architect_available(),
        "architect_reason": None if amp.architect_available() else amp.architect_off_reason(),
        "codex_installed": amp.codex_available(),
        "codex_logged_in": amp.codex_logged_in(),
        "openrouter_enabled": amp.openrouter_enabled(),
        "openrouter_key": bool(amp.find_openrouter_key()),
        "auto_escalate": bool(cfg.get("auto_escalate", True)),
        "architect_actions": amp.ARCHITECT_ACTIONS,
        "auto_max_rounds": amp.AUTO_MAX_ROUNDS,
        "roles": amp.role_view(),
        # The dial and the evidence for it, together. A threshold shown without
        # what the scores behind it actually turned out to be worth is a number
        # asking to be trusted, and there is no reason to.
        "adopt_confidence": amp.adopt_bar(),
        "adopt_need": amp.need_bar(),
        "calibration": amp.calibration(),
        "spec_max_rounds": amp.spec_max_rounds(),
        "spec_max_rounds_limit": amp.SPEC_MAX_ROUNDS_LIMIT,
    }


def do_role_set(body: dict) -> dict:
    """Turn a role on or off, or change which model answers as it."""
    role = body.get("role")
    on = body.get("on")
    model = body.get("model")
    if on is None and model is None:
        return {"ok": False, "error": "nothing to set - expected `on` or `model`"}
    try:
        roles = amp.set_role(role, on=None if on is None else bool(on), model=model)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    return {**do_settings(), "roles": roles}


SETTINGS_FLAGS = ("openrouter_enabled", "auto_escalate")
# Not a flag: it names one of a fixed set, and an unrecognised name is refused
# here rather than written and silently fallen back from at read time.
SETTINGS_CHOICES = {"architect_backend": amp.ARCHITECT_BACKENDS}
# Neither: probabilities, and they live under `autonomy` rather than at the top
# level because they are thresholds on how much runs without being watched.
SETTINGS_BARS = ("adopt_confidence", "adopt_need")
# Whole numbers with a stated range, refused here rather than clamped at read
# time: a cap silently held at the ceiling would report a spend cap the operator
# never set as the reason a document stopped being improved.
SETTINGS_INTS = {"spec_max_rounds": (1, amp.SPEC_MAX_ROUNDS_LIMIT)}


def do_settings_set(body: dict) -> dict:
    cfg = amp.config()
    changed = {}
    # Neither flags nor choices: numbers, refused here if they are not ones or
    # not probabilities, rather than written and clamped silently at read time
    # where the operator would never learn a bar is not where they set it.
    for k in SETTINGS_BARS:
        if k not in body:
            continue
        try:
            v = float(body[k])
        except (TypeError, ValueError):
            return {"ok": False, "error": f"{k} must be a number between 0 and 1"}
        if not 0.0 <= v <= 1.0:
            return {"ok": False, "error": f"{k} must be between 0 and 1, not {v}"}
        cfg.setdefault("autonomy", {})[k] = round(v, 3)
        changed[k] = round(v, 3)
    for k, (lo, hi) in SETTINGS_INTS.items():
        if k not in body:
            continue
        try:
            n = int(body[k])
        except (TypeError, ValueError):
            return {"ok": False, "error": f"{k} must be a whole number between {lo} and {hi}"}
        if not lo <= n <= hi:
            return {"ok": False, "error": f"{k} must be between {lo} and {hi}, not {n}"}
        cfg[k] = n
        changed[k] = n
    for k in SETTINGS_FLAGS:
        if k in body:
            cfg[k] = bool(body[k])
            changed[k] = cfg[k]
    for k, allowed in SETTINGS_CHOICES.items():
        if k in body:
            v = body[k]
            if v not in allowed:
                return {"ok": False,
                        "error": f"{k} must be one of {', '.join(allowed)}"}
            cfg[k] = v
            changed[k] = v
    if not changed:
        return {"ok": False, "error": f"nothing to set - expected any of "
                                      f"{', '.join(SETTINGS_FLAGS + tuple(SETTINGS_CHOICES)
                                                   + SETTINGS_BARS + tuple(SETTINGS_INTS))}"}
    amp.save_json(amp.CONFIG_PATH, cfg)
    return {**do_settings(), "changed": changed}


# ---------------------------------------------------------------- database
#
# `save_json` mirrors itself as it writes, so these endpoints are about the
# things a write cannot do for itself: sweeping the files nothing calls
# save_json for, saying whether the two copies agree, and handing the operator
# a single file they can put somewhere else.


def do_db() -> dict:
    return store.status()


def do_db_set(body: dict) -> dict:
    changed = {}
    for k in ("mirror", "history_keep", "sweep_min"):
        if k in body:
            try:
                store.set_setting(k, body[k])
            except ValueError as e:
                return {"ok": False, "error": str(e)}
            changed[k] = body[k]
    if not changed:
        return {"ok": False, "error": "nothing to set - expected mirror, "
                                      "history_keep or sweep_min"}
    return {**store.status(), "changed": changed}


def do_db_backup() -> dict:
    return {**store.backup(), "status": store.status()}


def do_db_verify() -> dict:
    return store.verify()


def do_db_prune(body: dict) -> dict:
    r = store.prune(body.get("keep"))
    store.compact()
    return {**r, "status": store.status()}


def do_db_history(path: str) -> dict:
    if not path:
        return {"ok": False, "error": "which document?"}
    return {"ok": True, "path": path, "revisions": store.history(path)}


def _mirror_ticker():
    """Sweep the state directory into the mirror on a clock.

    Every `save_json` already mirrors itself, so this is for what does not go
    through it: packet zips, ruling and consult transcripts, published reports,
    goal files - and whatever a later part of the harness writes without ever
    being told this file exists. Catching those by sweeping rather than by
    hooking each writer is the difference between one thing to maintain and a
    growing list of places to remember.
    """
    while True:
        mins = int(store.settings().get("sweep_min") or 0)
        time.sleep(max(mins, 1) * 60.0)
        if int(store.settings().get("sweep_min") or 0) <= 0 or not store.enabled():
            continue
        try:
            store.backup()
            # Trimmed on the same clock, not only when somebody opens Settings.
            # The board alone is over a megabyte and is rewritten on every
            # worker heartbeat, so a mirror nobody trims is a disk filling up
            # quietly - which is its own way of losing data.
            store.prune()
        except Exception:
            traceback.print_exc()


def do_codex_auth_cancel() -> dict:
    global _CODEX_LOGIN
    with _CODEX_LOCK:
        if _CODEX_LOGIN:
            _CODEX_LOGIN.close()
            _CODEX_LOGIN = None
    return {"ok": True}


def do_task(lane: str, task_id: str) -> dict:
    for rec in amp.board().get("tasks", {}).get(lane, []):
        if rec.get("task_id") == task_id:
            return {"ok": True, "task": rec}
    return {"ok": False, "error": "no such task"}


def do_log(lane: str, task_id: str) -> dict:
    """The worker's own turn-by-turn transcript, not just its final answer."""
    task = do_task(lane, task_id)
    if not task["ok"]:
        return task
    rec = task["task"]
    if rec.get("backend") != "claude":
        return {"ok": False, "error": "only claude lanes keep a transcript"}
    # A resumed task appends to the session it resumed, so that is the log.
    session = rec.get("session_id") or rec.get("resume_of") or rec.get("task_id")
    events = amp.transcript(session)
    if not events:
        return {"ok": False, "error": f"no transcript on disk for session {session[:8]}"
                                      " (the worker may not have started)"}
    return {"ok": True, "session_id": session, "task": rec, "events": events}


# ---------------------------------------------------------------- orchestrator chat


def _first_line(text: str, limit: int = 240) -> str:
    """A prompt or a result, shortened to something a chat bubble can hold."""
    body = " ".join((text or "").split())
    return body[:limit] + "…" if len(body) > limit else body


def chat_payload(brief: bool = False) -> dict:
    """The orchestrator thread: operator notes merged with what the board did.

    Task activity is derived rather than stored, so the thread cannot disagree
    with the board it is summarising.

    `brief` adds the conversation with the briefer about that same thread, and
    starts a turn if one is owed - see `_brief_view`. It is off by default and
    the console only asks for it while somebody is actually looking at briefing
    mode, which is what keeps the noisy default free.
    """
    # An idea is a note, but a quieter one: it is an aside a worker made in
    # passing, and it should not read like something that happened.
    msgs = [{"kind": "idea" if n.get("kind") == "idea" else "note",
             "id": n["id"], "at": n["at"], "lane": n.get("lane"), "text": n["text"]}
            for n in amp.notes()]
    # The orchestrator's own conversation. Unlike the rest of the feed this is
    # stored, because it is the thing itself rather than a summary of it.
    for t in amp.orchestrator_turns():
        msgs.append({
            "kind": {"you": "you", "harness": "harness"}.get(t["role"], "amp"),
            "id": t["id"], "at": t["at"], "text": t.get("text") or "",
            "status": t.get("status"), "cost_usd": t.get("cost_usd"),
            "num_turns": t.get("num_turns"),
        })
    # A goal that stopped to ask something, as a question rather than as one
    # more line of activity. Derived, so it disappears from the thread the
    # moment it is answered and cannot outlive the thing it is about - the same
    # reason task activity is derived. It carries every question in full: the
    # note that used to stand in for this truncated three questions into 400
    # characters, which is enough to know a lane stopped and not enough to
    # answer it.
    for q in amp.blocked_questions():
        msgs.append({"kind": "question", "id": f"q:{q['goal_id']}", "at": q["at"],
                     "lane": q["lane"], "goal_id": q["goal_id"],
                     "questions": q["questions"], "triaged": bool(q["triaged_at"]),
                     "text": q["questions"][0]})
    for lane, recs in amp.board().get("tasks", {}).items():
        for rec in recs:
            tid, backend = rec.get("task_id"), rec.get("backend")
            if rec.get("dispatched_at"):
                msgs.append({
                    "kind": "dispatch", "id": f"d:{tid}", "at": rec["dispatched_at"],
                    "lane": lane, "task_id": tid, "backend": backend,
                    "model": rec.get("model"), "resumed": bool(rec.get("resume_of")),
                    "text": _first_line(rec.get("prompt", "")),
                })
            if rec.get("finished_at"):
                msgs.append({
                    "kind": "result", "id": f"r:{tid}", "at": rec["finished_at"],
                    "lane": lane, "task_id": tid, "backend": backend,
                    "status": rec.get("status"), "cost_usd": rec.get("cost_usd"),
                    "num_turns": rec.get("num_turns"),
                    "text": _first_line(rec.get("error") or rec.get("result") or ""),
                })
    # Escalations belong in the thread too - an architect ruling on a lane is
    # the most consequential thing that happens here, and it is the one thing
    # that can start without you.
    for summary in amp.consults():
        c = amp.load_consult(summary["id"])
        if not c:
            continue
        rounds = 0
        msgs.append({
            "kind": "escalation", "id": f"e:{c['id']}", "at": c["opened_at"],
            "lane": c.get("lane"), "consult_id": c["id"],
            "trigger": c.get("trigger"), "text": _first_line(c.get("question", "")),
        })
        for i, t in enumerate(c.get("turns", [])):
            if t["role"] != "gpt":
                continue
            rounds += 1
            msgs.append({
                "kind": "ruling", "id": f"g:{c['id']}:{i}", "at": t["at"],
                "lane": c.get("lane"), "consult_id": c["id"], "round": rounds,
                "needs": len(amp.parse_needs(t["text"])),
                "text": _first_line(t["text"]),
            })
    msgs.sort(key=lambda m: m.get("at") or "")
    out = {"ok": True, "messages": msgs}
    if brief:
        out["brief"] = _brief_view(msgs)
    return out


# ---------------------------------------------------------------- prior sessions


def do_history(limit: int, everywhere: bool) -> dict:
    """Every recent Claude Code chat about this workspace, and what looks wrong."""
    sessions = amp.prior_sessions(limit=limit, under=None if everywhere else amp.ROOT)
    return {"ok": True, "sessions": sessions,
            "troubled": sum(1 for s in sessions if s["symptoms"])}


def do_session_log(session_id: str) -> dict:
    events = amp.session_events(session_id)
    if not events:
        return {"ok": False, "error": f"no readable transcript for {session_id[:8]}"}
    return {"ok": True, "session_id": session_id, "events": events}


def do_history_note(body: dict) -> dict:
    """Put a prior chat into the orchestrator thread, so it is on the board."""
    sid = body.get("session_id") or ""
    path = amp.transcript_path(sid)
    if not path:
        return {"ok": False, "error": "no such session"}
    s = amp.scan_session(path)
    symptoms = amp.diagnose_session(s)
    amp.add_note(
        f"prior chat {sid[:8]} in {s['project']} ({s['last_at'][:16]}): "
        + (", ".join(symptoms) if symptoms else "nothing obviously wrong")
        + f"\n{s['first_prompt'][:200]}",
        lane=body.get("lane") or None,
    )
    return {"ok": True, "symptoms": symptoms}


def do_history_ask(body: dict) -> dict:
    """Hand a prior chat to the architect: what went wrong, and what to do now."""
    sid = body.get("session_id") or ""
    lane = body.get("lane") or ""
    cfg = amp.config()
    if lane not in cfg["lanes"]:
        return {"ok": False, "error": f"pick a lane to attach this to (got {lane!r})"}
    if not amp.architect_available():
        return {"ok": False, "error": amp.architect_off_reason()}
    brief = amp.session_brief(sid)
    if not brief:
        return {"ok": False, "error": "no such session"}
    try:
        c = amp.open_consult(
            lane,
            "This is a prior Claude Code session in this workspace that may have gone "
            "wrong. Diagnose it: say what it was trying to do, where it actually got "
            "to, what went wrong, and what the next build order should be.\n\n" + brief,
            model_key=body.get("model") or cfg.get("consult_model", amp.DEFAULT_CONSULT),
            trigger="history",
        )
    except SystemExit as e:
        return {"ok": False, "error": str(e) or "consult failed"}
    return {"ok": True, "consult": _consult_view(c)}


# ---------------------------------------------------------------- briefing
#
# Briefing mode is the same thread with somebody on the other end of it. The
# rules that person works to, why it is a view and never a source, and why it is
# allowed to stay quiet are all in `amp.py` under "briefing". This is the half
# that decides what counts as NEWS - which is the whole difficulty.
#
# It hangs off `chat_payload` rather than sitting on its own endpoint for one
# reason that matters: what it is told has to be the feed on screen. Two calls
# means two walks of the board, and a dispatch landing between them means being
# told about a thread that is not the one you are looking at.

# How far back the briefer reads. This thread is 1157 lines long on the board it
# was built against, and briefing all of it would be both expensive and wrong:
# "what has happened" means the recent past, and a summary that reaches back
# three days buries today under it. Every line is still in the feed, which is
# one click away.
BRIEF_WINDOW = 40


def _brief_line(m: dict) -> str:
    """One message as a line the briefer can read.

    Deliberately not JSON. This is prose for a model to summarise, and the
    words that carry the meaning - stopped, failed, escalated - should be in
    it rather than encoded in a `kind` field it has to interpret.
    """
    kind, lane = m.get("kind"), m.get("lane") or ""
    at = (m.get("at") or "")[:16].replace("T", " ")
    text = " ".join((m.get("text") or "").split())[:400]
    cost = f" ${m['cost_usd']:.2f}" if m.get("cost_usd") else ""
    if kind == "note":
        return f"{at} note: {text}"
    if kind == "idea":
        return f"{at} passing idea{f' on {lane}' if lane else ''}: {text}"
    if kind == "question":
        qs = " | ".join(m.get("questions") or [])[:600]
        return (f"{at} STOPPED: goal on {lane} is waiting on a decision and cannot "
                f"continue: {qs}")
    if kind == "you":
        return f"{at} operator said: {text}"
    if kind == "harness":
        return f"{at} the harness asked the orchestrator: {text}"
    if kind == "amp":
        if m.get("status") == "running":
            return f"{at} the orchestrator is mid-answer"
        return f"{at} orchestrator replied{cost}: {text}"
    if kind == "dispatch":
        return (f"{at} sent to a worker on {lane} ({m.get('model') or m.get('backend')})"
                f"{' as a reply' if m.get('resumed') else ''}: {text}")
    if kind == "result":
        turns = f", {m['num_turns']} turns" if m.get("num_turns") else ""
        return f"{at} worker on {lane} {m.get('status')}{cost}{turns}: {text}"
    if kind == "escalation":
        auto = " (started on its own)" if m.get("trigger") == "auto" else ""
        return f"{at} {lane} escalated to the architect{auto}: {text}"
    if kind == "ruling":
        needs = f", naming {m['needs']} piece(s) of work" if m.get("needs") else ""
        return f"{at} architect ruled on {lane} (round {m.get('round')}{needs}): {text}"
    return f"{at} {kind} {lane}: {text}"


# How long the briefer waits between messages it sends off its own bat. A person
# watching your board does not text you once per dispatch; they let a few things
# pile up and then tell you where it got to. Nothing to do with cost - it is
# about being someone worth reading. A reply to something you SAID is never
# delayed by this.
BRIEF_MIN_GAP_S = 45


def _line_hash(line: str) -> str:
    return hashlib.sha256(line.encode()).hexdigest()[:12]


def _brief_new(msgs: list[dict], seen: set) -> tuple[str, list[str]]:
    """The lines the briefer has not been shown yet, and their hashes.

    Hashing the rendered LINE, rather than tracking a position in the feed, is
    what makes "new" mean the right thing here. Two reasons, and the second is
    the one that bites:

    A message can CHANGE. A dispatch that was running when the briefer last
    looked renders differently once it finishes, carrying the result and the
    cost - the news is in the change, not in a new entry, and any watermark
    that counts messages or compares timestamps sails straight past it.

    A message can also NOT change while everything around it does. Re-reading
    forty lines because one arrived is what made the old brief repeat itself,
    and repetition is the single thing that stops this reading like a person.

    `_brief_line` is deliberately stable for work in flight - it says "mid-answer"
    and not how long it has been - so a running task does not re-hash on every
    poll and start a conversation with itself.
    """
    fresh = [(l, h) for l, h in
             ((l, _line_hash(l)) for l in
              (_brief_line(m) for m in msgs[-BRIEF_WINDOW:]))
             if h not in seen]
    return "\n".join(l for l, _ in fresh), [h for _, h in fresh]


def _run_brief_bg(new_lines: str, sig: str, seen: list, asked: str, covers: int):
    try:
        amp.brief_turn(new_lines, sig, seen, asked=asked, covers=covers)
    except Exception:
        traceback.print_exc()


def _brief_start(msgs: list[dict], asked: str = "") -> bool:
    """Start a turn if there is one owed. Returns whether one was started.

    Shared by the poll and by the operator saying something, because the two
    differ in exactly two ways and it is clearer to state them than to keep two
    copies: being asked overrides the quiet gap, and being asked means a turn
    happens even when the board has done nothing at all.
    """
    rec = amp.brief()
    new_lines, hashes = _brief_new(msgs, set(rec.get("seen") or []))
    # A question that was asked while a turn already held the claim was dropped
    # on the floor: the POST could not start anything, and nothing retried. The
    # poll picks that debt back up here, so being answered does not depend on
    # having typed at a quiet moment.
    asked = asked or amp.brief_unanswered()
    if not asked:
        if not new_lines:
            return False
        last = rec.get("last_run_at")
        if last:
            try:
                gap = (datetime.now(timezone.utc)
                       - datetime.fromisoformat(last)).total_seconds()
            except ValueError:
                gap = BRIEF_MIN_GAP_S + 1
            if gap < BRIEF_MIN_GAP_S:
                return False
    # The signature covers what was asked as well as what is new, so two
    # different questions about an unchanged board are two turns, not one
    # refused as a duplicate.
    sig = hashlib.sha256((asked + "\n" + new_lines).encode()).hexdigest()
    if not amp.claim_brief(sig):
        return False
    threading.Thread(target=_run_brief_bg,
                     args=(new_lines, sig, hashes, asked, len(hashes)),
                     daemon=True).start()
    return True


def _brief_view(msgs: list[dict]) -> dict:
    """The conversation so far, and whether the other side is mid-sentence.

    No `current` flag any more, and its absence is the point. A brief that was
    rewritten in place had to say whether it was still true; turns are dated
    and stay said, so they carry that on their face the way any chat does.
    """
    _brief_start(msgs)
    rec = amp.brief()
    return {"turns": rec.get("turns") or [],
            "working": rec.get("status") == "running",
            "error": rec.get("error") if rec.get("status") == "failed" else None}


def do_brief_send(body: dict) -> dict:
    """The operator saying something to the briefer.

    A separate route from `/api/chat` because it goes somewhere else entirely:
    nothing here reaches the board, no lane hears it, and no work starts. The
    briefer reads and talks, and that is the whole of what this can do.
    """
    text = (body.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "nothing to say"}
    amp.brief_say(text)
    msgs = chat_payload()["messages"]
    return {"ok": True, "working": _brief_start(msgs, asked=text)}


def _run_orchestrator_bg(text: str):
    try:
        amp.orchestrator_ask(text, base_url=BASE_URL)
    except Exception:
        traceback.print_exc()


def do_chat_send(body: dict) -> dict:
    """Send a line.

    With a lane named it goes straight to a worker in that lane, unchanged -
    the escape hatch for when you already know exactly who should do it.
    Without one it goes to the orchestrator, which can answer, look things up,
    run git here, and dispatch to lanes itself.
    """
    text = (body.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "nothing to send"}
    lane = (body.get("lane") or "").strip()
    if lane == "note":
        amp.add_note(text)
        return {"ok": True, "dispatched": False}
    if lane:
        out = do_dispatch({**body, "lane": lane, "prompt": text})
        if not out.get("ok"):
            return out
        return {"ok": True, "dispatched": True, "lane": lane, "task_id": out.get("task_id")}
    if not amp.claude_available():
        return {"ok": False, "error": "claude CLI not found"}
    if amp.orch_busy():
        return {"ok": False, "error": "the orchestrator is still working on the last one"}
    threading.Thread(target=_run_orchestrator_bg, args=(text,), daemon=True).start()
    return {"ok": True, "dispatched": False, "orchestrating": True}


# ---------------------------------------------------------------- http


class Handler(BaseHTTPRequestHandler):
    server_version = "ampcode/0.1"

    def log_message(self, fmt, *args):  # quieter console
        pass

    def _send(self, code: int, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, (bytes, bytearray)):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True

    def _static(self, rel: str):
        # Confined to this directory, by PATH and not by prefix. `startswith`
        # on the string was true of any sibling whose name merely begins with
        # this one - `/api/../code-backup/x` resolves outside the tree and
        # passes a prefix test, which is the whole of the check it was.
        target = (HERE / rel).resolve()
        if not target.is_relative_to(HERE) or not target.is_file():
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        body = target.read_bytes()
        if target.name == "index.html":
            # The token reaches the page here and only here, so that no route
            # hands it out and it is never in a file on disk that a build step
            # could commit. The page has already passed the Host check to get
            # this far, which is the check that decides whether it should have
            # it at all.
            body = body.replace(b"__AMP_TOKEN__", _TOKEN.encode())
        self._send(200, body, ctype)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except json.JSONDecodeError:
            return {}

    def _refuse(self, path: str) -> tuple[int, str] | None:
        """Why this request is not allowed in, or None. See the note by HOST.

        Returns rather than sends, so that one function decides and the two
        verbs cannot drift apart - the same reason `merge_blockers` is one
        function with three callers. A gate that each handler re-implements is
        a gate a handler forgets.
        """
        # 1. Host. Before anything else and for every path including `/`,
        #    because under rebinding the page is same-origin and everything
        #    below this line is something the attacker also has.
        host = (self.headers.get("Host") or "").strip().lower()
        if _ALLOWED_HOSTS and host not in _ALLOWED_HOSTS:
            return (403, f"this console answers to {sorted(_ALLOWED_HOSTS)[0]}, "
                         f"not {host or '(no Host header)'}")

        # Static assets are the delivery path for the token, so they stop here.
        # They are files on disk next to this one; there is nothing behind them
        # to drive, and a page that has got past the Host check to read app.css
        # has read a file it could have read from the repository anyway.
        if not path.startswith("/api/"):
            return None

        # 2. Fetch metadata, with Origin as the fallback. Neither is settable
        #    by page JavaScript, which is the whole point; an absent pair is a
        #    non-browser client and is left to the token below.
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site and site not in _FETCH_SITE_OK:
            return (403, f"cross-site request refused (Sec-Fetch-Site: {site})")
        origin = (self.headers.get("Origin") or "").strip().lower()
        if origin and urlparse(origin).netloc not in _ALLOWED_HOSTS:
            return (403, f"cross-origin request refused (Origin: {origin})")

        # 3. The token.
        if _TOKEN and not secrets.compare_digest(
                (self.headers.get(amp.TOKEN_HEADER) or "").strip(), _TOKEN):
            return (401, f"no {amp.TOKEN_HEADER} - the console mints one into "
                         f"{amp.console_token_path()} and the page is served it on load")
        return None

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            stop = self._refuse(u.path)
            if stop:
                return self._send(stop[0], {"error": stop[1]})
            if u.path in ("/", "/index.html"):
                return self._static("index.html")
            if u.path == "/api/state":
                return self._send(200, state_payload())
            if u.path == "/api/rulings":
                return self._send(200, {"rulings": rulings_payload()})
            if u.path == "/api/ruling":
                name = (q.get("name") or [""])[0]
                p = amp.RULING_DIR / Path(name).name
                if not p.is_file():
                    return self._send(404, {"error": "no such ruling"})
                return self._send(200, {"ok": True, "text": p.read_text()})
            if u.path == "/api/credits":
                return self._send(200, do_credits())
            if u.path == "/api/task":
                return self._send(
                    200, do_task((q.get("lane") or [""])[0], (q.get("task_id") or [""])[0])
                )
            if u.path == "/api/log":
                return self._send(
                    200, do_log((q.get("lane") or [""])[0], (q.get("task_id") or [""])[0])
                )
            if u.path == "/api/chat":
                return self._send(200, chat_payload(
                    brief=(q.get("brief") or [""])[0] == "1"))
            if u.path == "/api/history":
                return self._send(200, do_history(
                    int((q.get("limit") or ["40"])[0]),
                    (q.get("all") or [""])[0] == "1"))
            if u.path == "/api/session":
                return self._send(200, do_session_log((q.get("id") or [""])[0]))
            if u.path == "/api/consults":
                return self._send(200, do_consults((q.get("lane") or [None])[0],
                                                   (q.get("id") or [None])[0]))
            if u.path == "/api/goal":
                return self._send(200, do_goal({"goal_id": (q.get("id") or [""])[0]}))
            if u.path == "/api/flow":
                return self._send(200, do_flow((q.get("lane") or [None])[0]))
            if u.path == "/api/findings":
                return self._send(200, do_findings(
                    (q.get("lane") or [None])[0],
                    (q.get("unread") or [""])[0] == "1"))
            if u.path == "/api/ideas":
                return self._send(200, do_ideas((q.get("lane") or [None])[0]))
            if u.path == "/api/obligations":
                return self._send(200, do_obligations())
            if u.path == "/api/direction":
                return self._send(200, do_direction((q.get("lane") or [None])[0]))
            if u.path == "/api/spec":
                return self._send(200, do_spec((q.get("lane") or [None])[0]))
            if u.path == "/api/spec/run":
                return self._send(200, do_spec_run((q.get("id") or [None])[0]))
            if u.path == "/api/spec/candidates":
                return self._send(200, do_spec_candidates((q.get("lane") or [None])[0]))
            if u.path == "/api/lanes":
                return self._send(200, do_lanes())
            if u.path == "/api/lane/directions":
                return self._send(200, do_lane_directions())
            if u.path == "/api/blueprint":
                return self._send(200, do_blueprint_map())
            if u.path == "/api/blueprint/triggers":
                return self._send(200, do_blueprint_triggers())
            if u.path == "/api/blueprint/actions":
                return self._send(200, do_blueprint_actions())
            if u.path == "/api/blueprint/flow":
                return self._send(200, do_blueprint_flow(
                    (q.get("lens") or ["agents"])[0], (q.get("action") or [""])[0],
                    (q.get("lane") or [""])[0], (q.get("level") or [""])[0]))
            if u.path == "/api/blueprint/ports":
                return self._send(200, do_blueprint_ports())
            if u.path == "/api/blueprint/context":
                # Builds the real prompt for real. No model is called, nothing
                # is dispatched, and nothing on disk is written - it is the same
                # code path the action takes, stopped one step before sending.
                return self._send(200, do_blueprint_context(
                    (q.get("action") or [""])[0], (q.get("lane") or [""])[0]))
            if u.path == "/api/doctrine":
                return self._send(200, do_doctrine())
            # No `lane` is the whole workspace, which is what this pane has
            # always shown. The narrowing is a request the console makes, not a
            # default the server imposes on every client.
            if u.path == "/api/deploy":
                return self._send(200, do_deploy((q.get("lane") or [""])[0]))
            if u.path == "/api/deploy/pages":
                return self._send(200, do_deploy_pages((q.get("lane") or [""])[0]))
            if u.path == "/api/prs":
                return self._send(200, do_prs((q.get("lane") or [""])[0]))
            if u.path == "/api/workspaces":
                return self._send(200, do_workspaces())
            if u.path == "/api/supervisor":
                return self._send(200, do_supervisor())
            if u.path == "/api/reports":
                return self._send(200, do_reports())
            if u.path.startswith("/reports/"):
                # Served from the workspace state dir, not from HERE, so it is
                # its own route rather than a hole in `_static`. Only the file
                # name is taken from the request - a path with a slash or a
                # `..` in it resolves to a name that is not there.
                p = amp.REPORT_DIR / Path(u.path[len("/reports/"):]).name
                if not p.is_file():
                    return self._send(404, {"error": "no such report"})
                return self._send(200, p.read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/db":
                return self._send(200, do_db())
            if u.path == "/api/db/history":
                return self._send(200, do_db_history((q.get("path") or [""])[0]))
            if u.path == "/api/db/changes":
                # The feed a cloud sync will read. Exposed now because a sync
                # that has to be designed against a database with no change
                # log is a rewrite, not a feature.
                return self._send(200, store.changes(
                    int((q.get("since") or ["0"])[0]),
                    min(int((q.get("limit") or ["500"])[0]), 5000)))
            if u.path == "/api/db/doc":
                # A held document, current or at a revision. A read: this never
                # writes over what is on disk. Recovering a file is a copy the
                # operator makes deliberately, not a button that overwrites
                # live state under a running harness.
                seq = (q.get("seq") or [""])[0]
                b = store.body((q.get("path") or [""])[0], int(seq) if seq else None)
                if b is None:
                    return self._send(404, {"error": "nothing held at that path"})
                return self._send(200, b, "text/plain; charset=utf-8")
            if u.path == "/api/db/export":
                # Snapshotted through SQLite's own backup API rather than
                # copied: a WAL database copied byte-for-byte while in use can
                # be missing committed pages, which is the one thing a backup
                # may not be.
                tmp = Path(tempfile.gettempdir()) / f"amp-export-{int(time.time())}.db"
                try:
                    store.snapshot(tmp)
                    data = tmp.read_bytes()
                finally:
                    tmp.unlink(missing_ok=True)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Disposition",
                                 'attachment; filename="amp-backup.db"')
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                return self.wfile.write(data)
            if u.path == "/api/preview":
                return self._send(200, do_preview((q.get("lane") or [""])[0],
                                                  (q.get("source") or [""])[0],
                                                  (q.get("dir") or [""])[0]))
            if u.path == "/api/preview/stamp":
                return self._send(200, do_preview_stamp((q.get("lane") or [""])[0]))
            if u.path == "/api/diff":
                lane = (q.get("lane") or [""])[0]
                attempt = q.get("attempt")
                return self._send(
                    200, do_diff(lane, int(attempt[0]) if attempt else None, (q.get("task_id") or [None])[0])
                )
            if u.path.startswith("/api/"):
                return self._send(404, {"error": "unknown endpoint"})
            return self._static(u.path.lstrip("/"))
        except Exception:
            traceback.print_exc()
            return self._send(500, {"error": traceback.format_exc(limit=3)})

    def do_POST(self):
        u = urlparse(self.path)
        try:
            stop = self._refuse(u.path)
            if stop:
                # Read the body first even when refusing. The client has
                # already sent it, and leaving it in the socket makes the next
                # request on a kept-alive connection start mid-JSON, which
                # surfaces as an unrelated parse error somewhere else.
                self._body()
                return self._send(stop[0], {"error": stop[1]})
            body = self._body()
            if u.path == "/api/poll":
                return self._send(200, do_poll(body.get("lane")))
            if u.path == "/api/dispatch":
                return self._send(200, do_dispatch(body))
            if u.path == "/api/chat":
                return self._send(200, do_chat_send(body))
            # Talking to the briefer, which is not talking to the board. Its own
            # route so that nothing about dispatching can arrive here by accident.
            if u.path == "/api/brief":
                return self._send(200, do_brief_send(body))
            if u.path == "/api/ask":
                return self._send(200, do_ask(body))
            if u.path == "/api/blueprint/stack":
                return self._send(200, do_blueprint_stack(body))
            if u.path == "/api/blueprint/place":
                return self._send(200, do_blueprint_place(body))
            if u.path == "/api/blueprint/trigger":
                return self._send(200, do_blueprint_trigger(body))
            if u.path == "/api/goal/open":
                return self._send(200, do_open_goal(body))
            if u.path == "/api/goal/answer":
                return self._send(200, do_answer_goal(body))
            if u.path == "/api/goal/triage":
                return self._send(200, do_triage_goal(body))
            if u.path == "/api/goal/push":
                return self._send(200, do_push_goal(body))
            if u.path == "/api/goal/close":
                return self._send(200, do_close_goal(body))
            if u.path == "/api/goal/reopen":
                return self._send(200, do_goal_reopen(body))
            # Two routes, not one with a flag. The dry run is something you can
            # call freely and often; the other one leaves the machine.
            if u.path == "/api/goal/publish/report":
                return self._send(200, do_publish_report(body))
            if u.path == "/api/goal/publish":
                return self._send(200, do_publish_goal(body))
            # Reviewing is a read that costs a model call; adopting is the only
            # one of the three that starts work, and it is a separate word.
            if u.path == "/api/direction/review":
                return self._send(200, do_direction_review(body))
            if u.path == "/api/direction/explore":
                return self._send(200, do_direction_explore(body))
            if u.path == "/api/direction/case":
                return self._send(200, do_direction_case(body))
            if u.path == "/api/spec/start":
                return self._send(200, do_spec_start(body))
            if u.path == "/api/spec/close":
                return self._send(200, do_spec_close(body))
            if u.path == "/api/spec/rate":
                return self._send(200, do_spec_rate(body))
            if u.path == "/api/spec/campaign":
                return self._send(200, do_spec_campaign(body))
            if u.path == "/api/spec/campaign/stop":
                return self._send(200, do_spec_campaign_stop(body))
            if u.path == "/api/spec/draft":
                return self._send(200, do_spec_draft(body))
            if u.path == "/api/lane/refresh":
                return self._send(200, do_lane_refresh(body))
            if u.path == "/api/report":
                return self._send(200, do_report(body))
            if u.path == "/api/report/solve":
                return self._send(200, do_solve_report(body))
            if u.path == "/api/direction/proposal":
                return self._send(200, do_direction_proposal(body))
            if u.path == "/api/direction/auto":
                return self._send(200, do_direction_auto(body))
            if u.path == "/api/spec/auto":
                return self._send(200, do_auto_spec(body))
            if u.path == "/api/spec/explore":
                return self._send(200, do_spec_explore(body))
            if u.path == "/api/findings/ack":
                return self._send(200, do_ack_findings(body))
            if u.path == "/api/findings/settle":
                return self._send(200, do_settle_findings(body))
            if u.path == "/api/ideas/close":
                return self._send(200, do_close_ideas(body))
            if u.path == "/api/doctrine/ratify":
                return self._send(200, do_ratify_doctrine())
            # The only writer of DOCTRINE.md. It sits next to ratify on purpose:
            # a save changes the digest, which is what puts the change on the
            # board unratified, which is the only reason writing it here is safe.
            if u.path == "/api/doctrine/save":
                return self._send(200, do_set_doctrine(body))
            if u.path == "/api/doctrine/draft":
                return self._send(200, do_draft_doctrine())
            if u.path == "/api/doctrine/review":
                return self._send(200, do_review_doctrine())
            # `use` moves the ground under everything else in this process and
            # refuses while anything is running; the rest are bookkeeping.
            if u.path == "/api/workspace/use":
                return self._send(200, do_workspace_use(body))
            if u.path == "/api/workspace/add":
                return self._send(200, do_workspace_add(body))
            if u.path == "/api/workspace/rename":
                return self._send(200, do_workspace_rename(body))
            if u.path == "/api/workspace/remove":
                return self._send(200, do_workspace_remove(body))
            if u.path == "/api/lane/move":
                return self._send(200, do_lane_move(body))
            if u.path == "/api/mission":
                return self._send(200, do_mission(body))
            if u.path == "/api/supervise":
                return self._send(200, do_supervise())
            if u.path == "/api/obligation/add":
                return self._send(200, do_add_obligation(body))
            if u.path == "/api/obligation/check":
                return self._send(200, do_check_obligation(body))
            if u.path == "/api/obligation/set":
                return self._send(200, do_set_obligation(body))
            if u.path == "/api/obligation/remove":
                return self._send(200, do_remove_obligation(body))
            if u.path == "/api/consult/relay":
                return self._send(200, do_relay(body))
            if u.path == "/api/consult/continue":
                return self._send(200, do_continue_consult(body))
            if u.path == "/api/consult/close":
                return self._send(200, do_close_consult(body))
            if u.path == "/api/cancel":
                return self._send(200, do_cancel(body))
            if u.path == "/api/restart":
                return self._send(200, do_restart(body))
            if u.path == "/api/history/note":
                return self._send(200, do_history_note(body))
            if u.path == "/api/history/ask":
                return self._send(200, do_history_ask(body))
            if u.path == "/api/preview/start":
                return self._send(200, do_preview_start(body))
            if u.path == "/api/preview/stop":
                return self._send(200, do_preview_stop(body))
            if u.path == "/api/apply":
                return self._send(200, do_apply(body))
            if u.path == "/api/lane/add":
                return self._send(200, do_lane_add(body))
            if u.path == "/api/lane/env":
                return self._send(200, do_bind_env(body))
            if u.path == "/api/lane/backend":
                return self._send(200, do_set_backend(body))
            if u.path == "/api/lane/mode":
                return self._send(200, do_set_lane_mode(body))
            if u.path == "/api/lane/direction":
                return self._send(200, do_set_lane_direction(body))
            if u.path == "/api/lane/direction/draft":
                return self._send(200, do_draft_lane_direction(body))
            if u.path == "/api/lane/stage":
                return self._send(200, do_set_lane_stage(body))
            if u.path == "/api/deploy/login":
                return self._send(200, do_provider_login(body))
            if u.path == "/api/deploy/poll":
                return self._send(200, do_provider_poll())
            if u.path == "/api/deploy/preflight":
                return self._send(200, do_deploy_preflight(body))
            if u.path == "/api/deploy/run":
                return self._send(200, do_deploy_run(body))
            if u.path == "/api/deploy/status":
                return self._send(200, do_deploy_status(body))
            if u.path == "/api/deploy/cancel":
                return self._send(200, do_deploy_cancel(body))
            if u.path == "/api/deploy/history":
                return self._send(200, do_deploy_history(body))
            if u.path == "/api/pr/report":
                return self._send(200, do_pr_report(body))
            if u.path == "/api/pr/write-checks":
                return self._send(200, do_pr_write_checks(body))
            if u.path == "/api/pr/waive":
                return self._send(200, do_pr_waive(body))
            # `confirm` gates the merge the way it gates a publish - without it
            # this is a preflight that changes nothing on GitHub.
            if u.path == "/api/pr/merge":
                return self._send(200, do_pr_merge(body))
            if u.path == "/api/pr/resolve":
                return self._send(200, do_pr_resolve(body))
            if u.path == "/api/pr/auto":
                return self._send(200, do_pr_auto(body))
            if u.path == "/api/pr/auto/step":
                return self._send(200, do_pr_auto_step(body))
            if u.path == "/api/auth/start":
                return self._send(200, do_auth_start())
            if u.path == "/api/auth/code":
                return self._send(200, do_auth_code(body))
            if u.path == "/api/codex-auth/start":
                return self._send(200, do_codex_auth_start())
            if u.path == "/api/codex-auth/poll":
                return self._send(200, do_codex_auth_poll())
            if u.path == "/api/codex-auth/cancel":
                return self._send(200, do_codex_auth_cancel())
            if u.path == "/api/settings":
                return self._send(200, do_settings())
            if u.path == "/api/settings/set":
                return self._send(200, do_settings_set(body))
            if u.path == "/api/role/set":
                return self._send(200, do_role_set(body))
            if u.path == "/api/db/set":
                return self._send(200, do_db_set(body))
            if u.path == "/api/db/backup":
                return self._send(200, do_db_backup())
            if u.path == "/api/db/verify":
                return self._send(200, do_db_verify())
            if u.path == "/api/db/prune":
                return self._send(200, do_db_prune(body))
            return self._send(404, {"error": "unknown endpoint"})
        except Exception:
            traceback.print_exc()
            return self._send(500, {"error": traceback.format_exc(limit=3)})


def free_port(start: int) -> int:
    for p in range(start, start + 40):
        with socket.socket() as s:
            if s.connect_ex((HOST, p)) != 0:
                return p
    return start


# One console per STATE DIRECTORY, which is the thing actually being shared -
# not per machine and not per port. Two consoles pointed at different AMP_HOMEs
# are two separate harnesses and both may run; that pair is a normal setup
# here, one of them serving a preview. The `.lock` suffix is what keeps this
# out of the mirror (`store.SKIP_SUFFIX`), so renaming it needs a look there.
CONSOLE_LOCK = ".console.lock"


def _lock_holder(path: Path) -> dict | None:
    """Who holds this lock, or None if nobody does any more.

    A lock file outlives `kill -9`, so its existence proves nothing on its own,
    and treating it as proof would mean recovering from a crash by deleting a
    dotfile nobody documented. What is checked is the PROCESS.
    """
    try:
        rec = json.loads(path.read_text())
        pid = int(rec.get("pid") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        # An unreadable lock names nobody. It cannot be honoured, because
        # there is no port to send the operator to and no pid to check.
        return None
    if pid <= 0 or pid == os.getpid():
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    # Only ever used to declare a lock STALE, never to declare one live. A pid
    # recycled into an unrelated program is not a console, and refusing to
    # start for it would be a refusal the operator cannot act on. Where this
    # cannot be read the pid check above stands on its own.
    try:
        cmd = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return rec
    return rec if b"server.py" in cmd else None


def take_console_lock(port: int, host: str) -> Path | None:
    """Refuse to be the second console on one state directory.

    `main` below assumes it is the only console: it marks every `running`
    orchestrator turn failed, and `adopt_orphans` reconciles the SHARED board
    against IN-PROCESS dicts that a second console starts empty. So a second
    console does not merely race the first - it writes "the worker did not
    survive" about a live worker another console is watching, and adopts that
    worker's process group as its own. `free_port` is what made this silent:
    it succeeds on the next port instead of failing on this one, so the way
    you find out is by reading two boards that disagree.
    """
    path = amp.STATE_ROOT / CONSOLE_LOCK
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({"pid": os.getpid(), "port": port, "host": host,
                       "at": amp.now()}, indent=2, sort_keys=True) + "\n"
    # Bounded, because the retry is for a lock found stale and cleared. An
    # unbounded loop here would spin against a console starting at the same
    # moment, which is the one case the exclusive create already settles.
    for _ in range(3):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            held = _lock_holder(path)
            if held:
                raise SystemExit(
                    f"amp: a console is already running on this state directory\n"
                    f"      {amp.STATE_ROOT}\n"
                    f"      pid {held.get('pid')}, started {held.get('at')}, at "
                    f"http://{held.get('host') or HOST}:{held.get('port')}\n"
                    f"      Two consoles here do not just race: the second one marks the "
                    f"first's\n"
                    f"      live orchestrator turn failed and adopts its running workers. "
                    f"Use the\n"
                    f"      one above, or stop it first.")
            print(f"  cleared a stale console lock ({path.name})")
            try:
                path.unlink()
            except OSError:
                pass
            continue
        with os.fdopen(fd, "w") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        amp._fsync_dir(path.parent)
        return path
    raise SystemExit(f"amp: could not take the console lock at {path}")


def release_console_lock(path: Path | None) -> None:
    """Give the lock back, and only if it is still ours.

    A lock this process took over as stale can since have been taken by a
    third console - deleting that one on the way out would hand the directory
    to whoever starts next, which is the race this whole thing closes.
    """
    if path is None:
        return
    try:
        if int(json.loads(path.read_text()).get("pid") or 0) == os.getpid():
            path.unlink()
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="amp browser console")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--host", default=HOST)
    ap.add_argument("--open", action="store_true", help="open a browser")
    a = ap.parse_args(argv)

    global BASE_URL
    port = free_port(a.port)
    # Before the socket and before the sweep below, because the point is not to
    # start at all. Taken after `free_port` only so the refusal can say which
    # port THIS console would have used.
    lock = take_console_lock(port, a.host)
    # Before the socket, so there is no window in which a request could be
    # served without a token to check it against. The port has to be settled
    # first because `Host` carries it.
    set_allowed_hosts(a.host, port)
    mint_console_token()
    srv = ThreadingHTTPServer((a.host, port), Handler)
    url = f"http://{a.host}:{port}"
    BASE_URL = url
    # A turn marked `running` in the file names a process that died with the
    # last server. Left alone it wedges the dock, since a busy orchestrator
    # refuses the next line.
    for t in amp.orchestrator_turns():
        if t.get("status") == "running":
            amp.orch_update(t["id"], {"status": "failed", "finished_at": amp.now(),
                                      "text": "interrupted - the console restarted"})
    # Lane workers outlive the console rather than dying with it, so the same
    # sweep has to reach them - but by picking them back up, not writing them
    # off. One of these is a live Opus session with a budget already spent.
    for a_ in amp.adopt_orphans():
        print(f"  {'picked up' if a_['adopted'] else 'settled orphaned'} "
              f"{a_['lane']} {a_['task_id'][:8]}"
              + (f" (pid {a_['pid']})" if a_.get("pid") else ""))
        if a_["adopted"]:
            # Synchronously, before anything can dispatch: the lane an adopted
            # worker occupies must look occupied.
            lk = lane_lock(a_["lane"])
            lk.acquire()
            threading.Thread(target=_hold_lane_for_adopted,
                             args=(a_["lane"], a_["task_id"], lk), daemon=True).start()
    # Order matters: the queue is drained only after adoption has run, so the
    # cap counts the workers that were picked up and their lanes read as busy.
    amp.ON_SETTLE = _drain_queue
    # A worker this process did not start still has an architect waiting on it.
    amp.ON_WORKER_DONE = _settle
    # A ruling that asks for evidence sends a worker for it, rather than stopping.
    amp.ON_CONSULT_NEEDS = _gather_for_consult
    # A goal sends its own next task out.
    amp.ON_GOAL_DISPATCH = _dispatch_for_goal
    # A spec run sends its own next writer out.
    amp.ON_SPEC_DISPATCH = _dispatch_for_spec
    # The unattended spec loop asks for a lane's first document. `queue=False`
    # so a busy lane is refused rather than backed up: the loop comes round
    # again in a minute and will ask again if the lane still has no spec, and a
    # queue would collect one draft request per tick behind the same worker.
    amp.ON_DRAFT_DISPATCH = lambda body: do_dispatch(body, queue=False)
    # The hooks above are deliberately set after adoption, so nothing dispatches
    # into a lane whose lock has not been re-taken yet. That leaves a window: a
    # worker that finished during it reported to nobody. This closes it.
    for cid in _recover_stuck_consults():
        print(f"  relayed a finished worker into consult {cid}")
    for gid in _recover_stuck_goals():
        print(f"  picked goal {gid} back up")
    for sid in _recover_stuck_specruns():
        print(f"  picked spec run {sid} back up")
    n_q = _load_queue()
    if n_q:
        print(f"  {n_q} queued task(s) restored")
    _drain_queue()
    # Standing obligations are checked on a clock, not on a page view: one that
    # is only evaluated while someone is watching is not standing.
    n_ob = len(amp.obligations())
    if n_ob:
        print(f"  {n_ob} standing obligation(s) on a {_OBLIGATION_TICK / 60:.0f}m clock")
    threading.Thread(target=_obligation_ticker, daemon=True).start()
    print(f"  goals kept moving on a {_PIPELINE_TICK:.0f}s heartbeat")
    threading.Thread(target=_pipeline_ticker, daemon=True).start()
    # One sweep at startup, before anything can be dispatched: whatever the
    # last run left on disk is mirrored before this one starts changing it.
    if store.enabled():
        r = store.backup()
        s = store.status()
        if not s.get("ok"):
            # The JSON files are the authoritative ones, so a mirror that
            # cannot be opened is a thing to say out loud and carry on past -
            # not a reason to refuse to start the console. The sweep is left
            # off because it would fail every ten minutes and the only fix is
            # off this machine's hot path anyway.
            print(f"  ! the database is not usable, and is not being written: "
                  f"{s.get('error') or 'cannot read it'}")
        else:
            print(f"  database {s['path']} - {s['docs']} documents, "
                  f"{s['revisions']} revisions ({r['written']} mirrored just now)")
            threading.Thread(target=_mirror_ticker, daemon=True).start()
    else:
        print("  ! the SQLite mirror is off - only the JSON files are being kept")
    print(f"amp console -> {url}")
    if not amp.claude_available():
        print("  ! claude CLI not found - claude lanes cannot dispatch")
    if not amp.find_openrouter_key():
        print("  ! no OpenRouter key - escalation disabled")
    if amp.claude_auth_problem():
        print("  ! Claude not connected - open the console and click Connect Claude")
    if a.open:
        amp.open_url(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        # A preview's dev server is our child and holds a lane's worktree open.
        # Left behind, it survives every restart of this file and accumulates.
        preview.stop_all()
        # The token names a console that is gone. Leaving it would hand the
        # next reader a credential for nothing, and the next console mints its
        # own anyway. Best effort - an orphaned token authenticates against no
        # listener.
        try:
            amp.console_token_path().unlink()
        except OSError:
            pass
        # Last, so a crash in the line above still hands the directory back.
        # A lock left behind is recoverable - the next console reads the pid,
        # finds nothing, and says it cleared a stale one.
        release_console_lock(lock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
