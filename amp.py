#!/usr/bin/env python3
"""amp - lead-manager harness for the [&] stack.

One chat window (Claude) dispatches coding work to workers across many repos,
tracks them on a board, and escalates to GPT-5.6 when a lane stalls.

Layers:
  lanes    a named (repo, path, backend) triple - one per sub-repo
  workers  `claude -p` runs in an isolated worktree (default), or
           `codex cloud exec` tasks billed to the ChatGPT subscription
  consult  GPT-5.6 via OpenRouter, reached with a packet built from a lane

Stdlib only. No secrets are stored in tracked files.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import html
import io
import json
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import store

HERE = Path(__file__).resolve().parent                   # the program (this repo)
ROOT = Path(os.environ.get("AMP_ROOT") or HERE.parent)   # workspace holding the lanes

# State lives OUTSIDE this repo so the program stays shareable and no key,
# board, packet, or ruling is ever committed. Override with AMP_HOME.
STATE_ROOT = Path(os.environ.get("AMP_HOME") or (ROOT / ".amp"))

# Two things live at the root and are deliberately NOT per-workspace:
#
#   the secrets, because an OpenRouter key and a Claude worker token belong to
#   the machine and the person, not to whichever set of lanes is in front of
#   you. Copying them per workspace would mean signing in again per workspace
#   and having several copies of one credential to leak.
#
#   the workspace registry itself, for the obvious reason.
SECRETS_PATH = STATE_ROOT / ".secrets.json"
WORKSPACES_PATH = STATE_ROOT / ".workspaces.json"

# Everything else is workspace state, and this table is the only place that
# says so. `_bind_state` builds every one of these names from a base directory,
# so adding a file to the harness means adding a line here rather than
# remembering to teach the workspace switch about it later. The names are
# module globals because that is what four thousand lines of this file already
# read; what changed is that they are now bound rather than computed once.
_STATE_LAYOUT = (
    ("CONFIG_PATH", "config.json"),
    ("BOARD_PATH", ".board.json"),
    ("CHAT_PATH", ".chat.json"),
    ("ORCH_PATH", ".orchestrator.json"),
    ("QUEUE_PATH", ".queue.json"),
    ("DOCTRINE_PIN_PATH", ".doctrine.json"),
    ("FINDINGS_PATH", ".findings.json"),
    ("IDEAS_PATH", ".ideas.json"),
    ("OBLIGATIONS_PATH", ".obligations.json"),
    ("DIRECTION_PATH", ".direction.json"),
    ("DEPLOY_PATH", ".deploys.json"),
    ("SUPERVISOR_PATH", ".supervisor.json"),
    ("REPORT_PATH", ".reports.json"),
    ("REPORT_DIR", "reports"),
    ("PACKET_DIR", "packets"),
    ("RULING_DIR", "rulings"),
    ("WORKTREE_DIR", "worktrees"),
    ("CONSULT_DIR", "consults"),
    ("GOAL_DIR", "goals"),
    ("SPECRUN_DIR", "specruns"),
    ("SPECRATE_PATH", ".specrates.json"),
    ("SPECPLAN_PATH", ".specplans.json"),
    ("SPECAUTO_PATH", ".specauto.json"),
)


def _bind_state(base: Path) -> None:
    """Point every workspace-scoped path at `base`."""
    g = globals()
    g["STATE"] = base
    for name, rel in _STATE_LAYOUT:
        g[name] = base / rel


_bind_state(STATE_ROOT)

# The SQLite mirror sits under the state ROOT, not under a workspace: one file
# holds every workspace, so "back this up" and "sync this" name one thing.
# Binding here rather than per-workspace is why a workspace switch needs to
# tell it nothing.
store.bind(STATE_ROOT)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# GPT-5.6 family on OpenRouter, cheapest first. There is no gpt-5.6-codex.
CONSULT_MODELS = {
    "luna": "openai/gpt-5.6-luna-pro",    # $1 / $6  per M
    "terra": "openai/gpt-5.6-terra-pro",  # $2.5 / $15 per M
    "sol": "openai/gpt-5.6-sol-pro",      # $5 / $30 per M
}
DEFAULT_CONSULT = "terra"

# Who answers as the architect. `codex` runs `codex exec` against your ChatGPT
# subscription and costs no metered money; `openrouter` is the metered API path.
ARCHITECT_BACKENDS = ("codex", "openrouter", "claude")
DEFAULT_ARCHITECT = "codex"

# codex cloud list --limit is capped at 20 by the CLI.
LIST_LIMIT = 20

# Worker backends. claude runs locally and can be resumed for follow-up turns;
# codex runs in the cloud, one-shot, with no reply channel.
BACKENDS = ("claude", "codex")
DEFAULT_BACKEND = "claude"
DEFAULT_CLAUDE_MODEL = "opus"
DEFAULT_BUDGET_USD = 1.00

# ---------------------------------------------------------------------- roles
#
# Three models do three different jobs here. A WORKER writes the code. An
# ARCHITECT answers when a worker escalates. A SUPERVISOR holds the whole
# workspace against its mission. Until now only the architect was nameable in
# the console, so the other two were lit by proxy or not lit at all.
#
# They are three settings and not one because they are three decisions. The
# worker runs constantly and is the expensive one; the architect runs on
# escalation; the supervisor runs once, when asked. Wiring them together means a
# change made for one reason silently moves the other two.
#
# Each can also be switched off, and OFF MEANS REFUSED, not hidden. A dark light
# because you turned the role off and a dark light because the CLI is signed out
# are different facts, and the header has to say which one it is looking at -
# the whole point of a light is that you can tell what it means without asking.

ROLES = ("worker", "architect", "supervisor")

# Every model this harness can reach, and which family it comes from. The family
# is what decides how it is called, so it is recorded once here rather than
# re-derived by each caller from the spelling of a name.
MODELS = {
    "opus":   {"kind": "claude", "note": "Claude, on your Claude subscription"},
    "sonnet": {"kind": "claude", "note": "Claude, cheaper and faster"},
    "haiku":  {"kind": "claude", "note": "Claude, cheapest and fastest"},
    "codex":  {"kind": "codex",  "note": "GPT, on your ChatGPT subscription - no metered spend"},
    "luna":   {"kind": "openrouter", "note": "GPT-5.6 luna - $1 / $6 per M"},
    "terra":  {"kind": "openrouter", "note": "GPT-5.6 terra - $2.5 / $15 per M"},
    "sol":    {"kind": "openrouter", "note": "GPT-5.6 sol - $5 / $30 per M"},
}

# The supervisor may also be set to this, meaning: whatever the architect is.
FOLLOW_ARCHITECT = "architect"

# Which families can hold down which job, and why not when they cannot.
#
# Every role's dropdown lists EVERY model, always. Offering each role a
# different short list was the wrong call: a list that silently differs per role
# reads as a bug, and it leaves the console nowhere to say why the option you
# wanted is not there. So the ones that cannot do the job are shown, disabled,
# each carrying its reason.
ROLE_CANNOT = {
    "worker": {
        "openrouter": "no worker runs on the OpenRouter API - a worker edits "
                      "files and runs commands, and that path is chat only",
        "codex": "a codex worker is a property of the lane, not a global "
                 "setting - set the backend on the lane itself",
    },
    "architect": {},
    "supervisor": {},
}


def model_kind(name: str) -> str:
    """Which family a model belongs to, and so how it gets called."""
    return (MODELS.get(name) or {}).get("kind") or "openrouter"

# A worker can spawn a build, a benchmark, or a loop that never ends. These are
# the ceilings it runs under; override any of them in config.json.
DEFAULT_WORKER_TIMEOUT_S = 30 * 60
DEFAULT_WORKER_MEM_GB = 8.0
DEFAULT_WORKER_NICE = 10

# A wall clock cannot tell a worker that is hung from one that is halfway
# through a twenty-minute build - both just sit there. Two readings together
# can: the transcript it writes as it goes has stopped growing, AND its process
# group is burning no CPU. A build is quiet but hot; a wedged worker is quiet
# and cold. Only the second gets stopped.
DEFAULT_WORKER_STALL_S = 15 * 60
IDLE_CPU_PCT = 2.0

# One worker per lane is already enforced, but nine lanes is still nine Opus
# sessions at once - enough to burn a subscription's window in minutes and to
# put nine builds on one laptop. This is the ceiling across the whole board.
DEFAULT_MAX_WORKERS = 3

# A headless worker has nobody to ask. "acceptEdits" waves through file writes but
# still stops on every bash command, so a worker that needs to run a build spends
# its whole budget being asked a question no one can answer. What actually keeps
# this safe is the worktree: a worker only ever sees a clean, throwaway checkout
# on its own branch, and the timeout, memory ceiling and nice level still bind.
DEFAULT_PERMISSION_MODE = "bypassPermissions"

# The orchestrator is not a lane. It runs in the shared checkout, not a
# worktree, because everything it gets asked is about the shared checkout: what
# is running, what is uncommitted, what should go out next. A worktree-isolated
# session is structurally unable to answer any of that. It is one long session,
# resumed every turn, so the window remembers what you already told it.
DEFAULT_ORCH_MODEL = "opus"
DEFAULT_ORCH_BUDGET_USD = 3.00

TERMINAL_STATES = {"completed", "succeeded", "failed", "cancelled", "canceled", "error"}


# ---------------------------------------------------------------- infrastructure


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def die(msg: str, code: int = 1):
    print(f"amp: {msg}", file=sys.stderr)
    raise SystemExit(code)


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as e:
        die(f"{path.name} is not valid JSON: {e}")


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)
    # The second copy, taken from the bytes we just wrote rather than by
    # reading the file back - so the mirror holds this write even if the next
    # one lands a millisecond later. It cannot raise and the file above is
    # already durable, so a broken mirror costs history, never state.
    store.record(path, text)


def config() -> dict:
    return load_json(CONFIG_PATH, {"lanes": {}, "consult_model": DEFAULT_CONSULT})


def board() -> dict:
    return load_json(BOARD_PATH, {"tasks": {}, "polled_at": None})


# ---------------------------------------------------------------- workspaces
#
# A workspace is a whole separate run of this harness against the same disk: its
# own lanes, board, goals, consults, findings, obligations, direction and
# worktrees. Building the core stack and building demos on top of the core stack
# are two different jobs with two different sets of open work, and mixing them
# on one board means the cap, the poll and the direction review are all reasoning
# about a pile that has no single answer to "what is this for".
#
# What is NOT per-workspace: the credentials (one machine, one person, one key)
# and DOCTRINE.md (how we work does not change because the subject did). What IS
# per-workspace is the MISSION - what this particular workspace is for - and it
# is the thing the supervisor holds everything else against.
#
# The default workspace is the state directory itself, not a subdirectory of it.
# That is the one piece of asymmetry here and it is deliberate: everything that
# already exists on this disk keeps working, unmoved, with no migration step
# that could lose a board mid-flight. Later workspaces live under `ws/<slug>`.

DEFAULT_WORKSPACE = "core"
_WORKSPACE_LOCK = threading.Lock()
_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9_-]{0,30}$")

# Anything that has to be dropped when the ground moves under it. The server
# registers its queue here; nothing in this file needs one yet.
WORKSPACE_HOOKS: list = []


class WorkspaceError(ValueError):
    """A workspace cannot be created, removed or switched to right now."""


def _default_registry() -> dict:
    return {
        "current": DEFAULT_WORKSPACE,
        "workspaces": {
            DEFAULT_WORKSPACE: {
                "name": "core development",
                "dir": "",
                "mission": "",
                "created": now(),
            }
        },
    }


def workspaces() -> dict:
    """The registry, repaired rather than trusted.

    A registry naming a current workspace that is not in it would leave every
    path bound to nothing, so the read is where that is caught.
    """
    reg = load_json(WORKSPACES_PATH, None)
    if not isinstance(reg, dict) or not isinstance(reg.get("workspaces"), dict) \
            or not reg["workspaces"]:
        return _default_registry()
    if reg.get("current") not in reg["workspaces"]:
        reg["current"] = DEFAULT_WORKSPACE if DEFAULT_WORKSPACE in reg["workspaces"] \
            else sorted(reg["workspaces"])[0]
    return reg


def _save_workspaces(reg: dict):
    save_json(WORKSPACES_PATH, reg)


def workspace_dir(slug: str, reg: dict | None = None) -> Path:
    reg = reg or workspaces()
    ws = reg["workspaces"].get(slug)
    if ws is None:
        raise WorkspaceError(f"there is no workspace called {slug!r}")
    rel = (ws.get("dir") or "").strip("/")
    return STATE_ROOT / rel if rel else STATE_ROOT


def current_workspace() -> str:
    return workspaces()["current"]


def workspace(slug: str | None = None) -> dict:
    reg = workspaces()
    slug = slug or reg["current"]
    ws = dict(reg["workspaces"].get(slug) or {})
    ws["slug"] = slug
    return ws


def _slug_for(name: str, taken) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")[:24] or "ws"
    slug, n = base, 2
    while slug in taken:
        slug, n = f"{base}-{n}", n + 1
    return slug


def add_workspace(name: str, *, mission: str = "", slug: str | None = None) -> dict:
    """Open a new, empty workspace. It starts with no lanes and no board."""
    name = (name or "").strip()
    if not name:
        raise WorkspaceError("a workspace needs a name")
    with _WORKSPACE_LOCK:
        reg = workspaces()
        slug = slug or _slug_for(name, reg["workspaces"])
        if not _SLUG_OK.match(slug):
            raise WorkspaceError(f"{slug!r} is not a usable workspace id")
        if slug in reg["workspaces"]:
            raise WorkspaceError(f"there is already a workspace called {slug!r}")
        ws = {"name": name, "dir": f"ws/{slug}", "mission": (mission or "").strip(),
              "created": now()}
        reg["workspaces"][slug] = ws
        _save_workspaces(reg)
    (STATE_ROOT / "ws" / slug).mkdir(parents=True, exist_ok=True)
    return dict(ws, slug=slug)


def rename_workspace(slug: str, name: str) -> dict:
    name = (name or "").strip()
    if not name:
        raise WorkspaceError("a workspace needs a name")
    with _WORKSPACE_LOCK:
        reg = workspaces()
        if slug not in reg["workspaces"]:
            raise WorkspaceError(f"there is no workspace called {slug!r}")
        reg["workspaces"][slug]["name"] = name
        _save_workspaces(reg)
        return dict(reg["workspaces"][slug], slug=slug)


def remove_workspace(slug: str) -> dict:
    """Forget a workspace. Its directory is left on disk, untouched.

    Deleting the state would delete goals, rulings and transcripts that are the
    only record of what was decided and why. Dropping it from the registry makes
    it stop appearing; recovering it is a matter of putting the entry back.
    """
    with _WORKSPACE_LOCK:
        reg = workspaces()
        if slug not in reg["workspaces"]:
            raise WorkspaceError(f"there is no workspace called {slug!r}")
        if slug == reg["current"]:
            raise WorkspaceError("switch to another workspace before removing this one")
        if len(reg["workspaces"]) == 1:
            raise WorkspaceError("this is the only workspace")
        gone = reg["workspaces"].pop(slug)
        _save_workspaces(reg)
        return dict(gone, slug=slug, dropped=True)


def switch_blocked() -> str | None:
    """Why the ground must not move right now, or None.

    Every path in this file is a module global, so a switch rebinds them under
    whatever is already running. A worker that started against one board and
    settles against another writes its result into the wrong workspace, and the
    lane it was holding never gets released. There is no clever fix for that
    which is worth having - the honest answer is to refuse while anything is in
    flight and say what is in flight.
    """
    n = live_workers()
    if n:
        # Name them. "2 workers still running" is a fact the operator cannot act
        # on; "traaviis, trvm" tells them which two lanes to stop or wait for,
        # which is the difference between a refusal and a dead end.
        where = live_worker_lanes()
        return (f"{n} worker{'s' if n != 1 else ''} still running"
                + (f" in {', '.join(where)}" if where else ""))
    if orch_busy():
        return "the orchestrator is mid-turn"
    return None


def use_workspace(slug: str) -> dict:
    """Point the whole harness at another workspace."""
    with _WORKSPACE_LOCK:
        reg = workspaces()
        if slug not in reg["workspaces"]:
            raise WorkspaceError(f"there is no workspace called {slug!r}")
        if slug == reg["current"]:
            return workspace(slug)
        why = switch_blocked()
        if why:
            raise WorkspaceError(f"cannot switch workspace: {why}")
        base = workspace_dir(slug, reg)
        base.mkdir(parents=True, exist_ok=True)
        _bind_state(base)
        reg["current"] = slug
        _save_workspaces(reg)
    # Outside the lock: a hook that wanted to read state would deadlock on it,
    # and by here the rebinding is already done and durable.
    for hook in list(WORKSPACE_HOOKS):
        try:
            hook(slug)
        except Exception:                                     # noqa: BLE001
            pass
    return workspace(slug)


# ---------------------------------------------------------------- the mission
#
# The doctrine says how work is done anywhere in this ecosystem. The mission
# says what THIS workspace is for, in the operator's own words, and it is the
# only text in the harness that is neither derived nor generated - nothing
# proposes it, nothing reviews it, nothing may edit it but Travis. Rule 6 in one
# field.
#
# It goes out with the doctrine to every planner, worker and review, because a
# plan that is excellent and off-mission is the expensive failure this is here
# to catch.


def mission(slug: str | None = None) -> str:
    return (workspace(slug).get("mission") or "").strip()


def set_mission(text: str, slug: str | None = None) -> dict:
    with _WORKSPACE_LOCK:
        reg = workspaces()
        slug = slug or reg["current"]
        if slug not in reg["workspaces"]:
            raise WorkspaceError(f"there is no workspace called {slug!r}")
        reg["workspaces"][slug]["mission"] = (text or "").strip()
        reg["workspaces"][slug]["mission_at"] = now()
        _save_workspaces(reg)
        return dict(reg["workspaces"][slug], slug=slug)


def mission_block(intro: str = "What this workspace is for") -> str:
    """The mission under a heading, or nothing. Never an invented one."""
    m = mission()
    return f"\n\n# {intro}\n\n{m}\n" if m else ""


def workspace_view() -> dict:
    reg = workspaces()
    cur = reg["current"]
    return {
        "current": cur,
        "mission": mission(cur),
        "blocked": switch_blocked(),
        "list": [
            {"slug": s, "name": w.get("name") or s,
             "mission": (w.get("mission") or "").strip(),
             "created": w.get("created"), "current": s == cur}
            for s, w in sorted(reg["workspaces"].items(),
                               key=lambda kv: kv[1].get("created") or "")
        ],
    }


# Import binds to the root; this moves it to whichever workspace was in use when
# the console last stopped. Wrapped because a harness that cannot start at all
# because of a damaged registry is worse than one that starts on the default.
try:
    _bind_state(workspace_dir(current_workspace()))
except Exception as _e:                                       # noqa: BLE001
    print(f"amp: staying on the default workspace ({_e})", file=sys.stderr)


CHROME_NAMES = ("google-chrome-stable", "google-chrome", "chromium", "chromium-browser")


def browser_cmd() -> str | None:
    """Chrome by preference. python's webbrowser picks firefox on this box."""
    for name in CHROME_NAMES:
        path = shutil.which(name)
        if path:
            return path
    return shutil.which("xdg-open")


def open_url(url: str) -> bool:
    cmd = browser_cmd()
    if not cmd:
        return False
    subprocess.Popen([cmd, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)
    return True


def run(cmd: list[str], cwd: Path | None = None, check: bool = False,
        env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, cwd=str(cwd) if cwd else None, capture_output=True, text=True, check=check,
        env=env,
    )


# The console runs claude workers on background threads, so board writes are
# serialized: two workers finishing at once must not lose each other's record.
_BOARD_LOCK = threading.Lock()


def record_task(lane_name: str, rec: dict):
    with _BOARD_LOCK:
        b = board()
        b.setdefault("tasks", {}).setdefault(lane_name, []).insert(0, rec)
        save_json(BOARD_PATH, b)


def update_task(lane_name: str, task_id: str, patch: dict):
    with _BOARD_LOCK:
        b = board()
        for rec in b.get("tasks", {}).get(lane_name, []):
            if rec.get("task_id") == task_id:
                rec.update(patch)
                break
        save_json(BOARD_PATH, b)


_CHAT_LOCK = threading.Lock()


def add_note(text: str, lane: str | None = None, kind: str | None = None) -> dict:
    """A line the operator typed into the orchestrator thread.

    Only notes are stored. Everything else in the thread is derived from the
    board, so the feed can never drift from what the workers actually did.

    `kind` is how the thread tells an ordinary line from one it should show
    differently - an `idea` is a passing remark, not a report.
    """
    note = {"id": uuid.uuid4().hex[:12], "at": now(), "text": text, "lane": lane,
            "kind": kind}
    with _CHAT_LOCK:
        chat = load_json(CHAT_PATH, {"notes": []})
        chat.setdefault("notes", []).append(note)
        save_json(CHAT_PATH, chat)
    return note


def notes() -> list[dict]:
    return load_json(CHAT_PATH, {"notes": []}).get("notes", [])


# ---------------------------------------------------------------- orchestrator

_ORCH_LOCK = threading.Lock()


def orchestrator() -> dict:
    return load_json(ORCH_PATH, {"session_id": None, "turns": []})


def orchestrator_turns() -> list[dict]:
    return orchestrator().get("turns", [])


def orch_append(turn: dict) -> dict:
    with _ORCH_LOCK:
        o = orchestrator()
        o.setdefault("turns", []).append(turn)
        save_json(ORCH_PATH, o)
    return turn


def orch_update(turn_id: str, patch: dict):
    with _ORCH_LOCK:
        o = orchestrator()
        for t in o.get("turns", []):
            if t.get("id") == turn_id:
                t.update(patch)
                break
        if "session_id" in patch and patch["session_id"]:
            o["session_id"] = patch["session_id"]
        save_json(ORCH_PATH, o)


def orch_busy() -> bool:
    return any(t.get("status") == "running" for t in orchestrator_turns())


# ---------------------------------------------------------------- doctrine
#
# The one piece of context that is not about a lane. Everything else this
# harness assembles - the repository state, the transcript, the board - answers
# "what is here"; this answers "what are we holding ourselves to, and what are
# we trying to find out". It is written once, at {root}/DOCTRINE.md, and injected
# verbatim into every architect plan, every architect review, every goal
# worker's prompt and the orchestrator's own brief, so that no surface can drift
# from it independently.
#
# Only the block between the markers travels. The commentary below it in that
# file is where each rule came from and what it cost to learn - worth reading,
# too long to carry into every prompt, and the discipline of keeping the core
# short is what keeps it from being skimmed.

DOCTRINE_PATH = Path(os.environ.get("AMP_DOCTRINE") or (ROOT / "DOCTRINE.md"))
DOCTRINE_BEGIN = "<!-- BEGIN CORE -->"
DOCTRINE_END = "<!-- END CORE -->"
_DOCTRINE_CACHE: dict = {"mtime": None, "text": ""}


def doctrine() -> str:
    """The injectable core of DOCTRINE.md, or "" if there isn't one.

    Cached on mtime rather than read once at import, so editing the file takes
    effect on the next prompt instead of the next restart - the point of writing
    the values down is that they can be corrected, and a correction nobody is
    running under is not a correction.

    Missing file, missing markers and empty core all return "", and every caller
    treats that as "say nothing" rather than substituting a default. A doctrine
    this harness made up for itself would be exactly the thing rule 6 forbids.
    """
    try:
        st = DOCTRINE_PATH.stat()
    except OSError:
        _DOCTRINE_CACHE.update(mtime=None, text="")
        return ""
    key = (st.st_mtime_ns, st.st_size)
    if _DOCTRINE_CACHE["mtime"] == key:
        return _DOCTRINE_CACHE["text"]
    try:
        raw = DOCTRINE_PATH.read_text()
    except (OSError, UnicodeDecodeError):
        raw = ""
    a, b = raw.find(DOCTRINE_BEGIN), raw.find(DOCTRINE_END)
    core = raw[a + len(DOCTRINE_BEGIN):b].strip() if 0 <= a < b else ""
    _DOCTRINE_CACHE.update(mtime=key, text=core)
    return core


def doctrine_block(intro: str) -> str:
    """The core under a heading, or nothing at all. Never a partial quotation."""
    core = doctrine()
    return f"\n\n# {intro}\n\n{core}\n" if core else ""


# Ratification, and why a digest rather than a permission.
#
# Nothing here can stop an agent editing DOCTRINE.md - the orchestrator runs in
# the shared checkout with a shell, and a rule that a determined process can
# ignore is not an enforcement mechanism. What can be made true is that such an
# edit cannot happen *quietly*: the core is pinned by digest, and any change to
# it stands on the board, named, until the operator says it was theirs. A
# doctrine a system can amend without anyone noticing is one it holds nobody to.
#
# The harness never pins on its own. An unratified core is itself the
# observation - that is the whole point of rule 6.

# DOCTRINE_PIN_PATH is bound by _bind_state; see _STATE_LAYOUT. The doctrine
# itself is ecosystem-wide, but ratifying it is an act by an operator looking at
# one board, so the pin travels with the workspace.


def doctrine_digest() -> str:
    core = doctrine()
    return hashlib.sha256(core.encode()).hexdigest()[:16] if core else ""


def doctrine_pin() -> dict:
    return load_json(DOCTRINE_PIN_PATH, {"digest": None, "at": None})


def ratify_doctrine() -> dict:
    """The operator saying this version of the core is the one that stands."""
    pin = {"digest": doctrine_digest(), "at": now()}
    save_json(DOCTRINE_PIN_PATH, pin)
    return pin


def doctrine_state() -> dict:
    """Whether what is being injected is what the operator last ratified."""
    cur = doctrine_digest()
    pin = doctrine_pin()
    return {"present": bool(cur), "digest": cur, "ratified": pin.get("digest"),
            "ratified_at": pin.get("at"),
            "drifted": bool(cur and pin.get("digest") and pin["digest"] != cur),
            "unratified": bool(cur and not pin.get("digest"))}


# ---------------------------------------------------------------- findings
#
# The return path. Doctrine going out to every worker is only half a loop: if
# nothing comes back, the values can only be obeyed, never corrected, and the
# work that would have corrected them is spent and forgotten inside a transcript
# nobody re-reads. So every worker report and every architect review ends with a
# DOCTRINE: line, and the ones that say something land here, unread, until the
# operator has actually been told.
#
# `none` is deliberately not stored. It is the ordinary answer and storing it
# would bury the four findings a week under two hundred non-findings - the same
# reason the doctrine tells workers not to manufacture one.

# FINDINGS_PATH is bound by _bind_state; see _STATE_LAYOUT.
_FINDINGS_LOCK = threading.Lock()

# Ordered by how much they should interrupt the operator. A contradiction means
# something we believed and acted on is false, so it outranks a success.
BEARINGS = ("contradicted", "advanced", "proposed", "none")

# The heading, however it gets dressed up: `DOCTRINE:`, `## DOCTRINE:`,
# `**DOCTRINE:**`. The bearing is the first word after it, and everything to the
# end of the text is the finding.
_DOCTRINE_HEAD = re.compile(r"^[\s>#*_-]*DOCTRINE[\s*_]*:[\s*_]*", re.I | re.M)


def parse_doctrine(text: str) -> dict | None:
    """The DOCTRINE: finding at the end of a report, or None.

    The *last* heading, not the first: a worker that quotes its own instructions
    back before answering them would otherwise be read as reporting the example.
    An unrecognised first word is kept as `proposed` rather than dropped - a
    finding that does not fit the four words is still a finding, and silently
    discarding it is the failure this channel exists to prevent.
    """
    if not text:
        return None
    heads = list(_DOCTRINE_HEAD.finditer(text))
    if not heads:
        return None
    body = text[heads[-1].end():].strip().lstrip("*_ \t")
    if not body:
        return None
    first = re.match(r"[a-zA-Z]+", body)
    word = first.group(0).lower() if first else ""
    bearing = word if word in BEARINGS else "proposed"
    if bearing == word:
        body = body[first.end():].lstrip(" *_-:.\u2014\t\n")
    if bearing == "none":
        return {"bearing": "none", "text": body[:600]}
    return {"bearing": bearing, "text": body[:4000]} if body else None


def record_finding(bearing: str, text: str, *, lane: str | None = None,
                   source: str = "worker", **where) -> dict | None:
    """File a finding. Returns None for `none`, which is not worth keeping."""
    bearing = (bearing or "").strip().lower()
    text = (text or "").strip()
    if bearing not in BEARINGS or bearing == "none" or not text:
        return None
    f = {"id": uuid.uuid4().hex[:12], "at": now(), "bearing": bearing,
         "text": text[:4000], "lane": lane, "source": source,
         "read_at": None, **{k: v for k, v in where.items() if v}}
    with _FINDINGS_LOCK:
        store = load_json(FINDINGS_PATH, {"findings": []})
        store.setdefault("findings", []).append(f)
        store["findings"] = store["findings"][-500:]
        save_json(FINDINGS_PATH, store)
    return f


def record_worker_finding(lane_name: str, rec: dict) -> dict | None:
    """File whatever a finished worker said under DOCTRINE:, if anything.

    Read from `result` - the worker's own last message - and not from the report
    the architect gets, which carries a diff that may well contain the word.
    """
    f = parse_doctrine(rec.get("result") or "")
    if not f:
        return None
    return record_finding(f["bearing"], f["text"], lane=lane_name, source="worker",
                          task_id=rec.get("task_id"), goal_id=rec.get("goal_id"))


def findings(*, lane: str | None = None, unread_only: bool = False) -> list[dict]:
    """Newest first, and a contradiction ahead of anything else the same day."""
    out = load_json(FINDINGS_PATH, {"findings": []}).get("findings", [])
    if lane:
        out = [f for f in out if f.get("lane") == lane]
    if unread_only:
        out = [f for f in out if not f.get("read_at")]
    return sorted(out, key=lambda f: (f.get("bearing") == "contradicted",
                                      f.get("at") or ""), reverse=True)


def settle_finding(fid: str, settlement: dict) -> bool:
    """Close a finding because something was DONE about it.

    Not the same act as `ack_findings`, and the difference is the whole point.
    An ack says the operator has seen it, which only the operator can make true.
    A settlement says the harness performed a specific, recorded consequence -
    a rung retracted, a later finding that supersedes this one, a proposal
    filed - and carries the id of that consequence. If the consequence could not
    be performed there is no settlement, and the finding stays unread.
    """
    with _FINDINGS_LOCK:
        store = load_json(FINDINGS_PATH, {"findings": []})
        for f in store.get("findings", []):
            if f.get("id") == fid and not f.get("read_at"):
                f["read_at"] = now()
                f["settled"] = settlement
                save_json(FINDINGS_PATH, store)
                return True
    return False


def ack_findings(ids: list[str]) -> int:
    """Mark findings as told to the operator. Only they can make this true."""
    want = set(ids or ())
    n = 0
    with _FINDINGS_LOCK:
        store = load_json(FINDINGS_PATH, {"findings": []})
        for f in store.get("findings", []):
            if f.get("id") in want and not f.get("read_at"):
                f["read_at"] = now()
                n += 1
        save_json(FINDINGS_PATH, store)
    return n


def findings_summary() -> dict:
    """What the header needs: how many are waiting, and whether any is bad news."""
    unread = findings(unread_only=True)
    return {"unread": len(unread),
            "contradicted": sum(1 for f in unread if f["bearing"] == "contradicted"),
            "top": unread[0] if unread else None}


# ------------------------------------------------------------------- ideas
#
# The other thing a worker learns and nobody keeps.
#
# A finding is about the doctrine: something we believed turned out to be false,
# or a claim moved up the ladder. That channel is narrow on purpose, and it has
# a cost - it feeds the contradictions gate, so filing into it stops the fleet.
# But most of what a worker notices is neither: it is "while I was in here I saw
# that X would obviously be worth doing", and there has been nowhere to put it.
# It goes into a transcript, the worktree is thrown away, and the idea is gone.
#
# So: a second, cheap channel. It is deliberately NOT a finding.
#   - it gates nothing and interrupts nothing;
#   - it says one line in the chat when it arrives, which is all the operator
#     asked for at the moment it arrives;
#   - and it accumulates, so the report and the solver can read a season of them
#     at once and turn the ones that are still good into actual proposals.
#
# An idea is a lead, not a claim. Nothing here is scored, adopted or acted on
# until something that CAN be held to the bars picks it up.

# IDEAS_PATH is bound by _bind_state; see _STATE_LAYOUT.
_IDEAS_LOCK = threading.Lock()

# `IDEA:`, `## IDEA:`, `**IDEA:**`, `- IDEA:`. Every one of them, not the last:
# a worker that noticed three things should not have two of them dropped.
_IDEA_HEAD = re.compile(r"^[\s>#*_-]*IDEAS?[\s*_]*:[\s*_]*(.+)$", re.I | re.M)

IDEA_MAX = 400


def parse_ideas(text: str) -> list[str]:
    """Every `IDEA:` line in a report, one line each, in the order written."""
    out = []
    for m in _IDEA_HEAD.finditer(text or ""):
        line = m.group(1).strip().strip("*_ \t")
        # "none" is the ordinary answer here too, and storing it would bury the
        # real ones exactly as it would in findings.
        if line and line.lower() not in ("none", "none.", "n/a", "-"):
            out.append(line[:600])
    return out


def record_ideas(texts: list[str], *, lane: str | None = None,
                 source: str = "worker", **where) -> list[dict]:
    """File leads. Repeats of something already filed are dropped, not stacked.

    Workers on the same lane notice the same obvious thing repeatedly, and a
    list with the same line on it eleven times reads as noise rather than as
    eleven agreements - so a repeat bumps `seen` on the one already there.
    """
    fresh = []
    with _IDEAS_LOCK:
        store = load_json(IDEAS_PATH, {"ideas": []})
        rows = store.setdefault("ideas", [])
        by_text = {_norm_prompt(i["text"]): i for i in rows}
        for t in texts:
            t = (t or "").strip()
            if not t:
                continue
            old = by_text.get(_norm_prompt(t))
            if old:
                old["seen"] = int(old.get("seen") or 1) + 1
                old["last_at"] = now()
                continue
            row = {"id": "i" + uuid.uuid4().hex[:9], "at": now(), "text": t[:600],
                   "lane": lane, "source": source, "seen": 1, "state": "open",
                   **{k: v for k, v in where.items() if v}}
            rows.append(row)
            by_text[_norm_prompt(t)] = row
            fresh.append(row)
        store["ideas"] = rows[-IDEA_MAX:]
        save_json(IDEAS_PATH, store)
    return fresh


def record_worker_ideas(lane_name: str, rec: dict) -> list[dict]:
    """Whatever a finished worker noticed in passing, and said one line about.

    Read from `result`, the worker's own last message, for the same reason the
    finding is: the report the architect gets carries a diff, and a diff can
    contain the word.
    """
    fresh = record_ideas(parse_ideas(rec.get("result") or ""), lane=lane_name,
                         source="worker", task_id=rec.get("task_id"),
                         goal_id=rec.get("goal_id"))
    for i in fresh:
        # Briefly, in the chat, as it arrives. That is the whole point: it is
        # said once, cheaply, and then it waits.
        add_note(f"idea from {lane_name}: {i['text']}", lane=lane_name, kind="idea")
    return fresh


def ideas(*, lane: str | None = None, open_only: bool = True) -> list[dict]:
    """Newest first, and the ones several workers hit on ahead of the rest."""
    out = load_json(IDEAS_PATH, {"ideas": []}).get("ideas", [])
    if lane:
        out = [i for i in out if i.get("lane") == lane]
    if open_only:
        out = [i for i in out if i.get("state", "open") == "open"]
    return sorted(out, key=lambda i: (int(i.get("seen") or 1), i.get("at") or ""),
                  reverse=True)


def close_ideas(ids: list[str], state: str = "picked") -> int:
    """Take ideas off the list - picked up, or not worth it. Both are answers."""
    want = set(ids or ())
    n = 0
    with _IDEAS_LOCK:
        store = load_json(IDEAS_PATH, {"ideas": []})
        for i in store.get("ideas", []):
            if i.get("id") in want and i.get("state", "open") == "open":
                i["state"], i["closed_at"] = state, now()
                n += 1
        save_json(IDEAS_PATH, store)
    return n


# ------------------------------------------------------------------ obligations
#
# A goal is a thing that becomes true once and is then finished. An obligation is
# a thing that has to KEEP being true - the published docs still describe the code,
# the benchmark page still shows the numbers the benchmark actually produced.
#
# The difference that matters is not the schedule. It is that an obligation owns a
# CHECK which decides whether it is currently true. A timer that re-runs a job
# spends a worker whether or not anything moved; a check spends one only when
# something did, and it answers "is this current?" with an observation instead of
# an assumption. That is rule 1 applied to maintenance.
#
# Two deliberate limits, both learned rather than chosen:
#
#   - Checks run at ROOT, in the shared checkout. They have to: the docs atlas
#     build scans every repo in the stack and cannot see a sibling from inside a
#     lane worktree. A check is read-only, so this is safe.
#
#   - A FIX is a worker that writes, and `claude_worktree` already documents why a
#     background writer in the shared checkout is dangerous - interactive sessions
#     run against it constantly. So auto_fix defaults to False: drift is reported,
#     and dispatching something to repair it stays the operator's call until they
#     say otherwise per obligation. An obligation that repaired the tree out from
#     under a live session would be worse than a stale docs site.

# OBLIGATIONS_PATH is bound by _bind_state; see _STATE_LAYOUT.
_OBLIGATION_LOCK = threading.Lock()
OBLIGATION_STATES = ("unchecked", "ok", "drifted", "broken")


def obligations() -> list[dict]:
    return load_json(OBLIGATIONS_PATH, [])


def save_obligations(obs: list[dict]):
    save_json(OBLIGATIONS_PATH, obs)


def add_obligation(name: str, check: str, *, why: str = "", fix: str = "",
                   every_hours: float = 24.0, lane: str | None = None,
                   auto_fix: bool = False) -> dict:
    """Register something that has to keep being true, and the command that decides."""
    name = (name or "").strip()
    check = (check or "").strip()
    if not name:
        die("an obligation needs a name")
    if not check:
        die("an obligation needs a check - without one it is a reminder, not an obligation")
    with _OBLIGATION_LOCK:
        obs = obligations()
        if any(o["name"] == name for o in obs):
            die(f"there is already an obligation named {name!r}")
        ob = {"id": "o" + uuid.uuid4().hex[:8], "name": name, "why": why.strip(),
              "check": check, "fix": fix.strip(), "every_hours": float(every_hours),
              "lane": lane, "auto_fix": bool(auto_fix), "enabled": True,
              "state": "unchecked", "added_at": now(),
              "last_checked": None, "last_ok_at": None, "last_drift_at": None,
              "last_rc": None, "last_output": "", "last_task_id": None, "checks": 0}
        obs.append(ob)
        save_obligations(obs)
    return ob


def update_obligation(oid: str, patch: dict) -> dict | None:
    with _OBLIGATION_LOCK:
        obs = obligations()
        for o in obs:
            if o["id"] == oid:
                o.update(patch)
                save_obligations(obs)
                return o
    return None


def remove_obligation(oid: str) -> bool:
    with _OBLIGATION_LOCK:
        obs = obligations()
        keep = [o for o in obs if o["id"] != oid]
        if len(keep) == len(obs):
            return False
        save_obligations(keep)
    return True


def obligation_due(ob: dict, *, at: float | None = None) -> bool:
    """Enabled, and not checked within its own interval."""
    if not ob.get("enabled"):
        return False
    last = ob.get("last_checked")
    if not last:
        return True
    try:
        prev = datetime.fromisoformat(last).timestamp()
    except (TypeError, ValueError):
        return True
    return (at or time.time()) - prev >= float(ob.get("every_hours") or 24.0) * 3600.0


def run_obligation_check(ob: dict, *, timeout: int = 900) -> dict:
    """Run the check in the shared checkout and record what it said.

    Three outcomes, kept apart on purpose. rc 0 is `ok`. A non-zero rc is
    `drifted` - the thing the check describes is no longer true. A check that
    could not run at all (timeout, missing interpreter) is `broken`, NOT drifted:
    reporting "the docs are stale" because the checker crashed would be inventing
    an observation, and a broken check must be fixed before its verdict means
    anything.
    """
    started = now()
    try:
        p = subprocess.run(ob["check"], shell=True, cwd=str(ROOT), timeout=timeout,
                           capture_output=True, text=True)
        rc, out = p.returncode, ((p.stdout or "") + (p.stderr or ""))
    except subprocess.TimeoutExpired:
        rc, out = None, f"check exceeded {timeout}s and was killed"
    except OSError as e:
        rc, out = None, f"check could not run: {e}"
    state = "broken" if rc is None else ("ok" if rc == 0 else "drifted")
    patch = {"state": state, "last_checked": started, "last_rc": rc,
             "last_output": (out or "").strip()[-4000:],
             "checks": int(ob.get("checks") or 0) + 1}
    if state == "ok":
        patch["last_ok_at"] = started
    elif state == "drifted":
        patch["last_drift_at"] = started
    return update_obligation(ob["id"], patch) or {**ob, **patch}


def obligations_summary() -> dict:
    obs = obligations()
    return {"total": len(obs),
            "drifted": sum(1 for o in obs if o.get("state") == "drifted"),
            "broken": sum(1 for o in obs if o.get("state") == "broken"),
            "unchecked": sum(1 for o in obs if o.get("state") == "unchecked")}


ORCH_BRIEF = """You are [&] orchestrator: the operator's single window onto this workspace.

You run in the shared checkout at {root} - not a worktree - so you can see
uncommitted work, run git, and answer questions about what is actually here.
{doctrine}
That is what this whole workspace is for, and it is what the operator wants to
talk about. Dispatching and status are the mechanics; the conversation worth
having is which claims moved up the ladder, what got contradicted, and what is
worth building next because of it. When you report on work, report it in those
terms - "this took X from in_tree to live_local, here is the evidence" - not as
a list of tasks that finished.

Findings come back to you through the harness, not by you going to look:
  curl -s '{base}/api/findings'
      what workers and the architect have reported under DOCTRINE: - what
      advanced, what was contradicted, what is being proposed. `unread` is what
      the operator has not been told yet. A contradiction outranks everything
      else on the board; surface it first and by name.
  curl -s -X POST {base}/api/findings/ack \\
       -H 'content-type: application/json' -d '{{"ids":["ID","ID"]}}'
      mark findings as told to the operator, once you have actually told them
  curl -s '{base}/api/ideas'
      the other channel: things workers noticed in passing while doing something
      else, one line each, never judged. Not findings - they gate nothing and
      mean nothing until something picks them up. Do not read this list out
      unprompted; mention one only when the operator is asking what is worth
      doing next, and say that nobody has assessed it.

You may propose an amendment to the doctrine and you must not make one. If work
here shows a rule is wrong, or there is a value worth adding, say so to the
operator as a proposal with what would test it. Editing {root}/DOCTRINE.md is
theirs alone - a system that can rewrite what it is held to is not held to
anything.

Some things are not goals but obligations: they have to keep being true, and each
owns a check that decides whether it currently is. A goal finishes; an obligation
comes back. The harness runs the checks on a clock and you can read the verdicts:
  curl -s '{base}/api/obligations'
      each one's name, its check, when it last ran, and what it said. `drifted`
      means the check ran and the thing is no longer true. `broken` means the
      check could not run - that is NOT drift, and reporting it as though the
      artifact were stale would be inventing an observation.
  curl -s -X POST {base}/api/obligation/check \\
       -H 'content-type: application/json' -d '{{"id":"ID"}}'
      run one now instead of waiting for the clock

Repairing drift is a dispatch, and most obligations are about artifacts built
from the whole workspace rather than one repo - so their fix has to run in the
shared checkout, where the operator's own sessions are working. That is why
auto-fix is off by default. Tell the operator what has drifted and what the
recorded fix is; do not run it in the shared tree on your own initiative.

The console you are part of is serving at {base}. Drive the board through it
rather than reimplementing it: every guard (one worker per lane, the global
worker cap, budgets, timeouts, the memory ceiling) lives behind these
endpoints, and none of them apply to anything you run yourself.

  curl -s {base}/api/state
      lanes with their paths and backends, recent tasks per lane, how many
      workers are running and the cap
  curl -s '{base}/api/log?lane=NAME&task_id=ID'
      a worker's transcript, turn by turn
  curl -s '{base}/api/consults?lane=NAME'
      escalation threads with GPT-5.6 and their rulings
  curl -s -X POST {base}/api/dispatch \\
       -H 'content-type: application/json' \\
       -d '{{"lane":"NAME","prompt":"...","budget":1,"model":"opus"}}'
      start a worker. Returns immediately - it runs in the background. Every
      prompt you write should end with the landing rule below, because a worker
      that does not commit loses its work to the clock, and one that commits
      without building leaves a broken commit that looks finished.
  curl -s -X POST {base}/api/ask \\
       -H 'content-type: application/json' \\
       -d '{{"lane":"NAME","question":"..."}}'
      escalate to GPT-5.6, the architect, about a lane
  curl -s -X POST {base}/api/cancel \\
       -H 'content-type: application/json' -d '{{"task_id":"ID"}}'
      stop a running worker and everything it spawned
  curl -s -X POST {base}/api/restart \\
       -H 'content-type: application/json' -d '{{"lane":"NAME"}}'
      stop a worker and run its own prompt again. Spends a second budget, so
      only when the operator asked for it
  curl -s -X POST {base}/api/lane/add \\
       -H 'content-type: application/json' \\
       -d '{{"lane":"NAME","path":"DIR"}}'
      register a repo under {root} as a new lane, so you can dispatch to it.
      `path` defaults to the lane name; `repo` is read off the git origin;
      `branch` defaults to main. Answers {{"ok":false,"error":"..."}} with the
      reason if the directory is not a repo root, the branch does not exist, or
      the name is already taken

Work that has no lane is not work you cannot do - it is a lane you have not
made yet. If the operator points at a repo under {root} with nothing covering
it, add the lane and dispatch, in the same turn. Do not offer to do the work
yourself in your own turn instead: that spends your budget on something a
worker is for, and leaves nothing on the board to watch. Only ask first if the
right repo or branch is genuinely ambiguous.

When the operator states an objective rather than a task - "finish X", "get Y
working", anything with an end state rather than an action - open a **goal**,
do not dispatch a worker at it. A goal is the whole cycle: the architect writes
a definition of done and an ordered task list against the real repository, a
worker takes one task at a time, every result is judged against that definition,
any done-item carrying a `check` is settled by running the command rather than
by anyone's opinion, and the next task goes out on its own. It ends when the
definition of done is met, or it stops and says which bound it hit.

  curl -s -X POST {base}/api/goal/open \\
       -H 'content-type: application/json' \\
       -d '{{"lane":"NAME","objective":"finish the X implementation"}}'
      plan and start one. This calls the architect, so it takes ~20s
  curl -s '{base}/api/goal?id=GID'
      the whole goal: done-conditions with what met them, tasks and their
      states, the log of every round
  curl -s -X POST {base}/api/goal/answer \\
       -H 'content-type: application/json' -d '{{"goal_id":"GID","text":"..."}}'
      answer what it asked and let it carry on
  curl -s -X POST {base}/api/goal/push \\
       -H 'content-type: application/json' -d '{{"goal_id":"GID"}}'
      give a goal that ran out of rounds its budget back
  curl -s -X POST {base}/api/goal/close \\
       -H 'content-type: application/json' -d '{{"goal_id":"GID"}}'
      abandon it

`observations` in /api/state is the harness saying what is obviously wrong right
now, computed from the board and the worktrees - not guessed. Read it before
answering "how are things going", and quote it rather than re-deriving it:
- `repeat-kill` - the same instruction has been killed by a limit more than
  once. Sending it again unchanged buys another thirty minutes of nothing
- `work-at-risk` - uncommitted changes sitting in a lane worktree with no
  worker in it. Real work that no branch will ever be merged from
- `thread-mute` - a thread waiting on a worker that already finished
- `goal-stopped` / `thread-stopped` - waiting on the operator
- `shared-repo` - two lanes over one directory, which is how two workers each
  "finish" the same thing on separate branches

A consult carries itself: a ruling that ends in NEED: lines has its file needs
answered off disk and a worker sent for anything observable, and the report goes
back to the architect automatically, round after round. So `blocked_on` on a
thread is the thing to read, not the presence of needs:
- `gathering` - a worker is out fetching it. Nothing to do, and nothing to
  report as stalled
- `operator` - it asked for a decision only the operator can make. This one is
  worth surfacing to them, with the actual question
- `stalled`, `rounds`, `tokens` - it stopped itself. Say so plainly; do not
  restart it by asking the same thing again without adding something new

  curl -s -X POST {base}/api/consult/continue \\
       -H 'content-type: application/json' -d '{{"consult_id":"ID"}}'
      send a halted thread after what it is missing again - files off disk, a
      worker for anything observable. This is also how a thread from before any
      of that existed gets asked for the first time

Never answer "is it stuck?" from elapsed time or from a silent log - a worker
halfway through a twenty-minute build looks exactly like a wedged one. The
`workers` map in /api/state has already made that call, per task id:
- `phase` and `doing` - what it is doing right now, read from its transcript
  ("running gcc -O2 ...", "editing src/foo.c", "thinking")
- `quiet_s` - how long since it last wrote anything
- `cpu_pct` - how hard its process group is working. Quiet AND cold is hung;
  quiet AND hot is a build, and is fine
- `stalled` - the harness's own verdict. Trust this one over your own reading
- `adopted` - it outlived a console restart and was picked back up
A stalled worker is killed by the harness on its own, with the reason written
to the board. You do not need to watch for that, and cannot.

Lanes are separate git repos under {root}. A dispatched worker gets a clean
worktree on branch amp/NAME cut from committed HEAD, so it cannot see
uncommitted work in the shared checkout. If that matters for a task, say so
instead of dispatching blind.

End every worker prompt you write with this, verbatim. A goal's workers get it
already; one you dispatch yourself does not, and both failure modes it names
have actually happened here:

{landing}

Ask every worker you dispatch to end its report with a `DOCTRINE:` section
saying which of `advanced` / `contradicted` / `proposed` / `none` its work bears
on, and why. A goal's workers are already asked this. `none` is the ordinary
answer and is fine; an invented finding is worse than no finding.

Ask them for `IDEA:` lines too, verbatim:

{ideas}

Those land on a list nobody has to read today. They gate nothing and start
nothing. A report can pick them up later, which is the only reason they are
worth writing down at all - so do not act on one because you saw it here.

What you cannot do, and must not promise:
- **You stop existing the moment you reply.** There is no background you. Never
  say you will watch for something, check back, follow up, or do anything
  "when X finishes" - none of that can happen. If it needs doing later, either
  do it now or tell the operator it is theirs to trigger.
- A dispatch with no slot free is not refused, it is **queued**, and the worker
  whose finishing frees the slot starts it. /api/dispatch answers
  `{{"queued": true, "position": N}}` when that happens. Report that as queued
  by the harness - not as something you will come back and do.

How to behave:
- You are a chat first. If the question is "is anything running", answer it.
  Do not dispatch anything unless the operator asked for work to be done.
- Anything long belongs in a lane worker, not in your own turn. Dispatch it,
  then say what you dispatched and where to watch it.
- Read /api/state before naming a lane. Never dispatch to one that is not
  there - create it with /api/lane/add first, then dispatch to it.
- Be brief. The reply lands in a small chat dock, not a terminal.

How this harness authenticates, because it is not how you would guess:
- Workers do NOT use ~/.claude/.credentials.json. That file is very likely
  stale and it does not matter. Reading it, or running `claude auth status`,
  tells you nothing about whether this harness can dispatch.
- The harness mints its own long-lived worker token via the console's
  "Connect Claude" button and stores it at {root}/.amp/.secrets.json under
  `claude_oauth_token`. It is injected into every worker subprocess - including
  the one running you right now.
- So: if you are answering at all, worker auth is working. To check properly,
  read `health.claude_auth` from `curl -s {base}/api/state` - null means fine,
  a string is the actual problem. It is the same reading the header dot shows.
- A `failed` task on a lane can be hours old. Check its timestamp before
  calling anything broken, and never tell the operator to run `claude login`
  or `claude setup-token` in a terminal - the console signs in by itself.

git, specifically:
- Commit when asked. Stage named paths, not `.` or `-A`, so nothing stray or
  secret rides along.
- {root}/.amp is harness state - board, keys, packets, rulings. Never commit it.
- Do not push, force-push, reset --hard, or delete branches unless the
  operator's message asks for it. Their message is the authorisation.
- Most subdirectories here are their own git repos. Commit inside the repo that
  owns the change; do not bump a gitlink in the parent."""


def orchestrator_brief(base_url: str) -> str:
    return ORCH_BRIEF.format(
        root=ROOT, base=base_url, landing=landing_rule(), ideas=idea_rule(),
        doctrine=mission_block() + doctrine_block("What we are held to"))


def orchestrator_ask(text: str, *, base_url: str, model: str | None = None,
                     budget: float | None = None, role: str = "you") -> dict:
    """One orchestrator turn: record it, run it, record the reply.

    Blocking. The console runs it on a thread so the feed can show it working.

    `role` is who is asking. It was hard-coded to `you`, which was true while the
    only way in was the operator typing - and stops being true the moment the
    harness asks something on its own. A feed that shows the harness's own
    prompts under the operator's name is a feed you cannot use to work out who
    decided what, which is most of what the thread is for. `harness` is
    rendered differently and is never attributed to them.
    """
    cfg = config()
    model = model or cfg.get("orchestrator_model", DEFAULT_ORCH_MODEL)
    budget = float(budget or cfg.get("orchestrator_budget_usd", DEFAULT_ORCH_BUDGET_USD))

    o = orchestrator()
    prior = o.get("session_id")
    turn_id = uuid.uuid4().hex[:12]
    orch_append({"id": f"u{turn_id}", "at": now(), "role": role, "text": text})
    orch_append({"id": turn_id, "at": now(), "role": "amp", "status": "running",
                 "text": "", "model": model})

    problem = claude_auth_problem()
    if problem:
        orch_update(turn_id, {"status": "failed", "text": problem, "finished_at": now()})
        return {"ok": False, "error": problem}

    out = claude_turn(
        text, ROOT,
        model=model, budget=budget,
        session_id=prior or str(uuid.uuid4()),
        task_id=f"orch:{turn_id}",
        resume=bool(prior),
        system=orchestrator_brief(base_url),
    )
    orch_update(turn_id, {
        "status": out.get("status"),
        "text": out.get("result") or out.get("error") or "",
        "cost_usd": out.get("cost_usd"),
        "num_turns": out.get("num_turns"),
        "session_id": out.get("session_id"),
        "finished_at": now(),
    })
    return {"ok": out.get("status") == "completed", "turn_id": turn_id,
            "error": out.get("error")}


def latest_claude_task(lane_name: str) -> dict | None:
    for rec in board().get("tasks", {}).get(lane_name, []):
        if rec.get("backend") == "claude":
            return rec
    return None


def resumable_session(lane_name: str, ident: str) -> str | None:
    """The session behind a named task, if it can still be resumed.

    Resuming only ever reached the newest task on a lane, which is exactly the
    wrong one: a worker killed by a limit is usually followed by others, so the
    session actually worth continuing is buried. This resolves any task id or
    session id on the lane - a prefix will do - and confirms the transcript is
    still on disk, because a session with no transcript cannot be resumed and
    saying so now is better than a worker failing to start.
    """
    ident = (ident or "").strip()
    if not ident:
        return None
    for rec in board().get("tasks", {}).get(lane_name, []):
        if rec.get("backend") != "claude":
            continue
        sid = rec.get("session_id") or rec.get("resume_of") or rec.get("task_id")
        if not sid:
            continue
        if ident in (rec.get("task_id"), sid) or sid.startswith(ident) \
                or (rec.get("task_id") or "").startswith(ident):
            return sid if transcript_path(sid) else None
    return None


def find_openrouter_key() -> str | None:
    key = os.environ.get("OPENROUTER_API_KEY")
    if key:
        return key.strip()
    key = load_json(SECRETS_PATH, {}).get("openrouter_api_key")
    return key.strip() if key else None


def openrouter_key() -> str:
    key = find_openrouter_key()
    if not key:
        die(
            "no OpenRouter key. Set OPENROUTER_API_KEY, or write "
            f"{SECRETS_PATH.name} as {{\"openrouter_api_key\": \"sk-or-...\"}} (untracked)."
        )
    return key


def openrouter_enabled() -> bool:
    """The switch in Settings. On unless it has been turned off."""
    return bool(config().get("openrouter_enabled", True))


def openrouter_available() -> bool:
    """Whether anything may spend on OpenRouter right now.

    Callers should ask this rather than `find_openrouter_key`, so that switching
    it off is the same kind of thing as never having had a key: the paths that
    already knew how to do without an architect keep working.
    """
    return openrouter_enabled() and bool(find_openrouter_key())


def architect_backend() -> str:
    """Who answers as the architect: your ChatGPT subscription, or the API.

    An unrecognised value falls back to the default rather than failing, because
    a typo in config.json should not take the architect away entirely.
    """
    b = config().get("architect_backend") or DEFAULT_ARCHITECT
    return b if b in ARCHITECT_BACKENDS else DEFAULT_ARCHITECT


def architect_available() -> bool:
    """Whether an architect round can run at all right now.

    Ask this rather than `openrouter_available`: which backend is wired up is
    settings, and the paths that skip a round when there is no architect should
    not have to know or care which one is missing.
    """
    # The role switch is checked first and for the same reason the light exists:
    # off means refused. If it only dimmed a dot while every escalation still
    # went out, the switch would be decoration.
    if not role_on("architect"):
        return False
    # Asked of the model, not of the backend name, so there is one answer to
    # "is Claude signed in" however many roles are pointed at it.
    ok, _ = model_ready(role_model("architect"))
    return ok


def architect_off_reason() -> str:
    """Why the architect is unavailable, in the words that say what to do.

    "no OpenRouter key configured" is wrong and unhelpful when the truth is that
    you turned it off on purpose two minutes ago, or that the architect is not
    on OpenRouter at all any more.
    """
    if not role_on("architect"):
        return "the architect is switched off in Settings"
    m = role_model("architect")
    _, why = model_ready(m)
    # The fallback is reachable only in a race - available when checked, asked
    # why a moment later. Say that. "the architect is unavailable" would be a
    # tautology with nothing to act on, which is the thing this function exists
    # to avoid.
    return why or f"the architect ({m}) was available a moment ago - try again"


# ------------------------------------------------------- the three role lights
#
# Note what is NOT here: a second copy of `claude_model`, `architect_backend` or
# `consult_model`. A role panel that kept its own idea of the worker model would
# drift from the one the dispatcher actually reads, and the header would then be
# lit for a model nobody is running. These read and write through to the setting
# that already owned each one.

def role_on(role: str) -> bool:
    """Whether this role may run at all. A role nobody has touched is on."""
    return bool((config().get("roles") or {}).get(role, {}).get("on", True))


def role_allows(role: str, model: str) -> str:
    """Why this role cannot be that model, or empty if it can."""
    if model == FOLLOW_ARCHITECT:
        return "" if role == "supervisor" else "only the supervisor can follow the architect"
    if model not in MODELS:
        return f"there is no model called {model!r}"
    return ROLE_CANNOT.get(role, {}).get(model_kind(model), "")


def role_choice(role: str) -> str:
    """What was chosen for this role, which is not always what it resolves to."""
    cfg = config()
    if role == "worker":
        m = cfg.get("claude_model") or DEFAULT_CLAUDE_MODEL
        return m if not role_allows("worker", m) else DEFAULT_CLAUDE_MODEL
    if role == "architect":
        b = architect_backend()
        if b == "codex":
            return "codex"
        if b == "claude":
            m = cfg.get("claude_architect_model") or DEFAULT_CLAUDE_MODEL
            return m if model_kind(m) == "claude" else DEFAULT_CLAUDE_MODEL
        m = cfg.get("consult_model")
        return m if m in CONSULT_MODELS else DEFAULT_CONSULT
    if role == "supervisor":
        m = (cfg.get("roles") or {}).get("supervisor", {}).get("model") or FOLLOW_ARCHITECT
        return m if not role_allows("supervisor", m) else FOLLOW_ARCHITECT
    raise ValueError(f"no such role: {role!r}")


def role_model(role: str) -> str:
    """Which model actually answers as this role.

    The supervisor's default is `architect`, meaning follow whatever the
    architect is. It resolves HERE rather than at the call site, so the header,
    the settings sheet and the code that makes the call cannot disagree about
    who is answering.
    """
    m = role_choice(role)
    return role_model("architect") if m == FOLLOW_ARCHITECT else m


def role_ready(role: str) -> tuple[bool, str]:
    """Can this role answer right now, and if not, what to do about it.

    The reason matters as much as the boolean: "switched off in Settings" and
    "ChatGPT is not connected" both produce a dark light and want completely
    different things from you.
    """
    if not role_on(role):
        return False, "switched off in Settings"
    return model_ready(role_model(role))


def model_ready(model: str) -> tuple[bool, str]:
    """Can this model answer at all right now, and if not, what to do about it.

    Keyed on the family and not on the role, because "is Claude signed in" has
    one answer however many jobs are pointed at it.
    """
    kind = model_kind(model)
    if kind == "claude":
        if not claude_available():
            return False, "Claude Code is not installed"
        problem = claude_auth_problem()
        return (False, problem) if problem else (True, "")
    if kind == "codex":
        if not codex_available():
            return False, "the codex CLI is not installed"
        if not codex_logged_in():
            return False, "ChatGPT is not connected"
        return True, ""
    if not openrouter_enabled():
        return False, "OpenRouter is switched off in Settings"
    if not find_openrouter_key():
        return False, "no OpenRouter key configured"
    return True, ""


def role_choices(role: str) -> list[dict]:
    """Every model, for every role, with the ones that cannot do the job marked.

    `cannot` and `why_not` are two different failures and are kept apart. A model
    that CANNOT hold the role is a fact about the job and never changes; one that
    could but is not signed in is a fact about today and is fixable. Collapsing
    them into one grey entry would tell you to go and fix something that no
    amount of signing in will fix.
    """
    names = ([FOLLOW_ARCHITECT] if role == "supervisor" else []) + list(MODELS)
    out = []
    for m in names:
        cannot = role_allows(role, m)
        resolved = role_model("architect") if m == FOLLOW_ARCHITECT else m
        ok, why = (False, "") if cannot else model_ready(resolved)
        out.append({
            "value": m,
            "note": (f"follows the architect, currently {resolved}"
                     if m == FOLLOW_ARCHITECT else MODELS[m]["note"]),
            "cannot": cannot,
            "why_not": "" if cannot or ok else why,
        })
    return out


def role_view() -> list[dict]:
    """The three lights, in the order they are read: who does the work, who is
    asked when it gets stuck, who checks it is the right work."""
    out = []
    for r in ROLES:
        ok, why = role_ready(r)
        out.append({
            "role": r,
            "on": role_on(r),
            "model": role_model(r),
            "choice": role_choice(r),
            "choices": role_choices(r),
            "ready": ok,
            "why": why,
        })
    return out


def set_role(role: str, on=None, model=None) -> list[dict]:
    """Change a role, writing through to whichever setting already owned it."""
    if role not in ROLES:
        raise ValueError(f"no such role: {role!r}")
    cfg = config()
    slot = cfg.setdefault("roles", {}).setdefault(role, {})
    if on is not None:
        slot["on"] = bool(on)
    if model is not None:
        # One gate, the same one the dropdown was built from. Re-listing the
        # allowed models here is how a menu and a validator drift apart until
        # the console offers you something the server then refuses.
        cannot = role_allows(role, model)
        if cannot:
            raise ValueError(f"the {role} cannot be {model}: {cannot}")
        if role == "worker":
            cfg["claude_model"] = model
        elif role == "architect":
            kind = model_kind(model)
            cfg["architect_backend"] = kind
            # Written to the setting that family already owned, so the value
            # survives switching away and back. Pinning the architect to codex
            # for an afternoon should not forget which OpenRouter tier you
            # were on.
            if kind == "openrouter":
                cfg["consult_model"] = model
            elif kind == "claude":
                cfg["claude_architect_model"] = model
        else:
            slot["model"] = model
    save_json(CONFIG_PATH, cfg)
    return role_view()


def role_chat(role: str, messages: list[dict], max_tokens: int = 8000) -> dict:
    """One round as a named role, through whichever model that role is set to.

    `architect_chat` dispatches on the one global backend, which is right for
    the architect and wrong for anything else: a supervisor pinned to codex
    while the architect sits on OpenRouter would silently go to OpenRouter and
    spend money the operator had moved away from.
    """
    m = role_model(role)
    kind = model_kind(m)
    if kind == "codex":
        return codex_chat(messages, DEFAULT_CONSULT, max_tokens)
    if kind == "claude":
        return claude_chat(messages, m, max_tokens)
    return openrouter_chat(messages, m, max_tokens)


def lane_backend(lane: dict) -> str:
    return lane.get("backend") or DEFAULT_BACKEND


def lane_or_die(cfg: dict, name: str) -> dict:
    lane = cfg["lanes"].get(name)
    if not lane:
        known = ", ".join(sorted(cfg["lanes"])) or "(none configured)"
        die(f"unknown lane {name!r}. Known lanes: {known}")
    return lane


class LaneError(ValueError):
    """A lane could not be created or changed. The message is meant for a person."""


# ---------------------------------------------------------------- lane modes
#
# How much of itself a lane is allowed to run unattended.
#
# The obvious design is a checkbox per lane, and it is wrong. Work starts in a
# lane from six independent places - a waiting proposal being adopted, a review
# adopting as a goal closes, the spec loop drafting, rating, or opening a
# campaign, and a campaign stepping - so an `on/off` flag would have to be
# re-tested at all six, and each site would decide for itself what off meant.
# The result is a switch that stops some work and not other work, with no way to
# predict which. That is worse than no switch: it reads as a halt and is not one.
#
# So a lane has one MODE, and the mode is consulted where work STARTS - never
# where work continues. Reading, reviewing, checking and reporting are never
# gated: a lane that stops reporting has gone dark, which is a different thing
# from being paused and much harder to notice.
#
# The modes are ordered. Each admits strictly less than the one above it:
#
#   build      everything. What every lane does today.
#   maintain   corrective work only - development is halted, fixes are not.
#   frozen     nothing starts. Findings still file, reviews still run.
#   archived   as frozen, and the lane stops being proposed into at all.
#
# What makes `maintain` implementable rather than a slogan is that the harness
# already records where every piece of work CAME FROM, so "corrective" does not
# need a label anybody types and cannot be gamed by whoever writes the proposal:
# it is read off the origin that was recorded when the work was created.
LANE_MODES = ("build", "maintain", "frozen", "archived")
DEFAULT_LANE_MODE = "build"

# A lane is doing one of two things, and the difference is where the work came
# from. `explore` looks for somewhere new to go; the spec loop builds documents
# that do not exist yet. Both are development. A settlement residue is what was
# left over after a claim we acted on turned out to be false, and a `solve`
# proposal comes from a gate that was raised against us - neither is a new
# direction, both are the stack repairing something it already got wrong.
WORK_KINDS = ("development", "corrective")
_SOURCE_KIND = {
    "explore": "development",
    "spec": "development",
    # What a finished goal's review proposes next. Development: a review asks
    # what should happen now, which is a new direction even when the goal it
    # reviewed was itself a repair. The follow-up repairs come back through
    # `settle` and `solve`, which are where a lane on maintain picks them up.
    "review": "development",
    "settle": "corrective",
    "settle-residue": "corrective",
    "solve": "corrective",
}
_MODE_ADMITS = {
    "build": ("development", "corrective"),
    "maintain": ("corrective",),
    "frozen": (),
    "archived": (),
}
MODE_MEANS = {
    "build": "everything runs",
    "maintain": "fixes run, new development does not",
    "frozen": "nothing starts; findings and reviews still run",
    "archived": "nothing starts, and nothing is proposed into it",
}


def lane_mode(lane_name: str) -> str:
    """What this lane is allowed to do. A lane nobody has set is building."""
    m = ((config().get("lanes") or {}).get(lane_name) or {}).get("mode")
    return m if m in LANE_MODES else DEFAULT_LANE_MODE


def work_kind(source: str | None) -> str:
    """Whether work from this origin is development or a repair.

    Defaults to `development`, which is the strict reading: an origin this does
    not recognise is one nobody has classified, and treating the unclassified as
    corrective would let any new source of work walk straight through a lane that
    was explicitly told to stop developing.
    """
    return _SOURCE_KIND.get(str(source or "").strip(), "development")


def lane_admits(lane_name: str, kind: str) -> str | None:
    """Why this lane may not start that kind of work unattended, or None.

    The single place a mode is enforced. Both callers - `proposal_hold` and
    `spec_auto_on` - answer with the string, so the reason a lane is quiet is
    the same sentence wherever it is read, and the operator never has to work
    out which of several switches is the one holding it.
    """
    mode = lane_mode(lane_name)
    if kind in _MODE_ADMITS.get(mode, ()):
        return None
    if mode == "maintain":
        return (f"{lane_name} is set to maintain, so it takes fixes but not new "
                f"development - this is {kind}")
    if mode == "archived":
        return f"{lane_name} is archived"
    if mode == "frozen":
        return f"{lane_name} is frozen, so nothing starts in it unattended"
    return None


def set_lane_mode(lane_name: str, mode: str) -> dict:
    """Change what a lane may do next. Never touches what it is doing now.

    Deliberately not a stop button. Work already running is a commitment that was
    already made, and killing a goal mid-flight leaves a worktree full of
    uncommitted work with nobody to review it - strictly worse than the
    development it was doing. So the mode takes effect at the next START, and the
    return says how much is still in flight so that is visible rather than
    discovered later as "I switched it off and it kept going".
    """
    mode = (mode or "").strip()
    if mode not in LANE_MODES:
        raise LaneError(f"mode must be one of {', '.join(LANE_MODES)}")
    cfg = config()
    if lane_name not in (cfg.get("lanes") or {}):
        raise LaneError(f"unknown lane {lane_name!r}")
    was = lane_mode(lane_name)
    cfg["lanes"][lane_name]["mode"] = mode
    save_json(CONFIG_PATH, cfg)
    running = [g for g in goals(lane_name) if g.get("state") == "running"]
    if was != mode:
        add_note(f"{lane_name} set to {mode} — {MODE_MEANS[mode]}"
                 + (f", and {len(running)} goal(s) already running will finish first"
                    if running and mode != "build" else ""))
    return {"lane": lane_name, "mode": mode, "was": was,
            "means": MODE_MEANS[mode],
            "in_flight": [{"id": g["id"], "objective": (g.get("objective") or "")[:120]}
                          for g in running]}


def lanes_view() -> dict:
    """Every lane and what it is currently allowed to do.

    `held` is the number that makes a mode honest. A mode is a claim about what
    will not happen, and a claim like that is unfalsifiable while nothing counts
    what it stopped: the operator sets a lane to maintain and then has to take it
    on trust for the rest of the day. This counts the proposals sitting in
    Direction that WOULD be adoptable and are held by this lane's mode and
    nothing else - so the switch reports its own effect, and a mode holding
    nothing is visibly holding nothing rather than looking the same as one doing
    the whole job.
    """
    cfg = config().get("lanes") or {}
    rungs = lane_rungs()
    props = [p for p in open_proposals() if p.get("kind") == "goal"]
    running = [g for g in goals() if g.get("state") == "running"]
    out = []
    for name in sorted(cfg):
        lane = cfg[name] or {}
        mode = lane_mode(name)
        mine = [p for p in props if p.get("lane") == name]
        # Held by the MODE specifically, not by a bar or a gate: re-asked with
        # the lane treated as building, so a proposal that would be waiting on
        # its odds anyway is not counted as something the mode is stopping.
        held = [p for p in mine
                if lane_admits(name, work_kind(p.get("source")))
                and not _hold_but_for_mode(p)]
        out.append({
            "name": name, "mode": mode, "means": MODE_MEANS[mode],
            "path": lane.get("path"), "repo": lane.get("repo"),
            "branch": lane.get("branch"), "backend": lane_backend(lane),
            "rung": rungs.get(name),
            "auto_spec": bool(spec_auto(name).get("on")),
            # Reported separately from `auto_spec` because they are two different
            # facts: the switch is on, and the mode is refusing it anyway. One
            # combined boolean would make the tab say the loop is off when what
            # is true is that you left it on and something else is holding it.
            "auto_spec_held": bool(spec_auto(name).get("on"))
                              and lane_admits(name, "development") is not None,
            "running": [{"id": g["id"], "objective": (g.get("objective") or "")[:120]}
                        for g in running if g.get("lane") == name],
            "proposals": len(mine),
            "held": len(held),
        })
    return {"lanes": out, "modes": list(LANE_MODES), "means": MODE_MEANS,
            "default": DEFAULT_LANE_MODE}


def _hold_but_for_mode(p: dict) -> str | None:
    """Why this proposal would wait even if its lane were building."""
    c, need = p.get("confidence"), p.get("need")
    if c is None or need is None:
        return "not scored"
    if c < adopt_bar() or need < need_bar():
        return "under a bar"
    return "a gate is up" if escalations(p.get("lane")) else None


# A lane name becomes a git branch (`amp/<name>`) and a directory under
# .amp/worktrees/, so it has to survive being both. Rejecting a bad one here is
# the difference between "that name has a slash in it" and a git error at
# dispatch time, half an hour later, that reads like a harness bug.
LANE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,31}$")


def add_lane(name: str, *, repo: str | None = None, path: str | None = None,
             branch: str = "main", backend: str = DEFAULT_BACKEND,
             env_id: str | None = None, replace: bool = False) -> dict:
    """Register a directory as a lane, checking everything dispatch will need.

    Shared by `./amp lane add` and the console, so a lane made from the chat is
    the same object as one made from the terminal. Every check here is one that
    would otherwise fail later, inside a worker, where the operator cannot see
    it. Raises LaneError, which both callers render their own way.
    """
    name = (name or "").strip()
    if not LANE_NAME_RE.match(name):
        raise LaneError(
            f"{name!r} is not a usable lane name. It becomes a git branch and a "
            "directory, so: lowercase letters, digits, dot, dash or underscore, "
            "starting with a letter or digit, up to 32 characters."
        )
    if backend not in BACKENDS:
        raise LaneError(f"backend must be one of {', '.join(BACKENDS)}")

    cfg = config()
    if name in cfg["lanes"] and not replace:
        old = cfg["lanes"][name]
        raise LaneError(
            f"lane {name!r} already exists ({old.get('repo')} at {old.get('path')}). "
            "Pass replace to repoint it - and note its existing worktree at "
            f"{WORKTREE_DIR / name} is reused as-is, so delete that first if the "
            "repo is changing."
        )

    rel = (path or name).strip().strip("/")
    abs_path = (ROOT / rel).resolve()
    if not abs_path.is_dir():
        raise LaneError(f"no such directory: {abs_path}")
    try:
        rel = str(abs_path.relative_to(ROOT))
    except ValueError:
        raise LaneError(f"{abs_path} is outside the workspace {ROOT}") from None

    # Worktrees are cut from this directory, so it has to be a repository root.
    # A subdirectory would silently cut them from the *parent* repo instead, and
    # the worker would look right while editing the wrong tree.
    p = run(["git", "rev-parse", "--show-toplevel"], cwd=abs_path)
    if p.returncode != 0:
        raise LaneError(f"{abs_path} is not a git repository - a lane needs one "
                        "to cut a worker's worktree from")
    top = Path(p.stdout.strip()).resolve()
    if top != abs_path:
        raise LaneError(f"{abs_path} sits inside the repo at {top}. Point the lane "
                        "at the repository root, or workers would edit that one.")

    if not repo:
        p = run(["git", "remote", "get-url", "origin"], cwd=abs_path)
        url = p.stdout.strip()
        if p.returncode != 0 or not url:
            raise LaneError(f"{abs_path} has no `origin` remote, so the repo name "
                            "cannot be inferred - pass repo as owner/name")
        if url.startswith("git@github.com:"):
            repo = url.split(":", 1)[1].removesuffix(".git")
        elif "github.com/" in url:
            repo = url.split("github.com/", 1)[1].removesuffix(".git")
        else:
            raise LaneError(f"could not read a repo name out of origin {url!r} - "
                            "pass repo as owner/name")

    # The base a worker branches from. Checking it now turns a dispatch-time
    # worktree failure into a sentence about a typo.
    branch = (branch or "main").strip() or "main"
    if run(["git", "rev-parse", "--verify", "--quiet", branch], cwd=abs_path).returncode != 0:
        raise LaneError(f"{repo} has no branch {branch!r} to branch workers off")

    # The mode survives a repoint. `replace` is for pointing a lane at a
    # different checkout, and it rebuilds the record from scratch - which would
    # silently put a lane you had frozen back to building, at the one moment you
    # are least likely to look. Whether a lane may run is not a fact about where
    # its code lives.
    keep = lane_mode(name) if name in (cfg.get("lanes") or {}) else DEFAULT_LANE_MODE
    cfg["lanes"][name] = {"repo": repo, "path": rel, "branch": branch,
                          "backend": backend, "env_id": env_id, "mode": keep}
    save_json(CONFIG_PATH, cfg)
    lane = dict(cfg["lanes"][name], name=name)
    # A codex lane without an env id is configured but cannot dispatch, and the
    # id only exists in an interactive TUI - so say so rather than let the first
    # dispatch discover it.
    lane["needs_env"] = backend == "codex" and not env_id
    return lane


# ---------------------------------------------------------------- claude bridge


def claude_available() -> bool:
    return shutil.which("claude") is not None


CLAUDE_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"


def claude_token() -> str | None:
    """The harness's own long-lived worker token, if one has been connected."""
    tok = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if tok:
        return tok.strip()
    tok = load_json(SECRETS_PATH, {}).get("claude_oauth_token")
    return tok.strip() if tok else None


def claude_env() -> dict:
    """Environment for a worker subprocess.

# Every workspace-scoped store that can hold something about ONE lane, and how
# to find that lane's records in it. Written out rather than discovered, because
# a store nobody listed is a store whose contents are silently left behind - and
# the whole point of the report below is that nothing is left behind silently.
_LANE_STORES = (
    ("findings", "FINDINGS_PATH", "findings"),
    ("ideas", "IDEAS_PATH", "ideas"),
    ("obligations", "OBLIGATIONS_PATH", "obligations"),
    ("direction proposals", "DIRECTION_PATH", "proposals"),
    ("reports", "REPORT_PATH", "reports"),
)


def _lane_leftovers(name: str) -> dict:
    """How many records about this lane each store still holds.

    Counted, not moved. Moving them would mean understanding five schemas well
    enough to rewrite them, and getting one wrong loses a finding - which is the
    record of something we believed and acted on. Counting them is honest and
    cheap, and it turns "some history stayed behind" into a number the operator
    can decide about.
    """
    out = {}
    for label, gname, key in _LANE_STORES:
        path = globals().get(gname)
        if not path:
            continue
        blob = load_json(path, {})
        rows = blob.get(key) if isinstance(blob, dict) else None
        if not isinstance(rows, list):
            continue
        n = sum(1 for r in rows if isinstance(r, dict) and r.get("lane") == name)
        if n:
            out[label] = n
    return out


def move_lane(name: str, to_slug: str, *, dry_run: bool = False,
              from_slug: str | None = None) -> dict:
    """Move one lane, and the work recorded against it, into another workspace.

    A workspace owns its lanes, its goals and its worktrees, so re-registering
    the lane in the target and deleting it here would be a data loss dressed as
    a reorganisation: the goals stay in the old workspace's `goals/`, and the
    only place that says what this lane was asked to do stops being reachable
    from the lane. So the goals move with it.

    The worktree moves too, by `git worktree move`, so that uncommitted work in
    a lane's tree survives the reorganisation. If git refuses - it does when the
    tree is dirty in ways it cannot replay, or locked - the whole move is
    refused rather than half-done. A lane whose config lives in one workspace
    and whose tree is registered under another is a lane that will fail at
    dispatch time, inside a worker, where nobody can see it.

    What does NOT move is counted and reported. See `_lane_leftovers`.

    `from_slug` is the workspace the caller BELIEVES this lane is in, and the
    move is refused if that is not true. It reads like belt and braces until you
    notice that `current` is one field in one shared file with no owner: two
    consoles against this state directory - which is a normal Wednesday here -
    fight over it, and the loser is a process that thinks it is in `core` while
    the registry says `products`. Found live, by watching a second console on
    another port walk every workspace out from under this one. Without this
    check, "move the lane I am looking at" is a bet on nobody else having moved
    the ground since the dry run.
    """
    reg = workspaces()
    frm = reg["current"]
    if from_slug and from_slug != frm:
        raise WorkspaceError(
            f"this asked to move {name!r} out of {from_slug!r}, but the harness is "
            f"in {frm!r} now - something else moved it. Look again before moving "
            "anything.")
    if to_slug not in reg["workspaces"]:
        raise WorkspaceError(f"there is no workspace called {to_slug!r}")
    if to_slug == frm:
        raise WorkspaceError(f"lane {name!r} is already in {frm!r}")

    cfg = config()
    lane = (cfg.get("lanes") or {}).get(name)
    if lane is None:
        raise WorkspaceError(f"there is no lane called {name!r} in {frm!r}")

    # A worker mid-turn is writing into paths that are about to stop existing.
    # This is the same refusal `switch_blocked` makes, narrowed to one lane.
    if name in live_worker_lanes():
        raise WorkspaceError(f"a worker is running in {name!r} - stop it or wait for it")

    dest = workspace_dir(to_slug, reg)
    dest_cfg_path = dest / "config.json"
    dest_cfg = load_json(dest_cfg_path, {"lanes": {}, "consult_model": DEFAULT_CONSULT})
    if name in (dest_cfg.get("lanes") or {}):
        raise WorkspaceError(f"{to_slug!r} already has a lane called {name!r}")

    mine = [g for g in (load_goal(s["id"]) or {} for s in goals()) if g.get("lane") == name]
    wt = WORKTREE_DIR / name
    dest_wt = dest / "worktrees" / name
    plan = {"ok": True, "lane": name, "from": frm, "to": to_slug,
            "goals": len(mine), "worktree": str(wt) if wt.exists() else None,
            "left_behind": _lane_leftovers(name), "moved": False}
    if dry_run:
        return plan

    # The worktree first. It is the only step that can fail for a reason outside
    # this file, and doing it first means a failure leaves everything else where
    # it was - rather than a config already rewritten to point at a tree that
    # never arrived.
    if wt.exists():
        dest_wt.parent.mkdir(parents=True, exist_ok=True)
        repo = (ROOT / lane["path"]).resolve()
        p = run(["git", "worktree", "move", str(wt), str(dest_wt)], cwd=repo)
        if p.returncode != 0:
            raise WorkspaceError(
                f"git would not move {name}'s worktree, so nothing was moved:\n"
                + (p.stderr or p.stdout).strip()[:400])

    # Write the copy before deleting the original, and go through `save_json` so
    # the SQLite mirror holds the goal at its new path. A goal moved with
    # `write_text` would arrive on disk and be invisible to `store`, so the
    # history of a moved goal would end at the move.
    (dest / "goals").mkdir(parents=True, exist_ok=True)
    for g in mine:
        save_json(dest / "goals" / f"{g['id']}.json", g)
        goal_path(g["id"]).unlink(missing_ok=True)

    dest_cfg.setdefault("lanes", {})[name] = lane
    save_json(dest_cfg_path, dest_cfg)
    cfg["lanes"].pop(name, None)
    save_json(CONFIG_PATH, cfg)
    plan["moved"] = True
    return plan


    Workers must not depend on whatever credentials the launching shell happens
    to carry - the console has none. Connecting Claude once stores a long-lived
    token here and every worker gets it explicitly.
    """
    env = dict(os.environ)
    tok = claude_token()
    if tok:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    return env


def claude_auth_problem() -> str | None:
    """Why a background worker would fail to authenticate, or None if it can.

    `claude auth status` is useless here: it answers loggedIn:true with a stored
    OAuth token the API rejects with a 401, and the rejection takes ~3 minutes of
    retries to surface. The stored expiry is the honest signal, and it is free.
    """
    if claude_token() or os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        oauth = json.loads(CLAUDE_CREDENTIALS.read_text()).get("claudeAiOauth") or {}
    except (OSError, json.JSONDecodeError):
        return "Claude is not connected - click Connect Claude"
    exp = oauth.get("expiresAt")
    if not isinstance(exp, (int, float)):
        return "Claude credentials carry no expiry - click Connect Claude"
    stale = time.time() - exp / 1000
    if stale > 0:
        return (f"Claude sign-in expired {stale / 86400:.0f} days ago "
                f"- click Connect Claude")
    return None


# ------------------------------------------------------- connecting the worker

TOKEN_RE = re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")
# OpenAI keys and any JWT, for the codex pty. The device flow is not supposed to
# print either - the CLI writes its own credential file - but "not supposed to"
# is the assumption that leaked a token here once already.
OAI_SECRET_RE = re.compile(
    r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"
    r"|\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"
)
AUTH_URL_RE = re.compile(r"https://\S*oauth/authorize\S*")
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b[>=\]][0-9;]*[a-zA-Z]?|\r")
# The TUI writes runs of spaces as cursor-forward moves. Deleting those glues
# words together ("makesure the fullcde"), so replace them with real spaces.
CUF_RE = re.compile(r"\x1b\[([0-9]*)C")


def strip_ansi(s: str) -> str:
    s = CUF_RE.sub(lambda m: " " * max(1, int(m.group(1) or 1)), s)
    return ANSI_RE.sub("", s)


def redact(s: str) -> str:
    """Never surface the pty buffer raw - the token is printed in it.

    Diagnostics get shown in the console, written to logs and pasted into GPT-5.6
    packets, so a token that reaches an error string reaches all three. Both
    vendors' shapes are covered because both sign-ins now run through here, and
    a redactor that only knows one vendor is a redactor that fails silently the
    first time the other one prints something.
    """
    s = TOKEN_RE.sub("sk-ant-<redacted>", s)
    return OAI_SECRET_RE.sub("<redacted>", s)


def last_message(buf: str, n: int = 2) -> str:
    """The last few meaningful lines of TUI output, for an error message."""
    lines = [ln.strip() for ln in redact(buf).splitlines() if ln.strip()]
    return "\n".join(lines[-n:])


def pty_read(fd: int, buf: str, deadline: float, done, idle: float = 0.5) -> str:
    """Read a TUI's pty into `buf` until `done(buf)` or the deadline.

    Neither sign-in closes its stdout while it is waiting, so there is no EOF to
    read to: the caller says what it is waiting for and how long it will wait.
    """
    while time.time() < deadline:
        r, _, _ = select.select([fd], [], [], idle)
        if not r:
            continue
        try:
            chunk = os.read(fd, 65536)
        except OSError:      # the child exited and the pty went away
            break
        if not chunk:
            break
        buf += strip_ansi(chunk.decode("utf8", "replace"))
        if done(buf):
            break
    return buf


def pty_close(pid: int | None, fd: int | None):
    """Stop a sign-in child and drop its pty, tolerating either already gone."""
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, os.WNOHANG)
        except OSError:
            pass
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass


class ClaudeLogin:
    """Drives `claude setup-token` so sign-in can happen in the console.

    `setup-token` is a TUI: it paints an authorize URL, waits for the user to
    paste the code back, then prints a long-lived token. Two details make it
    scriptable - it needs a pty (it will not talk to a pipe), and the pty must
    be very wide or it hard-wraps the URL into unusable fragments.
    """

    WIDTH = 400
    IDLE = 0.5

    def __init__(self):
        self.pid: int | None = None
        self.fd: int | None = None
        self.buf = ""
        self.url: str | None = None
        self.token: str | None = None

    def _drain(self, deadline: float, done) -> str:
        self.buf = pty_read(self.fd, self.buf, deadline, done, self.IDLE)
        return self.buf

    def start(self, timeout: float = 45.0) -> str:
        """Launch the flow and return the URL the user must open."""
        if not claude_available():
            raise RuntimeError("claude CLI not found (npm i -g @anthropic-ai/claude-code)")
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            env = dict(os.environ)
            # A stale ambient credential would make setup-token skip the flow.
            for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL",
                      "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
                env.pop(k, None)
            # Point the CLI's own auto-open at Chrome; without this it follows
            # BROWSER/xdg and lands in firefox on this box.
            chrome = browser_cmd()
            if chrome:
                env["BROWSER"] = chrome
            os.execvpe("claude", ["claude", "setup-token"], env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, self.WIDTH, 0, 0))
        self._drain(time.time() + timeout, AUTH_URL_RE.search)
        m = AUTH_URL_RE.search(self.buf)
        if not m:
            self.close()
            raise RuntimeError("claude setup-token did not offer a sign-in URL:\n"
                               + last_message(self.buf, 4))
        self.url = m.group(0)
        return self.url

    # A rejected code makes the TUI re-draw its prompt instead of exiting, so
    # waiting for the token alone would burn the whole timeout on a typo.
    REPROMPT = re.compile(r"Paste code here|Invalid|invalid|failed|error", re.I)

    def submit(self, code: str, timeout: float = 60.0) -> str:
        """Hand the pasted code back and return the long-lived token."""
        if self.fd is None:
            raise RuntimeError("sign-in was not started")
        self.buf = ""
        os.write(self.fd, code.strip().encode() + b"\r")
        self._drain(time.time() + timeout,
                    lambda b: TOKEN_RE.search(b) or self.REPROMPT.search(b))
        m = TOKEN_RE.search(self.buf)
        if not m:
            self.close()
            raise RuntimeError(self._reason() or
                               "that code was not accepted - try Connect Claude again")
        self.token = m.group(0)
        self.close()
        return self.token

    def _reason(self) -> str | None:
        """The CLI's own complaint, preferred over the trailing 'Press Enter' noise."""
        for ln in redact(self.buf).splitlines():
            s = " ".join(ln.split())
            if s.lower().startswith("oauth error") or "invalid code" in s.lower():
                return s
        return None

    def close(self):
        pty_close(self.pid, self.fd)
        self.pid, self.fd = None, None


def store_claude_token(token: str):
    """Untracked, same file as the OpenRouter key."""
    s = load_json(SECRETS_PATH, {})
    s["claude_oauth_token"] = token.strip()
    save_json(SECRETS_PATH, s)
    SECRETS_PATH.chmod(0o600)


class CodexLogin:
    """Drives `codex login --device-auth` so ChatGPT sign-in can happen here.

    Deliberately not the same shape as ClaudeLogin, because the CLI is not: this
    flow prints a URL *and* a one-time code, the operator types the code into the
    page, and the CLI polls OpenAI itself and exits once it is approved. So the
    second call is a poll, not a submission - there is nothing to paste back, and
    an endpoint that asked for a code would be asking for the wrong thing.

    Whether it worked is decided by `codex login status`, not by the text in the
    buffer. The buffer says what the CLI claims; the status says what is on disk.
    """

    WIDTH = 200
    IDLE = 0.5
    TTL = 15 * 60          # the CLI states the code expires in 15 minutes

    URL_RE = re.compile(r"https://\S*openai\.com/\S*device\S*")
    # The pairing code, e.g. ABCD-EFGHI. Shown to the operator on purpose: it is
    # useless to anyone who does not already hold their browser session, and it
    # is never stored anywhere.
    CODE_RE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,8}\b")

    def __init__(self):
        self.pid: int | None = None
        self.fd: int | None = None
        self.buf = ""
        self.url: str | None = None
        self.code: str | None = None
        self.started = 0.0

    def start(self, timeout: float = 45.0) -> dict:
        """Launch the flow and return the URL and code the operator needs."""
        if not codex_available():
            raise RuntimeError("codex CLI not found (npm i -g @openai/codex)")
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            env = dict(os.environ)
            # An ambient key would let the CLI answer from the environment
            # instead of running the flow that was actually asked for.
            for k in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
                env.pop(k, None)
            # Chrome, for the same reason as the Claude flow: BROWSER/xdg lands
            # in firefox on this box.
            chrome = browser_cmd()
            if chrome:
                env["BROWSER"] = chrome
            os.execvpe("codex", ["codex", "login", "--device-auth"], env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, self.WIDTH, 0, 0))
        self.buf = pty_read(
            self.fd, self.buf, time.time() + timeout,
            lambda b: self.URL_RE.search(b) and self.CODE_RE.search(b), self.IDLE,
        )
        url, code = self.URL_RE.search(self.buf), self.CODE_RE.search(self.buf)
        if not (url and code):
            self.close()
            raise RuntimeError("codex login did not offer a device code:\n"
                               + last_message(self.buf, 4))
        self.url, self.code = url.group(0), code.group(0)
        self.started = time.time()
        return {"url": self.url, "code": self.code}

    def poll(self) -> dict:
        """Where the flow has got to: pending, connected, or failed and why."""
        if self.fd is None:
            return {"state": "failed", "error": "sign-in is not running"}
        # Keep reading, so a complaint printed while we were not looking is in
        # the buffer by the time it is needed for the error message.
        self.buf = pty_read(self.fd, self.buf, time.time() + 0.2, lambda b: False, 0.1)
        try:
            done, _ = os.waitpid(self.pid, os.WNOHANG)
        except (OSError, TypeError):
            done = -1
        if done == 0:
            if time.time() - self.started > self.TTL:
                self.close()
                return {"state": "failed", "error": "that code expired - start again"}
            return {"state": "pending", "url": self.url, "code": self.code}
        self.pid = None          # reaped already; close() must not wait on it
        self.close()
        # `fresh`, because the sign-in we are reporting on finished a moment ago
        # and the cached answer is from before it. Telling someone who has just
        # signed in that they are not signed in is the whole failure mode a
        # cache like that introduces.
        if codex_logged_in(fresh=True):
            return {"state": "connected"}
        return {"state": "failed",
                "error": self._reason() or "sign-in did not complete - try again"}

    def _reason(self) -> str | None:
        """The CLI's own complaint, preferred over an exit code nobody can read."""
        for ln in reversed(redact(self.buf).splitlines()):
            s = " ".join(ln.split())
            if re.match(r"(error|failed)\b", s, re.I) or \
               re.search(r"\b(expired|denied|timed out|rejected)\b", s, re.I):
                return s
        return None

    def close(self):
        pty_close(self.pid, self.fd)
        self.pid, self.fd = None, None


# What each deploy provider's sign-in actually is, as a command. Both of these
# are the same shape as the ChatGPT flow and NOT the Claude one: they print a
# URL, the operator approves it in a browser, and the CLI polls the provider
# itself and exits. There is nothing to paste back.
LOGIN_CMD = {
    "fly": ["flyctl", "auth", "login"],
    "cloudflare": ["wrangler", "login"],
}


class ProviderLogin:
    """Drives a deploy provider's browser sign-in from the console.

    One class for both providers rather than one each, because unlike the two
    model CLIs they genuinely are the same flow, and the thing that decides
    whether it worked is the same for both: the provider's own `whoami`, run
    fresh afterwards. The buffer says what the CLI claims. `whoami` says what a
    deploy would actually find, which is the only claim worth putting on screen
    - the failure this avoids is telling the operator they are signed in and
    then having nine services fail to deploy.

    Why this is worth building at all, stated where it can be checked: ten
    deployable services in this workspace, ten of them stranded, and the mission
    is to move lanes onto `live_deployed`. The architect said so itself from two
    independent call paths across eleven lanes - "the remaining uncertainties
    require operator-controlled deployed machines, credentials, spending, or
    acceptance decisions" - while sharpening spent four rounds per proposal
    being told the wording was not the problem.
    """

    WIDTH = 400            # both wrap the authorize URL into fragments if narrow
    IDLE = 0.5
    TTL = 10 * 60

    URL_RE = re.compile(r"https://\S+")

    def __init__(self, provider: str):
        self.provider = provider
        self.pid: int | None = None
        self.fd: int | None = None
        self.buf = ""
        self.url: str | None = None
        self.started = 0.0

    def start(self, timeout: float = 60.0) -> dict:
        """Launch the flow and return the URL the operator has to approve."""
        cmd = LOGIN_CMD.get(self.provider)
        if not cmd:
            raise RuntimeError(f"nothing here knows how to sign in to {self.provider!r}")
        if not shutil.which(cmd[0]):
            raise RuntimeError(f"{cmd[0]} is not installed, so there is nothing to sign in to")
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            env = dict(os.environ)
            # An ambient token would let the CLI answer from the environment
            # rather than running the flow that was asked for, and the operator
            # would be signed in as whoever that token belongs to.
            for k in ("FLY_API_TOKEN", "FLY_ACCESS_TOKEN",
                      "CLOUDFLARE_API_TOKEN", "CF_API_TOKEN"):
                env.pop(k, None)
            chrome = browser_cmd()
            if chrome:
                env["BROWSER"] = chrome
            os.execvpe(cmd[0], cmd, env)
        fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", 50, self.WIDTH, 0, 0))
        # `flyctl` upgrades itself on first run and prints a progress bar for
        # thirty seconds before it says anything useful, which is why the wait
        # here is generous and why the caller is told this can be slow.
        self.buf = pty_read(self.fd, self.buf, time.time() + timeout,
                            self.URL_RE.search, self.IDLE)
        m = self.URL_RE.search(self.buf)
        if not m:
            self.close()
            raise RuntimeError(f"{cmd[0]} did not offer a sign-in URL:\n"
                               + last_message(self.buf, 4))
        self.url = m.group(0).rstrip(".,")
        self.started = time.time()
        return {"url": self.url}

    def poll(self) -> dict:
        """Where the flow has got to: pending, connected, or failed and why."""
        if self.fd is None:
            return {"state": "failed", "error": "sign-in is not running"}
        # Keep draining, so a complaint printed while nobody was looking is in
        # the buffer by the time the error message needs it.
        self.buf = pty_read(self.fd, self.buf, time.time() + 0.2, lambda b: False, 0.1)
        try:
            done, _ = os.waitpid(self.pid, os.WNOHANG)
        except (OSError, TypeError):
            done = -1
        if done == 0:
            # Asked even while the child is still up. `wrangler login` leaves a
            # server running after it has written the credential, so waiting for
            # the process to exit would report a completed sign-in as pending
            # for as long as the operator was willing to watch it.
            if _whoami(self.provider).get("ok"):
                self.close()
                return {"state": "connected"}
            if time.time() - self.started > self.TTL:
                self.close()
                return {"state": "failed", "error": "that sign-in expired - start again"}
            return {"state": "pending", "url": self.url}
        self.pid = None          # reaped already; close() must not wait on it
        self.close()
        if _whoami(self.provider).get("ok"):
            return {"state": "connected"}
        return {"state": "failed",
                "error": self._reason() or "sign-in did not complete - try again"}

    def _reason(self) -> str | None:
        """The CLI's own complaint, preferred over an exit code nobody can read."""
        for ln in reversed(redact(self.buf).splitlines()):
            s = " ".join(ln.split())
            if re.match(r"(error|failed)\b", s, re.I) or \
               re.search(r"\b(expired|denied|timed out|rejected)\b", s, re.I):
                return s
        return None

    def close(self):
        pty_close(self.pid, self.fd)
        self.pid, self.fd = None, None


# ---------------------------------------------------------------- supervision


class Killed(Exception):
    """A worker was stopped: by you, by the clock, or by the memory watchdog."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


_PAGE = os.sysconf("SC_PAGE_SIZE")
_CLK_TCK = os.sysconf("SC_CLK_TCK")

_PROCS: dict[str, subprocess.Popen] = {}
_STARTED: dict[str, float] = {}
_CANCELLED: set[str] = set()
# What each watchdog last saw. The watchdogs already tick every two seconds, so
# they do the sampling and the console reads their answer - two samplers taking
# CPU deltas off the same processes would each see a fraction of the truth.
_ACTIVITY: dict[str, dict] = {}
_PROCS_LOCK = threading.Lock()


def limits() -> dict:
    """How much of this machine a single worker is allowed to take."""
    cfg = config()
    return {
        "timeout_s": int(cfg.get("worker_timeout_s", DEFAULT_WORKER_TIMEOUT_S)),
        "mem_gb": float(cfg.get("worker_mem_gb", DEFAULT_WORKER_MEM_GB)),
        "nice": int(cfg.get("worker_nice", DEFAULT_WORKER_NICE)),
        "stall_s": int(cfg.get("worker_stall_s", DEFAULT_WORKER_STALL_S)),
        "max_workers": int(cfg.get("max_workers", DEFAULT_MAX_WORKERS)),
        "permission_mode": str(cfg.get("permission_mode", DEFAULT_PERMISSION_MODE)),
    }


def live_workers() -> int:
    """Workers this harness has running right now, across every lane.

    The orchestrator is not one of them. It is the control surface, so counting
    it means the board can only ever fill `cap - 1` slots, and it takes the last
    one itself at the exact moment it is trying to hand out work.

    Adopted workers ARE counted. They survived a console restart, they are still
    burning a budget and still on the machine, and a cap that cannot see them is
    not a cap - it would wave through a full board on top of them.
    """
    with _PROCS_LOCK:
        tids = set(_PROCS) | set(_ADOPTED)
        return sum(1 for tid in tids if not tid.startswith("orch:"))


def live_worker_lanes() -> list[str]:
    """Which lanes are holding a live worker right now.

    `live_workers` answers how many, which is what a cap needs. A refusal needs
    to say which, because a count is not something anyone can act on.
    """
    with _PROCS_LOCK:
        tids = {t for t in set(_PROCS) | set(_ADOPTED) if not t.startswith("orch:")}
    if not tids:
        return []
    return sorted(
        lane for lane, recs in (board().get("tasks") or {}).items()
        if any(r.get("task_id") in tids for r in recs)
    )


def _proc_table() -> dict[int, dict]:
    """One pass over /proc: ppid, pgrp, resident bytes and CPU ticks per pid."""
    table = {}
    try:
        entries = list(os.scandir("/proc"))
    except OSError:
        return table
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            stat = Path(entry.path, "stat").read_text()
            # comm is parenthesised and may contain spaces, so fields are read
            # from after the last ')': [0]=state, [1]=ppid, [2]=pgrp,
            # [11]=utime, [12]=stime, [21]=rss.
            f = stat[stat.rindex(")") + 2:].split()
            table[int(entry.name)] = {
                "ppid": int(f[1]), "pgrp": int(f[2]),
                "rss": int(f[21]) * _PAGE,
                "ticks": int(f[11]) + int(f[12]),
            }
        except (OSError, ValueError, IndexError):
            continue
    return table


def worker_procs(root: int, table: dict[int, dict] | None = None) -> set[int]:
    """Every pid belonging to a worker: its descendants, and their groups.

    Not just its process group. `supervise` starts each worker with
    `start_new_session=True` and the comment used to say that made a kill reach
    the whole tree - but Claude Code's Bash tool detaches every shell it runs
    into a session of its own, so a worker's compiler, test run and benchmark
    all sit in process groups this one knows nothing about. Weighing only the
    group meant weighing `claude` and its MCP servers: a worker burning two
    whole cores on `ic32 --test` reported 1% CPU, which is how the stall
    watchdog came to kill a busy worker and write "no output and no CPU" on the
    board about a machine that was pinned.

    So the tree is walked by parentage, and then widened to whole process groups
    so that a child which has already outlived its shell still counts. What this
    still cannot see is a group that left the tree entirely before the sample -
    every member reparented to init with no survivor to name it. Killing by
    group rather than by pid is what keeps that from happening in the first
    place.
    """
    table = _proc_table() if table is None else table
    kids: dict[int, list[int]] = {}
    for pid, p in table.items():
        kids.setdefault(p["ppid"], []).append(pid)

    seen, stack = set(), [root]
    while stack:
        pid = stack.pop()
        if pid in seen or pid not in table:
            continue
        seen.add(pid)
        stack.extend(kids.get(pid, ()))

    groups = {table[pid]["pgrp"] for pid in seen} | {root}
    return seen | {pid for pid, p in table.items() if p["pgrp"] in groups}


def group_stat(root: int) -> dict:
    """Resident bytes, CPU seconds and process count across a worker's tree.

    None of it is ever about `claude` itself - the memory and the CPU are in the
    compiler, the benchmark, or the MCP servers it spawned, so everything it
    started is what gets weighed. CPU is here because it is the only cheap
    reading that distinguishes a worker doing slow work from a worker doing none.
    """
    table = _proc_table()
    pids = worker_procs(root, table)
    rss = sum(table[p]["rss"] for p in pids if p in table)
    ticks = sum(table[p]["ticks"] for p in pids if p in table)
    return {"rss": rss, "cpu_s": ticks / _CLK_TCK, "procs": len(pids)}


def group_rss(root: int) -> int:
    """Resident bytes across everything the worker is running."""
    return group_stat(root)["rss"]


def _alive(pgid: int) -> bool:
    return Path(f"/proc/{pgid}").exists()


def worker_pgids(root: int) -> list[int]:
    """Every process group the worker has anything running in, its own last.

    One `killpg` on the worker's own group is not enough and never was: the
    shells Claude Code runs sit in sessions of their own, so a benchmark or a
    compile survives the kill, keeps a core, and is still there long after the
    board says the task is over.

    Its own group sorts last so a sweep cannot kill the worker first and orphan
    the rest of the tree it was about to walk.
    """
    table = _proc_table()
    groups = {table[p]["pgrp"] for p in worker_procs(root, table) if p in table}
    groups.add(root)
    return sorted(groups, key=lambda g: (g == root, g))


def _signal_groups(groups: list[int], sig: int) -> int:
    sent = 0
    for pgid in groups:
        try:
            os.killpg(pgid, sig)
            sent += 1
        except (ProcessLookupError, PermissionError):
            continue
    return sent


def _kill_pgid(root: int, wait):
    """Take down a worker and everything it spawned, politely then not.

    The groups are read once, before anything is signalled - both because
    signalling as you walk tears down the parentage the walk is reading, and
    because by the time the worker has exited there is nothing left to walk
    from, and its detached shells would go unfound at exactly the moment they
    became orphans.
    """
    groups = worker_pgids(root)
    if not _signal_groups(groups, signal.SIGTERM):
        return
    wait()
    _signal_groups(groups, signal.SIGKILL)


def _kill_group(proc: subprocess.Popen):
    def wait():
        try:
            proc.wait(timeout=10)
            return True
        except subprocess.TimeoutExpired:
            return False
    _kill_pgid(proc.pid, wait)


def _kill_adopted(pgid: int):
    """An adopted worker is not ours to wait() on, so poll /proc instead."""
    def wait():
        for _ in range(20):
            if not _alive(pgid):
                return True
            time.sleep(0.5)
        return False
    _kill_pgid(pgid, wait)


def cancel_worker(task_id: str) -> bool:
    """Stop a running worker on demand. False if it is not running here."""
    with _PROCS_LOCK:
        proc = _PROCS.get(task_id)
        pgid = _ADOPTED.get(task_id)
        if proc or pgid:
            _CANCELLED.add(task_id)
    if proc:
        _kill_group(proc)
        return True
    if pgid:
        _kill_adopted(pgid)
        return True
    return False


# A worker's transcript is the only place it says what it is doing, and it
# writes it as it goes - so a live session can simply be read. These map the
# tool it last reached for onto a word for the kind of work that is.
_PHASES = {
    "Bash": "running", "Edit": "editing", "Write": "writing", "NotebookEdit": "editing",
    "Read": "reading", "Grep": "searching", "Glob": "searching",
    "WebFetch": "fetching", "WebSearch": "searching", "Task": "delegating",
    "TodoWrite": "planning", "AskUserQuestion": "waiting on an answer",
}
# Commands that mean the worker is doing the slow thing on purpose. A worker
# inside one of these is expected to go quiet for a long time.
_LONG_RUNNING = ("bench", "test", "gcc", "cc ", "clang", "make", "cargo", "mix ",
                 "npm ", "pytest", "python", "cmake", "go build", "zig ")


def _tail_records(path: Path, nbytes: int = 64_000) -> list[dict]:
    """Parsed records from the end of a transcript, without reading the file.

    These grow into the hundreds of megabytes, and the console asks every few
    seconds. Only the tail can answer "what is it doing right now" anyway.
    """
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > nbytes:
            fh.seek(size - nbytes)
            buf = fh.read().split(b"\n", 1)[-1]   # the seek lands mid-line
        else:
            buf = fh.read()
    out = []
    for line in buf.decode("utf-8", "replace").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def worker_activity(session_id: str) -> dict:
    """What a running worker is doing, read from the transcript it is writing."""
    path = transcript_path(session_id)
    if not path:
        # Claude Code writes the file on its first turn, so this is startup -
        # or, much later, a worker that never got far enough to say anything.
        return {"phase": "starting", "doing": "", "quiet_s": 0, "turns": 0}
    quiet_s = int(max(0.0, time.time() - path.stat().st_mtime))
    events = [e for rec in _tail_records(path) for e in record_events(rec)]
    turns = sum(1 for e in events if e["kind"] == "tool")
    # Only what the worker itself said. The user side of a transcript carries
    # tool notifications and system reminders, which read as the worker
    # narrating machinery it never chose to run.
    last = next((e for e in reversed(events)
                 if e["kind"] == "tool"
                 or (e["kind"] == "text" and e.get("role") == "assistant")), None)
    if not last:
        return {"phase": "thinking", "doing": "", "quiet_s": quiet_s, "turns": turns}
    if last["kind"] == "text":
        return {"phase": "thinking", "doing": " ".join(last["text"].split())[:120],
                "quiet_s": quiet_s, "turns": turns}
    text = last.get("text") or ""
    phase = _PHASES.get(last.get("tool"), last.get("tool") or "working")
    long_running = (last.get("tool") == "Bash"
                    and any(k in text.lower() for k in _LONG_RUNNING))
    return {"phase": phase, "doing": text[:160], "tool": last.get("tool"),
            "quiet_s": quiet_s, "turns": turns, "long_running": long_running}


def worker_stats() -> dict[str, dict]:
    """What each live worker is costing the machine, and what it is doing."""
    with _PROCS_LOCK:
        tids = list(_PROCS) + [t for t in _ADOPTED if t not in _PROCS]
        seen = {t: dict(_ACTIVITY.get(t) or {}) for t in tids}
    lim = limits()
    out = {}
    for tid in tids:
        s = seen.get(tid) or {}
        sid = s.get("session_id") or tid
        act = worker_activity(sid)
        # The watchdog's own reading of quiet time is what it kills on, but it
        # only samples every two seconds; the transcript mtime is exact.
        out[tid] = {
            "rss_gb": round(s.get("rss", 0) / 1e9, 2),
            "cpu_pct": round(s.get("cpu_pct", 0.0), 1),
            "procs": s.get("procs", 0),
            "elapsed_s": int(time.time() - _STARTED.get(tid, time.time())),
            "mem_gb": lim["mem_gb"],
            "timeout_s": lim["timeout_s"],
            "stall_s": lim["stall_s"],
            "adopted": tid in _ADOPTED,
            "stalled": bool(s.get("cold_s", 0) >= lim["stall_s"]),
            **act,
        }
    return out


def _sample(task_id: str, pgid: int, session_id: str, prev: dict, dt: float) -> dict:
    """One watchdog tick: how hot the group is, and how long it has been mute.

    `cold_s` is the reading that matters - time during which the worker wrote
    nothing AND burned no CPU. It resets the moment either one moves, so a long
    compile (silent, hot) and a long think (brief, then loud) both keep it at
    zero. Only a worker that is doing nothing at all lets it climb.
    """
    g = group_stat(pgid)
    cpu_pct = max(0.0, (g["cpu_s"] - prev.get("cpu_s", g["cpu_s"])) / dt * 100.0) if dt else 0.0
    path = transcript_path(session_id)
    mtime = path.stat().st_mtime if path and path.exists() else 0.0
    wrote = mtime > prev.get("mtime", 0.0)
    cold = 0.0 if (wrote or cpu_pct >= IDLE_CPU_PCT) else prev.get("cold_s", 0.0) + dt
    return {"rss": g["rss"], "cpu_s": g["cpu_s"], "cpu_pct": cpu_pct,
            "procs": g["procs"], "mtime": max(mtime, prev.get("mtime", 0.0)),
            "cold_s": cold, "session_id": session_id}


def supervise(argv: list[str], cwd: Path, env: dict, *, task_id: str,
              timeout_s: int, mem_gb: float, nice_by: int,
              session_id: str = "", stall_s: int = 0) -> subprocess.CompletedProcess:
    """Run a worker so that it cannot take the machine down with it.

    Its own session, lowered priority (so a runaway build never starves the
    desktop), a wall clock, an RSS watchdog, and a stall watchdog. Raises Killed
    if any of those stopped it.

    The session is not what makes a kill reach the whole tree - it used to say
    so here and it was wrong. Claude Code detaches every shell it runs into a
    session of its own, so both the weighing and the killing walk the process
    tree instead; see `worker_procs`.
    """
    # `nice` rather than a preexec_fn: this runs inside a threaded server, where
    # forking with a Python callback can deadlock.
    nicer = shutil.which("nice")
    cmd = [nicer, "-n", str(nice_by), *argv] if nicer and nice_by else list(argv)

    proc = subprocess.Popen(
        cmd, cwd=str(cwd), env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    with _PROCS_LOCK:
        _PROCS[task_id] = proc
        _STARTED[task_id] = time.time()
        _CANCELLED.discard(task_id)

    mem_bytes = int(mem_gb * 1e9)
    stop, breach = threading.Event(), []

    def watch():
        deadline = time.monotonic() + timeout_s
        sid = session_id or task_id
        seen, last = {}, time.monotonic()
        while not stop.wait(2.0):
            tick = time.monotonic()
            seen = _sample(task_id, proc.pid, sid, seen, tick - last)
            last = tick
            with _PROCS_LOCK:
                _ACTIVITY[task_id] = seen
            if tick > deadline:
                span = f"{timeout_s // 60} min" if timeout_s >= 60 else f"{timeout_s}s"
                breach.append(f"stopped after {span} - over its time limit")
            elif mem_bytes and seen["rss"] > mem_bytes:
                breach.append(f"stopped - it and its children held more than {mem_gb:g} GB")
            elif stall_s and seen["cold_s"] >= stall_s:
                # Not a timeout: it had time left and was using none of it.
                breach.append(f"stopped - hung for {int(seen['cold_s']) // 60} min "
                              "with no output and no CPU")
            else:
                continue
            _kill_group(proc)
            return

    threading.Thread(target=watch, daemon=True).start()
    try:
        out, err = proc.communicate()
    finally:
        stop.set()
        with _PROCS_LOCK:
            _PROCS.pop(task_id, None)
            _STARTED.pop(task_id, None)
            _ACTIVITY.pop(task_id, None)
            cancelled = task_id in _CANCELLED
            _CANCELLED.discard(task_id)

    if cancelled:
        raise Killed("cancelled")
    if breach:
        raise Killed(breach[0])
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


# ---------------------------------------------------------------- adoption
#
# Workers run in their own session so that killing one reaches its whole tree.
# The cost of that is they also survive the console: restart it and every live
# worker reparents to init, keeping its budget and losing its watchdog, while
# its board record says `running` until someone notices. That is not a rare
# case - the console gets restarted for every change to this file. So on boot
# the board is reconciled against /proc: what is still alive gets picked back
# up, and what is not gets settled.

_ADOPTED: dict[str, int] = {}

# An adopted worker finishing frees a slot exactly like any other, and the queue
# that wants that slot lives in the console. amp.py cannot import server.py, so
# the console hands its drain in here. Without it, a queue could sit full behind
# workers that were picked up rather than started.
ON_SETTLE = None   # set by the console to its queue drain

# An adopted worker is one this process did not start, so nothing here ran the
# after-the-worker step: reporting back to the architect that sent it, or
# escalating if it came back blocked. Without this hook, a thread that sent a
# worker for evidence waits for a report that already happened and never moves
# again. The console sets it to the same settle path a worker it started uses.
ON_WORKER_DONE = None   # set by the console; called (lane_name, record)


def adopted_task_ids() -> set[str]:
    with _PROCS_LOCK:
        return set(_ADOPTED)


def _settled(lane_name: str | None = None, rec: dict | None = None):
    if ON_WORKER_DONE and lane_name and rec:
        try:
            ON_WORKER_DONE(lane_name, rec)
        except Exception as e:
            print(f"amp: worker-done hook failed: {e}", file=sys.stderr)
    if ON_SETTLE:
        try:
            ON_SETTLE()
        except Exception as e:
            print(f"amp: settle hook failed: {e}", file=sys.stderr)


def _pgid_for_session(session_id: str) -> int | None:
    """The live worker running this session, by the id in its own argv.

    Matching on argv rather than a recorded pid because a pid recorded before a
    restart may since have been reused by something else entirely.

    Both spellings, because a worker carries its session id under `--session-id`
    when it is new and under `--resume` when it is continued, and looking only
    for the first one made every resumed worker invisible here. What that looked
    like on the board was not an error: the record was settled "the worker did
    not survive it", the lane's slot was freed, and the queue dispatched a fresh
    worker onto the same worktree - while the one that had supposedly not
    survived was still running in it. Two workers in one working tree is the one
    thing the lane lock exists to prevent, and this was the way around it.
    """
    needles = [f"{flag}\0{session_id}".encode()
               for flag in ("--session-id", "--resume")]
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            argv = Path(entry.path, "cmdline").read_bytes()
        except OSError:
            continue
        if any(n in argv for n in needles) and b"claude" in argv.split(b"\0", 1)[0]:
            return int(entry.name)
    return None


def _settle_adopted(lane_name: str, rec: dict, reason: str | None):
    """Close out a worker whose exit we watched but whose result we never got.

    An adopted worker's stdout went to a pipe that died with the old console,
    so the JSON summary is gone for good. What it said last is still in the
    transcript, and that is reported as what it is - no cost, no turn count,
    marked `adopted` so nothing downstream reads it as a clean result.
    """
    sid = rec.get("resume_of") or rec["task_id"]
    events = transcript(sid)
    last = next((e["text"] for e in reversed(events)
                 if e["kind"] == "text" and e.get("role") == "assistant"), "")
    patch = {"finished_at": now(), "adopted": True, "session_id": sid}
    if reason:
        patch.update(status="failed", error=reason, killed=reason)
    elif last:
        patch.update(status="completed", result=last,
                     note="picked up after a console restart - cost unknown")
    else:
        patch.update(status="failed",
                     error="the console restarted while this ran, and it left nothing behind")
    update_task(lane_name, rec["task_id"], patch)
    _settled(lane_name, rec)


def _watch_adopted(lane_name: str, rec: dict, pgid: int):
    """The watchdog an adopted worker lost, reattached."""
    task_id, sid = rec["task_id"], rec.get("resume_of") or rec["task_id"]
    lim = limits()
    mem_bytes = int(lim["mem_gb"] * 1e9)
    started = _STARTED.get(task_id, time.time())
    # How long it has been mute is a recorded fact - the transcript's mtime -
    # so an adopted worker does not get a fresh fifteen minutes of silence just
    # because the console was restarted. How busy it has been is not recorded
    # anywhere, so the first tick is a warm-up: it establishes a CPU baseline,
    # and a worker that turns out to be mid-build clears the seeded time on it.
    path = transcript_path(sid)
    quiet = time.time() - path.stat().st_mtime if path and path.exists() else 0.0
    seen = {"mtime": path.stat().st_mtime if path and path.exists() else 0.0,
            "cold_s": max(0.0, min(quiet, time.time() - started))}
    last, reason, warm = time.monotonic(), None, 0
    while True:
        time.sleep(2.0)
        if not _alive(pgid):
            break
        tick = time.monotonic()
        seen = _sample(task_id, pgid, sid, seen, tick - last)
        last = tick
        warm += 1
        with _PROCS_LOCK:
            _ACTIVITY[task_id] = seen
            if task_id in _CANCELLED:
                reason = "cancelled"
                break
        if warm < 2:
            continue
        if time.time() - started > lim["timeout_s"]:
            reason = f"stopped after {lim['timeout_s'] // 60} min - over its time limit"
        elif mem_bytes and seen["rss"] > mem_bytes:
            reason = f"stopped - it and its children held more than {lim['mem_gb']:g} GB"
        elif lim["stall_s"] and seen["cold_s"] >= lim["stall_s"]:
            reason = (f"stopped - hung for {int(seen['cold_s']) // 60} min "
                      "with no output and no CPU")
        if reason:
            _kill_adopted(pgid)
            break
    with _PROCS_LOCK:
        _ADOPTED.pop(task_id, None)
        _STARTED.pop(task_id, None)
        _ACTIVITY.pop(task_id, None)
        cancelled = task_id in _CANCELLED
        _CANCELLED.discard(task_id)
    _settle_adopted(lane_name, rec, "cancelled" if cancelled else reason)


def adopt_orphans() -> list[dict]:
    """Reconcile every `running` board record against what is actually alive."""
    out = []
    for lane_name, tasks in (board().get("tasks") or {}).items():
        for rec in tasks:
            if rec.get("status") != "running" or rec.get("backend") != "claude":
                continue
            task_id = rec["task_id"]
            with _PROCS_LOCK:
                if task_id in _PROCS or task_id in _ADOPTED:
                    continue
            pgid = _pgid_for_session(rec.get("resume_of") or task_id)
            if not pgid:
                _settle_adopted(lane_name, rec,
                                "the console restarted while this was running "
                                "and the worker did not survive it")
                out.append({"lane": lane_name, "task_id": task_id, "adopted": False})
                continue
            with _PROCS_LOCK:
                _ADOPTED[task_id] = pgid
                # Its real start is on the board; the wall clock has been
                # running the whole time the console was not watching.
                _STARTED[task_id] = _epoch(rec.get("dispatched_at")) or time.time()
            threading.Thread(target=_watch_adopted, args=(lane_name, rec, pgid),
                             daemon=True).start()
            out.append({"lane": lane_name, "task_id": task_id, "adopted": True, "pid": pgid})
    return out


def _epoch(iso: str | None) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return 0.0


def claude_worktree(lane_name: str, lane: dict, branch: str) -> Path:
    """The isolated tree a lane's claude worker edits.

    Workers never touch the shared checkout: interactive sessions run against it
    constantly, and a background writer there would corrupt live work.
    """
    wt = WORKTREE_DIR / lane_name
    if wt.exists():
        return wt
    repo = (ROOT / lane["path"]).resolve()
    wt.parent.mkdir(parents=True, exist_ok=True)
    p = run(["git", "worktree", "add", "-B", f"amp/{lane_name}", str(wt), branch], cwd=repo)
    if p.returncode != 0:
        die(f"could not create a worktree for {lane_name}:\n{(p.stderr or p.stdout).strip()}")
    return wt


def claude_turn(prompt: str, cwd: Path, *, model: str, budget: float, session_id: str,
                task_id: str, resume: bool, system: str | None = None) -> dict:
    """One headless claude turn, supervised.

    Resuming is what Codex Cloud cannot do: it answers a worker that stopped to
    ask, in the session it stopped in.
    """
    lim = limits()
    argv = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--model", model,
        "--max-budget-usd", f"{budget:g}",
        "--permission-mode", lim["permission_mode"],
        "--resume" if resume else "--session-id", session_id,
    ]
    # Appended rather than sent as a first message: it has to hold on every
    # resumed turn too, and a resumed turn sends only what you just typed.
    if system:
        argv += ["--append-system-prompt", system]
    try:
        p = supervise(argv, cwd, claude_env(), task_id=task_id,
                      timeout_s=lim["timeout_s"], mem_gb=lim["mem_gb"], nice_by=lim["nice"],
                      session_id=session_id, stall_s=lim["stall_s"])
    except Killed as k:
        return {"status": "cancelled" if k.reason == "cancelled" else "failed",
                "session_id": session_id, "error": k.reason, "killed": k.reason}
    try:
        d = json.loads(p.stdout)
    except json.JSONDecodeError:
        err = (p.stderr or p.stdout).strip()[:4000]
        return {"status": "failed", "error": err or f"claude exited {p.returncode}"}
    failed = bool(d.get("is_error")) or p.returncode != 0
    status = "failed" if failed else "completed"
    error = d.get("result") if failed else None
    killed = None
    budget_hit = False
    # A worker that ran out of money did not finish, whatever the exit code says.
    # The CLI reports a budget stop as a clean exit, so calling it "completed" is
    # how work gets quietly dropped: the record then looks exactly like a worker
    # that finished and reported, which is the one thing it is not.
    #
    # The tell is an EMPTY RESULT. A worker that genuinely finishes says what it
    # did; one that is cut off says nothing, because saying it was going to be
    # the next thing it did. This deliberately does not test `stop_reason`: the
    # first version of this check only caught `tool_use`, and on the real board
    # that missed a worker which burned $1.04 of a $1.00 cap over 33 turns and
    # reported nothing under `end_turn`. Eleven of thirteen real tasks stopped on
    # their cap and all of them were filed as successes.
    spent, cap = float(d.get("total_cost_usd") or 0), float(budget or 0)
    if not failed and not (d.get("result") or "").strip():
        on_cap = bool(cap) and spent >= cap * 0.9
        killed = (f"stopped after {d.get('num_turns') or '?'} turns with nothing "
                  "reported" + (f" - it spent ${spent:.2f} of its ${cap:.2f} cap"
                                if on_cap else ""))
        status, error = "failed", killed
    elif not failed and cap and spent >= cap * 0.9:
        # It did report, but it was on the cap when it did, so what it says may
        # be a summary of an unfinished job rather than a finished one. Not a
        # failure - it spoke - but the board should not present it as a clean
        # finish either, and `budget_hit` is what the console reads to say so.
        budget_hit = True
    return {
        "status": status,
        "session_id": d.get("session_id") or session_id,
        "result": d.get("result"),
        "cost_usd": d.get("total_cost_usd"),
        "num_turns": d.get("num_turns"),
        "duration_ms": d.get("duration_ms"),
        "stop_reason": d.get("stop_reason"),
        "error": error,
        "killed": killed,
        "budget_hit": budget_hit,
    }


def start_claude_task(
    lane_name: str,
    lane: dict,
    prompt: str,
    *,
    branch: str,
    model: str,
    budget: float,
    isolate: bool = True,
    resume_of: str | None = None,
) -> dict:
    """Put a `running` record on the board and return it. Does not execute."""
    if not claude_available():
        die("claude CLI not found (npm i -g @anthropic-ai/claude-code)")
    cwd = claude_worktree(lane_name, lane, branch) if isolate else (ROOT / lane["path"])
    rec = {
        "task_id": str(uuid.uuid4()),
        "backend": "claude",
        "status": "running",
        "dispatched_at": now(),
        "prompt": prompt,
        "branch": branch,
        "model": model,
        "budget_usd": budget,
        "cwd": str(cwd),
        "isolated": isolate,
        "resume_of": resume_of,
    }
    record_task(lane_name, rec)
    return rec


def run_claude_task(lane_name: str, rec: dict) -> dict:
    """Execute a started record and settle it on the board."""
    problem = claude_auth_problem()
    if problem:
        # Without this the worker spends ~3 minutes retrying into a 401.
        patch = {"status": "failed", "error": problem, "finished_at": now()}
        update_task(lane_name, rec["task_id"], patch)
        return {**rec, **patch}
    out = claude_turn(
        rec["prompt"],
        Path(rec["cwd"]),
        model=rec["model"],
        budget=rec["budget_usd"],
        session_id=rec.get("resume_of") or rec["task_id"],
        task_id=rec["task_id"],
        resume=bool(rec.get("resume_of")),
    )
    patch = {k: out.get(k) for k in
             ("status", "session_id", "result", "cost_usd", "num_turns",
              "stop_reason", "error", "killed", "budget_hit")}
    patch["finished_at"] = now()
    update_task(lane_name, rec["task_id"], patch)
    return {**rec, **patch}


TRANSCRIPT_ROOT = Path.home() / ".claude" / "projects"

# A tool result can be a whole file. The console wants the shape of the turn,
# not the payload, so results are clipped here rather than shipped and hidden.
RESULT_CLIP = 1600


def transcript_path(session_id: str) -> Path | None:
    """Claude Code writes one JSONL per session, under a mangled-cwd directory.

    Globbing rather than re-deriving the mangling: the session id is a uuid, so
    it is unique across every project directory, and the mangling is theirs to
    change.
    """
    if not session_id:
        return None
    hits = sorted(TRANSCRIPT_ROOT.glob(f"*/{session_id}.jsonl"))
    return hits[0] if hits else None


def _tool_headline(name: str, inp: dict) -> str:
    """One line naming what a tool call actually touched."""
    if not isinstance(inp, dict):
        return name
    for key in ("file_path", "path", "pattern", "command", "url", "prompt"):
        val = inp.get(key)
        if isinstance(val, str) and val.strip():
            return " ".join(val.split())[:200]
    return name


def _clip(text: str, limit: int) -> tuple[str, bool]:
    return (text[:limit], True) if len(text) > limit else (text, False)


def record_events(rec: dict) -> list[dict]:
    """The renderable turns inside one transcript record.

    Bookkeeping records (queue-operation, last-prompt, summaries) carry no
    conversation and yield nothing.
    """
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return []
    role, at = msg.get("role"), rec.get("timestamp")
    content = msg.get("content")
    if isinstance(content, str):
        return [{"role": role, "kind": "text", "text": content, "at": at}]
    if not isinstance(content, list):
        return []
    events = []
    for block in content:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in ("text", "thinking"):
            body = block.get("text") or block.get("thinking") or ""
            if body.strip():
                events.append({"role": role, "kind": kind, "text": body, "at": at})
        elif kind == "tool_use":
            name = block.get("name") or "tool"
            events.append({"role": role, "kind": "tool", "tool": name, "at": at,
                           "text": _tool_headline(name, block.get("input") or {})})
        elif kind == "tool_result":
            body = block.get("content")
            if isinstance(body, list):
                body = "\n".join(b.get("text", "") for b in body if isinstance(b, dict))
            body, clipped = _clip(str(body or ""), RESULT_CLIP)
            events.append({"role": role, "kind": "result", "text": body, "at": at,
                           "clipped": clipped, "error": bool(block.get("is_error"))})
    return events


def transcript(session_id: str) -> list[dict]:
    """A worker session as ordered, renderable turns."""
    path = transcript_path(session_id)
    if not path:
        return []
    events = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        events.extend(record_events(rec))
    return events


def transcript_digest(session_id: str, turns: int = 40, clip: int = 300) -> str:
    """The tail of a worker session, one line per turn."""
    events = transcript(session_id)
    if not events:
        return ""
    lines = []
    for e in events[-turns:]:
        who = e.get("tool") if e["kind"] == "tool" else e["kind"]
        lines.append(f"[{who}] " + " ".join((e.get("text") or "").split())[:clip])
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------- prior sessions
#
# Every chat you have ever had with Claude Code is already on this disk, and most
# of the work this harness is meant to supervise happened in them. There are
# thousands of files and hundreds of megabytes of them, so nothing here reads a
# whole session: each one is judged from its head and its tail.

HEAD_BYTES = 96_000
TAIL_BYTES = 256_000


def _head_tail(path: Path) -> tuple[list[dict], list[dict]]:
    """Parsed records from the start and the end of a transcript."""
    size = path.stat().st_size
    with path.open("rb") as fh:
        head = fh.read(min(size, HEAD_BYTES))
        if size <= HEAD_BYTES:
            tail = b""                        # the head is the whole file
        else:
            fh.seek(max(HEAD_BYTES, size - TAIL_BYTES))
            # The seek lands mid-line; that first fragment is not JSON.
            tail = fh.read().split(b"\n", 1)[-1]

    def parse(buf: bytes) -> list[dict]:
        out = []
        for line in buf.decode("utf-8", "replace").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out

    recs = parse(head)
    return recs, (parse(tail) or recs)


# What a session says about itself when it is in trouble.
TROUBLE = {
    "usage limit": "hit a Claude usage limit",
    "rate limit": "was rate limited",
    "context low": "ran low on context",
    "prompt is too long": "overflowed its context window",
    "BLOCKED:": "reported itself blocked",
    "ESCALATE:": "asked to escalate",
    "MemoryError": "ran out of memory",
    "No space left": "filled the disk",
}


def scan_session(path: Path) -> dict:
    """One prior chat, summarised from its two ends."""
    head, tail = _head_tail(path)
    # One enormous opening record can fill the whole head, leaving no turn that
    # carries a cwd. The tail always has one, and a session with no directory is
    # dropped as "not this workspace" - so the fallback decides whether a chat is
    # visible at all, not just how it is labelled.
    cwd = (next((r["cwd"] for r in head if r.get("cwd")), "")
           or next((r["cwd"] for r in reversed(tail) if r.get("cwd")), ""))
    first = next((e["text"] for r in head for e in record_events(r)
                  if e["kind"] == "text" and e["role"] == "user"), "")
    tail_events = [e for r in tail for e in record_events(r)]
    last = tail_events[-1] if tail_events else {}
    return {
        "id": path.stem,
        "cwd": cwd,
        "project": Path(cwd).name if cwd else path.parent.name,
        "branch": next((r["gitBranch"] for r in reversed(tail) if r.get("gitBranch")), ""),
        "started_at": next((r["timestamp"] for r in head if r.get("timestamp")), ""),
        "last_at": next((r["timestamp"] for r in reversed(tail) if r.get("timestamp")), ""),
        "size": path.stat().st_size,
        "first_prompt": " ".join(first.split())[:400],
        "last_kind": last.get("kind", ""),
        "last_text": " ".join((last.get("text") or "").split())[:400],
        "tail_events": tail_events,
    }


# Tools that change nothing by themselves: doing one of these over and over is
# the shape of a worker that is stuck.
PROBE_TOOLS = {"Bash", "Read", "Grep", "Glob", "WebFetch"}


def diagnose_session(s: dict) -> list[str]:
    """What looks wrong with a prior chat, from its tail alone."""
    events = s.get("tail_events") or []
    found = []
    # A chat that is still going has nothing to diagnose: its tail is still being
    # written, so every signal here is about a turn that has not finished yet.
    if _active_recently(s.get("last_at")):
        return found
    # Trouble surfaces either in what the assistant says or in a tool call that
    # failed. A *successful* read is excluded on purpose: a session that merely
    # read a file naming these markers is not a session in trouble, and reading
    # this very file is enough to trip all eight of them at once.
    blob = "\n".join(e.get("text") or "" for e in events[-80:]
                     if e.get("role") == "assistant" or e.get("error"))
    for marker, said in TROUBLE.items():
        if marker in blob:
            found.append(said)

    errors = sum(1 for e in events[-60:] if e.get("error"))
    if errors >= 5:
        found.append(f"{errors} failing tool calls in its last turns")

    # The same command over and over is a worker going in circles. Editing one
    # file repeatedly is not that - it is how editing a file works - so only the
    # tools that make no change on their own are counted.
    calls = [e["text"] for e in events[-40:]
             if e["kind"] == "tool" and e.get("tool") in PROBE_TOOLS]
    repeat = max((calls.count(c) for c in set(calls)), default=0)
    if repeat >= 5:
        found.append(f"ran the same command {repeat} times - it was looping")

    # A session whose last word is the assistant reaching for a tool never got an
    # answer back: it was interrupted, not finished.
    if s.get("last_kind") in ("tool", "thinking"):
        found.append("stopped mid-turn - it was interrupted rather than finished")
    return found


def _active_recently(iso: str | None, minutes: int = 15) -> bool:
    try:
        last = datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - last).total_seconds() < minutes * 60


def prior_sessions(limit: int = 40, under: Path | None = ROOT,
                   min_size: int = 4000) -> list[dict]:
    """Recent Claude Code chats, newest first, with what looks wrong in each.

    Scoped to this workspace by default: chats about someone else's directory are
    not what this harness is here to supervise.
    """
    paths = sorted(TRANSCRIPT_ROOT.glob("*/*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    live = set(_PROCS)
    out = []
    for p in paths:
        if len(out) >= limit:
            break
        try:
            if p.stat().st_size < min_size:
                continue                      # a chat with nothing in it
            s = scan_session(p)
        except OSError:
            continue
        if under and not str(Path(s["cwd"] or "/")).startswith(str(under)):
            continue
        s["symptoms"] = diagnose_session(s)
        s["mine"] = s["id"] in live           # a worker this harness is running
        s.pop("tail_events", None)
        out.append(s)
    return out


def session_events(session_id: str, limit: int = 400) -> list[dict]:
    """The readable tail of any session, this harness's or not.

    A transcript can be a hundred megabytes; only its end is ever read, because
    only its end says where the work got to.
    """
    path = transcript_path(session_id)
    if not path:
        return []
    head, tail = _head_tail(path)
    events = [e for r in (tail or head) for e in record_events(r)]
    return events[-limit:]


def session_brief(session_id: str) -> str:
    """A prior chat written up for an architect: what it was for, and where it got to."""
    path = transcript_path(session_id)
    if not path:
        return ""
    s = scan_session(path)
    events = s.pop("tail_events", [])
    symptoms = diagnose_session({**s, "tail_events": events})
    lines = [f"# Prior Claude session {s['id'][:8]}\n",
             f"- directory: {s['cwd'] or 'unknown'}\n",
             f"- branch: {s['branch'] or 'unknown'}\n",
             f"- ran: {s['started_at']} → {s['last_at']}\n",
             f"- transcript size: {s['size'] // 1000} KB\n"]
    if symptoms:
        lines.append("\n## What looks wrong\n\n" +
                     "".join(f"- {x}\n" for x in symptoms))
    lines.append(f"\n## What it was asked to do\n\n{s['first_prompt']}\n")
    tail = "\n".join(
        f"[{e.get('tool') if e['kind'] == 'tool' else e['kind']}] "
        + " ".join((e.get("text") or "").split())[:300]
        for e in events[-40:])
    lines.append("\n## Where it got to (last turns)\n\n```\n" + tail + "\n```\n")
    return "".join(lines)


# A worker can write a large generated file. Its content still belongs in the
# diff - a name alone tells you nothing about whether the work is right.
UNTRACKED_CLIP = 20000


def claude_diff(lane_name: str, lane: dict) -> str:
    """What the worker changed in its worktree, vs the lane's base branch."""
    wt = WORKTREE_DIR / lane_name
    if not wt.exists():
        return ""
    base = lane.get("branch") or "main"
    if run(["git", "rev-parse", "--verify", "--quiet", base], cwd=wt).returncode != 0:
        # Otherwise `git diff base...HEAD` fails and the caller reads the empty
        # string as "the worker changed nothing".
        return (f"# cannot diff: this lane's base branch {base!r} does not exist in the repo.\n"
                f"# Set the right one on the lane (master? develop?) and dispatch again.")

    parts = []
    committed = run(["git", "diff", f"{base}...HEAD"], cwd=wt).stdout
    if committed:
        stat = run(["git", "diff", "--stat", f"{base}...HEAD"], cwd=wt).stdout
        parts.append(f"# committed on amp/{lane_name} (vs {base})\n{stat}\n{committed}")
    working = run(["git", "diff", "HEAD"], cwd=wt).stdout
    if working:
        parts.append(f"# uncommitted in the worktree\n{working}")

    untracked = run(["git", "ls-files", "--others", "--exclude-standard"], cwd=wt).stdout.split()
    for rel in untracked:
        parts.append(f"# new file: {rel}\n" + _new_file_diff(wt / rel))
    return "\n\n".join(parts)


def _new_file_diff(path: Path) -> str:
    """An untracked file rendered as an all-additions patch."""
    try:
        text = path.read_text()
    except (OSError, UnicodeDecodeError):
        size = path.stat().st_size if path.exists() else 0
        return f"# (binary or unreadable, {size} bytes)"
    body, clipped = _clip(text, UNTRACKED_CLIP)
    out = "\n".join("+" + line for line in body.splitlines())
    return out + ("\n+ … clipped" if clipped else "")


# ---------------------------------------------------------------- codex bridge


def codex_available() -> bool:
    return shutil.which("codex") is not None


# Asking the CLI whether it is signed in costs a process spawn, and the console
# now asks it several times a second: every role's dropdown lists every model,
# and the poll that draws the header rebuilds all three. Measured at 5 spawns
# and 0.49s per poll before this cache. Sign-in state changes about once a
# month, so a few seconds of staleness buys back all of it - and `fresh=True`
# is there for the one caller that has just changed it and must not be told
# what was true a moment ago.
_CODEX_LOGIN_CACHE = {"at": -1e9, "ok": False}
_CODEX_LOGIN_TTL_S = 15.0


def codex_logged_in(fresh: bool = False) -> bool:
    if not codex_available():
        return False
    now = time.monotonic()
    if not fresh and now - _CODEX_LOGIN_CACHE["at"] < _CODEX_LOGIN_TTL_S:
        return _CODEX_LOGIN_CACHE["ok"]
    p = run(["codex", "login", "status"])
    ok = "not logged in" not in (p.stdout + p.stderr).lower()
    _CODEX_LOGIN_CACHE.update(at=now, ok=ok)
    return ok


# An architect round is one model call, but it is a long one - a full packet,
# a repo's worth of context, and a build order written out as numbered steps.
ARCHITECT_TIMEOUT_S = 15 * 60


def _codex_prompt(messages: list[dict]) -> str:
    """Flatten a chat thread into the single prompt `codex exec` takes.

    Every round re-sends the whole conversation, which is exactly what the
    OpenRouter path did, so nothing changes about what the architect sees.
    `codex exec resume` could send only the new turn instead, and that is the
    obvious next improvement, but it makes the thread a thing the CLI owns and
    we do not - worth doing deliberately, not as a side effect of this.
    """
    parts = []
    for m in messages:
        text = (m.get("content") or "").strip()
        if not text:
            continue
        role = m.get("role")
        if role == "system":
            parts.append(text)
        elif role == "assistant":
            parts.append("--- your earlier ruling ---\n" + text)
        else:
            parts.append("--- ---\n" + text)
    return "\n\n".join(parts)


# The token counts codex adds up, and only those. `cached_input_tokens` and
# `cache_write_input_tokens` are parts of `input_tokens`, and
# `reasoning_output_tokens` is part of `output_tokens`, so summing every integer
# in the record would double count a cached round by roughly twice over.
CODEX_TOKEN_FIELDS = ("input_tokens", "output_tokens")


def _usage_records(obj, out: list):
    """Every usage record in an event, wherever the schema happens to put it.

    Stops descending once it has one, so a record nested inside a record is
    counted once rather than twice.
    """
    if isinstance(obj, dict):
        if "total_tokens" in obj or "input_tokens" in obj:
            out.append(obj)
            return
        for v in obj.values():
            _usage_records(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _usage_records(v, out)


def _record_tokens(rec: dict) -> int:
    """One usage record's total, however that record spells it.

    codex reports `turn.completed` with input and output counts and no total;
    OpenAI-shaped payloads report `total_tokens`. Take whichever is there rather
    than assuming a schema neither of them promised.
    """
    if isinstance(rec.get("total_tokens"), int):
        return rec["total_tokens"]
    return sum(v for k, v in rec.items()
               if k in CODEX_TOKEN_FIELDS and isinstance(v, int))


def _codex_usage(stdout: str, prompt: str, reply: str) -> dict:
    """Token usage from the event stream, or a measured estimate saying so.

    This matters more than it looks. `cost_tokens` is what AUTO_TOKEN_CEILING
    stops a runaway thread with, so if the events ever stop carrying usage and
    this quietly returned zero, the ceiling would stop being a ceiling and
    nothing anywhere would say so. A rough estimate that is labelled as one
    keeps the brake connected - and the first version of this fell back to that
    estimate against a real run, reporting 14 tokens where the CLI had just said
    15,427, which is exactly how a ceiling stops being one without complaint.
    """
    total = 0
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        recs: list = []
        _usage_records(ev, recs)
        # Summed, not maxed: one exec can complete several turns, and each
        # reports only its own.
        total += sum(_record_tokens(r) for r in recs)
    if total:
        return {"total_tokens": total}
    return {"total_tokens": (len(prompt) + len(reply)) // 4, "estimated": True}


def codex_chat(messages: list[dict], model_key: str, max_tokens: int = 8000,
               *, web: bool = False) -> dict:
    """The architect, answered by `codex exec` against your ChatGPT subscription.

    Returns the shape `openrouter_chat` returns, because the two call sites read
    `choices[0].message.content` and `usage.total_tokens` and have no business
    knowing which backend answered.

    `model_key` is the luna/terra/sol price tier, which means nothing here - a
    subscription has no per-token price to choose between - so it is ignored
    rather than mistranslated into a model name that may not exist. Set
    `codex_architect_model` in config.json to pin one. `max_tokens` likewise has
    no equivalent: the CLI does not take a reply ceiling.

    It runs `-s read-only` from an empty directory, which means it cannot write
    anything, anywhere. It does NOT mean it cannot look: read-only permits reads
    across the filesystem, and a test round asked for the first line of a file
    outside the temp directory and gave it correctly. The empty cwd only means
    it starts with nothing to hand.

    So this is a real difference from the OpenRouter path, which was blind and
    could only answer from the packet. This architect may sometimes go and read
    a file instead of saying NEED[file], which is usually faster and occasionally
    wrong - it reads the live tree, not the lane's isolated worktree, so what it
    sees is not what the worker will be working on. Worth watching; the fix, if
    it turns out to matter, is to point it at the worktree deliberately rather
    than to pretend it cannot see.

    `web` lets the round search the internet. It is off by default and turned on
    per call rather than in config, because a round that can go and read the
    world answers a different question from one that can only read this
    workspace, and which of the two you wanted is a property of the question and
    not of the machine.
    """
    if not codex_available():
        die("codex CLI not found. Install it (npm i -g @openai/codex), or point "
            "the architect back at OpenRouter in Settings.")
    if not codex_logged_in():
        die("ChatGPT is not connected. Click Connect ChatGPT in the console, or "
            "point the architect back at OpenRouter in Settings.")

    prompt = _codex_prompt(messages)
    model = config().get("codex_architect_model")
    with tempfile.TemporaryDirectory(prefix="amp-architect-") as td:
        out = Path(td) / "reply.md"
        cmd = ["codex", "exec", "--json", "--skip-git-repo-check",
               "-s", "read-only", "-C", td, "-o", str(out)]
        if web:
            cmd += ["-c", "tools.web_search=true"]
        if model:
            cmd += ["-m", model]
        cmd.append("-")          # the packet is tens of KB: stdin, never argv
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                               timeout=ARCHITECT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            die(f"the codex architect timed out after {ARCHITECT_TIMEOUT_S}s")
        reply = out.read_text() if out.is_file() else ""

    if not reply.strip():
        # An empty answer alongside a clean exit code is worse than an error,
        # because it would be appended to the thread as though the architect had
        # deliberately said nothing. Treat it as the failure it is, in the CLI's
        # own words - redacted, because they are its words and not ours.
        detail = last_message(redact(p.stderr or p.stdout), 4).strip()
        die(f"the codex architect returned nothing (exit {p.returncode})"
            + (f":\n{detail}" if detail else ""))

    return {
        "choices": [{"message": {"role": "assistant", "content": reply}}],
        "model": f"codex/{model or 'default'}",
        "usage": _codex_usage(p.stdout, prompt, reply),
    }


def claude_chat(messages: list[dict], model: str, max_tokens: int = 8000) -> dict:
    """One round answered by Claude, as an architect or supervisor - not a worker.

    Returns the shape `openrouter_chat` and `codex_chat` return, for the same
    reason they agree with each other: the call sites read
    `choices[0].message.content` and `usage.total_tokens` and have no business
    knowing who answered.

    `--tools ""` is what makes this a CHAT rather than a coding agent. Without
    it, `claude -p` is the same thing the workers are - it would go and edit
    files while answering a question about them, from whatever directory the
    console happens to be sitting in. With it, this round can read nothing,
    write nothing and run nothing; it answers from the packet, exactly as the
    OpenRouter path does. The empty temp cwd is belt-and-braces on top.

    `max_tokens` has no equivalent on this CLI, same as codex. The spend guard
    that does exist is `--max-budget-usd`, so that is what is passed.
    """
    if not claude_available():
        die("Claude Code is not installed, so it cannot answer as an architect "
            "or supervisor. Pick a different model in Settings.")
    problem = claude_auth_problem()
    if problem:
        die(problem)

    prompt = _codex_prompt(messages)
    with tempfile.TemporaryDirectory(prefix="amp-think-") as td:
        cmd = ["claude", "-p", "--output-format", "json",
               "--model", model, "--tools", "",
               "--max-budget-usd", f"{architect_budget_usd():.2f}"]
        try:
            p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                               timeout=ARCHITECT_TIMEOUT_S, cwd=td,
                               env=claude_env())
        except subprocess.TimeoutExpired:
            die(f"the Claude architect timed out after {ARCHITECT_TIMEOUT_S}s")

    try:
        env = json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        env = {}
    reply = (env.get("result") or "") if not env.get("is_error") else ""

    if not reply.strip():
        # Same rule as codex: an empty answer with a clean exit is worse than an
        # error, because it gets appended to the thread as though Claude had
        # deliberately said nothing. Its own words, redacted, because they are
        # its words and not ours.
        detail = last_message(redact(p.stderr or p.stdout), 4).strip()
        die(f"the Claude architect returned nothing (exit {p.returncode})"
            + (f":\n{detail}" if detail else ""))

    usage = env.get("usage") or {}
    total = sum(v for k, v in usage.items()
                if k in ("input_tokens", "output_tokens") and isinstance(v, int))
    return {
        "choices": [{"message": {"role": "assistant", "content": reply}}],
        "model": f"claude/{model}",
        # Measured-and-labelled rather than zero, for the reason spelled out in
        # `_codex_usage`: AUTO_TOKEN_CEILING is a brake, and a brake that reads
        # zero when the numbers go missing has quietly stopped being one.
        "usage": ({"total_tokens": total} if total
                  else {"total_tokens": (len(prompt) + len(reply)) // 4,
                        "estimated": True}),
    }


def architect_budget_usd() -> float:
    """What one thinking round may spend on the Claude path.

    Its own setting, not the worker's `budget_usd`: a worker's budget covers a
    whole build, and handing that same ceiling to a single question would make
    an expensive accident cheap to trigger.
    """
    try:
        return max(0.01, float(config().get("architect_budget_usd") or 0.50))
    except (TypeError, ValueError):
        return 0.50


def _normalize_task(raw: dict) -> dict:
    """codex cloud list --json shape is not contractually stable (EXPERIMENTAL).

    Pull the fields we need by trying known aliases, and keep the raw record so
    nothing is silently lost if the schema moves.
    """

    def pick(*names):
        for n in names:
            if isinstance(raw, dict) and raw.get(n) not in (None, ""):
                return raw[n]
        return None

    return {
        "task_id": pick("id", "task_id", "taskId"),
        "status": (pick("status", "state", "phase") or "unknown"),
        "title": pick("title", "name", "prompt", "query"),
        "env_id": pick("environment_id", "env_id", "environmentId", "env"),
        "url": pick("url", "web_url", "link"),
        "updated_at": pick("updated_at", "updatedAt", "created_at", "createdAt"),
        "raw": raw,
    }


def codex_list(env_id: str | None = None, limit: int = LIST_LIMIT) -> list[dict]:
    cmd = ["codex", "cloud", "list", "--json", "--limit", str(min(limit, LIST_LIMIT))]
    if env_id:
        cmd += ["--env", env_id]
    p = run(cmd)
    if p.returncode != 0:
        die(f"codex cloud list failed:\n{(p.stderr or p.stdout).strip()}")
    text = p.stdout.strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # tolerate JSON-lines
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
        data = rows
    if isinstance(data, dict):
        data = data.get("tasks") or data.get("data") or data.get("items") or [data]
    return [_normalize_task(r) for r in data if isinstance(r, dict)]


# ---------------------------------------------------------------- commands


def cmd_login(args):
    """Connect a worker token from the terminal. The console does the same thing."""
    lg = ClaudeLogin()
    try:
        url = lg.start()
    except RuntimeError as e:
        die(str(e))
    print("Opening Chrome to sign in. If it did not open, use this URL:\n")
    print(f"  {url}\n")
    open_url(url)
    try:
        code = input("Paste the code here > ").strip()
    except (EOFError, KeyboardInterrupt):
        lg.close()
        die("cancelled")
    try:
        token = lg.submit(code)
    except RuntimeError as e:
        die(str(e))
    store_claude_token(token)
    print(f"\nconnected. token stored in {SECRETS_PATH} (untracked, chmod 600)")
    return 0


def cmd_doctor(args):
    cfg = config()
    print("amp doctor")
    print("=" * 60)

    ok = True
    lanes = cfg["lanes"]
    uses_codex = any(lane_backend(l) == "codex" for l in lanes.values())

    claude_ok = claude_available()
    print(f"  claude CLI         {'OK' if claude_ok else 'MISSING (npm i -g @anthropic-ai/claude-code)'}")
    if claude_ok:
        print(f"    version          {run(['claude', '--version']).stdout.strip()}")
        problem = claude_auth_problem()
        print(f"  claude auth        {'OK' if not problem else 'STALE'}")
        if problem:
            print(f"    {problem}")
            claude_ok = False
    ok &= claude_ok

    codex_ok = codex_available()
    print(f"  codex CLI          {'OK' if codex_ok else 'missing' + (' (npm i -g @openai/codex)' if uses_codex else ' (optional)')}")
    if codex_ok:
        print(f"    version          {run(['codex', '--version']).stdout.strip()}")
        logged = codex_logged_in()
        print(f"  codex auth         {'OK' if logged else 'NOT LOGGED IN -> run: codex login'}")
        if uses_codex:
            ok &= logged
    elif uses_codex:
        ok = False

    gh_ok = shutil.which("gh") is not None
    print(f"  gh CLI             {'OK' if gh_ok else 'missing (optional)'}")

    key = find_openrouter_key()
    if key:
        print(f"  openrouter key     OK ({key[:12]}...)")
    else:
        print("  openrouter key     MISSING -> escalation disabled")
        ok = False

    print(f"  lanes configured   {len(lanes)}")
    unbound = [n for n, l in lanes.items() if lane_backend(l) == "codex" and not l.get("env_id")]
    for n in sorted(lanes):
        lane = lanes[n]
        be = lane_backend(lane)
        mark = "NO ENV" if n in unbound else "OK "
        print(f"    {mark:7} {n:<22} {be:<7} {lane.get('repo','?')}")
    if unbound:
        print()
        print("  Codex lanes without an env id cannot dispatch. Get ids with:")
        print("    codex cloud            # interactive TUI, copy the env id per repo")
        print("    ./amp lane env <name> <ENV_ID>")
        ok = False

    budget = cfg.get("claude_budget_usd", DEFAULT_BUDGET_USD)
    print(f"  claude budget cap  ${budget:.2f} per dispatch  (model {cfg.get('claude_model', DEFAULT_CLAUDE_MODEL)})")
    print(f"  worker worktrees   {WORKTREE_DIR}")

    print("=" * 60)
    print("READY" if ok else "NOT READY - resolve the items above")
    return 0 if ok else 1


def cmd_lanes(args):
    cfg = config()
    lanes = cfg["lanes"]
    if not lanes:
        print("no lanes. Add one:\n  ./amp lane add wrl --repo c-u-l8er/WRL --path WRL")
        return 0
    print(f"{'LANE':<22} {'REPO':<32} {'BRANCH':<14} {'BACKEND':<8} ENV")
    for name in sorted(lanes):
        l = lanes[name]
        be = lane_backend(l)
        env = "" if be == "claude" else (l.get("env_id") or "-- unbound --")
        print(f"{name:<22} {l.get('repo',''):<32} {l.get('branch','main'):<14} {be:<8} {env}")
    return 0


def cmd_lane_add(args):
    try:
        lane = add_lane(args.name, repo=args.repo, path=args.path, branch=args.branch,
                        backend=args.backend, env_id=args.env, replace=args.replace)
    except LaneError as e:
        die(str(e))
    print(f"lane {args.name!r} -> {lane['repo']} ({lane['path']}) "
          f"branch={lane['branch']} backend={lane['backend']}")
    if lane["needs_env"]:
        print("  no env id yet. Run `codex cloud` to find it, then:")
        print(f"    ./amp lane env {args.name} <ENV_ID>")
    return 0


def cmd_lane_env(args):
    cfg = config()
    lane_or_die(cfg, args.name)
    cfg["lanes"][args.name]["env_id"] = args.env_id
    save_json(CONFIG_PATH, cfg)
    print(f"lane {args.name!r} bound to env {args.env_id}")
    return 0


def cmd_lane_backend(args):
    cfg = config()
    lane_or_die(cfg, args.name)
    cfg["lanes"][args.name]["backend"] = args.backend
    save_json(CONFIG_PATH, cfg)
    print(f"lane {args.name!r} now dispatches to {args.backend}")
    if args.backend == "codex" and not cfg["lanes"][args.name].get("env_id"):
        print(f"  bind an env id first: ./amp lane env {args.name} <ENV_ID>")
    return 0


def _print_claude_result(rec: dict):
    print(rec.get("result") or rec.get("error") or "(no output)")
    print()
    cost = rec.get("cost_usd") or 0.0
    print(f"{rec['status']}  ${cost:.4f}  {rec.get('num_turns') or '?'} turns  session {rec.get('session_id')}")
    print(f"worktree: {rec['cwd']}")
    if rec["status"] == "completed":
        print(f"review it with: ./amp diff {rec.get('lane','<lane>')}")


def cmd_dispatch(args):
    cfg = config()
    lane = lane_or_die(cfg, args.lane)
    backend = args.backend or lane_backend(lane)

    prompt = args.prompt
    if args.prompt_file:
        prompt = Path(args.prompt_file).read_text()
    if not (prompt or "").strip():
        die("prompt is empty")
    branch = args.branch or lane.get("branch") or "main"

    if backend == "claude":
        budget = args.budget if args.budget is not None else cfg.get(
            "claude_budget_usd", DEFAULT_BUDGET_USD
        )
        model = args.model or cfg.get("claude_model", DEFAULT_CLAUDE_MODEL)
        rec = start_claude_task(
            args.lane, lane, prompt,
            branch=branch, model=model, budget=budget, isolate=not args.shared_tree,
        )
        where = rec["cwd"] if rec["isolated"] else f"{rec['cwd']}  (SHARED TREE)"
        print(f"dispatching -> {args.lane} [claude {model}]  cap ${budget:.2f}")
        print(f"  {where}\n")
        rec = run_claude_task(args.lane, rec)
        rec["lane"] = args.lane
        _print_claude_result(rec)
        return 0 if rec["status"] == "completed" else 1

    env_id = lane.get("env_id")
    if not env_id:
        die(
            f"lane {args.lane!r} has no codex env id.\n"
            f"  Run `codex cloud` to browse environments, then:\n"
            f"    ./amp lane env {args.lane} <ENV_ID>"
        )
    if not codex_logged_in():
        die("codex is not logged in. Run: codex login")

    cmd = ["codex", "cloud", "exec", "--env", env_id, "--branch", branch]
    if args.attempts and args.attempts > 1:
        cmd += ["--attempts", str(args.attempts)]
    cmd += [prompt]

    print(f"dispatching -> {args.lane} ({lane['repo']} @ {branch}, best-of-{args.attempts})")
    p = run(cmd)
    out = (p.stdout + p.stderr).strip()
    print(out or "(no output)")
    if p.returncode != 0:
        die(f"dispatch failed (exit {p.returncode})")

    b = board()
    b["tasks"].setdefault(args.lane, [])
    b["tasks"][args.lane].insert(
        0,
        {
            "backend": "codex",
            "dispatched_at": now(),
            "prompt": prompt,
            "branch": branch,
            "attempts": args.attempts,
            "env_id": env_id,
            "submit_output": out,
            "status": "submitted",
        },
    )
    save_json(BOARD_PATH, b)
    print("\nrecorded on board. Poll with: ./amp poll")
    return 0


def cmd_reply(args):
    """Answer a claude worker mid-task. Codex Cloud has no equivalent."""
    cfg = config()
    lane = lane_or_die(cfg, args.lane)
    prior = latest_claude_task(args.lane)
    if not prior:
        die(f"no claude task on the board for {args.lane!r}")
    session = prior.get("session_id") or prior.get("task_id")
    if getattr(args, "session", None):
        session = resumable_session(args.lane, args.session)
        if not session:
            die(f"no resumable claude session matching {args.session!r} in {args.lane!r}")
        prior = next((r for r in board().get("tasks", {}).get(args.lane, [])
                      if (r.get("session_id") or r.get("task_id")) == session), prior)
    budget = args.budget if args.budget is not None else cfg.get(
        "claude_budget_usd", DEFAULT_BUDGET_USD
    )
    rec = start_claude_task(
        args.lane, lane, args.prompt,
        branch=prior.get("branch") or lane.get("branch") or "main",
        model=prior.get("model") or cfg.get("claude_model", DEFAULT_CLAUDE_MODEL),
        budget=budget,
        isolate=prior.get("isolated", True),
        resume_of=session,
    )
    print(f"resuming {args.lane} session {session}  cap ${budget:.2f}\n")
    rec = run_claude_task(args.lane, rec)
    rec["lane"] = args.lane
    _print_claude_result(rec)
    return 0 if rec["status"] == "completed" else 1


def cmd_poll(args):
    """Refresh codex lanes. Claude lanes settle themselves - nothing to poll."""
    cfg = config()
    b = board()

    lanes = [args.lane] if args.lane else sorted(cfg["lanes"])
    codex_lanes = [n for n in lanes if lane_backend(cfg["lanes"].get(n, {})) == "codex"]
    if codex_lanes and not codex_logged_in():
        die("codex is not logged in. Run: codex login")

    seen = 0
    for name in codex_lanes:
        lane = cfg["lanes"].get(name)
        if not lane or not lane.get("env_id"):
            continue
        tasks = codex_list(env_id=lane["env_id"])
        b.setdefault("remote", {})[name] = tasks
        seen += len(tasks)
    b["polled_at"] = now()
    save_json(BOARD_PATH, b)
    print(f"polled {len(lanes)} lane(s), {seen} remote task(s) at {b['polled_at']}")
    return cmd_board(args)


def cmd_board(args):
    cfg = config()
    b = board()
    remote = b.get("remote", {})
    print()
    print(f"BOARD  (polled {b.get('polled_at') or 'never'})")
    print("=" * 78)
    if not cfg["lanes"]:
        print("  no lanes configured")
        return 0
    local = b.get("tasks", {})
    stalled = []
    for name in sorted(cfg["lanes"]):
        lane = cfg["lanes"][name]
        be = lane_backend(lane)
        head = f"{name}  [{lane.get('repo','?')}]  {be}"

        if be == "claude":
            tasks = [t for t in local.get(name, []) if t.get("backend") == "claude"][:5]
            if not tasks:
                print(f"  {head}\n      idle")
                continue
            print(f"  {head}")
            for t in tasks:
                cost = t.get("cost_usd")
                money = f"${cost:.4f}" if cost else "-"
                line = (t.get("result") or t.get("error") or t.get("prompt") or "")
                print(f"      {t.get('status','?'):<12} {money:<10} {line.replace(chr(10), ' ')[:44]}")
            if any(_is_stalled(t) for t in tasks):
                stalled.append(name)
            continue

        tasks = remote.get(name, [])
        if not lane.get("env_id"):
            print(f"  {head}\n      unbound - no codex env id")
            continue
        if not tasks:
            print(f"  {head}\n      idle")
            continue
        print(f"  {head}")
        for t in tasks[:5]:
            title = (t.get("title") or "").replace("\n", " ")[:46]
            print(f"      {t.get('status','?'):<12} {str(t.get('task_id'))[:20]:<22} {title}")
        if any(_is_stalled(t) for t in tasks):
            stalled.append(name)
    print("=" * 78)
    if stalled:
        print(f"  needs attention: {', '.join(stalled)}   (./amp ask <lane> -q '...')")
    return 0


def _is_stalled(t: dict) -> bool:
    s = (t.get("status") or "").lower()
    return s in {"failed", "error", "cancelled", "canceled"}


def cmd_diff(args):
    cfg = config()
    lane = lane_or_die(cfg, args.lane)
    if lane_backend(lane) == "claude":
        d = claude_diff(args.lane, lane)
        print(d or "(worker made no changes)")
        return 0
    task_id = args.task_id or _latest_task_id(args.lane)
    cmd = ["codex", "cloud", "diff", task_id]
    if args.attempt:
        cmd += ["--attempt", str(args.attempt)]
    p = run(cmd)
    print(p.stdout or p.stderr)
    return p.returncode


def cmd_apply(args):
    cfg = config()
    lane = lane_or_die(cfg, args.lane)
    if lane_backend(lane) == "claude":
        # A claude worker's output already lives on a real branch. Merging it into
        # the shared checkout is yours to do: other sessions are editing that tree.
        print(f"claude lane {args.lane!r} - nothing to apply, the work is on a branch.\n")
        print(f"  worktree  {WORKTREE_DIR / args.lane}")
        print(f"  branch    amp/{args.lane}")
        print(f"\nreview:  ./amp diff {args.lane}")
        print(f"merge:   git -C {ROOT / lane['path']} merge amp/{args.lane}")
        return 0
    task_id = args.task_id or _latest_task_id(args.lane)
    cwd = ROOT / lane["path"]
    cmd = ["codex", "cloud", "apply", task_id]
    if args.attempt:
        cmd += ["--attempt", str(args.attempt)]
    print(f"applying {task_id} into {cwd}")
    p = run(cmd, cwd=cwd)
    print(p.stdout or p.stderr)
    return p.returncode


def _latest_task_id(lane_name: str) -> str:
    b = board()
    tasks = b.get("remote", {}).get(lane_name, [])
    if not tasks:
        die(f"no polled tasks for lane {lane_name!r}. Run ./amp poll (or pass a task id).")
    tid = tasks[0].get("task_id")
    if not tid:
        die(f"latest task for {lane_name!r} has no id in the codex payload")
    return tid


# ---------------------------------------------------------------- packet + consult


def build_packet(lane_name: str, question: str, extra_files: list[str]) -> tuple[Path, str]:
    """Zip a lane's state for GPT-5.6. Returns (zip_path, markdown_brief).

    `zip` is not installed on this box; use python zipfile.
    """
    cfg = config()
    lane = lane_or_die(cfg, lane_name)
    repo_path = ROOT / lane["path"]
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    PACKET_DIR.mkdir(parents=True, exist_ok=True)
    zpath = PACKET_DIR / f"{lane_name}-{stamp}.zip"

    status = run(["git", "status", "--porcelain"], cwd=repo_path).stdout
    log = run(["git", "log", "--oneline", "-15"], cwd=repo_path).stdout
    diff = run(["git", "diff", "HEAD"], cwd=repo_path).stdout
    branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_path).stdout.strip()

    b = board()
    lane_tasks = b.get("remote", {}).get(lane_name, [])
    local_hist = b.get("tasks", {}).get(lane_name, [])[:3]

    brief = io.StringIO()
    brief.write(f"# Packet: {lane_name}\n\n")
    brief.write(f"- repo: `{lane['repo']}`  branch: `{branch}`\n")
    brief.write(f"- generated: {now()}\n\n")
    brief.write("## Question for GPT-5.6\n\n")
    brief.write(question.strip() + "\n\n")
    brief.write("## Recent commits\n\n```\n" + (log or "(none)") + "```\n\n")
    brief.write("## Working tree status\n\n```\n" + (status or "(clean)") + "```\n\n")
    if local_hist:
        brief.write("## Recent dispatches\n\n")
        for h in local_hist:
            be = h.get("backend", "codex")
            brief.write(f"- {h['dispatched_at']} [{be}] ({h.get('status')}): {h['prompt'][:200]}\n")
        brief.write("\n")

    last_claude = next((h for h in local_hist if h.get("backend") == "claude"), None)
    if last_claude and (last_claude.get("result") or last_claude.get("error")):
        brief.write("## What the claude worker reported\n\n")
        brief.write((last_claude.get("result") or last_claude.get("error"))[:8000] + "\n\n")
    if last_claude:
        # A stalled worker's own trail is the most useful thing to hand an
        # architect, but a raw transcript is mostly tool payload, so it is one
        # line per turn.
        tail = transcript_digest(last_claude.get("session_id") or last_claude.get("task_id"))
        if tail:
            brief.write("## What the worker actually did (last turns)\n\n```\n" + tail + "```\n\n")

    worker_diff = claude_diff(lane_name, lane) if lane_backend(lane) == "claude" else ""
    if worker_diff:
        brief.write(f"## Worker branch amp/{lane_name} vs {branch}\n\n")
        brief.write("```diff\n" + worker_diff[:20000] + "\n```\n\n")
    if lane_tasks:
        brief.write("## Codex cloud tasks\n\n")
        for t in lane_tasks[:5]:
            brief.write(f"- `{t.get('status')}` {t.get('task_id')} {t.get('title') or ''}\n")
        brief.write("\n")
    if diff:
        brief.write(f"## Uncommitted diff ({len(diff)} bytes, full copy in zip)\n\n")
        brief.write("```diff\n" + diff[:20000] + "\n```\n\n")
    brief_text = brief.getvalue()

    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("PACKET_README.md", brief_text)
        z.writestr("git/status.txt", status)
        z.writestr("git/log.txt", log)
        z.writestr("git/diff.patch", diff)
        if worker_diff:
            z.writestr("git/worker.patch", worker_diff)
        for rel in extra_files:
            src = Path(rel)
            if not src.is_absolute():
                src = repo_path / rel
            if src.exists() and src.is_file():
                z.write(src, f"files/{src.name}")
            else:
                z.writestr(f"files/MISSING-{Path(rel).name}.txt", f"not found: {rel}")

    return zpath, brief_text


CONSULT_SYSTEM = (
    "You are the consulting architect for the [&] protocol stack. You are given a packet "
    "describing a stalled or ambiguous engineering situation, and you are in a working "
    "conversation about it: the operator can answer you, and a Claude worker with shell "
    "and editor access in an isolated git worktree will carry out what you rule and "
    "report back to you.\n\n"
    "Rule decisively. State: (1) the ruling, (2) the reasoning, (3) the exact next build "
    "order as numbered steps, (4) anything you are refusing to rule on and why.\n\n"
    "Write the build order as instructions to that worker, not as advice to a person - "
    "the steps are handed to it verbatim.\n\n"
    "If something is missing and you would otherwise be guessing, do not guess. Put each "
    "missing thing on its own line, and tag it with who can answer it:\n"
    "  NEED[file]: an exact path in the repo. It is read and handed back immediately.\n"
    "  NEED[evidence]: command output, a test result, a source excerpt - anything a "
    "worker with a shell in the worktree can go and observe. It is sent to fetch it and "
    "reports back to you, automatically.\n"
    "  NEED[decision]: a judgement only the operator can make - what to build, what "
    "counts as done, what to spend. This one stops the thread until they answer, so "
    "spend them carefully: ask for evidence wherever evidence would do.\n\n"
    "Be concrete and terse. Do not hedge."
)


def openrouter_chat(messages: list[dict], model_key: str, max_tokens: int = 8000) -> dict:
    # Enforced here rather than only at the callers, because this is the one
    # place the money is actually spent. A guard at each entry point is a guard
    # that is missing from whichever entry point gets added next.
    if not openrouter_enabled():
        die("OpenRouter is switched off in Settings. Turn it back on to use the "
            "architect, or answer this thread yourself.")
    model = CONSULT_MODELS.get(model_key, model_key)
    key = openrouter_key()
    payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
    req = urllib.request.Request(
        OPENROUTER_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "X-Title": "ProjectAmp2 amp harness",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        die(f"OpenRouter HTTP {e.code}: {e.read().decode()[:600]}")
    except urllib.error.URLError as e:
        die(f"OpenRouter unreachable: {e.reason}")


def web_search_backend() -> bool:
    """Whether the architect, as currently configured, can look at the internet.

    Asked rather than assumed, because a caller that wants the world and gets a
    round which quietly could not reach it would get back plausible recall
    presented as research. The one caller that wants this says so in its answer.
    """
    return architect_backend() == "codex"


def architect_chat(messages: list[dict], model_key: str, max_tokens: int = 8000,
                   *, web: bool = False) -> dict:
    """One architect round, wherever the architect currently lives.

    Every path that consults the architect goes through here, so switching
    backend is a settings change and not a code change, and so there is exactly
    one place to look when asking what a round costs and who it asks.

    `web` is a request, not a guarantee - only the codex backend can honour it.
    Ask `web_search_backend()` first if the difference matters to what you say
    about the answer.
    """
    backend = architect_backend()
    if backend == "codex":
        return codex_chat(messages, model_key, max_tokens, web=web)
    if backend == "claude":
        return claude_chat(messages, role_model("architect"), max_tokens)
    return openrouter_chat(messages, model_key, max_tokens)


# ---------------------------------------------------------------- consult threads
#
# One consult is one working conversation about one lane. It holds the packet,
# every ruling, every answer you gave, and every report the worker brought back,
# so the architect keeps its own context across as many rounds as it takes.

# CONSULT_DIR is bound by _bind_state; see _STATE_LAYOUT.

_CONSULT_LOCK = threading.Lock()

# How a turn is introduced to the architect. The packet is already a document.
TURN_PREFIX = {
    "packet": "",
    "you": "The operator answers:\n\n",
    "supplied": "You asked for these. Here they are:\n\n",
    "worker": "The Claude worker carried out your build order. It reported:\n\n",
}


def consult_path(cid: str) -> Path:
    return CONSULT_DIR / f"{Path(cid).name}.json"


def load_consult(cid: str) -> dict | None:
    p = consult_path(cid)
    return json.loads(p.read_text()) if p.is_file() else None


def save_consult(c: dict):
    CONSULT_DIR.mkdir(parents=True, exist_ok=True)
    save_json(consult_path(c["id"]), c)


def consults(lane: str | None = None) -> list[dict]:
    """Summaries, newest first."""
    if not CONSULT_DIR.exists():
        return []
    out = []
    for p in CONSULT_DIR.glob("*.json"):
        try:
            c = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if lane and c.get("lane") != lane:
            continue
        out.append({
            "id": c["id"], "lane": c.get("lane"), "opened_at": c.get("opened_at"),
            "status": c.get("status"), "trigger": c.get("trigger"),
            "question": c.get("question"), "model": c.get("model"),
            "rounds": sum(1 for t in c.get("turns", []) if t["role"] == "gpt"),
            "needs": c.get("needs") or [],
            # The same needs, carrying the architect's own `[decision]` tag.
            # `needs` has had the tag stripped off it, and guessing the kind
            # back from the wording gets it wrong: "Select next target: ..." is
            # a decision by any reading and matches none of the markers. Read
            # the tag rather than re-inferring it.
            "needs_typed": parse_typed_needs(last_ruling(c)),
            "blocked_on": c.get("blocked_on"),
            "blocked_why": BLOCK_REASONS.get(c.get("blocked_on") or ""),
            "cost_tokens": c.get("cost_tokens", 0),
            "last_at": (c.get("turns") or [{}])[-1].get("at"),
        })
    out.sort(key=lambda c: c.get("opened_at") or "", reverse=True)
    return out


# A need line, with or without the tag. Untagged lines come from older threads
# and from rulings that ignored the format, and they still have to work.
_NEED_RE = re.compile(
    r"^(?:[-*]\s*|\d+[.)]\s*)?NEED\s*(?:\[\s*(file|evidence|decision)\s*\])?\s*:\s*(.+)$",
    re.IGNORECASE,
)

# Phrases that mean the missing thing is a judgement rather than a fact. Only
# untagged needs are read this way - the architect is asked to tag its own, and
# the tag wins - so this list exists for older threads and for rulings that
# ignore the format.
#
# It leans towards `decision`, deliberately. Calling a decision "evidence" sends
# a worker to fetch something that does not exist, and a worker that cannot find
# a thing is under real pressure to produce a plausible one; the architect then
# rules on an invention as though it had been observed. Calling evidence a
# "decision" only stops the thread and asks you, which is what happened before
# any of this existed. The cheap mistake is the one to make.
_DECISION_MARKERS = (
    "decision", "decide", "your call", "confirm", "choose", "which of",
    "go/no-go", "go / no-go", "approve", "sign-off", "sign off", "priorit",
    "objective", "acceptance criteri", "success criteri", "definition of done",
    "scope", "budget", "preference", "do you want", "should we", "intent",
    "trade-off", "tradeoff", "policy you", "rule from you", "direction",
    "the operator", "operator-side", "you would like", "you prefer",
)


def need_kind(need: str) -> str:
    """Who can answer this: `decision` means only the operator, else `evidence`.

    Nothing is classified `file` here. Whether a need is a file is not a property
    of how it is worded - it is whether the path is on disk, which resolve_needs
    settles by going and looking. A need that names a file we do not have is
    evidence, because a worker can still go and find out.
    """
    low = need.lower()
    return "decision" if any(m in low for m in _DECISION_MARKERS) else "evidence"


def parse_typed_needs(text: str) -> list[dict]:
    """The architect's own list of what it is missing, each with a kind."""
    out = []
    for line in text.splitlines():
        m = _NEED_RE.match(line.strip())
        if not m:
            continue
        tag, body = m.group(1), m.group(2).strip()
        out.append({"kind": (tag or "").lower() or need_kind(body), "text": body})
    return out


def parse_needs(text: str) -> list[str]:
    """The architect's own list of what it is missing."""
    return [n["text"] for n in parse_typed_needs(text)]


def resolve_needs(lane_name: str, needs: list[str]) -> tuple[str, list[str]]:
    """Answer every NEED: that names a file we can just go and read.

    An architect that has to wait a round for a path it already named is an
    architect spending your money on nothing.

    Returns what to send back, and which needs it covers - the rest have to be
    got some other way, and the caller has to know which those are.
    """
    cfg = config()
    lane = cfg["lanes"].get(lane_name) or {}
    roots = [ROOT / lane.get("path", ""), ROOT, WORKTREE_DIR / lane_name]
    chunks, answered = [], []
    for need in needs:
        # A path is the first token that looks like one; the rest is prose.
        cand = next((w.strip("`'\",") for w in need.split()
                     if "/" in w or w.strip("`'\",").endswith((".md", ".py", ".json", ".ex"))), None)
        if not cand:
            continue
        hit = next((r / cand for r in roots if (r / cand).is_file()), None)
        if not hit:
            continue
        body, clipped = _clip(hit.read_text(errors="replace"), 30000)
        chunks.append(f"### {cand}\n\n```\n{body}\n```" + ("\n\n(clipped)" if clipped else ""))
        answered.append(need)
    return "\n\n".join(chunks), answered


def open_consult(lane_name: str, question: str, *, model_key: str,
                 trigger: str = "manual", extra_files: list[str] | None = None) -> dict:
    """Build the packet, start the thread, and get the first ruling."""
    zpath, brief = build_packet(lane_name, question, extra_files or [])
    c = {
        "id": "c" + uuid.uuid4().hex[:10],
        "lane": lane_name,
        "model": model_key,
        "opened_at": now(),
        "status": "open",
        "trigger": trigger,
        "question": question,
        "cost_tokens": 0,
        "turns": [{"role": "packet", "at": now(), "text": brief, "packet": zpath.name}],
    }
    save_consult(c)
    return advance_consult(c["id"])


def advance_consult(cid: str) -> dict:
    """Send the whole thread to the architect and append what it says back."""
    with _CONSULT_LOCK:
        c = load_consult(cid)
        if not c:
            die(f"no consult {cid!r}")
        msgs = [{"role": "system", "content": CONSULT_SYSTEM}]
        for t in c["turns"]:
            if t["role"] == "gpt":
                msgs.append({"role": "assistant", "content": t["text"]})
            else:
                msgs.append({"role": "user", "content": TURN_PREFIX.get(t["role"], "") + t["text"]})

    resp = architect_chat(msgs, c["model"])
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or "(empty)"
    usage = resp.get("usage") or {}

    with _CONSULT_LOCK:
        c = load_consult(cid)
        c["turns"].append({"role": "gpt", "at": now(), "text": text,
                           "model": resp.get("model"), "usage": usage})
        c["needs"] = parse_needs(text)
        c["cost_tokens"] = c.get("cost_tokens", 0) + (usage.get("total_tokens") or 0)
        save_consult(c)
    write_ruling(c)

    return auto_continue(cid)


# How far a thread is allowed to carry itself without you. Resolving its own
# NEED: lines is the whole point of the loop; resolving them forever is a bill.
# Both bounds are per thread, and both are small enough that hitting one is
# something you read about rather than something you pay for.
AUTO_MAX_ROUNDS = 6
AUTO_TOKEN_CEILING = 500_000

# Everything that reaches the architect, and therefore spends. Listed here beside
# the loop it describes rather than written into the settings page, because a
# list of what something costs is worth nothing the moment it drifts from the
# code. `unattended` is the column that matters: those fire with no click.
ARCHITECT_ACTIONS = [
    {"action": "Ask GPT-5.6",
     "fires": "you click Ask GPT-5.6, or send a dock message to an open thread",
     "unattended": False},
    {"action": "Continue a thread",
     "fires": "you click Continue on a thread that stopped",
     "unattended": False,
     "note": f"then carries itself up to {AUTO_MAX_ROUNDS} more rounds"},
    {"action": "Ask about history",
     "fires": "you ask a question from the History panel",
     "unattended": False},
    {"action": "Plan a goal",
     "fires": "you open a goal - the architect writes the plan and the next task",
     "unattended": False},
    {"action": "Review a goal's work",
     "fires": "a goal's worker finishes; the architect judges it against the "
              "definition of done and decides what goes next",
     "unattended": True},
    {"action": "Worker reports back",
     "fires": "a worker you relayed a ruling to finishes; its report goes to the "
              "architect that sent it and the thread advances a round",
     "unattended": True},
    {"action": "Gathered evidence returns",
     "fires": "the architect said NEED[evidence], a worker fetched it, and the "
              "thread advances on the answer",
     "unattended": True},
    {"action": "Auto-escalation",
     "fires": "a worker stops blocked and no thread owns it, so one is opened",
     "unattended": True,
     "note": "already has its own switch: auto_escalate"},
]

# Reading the balance is a plain GET against OpenRouter and costs nothing, so it
# keeps working with the switch off - that is how you check what you saved.

# Set by the console to (cid, needs) -> bool. Gathering evidence means starting a
# worker, and only the console knows how to do that - amp.py cannot import it.
# Without the hook the loop still answers file needs; it just cannot fetch.
ON_CONSULT_NEEDS = None

# Why a thread stopped continuing itself. `operator` is the only one that is not
# a limit being hit: it means the architect asked for something that does not
# exist anywhere on this machine, and waiting is correct.
BLOCK_REASONS = {
    "operator": "waiting on a decision only you can make",
    "stalled": "the architect asked for the same things again, so the thread is "
               "going round rather than forward",
    "rounds": "this thread has already continued itself as far as it is allowed to",
    "tokens": "this thread has spent as much of the OpenRouter balance as it is allowed to",
    "no-gather": "nobody is wired up to send a worker for evidence",
    "gather-failed": "a worker could not be sent to gather what it asked for",
}


def _need_key(needs: list[str]) -> list[str]:
    """This round's request, in a form two rounds can be compared by."""
    return sorted(" ".join(n.lower().split())[:120] for n in needs)


def _block(cid: str, why: str | None) -> dict:
    with _CONSULT_LOCK:
        c = load_consult(cid)
        c["blocked_on"] = why
        save_consult(c)
    return c


def auto_continue(cid: str) -> dict:
    """Keep a consult moving on its own until only you can move it.

    A ruling that ended in NEED: lines used to be where a thread stopped. The
    file-shaped needs were answered off disk and everything else sat there until
    you happened to look - which is most of them, because most of what an
    architect asks for is evidence: command output, a test result, what the
    source actually says. A worker in the lane can go and get all of that, and
    the report comes back through the same relay a build order's report comes
    back through, for as many rounds as it takes.

    What it will not do is guess on your behalf. A need that is a decision - what
    to build, what counts as done, what to spend - halts the thread and says so,
    because that answer is not on this machine to be fetched.
    """
    c = load_consult(cid)
    needs = parse_typed_needs(last_ruling(c))
    if not needs:
        return _block(cid, None)

    rounds = c.get("auto_rounds", 0)
    trail = c.get("need_trail") or []
    key = _need_key([n["text"] for n in needs])
    if key in trail:
        return _halted(cid, "stalled", needs)
    if rounds >= AUTO_MAX_ROUNDS:
        return _halted(cid, "rounds", needs)
    if c.get("cost_tokens", 0) >= AUTO_TOKEN_CEILING:
        return _halted(cid, "tokens", needs)

    with _CONSULT_LOCK:
        c = load_consult(cid)
        c["auto_rounds"] = rounds + 1
        c["need_trail"] = (trail + [key])[-8:]
        save_consult(c)

    # Files first: anything already on disk is instant and free.
    supplied, answered = resolve_needs(c["lane"], [n["text"] for n in needs])
    if supplied:
        add_turn(cid, "supplied", supplied)
        return advance_consult(cid)

    # A tagged `file` we could not find is not a decision - a worker can still go
    # and look for it - so it is fetched like any other evidence.
    outstanding = [n for n in needs if n["text"] not in answered]
    evidence = [n["text"] for n in outstanding if n["kind"] != "decision"]
    if not evidence:
        return _halted(cid, "operator", outstanding)
    if not ON_CONSULT_NEEDS:
        return _halted(cid, "no-gather", outstanding)
    try:
        sent = ON_CONSULT_NEEDS(cid, evidence)
    except Exception as e:
        print(f"amp: gather hook failed: {e}", file=sys.stderr)
        sent = False
    if not sent:
        return _halted(cid, "gather-failed", outstanding)
    # A round can want both kinds at once. The worker goes for the evidence
    # straight away, and you are told about the decisions now rather than a round
    # later, so the two can be answered in parallel instead of in series.
    decisions = [n["text"] for n in outstanding if n["kind"] == "decision"]
    if decisions:
        add_note(f"consult {cid} ({c['lane']}) sent a worker for evidence, but also "
                 f"wants a decision only you can make: {'; '.join(decisions)[:400]}",
                 lane=c.get("lane"))
    return _block(cid, "gathering")


def _halted(cid: str, why: str, needs: list[dict]) -> dict:
    """Stop, and put the reason where the operator already looks.

    A thread that goes quiet is the bug being fixed here. Whatever stops it says
    so in the orchestrator feed, including - especially - when what stopped it is
    that it needs an answer from you.
    """
    c = _block(cid, why)
    wanted = "; ".join(n["text"] for n in needs)[:400]
    add_note(f"consult {cid} ({c['lane']}) stopped: {BLOCK_REASONS.get(why, why)}. "
             f"It is still missing: {wanted}", lane=c.get("lane"))
    return c


def _elsewhere(lane_name: str | None) -> str:
    """The other repositories in this workspace, named, with their paths.

    Without this a worker asked about a path in a different repository looks,
    finds nothing - correctly, it is not in its worktree - and reports NOT
    FOUND. The architect cannot tell that apart from the thing being absent, so
    it rules that the evidence does not exist and closes on it. That happened:
    six artifacts were declared unreproducible while all six sat on disk one
    directory over. Naming the other lanes turns "I cannot see it" into a
    different sentence from "it is not there".
    """
    cfg = config()
    others = [(n, ROOT / (l.get("path") or n))
              for n, l in (cfg.get("lanes") or {}).items() if n != lane_name]
    if not others:
        return ""
    rows = "\n".join(f"  {n:<14} {p}" for n, p in sorted(others))
    return (
        "\n\nThis workspace holds several separate git repositories, one per lane. "
        f"You are in the {lane_name!r} lane's worktree and can see only that "
        "repository. The others are real and on disk, at:\n\n" + rows + "\n\n"
        "If an item asks about a path in one of those, do not report it as missing - "
        "you are simply not looking at it. Write `OUT OF THIS LANE:` under that item, "
        "say which lane owns it, and say that a worker in that lane could answer it. "
        "`NOT FOUND` and `OUT OF THIS LANE` mean opposite things to the architect, "
        "and it will close a line of work on the first one."
    )


def gather_prompt(needs: list[str], lane_name: str | None = None) -> str:
    """A read-only errand: get exactly these things, and say so if they are not there."""
    items = "\n".join(f"{i}. {n}" for i, n in enumerate(needs, 1))
    return (
        "A consulting architect (GPT-5.6) is ruling on this lane and has stopped, because "
        "it is missing the things below and will not guess at them. Go and get them.\n\n"
        "This turn is for gathering only. Do not edit files, do not implement anything, "
        "do not fix what you find - report it.\n\n"
        "--- needed ---\n\n" + items + "\n\n--- end needed ---\n\n"
        "Answer each item by its number. For a command, give the exact command and its "
        "real output. For a file, give the exact path and the part that matters. If "
        "something does not exist or cannot be determined from this worktree, write "
        "`NOT FOUND:` under that item and say what you looked at.\n\n"
        "A plain `NOT FOUND` is worth far more here than a plausible answer: the "
        "architect will rule on whatever you report, so anything you infer rather than "
        "observe becomes a decision made on evidence that does not exist. Do not "
        "summarise away detail it asked for by name.\n\n"
        "One thing it may not know about you: you are in an isolated worktree cut from "
        "committed HEAD, so you cannot see uncommitted work in the operator's checkout. "
        "If an item is really about that - what is uncommitted, what is stashed, what is "
        "untracked over there - say so under the item instead of reporting your own "
        "worktree's answer as though it were theirs."
        + _elsewhere(lane_name)
    )


def add_turn(cid: str, role: str, text: str, **extra) -> dict:
    with _CONSULT_LOCK:
        c = load_consult(cid)
        if not c:
            die(f"no consult {cid!r}")
        c["turns"].append({"role": role, "at": now(), "text": text, **extra})
        # You answering is the thread moving for a real reason, so it gets its
        # automatic rounds back - the budget is on the loop running unattended,
        # not on the conversation being long.
        if role == "you":
            c["auto_rounds"] = 0
            c["need_trail"] = []
        save_consult(c)
    return c


def last_ruling(c: dict) -> str:
    for t in reversed(c.get("turns", [])):
        if t["role"] == "gpt":
            return t["text"]
    return ""


def write_ruling(c: dict):
    """A consult also lands as a readable file, one per thread, rewritten each round."""
    RULING_DIR.mkdir(parents=True, exist_ok=True)
    body = [f"# GPT-5.6 consult: {c['lane']}\n",
            f"- id: {c['id']}\n- model: {c['model']}\n- opened: {c['opened_at']}\n"
            f"- trigger: {c.get('trigger')}\n- tokens: {c.get('cost_tokens')}\n"]
    for t in c["turns"]:
        head = {"packet": "Packet", "gpt": "Ruling", "you": "You",
                "supplied": "Supplied", "worker": "Worker"}.get(t["role"], t["role"])
        body.append(f"\n## {head} — {t['at']}\n\n{t['text']}\n")
    (RULING_DIR / f"{c['lane']}-{c['id']}.md").write_text("".join(body))


def worker_report(lane_name: str, rec: dict) -> str:
    """What the worker did, in the form the architect needs to judge it."""
    cfg = config()
    lane = cfg["lanes"].get(lane_name) or {}
    out = [f"status: {rec.get('status')}   turns: {rec.get('num_turns')}   "
           f"cost: ${rec.get('cost_usd') or 0:.4f}",
           "", (rec.get("result") or rec.get("error") or "(no output)")[:8000]]
    diff = claude_diff(lane_name, lane)
    if diff:
        out += ["", "## What it changed", "", "```diff", diff[:30000], "```"]
    else:
        out += ["", "It changed nothing in the worktree."]
    return "\n".join(out)


def ruling_prompt(c: dict) -> str:
    """The ruling, addressed to the worker that has to carry it out."""
    return (
        "A consulting architect (GPT-5.6) reviewed this lane and issued the ruling below. "
        "Carry out its build order in this worktree.\n\n"
        "If a step is impossible, or the ruling is wrong about this codebase, stop and say "
        "so on a line beginning with `BLOCKED:` and explain what it got wrong. Do not "
        "improvise around it - the architect will be shown what you say and will rule again."
        "\n\n--- ruling ---\n\n" + last_ruling(c) + "\n\n--- end ruling ---"
    )


# What makes a finished worker worth escalating. A worker that never got a turn
# in failed on this machine, not on the engineering, so it is not a question for
# an architect.
ESCALATE_MARKERS = ("BLOCKED:", "ESCALATE:")


def needs_escalation(rec: dict) -> str | None:
    """Why this finished task should go to GPT-5.6, or None."""
    if rec.get("backend") != "claude" or rec.get("killed"):
        return None                       # you stopped it, or a limit did
    body = rec.get("result") or ""
    for m in ESCALATE_MARKERS:
        if m in body:
            return body[body.index(m):].splitlines()[0][:300]
    if rec.get("status") == "failed" and rec.get("num_turns"):
        return f"the worker failed after {rec['num_turns']} turns: {(rec.get('error') or '')[:200]}"
    return None


# ---------------------------------------------------------------- goals
#
# A consult is a conversation about a lane. A goal is an objective for one, and
# the difference is that a goal knows when it is finished. It carries three
# things a prompt does not: a definition of done written before any work starts,
# an ordered task list, and a record of which done-items are actually met and by
# what evidence. The architect plans it and judges it, workers carry it out, and
# the loop between them runs without you until it finishes or hits a bound.
#
# The one rule that keeps this honest: a done-item may carry a `check`, a shell
# command run in the lane's worktree. Where there is a check, "met" means the
# command exited zero, and nobody's opinion overrides that. Where there is not,
# "met" is the architect's judgement of a worker's report, and it is recorded as
# such rather than dressed up as a test.

# GOAL_DIR is bound by _bind_state; see _STATE_LAYOUT.
_GOAL_LOCK = threading.Lock()

# The same shape of bound as a consult, for the same reason: this spends real
# money while you are not looking. A goal that hits one stops and says so.
GOAL_MAX_ROUNDS = 14
GOAL_TOKEN_CEILING = 900_000
GOAL_CHECK_TIMEOUT = 180

# Set by the console to (goal, task) -> dispatch result. Starting a worker is
# the console's job; amp.py cannot import it.
ON_GOAL_DISPATCH = None

GOAL_STOPPED = {
    "operator": "it needs a decision only you can make",
    "rounds": "it has run as many rounds as it is allowed to without you",
    "tokens": "it has spent as much of the OpenRouter balance as it is allowed to",
    "no-dispatch": "nobody is wired up to start workers for it",
    "dispatch-failed": "a worker could not be started for its next task",
    "no-plan": "the architect did not return a plan that could be read",
}

GOAL_PLAN_SYSTEM = (
    "You are the consulting architect for the [&] protocol stack. The operator has given "
    "you one objective for one codebase. You are not doing the work: Claude workers with "
    "shell and editor access in an isolated git worktree will, one task at a time, and you "
    "will judge each result against the definition of done you write now.\n\n"
    "Reply with one JSON object and nothing else:\n"
    "{\n"
    '  "done": [{"text": "a single checkable condition", "check": "shell command or null"}],\n'
    '  "tasks": [{"text": "one instruction to one worker, in order"}],\n'
    '  "questions": ["a decision only the operator can make"]\n'
    "}\n\n"
    "Rules:\n"
    "- `done` is the definition of done. Each item is one condition that is either true or "
    "false about the repository, not an activity. Prefer conditions a command can settle, "
    "and put that command in `check` - it is run in the worktree and its exit code is what "
    "counts. Use null where no command can honestly settle it.\n"
    "- `tasks` are sized for one worker session of under twenty minutes each. Order them so "
    "each one is possible when it is reached. Six or fewer to start; you can add more after "
    "you see what comes back.\n"
    "- `questions` is for what you genuinely cannot decide: what the operator wants built, "
    "what counts as good enough, what to spend. Asking one stops everything until they "
    "answer, so ask only for what a worker could not go and find out.\n"
    "- Ground every item in the repository state you were given. Do not invent files, tests, "
    "or commands that are not evidenced there.\n"
    "- The operator's doctrine is included below the objective. Plan under it: where a "
    "done-item is a claim about status, write it in the vocabulary of the evidence ladder "
    "(`spec`, `in_tree`, `live_local`, `live_deployed`, `external`) and make the check the "
    "thing that settles which rung it is on. Never write a done-item that says \"done\"."
)

GOAL_REVIEW_SYSTEM = (
    "You are the consulting architect judging progress against a definition of done you "
    "wrote. You are given the objective, the definition of done with the result of any "
    "checks that were run, the task list, and what the last worker reported.\n\n"
    "Reply with one JSON object and nothing else:\n"
    "{\n"
    '  "done": [{"text": "exactly the item text you were given", "met": true,\n'
    '            "evidence": "what makes it true - a check result, a diff, a file"}],\n'
    '  "tasks": [{"text": "...", "state": "todo|done|dropped"}],\n'
    '  "verdict": "continue|done|blocked",\n'
    '  "why": "one or two sentences",\n'
    '  "questions": ["a decision only the operator can make"],\n'
    '  "doctrine": {"bearing": "advanced|contradicted|proposed|none",\n'
    '               "text": "what it is, in one or two sentences, or \\"\\" for none"}\n'
    "}\n\n"
    "Rules:\n"
    "- Return every done-item, in the order given, with its text unchanged. A check that "
    "was run outranks your reading of any report: if it failed, the item is not met, "
    "whatever the worker said about it.\n"
    "- `met: true` on an item with no check means you are asserting it from evidence in the "
    "report. Say what that evidence is. If the report does not contain it, the item is not "
    "met and the next task is to produce it.\n"
    "- Return the task list with states updated, and append new tasks if what came back "
    "showed work that is genuinely needed. Do not pad it: a goal ends.\n"
    "- `verdict` is `done` only when every done-item is met. Use `blocked` when no worker "
    "can make progress without an operator decision, and put that in `questions`.\n"
    "- `doctrine` is what this round bears on the operator's doctrine, included below the "
    "objective. `advanced` names a claim and the rungs it moved between; `contradicted` "
    "names what is now known to be false or weaker than stated, and outranks everything "
    "else in this reply; `proposed` states a new or amended value as a claim that could be "
    "wrong, and says what would test it. `none` is the ordinary answer and is expected most "
    "rounds. Do not manufacture a finding to have one - that is the fabrication the doctrine "
    "forbids. You propose amendments; only the operator adopts them."
)


def goal_path(gid: str) -> Path:
    return GOAL_DIR / f"{Path(gid).name}.json"


def load_goal(gid: str) -> dict | None:
    p = goal_path(gid)
    return json.loads(p.read_text()) if p.is_file() else None


def save_goal(g: dict):
    GOAL_DIR.mkdir(parents=True, exist_ok=True)
    g["updated_at"] = now()
    save_json(goal_path(g["id"]), g)


def goals(lane: str | None = None) -> list[dict]:
    """Summaries, newest first."""
    if not GOAL_DIR.exists():
        return []
    out = []
    for p in GOAL_DIR.glob("*.json"):
        try:
            g = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if lane and g.get("lane") != lane:
            continue
        dod = g.get("done") or []
        tasks = g.get("tasks") or []
        out.append({
            "id": g["id"], "lane": g.get("lane"), "objective": g.get("objective"),
            "state": g.get("state"), "opened_at": g.get("opened_at"),
            "updated_at": g.get("updated_at"), "rounds": g.get("rounds", 0),
            "cost_tokens": g.get("cost_tokens", 0),
            "stopped_on": g.get("stopped_on"),
            "stopped_why": GOAL_STOPPED.get(g.get("stopped_on") or ""),
            "questions": g.get("questions") or [],
            "met": sum(1 for d in dod if d.get("met")), "dod": len(dod),
            "tasks_done": sum(1 for t in tasks if t.get("state") == "done"),
            "tasks_total": sum(1 for t in tasks if t.get("state") != "dropped"),
            "now": next((t["text"] for t in tasks if t.get("state") == "running"), None),
        })
    out.sort(key=lambda g: g.get("opened_at") or "", reverse=True)
    return out


def goal_brief(lane_name: str) -> str:
    """The repository as it actually is, for planning against.

    Deliberately small and factual. A plan written against an invented file is
    worse than no plan, because the first worker spends a session discovering it.
    """
    cfg = config()
    lane = cfg["lanes"].get(lane_name) or {}
    repo = (ROOT / lane.get("path", ".")).resolve()
    wt = WORKTREE_DIR / lane_name
    where = wt if wt.exists() else repo
    parts = [f"# Lane `{lane_name}`",
             f"- repo: {lane.get('repo') or lane.get('path')}",
             f"- path: {repo}",
             f"- base branch: {lane.get('branch') or 'main'}",
             f"- worker worktree: {where}"]

    def cap(cmd, title, limit=3000):
        p = run(cmd, cwd=where)
        body = (p.stdout or p.stderr or "").strip()
        if body:
            parts.append(f"\n## {title}\n```\n{body[:limit]}\n```")

    cap(["git", "log", "--oneline", "-12"], "Recent commits")
    cap(["git", "status", "--short"], "Working tree")
    cap(["ls", "-1"], "Top level")
    for rel in ("README.md", "docs/spec/README.md", "AGENTS.md", "CLAUDE.md"):
        f = where / rel
        if f.is_file():
            try:
                head = "\n".join(f.read_text().splitlines()[:60])
            except (OSError, UnicodeDecodeError):
                continue
            parts.append(f"\n## {rel} (first 60 lines)\n```\n{head[:4000]}\n```")
    return "\n".join(parts)


def _json_reply(text: str) -> dict | None:
    """The one JSON object in a model's reply, fences and preamble and all."""
    body = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", body, re.S)
    if fence:
        body = fence.group(1).strip()
    start = body.find("{")
    if start < 0:
        return None
    depth, in_str, esc = 0, False, False
    for i, ch in enumerate(body[start:], start):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    out = json.loads(body[start:i + 1])
                except json.JSONDecodeError:
                    return None
                return out if isinstance(out, dict) else None
    return None


def _goal_chat(g: dict, system: str, user: str) -> tuple[dict | None, str]:
    resp = architect_chat([{"role": "system", "content": system},
                            {"role": "user", "content": user}], g["model"])
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    usage = resp.get("usage") or {}
    with _GOAL_LOCK:
        cur = load_goal(g["id"]) or g
        cur["cost_tokens"] = cur.get("cost_tokens", 0) + (usage.get("total_tokens") or 0)
        cur["rounds"] = cur.get("rounds", 0) + 1
        save_goal(cur)
    return _json_reply(text), text


def open_goal(lane_name: str, objective: str, *, model_key: str | None = None) -> dict:
    """State an objective for a lane. The architect turns it into a plan."""
    cfg = config()
    if lane_name not in cfg["lanes"]:
        die(f"unknown lane {lane_name!r}")
    g = {
        "id": "g" + uuid.uuid4().hex[:10],
        "lane": lane_name,
        "objective": objective.strip(),
        "model": model_key or cfg.get("consult_model", DEFAULT_CONSULT),
        "opened_at": now(),
        "state": "planning",
        # Who is planning it, so that "the planner died" is a fact rather than a
        # guess from a clock. `plan_goal` is one architect call and every one of
        # its exits either starts the goal or stops it - so a record still
        # saying `planning` with nobody on it is a plan that never arrived, and
        # until this was written down there was no way to tell that apart from
        # a plan still being written. See `stranded_plans`.
        "planning_pid": os.getpid(),
        "done": [], "tasks": [], "questions": [],
        "rounds": 0, "cost_tokens": 0,
        "log": [], "stopped_on": None,
    }
    save_goal(g)
    return plan_goal(g["id"])


def goal_log(gid: str, text: str, **extra) -> dict:
    with _GOAL_LOCK:
        g = load_goal(gid)
        if not g:
            die(f"no goal {gid!r}")
        g.setdefault("log", []).append({"at": now(), "text": text, **extra})
        g["log"] = g["log"][-60:]
        save_goal(g)
    return g


def _goal_stop(gid: str, why: str | None, note: str = "") -> dict:
    with _GOAL_LOCK:
        g = load_goal(gid)
        g["stopped_on"] = why
        if why:
            g["state"] = "blocked"
        save_goal(g)
    if why:
        goal_log(gid, f"stopped: {GOAL_STOPPED.get(why, why)}" + (f" {note}" if note else ""))
        add_note(f"goal {gid} ({g['lane']}) stopped: {GOAL_STOPPED.get(why, why)}."
                 + (f" {note}" if note else ""), lane=g.get("lane"))
    return g


def plan_goal(gid: str) -> dict:
    g = load_goal(gid)
    user = (f"# Objective\n\n{g['objective']}"
            + mission_block()
            + doctrine_block("What this work is held to")
            + f"\n\n# The repository right now\n\n{goal_brief(g['lane'])}")
    plan, raw = _goal_chat(g, GOAL_PLAN_SYSTEM, user)
    if not plan:
        goal_log(gid, "the architect's plan could not be read as JSON", raw=raw[:2000])
        return _goal_stop(gid, "no-plan")
    with _GOAL_LOCK:
        g = load_goal(gid)
        g["done"] = [{"text": str(d.get("text") or "").strip(),
                      "check": (d.get("check") or None), "met": False, "evidence": ""}
                     for d in (plan.get("done") or []) if (d.get("text") or "").strip()]
        g["tasks"] = [{"id": "t" + uuid.uuid4().hex[:6],
                       "text": str(t.get("text") or "").strip(), "state": "todo"}
                      for t in (plan.get("tasks") or []) if (t.get("text") or "").strip()]
        g["questions"] = [q for q in (plan.get("questions") or []) if str(q).strip()]
        g["state"] = "running"
        g["planning_pid"] = None      # the plan arrived; nobody is planning it now
        save_goal(g)
    goal_log(gid, f"planned: {len(g['done'])} done-conditions, {len(g['tasks'])} tasks")
    if g["questions"]:
        return _goal_stop(gid, "operator", "It asks: " + "; ".join(g["questions"])[:400])
    if not g["tasks"]:
        return _goal_stop(gid, "no-plan", "the plan had no tasks in it")
    return goal_dispatch(gid)


# ------------------------------------------------------- reopening a live goal
#
# Two things an operator can do to a goal that is already open, and they are
# different operations however similar they look on a page:
#
#   RECALCULATE keeps the objective and rebuilds the plan against the repository
#   as it is NOW. A plan is written once, against a tree that then changes for
#   hours - by this goal's own workers, and by every other lane's. Its later
#   tasks were written for a repository that no longer exists, and its
#   done-conditions can be checking a path somebody has since moved.
#
#   IMPROVE changes the objective itself, because the plan is fine and the thing
#   being asked for is what cannot land.
#
# Neither runs on the heartbeat. Each is an architect call, and each rewrites
# work in flight - that is an operator's decision both times.

GOAL_REPLAN_SYSTEM = (
    "You are the consulting architect. You planned this objective earlier, work has "
    "happened since, and the repository is not what it was when you planned it. Write the "
    "plan you would write for this same objective TODAY.\n\n"
    "Reply with one JSON object and nothing else, in exactly the shape you used before:\n"
    "{\n"
    '  "done": [{"text": "a single checkable condition", "check": "shell command or null"}],\n'
    '  "tasks": [{"text": "one instruction to one worker, in order"}],\n'
    '  "questions": ["a decision only the operator can make"]\n'
    "}\n\n"
    "Rules:\n"
    "- The objective does not change. You are re-deriving how to reach it, not what it is.\n"
    "- A done-condition already met is settled. Restate it in the SAME WORDS if it still "
    "belongs to this objective - matching text is how its evidence is kept - and leave it "
    "out only if the objective genuinely no longer needs it. You may not reword a met "
    "condition; that silently discards the evidence that met it.\n"
    "- Drop tasks that the work since has made pointless, and say nothing about them. "
    "Keep the wording of a task that is still right, so its state carries over.\n"
    "- Ground every item in the repository state you were given, which is current. This is "
    "the whole point of the call: a check that names a file nobody has, or a task written "
    "for a layout that has since moved, is what you are here to correct.\n"
    "- If the work since has revealed something only the operator can decide, ask it. "
    "Asking stops the goal, so ask only for what a worker could not go and find out."
)

GOAL_IMPROVE_SYSTEM = (
    "You are the consulting architect. An objective is open and is not landing. Say whether "
    "it can be made to land, and if so what it should say instead.\n\n"
    "Reply with one JSON object and nothing else:\n"
    "{\n"
    '  "objective": "the revised objective, or null to leave it alone",\n'
    '  "what_changed": "what you narrowed or split off, and which unknown that kills",\n'
    '  "why": "why this version can land where the current one cannot",\n'
    '  "keeps_the_point": "what the mission wanted from the original, and where it is in '
    'this one",\n'
    '  "assessment": "what is actually stopping it, in two or three sentences"\n'
    "}\n\n"
    "Rules:\n"
    "- Work already met against the current objective stays met. A revision that invalidates "
    "a satisfied done-condition is not an improvement, it is a restart wearing the same "
    "goal id - return null and say so.\n"
    "- Narrowing until the mission no longer wants it is the failure this call exists to "
    "avoid. `keeps_the_point` is where you show it did not happen; if you cannot fill that "
    "field honestly, return null.\n"
    "- You may not raise the odds by deciding something that is the operator's. If what is "
    "stopping it is money, a credential, an account, a deploy target, or what counts as good "
    "enough, the honest revision is one that DOES NOT NEED that - never one that assumes it, "
    "and never one that redefines done so the missing thing stops mattering. If no such "
    "revision exists, return null and name the thing.\n"
    "- Returning null is a real answer and often the right one. The goal stays exactly as "
    "it is, and the operator learns what it is waiting on."
)


def _reopen_doc(g: dict) -> str:
    """A live goal, its evidence, and the repository underneath it right now."""
    done = "\n".join(
        f"- [{'met' if d.get('met') else 'not met'}] {d['text']}"
        + (f"\n    check: `{d['check']}`" if d.get("check") else "")
        + (f" (exit {d['check_exit']})" if d.get("check_exit") is not None else "")
        + (f"\n    evidence: {str(d.get('evidence'))[:300]}" if d.get("evidence") else "")
        for d in g.get("done") or []) or "(none)"
    tasks = "\n".join(f"- ({t.get('state')}) {t.get('text')}"
                      for t in g.get("tasks") or []) or "(none)"
    log = "\n".join(f"- {e.get('at')}: {str(e.get('text'))[:300]}"
                    for e in (g.get("log") or [])[-14:]) or "(nothing yet)"
    return (f"# Objective\n\n{g['objective']}\n\n"
            f"# The definition of done, as it stands\n\n{done}\n\n"
            f"# The task list, as it stands\n\n{tasks}\n\n"
            f"# What has happened on this goal\n\n{log}\n\n"
            f"- rounds used: {g.get('rounds', 0)} of {GOAL_MAX_ROUNDS}\n"
            f"- stopped on: {g.get('stopped_on') or 'nothing, it is live'}\n"
            + mission_block()
            + doctrine_block("What this work is held to")
            + f"\n\n# The repository right now\n\n{goal_brief(g['lane'])}")


def _reopenable(gid: str) -> tuple[dict | None, str]:
    """The goal, or why it may not be reopened. Shared by both operations."""
    g = load_goal(gid)
    if not g:
        return None, f"no goal {gid!r}"
    if g.get("state") == "done":
        return None, "a finished goal is not replanned - propose the next one instead"
    if any(t.get("state") == "running" for t in g.get("tasks") or []):
        # The worker is out against the task list it was handed. Rewriting that
        # list underneath it means its result comes back matching nothing, and
        # the session is spent for a report nobody can file.
        return None, "a worker is out on this goal - wait for it to report, then try again"
    if not architect_available():
        return None, architect_off_reason()
    return g, ""


def recalculate_goal(gid: str) -> dict:
    """Rebuild a live goal's plan against the repository as it is now.

    Met conditions keep their evidence, matched on text - which is why the
    system prompt forbids rewording one. Task state carries over the same way.
    """
    g, why = _reopenable(gid)
    if not g:
        return {"ok": False, "error": why}

    checks = run_goal_checks(gid)          # so the architect judges live results
    g = load_goal(gid)
    plan, raw = _goal_chat(g, GOAL_REPLAN_SYSTEM, _reopen_doc(g))
    if not plan:
        goal_log(gid, "the architect's replan could not be read as JSON", raw=raw[:2000])
        return {"ok": False, "error": "the architect's answer could not be read as JSON"}

    with _GOAL_LOCK:
        g = load_goal(gid)
        was_done = {d["text"]: d for d in g.get("done") or []}
        was_tasks = {t["text"]: t for t in g.get("tasks") or []}
        kept_done = kept_tasks = 0
        fresh_done = []
        for d in (plan.get("done") or []):
            text = str(d.get("text") or "").strip()
            if not text:
                continue
            old = was_done.get(text)
            if old and old.get("met"):
                kept_done += 1
                old["check"] = d.get("check") or None
                fresh_done.append(old)
            else:
                fresh_done.append({"text": text, "check": d.get("check") or None,
                                   "met": False, "evidence": ""})
        fresh_tasks = []
        for t in (plan.get("tasks") or []):
            text = str(t.get("text") or "").strip()
            if not text:
                continue
            old = was_tasks.get(text)
            if old:
                kept_tasks += 1
                fresh_tasks.append(old)
            else:
                fresh_tasks.append({"id": "t" + uuid.uuid4().hex[:6], "text": text,
                                    "state": "todo"})
        dropped = [t["text"] for t in was_tasks.values() if t["text"] not in
                   {x["text"] for x in fresh_tasks}]
        lost = [t for t in was_done if t not in {d["text"] for d in fresh_done}
                and was_done[t].get("met")]
        g["done"], g["tasks"] = fresh_done, fresh_tasks
        g["questions"] = [q for q in (plan.get("questions") or []) if str(q).strip()]
        g["replanned_at"] = now()
        # Whatever it was stopped on, it was stopped under the OLD plan. Holding
        # the stop across a replan would make the button do nothing visible.
        g["state"], g["stopped_on"] = "running", None
        save_goal(g)

    note = (f"recalculated: {len(g['done'])} done-condition(s) ({kept_done} already met kept), "
            f"{len(g['tasks'])} task(s) ({kept_tasks} carried over, {len(dropped)} dropped)")
    goal_log(gid, note, checks=len(checks or []))
    if lost:
        # Named, because it is the one thing this operation can quietly destroy.
        goal_log(gid, "these met conditions are no longer in the definition of done: "
                      + "; ".join(lost)[:400])
    add_note(f"goal {gid} ({g['lane']}) {note}", lane=g.get("lane"))
    if g["questions"]:
        return {"ok": True, "goal": _goal_stop(gid, "operator",
                                               "It asks: " + "; ".join(g["questions"])[:400]),
                "kept_done": kept_done, "dropped_tasks": dropped, "lost_met": lost}
    if not g["tasks"]:
        # Every task gone and nothing new. That is the architect saying the plan
        # is finished, not that it is empty - let the review settle it.
        return {"ok": True, "goal": goal_review(gid, "the plan was recalculated and came back "
                                                     "with no remaining tasks"),
                "kept_done": kept_done, "dropped_tasks": dropped, "lost_met": lost}
    return {"ok": True, "goal": goal_dispatch(gid), "kept_done": kept_done,
            "dropped_tasks": dropped, "lost_met": lost}


def improve_goal(gid: str, *, apply: bool = False) -> dict:
    """Ask whether a live goal's objective can be made to land, and optionally do it.

    Two steps on purpose. Changing the objective of work already in flight is
    not a thing to do on one click and a hope - the first call answers, the
    operator reads it, and a second call with `apply` commits to it.
    """
    g, why = _reopenable(gid)
    if not g:
        return {"ok": False, "error": why}

    out, raw = _goal_chat(g, GOAL_IMPROVE_SYSTEM, _reopen_doc(g))
    if not out:
        goal_log(gid, "the architect's improvement could not be read as JSON", raw=raw[:2000])
        return {"ok": False, "error": "the architect's answer could not be read as JSON"}

    revised = str(out.get("objective") or "").strip()
    ans = {"ok": True, "goal_id": gid, "objective": revised or None,
           "was": g["objective"],
           "what_changed": str(out.get("what_changed") or "")[:600],
           "why": str(out.get("why") or "")[:600],
           "keeps_the_point": str(out.get("keeps_the_point") or "")[:600],
           "assessment": str(out.get("assessment") or "")[:1000],
           "applied": False}
    if not revised or _norm_prompt(revised) == _norm_prompt(g["objective"]):
        ans["objective"] = None
        goal_log(gid, "improvement: left alone - " + (ans["assessment"] or "no reason given"))
        return ans
    if not apply:
        goal_log(gid, f"improvement offered: {revised[:300]}", what_changed=ans["what_changed"])
        return ans

    with _GOAL_LOCK:
        cur = load_goal(gid)
        cur.setdefault("objective_history", []).append(
            {"at": now(), "was": cur["objective"], "what_changed": ans["what_changed"]})
        cur["objective"] = revised[:2000]
        save_goal(cur)
    goal_log(gid, f"objective replaced: {revised[:300]}", what_changed=ans["what_changed"])
    add_note(f"goal {gid} ({g['lane']}) objective improved: {ans['what_changed'][:200]}",
             lane=g.get("lane"))
    # A new objective and an old plan is the one state this must not leave
    # behind: the tasks would be working toward something nobody asked for.
    ans["applied"] = True
    ans["recalculated"] = recalculate_goal(gid)
    return ans


# What to tell a worker about the clock, learned the hard way.
#
# A worker stopped by a limit keeps everything it wrote to disk and loses
# everything it was about to say - so an uncommitted worktree is the one state
# where a kill is expensive. Telling a worker to commit early fixes that, and
# creates a second problem: it will commit work it has not built. That is not
# hypothetical. An ic32.c optimisation was cut off mid-rename, sat unverified
# through two more kills, was committed on an early-commit instruction, and only
# a run that survived long enough to call the compiler found that it declared
# one name and used another. So the instruction has to carry both halves: commit
# so the work survives, then verify, then fix or amend - and never end holding
# a commit nobody built.
LANDING_RULE = (
    "You have about {mins} minutes of wall clock before you are stopped. Being "
    "stopped keeps everything you have written to disk and loses everything you "
    "were about to say, so work in small steps and land them as you go:\n\n"
    "1. Commit as soon as you have something coherent, before you verify it. A "
    "commit is how work survives you, not a claim that it works.\n"
    "2. Then build it and run its tests. A commit you have not built is the one "
    "thing worse than uncommitted work, because it looks finished.\n"
    "3. If it fails, fix it and amend, or revert that part and say so. Do not "
    "leave the branch holding a commit nobody has run.\n\n"
    "If you are running long, stop early and report where you got to - a partial "
    "result that is committed and reported is worth more than a complete one "
    "that is cut off."
)


def landing_rule() -> str:
    return LANDING_RULE.format(mins=limits()["timeout_s"] // 60)


def goal_worker_prompt(g: dict, task: dict) -> str:
    dod = "\n".join(f"- {d['text']}" + (f"   [checked by: `{d['check']}`]" if d.get("check") else "")
                    for d in g["done"]) or "- (none stated)"
    done_before = [t["text"] for t in g["tasks"] if t.get("state") == "done"]
    prior = ("\n\nAlready done in this goal by earlier workers:\n"
             + "\n".join(f"- {t}" for t in done_before)) if done_before else ""
    return (
        f"You are one worker on a shared goal in this repository.\n\n"
        f"# The goal\n\n{g['objective']}"
        + mission_block()
        + doctrine_block("What your work is held to")
        + f"\n\n# It is finished when all of these are true\n\n{dod}\n\n"
        f"# Your task, and only this one\n\n{task['text']}{prior}\n\n"
        f"Do this task. Do not do the other tasks - other workers have them, and two "
        f"agents editing the same files is how this goes wrong.\n\n"
        f"{landing_rule()}\n\n"
        f"When you finish, report: what you changed, what you ran and what it printed, and "
        f"which of the done-conditions above you believe your work makes true, with the "
        f"evidence for each. If you could not do the task, say so on a line beginning "
        f"`BLOCKED:` and say exactly what stopped you. Do not improvise a different task.\n\n"
        f"End your report with a section headed `DOCTRINE:` on its own line, whose first "
        f"word is one of `advanced`, `contradicted`, `proposed` or `none`, followed by a "
        f"sentence or two. `none` means this was ordinary work, and is the right answer most "
        f"of the time - do not invent a finding to fill the section.\n\n"
        f"{idea_rule()}"
    )


def idea_rule() -> str:
    """What to do with something worth building that is not this task.

    A worker is the only thing that ever reads this code with its hands in it,
    and what it notices there is thrown away with the worktree. One line costs
    nothing and gates nothing - it is a lead, filed and left alone until a report
    or the operator decides it is worth anything.
    """
    return (
        "If, while doing this, you notice something worth building or fixing that is NOT "
        "part of this task - a feature nobody has asked for yet, a goal that obviously "
        "wants opening, a piece of this that is quietly wrong - write one line for each, "
        "each beginning `IDEA:` on its own line. One sentence, concrete, and say where you "
        "saw it. Do not act on any of them and do not widen your task by even one file: "
        "they are noted for later and nothing more. This is optional and most reports will "
        "have none - an invented idea costs more than a missing one."
    )


def goal_dispatch(gid: str) -> dict:
    """Send the next task out. One worker per goal at a time, by construction."""
    g = load_goal(gid)
    if g.get("state") not in ("running",):
        return g
    if any(t.get("state") == "running" for t in g["tasks"]):
        return g                                   # one is already out
    nxt = next((t for t in g["tasks"] if t.get("state") == "todo"), None)
    if not nxt:
        return goal_review(gid, "every task in the list is finished")
    if g.get("rounds", 0) >= GOAL_MAX_ROUNDS:
        return _goal_stop(gid, "rounds")
    if g.get("cost_tokens", 0) >= GOAL_TOKEN_CEILING:
        return _goal_stop(gid, "tokens")
    if not ON_GOAL_DISPATCH:
        return _goal_stop(gid, "no-dispatch")
    with _GOAL_LOCK:
        g = load_goal(gid)
        for t in g["tasks"]:
            if t["id"] == nxt["id"]:
                t["state"] = "running"
        save_goal(g)
    try:
        out = ON_GOAL_DISPATCH(g, nxt)
    except Exception as e:
        print(f"amp: goal dispatch hook failed: {e}", file=sys.stderr)
        out = {"ok": False, "error": str(e)}
    if not out.get("ok"):
        with _GOAL_LOCK:
            g = load_goal(gid)
            for t in g["tasks"]:
                if t["id"] == nxt["id"]:
                    t["state"] = "todo"
            save_goal(g)
        return _goal_stop(gid, "dispatch-failed", str(out.get("error") or "")[:200])
    return goal_log(gid, ("queued" if out.get("queued") else "dispatched")
                    + f" a worker for: {nxt['text'][:200]}",
                    task_id=out.get("task_id"), goal_task=nxt["id"])


def run_check(cmd: str, cwd: Path) -> tuple[int | None, str]:
    """Run one done-item's check. Returns (exit code or None if it timed out, output).

    Not `subprocess.run(..., timeout=)`, because that kills only the SHELL. A
    check that starts anything outliving it - a dev server, a watcher, a build
    daemon - leaves those running in the worktree after the timeout, and they
    accumulate one per timed-out check, all holding the same lane's tree open.
    Measured on `sleep N & sleep N` with a 3s ceiling: the old path leaves two
    processes behind, this one leaves none.

    RETRACTED: this said the old path would also HANG FOREVER after the kill,
    because its second drain has no timeout of its own and a grandchild still
    holds the pipe. That is what the stdlib code reads like, but four attempts
    to reproduce it (`setsid sleep`, a detached writer, `nohup`) all raised
    `TimeoutExpired` on schedule. The leak is measured; the hang is not, and was
    never observed in this repo.

    So: own process group, kill the GROUP, and give the drain a deadline of its
    own. A check that will not die is reported as a check that would not die.
    """
    p = subprocess.Popen(cmd, shell=True, cwd=cwd, text=True, start_new_session=True,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         env={**os.environ, **claude_env()})
    try:
        body, _ = p.communicate(timeout=GOAL_CHECK_TIMEOUT)
        return p.returncode, (body or "").strip()
    except subprocess.TimeoutExpired:
        pass
    for sig, grace in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        try:
            os.killpg(p.pid, sig)
        except OSError:
            pass
        try:
            body, _ = p.communicate(timeout=grace)
            return None, (f"(no result after {GOAL_CHECK_TIMEOUT}s - timed out)\n"
                          + (body or "").strip())
        except subprocess.TimeoutExpired:
            continue
    # Killing the group did not free the pipe, so something outside it holds the
    # write end. Reading further would block for good; say so and let go.
    return None, (f"(no result after {GOAL_CHECK_TIMEOUT}s - timed out, and it "
                  f"survived SIGKILL of its process group with the pipe still "
                  f"held. Output is unreadable; treat this check as unrun.)")


def run_goal_checks(gid: str, only: set[str] | None = None) -> list[dict]:
    """Run every done-item's check in the lane worktree. Exit code is the answer.

    `only` narrows it to the named conditions. There is one caller: a check that
    has just been written needs running to find out whether it says anything,
    and re-running the forty that already passed to learn that would cost
    minutes and answer a question nobody asked. The write-back below already
    ignores conditions it has no result for, so a narrowed run cannot clear the
    dates on the ones it skipped.
    """
    g = load_goal(gid)
    wt = WORKTREE_DIR / g["lane"]
    if not wt.exists():
        return []
    out = []
    for d in g["done"]:
        cmd = d.get("check")
        if not cmd or (only is not None and d["text"] not in only):
            continue
        try:
            code, body = run_check(cmd, wt)
            out.append({"text": d["text"], "check": cmd, "exit": code,
                        "output": body[-1500:]})
        except OSError as e:
            out.append({"text": d["text"], "check": cmd, "exit": None,
                        "output": f"(could not run: {e})"})
    with _GOAL_LOCK:
        g = load_goal(gid)
        by_text = {c["text"]: c for c in out}
        for d in g["done"]:
            c = by_text.get(d["text"])
            if not c:
                continue
            # A check is the only thing here that outranks a judgement, in both
            # directions: passing is not evidence of intent, but failing is proof
            # the item is not met, whatever anyone reported.
            d["check_exit"] = c["exit"]
            d["check_at"] = now()
            if c["exit"] != 0:
                d["met"] = False
        save_goal(g)
    return out


def goal_state_doc(g: dict, checks: list[dict], report: str) -> str:
    dod = []
    by_text = {c["text"]: c for c in checks}
    for d in g["done"]:
        line = f"- [{'x' if d.get('met') else ' '}] {d['text']}"
        if d.get("check"):
            c = by_text.get(d["text"])
            if c:
                line += (f"\n      check `{c['check']}` -> exit {c['exit']}\n"
                         f"      output: {c['output'][:800] or '(nothing)'}")
            else:
                line += f"\n      check `{d['check']}` (not run this round)"
        if d.get("evidence"):
            line += f"\n      previously: {d['evidence'][:300]}"
        dod.append(line)
    tasks = "\n".join(f"- [{t.get('state')}] {t['text']}" for t in g["tasks"])
    return (f"# Objective\n\n{g['objective']}"
            + mission_block()
            + doctrine_block("What this work is held to")
            + f"\n\n# Definition of done\n\n" + "\n".join(dod) + "\n\n"
            f"# Tasks\n\n{tasks}\n\n"
            f"# What just came back\n\n{report[:20000]}")


def mark_reviewing(gid: str, on: bool):
    """Stamp who is judging this goal right now, so an idle one can be told apart.

    Between "the worker finished" and "reviewed:" a goal has no running task and
    unfinished tasks - which is indistinguishable, from the outside, from a goal
    that was dropped mid-review by a console restart. The distinguishing fact is
    whether the process that took the review is still this one. Elapsed time
    cannot answer it: a review runs every done-item's check, and eight checks at
    the 180s ceiling is longer than any timeout worth waiting.
    """
    with _GOAL_LOCK:
        g = load_goal(gid)
        if not g:
            return
        g["reviewing_pid"] = os.getpid() if on else None
        g["reviewing_since"] = now() if on else None
        save_goal(g)


def goal_review(gid: str, report: str) -> dict:
    """Judge what came back against the definition of done, then keep going."""
    mark_reviewing(gid, True)
    try:
        return _goal_review(gid, report)
    finally:
        mark_reviewing(gid, False)


def _goal_review(gid: str, report: str) -> dict:
    g = load_goal(gid)
    if g.get("cost_tokens", 0) >= GOAL_TOKEN_CEILING:
        return _goal_stop(gid, "tokens")
    if g.get("rounds", 0) >= GOAL_MAX_ROUNDS:
        return _goal_stop(gid, "rounds")
    checks = run_goal_checks(gid)
    g = load_goal(gid)
    verdict, raw = _goal_chat(g, GOAL_REVIEW_SYSTEM, goal_state_doc(g, checks, report))
    if not verdict:
        goal_log(gid, "the architect's review could not be read as JSON", raw=raw[:2000])
        return _goal_stop(gid, "no-plan", "its review was not readable")

    with _GOAL_LOCK:
        g = load_goal(gid)
        stated = {str(d.get("text") or "").strip(): d for d in (verdict.get("done") or [])}
        for d in g["done"]:
            said = stated.get(d["text"])
            if not said:
                continue
            met = bool(said.get("met"))
            if d.get("check") and d.get("check_exit") not in (0, None):
                met = False           # the command already answered this
            d["met"] = met
            if said.get("evidence"):
                d["evidence"] = str(said["evidence"])[:600]
        # Task list: keep ids for the ones we already know, append the rest.
        known = {t["text"]: t for t in g["tasks"]}
        fresh = []
        for t in (verdict.get("tasks") or []):
            text = str(t.get("text") or "").strip()
            if not text:
                continue
            state = t.get("state") if t.get("state") in ("todo", "done", "dropped") else "todo"
            if text in known:
                old = known.pop(text)
                # A worker is out on this one; a review does not get to retire it.
                if old.get("state") != "running":
                    old["state"] = state
                fresh.append(old)
            else:
                fresh.append({"id": "t" + uuid.uuid4().hex[:6], "text": text, "state": state})
        for leftover in known.values():
            if leftover.get("state") == "running":
                fresh.append(leftover)
        g["tasks"] = fresh
        g["questions"] = [q for q in (verdict.get("questions") or []) if str(q).strip()]
        g["last_verdict"] = {"verdict": verdict.get("verdict"), "why": verdict.get("why"),
                             "at": now()}
        save_goal(g)

    why = str(verdict.get("why") or "")[:400]
    goal_log(gid, f"reviewed: {verdict.get('verdict')} - {why}")

    d = verdict.get("doctrine")
    if isinstance(d, dict):
        f = record_finding(str(d.get("bearing") or ""), str(d.get("text") or ""),
                           lane=g.get("lane"), source="architect", goal_id=gid)
        if f:
            goal_log(gid, f"doctrine: {f['bearing']} - {f['text'][:300]}")

    met = all(d.get("met") for d in g["done"]) and bool(g["done"])
    if verdict.get("verdict") == "done" and met:
        with _GOAL_LOCK:
            g = load_goal(gid)
            g["state"], g["stopped_on"] = "done", None
            save_goal(g)
        add_note(f"goal {gid} ({g['lane']}) is finished: {g['objective'][:200]}",
                 lane=g.get("lane"))
        # And immediately ask what it was for. This is the only moment the whole
        # goal is in front of the architect with its evidence attached, which is
        # what makes "is there anywhere left to go here" answerable rather than a
        # guess. It must not be able to unfinish the goal: a review that throws
        # loses a proposal, not a completion.
        try:
            direction_review(gid, auto=True)
        except Exception as e:                                    # noqa: BLE001
            goal_log(gid, f"the direction review failed ({e}); the goal is still finished")
        return load_goal(gid) or g
    if verdict.get("verdict") == "done" and not met:
        # Said done, is not done. The definition of done is the authority here.
        unmet = [d["text"] for d in g["done"] if not d.get("met")]
        goal_log(gid, "the architect called it done while these are still unmet: "
                      + "; ".join(unmet)[:400])
    if verdict.get("verdict") == "blocked" or g["questions"]:
        return _goal_stop(gid, "operator",
                          "It asks: " + "; ".join(g["questions"])[:400] if g["questions"] else why)
    return goal_dispatch(gid)


def idle_goals() -> list[dict]:
    """Goals that are running, have work left, and have nobody doing any of it.

    This is the shape every silent halt has taken. A goal stops moving because
    the step that was going to move it - a review, a dispatch - was interrupted,
    and nothing afterwards asks whether anyone is still on it. The goal keeps
    saying `running`, the board shows no worker, and the whole thing reads as
    healthy right up until you count the workers and find none.

    Deliberately NOT time-based. A goal is idle when no task is out and no live
    process is judging it, both of which are facts rather than estimates.

    The liveness test has to come FIRST. This used to skip any goal with no
    `todo` task before ever asking who was on it, on the reasoning that a review
    would close it - and that is true exactly while a review is still running.
    A goal reaches "no running task, no todo task" in only two ways: it is being
    reviewed right now, or its review was interrupted. The second one is the
    halt, and it was the one case the guard threw away, so the goal sat in
    `running` with every task done and nothing left alive to close it. `prism`
    sat like that for two hours behind a `reviewing_pid` whose process was gone.

    A goal with nothing left to send is not a problem for the caller either:
    `goal_dispatch` finds no `todo`, and resumes the interrupted review itself.
    """
    out = []
    for row in goals():
        if row.get("state") != "running":
            continue
        g = load_goal(row["id"])
        if not g:
            continue
        if any(t.get("state") == "running" for t in g.get("tasks") or []):
            continue
        pid = g.get("reviewing_pid")
        if pid and pid == os.getpid():
            continue                      # this process is judging it right now
        if pid and pid_alive(pid):
            continue                      # some other live console has it
        if g.get("hold_until") and g["hold_until"] > now():
            continue                      # waiting out a capacity limit
        out.append(g)
    return out


# How long a `planning` goal with no recorded planner is left alone before it is
# read as abandoned. Only ever applies to records opened before `planning_pid`
# was written down - for anything opened since, liveness answers it and no clock
# is consulted at all. Generous because the cost of being wrong is asymmetric: a
# reaped plan that was still coming is work thrown away, and one architect call
# has never taken half an hour.
PLAN_GRACE_MIN = 30.0


def stranded_plans() -> list[dict]:
    """Goals whose plan never arrived and whose planner is gone.

    The same halt `idle_goals` catches, one state earlier, and it was invisible
    for the same reason: everything that looks for stuck work looks at goals
    that say `running`. `planning` is a state a goal passes through in about
    twenty seconds, so nothing was written to handle a goal that stopped in it -
    and `plan_goal` is a single architect call, so a console restart in the
    middle of one leaves the record exactly as `open_goal` wrote it, for ever.

    A `wrl` goal sat like that for an hour and twenty minutes while its lane
    held four proposals nobody could adopt. Its twin, opened two minutes later
    by the retry, planned fine - so the objective was never lost, only a lane
    was, and no gate says a word about it because the board reads it as a goal
    being planned right now.

    Two facts have to hold, and the second is the one that makes this safe to
    do without asking. The planner is not alive: a pid that is gone, checked the
    way `idle_goals` checks its reviewer. And the record CONTAINS NOTHING - no
    done-conditions, no tasks, no log, no tokens spent. That is not "it looks
    stale", it is the record saying in its own fields that nothing ever
    happened, and it is why abandoning one discards no work. A `planning` goal
    that does hold something is left alone and shows up stuck, for a person.
    """
    out = []
    for row in goals():
        if row.get("state") != "planning":
            continue
        g = load_goal(row["id"])
        if not g:
            continue
        pid = g.get("planning_pid")
        if pid == os.getpid():
            continue                      # this process is planning it right now
        if pid and pid_alive(pid):
            continue                      # some other live console has it
        if not pid and (not g.get("opened_at")
                        or _active_recently(g["opened_at"], PLAN_GRACE_MIN)):
            continue                      # opened before pids were recorded, still young
        if (g.get("done") or g.get("tasks") or g.get("log")
                or g.get("questions") or g.get("cost_tokens")):
            continue                      # it holds something; that is a person's call
        out.append(g)
    return out


def reap_stranded_plans() -> list[str]:
    """Free the lanes held by plans that never arrived.

    Not a repair: it does not re-plan. Re-planning would put a second goal into
    a lane that may since have got one - which is how the stranded record came
    to exist in the first place, a retry landing beside its own corpse - and the
    objective is not lost by leaving it alone, because a lane with no goal and
    no proposal is exactly what `explore_idle_lanes` goes looking for.

    What it does is stop an empty record from occupying a worktree.
    """
    out = []
    for g in stranded_plans():
        goal_log(g["id"], "the plan never arrived and the planner is gone - "
                          "abandoned so the lane is not held by an empty record")
        close_goal(g["id"], "abandoned")
        out.append(f"{g['lane']} {g['id']}")
    return out


# ------------------------------------------------------- waiting out a limit
#
# There are two reasons a worker can fail, and treating them the same is what
# takes the whole fleet down at once. A worker that failed AT THE WORK left
# something behind to judge, and the architect spends a round judging it. A
# worker that never got to start - the subscription window is spent, the API is
# overloaded - left nothing, and there is nothing to judge.
#
# The second kind arrives in every lane within seconds of the first, because
# they all draw on one account. Reviewing those failures costs a round per goal
# per attempt, and rounds are capped, so a limit lasting an hour ends with every
# goal stopped on `too many rounds` and no worker left running. Then the limit
# lifts and nothing restarts, because nothing is left to restart. That is the
# day-scale halt: the fleet does not crash, it spends itself out against a wall.
#
# So a capacity failure costs no round and no judgement. The task goes back to
# `todo`, the goal is held for a while, and the heartbeat picks it up when the
# hold expires - which is what a person would have done.

CAPACITY_MARKERS = (
    "usage limit", "rate limit", "rate_limit", "429", "529",
    "overloaded", "quota", "try again later", "temporarily unavailable",
)
CAPACITY_HOLD_S = 600


def capacity_problem(rec: dict) -> str | None:
    """Whether a worker failed for want of capacity rather than at the work."""
    if rec.get("status") not in ("failed", "cancelled"):
        return None
    # `error` and `killed` only, never the worker's own prose. A worker that
    # merely WROTE about rate limits has not hit one, and the same mistake -
    # scanning a report for markers the report is allowed to discuss - already
    # cost us once in `diagnose_session`.
    blob = " ".join(str(rec.get(k) or "") for k in ("error", "killed")).lower()
    return next((m for m in CAPACITY_MARKERS if m in blob), None)


def goal_hold(gid: str, seconds: int, why: str):
    """Put a goal down for a while without spending anything on it."""
    with _GOAL_LOCK:
        g = load_goal(gid)
        if not g:
            return
        for t in g["tasks"]:
            if t.get("state") == "running":
                t["state"] = "todo"       # it never ran; it is still owed
        g["hold_until"] = ts_in(seconds)
        save_goal(g)
    goal_log(gid, f"held {seconds // 60}m: {why}")


def ts_in(seconds: int) -> str:
    """A `now()`-shaped stamp `seconds` from now, so the two compare as strings."""
    return (datetime.now(timezone.utc)
            + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, ValueError, TypeError):
        return False
    return True


def goal_worker_done(gid: str, lane_name: str, rec: dict) -> dict:
    """A goal's worker stopped. Mark its task, then judge what it produced."""
    g = load_goal(gid)
    if not g:
        return {}
    task_id = rec.get("goal_task")
    limit = capacity_problem(rec)
    if limit:
        goal_hold(gid, CAPACITY_HOLD_S,
                  f"{limit} - the worker never started, so there is nothing to judge")
        return load_goal(gid) or {}
    killed = rec.get("killed") or (rec.get("status") == "failed")
    with _GOAL_LOCK:
        g = load_goal(gid)
        for t in g["tasks"]:
            if t["id"] == task_id or (not task_id and t.get("state") == "running"):
                t["state"] = "blocked" if killed else "done"
                t["task_id"] = rec.get("task_id")
                t["note"] = (rec.get("error") or "")[:300] if killed else ""
        save_goal(g)
    report = worker_report(lane_name, rec)
    if killed:
        report = (f"This worker did not finish: {rec.get('error') or 'it was stopped'}.\n"
                  f"Whatever is in the worktree below is as far as it got. Judge that, and "
                  f"if the task needs finishing, say so as a task - it can be resumed.\n\n"
                  + report)
    goal_log(gid, f"worker {str(rec.get('task_id'))[:8]} "
                  + ("was stopped" if killed else "finished") + f" ({rec.get('status')})",
             task_id=rec.get("task_id"))
    if not architect_available():
        # The task state above is already saved; only the architect's judgement
        # of it is skipped. Said in the goal's own log, because that is where
        # someone wondering why nothing happened next will be looking.
        goal_log(gid, f"not reviewed: {architect_off_reason().lower()}",
                 task_id=rec.get("task_id"))
        return load_goal(gid) or {}
    return goal_review(gid, report)


def answer_goal(gid: str, text: str) -> dict:
    """You answering is the thread moving for a real reason: it gets its rounds back."""
    with _GOAL_LOCK:
        g = load_goal(gid)
        if not g:
            die(f"no goal {gid!r}")
        g["questions"] = []
        g["stopped_on"] = None
        g["state"] = "running"
        g["rounds"] = 0
        # These questions are answered, so the triage that read them is spent.
        # Left set, a goal that stops again later - on something else entirely -
        # is treated as already looked at and nobody ever looks.
        g["triaged_at"] = None
        save_goal(g)
    goal_log(gid, f"you answered: {text[:300]}")
    return goal_review(gid, f"The operator answers your questions:\n\n{text}")


# --------------------------------------------------- getting asked, and answering
#
# A goal that cannot go on without a decision stops and says what it needs. That
# much always worked. What did not is everything after: the question went into
# the feed as one truncated line among the dispatches, nothing ever looked at it
# again, and the lane stayed stopped until somebody happened to scroll past it.
# Four goals were sitting like that at once.
#
# Two things fix it, and they are different things.
#
# The first is that the question belongs in the thread AS A QUESTION - every
# part of it, with the goal it came from, and somewhere to type the answer. That
# is `blocked_questions()`, derived from the board like the rest of the feed so
# it cannot drift from it.
#
# The second is triage, and it is where the care goes. Doctrine rule 6 names
# what is Travis's: what to build, what counts as good enough, what to spend,
# what to publish, what to entrench. Those are most of what a stopped goal asks
# about, and a harness that answered them would be a harness that had quietly
# taken the decisions the escalation floor exists to protect. So triage is
# ALLOWED TO ANSWER ONLY FROM EVIDENCE - what the repository, a prior ruling or
# a spec already says - and is required to hand everything else back with the
# choice stated plainly enough to answer in a word. Reading it out is worth a
# lot on its own: a goal asking three questions of which one is a matter of
# record is a goal that stops for one decision instead of three.

TRIAGE_RULES = """A goal has stopped because it asked for a decision. Deal with it now.

Goal {gid} in lane {lane}. Its objective:

{objective}

It asks:

{questions}

Take each question separately and put it in exactly one of two piles.

EVIDENCE - the answer is already recorded somewhere you can read: in this
repository, in a spec under docs/spec/, in an architect ruling, in a commit, in
a prior finding. Go and read it, and answer with what it says and where you read
it. "The spec at X says Y" is an answer. "I think Y" is not - if you are
reasoning rather than reading, it is not this pile.

TRAVIS - doctrine rule 6: what to build, what counts as good enough, what to
spend, what to publish, what to entrench. Anything needing money, credentials,
an account, a deployment target, or permission is his by definition. So is any
question whose honest answer is a preference. You must not answer these, and you
must not talk the goal into a smaller version of the same decision.

If every question is EVIDENCE, answer them all together:

  curl -s -X POST {base}/api/goal/answer \\
       -H 'content-type: application/json' \\
       -d '{{"goal_id":"{gid}","text":"..."}}'

That restarts the goal, so only do it when you have actually read the answers.

If any question is TRAVIS, do not call that endpoint at all - a partial answer
restarts the goal with the real decision still unmade. Instead reply to him
here, in under 80 words: the lane, the one decision, the options as you
understand them, and what you already settled from evidence so he is not asked
that part. If you found the evidence pile is empty, say that too.

Reply with what you did, not with a plan to do it."""


def operator_blocked_goals() -> list[dict]:
    """Goals stopped because they asked something only a person can settle.

    The counterpart to `budget_stopped`. Both read `blocked`; one is a goal that
    ran out of budget, this one is a goal waiting for an answer, and they need
    different things done to them.
    """
    return [r for r in goals()
            if r.get("state") == "blocked" and r.get("stopped_on") == "operator"]


def blocked_questions() -> list[dict]:
    """Every outstanding question, whole, with the goal that asked it."""
    out = []
    for row in operator_blocked_goals():
        g = load_goal(row["id"])
        if not g or not g.get("questions"):
            continue
        out.append({"goal_id": g["id"], "lane": g.get("lane"),
                    "at": g.get("updated_at") or g.get("opened_at"),
                    "objective": g.get("objective") or "",
                    "questions": list(g["questions"]),
                    "triaged_at": g.get("triaged_at")})
    return out


def mark_triaged(gid: str):
    with _GOAL_LOCK:
        g = load_goal(gid)
        if g:
            g["triaged_at"] = now()
            save_goal(g)


def triage_blocked_goals(base_url: str) -> list[str]:
    """Hand ONE newly stopped goal to the orchestrator, and only one.

    Marked BEFORE the call, not after. A turn that dies partway through has
    still spent a budget and may still have answered the goal, and a retry loop
    around a model call is how a stopped lane becomes an expensive stopped lane.
    Once, or not at all.

    One per call rather than all of them, for two reasons that both bite. The
    orchestrator is a single conversation, so two turns at once is not a queue
    but a corrupted thread. And this blocks its caller: four goals stopped
    together - which is what actually happened - would be four opus turns back
    to back, holding the heartbeat for minutes and spending four budgets before
    anyone saw the first answer. At one a minute the backlog still clears in
    four, and the first reply arrives while the rest are still waiting.
    """
    if not (config().get("autonomy") or {}).get("triage", True):
        return []
    if orch_busy():
        return []
    row = next((r for r in blocked_questions() if not r.get("triaged_at")), None)
    if not row:
        return []
    gid = row["goal_id"]
    mark_triaged(gid)
    try:
        orchestrator_ask(
            TRIAGE_RULES.format(
                gid=gid, lane=row["lane"], base=base_url,
                objective=row["objective"][:1500],
                questions="\n".join(f"{i}. {q}" for i, q in enumerate(row["questions"], 1))),
            base_url=base_url, role="harness")
    except Exception as e:
        goal_log(gid, f"triage failed: {e}")
        return []
    return [gid]


def close_goal(gid: str, state: str = "abandoned") -> dict:
    with _GOAL_LOCK:
        g = load_goal(gid)
        if not g:
            die(f"no goal {gid!r}")
        g["state"] = state
        g["stopped_on"] = None
        save_goal(g)
    return g


# ------------------------------------------------------------------ publishing
#
# The end of the pipeline. Work finishes on `amp/<lane>` inside a worktree and
# until now stopped there - nothing in this harness has ever run `git push`.
#
# What this does NOT do is merge. A pull request is a handoff, not a decision.
# The ladder's top rungs are somebody's judgement about evidence, and a program
# that merged its own work would be awarding itself the rung it is supposed to
# be making a case for. So: push the branch, open the PR, write down what the
# evidence actually was, and stop.
#
# The gate is the goal's own definition of done, read the way the doctrine says
# to read a claim. A condition whose check PASSED is evidence. A condition with
# no check at all, or one whose check has never been run, is a claim at rung
# `spec` wearing the costume of `in_tree`, and it does not open a PR.
#
# The checks are re-run here rather than trusting the exit codes already stored
# on the goal. Those say the check passed once, against a tree that has since
# had more commits written into it - which is not the same as passing on what is
# about to be pushed, and the difference is the entire reason to have a gate.

PUBLISH_REMOTE = "origin"


def _git(wt: Path, *args: str) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(wt), *args])


def publish_report(gid: str, *, rerun: bool = True) -> dict:
    """Decide whether a goal's work may be published, and say why not if not.

    Pure inspection plus the checks themselves - it pushes nothing and opens
    nothing. `publish_goal` calls this first and refuses on any blocker, so the
    dry run an operator reads and the gate the code enforces are the same code
    rather than two descriptions of one intention that drift apart.
    """
    g = load_goal(gid)
    if not g:
        return {"ok": False, "blocked": [f"no goal {gid!r}"]}
    lane = g["lane"]
    wt = WORKTREE_DIR / lane
    cfg = config()["lanes"].get(lane) or {}
    base = cfg.get("branch") or "main"
    branch = f"amp/{lane}"
    rep: dict = {
        "ok": False, "goal_id": gid, "lane": lane, "branch": branch, "base": base,
        "objective": (g.get("objective") or "").split("\n\n")[0],
        "worktree": str(wt), "blocked": [], "conditions": [], "commits": [],
        "repo": None, "ahead": 0, "dirty": 0, "pr_url": g.get("pr_url"),
    }
    if not wt.exists():
        rep["blocked"].append(f"no worktree at {wt} - nothing was ever built here")
        return rep

    r = _git(wt, "remote", "get-url", PUBLISH_REMOTE)
    if r.returncode != 0:
        rep["blocked"].append(f"lane {lane} has no {PUBLISH_REMOTE} remote")
    else:
        rep["repo"] = r.stdout.strip()

    # Uncommitted work is work nobody has reviewed and the PR would not contain.
    # Publishing around it would produce a pull request that is quietly not the
    # thing that was tested.
    dirty = [l for l in _git(wt, "status", "--porcelain").stdout.splitlines() if l.strip()]
    rep["dirty"] = len(dirty)
    if dirty:
        rep["blocked"].append(
            f"{len(dirty)} uncommitted file(s) in the worktree - the PR would not "
            f"contain them, so what gets reviewed would not be what was checked")

    ahead = _git(wt, "rev-list", "--count", f"{base}..{branch}")
    rep["ahead"] = int(ahead.stdout.strip() or 0) if ahead.returncode == 0 else 0
    if not rep["ahead"]:
        rep["blocked"].append(f"{branch} is not ahead of {base} - there is nothing to publish")
    else:
        rep["commits"] = _git(
            wt, "log", "--no-merges", "--format=%h %s", f"{base}..{branch}"
        ).stdout.strip().splitlines()

    if g.get("state") != "done":
        rep["blocked"].append(
            f"goal is {g.get('state')!r}, not done - publish it when it has finished "
            f"making its case, or close it and publish the lane by hand")

    # Only once nothing else has already ruled it out. Re-running the checks is
    # the expensive half of this function and it runs commands - `npm test` and
    # the like - inside the lane worktree. If the goal is still running there is
    # a worker writing to that tree right now, and racing a build against it
    # would be both a wrong answer and a way to break somebody else's work.
    if rerun and not rep["blocked"]:
        run_goal_checks(gid)
        g = load_goal(gid) or g
    else:
        # Set wherever the checks were NOT re-run, which now includes the case a
        # caller asked for. It used to be set only when a blocker had already
        # skipped them, so `rerun=False` - the cheap path the pull-requests tab
        # reads every lane through - came back indistinguishable from a report
        # whose checks had just passed. Exit codes with no date on them are the
        # thing this whole gate exists to distrust.
        rep["stale_checks"] = True
    dod = g.get("done") or []
    if not dod:
        rep["blocked"].append("this goal has no definition of done, so there is nothing to gate on")
    for d in dod:
        cond = {"text": d.get("text"), "check": d.get("check"),
                "exit": d.get("check_exit"), "met": bool(d.get("met")),
                "waived": d.get("waived"), "uncheckable": d.get("uncheckable")}
        if not d.get("check"):
            cond["verdict"] = "no check - judgement only, rung `spec`"
        elif d.get("check_exit") is None:
            cond["verdict"] = "check has never run"
        elif d.get("check_exit") == 0:
            cond["verdict"] = "passed"
        else:
            cond["verdict"] = f"failed, exit {d.get('check_exit')}"
        rep["conditions"].append(cond)
    # A waiver does not make a condition pass. It is a named person taking
    # responsibility for one that did not, and it stops the gate rather than
    # satisfying it - which is why `unproven` counts it and `blocked` does not.
    # The distinction is the whole point: the verdict on the row still says what
    # actually happened, the PR body says who waived it and why, and nothing
    # anywhere claims a check passed. See `waive_condition` for who may do this.
    unproven = [c for c in rep["conditions"] if c.get("verdict") != "passed"]
    standing = [c for c in unproven if not c.get("waived")]
    rep["waived"] = [c for c in unproven if c.get("waived")]
    if standing:
        rep["blocked"].append(
            f"{len(standing)} of {len(rep['conditions'])} done-conditions are not "
            f"backed by a check that passed just now")

    rep["ok"] = not rep["blocked"]
    return rep


def publish_body(rep: dict, g: dict) -> str:
    """The pull request description: the case, and the evidence for it.

    Written from the goal rather than from the diff on purpose. A reviewer can
    already read the diff; what they cannot recover from it is which conditions
    this work was supposed to satisfy and which command was run to decide that
    it had.
    """
    lines = [(g.get("objective") or "").strip(), "",
             "## What had to be true", ""]
    for c in rep["conditions"]:
        lines.append(f"- [{'x' if c['met'] else ' '}] {c['text']}")
        if c.get("check"):
            lines.append(f"  - `{c['check']}` &rarr; **{c['verdict']}**")
        else:
            lines.append(f"  - {c['verdict']}")
        # Named here, on the condition itself, and not gathered into a footnote.
        # A waiver is the one thing in this description a reviewer must not miss,
        # and a list at the bottom is a list nobody reads next to the item it is
        # about.
        if c.get("waived"):
            w = c["waived"]
            lines.append(f"  - :warning: **waived by {w.get('by', 'the operator')}** "
                         f"on {str(w.get('at', ''))[:10]} &mdash; {w.get('why') or 'no reason given'}")
    if rep.get("waived"):
        lines += ["", f"> **{len(rep['waived'])} of these {len(rep['conditions'])} conditions "
                      f"were not proven by a check.** They were waived by hand so this could be "
                      f"handed over. Nothing automated decided they were satisfied."]
    tasks = [t for t in (g.get("tasks") or []) if t.get("state") == "done"]
    if tasks:
        lines += ["", "## What was done", ""] + [f"- {t['text']}" for t in tasks]
    lines += [
        "", "## How this was produced", "",
        f"An `amp` worker on branch `{rep['branch']}`, {rep['ahead']} commit(s) "
        f"ahead of `{rep['base']}`. The conditions above were re-checked "
        f"immediately before this branch was pushed, in the worktree the work "
        f"was done in.",
        "",
        "Nothing merged this. The harness opens the pull request and stops there "
        "deliberately: whether this is good enough to land is a judgement, and "
        "the harness is not the one making it.",
    ]
    return "\n".join(lines)


def publish_goal(gid: str, *, dry_run: bool = True) -> dict:
    """Push the lane branch and open a pull request. Never merges."""
    rep = publish_report(gid)
    rep["dry_run"] = dry_run
    rep["pushed"] = False
    if not rep["ok"] or dry_run:
        return rep

    g = load_goal(gid)
    wt = Path(rep["worktree"])
    p = _git(wt, "push", "-u", PUBLISH_REMOTE, rep["branch"])
    if p.returncode != 0:
        rep["ok"] = False
        rep["blocked"].append(f"push failed: {(p.stderr or p.stdout).strip()[-600:]}")
        return rep
    rep["pushed"] = True

    title = f"{rep['objective'][:70]}" + ("…" if len(rep["objective"]) > 70 else "")
    pr = run(["gh", "pr", "create", "--base", rep["base"], "--head", rep["branch"],
              "--title", title, "--body", publish_body(rep, g)], cwd=wt)
    out = (pr.stdout + pr.stderr).strip()
    if pr.returncode != 0:
        # The branch is up either way, which is worth saying plainly rather than
        # reporting a clean failure - the push is not rolled back.
        rep["ok"] = False
        rep["blocked"].append(f"branch pushed, but opening the PR failed: {out[-600:]}")
        return rep
    url = next((w for w in out.split() if w.startswith("https://")), out)
    rep["pr_url"] = url
    with _GOAL_LOCK:
        g = load_goal(gid)
        g["pr_url"] = url
        g["published_at"] = now()
        g.setdefault("log", []).append({"at": now(), "text": f"opened a pull request: {url}"})
        save_goal(g)
    return rep


# --------------------------------------------------------------- pull requests
#
# The handoff, seen across every lane at once instead of one goal at a time.
#
# It was already possible to publish a goal - the button has been on the goal
# panel for weeks. What was not possible was noticing that nobody ever had. The
# first reading of this view answered that: NINETEEN goals finished, ZERO pull
# requests, ever. Every one of those is work that ran, passed its own checks,
# and stopped inside a worktree on this machine, and no screen in the console
# said so, because each goal panel showed one goal and each one looked fine.
#
# So the tab is built around the two questions a per-goal view structurally
# cannot answer:
#
#   what is finished and has never been handed to anyone
#   what has been handed over and is not being tested on the other side
#
# The second is the user's sentence - "make sure tests and benchmarks run or are
# made" - and it has two halves that are easy to confuse. On the GitHub side, a
# pull request with an empty check rollup is one nothing is testing; and a
# rollup that is entirely SKIPPED renders as a green tick while having run
# nothing at all, which is worse than red. Both are named here as what they are.
#
# On our side, "or are made" is the harder half. A done-condition with no check
# is a claim at rung `spec` wearing the costume of `in_tree` - the gate already
# refuses to publish on one, correctly, and until now that was the end of it:
# thirty-one such conditions sat across the board with nothing that could move
# them. `write_checks` asks the architect for the missing command, which is the
# only move here that produces new evidence rather than new prose - a command
# that runs is an event, and its exit code is a fact about the repository. What
# it must never become is a way to paint a tick on a condition nobody checked,
# so: a command that cannot fail is refused before it is stored, the architect
# is allowed to answer that no command can decide a condition, and a check
# written this way carries who wrote it for the rest of its life.
#
# And `waive_condition`, which is the pressure valve. A gate with no exit is a
# gate people walk around outside the tool, and then the record is gone. A
# waiver does not make a condition pass - the verdict on the row still says
# exactly what happened - it records a person taking responsibility for handing
# over work that was not proven, and it prints that, in those words, in the pull
# request body where the reviewer will see it.
#
# Nothing here merges anything, and there is no button that could. See the
# comment above `publish_report`: the harness makes the case and stops.

GH_PR_FIELDS = ("number,title,url,isDraft,headRefName,baseRefName,createdAt,"
                "statusCheckRollup")
GH_TIMEOUT = 45


def pr_repos() -> list[dict]:
    """One entry per distinct repository the lanes point at.

    Keyed on the repository and not on the lane, because two lanes can be two
    pieces of work in ONE repository - `trvm` and `wrlm` are both c-u-l8er/TRVM
    - and asking GitHub once per lane would list every pull request twice, each
    copy filed under a different lane, as though there were two of them.
    """
    by_repo: dict[str, dict] = {}
    for name, cfg in sorted((config().get("lanes") or {}).items()):
        repo = (cfg or {}).get("repo")
        if not repo:
            continue
        d = by_repo.setdefault(repo, {"repo": repo, "lanes": [],
                                      "dir": str((ROOT / (cfg.get("path") or ".")).resolve())})
        d["lanes"].append(name)
    return sorted(by_repo.values(), key=lambda r: r["repo"])


def _check_state(item: dict) -> str:
    """One rollup entry, as one of four words.

    GitHub returns two different shapes through the same list - a CheckRun
    carries `status` and `conclusion`, a StatusContext carries `state` - and
    reading only one of them silently drops half of somebody's CI.

    SKIPPED is kept apart from SUCCESS deliberately. GitHub renders a skipped
    job with the same green tick as a passing one, and a workflow whose every
    job was skipped by a path filter is a pull request that has been tested by
    nothing while looking fully green. That is the exact failure this tab is
    supposed to catch, so it cannot be folded into the word `passed`.
    """
    status = (item.get("status") or "").upper()
    if status and status != "COMPLETED":
        return "pending"
    v = (item.get("conclusion") or item.get("state") or "").upper()
    if v in ("SUCCESS", "NEUTRAL"):
        return "passed"
    if v == "SKIPPED":
        return "skipped"
    if v in ("PENDING", "QUEUED", "IN_PROGRESS", "WAITING", "EXPECTED", ""):
        return "pending"
    return "failed"


def _rollup(pr: dict) -> dict:
    """What the other side's automation has actually said about this branch."""
    items = pr.get("statusCheckRollup") or []
    counts = {"passed": 0, "failed": 0, "pending": 0, "skipped": 0}
    for i in items:
        counts[_check_state(i)] += 1
    if not items:
        # The finding. Not an absence of news - it is news: this branch is being
        # proposed for merge and no automation on the far side will ever have an
        # opinion about it.
        verdict = "none"
    elif counts["failed"]:
        verdict = "failing"
    elif counts["pending"]:
        verdict = "running"
    elif counts["passed"]:
        verdict = "passing"
    else:
        verdict = "skipped"
    failing = [i.get("name") or i.get("context") or "?" for i in items
               if _check_state(i) == "failed"]
    return {"verdict": verdict, "total": len(items), "failing": failing[:6], **counts}


def open_prs() -> dict:
    """Every open pull request across every lane repository, with its checks.

    Returns the repositories alongside the pull requests, and that is not
    packaging. A repository GitHub would not answer about has no pull requests
    in this reply, which is the same shape as a repository with none - and those
    two must never render the same way. The repository row carries `why` so the
    difference survives as far as the screen.

    Asked of GitHub rather than read from the goals: a pull request opened by
    hand, from another machine, or by somebody else is still a pull request
    against work this harness is doing, and a list built from our own
    `pr_url` fields would be a list of what we remember doing.

    One thread per repository. Ten sequential round-trips to GitHub is ten
    seconds of a tab that is expected to open, and each thread writes only into
    its own slot - there is no shared accumulator here to lock.
    """
    repos = pr_repos()
    got: dict[str, dict] = {}

    def ask(r):
        try:
            p = subprocess.run(["gh", "pr", "list", "--repo", r["repo"], "--state", "open",
                                "--json", GH_PR_FIELDS, "--limit", "30"],
                               capture_output=True, text=True, timeout=GH_TIMEOUT)
        except (subprocess.TimeoutExpired, OSError) as e:
            got[r["repo"]] = {"why": f"gh did not answer: {e}", "prs": []}
            return
        if p.returncode != 0:
            got[r["repo"]] = {"why": last_line(redact(p.stderr or p.stdout)), "prs": []}
            return
        try:
            listed = json.loads(p.stdout or "[]")
        except ValueError:
            got[r["repo"]] = {"why": "gh answered with something that is not JSON", "prs": []}
            return
        got[r["repo"]] = {"why": None, "prs": listed}

    threads = [threading.Thread(target=ask, args=(r,), daemon=True) for r in repos]
    for t in threads:
        t.start()
    for t in threads:
        t.join(GH_TIMEOUT + 5)

    # `pr_url` is matched back on so a pull request this harness opened is shown
    # as ours. It is a lookup and never a filter: a PR with no goal behind it
    # still belongs on this list, and saying "we did not open this one" is more
    # use than leaving it out.
    mine = {g.get("pr_url"): g for g in (load_goal(x["id"]) or {} for x in goals())
            if g.get("pr_url")}
    out = []
    for r in repos:
        res = got.get(r["repo"]) or {"why": "this repository was never asked", "prs": []}
        for pr in res["prs"]:
            g = mine.get(pr.get("url")) or {}
            out.append({"repo": r["repo"], "lanes": r["lanes"],
                        "number": pr.get("number"), "title": pr.get("title"),
                        "url": pr.get("url"), "draft": bool(pr.get("isDraft")),
                        "head": pr.get("headRefName"), "base": pr.get("baseRefName"),
                        "at": pr.get("createdAt"), "checks": _rollup(pr),
                        "goal_id": g.get("id"), "lane": g.get("lane")})
        r["why"] = res["why"]
        r["open"] = len(res["prs"])
    out.sort(key=lambda p: p.get("at") or "", reverse=True)
    return {"prs": out, "repos": repos}


def condition_tally(g: dict) -> dict:
    """How much of one goal's definition of done is backed by something that ran.

    Four buckets and not two, because "no check" and "a check that has never
    run" fail for different reasons and are fixed by different people - one
    needs a command written, the other needs a command executed - and a single
    `unproven` number sends the operator to the wrong one half the time.
    """
    t = {"passed": 0, "failed": 0, "unrun": 0, "unchecked": 0,
         "judgement": 0, "waived": 0, "total": 0, "gaps": [], "checks": []}
    for d in (g.get("done") or []):
        t["total"] += 1
        if d.get("waived"):
            t["waived"] += 1
        if not d.get("check"):
            # Already asked, and the answer was that no command can decide it.
            # Counted apart so the pile of unasked ones is not padded by the
            # ones there is nothing further to do about.
            if d.get("uncheckable"):
                t["judgement"] += 1
            else:
                t["unchecked"] += 1
                t["gaps"].append(d.get("text"))
            continue
        t["checks"].append({"text": d.get("text"), "check": d.get("check"),
                            "exit": d.get("check_exit"), "at": d.get("check_at"),
                            "by": d.get("check_by")})
        if d.get("check_exit") is None:
            t["unrun"] += 1
        elif d.get("check_exit") == 0:
            t["passed"] += 1
        else:
            t["failed"] += 1
    return t


def pr_view() -> dict:
    """The handoff across the whole workspace: what is stuck, and where.

    Every report is built with `rerun=False`. Re-running the checks of nineteen
    finished goals would be minutes of builds fired off by opening a tab, and -
    worse - it would run them in worktrees that other lanes' workers are live
    in. The reports therefore carry `stale_checks`, and the button that actually
    publishes re-runs everything for real before it pushes anything.
    """
    ident = _whoami("github")
    todo = [g for g in goals() if g.get("state") == "done"]
    rows: list[dict] = []
    lock = threading.Lock()

    def look(s):
        try:
            rep = publish_report(s["id"], rerun=False)
        except Exception as e:                 # a broken worktree is not a crash
            rep = {"ok": False, "goal_id": s["id"], "lane": s.get("lane"),
                   "blocked": [f"this goal could not be read: {redact(str(e))[:200]}"],
                   "conditions": [], "commits": []}
        g = load_goal(s["id"]) or {}
        rep["objective"] = (g.get("objective") or "").split("\n\n")[0]
        rep["tally"] = condition_tally(g)
        rep["pr_url"] = g.get("pr_url")
        with lock:
            rows.append(rep)

    threads = [threading.Thread(target=look, args=(s,), daemon=True) for s in todo]
    for t in threads:
        t.start()
    for t in threads:
        t.join(120)

    # Ready first, then whatever is closest to ready. `blocked` is a list of
    # sentences, and its length is how many separate things are wrong - which is
    # the only ordering here that is a fact rather than a preference.
    rows.sort(key=lambda r: (bool(r.get("pr_url")), len(r.get("blocked") or []),
                             r.get("lane") or ""))
    asked = open_prs()
    live = asked["prs"]
    return {
        "github": {"provider": "github", "signin": DEPLOY_SIGNIN["github"], **ident},
        # The annotated ones from the call that was actually made, not a second
        # `pr_repos()`. A fresh list would carry no `why`, and every repository
        # GitHub refused would silently become one with nothing open.
        "repos": asked["repos"],
        "open": live,
        "ready": rows,
        # The three counts the tab exists for, each one a number nothing else in
        # the console can produce.
        "handoff_ready": sum(1 for r in rows if r.get("ok") and not r.get("pr_url")),
        "finished_unshipped": sum(1 for r in rows if not r.get("pr_url")),
        "untested": sum(1 for p in live if p["checks"]["verdict"] in ("none", "skipped")),
        "gaps": sum(r["tally"]["unchecked"] for r in rows if r.get("tally")),
    }


# ----------------------------------------------------- writing a missing check

CHECK_WRITE_SYSTEM = (
    "You are the consulting architect. A goal has finished and cannot be handed over, because "
    "some of its done-conditions were never given a command that decides them. A condition "
    "nobody can check is somebody's opinion, and this harness does not let an opinion open a "
    "pull request.\n\n"
    "For each condition below, give the ONE shell command that decides it.\n\n"
    "Reply with one JSON object and nothing else:\n"
    "{\n"
    '  "checks": [{"text": "the condition, copied word for word",\n'
    '              "check": "a shell command, or null",\n'
    '              "why": "one sentence: what exit 0 would prove, or why no command can"}]\n'
    "}\n\n"
    "Rules:\n"
    "- Copy `text` EXACTLY. It is how your answer is matched back to the condition; a reworded "
    "one is discarded.\n"
    "- The command runs from the root of the worktree shown below, non-interactively, with no "
    "network guaranteed. It must exit 0 when the condition holds and NON-ZERO when it does not.\n"
    "- It has to be able to fail. `true`, `echo ...`, or anything that exits 0 whatever the "
    "repository contains will be rejected before it is stored, and it would be worse than "
    "nothing: it turns an honest gap into a green tick.\n"
    "- Assume NOTHING about what interpreters are on PATH. On this machine bare `python3` hits a "
    "version shim and refuses, so a `python3 -c ...` check fails without ever running - which "
    "records the condition as NOT MET on the word of a command that never executed. If you need "
    "an interpreter, invoke one the repository itself already invokes. A check that cannot run "
    "is removed again after it is tried, and the condition goes back to being an open gap.\n"
    "- Prefer a command that already exists in the repository - the test runner, the benchmark, "
    "the conformance script - over a shell expression you invent. Ground it in the files listed "
    "below; a check naming a file nobody has is a check that fails for the wrong reason.\n"
    "- If no command can decide a condition, answer `null` and say why in one sentence. That is "
    "a real answer and it is recorded. Do not invent a command that tests something adjacent and "
    "easier - a check for the wrong thing is the only outcome here worse than no check."
)

_ALWAYS_TRUE = re.compile(r"^\s*(true|:|exit\s+0|echo\b[^;&|]*)\s*$", re.I)


def worthless_check(cmd: str) -> str | None:
    """Why this command could not be evidence about anything, or None.

    Deliberately not a quality bar. A weak check that genuinely runs something
    is still a check, and judging how good it is belongs to whoever reviews the
    pull request. This catches the single outcome that is actively harmful: a
    command that exits 0 no matter what the repository contains, which would
    convert an honest gap into a passing condition and take the gate with it.
    """
    cmd = (cmd or "").strip()
    if not cmd:
        return "it is empty"
    parts = [p.strip() for p in re.split(r"&&|\|\||;|\n", cmd) if p.strip()]
    if parts and all(_ALWAYS_TRUE.match(p) for p in parts):
        return "it exits 0 whatever the repository contains, so it cannot fail"
    return None


# What a shell says when the command itself was never there to run. Read from
# OUTPUT and not from an exit code, because the codes collide: this machine's
# version shim exits 126 for a missing interpreter and plenty of real test
# runners exit 126 for their own reasons, so the number alone cannot tell a
# broken check apart from a failing condition.
_NEVER_RAN = (
    "command not found",
    "no such file or directory",
    "no version is set for command",   # the version shim on this machine
    "is not recognized as an internal or external command",
)


def did_not_run(exit_code: int | None, output: str) -> str | None:
    """Why this check never actually executed, or None if it ran and judged.

    The mirror of `worthless_check`, and it exists because of a live run: the
    architect proposed two perfectly reasonable `python3 -c ...` checks, and on
    this machine bare `python3` hits a version shim that refuses. Both would
    have been stored, run, and recorded as FAILING - and a failing condition
    reads as "the work is not done", so the operator is sent to fix code that
    was never the problem, while the condition itself stays unproven forever.

    It is the gentler of the two failures: a check that cannot fail turns a gap
    into a green tick, one that cannot run turns a gap into a red cross. Neither
    is evidence, and only the first was being caught.
    """
    if exit_code is None or exit_code == 0:
        return None                    # a timeout already reports itself as unrun
    low = (output or "").lower()
    for m in _NEVER_RAN:
        if m in low:
            return f"the shell answered {m!r}, so the command never ran"
    return None


def write_checks(gid: str, *, apply: bool = False) -> dict:
    """Ask the architect for the commands that are missing, and optionally keep them.

    Two steps, like `improve_goal`: the first answers and the operator reads it,
    the second commits to it. The reason is specific rather than ceremonial - a
    check is the thing this whole harness treats as outranking every judgement,
    so one arriving from a model deserves to be looked at by a person once
    before it starts deciding whether work may be handed over.

    Applying does three things and they are all the point: the command is
    stored, it is stamped with who wrote it, and it is RUN. The run is not a
    formality. A check that has never executed proves nothing, and the exit code
    that comes back here is the first real evidence the condition has ever had.
    """
    g = load_goal(gid)
    if not g:
        return {"ok": False, "error": f"no goal {gid!r}"}
    missing = [d for d in (g.get("done") or [])
               if not d.get("check") and not d.get("uncheckable")]
    if not missing:
        return {"ok": False, "error": "every condition on this goal either has a check or has "
                                      "already been ruled uncheckable"}
    if not architect_available():
        return {"ok": False, "error": architect_off_reason()}

    wt = WORKTREE_DIR / g["lane"]
    doc = (f"# Objective\n\n{g.get('objective')}\n\n"
           f"# Where the command will run\n\n`{wt}` (lane `{g['lane']}`)\n\n"
           + goal_brief(g["lane"])
           + "\n\n# Conditions with no check\n\n"
           + "\n".join(f"- {d['text']}" for d in missing))
    plan, raw = _goal_chat(g, CHECK_WRITE_SYSTEM, doc)
    if not plan:
        goal_log(gid, "the architect's answer about missing checks could not be read as JSON",
                 raw=raw[:2000])
        return {"ok": False, "error": "the architect's answer could not be read as JSON"}

    want = {d["text"] for d in missing}
    proposed, dropped = [], []
    for c in (plan.get("checks") or []):
        text = str(c.get("text") or "").strip()
        if text not in want:
            # Named rather than silently ignored. A reworded condition is the
            # one failure mode of matching on text, and an operator who is told
            # "3 of 4 came back" will go and look; one who is told nothing will
            # believe the fourth had no answer.
            dropped.append(text[:120])
            continue
        cmd = (c.get("check") or "").strip() or None
        why = " ".join(str(c.get("why") or "").split())[:400]
        bad = worthless_check(cmd) if cmd else None
        proposed.append({"text": text, "check": None if bad else cmd,
                         "why": why, "refused": bad,
                         "verdict": "refused" if bad else ("check" if cmd else "judgement")})
    unanswered = sorted(want - {p["text"] for p in proposed})
    out = {"ok": True, "goal_id": gid, "lane": g["lane"], "applied": False,
           "proposed": proposed, "dropped": dropped, "unanswered": unanswered}
    if not apply:
        return out

    with _GOAL_LOCK:
        g = load_goal(gid)
        by_text = {p["text"]: p for p in proposed}
        wrote = ruled = 0
        for d in g.get("done") or []:
            p = by_text.get(d["text"])
            # `d.get("check")` again under the lock: this function makes two
            # calls a minute apart with a model round-trip between them, and a
            # worker or a replan can have given the condition a check in the
            # meantime. Overwriting one that somebody else wrote with one from
            # a stale reading is the race worth closing here.
            if not p or d.get("check") or d.get("uncheckable"):
                continue
            if p["check"]:
                d["check"] = p["check"]
                # Carried for the life of the condition. A check written by a
                # model that was shown the condition it had to satisfy is not
                # the same evidence as one written alongside the work, and a
                # reviewer weighing `live_deployed` is entitled to know which
                # kind they are reading.
                d["check_by"] = "architect"
                d["check_written_at"] = now()
                d["check_why"] = p["why"]
                wrote += 1
            elif p["verdict"] == "judgement":
                d["uncheckable"] = p["why"] or "the architect gave no command and no reason"
                d["uncheckable_at"] = now()
                ruled += 1
            # A REFUSED command falls through and changes nothing, which took a
            # test to notice. It used to land in the branch above, because a
            # refusal and a judgement both arrive with `check` set to None - so
            # a condition whose only offered check was `true` was recorded as
            # one no command can decide, carrying the model's justification for
            # the bad command as the reason. That is the exact failure
            # `worthless_check` exists to stop, arriving one line after it
            # succeeded: the gap count fell, nothing was checked, and the
            # sentence on the record was false. It stays a gap, and it can be
            # asked again.
        save_goal(g)

    out["applied"] = True
    out["wrote"] = wrote
    out["ruled_uncheckable"] = ruled
    goal_log(gid, f"the architect wrote {wrote} missing check(s) and ruled {ruled} "
                  f"condition(s) undecidable by command")
    if wrote:
        # Only the new ones. See `run_goal_checks`: re-running the forty that
        # already passed would cost minutes and answer nothing that was asked.
        out["results"] = run_goal_checks(gid, only={p["text"] for p in proposed if p["check"]})

        # A written check that turns out not to RUN is taken straight back off
        # the goal. `worthless_check` can only read the command; whether the
        # thing it names exists on this machine is not knowable until it is
        # tried, and trying it is what this run just did.
        #
        # Keeping it would be worse than the gap it filled: `run_goal_checks`
        # has already set `met = False` on the condition, so a command that was
        # never there to run now reads as proof the work is not done. Removing
        # it restores the honest state - nobody has checked this - and it can be
        # asked again, which is the same ending a refusal gets.
        broke = []
        for r in out["results"]:
            why = did_not_run(r["exit"], r.get("output") or "")
            if why:
                broke.append({"text": r["text"], "check": r["check"], "why": why})
        if broke:
            bad = {b["text"] for b in broke}
            with _GOAL_LOCK:
                g = load_goal(gid)
                for d in g.get("done") or []:
                    if d["text"] in bad and d.get("check_by") == "architect":
                        for k in ("check", "check_by", "check_written_at", "check_why",
                                  "check_exit", "check_at"):
                            d.pop(k, None)
                        # `met` was forced to False by the run above, on the
                        # word of a command that never executed. It has no
                        # standing to say anything about this condition.
                        d.pop("met", None)
                save_goal(g)
            wrote -= len(broke)
            out["wrote"] = wrote
            goal_log(gid, f"{len(broke)} of those check(s) never ran on this machine and "
                          f"were taken back off the goal")
        out["did_not_run"] = broke
        out["tally"] = condition_tally(load_goal(gid) or {})
    return out


def waive_condition(gid: str, text: str, why: str, *, by: str = "operator") -> dict:
    """Record that a person is handing over work one condition did not prove.

    This does NOT mark the condition met and it does not make its check pass.
    Everything on the record still says exactly what happened - see the verdicts
    in `publish_report`, which are untouched by this. What it does is stop that
    unproven condition blocking the handoff, under a name and a reason, both of
    which are printed in the pull request body where the reviewer will read them
    next to the condition they are about.

    The reason is required, and a waiver with nothing to say is refused. The
    entire value of this is the sentence; without one it is a way of deleting a
    gate quietly, which is what people do instead when a gate has no exit.

    Nothing automated may call this. No ticker path reaches it, and the argument
    is the same as for publishing: a waiver is somebody accepting a consequence,
    and a program accepting a consequence on its own behalf is not accepting
    anything.
    """
    why = " ".join((why or "").split())
    if len(why) < 8:
        return {"ok": False, "error": "a waiver needs a reason - it is the only thing that "
                                      "makes it different from deleting the condition"}
    with _GOAL_LOCK:
        g = load_goal(gid)
        if not g:
            return {"ok": False, "error": f"no goal {gid!r}"}
        d = next((x for x in (g.get("done") or []) if x.get("text") == text), None)
        if not d:
            return {"ok": False, "error": "no condition on this goal says that"}
        if d.get("check") and d.get("check_exit") == 0:
            # Refused rather than allowed-and-ignored. A waiver on a condition
            # that passed is a record of an anxiety, and it would read on the
            # pull request as though something had gone wrong here.
            return {"ok": False, "error": "that condition's check passed - there is nothing "
                                          "to waive"}
        d["waived"] = {"at": now(), "by": by, "why": why}
        save_goal(g)
    goal_log(gid, f"{by} waived an unproven condition: {text[:120]} - {why[:200]}")
    add_note(f"goal {gid} ({g['lane']}): {by} waived \"{text[:80]}\" - {why[:160]}",
             lane=g.get("lane"))
    return {"ok": True, "report": publish_report(gid, rerun=False)}


def unwaive_condition(gid: str, text: str) -> dict:
    """Take a waiver back. The gate closes again immediately."""
    with _GOAL_LOCK:
        g = load_goal(gid)
        if not g:
            return {"ok": False, "error": f"no goal {gid!r}"}
        d = next((x for x in (g.get("done") or []) if x.get("text") == text), None)
        if not d or not d.get("waived"):
            return {"ok": False, "error": "that condition is not waived"}
        d.pop("waived", None)
        save_goal(g)
    goal_log(gid, f"a waiver was withdrawn from: {text[:120]}")
    return {"ok": True, "report": publish_report(gid, rerun=False)}


# ------------------------------------------------------- what can be deployed
#
# The mission is to move lanes off `live_local` and onto `live_deployed`, and a
# rung moves when evidence moves it - so `live_deployed` needs a deploy that
# actually happened, which needs a credential that actually works. Nothing in
# the harness knew whether one did.
#
# It cost the fleet directly and took a cross-tabulation to see. Eleven lanes,
# three running goals against a cap of ten, and every proposal in a lane that
# was free sat under a bar. The architect had written the reason on each one and
# nothing was reading it: "the low confidence comes from unestablished deployed
# access", "success now depends on deployed hosts, access, and installed real
# implementations", "the remaining uncertainties require operator-controlled
# deployed machines, credentials, spending, or acceptance decisions". Two
# independent call paths, eleven lanes, the same sentence. Sharpening had spent
# two to four rounds on most of them being told the wording was not the problem.
#
# So this is not a deploy button. It is the answer to "which credential is
# missing, and what does that cost us" - discovered from disk and checked by
# asking the provider, because the two failures worth catching are a target
# nobody registered and a login everybody assumed.

# What marks a directory as deployable, and by whom. Discovered, never
# configured: a list of deploy targets kept by hand is a list that is wrong the
# first time somebody adds a service and does not think to come here.
DEPLOY_MARKERS = (
    ("fly", "fly.toml"),
    ("cloudflare", "wrangler.toml"),
    ("cloudflare", "wrangler.jsonc"),
    ("cloudflare", "wrangler.json"),
    ("npm", "package.json"),
)


def _npm_package(path: Path) -> dict | None:
    """What a `package.json` says about itself, or None if it is not publishable.

    The odd one out among the markers, and the reason this exists: every other
    marker file means "deployable" by being present at all, and `package.json`
    does not. Most of them in this workspace are a build config for a site, or
    an app nobody would put on a registry, and listing those as things that
    could be published would bury the eight that can under thirty that cannot.

    So the marker is the file AND what it says: a name, a version, and no
    `private` flag - which is npm's own way of writing down "do not publish
    this", already in the tree, honoured rather than second-guessed.
    """
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get("private") or not d.get("name") or not d.get("version"):
        return None
    return {"package": str(d["name"]), "version": str(d["version"])}

# How to ask each provider who we are, and what its answer has to CONTAIN before
# that counts as a yes.
#
# A command, not a token check: a token that exists is not a token that works,
# and the difference is a deploy that fails after the operator has been told they
# are signed in. But the command's exit code is not the answer either -
#
#     $ wrangler whoami
#      wrangler 4.115.0
#     Getting User settings...
#     You are not authenticated. Please run `wrangler login`.
#     $ echo $?
#     0
#
# - measured on wrangler 4.115.0. Read off the return code, that transcript is a
# signed-in Cloudflare account, and the `who` column gets filled from the first
# line of output, which is a version banner. So the console would report a
# version number as a person, count every Worker as deployable, and lead the
# Publish tab with `0 stranded` while nobody was signed in to anything.
#
# What decides is whether the provider NAMED somebody. Each `who` below is the
# patterns that capture that name out of the provider's own words, tried in
# order; no capture is a no, whatever the tool returned.
#
# In order, because a provider can be signed in more than one way and they are
# not equally informative. Cloudflare says who we are when there is a who, and
# only what kind of credential it is when there is not - an API token belongs to
# no email. Taking the address first and falling back to the credential means
# the row says "travis@..." where it can and "an API Token" where that is
# genuinely all there is, instead of one of them always.
DEPLOY_WHOAMI = {
    # One address on one line, and nothing else in the buffer.
    "fly": {"cmd": ["flyctl", "auth", "whoami"],
            "who": (r"^\s*(\S+@\S+\.\S+?)\s*$",)},
    # "You are logged in with an OAuth Token, associated with the email x@y."
    "cloudflare": {"cmd": ["wrangler", "whoami"],
                   "who": (r"associated with the email (\S+?)\.?\s*$",
                           r"You are logged in with (.+?)\.?\s*$"),
                   "no": r"^.*not authenticated.*$"},
    # A bare username, alone on the line.
    "npm": {"cmd": ["npm", "whoami"], "who": (r"^\s*([\w.@/-]+)\s*$",)},
    # Not a deploy target - nothing is shipped to GitHub - but the identical
    # question asked of a fourth CLI, and the pull-requests tab needs the same
    # answer this table already knows how to get. It stays out of the Publish
    # tab's identity list on its own: that list is derived from the providers
    # the discovered targets actually name, and no marker file names this one.
    "github": {"cmd": ["gh", "auth", "status"],
               "who": (r"Logged in to \S+ account (\S+)",),
               "no": r"^.*not logged in.*$"},
}

DEPLOY_SIGNIN = {
    "fly": "flyctl auth login",
    "cloudflare": "wrangler login",
    "npm": "npm login",
    "github": "gh auth login",
}


def deploy_targets() -> list[dict]:
    """Every deployable thing in the workspace, found by looking.

    Two levels deep from the workspace root and no further: a `fly.toml` inside
    `node_modules` is a dependency's, not ours, and one six directories down is
    a vendored copy. Both would be reported as ours and neither would deploy.

    A target is reported with the lane that owns it where one does, and without
    one where none does. That second case is the one worth having: a service
    with a `fly.toml` and no lane is a thing that can be deployed and that no
    worker is ever going to look at.
    """
    lanes_by_path = {}
    for name, cfg in (config().get("lanes") or {}).items():
        p = (cfg or {}).get("path") or ""
        lanes_by_path.setdefault(str((ROOT / p).resolve()), name)
    skip = {"node_modules", ".git", "_build", "deps", "old_scrap", "dist", ".amp"}
    out: list[dict] = []
    for provider, marker in DEPLOY_MARKERS:
        for path in list(ROOT.glob(marker)) + list(ROOT.glob(f"*/{marker}")) \
                + list(ROOT.glob(f"*/*/{marker}")):
            if skip & set(path.parts):
                continue
            extra = _npm_package(path) if provider == "npm" else {}
            if extra is None:
                continue
            d = path.parent.resolve()
            rel = str(d.relative_to(ROOT)) if d != ROOT else "."
            # The key is set HERE and not by whoever happens to render these.
            # It was added in `deploy_view` first, so a target fetched through
            # any other path - the preflight endpoint, for one - came back
            # without the field it is addressed by, which reads as a target that
            # does not exist.
            out.append({"provider": provider, "key": f"{provider}:{rel}",
                        "dir": str(d), "rel": rel, "marker": marker,
                        "lane": lanes_by_path.get(str(d)), **extra})
    # A directory can carry two markers - a Worker in front of a Fly backend is
    # an ordinary shape - so this is keyed on the pair, not on the directory.
    seen, uniq = set(), []
    for t in sorted(out, key=lambda t: (t["provider"], t["rel"])):
        k = (t["provider"], t["dir"])
        if k not in seen:
            seen.add(k)
            uniq.append(t)
    return uniq


def _whoami(provider: str) -> dict:
    """Ask one provider who we are, and report what it said rather than a guess.

    Signed in means two things together: the command succeeded AND it named
    somebody. Either alone is a claim rather than a fact - see DEPLOY_WHOAMI for
    the transcript where one of them is true and nobody is signed in.

    A timeout is a `no`, not an exception: every one of these reaches the
    network, and this is called from a request thread. An install that is
    missing and a login that is absent are reported apart, because they are
    different problems with different fixes and the operator would otherwise be
    told to sign in to something that is not installed.
    """
    spec = DEPLOY_WHOAMI.get(provider)
    if not spec:
        return {"ok": False, "installed": False, "who": None,
                "why": f"nothing here knows how to check {provider!r}"}
    cmd = spec["cmd"]
    if not shutil.which(cmd[0]):
        return {"ok": False, "installed": False, "who": None,
                "why": f"{cmd[0]} is not installed"}
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except (subprocess.TimeoutExpired, OSError) as e:
        return {"ok": False, "installed": True, "who": None,
                "why": f"{cmd[0]} did not answer: {e}"}
    text = redact((r.stdout + r.stderr).strip())
    for pattern in (spec["who"] if r.returncode == 0 else ()):
        named = re.search(pattern, text, re.M)
        if named:
            return {"ok": True, "installed": True,
                    "who": " ".join(named.group(1).split()), "why": None}
    return {"ok": False, "installed": True, "who": None,
            "why": _refusal(text, spec.get("no"))
                   or f"{cmd[0]} answered but named nobody"}


def _refusal(text: str, pattern: str | None = None) -> str:
    """The one line worth showing when a provider will not name us.

    The last line by default, which is where a CLI that failed puts its error.
    Where a provider is known to keep talking afterwards - wrangler follows
    `You are not authenticated` with a suggestion about temporary previews - the
    line that carries the refusal is named instead, so the operator is not sent
    to read a workaround in place of the reason.
    """
    if pattern:
        m = re.search(pattern, text, re.M)
        if m:
            return " ".join(m.group(0).split())
    return last_line(text)


def last_line(text: str, first: bool = False) -> str:
    """The one line of a command's output worth putting in a status row."""
    lines = [" ".join(l.split()) for l in (text or "").splitlines() if l.strip()]
    return (lines[0] if first else lines[-1]) if lines else ""


def deploy_view() -> dict:
    """What could be deployed, what it needs, and whether that thing works now.

    The count that matters is `stranded`: targets whose provider we cannot sign
    in to. That number is the mission's own ceiling written down - each one is a
    service that can never produce the evidence `live_deployed` requires, no
    matter how many workers are pointed at it.
    """
    targets = deploy_targets()
    want = sorted({t["provider"] for t in targets} | {"npm"})
    ident = {p: _whoami(p) for p in want}
    for t in targets:
        t["signed_in"] = bool(ident.get(t["provider"], {}).get("ok"))
    # PUBLISHES only. This took the last run of any kind at first, and a
    # `flyctl config validate` that exited 0 then appeared on the PULSE row as
    # "done ... exit 0" - in the line whose entire job is to say what was last
    # SHIPPED from here. A dry run dressed as a deploy is the confusion this
    # tab exists to prevent, arriving through the tab itself.
    last = {r["key"]: r for r in reversed(deploy_runs()) if r.get("mode") == "publish"}
    for t in targets:
        t["last"] = last.get(t["key"])
    if ident.get("npm", {}).get("ok"):
        _ask_registry(targets)
    return {"targets": targets,
            "identities": [{"provider": p, "signin": DEPLOY_SIGNIN.get(p), **ident[p]}
                           for p in want],
            "stranded": sum(1 for t in targets if not t["signed_in"]),
            "lanes_stranded": sorted({t["lane"] for t in targets
                                      if t["lane"] and not t["signed_in"]}),
            "waiting": [t["key"] for t in targets if t.get("unpublished")],
            "running": deploy_running()}


def _ask_registry(targets: list[dict]) -> None:
    """Ask npm what it already has, for every package at once.

    Worth the wall clock and worth doing here rather than on a button, because
    it answers the question the tab is actually for. `stranded` says what CANNOT
    be published; this says what HAS NOT been - a version sitting in the tree
    that the registry has never seen is finished work that nobody outside this
    machine can use, and it is invisible everywhere else in the console.

    In parallel because it is eight sequential network round-trips otherwise,
    and each one writes only into its own target - no shared accumulator, so
    there is nothing here to lock.
    """
    pkgs = [t for t in targets if t["provider"] == "npm" and t.get("package")]

    def ask(t):
        try:
            reg = _npm_registry(t["package"])
        except Exception:
            reg = {"versions": [], "latest": None}
        t["registry"] = reg["latest"]
        # Exactly one claim: this version is not on the registry. Not "the tree
        # is ahead" - a tree can be behind, and `opensentience.org/box-and-box`
        # at 0.9.0 against a registry serving 0.10.0 is - and which of those it
        # is, is a judgement from two numbers that are both shown.
        t["unpublished"] = t.get("version") not in reg["versions"]

    threads = [threading.Thread(target=ask, args=(t,), daemon=True) for t in pkgs]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=30)


# ------------------------------------------------------------------ publishing
#
# Everything above answers "could this be deployed". This is the part that does
# it, and the whole of its design is one rule: NOTHING HERE RUNS ON ITS OWN.
#
# Every other loop in this console is autonomous by default - the ticker opens
# goals, adopts proposals, reaps stranded plans, sharpens objectives - and that
# is right, because the worst a wrong one costs is a worker's time in a worktree
# that gets thrown away. This one reaches production and spends the operator's
# money, and there is no worktree to throw away afterwards. So a publish happens
# because a person pressed a button, it is never scheduled, never retried, and
# never triggered by a goal closing.
#
# What it leaves behind is the point. A deploy that succeeds and is not written
# down is exactly the `live_deployed` claim nobody can check - and that rung has
# already been claimed once on this stack without evidence, which is why the
# contradictions gate exists. So each run records what was run, in which
# directory, at which commit, what the provider said back verbatim, and then -
# separately - what an independent question about the world answered afterwards.
# The two are kept apart because a deploy command exiting 0 is not a live
# service, and the gap between those two sentences is where the false rung came
# from.

# What each provider is asked to do.
#
# Two commands each, and the first one is the same command the preflight runs -
# so what the operator is shown before publishing and what they publish with are
# one thing that cannot drift apart.
#
# The three checks are NOT equally strong and the tab says so rather than
# levelling them. `wrangler --dry-run` and `npm --dry-run` do everything the
# real command does except the upload. `flyctl config validate` only reads the
# config file: it can pass on a service whose build is broken. A check that is
# weaker than its neighbours is worth having; a check that claims to be as
# strong as its neighbours is worse than none.
DEPLOY_RUN = {
    "fly": {"check": ["flyctl", "config", "validate"],
            "publish": ["flyctl", "deploy", "--yes"],
            "check_is": "the config file only - not the build"},
    "cloudflare": {"check": ["wrangler", "deploy", "--dry-run"],
                   "publish": ["wrangler", "deploy"],
                   "check_is": "a full build, with nothing uploaded"},
    "npm": {"check": ["npm", "publish", "--dry-run"],
            "publish": ["npm", "publish", "--access", "public"],
            "check_is": "a full pack, with nothing published"},
}

# A publish is allowed to take a long time - a cold Fly build reaches a remote
# builder and a registry - but not forever, because it holds a thread and the
# operator is watching a spinner. Half an hour, then the process GROUP is killed
# the way `run_check` kills one, because a deploy that has started a builder
# leaves it running otherwise.
DEPLOY_TIMEOUT = 1800


def deploy_key(t: dict) -> str:
    """What names one deployable thing. Provider and place, because a directory
    can carry two markers - a Worker in front of a Fly backend is ordinary."""
    return t.get("key") or f"{t.get('provider')}:{t.get('rel')}"


def _git_state(d: Path) -> dict:
    """The commit a deploy from this directory would be shipping.

    `-- .` on the status, not the bare repo: these are ~27 separate repos and a
    deploy of `PULSE` has no business being blocked by an edit under `TRVM`.

    `dirty` is the fact this exists for. Publishing a working tree with
    uncommitted changes ships bytes that no commit names, and the record would
    then carry a sha that does not describe what is running - which is a worse
    outcome than no record, because it is a checkable-looking claim that is
    false.
    """
    sha = run(["git", "-C", str(d), "rev-parse", "--short", "HEAD"])
    st = run(["git", "-C", str(d), "status", "--porcelain", "--", "."])
    if sha.returncode != 0:
        return {"sha": None, "dirty": [], "why": last_line(redact(sha.stderr))}
    return {"sha": sha.stdout.strip(),
            "dirty": [l[3:] for l in st.stdout.splitlines() if l.strip()][:20],
            "why": None}


def _npm_registry(package: str) -> dict:
    """Every version the registry serves, and which one it calls latest.

    The whole list, not `npm view <pkg> version`, and the difference is not
    cosmetic. That command answers "what is the latest version", and three
    places here were reading its answer as "does this version exist" - which
    are different questions whenever a tree is behind the registry rather than
    ahead of it. Measured: `box-and-box` locally at 0.9.0 with 0.10.0 on the
    registry was reported as "a version the registry does not have", which was
    true, but the console had no way to know that - it would have said the same
    thing about a 0.8.0 that IS published.

    An empty list means the registry has nothing under this name, and is
    reported as such rather than as an error: a package that has never been
    published is the ordinary state of a package that has never been published.
    """
    r = run(["npm", "view", package, "versions", "--json"])
    if r.returncode != 0:
        return {"versions": [], "latest": None, "known": False}
    try:
        v = json.loads(r.stdout or "[]")
    except ValueError:
        return {"versions": [], "latest": None, "known": False}
    v = [str(x) for x in (v if isinstance(v, list) else [v])]
    return {"versions": v, "latest": v[-1] if v else None, "known": True}


def deploy_preflight(t: dict) -> dict:
    """Everything that would stop this publish, each one a fact rather than a
    rule.

    Returns blockers and notes apart. A blocker is something the harness will
    refuse to publish over; a note is something worth reading that is not an
    answer either way. The distinction matters because the one case that looked
    like a blocker and is not is npm's: a package whose version is already on
    the registry cannot be published again, and that is not a failure, it is
    "there is nothing here to do".
    """
    ident = _whoami(t["provider"])
    git = _git_state(Path(t["dir"]))
    blockers, notes = [], []
    if not ident["ok"]:
        blockers.append(f"not signed in to {t['provider']}: {ident['why']}")
    if git["why"]:
        notes.append(f"no commit names these bytes: {git['why']}")
    elif git["dirty"]:
        blockers.append(
            f"{len(git['dirty'])} uncommitted change(s) under {t['rel']} "
            f"({', '.join(git['dirty'][:3])}{'…' if len(git['dirty']) > 3 else ''}) - "
            f"publishing now ships bytes no commit names")
    if t["provider"] == "npm":
        reg = _npm_registry(t["package"]) if ident["ok"] else {"versions": [], "latest": None}
        if reg["latest"]:
            notes.append(f"the registry's latest is {t['package']}@{reg['latest']}")
        elif ident["ok"]:
            notes.append(f"the registry has never heard of {t['package']}")
        if t.get("version") in reg["versions"]:
            blockers.append(f"{t['package']}@{t['version']} is already published - "
                            f"bump the version in package.json first")
    return {"key": deploy_key(t), "blockers": blockers, "notes": notes,
            "sha": git["sha"], "signed_in": ident["ok"],
            "check_is": DEPLOY_RUN.get(t["provider"], {}).get("check_is")}


# One entry per running publish, keyed the way targets are. Not a queue: two
# publishes of DIFFERENT things at once is fine and normal, two of the SAME
# thing is a mistake nobody meant to make, and the key is what makes the second
# one refusable.
_DEPLOYS: dict[str, dict] = {}
_DEPLOY_LOCK = threading.Lock()


def deploy_running() -> list[dict]:
    with _DEPLOY_LOCK:
        return [{k: v for k, v in d.items() if k != "proc"} for d in _DEPLOYS.values()]


def deploy_status(key: str) -> dict | None:
    with _DEPLOY_LOCK:
        d = _DEPLOYS.get(key)
        return {k: v for k, v in d.items() if k != "proc"} if d else None


def deploy_runs(limit: int = 200) -> list[dict]:
    return (load_json(DEPLOY_PATH, {"runs": []}).get("runs") or [])[-limit:]


def _record_deploy(rec: dict) -> dict:
    with _DEPLOY_LOCK:
        store = load_json(DEPLOY_PATH, {"runs": []})
        store.setdefault("runs", []).append(rec)
        store["runs"] = store["runs"][-400:]
        save_json(DEPLOY_PATH, store)
    return rec


def start_deploy(key: str, *, publish: bool = False) -> dict:
    """Run one provider's own command against one target, on a thread.

    Refuses rather than queues when the same target is already running, and
    refuses a publish whose preflight found a blocker - checked HERE and not
    only in the browser, because a gate that lives in the frontend is a gate
    that a second tab walks around.

    A `check` is allowed to run over blockers on purpose: finding out what the
    provider says about a target you are not signed in to is exactly what a
    check is for, and the answer will be the provider's refusal, which is more
    use than ours.
    """
    t = next((x for x in deploy_targets() if deploy_key(x) == key), None)
    if not t:
        return {"ok": False, "error": f"nothing here is called {key!r}"}
    spec = DEPLOY_RUN.get(t["provider"])
    if not spec:
        return {"ok": False, "error": f"nothing here knows how to deploy to {t['provider']}"}
    pre = deploy_preflight(t)
    if publish and pre["blockers"]:
        return {"ok": False, "error": "; ".join(pre["blockers"]), "preflight": pre}
    with _DEPLOY_LOCK:
        if key in _DEPLOYS:
            return {"ok": False, "error": f"{key} is already running"}
        rec = {"id": "d" + uuid.uuid4().hex[:9], "key": key, "at": now(),
               "provider": t["provider"], "rel": t["rel"], "lane": t.get("lane"),
               "package": t.get("package"), "version": t.get("version"),
               "mode": "publish" if publish else "check",
               "cmd": " ".join(spec["publish" if publish else "check"]),
               "sha": pre["sha"], "state": "running", "exit": None,
               "output": "", "seconds": None, "verify": None}
        _DEPLOYS[key] = rec
    threading.Thread(target=_deploy_thread, args=(rec, t, spec, publish),
                     daemon=True).start()
    return {"ok": True, "run": {k: v for k, v in rec.items() if k != "proc"}}


def _deploy_thread(rec: dict, t: dict, spec: dict, publish: bool):
    cmd = spec["publish" if publish else "check"]
    t0 = time.time()
    try:
        code, out = _run_deploy_cmd(cmd, Path(t["dir"]), rec)
    except Exception as e:                    # a crash here must still record
        code, out = None, f"the console failed to run it: {e}"
    rec["exit"] = code
    rec["output"] = redact(out)[-20000:]
    rec["seconds"] = round(time.time() - t0, 1)
    rec["state"] = "done" if code == 0 else "failed"
    # Verification is a SEPARATE question, asked only of a publish that claimed
    # to work - and asked of the world, not of the command that just ran. See
    # `_verify_deploy`: if it disagrees, the run is recorded as having failed,
    # because "the tool exited 0" is the sentence that produced a false rung.
    if publish and code == 0:
        rec["verify"] = _verify_deploy(t, rec)
        if rec["verify"] and rec["verify"].get("ok") is False:
            rec["state"] = "unverified"
    with _DEPLOY_LOCK:
        _DEPLOYS.pop(rec["key"], None)
    _record_deploy({k: v for k, v in rec.items() if k != "proc"})


def _run_deploy_cmd(cmd: list[str], cwd: Path, rec: dict) -> tuple[int | None, str]:
    """Run it in its own process group and stream what it says into the record.

    Streamed rather than captured at the end so the tab shows a long Fly build
    happening instead of six silent minutes - and killed by GROUP on the
    deadline, because a deploy that has started a remote builder or a docker
    build leaves those behind when only the shell is killed. Same reasoning as
    `run_check`, which measured it.
    """
    p = subprocess.Popen(cmd, cwd=str(cwd), text=True, start_new_session=True,
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    with _DEPLOY_LOCK:
        rec["proc"] = p
    lines: list[str] = []
    deadline = time.time() + DEPLOY_TIMEOUT
    for line in p.stdout:
        lines.append(line.rstrip())
        rec["output"] = redact("\n".join(lines))[-20000:]
        if time.time() > deadline:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(p.pid, sig)
                except OSError:
                    pass
                try:
                    p.wait(timeout=5)
                    break
                except subprocess.TimeoutExpired:
                    continue
            lines.append(f"(killed after {DEPLOY_TIMEOUT}s)")
            return None, "\n".join(lines)
    p.wait()
    return p.returncode, "\n".join(lines)


def cancel_deploy(key: str) -> dict:
    """Stop one. The group, not the process - see `_run_deploy_cmd`."""
    with _DEPLOY_LOCK:
        d = _DEPLOYS.get(key)
        p = d.get("proc") if d else None
    if not p:
        return {"ok": False, "error": f"{key} is not running"}
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


_URL_IN_OUTPUT = re.compile(r"https://[\w.-]+(?:/[\w./?%&=-]*)?")


def _verify_deploy(t: dict, rec: dict) -> dict | None:
    """Ask the world whether the thing is actually there.

    Deliberately not a reading of the deploy's own output. The output is the
    tool's account of what it did, and this whole tab exists because a tool's
    account of what it did was once enough to move a lane to `live_deployed`.
    So: the registry is asked what version it now serves, and a service is asked
    for its own URL over HTTP. Both can say no after a command said yes.

    Returns None when there is nothing this knows how to ask - stated as None
    rather than as a pass, because an unasked question is not a confirmation.
    """
    if t["provider"] == "npm":
        reg = _npm_registry(t["package"])
        here = t.get("version") in reg["versions"]
        return {"how": f"npm view {t['package']} versions",
                "answer": (f"{t.get('version')} is there" if here
                           else f"latest is {reg['latest'] or 'nothing'}"),
                "ok": here}
    url = None
    if t["provider"] == "fly":
        r = run(["flyctl", "status", "--json"], cwd=Path(t["dir"]))
        try:
            host = (json.loads(r.stdout) or {}).get("Hostname")
            url = f"https://{host}" if host else None
        except (ValueError, AttributeError):
            url = None
    if not url:
        # Both providers print the URL they deployed to. Falling back to it is
        # weaker than asking the platform, and it is still a real request to a
        # real address - what it cannot catch is a deploy that printed the
        # wrong URL.
        found = _URL_IN_OUTPUT.findall(rec.get("output") or "")
        url = next((u for u in found if "fly.dev" in u or "workers.dev" in u
                    or ".pages.dev" in u), None) or (found[-1] if found else None)
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "amp-publish"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.status
    except urllib.error.HTTPError as e:
        status = e.code
    except Exception as e:
        return {"how": f"GET {url}", "answer": redact(str(e))[:200], "ok": False}
    return {"how": f"GET {url}", "answer": str(status), "ok": 200 <= status < 400,
            "url": url}


# ------------------------------------------------------------------ direction
#
# A goal answers "what has to be true next". Nothing so far answers "and then
# what" - so the pipeline runs out of direction silently, by having no goals
# left, which looks exactly like being finished.
#
# Direction is the layer above goals. It is four things kept apart on purpose:
#
#   - the THESIS: the bet the whole stack is making, one paragraph
#   - the VALUES: what work is held to on the way there
#   - the FINDINGS: what the work has actually said back about both
#   - the OPEN QUESTIONS: what we believe but have not settled
#
# The first two are DOCTRINE.md and are read from it rather than restated here,
# because a second copy of the values is a second thing to keep true. The third
# already exists. The fourth is half in DOCTRINE.md under "Open theses" and half
# proposed by the architect as work reveals it.
#
# The loop this closes: when a goal finishes, look back at all four and ask
# whether this lane has anywhere left to travel. The answer is a PROPOSAL, not a
# goal. Rule 6 says what to build is Travis's decision, and a system that turned
# its own review into its own next objective would be deciding what to build by
# having decided what to review. `auto_adopt` exists for when he says otherwise,
# and defaults off.

# DIRECTION_PATH is bound by _bind_state; see _STATE_LAYOUT.
_DIRECTION_LOCK = threading.Lock()
_SECTION_HEAD = re.compile(r"^##\s+(.+?)\s*$", re.M)


def doctrine_sections() -> list[dict]:
    """DOCTRINE.md split at its `##` headings.

    The whole file, not just the injectable core: the commentary under each rule
    is where the rule came from and what it cost, and that is the half worth
    reading when deciding where to go next.
    """
    try:
        raw = DOCTRINE_PATH.read_text()
    except OSError:
        return []
    raw = raw.replace(DOCTRINE_BEGIN, "").replace(DOCTRINE_END, "")
    heads = list(_SECTION_HEAD.finditer(raw))
    out = []
    for i, h in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(raw)
        body = re.sub(r"\n-{3,}\s*$", "", raw[h.end():end].strip()).strip()
        if body:
            out.append({"title": h.group(1).strip(), "body": body})
    return out


def _section(sections: list[dict], *words: str) -> dict | None:
    for s in sections:
        low = s["title"].lower()
        if any(w in low for w in words):
            return s
    return None


def direction_store() -> dict:
    return load_json(DIRECTION_PATH, {"proposals": [], "reviews": [], "auto_adopt": False})


def _save_direction(store: dict):
    store["proposals"] = store.get("proposals", [])[-200:]
    store["reviews"] = store.get("reviews", [])[-40:]
    save_json(DIRECTION_PATH, store)


def proposals(*, kind: str | None = None, state: str = "open") -> list[dict]:
    out = direction_store().get("proposals", [])
    if kind:
        out = [p for p in out if p.get("kind") == kind]
    if state:
        out = [p for p in out if p.get("state") == state]
    return sorted(out, key=lambda p: p.get("at") or "", reverse=True)


def set_proposal(pid: str, state: str, **extra) -> dict | None:
    with _DIRECTION_LOCK:
        store = direction_store()
        for p in store.get("proposals", []):
            if p.get("id") == pid:
                p["state"] = state
                p["decided_at"] = now()
                p.update(extra)
                _save_direction(store)
                return p
    return None


def set_auto_adopt(on: bool) -> bool:
    with _DIRECTION_LOCK:
        store = direction_store()
        store["auto_adopt"] = bool(on)
        _save_direction(store)
    return bool(on)


# ------------------------------------------------------- the escalation floor
#
# Auto-adopt on its own is a machine that decides what to build next and then
# builds it, forever, with nobody reading what it found out. The operator asked
# for the middle setting: run everything unattended UNTIL something crosses a
# threshold, and then stop and ask. This is that threshold.
#
# What it is NOT is a spend limit in dollars. Workers run on the Claude
# subscription and the architect on codex, so `cost_usd` on a task record is
# what the API would have charged, not what was charged. Treating it as money
# would be a fabricated number, and the doctrine's rule 2 covers that. It is
# kept as a BURN proxy - the real ceiling is the rolling subscription window -
# and named `notional` everywhere so nobody reads it as a bill.
#
# The four gates, and why each one is worth stopping for:
#
#   contradictions - something we believed and acted on is false. The doctrine
#     says this outranks everything else in a report. Adopting more work on top
#     of an unread contradiction builds on the thing that was just disproved.
#   off_mission    - the supervisor's whole job is to hold the workspace against
#     the mission. If it says we have left it, more autonomy is more distance.
#   lane_failures  - a lane whose goals keep stopping is not a lane that needs
#     another goal. It is a lane with something wrong in it.
#   notional_day   - a crude burn ceiling, so a loop that has started going in
#     circles cannot spend the subscription window doing it all night.
#   budget_stops   - goals that ran out of rounds or tokens. Nothing restarts
#     them and nothing proposes past them, so each one silently retires a lane.
#   fleet_floor    - too few goals still running and nothing left to adopt.
#
# Every gate is a REASON TO ASK, never a reason to kill: nothing already running
# is stopped. Crossing one only means the pipeline stops feeding ITSELF new
# objectives, and the proposals stay open with your name on them.

ESCALATE_DEFAULTS = {
    "contradictions": 3,
    "lane_failures": 2,
    "notional_day_usd": 60.0,
    "off_mission": True,
    "fleet_floor": 3,
    "budget_stops": 2,
}


def escalate_policy() -> dict:
    cfg = (config().get("autonomy") or {}).get("escalate") or {}
    return {**ESCALATE_DEFAULTS, **{k: v for k, v in cfg.items() if v is not None}}


def notional_spend_today() -> float:
    """What today's workers would have cost at API prices. A burn proxy."""
    today = now()[:10]
    return round(sum(float(r.get("cost_usd") or 0)
                     for recs in (board().get("tasks") or {}).values() for r in recs
                     if str(r.get("dispatched_at") or "")[:10] == today), 2)


def lane_failure_streak(lane_name: str) -> int:
    """How many of this lane's most recent goals stopped without finishing."""
    n = 0
    for row in goals(lane_name):
        if row.get("state") == "running":
            continue
        if row.get("state") == "done":
            break
        n += 1
    return n


def escalations(lane_name: str | None = None) -> list[dict]:
    """Every threshold currently crossed. Empty means run unattended."""
    pol, out = escalate_policy(), []
    found = findings_summary()
    if pol.get("contradictions") and found.get("contradicted", 0) >= pol["contradictions"]:
        out.append({"gate": "contradictions", "at": found["contradicted"],
                    "limit": pol["contradictions"],
                    "why": f"{found['contradicted']} finding(s) say something we believed is "
                           f"false, and none has been read. Building on top of that is "
                           f"building on the thing that was just disproved."})
    if pol.get("off_mission"):
        last = (supervisor_view() or {}).get("last") or {}
        if last.get("verdict") == "off_mission":
            out.append({"gate": "off_mission", "at": "off_mission", "limit": "aligned",
                        "why": "the supervisor's last reading was that this workspace has "
                               "left its mission: " + str(last.get("assessment") or "")[:300]})
    spend = notional_spend_today()
    if pol.get("notional_day_usd") and spend >= float(pol["notional_day_usd"]):
        out.append({"gate": "notional_day", "at": spend, "limit": pol["notional_day_usd"],
                    "why": f"today's workers would have cost ${spend:.2f} at API prices. That "
                           f"is not a bill - they ran on the subscription - but it is how much "
                           f"of the window has gone, and it is past the mark you set."})
    if lane_name and pol.get("lane_failures"):
        streak = lane_failure_streak(lane_name)
        if streak >= pol["lane_failures"]:
            out.append({"gate": "lane_failures", "at": streak, "limit": pol["lane_failures"],
                        "why": f"{lane_name}'s last {streak} goals stopped without finishing. "
                               f"Another goal is not what that lane needs."})
    if pol.get("budget_stops"):
        spent = budget_stopped()
        if len(spent) >= int(pol["budget_stops"]):
            named = ", ".join(f"{r['lane']} ({r['stopped_on']}, {r['rounds']} rounds)"
                              for r in spent[:6])
            out.append({"gate": "budget_stops", "at": len(spent), "limit": pol["budget_stops"],
                        "why": f"{len(spent)} goal(s) ran out of the budget they were given "
                               f"and stopped without finishing: {named}. Nothing restarts "
                               f"them and nothing proposes past them - only a FINISHED goal "
                               f"gets a direction review - so each one holds its lane for "
                               f"good and the fleet loses a lane at a time. Extend the "
                               f"budget or close the goal; both are yours."})
    if pol.get("fleet_floor"):
        live = sum(1 for r in goals() if r.get("state") == "running")
        if live < int(pol["fleet_floor"]) and not open_proposals():
            out.append({"gate": "fleet_floor", "at": live, "limit": pol["fleet_floor"],
                        "why": f"{live} goal(s) are still running and there is nothing left "
                               f"to adopt. A review proposes only inside the lane it was "
                               f"reviewing, so a lane that says it is exhausted stays empty - "
                               f"the fleet drains a lane at a time and no gate above notices. "
                               f"What to build next is yours to say."})
    return out


def budget_stopped() -> list[dict]:
    """Goals that stopped because they ran out of rounds or tokens, not because
    they finished and not because they need a decision.

    These are the quiet way the fleet shrinks. A goal that hits `GOAL_MAX_ROUNDS`
    is set to `blocked`, and three separate things then decline to touch it:
    `idle_goals` only considers goals that still say `running`, the direction
    review that proposes the NEXT objective in a lane fires only on the `done`
    path, and `goals_stuck` counts it alongside the goals correctly waiting on an
    operator decision. So the lane it was holding never runs anything again, and
    the board reads as if somebody was asked a question.

    Restarting them automatically is not the fix - the cap exists so that
    deciding to spend more reaches you (doctrine rule 6). Being able to see them
    is.
    """
    return [r for r in goals()
            if r.get("state") == "blocked" and r.get("stopped_on") in ("rounds", "tokens")]


def open_proposals() -> list[dict]:
    """Proposed objectives nobody has adopted or turned down yet."""
    return [p for p in direction_store().get("proposals", [])
            if p.get("state") == "open" and p.get("kind") == "goal"]


# ------------------------------------------------------------- four scores
# Every proposed objective carries four numbers, and each one exists because a
# different question has to be answered before a lane is spent on it:
#
#   odds     - will it FINISH: every done-condition judged met, without stopping
#     to ask the operator for something.
#   need     - how much the mission wants it NOW: which rung of the evidence
#     ladder it moves, and what stops being unknown when it lands.
#   cost     - what it will spend to find out, in notional dollars.
#   headroom - whether looking at it AGAIN would help: is it held back by how it
#     is written, or by something only the operator can supply.
#
# The discipline that makes them worth anything is that EACH ANSWERS TO A
# RECORDED EVENT. "How good is this idea" cannot be checked afterwards and
# therefore cannot be wrong, which makes it worthless as a gate - a model would
# be free to say 0.9 forever. So: odds is checked against the goal's final
# state, need against whether the rung it claimed actually moved in the next
# review, cost against what the goal's tasks really billed, and headroom against
# whether the next sharpen round ACTUALLY RAISED the score - which the sharpener
# already records, because it writes down what a proposal scored before and
# after. `calibration()` is those four comparisons, and it is the only reason any
# of this is worth more than a model asserting numbers about itself. Read it
# before trusting a bar.
#
# Headroom is the one that decides where an architect call goes. Sharpening is
# not free and only one runs per tick, so the question "which held proposal is
# worth looking at again" needs an answer better than "the oldest". A proposal
# stuck at 30% because it bundles five things can be split and will move. One
# stuck at 30% because it needs a credential nobody has will score 30% forever,
# and spending two rounds discovering that is the waste this number prevents.
#
# A fifth number is DERIVED and never stated: `worth`, which is
# odds x need / cost. It is what ranks proposals when several are waiting and
# only one can be started, and it is deliberately not a gate - a cheap certainty
# that the mission barely wants should not outrank the thing we actually need.
#
# What was rejected, and why, so it does not get re-proposed: blast radius (real
# but the wrong layer - workers already run in isolated worktrees and `apply`
# refuses to merge), urgency split from importance (no distinct recorded event
# checks it, so it would be a dial with nothing behind it), and duplication
# (already handled by the dedupe and by telling the architect what is open).
#
# The bars are the dial between "adopt everything" and "ask me about
# everything". They are deliberately NOT `escalations()` gates: those are facts
# about the workspace and hold every lane at once, these are facts about one
# objective and hold only that objective.

DEFAULT_ADOPT_CONFIDENCE = 0.6
DEFAULT_ADOPT_NEED = 0.5

# A round that moved the odds by less than this moved nothing worth paying for.
# Sharpening stops when the sharpening stops working, not after a set number of
# tries - see `sharpen_converged`.
SHARPEN_GAIN_FLOOR = 0.02

# The spend backstop, and nothing more. Convergence is the real stopping rule;
# this exists only so an objective that improves by a hair every time cannot
# bill for ever.
SHARPEN_HARD_CAP = 6

# Below this much headroom the harness stops spending calls on a proposal by
# itself. Not a bar the operator sets, because it is not a decision about how
# much autonomy to allow - it is the point past which the architect has said its
# own next answer would be the same one.
SHARPEN_FLOOR = 0.15

# Divisor floor for `worth`. A proposal estimated at nothing is not infinitely
# worth doing, it is one nobody has costed - and without a floor it would sort
# above every real proposal forever.
COST_FLOOR_USD = 0.25


def pct(c) -> str:
    """A score as a percentage, or the word for not having one. `0.0` is a score."""
    return "unscored" if c is None else f"{float(c):.0%}"


def usd(c) -> str:
    return "uncosted" if c is None else f"${float(c):.2f}"


def _num(v, lo: float, hi: float):
    """One number off an architect reply, clamped, or None if it was not one.

    Absent or unreadable is `None`, never `0.0`. They mean opposite things -
    0.0 is "this will not finish" or "the mission does not want this", None is
    "nobody has judged it" - and a missing field quietly becoming the most
    damning number in the range would hold a proposal back, or rank it top, for
    a reason nobody ever stated.
    """
    try:
        return round(min(hi, max(lo, float(v))), 3)
    except (TypeError, ValueError):
        return None


def _scored(n: dict) -> dict:
    """The four scores off an architect reply, clamped and typed."""
    c, need = _num(n.get("confidence"), 0.0, 1.0), _num(n.get("need"), 0.0, 1.0)
    return {"confidence": c,
            "need": need,
            "why_need": str(n.get("why_need") or "")[:400],
            # No upper clamp worth naming: an honest estimate of a very large
            # job is information, and squashing it to a ceiling would make the
            # expensive thing sort like a cheap one.
            "cost_usd": _num(n.get("cost_usd"), 0.0, 10_000.0),
            "headroom": _num(n.get("headroom"), 0.0, 1.0),
            "why_headroom": str(n.get("why_headroom") or "")[:400],
            "unknowns": [str(u)[:300] for u in (n.get("unknowns") or []) if str(u).strip()][:6],
            "scored_at": now() if c is not None else None}


def _bar(key: str, default: float) -> float:
    cfg = config().get("autonomy") or {}
    try:
        return min(1.0, max(0.0, float(cfg.get(key, default))))
    except (TypeError, ValueError):
        return default


def adopt_bar() -> float:
    """The odds a proposal must reach to be adopted with nobody watching."""
    return _bar("adopt_confidence", DEFAULT_ADOPT_CONFIDENCE)


def need_bar() -> float:
    """How much the mission must want it before it is started unattended."""
    return _bar("adopt_need", DEFAULT_ADOPT_NEED)


def worth(p: dict):
    """Expected mission movement per dollar. The ranking key, never a gate.

    None unless all three are known, because a partial answer here is worse
    than no answer: an unscored proposal would rank either top or bottom on
    whichever field happened to be missing.
    """
    c, need = p.get("confidence"), p.get("need")
    if c is None or need is None:
        return None
    cost = p.get("cost_usd")
    return round(c * need / max(COST_FLOOR_USD, cost if cost is not None else COST_FLOOR_USD), 3)


def proposal_hold(p: dict) -> str | None:
    """Why this proposal may not be adopted unattended, or None if it may.

    Cost is not tested here on purpose. It ranks, and it feeds the burn ceiling
    that already exists in `escalations()`; making it a third bar would mean a
    proposal we badly need and are confident about gets refused for being big,
    which is a decision about what to spend and therefore the operator's.
    """
    # The lane's mode, before anything about the proposal itself. Every path
    # that adopts unattended comes through here, so this is the whole of the
    # enforcement for goals - and because the answer is a hold reason rather
    # than a filter, a proposal into a lane that is not building stays visible
    # in Direction with the mode as its stated reason, instead of vanishing.
    stopped = lane_admits(p.get("lane") or "", work_kind(p.get("source")))
    if stopped:
        return stopped
    c, need = p.get("confidence"), p.get("need")
    # Named separately rather than as one "unscored", because they are different
    # missing facts with different fixes, and a proposal scored under the old
    # single-number scheme has one of them and not the other.
    if c is None and need is None:
        return "nobody has scored it yet"
    if c is None:
        return "nobody has judged whether it would finish"
    if need is None:
        return "nobody has judged how much the mission wants it"
    bar, nbar = adopt_bar(), need_bar()
    if c < bar:
        return f"{c:.0%} odds of finishing, under the {bar:.0%} bar you set"
    if need < nbar:
        return f"the mission wants it {need:.0%}, under the {nbar:.0%} bar you set"
    # Over the bars and still holding room to improve. Clearing the bars says
    # the objective is worth starting; it does not say it is worth starting AS
    # WRITTEN, and those are different questions that this function used to
    # answer with one number. `headroom` is the architect's own claim that
    # reading it again would raise its score - so adopting on the spot spends a
    # worker on a version somebody has already said is not the best available
    # one, and the improvement can never be made afterwards because adoption is
    # what retires the proposal.
    #
    # Only on a STATED number. `headroom` absent is nobody having judged it, not
    # a claim of nothing to do, and holding work on a field that was never
    # written is the failure `_num` returns None to avoid.
    #
    # `sharpen_converged` is the exit, and it has to be here or this is a trap:
    # an architect that keeps asserting headroom while the rounds stop moving
    # the odds would hold the objective for ever. Converged means the measured
    # gain died or the spend cap hit, and either way the next round buys
    # nothing, so the objective starts at whatever it reached.
    return _sharpen_hold(p)


def _sharpen_hold(p: dict) -> str | None:
    """The sharpening hold on its own, whether or not it is the operative one."""
    head = p.get("headroom")
    if head is None or head < SHARPEN_FLOOR or sharpen_converged(p):
        return None
    return (f"{head:.0%} room left to improve it, over the {SHARPEN_FLOOR:.0%} "
            f"worth another look — sharpening it first")


def held_for_sharpening(p: dict) -> bool:
    """Whether the only thing between this objective and a worker is a rewrite.

    Asked because the panel has to say whether the operator is being told to do
    something. Every other hold waits on a person; this one waits on a call the
    harness makes itself, and rendering them alike sends the operator to look at
    a queue that is already draining.

    Compared against `proposal_hold` rather than tested on its own, so that a
    proposal held by a bar AND carrying headroom reports the bar - which is the
    reason it is actually stopped, and the only one a person can act on.
    """
    why = _sharpen_hold(p)
    return why is not None and proposal_hold(p) == why


def lane_record(lane_name: str, n: int = 8) -> dict:
    """How this lane's recent goals actually ended.

    The evidence an estimate is made against, and the reason it is given to the
    architect rather than left to be inferred: a lane whose last four goals all
    stopped on `operator` is going to stop on `operator` again, and that is a
    fact about the lane that no amount of reading the objective reveals.
    """
    rows = [r for r in goals(lane_name) if r.get("state") not in ("running", "planning")][:n]
    out = {"n": len(rows), "done": 0, "stops": {}}
    for r in rows:
        if r.get("state") == "done":
            out["done"] += 1
        else:
            k = r.get("stopped_on") or "unknown"
            out["stops"][k] = out["stops"].get(k, 0) + 1
    return out


def _deploy_block(lane_name: str) -> str:
    """What this lane has actually shipped, for the architect who judges rungs.

    `live_deployed` is a rung a reviewer awards, and until this existed there
    was nothing on disk for them to award it FROM - so it was awarded from a
    worker's sentence about having deployed something, which is how this stack
    came to hold a `live_deployed` claim that no deploy supports.

    Every line here is a run that happened: its command, its exit, and - kept
    separate on purpose - what an independent question about the world answered
    afterwards. The separation is the whole value. A run that exited 0 and
    whose URL then returned 502 reads as `exit 0 · GET ... 502`, and an
    architect shown that will not write `live_deployed`.
    """
    runs = [r for r in deploy_runs() if r.get("lane") == lane_name
            and r.get("mode") == "publish"][-6:]
    if not runs:
        return ("# What this lane has shipped\n\nNothing. No publish to any provider "
                "has been recorded for this lane, so no claim about it being deployed "
                "has evidence behind it here.")
    lines = []
    for r in reversed(runs):
        v = r.get("verify") or {}
        checked = (f"{v.get('how')} → {v.get('answer')}" if v
                   else "nothing independent was asked")
        lines.append(f"- {r.get('at', '')[:16]} `{r.get('cmd')}` in {r.get('rel')} "
                     f"at {r.get('sha') or 'an unnamed commit'} → exit {r.get('exit')}; "
                     f"then {checked}")
    return ("# What this lane has shipped\n\n" + "\n".join(lines)
            + "\n\nThe part after `then` is a separate question asked of the world "
              "afterwards, not the deploy tool's own account of itself. A run that "
              "exited 0 and was not independently confirmed does not settle "
              "`live_deployed`.")


def _record_block(lane_name: str) -> str:
    rec = lane_record(lane_name)
    if not rec["n"]:
        return ""
    stops = ", ".join(f"{v} on {k}" for k, v in sorted(rec["stops"].items())) or "none"
    return (f"# How this lane actually ends up\n\n"
            f"Of its last {rec['n']} settled goals, {rec['done']} finished. "
            f"The rest stopped: {stops}. Score against this, not against the "
            f"objective read on its own.")


LADDER_RUNGS = ("spec", "in_tree", "live_local", "live_deployed", "external")


def _rung_key(lane_name: str | None, claim: str, to: str) -> str:
    """What makes two mentions of a ladder entry the same entry.

    A ladder entry has no id - it is prose written by a reviewer - so the claim
    text is all there is to match on. Normalised, because the finding that
    retracts an entry quotes it rather than copying it.
    """
    return f"{lane_name or ''}\u0000{_norm_prompt(claim or '')}\u0000{to or ''}"


def retractions() -> dict[str, dict]:
    """Ladder entries a later judgement has taken back, by `_rung_key`."""
    return {_rung_key(r.get("lane"), r.get("claim"), r.get("to")): r
            for r in direction_store().get("retractions", [])}


def _claim_key(lane_name: str | None, claim: str) -> str:
    """What makes two statements of a false claim the same correction.

    A ladder entry is matched by quoting it, so `_rung_key` can take the text as
    it stands. A correction is written in the architect's own words each time it
    comes up, so the same claim comes back with a capital letter or a full stop
    on the end - which is not a second claim, and recording it as one would fill
    the scoring block with the same sentence.
    """
    return _rung_key(lane_name, (claim or "").strip().strip(".;,:—-").strip(), "")


def corrections(lane_name: str | None = None) -> list[dict]:
    """Claims that were made and are false, but were never counted as rungs."""
    rows = direction_store().get("corrections") or []
    return [c for c in rows if lane_name is None or c.get("lane") == lane_name]


def record_correction(lane_name: str, claim: str, why: str, *,
                      finding_id: str | None = None, goal_id: str | None = None,
                      source: str = "settle") -> dict | None:
    """Write down that a claim the stack acted on is false, and keep it in view.

    The companion to `retract_rung`, for the case that stopped the settling loop
    dead: most false claims are never ladder entries at all. They are sentences
    in a worker's report, or a done-condition somebody wrote, and `retract_rung`
    correctly refuses them - there is no entry to take back, and inventing one
    would be a retraction that fabricated the claim it walked back.

    But "nothing to retract" was then treated as "nothing can be done", so the
    finding stayed open forever and went on blocking every proposal in every
    lane. That is the wrong conclusion. A claim that was never counted still
    needs to stop being repeated, and the consequence the harness can actually
    perform is to put the correction where the next architect will read it -
    `_need_block`, which is shown to every proposal scored anywhere on the
    stack. A correction nothing reads would be decoration; this one is read on
    every scoring call for the lane it names.

    Deliberately NOT a rung movement. Nothing here moves a lane up or down: the
    claim never earned a rung, so taking one away would be as false as the claim
    was. Returns None on an empty claim - a correction with nothing in it is
    exactly the settlement-by-assertion this whole path exists to prevent.
    """
    claim = (claim or "").strip()
    if not claim or not lane_name:
        return None
    key = _claim_key(lane_name, claim)
    for c in corrections():
        if _claim_key(c.get("lane"), c.get("claim")) == key:
            return c
    rec = {"id": "c" + uuid.uuid4().hex[:9], "at": now(), "lane": lane_name,
           "claim": claim[:1000], "why": (why or "")[:1000],
           "finding_id": finding_id, "goal_id": goal_id, "source": source}
    with _DIRECTION_LOCK:
        store = direction_store()
        store.setdefault("corrections", []).append(rec)
        _save_direction(store)
    return rec


def retract_rung(lane_name: str, claim: str, to: str, why: str, *,
                 finding_id: str | None = None, source: str = "settle") -> dict | None:
    """Take back a rung a review recorded, because the evidence did not hold.

    The review itself is left exactly as written - it is the record of what was
    said at the time, and editing it would destroy the only account of how the
    mistake was made. The retraction sits beside it and `lane_rungs` stops
    counting the entry, which is the only thing that has to change.

    Returns None if no such entry was ever recorded. That matters: it is the
    check that stops a retraction inventing the claim it walks back.
    """
    key = _rung_key(lane_name, claim, to)
    hit = None
    for r in direction_store().get("reviews", []):
        for e in (r.get("ladder") or []):
            if _rung_key(r.get("lane"), e.get("claim"), e.get("to")) == key:
                hit = {"review_id": r.get("id"), "at_review": r.get("at"),
                       "from": e.get("from"), "evidence": e.get("evidence")}
                break
    if not hit:
        return None
    if key in retractions():
        return retractions()[key]
    rec = {"id": "x" + uuid.uuid4().hex[:9], "at": now(), "lane": lane_name,
           "claim": (claim or "")[:1000], "to": to, "from": hit["from"],
           "why": (why or "")[:1000], "finding_id": finding_id, "source": source,
           "review_id": hit["review_id"]}
    with _DIRECTION_LOCK:
        store = direction_store()
        store.setdefault("retractions", []).append(rec)
        _save_direction(store)
    return rec


def lane_rungs() -> dict[str, str]:
    """The highest rung each lane has actually been judged to reach.

    Derived from the reviews, not asserted anywhere: a review records the claims
    a finished goal moved and the rungs it moved them between, so the top of
    that list is the furthest the lane's evidence has ever got. This is the
    single most useful fact for judging how much the mission wants a proposal,
    and it is exactly what an architect reading one objective cannot see.

    Retracted entries do not count. Without that this reading is monotonic by
    construction - the highest rung ever claimed can never fall, even after a
    later review judges that it was never earned - and a lane that overstated
    itself once would go on being scored as if it had not.
    """
    gone = retractions()
    top: dict[str, str] = {}
    for r in direction_store().get("reviews", []):
        lane_name = r.get("lane")
        for e in (r.get("ladder") or []):
            rung = e.get("to")
            if not lane_name or rung not in LADDER_RUNGS:
                continue
            if _rung_key(lane_name, e.get("claim"), rung) in gone:
                continue
            if lane_name not in top or LADDER_RUNGS.index(rung) > LADDER_RUNGS.index(top[lane_name]):
                top[lane_name] = rung
    return top


def _need_block(lane_name: str) -> str:
    """Where every lane stands, so `need` is scored against the stack, not the lane.

    Deliberately the WHOLE stack and not just this lane. Need is comparative -
    it is how much the mission wants this next, and "next" means instead of
    everything else - so an architect shown one lane in isolation has no way to
    say anything but "quite a lot".
    """
    top = lane_rungs()
    fixed: dict[str, list[dict]] = {}
    for c in corrections():
        fixed.setdefault(c.get("lane") or "", []).append(c)
    # Not `if not top`. A stack with no rungs can still have corrections, and it
    # is the case where they matter most - nothing has been earned, so the only
    # thing this block has to say is which of the claims lying around is false.
    # Modes count for the same reason and were the second thing to fall through
    # this hole: a lane can be frozen on a stack that has never recorded a rung,
    # and staying silent there sends the architect to propose into it.
    shut = {n for n in (config().get("lanes") or {}) if lane_mode(n) != DEFAULT_LANE_MODE}
    if not top and not fixed and not shut:
        return ""
    rows = []
    for name in sorted(set(top) | set(fixed) | shut | {lane_name}):
        rung = top.get(name)
        # The rung is stated for every lane including the shut ones, because a
        # rung is a fact about what was earned and stays true whether or not
        # anyone is still working there - blanking it would make the ladder lie
        # about the stack. What is added is the mode, and only where it is not
        # `build`: an architect that proposes into an archived lane is spending a
        # call on work that `proposal_hold` is then going to refuse, and the only
        # way it can know that is by being told here.
        mode = lane_mode(name)
        rows.append(f"- **{name}**{' (the lane you are scoring)' if name == lane_name else ''}: "
                    + (f"evidence has reached `{rung}`" if rung
                       else "no claim has ever been judged past `spec`")
                    + (f" — **{mode}**: {MODE_MEANS[mode]}, so do not propose "
                       f"{'anything' if mode in ('frozen', 'archived') else 'new development'} "
                       f"here" if mode != DEFAULT_LANE_MODE else ""))
    out = ("# How far each lane's evidence has actually got\n\n"
           + "\n".join(rows)
           + "\n\nThese are the rungs recorded by earlier reviews, so they are what the "
             "stack has EARNED, not what it aims at. Score `need` against the distance "
             "between this and the mission.")
    if fixed:
        # The claims that were made, acted on, and turned out to be false without
        # ever having been counted as a rung. They move nothing on the ladder,
        # which is exactly why they need saying: nothing else in this block would
        # show them, and the failure they cause is a later call confidently
        # repeating a sentence the stack has already disproved.
        lines = []
        for name in sorted(fixed):
            for c in fixed[name]:
                lines.append(f"- **{name}**: {str(c.get('claim') or '')[:300]}"
                             + (f" — {str(c.get('why') or '')[:220]}" if c.get("why") else ""))
        out += ("\n\n# Claims already found false here — do not repeat them\n\n"
                + "\n".join(lines)
                + "\n\nThese were never rungs, so nothing above was inflated by them. "
                  "They are listed because they were believed once and are not true.")
    return out


def goal_spend(gid: str) -> float:
    """What a goal's workers actually billed, notionally. The check on `cost`."""
    return round(sum(float(r.get("cost_usd") or 0)
                     for recs in (board().get("tasks") or {}).values() for r in recs
                     if r.get("goal_id") == gid), 2)


def _moved_a_rung(gid: str) -> bool | None:
    """Did the review after this goal actually move a claim up the ladder?

    The check on `need`, and the weakest of the three - a rung moves because a
    later architect call judged the evidence reached it, so this is one model's
    reading of another's estimate. It is still worth having, because the two
    calls see different things: the scorer sees an objective and the reviewer
    sees what the work came back with. None means no review has run yet, which
    is not evidence either way.
    """
    revs = [r for r in direction_store().get("reviews", []) if r.get("goal_id") == gid]
    if not revs:
        return None
    return any(r.get("ladder") for r in revs)


def _refine_record() -> dict:
    """Whether claimed headroom turned into anything. The check on `headroom`.

    Reads the log every sharpen attempt writes: what the proposal was claimed to
    have left in it, what it scored before, and what the best version of it
    scored after. The event is `after > before`, so this is a probability
    checked the same way `confidence` is.

    It needs no goal to have run, which makes it the fastest of the four to
    become real - the loop closes inside the pipeline instead of waiting on a
    fleet. That is also its limit: it says the sharpener knows when it can help,
    not that helping was worth the call.
    """
    n = rose = 0
    said = gain = 0.0
    for p in direction_store().get("proposals", []):
        for a in (p.get("sharpen_log") or []):
            if a.get("claimed_headroom") is None or a.get("before") is None \
                    or a.get("after") is None:
                continue
            n += 1
            said += a["claimed_headroom"]
            if a["after"] > a["before"]:
                rose += 1
            gain += a["after"] - a["before"]
    return {"n": n,
            "stated": round(said / n, 2) if n else None,
            "actual": round(rose / n, 2) if n else None,
            "gain": round(gain / n, 3) if n else None,
            "floor": SHARPEN_FLOOR}


def calibration() -> dict:
    """What the four scores turned out to be worth.

    Reads only what was already recorded: an adopted proposal names the goal it
    opened, and that goal has since finished or stopped. Each score is checked
    against its own event and they are kept apart, because they can be wrong in
    opposite directions - consistently over-confident and consistently
    under-costed would cancel in any single number and stay invisible.

    Goals still open are counted nowhere. They are not evidence yet, and
    counting them as failures is how a calibration table talks itself into
    saying the estimates are worse than anyone has established.
    """
    edges = [0.0, 0.5, 0.7, 0.85]
    bands = [{"from": lo, "to": (edges[i + 1] if i + 1 < len(edges) else 1.0),
              "n": 0, "finished": 0} for i, lo in enumerate(edges)]
    n = fin = 0
    stated = 0.0
    need_n = need_moved = 0
    need_said = 0.0
    cost_n = 0
    cost_said = cost_real = 0.0
    for p in direction_store().get("proposals", []):
        c = p.get("confidence")
        if p.get("state") != "adopted" or not p.get("goal_id"):
            continue
        g = load_goal(p["goal_id"])
        if not g or g.get("state") in ("planning", "running"):
            continue
        if c is not None:
            row = [b for b in bands if c >= b["from"]][-1]
            row["n"] += 1
            n += 1
            stated += c
            if g.get("state") == "done":
                row["finished"] += 1
                fin += 1
        moved = _moved_a_rung(p["goal_id"])
        if p.get("need") is not None and moved is not None:
            need_n += 1
            need_said += p["need"]
            need_moved += 1 if moved else 0
        # Only goals that ran to a stop are costed. A goal stopped early spent
        # less than it would have, and reading that as an over-estimate would
        # teach the scorer to under-cost everything.
        real = goal_spend(p["goal_id"])
        if p.get("cost_usd") is not None and real > 0 and g.get("state") == "done":
            cost_n += 1
            cost_said += p["cost_usd"]
            cost_real += real
    for b in bands:
        b["rate"] = round(b["finished"] / b["n"], 2) if b["n"] else None
    return {
        "bands": bands, "n": n, "bar": adopt_bar(), "need_bar": need_bar(),
        "stated": round(stated / n, 2) if n else None,
        "actual": round(fin / n, 2) if n else None,
        "need": {"n": need_n,
                 "stated": round(need_said / need_n, 2) if need_n else None,
                 "moved": round(need_moved / need_n, 2) if need_n else None},
        "cost": {"n": cost_n,
                 "stated": round(cost_said / cost_n, 2) if cost_n else None,
                 "actual": round(cost_real / cost_n, 2) if cost_n else None},
        "refine": _refine_record(),
    }


# Stated once and used by both the review and the sharpener. They used to carry
# near-identical copies of this text, which is how two definitions of the same
# number quietly stop being the same number.
_SCORE_RULES = (
    "- `confidence` is a probability, and it is a probability of ONE recorded event: that a "
    "worker fleet given this objective finishes it and has every one of its done-conditions "
    "judged met, WITHOUT stopping to ask the operator for something. Stopping to ask counts "
    "as not finishing. It is not how good the idea is and it is not how strongly you hold "
    "it: it is the share of times this would land. The number is written down, the outcome "
    "is written down beside it, and the two are compared - so a run of 0.9s that keep "
    "stopping is a visible fact about the scoring, not about the lanes. Anything resting on "
    "money, a credential, an account, a deployed target, or a decision about what counts as "
    "good enough is a stop, and an objective that needs one is BELOW 0.5 however good it "
    "is.\n"
    "- `need` is how much the MISSION wants this objective next, and it is a separate "
    "question from whether it would succeed. 1.0 is the thing the mission is currently "
    "blocked on. 0.0 is real work that the mission would not miss. Score it against the "
    "mission and the rung each lane is actually on, which you have been given - not against "
    "how interesting it is, and not against how close to done it feels. Tidying, "
    "refactoring, and more tests of a thing already at its rung are LOW however cheap and "
    "safe they are. What moves a lane to a rung it has never reached, or answers something "
    "the mission is waiting on, is HIGH even when it is hard.\n"
    "- `why_need` must name the specific thing: which claim moves from which rung to which, "
    "or which open question closes. \"It advances the mission\" is not an answer. This is "
    "checked - the review that follows the goal records which claims actually moved, and a "
    "high `need` whose rung never moved is a visible fact about the scoring.\n"
    "- `cost_usd` is what you expect the workers to burn finding out, at API prices, for "
    "this objective end to end including the retries a job like this usually needs. It is "
    "compared against what the goal really billed. Do not anchor on a round number: a "
    "one-file change that a worker verifies with an existing check is well under a dollar, "
    "and something that has to stand up a substrate is many.\n"
    "- `headroom` is the odds that LOOKING AT THIS AGAIN would raise its score - that the "
    "objective is held back by how it is written rather than by the world. High means there "
    "is something to do: it bundles several things that could be split, it reaches for a rung "
    "when a lower one would do first, it is vague where being specific would make it "
    "checkable. Low means the score is what it is: the thing it needs does not exist, or only "
    "the operator can supply it, and no amount of rewording changes that. An objective that "
    "is already good scores LOW too - there is nothing to improve. This decides where the "
    "next architect call is spent, and it is checked: the score before and after each attempt "
    "is recorded, so claiming headroom that never materialises is visible.\n"
    "- `why_headroom` names the specific move - \"split the deploy off from the harness\", "
    "\"aim at in_tree first\" - or says plainly that there is none. Do not describe the move "
    "and then not make it in `revision`; if you can see it, make it.\n"
    "- `unknowns` is what the objective needs that nobody has established exists, one clause "
    "each. An empty list claims everything it touches is already there, so return one only "
    "when that is true. This is the list that gets attacked to raise the number."
)

DIRECTION_SYSTEM = (
    "You are the consulting architect deciding where a codebase should go next. A goal "
    "has just finished in one lane of the operator's stack. You are given the operator's "
    "doctrine - the thesis the whole stack is betting on, the evidence ladder every claim "
    "is stated in, and the theses that are believed but not settled - then the goal that "
    "just closed with the evidence for each of its conditions, what the work reported back "
    "about the doctrine, what else is already open in this lane, and the repository as it "
    "now is.\n\n"
    "Reply with one JSON object and nothing else:\n"
    "{\n"
    '  "assessment": "where this lane now stands against the thesis, 2-4 sentences",\n'
    '  "ladder": [{"claim": "...", "from": "spec|in_tree|live_local|live_deployed|external",\n'
    '              "to": "...", "evidence": "what settles it"}],\n'
    '  "next": [{"objective": "one objective, what has to be true when it is finished",\n'
    '            "why": "what it buys, in terms of the thesis or an open question",\n'
    '            "confidence": 0.0,\n'
    '            "need": 0.0,\n'
    '            "why_need": "which rung moves, or which open question closes, if it lands",\n'
    '            "cost_usd": 0.0,\n'
    '            "headroom": 0.0,\n'
    '            "why_headroom": "the move that would raise this, or that there is none",\n'
    '            "unknowns": ["what this needs that is not established to exist"]}],\n'
    '  "research": [{"question": "what we do not know", "why": "why it matters",\n'
    '                "settled_by": "the observation or experiment that would answer it"}],\n'
    '  "exhausted": false,\n'
    '  "why_exhausted": "if there is genuinely nowhere worth going in this lane, say why"\n'
    "}\n\n"
    "Rules:\n"
    "- `ladder` is only for claims this finished goal actually moved, with the evidence "
    "that moved them. An empty list is a normal answer. Never move a claim to a rung the "
    "evidence you were given does not reach - when in doubt, downgrade.\n"
    "- `next` is what is worth doing next in THIS lane, grounded in the repository state "
    "you were given and in what is still unsettled. Zero, one, or two - not a backlog. Do "
    "not restate work that is already open, and do not propose work whose only merit is "
    "that it is more work. If the honest answer is that this lane is finished for now, "
    "return an empty list and set `exhausted` to true with a reason.\n"
    + _SCORE_RULES + "\n"
    "- `research` is the highest-value thing you can return: something we believe and have "
    "not tested, or that this goal has just made answerable. It must be stated so that it "
    "could come out either way, and `settled_by` must be something someone could actually "
    "do. If nothing new opened up, return an empty list.\n"
    "- You are proposing. The operator decides what gets built and what gets adopted into "
    "the doctrine. Do not write as if the decision is made."
)


def _direction_context(g: dict, sections: list[dict]) -> str:
    """Everything the architect needs to answer 'and then what' for one lane."""
    lane_name = g.get("lane") or ""
    open_theses = _section(sections, "open theses", "open questions")
    parts = [mission_block().strip(),
             doctrine_block("The doctrine this stack is held to").strip()]
    if open_theses:
        parts.append(f"# {open_theses['title']}\n\n{open_theses['body']}")

    q = [p["text"] for p in proposals(kind="question")][:10]
    if q:
        parts.append("# Questions already open, do not restate them\n\n"
                     + "\n".join(f"- {t}" for t in q))

    parts.append(f"# The goal that just finished\n\n{g.get('objective', '')}")
    dod = "\n".join(
        f"- [{'x' if d.get('met') else ' '}] {d.get('text', '')}"
        + (f"\n      check: `{d['check']}` exit {d.get('check_exit')}" if d.get("check") else "")
        + (f"\n      evidence: {str(d.get('evidence'))[:400]}" if d.get("evidence") else "")
        for d in (g.get("done") or []))
    if dod:
        parts.append(f"## Its definition of done, as judged\n\n{dod}")
    tasks = "\n".join(f"- ({t.get('state')}) {t.get('text', '')}" for t in (g.get("tasks") or []))
    if tasks:
        parts.append(f"## What was actually done\n\n{tasks}")

    fs = [f for f in findings(lane=lane_name)][:8]
    if fs:
        parts.append("# What the work reported back about the doctrine\n\n"
                     + "\n".join(f"- **{f['bearing']}** ({f.get('source')}): {f['text'][:600]}"
                                 for f in fs))

    others = [x for x in goals(lane_name)
              if x["id"] != g["id"] and x["state"] in ("planning", "running", "blocked")]
    if others:
        parts.append("# Already open in this lane, do not propose these again\n\n"
                     + "\n".join(f"- {o['objective'][:200]}" for o in others))

    rec = _record_block(lane_name)
    if rec:
        parts.append(rec)
    # The evidence for the one rung this stack has claimed without any. Always
    # appended, including when it is empty - "nothing has been shipped" is the
    # sentence that has to reach the reviewer, and a block that only appears
    # when there is something to show is a block that is silent in exactly the
    # case it was written for.
    parts.append(_deploy_block(lane_name))
    stands = _need_block(lane_name)
    if stands:
        parts.append(stands)
    # What this lane is FOR. Explore has been given this since it was written and
    # a review never was, which is backwards: explore proposes into a lane it has
    # no goal in, and a review proposes into the lane it just watched work in -
    # so the review is where `need` is scored most often, and it was the one
    # scoring it against nothing on disk.
    spec = _specs_block(lane_name, [])
    if spec:
        parts.append(spec)
    parts.append(f"# The repository right now\n\n{goal_brief(lane_name)}")
    return "\n\n".join(parts)


# ------------------------------------------------------------------- exploring
#
# A review answers "and then what" about ONE goal that just finished. That is
# the right question most of the time and it is why it fires on a goal closing.
# But it means proposals only ever arrive behind finished work, so the moment
# the fleet is held - by a gate, by a bar, by there being nothing left in a lane
# - is exactly the moment nothing new can appear. Exploring is the other door:
# it is asked for, it reads across every lane at once instead of down one, and
# it may go and look outside the workspace.
#
# It is deliberately NOT on the heartbeat. A button that manufactures work on a
# timer manufactures work.

EXPLORE_SYSTEM = (
    "You are the consulting architect for an operator's whole stack, not for one lane of "
    "it. Nothing has just finished. You are being asked, deliberately, where else there is "
    "to go - so the answers worth giving are the ones a review of a single finished goal "
    "structurally cannot see.\n\n"
    "You are given the operator's mission and doctrine, how far each lane's evidence has "
    "actually got, what the work in every lane has reported back, every question already "
    "open, and everything already proposed - including what was turned down.\n\n"
    "Three places to look, in this order:\n"
    "1. ACROSS LANES. What one lane has learned or built that another lane is stuck "
    "without. What two lanes are each solving separately. What a lane's finding implies "
    "somewhere it was never reported. This is the highest-value thing here and it is the "
    "one thing nobody else in this system is ever asked.\n"
    "2. OUTSIDE. If you can search the web, do: a standard, a released tool, a published "
    "result, or a change in the world that makes something here cheaper, unnecessary, or "
    "newly possible. Cite what you actually read.\n"
    "3. REFRAMING. Something the doctrine assumes that could be tested cheaply, or an "
    "objective everything else is waiting on that nobody has stated.\n\n"
    "Reply with one JSON object and nothing else:\n"
    "{\n"
    '  "assessment": "where the stack stands as a whole and what you went looking for, '
    '2-4 sentences",\n'
    '  "next": [{"objective": "one objective, what has to be true when it is finished",\n'
    '            "lane": "which lane it belongs to, from the list you were given",\n'
    '            "why": "what it buys, in terms of the mission or an open question",\n'
    '            "origin": "cross_lane|outside|reframe",\n'
    '            "sources": ["url you actually read, for origin=outside"],\n'
    '            "confidence": 0.0,\n'
    '            "need": 0.0,\n'
    '            "why_need": "which rung moves, or which open question closes, if it lands",\n'
    '            "cost_usd": 0.0,\n'
    '            "headroom": 0.0,\n'
    '            "why_headroom": "the move that would raise this, or that there is none",\n'
    '            "unknowns": ["what this needs that is not established to exist"]}],\n'
    '  "research": [{"question": "what we do not know", "why": "why it matters",\n'
    '                "settled_by": "the observation or experiment that would answer it",\n'
    '                "lane": "which lane it bears on"}],\n'
    '  "exhausted": false,\n'
    '  "why_exhausted": "if there is genuinely nothing here worth proposing, say why"\n'
    "}\n\n"
    "Rules:\n"
    "- At most three objectives, and fewer is a better answer than three padded. This is "
    "not a backlog and it is not a brainstorm: everything you return will be scored, ranked "
    "against everything already waiting, and possibly started by a worker fleet tonight.\n"
    "- Returning nothing is a real answer. If everything you can see is already open, "
    "already proposed, already turned down, or blocked on something only the operator can "
    "supply, return an empty list, set `exhausted` to true, and say which of those it is. "
    "An invented objective costs more than an empty answer, because it will be worked.\n"
    "- Do not restate anything on the lists you were given, in any wording. Do not re-"
    "propose something that was dismissed unless something has actually changed, and if so "
    "say in `why` what changed.\n"
    "- `lane` must be one of the lanes you were given. If an objective genuinely belongs to "
    "no existing lane, that is worth saying in `assessment` - it is the operator's call to "
    "open one, not yours.\n"
    "- `origin` must be honest. `outside` means you searched and read something; if you did "
    "not search, nothing is `outside`. `sources` may not contain a URL you did not read - "
    "an invented citation is worse than no citation, because it will be believed.\n"
    + _SCORE_RULES + "\n"
    "- `need` is the rule that matters most here. Exploring with nothing to react to is how "
    "plausible filler gets written. Anything you cannot say a rung or an open question for "
    "is low, and low is the correct score - do not round it up to justify having answered.\n"
    "- You are proposing. The operator decides what gets built and what gets adopted into "
    "the doctrine. Do not write as if the decision is made."
)


# Where a lane keeps the documents that say what it is FOR. Ordered, because the
# budget runs out and the first match should be the most load-bearing one.
_SPEC_GLOBS = ("docs/spec/README.md", "docs/spec/*.md", "docs/spec/*/*.md",
               "prompts/*.md", "docs/*.md",
               "SPEC.md", "DESIGN.md", "ARCHITECTURE.md", "ROADMAP.md", "README.md")

# Historical spec versions. A superseded design read as current is worse than no
# design at all: it proposes work that was already decided against.
_SPEC_SKIP = re.compile(r"(^|/)(old_scrap|node_modules|_build|deps|archive|\.git)(/|$)")

# Budgets. Every one of these is a bill: eleven lanes at four documents each is a
# hundred thousand characters of context on a button nobody watches. Reading one
# lane deeply is worth paying for; reading all of them deeply is paying to be told
# what the top of each README already said.
SPEC_FILE_LINES = 40      # opening lines, per file
SPEC_FILE_CHARS = 3000    # hard cap, per file
SPEC_LANE_FILES = 2       # per lane when the whole stack is being read
SPEC_FOCUS_FILES = 8      # per lane when that one lane is the subject


def _spec_outline(path: Path, lines: int = SPEC_FILE_LINES) -> str:
    """A design document reduced to what it claims, not what it says.

    The headings plus the opening prose. A spec is mostly detail that only
    matters once you are implementing against it, and the part that says what
    the thing is FOR is the title, the section names and the first paragraph.
    Sending the whole file would spend the context window proving that.
    """
    try:
        raw = path.read_text()
    except (OSError, UnicodeDecodeError):
        return ""
    body = raw.splitlines()
    opening = [ln for ln in body[:lines] if ln.strip()][:lines]
    heads = [ln.strip() for ln in body if re.match(r"^#{1,3}\s+\S", ln)]
    out = "\n".join(opening)
    # Only when there is more structure than the opening already showed. A
    # heading list that repeats the lines above it is noise wearing a label.
    rest = [h for h in heads if h not in opening]
    if len(rest) > 2:
        out += "\n\n<!-- remaining sections -->\n" + "\n".join(rest[:25])
    return out[:SPEC_FILE_CHARS]


def _lane_specs(lane_name: str, *, limit: int) -> list[tuple[str, Path]]:
    """The design documents in one lane's repo, best first, deduplicated."""
    lane = (config().get("lanes") or {}).get(lane_name) or {}
    root = (ROOT / lane.get("path", ".")).resolve()
    if not root.is_dir():
        return []
    out, seen = [], set()
    for pattern in _SPEC_GLOBS:
        for f in sorted(root.glob(pattern)):
            rel = str(f.relative_to(root))
            if f in seen or not f.is_file() or _SPEC_SKIP.search(rel):
                continue
            seen.add(f)
            out.append((rel, f))
            if len(out) >= limit:
                return out
    return out


def _specs_block(lane: str | None, names: list[str], *, limit: int | None = None) -> str:
    """What each lane's own documents say it is for.

    Explore used to be given lane NAMES and a repo path and nothing else - so
    "where should this lane go next" was answered from the lane's findings and
    its commit log, which record what has been done and say nothing about what
    it was ever meant to do. The specs are the only place the intent is written
    down, and they were the one thing the call could not see.

    Read at the time of the call rather than cached. A spec that changed this
    morning is exactly the case where exploring is worth paying for.
    """
    want = [lane] if lane else names
    per = limit if limit is not None else (SPEC_FOCUS_FILES if lane else SPEC_LANE_FILES)
    blocks = []
    for name in want:
        docs = _lane_specs(name, limit=per)
        if not docs:
            continue
        body = []
        for rel, f in docs:
            text = _spec_outline(f)
            if text.strip():
                body.append(f"### `{name}/{rel}`\n```markdown\n{text}\n```")
        if body:
            blocks.append(f"## {name}\n\n" + "\n\n".join(body))
    if not blocks:
        return ""
    return ("# What these lanes' own design documents say they are for\n\n"
            "Headings and opening prose, read off disk just now. A proposal that "
            "contradicts a lane's own spec is either wrong or is an argument that "
            "the spec has been overtaken - say which, in `why`.\n\n"
            + "\n\n".join(blocks))


# ------------------------------------------------------------------ spec review
#
# A lane's spec is the one document every `need` score in that lane is judged
# against, and until now nothing in the harness could improve one - the console
# could only report that a spec was thin. This is the loop that fixes that: the
# architect reads the document and says what is wrong with it, a worker rewrites
# it in the lane's worktree, and the architect reads the rewrite. It ends when
# both of them say it is solid, not when a counter runs out.


# The spend backstop, the same shape as `SHARPEN_HARD_CAP` and for the same
# reason. Agreement is the stopping rule; this is only here so a pair that
# cannot converge cannot bill forever.
#
# Settable in Settings, because the right number is a judgement about a
# particular document and not a property of the loop: a thin spec that is being
# rewritten wholesale genuinely needs more turns than a mature one being tidied,
# and a cap set low enough to stop the second stops the first mid-argument and
# reports it as a spend cap rather than as a document that was still improving.
SPEC_MAX_ROUNDS = 25
# The ceiling on that dial. Not a second stopping rule - it is the point past
# which "a pair that cannot converge cannot bill forever" stops being true.
SPEC_MAX_ROUNDS_LIMIT = 100


def spec_max_rounds() -> int:
    """How many review rounds one document gets before the run is capped."""
    try:
        n = int(config().get("spec_max_rounds", SPEC_MAX_ROUNDS))
    except (TypeError, ValueError):
        return SPEC_MAX_ROUNDS
    return min(SPEC_MAX_ROUNDS_LIMIT, max(1, n))

# The document as sent to the architect. Big enough for a real spec - unlike
# `SPEC_FILE_CHARS`, which is an outline budget for a call that is reading
# eleven lanes at once. This call is reading one file and is supposed to have
# all of it, because a reviewer shown two thirds of a document will report the
# missing third as a defect.
SPEC_REVIEW_CHARS = 90_000

_SPECRUN_LOCK = threading.Lock()

# Set by the server: how a spec run sends its worker out. Same arrangement as
# `ON_GOAL_DISPATCH` - the library knows what to ask for, the server owns the
# lane locks and the worker cap that decide whether it can be asked for now.
ON_SPEC_DISPATCH = None

# Set by the server for the same reason, but a plain dispatch body rather than
# a spec run: the unattended loop asks for the FIRST document, and there is no
# run to attach it to yet.
ON_DRAFT_DISPATCH = None


def lane_spec_files(lane_name: str) -> list[dict]:
    """Every markdown document in this lane's `docs/spec/`.

    Not the reader's fallback ladder and not a budget - the actual list, so the
    operator can pick the one to work on. `docs/spec/` only, because that is
    where a lane's design is supposed to live and a tab that offers to sharpen
    `README.md` would be quietly agreeing that it does not have to.
    """
    lane = (config().get("lanes") or {}).get(lane_name) or {}
    root = (ROOT / lane.get("path", ".")).resolve()
    # Both trees. A document a worker has written but you have not merged yet
    # exists only in `amp/<lane>`, and leaving it off this list would mean the
    # one button that creates a spec produces nothing visible on the tab that
    # offered it - so the first thing anyone would do is press it again.
    seen: dict[str, dict] = {}
    for base, only_in_worktree in ((root, False), (WORKTREE_DIR / lane_name, True)):
        spec_root = base / "docs" / "spec"
        if not spec_root.is_dir():
            continue
        for f in sorted(spec_root.rglob("*.md")):
            rel = str(f.relative_to(base))
            if not f.is_file() or _SPEC_SKIP.search(rel) or rel in seen:
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            seen[rel] = {
                "rel": rel, "bytes": st.st_size,
                "at": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                "lines": sum(1 for _ in f.open(errors="replace")),
                # Named, because "this document exists" and "this document
                # exists in a worktree you have not read yet" are different
                # claims and only one of them is true of your repository.
                "unmerged": only_in_worktree,
            }
    return [seen[k] for k in sorted(seen)]


def specrun_path(rid: str) -> Path:
    return SPECRUN_DIR / f"{Path(rid).name}.json"


def load_specrun(rid: str) -> dict | None:
    p = specrun_path(rid)
    return json.loads(p.read_text()) if p.is_file() else None


def save_specrun(r: dict):
    SPECRUN_DIR.mkdir(parents=True, exist_ok=True)
    save_json(specrun_path(r["id"]), r)


def specruns(lane: str | None = None, rel: str | None = None) -> list[dict]:
    """Summaries, newest first."""
    if not SPECRUN_DIR.exists():
        return []
    out = []
    for p in SPECRUN_DIR.glob("*.json"):
        try:
            r = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if (lane and r.get("lane") != lane) or (rel and r.get("rel") != rel):
            continue
        rounds = r.get("rounds") or []
        out.append({
            "id": r["id"], "lane": r.get("lane"), "rel": r.get("rel"),
            "opened_at": r.get("opened_at"), "state": r.get("state"),
            "verdict": r.get("verdict"), "why": r.get("why"),
            "rounds": len(rounds),
            "waiting_on": r.get("waiting_on"),
            "cost_tokens": r.get("cost_tokens", 0),
            "last_at": (rounds[-1] or {}).get("at") if rounds else r.get("opened_at"),
            "defects": (rounds[-1] or {}).get("defects") if rounds else None,
        })
    out.sort(key=lambda r: r.get("opened_at") or "", reverse=True)
    return out


def _spec_paths(lane_name: str, rel: str) -> tuple[Path, Path]:
    """Where this document lives in your tree, and where the worker edits it.

    Two paths on purpose. The worker never touches the checkout you are working
    in - it gets `amp/<lane>`, the same isolated worktree every other worker in
    this harness gets, and you merge it from the Diff tab when you have read it.
    """
    lane = (config().get("lanes") or {}).get(lane_name) or {}
    live = (ROOT / lane.get("path", ".")).resolve() / rel
    # Computed, not created. `claude_worktree` makes the tree if it is missing,
    # and this is called from read paths - a tab being opened must not run
    # `git worktree add` as a side effect of drawing a list.
    return live, WORKTREE_DIR / lane_name / rel


def _spec_read(lane_name: str, rel: str) -> tuple[str, str]:
    """The document under review, and where it was read from.

    The worktree copy once one exists, because that is the one the last round
    rewrote - reviewing your checkout instead would hand the architect the text
    it already asked to have changed and it would ask again.
    """
    live, wt = _spec_paths(lane_name, rel)
    for path, where in ((wt, "worktree"), (live, "repo")):
        try:
            if path.is_file():
                return path.read_text(errors="replace"), where
        except OSError:
            continue
    return "", "missing"


SPEC_REVIEW_SYSTEM = (
    "You are reviewing one design document for a working software stack. Your job is "
    "to decide whether it is solid enough to build against, and to say exactly what is "
    "missing when it is not.\n\n"
    "A solid spec answers, for the thing it describes: what it is for, what it must do, "
    "what it must never do, how anyone can tell whether an implementation is correct, and "
    "what has been decided against and why. A spec that is a feature list, a status "
    "report, or a description of what already exists is not solid, however long it is.\n\n"
    "Reply in this shape and nothing else:\n\n"
    "VERDICT: SOLID | NEEDS WORK\n"
    "WHY: one sentence.\n"
    "Then, only if NEEDS WORK, a numbered list. Each item is one concrete defect and "
    "the edit that fixes it - the section to add, the ambiguity to resolve, the claim to "
    "cut. Name the section. Do not ask for a rewrite of the whole document.\n\n"
    "Rules you are held to:\n"
    "- At most 6 items. If you can only name vague ones, the document is SOLID and you "
    "are padding.\n"
    "- Do not ask for anything the document already says. It is quoted in full below; "
    "check before you ask.\n"
    "- Do not ask for code, tests, benchmarks or a schedule. This is a spec, and work "
    "that belongs in the repo does not belong in it.\n"
    "- If a previous round asked for something and the writer explained why it does not "
    "apply, either accept that or say why the explanation is wrong. Do not silently ask "
    "again."
)


def _spec_round_history(r: dict) -> str:
    """What the earlier rounds asked for and what came back.

    Both halves, because half of the reason a review loop runs forever is that
    the reviewer cannot see that it already asked for this and was answered.
    """
    rounds = r.get("rounds") or []
    if not rounds:
        return ""
    parts = []
    for rd in rounds[-3:]:
        parts.append(f"### Round {rd.get('n')} — you said\n\n{rd.get('verdict')}: "
                     f"{rd.get('why') or ''}\n"
                     + "\n".join(f"{i + 1}. {d}" for i, d in enumerate(rd.get("defects") or [])))
        w = rd.get("worker") or {}
        if w.get("text"):
            parts.append(f"### Round {rd.get('n')} — the writer replied\n\n"
                         + w["text"].strip()[:4000]
                         + (f"\n\n(the file {'changed' if rd.get('changed') else 'did NOT change'})"))
    return ("\n\n## What has already been asked and answered\n\n"
            + "\n\n".join(parts))


def _spec_review_context(r: dict) -> str:
    text, where = _spec_read(r["lane"], r["rel"])
    body, clipped = _clip(text, SPEC_REVIEW_CHARS)
    parts = [
        mission_block().strip(),
        f"# The document\n\n`{r['lane']}/{r['rel']}` — read from the "
        + ("worker's worktree, so this is the current draft" if where == "worktree"
           else "repository"),
        f"```markdown\n{body}\n```"
        + ("\n\n(clipped — the document is longer than this)" if clipped else ""),
        f"# The lane it belongs to\n\n{_record_block(r['lane'])}",
    ]
    hist = _spec_round_history(r)
    if hist:
        parts.append(hist.strip())
    return "\n\n".join(p for p in parts if p)


_SPEC_VERDICT_RE = re.compile(r"^\s*VERDICT\s*:\s*(SOLID|NEEDS\s*WORK)", re.I | re.M)
_SPEC_WHY_RE = re.compile(r"^\s*WHY\s*:\s*(.+)$", re.I | re.M)
_SPEC_ITEM_RE = re.compile(r"^\s*(\d+)[.)]\s+(.+)$")


def _parse_spec_review(text: str) -> dict:
    """The architect's verdict, its reason, and the defects it named.

    A missing verdict line is read as NEEDS WORK rather than guessed at. The
    expensive mistake is calling a thin document solid because a model forgot a
    header; the cheap one is one more round.
    """
    m = _SPEC_VERDICT_RE.search(text or "")
    verdict = "SOLID" if (m and m.group(1).upper().replace(" ", "") == "SOLID") else "NEEDS WORK"
    mw = _SPEC_WHY_RE.search(text or "")
    why = mw.group(1).strip() if mw else ""
    defects, cur = [], None
    for line in (text or "").splitlines():
        m2 = _SPEC_ITEM_RE.match(line)
        if m2:
            cur = m2.group(2).strip()
            defects.append(cur)
        elif cur is not None and line.strip():
            defects[-1] += " " + line.strip()
        elif cur is not None:
            cur = None
    # A verdict of NEEDS WORK with nothing named is not a review. Treated as
    # solid-but-unexplained rather than sent to a worker with no instructions,
    # because a worker given an empty defect list rewrites whatever it likes.
    if verdict == "NEEDS WORK" and not defects:
        return {"verdict": "SOLID", "why": why or
                "the reviewer said it needed work but named nothing to change",
                "defects": [], "unexplained": True}
    return {"verdict": verdict, "why": why, "defects": defects[:6], "unexplained": False}


def spec_worker_prompt(r: dict, defects: list[str]) -> str:
    """What the worker is told. One document, one job, no side quests."""
    lane = (config().get("lanes") or {}).get(r["lane"]) or {}
    return (
        f"{mission_block().strip()}\n\n"
        f"# Your job\n\n"
        f"Improve the design document `{r['rel']}` in this worktree. That file, and "
        f"nothing else. Do not write code, do not run builds, do not touch any other "
        f"file, do not commit.\n\n"
        f"A reviewer read it and named these defects:\n\n"
        + "\n".join(f"{i + 1}. {d}" for i, d in enumerate(defects))
        + "\n\n# How to answer\n\n"
        f"Edit the file to fix what is genuinely wrong. If one of the numbered items is "
        f"already satisfied by the document, or is asking for something that does not "
        f"belong in a spec, do NOT invent a section to satisfy it - say so instead, and "
        f"quote the part of the document that already covers it.\n\n"
        f"End your reply with exactly one of these lines:\n\n"
        f"SPEC: REVISED — followed by one sentence per item saying what you changed.\n"
        f"SPEC: SOLID — followed by why the remaining items do not apply.\n\n"
        f"Say SOLID only if you changed nothing. Do not pad the document to look busy: "
        f"a spec that grew and did not get clearer is a worse spec.\n\n"
        f"The lane is `{r['lane']}` at `{lane.get('path', '.')}` in the main tree; you are "
        f"in an isolated worktree of it."
    )


_SPEC_WORKER_RE = re.compile(r"^\s*SPEC\s*:\s*(REVISED|SOLID)\b", re.I | re.M)


def spec_review_open(lane_name: str, rel: str) -> dict:
    """Start a spec run. One per document at a time, by construction."""
    if lane_name not in (config().get("lanes") or {}):
        die(f"unknown lane {lane_name!r}")
    # Either tree. A document that so far exists only in the worktree is the
    # normal state of one a worker has just drafted, and it is exactly the one
    # most worth sharpening.
    if _spec_read(lane_name, rel)[1] == "missing":
        die(f"no such document: {lane_name}/{rel}")
    for row in specruns(lane_name, rel):
        if row["state"] == "running":
            die(f"a run is already open on {rel} ({row['id']})")
    r = {
        "id": "s" + uuid.uuid4().hex[:10],
        "lane": lane_name,
        "rel": rel,
        "opened_at": now(),
        "state": "running",
        "waiting_on": "architect",
        "model": config().get("consult_model", DEFAULT_CONSULT),
        "cost_tokens": 0,
        "rounds": [],
    }
    save_specrun(r)
    return spec_review_step(r["id"])


def _spec_stop(rid: str, state: str, why: str) -> dict:
    with _SPECRUN_LOCK:
        r = load_specrun(rid)
        r.update({"state": state, "why": why, "waiting_on": None,
                  "closed_at": now()})
        save_specrun(r)
    return r


def spec_review_step(rid: str) -> dict:
    """One architect turn, and whatever it implies.

    Either both sides now agree and the run is done, or there are named defects
    and a worker is sent to fix them. The worker's reply comes back through
    `spec_worker_done`, which calls this again.
    """
    r = load_specrun(rid)
    if not r:
        die(f"no spec run {rid!r}")
    if r.get("state") != "running":
        return r
    cap = spec_max_rounds()
    if len(r.get("rounds") or []) >= cap:
        return _spec_stop(rid, "capped",
                          f"stopped at the {cap}-round spend cap without "
                          f"the two of them agreeing")
    if not architect_available():
        return _spec_stop(rid, "stopped", architect_off_reason())

    resp = architect_chat([{"role": "system", "content": SPEC_REVIEW_SYSTEM},
                           {"role": "user", "content": _spec_review_context(r)}],
                          r["model"])
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    got = _parse_spec_review(text)

    with _SPECRUN_LOCK:
        r = load_specrun(rid)
        n = len(r["rounds"]) + 1
        r["rounds"].append({"n": n, "at": now(), "verdict": got["verdict"],
                            "why": got["why"], "defects": got["defects"],
                            "review": text, "worker": None, "changed": None})
        r["cost_tokens"] = r.get("cost_tokens", 0) + ((resp.get("usage") or {}).get("total_tokens") or 0)
        save_specrun(r)

    if got["verdict"] == "SOLID":
        # Both of them. The writer's half of the agreement is not a separate
        # question that needs a separate call: it either revised the document
        # until the reviewer was satisfied, or it said the remaining items did
        # not apply and the reviewer has now agreed. Sending a worker out to be
        # asked "do you also think it is solid?" would be paying for a yes.
        return _spec_stop(rid, "solid", got["why"] or "the reviewer and the writer both "
                                                      "say this document is solid")
    # The genuine deadlock: the writer says the items do not apply, the reviewer
    # keeps asking, and nothing on disk has moved for two rounds. That is not
    # something a third round settles - it is a disagreement, and it is yours.
    done = load_specrun(rid)["rounds"]
    if all((rd.get("changed") is False) for rd in done[-3:-1]) and len(done) > 2:
        return _spec_stop(rid, "stalled",
                          "the writer says these do not apply and the reviewer keeps "
                          "asking - two rounds left the document byte-identical")
    if not ON_SPEC_DISPATCH:
        return _spec_stop(rid, "stopped", "no worker dispatch is wired up")

    with _SPECRUN_LOCK:
        r = load_specrun(rid)
        r["waiting_on"] = "worker"
        # Before the writer exists, not after. The whole stop rule turns on
        # whether this round moved the document, and a baseline read once the
        # worker is already running is a baseline that may already include the
        # worker's edit - which reads as "nothing changed" and stalls a run that
        # is working, or as "something changed" and runs a stalled one to the
        # cap. A fast worker makes the second one the common case.
        r["rounds"][-1]["before"] = _spec_read(r["lane"], r["rel"])[0]
        i = len(r["rounds"]) - 1
        save_specrun(r)
    out = ON_SPEC_DISPATCH(r, got["defects"])
    if not out.get("ok"):
        return _spec_stop(rid, "stopped",
                          f"could not send a worker: {str(out.get('error'))[:200]}")
    # By index, not `[-1]`. The task id only exists once dispatch returns, and by
    # then the writer may already have finished, settled, and started the next
    # round - so `[-1]` is a different round than the one that sent this worker.
    # That mis-files the id onto a round that dispatched nothing, which is how a
    # stopped run ends up saying a writer is still out on it.
    with _SPECRUN_LOCK:
        r = load_specrun(rid)
        if i < len(r["rounds"]):
            r["rounds"][i]["task_id"] = out.get("task_id")
            save_specrun(r)
    return load_specrun(rid)


def spec_worker_done(rid: str, lane_name: str, rec: dict) -> dict:
    """The writer has finished. Record what it said, and whether the file moved."""
    r = load_specrun(rid)
    if not r or r.get("state") != "running":
        return r or {}
    reply = (rec.get("result") or rec.get("error") or "").strip()
    m = _SPEC_WORKER_RE.search(reply)
    # `agreed is False` means the writer pushed back on the instructions. An
    # unparseable reply is neither agreement nor a dispute, so it is None and
    # the run carries on rather than being stopped by a formatting slip.
    agreed = None if not m else (m.group(1).upper() == "SOLID")
    after = _spec_read(r["lane"], r["rel"])[0]

    with _SPECRUN_LOCK:
        r = load_specrun(rid)
        # The round this writer was sent on: the last one still waiting for a
        # reply. Not `[-1]`, which is only the same round while nothing else has
        # moved, and not a match on `task_id`, which is written after dispatch
        # and so may not be there yet when a fast writer settles.
        rd = next((x for x in reversed(r["rounds"]) if not x.get("worker")), None)
        if rd is None:
            return r
        rd["worker"] = {"task_id": rec.get("task_id"), "session_id": rec.get("session_id"),
                        "at": now(), "agreed": agreed, "text": reply[:8000],
                        "status": rec.get("status")}
        rd["changed"] = after != (rd.get("before") or "")
        # The document itself is not kept on the round - it is on disk in the
        # worktree, and a copy here would be a second answer to "what does the
        # spec say" that can disagree with the first.
        rd.pop("before", None)
        r["waiting_on"] = "architect"
        save_specrun(r)

    # A worker that hit its budget or was killed may still have improved the
    # document before it stopped, and throwing that away to honour a status
    # field would be discarding work that is already on disk and already paid
    # for. What settles it is whether the file moved - the reviewer judges the
    # text either way, and it cannot judge a text that does not exist.
    if rec.get("status") != "completed" and not rd["changed"]:
        return _spec_stop(rid, "stopped",
                          f"the writer stopped without changing anything "
                          f"({rec.get('status') or 'no status'})")
    return spec_review_step(rid)


def close_specrun(rid: str, why: str = "closed by the operator") -> dict:
    r = load_specrun(rid)
    if not r:
        die(f"no spec run {rid!r}")
    return _spec_stop(rid, "closed", why) if r.get("state") == "running" else r


# ----------------------------------------------------------------- spec ratings
#
# The sharpen loop will improve any document you point it at. What it cannot do
# is tell you which one to point it at, and a stack of eleven lanes holds more
# design documents than anyone reads before deciding. These are the numbers that
# decide.
#
# They are deliberately the same three shapes the direction board already uses on
# a proposal, because the question is the same one: of the things I could pay for
# next, which one moves the mission most per dollar. Reusing the shape also means
# there is one thing to learn rather than two, and one place where a number that
# turns out to be useless can be deleted.
#
# What is NOT here: a "quality" score, which would be `solidity` under another
# name, and a "staleness" score, which is not a judgement at all - whether a
# rating was made against the current bytes is a FACT, checked against the sha
# below, and a model asked to guess at it would be guessing at something already
# known.

# The document as sent to the rater. Much smaller than `SPEC_REVIEW_CHARS`, on
# purpose. A review must see all of a document because it names defects, and an
# item asking for something already written is the failure that wastes a whole
# round. A rating is a judgement about shape and reaches the same answer from a
# large sample - and paying full price to rate every document, in order to decide
# which one to pay full price on, would defeat the point of rating them.
SPEC_RATE_CHARS = 30_000

# Below this `worth`, the harness will not spend a run on a document by itself.
# Not a gate on you: the button on a single document ignores it. Only on what
# a campaign starts unattended.
DEFAULT_SPEC_WORTH_BAR = 0.15

_SPECRATE_LOCK = threading.Lock()


def spec_worth_bar() -> float:
    return _bar("spec_worth", DEFAULT_SPEC_WORTH_BAR)


def _spec_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:16]


def spec_rates(lane_name: str | None = None) -> dict:
    """Every rating, or one lane's. Keyed by `rel`."""
    everything = load_json(SPECRATE_PATH, {})
    if lane_name is None:
        return everything
    return everything.get(lane_name) or {}


def save_spec_rate(lane_name: str, rel: str, rating: dict) -> None:
    with _SPECRATE_LOCK:
        everything = load_json(SPECRATE_PATH, {})
        everything.setdefault(lane_name, {})[rel] = rating
        save_json(SPECRATE_PATH, everything)


def spec_worth(rating: dict | None):
    """What one sharpen run on this document is expected to be worth.

    `need x headroom`: how much the mission wants this document to be right,
    times how much a round would actually move it. `None` unless both are
    known, for the same reason `worth()` is - a document ranked on whichever
    field happened to be present would sort top or bottom for a reason nobody
    stated.

    Solidity is deliberately not a factor. A solid document has no headroom, so
    including both would count the same fact twice and push already-finished
    documents down a ranking they are already at the bottom of.
    """
    if not rating:
        return None
    need, head = rating.get("need"), rating.get("headroom")
    if need is None or head is None:
        return None
    return round(need * head, 3)


SPEC_RATE_SYSTEM = (
    "You are rating one design document in a working software stack so that a harness "
    "can decide which document to spend a review-and-rewrite cycle on next. You are not "
    "reviewing it, and you are not rewriting it.\n\n"
    "A solid spec answers, for the thing it describes: what it is for, what it must do, "
    "what it must never do, how anyone can tell a correct implementation from a wrong "
    "one, and what has been decided against and why. A feature list, a status report or "
    "a description of what already exists is not a spec, however long it is.\n\n"
    "Reply with one JSON object and nothing else:\n\n"
    "{\n"
    '  "solidity": 0.0,\n'
    '  "why_solidity": "one sentence",\n'
    '  "need": 0.0,\n'
    '  "why_need": "one sentence",\n'
    '  "headroom": 0.0,\n'
    '  "why_headroom": "one sentence",\n'
    '  "gaps": ["one concrete missing thing", "another"]\n'
    "}\n\n"
    "What each number means:\n"
    "- solidity: how much of what an implementer needs is actually written down. 1.0 "
    "means somebody could build against this and know when they were done. 0.0 means it "
    "says almost nothing an implementer could use.\n"
    "- need: how much the mission above needs THIS document to be right. A document "
    "describing something the mission does not depend on scores low however thin it is.\n"
    "- headroom: how much one round of review-and-rewrite would actually move it. A "
    "document that is already solid has none. So does one whose gaps are missing FACTS "
    "that no writer could invent - decisions nobody has taken, numbers nobody has "
    "measured. Say which case it is in why_headroom.\n"
    "- gaps: at most 6, each one concrete and named. Empty when there are none.\n\n"
    "Rules you are held to:\n"
    "- Judge the document in front of you, not the one you would have written.\n"
    "- Length is not solidity. A short document that answers the five questions scores "
    "higher than a long one that does not.\n"
    "- Do not ask for code, tests, benchmarks or a schedule. Work that belongs in the "
    "repo does not belong in a spec.\n"
    "- Give a number for all three. A missing one is read as unrated, which keeps this "
    "document out of the ranking entirely rather than guessing on your behalf."
)


def _spec_rate_context(lane_name: str, rel: str, text: str, clipped: bool) -> str:
    return "\n\n".join(p for p in [
        mission_block().strip(),
        f"# The document\n\n`{lane_name}/{rel}`",
        f"```markdown\n{text}\n```"
        + ("\n\n(clipped - the document is longer than this. Rate what a document of "
           "this shape is worth; do not report the clip as a defect.)" if clipped else ""),
        f"# The lane it belongs to\n\n{_record_block(lane_name)}",
    ] if p)


def spec_rate(lane_name: str, rel: str) -> dict:
    """One architect call: how solid this document is, and whether sharpening it pays.

    Stores the sha of the exact text it read. A rating carried forward onto bytes
    it never saw is the one failure this cannot recover from by itself - it would
    rank a document on a judgement of a version that no longer exists, and it
    would look identical on screen to a rating that was made this minute.
    """
    if lane_name not in (config().get("lanes") or {}):
        die(f"unknown lane {lane_name!r}")
    text, where = _spec_read(lane_name, rel)
    if where == "missing":
        die(f"no such document: {lane_name}/{rel}")
    if not architect_available():
        die(architect_off_reason())
    body, clipped = _clip(text, SPEC_RATE_CHARS)
    model = config().get("consult_model", DEFAULT_CONSULT)
    resp = architect_chat([{"role": "system", "content": SPEC_RATE_SYSTEM},
                           {"role": "user",
                            "content": _spec_rate_context(lane_name, rel, body, clipped)}],
                          model, max_tokens=2000)
    reply = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    got = _json_reply(reply) or {}
    rating = {
        "rel": rel,
        "at": now(),
        # What was rated, not what is there now. `spec_view` compares the two.
        "sha": _spec_sha(text),
        "read_from": where,
        "clipped": clipped,
        "solidity": _num(got.get("solidity"), 0.0, 1.0),
        "why_solidity": str(got.get("why_solidity") or "")[:400],
        "need": _num(got.get("need"), 0.0, 1.0),
        "why_need": str(got.get("why_need") or "")[:400],
        "headroom": _num(got.get("headroom"), 0.0, 1.0),
        "why_headroom": str(got.get("why_headroom") or "")[:400],
        "gaps": [str(g)[:300] for g in (got.get("gaps") or []) if str(g).strip()][:6],
        "cost_tokens": (resp.get("usage") or {}).get("total_tokens") or 0,
        "model": model,
    }
    # Said out loud rather than left as three nulls. An unparseable reply and a
    # rater that genuinely could not judge the document look the same in the
    # data, and only one of them is worth retrying.
    if rating["solidity"] is None and rating["need"] is None and rating["headroom"] is None:
        rating["error"] = "the rater's reply had no numbers in it: " + reply.strip()[:200]
    save_spec_rate(lane_name, rel, rating)
    return rating


def spec_audit(lane_name: str) -> dict:
    """Rate every document in the lane that does not already have a current rating.

    Skips what is already rated against the bytes on disk. Re-rating an unchanged
    document buys a second opinion on text nothing has touched, and the whole
    point of the sha is so that this can tell the difference.
    """
    files = lane_spec_files(lane_name)
    have = spec_rates(lane_name)
    rated, skipped, failed = [], [], []
    for f in files:
        text, _ = _spec_read(lane_name, f["rel"])
        prior = have.get(f["rel"])
        if prior and not prior.get("error") and prior.get("sha") == _spec_sha(text):
            skipped.append(f["rel"])
            continue
        try:
            rated.append(spec_rate(lane_name, f["rel"]))
        except SystemExit as e:
            failed.append({"rel": f["rel"], "error": str(e)})
            # An architect that has gone away will not come back mid-loop, and
            # eleven identical failures is eleven identical messages.
            break
    return {"lane": lane_name, "rated": rated, "skipped": skipped, "failed": failed}


# --------------------------------------------------------------- spec campaigns
#
# "Sharpen this document" is a button. "Sharpen all of them" cannot be, because a
# lane runs one worker at a time by construction - `do_dispatch` refuses a second
# one - so firing eleven runs at once would start one and fail ten. A campaign is
# the queue that makes the difference: it holds the list, and the ticker starts
# the next document when the lane is free.
#
# It is a plan, not a schedule. It carries no timing and makes no promises about
# when a document is reached; what it guarantees is only that the lane does not
# go idle with work still on the list.

_SPECPLAN_LOCK = threading.Lock()


def spec_plans() -> dict:
    return load_json(SPECPLAN_PATH, {})


def spec_plan(lane_name: str) -> dict | None:
    return spec_plans().get(lane_name)


def _save_spec_plan(plan: dict) -> dict:
    with _SPECPLAN_LOCK:
        plans = load_json(SPECPLAN_PATH, {})
        plans[plan["lane"]] = plan
        save_json(SPECPLAN_PATH, plans)
    return plan


def spec_plan_order(lane_name: str) -> list[dict]:
    """The lane's documents, best-first, with the reason each one is where it is.

    Rated documents sort by `worth` descending. Unrated ones sort AFTER every
    rated one rather than before: an unrated document is not known to be worth
    nothing, but starting a paid run on a document nobody has judged - ahead of
    one that has been judged and scored well - would be spending the ranking's
    budget on the thing the ranking has the least to say about.
    """
    rates = spec_rates(lane_name)
    rows = []
    for f in lane_spec_files(lane_name):
        r = rates.get(f["rel"])
        rows.append({"rel": f["rel"], "worth": spec_worth(r),
                     "solidity": (r or {}).get("solidity"),
                     "rated": bool(r) and not (r or {}).get("error")})
    rows.sort(key=lambda x: (x["worth"] is None, -(x["worth"] or 0.0), x["rel"]))
    return rows


def spec_campaign_open(lane_name: str, *, rels: list[str] | None = None) -> dict:
    """Queue every document in the lane, best first."""
    if lane_name not in (config().get("lanes") or {}):
        die(f"unknown lane {lane_name!r}")
    order = spec_plan_order(lane_name)
    if rels is not None:
        want = set(rels)
        order = [o for o in order if o["rel"] in want]
    if not order:
        die(f"{lane_name} has no documents under docs/spec/ to sharpen")
    bar = spec_worth_bar()
    queue, skipped = [], []
    for o in order:
        # An unrated document is queued. A rated one below the bar is not, and
        # the reason is recorded - "it was skipped" and "it was never in the
        # list" are different facts and the tab should not have to guess.
        if o["worth"] is not None and o["worth"] < bar:
            skipped.append({"rel": o["rel"], "worth": o["worth"],
                            "why": f"worth {o['worth']} is under the {bar} bar"})
            continue
        queue.append(o["rel"])
    plan = {
        "lane": lane_name,
        "opened_at": now(),
        "state": "running" if queue else "done",
        "bar": bar,
        "queue": queue,
        "skipped": skipped,
        "done": [],
        "current": None,
        "why": None if queue else "every document is either already solid or under the bar",
    }
    return _save_spec_plan(plan)


def spec_campaign_close(lane_name: str, why: str = "stopped by the operator") -> dict | None:
    with _SPECPLAN_LOCK:
        plans = load_json(SPECPLAN_PATH, {})
        plan = plans.get(lane_name)
        if not plan or plan.get("state") != "running":
            return plan
        plan.update({"state": "stopped", "why": why, "closed_at": now(), "current": None})
        save_json(SPECPLAN_PATH, plans)
    return plan


def spec_campaign_step(lane_name: str) -> dict | None:
    """Move one campaign forward by at most one document.

    Called on a tick rather than from the settling worker. A run ends inside
    `spec_worker_done`, which is already several frames deep in a worker thread
    holding the run lock, and starting the next document from there would open a
    second run from inside the first one's stack. On a tick it is a plain start
    with nothing else in flight.
    """
    plan = spec_plan(lane_name)
    if not plan or plan.get("state") != "running":
        return None

    # Whatever was started last, judged from the run itself rather than from
    # anything remembered here - a campaign that trusted its own note of what it
    # started would keep waiting on a run somebody closed by hand.
    cur = plan.get("current")
    if cur:
        rows = specruns(lane_name, cur.get("rel"))
        row = next((r for r in rows if r["id"] == cur.get("run_id")), None)
        if row and row.get("state") == "running":
            return plan
        with _SPECPLAN_LOCK:
            plans = load_json(SPECPLAN_PATH, {})
            p = plans.get(lane_name) or plan
            p["done"] = (p.get("done") or []) + [{
                "rel": cur.get("rel"), "run_id": cur.get("run_id"),
                "state": (row or {}).get("state") or "gone",
                "why": (row or {}).get("why"), "at": now()}]
            p["current"] = None
            plans[lane_name] = p
            save_json(SPECPLAN_PATH, plans)
        plan = spec_plan(lane_name)

    if not plan.get("queue"):
        with _SPECPLAN_LOCK:
            plans = load_json(SPECPLAN_PATH, {})
            p = plans.get(lane_name) or plan
            p.update({"state": "done", "closed_at": now(), "current": None,
                      "why": f"{len(p.get('done') or [])} document(s) sharpened"})
            plans[lane_name] = p
            save_json(SPECPLAN_PATH, plans)
        return spec_plan(lane_name)

    if not architect_available():
        return spec_campaign_close(lane_name, architect_off_reason())
    # Somebody else's worker holds the lane. Not an error and not the campaign's
    # business - it waits for the next tick.
    if any(r.get("state") == "running" for r in specruns(lane_name)):
        return plan

    rel = plan["queue"][0]
    try:
        run = spec_review_open(lane_name, rel)
    except SystemExit as e:
        with _SPECPLAN_LOCK:
            plans = load_json(SPECPLAN_PATH, {})
            p = plans.get(lane_name) or plan
            p["queue"] = [q for q in (p.get("queue") or []) if q != rel]
            p["done"] = (p.get("done") or []) + [
                {"rel": rel, "run_id": None, "state": "stopped",
                 "why": str(e)[:200], "at": now()}]
            plans[lane_name] = p
            save_json(SPECPLAN_PATH, plans)
        return spec_plan(lane_name)

    with _SPECPLAN_LOCK:
        plans = load_json(SPECPLAN_PATH, {})
        p = plans.get(lane_name) or plan
        p["queue"] = [q for q in (p.get("queue") or []) if q != rel]
        # `spec_review_open` runs the first architect turn inline, so a document
        # the reviewer calls solid on sight is already finished by the time we
        # get here. Filed as done rather than left as `current`, which would
        # make the campaign wait a whole tick to notice.
        if run.get("state") == "running":
            p["current"] = {"rel": rel, "run_id": run["id"], "at": now()}
        else:
            p["done"] = (p.get("done") or []) + [
                {"rel": rel, "run_id": run["id"], "state": run.get("state"),
                 "why": run.get("why"), "at": now()}]
        plans[lane_name] = p
        save_json(SPECPLAN_PATH, plans)
    return spec_plan(lane_name)


def spec_campaigns_step() -> list[str]:
    """Every running campaign, moved on by one. The ticker's whole job here."""
    moved = []
    for lane_name in list(spec_plans()):
        try:
            before = spec_plan(lane_name) or {}
            after = spec_campaign_step(lane_name) or {}
            if (before.get("current") or {}).get("run_id") != \
               (after.get("current") or {}).get("run_id") or \
               before.get("state") != after.get("state"):
                moved.append(lane_name)
        except Exception as e:
            # Stopped and named, rather than re-raised. One lane that cannot be
            # stepped must not stop the others, and a campaign that throws every
            # tick forever is a loop nobody sees paying for nothing.
            spec_campaign_close(lane_name, f"{type(e).__name__}: {e}"[:200])
            moved.append(lane_name)
    return moved


# ------------------------------------------------------------- spec recovery
#
# A lane with nothing under `docs/spec/` has one of two problems, and they need
# opposite answers. Either the design was never written down - in which case
# somebody has to write it, and the only honest source is the code. Or it was
# written down and is sitting somewhere else, which happens constantly in a
# stack this size: a document about one product filed under another product's
# repo because that is where the person was working that day.
#
# Guessing between them is what a search is for. The second is cheap to check
# and costs nothing to be wrong about, so it is checked first and by reading the
# disk, not by asking a model where a file might be.

# How much of a document has to be about a lane before it is worth showing. One
# passing mention of a name is a citation, not a misfiled spec.
SPEC_FIND_MIN_HITS = 3


def _spec_lane_terms(lane_name: str, spec: dict) -> set[str]:
    """What a document would call this lane: its key, its path, and the last
    segment of that path - which is usually the product's actual name."""
    path = spec.get("path", ".")
    return {t for t in (lane_name.lower(), Path(path).name.lower(), path.lower())
            if len(t) > 2}


def spec_candidates(lane_name: str) -> list[dict]:
    """Design documents elsewhere in the stack that are mostly about THIS lane.

    Reads every `<dir>/docs/spec/**.md` in the repository that is not already
    inside this lane's tree and counts how often it names the lane - its key,
    its path, and the last segment of its path, which is what a document
    usually calls a product.

    Every top-level directory is searched, not only the ones that happen to be
    configured as lanes. Most of this repository is not a lane, and a document
    that has drifted is most likely to have drifted somewhere nobody is
    watching - searching only the lanes would look everywhere except where the
    answer is.

    The count is the evidence and it is reported, not hidden behind a verdict.
    A file that mentions the lane forty times and lives in another repo is
    almost certainly misfiled; one that mentions it three times may just be
    describing an integration, and that is a judgement for you.
    """
    cfg = config().get("lanes") or {}
    lane = cfg.get(lane_name) or {}
    if not lane:
        return []
    mine = (ROOT / lane.get("path", ".")).resolve()
    terms = _spec_lane_terms(lane_name, lane)
    if not terms:
        return []
    # Which lane, if any, owns each directory - so a found document can be
    # named by the lane you would go to in this console to act on it.
    owner = {(ROOT / s.get("path", ".")).resolve(): n for n, s in cfg.items()}

    out = []
    for root in sorted(p for p in ROOT.iterdir() if p.is_dir()):
        # `.amp` holds our own worktrees, which are copies of these same
        # documents; offering one back as a discovery would be a loop.
        if root.name.startswith("."):
            continue
        root = root.resolve()
        spec_root = root / "docs" / "spec"
        if root == mine or not spec_root.is_dir():
            continue
        other = owner.get(root)
        # The same names the subject lane is searched by, for the lane the
        # document actually sits under. Asking only about the lane's key would
        # call a document that says `AmpersandBoxDesign` on every page but never
        # `abd` misfiled, which is the opposite of true. A directory that is no
        # lane is still named by its own directory name.
        host_terms = (_spec_lane_terms(other, cfg[other]) if other
                      else {root.name.lower()})
        for f in sorted(spec_root.rglob("*.md")):
            rel = str(f.relative_to(root))
            if not f.is_file() or _SPEC_SKIP.search(rel):
                continue
            try:
                if f.resolve().is_relative_to(mine):
                    continue
                text = f.read_text(errors="replace")
            except OSError:
                continue
            low = text.lower()
            hits = sum(low.count(t) for t in terms)
            if hits < SPEC_FIND_MIN_HITS:
                continue
            head = next((ln.strip("# ").strip() for ln in text.splitlines()
                         if ln.startswith("#")), "")
            out.append({
                # `None` when the document sits somewhere this console has no
                # lane for. That is not a gap in the search, it is the finding:
                # nothing here is watching that directory.
                "lane": other, "rel": rel, "path": f"{root.name}/{rel}",
                "title": head[:120], "hits": hits,
                "lines": text.count("\n") + 1, "bytes": len(text.encode()),
                # Whether the lane it currently sits under is also named in it.
                # A document about both is an integration note; one that never
                # names its own host is the misfiled case.
                "names_host": any(t in low for t in host_terms),
            })
    # The count leads. Naming the host was tried as the first key and the real
    # repository refuted it: a document titled `OS-010-PULSE-SPECIFICATION` that
    # says `pulse` seventy-five times also says the name of the directory it
    # sits in, so it sorted below a file that mentions the lane four times in
    # passing. Whether a document is a lane's design is carried by how much of
    # it is about that lane; naming the host only breaks ties.
    out.sort(key=lambda r: (-r["hits"], r["names_host"], r["path"]))
    return out


def spec_draft_prompt(lane_name: str, candidates: list[dict] | None = None) -> str:
    """What the worker is told when a lane has no spec at all.

    It is asked to write down what the code already commits to, and explicitly
    NOT to design. A worker asked to write a spec for a codebase writes the spec
    it would like the codebase to have, and the result reads like a plan while
    describing nothing that exists - which is worse than no document, because the
    next reader believes it.
    """
    lane = (config().get("lanes") or {}).get(lane_name) or {}
    found = ""
    if candidates:
        found = ("\n\n# Documents elsewhere in the stack that are mostly about this lane\n\n"
                 + "\n".join(f"- `{c['path']}` ({c['hits']} mentions) — {c['title']}"
                             for c in candidates[:8])
                 + "\n\nThey are NOT in this worktree and you cannot edit them. Read them if "
                   "you can reach them and say in your reply whether the design is already "
                   "written down somewhere else - that is worth more than a second copy.")
    return (
        f"{mission_block().strip()}\n\n"
        f"# Your job\n\n"
        f"This lane has no design document. Write `docs/spec/SPEC.md` in this worktree. "
        f"That file, and nothing else. Do not write code, do not run builds, do not "
        f"change any other file, do not commit.\n\n"
        f"# What to write\n\n"
        f"Read the code and write down what it already commits to. A spec answers, for "
        f"the thing it describes: what it is for, what it must do, what it must never do, "
        f"how anyone can tell a correct implementation from a wrong one, and what has "
        f"been decided against and why.\n\n"
        f"You are describing, not designing. Do not invent requirements the code does "
        f"not have, do not write a roadmap, and do not write a status report. Where the "
        f"code makes a decision whose reason you cannot find, say that the reason is not "
        f"recorded rather than inventing one - an unanswered question written down is "
        f"worth more here than a confident guess, because the next round can ask it.\n\n"
        f"Keep it short enough that somebody reads it.{found}\n\n"
        f"# How to answer\n\n"
        f"End your reply with exactly one of these lines:\n\n"
        f"SPEC: DRAFTED — followed by one sentence per section saying where you got it.\n"
        f"SPEC: BLOCKED — followed by why you could not, in one sentence.\n\n"
        f"The lane is `{lane_name}` at `{lane.get('path', '.')}` in the main tree; you are "
        f"in an isolated worktree of it."
    )


def spec_draft_status(lane_name: str) -> dict | None:
    """The draft worker that is out for this lane right now, if there is one.

    Dispatch returns the moment the worker is spawned, so a button that reports
    only its own request goes back to idle about a second in and stays there for
    the several minutes the work actually takes. That reads as a button that did
    nothing, and the honest next move is to press it again - which is a second
    paid worker, or a queue entry behind the first.

    So this is derived from the recorded task rather than from whatever the page
    remembers asking for. It survives a reload, it is true in a second tab, and
    it is still true if the console restarted while the worker ran.
    """
    for rec in board().get("tasks", {}).get(lane_name, []):
        if rec.get("spec_draft") and rec.get("status") == "running":
            return {
                "state": "running",
                "task_id": rec.get("task_id"),
                "at": rec.get("dispatched_at") or rec.get("started_at"),
            }
    # Queued counts as in flight. A lane runs one worker at a time, so a draft
    # asked for while something else is running is real, pending, paid-for work
    # that has not started - and showing nothing there is the same defect one
    # step earlier.
    for i, body in enumerate(load_json(QUEUE_PATH, {}).get("queued") or []):
        if body.get("spec_draft") and body.get("lane") == lane_name:
            return {"state": "queued", "position": i + 1, "at": body.get("queued_at")}
    return None


# ----- the spec loop, run unattended
#
# Draft what is missing, rate what exists, sharpen what is under the bar, stop
# when it is over the bar. Four steps, one per tick, cheapest first.
#
# Everything here is capped by a RECORDED attempt count rather than by a
# stopping rule the loop judges for itself. A document the pair cannot converge
# on is not rare, and the failure mode is not that it stays thin - it is that
# the harness pays a worker and an architect to rewrite it every hour, forever,
# and the only evidence is the bill.

# The threshold the user asked for: sharpen until the architect calls it solid.
# Deliberately below 1.0 - a reviewer that has just rewritten a document is not
# going to score its own work perfect, and a bar it cannot reach is a loop that
# only ever stops on the attempt cap.
DEFAULT_SPEC_SOLID_BAR = 0.75

# How many times the loop may send a worker to write a lane's FIRST document.
# More than one because a draft can fail on a bad tree or a dead credential;
# not many more because a lane where drafting keeps producing nothing is a lane
# with something wrong that another worker will not fix.
SPEC_AUTO_DRAFTS = 2

# How many campaigns one lane may be given. Each campaign is already capped at
# SPEC_MAX_ROUNDS per document, so this bounds the outer loop: rate, sharpen,
# re-rate, sharpen again. Three passes that do not clear the bar is a document
# that needs a person, and saying so is more useful than a fourth pass.
SPEC_AUTO_CAMPAIGNS = 3

_SPECAUTO_LOCK = threading.Lock()


def spec_solid_bar() -> float:
    return _bar("spec_solid", DEFAULT_SPEC_SOLID_BAR)


def spec_auto_on(lane_name: str) -> bool:
    """Whether the harness may run the spec loop on THIS lane, unattended.

    Per lane, not per stack. It was stack-wide first, and drawn inside a lane's
    own tab, so switching one lane on appeared to switch on every lane - which
    it did. A control that sits under a lane's name and silently governs eleven
    of them is not a mislabelled control, it is the wrong control: lanes are not
    equally ready for this, and the one thing an operator needs is to exclude
    the ones that are not.

    Its own switch rather than riding on `auto_adopt`. Auto-adopt buys one
    architect call on a proposal that already exists; this drafts documents and
    sends workers into worktrees, which is a different order of spending and a
    different blast radius. Somebody who wants the first does not automatically
    want the second, and a single switch would make that choice for them.

    The lane's MODE outranks this switch rather than sitting beside it. Drafting
    and sharpening documents is development, so a lane told to stop developing
    has already answered this question - and two per-lane switches that can
    disagree would leave the operator reading one of them and getting the other.
    """
    return (bool(spec_auto(lane_name).get("on"))
            and lane_admits(lane_name, "development") is None)


def set_auto_spec(lane_name: str, on: bool) -> bool:
    _spec_auto_note(lane_name, "switch",
                    "on, by the operator" if on else "off, by the operator", on=bool(on))
    return bool(on)


def spec_auto(lane_name: str | None = None) -> dict:
    everything = load_json(SPECAUTO_PATH, {})
    if lane_name is None:
        return everything
    return everything.get(lane_name) or {"drafts": 0, "campaigns": 0, "log": []}


def _spec_auto_note(lane_name: str, what: str, why: str, **fields) -> dict:
    """Record that the loop did something, and what it was.

    The log is the only account of unattended spending on this lane. A step that
    happened and left nothing behind is indistinguishable from a step that was
    skipped, which is exactly the confusion that makes an automated loop
    impossible to trust or to debug.
    """
    with _SPECAUTO_LOCK:
        everything = load_json(SPECAUTO_PATH, {})
        rec = everything.get(lane_name) or {"drafts": 0, "campaigns": 0, "log": []}
        for k, v in fields.items():
            rec[k] = v
        entry = {"at": now(), "what": what, "why": why}
        rec["log"] = ([entry] + (rec.get("log") or []))[:40]
        rec["last_at"] = entry["at"]
        everything[lane_name] = rec
        save_json(SPECAUTO_PATH, everything)
        return rec


def _spec_auto_busy(lane_name: str, what: str | None) -> None:
    """Mark that a step is under way, or that it has finished.

    An architect call takes tens of seconds and runs on the heartbeat thread, so
    from the tab's point of view nothing at all is happening until it lands and
    the verdict jumps. The whole loop looked like it had gone from thinking
    about it to done with nothing in between, which is what makes an unattended
    loop impossible to watch.

    Written to the same file as everything else so it is a recorded fact rather
    than a guess the page maintains: it is still true in a second tab, and a
    step interrupted by a console restart leaves the marker behind rather than
    quietly clearing it.
    """
    with _SPECAUTO_LOCK:
        everything = load_json(SPECAUTO_PATH, {})
        rec = everything.get(lane_name) or {"drafts": 0, "campaigns": 0, "log": []}
        rec["busy"] = {"what": what, "at": now()} if what else None
        everything[lane_name] = rec
        save_json(SPECAUTO_PATH, everything)


def spec_fingerprint(lane_name: str) -> str:
    """What this lane's documents are, right now, as one value.

    Used to decide whether Direction has already been asked about THESE
    documents. A timestamp would not do: the question is not when the lane was
    last explored, it is whether it was explored against the text that is there
    now, and a spec that has since been rewritten is a different question.
    """
    rows = [f"{f['rel']}:{_spec_sha(_spec_read(lane_name, f['rel'])[0])}"
            for f in lane_spec_files(lane_name)]
    return hashlib.sha256("|".join(rows).encode()).hexdigest()[:16]


def spec_lane_state(lane_name: str) -> dict:
    """Where this lane's documents stand against the threshold. Derived.

    `verdict` is what the loop acts on and what the tab reports, so both of them
    are answering the same question from the same numbers rather than each
    deciding for itself what "done" means.
    """
    bar = spec_solid_bar()
    files = lane_spec_files(lane_name)
    rates = spec_rates(lane_name)
    rated, unrated, thin = [], [], []
    for f in files:
        r = rates.get(f["rel"])
        fresh = bool(r) and r.get("sha") == _spec_sha(_spec_read(lane_name, f["rel"])[0])
        if not fresh or r.get("solidity") is None:
            unrated.append(f["rel"])
        elif r["solidity"] < bar:
            thin.append(f["rel"])
            rated.append(r["solidity"])
        else:
            rated.append(r["solidity"])
    if not files:
        verdict = "missing"
    elif unrated:
        verdict = "unrated"
    elif thin:
        verdict = "thin"
    else:
        verdict = "solid"
    return {
        "lane": lane_name, "bar": bar, "verdict": verdict,
        "files": len(files), "unrated": unrated, "thin": thin,
        # None rather than 0.0 when nothing is rated, for the reason every other
        # score here is: an unmeasured document and a document measured at zero
        # are different claims.
        "solidity": round(min(rated), 3) if rated else None,
    }


def spec_auto_step(lane_name: str) -> dict | None:
    """At most one step of the loop for one lane. Cheapest first.

    Ordered by cost on purpose: rating is one architect call, drafting and
    sharpening are workers. Doing the cheap measurement first means the
    expensive step is aimed at a document somebody has actually judged, rather
    than at whichever one is alphabetically first.
    """
    st = spec_lane_state(lane_name)
    rec = spec_auto(lane_name)

    if st["verdict"] == "solid":
        # Solid is not the end of the loop, it is the point of it. A document
        # nothing ever reads is a document that changed nothing, so the last
        # step is to put it in front of the thing that decides what to build.
        # Once per version of the documents, keyed on their contents rather
        # than on a flag, so a spec that gets rewritten is asked about again and
        # one that has not is not paid for twice.
        fp = spec_fingerprint(lane_name)
        if rec.get("explored_for") == fp or not architect_available():
            return None
        _spec_auto_busy(lane_name, "asking direction what to build from it")
        try:
            out = explore_direction(lane_name, web=False)
        finally:
            _spec_auto_busy(lane_name, None)
        if not out.get("ok"):
            return {"lane": lane_name, "did": "explore", "ok": False,
                    "error": out.get("error")}
        n = sum(1 for p in (out.get("proposals") or []) if p.get("kind") == "goal")
        _spec_auto_note(lane_name, "explore",
                        f"{lane_name} cleared the bar; asked what to build from it",
                        explored_for=fp, explored_at=now(), proposed=n)
        return {"lane": lane_name, "did": "explore", "ok": True, "proposed": n}

    # A worker or a campaign already out for this lane. Nothing to start, and
    # nothing to report - the loop is mid-step, not stuck.
    if spec_draft_status(lane_name):
        return None
    plan = spec_plan(lane_name)
    if plan and plan.get("state") == "running":
        return None

    if st["verdict"] == "missing":
        if not role_on("worker"):
            return None
        if rec.get("drafts", 0) >= SPEC_AUTO_DRAFTS:
            return None
        out = ON_DRAFT_DISPATCH and ON_DRAFT_DISPATCH({
            "lane": lane_name,
            "prompt": spec_draft_prompt(lane_name, spec_candidates(lane_name)),
            "backend": "claude",
            "report_back": False,
            "spec_draft": True,
        })
        if not out or not out.get("ok"):
            # Not counted as an attempt. A dispatch the harness refused - workers
            # switched off, the board at its cap - is not a draft that failed,
            # and burning the budget on it would retire the lane over a queue.
            return {"lane": lane_name, "did": "draft", "ok": False,
                    "error": (out or {}).get("error") or "could not dispatch"}
        _spec_auto_note(lane_name, "draft",
                        f"{lane_name} has no document under docs/spec/",
                        drafts=rec.get("drafts", 0) + 1)
        return {"lane": lane_name, "did": "draft", "ok": True,
                "attempt": rec.get("drafts", 0) + 1}

    if st["verdict"] == "unrated":
        if not architect_available():
            return None
        rel = st["unrated"][0]
        _spec_auto_busy(lane_name, f"rating {rel}")
        try:
            rating = spec_rate(lane_name, rel)
        finally:
            _spec_auto_busy(lane_name, None)
        return {"lane": lane_name, "did": "rate", "ok": not rating.get("error"),
                "rel": rel, "solidity": rating.get("solidity"),
                "error": rating.get("error")}

    # thin
    if not role_on("worker"):
        return None
    fp = spec_fingerprint(lane_name)
    if rec.get("settled_for") == fp:
        # Asked once, refused, and the documents have not changed since. The
        # next tick would get the same refusal for the same reason.
        return None
    if rec.get("campaigns", 0) >= SPEC_AUTO_CAMPAIGNS:
        return None
    plan = spec_campaign_open(lane_name, rels=st["thin"])
    queued = len(plan.get("queue") or [])
    if not queued:
        # The campaign refused every document, and it refused them on a
        # different question than the one that selected them: this loop picks by
        # SOLIDITY - is the document good enough - and a campaign spends by
        # WORTH, which is how much the mission needs the document times how far
        # a round would actually move it. So a lane can be genuinely thin and
        # still have nothing worth paying to sharpen, and that is a real answer
        # rather than a failure.
        #
        # It therefore costs no attempt, and it is recorded against the text it
        # was decided about so the loop stops asking. The first version counted
        # it, and burned a lane's entire campaign budget on three identical
        # no-ops in three minutes - three log lines that read like three
        # campaigns and left the lane retired over work nobody did.
        _spec_auto_note(lane_name, "settled",
                        f"{len(st['thin'])} document(s) under {st['bar']}, none of them "
                        f"worth a sharpen round at the {plan['bar']} worth bar",
                        settled_for=fp)
        return {"lane": lane_name, "did": "settled", "ok": True,
                "thin": len(st["thin"])}
    _spec_auto_note(lane_name, "campaign",
                    f"{len(st['thin'])} document(s) under {st['bar']}",
                    campaigns=rec.get("campaigns", 0) + 1)
    return {"lane": lane_name, "did": "campaign", "ok": True,
            "queued": queued,
            "attempt": rec.get("campaigns", 0) + 1}


def spec_explore(lane_name: str) -> dict:
    """Ask Direction what to build from this lane's documents, on demand.

    The same call the loop makes when a lane clears the bar, reachable without
    waiting for it and without the loop switched on at all. It also RE-asks: the
    loop deliberately will not pay twice for the same text, which is right for
    something running on a heartbeat and wrong as the only way to get an answer,
    because the first answer can simply be a bad one.
    """
    if lane_name not in (config().get("lanes") or {}):
        die(f"unknown lane {lane_name!r}")
    _spec_auto_busy(lane_name, "asking direction what to build from it")
    try:
        out = explore_direction(lane_name, web=False)
    finally:
        _spec_auto_busy(lane_name, None)
    if not out.get("ok"):
        return out
    n = sum(1 for p in (out.get("proposals") or []) if p.get("kind") == "goal")
    _spec_auto_note(lane_name, "explore", "asked by the operator",
                    explored_for=spec_fingerprint(lane_name),
                    explored_at=now(), proposed=n)
    return {**out, "proposed": n}


def spec_auto_run() -> list[dict]:
    """One step, across the whole stack, per call.

    One rather than one-per-lane for the same reason `auto_sharpen` takes one
    proposal: this runs on the heartbeat, every step is a paid call or a worker,
    and eleven lanes stepping together would empty the worker cap in a single
    tick and hold the heartbeat while it did.

    Lanes are taken in a fixed order and the loop remembers nothing about where
    it stopped, so the lane that is worst off is not necessarily the one served
    - but every lane is served within a few ticks, and a rotation that can starve
    is worse than one that is merely unhurried.
    """
    for lane_name in sorted(config().get("lanes") or {}):
        if not spec_auto_on(lane_name):
            continue
        try:
            out = spec_auto_step(lane_name)
        except SystemExit as e:
            # An architect that has gone away will not come back for the next
            # lane either, and eleven identical failures is eleven identical
            # log lines saying nothing new.
            return [{"lane": lane_name, "did": "stop", "ok": False, "error": str(e)}]
        except Exception as e:
            _spec_auto_note(lane_name, "error", f"{type(e).__name__}: {e}"[:200])
            return [{"lane": lane_name, "did": "error", "ok": False,
                     "error": f"{type(e).__name__}: {e}"[:200]}]
        if out:
            return [out]
    return []


def spec_view(lane_name: str) -> dict:
    """One lane's spec documents and every run against them.

    The newest run per document carries its rounds; the older ones are summaries.
    That split is the whole cost control here - a document with a dozen runs
    behind it holds a dozen full reviews and every worker reply, and sending all
    of that on a four-second poll to draw a row that says `closed` is paying to
    move text nobody has opened. The older ones are fetched by id when clicked.
    """
    files = lane_spec_files(lane_name)
    runs = specruns(lane_name)
    by_rel: dict[str, list] = {}
    for row in runs:
        by_rel.setdefault(row["rel"], []).append(row)
    for rows in by_rel.values():
        full = load_specrun(rows[0]["id"])
        if full:
            rows[0] = {**rows[0], "rounds": full.get("rounds") or []}
    lane = (config().get("lanes") or {}).get(lane_name) or {}
    have = {f["rel"] for f in files}
    rates = spec_rates(lane_name)
    rank = {row["rel"]: i + 1 for i, row in enumerate(spec_plan_order(lane_name))}

    rows_out = []
    for f in files:
        r = rates.get(f["rel"])
        if r:
            # Whether the rating is about the bytes that are there now. A rating
            # is a judgement of a specific text, and the whole reason the sha is
            # stored is so that a judgement of a version that has since been
            # rewritten can be shown as what it is instead of quietly ranking
            # a document on a reading of a file that no longer exists.
            r = {**r, "stale": r.get("sha") != _spec_sha(_spec_read(lane_name, f["rel"])[0]),
                 "worth": spec_worth(r)}
        rows_out.append({**f, "runs": by_rel.get(f["rel"]) or [],
                         "rating": r, "rank": rank.get(f["rel"])})

    # Read once. Both of these walk the lane's documents off disk, and asking
    # three times in one response can answer differently each time if a worker
    # writes a file in between - which would draw a verdict about one version of
    # the lane next to a staleness flag about another.
    _auto, _fp, _state = spec_auto(lane_name), spec_fingerprint(lane_name), spec_lane_state(lane_name)

    return {
        "lane": lane_name,
        "path": lane.get("path", "."),
        "files": rows_out,
        "plan": spec_plan(lane_name),
        "worth_bar": spec_worth_bar(),
        # Only asked for when there is nothing to show. It reads every other
        # lane's spec directory off disk, which is cheap but not free, and a
        # lane that has its own documents does not need to be told where someone
        # else's are.
        "candidates": [] if files else spec_candidates(lane_name),
        # Always asked, not only when the lane is empty: a draft worker that has
        # already written the document is exactly the case where `files` is no
        # longer empty while the worker is still running, and dropping the
        # indicator at that moment would say "done" before it is.
        "drafting": spec_draft_status(lane_name),
        # Where this lane stands against the threshold, and what the unattended
        # loop has already spent getting it there. Shown whether or not the loop
        # is switched on: the verdict is the same fact either way, and a bar you
        # can only see while automation is running is a bar you cannot check.
        "state": _state,
        "auto": {**_auto, "max_drafts": SPEC_AUTO_DRAFTS,
                 "max_campaigns": SPEC_AUTO_CAMPAIGNS,
                 # Whether the documents have changed since Direction was last
                 # asked about them. The button says "again" rather than
                 # pretending the previous answer is still about this text.
                 "explored_stale": bool(_auto.get("explored_for"))
                 and _auto.get("explored_for") != _fp,
                 # The loop has stopped on this lane on purpose, and it stopped
                 # on THIS text. Distinct from a spent budget: nothing was spent.
                 "settled": _auto.get("settled_for") == _fp},
        # Runs against a document that is no longer there - renamed, moved, or
        # deleted since. Listed separately rather than dropped: a review of a
        # file nobody can find is exactly the thing that would otherwise vanish
        # without anyone deciding it should.
        "orphans": [r for r in runs if r["rel"] not in have],
        "max_rounds": spec_max_rounds(),
        # Named, because an empty list here and a lane that keeps its design
        # somewhere else look identical on screen and are not the same problem.
        "gap": None if files else
               f"`{lane.get('path', '.')}/docs/spec/` does not exist or holds no markdown",
        "architect": architect_available(),
        "architect_why": None if architect_available() else architect_off_reason(),
    }


def _spec_health_block(names: list[str]) -> str:
    """What has been JUDGED about each lane's documents, not what they say.

    `_specs_block` sends the explorer the headings and opening prose, which is
    what the lane is for. This is the other half: whether anyone has read it and
    what they found wrong with it. The difference matters because the two
    strongest proposals a spec can produce are invisible in the prose - a lane
    with NO design document at all has no prose to send, so it simply does not
    appear, and a named gap in an existing one reads as an absence, which is the
    single hardest thing to notice by reading.

    Gaps are quoted rather than summarised. They were written by a reader who
    had the whole document in front of it, and re-describing them here would put
    a second opinion in front of the first one.
    """
    rows = []
    for name in names:
        if not name:
            continue
        st = spec_lane_state(name)
        rates = spec_rates(name)
        if st["verdict"] == "missing":
            found = spec_candidates(name)
            where = ("" if not found else
                     " Documents elsewhere in the repository that name it: "
                     + ", ".join(f"`{c['path']}` ({c['hits']} mentions)" for c in found[:4])
                     + " - which may mean its design is filed under the wrong lane.")
            rows.append(f"- **{name}**: has NO design document at all.{where}")
            continue
        gaps = []
        for rel, r in sorted(rates.items()):
            for g in (r.get("gaps") or [])[:4]:
                gaps.append(f"    - `{rel}`: {g}")
        scored = (f"lowest solidity {st['solidity']}" if st["solidity"] is not None
                  else "nothing scored yet")
        rows.append(f"- **{name}**: {st['files']} document(s), {scored} "
                    f"(the bar is {st['bar']})."
                    + (f" Under the bar: {', '.join('`' + t + '`' for t in st['thin'])}."
                       if st["thin"] else "")
                    + ("\n" + "\n".join(gaps) if gaps else ""))
    if not rows:
        return ""
    return ("# What has been judged about those documents\n\n"
            "Scores and gaps from a reader that had each document in full. A lane "
            "with no design document, or with a gap named here, is a real thing to "
            "propose against - but propose the WORK, not the writing of the "
            "document: the harness already drafts and sharpens specs on its own, "
            "so \"write a spec for X\" duplicates a loop that is already running.\n\n"
            + "\n".join(rows))


def _explore_context(lane: str | None, *, web: bool) -> str:
    """The whole stack at once, which is the thing a per-goal review never sees."""
    sections = doctrine_sections()
    cfg = config()
    names = sorted(cfg.get("lanes") or {})
    parts = [mission_block().strip(),
             doctrine_block("The doctrine this stack is held to").strip()]

    open_theses = _section(sections, "open theses", "open questions")
    if open_theses:
        parts.append(f"# {open_theses['title']}\n\n{open_theses['body']}")

    stands = _need_block(lane or "")
    if stands:
        parts.append(stands)

    rows = []
    for name in names:
        ln = cfg["lanes"][name] or {}
        live = [g for g in goals(name) if g["state"] in ("planning", "running", "blocked")]
        rec = lane_record(name)
        rows.append(f"- **{name}** ({ln.get('repo') or ln.get('path')}): "
                    f"{len(live)} goal(s) open, "
                    + (f"{rec['done']}/{rec['n']} of its last settled goals finished"
                       if rec["n"] else "nothing settled yet"))
    parts.append("# The lanes\n\n" + "\n".join(rows))

    specs = _specs_block(lane, names)
    if specs:
        parts.append(specs)

    health = _spec_health_block([lane] if lane else names)
    if health:
        parts.append(health)

    # Every lane's findings together, which is the raw material for the only
    # question this call is uniquely able to answer. A finding reported in one
    # lane is invisible to that lane's own review of any other lane, so if there
    # is a connection to be made, this is the only place it can be made.
    fs = findings()[:40]
    if fs:
        by: dict[str, list] = {}
        for f in fs:
            by.setdefault(f.get("lane") or "unfiled", []).append(f)
        parts.append("# What the work has reported back, every lane\n\n"
                     + "\n\n".join(
                         f"## {ln}\n" + "\n".join(
                             f"- **{f['bearing']}** ({f.get('source')}): {f['text'][:400]}"
                             for f in items)
                         for ln, items in sorted(by.items())))

    live = [g for g in goals() if g["state"] in ("planning", "running", "blocked")]
    if live:
        parts.append("# Already being worked, do not propose these again\n\n"
                     + "\n".join(f"- ({g['lane']}) {g['objective'][:200]}" for g in live))

    op = proposals()
    if op:
        parts.append("# Already proposed and waiting, do not restate them\n\n"
                     + "\n".join(f"- ({p.get('lane')}) [{p.get('kind')}] {p.get('text', '')[:200]}"
                                 for p in op[:30]))

    # Shown on purpose. Without it the same idea comes back every time the
    # button is pressed, and dismissing something would stop meaning anything.
    dis = proposals(state="dismissed")[:20]
    if dis:
        parts.append("# Already turned down by the operator, do not re-propose these\n\n"
                     + "\n".join(f"- ({p.get('lane')}) {p.get('text', '')[:200]}" for p in dis))

    parts.append("# Whether you can look outside\n\n"
                 + ("You have web search. Use it, and cite what you read."
                    if web else
                    "You have NO web search on this round. Nothing may be marked `outside`, "
                    "and you may not cite a URL. Answer from what you were given."))
    return "\n\n".join(parts)


def explore_direction(lane: str | None = None, *, web: bool = True) -> dict:
    """Go looking for direction instead of waiting for a goal to finish.

    Costs one architect call, and searches the web unless told not to. This said
    "manual only, and there should not be a heartbeat path into here" for as
    long as the only caller was a button. There is one now - see
    `explore_idle_lanes` - and the sentence was right about what it refused: an
    unattended general explorer over every lane, every tick, with the web on. It
    was wrong that no bounded version could exist, and the fleet paid for it by
    losing lanes one at a time with nothing looking for them.

    This function is unchanged by that and stays indiscriminate on purpose: it
    explores whatever it is pointed at. Every restriction lives in the caller,
    where it can be read in one place, rather than being spread through here as
    conditions that each look reasonable alone.
    """
    if not architect_available():
        return {"ok": False, "error": architect_off_reason()}
    can_web = bool(web) and web_search_backend()

    model = config().get("consult_model", DEFAULT_CONSULT)
    resp = architect_chat([{"role": "system", "content": EXPLORE_SYSTEM},
                           {"role": "user", "content": _explore_context(lane, web=can_web)}],
                          model, web=can_web)
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    out = _json_reply(text)
    if not out:
        return {"ok": False, "error": "the architect's answer could not be read as JSON",
                "raw": text[:2000]}

    rev = {"id": "d" + uuid.uuid4().hex[:9], "at": now(), "lane": lane,
           "goal_id": None, "goal": "",
           "kind": "explore", "web": can_web,
           "assessment": str(out.get("assessment") or "")[:2000],
           # No ladder. A rung moves when evidence moves it, and this call
           # produced no evidence - it read what was already there.
           "ladder": [],
           "exhausted": bool(out.get("exhausted")),
           "why_exhausted": str(out.get("why_exhausted") or "")[:600],
           "tokens": (resp.get("usage") or {}).get("total_tokens") or 0}

    known = set(config().get("lanes") or {})
    seen = {_norm_prompt(p.get("text", "")) for p in direction_store().get("proposals", [])}
    fresh, misfiled = [], []
    for n in (out.get("next") or [])[:3]:
        obj = str((n or {}).get("objective") or "").strip()
        if not obj or _norm_prompt(obj) in seen:
            continue
        want = str(n.get("lane") or "").strip()
        if want not in known:
            # Not silently refiled into whatever lane was on screen. A proposal
            # in the wrong lane is worked by the wrong worker against the wrong
            # tree, and the operator would have no way to see it happened.
            misfiled.append({"objective": obj[:200], "lane": want})
            continue
        seen.add(_norm_prompt(obj))
        origin = str(n.get("origin") or "").strip()
        fresh.append({"id": "p" + uuid.uuid4().hex[:9], "at": now(), "kind": "goal",
                      "lane": want, "text": obj[:1000],
                      "why": str(n.get("why") or "")[:1000], "state": "open",
                      "review_id": rev["id"],
                      # Tagged so calibration can one day ask the only question
                      # that makes this button worth its call: whether anything
                      # explored is ever adopted, and ever moves a rung.
                      "source": "explore",
                      "origin": origin if origin in ("cross_lane", "outside", "reframe") else "",
                      "sources": [str(u)[:300] for u in (n.get("sources") or [])
                                  if str(u).strip().startswith("http")][:6],
                      **_scored(n)})
    for r in (out.get("research") or [])[:3]:
        qn = str((r or {}).get("question") or "").strip()
        if not qn or _norm_prompt(qn) in seen:
            continue
        seen.add(_norm_prompt(qn))
        want = str(r.get("lane") or "").strip()
        fresh.append({"id": "p" + uuid.uuid4().hex[:9], "at": now(), "kind": "question",
                      "lane": want if want in known else lane,
                      "text": qn[:1000], "why": str(r.get("why") or "")[:1000],
                      "settled_by": str(r.get("settled_by") or "")[:1000],
                      "state": "open", "review_id": rev["id"], "source": "explore"})

    with _DIRECTION_LOCK:
        store = direction_store()
        store.setdefault("proposals", []).extend(fresh)
        store.setdefault("reviews", []).append(rev)
        _save_direction(store)

    ngoal = sum(1 for p in fresh if p["kind"] == "goal")
    nq = sum(1 for p in fresh if p["kind"] == "question")
    add_note(f"explored{' the web and' if can_web else ''} "
             + (f"{lane}, which had no goal and nothing waiting" if lane else "every lane")
             + ": " + rev["assessment"][:400]
             + (f" — {ngoal} goal(s) proposed" if ngoal else "")
             + (f", {nq} open question(s)" if nq else "")
             + (" — nothing worth proposing: " + rev["why_exhausted"][:200]
                if rev["exhausted"] else "")
             + ("".join(f" — proposed for an unknown lane {m['lane']!r}, dropped: "
                        f"{m['objective'][:120]}" for m in misfiled)),
             lane=lane)

    rev["proposals"] = fresh
    rev["misfiled"] = misfiled
    rev["ok"] = True
    # Nothing is auto-adopted here, whatever the auto-adopt setting says. That
    # used to be justified by "a person pressed the button, the same person can
    # press adopt", which stopped being true when the heartbeat gained a path in
    # - and the separation matters more now, not less. Exploring is the one call
    # that invents work rather than reacting to it, and a proposal that started
    # itself would be the only work in the harness never held against a bar.
    # These land open like everything else; `resume_adoption` picks them up on
    # the next tick if, and only if, they clear what everything else clears.
    return rev


_SCORE_FIELDS = ('"confidence": 0.0, "need": 0.0, "why_need": "...", "cost_usd": 0.0, '
                 '"headroom": 0.0, "why_headroom": "...", "unknowns": ["..."]')

SHARPEN_SYSTEM = (
    "You are the consulting architect. An objective has been proposed for one lane of the "
    "operator's stack and has not been started. Score it, and then see whether it can be "
    "made more likely to land without giving up what it was for.\n\n"
    "Reply with one JSON object and nothing else:\n"
    "{\n"
    "  " + _SCORE_FIELDS + ",\n"
    '  "reasoning": "why those numbers, 1-3 sentences",\n'
    '  "revision": {"objective": "...", "why": "...", ' + _SCORE_FIELDS + ",\n"
    '               "what_changed": "what you narrowed or split off, and which unknown that '
    'kills"} or null,\n'
    '  "alternates": [{"objective": "...", "why": "...", ' + _SCORE_FIELDS + "}]\n"
    "}\n\n"
    "Rules:\n"
    + _SCORE_RULES + "\n"
    "- A `revision` is THE SAME objective made likelier to land: narrowed, or with the part "
    "that needs something we do not have split off, or aimed at a lower rung of the evidence "
    "ladder first so the higher one becomes reachable later. It must still be worth doing. A "
    "revision that scores well by asking for nothing of value is worse than the original - "
    "return null. Return null too if you cannot honestly beat the original.\n"
    "- Raising `confidence` by cutting the objective down until the mission no longer wants "
    "it is not an improvement, and it will show: `need` is scored too, and a revision that "
    "trades need away for odds is the failure this rule exists to catch. If the only way to "
    "make it likely is to make it not worth doing, return null and say so.\n"
    "- You may not raise a number by deciding something that is the operator's. If an unknown "
    "is money, a credential, an account, a deploy target, or what counts as good enough, the "
    "honest revision is one that DOES NOT NEED it - never one that assumes it, and never one "
    "that quietly redefines done so the missing thing stops mattering.\n"
    "- `alternates` are different routes to the same end, for when the objective is right and "
    "the approach is what is costing it. Zero, one or two. An empty list is a normal answer.\n"
    "- You are proposing. The operator decides what gets built."
)


def _sharpen_record(p: dict) -> str:
    """What the previous attempts on this objective actually achieved.

    The line this replaces said "you have already tried once", which was true
    when two attempts was the whole allowance and is a plain falsehood now that
    a proposal keeps being sharpened while sharpening keeps working. Telling a
    model it is on round two when it is on round five is the harness narrating
    instead of reporting.

    The numbers matter more than the count. Twelve of the fourteen rounds on
    record moved the odds DOWN - the sharpener was being asked to improve
    something without ever being told whether improving it had worked before,
    and it could not see that its own last answer had made things worse.
    """
    log = sharpen_history(p)
    if not log:
        return ""
    lines = []
    for a in log[-4:]:
        b, af = a.get("before"), a.get("after")
        moved = "unmeasured" if b is None or af is None else f"{pct(b)} → {pct(af)}"
        lines.append(f"- round {a.get('round')}: {moved}"
                     + (" (a revision was taken)" if a.get("took_revision") else ""))
    return ("\n\n## What the previous attempts to improve this did\n\n"
            + "\n".join(lines)
            # From `sharpen_rounds`, not from the length of the log. Rounds that
            # ran before the log existed left a count and no measurement, so
            # counting entries reports attempt 2 on a proposal at round 3.
            + f"\n\nThis is attempt {int(p.get('sharpen_rounds') or 0) + 1}. "
              "If the earlier ones did not move it, "
              "say what is actually holding it rather than restating them, and return null "
              "for the revision if nothing you can do moves it.")


def _sharpen_context(p: dict) -> str:
    """What the architect needs to judge one proposed objective."""
    lane_name = p.get("lane") or ""
    parts = [mission_block().strip(),
             doctrine_block("The doctrine this stack is held to").strip(),
             f"# The proposed objective\n\n{p.get('text', '')}"]
    if p.get("why"):
        parts.append(f"## Why it was proposed\n\n{p['why']}")
    if p.get("confidence") is not None:
        parts.append("## What it was scored before\n\n"
                     f"odds {pct(p.get('confidence'))}, "
                     f"need {pct(p.get('need'))}, cost {usd(p.get('cost_usd'))}"
                     + (f"\nwhy it was needed: {p['why_need']}" if p.get("why_need") else "")
                     + ("\nunknowns then: " + "; ".join(p.get("unknowns") or [])
                        if p.get("unknowns") else "")
                     + _sharpen_record(p))
    others = [x for x in goals(lane_name) if x["state"] in ("planning", "running", "blocked")]
    if others:
        parts.append("# Already open in this lane, do not propose these\n\n"
                     + "\n".join(f"- ({o['state']}) {o['objective'][:200]}" for o in others))
    rec = _record_block(lane_name)
    if rec:
        parts.append(rec)
    stands = _need_block(lane_name)
    if stands:
        parts.append(stands)
    # Two documents rather than the focused eight. This call is judging one
    # objective against what the lane is for, not surveying the lane, and it
    # runs once per tick - the deep read belongs to the review that proposed
    # the objective in the first place.
    spec = _specs_block(lane_name, [], limit=SPEC_LANE_FILES)
    if spec:
        parts.append(spec)
    parts.append(f"# The repository right now\n\n{goal_brief(lane_name)}")
    return "\n\n".join(parts)


def sharpen_proposal(pid: str) -> dict:
    """Put odds on a proposal, and try to improve them. One architect call.

    Three things can come back and they are deliberately kept apart:

    the SCORE lands on the proposal itself, so a proposal that is simply good
    becomes adoptable without anything else changing;

    a REVISION is the same objective made likelier, and it becomes a new
    proposal that supersedes this one - but ONLY if it is both more likely to
    land AND no less wanted by the mission. A revision that scores lower is not
    an improvement, it is a different and worse objective with the original
    thrown away, and taking it because it arrived under the heading "revision"
    is how a sharpener talks a lane into smaller and smaller work;

    ALTERNATES are different routes to the same end and are added alongside.
    They never replace anything, because "here is another way" is not a claim
    that the first way was wrong.
    """
    p = next((x for x in direction_store().get("proposals", []) if x.get("id") == pid), None)
    if not p:
        return {"ok": False, "error": f"no proposal {pid!r}"}
    if p.get("state") != "open" or p.get("kind") != "goal":
        return {"ok": False, "error": "only an open objective can be sharpened"}
    if not architect_available():
        return {"ok": False, "error": architect_off_reason()}

    model = config().get("consult_model", DEFAULT_CONSULT)
    resp = architect_chat([{"role": "system", "content": SHARPEN_SYSTEM},
                           {"role": "user", "content": _sharpen_context(p)}], model)
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    out = _json_reply(text)
    if not out:
        return {"ok": False, "error": "the architect's answer could not be read as JSON",
                "raw": text[:2000]}

    scored = _scored(out)
    rounds = int(p.get("sharpen_rounds") or 0) + 1
    was = p.get("confidence")
    # Read before the new score overwrites it. This is the claim the call is
    # about to test: somebody said looking again would help, and this call is
    # the looking.
    claimed = p.get("headroom")

    rev, alts = out.get("revision") or None, []
    made = []
    with _DIRECTION_LOCK:
        store = direction_store()
        cur = next((x for x in store["proposals"] if x.get("id") == pid), None)
        if not cur:
            return {"ok": False, "error": f"no proposal {pid!r}"}
        cur.update(scored)
        cur["sharpen_rounds"] = rounds
        cur["sharpen_reasoning"] = str(out.get("reasoning") or "")[:800]

        seen = {_norm_prompt(x.get("text", "")) for x in store["proposals"]}

        def _add(src: dict, **extra) -> dict | None:
            obj = str((src or {}).get("objective") or "").strip()
            if not obj or _norm_prompt(obj) in seen:
                return None
            seen.add(_norm_prompt(obj))
            new = {"id": "p" + uuid.uuid4().hex[:9], "at": now(), "kind": "goal",
                   "lane": cur.get("lane"), "text": obj[:1000],
                   "why": str(src.get("why") or "")[:1000], "state": "open",
                   "from_goal": cur.get("from_goal"), "review_id": cur.get("review_id"),
                   # Inherited, never re-derived. Sharpening rewrites an
                   # objective; it does not change where the work came from, and
                   # a sharpened repair is still a repair. Left off, this is a
                   # laundry: a `settle-residue` proposal goes through one round
                   # here and comes out classified as development, so a lane set
                   # to maintain silently stops taking the fix it was left open
                   # for. The origin is the whole basis of that decision, so it
                   # travels with the objective.
                   "source": cur.get("source"),
                   "sharpen_rounds": rounds, **_scored(src), **extra}
            store["proposals"].append(new)
            return new

        # More likely to land, AND no less wanted. The second half is the guard
        # that matters now: narrowing an objective almost always raises its
        # odds, and the cheapest way to raise them is to cut away the part the
        # mission actually wanted. That revision arrives looking like progress -
        # a bigger number, under the heading "revision" - and taking it is how a
        # lane gets talked down to work nobody needs. Scoring need is what makes
        # the trade visible; refusing it here is what makes it not happen.
        took = None
        if rev and (rev.get("confidence") or 0) > (scored["confidence"] or 0) \
                and (rev.get("need") or 0) >= (scored["need"] or 0):
            took = _add(rev, sharpened_from=pid,
                        what_changed=str(rev.get("what_changed") or "")[:600])
            if took:
                cur["state"] = "superseded"
                cur["decided_at"] = now()
                cur["superseded_by"] = took["id"]
                made.append(took)
        for a in (out.get("alternates") or [])[:2]:
            new = _add(a, alternate_of=pid)
            if new:
                alts.append(new)
                made.append(new)

        # What the attempt was worth, written down beside what it was predicted
        # to be worth. `after` is the best this objective ended up at, which is
        # the revision's odds when one was taken and the re-score otherwise -
        # because the question headroom answers is whether the objective got
        # better, not whether this particular record did.
        cur.setdefault("sharpen_log", []).append(
            {"at": now(), "round": rounds, "claimed_headroom": claimed,
             "before": was,
             "after": (took or {}).get("confidence") if took else scored["confidence"],
             "took_revision": bool(took)})
        _save_direction(store)

    lane, now_c = p.get("lane"), scored["confidence"]
    note = (f"scored in {lane}: odds {pct(was)} → {pct(now_c)}, "
            f"mission wants it {pct(scored['need'])}, about {usd(scored['cost_usd'])} "
            f"— {p.get('text', '')[:120]}")
    if took:
        note += (f" — sharpened to {pct(took.get('confidence'))} odds at "
                 f"{pct(took.get('need'))} need: {took.get('what_changed', '')[:200]}")
    elif rev:
        # Worth saying out loud. A rejected revision is the guard doing its job,
        # and silence here would read as the sharpener having found nothing.
        note += " — a revision was offered and refused for trading away odds or need"
    if alts:
        note += f" — {len(alts)} alternate route(s) proposed"
    if scored["headroom"] is not None and scored["headroom"] < SHARPEN_FLOOR:
        note += " — nothing left to sharpen: " + (scored["why_headroom"] or "no reason given")[:160]
    add_note(note[:600], lane=lane)
    return {"ok": True, "proposal_id": pid, "confidence": now_c,
            "need": scored["need"], "cost_usd": scored["cost_usd"],
            "headroom": scored["headroom"], "why_headroom": scored["why_headroom"],
            "unknowns": scored["unknowns"], "reasoning": out.get("reasoning"),
            "revision": took if rev else None, "alternates": alts,
            "superseded": bool(took), "rounds": rounds, "made": made}


def direction_review(gid: str, *, auto: bool = False) -> dict:
    """Look back at a finished goal and ask whether there is anywhere left to go.

    Returns the review record. Costs one architect call, which is why it fires on
    a goal closing rather than on a timer: the moment there is something new to
    judge is the moment the judgement is worth paying for.
    """
    g = load_goal(gid)
    if not g:
        return {"ok": False, "error": f"no goal {gid!r}"}
    if not architect_available():
        return {"ok": False, "error": architect_off_reason()}

    sections = doctrine_sections()
    model = g.get("model") or config().get("consult_model", DEFAULT_CONSULT)
    resp = architect_chat([{"role": "system", "content": DIRECTION_SYSTEM},
                           {"role": "user", "content": _direction_context(g, sections)}], model)
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    out = _json_reply(text)
    if not out:
        return {"ok": False, "error": "the architect's answer could not be read as JSON",
                "raw": text[:2000]}

    rev = {"id": "d" + uuid.uuid4().hex[:9], "at": now(), "lane": g.get("lane"),
           "goal_id": gid, "goal": (g.get("objective") or "")[:300],
           "assessment": str(out.get("assessment") or "")[:2000],
           "ladder": [x for x in (out.get("ladder") or []) if isinstance(x, dict)][:6],
           "exhausted": bool(out.get("exhausted")),
           "why_exhausted": str(out.get("why_exhausted") or "")[:600],
           "tokens": (resp.get("usage") or {}).get("total_tokens") or 0,
           "auto": auto}

    # Nothing already open, and nothing already turned down. A review that
    # re-proposed what was dismissed last week would make dismissing it useless.
    seen = {_norm_prompt(p.get("text", "")) for p in direction_store().get("proposals", [])}
    fresh = []
    for n in (out.get("next") or []):
        obj = str((n or {}).get("objective") or "").strip()
        if not obj or _norm_prompt(obj) in seen:
            continue
        seen.add(_norm_prompt(obj))
        fresh.append({"id": "p" + uuid.uuid4().hex[:9], "at": now(), "kind": "goal",
                      "lane": g.get("lane"), "text": obj[:1000],
                      "why": str(n.get("why") or "")[:1000], "state": "open",
                      "from_goal": gid, "review_id": rev["id"],
                      # Stated, though it is also what an absent origin would
                      # default to. The default is a safety net for origins
                      # nobody has classified; leaning on it here would mean the
                      # single biggest producer of proposals in the harness is
                      # classified by silence, and the first time somebody
                      # changed that default this path would change with it
                      # without anyone deciding it should.
                      "source": "review",
                      **_scored(n)})
    for r in (out.get("research") or []):
        qn = str((r or {}).get("question") or "").strip()
        if not qn or _norm_prompt(qn) in seen:
            continue
        seen.add(_norm_prompt(qn))
        fresh.append({"id": "p" + uuid.uuid4().hex[:9], "at": now(), "kind": "question",
                      "lane": g.get("lane"), "text": qn[:1000],
                      "why": str(r.get("why") or "")[:1000],
                      "settled_by": str(r.get("settled_by") or "")[:1000],
                      "state": "open", "from_goal": gid, "review_id": rev["id"]})

    with _DIRECTION_LOCK:
        store = direction_store()
        store.setdefault("proposals", []).extend(fresh)
        store.setdefault("reviews", []).append(rev)
        _save_direction(store)

    ngoal = sum(1 for p in fresh if p["kind"] == "goal")
    nq = sum(1 for p in fresh if p["kind"] == "question")
    # Straight into the orchestrator thread, because this is the one thing in the
    # harness that is about where the whole stack is going rather than what one
    # worker did, and it should not have to be gone looking for.
    add_note(f"direction · {g.get('lane')}: {rev['assessment'][:400]}"
             + (f" — {ngoal} goal(s) proposed" if ngoal else "")
             + (f", {nq} open question(s)" if nq else "")
             + (" — nothing further worth doing here: " + rev["why_exhausted"][:200]
                if rev["exhausted"] else ""),
             lane=g.get("lane"))
    goal_log(gid, f"direction reviewed: {ngoal} proposed, {nq} question(s)")

    rev["proposals"] = fresh
    rev["ok"] = True
    if direction_store().get("auto_adopt"):
        blocked = escalations(g.get("lane"))
        rev["escalations"] = blocked
        if blocked:
            # Deliberately leaves the proposals `open`. The work is not thrown
            # away and nothing running is stopped - the pipeline just stops
            # feeding itself, which is the whole difference between autonomy
            # and a machine nobody can get in front of.
            names = ", ".join(b["gate"] for b in blocked)
            goal_log(gid, f"not auto-adopted: {names}")
            add_note(f"auto-adopt held in {g.get('lane')} — {names}. "
                     + " ".join(b["why"] for b in blocked)[:600]
                     + f" {ngoal} proposed objective(s) are waiting for you in Direction.",
                     lane=g.get("lane"))
        else:
            # The gates are about the workspace and have just said yes. The bar
            # is about each objective on its own, so it is asked separately and
            # per proposal - a review can quite properly adopt one of its two
            # and leave the other showing why it was not taken.
            picks = [p for p in fresh if p["kind"] == "goal"]
            rev["adopted"] = [adopt_proposal(p["id"]) for p in picks if not proposal_hold(p)]
            held = [(p, proposal_hold(p)) for p in picks if proposal_hold(p)]
            rev["held"] = [{"id": p["id"], "text": p["text"], "why": why} for p, why in held]
            for p, why in held:
                add_note(f"not adopted in {p['lane']} — {why}: {p['text'][:200]}"
                         + (" Unknowns: " + "; ".join(p.get("unknowns") or [])
                            if p.get("unknowns") else ""),
                         lane=p.get("lane"))
    return rev


def adopt_proposal(pid: str) -> dict:
    """Turn a proposed objective into a real goal. Only this makes it work.

    A question is never adopted this way. It is not an objective, it is something
    we do not know, and the answer to it is Travis writing it into the doctrine's
    open theses - which no agent may do.
    """
    p = next((x for x in direction_store().get("proposals", []) if x.get("id") == pid), None)
    if not p:
        return {"ok": False, "error": f"no proposal {pid!r}"}
    if p.get("state") != "open":
        return {"ok": False, "error": f"already {p.get('state')}"}
    if p.get("kind") != "goal":
        return {"ok": False, "error": "an open question is not an objective"}
    objective = p["text"] + (f"\n\nWhy: {p['why']}" if p.get("why") else "")
    g = open_goal(p["lane"], objective)
    set_proposal(pid, "adopted", goal_id=g.get("id"))
    # The scores travel with the work. Without this they live only in the
    # proposal store, which is trimmed, and `calibration` would start quietly
    # losing its oldest and most informative rows.
    with _GOAL_LOCK:
        gg = load_goal(g["id"])
        if gg:
            gg["confidence"] = p.get("confidence")
            gg["need"] = p.get("need")
            gg["cost_estimate_usd"] = p.get("cost_usd")
            gg["unknowns"] = p.get("unknowns") or []
            gg["proposal_id"] = pid
            save_goal(gg)
    add_note(f"adopted a proposed goal in {p['lane']}: {pct(p.get('confidence'))} odds, "
             f"mission wants it {pct(p.get('need'))}, about {usd(p.get('cost_usd'))} — "
             f"{p['text'][:200]}", lane=p.get("lane"))
    return {"ok": True, "goal": g, "proposal_id": pid, "confidence": p.get("confidence"),
            "need": p.get("need"), "cost_usd": p.get("cost_usd"), "worth": worth(p)}


def resume_adoption() -> list[dict]:
    """Adopt one waiting proposal whose gate has since come down.

    Auto-adopt is decided exactly once, in the direction review that fires as a
    goal closes. If a gate was up at that instant the proposals are left `open`
    - correctly; that is the gate doing its job - and then nothing ever looks
    again. The next review in that lane would look, but a review fires only when
    a goal FINISHES, and the lane now has no goal left to finish. So the lane is
    not paused, it is retired, by a gate that says nothing about that lane.

    Four lanes were sitting like that, one of them for over four hours, behind a
    burn ceiling that resets at midnight and a contradiction count that reading
    the findings would clear. Both of those gates come down on their own, and
    nothing was waiting to notice.

    Nothing here weakens a gate. A lane whose gate is still up is skipped
    exactly as before, and a proposal you dismissed stays dismissed. Crossing a
    threshold just costs a pause now instead of the lane.

    One per call, for the reason triage is: adopting runs `plan_goal`, which is
    an architect call, and doing every waiting lane at once would hold the
    heartbeat for minutes.

    Which one is the point of scoring. This used to take the OLDEST waiting
    proposal, which is a fair rule and an uninformed one - age says nothing
    about whether the mission wants the thing. It now takes the highest `worth`,
    so the one goal a tick can start is the best expected movement per dollar
    rather than whichever happened to be proposed first.
    """
    if not direction_store().get("auto_adopt"):
        return []
    busy = {r.get("lane") for r in goals() if r.get("state") == "running"}
    ready = [p for p in open_proposals()
             # One goal to a lane. The review never had to test this, because it
             # only ever proposes into the lane whose goal has just closed.
             if p.get("lane") not in busy
             and not escalations(p.get("lane"))
             and not proposal_hold(p)]
    # `worth` cannot be None here - nothing unscored gets past `proposal_hold` -
    # but age stays as the tie-break so two equal proposals still resolve in a
    # fixed order rather than on dict ordering.
    ready.sort(key=lambda p: (-(worth(p) or 0), p.get("at") or ""))
    return [adopt_proposal(ready[0]["id"])] if ready else []


# How long a lane that was explored and yielded nothing is left alone. The
# cooldown only ever covers that case: an explore that DOES propose something
# leaves the lane holding a proposal, so it stops being idle and is not picked
# again anyway. What this bounds is the lane the architect has already looked at
# and had no answer for - without it that lane is an architect call every tick,
# forever, for an answer that is not going to change in a minute.
EXPLORE_COOLDOWN_H = 6.0


def idle_lanes() -> list[str]:
    """Lanes with no work, nothing waiting to become work, and no way back.

    Not "lanes doing nothing right now" - lanes that have nothing queued to do
    NEXT either, which is a different and much worse state. A lane with a
    proposal is paused; a lane with neither a goal nor a proposal is retired,
    because the only thing that proposes into a lane on its own is the review
    that fires when one of its goals CLOSES, and a lane with no goal has none
    left to close. Nothing in the harness reaches it again.

    Ordered by how long each has been without direction, longest first. That is
    an uninformed rule and it is chosen deliberately: there is nothing scored to
    sort on here - the absence of anything scored is the situation being fixed -
    and the failure is precisely a lane being passed over indefinitely, so time
    ignored is the one measure that bears on it.
    """
    cfg = config().get("lanes") or {}
    live = {g.get("lane") for g in goals()
            if g.get("state") in ("running", "planning", "blocked")}
    waiting = {p.get("lane") for p in open_proposals()}
    d = direction_store()
    last: dict[str, float] = {}
    for rec in (d.get("proposals") or []) + (d.get("reviews") or []):
        n = rec.get("lane")
        if n:
            last[n] = max(last.get(n, 0.0), _epoch(rec.get("at")))
    for g in goals():
        n = g.get("lane")
        if n:
            last[n] = max(last.get(n, 0.0), _epoch(g.get("opened_at")))
    out = [n for n in cfg if n not in live and n not in waiting]
    return sorted(out, key=lambda n: (last.get(n, 0.0), n))


def explore_idle_lanes() -> list[dict]:
    """Go looking for direction in ONE lane that has none, and would never get any.

    `explore_direction` was manual only, and said so: it costs an architect call
    and can search the web, so a heartbeat into a general explorer is a way to
    spend money all night. That reasoning is still right about a general
    explorer. It was wrong about the fleet, and the measurement is what changed
    it: five of eleven lanes held no goal and no proposal, so nothing would ever
    propose into them again, and the fleet sat at three workers against a cap of
    ten - not held by the cap, the budget, or any gate, but by lanes quietly
    dropping out of the loop one at a time and nothing looking for them.

    So this is not that explorer. It is bounded on every side that made the
    original answer no:

      it only ever considers a lane with NOTHING - no goal, no proposal;
      one lane per call, like adoption, because the call is not cheap;
      no web search, which is the half that made exploring slow and dear;
      a lane it has already looked at is left alone for `EXPLORE_COOLDOWN_H`;
      a gate up in that lane skips it, unchanged;
      and a lane whose mode does not admit development is never touched, which
      is the whole of `archived` - nothing is proposed into it.

    It proposes and nothing more. Whatever comes back is scored, held by the
    same bars, and adopted by the same path as anything else, so this widens
    what the fleet can see and moves none of the decisions about it.
    """
    if not direction_store().get("auto_adopt"):
        return []
    if not architect_available():
        return []
    stamps = (direction_store().get("explored") or {})
    cut = time.time() - EXPLORE_COOLDOWN_H * 3600.0
    for name in idle_lanes():
        if lane_admits(name, "development") is not None:
            continue
        if escalations(name):
            continue
        if _epoch(stamps.get(name)) > cut:
            continue
        # Stamped BEFORE the call, for the reason triage is: a call that dies
        # partway through has still been paid for, and a retry loop around a
        # model call is how an idle lane becomes an expensive idle lane.
        d = direction_store()
        d.setdefault("explored", {})[name] = now()
        _save_direction(d)
        rev = explore_direction(name, web=False)
        return [{"lane": name, "ok": bool(rev.get("ok")),
                 "error": rev.get("error"),
                 "proposed": len(rev.get("proposals") or []),
                 "exhausted": bool(rev.get("exhausted"))}]
    return []


def sharpen_history(p: dict) -> list[dict]:
    """Every sharpening round this objective has been through, oldest first.

    A round that produces a revision retires the record it improved and carries
    the objective forward under a new id, so what the earlier rounds measured
    stays on the retired record. The log is deliberately not copied onto the
    successor - `headroom_calibration` counts every entry it can find, and a
    copy would count each round twice - so the successor is walked back through
    `sharpened_from` instead.
    """
    by_id = {x.get("id"): x for x in direction_store().get("proposals", [])}
    seen: set = set()
    out: list[dict] = []
    cur: dict | None = p
    while cur is not None and cur.get("id") not in seen:
        seen.add(cur.get("id"))
        out = list(cur.get("sharpen_log") or []) + out
        cur = by_id.get(cur.get("sharpened_from")) if cur.get("sharpened_from") else None
    return out


def sharpen_gain(p: dict) -> float | None:
    """How much the last round actually moved this objective's odds.

    Read off the log every round already writes. The measurement was being
    recorded and nothing was reading it, which is why the stopping rule could
    only count attempts.

    None when no round has been run, and that is not zero: an objective nobody
    has sharpened has not failed to improve, it has not been tried.
    """
    log = sharpen_history(p)
    if not log:
        return None
    before, after = log[-1].get("before"), log[-1].get("after")
    return None if before is None or after is None else after - before


def sharpen_converged(p: dict) -> str | None:
    """Why this proposal is done being sharpened, or None if it is not.

    The rule this replaces was a flat count - two tries and stop, whatever
    happened in them - which gets both cases wrong. It cuts off an objective
    still climbing, and it goes on paying for one that has not moved in rounds.
    The record already says which is which, so the stopping rule reads the
    measurement instead of the attempt count: keep going while going is
    working, stop when it stops.

    A negative gain stops it too. A round that made the odds worse is not a
    round to run again.

    `SHARPEN_HARD_CAP` is a spend backstop and nothing more. It exists so an
    objective that improves by a hair every single time cannot bill for ever,
    and it is reported differently on purpose: "stopped improving" means this
    is as good as it gets, "hit the cap" means it was cut off mid-climb and
    the operator may want to look.

    Returns the sentence rather than a bool because that difference is the
    thing worth showing.
    """
    # Scoring is not sharpening. A proposal missing a score has no odds to
    # improve, so there is no gain to measure and nothing to have converged -
    # the same reason the old cap did not apply to it either.
    if p.get("confidence") is None or p.get("need") is None:
        return None
    gain = sharpen_gain(p)
    if gain is not None and gain < SHARPEN_GAIN_FLOOR:
        return (f"last round moved the odds {gain:+.0%}, under the "
                f"{SHARPEN_GAIN_FLOOR:.0%} that makes another one worth paying for")
    if int(p.get("sharpen_rounds") or 0) >= SHARPEN_HARD_CAP:
        return (f"stopped at the {SHARPEN_HARD_CAP}-round spend cap while it was "
                f"still gaining — not because it ran out of room")
    return None


def sharpenable() -> list[dict]:
    """Open proposals that are being held back and are worth another look.

    Held back now includes held back BY its own headroom - see `proposal_hold`.
    This used to skip anything above the bars on the reasoning that it was
    already going to be adopted and a call to agree with it bought nothing. What
    that missed is that clearing the bars and being well written are different
    facts: three docs objectives sat at 96-98% odds with 62-80% room to improve
    and no sharpening round ever run, because every one of them was too good to
    be looked at again and would have started as first drafted.

    Unscored first, because a proposal nobody has judged is not being held back
    for a reason - it is simply unjudged, and one call turns it into something
    that can be decided either way.

    After that, highest `headroom` first, which is the whole reason that number
    exists. There is one of these calls per tick, and ordering them by age or by
    worth answers a question nobody asked: age says nothing at all, and worth
    says which one would be best IF it could be rescued, not which one can. A
    proposal held at 30% because it bundles five things can be split and will
    move; one held at 30% because it needs a credential nobody has will score
    30% forever. Those two look identical until something asks which is which.

    Below `SHARPEN_FLOOR` it is dropped entirely rather than ranked last. The
    architect has already said, in `why_headroom`, that nothing it can do moves
    this - so the next call would spend real money to be told that again. It
    stays open and stays visible, and the operator can still sharpen it by hand,
    because "the architect can't move this" is not "this is dead".

    Stopping is `sharpen_converged`, which asks what the last round measured
    rather than how many have been run. It does not apply to a proposal missing
    a score outright, because scoring is not sharpening. Three sat open for
    hours at 72%, 79% and 80% odds with no `need` on them at all, written by a
    build that had no such field - every one of them over the bar it was being
    held against, and every one already at the old flat cap, so nothing would
    look at them again. A lane can be retired by a field that arrived after it.
    """
    return sorted([p for p in open_proposals()
                   if proposal_hold(p)
                   and not sharpen_converged(p)
                   and (p.get("headroom") is None or p["headroom"] >= SHARPEN_FLOOR)],
                  key=lambda p: (p.get("headroom") is not None,
                                 -(p.get("headroom") or 0),
                                 -(worth(p) or 0), p.get("at") or ""))


def auto_sharpen() -> list[dict]:
    """Score or improve one held-back proposal per call.

    Not gated on `escalations()`, unlike adoption, and the difference is the
    point: a gate stops the pipeline STARTING work, and this starts none. It
    turns a proposal nobody can judge into one that can be judged, which is
    what an operator standing at a raised gate actually needs. Triage is
    allowed past the gates for the same reason.

    `auto_adopt` still governs it, because that is the switch for the harness
    spending on the pipeline unattended, and an architect call is spending.

    One per call: it is an architect call and this runs on the heartbeat.
    """
    if not direction_store().get("auto_adopt"):
        return []
    for p in sharpenable():
        return [sharpen_proposal(p["id"])]
    return []


def direction_state(lane: str | None = None) -> dict:
    """Where the direction itself stands. Derived, never stored.

    "Nowhere left to go" is a real answer and it is the one the panel used to be
    worst at showing: it looked exactly like "nothing has run yet", which is a
    completely different situation with a completely different fix. These are the
    facts that tell them apart, and they are kept separate on purpose -
    proposals waiting is about the list, gates up is about the workspace, and
    merging them into one traffic light would hide whichever was true.
    """
    store = direction_store()
    props = [p for p in store.get("proposals", [])
             if p.get("state") == "open" and p.get("kind") == "goal"
             and (not lane or p.get("lane") == lane)]
    held: dict[str, int] = {}
    ready = 0
    for p in props:
        why = proposal_hold(p)
        if why:
            # Tallied under one phrase rather than under its own sentence. Every
            # other hold reason quotes the objective's own numbers, so counting
            # the sentences groups them usefully; a sharpening hold quotes its
            # headroom, which is different for every proposal, so the same tally
            # would print three all-but-identical sentences that add up to one
            # fact - and the fact worth reporting is that the harness is dealing
            # with them, not what each one's percentage happens to be.
            key = "still being sharpened" if held_for_sharpening(p) else why
            held[key] = held.get(key, 0) + 1
        else:
            ready += 1

    ex = [r for r in store.get("reviews", []) if r.get("kind") == "explore"]
    last_ex = max(ex, key=lambda r: r.get("at") or "") if ex else None
    cases = [r for r in store.get("reviews", []) if r.get("kind") == "case"]
    last_case = max(cases, key=lambda r: r.get("at") or "") if cases else None
    gates = escalations(lane)
    running = sum(1 for g in goals(lane) if g.get("state") == "running")

    if props and ready:
        verdict = "ready"
        # Split, because "waiting on you" is an instruction and it is false for
        # the ones the harness has queued for a sharpening round of its own.
        sharpening = held.get("still being sharpened", 0)
        waiting = len(props) - ready - sharpening
        headline = (f"{ready} objective(s) can start"
                    + (f", {waiting} waiting on you" if waiting else "")
                    + (f", {sharpening} still being sharpened" if sharpening else ""))
    elif props:
        verdict = "held"
        headline = (f"{len(props)} objective(s) waiting, none of them able to start - "
                    + "; ".join(f"{n} because {w}" for w, n in sorted(held.items(),
                                                                     key=lambda kv: -kv[1])))
    elif last_ex and last_ex.get("exhausted"):
        verdict = "nowhere"
        headline = ("nowhere left to go that this stack can see. Explored "
                    + str(last_ex.get("at") or "")[:16].replace("T", " ")
                    + (" including the web" if last_ex.get("web") else "")
                    + " and it found nothing worth proposing.")
    elif last_ex:
        verdict = "empty"
        headline = "nothing waiting. The last look found things but none of them survived."
    else:
        verdict = "unexplored"
        headline = ("nothing waiting, and nobody has gone looking. A review only fires when "
                    "a goal finishes.")

    return {
        "verdict": verdict,
        "headline": headline,
        "waiting": len(props),
        "ready": ready,
        "running": running,
        "held": [{"why": w, "n": n} for w, n in sorted(held.items(), key=lambda kv: -kv[1])],
        # Kept beside the verdict rather than folded into it. A lane can have
        # objectives ready to start AND be unable to start them, and one number
        # cannot say both.
        "gates": gates,
        "auto_adopt": bool(store.get("auto_adopt")),
        "last_explore": ({k: last_ex.get(k) for k in
                          ("id", "at", "lane", "web", "exhausted", "why_exhausted",
                           "assessment", "tokens")}
                         if last_ex else None),
        "last_explore_found": sum(1 for p in store.get("proposals", [])
                                  if last_ex and p.get("review_id") == last_ex.get("id")),
        "last_case": last_case,
        # The button that writes the case is offered when there is a case to
        # write: the direction is stuck, or the workspace is.
        "case_worth_asking": verdict in ("nowhere", "empty", "held", "unexplored") or bool(gates),
    }


# --------------------------------------------------------------------- the case
#
# What to do when the honest answer is "nowhere". Exploring can tell you the
# stack has run out of moves; it cannot tell you why, because the reason is
# almost never in the code - it is a decision nobody has made. This writes that
# up and puts it in front of the operator, which is the only place it can be
# settled. It proposes no objective, because there is none to propose: that is
# the situation being described.

CASE_SYSTEM = (
    "You are the consulting architect. The operator's stack has run out of moves it can "
    "make by itself, and you are being asked to write up why and put it in front of them.\n\n"
    "You are given the mission, the doctrine, where each lane's evidence has got to, what "
    "the work has reported, everything proposed and everything turned down, the thresholds "
    "currently holding the fleet, and what the last look for new direction concluded.\n\n"
    "This is NOT a request for another objective. If there were one worth proposing it "
    "would already be on the list. Almost always the real blocker is a decision nobody has "
    "made - what counts as good enough, what to spend, what to publish, what to entrench, "
    "which of two directions to take - and the work cannot make it. Find it and state it.\n\n"
    "Reply with one JSON object and nothing else:\n"
    "{\n"
    '  "situation": "what is actually true right now, 3-5 sentences, no consolation",\n'
    '  "blocked_on": [{"what": "the specific thing", '
    '"whose": "operator|work|world",\n'
    '                  "why": "why nothing moves until it is settled"}],\n'
    '  "decisions": [{"decision": "the question only the operator can answer, as a '
    'question",\n'
    '                 "options": ["the real alternatives, including doing nothing"],\n'
    '                 "recommend": "which one, and it must be one of the options",\n'
    '                 "because": "the reason, in terms of the mission",\n'
    '                 "unlocks": "what specifically becomes possible once it is settled",\n'
    '                 "cost_of_waiting": "what it costs to leave this open"}],\n'
    '  "direction_change": {"what": "a change to the direction itself, if the honest '
    'reading is that the current one is wrong", "why": "...", "instead": "..."} or null,\n'
    '  "nothing_needed": false,\n'
    '  "why_nothing_needed": "if the stack is genuinely fine and simply between things, '
    'say so plainly"\n'
    "}\n\n"
    "Rules:\n"
    "- Name the decision, do not make it. Recommending is your job; deciding is the "
    "operator's, and writing as though it were settled is the one thing you may not do.\n"
    "- `whose` must be honest. `operator` is money, credentials, accounts, deploy targets, "
    "what counts as good enough, what to publish, what to entrench. `work` is something a "
    "worker could go and do - and if anything is `work`, say in `situation` why it is not "
    "already proposed, because that is a hole in the pipeline and worth more than the case. "
    "`world` is something that does not exist yet, anywhere.\n"
    "- At most three decisions, ordered by what unlocks most. A list of everything that "
    "could be decided is worthless; the operator needs the one to make first.\n"
    "- `cost_of_waiting` may not be rhetorical. If leaving it open costs nothing much, say "
    "that - it is useful, and it is how the operator knows which to ignore.\n"
    "- `direction_change` is for when the mission or the doctrine is what is wrong, not the "
    "plan under it. It is a serious thing to say and usually null. Say it when it is true.\n"
    "- If the stack is simply between things and needs nothing, set `nothing_needed` and "
    "say so. That is a better answer than an invented crisis."
)


def _case_context(lane: str | None) -> str:
    st = direction_state(lane)
    parts = [_explore_context(lane, web=False),
             "# Where the direction itself stands\n\n" + st["headline"]]
    if st["held"]:
        parts.append("## Waiting objectives nothing will start\n\n"
                     + "\n".join(f"- {h['n']} because {h['why']}" for h in st["held"]))
    if st["gates"]:
        parts.append("# Thresholds currently holding the whole workspace\n\n"
                     + "\n".join(f"- **{g['gate']}** (at {g['at']}, your limit {g['limit']}): "
                                 f"{g['why']}" for g in st["gates"]))
    ex = st["last_explore"]
    if ex:
        parts.append("# What the last look for new direction concluded\n\n"
                     + f"{ex.get('assessment') or ''}\n\n"
                     + (f"It concluded there was nowhere left to go: {ex.get('why_exhausted')}"
                        if ex.get("exhausted") else
                        f"It proposed {st['last_explore_found']} thing(s)."))
    return "\n\n".join(parts)


def direction_case(lane: str | None = None) -> dict:
    """Write up why the stack is stuck and put it in front of the operator.

    Costs one architect call. Produces no objective - if there were one worth
    proposing it would be on the list already, and the absence is the point.
    """
    if not architect_available():
        return {"ok": False, "error": architect_off_reason()}
    model = config().get("consult_model", DEFAULT_CONSULT)
    resp = architect_chat([{"role": "system", "content": CASE_SYSTEM},
                           {"role": "user", "content": _case_context(lane)}], model)
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    out = _json_reply(text)
    if not out:
        return {"ok": False, "error": "the architect's answer could not be read as JSON",
                "raw": text[:2000]}

    def _dec(d):
        opts = [str(o)[:300] for o in (d.get("options") or []) if str(o).strip()][:5]
        rec = str(d.get("recommend") or "")[:300]
        return {"decision": str(d.get("decision") or "")[:500], "options": opts,
                "recommend": rec,
                # Said out loud rather than silently corrected. A recommendation
                # that is not one of the options is the architect having drifted
                # off the question it was asked, and hiding that would leave the
                # operator choosing between three things and told to pick a
                # fourth.
                "off_options": bool(rec and opts and rec not in opts),
                "because": str(d.get("because") or "")[:600],
                "unlocks": str(d.get("unlocks") or "")[:400],
                "cost_of_waiting": str(d.get("cost_of_waiting") or "")[:400]}

    ch = out.get("direction_change")
    rec = {"id": "d" + uuid.uuid4().hex[:9], "at": now(), "lane": lane,
           "kind": "case", "goal_id": None, "goal": "", "ladder": [],
           "assessment": str(out.get("situation") or "")[:2000],
           "blocked_on": [{"what": str(b.get("what") or "")[:400],
                           "whose": str(b.get("whose") or "")[:20],
                           "why": str(b.get("why") or "")[:600]}
                          for b in (out.get("blocked_on") or []) if isinstance(b, dict)][:6],
           "decisions": [_dec(d) for d in (out.get("decisions") or [])
                         if isinstance(d, dict)][:3],
           "direction_change": ({"what": str(ch.get("what") or "")[:600],
                                 "why": str(ch.get("why") or "")[:600],
                                 "instead": str(ch.get("instead") or "")[:600]}
                                if isinstance(ch, dict) and str(ch.get("what") or "").strip()
                                else None),
           "nothing_needed": bool(out.get("nothing_needed")),
           "why_nothing_needed": str(out.get("why_nothing_needed") or "")[:600],
           "exhausted": False, "why_exhausted": "",
           "tokens": (resp.get("usage") or {}).get("total_tokens") or 0}

    with _DIRECTION_LOCK:
        store = direction_store()
        store.setdefault("reviews", []).append(rec)
        _save_direction(store)

    # Into the orchestrator thread in full, not summarised. This is the one
    # thing the harness produces that is addressed to Travis rather than about
    # the work, and it is useless if it has to be gone looking for.
    body = [f"the case for {lane or 'the whole stack'}: {rec['assessment']}"]
    for d in rec["decisions"]:
        body.append(f"\n— YOUR CALL: {d['decision']}"
                    + (f"\n  options: {' | '.join(d['options'])}" if d["options"] else "")
                    + (f"\n  it recommends: {d['recommend']}" if d["recommend"] else "")
                    + (" (which is not one of the options it gave)" if d["off_options"] else "")
                    + (f"\n  because: {d['because']}" if d["because"] else "")
                    + (f"\n  unlocks: {d['unlocks']}" if d["unlocks"] else "")
                    + (f"\n  leaving it open costs: {d['cost_of_waiting']}"
                       if d["cost_of_waiting"] else ""))
    if rec["direction_change"]:
        body.append(f"\n— IT THINKS THE DIRECTION ITSELF IS WRONG: "
                    f"{rec['direction_change']['what']} — {rec['direction_change']['why']} "
                    f"Instead: {rec['direction_change']['instead']}")
    if rec["nothing_needed"]:
        body.append(f"\n— it says nothing is needed: {rec['why_nothing_needed']}")
    add_note("\n".join(body)[:4000], lane=lane)

    rec["ok"] = True
    return rec


def direction_view(lane: str | None = None) -> dict:
    """Everything the Direction tab shows, assembled in one place."""
    sections = doctrine_sections()
    store = direction_store()
    props = [p for p in store.get("proposals", []) if p.get("state") == "open"]
    if lane:
        props = [p for p in props if p.get("lane") == lane]
    revs = sorted(store.get("reviews", []), key=lambda r: r.get("at") or "", reverse=True)
    if lane:
        # Explores and cases are about the whole stack, so filtering them by the
        # lane that happened to be selected when the button was pressed hides
        # them from every other lane - which is how a 90-second architect call
        # can run, record, and look to the operator like nothing happened.
        revs = [r for r in revs
                if r.get("lane") == lane or r.get("kind") in ("explore", "case")]
    thesis = _section(sections, "thesis")
    return {
        "thesis": thesis,
        "ladder": _section(sections, "ladder"),
        "values": _section(sections, "rules"),
        "owed": _section(sections, "owe"),
        "open_theses": _section(sections, "open theses", "open questions"),
        "sections": sections,
        "findings": findings(lane=lane)[:60],
        "findings_summary": findings_summary(),
        # `hold` and `worth` are derived here rather than stored, so moving a bar
        # in Settings re-judges every waiting proposal at once instead of leaving
        # them labelled against a threshold that is no longer the threshold.
        # `sharpen_done` is derived here too, and it carries its own reason: the
        # button that disappears has to be able to say whether the objective
        # stopped improving or was cut off by the spend cap.
        "proposals": sorted([{**p, "hold": proposal_hold(p), "worth": worth(p),
                              "hold_is_sharpen": held_for_sharpening(p),
                              "sharpen_rounds": int(p.get("sharpen_rounds") or 0),
                              "sharpen_gain": sharpen_gain(p),
                              "sharpen_done": sharpen_converged(p)}
                             for p in props if p.get("kind") == "goal"],
                            key=lambda p: p.get("at") or "", reverse=True),
        "questions": sorted([p for p in props if p.get("kind") == "question"],
                            key=lambda p: p.get("at") or "", reverse=True),
        "reviews": revs[:8],
        "auto_adopt": bool(store.get("auto_adopt")),
        "bar": adopt_bar(),
        "need_bar": need_bar(),
        "calibration": calibration(),
        "rungs": lane_rungs(),
        # Sits with `rungs`, and is not lane-filtered for the same reason `rungs`
        # is not: this pair is the whole-stack record of what the workspace
        # believes. These are the claims that turned out to be false without ever
        # having earned a rung, so nothing in `rungs` moved for them - which is
        # exactly why they need showing. The ladder is the only place the console
        # reports what was believed, and these were never on it.
        "corrections": corrections(),
        "sharpen_cap": SHARPEN_HARD_CAP,
        "sharpen_gain_floor": SHARPEN_GAIN_FLOOR,
        "sharpen_floor": SHARPEN_FLOOR,
        # So the button can say what it is about to do rather than promising
        # research and then answering from recall.
        "can_web": web_search_backend(),
        "state": direction_state(lane),
        "doctrine_state": doctrine_state(),
        "doctrine_path": str(DOCTRINE_PATH),
        # A lane whose goal is finished and never reviewed is exactly where the
        # pipeline goes quiet, so the tab can offer to review it by name.
        "reviewable": [{"id": g["id"], "lane": g["lane"], "objective": g["objective"][:200]}
                       for g in goals(lane)
                       if g["state"] == "done"
                       and not any(r.get("goal_id") == g["id"] for r in store.get("reviews", []))],
    }


# ---------------------------------------------------------------- the supervisor
#
# Everything below the supervisor is local. A worker sees one task. A goal sees
# one objective. A direction review sees one lane, at the moment one goal in it
# finished. Each of those can be entirely correct and the workspace as a whole
# can still be quietly going somewhere nobody chose - the classic failure is not
# a bad step, it is a hundred good steps in a direction that stopped being the
# point three weeks ago.
#
# The supervisor is the only pass that looks at all of it at once and holds it
# against the mission. It is given the mission, the doctrine, every goal in
# every lane with its state, the open proposals and unsettled questions, what
# the work has reported back, which obligations are drifting, and what is on the
# board right now.
#
# Three hard limits on it, in descending order of how easy they would be to lose:
#
#   It never acts. Not a dispatch, not a close, not an adoption, not a pause. It
#   returns a reading and a recommendation and stops. A supervisor that could
#   cancel a goal for being off-mission would be deciding what gets built, which
#   is rule 6, and it would be doing it from a summary rather than from the work.
#
#   It never edits the mission. If the mission is silent on something real it
#   says so under `mission_gap` and that is a question for Travis, not a patch.
#
#   It refuses to run without a mission at all, rather than inventing one to
#   measure against. "Aligned with nothing" is not a finding, it is a fabrication,
#   and rule 2 covers it.

SUPERVISOR_SYSTEM = (
    "You are the supervisor of one workspace in an operator's stack. You are not doing "
    "the work and you are not planning it. Your single job is to say whether what is "
    "actually happening still serves the mission you are given, and to name precisely "
    "where it does not.\n\n"
    "You are given: the mission, in the operator's own words; the doctrine every agent "
    "here is held to; every goal in the workspace with its lane and state; the objectives "
    "proposed but not adopted, and the questions still open; what the work has reported "
    "back about the doctrine; which standing obligations are currently failing; and what "
    "is running right now.\n\n"
    "Reply with one JSON object and nothing else:\n"
    "{\n"
    '  "verdict": "aligned|drifting|off_mission",\n'
    '  "summary": "what this workspace is actually doing right now, and whether that is '
    'the mission, 2-4 sentences",\n'
    '  "aligned": [{"what": "the work", "why": "the part of the mission it serves"}],\n'
    '  "drift": [{"what": "the work", "where": "lane, or goal id, or obligation name",\n'
    '             "why": "what about it does not serve the mission",\n'
    '             "recommend": "what you would do about it"}],\n'
    '  "missing": [{"what": "something the mission calls for that nothing here is doing",\n'
    '               "why": "why it matters"}],\n'
    '  "mission_gap": "if the mission is silent or ambiguous about something real that is '
    'happening here, say what. Otherwise the empty string."\n'
    "}\n\n"
    "Rules:\n"
    "- Judge against the MISSION, not against whether the work looks good. Excellent work "
    "on the wrong thing is the exact failure you exist to catch, and it will look like "
    "progress in everything you are shown.\n"
    "- `drift` must point at something specific by lane, goal id or name. A general worry "
    "with nothing attached to it is not usable and does not belong in the list.\n"
    "- `missing` is for what the mission asks for and nothing here is carrying. An empty "
    "list is a normal answer.\n"
    "- You recommend. You do not decide, and nothing you return is carried out "
    "automatically. Do not write as if work will be stopped because you said so.\n"
    "- Do not invent counts, states or outcomes. If you were not shown it, you do not "
    "know it, and saying so is a better answer than an estimate.\n"
    "- If the workspace genuinely is on mission, say `aligned` and keep it short. Do not "
    "manufacture drift to have something to report."
)

SUPERVISOR_VERDICTS = ("aligned", "drifting", "off_mission")


def supervisor_store() -> dict:
    return load_json(SUPERVISOR_PATH, {"reports": []})


def _save_supervisor(store: dict):
    store["reports"] = store.get("reports", [])[-40:]
    save_json(SUPERVISOR_PATH, store)


def _supervisor_context() -> str:
    """The whole workspace, flattened, in the order it should be read."""
    ws = workspace()
    parts = [f"# The mission of this workspace ({ws.get('name')})\n\n{mission()}",
             doctrine_block("The doctrine every agent here is held to").strip()]

    gs = goals()
    if gs:
        by_lane: dict[str, list[dict]] = {}
        for g in gs:
            by_lane.setdefault(g.get("lane") or "?", []).append(g)
        lines = []
        for lane_name in sorted(by_lane):
            lines.append(f"## {lane_name}")
            for g in sorted(by_lane[lane_name], key=lambda x: x.get("opened_at") or ""):
                lines.append(
                    f"- [{g['state']}] `{g['id']}` {(g.get('objective') or '')[:220]}"
                    + (f"  ({g['met']}/{g['dod']} done-conditions met)" if g.get("dod") else "")
                    + (f"  now: {g['now'][:120]}" if g.get("now") else ""))
        parts.append("# Every goal in this workspace\n\n" + "\n".join(lines))
    else:
        parts.append("# Every goal in this workspace\n\nThere are none.")

    props = proposals(kind="goal")[:12]
    if props:
        parts.append("# Proposed but not adopted\n\n"
                     + "\n".join(f"- ({p.get('lane')}) {p['text'][:220]}" for p in props))
    qs = proposals(kind="question")[:12]
    if qs:
        parts.append("# Questions open and unsettled\n\n"
                     + "\n".join(f"- ({p.get('lane')}) {p['text'][:220]}" for p in qs))

    fs = findings()[:12]
    if fs:
        parts.append("# What the work has reported back about the doctrine\n\n"
                     + "\n".join(f"- **{f['bearing']}** ({f.get('lane') or '-'}): {f['text'][:400]}"
                                 for f in fs))

    bad = [o for o in obligations() if o.get("state") in ("drifted", "broken")]
    if bad:
        parts.append("# Standing obligations that are currently failing\n\n"
                     + "\n".join(f"- [{o['state']}] {o.get('name')}: `{o.get('check')}`"
                                 + (f" — {o.get('why')}" if o.get("why") else "")
                                 for o in bad))

    b = board()
    live = [t for t in b.get("tasks", {}).values() if t.get("state") not in TERMINAL_STATES]
    if live:
        parts.append("# Running right now\n\n"
                     + "\n".join(f"- ({t.get('lane')}) {str(t.get('prompt', ''))[:180]}"
                                 for t in live[:20]))

    lanes = sorted((config().get("lanes") or {}).keys())
    parts.append("# Lanes registered in this workspace\n\n"
                 + (", ".join(lanes) if lanes else "none"))
    return "\n\n".join(p for p in parts if p)


def supervise_workspace() -> dict:
    """Hold the whole workspace against its mission. Reports; changes nothing."""
    if not mission():
        return {"ok": False, "error": "this workspace has no mission yet, and there is "
                                      "nothing to hold the work against until it does",
                "needs_mission": True}
    # Its own switch and its own model, not the architect's. The two roles are
    # asked different questions and an operator who turns off escalation to stop
    # spending has not thereby said the workspace should stop being checked.
    ready, why = role_ready("supervisor")
    if not ready:
        return {"ok": False, "error": f"the supervisor cannot run: {why}"}

    resp = role_chat("supervisor",
                     [{"role": "system", "content": SUPERVISOR_SYSTEM},
                      {"role": "user", "content": _supervisor_context()}])
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    out = _json_reply(text)
    if not out:
        return {"ok": False, "error": "the supervisor's answer could not be read as JSON",
                "raw": text[:2000]}

    verdict = str(out.get("verdict") or "").strip().lower()
    rep = {
        "id": "s" + uuid.uuid4().hex[:9], "at": now(),
        "workspace": current_workspace(),
        "mission": mission(),
        # An unrecognised verdict is not quietly promoted to "aligned". The
        # reading is unknown, and unknown is a legitimate thing to display.
        "verdict": verdict if verdict in SUPERVISOR_VERDICTS else "unknown",
        "summary": str(out.get("summary") or "")[:2000],
        "aligned": [x for x in (out.get("aligned") or []) if isinstance(x, dict)][:12],
        "drift": [x for x in (out.get("drift") or []) if isinstance(x, dict)][:12],
        "missing": [x for x in (out.get("missing") or []) if isinstance(x, dict)][:12],
        "mission_gap": str(out.get("mission_gap") or "")[:1000],
        # Who read it. A reading is only as good as what took it, and the model
        # can be changed between readings.
        "model": role_model("supervisor"),
        "tokens": (resp.get("usage") or {}).get("total_tokens") or 0,
    }
    store = supervisor_store()
    store.setdefault("reports", []).append(rep)
    _save_supervisor(store)

    nd, nm = len(rep["drift"]), len(rep["missing"])
    add_note(f"supervisor · {rep['verdict']}: {rep['summary'][:400]}"
             + (f" — {nd} drifting" if nd else "")
             + (f", {nm} unattended" if nm else "")
             + (f" — the mission does not say: {rep['mission_gap'][:200]}"
                if rep["mission_gap"] else ""))
    rep["ok"] = True
    return rep


def supervisor_view() -> dict:
    """What the mission strip shows: the last reading, and whether it is stale.

    Stale means the workspace has moved since the reading was taken. It is a
    count of what has closed or been proposed since, not a clock, because a
    reading of a workspace nobody touched is not out of date just because it is
    from yesterday.
    """
    store = supervisor_store()
    reps = [r for r in store.get("reports", []) if r.get("workspace") == current_workspace()]
    last = reps[-1] if reps else None
    since = 0
    if last:
        at = last.get("at") or ""
        since = (sum(1 for g in goals() if (g.get("updated_at") or "") > at)
                 + sum(1 for p in proposals(state="") if (p.get("at") or "") > at))
    return {
        "mission": mission(),
        "last": last,
        "stale": bool(last and since),
        "since": since,
        "history": [{"id": r["id"], "at": r["at"], "verdict": r.get("verdict"),
                     "drift": len(r.get("drift") or [])} for r in reps[-12:]][::-1],
        "available": architect_available(),
        "why_unavailable": None if architect_available() else architect_off_reason(),
    }


# ---------------------------------------------------------------- observations
#
# What is obviously wrong, right now, computed from what is already on disk. No
# model is asked, because these are facts and a model would only be able to
# guess at them - and because the point of this is to be true at a glance, in a
# window the operator is already looking at.

_KILL_PHRASES = ("over its time limit", "hung for", "held more than",
                 "stopped mid-task")


def _norm_prompt(p: str) -> str:
    return " ".join((p or "").lower().split())[:200]


def _resume_hint(lane_name: str, rec: dict | None) -> str | None:
    """The session id an observation is telling you to continue, or nothing.

    Nothing is the honest answer when the transcript is gone: a suggestion that
    cannot be acted on is worse than no suggestion.
    """
    if not rec:
        return None
    sid = rec.get("session_id") or rec.get("resume_of") or rec.get("task_id")
    return sid if sid and transcript_path(sid) else None


def observations() -> list[dict]:
    """Problems worth naming, newest evidence first. Facts only."""
    out = []
    cfg = config()
    b = board()
    tasks = b.get("tasks") or {}

    # The doctrine every worker is running under is not what you last ratified.
    # High, and first: this is the one file where a change nobody authorised
    # changes what everything else is held to.
    d = doctrine_state()
    if d["drifted"]:
        out.append({
            "kind": "doctrine-changed", "lane": None, "severity": "high",
            "at": now(), "ratify": True,
            "text": f"the doctrine core at {DOCTRINE_PATH} has changed since you "
                    f"ratified it ({d['ratified']} -> {d['digest']}). Every worker and "
                    f"every architect turn is already running under the new text.",
            "fix": "read the diff, then confirm it was you",
        })
    elif d["unratified"]:
        out.append({
            "kind": "doctrine-unratified", "lane": None, "severity": "medium",
            "at": now(), "ratify": True,
            "text": f"the doctrine at {DOCTRINE_PATH} is being injected into every "
                    f"worker and architect prompt but you have never ratified it.",
            "fix": "read it, then confirm it is yours",
        })

    # A standing obligation whose check says it is no longer true. This is the
    # only place in this list where the evidence is something the harness went
    # and RAN, rather than something it noticed lying around, so it carries the
    # command and the rung the check reached.
    for ob in obligations():
        if ob.get("state") == "drifted":
            out.append({
                "kind": "obligation-drifted", "lane": ob.get("lane"), "severity": "medium",
                "at": ob.get("last_drift_at") or ob.get("last_checked") or now(),
                "text": f"{ob['name']} is no longer current: `{ob['check']}` exited "
                        f"{ob.get('last_rc')} at {ob.get('last_checked')}.",
                "fix": (ob.get("fix") or "no fix is recorded for this obligation")
                       + ("" if ob.get("auto_fix") else
                          " (auto-fix is off, so nothing has been dispatched)"),
            })
        elif ob.get("state") == "broken":
            # Not drift. A checker that cannot run has no verdict to report, and
            # saying "stale" on its behalf would be inventing an observation.
            out.append({
                "kind": "obligation-broken", "lane": ob.get("lane"), "severity": "medium",
                "at": ob.get("last_checked") or now(),
                "text": f"the check for {ob['name']} could not run, so nothing is known "
                        f"about whether it is current: {(ob.get('last_output') or '')[:200]}",
                "fix": "fix the check before trusting its verdict",
            })

    # A worker killed by a limit, twice, on the same instruction. Sending it a
    # third time is not persistence, it is the same thirty minutes again.
    for lane_name, recs in tasks.items():
        seen: dict[str, list[dict]] = {}
        for r in recs:
            if any(p in (r.get("error") or "") for p in _KILL_PHRASES):
                seen.setdefault(_norm_prompt(r.get("prompt")), []).append(r)
        for _, group in seen.items():
            if len(group) < 2:
                continue
            # The one to continue is the attempt that got furthest, which is the
            # most recent - not whichever the board happens to list first.
            latest = max(group, key=lambda r: r.get("finished_at") or "")
            # Telling someone to resume a session they have already resumed is
            # noise, and worse than noise when the resume was killed too: the
            # advice reads as untried when it has been tried and did not hold.
            # So say what became of it. A continuation is a later record that
            # names this session as the one it resumed.
            sid = latest.get("session_id") or latest.get("task_id")
            after = [r for r in recs
                     if r.get("resume_of") == sid
                     and r.get("task_id") != latest.get("task_id")
                     and (r.get("finished_at") or "") > (latest.get("finished_at") or "")]
            went = max(after, key=lambda r: r.get("finished_at") or "", default=None)
            done = bool(went) and went.get("status") == "completed"
            out.append({
                "kind": "repeat-kill", "lane": lane_name,
                "severity": "medium" if done else "high",
                "at": group[0].get("finished_at"),
                "text": f"the same instruction has been killed by a limit {len(group)} times "
                        f"in {lane_name}: \"{(group[0].get('prompt') or '')[:120]}…\". "
                        + ("Dispatching it again unchanged will end the same way."
                           if not went else
                           f"It was resumed {len(after)} time(s) since, and the last of "
                           f"those {'finished' if done else 'was cut off too'}."),
                "fix": ("check what it landed before sending anything else" if done else
                        "resume its session instead of restarting it, or cut the task down"),
                "task_id": group[0].get("task_id"),
                # Naming the session is what makes the advice followable: without
                # it, "resume its session" means finding a uuid by hand.
                "resume": _resume_hint(lane_name, latest),
            })

    # Work a killed worker left behind. The worktree still has it; nothing is
    # coming back for it on its own.
    for lane_name, lane in cfg["lanes"].items():
        wt = WORKTREE_DIR / lane_name
        if not wt.exists():
            continue
        dirty = run(["git", "status", "--porcelain"], cwd=wt).stdout.strip()
        if not dirty:
            continue
        last = next((r for r in tasks.get(lane_name, []) if r.get("backend") == "claude"), None)
        if not last or last.get("status") == "running":
            continue                                # someone is still working in there
        # Who left it is not the point and often cannot be known - a later
        # worker runs in the same tree. The fact is that there are changes in a
        # worktree no branch will ever be merged from unless someone applies it.
        # The session worth resuming is the last one that was cut off, which is
        # often not the last one that ran - a short worker can finish after it.
        # Ordered by when they stopped, because the board is in the order things
        # were started and a long worker finishes after a later short one.
        cut_off = [r for r in tasks.get(lane_name, [])
                   if r.get("backend") == "claude" and r.get("killed")]
        killed_last = max(cut_off, key=lambda r: r.get("finished_at") or "", default=None)
        # A worker the operator stopped on purpose is not an alarm: the stopping
        # was a decision, and whatever it left is that decision's consequence.
        # Only a worker a limit cut off is something nobody chose.
        killed = bool(killed_last) and any(
            p in (killed_last.get("error") or "") for p in _KILL_PHRASES)
        n = len(dirty.splitlines())
        out.append({
            "kind": "work-at-risk", "lane": lane_name,
            "severity": "high" if killed else "medium",
            "at": last.get("finished_at"),
            "text": f"{n} uncommitted file(s) sitting in the {lane_name} worktree"
                    + (f", and a worker there {killed_last.get('error') or 'did not finish'}"
                       if killed else ", and no worker is running")
                    + ". Nothing will pick this up by itself.",
            "fix": "resume that session to finish and commit it, or review the diff and apply it",
            "task_id": last.get("task_id"),
            "resume": _resume_hint(lane_name, killed_last or last),
        })

    # Threads and goals that are stopped and waiting on a person.
    for c in consults():
        if c.get("blocked_on") == "gathering":
            # Waiting on a worker is fine. Waiting on a worker that already
            # stopped is the failure that looks exactly like working.
            sent = next((r for lane_recs in tasks.values() for r in lane_recs
                         if r.get("consult_id") == c["id"] and r.get("report_back")), None)
            if sent and sent.get("status") != "running":
                got = load_consult(c["id"]) or {}
                if not any(t.get("task_id") == sent.get("task_id") and t.get("role") == "worker"
                           for t in got.get("turns") or []):
                    out.append({
                        "kind": "thread-mute", "lane": c.get("lane"), "severity": "high",
                        "at": sent.get("finished_at"),
                        "text": f"consult {c['id']} ({c.get('lane')}) is waiting on a worker "
                                "that already finished. Its report never reached the thread, "
                                "so it will wait forever.",
                        "fix": "continue it - that relays the report",
                        "consult_id": c["id"],
                    })
            continue
        if c.get("blocked_on") is None:
            continue
        # Why it stopped and whether it needs a person are two different
        # questions, and only the second one is urgent. A thread that hits the
        # token ceiling holding an unanswered decision was being reported as a
        # budget footnote - medium, no question shown - when what was actually
        # true is that the architect had asked something only the operator can
        # answer and then lost the ability to ask again. The reason it stopped
        # was masking the reason it needs you.
        asks = [n["text"] for n in c.get("needs_typed") or [] if n["kind"] == "decision"]
        why = c.get("blocked_why") or c["blocked_on"]
        limit = c["blocked_on"] in ("rounds", "tokens", "stalled")
        out.append({
            "kind": "thread-stopped", "lane": c.get("lane"),
            "severity": "high" if (asks or c["blocked_on"] == "operator") else "medium",
            "at": c.get("last_at"),
            # The question, not a note that there is one. Quoting it is the
            # difference between the operator answering now and going to find
            # out what was asked first - and the orchestrator brief already
            # promises this is surfaced "with the actual question".
            "text": f"consult {c['id']} ({c.get('lane')}) is stopped: {why}."
                    + (" It asks: " + "; ".join(asks)[:300] if asks else ""),
            "fix": ("answer it - it cannot continue itself any further"
                    if limit and asks else
                    "continue it, or answer it" if limit else "answer it, or continue it"),
            "consult_id": c["id"],
        })
    for g in goals():
        if g.get("state") == "blocked":
            out.append({
                "kind": "goal-stopped", "lane": g.get("lane"), "severity": "high",
                "at": g.get("updated_at"),
                "text": f"goal {g['id']} ({g.get('lane')}) is stopped: "
                        f"{g.get('stopped_why') or g.get('stopped_on')}"
                        + (" It asks: " + "; ".join(g["questions"])[:300] if g["questions"] else ""),
                "fix": "answer it",
                "goal_id": g["id"],
            })

    # Two lanes over one directory. Their workers get separate worktrees, so
    # this is not corruption - it is two plans for one codebase, which is the
    # coordination problem that shows up as work being undone.
    by_path: dict[str, list[str]] = {}
    for lane_name, lane in cfg["lanes"].items():
        by_path.setdefault(str((ROOT / lane.get("path", ".")).resolve()), []).append(lane_name)
    for path, names in by_path.items():
        if len(names) > 1:
            out.append({
                "kind": "shared-repo", "lane": names[0], "severity": "medium", "at": None,
                "text": f"lanes {', '.join(sorted(names))} all point at {path}. Each gets its "
                        "own worktree, so they will not see each other's work, and two of them "
                        "finishing means two branches over the same files.",
                "fix": "give them one goal between them, or merge the lanes",
            })

    order = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda o: (order.get(o.get("severity"), 3), o.get("at") or ""), reverse=False)
    return out


# ---------------------------------------------------------------- the report
#
# One HTML file that says what has happened, what is happening, and where this
# is going. Everything on the console is a live view of one slice; a report is
# the whole workspace at a moment, saved, so that the next one can be read
# against it.
#
# Three rules, all of them the same rule the rest of this file runs on:
#
#   Every line answers to a recorded event. The report reads the stores and
#   counts what is in them. It does not ask a model what happened, because a
#   model would be reconstructing from the same records with a chance of being
#   wrong about them.
#
#   "Since the last report" is a real boundary, not a window of days. A report
#   records when it was taken; the next one covers what moved after that. A
#   week in which nothing happened produces a report that says nothing
#   happened, which is the true and useful answer.
#
#   The one part a model does write - suggested reading - is fetched with a web
#   search and then every link is CHECKED by this harness before it ships. A
#   plausible URL is exactly the failure mode, so a link that does not resolve
#   is reported as unreachable rather than quietly kept or quietly dropped.

def report_store() -> dict:
    return load_json(REPORT_PATH, {"reports": []})


def _save_reports(store: dict):
    store["reports"] = store.get("reports", [])[-60:]
    save_json(REPORT_PATH, store)


def reports() -> list[dict]:
    """Every report taken for this workspace, newest first."""
    ws = current_workspace()
    rows = [r for r in report_store().get("reports", []) if r.get("workspace") == ws]
    return sorted(rows, key=lambda r: r.get("at") or "", reverse=True)


def last_report() -> dict | None:
    rows = reports()
    return rows[0] if rows else None


def _after(iso: str | None, since: str | None) -> bool:
    """Whether a timestamp falls inside the window. No `since` means everything."""
    if not since:
        return True
    return bool(iso) and str(iso) > since


def report_window(since: str | None) -> dict:
    """What moved since `since`. Empty lists are a real answer."""
    store = direction_store()
    revs = [r for r in store.get("reviews", []) if _after(r.get("at"), since)]
    props = store.get("proposals", [])
    rows = goals()

    # A goal is counted as closed in this window if it left the running states
    # while the window was open. `updated_at` is the only stamp a goal keeps for
    # that, so a goal edited after it closed lands in the later window - which is
    # the honest reading of the record rather than a guess at the earlier date.
    closed = [g for g in rows
              if g.get("state") not in ("running", "planning")
              and _after(g.get("updated_at"), since)]
    opened = [g for g in rows if _after(g.get("opened_at"), since)]

    # Rung moves are the only measure of progress this stack recognises, so they
    # are pulled out of the reviews and listed on their own.
    ladder = []
    for r in revs:
        for e in (r.get("ladder") or []):
            ladder.append({"lane": r.get("lane"), "at": r.get("at"),
                           "claim": str(e.get("claim") or "")[:300],
                           "from": e.get("from"), "to": e.get("to"),
                           "goal_id": r.get("goal_id")})

    tasks, spend = [], 0.0
    for lane_name, recs in (board().get("tasks") or {}).items():
        for r in recs:
            if not _after(r.get("finished_at") or r.get("dispatched_at"), since):
                continue
            spend += float(r.get("cost_usd") or 0)
            tasks.append({"lane": lane_name, "task_id": r.get("task_id"),
                          "status": r.get("status"), "killed": bool(r.get("killed")),
                          "at": r.get("finished_at") or r.get("dispatched_at"),
                          "cost_usd": round(float(r.get("cost_usd") or 0), 2),
                          "goal_id": r.get("goal_id"),
                          "prompt": str(r.get("prompt") or "")[:200],
                          "error": str(r.get("error") or "")[:200]})
    tasks.sort(key=lambda t: t.get("at") or "", reverse=True)

    obs = [{"name": o.get("name"), "lane": o.get("lane"), "state": o.get("state"),
            "at": o.get("last_checked"), "check": o.get("check"),
            "rc": o.get("last_rc")}
           for o in obligations()
           if _after(o.get("last_checked"), since) and o.get("state") in ("drifted", "broken")]

    sup = [r for r in supervisor_store().get("reports", [])
           if r.get("workspace") == current_workspace() and _after(r.get("at"), since)]

    out = {
        "goals_closed": closed, "goals_opened": opened,
        "ladder": sorted(ladder, key=lambda e: e.get("at") or "", reverse=True),
        "findings": [f for f in findings() if _after(f.get("at"), since)],
        "reviews": sorted(revs, key=lambda r: r.get("at") or "", reverse=True),
        "raised": [p for p in props if _after(p.get("at"), since)],
        "adopted": [p for p in props
                    if p.get("state") == "adopted" and _after(p.get("decided_at"), since)],
        "declined": [p for p in props
                     if p.get("state") == "declined" and _after(p.get("decided_at"), since)],
        "tasks": tasks, "spend_usd": round(spend, 2),
        "obligations": obs, "supervisor": sup,
    }
    out["counts"] = {k: len(v) for k, v in out.items() if isinstance(v, list)}
    # Whether anything at all moved. Said plainly, because a report that lists
    # six empty sections reads like a broken report rather than a quiet week.
    out["quiet"] = not any(out["counts"].values())
    return out


def report_now() -> dict:
    """What is in flight at the moment the report is taken."""
    rows = goals()
    stats = worker_stats()
    b = board()
    live = []
    for lane_name, recs in (b.get("tasks") or {}).items():
        for r in recs:
            if r.get("status") != "running":
                continue
            s = stats.get(r.get("task_id")) or {}
            live.append({"lane": lane_name, "task_id": r.get("task_id"),
                         "goal_id": r.get("goal_id"),
                         "prompt": str(r.get("prompt") or "")[:200],
                         "dispatched_at": r.get("dispatched_at"),
                         "elapsed_s": s.get("elapsed_s"), "rss_gb": s.get("rss_gb"),
                         "cpu_pct": s.get("cpu_pct"), "stalled": bool(s.get("stalled")),
                         "adopted": bool(s.get("adopted")),
                         "doing": s.get("doing") or s.get("last") or ""})
    lim = limits()
    return {
        "goals": [g for g in rows if g.get("state") in ("running", "planning")],
        "blocked": [g for g in rows if g.get("state") == "blocked"],
        "workers": sorted(live, key=lambda w: w.get("dispatched_at") or ""),
        "worker_cap": lim["max_workers"], "live": live_workers(),
        "limits": lim,
        "consults": [c for c in consults() if c.get("blocked_on") or c.get("status") == "open"],
        "spend_today": notional_spend_today(),
        "day_limit": escalate_policy().get("notional_day_usd"),
    }


def report_headed() -> dict:
    """Where this is going, and what is standing in the way of going there."""
    sections = doctrine_sections()
    props = sorted([{**p, "hold": proposal_hold(p), "worth": worth(p)}
                    for p in open_proposals() if p.get("kind") == "goal"],
                   key=lambda p: (p.get("worth") is None, -(p.get("worth") or 0)))
    return {
        "state": direction_state(None),
        "proposals": props,
        "questions": [p for p in open_proposals() if p.get("kind") == "question"],
        "gates": escalations(),
        "supervisor": (supervisor_view() or {}).get("last"),
        "rungs": lane_rungs(), "ladder": list(LADDER_RUNGS),
        # Claims found false that never earned a rung, so nothing above moves for
        # them. Shown because they are otherwise invisible: the ladder is the
        # only place the console reports what was believed, and these were never
        # on it.
        "corrections": corrections(),
        "theses": _section(sections, "open theses", "open questions"),
        "thesis": _section(sections, "thesis"),
        "auto_adopt": bool(direction_store().get("auto_adopt")),
        "bar": adopt_bar(), "need_bar": need_bar(),
        "calibration": calibration(),
    }


def report_lanes() -> dict:
    """Every lane, with the record it has actually built up."""
    cfg = config()
    rungs = lane_rungs()
    rows = goals()
    out = []
    for name, lane in (cfg.get("lanes") or {}).items():
        mine = [g for g in rows if g.get("lane") == name]
        out.append({
            "name": name, "path": lane.get("path"), "repo": lane.get("repo"),
            "backend": lane_backend(lane), "rung": rungs.get(name),
            "record": lane_record(name),
            "goals": {"total": len(mine),
                      "running": sum(1 for g in mine if g.get("state") == "running"),
                      "done": sum(1 for g in mine if g.get("state") == "done"),
                      "blocked": sum(1 for g in mine if g.get("state") == "blocked")},
            "failure_streak": lane_failure_streak(name),
        })
    return {"lanes": sorted(out, key=lambda r: r["name"])}


# How far back the charts look. Long enough for a trend, short enough that a
# quiet fortnight does not bury a busy week at one pixel per day.
HISTORY_DAYS = 21


def report_history(days: int = HISTORY_DAYS) -> dict:
    """Day by day, for as far back as the charts reach.

    Deliberately NOT scoped to the report window. The window answers "what
    moved since last time"; a chart answers "what has this looked like", and a
    trend drawn from a single window would be one point pretending to be a line.
    """
    today = datetime.now(timezone.utc).date()
    span = [str(today - timedelta(days=n)) for n in range(days - 1, -1, -1)]
    first = span[0]
    blank = {"runs": 0, "completed": 0, "failed": 0, "killed": 0, "spend": 0.0,
             "opened": 0, "closed": 0, "rungs": 0, "findings": 0}
    by_day = {d: dict(blank) for d in span}

    def bump(iso, key, n=1):
        d = str(iso or "")[:10]
        if d in by_day:
            by_day[d][key] += n

    lanes: dict[str, dict] = {}

    def lane(name):
        return lanes.setdefault(str(name), {
            "lane": str(name), "runs": 0, "completed": 0, "failed": 0,
            "killed": 0, "spend": 0.0, "rungs": 0, "findings": 0})

    for lane_name, recs in (board().get("tasks") or {}).items():
        for r in recs:
            at = r.get("finished_at") or r.get("dispatched_at")
            cost = float(r.get("cost_usd") or 0)
            status, killed = r.get("status"), bool(r.get("killed"))
            L = lane(lane_name)
            L["runs"] += 1
            L["spend"] += cost
            # `killed` is checked first: a run stopped by a limit is recorded as
            # failed AND killed, and counting it in both columns would make the
            # three outcomes add up to more than the runs they came from.
            L["killed" if killed else
              "completed" if status == "completed" else "failed"] += 1
            if str(at or "")[:10] >= first:
                bump(at, "runs")
                bump(at, "spend", cost)
                bump(at, "killed" if killed else
                     "completed" if status == "completed" else "failed")

    for g in goals():
        bump(g.get("opened_at"), "opened")
        if g.get("state") not in ("running", "planning"):
            bump(g.get("updated_at"), "closed")

    for r in direction_store().get("reviews", []):
        n = len(r.get("ladder") or [])
        if n:
            bump(r.get("at"), "rungs", n)
            lane(r.get("lane") or "?")["rungs"] += n

    for f in findings():
        bump(f.get("at"), "findings")
        lane(f.get("lane") or "?")["findings"] += 1

    rows = [{"day": d, **by_day[d]} for d in span]
    tot = {k: round(sum(r[k] for r in rows), 2) for k in blank}
    runs = sum(l["runs"] for l in lanes.values())
    return {
        "days": days, "from": first, "to": span[-1],
        "series": rows, "totals": tot,
        "lanes": sorted(lanes.values(), key=lambda l: -l["spend"]),
        "outcomes": {"completed": sum(l["completed"] for l in lanes.values()),
                     "failed": sum(l["failed"] for l in lanes.values()),
                     "killed": sum(l["killed"] for l in lanes.values()),
                     "runs": runs},
        "spend_all_time": round(sum(l["spend"] for l in lanes.values()), 2),
    }


# A rating is only worth printing once there is enough behind it to mean
# something. Under this many runs a lane is reported as unrated rather than
# given a grade that one lucky afternoon would have earned.
RATE_MIN_RUNS = 3

RATING_BANDS = ((0.80, "strong", "ok"), (0.60, "steady", "ok"),
                (0.40, "uneven", "warn"), (0.0, "stalled", "bad"))


def _band(score: float) -> tuple[str, str]:
    for floor, label, cls in RATING_BANDS:
        if score >= floor:
            return label, cls
    return "stalled", "bad"


def report_ratings() -> dict:
    """A scorecard per lane, and the arithmetic that produced it in the open.

    The score is a plain mean of whichever of four measures a lane actually has
    a record for. It is a summary of this workspace's own record, not a judgment
    of the code: a lane nobody has run is unrated, not bad.
    """
    hist = {l["lane"]: l for l in report_history()["lanes"]}
    streaks, out = {}, []
    for row in report_lanes()["lanes"]:
        name = row["name"]
        h = hist.get(name, {"runs": 0, "completed": 0, "failed": 0,
                            "killed": 0, "spend": 0.0, "rungs": 0, "findings": 0})
        g, runs = row["goals"], h["runs"]
        streak = row["failure_streak"] or 0
        streaks[name] = streak

        # Evidence is always scored, never omitted. A lane with nothing on the
        # ladder is not a lane with data missing - it is a lane whose claims are on
        # record as unproven, and skipping the measure would score it on everything
        # except the one thing it is worst at.
        parts = {"evidence": ((LADDER_RUNGS.index(row["rung"]) + 1) / len(LADDER_RUNGS)
                              if row["rung"] in LADDER_RUNGS else 0.0)}
        if runs:
            parts["reliability"] = h["completed"] / runs
        if g["total"]:
            parts["delivery"] = g["done"] / g["total"]
        if runs:
            parts["steadiness"] = max(0.0, 1.0 - streak / 3.0)

        rated = runs >= RATE_MIN_RUNS and len(parts) >= 2
        score = sum(parts.values()) / len(parts) if parts else None
        label, cls = _band(score) if (rated and score is not None) else ("unrated", "")
        out.append({
            "lane": name, "rung": row["rung"], "backend": row["backend"],
            "runs": runs, "completed": h["completed"], "failed": h["failed"],
            "killed": h["killed"], "spend": round(h["spend"], 2),
            "findings": h["findings"], "rungs_moved": h["rungs"],
            "goals": g, "failure_streak": streak,
            "success": round(h["completed"] / runs, 3) if runs else None,
            "cost_per_run": round(h["spend"] / runs, 2) if runs else None,
            "parts": {k: round(v, 3) for k, v in parts.items()},
            "score": round(score, 3) if score is not None else None,
            "rating": label, "cls": cls, "rated": rated,
            "why_unrated": ("" if rated else
                            f"{runs} run(s) on record, {RATE_MIN_RUNS} needed to rate"),
        })
    out.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0), r["lane"]))
    rated = [r for r in out if r["rated"]]
    return {
        "lanes": out, "rated": len(rated), "unrated": len(out) - len(rated),
        "min_runs": RATE_MIN_RUNS,
        "mean": round(sum(r["score"] for r in rated) / len(rated), 3) if rated else None,
        "measures": {
            "evidence": "how far up the evidence ladder this lane's claims have got; "
                        "nothing on the ladder scores nothing",
            "reliability": "worker runs that finished, over runs dispatched",
            "delivery": "goals reached done, over goals opened in this lane",
            "steadiness": "no run of consecutive failures; three in a row scores nothing",
        },
    }


# Where an action item belongs. The four are not severities - they are four
# different KINDS of answer, and mixing them is how "we should think about the
# roadmap" ends up in the same list as "a worker is wedged".
ACTION_GROUPS = (
    ("now", "Do now", "Something is stopping work, and it will not clear itself."),
    ("direction", "Change of direction",
     "The records say where this is pointed needs revisiting, not just working harder at."),
    ("goals", "Change to goal setting",
     "How objectives are judged, sized and let through is off against what actually happened."),
    ("watch", "Worth watching",
     "Real, on the record, not yet worth interrupting anything for."),
)

_SEV_RANK = {"high": 0, "medium": 1, "low": 2}


def _problem_headline(o: dict) -> str:
    """An observation as one scannable line: which thing, then what to do.

    The fix alone is not enough to head an item. Five stopped goals in five
    different lanes all carry the fix "answer it", and a list of five identical
    headings is a list nobody reads down. The subject comes off the front of the
    observation's own text, which is where it already names what it is about.
    """
    txt = " ".join(str(o.get("text") or "").split())
    fix = " ".join(str(o.get("fix") or "").split()) or "look at this"
    subject = re.split(r"[:.](?:\s|$)", txt, maxsplit=1)[0].strip()[:90]
    return f"{subject} - {fix[:110]}" if subject else fix


def report_actions(data: dict) -> dict:
    """What to actually do, derived from the records the rest of the page shows.

    Every item names the record that raised it. Nothing here is invented: if the
    record clears, the item stops appearing on the next report by itself. That
    is the whole point of deriving it rather than asking a model what it thinks
    the operator should do.
    """
    H, N, W = data["headed"], data["now"], data["window"]
    items = []

    def add(group, sev, what, why, *, where="", evidence=""):
        items.append({"group": group, "severity": sev, "what": what, "why": why,
                      "where": where, "evidence": evidence})

    # --- gates. The harness already decided these are stopping the fleet.
    for g in H["gates"]:
        add("now", "high", f'clear the {g["gate"]} gate', g["why"],
            evidence=f'{g["at"]} against a limit of {g["limit"]}')

    # --- standing problems. `fix` reads as an instruction on its own, but on its
    # own it also repeats: five different stopped goals all say "answer it".
    for o in data["problems"]:
        add("now" if o.get("severity") == "high" else "watch",
            o.get("severity") or "low",
            _problem_headline(o), " ".join(str(o.get("text") or "").split()),
            where=o.get("lane") or "", evidence=o.get("kind") or "")

    # --- goals that stopped and were never picked back up.
    for g in N["blocked"]:
        add("now", "high",
            f'unblock or close "{str(g.get("objective") or "")[:80]}"',
            str(g.get("stopped_why") or "it is blocked and nothing is moving it"),
            where=g.get("lane") or "", evidence=f'goal {g.get("id")}')

    # --- nobody is working, and there is nothing stopping them.
    st = H["state"]
    if not N["live"] and st.get("ready"):
        add("now", "high", f'dispatch the {st["ready"]} objective(s) that can start',
            "nothing is running and nothing is holding these back",
            evidence=f'{st["ready"]} ready, 0 of {N["worker_cap"]} workers busy')

    # --- direction.
    if st.get("verdict") in ("nowhere", "empty"):
        add("direction", "high", "propose new objectives",
            st.get("headline") or "there is nowhere left to go",
            evidence=f'direction reads {st["verdict"]}')
    if W["quiet"] and data.get("since"):
        add("direction", "medium", "find out why nothing moved",
            "not one goal, proposal, run or finding was recorded in this whole window",
            evidence=f'window opened {data["since"]["at"]}')
    for lane in data["lanes"]["lanes"]:
        if not lane["rung"] and not lane["goals"]["total"]:
            add("direction", "low",
                f'decide whether {lane["name"]} is still in scope',
                "no goal has ever been opened here and no claim has ever been "
                "put on the ladder, so it is a lane in name only",
                where=lane["name"], evidence=str(lane.get("path") or ""))

    # --- goal setting. `held` is the direction engine's own account of why
    # nothing can start, which makes it the exact list of what to change.
    for h in (st.get("held") or []):
        why = str(h.get("why") or "")
        n = h.get("n") or 0
        if "nobody has judged" in why:
            add("goals", "high", f"judge worth on {n} objective(s)",
                "they cannot be ranked, so they cannot start, so they sit there",
                evidence=f"{n} unjudged")
        elif "bar you set" in why:
            add("goals", "medium",
                f"{why} - either cut those {n} down or move the bar",
                f"{n} objective(s) are held here. Held is not a decision; it is the "
                "absence of one, and it costs the same as a no while looking like a "
                "maybe.",
                evidence=why)
        else:
            add("goals", "low", f"resolve {n} held objective(s)", why, evidence=why)

    cal = H.get("calibration") or {}
    cost = cal.get("cost") or {}
    if cost.get("n") and cost.get("stated") and cost.get("actual"):
        ratio = float(cost["actual"]) / float(cost["stated"])
        if ratio >= 1.5 or ratio <= 0.67:
            add("goals", "medium", "re-anchor the cost estimates",
                f'estimates said ${cost["stated"]:.2f} and the work cost '
                f'${cost["actual"]:.2f} - off by {ratio:.1f}x over {cost["n"]} '
                "finished objective(s). Sizing decides what gets picked, so a "
                "biased estimate picks the wrong work.",
                evidence=f'{cost["n"]} finished')
    if cal.get("n") and cal.get("stated") is not None and cal.get("actual") is not None:
        gap = float(cal["actual"]) - float(cal["stated"])
        if cal["n"] >= 3 and abs(gap) >= 0.2:
            add("goals", "medium", "re-anchor the odds of finishing",
                f'objectives were given {float(cal["stated"]):.0%} odds and '
                f'{float(cal["actual"]):.0%} of them actually finished, over '
                f'{cal["n"]} of them. The bar is set against a number that is wrong.',
                evidence=f'{cal["n"]} judged')

    # --- questions the architect asked and nobody answered.
    # The question itself is the heading. Titling fourteen of these "answer an open
    # question" makes a list nobody can scan, and knowing WHICH decision is being
    # waited on is the entire value of the item.
    for p in H["questions"]:
        q = " ".join(str(p.get("text") or "").split())
        add("now", "high", f"answer: {q[:150]}" if q else "answer an open question",
            "a worker stopped here and asked. Until it is answered that thread "
            "cannot continue, and the slot behind it stays idle.",
            where=p.get("lane") or "", evidence=q[:400])

    # --- money.
    lim = N.get("day_limit")
    if lim and N["spend_today"] >= float(lim) * 0.8:
        add("watch", "medium", "the day's notional budget is nearly gone",
            f'${N["spend_today"]:.2f} of ${float(lim):.2f} spent today',
            evidence=f'{N["spend_today"] / float(lim):.0%} of the day limit')

    # --- lanes that keep failing.
    for r in data["ratings"]["lanes"]:
        if r["failure_streak"] >= 2:
            add("now", "high", f'stop dispatching into {r["lane"]} until it is fixed',
                f'{r["failure_streak"]} run(s) in a row have failed there',
                where=r["lane"], evidence=f'streak {r["failure_streak"]}')
        elif r["rated"] and r["rating"] == "stalled":
            add("watch", "medium", f'look at what {r["lane"]} is actually producing',
                f'it scores {r["score"]:.2f} across '
                + ", ".join(f"{k} {v:.0%}" for k, v in r["parts"].items()),
                where=r["lane"], evidence=f'{r["runs"]} runs')

    # --- obligations that used to hold and no longer do. Only the ones the
    # observations did not already raise: `observations()` emits an
    # `obligation-drifted` for these, and listing both prints the same problem
    # twice under two different headings, which reads as two problems.
    seen_obligations = {str(o.get("text") or "").split(" is no longer current")[0]
                        for o in data["problems"]
                        if str(o.get("kind") or "").startswith("obligation-")}
    for o in data["obligations"]:
        if o.get("name") in seen_obligations:
            continue
        if o.get("state") in ("drifted", "broken"):
            add("now" if o.get("state") == "broken" else "watch",
                "high" if o.get("state") == "broken" else "medium",
                f'restore the {o.get("name")} obligation',
                f'it is {o.get("state")}: `{str(o.get("check") or "")[:120]}`',
                where=o.get("lane") or "", evidence=str(o.get("state")))

    fs = data["findings_summary"]
    if fs.get("contradicted"):
        add("now", "high", "read the contradicted findings",
            f'{fs["contradicted"]} finding(s) say something believed here is false. '
            "Until they are read, everything built on top is built on the thing "
            "that was just disproved.",
            evidence=f'{fs["unread"]} unread in total')

    items.sort(key=lambda i: (_SEV_RANK.get(i["severity"], 3), i["what"]))
    groups = [{"key": k, "title": t, "why": w,
               "items": [i for i in items if i["group"] == k]}
              for k, t, w in ACTION_GROUPS]
    return {"groups": groups, "total": len(items),
            "high": sum(1 for i in items if i["severity"] == "high")}


READING_SYSTEM = (
    "You are finding OUTSIDE READING for the operator of a software workspace. You are "
    "given what the workspace is for and what it is actually working on right now.\n\n"
    "Search the web and return things worth reading or watching that would genuinely "
    "help with THIS work: talks, papers, documentation, write-ups, videos, tools, prior "
    "art, and people who have already solved the problem being worked on.\n\n"
    "Reply with one JSON object and nothing else:\n"
    "{\n"
    '  "reading": [{"title": "...", "url": "https://...",\n'
    '               "kind": "video|paper|docs|article|tool|talk",\n'
    '               "what": "what it actually is, one sentence",\n'
    '               "why": "what about the work in front of this operator it speaks to",\n'
    '               "lane": "the lane it is relevant to, or the empty string"}],\n'
    '  "nothing_found": true|false,\n'
    '  "why_nothing": "if you found nothing worth sending, say why. Otherwise empty."\n'
    "}\n\n"
    "Rules:\n"
    "- Only URLs you actually retrieved in this search. Every link is fetched and "
    "checked before it is shown, so an invented one does not slip through - it shows up "
    "as a dead link with your name on it.\n"
    "- `why` must connect to something you were told about this workspace. A generic "
    "recommendation that would suit any software project is not worth a line.\n"
    "- At most 8. Fewer good ones beats a padded list.\n"
    "- Returning nothing is a real answer and an honest one."
)


def _reading_context(data: dict) -> str:
    """What the workspace is for and what it is doing, for a search to key on."""
    out = [mission_block("What this workspace is for") or "", ""]
    lanes = data.get("lanes", {}).get("lanes") or []
    if lanes:
        out.append("# The lanes, and how far each one's evidence has got\n")
        for r in lanes:
            out.append(f"- **{r['name']}** ({r.get('path')}): "
                       + (f"evidence has reached `{r['rung']}`" if r.get("rung")
                          else "no claim judged past `spec`")
                       + f", {r['goals']['running']} running, {r['goals']['done']} finished")
        out.append("")
    live = data.get("now", {}).get("goals") or []
    if live:
        out.append("# What is being worked on right now\n")
        for g in live[:12]:
            out.append(f"- [{g.get('lane')}] {str(g.get('objective') or '')[:200]}")
        out.append("")
    props = data.get("headed", {}).get("proposals") or []
    if props:
        out.append("# What is proposed next\n")
        for p in props[:10]:
            out.append(f"- [{p.get('lane')}] {str(p.get('text') or '')[:200]}")
        out.append("")
    probs = data.get("problems") or []
    if probs:
        out.append("# Problems currently standing\n")
        for o in probs[:10]:
            out.append(f"- {str(o.get('text') or '')[:220]}")
    return "\n".join(out).strip()


def check_url(url: str, timeout: int = 8) -> dict:
    """Whether a link actually resolves. The check that makes a citation a fact.

    A HEAD is tried first because it is cheap, and a GET follows when a host
    refuses HEAD - which several large ones do, YouTube among them. Anything
    that is not clearly a success is reported with its status rather than
    dropped, so a link the architect invented is visible as invented.
    """
    if not str(url or "").lower().startswith(("http://", "https://")):
        return {"ok": False, "status": None, "why": "not an http url"}
    req_headers = {"User-Agent": "Mozilla/5.0 (compatible; amp-report/1.0)"}
    for method in ("HEAD", "GET"):
        try:
            req = urllib.request.Request(url, method=method, headers=req_headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                code = int(getattr(r, "status", 0) or 0)
                if code < 400:
                    return {"ok": True, "status": code, "why": ""}
                if method == "GET":
                    return {"ok": False, "status": code, "why": f"HTTP {code}"}
        except urllib.error.HTTPError as e:
            if e.code in (403, 405, 501) and method == "HEAD":
                continue                       # host refuses HEAD; try a GET
            return {"ok": False, "status": e.code, "why": f"HTTP {e.code}"}
        except Exception as e:                 # DNS, TLS, timeout, bad url
            if method == "GET":
                return {"ok": False, "status": None, "why": str(e)[:120]}
    return {"ok": False, "status": None, "why": "no response"}


def report_reading(data: dict) -> dict:
    """Outside reading for what this workspace is doing. Every link checked.

    Off by default and never on a timer: it is a web search and an architect
    turn, and a report that quietly spends one every time it is taken would be
    spending on the operator's behalf without being asked.
    """
    if not web_search_backend():
        return {"available": False, "items": [],
                "why": f"the architect is set to {architect_backend()}, which cannot search "
                       f"the web from here. Set it to codex in Settings and this section "
                       f"fills in."}
    ok, why = role_ready("architect")
    if not ok:
        return {"available": False, "items": [], "why": why}
    try:
        res = architect_chat(
            [{"role": "system", "content": READING_SYSTEM},
             {"role": "user", "content": _reading_context(data)}],
            role_model("architect"), 4000, web=True)
    except Exception as e:
        return {"available": False, "items": [], "why": f"the search failed: {str(e)[:200]}"}
    text = ((res.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    out = _json_reply(text)
    if out is None:
        # A reply that did not parse is not the same answer as "I found
        # nothing", and reporting it as one would quietly turn a broken round
        # into a finding about the world.
        return {"available": False, "items": [],
                "why": "the architect answered, but not in the shape asked for, so "
                       "nothing here can be trusted as a citation"}
    items = []
    for it in (out.get("reading") or [])[:8]:
        url = str(it.get("url") or "").strip()
        chk = check_url(url)
        items.append({
            "title": str(it.get("title") or url)[:200], "url": url,
            "kind": str(it.get("kind") or "")[:20],
            "what": str(it.get("what") or "")[:400],
            "why": str(it.get("why") or "")[:400],
            "lane": str(it.get("lane") or "")[:60],
            "reachable": chk["ok"], "status": chk["status"], "check_why": chk["why"],
        })
    return {"available": True, "items": items,
            "checked": len(items), "dead": sum(1 for i in items if not i["reachable"]),
            "nothing_found": bool(out.get("nothing_found")) or not items,
            "why_nothing": str(out.get("why_nothing") or "")[:400],
            "tokens": (res.get("usage") or {}).get("total_tokens"), "at": now()}


def report_data(*, web: bool = False) -> dict:
    """The whole workspace at one moment, against the last report."""
    prev = last_report()
    since = (prev or {}).get("at")
    data = {
        "id": "r" + uuid.uuid4().hex[:10], "at": now(),
        "workspace": {"slug": current_workspace(),
                      "name": (workspace() or {}).get("name") or current_workspace()},
        "mission": mission(),
        "doctrine": doctrine_state(), "doctrine_path": str(DOCTRINE_PATH),
        "since": {"id": prev.get("id"), "at": prev.get("at")} if prev else None,
        "nth": len(reports()) + 1,
        "window": report_window(since),
        "now": report_now(),
        "headed": report_headed(),
        "lanes": report_lanes(),
        "problems": observations(),
        "findings_summary": findings_summary(),
        # Leads workers left behind while doing something else. They are in the
        # report because this is the one place a season of them is read at once.
        "ideas": ideas()[:40],
        "obligations": obligations(),
        "obligations_summary": obligations_summary(),
        "history": report_history(),
        "ratings": report_ratings(),
        "root": str(ROOT),
    }
    # Actions are derived LAST, from everything above: an action item is a
    # reading of the records on this page, so it cannot be built before them.
    data["actions"] = report_actions(data)
    data["reading"] = (report_reading(data) if web else
                       {"available": False, "items": [],
                        "why": "this report was taken without a web search"})
    return data


# ------------------------------------------------------------ rendering it
#
# One file, no network, no build step. A report that needs a server to be read
# is not a report you can keep, mail, or open in a year - and this one has to
# survive the console it came from.

REPORT_CSS = """
:root{--bg:#0b0d10;--panel:#12151a;--line:#242a33;--fg:#dfe4ea;--muted:#8b94a3;
--accent:#7dd3fc;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--chip:#1b2029}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.65 ui-sans-serif,
system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--accent)}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12.5px}
.wrap{max-width:1080px;margin:0 auto;padding:0 20px 80px}
header.top{border-bottom:1px solid var(--line);background:linear-gradient(180deg,#12151a,#0b0d10);
padding:28px 0 20px;margin-bottom:0}
header.top .wrap{padding-bottom:0}
h1{margin:0 0 4px;font-size:22px;letter-spacing:.2px}
h2{font-size:16px;margin:0 0 2px}
h3{font-size:13px;margin:18px 0 6px;color:var(--muted);text-transform:uppercase;
letter-spacing:.6px;font-weight:600}
p{margin:6px 0}
.muted{color:var(--muted)}
.sub{color:var(--muted);font-size:13px}
.verdict{margin:14px 0 0;padding:12px 14px;border:1px solid var(--line);border-radius:6px;
background:var(--panel);border-left:3px solid var(--muted)}
.verdict.ok{border-left-color:var(--ok)} .verdict.warn{border-left-color:var(--warn)}
.verdict.bad{border-left-color:var(--bad)}
.verdict b{font-size:15px}
nav.jump{position:sticky;top:0;z-index:5;background:rgba(11,13,16,.94);
backdrop-filter:blur(6px);border-bottom:1px solid var(--line);padding:8px 0;margin-bottom:6px}
nav.jump .wrap{padding-top:0;padding-bottom:0;display:flex;gap:6px;flex-wrap:wrap;align-items:center}
nav.jump a{padding:4px 9px;border:1px solid var(--line);border-radius:99px;font-size:12px;
text-decoration:none;color:var(--fg);white-space:nowrap}
nav.jump a:hover{border-color:var(--accent);color:var(--accent)}
nav.jump a .n{color:var(--muted);margin-left:5px}
.tools{display:flex;gap:8px;align-items:center;margin-left:auto}
input[type=search]{background:var(--chip);border:1px solid var(--line);color:var(--fg);
border-radius:5px;padding:5px 9px;font:inherit;font-size:12.5px;width:210px}
button{background:var(--chip);border:1px solid var(--line);color:var(--fg);border-radius:5px;
padding:5px 10px;font:inherit;font-size:12.5px;cursor:pointer}
button:hover{border-color:var(--accent);color:var(--accent)}
section{margin:26px 0 0;scroll-margin-top:54px}
section>h2{display:flex;align-items:baseline;gap:10px}
section>h2 .count{color:var(--muted);font-size:13px;font-weight:400}
.card{border:1px solid var(--line);background:var(--panel);border-radius:6px;
padding:11px 13px;margin:8px 0}
.card.hi{border-left:3px solid var(--bad)} .card.mid{border-left:3px solid var(--warn)}
.card.good{border-left:3px solid var(--ok)}
.card .hd{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap}
.card .hd .t{font-weight:600}
.card .why{color:var(--muted);margin-top:4px}
.chip{display:inline-block;background:var(--chip);border:1px solid var(--line);
border-radius:99px;padding:1px 8px;font-size:11.5px;color:var(--muted);white-space:nowrap}
.chip.lane{color:var(--accent);border-color:#274b5c}
.chip.ok{color:var(--ok)} .chip.warn{color:var(--warn)} .chip.bad{color:var(--bad)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:10px;margin:10px 0}
.stat{border:1px solid var(--line);background:var(--panel);border-radius:6px;padding:10px 12px}
.stat .v{font-size:22px;font-weight:600;line-height:1.2}
.stat .k{color:var(--muted);font-size:12px}
.stat.bad .v{color:var(--bad)} .stat.warn .v{color:var(--warn)} .stat.ok .v{color:var(--ok)}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}
th,td{text-align:left;padding:6px 9px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.4px;
cursor:pointer;user-select:none}
th:hover{color:var(--accent)}
tbody tr:hover{background:#161a20}
.bar{height:5px;background:var(--chip);border-radius:3px;overflow:hidden;min-width:70px}
.bar i{display:block;height:100%;background:var(--accent)}
details{border:1px solid var(--line);border-radius:6px;background:var(--panel);margin:8px 0}
details>summary{cursor:pointer;padding:9px 13px;font-weight:600;list-style:none;
display:flex;gap:8px;align-items:baseline}
details>summary::-webkit-details-marker{display:none}
details>summary:before{content:"\\25B8";color:var(--muted);display:inline-block;
transition:transform .12s}
details[open]>summary:before{transform:rotate(90deg)}
details>.body{padding:0 13px 11px;border-top:1px solid var(--line);margin-top:0}
.rungs{display:flex;gap:3px;align-items:center;flex-wrap:wrap}
.rung{padding:1px 7px;border-radius:99px;font-size:11px;border:1px solid var(--line);
color:var(--muted)}
.rung.on{background:#173a2a;color:var(--ok);border-color:#245c40}
.empty{color:var(--muted);font-style:italic;padding:6px 0}
.ladder-move{font-family:ui-monospace,monospace;font-size:12px}
.ladder-move .to{color:var(--ok)}
.hidden{display:none!important}
.chart{margin:10px 0 14px}
.chart svg{display:block;width:100%;height:auto;overflow:visible}
.chart .cap{display:flex;gap:14px;flex-wrap:wrap;align-items:baseline;color:var(--muted);
font-size:12px;margin-bottom:5px}
.legend{display:flex;gap:11px;flex-wrap:wrap;font-size:11.5px;color:var(--muted)}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:5px}
.axis{display:flex;justify-content:space-between;gap:10px;color:var(--muted);font-size:11px;
margin-top:3px}
.split{display:flex;height:16px;border-radius:4px;overflow:hidden;border:1px solid var(--line);
margin-top:6px}
.split span{display:block}
.means{border-left:2px solid var(--line);padding:3px 0 3px 11px;margin:9px 0;
color:var(--muted);font-size:13px}
.means b{color:var(--fg);font-weight:600}
.score{font-weight:600}
.score.ok{color:var(--ok)} .score.warn{color:var(--warn)} .score.bad{color:var(--bad)}
.act{border:1px solid var(--line);background:var(--panel);border-radius:6px;padding:11px 13px;
margin:8px 0;border-left:3px solid var(--muted)}
.act.high{border-left-color:var(--bad)} .act.medium{border-left-color:var(--warn)}
.act.low{border-left-color:var(--ok)}
.act .t{font-weight:600}
.act .ev{color:var(--muted);font-size:12px;margin-top:5px}
footer{margin-top:44px;padding-top:14px;border-top:1px solid var(--line);color:var(--muted);
font-size:12px}
pre.raw{white-space:pre-wrap;word-break:break-word;background:#0e1116;border:1px solid var(--line);
border-radius:6px;padding:12px;max-height:420px;overflow:auto;font-size:11.5px}
.dead{color:var(--bad);text-decoration:line-through}
@media print{nav.jump{display:none}details{break-inside:avoid}details>.body{display:block!important}
body{background:#fff;color:#111}.card,.stat,details,pre.raw{background:#fff}}
"""

REPORT_JS = """
// Filter: hides any card, row or details block that does not match. Sections
// with nothing left are hidden too, so filtering never leaves a bare heading
// claiming a count it is no longer showing.
const q = document.getElementById('q');
function applyFilter(){
  const t = (q.value || '').trim().toLowerCase();
  document.querySelectorAll('[data-f]').forEach(el => {
    el.classList.toggle('hidden', !!t && !el.dataset.f.includes(t));
  });
  document.querySelectorAll('section').forEach(s => {
    const items = s.querySelectorAll('[data-f]');
    const shown = [...items].filter(el => !el.classList.contains('hidden')).length;
    s.classList.toggle('hidden', !!t && items.length > 0 && shown === 0);
    const c = s.querySelector('h2 .count');
    if (c && c.dataset.total) c.textContent = t ? shown + ' of ' + c.dataset.total : c.dataset.total;
  });
  document.querySelectorAll('nav.jump a').forEach(a => {
    const s = document.querySelector(a.getAttribute('href'));
    a.classList.toggle('hidden', !!(s && s.classList.contains('hidden')));
  });
}
q.addEventListener('input', applyFilter);
document.getElementById('expand').onclick = () => {
  const any = [...document.querySelectorAll('details')].some(d => !d.open);
  document.querySelectorAll('details').forEach(d => d.open = any);
  document.getElementById('expand').textContent = any ? 'Collapse all' : 'Expand all';
};
// Sortable tables. Numeric when the whole column parses as a number, so a cost
// column sorts as money rather than as text beginning with a dollar sign.
// A cell states its sort key with data-v, on itself or on the chip inside it -
// which is what lets a "when" column sort by instant instead of by wording.
document.querySelectorAll('table').forEach(tb => {
  tb.querySelectorAll('th').forEach((th, i) => th.onclick = () => {
    const body = tb.tBodies[0], rows = [...body.rows];
    const val = r => {
      const c = r.cells[i];
      if (!c) return '';
      const k = c.dataset.v ?? c.querySelector('[data-v]')?.dataset.v;
      return (k ?? c.textContent ?? '').trim();
    };
    const num = rows.every(r => val(r) === '' || !isNaN(parseFloat(val(r))));
    const dir = th.dataset.dir === 'asc' ? -1 : 1;
    tb.querySelectorAll('th').forEach(o => delete o.dataset.dir);
    th.dataset.dir = dir === 1 ? 'asc' : 'desc';
    rows.sort((a, b) => dir * (num ? (parseFloat(val(a)) || 0) - (parseFloat(val(b)) || 0)
                                   : val(a).localeCompare(val(b))));
    rows.forEach(r => body.appendChild(r));
  });
});
document.getElementById('copyjson').onclick = async (e) => {
  await navigator.clipboard.writeText(document.getElementById('rawjson').textContent);
  e.target.textContent = 'Copied';
  setTimeout(() => e.target.textContent = 'Copy JSON', 1200);
};
"""


def _epoch(iso: str | None) -> int:
    """A timestamp as a sortable number. 0 when there is nothing to read."""
    try:
        return int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
    except (ValueError, TypeError):
        return 0


def _rel(iso: str | None, at: str | None = None) -> str:
    """How long ago, in words. Empty when there is no timestamp to speak of."""
    if not iso:
        return ""
    try:
        a = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        b = (datetime.fromisoformat(str(at).replace("Z", "+00:00")) if at
             else datetime.now(timezone.utc))
        s = max(0, int((b - a).total_seconds()))
    except (ValueError, TypeError):
        return ""
    for n, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if s >= n:
            return f"{s // n}{unit} ago"
    return "just now"


# ------------------------------------------------------------ charts
#
# Inline SVG, drawn in Python from the same numbers the tables print. No
# library and no network: a chart that pulls a script off a CDN is a blank box
# the first time the file is opened offline, and these have to still draw in a
# year on a machine that has never heard of this console.

C_OK, C_WARN, C_BAD, C_ACCENT = "#4ade80", "#fbbf24", "#f87171", "#7dd3fc"
CHART_W, CHART_H = 960, 140


def _tick(v: float) -> str:
    """A number as a person would write it. 3, not 3.0; 3.25, not 3.250000001.

    Not `_num`. That name is already the score reader six thousand lines up, and
    a second one down here silently replaced it for the whole module: every
    architect score came back unreadable, so `_scored` raised and nothing was
    ever scored again. The pipeline stopped, and the only visible symptom was
    proposals that never moved.
    """
    return f"{v:.2f}".rstrip("0").rstrip(".") if v % 1 else str(int(v))


def _svg(body: str, h: int = CHART_H) -> str:
    return f'<svg viewBox="0 0 {CHART_W} {h}" role="img">{body}</svg>'


def _legend(keys) -> str:
    return ('<span class="legend">' + "".join(
        f'<span><i style="background:{c}"></i>{html.escape(lb)}</span>' for _k, c, lb in keys)
        + "</span>")


def _axis(rows: list[dict]) -> str:
    """First day, middle day, last day. Enough to place the shape in time."""
    if not rows:
        return ""
    return ('<div class="axis">'
            + "".join(f'<span>{html.escape(rows[i]["day"])}</span>'
                      for i in (0, len(rows) // 2, -1))
            + "</div>")


def _chart_columns(rows: list[dict], keys, *, empty_msg: str, unit: str = "") -> str:
    """One stacked column per day; the tallest column sets the scale.

    The scale is printed in words rather than drawn as an axis, because at this
    size an axis is three unreadable labels and the only number that matters is
    how big it ever got.
    """
    top = max((sum(float(r.get(k) or 0) for k, _c, _l in keys) for r in rows), default=0)
    if not rows or top <= 0:
        return (f'<div class="chart"><div class="cap">{_legend(keys)}</div>'
                f'<div class="empty">{html.escape(empty_msg)}</div></div>')
    w = CHART_W / len(rows)
    bw = max(2.0, w * 0.66)
    body = [f'<line x1="0" y1="{CHART_H}" x2="{CHART_W}" y2="{CHART_H}" stroke="#242a33"/>']
    for i, r in enumerate(rows):
        x, y = i * w + (w - bw) / 2, float(CHART_H)
        for k, colour, label in keys:
            v = float(r.get(k) or 0)
            if v <= 0:
                continue
            h = v / top * (CHART_H - 8)
            y -= h
            body.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{bw:.1f}" height="{h:.1f}" '
                        f'fill="{colour}" rx="1"><title>{html.escape(r["day"])}: '
                        f'{unit}{_tick(v)} {html.escape(label)}</title></rect>')
    return (f'<div class="chart"><div class="cap">{_legend(keys)}'
            f'<span>tallest day: {unit}{_tick(top)}</span></div>'
            + _svg("".join(body)) + _axis(rows) + "</div>")


def _chart_grouped(rows: list[dict], keys, *, empty_msg: str) -> str:
    """Side-by-side columns per day, scaled by the largest single value.

    Not stacked, deliberately. Stacking is for series that partition a whole -
    a run is completed or failed or killed, and the three add up to the runs.
    Objectives opened and objectives closed do not add up to anything, and a
    stack of them would print a peak that is neither number.
    """
    top = max((float(r.get(k) or 0) for r in rows for k, _c, _l in keys), default=0)
    if not rows or top <= 0:
        return (f'<div class="chart"><div class="cap">{_legend(keys)}</div>'
                f'<div class="empty">{html.escape(empty_msg)}</div></div>')
    w = CHART_W / len(rows)
    bw = max(1.5, (w * 0.72) / len(keys))
    body = [f'<line x1="0" y1="{CHART_H}" x2="{CHART_W}" y2="{CHART_H}" stroke="#242a33"/>']
    for i, r in enumerate(rows):
        x0 = i * w + (w - bw * len(keys)) / 2
        for j, (k, colour, label) in enumerate(keys):
            v = float(r.get(k) or 0)
            if v <= 0:
                continue
            h = v / top * (CHART_H - 8)
            body.append(f'<rect x="{x0 + j * bw:.1f}" y="{CHART_H - h:.1f}" '
                        f'width="{bw:.1f}" height="{h:.1f}" fill="{colour}" rx="1">'
                        f'<title>{html.escape(r["day"])}: {_tick(v)} '
                        f'{html.escape(label)}</title></rect>')
    return (f'<div class="chart"><div class="cap">{_legend(keys)}'
            f'<span>biggest single day: {_tick(top)}</span></div>'
            + _svg("".join(body)) + _axis(rows) + "</div>")


def _chart_line(rows: list[dict], key: str, colour: str, *,
                label: str, unit: str = "") -> str:
    """A running total, drawn as a filled line.

    Cumulative on purpose. A daily bar says which days were busy; a running
    total says what the span has come to, and only one of those is a trend.
    """
    run, acc = [], 0.0
    for r in rows:
        acc += float(r.get(key) or 0)
        run.append(acc)
    top = max(run, default=0)
    if top <= 0:
        return ""
    step = CHART_W / max(1, len(run) - 1)
    pts = [(i * step, CHART_H - (v / top) * (CHART_H - 8)) for i, v in enumerate(run)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.6" fill="{colour}"><title>'
        f'{html.escape(rows[i]["day"])}: {unit}{_tick(run[i])} {html.escape(label)}'
        f'</title></circle>'
        for i, (x, y) in enumerate(pts) if float(rows[i].get(key) or 0) > 0)
    return (f'<div class="chart"><div class="cap"><span>{html.escape(label)}</span>'
            f'<span>{unit}{_tick(top)} by {html.escape(rows[-1]["day"])}</span></div>'
            + _svg(f'<polygon points="0,{CHART_H} {line} {CHART_W},{CHART_H}" '
                   f'fill="{colour}" opacity=".13"/>'
                   f'<polyline points="{line}" fill="none" stroke="{colour}" '
                   f'stroke-width="2"/>{dots}')
            + _axis(rows) + "</div>")


def _chart_split(parts, total: int) -> str:
    """One bar showing how a whole divides. Shares, since the counts are printed."""
    if total <= 0:
        return ""
    e = html.escape
    return ('<div class="split">' + "".join(
        f'<span style="width:{100 * n / total:.2f}%;background:{c}" '
        f'title="{e(lb)}: {n} of {total}"></span>' for n, c, lb in parts if n > 0)
        + '</div><div class="axis"><span>'
        + " &middot; ".join(f"{e(lb)} {n}/{total} ({n / total:.0%})"
                            for n, _c, lb in parts if n > 0)
        + "</span></div>")


def render_report(data: dict) -> str:
    """The whole report as one standalone page."""
    e = html.escape
    W, N, H = data["window"], data["now"], data["headed"]
    at = data["at"]
    out: list[str] = []
    add = out.append

    def sec(sid: str, title: str, total=None, note: str = ""):
        c = ("" if total is None else
             f'<span class="count" data-total="{total}">{total}</span>')
        add(f'<section id="{sid}"><h2>{e(title)} {c}</h2>')
        if note:
            add(f'<p class="sub">{note}</p>')

    def empty(msg: str):
        add(f'<div class="empty">{e(msg)}</div>')

    def means(txt: str):
        # What the numbers just above actually say, in a sentence. Written from
        # the numbers, so it cannot drift away from them the way a hand-written
        # caption does the first time the data changes.
        add(f'<div class="means">{txt}</div>')

    def chips(*pairs) -> str:
        return "".join(f'<span class="chip {cls}">{e(str(txt))}</span>'
                       for txt, cls in pairs if txt not in (None, "", 0))

    def card(title: str, body: str = "", *, cls: str = "", meta: str = "", f: str = ""):
        key = e((f or title + " " + body + " " + meta).lower())
        add(f'<div class="card {cls}" data-f="{key}">'
            f'<div class="hd"><span class="t">{title}</span>{meta}</div>'
            + (f'<div class="why">{body}</div>' if body else "") + "</div>")

    def when(iso, label: str = "") -> str:
        # `data-v` carries the instant as a number, so a "when" column sorts by
        # time rather than by the spelling of "10m ago", which lands before
        # "2h ago" alphabetically.
        r = _rel(iso, at)
        return (f'<span class="chip" data-v="{_epoch(iso)}" title="{e(str(iso or ""))}">'
                f'{e(label + r if r else str(iso or ""))}</span>') if iso else ""

    # ---- head
    ws = data["workspace"]
    since = data.get("since")
    cover = (f'everything since report #{data["nth"] - 1}, taken {e(since["at"])} '
             f'({_rel(since["at"], at)})' if since else
             "everything on record - this is the first report for this workspace")
    add(f'<header class="top"><div class="wrap">'
        f'<h1>{e(ws["name"])} &mdash; report #{data["nth"]}</h1>'
        f'<div class="sub">Taken {e(at)}. Covers {cover}.</div>')

    # The verdict line. Two facts decide it and they are different facts: whether
    # anything moved, and whether anything can move next.
    gates = H.get("gates") or []
    st = H.get("state") or {}
    if W["quiet"]:
        cls, head = "warn", "Nothing has moved since the last report."
    elif gates:
        cls, head = "warn", f"Work landed, and {len(gates)} gate(s) are holding the fleet."
    elif st.get("verdict") == "nowhere":
        cls, head = "bad", "Work landed, and there is nowhere left to go."
    else:
        cls, head = "ok", "Work landed and there is somewhere to go next."
    add(f'<div class="verdict {cls}"><b>{e(head)}</b><div class="sub">'
        f'{e(str(st.get("headline") or ""))}</div></div>')
    if data["mission"]:
        add('<details style="margin-top:12px"><summary>The mission this is measured against</summary>'
            f'<div class="body"><p>{e(data["mission"]).replace(chr(10), "<br>")}</p></div></details>')
    else:
        add('<div class="verdict bad" style="margin-top:12px"><b>This workspace has no mission '
            'set.</b><div class="sub">Nothing below can be judged as on it or off it.</div></div>')
    add("</div></header>")

    # ---- nav
    nav = [("happened", "Since last report"), ("now", "Right now"),
           ("headed", "Where it is headed"), ("lanes", "Lanes"),
           ("history", "Over time"), ("ratings", "Scorecard"),
           ("problems", "Problems"), ("findings", "Findings"),
           ("ideas", "Noticed in passing"),
           ("reading", "Reading"), ("actions", "What to do"), ("raw", "Raw")]
    add('<nav class="jump"><div class="wrap">'
        + "".join(f'<a href="#{i}">{e(t)}</a>' for i, t in nav)
        + '<span class="tools"><input type="search" id="q" placeholder="filter everything">'
          '<button id="expand">Expand all</button></span></div></nav>')
    add('<div class="wrap">')

    # ---- since the last report
    sec("happened", "Since the last report", None,
        "Counted from the records, not summarised by a model.")
    add('<div class="grid">'
        + _stat(len(W["goals_closed"]), "goals closed")
        + _stat(len(W["ladder"]), "rung moves", "ok" if W["ladder"] else "")
        + _stat(len(W["findings"]), "findings")
        + _stat(len(W["tasks"]), "worker runs")
        + _stat(f'${W["spend_usd"]:.2f}', "notional spend")
        + _stat(len(W["raised"]), "objectives proposed")
        + "</div>")
    if W["quiet"]:
        empty("Nothing at all moved in this window. No goal closed, nothing was "
              "proposed, no worker ran and nothing was reported back.")
    else:
        means(
            (f'<b>{len(W["ladder"])} claim(s) moved up the evidence ladder</b>, '
             'which is the one number here that means something got harder to argue '
             'with. ' if W["ladder"] else
             '<b>No claim moved up the evidence ladder</b> in this window, so whatever '
             'else happened, nothing got more provable. ')
            + (f'{len(W["tasks"])} worker run(s) cost ${W["spend_usd"]:.2f} notional. '
               if W["tasks"] else "No worker ran. ")
            + (f'{len(W["goals_closed"])} objective(s) settled and '
               f'{len(W["raised"])} new one(s) were proposed.'
               if W["goals_closed"] or W["raised"] else
               "No objective settled and none was proposed."))

    if W["ladder"]:
        add("<h3>Claims that moved up the ladder</h3>")
        add(_table(["when", "lane", "claim", "move"],
                   [[when(x["at"]), _lane(x["lane"]),
                     e(x["claim"]),
                     f'<span class="ladder-move">{e(str(x["from"] or "?"))} &rarr; '
                     f'<span class="to">{e(str(x["to"] or "?"))}</span></span>']
                    for x in W["ladder"]]))
    if W["goals_closed"]:
        add("<h3>Goals that closed</h3>")
        for g in W["goals_closed"][:40]:
            done = g.get("state") == "done"
            card(e(str(g.get("objective") or "")[:220]),
                 e(str(g.get("stopped_why") or g.get("stopped_on") or "")),
                 cls="good" if done else "mid",
                 meta=_lane(g.get("lane"))
                      + chips((g.get("state"), "ok" if done else "warn"),
                              (f'{g.get("met")}/{g.get("dod")} done-conditions', ""),
                              (f'{g.get("rounds")} rounds', ""))
                      + when(g.get("updated_at")))
    if W["reviews"]:
        add("<h3>Direction reviews</h3>")
        for r in W["reviews"][:20]:
            card(e((r.get("kind") or "review").title()
                   + (f' - {r.get("lane")}' if r.get("lane") else "")),
                 e(str(r.get("assessment") or "")[:600]),
                 meta=chips((r.get("kind"), ""), ("web", "ok") if r.get("web") else ("", ""))
                      + when(r.get("at")))
    if W["adopted"] or W["declined"]:
        add("<h3>Proposals decided</h3>")
        for p in (W["adopted"] + W["declined"])[:30]:
            adopted = p.get("state") == "adopted"
            card(e(str(p.get("text") or "")[:220]), "",
                 cls="good" if adopted else "",
                 meta=_lane(p.get("lane"))
                      + chips((p.get("state"), "ok" if adopted else "warn"))
                      + when(p.get("decided_at")))
    if W["tasks"]:
        add("<h3>Worker runs</h3>")
        add(_table(["when", "lane", "status", "cost", "what it was sent"],
                   [[when(t["at"]), _lane(t["lane"]),
                     chips((t["status"], "ok" if t["status"] == "completed"
                            else "bad" if t["killed"] else "warn")),
                     f'<span data-v="{t["cost_usd"]}">${t["cost_usd"]:.2f}</span>',
                     e(t["prompt"]) + (f'<div class="muted">{e(t["error"])}</div>'
                                       if t["error"] else "")]
                    for t in W["tasks"][:60]]))
    add("</section>")

    # ---- right now
    sec("now", "Right now", None, "What is in flight at the moment this was taken.")
    cap = N["worker_cap"] or 0
    add('<div class="grid">'
        + _stat(f'{N["live"]}/{cap}', "workers running",
                "warn" if cap and N["live"] >= cap else "ok" if N["live"] else "")
        + _stat(len(N["goals"]), "goals live")
        + _stat(len(N["blocked"]), "goals stopped", "bad" if N["blocked"] else "")
        + _stat(f'${N["spend_today"]:.2f}', "notional today",
                "bad" if N["day_limit"] and N["spend_today"] >= float(N["day_limit"]) else "")
        + "</div>")
    means(
        (f'<b>{N["live"]} of {cap} worker slot(s) are busy.</b> ' if N["live"] else
         f'<b>Nothing is running</b>, and there are {cap} worker slot(s) free. ')
        + (f'{len(N["blocked"])} objective(s) are stopped waiting on a person - '
           'those will not clear themselves. ' if N["blocked"] else "")
        + (f'Today has cost ${N["spend_today"]:.2f} of a ${float(N["day_limit"]):.2f} '
           'notional day limit.' if N["day_limit"] else
           f'Today has cost ${N["spend_today"]:.2f}, against no day limit.'))
    if N["workers"]:
        add("<h3>Workers on the machine</h3>")
        add(_table(["lane", "for", "cpu", "mem", "doing"],
                   [[_lane(w["lane"]),
                     f'<span data-v="{w["elapsed_s"] or 0}">{_dur(w["elapsed_s"])}</span>',
                     f'{w["cpu_pct"] or 0}%', f'{w["rss_gb"] or 0} GB',
                     e(str(w["doing"] or w["prompt"])[:160])
                     + (' <span class="chip bad">stalled</span>' if w["stalled"] else "")
                     + (' <span class="chip warn">adopted</span>' if w["adopted"] else "")]
                    for w in N["workers"]]))
    else:
        empty("No worker is running.")
    if N["goals"]:
        add("<h3>Goals in flight</h3>")
        for g in N["goals"]:
            pct_done = int(100 * (g.get("tasks_done") or 0) / max(1, g.get("tasks_total") or 1))
            card(e(str(g.get("objective") or "")[:220]),
                 e(str(g.get("now") or "")),
                 meta=_lane(g.get("lane"))
                      + chips((g.get("state"), "ok"),
                              (f'{g.get("tasks_done")}/{g.get("tasks_total")} steps', ""))
                      + f'<span class="bar" style="width:80px"><i style="width:{pct_done}%"></i></span>')
    if N["blocked"]:
        add("<h3>Goals stopped, waiting on someone</h3>")
        for g in N["blocked"]:
            card(e(str(g.get("objective") or "")[:220]),
                 e("; ".join(g.get("questions") or [])[:400]
                   or str(g.get("stopped_why") or g.get("stopped_on") or "")),
                 cls="hi", meta=_lane(g.get("lane")) + chips((g.get("stopped_on"), "bad")))
    add("</section>")

    # ---- where it is headed
    sec("headed", "Where it is headed", None,
        "The direction, what is waiting to start, and what is stopping it.")
    add(f'<div class="verdict {"bad" if st.get("verdict") in ("nowhere",) else "warn" if st.get("verdict") in ("held", "empty", "unexplored") else "ok"}">'
        f'<b>{e(str(st.get("verdict") or "unknown"))}</b>'
        f'<div class="sub">{e(str(st.get("headline") or ""))}</div></div>')
    means(
        (f'<b>{len(gates)} gate(s) are holding the fleet.</b> A gate is a limit the '
         'harness was told to stop at, so no amount of dispatching will get past one. '
         if gates else "<b>No gate is holding the fleet.</b> ")
        + (f'{st.get("ready") or 0} objective(s) could start now; '
           f'{sum(h.get("n") or 0 for h in (st.get("held") or []))} are held back. '
           if (st.get("ready") or st.get("held")) else "Nothing is waiting to start. ")
        + (f'{len(H["questions"])} question(s) are open and nobody has answered them.'
           if H["questions"] else "No question is waiting on an answer."))
    if gates:
        add("<h3>Gates holding the fleet</h3>")
        for g in gates:
            card(e(str(g.get("gate"))), e(str(g.get("why") or "")), cls="hi",
                 meta=chips((f'at {g.get("at")}', "bad"), (f'limit {g.get("limit")}', "")))
    if H["proposals"]:
        add("<h3>Objectives waiting</h3>")
        for p in H["proposals"][:30]:
            hold = p.get("hold")
            card(e(str(p.get("text") or "")[:260]),
                 e(str(hold or "")) or e(str(p.get("why") or "")[:300]),
                 cls="mid" if hold else "good",
                 meta=_lane(p.get("lane"))
                      + chips((f'odds {pct(p.get("confidence"))}', ""),
                              (f'need {pct(p.get("need"))}', ""),
                              (f'cost {usd(p.get("cost_usd"))}', ""),
                              (f'worth {p.get("worth"):.2f}' if p.get("worth") is not None else "", ""),
                              ("waiting" if hold else "ready", "warn" if hold else "ok")))
    else:
        empty("Nothing is waiting to start.")
    if H["questions"]:
        add("<h3>Questions nobody has settled</h3>")
        for p in H["questions"][:20]:
            card(e(str(p.get("text") or "")[:300]), "", meta=_lane(p.get("lane")))
    sup = H.get("supervisor")
    if sup:
        add("<h3>The supervisor's last reading</h3>")
        card(e(str(sup.get("verdict") or "")),
             e(str(sup.get("assessment") or sup.get("summary") or "")[:800]),
             cls="hi" if sup.get("verdict") == "off_mission" else
                 "mid" if sup.get("verdict") == "drifting" else "good",
             meta=when(sup.get("at")))
    add("</section>")

    # ---- lanes
    lanes = data["lanes"]["lanes"]
    sec("lanes", "Lanes", len(lanes),
        "How far each lane's evidence has actually got, and how its goals tend to end.")
    on_ladder = [r for r in lanes if r.get("rung")]
    deployed = [r for r in on_ladder if _reached(r["rung"], "live_deployed")]
    means(
        f'<b>{len(on_ladder)} of {len(lanes)} lane(s) have any claim on the evidence '
        f'ladder at all</b>, and {len(deployed)} have got as far as running somewhere '
        'real. The ladder runs '
        + " &rarr; ".join(e(x) for x in H["ladder"])
        + ' - a lane sitting at the left end is a lane whose claims are still only '
          'written down.')
    for r in lanes:
        rec = r["record"]
        if rec["n"]:
            stops = ", ".join(f"{v} on {k}" for k, v in sorted(rec["stops"].items())) or "none"
            record = (f'Of its last {rec["n"]} settled goals, {rec["done"]} finished. '
                      f'The rest stopped: {e(stops)}.')
        else:
            record = "No goal here has settled yet, so this lane has no record to judge by."
        rungs = "".join(
            f'<span class="rung{" on" if _reached(r.get("rung"), x) else ""}">{e(x)}</span>'
            for x in H["ladder"])
        add(f'<div class="card" data-f="{e((r["name"] + " " + str(r.get("path") or "")).lower())}">'
            f'<div class="hd"><span class="t">{e(r["name"])}</span>'
            f'<span class="chip mono">{e(str(r.get("path") or ""))}</span>'
            + chips((r.get("backend"), ""),
                    (f'{r["goals"]["running"]} running', "ok" if r["goals"]["running"] else ""),
                    (f'{r["goals"]["blocked"]} stopped', "bad" if r["goals"]["blocked"] else ""))
            + f'</div><div class="rungs" style="margin-top:7px">{rungs}</div>'
            + f'<div class="why">{record}</div></div>')
    add("</section>")

    # ---- over time
    hist = data["history"]
    S, T = hist["series"], hist["totals"]
    O = hist["outcomes"]
    sec("history", "Over time", None,
        f'Every day from {e(hist["from"])} to {e(hist["to"])}, from the same records. '
        f'Deliberately wider than the report window: a trend drawn from one window '
        f'is a single point pretending to be a line.')
    add('<div class="grid">'
        + _stat(T["runs"], f'worker runs in {hist["days"]} days')
        + _stat(f'${T["spend"]:.2f}', "notional spend in that span")
        + _stat(T["opened"], "objectives opened")
        + _stat(T["closed"], "objectives closed",
                "bad" if T["opened"] and not T["closed"] else
                "warn" if T["closed"] < T["opened"] else "ok")
        + _stat(T["rungs"], "rung moves", "ok" if T["rungs"] else "bad")
        + _stat(T["findings"], "findings reported")
        + "</div>")
    busy = sum(1 for r in S if r["runs"] or r["opened"] or r["closed"] or r["findings"])
    drift = T["opened"] - T["closed"]
    means(
        f'Something was recorded on <b>{busy} of the last {hist["days"]} days</b>. '
        + (f'The backlog <b>grew by {drift}</b>: more objectives were opened than closed. '
           if drift > 0 else
           f'The backlog <b>shrank by {-drift}</b>: more objectives closed than opened. '
           if drift < 0 else "Objectives opened and closed at the same rate. ")
        + (f'Evidence moved up the ladder <b>{T["rungs"]} time(s)</b>, which is the only '
           'one of these numbers that means a claim got harder to argue with.'
           if T["rungs"] else
           '<b>No claim moved up the evidence ladder at all</b> in this span, so however '
           'busy the days below look, nothing got more provable.'))

    add("<h3>Worker runs a day, by how they ended</h3>")
    add(_chart_columns(S, [("completed", C_OK, "completed"),
                           ("failed", C_WARN, "failed"),
                           ("killed", C_BAD, "stopped by a limit")],
                       empty_msg="no worker has run on any day in this span"))
    add("<h3>Objectives opened against objectives closed</h3>")
    add(_chart_grouped(S, [("opened", C_ACCENT, "opened"), ("closed", C_OK, "closed")],
                       empty_msg="no objective was opened or closed in this span"))
    add("<h3>What the work reported back</h3>")
    add(_chart_grouped(S, [("rungs", C_OK, "rung moves"), ("findings", C_ACCENT, "findings")],
                       empty_msg="nothing was reported back in this span"))
    add("<h3>Notional spend, running total</h3>")
    add(_chart_line(S, "spend", C_WARN, label="spent so far", unit="$")
        or _chart_columns(S, [("spend", C_WARN, "spend")],
                          empty_msg="nothing has been spent in this span", unit="$"))

    add("<h3>How every run on record ended</h3>")
    add(_chart_split([(O["completed"], C_OK, "completed"),
                      (O["failed"], C_WARN, "failed"),
                      (O["killed"], C_BAD, "stopped by a limit")], O["runs"])
        or '<div class="empty">no worker run has ever been recorded here</div>')

    add("<h3>Day by day</h3>")
    add(_table(["day", "runs", "completed", "failed", "stopped", "spend",
                "opened", "closed", "rungs", "findings"],
               [[f'<span data-v="{r["day"]}">{e(r["day"])}</span>',
                 str(r["runs"]), str(r["completed"]), str(r["failed"]), str(r["killed"]),
                 f'<span data-v="{r["spend"]:.4f}">${r["spend"]:.2f}</span>',
                 str(r["opened"]), str(r["closed"]), str(r["rungs"]), str(r["findings"])]
                for r in reversed(S)]))
    add("</section>")

    # ---- scorecard
    R = data["ratings"]
    sec("ratings", "Lane scorecard", len(R["lanes"]),
        f'A score per lane, and the arithmetic behind it in the open. A lane with '
        f'fewer than {R["min_runs"]} runs on record is left unrated rather than given '
        f'a grade one lucky afternoon would have earned.')
    add('<div class="grid">'
        + _stat(R["rated"], "lanes with enough record to rate")
        + _stat(R["unrated"], "lanes not yet rated",
                "warn" if R["unrated"] else "ok")
        + _stat(f'{R["mean"]:.2f}' if R["mean"] is not None else "n/a", "mean score",
                _band(R["mean"])[1] if R["mean"] is not None else "")
        + _stat(f'${hist["spend_all_time"]:.2f}', "notional spend, all time")
        + "</div>")
    if R["rated"]:
        best = R["lanes"][0]
        worst = [x for x in R["lanes"] if x["rated"]][-1]
        means(
            f'<b>{e(best["lane"])}</b> scores highest at {best["score"]:.2f} '
            f'({e(best["rating"])}); <b>{e(worst["lane"])}</b> lowest at '
            f'{worst["score"]:.2f} ({e(worst["rating"])}). '
            + (f'{R["unrated"]} lane(s) have too little on record to say anything about. '
               if R["unrated"] else "")
            + "A score is a summary of this workspace's own record, not a judgment of "
              "the code: a lane nobody has run is unrated, not bad.")
    else:
        means("<b>Not one lane has enough on record to be rated yet.</b> "
              f'Rating needs {R["min_runs"]} runs and at least two of the four measures; '
              "until then the table below is a list of what is missing, not a ranking.")

    add(_table(["lane", "rating", "score", "evidence", "runs", "finished", "cost/run",
                "spend", "findings", "rungs", "goals done", "streak"],
               [[_lane(r["lane"]),
                 (f'<span class="score {r["cls"]}">{e(r["rating"])}</span>' if r["rated"]
                  else f'<span class="chip" title="{e(r["why_unrated"])}">unrated</span>'),
                 (f'<span data-v="{r["score"]}">{r["score"]:.2f}</span>'
                  if r["score"] is not None else '<span data-v="-1">&mdash;</span>'),
                 f'<span data-v="{LADDER_RUNGS.index(r["rung"]) + 1 if r["rung"] in LADDER_RUNGS else 0}">'
                 f'{e(str(r["rung"] or "nothing on the ladder"))}</span>',
                 str(r["runs"]),
                 (f'<span data-v="{r["success"]}">{r["success"]:.0%}</span>'
                  if r["success"] is not None else '<span data-v="-1">&mdash;</span>'),
                 (f'<span data-v="{r["cost_per_run"]}">${r["cost_per_run"]:.2f}</span>'
                  if r["cost_per_run"] is not None else '<span data-v="-1">&mdash;</span>'),
                 f'<span data-v="{r["spend"]}">${r["spend"]:.2f}</span>',
                 str(r["findings"]), str(r["rungs_moved"]),
                 f'<span data-v="{r["goals"]["done"]}">{r["goals"]["done"]}'
                 f'/{r["goals"]["total"]}</span>',
                 (f'<span class="chip bad" data-v="{r["failure_streak"]}">'
                  f'{r["failure_streak"]} failing</span>' if r["failure_streak"]
                  else f'<span data-v="0">&mdash;</span>')]
                for r in R["lanes"]]))

    add("<h3>What each measure is counting</h3>")
    add(_table(["measure", "what it counts", "best lane"],
               [[e(k), e(v),
                 (lambda ranked: (f'{_lane(ranked[0]["lane"])} '
                                  f'{ranked[0]["parts"][k]:.0%}') if ranked else
                  '<span class="muted">no lane has this measure yet</span>')(
                     sorted((x for x in R["lanes"] if k in x["parts"]),
                            key=lambda x: -x["parts"][k]))]
                for k, v in R["measures"].items()]))
    means("A lane's score is the plain mean of whichever of these four it has a record "
          "for - not a weighted formula, because there is no evidence here for what the "
          "weights should be. Every part is shown so the mean can be argued with.")

    add("<h3>Where the money went</h3>")
    top_spend = max((x["spend"] for x in R["lanes"]), default=0)
    if top_spend > 0:
        add(_table(["lane", "share of spend", "spend", "runs", "per run"],
                   [[_lane(x["lane"]),
                     f'<span class="bar" style="width:160px" data-v="{x["spend"]}">'
                     f'<i style="width:{100 * x["spend"] / top_spend:.1f}%"></i></span>',
                     f'<span data-v="{x["spend"]}">${x["spend"]:.2f}</span>',
                     str(x["runs"]),
                     (f'${x["cost_per_run"]:.2f}' if x["cost_per_run"] is not None
                      else "&mdash;")]
                    for x in R["lanes"] if x["spend"] > 0]))
    else:
        empty("Nothing has been spent in any lane yet.")
    add("</section>")

    # ---- problems
    probs = data["problems"]
    sec("problems", "Problems standing right now", len(probs),
        "Computed from what is on disk. Nothing here was asked of a model.")
    if not probs:
        empty("Nothing is obviously wrong.")
    for o in probs:
        card(e(str(o.get("kind"))), e(str(o.get("text") or ""))
             + (f'<div class="muted">Fix: {e(str(o.get("fix")))}</div>' if o.get("fix") else ""),
             cls="hi" if o.get("severity") == "high" else "mid",
             meta=_lane(o.get("lane")) + chips((o.get("severity"),
                                                "bad" if o.get("severity") == "high" else "warn"))
                  + when(o.get("at")))
    add("</section>")

    # ---- findings
    fs = W["findings"]
    fsum = data["findings_summary"]
    sec("findings", "What the work reported back", len(fs),
        f'{fsum.get("unread", 0)} unread in total, {fsum.get("contradicted", 0)} of them '
        f'contradicting something this stack believed.')
    if fsum.get("contradicted"):
        means(f'<b>{fsum["contradicted"]} finding(s) say something believed here is '
              'false.</b> Until those are read, anything built on top is built on the '
              'thing that was just disproved - which is why a contradicted finding '
              'outranks every other kind.')
    if not fs:
        empty("Nothing was reported back in this window.")
    for f in fs[:80]:
        b = f.get("bearing")
        card(e(str(b or "")), e(str(f.get("text") or "")[:900]),
             cls="hi" if b == "contradicted" else "good" if b == "advanced" else "",
             meta=_lane(f.get("lane"))
                  + chips((b, "bad" if b == "contradicted" else "ok" if b == "advanced" else ""),
                          ("unread", "warn") if not f.get("read_at") else ("", ""))
                  + when(f.get("at")))
    add("</section>")

    # ---- ideas
    #
    # The cheap channel. A worker with its hands in the code notices something
    # that is not its task, says one line about it, and carries on. Nothing here
    # has been judged - it is a list of leads, and its whole value is that they
    # are read together, months of them at once, rather than one at a time in a
    # transcript that gets deleted.
    ids_ = data.get("ideas") or []
    sec("ideas", "Noticed in passing, and never picked up", len(ids_) or None,
        "Written by workers doing something else. None of it is scored, none of it is a "
        "claim, and none of it has been acted on. Solving this report is what turns the "
        "ones worth having into proposals.")
    if not ids_:
        empty("No worker has left anything on this list.")
    for i in ids_[:60]:
        n = int(i.get("seen") or 1)
        card("", e(str(i.get("text") or "")[:600]),
             meta=_lane(i.get("lane"))
                  + chips((f"seen {n}x", "warn") if n > 1 else ("", ""))
                  + when(i.get("at")))
    if len(ids_) > 1:
        means("These are the cheapest thing on this page and the only part of it nobody "
              "has read. An idea repeated by several workers is several independent "
              "people noticing the same gap, which is worth more than any one of them.")
    add("</section>")

    # ---- reading
    rd = data.get("reading") or {}
    items = rd.get("items") or []
    sec("reading", "Worth reading", len(items) or None,
        "Found by web search against what this workspace is doing. "
        "Every link was fetched by the harness before it was printed.")
    if not rd.get("available"):
        empty(str(rd.get("why") or "no search was run"))
    elif not items:
        empty(str(rd.get("why_nothing") or "the search returned nothing worth sending"))
    else:
        add(f'<p class="sub">{rd.get("checked", 0)} link(s) checked, '
            f'{rd.get("dead", 0)} did not resolve.</p>')
        for it in items:
            live_link = it["reachable"]
            title = (f'<a href="{e(it["url"])}" target="_blank" rel="noreferrer noopener">'
                     f'{e(it["title"])}</a>' if live_link else
                     f'<span class="dead">{e(it["title"])}</span>')
            card(title,
                 e(it["what"]) + (f'<div class="muted">{e(it["why"])}</div>' if it["why"] else "")
                 + ("" if live_link else
                    f'<div class="muted">This link did not resolve '
                    f'({e(str(it["check_why"] or it["status"]))}), so it is shown but not '
                    f'linked - the architect may have invented it.</div>'),
                 cls="" if live_link else "mid",
                 meta=chips((it["kind"], ""), (it["lane"], "lane"))
                      + chips(("checked", "ok") if live_link else ("unreachable", "bad")),
                 f=(it["title"] + " " + it["what"] + " " + it["why"]).lower())
    add("</section>")

    # ---- what to do
    A = data["actions"]
    sec("actions", "What to do about it", A["total"],
        "Every item names the record that raised it. Nothing here was invented: if "
        "the record clears, the item stops appearing on the next report by itself.")
    if not A["total"]:
        empty("Nothing on record is asking for a decision. That is either a good "
              "sign or a sign that nothing has been recorded.")
    else:
        means(f'<b>{A["high"]} of {A["total"]}</b> item(s) are the kind that stop work '
              'rather than merely describe it. The four groups below are not '
              'severities - they are four different kinds of answer, and mixing them '
              'is how "revisit the roadmap" ends up in the same list as "a worker is '
              'wedged".')
    for g in A["groups"]:
        if not g["items"]:
            continue
        add(f'<h3>{e(g["title"])} &mdash; {len(g["items"])}</h3>')
        add(f'<p class="sub">{e(g["why"])}</p>')
        for i in g["items"]:
            add(f'<div class="act {e(i["severity"])}" '
                f'data-f="{e((i["what"] + " " + i["why"] + " " + i["where"]).lower())}">'
                f'<div class="hd"><span class="t">{e(i["what"])}</span> '
                + _lane(i["where"])
                + chips((i["severity"], "bad" if i["severity"] == "high"
                         else "warn" if i["severity"] == "medium" else "ok"))
                + f'</div><div class="why">{e(i["why"])}</div>'
                + (f'<div class="ev">on the record: {e(i["evidence"])}</div>'
                   if i["evidence"] else "")
                + "</div>")
    add("</section>")

    # ---- raw
    sec("raw", "Everything, as data", None,
        "The exact record this page was rendered from.")
    add('<p><button id="copyjson">Copy JSON</button></p>'
        f'<pre class="raw" id="rawjson">{e(json.dumps(data, indent=2, default=str))}</pre>')
    add("</section>")

    add(f'<footer>Generated by the [&] console from {e(data["root"])} at {e(at)}. '
        f'Report id <code>{e(data["id"])}</code>. '
        f'Doctrine {e(str(data["doctrine"].get("digest") or "unpinned"))}'
        + (" (changed since you ratified it)" if data["doctrine"].get("drifted") else "")
        + ".</footer></div>")

    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<title>{e(ws['name'])} - report #{data['nth']}</title>"
            f"<style>{REPORT_CSS}</style></head><body>"
            + "".join(out)
            + f"<script>{REPORT_JS}</script></body></html>")


def _stat(value, key: str, cls: str = "") -> str:
    return (f'<div class="stat {cls}"><div class="v">{html.escape(str(value))}</div>'
            f'<div class="k">{html.escape(key)}</div></div>')


def _lane(name) -> str:
    return f'<span class="chip lane">{html.escape(str(name))}</span>' if name else ""


def _dur(s) -> str:
    s = int(s or 0)
    return f"{s // 3600}h {s % 3600 // 60}m" if s >= 3600 else f"{s // 60}m {s % 60}s"


def _reached(rung: str | None, x: str) -> bool:
    """Whether a lane's evidence has got at least as far as rung `x`."""
    if not rung or rung not in LADDER_RUNGS or x not in LADDER_RUNGS:
        return False
    return LADDER_RUNGS.index(x) <= LADDER_RUNGS.index(rung)


def _table(head: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return ""
    th = "".join(f"<th>{html.escape(h)}</th>" for h in head)
    body = "".join(
        '<tr data-f="' + html.escape(" ".join(re.sub("<[^>]+>", " ", c) for c in r).lower())
        + '">' + "".join(f"<td>{c}</td>" for c in r) + "</tr>"
        for r in rows)
    return f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table>"


def make_report(*, web: bool = False) -> dict:
    """Take a report, write it, and remember that it was taken.

    Recording it is what makes the NEXT one mean anything: the window is
    "since the last one", so a report that is not written down would make every
    later report cover the whole history again.
    """
    data = report_data(web=web)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    name = f"report-{data['at'].replace(':', '').replace('-', '')[:15]}-{data['id']}.html"
    path = REPORT_DIR / name
    path.write_text(render_report(data), encoding="utf-8")
    # The same numbers the page was rendered from, kept beside it. The solver
    # reads THIS, not a fresh survey: solving report six against the workspace
    # as it is now would answer a question about today under the heading of a
    # report taken on Tuesday, and every claim it made would be unfalsifiable.
    data_name = name[:-len(".html")] + ".json"
    (REPORT_DIR / data_name).write_text(json.dumps(data, indent=1, sort_keys=True) + "\n",
                                        encoding="utf-8")
    rec = {"id": data["id"], "at": data["at"], "workspace": data["workspace"]["slug"],
           "file": name, "data_file": data_name, "nth": data["nth"],
           "since": (data["since"] or {}).get("id"),
           "web": bool(web and (data["reading"] or {}).get("available")),
           "counts": data["window"]["counts"], "quiet": data["window"]["quiet"],
           "gates": len(data["headed"].get("gates") or []),
           "verdict": (data["headed"].get("state") or {}).get("verdict")}
    store = report_store()
    store.setdefault("reports", []).append(rec)
    _save_reports(store)
    return {"ok": True, **rec, "path": str(path)}


# ---------------------------------------------------------------- the solver
#
# A report is a reading. This is the thing that acts on one.
#
# Everything else that proposes work looks at a slice: a direction review sees
# one finished goal, a sharpen sees one objective, explore sees the lanes and
# their findings. None of them sees the SHAPE - a lane whose reliability is 100%
# and whose delivery is 20%, a gate that has held for three days, a rating that
# fell between report four and report six, thirty-one action items of which
# fourteen are the same stopped question. That shape is what a report is, and
# until now nothing read it back.
#
# What the solver may do is deliberately narrow. It writes PROPOSALS, into the
# same store, scored on the same four axes, held by the same bars and the same
# gates. It does not adopt, it does not edit the mission, it does not touch the
# doctrine, and it does not change a goal. Its recommendations about those are
# text addressed to the operator, because every one of them is rule 6's.

SOLVER_SYSTEM = (
    "You are the consulting architect. You are reading a REPORT on the operator's stack - "
    "not the stack itself. Everything in it was derived from recorded events, so it is what "
    "actually happened, and the numbers are the ones the operator has in front of them.\n\n"
    "You are answering one question: given this shape, what should change?\n\n"
    "Reply with one JSON object and nothing else:\n"
    "{\n"
    '  "reading": "what the shape of this report actually says, in 3-6 sentences. The thing '
    'a person would only see by looking at all of it at once",\n'
    '  "next": [{"objective": "...", "lane": "...", "why": "which line of this report '
    'made you propose it", ' + _SCORE_FIELDS + "}],\n"
    '  "research": [{"question": "...", "why": "...", "settled_by": "...", "lane": "..."}],\n'
    '  "goal_moves": [{"goal_id": "...", "move": "recalculate|improve|close|extend", '
    '"why": "the recorded fact that says so"}],\n'
    '  "direction": [{"change": "what to change about where this is pointed", '
    '"why": "...", "evidence": "the line of the report that says so"}],\n'
    '  "goal_setting": [{"change": "what to change about HOW objectives are chosen, scored, '
    'or gated - the bars, the budgets, the thresholds", "why": "...", "evidence": "..."}],\n'
    '  "picked_up": ["the id of an idea a worker left behind that you turned into an item '
    'of `next` or `research` above"],\n'
    '  "nothing": false, "why_nothing": "if the report says the stack is fine, say so here"\n'
    "}\n\n"
    "Rules:\n"
    "- The section of things workers noticed in passing is the one part of this report "
    "nobody has read. Each line was written by a worker with its hands in that code, doing "
    "something else, and then dropped. That is where an objective nobody has thought of "
    "comes from - and also where a great deal of noise comes from, so judge them: the ones "
    "worth having become items of `next` or `research` like anything else, scored and "
    "sourced, and their ids go in `picked_up`. Leave the rest alone; not picking one up is "
    "not a rejection, and it will still be there next report.\n"
    "- Cite the report. Every item carries the number, row or item it came off. An item you "
    "cannot source to a line of this report is one you brought with you, and it is exactly "
    "the thing a report exists to stop.\n"
    "- `next` and `research` become real proposals that a worker fleet may start tonight. At "
    "most three of each, and fewer is a better answer. Do not restate anything already open, "
    "already proposed, or already turned down - you were given all three lists.\n"
    "- `goal_moves` are about goals that already exist, by id, from the lists you were "
    "given. `recalculate` when the plan was written against a tree that has since moved; "
    "`improve` when the objective itself is what cannot land; `close` when the record says "
    "it is finished or pointless; `extend` when it ran out of budget mid-flight. You are "
    "recommending - the operator clicks.\n"
    "- `direction` and `goal_setting` are the two things you may NOT do and may say. "
    "`direction` is where the stack is pointed. `goal_setting` is the machinery that chooses "
    "objectives: the adopt bars, the escalation limits, the round caps, the budgets. Both "
    "are the operator's, both are written down here, and neither takes effect from this "
    "call. Say the number you would change and what to.\n"
    "- A gate that is up is not a bug. It is the operator being asked something. Say what is "
    "being asked and what answering it would unblock; do not propose routing around it.\n"
    + _SCORE_RULES + "\n"
    "- `nothing` is a real answer. A quiet report with no gates up and no problems standing "
    "should return empty lists, and saying so costs nothing. Filling six fields because "
    "there are six fields is how a report starts generating work instead of reading it."
)


def _report_facts(d: dict) -> str:
    """The report, flattened to the numbers. Not the page - the page is for a person."""
    W, N, H = d.get("window") or {}, d.get("now") or {}, d.get("headed") or {}
    R, HI = d.get("ratings") or {}, d.get("history") or {}
    out = [f"# Report #{d.get('nth')} taken {d.get('at')}",
           f"- covers: since {(d.get('since') or {}).get('at') or 'the beginning'}",
           f"- verdict: {(H.get('state') or {}).get('verdict')}",
           f"- quiet window: {W.get('quiet')}"]

    if W.get("counts"):
        out.append("\n## What moved in the window\n"
                   + "\n".join(f"- {k}: {v}" for k, v in sorted(W["counts"].items())))
    if W.get("ladder"):
        out.append("\n## Rungs moved in the window\n"
                   + "\n".join(f"- ({x.get('lane')}) {x.get('claim', '')[:160]}: "
                               f"{x.get('from')} -> {x.get('to')}" for x in W["ladder"][:20]))

    if H.get("gates"):
        out.append("\n## Gates up right now, holding the whole fleet\n"
                   + "\n".join(f"- **{g['gate']}**: at {g['at']}, limit {g['limit']}. "
                               f"{g['why']}" for g in H["gates"]))
    if H.get("proposals"):
        out.append("\n## Proposals waiting, and what is holding each one\n"
                   + "\n".join(f"- [{p.get('id')}] ({p.get('lane')}) odds {pct(p.get('confidence'))}, "
                               f"need {pct(p.get('need'))}, {usd(p.get('cost_usd'))} — "
                               f"held: {p.get('hold') or 'nothing'} — {p.get('text', '')[:200]}"
                               for p in H["proposals"][:20]))
    if H.get("questions"):
        out.append("\n## Open questions a worker stopped to ask\n"
                   + "\n".join(f"- ({q.get('lane')}) {q.get('text', '')[:250]}"
                               for q in H["questions"][:20]))

    for key, title in (("goals", "Goals running"), ("blocked", "Goals stopped")):
        rows = N.get(key) or []
        if rows:
            out.append(f"\n## {title}\n" + "\n".join(
                f"- [{g.get('id')}] ({g.get('lane')}) {g.get('state')}"
                + (f", stopped on {g['stopped_on']}" if g.get("stopped_on") else "")
                + f", {g.get('rounds', 0)} round(s): {str(g.get('objective'))[:200]}"
                for g in rows[:20]))

    if R.get("lanes"):
        out.append("\n## Scorecard (0-1, derived from the records)\n"
                   + "\n".join(
                       f"- **{r['lane']}**: {r.get('score')} {r.get('grade', '')} — "
                       + ", ".join(f"{k} {v}" for k, v in sorted((r.get("parts") or {}).items()))
                       + f" — rung `{r.get('rung')}`, spent {usd(r.get('spend'))}"
                       for r in R["lanes"]))
    if R.get("measures"):
        out.append("\n## What each measure counts\n"
                   + "\n".join(f"- **{k}**: {v}" for k, v in sorted(R["measures"].items())))
    if HI.get("series"):
        # `days` is how MANY days the window covers; `series` is the days themselves.
        days = [x for x in HI["series"] if any(v for k, v in x.items() if k != "day")]
        if days:
            out.append("\n## Day by day\n" + "\n".join(
                "- " + x["day"] + ": " + ", ".join(f"{k} {v}" for k, v in sorted(x.items())
                                                   if k != "day" and v)
                for x in days[-21:]))

    if d.get("problems"):
        out.append("\n## Standing problems\n"
                   + "\n".join(f"- [{o.get('severity')}] ({o.get('lane') or '-'}) "
                               f"{' '.join(str(o.get('text') or '').split())[:300]} "
                               f"— fix: {o.get('fix')}" for o in d["problems"][:25]))
    acts = [a for grp in ((d.get("actions") or {}).get("groups") or [])
            for a in (grp.get("items") or [])]
    if acts:
        out.append("\n## The action items this report already derived\n"
                   + "\n".join(f"- [{a.get('group')}/{a.get('severity')}] {a.get('what')}"
                               f" — {a.get('why')}" for a in acts[:40]))
    if d.get("ideas"):
        out.append("\n## Things workers noticed in passing and nobody has picked up\n"
                   "Each was written by a worker doing something else. None is scored, "
                   "none is a claim, and none has been acted on.\n"
                   + "\n".join(f"- [{i.get('id')}] ({i.get('lane') or '-'}) "
                               + (f"seen {i['seen']}x " if int(i.get("seen") or 1) > 1 else "")
                               + str(i.get("text") or "")[:400] for i in d["ideas"][:40]))
    if (d.get("findings_summary") or {}).get("unread"):
        fs = d["findings_summary"]
        out.append(f"\n## Findings\n- {fs['unread']} unread, of which "
                   f"{fs.get('contradicted', 0)} contradict something we believed")
    return "\n".join(out)


def _solver_context(d: dict) -> str:
    """The report, plus the three lists that stop it proposing what already exists."""
    known = sorted(config().get("lanes") or {})
    parts = [mission_block().strip(),
             doctrine_block("The doctrine this stack is held to").strip(),
             _report_facts(d),
             "# The lanes that exist\n\n" + ", ".join(f"`{n}`" for n in known)]

    live = [g for g in goals() if g["state"] in ("planning", "running", "blocked")]
    if live:
        parts.append("# Goals open right now, by id - do not propose these again\n\n"
                     + "\n".join(f"- [{g['id']}] ({g['lane']}) {g['state']}: "
                                 f"{g['objective'][:200]}" for g in live))
    op = proposals()
    if op:
        parts.append("# Already proposed and waiting - do not restate them\n\n"
                     + "\n".join(f"- ({p.get('lane')}) [{p.get('kind')}] "
                                 f"{p.get('text', '')[:200]}" for p in op[:30]))
    dis = proposals(state="dismissed")[:20]
    if dis:
        parts.append("# Already turned down by the operator - do not re-propose these\n\n"
                     + "\n".join(f"- ({p.get('lane')}) {p.get('text', '')[:200]}" for p in dis))

    # The numbers `goal_setting` is allowed to argue about, stated rather than
    # implied. Asked to change a bar without being told what it is, a model
    # invents one, and the operator gets a recommendation to set 0.6 to 0.6.
    parts.append("# The dials that currently decide what gets started\n\n"
                 f"- adopt bar (odds a proposal must reach): {adopt_bar():.2f}\n"
                 f"- need bar (how much the mission must want it): {need_bar():.2f}\n"
                 f"- sharpening stops when a round gains less than: {SHARPEN_GAIN_FLOOR}"
                 f" (spend backstop: {SHARPEN_HARD_CAP} rounds)\n"
                 f"- sharpen floor (headroom below which it stops trying): {SHARPEN_FLOOR}\n"
                 f"- auto-adopt: {'on' if direction_store().get('auto_adopt') else 'off'}\n"
                 f"- escalation limits: "
                 + json.dumps(escalate_policy(), sort_keys=True))
    return "\n\n".join(parts)


def report_data_for(rid: str | None = None) -> tuple[dict | None, str]:
    """The data a given report was rendered from, or why it cannot be read."""
    rows = reports()
    if not rows:
        return None, "no report has been taken yet - take one first"
    rec = next((r for r in rows if r.get("id") == rid), None) if rid else rows[0]
    if not rec:
        return None, f"no report {rid!r}"
    name = rec.get("data_file")
    if not name:
        # Written before reports kept their data. Re-deriving would answer about
        # today under an older report's heading, which is the one thing this
        # must not do quietly.
        return None, (f"report #{rec.get('nth')} was taken before reports kept their "
                      f"numbers on disk, so it cannot be solved. Take a new one.")
    path = REPORT_DIR / name
    if not path.exists():
        return None, f"the numbers for report #{rec.get('nth')} are missing from disk"
    try:
        return {**json.loads(path.read_text()), "_rec": rec}, ""
    except (OSError, json.JSONDecodeError) as e:
        return None, f"the numbers for report #{rec.get('nth')} could not be read: {e}"


def solve_report(rid: str | None = None) -> dict:
    """Read a report back and propose what to do about it. One architect call."""
    if not architect_available():
        return {"ok": False, "error": architect_off_reason()}
    d, why = report_data_for(rid)
    if not d:
        return {"ok": False, "error": why}
    rec = d["_rec"]

    model = config().get("consult_model", DEFAULT_CONSULT)
    resp = architect_chat([{"role": "system", "content": SOLVER_SYSTEM},
                           {"role": "user", "content": _solver_context(d)}], model)
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    out = _json_reply(text)
    if not out:
        return {"ok": False, "error": "the architect's answer could not be read as JSON",
                "raw": text[:2000]}

    def _rows(key: str, fields: tuple[str, ...], limit: int = 6) -> list[dict]:
        rows = []
        for x in (out.get(key) or [])[:limit]:
            if not isinstance(x, dict):
                continue
            row = {f: str(x.get(f) or "")[:600] for f in fields}
            if any(row.values()):
                rows.append(row)
        return rows

    by_id = {g["id"]: g for g in goals()}
    moves = [{**m, "lane": by_id[m["goal_id"]]["lane"]}
             for m in _rows("goal_moves", ("goal_id", "move", "why"))
             # A move against a goal that does not exist is not a recommendation,
             # it is a typo the operator would have to check by hand.
             if m["goal_id"] in by_id
             and m["move"] in ("recalculate", "improve", "close", "extend")]

    rev = {"id": "d" + uuid.uuid4().hex[:9], "at": now(), "lane": None,
           "goal_id": None, "goal": "", "kind": "solve", "web": False,
           "report_id": rec.get("id"), "report_nth": rec.get("nth"),
           "assessment": str(out.get("reading") or "")[:2000],
           "ladder": [],
           "goal_moves": moves,
           "direction": _rows("direction", ("change", "why", "evidence")),
           "goal_setting": _rows("goal_setting", ("change", "why", "evidence")),
           "exhausted": bool(out.get("nothing")),
           "why_exhausted": str(out.get("why_nothing") or "")[:600],
           "tokens": (resp.get("usage") or {}).get("total_tokens") or 0}

    known = set(config().get("lanes") or {})
    seen = {_norm_prompt(p.get("text", "")) for p in direction_store().get("proposals", [])}
    fresh, misfiled = [], []
    for n in (out.get("next") or [])[:3]:
        obj = str((n or {}).get("objective") or "").strip()
        if not obj or _norm_prompt(obj) in seen:
            continue
        want = str(n.get("lane") or "").strip()
        if want not in known:
            misfiled.append({"objective": obj[:200], "lane": want})
            continue
        seen.add(_norm_prompt(obj))
        fresh.append({"id": "p" + uuid.uuid4().hex[:9], "at": now(), "kind": "goal",
                      "lane": want, "text": obj[:1000],
                      "why": str(n.get("why") or "")[:1000], "state": "open",
                      "review_id": rev["id"], "source": "solve",
                      "from_report": rec.get("id"), **_scored(n)})
    for r in (out.get("research") or [])[:3]:
        qn = str((r or {}).get("question") or "").strip()
        if not qn or _norm_prompt(qn) in seen:
            continue
        seen.add(_norm_prompt(qn))
        want = str(r.get("lane") or "").strip()
        fresh.append({"id": "p" + uuid.uuid4().hex[:9], "at": now(), "kind": "question",
                      "lane": want if want in known else None,
                      "text": qn[:1000], "why": str(r.get("why") or "")[:1000],
                      "settled_by": str(r.get("settled_by") or "")[:1000],
                      "state": "open", "review_id": rev["id"], "source": "solve",
                      "from_report": rec.get("id")})

    with _DIRECTION_LOCK:
        store = direction_store()
        store.setdefault("proposals", []).extend(fresh)
        store.setdefault("reviews", []).append(rev)
        _save_direction(store)

    # An idea that became a proposal is off the list, because it is now being
    # held to the bars - leaving it open would put the same thing in front of the
    # next report twice, once scored and once not.
    open_ideas = {i["id"] for i in ideas()}
    picked = [x for x in (out.get("picked_up") or [])[:20]
              if isinstance(x, str) and x in open_ideas]
    if picked:
        close_ideas(picked, "picked")
    rev["picked_up"] = picked

    ngoal = sum(1 for p in fresh if p["kind"] == "goal")
    nq = len(fresh) - ngoal
    add_note(f"solved report #{rec.get('nth')}: " + rev["assessment"][:400]
             + (f" — {ngoal} objective(s) proposed" if ngoal else "")
             + (f", {nq} open question(s)" if nq else "")
             + (f", {len(moves)} move(s) on open goals" if moves else "")
             + (f", {len(rev['direction'])} change(s) of direction" if rev["direction"] else "")
             + (f", {len(rev['goal_setting'])} change(s) to how goals are set"
                if rev["goal_setting"] else "")
             + (" — nothing to do: " + rev["why_exhausted"][:200] if rev["exhausted"] else ""))

    rev["proposals"] = fresh
    rev["misfiled"] = misfiled
    rev["ok"] = True
    # Nothing is adopted, no goal is moved, no dial is turned. Every one of those
    # is a click, and this call is a reading.
    return rev


# ------------------------------------------------------------- the settler
#
# The contradictions gate had no worker.
#
# A finding that says "something we believed is false" stops the fleet, and
# rightly - building on a disproved claim is the one thing the ladder exists to
# prevent. But nothing was ever assigned to SETTLE one. They accumulated, the
# gate held, and the only way out was the operator reading eighteen paragraphs
# of review prose and deciding what each one implied.
#
# Worse, most of them could not be settled at all. Six of the first eighteen say
# some version of "this claim was recorded at a rung it did not earn" - and
# until `retract_rung` existed there was no way to lower a rung, because
# `lane_rungs` takes the maximum ever recorded. The architect could see the
# mistake, file it, and watch the false rung go on being fed to every proposal
# scored in every lane. The gate was not being difficult; it was pointing at a
# hole.
#
# So this pass reads each contradiction against the record and routes it. What
# it may do is narrow, and every route ends in something PERFORMED:
#
#   retract   - the claim was overstated. Name the ladder entry; the harness
#               checks it was really recorded and takes the rung back.
#   supersede - a later finding already refutes this one. Name it; the harness
#               checks it exists and is actually later.
#   work      - it needs code. The harness files a PROPOSAL, scored and held by
#               the same bars as anything else. Not a task: adopting is rule 6's.
#   keep      - it cannot be settled from the record. It stays unread.
#
# A verdict whose consequence the harness cannot perform is downgraded to
# `keep`. That is the load-bearing rule: no finding is ever closed by an
# opinion, only by a thing that happened and can be named.

SETTLE_SYSTEM = (
    "You are the consulting architect, settling CONTRADICTIONS on the operator's stack.\n\n"
    "A contradiction is a finding that says something the stack believed and acted on is "
    "false. Each one is quoted to you exactly as it was filed. They are blocking every "
    "proposal in every lane, so they have to be resolved - but resolving one means saying "
    "what it IMPLIES, not agreeing that it was filed.\n\n"
    "For each finding give exactly one verdict:\n\n"
    "- `retract`: the finding says a claim was recorded at a rung it did not earn. Give the "
    "ladder entry VERBATIM from the list of recorded entries below - `claim` copied exactly, "
    "and `to` the rung it was wrongly moved to. The rung will be taken back. Use this "
    "whenever a finding disputes evidence rather than code.\n"
    "- `correct`: the finding disproves a claim that was made and acted on, but which is NOT "
    "on the recorded ladder - a sentence in a worker's report, or a done-condition. Give "
    "`claim` as the false statement, in your own words, in one sentence. Nothing moves on the "
    "ladder, because it never earned a rung; the correction is recorded against the lane and "
    "shown to every architect scoring anything in it, so it stops being repeated. Use this "
    "instead of `keep` whenever the only reason you cannot `retract` is that the claim was "
    "never a rung.\n"
    "- `supersede`: a LATER finding in this list already refutes or replaces this one - it "
    "checked the same thing and got a different answer. Give `by` as that finding's id. It "
    "must be later than the one you are settling.\n"
    "- `work`: settling it needs code or a real run. Give an `objective` and a `lane`, and "
    "score it. This becomes a proposal in the ordinary queue; it is not started.\n"
    "- `keep`: you cannot settle it from what you have been given. Say why. This is a real "
    "answer and it is better than a guess - the finding stays open and the operator reads it.\n\n"
    "Rules:\n"
    "- Do not invent a ladder entry. If the claim you want to retract is not in the recorded "
    "list, the verdict is `correct`, not `retract` and not `keep`.\n"
    "- Several findings often say the same thing about the same claim. Retract the entry once "
    "and `supersede` the rest onto the finding that made the point best.\n"
    "- A finding being old is not a reason to close it.\n"
    "- Settling is not agreeing. If a finding is wrong, say so in `why` and `keep` it.\n"
    "- Most findings carry TWO claims: one that later evidence resolves, and one that is left "
    "over. Settle the first with your verdict and put the second in `residue` with an "
    "`objective` - that files a proposal for the leftover. A finding is not `keep` just "
    "because part of it is unfinished.\n\n"
    "Reply with one JSON object and nothing else:\n"
    "{\n"
    '  "reading": "what these contradictions add up to, in 3-6 sentences: what the stack '
    'has been getting wrong, not a list of the findings",\n'
    '  "settle": [{"finding": "<id>", "verdict": "retract|correct|supersede|work|keep", '
    '"why": "the reason, addressed to the operator",\n'
    '      "claim": "(retract) the recorded ladder entry, copied exactly — '
    '(correct) the false statement, in one sentence",\n'
    '      "to": "(retract) the rung it was wrongly moved to",\n'
    '      "by": "(supersede) the id of the later finding that settles it",\n'
    '      "objective": "(work) what to do about it", "lane": "(work) which lane", '
    + _SCORE_FIELDS + ",\n"
    '      "residue": {"objective": "what is LEFT OVER after that verdict, if anything", '
    '"lane": "...", "why": "...", ' + _SCORE_FIELDS + "}}]\n"
    "}"
)


def _settle_context(rows: list[dict]) -> str:
    """The findings, the ladder entries they might be about, and the goals."""
    cfg = config()
    out = [f"# The mission\n\n{cfg.get('mission') or '(none set)'}"]

    gone = retractions()
    entries = []
    for r in direction_store().get("reviews", []):
        for e in (r.get("ladder") or []):
            if not r.get("lane") or e.get("to") not in LADDER_RUNGS:
                continue
            if _rung_key(r.get("lane"), e.get("claim"), e.get("to")) in gone:
                continue
            entries.append(f"- **{r.get('lane')}** `{e.get('from')}` -> `{e.get('to')}`: "
                           f"{str(e.get('claim') or '')[:400]}")
    if entries:
        out.append("# Every rung on record, and the claim it was moved for\n\n"
                   "To `retract` one, copy its claim text exactly and give its `to` rung. "
                   "Anything not on this list cannot be retracted - but note that this is "
                   "the list of RUNGS, not the list of claims. Most of what the stack "
                   "believes was never recorded here at all, and a finding that disproves "
                   "one of those is `correct`, not `keep`.\n\n" + "\n".join(entries))
    fixed = corrections()
    if fixed:
        out.append("# Claims already corrected\n\n"
                   "Do not correct these again; if a finding only repeats one of them, it "
                   "is `supersede` or `keep`.\n\n"
                   + "\n".join(f"- **{c.get('lane')}**: {str(c.get('claim') or '')[:300]}"
                               for c in fixed))
    out.append("# The highest rung each lane is currently credited with\n\n"
               + "\n".join(f"- {k}: `{v}`" for k, v in sorted(lane_rungs().items()))
               + "\n\nThese are what every proposal in every lane is currently scored "
                 "against. A rung that was not earned is inflating all of them.")

    by_goal = {g["id"]: g for g in goals()}
    seen_goals = []
    for f in rows:
        g = by_goal.get(f.get("goal_id") or "")
        if g and g["id"] not in [x["id"] for x in seen_goals]:
            seen_goals.append(g)
    if seen_goals:
        out.append("# The goals these findings came out of\n\n"
                   + "\n".join(f"- `{g['id']}` **{g['lane']}** [{g.get('state')}]: "
                               f"{str(g.get('objective') or '')[:400]}" for g in seen_goals))

    lines = []
    for f in sorted(rows, key=lambda x: x.get("at") or ""):
        lines.append(f"## `{f['id']}` — {f.get('at')} — {f.get('lane')} — filed by "
                     f"{f.get('source')} — goal `{f.get('goal_id') or 'none'}`\n\n"
                     + str(f.get("text") or ""))
    out.append("# The contradictions, oldest first\n\n"
               "Oldest first on purpose: a later one often checked what an earlier one "
               "asserted.\n\n" + "\n\n".join(lines))
    return "\n\n".join(out)


def settle_findings(lane: str | None = None, limit: int = 24) -> dict:
    """Read the open contradictions and settle the ones that can be settled."""
    if not architect_available():
        return {"ok": False, "error": architect_off_reason()}
    rows = [f for f in findings(lane=lane, unread_only=True)
            if f.get("bearing") == "contradicted"][:limit]
    if not rows:
        return {"ok": False, "error": "no unread contradictions to settle"}

    model = config().get("consult_model", DEFAULT_CONSULT)
    resp = architect_chat([{"role": "system", "content": SETTLE_SYSTEM},
                           {"role": "user", "content": _settle_context(rows)}], model)
    text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    out = _json_reply(text)
    if not out:
        return {"ok": False, "error": "the architect's answer could not be read as JSON",
                "raw": text[:2000]}

    by_id = {f["id"]: f for f in rows}
    known = set(config().get("lanes") or {})
    seen = {_norm_prompt(p.get("text", "")) for p in direction_store().get("proposals", [])}
    rid = "d" + uuid.uuid4().hex[:9]
    settled, kept, fresh, retracted, corrected = [], [], [], [], []

    def file_proposal(spec: dict, want_lane: str | None, why_text: str, src: str,
                      fid: str) -> dict | None:
        """Put an objective into the ordinary queue, or refuse to. Nothing starts."""
        obj = str((spec or {}).get("objective") or "").strip()
        want = str((spec or {}).get("lane") or "").strip() or want_lane
        if not obj or want not in known or _norm_prompt(obj) in seen:
            return None
        seen.add(_norm_prompt(obj))
        p = {"id": "p" + uuid.uuid4().hex[:9], "at": now(), "kind": "goal",
             "lane": want, "text": obj[:1000],
             "why": (str(spec.get("why") or "") or why_text)[:1000], "state": "open",
             "review_id": rid, "source": src, "from_finding": fid, **_scored(spec)}
        fresh.append(p)
        return p

    for v in (out.get("settle") or [])[:limit]:
        if not isinstance(v, dict):
            continue
        f = by_id.get(str(v.get("finding") or ""))
        if not f:
            continue
        verdict = str(v.get("verdict") or "").strip().lower()
        why = str(v.get("why") or "")[:1000]
        done = None

        if verdict == "retract":
            r = retract_rung(f.get("lane"), str(v.get("claim") or ""),
                             str(v.get("to") or ""), why, finding_id=f["id"])
            # No such entry was ever recorded, so there is nothing to take back
            # and the finding is not settled by saying there was.
            if r:
                retracted.append(r)
                done = {"how": "retract", "why": why, "retraction_id": r["id"],
                        "claim": r["claim"], "to": r["to"]}
            else:
                # Falls through to `correct` rather than to `keep`. The claim is
                # still false and still worth not repeating; the only thing that
                # is missing is a rung to take away.
                verdict = "correct"

        if verdict == "correct":
            c = record_correction(f.get("lane"), str(v.get("claim") or ""), why,
                                  finding_id=f["id"], goal_id=f.get("goal_id"))
            if c:
                corrected.append(c)
                done = {"how": "correct", "why": why,
                        "correction_id": c["id"], "claim": c["claim"]}
            else:
                why = ("no claim was named, so there was nothing to correct — " + why)

        elif verdict == "supersede":
            later = by_id.get(str(v.get("by") or ""))
            # It has to exist, be a different finding, and actually be later:
            # "a newer one settles it" is only a reason if one is newer.
            if later and later["id"] != f["id"] and (later.get("at") or "") > (f.get("at") or ""):
                done = {"how": "supersede", "why": why, "by": later["id"]}
            else:
                why = ("the finding named as superseding it is not a later finding on "
                       "record — " + why)

        elif verdict == "work":
            p = file_proposal(v, f.get("lane"), why, "settle", f["id"])
            if p:
                done = {"how": "work", "why": why, "proposal_id": p["id"]}
            else:
                why = (("no proposal could be filed for it — " + why)
                       if str(v.get("objective") or "").strip() else why)

        # Whatever the verdict was, what is LEFT of the finding after it. Most
        # of these carry two claims - one that later evidence resolves and one
        # that does not - and with no way to say that, a finding was kept whole
        # because a fragment of it was unfinished. Three of them sat open for a
        # day blocking every proposal on the stack for exactly that reason.
        if done:
            left = file_proposal(v.get("residue") or {}, f.get("lane"), why,
                                 "settle-residue", f["id"])
            if left:
                done["residue_id"] = left["id"]
                done["residue"] = left["text"][:300]

        if done:
            if settle_finding(f["id"], {**done, "at": now(), "review_id": rid}):
                settled.append({"finding": f["id"], **done})
        else:
            kept.append({"finding": f["id"], "why": why or "left for the operator to read",
                         "text": str(f.get("text") or "")[:300]})

    rev = {"id": rid, "at": now(), "lane": lane, "goal_id": None, "goal": "",
           "kind": "settle", "web": False,
           "assessment": str(out.get("reading") or "")[:2000],
           "ladder": [], "goal_moves": [], "direction": [], "goal_setting": [],
           "settled": settled, "kept": kept, "retractions": retracted,
           "corrections": corrected,
           "considered": [f["id"] for f in rows],
           "tokens": (resp.get("usage") or {}).get("total_tokens") or 0}
    with _DIRECTION_LOCK:
        store = direction_store()
        store.setdefault("proposals", []).extend(fresh)
        store.setdefault("reviews", []).append(rev)
        _save_direction(store)

    add_note(f"settled {len(settled)} of {len(rows)} contradiction(s): " + rev["assessment"][:300]
             + (f" — {len(retracted)} rung(s) retracted" if retracted else "")
             + (f", {len(corrected)} claim(s) corrected without moving a rung" if corrected else "")
             + (f", {len(fresh)} proposal(s) filed" if fresh else "")
             + (f", {len(kept)} left open for you" if kept else ""))

    rev["proposals"] = fresh
    rev["rungs_now"] = lane_rungs()
    rev["ok"] = True
    return rev


def settle_contradictions() -> dict | None:
    """Settle the contradictions that are holding the whole fleet, if any are.

    The contradictions gate is workspace-wide: at the limit, no lane adopts, no
    lane explores, nothing starts anywhere. That is right - building on a claim
    that was just disproved is the one thing worth stopping everything for - but
    it was only ever cleared by a person pressing a button, and findings arrive
    from every worker that finishes. So the steady state of an unattended fleet
    is not "running": it is three contradictions deep and stopped, and the
    longer it runs the more certainly it gets there. Measured twice in one
    hour, from a standing start both times.

    Clearing the gate is NOT what this does, and the difference is the whole
    justification. `settle_findings` settles a finding by performing a
    consequence - retracting the rung that was not earned, superseding a
    finding another one already covers, recording a false claim so every future
    scoring call is told not to repeat it, filing the leftover as work - and it
    refuses, keeping the finding open, whenever the consequence is one the
    harness cannot actually perform. So the gate comes down exactly when the
    thing it was demanding has been done, and stays up when it has not. Marking
    findings read would also clear it, and would be a lie.

    Runs only while the gate is UP, which is also what bounds it: there is no
    schedule here, and nothing to tune. The one hazard left is a finding nobody
    can settle - it holds the gate open forever, and a retry every tick would be
    an architect call a minute for an answer that cannot change - so an
    unchanged set is not asked about twice. A NEW contradiction is a new
    question and does get asked.
    """
    if not direction_store().get("auto_adopt"):
        return None
    if not any(e.get("gate") == "contradictions" for e in escalations()):
        return None
    open_ids = sorted(f["id"] for f in findings(unread_only=True)
                      if f.get("bearing") == "contradicted")
    if not open_ids:
        return None
    d = direction_store()
    if not set(open_ids) - set(d.get("settle_tried") or []):
        return None
    # Recorded BEFORE the call, and as the ids asked about rather than a
    # timestamp: what makes a retry pointless is that the question is the same,
    # not that it was asked recently. A call that dies partway through has still
    # been paid for and may still have settled something.
    d["settle_tried"] = open_ids
    _save_direction(d)
    out = settle_findings()
    return {"settled": len(out.get("settled") or []),
            "kept": len(out.get("kept") or []),
            "considered": len(open_ids),
            "ok": bool(out.get("ok")), "error": out.get("error")}


def cmd_packet(args):
    zpath, brief = build_packet(args.lane, args.question or "(no question stated)", args.file or [])
    print(f"packet: {zpath}  ({zpath.stat().st_size} bytes)")
    print("forward this to GPT-5.6 manually, or run ./amp ask to send it automatically")
    return 0


def cmd_ask(args):
    cfg = config()
    model_key = args.model or cfg.get("consult_model", DEFAULT_CONSULT)
    print(f"consulting {CONSULT_MODELS.get(model_key, model_key)} ...")
    t0 = time.time()
    c = open_consult(args.lane, args.question, model_key=model_key, extra_files=args.file or [])
    print("=" * 78)
    print(last_ruling(c))
    print("=" * 78)
    print(f"consult {c['id']}   ({time.time() - t0:.0f}s, {c['cost_tokens']} tokens)")
    print("saved: {}".format(RULING_DIR / "{}-{}.md".format(c["lane"], c["id"])))
    if c.get("needs"):
        print("\nit still needs:\n  " + "\n  ".join(c["needs"]))
    return 0


def cmd_serve(args):
    """Launch the browser console in code/."""
    server = HERE / "server.py"
    if not server.exists():
        die(f"console not found at {server}")
    argv = [sys.executable, str(server), "--port", str(args.port)]
    if args.open:
        argv.append("--open")
    os.execv(sys.executable, argv)


def cmd_credits(args):
    key = openrouter_key()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/credits", headers={"Authorization": f"Bearer {key}"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        d = json.loads(r.read().decode())["data"]
    left = d["total_credits"] - d["total_usage"]
    print(f"OpenRouter credit remaining: ${left:.2f}")
    for k, m in CONSULT_MODELS.items():
        print(f"  {k:<6} {m}")
    return 0


def cmd_db(args):
    """The mirror from a terminal, so a backup never needs the console running."""
    if args.sub == "status":
        s = store.status()
        print(f"database  {s['path']}")
        if not s["exists"]:
            print("  not created yet - run `amp db backup`")
            return 0
        print(f"  {s['docs']} documents, {s['revisions']} revisions, "
              f"{s['bytes'] / 1e6:.2f} MB on disk")
        print(f"  mirror {'on' if s['settings']['mirror'] == '1' else 'OFF'}, "
              f"history {s['settings']['history_keep']} per document, "
              f"cursor {s['cursor']}, device {s['device']}")
        for w in s["workspaces"]:
            print(f"    {w['workspace']:<12} {w['docs']:>4} docs  {w['bytes'] / 1e6:.2f} MB")
        if s["failures"]["n"]:
            print(f"  ! {s['failures']['n']} write(s) failed, last: {s['failures']['last']}")
        return 0
    if args.sub == "backup":
        r = store.backup()
        print(f"mirrored {r['written']} changed of {r['scanned']} files"
              + (f", {r['failed']} failed" if r["failed"] else ""))
        return 0
    if args.sub == "verify":
        v = store.verify()
        print(f"{v['current']} current, {len(v['stale'])} stale, "
              f"{len(v['missing'])} not mirrored, {len(v['held'])} held after deletion")
        for label, rows in (("stale", v["stale"]), ("missing", v["missing"])):
            for rel in rows[:20]:
                print(f"  {label:<8} {rel}")
        return 0 if v["clean"] else 1
    if args.sub == "prune":
        r = store.prune(args.keep)
        print(f"removed {r['removed']} revisions, {r['kept']} kept "
              f"({r['keep']} per document)")
        store.compact()
        return 0
    if args.sub == "export":
        dest = Path(args.dest or f"amp-backup-{datetime.now():%Y%m%d-%H%M%S}.db")
        store.snapshot(dest)
        print(f"wrote {dest} ({dest.stat().st_size / 1e6:.2f} MB)")
        return 0
    if args.sub == "show":
        b = store.body(args.path, args.seq)
        if b is None:
            die(f"nothing held at {args.path}"
                + (f" revision {args.seq}" if args.seq else ""))
        sys.stdout.write(b.decode(errors="replace"))
        return 0
    if args.sub == "history":
        rows = store.history(args.path, args.limit)
        if not rows:
            print(f"no history for {args.path}")
            return 1
        for r in rows:
            print(f"  {r['seq']:>6}  {r['written_at']}  {r['bytes']:>9} B  {r['sha'][:12]}")
        return 0
    return 1


# ---------------------------------------------------------------- cli


def main(argv=None):
    p = argparse.ArgumentParser(prog="amp", description="lead-manager harness for the [&] stack")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="preflight: worker CLIs, keys, lane bindings").set_defaults(
        func=cmd_doctor
    )
    sub.add_parser("lanes", help="list lanes").set_defaults(func=cmd_lanes)
    sub.add_parser("board", help="show the board").set_defaults(func=cmd_board)
    sub.add_parser("credits", help="OpenRouter credit remaining").set_defaults(func=cmd_credits)

    sv = sub.add_parser("serve", help="launch the browser console (code/)")
    sv.add_argument("--port", type=int, default=8787)
    sv.add_argument("--open", action="store_true")
    sv.set_defaults(func=cmd_serve)

    lane = sub.add_parser("lane", help="manage lanes").add_subparsers(dest="sub", required=True)
    a = lane.add_parser("add", help="add a lane")
    a.add_argument("name")
    a.add_argument("--repo", help="owner/name (inferred from git origin if omitted)")
    a.add_argument("--path", help="path under ProjectAmp2 (defaults to name)")
    a.add_argument("--branch", default="main")
    a.add_argument("--backend", choices=BACKENDS, default=DEFAULT_BACKEND)
    a.add_argument("--env", help="codex cloud environment id")
    a.add_argument("--replace", action="store_true", help="repoint a lane that already exists")
    a.set_defaults(func=cmd_lane_add)

    e = lane.add_parser("env", help="bind a codex env id to a lane")
    e.add_argument("name")
    e.add_argument("env_id")
    e.set_defaults(func=cmd_lane_env)

    lb = lane.add_parser("backend", help="choose a lane's worker backend")
    lb.add_argument("name")
    lb.add_argument("backend", choices=BACKENDS)
    lb.set_defaults(func=cmd_lane_backend)

    d = sub.add_parser("dispatch", help="send a task to a lane's worker")
    d.add_argument("lane")
    d.add_argument("prompt", nargs="?", default="")
    d.add_argument("--prompt-file")
    d.add_argument("--backend", choices=BACKENDS, help="override the lane's backend")
    d.add_argument("--attempts", type=int, default=1, help="codex only: best-of-N")
    d.add_argument("--branch")
    d.add_argument("--model", help=f"claude only, default {DEFAULT_CLAUDE_MODEL}")
    d.add_argument("--budget", type=float, help=f"claude only, USD cap (default {DEFAULT_BUDGET_USD:.2f})")
    d.add_argument(
        "--shared-tree",
        action="store_true",
        help="claude only: edit the real checkout instead of an isolated worktree",
    )
    d.set_defaults(func=cmd_dispatch)

    rp = sub.add_parser("reply", help="answer a claude worker mid-task (resumes its session)")
    rp.add_argument("lane")
    rp.add_argument("prompt")
    rp.add_argument("--budget", type=float)
    rp.add_argument("--session", help="a task or session id (a prefix is enough) - "
                                      "defaults to the newest, which is rarely the "
                                      "one that was cut off")
    rp.set_defaults(func=cmd_reply)

    po = sub.add_parser("poll", help="refresh the board from Codex Cloud")
    po.add_argument("--lane")
    po.set_defaults(func=cmd_poll)

    df = sub.add_parser("diff", help="show a task's diff")
    df.add_argument("lane")
    df.add_argument("--task-id")
    df.add_argument("--attempt", type=int)
    df.set_defaults(func=cmd_diff)

    ap = sub.add_parser("apply", help="apply a task's diff into the local worktree")
    ap.add_argument("lane")
    ap.add_argument("--task-id")
    ap.add_argument("--attempt", type=int)
    ap.set_defaults(func=cmd_apply)

    pk = sub.add_parser("packet", help="build a GPT-5.6 packet zip (manual forward)")
    pk.add_argument("lane")
    pk.add_argument("-q", "--question")
    pk.add_argument("-f", "--file", action="append")
    pk.set_defaults(func=cmd_packet)

    ak = sub.add_parser("ask", help="build a packet AND send it to GPT-5.6")
    ak.add_argument("lane")
    ak.add_argument("-q", "--question", required=True)
    ak.add_argument("-f", "--file", action="append")
    ak.add_argument("--model", choices=list(CONSULT_MODELS), help="default: terra")
    ak.set_defaults(func=cmd_ask)

    lg = sub.add_parser("login", help="connect a long-lived Claude worker token")
    lg.set_defaults(func=cmd_login)

    dbp = sub.add_parser("db", help="the SQLite mirror of everything on disk")
    dbs = dbp.add_subparsers(dest="sub", required=True)
    dbs.add_parser("status", help="what the mirror holds")
    dbs.add_parser("backup", help="sweep the state directory into the mirror")
    dbs.add_parser("verify", help="compare the mirror against the files")
    pr = dbs.add_parser("prune", help="drop old revisions and compact")
    pr.add_argument("--keep", type=int, help="revisions per document")
    ex = dbs.add_parser("export", help="write a consistent copy of the database")
    ex.add_argument("dest", nargs="?")
    hi = dbs.add_parser("history", help="revisions held for one document")
    hi.add_argument("path", help="path relative to the state root, e.g. .board.json")
    hi.add_argument("--limit", type=int, default=50)
    sh = dbs.add_parser("show", help="print a held document (does not write disk)")
    sh.add_argument("path")
    sh.add_argument("--seq", type=int, help="a revision, default the current one")
    dbp.set_defaults(func=cmd_db)

    args = p.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
